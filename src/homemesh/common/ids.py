"""Sortable, URL-safe identifiers.

IDs look like ``<prefix>_<26 chars>`` where the 26 characters are a ULID-style
Crockford base32 encoding of a 48-bit millisecond timestamp plus 80 random bits.
They sort roughly by creation time, which keeps SQLite indexes tidy and makes
log correlation easier.
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def ulid() -> str:
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ms, 10) + _encode(rand, 16)


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid().lower()}"
