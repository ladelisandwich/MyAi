"""Durable job records and job event logs."""

from __future__ import annotations

from typing import Any

from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode, jdump

_JOB_JSON = {"payload_json": {}, "result_json": None, "decision_json": None, "metrics_json": None}


class JobRepo(Repo):
    async def create(self, job: dict[str, Any]) -> None:
        now = now_iso()
        cols = ["id", "type", "status", "priority", "user_id", "conversation_id", "message_id", "collection_id",
                "requested_node_id", "model", "backend", "payload_json", "max_attempts", "idempotent",
                "parent_job_id", "created_at", "updated_at"]
        vals = [job["id"], job["type"], job.get("status", "queued"), job.get("priority", "normal"),
                job.get("user_id"), job.get("conversation_id"), job.get("message_id"), job.get("collection_id"),
                job.get("requested_node_id"), job.get("model"), job.get("backend"), jdump(job.get("payload", {})),
                job.get("max_attempts", 1), int(bool(job.get("idempotent"))), job.get("parent_job_id"), now, now]
        await self.db.execute(f"INSERT INTO jobs({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)

    async def get(self, job_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM jobs WHERE id=?", (job_id,)), **_JOB_JSON)

    async def update(self, job_id: str, **fields: Any) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            if k in ("payload", "result", "decision", "metrics"):
                k = f"{k}_json"
            if k.endswith("_json"):
                v = jdump(v)
            elif isinstance(v, bool):
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(now_iso())
        await self.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*vals, job_id))

    async def transition(self, job_id: str, from_statuses: tuple[str, ...], to_status: str, **fields: Any) -> bool:
        """Compare-and-set status change. Returns False if the job was not in ``from_statuses``."""
        sets, vals = ["status=?", "updated_at=?"], [to_status, now_iso()]
        for k, v in fields.items():
            if k in ("payload", "result", "decision", "metrics"):
                k = f"{k}_json"
            if k.endswith("_json"):
                v = jdump(v)
            elif isinstance(v, bool):
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        placeholders = ",".join("?" * len(from_statuses))
        n = await self.db.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id=? AND status IN ({placeholders})",
            (*vals, job_id, *from_statuses))
        return n == 1

    async def list(self, *, status: list[str] | None = None, job_type: str | None = None,
                   node_id: str | None = None, limit: int = 100, offset: int = 0) -> list[Row]:
        sql = ("SELECT id, type, status, priority, conversation_id, message_id, collection_id, node_id,"
               " requested_node_id, model, backend, mock, error_code, error, progress, progress_message, attempts,"
               " max_attempts, idempotent, cancel_requested, parent_job_id, metrics_json, decision_json,"
               " created_at, dispatched_at, started_at, finished_at, updated_at FROM jobs WHERE 1=1")
        params: list[Any] = []
        if status:
            sql += f" AND status IN ({','.join('?' * len(status))})"
            params += status
        if job_type:
            sql += " AND type=?"
            params.append(job_type)
        if node_id:
            sql += " AND node_id=?"
            params.append(node_id)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [decode(r, metrics_json=None, decision_json=None) for r in await self.db.fetchall(sql, params)]  # type: ignore[misc]

    async def counts_by_status(self) -> dict[str, int]:
        rows = await self.db.fetchall("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    async def queued(self, limit: int = 100) -> list[Row]:
        rows = await self.db.fetchall(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY CASE priority WHEN 'interactive' THEN 0"
            " WHEN 'normal' THEN 1 ELSE 2 END, created_at LIMIT ?", (limit,))
        return [decode(r, **_JOB_JSON) for r in rows]  # type: ignore[misc]

    async def count_queued(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM jobs WHERE status='queued'"))

    async def active_on_node(self, node_id: str) -> list[Row]:
        rows = await self.db.fetchall("SELECT * FROM jobs WHERE node_id=? AND status IN ('dispatched','running')",
                                      (node_id,))
        return [decode(r, **_JOB_JSON) for r in rows]  # type: ignore[misc]

    async def unfinished(self) -> list[Row]:
        rows = await self.db.fetchall("SELECT * FROM jobs WHERE status IN ('queued','dispatched','running')")
        return [decode(r, **_JOB_JSON) for r in rows]  # type: ignore[misc]

    async def add_event(self, job_id: str, kind: str, message: str = "", data: dict[str, Any] | None = None,
                        level: str = "info") -> None:
        await self.db.execute("INSERT INTO job_events(job_id, ts, level, kind, message, data_json) VALUES (?,?,?,?,?,?)",
                              (job_id, now_iso(), level, kind, message[:2000], jdump(data)))

    async def events(self, job_id: str, limit: int = 500) -> list[Row]:
        rows = await self.db.fetchall("SELECT * FROM job_events WHERE job_id=? ORDER BY id LIMIT ?", (job_id, limit))
        return [decode(r, data_json=None) for r in rows]  # type: ignore[misc]

    async def recent_errors(self, limit: int = 10) -> list[Row]:
        return await self.db.fetchall(
            "SELECT id, type, node_id, model, error_code, error, finished_at FROM jobs WHERE status='failed'"
            " ORDER BY finished_at DESC LIMIT ?", (limit,))

    async def metrics_summary(self, limit: int = 200) -> list[Row]:
        rows = await self.db.fetchall(
            "SELECT type, node_id, model, mock, metrics_json, finished_at FROM jobs WHERE status='completed'"
            " AND metrics_json IS NOT NULL ORDER BY finished_at DESC LIMIT ?", (limit,))
        return [decode(r, metrics_json={}) for r in rows]  # type: ignore[misc]

    async def purge_finished_before(self, iso_ts: str) -> int:
        return await self.db.execute(
            "DELETE FROM jobs WHERE status IN ('completed','failed','cancelled','interrupted') AND finished_at < ?",
            (iso_ts,))
