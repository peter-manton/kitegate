"""Gateway settings, all from the environment so nothing personal lives in a repo.

    KITEGATE_API_KEY          required. Clients send it as "Authorization: Bearer ..."
    KITEGATE_UPSTREAM_URL             required. OpenAI-compatible base URL, e.g. http://nemo:8000/v1
    KITEGATE_UPSTREAM_API_KEY         key the gateway sends upstream (optional)
    KITEGATE_UPSTREAM_KIND            "openai" (default) or "nemo" (NeMo Guardrails server)
    KITEGATE_NEMO_CONFIG_ID           NeMo config to use; empty uses the server's default
    KITEGATE_CHILD_MODELS             comma-separated children's models: "alias" or "alias=upstream-model"
    KITEGATE_CHILD_USERS              comma-separated child account emails, for deny-by-default
    KITEGATE_ADULT_USERS              comma-separated adult account emails: the only accounts
                                      passthrough serves. Anyone else on another model is
                                      refused, so an account nobody listed is treated as a child.
    KITEGATE_PACK                     default values pack (default: secular-neutral)
    KITEGATE_ALLOWED_PACKS            packs a client may pick per request with x-kitegate-pack
    KITEGATE_PACK_KEYS                extra public keys trusted to sign packs (see signing.py)
    KITEGATE_REQUIRE_SIGNED_PACKS     "true": refuse any pack without a trusted signature
    KITEGATE_INJECT_FRAMING           "true" (default): add the pack's framing to the system message.
                                      The core framing (core.py CORE_FRAMING) is sent either way
                                      and cannot be switched off here.
    KITEGATE_CLIENT_SYSTEM            what happens to a client's own system (or developer)
                                      messages on a children's model. The gateway's system
                                      message is always the only one, and first:
                                      "fold" (default): their text goes into it, after the core
                                             and pack framing, ranked below them
                                      "drop": they are removed
                                      Adult passthrough models are not touched either way.
    KITEGATE_PASSTHROUGH              "true": forward other models unscreened (adults only)
    KITEGATE_PASSTHROUGH_URL          where adult traffic goes (default: the upstream URL)
    KITEGATE_PASSTHROUGH_API_KEY      key for that (default: the upstream key)
    KITEGATE_BLOCK_LOG                optional JSONL path; blocks are appended for adult review
    KITEGATE_ALERT_WEBHOOK            optional URL; distress events are POSTed to it
    KITEGATE_UPSTREAM_TIMEOUT         seconds (default 120)
    KITEGATE_ALLOW_INSECURE_UPSTREAM  "true": allow plain http:// to non-local hosts (not advised)

Pictures (see images.py for what is and is not covered):

    KITEGATE_IMAGE_POLICY             "scan" (default) | "block" | "allow"
                                      scan:  a picture from an allowed host is checked by a
                                             hosted moderation model and removed only if it
                                             fails. Needs KITEGATE_IMAGE_SCAN_API_KEY.
                                      block: pictures are removed from replies
                                      allow: shown unchecked, host allowlist not applied
                                             (what happened before this setting existed)
    KITEGATE_IMAGE_SCAN_URL           OpenAI-compatible /moderations endpoint
    KITEGATE_IMAGE_SCAN_API_KEY       key for it; required when policy is "scan". With no
                                      key and no explicit policy, the default is "block":
                                      there is no safe way to show an unscanned picture.
    KITEGATE_IMAGE_SCAN_MODEL         default omni-moderation-latest
    KITEGATE_IMAGE_SCAN_TIMEOUT       seconds (default 10)
    KITEGATE_IMAGE_CACHE_TTL          seconds a picture's verdict is trusted (default 21600,
                                      six hours). A picture can be replaced under the same
                                      URL, so a verdict is asked for again after this long.
    KITEGATE_IMAGE_HOSTS              hosts a picture may come from. Defaults to the hosts
                                      of the curated photo providers this project ships
                                      (TRUSTED_IMAGE_HOSTS below), so a picture the model
                                      invented from anywhere else is dropped with no API
                                      call. "*" turns the allowlist off and scans whatever
                                      arrives.
    KITEGATE_IMAGE_THRESHOLDS         overrides, e.g. "sexual=0.1,violence=0.6"
    KITEGATE_IMAGE_UPLOADS            "allow": let children send pictures to the model.
                                      Off by default and NOT scanned — a child's own
                                      photo must not be sent to a third party.
    KITEGATE_PHOTOS                   curated photo providers for the gateway's own
                                      find_photo tool, tried in order (photos.py):
                                      "wikimedia" (default), "nasa" (space only).
                                      "off" removes the tool. It is offered to the model
                                      only when pictures can actually be shown, so with
                                      KITEGATE_IMAGE_POLICY=block it is off whatever
                                      this says. No front end needs a tool installed.

The picture defaults are resolved in from_env, not in the field defaults: the
dataclass is a plain value object and says only what it was given, so a test
or a caller building one by hand gets no pictures and no allowlist unless it
asks. "Default" below therefore means "what an unset environment variable
becomes".

Serving (read by `kitegate`, not by the app itself):

    KITEGATE_HOST             bind address (default 127.0.0.1; the Docker image sets 0.0.0.0)
    KITEGATE_PORT             port (default 8080)
    KITEGATE_TLS_CERT         PEM certificate; with the key, the gateway serves HTTPS itself
    KITEGATE_TLS_KEY          PEM private key

Children's messages and the API key cross every hop, so outbound URLs must
be https:// unless the host is loopback or a single-label name (a Docker
Compose service such as "nemo"), where traffic never leaves the machine.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from ..images import DEFAULT_CACHE_TTL, IMAGE_THRESHOLDS
from ..messages import split_list
from ..packs import bundled_packs
from ..photos import DEFAULT_PROVIDERS, PROVIDERS, get_providers, image_hosts_for
from .payloads import CLIENT_SYSTEM_POLICIES

_TRUE = {"1", "true", "yes", "on"}
MIN_KEY_LENGTH = 16
IMAGE_POLICIES = ("block", "scan", "allow")
DEFAULT_SCAN_URL = "https://api.openai.com/v1/moderations"
DEFAULT_SCAN_MODEL = "omni-moderation-latest"

# Every host the curated providers in photos.py can return, and the default
# allowlist. Two reasons it is the default rather than empty:
#
#   - "scanned" and "from a source someone chose" are different guarantees and
#     a child should have both. A moderation model says a picture is not
#     explicit; it does not say the picture is a photograph of the subject
#     from a governed collection.
#   - it is free. The host check runs before the API call, so a picture the
#     model invented, or one a tool fetched from somewhere unexpected, costs
#     nothing to drop.
#
# Derived from the providers rather than written out, because a provider's
# hosts are not the ones you would guess (Wikimedia serves scaled thumbnails
# and unscaled originals from two different hosts) and because registering a
# new provider must not silently drop its pictures.
TRUSTED_IMAGE_HOSTS = frozenset(image_hosts_for(sorted(PROVIDERS)))
ANY_IMAGE_HOST = "*"
PHOTOS_OFF = {"off", "none", "false", "no", "0"}


class SettingsError(ValueError):
    pass


def _is_local_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A single-label name ("nemo", "litellm") is a Docker/Compose service
        # on the same host. Anything with a dot may be across a network.
        return bool(host) and "." not in host


def check_transport(name: str, url: str, allow_insecure: bool) -> None:
    """Refuse a URL that would send children's messages in clear text."""
    if not url:
        return
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme != "http":
        raise SettingsError(f"{name} must be an http(s) URL, got {url!r}")
    if allow_insecure or _is_local_host(parts.hostname or ""):
        return
    raise SettingsError(
        f"{name} is plain http:// to {parts.hostname!r}, which would send children's messages "
        "unencrypted. Use https://, or set KITEGATE_ALLOW_INSECURE_UPSTREAM=true if this "
        "network path is protected another way.")


def _parse_thresholds(value: str) -> Dict[str, float]:
    """"sexual=0.1,violence=0.6" over the defaults in images.py.

    Unknown category names are refused rather than ignored: a typo would
    otherwise read as a tightened threshold that is not actually applied.
    """
    out = dict(IMAGE_THRESHOLDS)
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, score = item.partition("=")
        name = name.strip().lower()
        if name not in IMAGE_THRESHOLDS:
            raise SettingsError(
                f"KITEGATE_IMAGE_THRESHOLDS names unknown category {name!r}; "
                f"known: {sorted(IMAGE_THRESHOLDS)}")
        try:
            out[name] = float(score)
        except ValueError as exc:
            raise SettingsError(f"KITEGATE_IMAGE_THRESHOLDS: {item!r} is not name=number") from exc
        if not 0.0 < out[name] <= 1.0:
            raise SettingsError(f"KITEGATE_IMAGE_THRESHOLDS: {name} must be above 0 and at most 1")
    return out


def _parse_models(value: str) -> Dict[str, str]:
    models: Dict[str, str] = {}
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        alias, _, upstream = item.partition("=")
        alias, upstream = alias.strip(), upstream.strip()
        models[alias.lower()] = upstream or alias
    return models


@dataclass(frozen=True)
class GatewaySettings:
    api_key: str
    upstream_url: str
    child_models: Dict[str, str]
    upstream_api_key: str = ""
    upstream_kind: str = "openai"
    nemo_config_id: str = ""
    child_users: FrozenSet[str] = frozenset()
    adult_users: FrozenSet[str] = frozenset()
    pack: str = "secular-neutral"
    allowed_packs: FrozenSet[str] = frozenset()
    inject_framing: bool = True
    client_system: str = "fold"
    passthrough: bool = False
    passthrough_url: str = ""
    passthrough_api_key: str = ""
    block_log: str = ""
    alert_webhook: str = ""
    timeout: float = 120.0
    allow_insecure_upstream: bool = False
    # Pictures: the conservative values, not the documented defaults. See the
    # note at the end of the module docstring; from_env resolves what an unset
    # environment variable means.
    image_policy: str = "block"
    image_scan_url: str = DEFAULT_SCAN_URL
    image_scan_api_key: str = ""
    image_scan_model: str = DEFAULT_SCAN_MODEL
    image_scan_timeout: float = 10.0
    image_cache_ttl: float = DEFAULT_CACHE_TTL
    image_hosts: FrozenSet[str] = frozenset()
    image_thresholds: Optional[Dict[str, float]] = None
    allow_uploads: bool = False
    # Empty, like the picture fields above: a hand-built settings object
    # offers no tool unless it asks for one.
    photo_providers: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.api_key or "") < MIN_KEY_LENGTH:
            raise SettingsError(f"KITEGATE_API_KEY must be at least {MIN_KEY_LENGTH} characters")
        if not self.upstream_url:
            raise SettingsError("KITEGATE_UPSTREAM_URL is required")
        if not self.child_models:
            raise SettingsError("KITEGATE_CHILD_MODELS must name at least one children's model")
        if self.upstream_kind not in ("openai", "nemo"):
            raise SettingsError("KITEGATE_UPSTREAM_KIND must be 'openai' or 'nemo'")
        if self.client_system not in CLIENT_SYSTEM_POLICIES:
            raise SettingsError(
                f"KITEGATE_CLIENT_SYSTEM must be one of {list(CLIENT_SYSTEM_POLICIES)}")
        both = {str(u).strip().lower() for u in self.child_users} & \
            {str(u).strip().lower() for u in self.adult_users}
        if both:
            raise SettingsError(f"listed as both a child and an adult: {sorted(both)}")
        check_transport("KITEGATE_UPSTREAM_URL", self.upstream_url, self.allow_insecure_upstream)
        if self.passthrough:
            check_transport("KITEGATE_PASSTHROUGH_URL", self.passthrough_url or self.upstream_url,
                            self.allow_insecure_upstream)
        check_transport("KITEGATE_ALERT_WEBHOOK", self.alert_webhook, self.allow_insecure_upstream)
        if self.image_policy not in IMAGE_POLICIES:
            raise SettingsError(f"KITEGATE_IMAGE_POLICY must be one of {list(IMAGE_POLICIES)}")
        if self.image_policy == "scan":
            if not self.image_scan_url:
                raise SettingsError("KITEGATE_IMAGE_SCAN_URL is required when KITEGATE_IMAGE_POLICY=scan")
            if not self.image_scan_api_key:
                # Failing at startup rather than per request: a scanner that
                # cannot authenticate would reject every picture, which looks
                # like a very strict policy instead of a missing key.
                raise SettingsError(
                    "KITEGATE_IMAGE_SCAN_API_KEY is required when KITEGATE_IMAGE_POLICY=scan. "
                    "Use KITEGATE_IMAGE_POLICY=block to show no pictures instead, or unset "
                    "KITEGATE_IMAGE_POLICY to let it fall back to that on its own.")
            check_transport("KITEGATE_IMAGE_SCAN_URL", self.image_scan_url, self.allow_insecure_upstream)
        if self.photo_providers:
            # A typo must stop the gateway, not read as "pictures are off".
            try:
                get_providers(self.photo_providers)
            except ValueError as exc:
                raise SettingsError(f"KITEGATE_PHOTOS: {exc}") from None

    @property
    def photos_enabled(self) -> bool:
        """Whether the gateway offers its photo tool to children's models.

        Not when pictures are blocked: every photograph would be removed on
        the way back, so the lookup would cost a model round trip and a
        request to Wikipedia for nothing a child could see.
        """
        return bool(self.photo_providers) and self.image_policy != "block"

    @property
    def packs(self) -> FrozenSet[str]:
        """Every pack a request may use: the default plus the allowed list."""
        return self.allowed_packs | {self.pack}

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "GatewaySettings":
        e = os.environ if env is None else env
        allowed = (e.get("KITEGATE_ALLOWED_PACKS") or "").strip()
        upstream_url = (e.get("KITEGATE_UPSTREAM_URL") or "").rstrip("/")
        upstream_key = e.get("KITEGATE_UPSTREAM_API_KEY") or ""
        scan_key = e.get("KITEGATE_IMAGE_SCAN_API_KEY") or ""

        # Pictures are on by default, and only ever scanned ones from an
        # allowed host. With no scanner there is no safe way to show one, so
        # the default falls back to "block" rather than refusing to start: a
        # text-only deployment has no opinion about pictures and should not
        # have to state one to come up. An EXPLICIT scan with no key is still
        # a startup error (__post_init__), because that operator asked for
        # something that cannot be delivered and silence would look like a
        # very strict policy instead of a missing key.
        policy = (e.get("KITEGATE_IMAGE_POLICY") or "").strip().lower()
        if not policy:
            policy = "scan" if scan_key else "block"
        hosts = (e.get("KITEGATE_IMAGE_HOSTS") or "").strip()
        image_hosts = (frozenset() if hosts == ANY_IMAGE_HOST
                       else split_list(hosts) if hosts
                       else TRUSTED_IMAGE_HOSTS)
        photos = (e.get("KITEGATE_PHOTOS") or "").strip().lower()
        photo_providers = (() if photos in PHOTOS_OFF
                           else tuple(n.strip() for n in photos.split(",") if n.strip()) if photos
                           else DEFAULT_PROVIDERS)
        return cls(
            api_key=e.get("KITEGATE_API_KEY") or "",
            upstream_url=upstream_url,
            upstream_api_key=upstream_key,
            upstream_kind=(e.get("KITEGATE_UPSTREAM_KIND") or "openai").strip().lower(),
            nemo_config_id=(e.get("KITEGATE_NEMO_CONFIG_ID") or "").strip(),
            child_models=_parse_models(e.get("KITEGATE_CHILD_MODELS") or ""),
            child_users=split_list(e.get("KITEGATE_CHILD_USERS")),
            adult_users=split_list(e.get("KITEGATE_ADULT_USERS")),
            pack=(e.get("KITEGATE_PACK") or "secular-neutral").strip(),
            allowed_packs=frozenset(bundled_packs()) if allowed == "*" else split_list(allowed),
            inject_framing=(e.get("KITEGATE_INJECT_FRAMING") or "true").strip().lower() in _TRUE,
            client_system=(e.get("KITEGATE_CLIENT_SYSTEM") or "fold").strip().lower(),
            passthrough=(e.get("KITEGATE_PASSTHROUGH") or "").strip().lower() in _TRUE,
            passthrough_url=(e.get("KITEGATE_PASSTHROUGH_URL") or upstream_url).rstrip("/"),
            passthrough_api_key=e.get("KITEGATE_PASSTHROUGH_API_KEY") or upstream_key,
            block_log=e.get("KITEGATE_BLOCK_LOG") or "",
            alert_webhook=e.get("KITEGATE_ALERT_WEBHOOK") or "",
            timeout=float(e.get("KITEGATE_UPSTREAM_TIMEOUT") or 120),
            allow_insecure_upstream=(e.get("KITEGATE_ALLOW_INSECURE_UPSTREAM") or "").strip().lower() in _TRUE,
            image_policy=policy,
            image_scan_url=(e.get("KITEGATE_IMAGE_SCAN_URL") or DEFAULT_SCAN_URL).strip().rstrip("/"),
            image_scan_api_key=scan_key,
            image_scan_model=(e.get("KITEGATE_IMAGE_SCAN_MODEL") or DEFAULT_SCAN_MODEL).strip(),
            image_scan_timeout=float(e.get("KITEGATE_IMAGE_SCAN_TIMEOUT") or 10),
            image_cache_ttl=float(e.get("KITEGATE_IMAGE_CACHE_TTL") or DEFAULT_CACHE_TTL),
            image_hosts=image_hosts,
            image_thresholds=_parse_thresholds(e.get("KITEGATE_IMAGE_THRESHOLDS") or ""),
            allow_uploads=(e.get("KITEGATE_IMAGE_UPLOADS") or "").strip().lower() in ("allow", *_TRUE),
            photo_providers=photo_providers,
        )
