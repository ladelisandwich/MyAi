"""Credential helpers: random tokens, hashing, password hashing, pairing codes.

* Passwords are hashed with Argon2id (argon2-cffi defaults, RFC 9106 profile).
* High-entropy machine secrets (session tokens, API tokens, node secrets, pairing codes)
  are stored as SHA-256 digests: they are random, so a slow KDF adds nothing, and a
  fast digest lets us look them up by hash. Plaintext secrets are never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

# Crockford-style alphabet without ambiguous characters (no I, L, O, U).
PAIRING_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

MIN_PASSWORD_LENGTH = 10


def generate_token(nbytes: int = 32, prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str) -> str | None:
    """Return an error message if the password is too weak, else None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
    if len(set(password)) < 4:
        return "Password is too repetitive."
    if len(password) > 1024:
        return "Password is too long."
    return None


def generate_pairing_code(groups: int = 3, group_len: int = 4) -> str:
    """Human-typeable one-time pairing code like ``7KQ2-M9XD-4TRW`` (60 bits of entropy)."""
    chars = [secrets.choice(PAIRING_ALPHABET) for _ in range(groups * group_len)]
    return "-".join("".join(chars[i:i + group_len]) for i in range(0, len(chars), group_len))


def normalize_pairing_code(code: str) -> str:
    """Accept user input with lowercase, spaces, missing dashes and O/0, I/L/1 look-alikes."""
    cleaned = code.strip().upper().replace(" ", "").replace("-", "")
    cleaned = cleaned.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
    return "-".join(cleaned[i:i + 4] for i in range(0, len(cleaned), 4))


def fingerprint_sha256(der_bytes: bytes) -> str:
    """Colon-separated uppercase SHA-256 fingerprint (same format as openssl -fingerprint)."""
    digest = hashlib.sha256(der_bytes).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def normalize_fingerprint(fp: str) -> str:
    raw = fp.strip().upper().removeprefix("SHA256:").replace(":", "").replace(" ", "")
    return ":".join(raw[i:i + 2] for i in range(0, len(raw), 2))
