"""Pack signatures: bundled packs, your own packs, and the kitegate-pack tool."""
import os
import shutil
import stat
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from kitegate import PackError, bundled_packs, load_pack
from kitegate import signing
from kitegate.packtool import main as packtool
from kitegate.signing import PackTrust, Signature, SignatureError, generate_key, sign

PACK = ("name: test-pack\ntrusted_adult: a grown-up\n"
        "replies: {hard_block: a, redirect: b, pii: c}\n")


@pytest.fixture(scope="module")
def key():
    return generate_key()  # (private PEM, public base64)


@pytest.fixture
def pack(tmp_path, key):
    """A pack file signed with `key`."""
    path = tmp_path / "test-pack.yaml"
    path.write_text(PACK, encoding="utf-8", newline="\n")
    (tmp_path / "test-pack.yaml.sig").write_text(sign(PACK, key[0]).to_text(), encoding="ascii")
    return path


# -- bundled packs --------------------------------------------------------------
def test_bundled_packs_are_signed_by_the_project_key():
    folder = resources.files("kitegate").joinpath("packs")
    for name in bundled_packs():
        text = folder.joinpath(f"{name}.yaml").read_text(encoding="utf-8")
        sig = Signature.from_text(folder.joinpath(f"{name}.yaml.sig").read_text(encoding="ascii"))
        assert sig.key_id == signing.key_id(signing.PROJECT_PUBLIC_KEY), name
        assert signing.verify(text, sig, signing.PROJECT_PUBLIC_KEY), (
            f"{name}.yaml changed since it was signed: re-sign with kitegate-pack sign")


def test_tampered_bundled_pack_is_refused(tmp_path, monkeypatch):
    # A copy of the package's packs with one reply changed, signatures untouched.
    copy = tmp_path / "kitegate" / "packs"
    shutil.copytree(Path(resources.files("kitegate").joinpath("packs")), copy)
    target = copy / "uk-school.yaml"
    target.write_text(target.read_text(encoding="utf-8").replace("your teacher", "a stranger"),
                      encoding="utf-8")
    monkeypatch.setattr(resources, "files", lambda _pkg: tmp_path / "kitegate")
    with pytest.raises(PackError, match="does not match"):
        load_pack("uk-school")
    assert load_pack("secular-neutral").name == "secular-neutral"  # untouched packs still load


def test_bundled_pack_without_signature_is_refused(tmp_path, monkeypatch):
    copy = tmp_path / "kitegate" / "packs"
    shutil.copytree(Path(resources.files("kitegate").joinpath("packs")), copy)
    (copy / "uk-school.yaml.sig").unlink()
    monkeypatch.setattr(resources, "files", lambda _pkg: tmp_path / "kitegate")
    with pytest.raises(PackError, match="not signed"):
        load_pack("uk-school")


# -- your own packs ---------------------------------------------------------------
def test_signed_pack_with_trusted_key_loads(pack, key):
    assert load_pack(pack, trust=PackTrust.from_keys([key[1]], require=True)).name == "test-pack"


def test_trusted_key_from_environment(pack, key, monkeypatch):
    monkeypatch.setenv("KITEGATE_PACK_KEYS", key[1])
    monkeypatch.setenv("KITEGATE_REQUIRE_SIGNED_PACKS", "true")
    assert load_pack(pack).name == "test-pack"


def test_untrusted_key_is_refused(pack):
    with pytest.raises(PackError, match="not trusted"):
        load_pack(pack, trust=PackTrust.from_keys([]))


def test_edited_pack_is_refused(pack, key):
    pack.write_text(PACK.replace("a grown-up", "nobody"), encoding="utf-8")
    with pytest.raises(PackError, match="does not match"):
        load_pack(pack, trust=PackTrust.from_keys([key[1]]))


def test_signature_from_another_key_with_a_forged_id_is_refused(pack, key):
    other_private, _ = generate_key()
    forged = Signature(signing.key_id(key[1]), sign(PACK, other_private).signature)
    pack.with_name("test-pack.yaml.sig").write_text(forged.to_text(), encoding="ascii")
    with pytest.raises(PackError, match="does not match"):
        load_pack(pack, trust=PackTrust.from_keys([key[1]]))


def test_unsigned_pack_refused_when_required(tmp_path):
    path = tmp_path / "plain.yaml"
    path.write_text(PACK, encoding="utf-8")
    with pytest.raises(PackError, match="not signed"):
        load_pack(path, trust=PackTrust.from_keys([], require=True))


def test_signatures_are_required_when_the_setting_is_not_mentioned():
    assert PackTrust.from_env({}).require is True
    assert PackTrust.from_env({"KITEGATE_REQUIRE_SIGNED_PACKS": ""}).require is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " false "])
def test_only_an_explicit_false_turns_the_requirement_off(value):
    assert PackTrust.from_env({"KITEGATE_REQUIRE_SIGNED_PACKS": value}).require is False


@pytest.mark.parametrize("value", ["flase", "nope", "disabled", "true", "1"])
def test_a_typo_does_not_turn_the_requirement_off(value):
    assert PackTrust.from_env({"KITEGATE_REQUIRE_SIGNED_PACKS": value}).require is True


def test_an_unsigned_pack_is_refused_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("KITEGATE_REQUIRE_SIGNED_PACKS", raising=False)
    path = tmp_path / "plain.yaml"
    path.write_text(PACK, encoding="utf-8")
    with pytest.raises(PackError, match="not signed"):
        load_pack(path)


def test_unsigned_pack_loads_with_a_warning_when_not_required(tmp_path, caplog):
    path = tmp_path / "plain.yaml"
    path.write_text(PACK, encoding="utf-8")
    assert load_pack(path, trust=PackTrust.from_keys([])).name == "test-pack"
    assert "not signed" in caplog.text


def test_windows_line_endings_do_not_break_a_signature(pack, key):
    pack.write_bytes(PACK.replace("\n", "\r\n").encode("utf-8"))
    assert load_pack(pack, trust=PackTrust.from_keys([key[1]], require=True)).name == "test-pack"


def test_garbage_signature_file_is_refused(pack, key):
    pack.with_name("test-pack.yaml.sig").write_text("not json", encoding="ascii")
    with pytest.raises(PackError, match="not a pack signature"):
        load_pack(pack, trust=PackTrust.from_keys([key[1]]))


def test_missing_cryptography_fails_closed(pack, key, monkeypatch):
    def unavailable():
        raise SignatureError("the 'cryptography' package is needed to check pack signatures")
    monkeypatch.setattr(signing, "_crypto", unavailable)
    with pytest.raises(PackError, match="cryptography"):
        load_pack(pack, trust=PackTrust.from_keys([key[1]]))


def test_bad_public_key_is_rejected():
    with pytest.raises(SignatureError):
        PackTrust.from_keys(["not-a-key"])


# -- the private key is the owner's alone ----------------------------------------------
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows access lists")


@posix_only
def test_keygen_makes_the_private_key_mode_0600(tmp_path):
    assert packtool(["keygen", "--out", str(tmp_path), "--name", "k"]) == 0
    assert stat.S_IMODE((tmp_path / "k.pem").stat().st_mode) == 0o600


@posix_only
def test_keygen_tightens_the_key_whatever_the_umask(tmp_path):
    old = os.umask(0)          # the most permissive umask there is
    try:
        assert packtool(["keygen", "--out", str(tmp_path), "--name", "k"]) == 0
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "k.pem").stat().st_mode) == 0o600


@windows_only
def test_keygen_leaves_the_private_key_to_the_current_account_only(tmp_path):
    assert packtool(["keygen", "--out", str(tmp_path), "--name", "k"]) == 0
    acl = subprocess.run(["icacls", str(tmp_path / "k.pem")], capture_output=True, text=True,
                         check=True).stdout
    entries = [line for line in acl.splitlines() if ":(" in line]
    assert len(entries) == 1, acl                      # one entry: no inherited ones left
    assert "(I)" not in entries[0], acl                # and it is not inherited
    assert os.environ["USERNAME"].lower() in entries[0].lower()


def test_the_windows_restriction_removes_inheritance_and_grants_only_the_account(tmp_path, monkeypatch):
    from kitegate import packtool as tool
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    monkeypatch.setattr(tool, "_windows_account", lambda: "HOST\\owner")
    key = tmp_path / "k.pem"
    assert tool._restrict_windows(key) is True
    assert seen["command"][0].lower().endswith("icacls.exe")
    assert seen["command"][1:] == [str(key), "/inheritance:r", "/grant:r", "HOST\\owner:F"]
    assert seen["kwargs"].get("shell") is not True      # a list of arguments, never a shell string


@pytest.mark.parametrize("error", [OSError("no icacls"), subprocess.CalledProcessError(5, "icacls")])
def test_a_failed_restriction_is_reported_not_hidden(tmp_path, monkeypatch, capsys, error):
    from kitegate import packtool as tool

    def failing(command, **kwargs):
        raise error

    monkeypatch.setattr(tool.subprocess, "run", failing)
    assert tool._restrict_windows(tmp_path / "k.pem") is False
    # And keygen, told the restriction failed, still writes the key but says so.
    monkeypatch.setattr(tool, "_restrict_to_owner", lambda path: False)
    assert tool.main(["keygen", "--out", str(tmp_path), "--name", "k"]) == 0
    assert "could not limit" in capsys.readouterr().err


# -- kitegate-pack ------------------------------------------------------------------
def test_tool_keygen_sign_verify(tmp_path, capsys):
    assert packtool(["keygen", "--out", str(tmp_path), "--name", "k"]) == 0
    assert packtool(["keygen", "--out", str(tmp_path), "--name", "k"]) == 1  # never overwrites
    pack = tmp_path / "mine.yaml"
    pack.write_text(PACK, encoding="utf-8")
    assert packtool(["sign", str(pack), "--key", str(tmp_path / "k.pem")]) == 0
    assert packtool(["verify", str(pack), "--key", str(tmp_path / "k.pub")]) == 0
    assert packtool(["verify", str(pack)]) == 1  # key not trusted
    pack.write_text(PACK + "description: changed\n", encoding="utf-8")
    assert packtool(["verify", str(pack), "--key", str(tmp_path / "k.pub")]) == 1
    capsys.readouterr()
    public = (tmp_path / "k.pub").read_text().strip()
    assert packtool(["key-id", public]) == 0
    assert capsys.readouterr().out.strip() == signing.key_id(public)


def test_tool_refuses_to_sign_an_invalid_pack(tmp_path):
    assert packtool(["keygen", "--out", str(tmp_path), "--name", "k"]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\n", encoding="utf-8")
    assert packtool(["sign", str(bad), "--key", str(tmp_path / "k.pem")]) == 1
    assert not bad.with_name("bad.yaml.sig").exists()
