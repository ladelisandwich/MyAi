
import pytest

from fsutil import symlink_or_skip
from homemesh.common.security import (
    fingerprint_sha256,
    generate_pairing_code,
    hash_password,
    hash_token,
    normalize_fingerprint,
    normalize_pairing_code,
    validate_password_strength,
    verify_password,
)
from homemesh.knowledge.pathsafety import PathPolicy, is_sensitive, validate_new_root
from homemesh.tools.safe_eval import CalcError, safe_eval


def test_password_hashing_argon2id():
    h = hash_password("a long passphrase")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "a long passphrase")
    assert not verify_password(h, "wrong")
    assert not verify_password("garbage", "x")


def test_password_strength():
    assert validate_password_strength("short")
    assert validate_password_strength("aaaaaaaaaaaa")
    assert validate_password_strength("correct horse battery") is None


def test_token_hash_is_deterministic_and_not_plaintext():
    assert hash_token("abc") == hash_token("abc") != "abc"


def test_pairing_codes_are_random_and_normalised():
    codes = {generate_pairing_code() for _ in range(200)}
    assert len(codes) == 200
    c = generate_pairing_code()
    assert len(c) == 14 and c.count("-") == 2
    assert normalize_pairing_code(c.lower().replace("-", " ")) == c
    assert normalize_pairing_code("abcd-efgh-o1l0") == "ABCD-EFGH-0110"


def test_fingerprint_formats():
    fp = fingerprint_sha256(b"hello")
    assert fp.count(":") == 31
    assert normalize_fingerprint("sha256:" + fp.replace(":", "").lower()) == fp


# ---------------------------------------------------------------- path safety

def test_sensitive_paths(tmp_path):
    for p in ("/home/u/.ssh/id_ed25519", "/home/u/.gnupg/x", "/home/u/proj/.env", "/x/server.key", "/x/vault.kdbx",
              "C:/Users/u/AppData/Roaming/Microsoft/Credentials/abc", "/home/u/.mozilla/firefox/cookies.sqlite"):
        assert is_sensitive(type(tmp_path)(p)), p
    assert not is_sensitive(type(tmp_path)("/home/u/Documents/notes.md"))


def test_refuses_disk_root_and_home(tmp_path, monkeypatch):
    with pytest.raises(PermissionError, match="entire filesystem"):
        validate_new_root(type(tmp_path)("/"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    with pytest.raises(PermissionError, match="home folder"):
        validate_new_root(tmp_path)
    (tmp_path / "Notes").mkdir()
    assert validate_new_root(tmp_path / "Notes") == (tmp_path / "Notes").resolve()


def test_traversal_and_symlink_escape_blocked(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "ok.txt").write_text("fine")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    symlink_or_skip(secret, root / "link.txt")
    pol = PathPolicy([root])
    assert pol.resolve(root / "ok.txt").name == "ok.txt"
    with pytest.raises(PermissionError):
        pol.resolve(root / ".." / "secret.txt")
    with pytest.raises(PermissionError):
        pol.resolve(root / "link.txt")
    with pytest.raises(PermissionError):
        pol.resolve(secret)
    (root / ".env").write_text("TOKEN=1")
    with pytest.raises(PermissionError, match="credential or secret"):
        pol.resolve(root / ".env")


def test_collection_root_must_be_inside_allowed(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    pol = PathPolicy([a])
    assert pol.check_root(a)
    with pytest.raises(PermissionError, match="outside"):
        pol.check_root(b)
    with pytest.raises(PermissionError, match="No folders are authorised"):
        PathPolicy([]).check_root(a)


# ---------------------------------------------------------------- calculator

def test_calculator_math():
    assert safe_eval("2+2*3") == 8
    assert safe_eval("sqrt(16) + 2^3") == 12
    assert abs(safe_eval("sin(pi/2)") - 1) < 1e-9


@pytest.mark.parametrize("expr", ["__import__('os')", "(1).__class__", "open('x')", "[1,2]", "9**9**9",
                                  "a+1", "lambda: 1", "factorial(10000)", "1/0", "x" * 600])
def test_calculator_rejects_unsafe_or_huge(expr):
    with pytest.raises(CalcError):
        safe_eval(expr)
