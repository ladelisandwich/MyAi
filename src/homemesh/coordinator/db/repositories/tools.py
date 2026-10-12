"""Tool policies, approval requests and the tool execution log."""

from __future__ import annotations

from typing import Any

from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode, jdump


class ToolRepo(Repo):
    async def policies(self) -> dict[str, Row]:
        rows = await self.db.fetchall("SELECT * FROM tool_policies")
        return {r["tool_name"]: decode(r) for r in rows}  # type: ignore[misc]

    async def set_policy(self, tool_name: str, enabled: bool, approval: str = "default") -> None:
        await self.db.execute(
            "INSERT INTO tool_policies(tool_name, enabled, approval, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(tool_name) DO UPDATE SET enabled=excluded.enabled, approval=excluded.approval,"
            " updated_at=excluded.updated_at", (tool_name, int(enabled), approval, now_iso()))

    async def create_approval(self, *, user_id: str | None, tool_name: str, arguments: dict[str, Any],
                              node_id: str | None, conversation_id: str | None, message_id: str | None,
                              side_effect: str, expires_at: str) -> Row:
        aid = new_id("appr")
        await self.db.execute(
            "INSERT INTO tool_approvals(id, user_id, tool_name, arguments_json, node_id, conversation_id, message_id,"
            " side_effect, status, requested_at, expires_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)",
            (aid, user_id, tool_name, jdump(arguments), node_id, conversation_id, message_id, side_effect, now_iso(),
             expires_at))
        return await self.get_approval(aid)  # type: ignore[return-value]

    async def get_approval(self, approval_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM tool_approvals WHERE id=?", (approval_id,)),
                      arguments_json={})

    async def decide(self, approval_id: str, approved: bool, decided_by: str, reason: str | None = None) -> bool:
        n = await self.db.execute(
            "UPDATE tool_approvals SET status=?, decided_at=?, decided_by=?, reason=? WHERE id=? AND status='pending'",
            ("approved" if approved else "denied", now_iso(), decided_by, reason, approval_id))
        return n == 1

    async def expire(self, approval_id: str) -> None:
        await self.db.execute("UPDATE tool_approvals SET status='expired', decided_at=? WHERE id=? AND status='pending'",
                              (now_iso(), approval_id))

    async def expire_stale(self) -> int:
        return await self.db.execute(
            "UPDATE tool_approvals SET status='expired', decided_at=? WHERE status='pending' AND expires_at < ?",
            (now_iso(), now_iso()))

    async def list_approvals(self, status: str | None = None, limit: int = 100) -> list[Row]:
        if status:
            rows = await self.db.fetchall("SELECT * FROM tool_approvals WHERE status=? ORDER BY requested_at DESC"
                                          " LIMIT ?", (status, limit))
        else:
            rows = await self.db.fetchall("SELECT * FROM tool_approvals ORDER BY requested_at DESC LIMIT ?", (limit,))
        return [decode(r, arguments_json={}) for r in rows]  # type: ignore[misc]

    async def start_execution(self, *, approval_id: str | None, user_id: str | None, tool_name: str,
                              arguments: dict[str, Any], node_id: str | None, conversation_id: str | None,
                              message_id: str | None, side_effect: str, status: str = "running") -> str:
        eid = new_id("texec")
        await self.db.execute(
            "INSERT INTO tool_executions(id, approval_id, user_id, tool_name, arguments_json, node_id, conversation_id,"
            " message_id, side_effect, status, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, approval_id, user_id, tool_name, jdump(arguments), node_id, conversation_id, message_id, side_effect,
             status, now_iso()))
        return eid

    async def finish_execution(self, execution_id: str, ok: bool, result: Any = None, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE tool_executions SET status=?, result_json=?, error=?, finished_at=? WHERE id=?",
            ("succeeded" if ok else "failed", jdump(result), error, now_iso(), execution_id))

    async def list_executions(self, limit: int = 100) -> list[Row]:
        rows = await self.db.fetchall("SELECT * FROM tool_executions ORDER BY started_at DESC LIMIT ?", (limit,))
        return [decode(r, arguments_json={}, result_json=None) for r in rows]  # type: ignore[misc]
