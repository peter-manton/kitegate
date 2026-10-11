"""Pack signatures: bundled packs, your own packs, and the kitegate-pack tool."""
import shutil
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
