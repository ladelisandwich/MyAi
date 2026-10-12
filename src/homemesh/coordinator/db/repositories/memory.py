"""Long-term memories (user facts, preferences, project notes)."""

from __future__ import annotations

from typing import Any

from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode
from homemesh.coordinator.db.repositories.conversations import fts_query


class MemoryRepo(Repo):
    async def create(self, user_id: str, content: str, normalized_hash: str, *, kind: str = "fact",
                     source: str = "user_explicit", status: str = "active", project_id: str | None = None,
                     conversation_id: str | None = None, message_id: str | None = None,
                     confidence: float | None = None, expires_at: str | None = None) -> Row:
        mid = new_id("mem")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO memories(id, user_id, project_id, kind, content, normalized_hash, source, status, confidence,"
            " conversation_id, message_id, expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, user_id, project_id, kind, content, normalized_hash, source, status, confidence, conversation_id,
             message_id, expires_at, now, now))
        return await self.get(mid, user_id)  # type: ignore[return-value]

    async def get(self, memory_id: str, user_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)))

    async def by_hash(self, user_id: str, normalized_hash: str, project_id: str | None) -> Row | None:
        return decode(await self.db.fetchone(
            "SELECT * FROM memories WHERE user_id=? AND normalized_hash=? AND project_id IS ?"
            " AND status IN ('active','candidate')", (user_id, normalized_hash, project_id)))

    async def list(self, user_id: str, *, status: list[str] | None = None, project_id: str | None = None,
                   kind: str | None = None, limit: int = 500, offset: int = 0, include_global: bool = True) -> list[Row]:
        sql = "SELECT * FROM memories WHERE user_id=?"
        params: list[Any] = [user_id]
        if status:
            sql += f" AND status IN ({','.join('?' * len(status))})"
            params += status
        if project_id:
            sql += " AND (project_id=?" + (" OR project_id IS NULL)" if include_global else ")")
            params.append(project_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " AND (expires_at IS NULL OR expires_at > ?) ORDER BY pinned DESC, updated_at DESC LIMIT ? OFFSET ?"
        params += [now_iso(), limit, offset]
        return [decode(r) for r in await self.db.fetchall(sql, params)]  # type: ignore[misc]

    async def search_fts(self, user_id: str, text: str, *, project_id: str | None = None,
                         limit: int = 20) -> list[Row]:
        q = fts_query(text)
        if not q:
            return []
        sql = ("SELECT m.*, bm25(memories_fts) AS rank_score FROM memories_fts JOIN memories m"
               " ON m.rowid = memories_fts.rowid WHERE memories_fts MATCH ? AND m.user_id=? AND m.status='active'"
               " AND (m.expires_at IS NULL OR m.expires_at > ?)")
        params: list[Any] = [q, user_id, now_iso()]
        if project_id:
            sql += " AND (m.project_id=? OR m.project_id IS NULL)"
            params.append(project_id)
        else:
            sql += " AND m.project_id IS NULL"
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return [decode(r) for r in await self.db.fetchall(sql, params)]  # type: ignore[misc]

    async def update(self, memory_id: str, user_id: str, **fields: Any) -> None:
        allowed = {"content", "normalized_hash", "kind", "status", "project_id", "pinned", "expires_at", "source"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(int(v) if isinstance(v, bool) else v)
        if sets:
            await self.db.execute(f"UPDATE memories SET {', '.join(sets)}, updated_at=? WHERE id=? AND user_id=?",
                                  (*vals, now_iso(), memory_id, user_id))

    async def mark_used(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        now = now_iso()
        await self.db.executemany("UPDATE memories SET use_count=use_count+1, last_used_at=? WHERE id=?",
                                  [(now, m) for m in memory_ids])

    async def delete(self, memory_id: str, user_id: str) -> int:
        def _tx(conn: Any) -> int:
            conn.execute("DELETE FROM embeddings WHERE owner_type='memory' AND owner_id=?", (memory_id,))
            return conn.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)).rowcount

        return await self.db.transaction(_tx)

    async def purge_expired(self) -> int:
        def _tx(conn: Any) -> int:
            ids = [r[0] for r in conn.execute("SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
                                               (now_iso(),))]
            for i in ids:
                conn.execute("DELETE FROM embeddings WHERE owner_type='memory' AND owner_id=?", (i,))
                conn.execute("DELETE FROM memories WHERE id=?", (i,))
            return len(ids)

        return await self.db.transaction(_tx)

    async def counts(self, user_id: str) -> dict[str, int]:
        rows = await self.db.fetchall("SELECT status, COUNT(*) AS n FROM memories WHERE user_id=? GROUP BY status",
                                      (user_id,))
        return {r["status"]: r["n"] for r in rows}
