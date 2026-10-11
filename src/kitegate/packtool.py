"""kitegate-pack: make signing keys, sign packs, check signatures.

    kitegate-pack keygen --out ~/.kitegate --name school-packs
    kitegate-pack sign my-pack.yaml --key ~/.kitegate/school-packs.pem
    kitegate-pack verify my-pack.yaml --key ~/.kitegate/school-packs.pub
    kitegate-pack key-id ~/.kitegate/school-packs.pub

Then trust the public key where packs are loaded: KITEGATE_PACK_KEYS=<public
key> (comma-separated for several). Unsigned packs are refused unless
KITEGATE_REQUIRE_SIGNED_PACKS=false. Keep the .pem file private and off any server
that loads packs: whoever holds it can sign packs your deployment trusts.
"""
from __future__ import annotations

import argparse
import getpass
import glob
import os
import subprocess  # nosec B404 - used once, in _restrict_to_owner, with a fixed executable
import sys
from pathlib import Path
from typing import List

import yaml

from .packs import PackError, _parse
from .signing import (SIGNATURE_SUFFIX, PackTrust, Signature, SignatureError, generate_key,
                      key_id, public_key_of, sign)


def _expand(patterns: List[str]) -> List[Path]:
    out: List[Path] = []
    for pattern in patterns:  # Windows shells do not expand globs
        matches = sorted(glob.glob(os.path.expanduser(pattern)))
        out.extend(Path(m) for m in (matches or [os.path.expanduser(pattern)]))
    return out


def _read_public(value: str) -> str:
    path = Path(os.path.expanduser(value))
    return path.read_text(encoding="utf-8").strip() if path.is_file() else value.strip()


def _windows_account() -> str:
    """DOMAIN\\user for the current account, as icacls wants it."""
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def _restrict_posix(path: Path) -> bool:
    """Mode 0600: readable and writable by the owner, nobody else."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def _restrict_windows(path: Path) -> bool:
    """Remove inherited access and grant the current account alone.

    os.open's mode does nothing on Windows, where a new file inherits the
    folder's access list (often Administrators, SYSTEM and sometimes Users).
    """
    icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
    # A fixed executable and a list of arguments, no shell: nothing in this
    # call comes from outside the process except the file name.
    command = [icacls, str(path), "/inheritance:r", "/grant:r", f"{_windows_account()}:F"]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=30)  # nosec B603
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _restrict_to_owner(path: Path) -> bool:
    """Make a private key usable by the current account only. True if done."""
    return _restrict_windows(path) if os.name == "nt" else _restrict_posix(path)


def cmd_keygen(args: argparse.Namespace) -> int:
    out = Path(os.path.expanduser(args.out))
    out.mkdir(parents=True, exist_ok=True)
    private, public = out / f"{args.name}.pem", out / f"{args.name}.pub"
    if private.exists() or public.exists():
        print(f"refusing to overwrite {private} or {public}", file=sys.stderr)
        return 1
    pem, pub = generate_key()
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
        # Restricted while the file is still empty, so the key is never
        # written to a file other accounts can read.
        restricted = _restrict_to_owner(private)
        fh.write(pem)
    if not restricted:
        print(f"WARNING: could not limit {private} to your account. Do it by hand before "
              "using the key: a private key other accounts can read can sign packs.",
              file=sys.stderr)
    public.write_text(pub + "\n", encoding="ascii", newline="\n")
    print(f"private key: {private}  (keep it secret, back it up offline)")
    print(f"public key:  {public}")
    print(f"key id:      {key_id(pub)}")
    print(f"trust it with: KITEGATE_PACK_KEYS={pub}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    pem = Path(os.path.expanduser(args.key)).read_text(encoding="ascii")
    kid = key_id(public_key_of(pem))
    for path in _expand(args.packs):
        text = path.read_text(encoding="utf-8")
        try:
            _parse(yaml.safe_load(text), str(path))  # never sign a pack that will not load
        except PackError as exc:
            print(f"not signed: {exc}", file=sys.stderr)
            return 1
        sig_path = path.with_name(path.name + SIGNATURE_SUFFIX)
        sig_path.write_text(sign(text, pem).to_text(), encoding="ascii", newline="\n")
        print(f"signed {path} with key {kid}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    keys = [_read_public(k) for k in args.key or []]
    keys += (os.environ.get("KITEGATE_PACK_KEYS") or "").split(",")
    trust = PackTrust.from_keys(keys, require=True)
    failed = 0
    for path in _expand(args.packs):
        sig_path = path.with_name(path.name + SIGNATURE_SUFFIX)
        try:
            sig = Signature.from_text(sig_path.read_text(encoding="ascii"), str(sig_path)) \
                if sig_path.is_file() else None
            trust.check(path.read_text(encoding="utf-8"), sig, str(path))
            print(f"ok      {path} (key {sig.key_id})")
        except SignatureError as exc:
            failed += 1
            print(f"FAILED  {exc}", file=sys.stderr)
    return 1 if failed else 0


def cmd_key_id(args: argparse.Namespace) -> int:
    print(key_id(_read_public(args.public_key)))
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kitegate-pack", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="make a new Ed25519 signing key")
    p.add_argument("--out", default="~/.kitegate", help="directory for the key files")
    p.add_argument("--name", default="pack-signing-key", help="file name, without extension")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser("sign", help="write PACK.yaml.sig next to each pack")
    p.add_argument("packs", nargs="+")
    p.add_argument("--key", required=True, help="private key (.pem)")
    p.set_defaults(func=cmd_sign)

    p = sub.add_parser("verify", help="check signatures against trusted keys")
    p.add_argument("packs", nargs="+")
    p.add_argument("--key", action="append", help="public key, or a .pub file (repeatable)")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("key-id", help="print a public key's id")
    p.add_argument("public_key", help="public key, or a .pub file")
    p.set_defaults(func=cmd_key_id)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SignatureError, OSError) as exc:
        print(f"kitegate-pack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
