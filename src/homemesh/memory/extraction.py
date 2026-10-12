"""Optional automatic memory extraction (disabled by default).

When enabled, after a conversation turn the assistant model is asked to list durable facts
the *user explicitly stated about themselves*. Results are stored as **candidates** with
``source=model_inferred``; nothing is used in future conversations until the user approves
it on the Memory page. The prompt forbids guesses, and the parser rejects anything that is
not a short plain statement.
"""

from __future__ import annotations

import json
import re

EXTRACTION_SYSTEM_PROMPT = (
    "You extract long-term memory candidates for a personal assistant. From the user's message, list facts or "
    "preferences the USER EXPLICITLY STATED about themselves, their projects, devices or preferences that would "
    "still be useful weeks from now. Do NOT guess, infer, or include anything the assistant said. Do NOT include "
    "secrets (passwords, keys, financial or health identifiers). If there is nothing, return []. "
    "Reply with ONLY a JSON array of short strings, each a complete sentence in third person starting with "
    "'The user'."
)

_ARRAY = re.compile(r"\[.*\]", re.S)
_SECRETISH = re.compile(r"(password|passphrase|api[_ -]?key|secret|token|ssn|social security|credit card|iban)", re.I)


def build_extraction_messages(user_message: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"User message:\n<<<\n{user_message[:4000]}\n>>>"}]


def parse_candidates(model_output: str, max_items: int = 5) -> list[str]:
    m = _ARRAY.search(model_output or "")
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for it in items:
        if not isinstance(it, str):
            continue
        s = it.strip()
        if not (10 <= len(s) <= 300) or _SECRETISH.search(s) or not s.lower().startswith("the user"):
            continue
        out.append(s)
        if len(out) >= max_items:
            break
    return out
