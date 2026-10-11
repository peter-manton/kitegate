"""The photo tool, run by the gateway itself rather than by a front end.

For a children's model the gateway offers the model one tool, `find_photo`,
and runs it here when the model asks. The child's front end never sees the
round trip: it sends a question and gets one screened answer back, with a
photograph in it when one helped. So nothing has to be installed in Open
WebUI, LibreChat or anything else, and every front end gets the same tool.

    child ─► front end ─► gateway ─► model
                             │  ◄── find_photo("tiger")
                             ├─► Wikipedia (the subject string only)
                             │  ──► result ─► model ─► answer with picture
                             ▼
                   screened, picture scanned ─► child

`kitegate.photos` holds the providers (which request to make, how to read
the answer); this holds the HTTP, the throttle and the cache. Same split as
`images` / `imagescan`.

WHAT LEAVES THE GATEWAY
Only the subject string, sent to the provider with a descriptive User-Agent.
No identity, no conversation. The subject is chosen by the model, so it is
screened before it goes: the core text rules (by the caller, as model output),
the imagery list in photos.py, the core PII patterns, and a length cap that
keeps "a whole question" from being sent where a noun phrase belongs.

THIS DOES NOT MAKE A PICTURE SAFE TO SHOW
It chooses a careful SOURCE. Whether a picture is displayed is decided by the
image policy on the way back to the child, which removes it if it fails. The
two halves are independent on purpose.

RATE LIMITING
Measured against the live MediaWiki API: a ten-subject run issuing several
requests per subject earned HTTP 429, and a throttled request that reads as
"no photograph found" makes the tool look randomly unreliable. Hence one
request per subject in the common case, a minimum gap between requests, one
bounded retry, a cache, and a 429 reported to the model as "busy" rather than
"not found". Wikimedia sends no Retry-After, so a short backoff is all there is.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from ..core import PII
from ..photos import (BLOCKED_SUBJECT, REFUSED_SUBJECT_REPLY, Photo, PhotoProvider,
                      get_providers, next_search_title)

logger = logging.getLogger("kitegate.photos")

TOOL_NAME = "find_photo"
#: Tool rounds before the model is told to answer in words. One is the normal
#: case; the second lets it retry with a better subject after "not found".
MAX_ROUNDS = 2
#: Lookups honoured per model turn. A model asking for ten pictures of one
#: animal gets three and is told the rest are unavailable.
MAX_CALLS = 3
#: A subject is a noun phrase. Anything longer is a question or a sentence,
#: and is not sent.
MAX_SUBJECT = 80

USER_AGENT = "kitegate-gateway/1.0 (self-hosted children's encyclopedia; +https://github.com/kitegate)"
THUMB_WIDTH = 640
TIMEOUT = 12.0
_MAX_CACHED = 512

NO_SUBJECT_REPLY = "No subject was given, so no picture was looked up."
LONG_SUBJECT_REPLY = ("That subject is too long. Pass a short noun phrase such as \"tiger\" "
                      "or \"Great Pyramid of Giza\", or answer in words.")
UNAVAILABLE_REPLY = "That tool is not available. Answer in words."
TOO_MANY_REPLY = "Only a few pictures can be looked up for one answer. Answer with the ones you have."
BUSY_REPLY = ("The picture library is busy at the moment and would not answer. This is "
              "nothing to do with what was asked. Answer the question in words, and mention "
              "that the picture could not be fetched just now and is worth trying again shortly.")


def not_found_reply(subject: str) -> str:
    return (f"No photograph was found for '{subject}'. "
            "Answer in words instead, and do not link to any other website.")


def render(photo: Photo) -> str:
    return (f"Photograph found for '{photo.title}' (from {photo.source}).\n\n"
            f"![{photo.title}]({photo.image_url})\n\n"
            f"{photo.caption}\n\n"
            "Include the image markdown above in your answer, exactly as given, and "
            "describe the picture to the child in your own simple words.")


def tool_spec(providers: Sequence[PhotoProvider]) -> Dict[str, Any]:
    """The OpenAI tool definition the model is offered."""
    if all(not p.general for p in providers):
        scope = " Only " + " and ".join(p.topic for p in providers) + " subjects have pictures."
    else:
        scope = ""
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": (
            "Find one real photograph of a subject, with a short caption, from a curated "
            "source (Wikipedia article lead images). Use it when a real picture would help "
            "explain something: an animal, a planet, a famous building, a country's flag, a "
            "historical object. Not for every answer. Returns markdown containing the image, "
            "which you should include in your answer. If no photograph is found, say so "
            "plainly and answer in words." + scope),
        "parameters": {
            "type": "object",
            "properties": {"subject": {
                "type": "string",
                "description": ("The thing to photograph, as a short noun phrase, e.g. "
                                "\"tiger\", \"Jupiter\", \"Great Pyramid of Giza\". "
                                "Never a whole question."),
            }},
            "required": ["subject"],
        },
    }}


class RateLimited(Exception):
    """A provider returned 429 twice. Distinct from "no image found" on purpose."""


def subject_of(call: dict) -> str:
    """The subject argument of one tool call, or "" if it cannot be read."""
    raw = ((call or {}).get("function") or {}).get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ""
    value = args.get("subject") if isinstance(args, dict) else None
    return value.strip() if isinstance(value, str) else ""


def refused(subject: str) -> Optional[str]:
    """The reply for a subject that must not leave the gateway, or None."""
    if not subject:
        return NO_SUBJECT_REPLY
    if len(subject) > MAX_SUBJECT:
        return LONG_SUBJECT_REPLY
    if BLOCKED_SUBJECT.search(subject):
        return REFUSED_SUBJECT_REPLY
    # Response-side screening has no PII tier (it false-positives on
    # encyclopedia text), but this string goes to a third party, so a phone
    # number or postcode in it is refused here.
    if any(pattern.search(subject) for _, pattern in PII):
        return REFUSED_SUBJECT_REPLY
    return None


class PhotoTool:
    """Looks up one photograph per subject from the configured providers."""

    def __init__(self, providers: Sequence[str], client: httpx.AsyncClient) -> None:
        self.providers: List[PhotoProvider] = get_providers(list(providers))
        self.client = client
        self.spec = tool_spec(self.providers)
        # Instance attributes rather than constants so a test can set them to
        # zero instead of sleeping.
        self.min_gap = 1.0
        self.retry_sleep = 2.0
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        # (provider, endpoint, subject) -> Photo or None
        self._cache: Dict[Tuple[str, str, str], Optional[Photo]] = {}

    # -- HTTP ---------------------------------------------------------------
    async def _throttle(self) -> None:
        async with self._lock:
            gap = time.monotonic() - self._last_request_at
            if gap < self.min_gap:
                await asyncio.sleep(self.min_gap - gap)
            self._last_request_at = time.monotonic()

    async def _get(self, url: str, params: dict) -> Optional[dict]:
        for attempt in (1, 2):
            await self._throttle()
            try:
                r = await self.client.get(url, params=params or None,
                                          headers={"user-agent": USER_AGENT}, timeout=TIMEOUT)
            except httpx.HTTPError:
                logger.warning("kitegate: photo provider unreachable: %s", url)
                return None
            if r.status_code == 429:
                if attempt == 1:
                    await asyncio.sleep(self.retry_sleep)
                    continue
                raise RateLimited(url)
            if r.status_code != 200:
                logger.warning("kitegate: photo provider returned HTTP %s: %s", r.status_code, url)
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    def _remember(self, key: Tuple[str, str, str], photo: Optional[Photo]) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= _MAX_CACHED:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = photo

    async def _find(self, provider: PhotoProvider, endpoint: str, subject: str) -> Optional[Photo]:
        """One provider endpoint. Raises RateLimited."""
        request = provider.lookup(endpoint, subject, THUMB_WIDTH)
        data = await self._get(request.url, request.params)
        if data is None:
            return None
        title, photo = provider.read_lookup(data, endpoint)
        if photo is not None:
            return photo
        # Only now spend a second request: no such article, or a
        # disambiguation page, and search is the way through both.
        search = provider.search(endpoint, subject)
        if search is None:
            return None
        data = await self._get(search.url, search.params)
        if data is None:
            return None
        nxt = next_search_title(provider.read_search(data), [subject, title or ""])
        if not nxt:
            return None
        request = provider.lookup(endpoint, nxt, THUMB_WIDTH)
        data = await self._get(request.url, request.params)
        if data is None:
            return None
        return provider.read_lookup(data, endpoint)[1]

    # -- the tool -----------------------------------------------------------
    async def find(self, subject: str) -> str:
        """The tool result for one subject, as text for the model."""
        subject = (subject or "").strip()
        reason = refused(subject)
        if reason:
            return reason
        throttled = False
        for provider in self.providers:
            for endpoint in provider.endpoints:
                key = (provider.name, endpoint, subject.lower())
                if key in self._cache:
                    photo = self._cache[key]
                    if photo is not None:
                        return render(photo)
                    continue
                try:
                    photo = await self._find(provider, endpoint, subject)
                except RateLimited:
                    logger.warning("kitegate: photo provider %s is rate limiting", provider.name)
                    throttled = True
                    continue
                self._remember(key, photo)
                if photo is not None:
                    return render(photo)
        return BUSY_REPLY if throttled else not_found_reply(subject)

    async def answer(self, tool_calls: List[dict]) -> List[dict]:
        """A tool message for every call in a model turn.

        Every call id gets an answer, including ones that are not this tool or
        are over the limit, because the chat API refuses a history where a
        tool call has no result.
        """
        out: List[dict] = []
        used = 0
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            name = (call.get("function") or {}).get("name")
            if name != TOOL_NAME:
                content = UNAVAILABLE_REPLY
            elif used >= MAX_CALLS:
                content = TOO_MANY_REPLY
            else:
                used += 1
                content = await self.find(subject_of(call))
            out.append({"role": "tool", "tool_call_id": call.get("id") or "", "content": content})
        return out
