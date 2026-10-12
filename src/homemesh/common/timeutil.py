"""Time helpers. All persisted timestamps are UTC ISO-8601 strings with a 'Z' suffix."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utcnow()
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def now_iso() -> str:
    return iso()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def iso_in(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0
