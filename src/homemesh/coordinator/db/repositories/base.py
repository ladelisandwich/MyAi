"""Shared helpers for repositories."""

from __future__ import annotations

import json
from typing import Any

from homemesh.coordinator.db.database import Database, Row


def jdump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), default=str)


def jload(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def decode(row: Row | None, **json_fields: Any) -> Row | None:
    """Decode JSON columns in-place. ``json_fields`` maps column -> default value."""
    if row is None:
        return None
    for col, default in json_fields.items():
        if col in row:
            row[col] = jload(row[col], default)
    for col in list(row):
        if col in ("mock", "connected", "embedded", "loaded", "remote", "pinned", "archived", "disabled",
                   "idempotent", "cancel_requested", "enabled"):
            row[col] = bool(row[col])
    return row


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db
