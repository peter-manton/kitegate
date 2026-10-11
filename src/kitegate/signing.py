"""Pack signatures: who wrote a values pack, and that nobody changed it since.

A pack decides who a child is sent to and what the replies say, so a
tampered pack could send a disclosure to the wrong adult or tell a child
something unkind. Packs therefore carry a detached Ed25519 signature in
"<pack>.yaml.sig", made over the pack's text with line endings normalised
(a Windows checkout must not break it).

Checking, at load time:
  - a bundled pack must be signed by the project key, always;
  - any other pack is checked against the keys you trust (KITEGATE_PACK_KEYS);
  - a signature that does not match, or comes from an untrusted key, is
    always refused;
  - an unsigned pack is refused unless KITEGATE_REQUIRE_SIGNED_PACKS is set to
    false (or 0, no, off), in which case it is loaded with a warning. Unset
    means required, and so does anything that is not an explicit "false": a
    typo must not quietly turn the check off.

Make and use keys with the `kitegate-pack` command (packtool.py).
"""
import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger("kitegate.signing")

SIGNATURE_SUFFIX = ".sig"
SIGNATURE_FORMAT = "kitegate-pack-signature/1"

# Signs the packs bundled with kitegate. Its private half is held by
# the maintainer and is never in this repository.
PROJECT_PUBLIC_KEY = "lVzvx3JxwksNvpvAbu4OooThbpgiCQgwr/e10JBTpe0="

_FALSE = ("0", "false", "no", "off")


class SignatureError(ValueError):
    pass


def canonical(text: str) -> bytes:
    """The bytes a signature covers: UTF-8, LF line endings."""
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _raw_public_key(public_key: str) -> bytes:
    try:
        raw = base64.b64decode(public_key.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise SignatureError(f"not a base64 public key: {public_key!r}") from exc
    if len(raw) != 32:
        raise SignatureError(f"an Ed25519 public key is 32 bytes, got {len(raw)}")
    return raw


def key_id(public_key: str) -> str:
    """Short, stable name for a public key: the first 16 hex digits of its SHA-256."""
    return hashlib.sha256(_raw_public_key(public_key)).hexdigest()[:16]


@dataclass(frozen=True)
class Signature:
    key_id: str
    signature: str  # base64

    def to_text(self) -> str:
        return json.dumps({"format": SIGNATURE_FORMAT, "key_id": self.key_id,
                           "signature": self.signature}) + "\n"

    @classmethod
    def from_text(cls, text: str, source: str = "signature") -> "Signature":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SignatureError(f"{source}: not a pack signature file") from exc
        if not isinstance(data, dict) or data.get("format") != SIGNATURE_FORMAT:
            raise SignatureError(f"{source}: not a {SIGNATURE_FORMAT} file")
        kid, sig = data.get("key_id"), data.get("signature")
        if not isinstance(kid, str) or not isinstance(sig, str):
            raise SignatureError(f"{source}: key_id and signature are required")
        return cls(kid, sig)


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                                       Ed25519PublicKey)
    except ImportError as exc:  # fail closed: a signature we cannot check is not trusted
        raise SignatureError("the 'cryptography' package is needed to check pack signatures") from exc
    return InvalidSignature, serialization, Ed25519PrivateKey, Ed25519PublicKey


def generate_key() -> Tuple[str, str]:
    """A new signing key: (private key PEM, public key base64)."""
    _, serialization, Ed25519PrivateKey, _ = _crypto()
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode("ascii")
    return pem, public_key_of(pem)


def public_key_of(private_pem: str) -> str:
    _, serialization, _, _ = _crypto()
    private = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign(text: str, private_pem: str) -> Signature:
    _, serialization, Ed25519PrivateKey, _ = _crypto()
    private = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise SignatureError("pack signing keys are Ed25519")
    raw = private.sign(canonical(text))
    return Signature(key_id(public_key_of(private_pem)), base64.b64encode(raw).decode("ascii"))


def verify(text: str, signature: Signature, public_key: str) -> bool:
    InvalidSignature, _, _, Ed25519PublicKey = _crypto()
    try:
        raw = base64.b64decode(signature.signature, validate=True)
        Ed25519PublicKey.from_public_bytes(_raw_public_key(public_key)).verify(raw, canonical(text))
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass(frozen=True)
class PackTrust:
    """Which keys may sign packs, and whether unsigned packs are refused."""

    keys: Dict[str, str] = field(default_factory=dict)  # key_id -> public key
    require: bool = False

    @classmethod
    def from_keys(cls, keys: Iterable[str] = (), require: bool = False) -> "PackTrust":
        found = {key_id(k): k.strip() for k in keys if k and k.strip()}
        found.setdefault(key_id(PROJECT_PUBLIC_KEY), PROJECT_PUBLIC_KEY)
        return cls(found, require)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "PackTrust":
        e = os.environ if env is None else env
        keys = (e.get("KITEGATE_PACK_KEYS") or "").split(",")
        # Required unless explicitly switched off: a deployment that says nothing
        # gets the safe behaviour, and a misspelt "false" does too.
        require = (e.get("KITEGATE_REQUIRE_SIGNED_PACKS") or "").strip().lower() not in _FALSE
        return cls.from_keys(keys, require)

    @classmethod
    def bundled(cls) -> "PackTrust":
        """Bundled packs: the project key only, and a signature is required."""
        return cls({key_id(PROJECT_PUBLIC_KEY): PROJECT_PUBLIC_KEY}, True)

    def check(self, text: str, signature: Optional[Signature], source: str) -> None:
        if signature is None:
            if self.require:
                raise SignatureError(f"{source}: the pack is not signed, and signed packs are required")
            logger.warning("kitegate: %s is not signed, and KITEGATE_REQUIRE_SIGNED_PACKS=false "
                           "lets it load. Sign it with kitegate-pack and remove that setting", source)
            return
        public = self.keys.get(signature.key_id)
        if public is None:
            raise SignatureError(f"{source}: signed by key {signature.key_id}, which is not trusted "
                                 "(add its public key to KITEGATE_PACK_KEYS)")
        if not verify(text, signature, public):
            raise SignatureError(f"{source}: the signature does not match; the pack was changed "
                                 "after it was signed")
