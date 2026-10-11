"""Screening the pictures a child is shown, using a hosted moderation model.

The regex core cannot say anything about a picture, so until now images were
not screened at all: `content_text` dropped image parts and a lead image in a
reply was only ever seen as the text of its markdown. This module is the
missing half, and it is deliberately NOT a classifier — it builds the request
for a hosted moderation API and reads the answer back, in the same shape as
`gateway/payloads.nemo_verdict`. The HTTP call itself lives in the gateway so
the core keeps its two dependencies.

Framework-free, like the engine and `messages`.

---------------------------------------------------------------------------
WHY A HOSTED API IS A REASONABLE CHOICE HERE, AND WHERE IT IS NOT
---------------------------------------------------------------------------
Two directions, and they are not the same problem:

  OUTBOUND, a picture the service SHOWS the child (a Wikipedia lead image).
  The image is already public, the URL reveals nothing about the child beyond
  the subject they asked about, and the verdict is identical for every user
  forever, so it caches. This is what the module is for.

  INBOUND, a picture the child UPLOADS. Possibly a photograph of the child.
  Sending that to a third party is the one thing a child-safety tool must not
  do casually, and OpenAI's own moderation guidance says explicitly not to
  send known or suspected CSAM to the endpoint because it is not built for it.
  So uploads are REFUSED by default and are never forwarded to the scanner.
  See `image_policy` / `allow_uploads` in gateway/config.py.

The scanner also never fetches an image itself: it hands the URL to the
moderation API and lets that service do the fetching. That removes the whole
SSRF, bandwidth, decode-bomb and image-parsing surface from this codebase,
which is the main reason this option is cheap. The only thing left to guard is
which URLs may be sent at all, which `safe_image_url` below does.

---------------------------------------------------------------------------
THRESHOLDS: WHY `flagged` IS NOT USED AS THE DECISION
---------------------------------------------------------------------------
The API returns a boolean `flagged` tuned for general-purpose platforms. It is
the wrong line for a seven-year-old's encyclopedia: a classical nude, a
painting of a battle or an anatomical drawing can sit well below the general
threshold and still be something a parent would not want appearing next to an
answer about the Romans. The release note for this project says the same thing
from the other direction: art and history lead images can show nudity or war.

So `flagged` is ignored and `category_scores` is compared against our own,
much lower thresholds. The cost is false positives, which here mean "no
picture" — the cheapest possible failure, and the documented intended
fallback. OpenAI also warns that scores may need recalibration as the model is
upgraded, which is why these live in one dict with a single override knob
rather than being scattered.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

# Only the categories the moderation model actually applies to IMAGE input.
# The text-only categories return 0 for an image, so including them would add
# a threshold that can never fire and imply cover that is not there.
#
# NOTE a real limitation, stated rather than papered over: `sexual/minors` is
# TEXT ONLY. The hosted model will not tell you that a picture sexualises a
# child, and its guidance is not to send suspected CSAM at all. This module
# therefore screens for adult sexual content, self-harm and violence in
# PUBLIC images being displayed. It is not, and must not be presented as, a
# CSAM control.
#
# Values are deliberately far below the API's own flagging line. Tuned for
# "would a parent mind this appearing beside an encyclopedia answer", not for
# "is this abusive content".
IMAGE_THRESHOLDS: Dict[str, float] = {
    "sexual": 0.20,
    "violence": 0.45,
    "violence/graphic": 0.25,
    "self-harm": 0.25,
    "self-harm/intent": 0.25,
    "self-harm/instructions": 0.25,
}

# Markdown images: ![alt](url), ![alt](<url>), ![alt](url "title").
# Anchored on the leading "!" so ordinary links are not treated as pictures.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s<>]+)>?[^)]*\)")
# HTML, in case a front end renders it.
_HTML_IMG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
# A bare URL that ends in an image extension. Some front ends embed these
# rather than linking them, so they count as a picture being shown.
_BARE_IMAGE = re.compile(
    r"https?://[^\s<>\"')\]]+\.(?:png|jpe?g|gif|webp|bmp|svg|avif|tiff?)(?:\?[^\s<>\"')\]]*)?",
    re.IGNORECASE)

_DATA_IMAGE = re.compile(r"^data:image/[a-z0-9.+-]+;base64,", re.IGNORECASE)


def image_urls(text: str) -> List[str]:
    """Every picture a piece of model or tool output would display.

    Order is preserved and duplicates are dropped, so a reply repeating one
    lead image costs one scan.
    """
    if not text:
        return []
    found: List[str] = []
    for pattern in (_MD_IMAGE, _HTML_IMG):
        found.extend(m.group(1).strip() for m in pattern.finditer(text))
    found.extend(m.group(0).strip() for m in _BARE_IMAGE.finditer(text))
    seen = set()
    out = []
    for url in found:
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def is_data_image(ref: str) -> bool:
    return bool(_DATA_IMAGE.match(ref or ""))


def safe_image_url(url: str, allowed_hosts: Iterable[str] = ()) -> Optional[str]:
    """The reason this URL must not be sent to the scanner, or None if it is fine.

    Three jobs, in order of how badly they fail:

      1. Keep internal URLs out of a third party's fetcher. The moderation API
         fetches whatever it is given, so passing it http://10.0.0.5/... would
         be asking someone else to probe the private network and would leak
         the hostname besides.
      2. Refuse cleartext, for the same reason the gateway refuses a plain
         http upstream: the picture a child is shown should not be swappable
         in transit.
      3. Enforce the operator's host allowlist, when set. This is the cheap,
         deterministic half of the defence and it does not cost an API call:
         if lead images only ever come from one media host, nothing else needs
         considering.
    """
    ref = (url or "").strip()
    if not ref:
        return "image.empty_url"
    if is_data_image(ref):
        # An inline upload. Never forwarded: see the module docstring.
        return "image.inline_data"
    parts = urlsplit(ref)
    if parts.scheme != "https":
        return "image.not_https"
    host = (parts.hostname or "").strip("[]").lower()
    if not host:
        return "image.no_host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return "image.private_host"
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        # A single-label name is a container on this machine, matching the
        # rule _is_local_host applies to upstreams in gateway/config.py.
        return "image.private_host"
    hosts = {h.strip().lower() for h in allowed_hosts if str(h).strip()}
    if hosts and host not in hosts and not any(host.endswith("." + h) for h in hosts):
        return "image.host_not_allowed"
    return None


def moderation_body(urls: List[str], model: str) -> dict:
    """The request body for an OpenAI-compatible /moderations call.

    One call carries every image in a reply: the endpoint takes a list, so a
    reply with three pictures is one round trip, not three.
    """
    return {"model": model,
            "input": [{"type": "image_url", "image_url": {"url": url}} for url in urls]}


def read_scores(result: dict) -> Dict[str, float]:
    scores = (result or {}).get("category_scores") or {}
    out: Dict[str, float] = {}
    for name, value in scores.items():
        try:
            out[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def worst_category(result: dict,
                   thresholds: Optional[Dict[str, float]] = None) -> Optional[Tuple[str, float]]:
    """The category that most exceeds its threshold, or None if the image is fine.

    "Most exceeded" rather than "highest score" so the rule label names the
    category that actually decided it, which is what ends up in the block log.
    """
    limits = thresholds or IMAGE_THRESHOLDS
    scores = read_scores(result)
    worst: Optional[Tuple[str, float]] = None
    margin = 0.0
    for name, limit in limits.items():
        score = scores.get(name)
        if score is None or score < limit:
            continue
        over = score - limit
        if worst is None or over > margin:
            worst, margin = (name, score), over
    return worst


def rule_label(category: str) -> str:
    """A moderation category as a kitegate rule label: image.violence_graphic."""
    return "image." + re.sub(r"[^a-z0-9]+", "_", str(category).lower()).strip("_")


def results_by_url(urls: List[str], data: dict,
                   thresholds: Optional[Dict[str, float]] = None) -> Dict[str, Optional[str]]:
    """Map each scanned URL to its rule label, or None when it passed.

    Results come back in request order. A short or missing list is treated as
    a failure for the images it does not cover rather than a pass, so a
    truncated response cannot quietly show an unscreened picture.
    """
    results = (data or {}).get("results")
    results = results if isinstance(results, list) else []
    out: Dict[str, Optional[str]] = {}
    for n, url in enumerate(urls):
        if n >= len(results) or not isinstance(results[n], dict):
            out[url] = "image.unscreened"
            continue
        worst = worst_category(results[n], thresholds)
        out[url] = rule_label(worst[0]) if worst else None
    return out


def strip_images(text: str, urls: Iterable[str]) -> str:
    """Remove the named pictures from text, leaving the words around them.

    Dropping the picture rather than blocking the whole answer is deliberate,
    and it is the one place this project redacts instead of blocking. Refusing
    a question about Rome because its lead image scored 0.3 on violence would
    teach a child that asking was wrong, when the honest outcome is simply
    that there is no good picture. The words were screened by the text tiers
    on their own merits and are unaffected.
    """
    if not text:
        return text
    targets = {u for u in urls if u}
    if not targets:
        return text

    def drop(match: "re.Match[str]") -> str:
        url = (match.group(1) if match.lastindex else match.group(0)).strip()
        return "" if url in targets else match.group(0)

    out = _MD_IMAGE.sub(drop, text)
    out = _HTML_IMG.sub(drop, out)
    out = _BARE_IMAGE.sub(lambda m: "" if m.group(0).strip() in targets else m.group(0), out)
    # Tidy the hole left behind: trailing spaces, then runs of blank lines.
    out = re.sub(r"[ \t]+(\r?\n)", r"\1", out)
    out = re.sub(r"(\r?\n){3,}", r"\1\1", out)
    return out.strip()


class ScanCache:
    """Remembered verdicts, keyed by URL.

    The reason the hosted option is cheap in practice. A children's
    encyclopedia has a very heavy head — tigers, volcanoes, the Romans — and a
    public lead image has the same verdict for every child, forever. After a
    short warm-up almost every request is a cache hit and makes no API call at
    all.

    Bounded and in-memory: a safety component should not be able to grow until
    the process dies. Oldest entries go first, which suits this access pattern because the
    popular subjects keep being re-inserted.
    """

    def __init__(self, limit: int = 4096) -> None:
        self.limit = max(1, int(limit))
        self._entries: "Dict[str, Optional[str]]" = {}
        self.hits = 0
        self.misses = 0

    def get(self, url: str) -> Tuple[bool, Optional[str]]:
        """(known, rule). rule is None for a picture that passed."""
        if url in self._entries:
            self.hits += 1
            return True, self._entries[url]
        self.misses += 1
        return False, None

    def put(self, url: str, rule: Optional[str]) -> None:
        if url in self._entries:
            del self._entries[url]
        elif len(self._entries) >= self.limit:
            self._entries.pop(next(iter(self._entries)))
        self._entries[url] = rule

    def __len__(self) -> int:
        return len(self._entries)
