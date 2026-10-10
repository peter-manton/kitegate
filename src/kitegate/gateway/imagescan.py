"""The HTTP half of picture screening: one call to a hosted moderation model.

`kitegate.images` holds the policy (which URLs may be sent, which category
scores mean "no"), and this holds the request. Same split as `payloads` and
`app`: the core stays plain-dict in, plain-dict out and keeps its two
dependencies, so the Open WebUI build can still inline it.

What makes this cheap, in order of how much it saves:

  1. The moderation API fetches the image. The gateway never downloads or
     decodes a picture, so there is no SSRF surface, no bandwidth, no image
     parser and no decompression-bomb handling in this codebase.
  2. Verdicts are cached by URL. A public lead image has the same answer for
     every child forever, and a children's encyclopedia asks about the same
     few hundred subjects, so the steady state is almost all cache hits.
  3. The host allowlist and the URL safety rules run first and cost nothing.
     A picture from somewhere unexpected is dropped without an API call.
  4. One call per reply, not per picture: the endpoint takes a list.

FAIL CLOSED, with a deliberate exception to the usual meaning. Everywhere else
in this project a failure blocks the whole exchange. Here a failure removes
the PICTURE and lets the words through, because the text has already been
screened on its own merits and refusing a question about Rome because a
moderation endpoint timed out would be a worse outcome for a child than an
answer with no photo. That is the documented intended fallback: no picture.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import httpx

from ..images import ScanCache, moderation_body, results_by_url, safe_image_url
from .config import GatewaySettings

logger = logging.getLogger("kitegate.images")


class ImageScanner:
    """Decides which of a reply's pictures may be shown."""

    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.cache = ScanCache()

    @property
    def enabled(self) -> bool:
        return self.settings.image_policy == "scan"

    async def check(self, urls: List[str]) -> Dict[str, str]:
        """The pictures that must NOT be shown, mapped to the rule that stopped them.

        An empty dict means every picture passed. Callers pass the result to
        `payloads.strip_response_images`.
        """
        if not urls:
            return {}
        if self.settings.image_policy == "allow":
            return {}
        if self.settings.image_policy == "block":
            # No API call, no key needed: pictures are simply not shown.
            return {url: "image.policy_block" for url in urls}

        removed: Dict[str, str] = {}
        to_scan: List[str] = []
        for url in urls:
            reason = safe_image_url(url, self.settings.image_hosts)
            if reason:
                removed[url] = reason
                continue
            known, rule = self.cache.get(url)
            if known:
                if rule:
                    removed[url] = rule
                continue
            to_scan.append(url)

        if to_scan:
            removed.update(await self._scan(to_scan))
        return removed

    async def _scan(self, urls: List[str]) -> Dict[str, str]:
        try:
            r = await self.client.post(
                self.settings.image_scan_url,
                json=moderation_body(urls, self.settings.image_scan_model),
                headers={"content-type": "application/json",
                         "authorization": f"Bearer {self.settings.image_scan_api_key}"},
                timeout=self.settings.image_scan_timeout)
        except httpx.HTTPError:
            # Not cached: a timeout says nothing about the picture, and
            # caching it would hide a recovered scanner behind a stale "no".
            logger.warning("kitegate: image scan unreachable; showing no pictures", exc_info=True)
            return {url: "image.scan_unavailable" for url in urls}
        if r.status_code != 200:
            logger.error("kitegate: image scan returned HTTP %s; showing no pictures", r.status_code)
            return {url: "image.scan_error" for url in urls}
        try:
            verdicts = results_by_url(urls, r.json(), self.settings.image_thresholds)
        except ValueError:
            logger.exception("kitegate: image scan response was not JSON")
            return {url: "image.scan_error" for url in urls}

        removed: Dict[str, str] = {}
        for url, rule in verdicts.items():
            if rule == "image.unscreened":
                # The response did not cover this URL. Not cached, for the
                # same reason as a timeout.
                removed[url] = rule
                continue
            self.cache.put(url, rule)
            if rule:
                removed[url] = rule
        return removed

    def log_removals(self, removed: Dict[str, str]) -> None:
        """One line per dropped picture, with the rule but not the child's question."""
        for url, rule in removed.items():
            logger.warning("kitegate: picture removed rule=%s url=%s", rule, url)

    def stats(self) -> Tuple[int, int]:
        return self.cache.hits, self.cache.misses


def scanner_for(settings: GatewaySettings,
                client: httpx.AsyncClient) -> Optional[ImageScanner]:
    """A scanner unless pictures are allowed through unchecked."""
    if settings.image_policy == "allow":
        return None
    return ImageScanner(settings, client)
