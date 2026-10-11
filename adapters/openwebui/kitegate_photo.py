"""
title: Kitegate Photo
author: kitegate
version: 0.1.0
description: Finds one real photograph for a subject from curated providers only (Wikipedia lead images by default). Built for children's encyclopedia models.
"""

# A real photograph for a child's question, from a named curated source.
#
# The providers, the subject screening and the response parsing all live in
# kitegate.photos, which this file inlines when built — so the same provider
# code is covered by the test suite rather than living only in a pasted
# function. This file is the part that cannot be unit tested: HTTP, throttling
# and the cache.
#
#   python tools/build_owui_photo.py   ->  dist/kitegate_openwebui_photo.py
#
# Install the BUILT file in Admin Panel > Functions > Tools, then enable it on
# the children's model. Open WebUI has no way to install a tool from the
# environment, so this step is manual even in the bundle.
#
# ---------------------------------------------------------------------------
# THIS TOOL DOES NOT MAKE A PICTURE SAFE TO SHOW
# ---------------------------------------------------------------------------
# It chooses a careful SOURCE. Whether the picture is displayed is decided by
# the gateway's image policy (KITEGATE_IMAGE_POLICY, see src/kitegate/images.py),
# which sees the URL on the way back to the child and removes it if it fails a
# moderation check. The two halves are independent on purpose:
#
#   this tool          narrow the source, and screen the subject before the
#                      request leaves, because a tool executes in Open WebUI's
#                      own backend and does not pass through the gateway first
#   the image policy   check the actual picture, and have the final say
#
# If you run the single-file FILTER instead of the gateway, there is no image
# policy and this tool is the only control. Prefer the gateway.
#
# ---------------------------------------------------------------------------
# DEPENDENCIES
# ---------------------------------------------------------------------------
# requests and pydantic only, both already in the open-webui image. There is
# deliberately no requirements frontmatter: a bundle that sets
# ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False would not install a
# declared dependency, so this would fail at call time instead of load time.
#
# ---------------------------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------------------------
# Measured against the live MediaWiki API: a single ten-subject run was enough
# to start receiving HTTP 429 when the code issued several requests per
# subject. That mattered more than it looked, because a throttled request
# returned nothing and the caller reported "no photograph found" — identical
# to a subject that genuinely has no image. The tool looked randomly
# unreliable to a child rather than rate-limited to an operator.
#
# Hence: one request per subject in the common case (the provider resolves the
# title and returns the image together), a process-wide minimum gap, one
# bounded retry, an in-process cache, and 429 reported distinctly so it can
# never again be mistaken for a missing picture.
#
# Wikimedia sends no Retry-After or equivalent on a 429 — measured, there is
# nothing in the response to respect — so the only option is a short backoff.
import threading
import time
from typing import Optional, Tuple

import requests
from pydantic import BaseModel, Field

from kitegate.photos import BLOCKED_SUBJECT, REFUSED_SUBJECT_REPLY  # kitegate:inline
from kitegate.photos import Photo, get_providers, next_search_title  # kitegate:inline

_MIN_REQUEST_GAP = 1.0   # seconds between outbound calls, process-wide
_RETRY_SLEEP = 2.0       # one backoff before giving up on a 429
_MAX_CACHED = 512
_rate_lock = threading.Lock()
_last_request_at = 0.0


class RateLimited(Exception):
    """A provider returned 429. Distinct from "no image found" on purpose."""


def _throttle() -> None:
    global _last_request_at
    with _rate_lock:
        gap = time.monotonic() - _last_request_at
        if gap < _MIN_REQUEST_GAP:
            time.sleep(_MIN_REQUEST_GAP - gap)
        _last_request_at = time.monotonic()


class Tools:
    class Valves(BaseModel):
        providers: str = Field(
            default="wikimedia",
            description=("Comma-separated photo providers, tried in order: wikimedia "
                         "(Wikipedia lead images), nasa (space only, public domain). "
                         "nasa answers confidently about non-space subjects, so keep it "
                         "after wikimedia."),
        )
        thumb_width: int = Field(default=640, description="Requested image width in pixels.")
        timeout_seconds: int = Field(default=12, description="Per-request HTTP timeout.")
        user_agent: str = Field(
            default="kitegate-photo/1.0 (self-hosted; +https://github.com/kitegate)",
            description=("Sent on every request. Wikimedia asks for a descriptive "
                         "User-Agent and throttles generic ones."),
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        self._session = requests.Session()
        # (provider, endpoint, subject) -> Photo or None
        self._cache: dict = {}

    # -- internals --------------------------------------------------------
    def _get(self, url: str, params: dict) -> Optional[dict]:
        for attempt in (1, 2):
            _throttle()
            try:
                response = self._session.get(
                    url, params=params or None,
                    headers={"User-Agent": self.valves.user_agent},
                    timeout=self.valves.timeout_seconds)
            except Exception:
                # A network failure reads the same as "no result" to the
                # caller, and a child gains nothing from a stack trace.
                return None
            if response.status_code == 429:
                if attempt == 1:
                    time.sleep(_RETRY_SLEEP)
                    continue
                raise RateLimited(url)
            if response.status_code != 200:
                return None
            try:
                return response.json()
            except ValueError:
                return None
        return None

    def _remember(self, key: tuple, photo: Optional[Photo]) -> None:
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= _MAX_CACHED:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = photo

    def _find(self, provider, endpoint: str, subject: str) -> Tuple[Optional[Photo], bool]:
        """(photo, searched). Raises RateLimited."""
        request = provider.lookup(endpoint, subject, self.valves.thumb_width)
        data = self._get(request.url, request.params)
        if data is None:
            return None, False
        title, photo = provider.read_lookup(data, endpoint)
        if photo is not None:
            return photo, False

        # Only now spend a second request: either there is no such article, or
        # it is a disambiguation page, and search is the way through both.
        search = provider.search(endpoint, subject)
        if search is None:
            return None, False
        data = self._get(search.url, search.params)
        if data is None:
            return None, True
        nxt = next_search_title(provider.read_search(data), [subject, title or ""])
        if not nxt:
            return None, True
        request = provider.lookup(endpoint, nxt, self.valves.thumb_width)
        data = self._get(request.url, request.params)
        if data is None:
            return None, True
        _, photo = provider.read_lookup(data, endpoint)
        return photo, True

    # -- tool -------------------------------------------------------------
    def find_photo(self, subject: str) -> str:
        """
        Find one real photograph of a subject, with a short caption.

        Use this when a real picture would help explain something: an animal, a
        planet, a famous building, a country's flag, a historical object. Pass a
        simple noun phrase as the subject, for example "tiger", "Jupiter",
        "Great Pyramid of Giza". Do not pass a whole question.

        Returns markdown containing the image, which you should include in your
        answer. If no suitable photograph is found, say so plainly and carry on
        answering in words.

        :param subject: The thing to find a photograph of, as a short noun phrase.
        """
        subject = (subject or "").strip()
        if not subject:
            return "No subject was given, so no picture was looked up."
        if BLOCKED_SUBJECT.search(subject):
            return REFUSED_SUBJECT_REPLY

        try:
            providers = get_providers(self.valves.providers)
        except ValueError as exc:
            # An operator error, so say so rather than reporting "no picture":
            # a misspelled provider name would otherwise look like an outage.
            return (f"The photo tool is misconfigured and found no provider ({exc}). "
                    "Answer in words instead and tell the child to mention this to a grown-up.")

        throttled = False
        for provider in providers:
            for endpoint in provider.endpoints:
                key = (provider.name, endpoint, subject.lower())
                if key in self._cache:
                    photo = self._cache[key]
                    if photo is not None:
                        return self._render(photo)
                    continue
                try:
                    photo, _ = self._find(provider, endpoint, subject)
                except RateLimited:
                    # Recorded and reported distinctly. Conflating this with
                    # "not found" is the specific bug this branch prevents.
                    throttled = True
                    continue
                self._remember(key, photo)
                if photo is not None:
                    return self._render(photo)

        if throttled:
            return ("The picture library is busy at the moment and would not answer. "
                    "This is nothing to do with what was asked. Answer the question in "
                    "words, and mention that the picture could not be fetched just now "
                    "and is worth trying again shortly.")
        return (f"No photograph was found for '{subject}'. "
                "Answer in words instead, and do not link to any other website.")

    @staticmethod
    def _render(photo: Photo) -> str:
        return (f"Photograph found for '{photo.title}' (from {photo.source}).\n\n"
                f"![{photo.title}]({photo.image_url})\n\n"
                f"{photo.caption}\n\n"
                "Include the image markdown above in your answer, and describe the "
                "picture to the child in your own simple words.")
