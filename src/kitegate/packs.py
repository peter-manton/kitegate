"""Values packs: the part of the policy each deployment chooses.

A pack declares who the child is sent to, which topics are redirected to that
person, the wording of the replies, and the framing given to the model. It can
NOT weaken the safety core: there is no key for disabling distress handling,
hard blocks or PII screening, and unknown keys are rejected so a typo cannot
silently do nothing.

Packs carry a detached signature (`<pack>.yaml.sig`, see signing.py). Bundled
packs must be signed by the project key; other packs are checked against the
keys you trust, and can be required to be signed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import List, Optional, Union

import yaml

from .core import Rule, compile_rules
from .signing import SIGNATURE_SUFFIX, PackTrust, Signature, SignatureError

_ALLOWED_KEYS = {"name", "description", "trusted_adult", "framing", "redirect", "replies"}
_REPLY_KEYS = {"hard_block", "redirect", "pii"}


class PackError(ValueError):
    pass


@dataclass(frozen=True)
class Replies:
    hard_block: str
    redirect: str
    pii: str


@dataclass(frozen=True)
class ValuesPack:
    name: str
    description: str
    trusted_adult: str
    framing: str
    redirect_rules: List[Rule]
    replies: Replies


def _parse(data: dict, source: str) -> ValuesPack:
    if not isinstance(data, dict):
        raise PackError(f"{source}: pack must be a mapping")
    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise PackError(f"{source}: unknown keys {sorted(unknown)}")
    for key in ("name", "trusted_adult", "replies"):
        if not data.get(key):
            raise PackError(f"{source}: missing required key '{key}'")

    replies = data["replies"]
    if not isinstance(replies, dict) or set(replies) != _REPLY_KEYS:
        raise PackError(f"{source}: replies must define exactly {sorted(_REPLY_KEYS)}")

    redirect = data.get("redirect") or {}
    if not isinstance(redirect, dict):
        raise PackError(f"{source}: redirect must map rule labels to regexes")
    pairs = []
    for label, pattern in redirect.items():
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PackError(f"{source}: redirect.{label} is not a valid regex: {exc}") from exc
        if not pattern.startswith(r"\b"):
            # Unanchored patterns match inside innocent words (the
            # Scunthorpe problem); enforce anchoring rather than document it.
            raise PackError(f"{source}: redirect.{label} must start with \\b")
        pairs.append((f"scope.{label}", pattern))

    return ValuesPack(
        name=str(data["name"]),
        description=str(data.get("description") or ""),
        trusted_adult=str(data["trusted_adult"]),
        framing=str(data.get("framing") or "").strip(),
        redirect_rules=compile_rules(pairs),
        replies=Replies(**{k: str(v).strip() for k, v in replies.items()}),
    )


def _signature(text: Optional[str], source: str) -> Optional[Signature]:
    return Signature.from_text(text, source) if text is not None else None


def load_pack(name_or_path: Union[str, Path], trust: Optional[PackTrust] = None) -> ValuesPack:
    """Load a bundled pack by name ("uk-school") or any pack file by path.

    The signature is checked first. Bundled packs must carry the project's
    signature. Other packs are checked against `trust` (default: from the
    environment, KITEGATE_PACK_KEYS and KITEGATE_REQUIRE_SIGNED_PACKS).
    """
    path = Path(name_or_path)
    try:
        if path.suffix in (".yaml", ".yml") and path.exists():
            text, source = path.read_text(encoding="utf-8"), str(path)
            sig_path = path.with_name(path.name + SIGNATURE_SUFFIX)
            sig_text = sig_path.read_text(encoding="ascii") if sig_path.is_file() else None
            (trust or PackTrust.from_env()).check(text, _signature(sig_text, str(sig_path)), source)
        else:
            folder = resources.files("kitegate").joinpath("packs")
            ref = folder.joinpath(f"{name_or_path}.yaml")
            if not ref.is_file():
                raise PackError(f"no bundled pack named '{name_or_path}'")
            text, source = ref.read_text(encoding="utf-8"), f"packs/{name_or_path}.yaml"
            sig_ref = folder.joinpath(f"{name_or_path}.yaml{SIGNATURE_SUFFIX}")
            sig_text = sig_ref.read_text(encoding="ascii") if sig_ref.is_file() else None
            PackTrust.bundled().check(text, _signature(sig_text, source + SIGNATURE_SUFFIX), source)
    except SignatureError as exc:
        raise PackError(str(exc)) from exc
    return _parse(yaml.safe_load(text), source)


def bundled_packs() -> List[str]:
    folder = resources.files("kitegate").joinpath("packs")
    return sorted(p.name[:-5] for p in folder.iterdir() if p.name.endswith(".yaml"))
