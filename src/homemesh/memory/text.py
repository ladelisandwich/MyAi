"""Pure memory logic: normalisation, de-duplication and explicit-request detection.

Explicit memories are taken from the user's own words ("remember that I use metric units").
They are stored verbatim with ``source=user_explicit`` - the assistant never paraphrases
them into something the user did not say. Automatically inferred memories are separate
(``source=model_inferred``, ``status=candidate``) and need the user's approval.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[\w']+", re.UNICODE)

EXPLICIT_PATTERNS = [
    re.compile(r"^\s*/remember\s+(?P<c>.+)$", re.I | re.S),
    re.compile(r"^\s*(?:please\s+|hey,?\s+)?(?:can you\s+|could you\s+)?remember(?:\s+that)?\s*[:,-]?\s+(?P<c>.+?)[.!?]?\s*$",
               re.I | re.S),
    re.compile(r"^\s*(?:please\s+)?(?:don'?t|do not)\s+forget(?:\s+that)?\s*[:,-]?\s+(?P<c>.+?)[.!?]?\s*$", re.I | re.S),
    re.compile(r"^\s*(?:note to self|save (?:this|to memory))\s*[:,-]\s*(?P<c>.+)$", re.I | re.S),
]
PREFERENCE_HINTS = re.compile(r"^(i\s+(prefer|like|love|hate|dislike|want|always|never|usually)|always\b|never\b|"
                              r"please\s+(always|never)\b|call me\b|my preferred)", re.I)
MAX_MEMORY_CHARS = 2000


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower().strip()
    t = _WS.sub(" ", t)
    return t.strip(" .!?;:,\"'")


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(normalize(text)) if len(w) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def detect_explicit(message: str) -> str | None:
    """Return the text the user explicitly asked to remember, or None."""
    msg = message.strip()
    if len(msg) > MAX_MEMORY_CHARS + 50 or "\n\n" in msg[:200]:
        return None
    for pat in EXPLICIT_PATTERNS:
        m = pat.match(msg)
        if m:
            content = m.group("c").strip().rstrip(".!?").strip()
            if 3 <= len(content) <= MAX_MEMORY_CHARS:
                return content[0].upper() + content[1:]
    return None


def classify_kind(content: str) -> str:
    return "preference" if PREFERENCE_HINTS.match(content.strip()) else "fact"


def is_near_duplicate(a: str, b: str, threshold: float = 0.85) -> bool:
    return normalize(a) == normalize(b) or jaccard(a, b) >= threshold
