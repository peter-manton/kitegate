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
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple
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

# Finding pictures. Not regexes over the whole reply: a renderer accepts more
# than any one pattern does (brackets inside the alt text, parentheses inside
# the URL, reference-style images, HTML without quotes), and every form a
# pattern missed was a picture shown to a child unscanned, even with the
# policy set to "block". So the markdown forms are read the way CommonMark
# reads them, and every HTML tag that can load a resource is taken apart.
#
# The result is a list of spans (start, end, urls), so stripping removes
# exactly what detection found and the two cannot disagree.

# A bare URL that ends in an image extension. Some front ends embed these
# rather than linking them, so they count as a picture being shown.
_BARE_IMAGE = re.compile(
    r"https?://[^\s<>\"')\]]+\.(?:png|jpe?g|gif|webp|bmp|svg|avif|tiff?)(?:\?[^\s<>\"')\]]*)?",
    re.IGNORECASE)
# Reference definitions: [label]: url "title". Up to three spaces of indent.
_REF_DEF = re.compile(r"^ {0,3}\[((?:[^\[\]\\]|\\.){1,999})\]:[ \t]*(?:\r?\n[ \t]*)?"
                      r"(?:<([^<>\n]*)>|(\S+))[^\n]*$", re.MULTILINE)
# HTML tags that can load a picture (or any resource a renderer would fetch).
_HTML_TAG = re.compile(r"<\s*(?:img|image|source|picture|video|audio|input|embed|object|iframe|"
                       r"svg|use|feimage|body|table|td|th|div|span|a)\b[^>]*>", re.IGNORECASE)
_HTML_ATTR = re.compile(r"\b(src|srcset|poster|data|href|xlink:href|background|style)\s*=\s*"
                        r"(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))", re.IGNORECASE)
_CSS_URL = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", re.IGNORECASE)
# Attributes that load something whatever the tag. href only counts on the
# SVG elements that use it to load an image.
_LOADING_ATTRS = {"src", "srcset", "poster", "data", "background", "style"}
_HREF_TAGS = re.compile(r"<\s*(?:image|use|feimage)\b", re.IGNORECASE)
# Bounds, so a reply of "![![![..." costs linear time rather than quadratic.
_MAX_SCAN = 2000

_DATA_IMAGE = re.compile(r"^data:image/[a-z0-9.+-]+;base64,", re.IGNORECASE)

Span = Tuple[int, int, List[str]]


def _close_bracket(text: str, start: int) -> int:
    """Index of the "]" matching the "[" at `start`, or -1."""
    depth = 0
    i = start
    stop = min(len(text), start + _MAX_SCAN)
    while i < stop:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _destination(text: str, start: int) -> Tuple[int, str]:
    """(end, url) of an inline destination "(url "title")" at `start`, or (-1, "")."""
    i = start + 1
    stop = min(len(text), start + _MAX_SCAN)
    while i < stop and text[i] in " \t\r\n":
        i += 1
    if i < stop and text[i] == "<":
        close = text.find(">", i, stop)
        if close == -1:
            return -1, ""
        url, i = text[i + 1:close], close + 1
    else:
        depth, begin = 0, i
        while i < stop:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch in " \t\r\n":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            i += 1
        url = text[begin:min(i, stop)]
    close = text.find(")", i, stop)
    if close == -1:
        return -1, ""
    return close + 1, url.strip().replace("\\", "")


def _label(text: str) -> str:
    return " ".join(text.replace("\\", "").split()).casefold()


def _markdown_spans(text: str) -> List[Span]:
    spans: List[Span] = []
    refs: List[Tuple[int, int, str]] = []
    i = text.find("![")
    while i != -1:
        close = _close_bracket(text, i + 1)
        if close != -1:
            after = close + 1
            if text[after:after + 1] == "(":
                end, url = _destination(text, after)
                if end != -1:
                    spans.append((i, end, [url]))
            elif text[after:after + 1] == "[":
                end = _close_bracket(text, after)
                if end != -1:
                    label = text[after + 1:end] or text[i + 2:close]
                    refs.append((i, end + 1, _label(label)))
            else:
                refs.append((i, after, _label(text[i + 2:close])))
        # From i + 2, not from the end: the alt text can hold another image.
        i = text.find("![", i + 2)
    if refs:
        defs: Dict[str, Tuple[int, int, str]] = {}
        for m in _REF_DEF.finditer(text):
            defs.setdefault(_label(m.group(1)), (m.start(), m.end(), (m.group(2) or m.group(3) or "").strip()))
        for start, end, label in refs:
            if label in defs:
                d_start, d_end, url = defs[label]
                spans.append((start, end, [url]))
                spans.append((d_start, d_end, [url]))
    return spans


def _html_spans(text: str) -> List[Span]:
    spans: List[Span] = []
    for tag in _HTML_TAG.finditer(text):
        urls: List[str] = []
        for attr in _HTML_ATTR.finditer(tag.group(0)):
            name = attr.group(1).lower()
            value = next(v for v in attr.group(2, 3, 4) if v is not None).strip()
            if name == "style":
                urls.extend(u.strip() for u in _CSS_URL.findall(value))
            elif name == "srcset":
                urls.extend(c.strip().split()[0] for c in value.split(",") if c.strip())
            elif name in _LOADING_ATTRS or _HREF_TAGS.match(tag.group(0)):
                urls.append(value)
        if urls:
            spans.append((tag.start(), tag.end(), urls))
    return spans


def image_spans(text: str) -> List[Span]:
    """Every picture in a piece of text, as (start, end, urls), in order."""
    if not text:
        return []
    spans = _markdown_spans(text) + _html_spans(text)
    covered = [(s, e) for s, e, _ in spans]
    for m in _BARE_IMAGE.finditer(text):
        if not any(s <= m.start() < e for s, e in covered):
            spans.append((m.start(), m.end(), [m.group(0)]))
    return sorted(spans, key=lambda s: (s[0], s[1]))


def image_urls(text: str) -> List[str]:
    """Every picture a piece of model or tool output would display.

    Order is preserved and duplicates are dropped, so a reply repeating one
    lead image costs one scan. An empty or relative URL is kept as given:
    safe_image_url refuses it, so the picture is removed rather than shown.
    """
    seen = set()
    out = []
    for _, _, urls in image_spans(text):
        for url in urls:
            url = url.strip()
            if url not in seen:
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


def scores_missing(result: dict, thresholds: Optional[Dict[str, float]] = None) -> List[str]:
    """The thresholded categories a moderation result gave no usable score for."""
    scores = read_scores(result)
    return [name for name in (thresholds or IMAGE_THRESHOLDS) if name not in scores]


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
        if scores_missing(results[n], thresholds):
            # A result that does not score every category we threshold on has
            # not screened the picture: a scanner that names its categories
            # differently, or a renamed field, would otherwise read as a pass.
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

    Uses the same spans as image_urls, so whatever was found is what goes.
    """
    if not text:
        return text
    targets = {u.strip() for u in urls if u is not None}
    if not targets:
        return text
    cut = sorted((s, e) for s, e, found in image_spans(text)
                 if any(u.strip() in targets for u in found))
    if not cut:
        return text
    merged: List[List[int]] = []
    for s, e in cut:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = text
    for s, e in reversed(merged):
        out = out[:s] + out[e:]
    # Tidy the hole left behind: trailing spaces, then runs of blank lines.
    out = re.sub(r"[ \t]+(\r?\n)", r"\1", out)
    out = re.sub(r"(\r?\n){3,}", r"\1\1", out)
    return out.strip()


#: How long a verdict is trusted, in seconds. The address of a picture is not
#: the picture: a Wikimedia file can be replaced (or vandalised) under the same
#: URL, and the moderation service fetched it once. An answer that never
#: expires would keep showing a replaced picture on the strength of a check
#: made on the old one, so every verdict is re-asked after this long.
DEFAULT_CACHE_TTL = 6 * 60 * 60.0


class ScanCache:
    """Remembered verdicts, keyed by URL, for a limited time.

    The reason the hosted option is cheap in practice. A children's
    encyclopedia has a very heavy head (tigers, volcanoes, the Romans) and a
    public lead image has the same verdict for every child for as long as the
    picture is the same one. After a short warm-up almost every request is a
    cache hit and makes no API call at all.

    Bounded and in-memory: a safety component should not be able to grow until
    the process dies. Oldest entries go first, which suits this access pattern
    because the popular subjects keep being re-inserted. An entry also expires
    after `ttl` seconds (DEFAULT_CACHE_TTL), so a replaced picture is checked
    again; the popular subjects cost one call per expiry, not one per child.
    """

    def __init__(self, limit: int = 4096, ttl: float = DEFAULT_CACHE_TTL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = max(1, int(limit))
        self.ttl = max(0.0, float(ttl))
        self._clock = clock
        self._entries: "Dict[str, Tuple[Optional[str], float]]" = {}
        self.hits = 0
        self.misses = 0

    def get(self, url: str) -> Tuple[bool, Optional[str]]:
        """(known, rule). rule is None for a picture that passed."""
        entry = self._entries.get(url)
        if entry is not None:
            rule, expires = entry
            if self._clock() < expires:
                self.hits += 1
                return True, rule
            del self._entries[url]  # stale: ask again
        self.misses += 1
        return False, None

    def put(self, url: str, rule: Optional[str]) -> None:
        if url in self._entries:
            del self._entries[url]
        elif len(self._entries) >= self.limit:
            self._entries.pop(next(iter(self._entries)))
        self._entries[url] = (rule, self._clock() + self.ttl)

    def __len__(self) -> int:
        return len(self._entries)
