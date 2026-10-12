"""Application settings (key/value JSON) and the audit log."""

from __future__ import annotations

from typing import Any

from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode, jdump, jload


class SettingsRepo(Repo):
    async def get(self, key: str, default: Any = None) -> Any:
        v = await self.db.fetchval("SELECT value_json FROM settings WHERE key=?", (key,))
        return jload(v, default) if v is not None else default

    async def all(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT key, value_json FROM settings")
        return {r["key"]: jload(r["value_json"]) for r in rows}

    async def set(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, jdump(value), now_iso()))


class AuditRepo(Repo):
    async def add(self, actor_type: str, actor_id: str | None, action: str, *, target_type: str | None = None,
                  target_id: str | None = None, outcome: str = "success", ip: str | None = None,
                  details: dict[str, Any] | None = None) -> None:
        await self.db.execute(
            "INSERT INTO audit_events(ts, actor_type, actor_id, action, target_type, target_id, outcome, ip,"
            " details_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), actor_type, actor_id, action, target_type, target_id, outcome, ip, jdump(details)))

    async def list(self, limit: int = 200, action_prefix: str | None = None) -> list[Row]:
        if action_prefix:
            rows = await self.db.fetchall(
                "SELECT * FROM audit_events WHERE action LIKE ? ORDER BY id DESC LIMIT ?",
                (action_prefix.replace("%", "") + "%", limit))
        else:
            rows = await self.db.fetchall("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))
        return [decode(r, details_json=None) for r in rows]  # type: ignore[misc]

    async def purge_before(self, iso_ts: str) -> int:
        return await self.db.execute("DELETE FROM audit_events WHERE ts < ?", (iso_ts,))
