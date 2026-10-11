"""Finding one real photograph for a subject, from a named, curated provider.

This is the half of the picture story that `images.py` does not cover.
`images.py` decides whether a picture may be SHOWN; this decides where a
picture comes from in the first place. A children's encyclopedia with no
pictures is a worse encyclopedia, and the safe way to add them is to choose the
source carefully rather than to filter an open one afterwards.

Framework-free and HTTP-free, exactly like `images.py`: a provider says what
request to make and how to read the answer, and the caller does the talking.
That keeps the package at its two dependencies, makes every provider testable
against a recorded response with no network, and leaves the HTTP to the
gateway (gateway/phototool.py).

---------------------------------------------------------------------------
WHY NOT IMAGE SEARCH
---------------------------------------------------------------------------
Open image search is the single largest unsafe-content surface in a feature
like this, and safesearch flags are best-effort in both directions. Nor is a
media-repository keyword search an improvement: Wikimedia Commons is
"curated" and also holds ~100M files, so a keyword query across it surfaces
clinical, anatomical and graphic material for perfectly innocent subjects.

So a provider here is not a search engine. It is a narrow, well-governed set:
the Wikipedia article LEAD IMAGE, which an editor chose to represent the
subject, or a named public-domain collection. For "tiger" that is a
photograph of a tiger.

---------------------------------------------------------------------------
ADDING A PROVIDER
---------------------------------------------------------------------------
Subclass PhotoProvider, implement the four methods, and register it in
PROVIDERS. Declare `image_hosts` honestly: it is what an operator puts in
KITEGATE_IMAGE_HOSTS, and a missing host means working pictures get dropped.

Before adding one, ask what its WORST result for an innocent subject looks
like, not its best. An art museum API is a poor children's provider however
reputable the museum, because art includes nudity and war; the release notes
for this project make the same point. A provider that cannot answer a subject
should return nothing, which costs a caption-less answer. A provider that
answers confidently with the wrong kind of picture is the failure that
matters.

Whatever a provider returns is still subject to the image policy in
`images.py`. A provider is the first line, not the only one.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from .normalise import variants

# ---------------------------------------------------------------------------
# SUBJECT SCREENING, and why it lives here rather than only in the engine
# ---------------------------------------------------------------------------
# The subject string the model invents is the one input to the whole feature
# that reaches an external API. The gateway screens it with the engine as model
# output before running the tool (gateway/phototool.py), but the engine's tiers
# are about conversation: "naked mole rat" is a fine question and a poor image
# query. So this list is applied on top, to the subject and again to any title
# a search resolves it to.
#
# This list is about IMAGERY, not about conversation. The topic policy belongs
# to the pack and the engine, and duplicating it here would produce confusing
# double failures. Every pattern is \b-anchored, for the reasons in core.py:
# "Drugstore beetle" and "needlefish" are not drug references.
BLOCKED_SUBJECT = re.compile(
    r"\b(?:"
    r"nude|naked|nudity|porn\w*|sex|sexual|genital\w*|penis|vagina|breasts?|"
    # gor(?:e|y), not a bare "gore": the ported pattern missed "a gory
    # battle", which is exactly the phrasing a child would use. Safe to widen
    # because the \b anchors keep Gregory, allegory and category out — in all
    # three, "gory" is preceded by a word character.
    r"anatomy|autopsy|corpse|cadaver|dead body|gor(?:e|y)|wound|injur\w*|surgery|"
    r"childbirth|birth canal|abortion|"
    r"execution|hanging|lynching|beheading|massacre|atrocity|"
    r"torture|mutilat\w*|suicide|self[- ]harm|"
    r"drugs?\b|syringe|needles?\b|cocaine|heroin|cannabis|"
    r"lingerie|bikini|swimsuit|underwear"
    r")\b",
    re.IGNORECASE,
)

def is_blocked(text: str) -> bool:
    """Whether a subject or title is imagery this project will not fetch.

    Checks the text as given and in its normalised forms (normalise.py), so a
    zero-width space or a look-alike letter does not walk past the list.
    """
    return any(BLOCKED_SUBJECT.search(v) for v in variants([text or ""]))


#: Characters that cannot be in a subject. "|" separates several titles in a
#: MediaWiki query, so one subject could fetch many articles; the others are
#: not allowed in a page title at all.
_TITLE_FORBIDDEN = set("|#<>[]{}")


def has_forbidden_characters(subject: str) -> bool:
    """True for a control or invisible character, or one a page title cannot hold."""
    return any(ch in _TITLE_FORBIDDEN or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn")
               for ch in subject or "")


# Deliberately does not name the word that matched. Telling a model exactly
# what tripped a filter invites it to retry around it.
REFUSED_SUBJECT_REPLY = (
    "No picture is available for that subject. Do not try a different wording "
    "for it. Answer in words instead, and if the child asked about something "
    "that is not suitable, suggest they ask a grown-up.")

_TRACKING_PREFIXES = ("utm_",)
MAX_CAPTION = 240


def strip_tracking(url: str) -> str:
    """Drop utm_* parameters.

    Measured: every image URL from the MediaWiki API carries utm_source,
    utm_campaign and utm_content. Removed so a child's browser does not make
    an analytics-tagged request, and so the same picture reached by two routes
    is one cache key in images.py rather than two.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [pair for pair in parts.query.split("&")
            if pair and not pair.split("=", 1)[0].startswith(_TRACKING_PREFIXES)]
    return urlunsplit(parts._replace(query="&".join(kept)))


def shorten(text: str, limit: int = MAX_CAPTION) -> str:
    """The first line of an article intro, trimmed on a word boundary."""
    caption = (text or "").strip().split("\n")[0].strip()
    if len(caption) <= limit:
        return caption
    return caption[:limit - 3].rsplit(" ", 1)[0] + "..."


@dataclass(frozen=True)
class Photo:
    title: str
    image_url: str
    caption: str = ""
    provider: str = ""
    source: str = ""     # the endpoint that answered, for the credit line


@dataclass(frozen=True)
class Request:
    url: str
    params: Dict[str, object]


class PhotoProvider:
    """One curated source of photographs.

    `endpoints` are tried in order and the first picture wins, so put the
    better-written or simpler source first.
    """

    name: str = ""
    endpoints: Tuple[str, ...] = ()
    image_hosts: Tuple[str, ...] = ()
    #: False for a provider that only knows one topic, so callers and docs can
    #: say so. A topical provider answers confidently about everything else,
    #: which is worse than not answering.
    general: bool = True
    topic: str = ""

    def lookup(self, endpoint: str, subject: str, width: int) -> Request:
        raise NotImplementedError

    def read_lookup(self, data: dict, endpoint: str) -> Tuple[Optional[str], Optional[Photo]]:
        """(resolved_title, photo). Either may be None.

        A title with no photo is a real and useful answer: it means the
        subject exists but the page has no lead image, which is the signal to
        try a search rather than to report a failure.
        """
        raise NotImplementedError

    def search(self, endpoint: str, subject: str) -> Optional[Request]:
        """A second request that turns a phrase into a title, or None if the
        provider has no search step."""
        return None

    def read_search(self, data: dict) -> List[str]:
        return []


class Wikimedia(PhotoProvider):
    """Wikipedia article lead images: an editor's choice for the subject.

    Simple English first, because its article text is written for learners
    ("the largest living member of the cat family" rather than "the genus
    Panthera") while carrying the same lead image.

    ONE request in the common case. MediaWiki capitalises the first letter and
    follows redirects itself, so a loose "tiger" resolves to "Tiger" without a
    separate call. That matters: an earlier version of this code issued up to
    six requests per subject and a ten-subject run was enough to earn HTTP 429.
    """

    name = "wikimedia"
    endpoints = ("https://simple.wikipedia.org", "https://en.wikipedia.org")
    # BOTH hosts, measured over ten subjects: scaled thumbnails come from
    # thumb.wikimedia.org (7/10) and unscaled originals from
    # upload.wikimedia.org (3/10). Listing only the obvious one would have
    # silently dropped the tiger.
    image_hosts = ("thumb.wikimedia.org", "upload.wikimedia.org")

    def lookup(self, endpoint: str, subject: str, width: int) -> Request:
        return Request(f"{endpoint}/w/api.php", {
            "action": "query", "prop": "pageimages|extracts", "piprop": "thumbnail",
            "pithumbsize": width, "exintro": 1, "explaintext": 1, "titles": subject,
            "format": "json", "redirects": 1})

    def read_lookup(self, data: dict, endpoint: str) -> Tuple[Optional[str], Optional[Photo]]:
        try:
            pages = data["query"]["pages"]
        except (KeyError, TypeError):
            return None, None
        if not isinstance(pages, dict):
            return None, None
        for page in pages.values():
            if not isinstance(page, dict) or "missing" in page:
                continue
            title = page.get("title")
            if not title:
                continue
            if is_blocked(title):
                # A redirect can land an innocent subject on an unsuitable
                # article, so the title it resolved to is checked, not only
                # the words that were asked for.
                continue
            thumb = (page.get("thumbnail") or {}).get("source")
            caption = shorten(page.get("extract") or "")
            if not thumb:
                # A disambiguation page reports no lead image. Measured:
                # "Mercury" comes back with a title, an extract listing the
                # meanings, and no thumbnail. So this is the search signal,
                # not an error.
                return title, None
            return title, Photo(title, strip_tracking(thumb), caption, self.name,
                                urlsplit(endpoint).hostname or endpoint)
        return None, None

    def search(self, endpoint: str, subject: str) -> Optional[Request]:
        return Request(f"{endpoint}/w/api.php", {
            "action": "query", "list": "search", "srsearch": subject,
            "srlimit": 3, "srnamespace": 0, "format": "json"})

    def read_search(self, data: dict) -> List[str]:
        try:
            results = data["query"]["search"]
        except (KeyError, TypeError):
            return []
        return [r["title"] for r in (results or [])
                if isinstance(r, dict) and r.get("title")]


class Nasa(PhotoProvider):
    """NASA's public image library. Public domain, no API key.

    TOPICAL, and that is the whole caveat. Measured: "tiger" returns 69 hits,
    every one of them Enceladus's "tiger stripes". It answers everything, and
    confidently, so it is not a general fallback and is off by default. Enable
    it alongside Wikimedia for a space-heavy deployment, where it is excellent.

    Search is the only step: one request returns the title, a description and
    a preview URL together.
    """

    name = "nasa"
    endpoints = ("https://images-api.nasa.gov",)
    image_hosts = ("images-assets.nasa.gov",)
    general = False
    topic = "space and astronomy"

    def lookup(self, endpoint: str, subject: str, width: int) -> Request:
        return Request(f"{endpoint}/search",
                       {"q": subject, "media_type": "image", "page_size": 5})

    def read_lookup(self, data: dict, endpoint: str) -> Tuple[Optional[str], Optional[Photo]]:
        items = ((data or {}).get("collection") or {}).get("items")
        for item in (items if isinstance(items, list) else []):
            if not isinstance(item, dict):
                continue
            meta = (item.get("data") or [{}])[0]
            if not isinstance(meta, dict):
                continue
            title = meta.get("title")
            # The preview link is used as given rather than rewritten to a
            # larger size. Measured on PIA01518: only ~thumb and ~orig exist,
            # and ~small, ~medium and ~large all return 403. Constructing a
            # size would produce a broken picture most of the time.
            preview = next((l.get("href") for l in item.get("links") or []
                            if isinstance(l, dict) and l.get("rel") == "preview"
                            and l.get("href")), None)
            if not title or not preview:
                continue
            if is_blocked(title):
                continue
            return title, Photo(title, preview, shorten(meta.get("description") or ""),
                                self.name, urlsplit(endpoint).hostname or endpoint)
        return None, None


PROVIDERS: Dict[str, PhotoProvider] = {p.name: p for p in (Wikimedia(), Nasa())}
DEFAULT_PROVIDERS = ("wikimedia",)


def get_providers(names) -> List[PhotoProvider]:
    """Providers by name, in the order given.

    An unknown name raises rather than being skipped: a typo would otherwise
    read as "pictures are off" and be very hard to tell from a provider
    outage.
    """
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",")]
    wanted = [str(n).strip().lower() for n in (names or []) if str(n).strip()]
    unknown = [n for n in wanted if n not in PROVIDERS]
    if unknown:
        raise ValueError(f"unknown photo provider(s) {unknown}; known: {sorted(PROVIDERS)}")
    return [PROVIDERS[n] for n in wanted or DEFAULT_PROVIDERS]


def image_hosts_for(names) -> List[str]:
    """Every image host the named providers can return.

    Use this to fill KITEGATE_IMAGE_HOSTS, rather than writing the list by
    hand: a provider's hosts are not always the ones you would guess.
    """
    hosts: List[str] = []
    for provider in get_providers(names):
        for host in provider.image_hosts:
            if host not in hosts:
                hosts.append(host)
    return hosts


def next_search_title(titles: List[str], tried: List[str]) -> Optional[str]:
    """The first search result worth a second lookup.

    Skips titles already tried, which the obvious version gets wrong: for
    "Mercury" the search returns ['Mercury', 'Mercury (element)',
    'Mercury (planet)'] and the first result is the disambiguation page that
    just failed. Comparing only against the original subject would retry it
    and give up, so a child asking about Mercury would get no picture at all.

    Also re-checks the title against BLOCKED_SUBJECT: search can land on an
    unsuitable article for an innocent query, so a resolved title is not
    trusted merely because the input passed.
    """
    seen = {t.strip().lower() for t in tried if t}
    for title in titles:
        if not title or title.strip().lower() in seen:
            continue
        if is_blocked(title):
            continue
        return title
    return None
