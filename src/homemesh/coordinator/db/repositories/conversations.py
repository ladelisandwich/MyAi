"""Projects, conversations and messages."""

from __future__ import annotations

from typing import Any

from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode, jdump

_MSG_JSON = {"metrics_json": None, "sources_json": None, "context_json": None, "tool_calls_json": None,
             "attachments_json": None}


def fts_query(text: str, max_terms: int = 12) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms OR-ed together (no FTS syntax injection)."""
    import re

    terms = [t for t in re.findall(r"[\w][\w'-]*", text.lower()) if len(t) > 1][:max_terms]
    if not terms:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


class ProjectRepo(Repo):
    async def create(self, user_id: str, name: str, description: str = "", category: str = "general") -> Row:
        pid = new_id("prj")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO projects(id, user_id, name, description, category, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (pid, user_id, name, description, category, now, now))
        return await self.get(pid, user_id)  # type: ignore[return-value]

    async def get(self, project_id: str, user_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM projects WHERE id=? AND user_id=?",
                                             (project_id, user_id)))

    async def list(self, user_id: str, include_archived: bool = False) -> list[Row]:
        sql = "SELECT * FROM projects WHERE user_id=?" + ("" if include_archived else " AND archived=0")
        return [decode(r) for r in await self.db.fetchall(sql + " ORDER BY name", (user_id,))]  # type: ignore[misc]

    async def update(self, project_id: str, user_id: str, **fields: Any) -> None:
        allowed = {"name", "description", "category", "archived"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [int(v) if isinstance(v, bool) else v for k, v in fields.items() if k in allowed]
        if sets:
            await self.db.execute(f"UPDATE projects SET {', '.join(sets)}, updated_at=? WHERE id=? AND user_id=?",
                                  (*vals, now_iso(), project_id, user_id))

    async def delete(self, project_id: str, user_id: str) -> int:
        return await self.db.execute("DELETE FROM projects WHERE id=? AND user_id=?", (project_id, user_id))


class ConversationRepo(Repo):
    async def create(self, user_id: str, title: str = "New conversation", project_id: str | None = None,
                     settings: dict[str, Any] | None = None) -> Row:
        cid = new_id("conv")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO conversations(id, user_id, project_id, title, settings_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (cid, user_id, project_id, title, jdump(settings or {}), now, now))
        return await self.get(cid, user_id)  # type: ignore[return-value]

    async def get(self, conv_id: str, user_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM conversations WHERE id=? AND user_id=?",
                                             (conv_id, user_id)), settings_json={})

    async def list(self, user_id: str, *, project_id: str | None = None, include_archived: bool = False,
                   limit: int = 200, offset: int = 0) -> list[Row]:
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count"
               " FROM conversations c WHERE c.user_id=?")
        params: list[Any] = [user_id]
        if project_id:
            sql += " AND c.project_id=?"
            params.append(project_id)
        if not include_archived:
            sql += " AND c.archived=0"
        sql += " ORDER BY c.pinned DESC, c.updated_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [decode(r, settings_json={}) for r in await self.db.fetchall(sql, params)]  # type: ignore[misc]

    async def update(self, conv_id: str, user_id: str, **fields: Any) -> None:
        allowed = {"title", "title_source", "project_id", "pinned", "archived", "settings_json"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            vals.append(jdump(v) if k == "settings_json" else (int(v) if isinstance(v, bool) else v))
        if sets:
            await self.db.execute(
                f"UPDATE conversations SET {', '.join(sets)}, updated_at=? WHERE id=? AND user_id=?",
                (*vals, now_iso(), conv_id, user_id))

    async def touch(self, conv_id: str) -> None:
        await self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), conv_id))

    async def delete(self, conv_id: str, user_id: str) -> int:
        return await self.db.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id))

    async def search(self, user_id: str, query: str, limit: int = 30) -> list[Row]:
        q = fts_query(query)
        if not q:
            return []
        return await self.db.fetchall(
            "SELECT m.id AS message_id, m.conversation_id, m.role, m.created_at, c.title,"
            " snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet"
            " FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid"
            " JOIN conversations c ON c.id = m.conversation_id"
            " WHERE messages_fts MATCH ? AND c.user_id=? ORDER BY rank LIMIT ?", (q, user_id, limit))


class MessageRepo(Repo):
    async def create(self, conversation_id: str, role: str, content: str, *, status: str = "complete",
                     parent_id: str | None = None, **extra: Any) -> Row:
        mid = new_id("msg")
        now = now_iso()
        cols = ["id", "conversation_id", "role", "content", "status", "parent_id", "created_at", "updated_at"]
        vals: list[Any] = [mid, conversation_id, role, content, status, parent_id, now, now]
        for k, v in extra.items():
            if k.endswith("_json"):
                v = jdump(v)
            elif isinstance(v, bool):
                v = int(v)
            cols.append(k)
            vals.append(v)
        await self.db.execute(f"INSERT INTO messages({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        return await self.get(mid)  # type: ignore[return-value]

    async def get(self, message_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM messages WHERE id=?", (message_id,)), **_MSG_JSON)

    async def list(self, conversation_id: str, limit: int = 1000) -> list[Row]:
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid LIMIT ?",
            (conversation_id, limit))
        return [decode(r, **_MSG_JSON) for r in rows]  # type: ignore[misc]

    async def update(self, message_id: str, **fields: Any) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            if k.endswith("_json"):
                v = jdump(v)
            elif isinstance(v, bool):
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(now_iso())
        await self.db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", (*vals, message_id))

    async def delete_after(self, conversation_id: str, message_id: str) -> int:
        """Delete a message and everything after it (used when editing/regenerating)."""
        row = await self.db.fetchone("SELECT rowid, created_at FROM messages WHERE id=?", (message_id,))
        if not row:
            return 0
        return await self.db.execute(
            "DELETE FROM messages WHERE conversation_id=? AND (created_at > ? OR (created_at = ? AND rowid >= ?))",
            (conversation_id, row["created_at"], row["created_at"], row["rowid"]))

    async def mark_stale_streaming(self) -> int:
        """After a coordinator restart, messages left 'streaming' were interrupted."""
        return await self.db.execute(
            "UPDATE messages SET status='interrupted', error_code='coordinator_restarted',"
            " error='The coordinator restarted while this response was being generated.', updated_at=?"
            " WHERE status='streaming'", (now_iso(),))
