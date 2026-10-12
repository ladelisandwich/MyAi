"""Minimal TOML serializer for HomeMesh configuration.

Python ships a TOML *reader* (``tomllib``) but no writer. HomeMesh configuration has a
fixed shape - top-level sections containing scalars, lists, small maps and arrays of
tables - so a focused writer avoids an extra dependency:

* top-level dicts become ``[section]`` tables,
* lists of dicts inside a section become ``[[section.key]]`` arrays of tables,
* any other dict is written as an inline table.
"""

from __future__ import annotations

import json
import math
from typing import Any

_BARE_KEY_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def _key(k: str) -> str:
    if k and set(k) <= _BARE_KEY_CHARS:
        return k
    return _string(k)


def _string(s: str) -> str:
    # JSON string escaping is a valid subset of TOML basic-string escaping.
    return json.dumps(s, ensure_ascii=False)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("NaN/inf cannot be stored in configuration")
        return repr(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items() if x is not None) + " }"
    raise TypeError(f"unsupported TOML value type: {type(v).__name__}")


def _is_aot(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _emit_body(out: list[str], table: dict[str, Any]) -> None:
    for k, v in table.items():
        if v is None or _is_aot(v):
            continue
        out.append(f"{_key(k)} = {_value(v)}")


def dumps(data: dict[str, Any]) -> str:
    out: list[str] = []
    top_scalars = {k: v for k, v in data.items() if not isinstance(v, dict) and not _is_aot(v)}
    _emit_body(out, top_scalars)
    for section, table in data.items():
        if not isinstance(table, dict):
            continue
        out.append("")
        out.append(f"[{_key(section)}]")
        _emit_body(out, table)
        for k, v in table.items():
            if _is_aot(v):
                for item in v:
                    out.append("")
                    out.append(f"[[{_key(section)}.{_key(k)}]]")
                    _emit_body(out, item)
    return "\n".join(out).strip() + "\n"
