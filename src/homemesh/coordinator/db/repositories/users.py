"""Users, browser sessions and API tokens."""

from __future__ import annotations

from typing import Any

from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode, jdump


class UserRepo(Repo):
    async def count(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM users"))

    async def create(self, username: str, password_hash: str, display_name: str = "", role: str = "owner") -> Row:
        uid = new_id("usr")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO users(id, username, display_name, password_hash, role, created_at, updated_at,"
            " password_changed_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, username, display_name or username, password_hash, role, now, now, now),
        )
        return await self.get(uid)  # type: ignore[return-value]

    async def get(self, user_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,)))

    async def by_username(self, username: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM users WHERE username = ?", (username,)))

    async def owner(self) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM users WHERE role='owner' ORDER BY created_at LIMIT 1"))

    async def set_password(self, user_id: str, password_hash: str) -> None:
        now = now_iso()
        await self.db.execute("UPDATE users SET password_hash=?, password_changed_at=?, updated_at=? WHERE id=?",
                              (password_hash, now, now, user_id))

    async def update_profile(self, user_id: str, display_name: str) -> None:
        await self.db.execute("UPDATE users SET display_name=?, updated_at=? WHERE id=?",
                              (display_name, now_iso(), user_id))


class SessionRepo(Repo):
    async def create(self, user_id: str, token_hash: str, expires_at: str, user_agent: str | None,
                     ip: str | None) -> str:
        sid = new_id("ses")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO sessions(id, user_id, token_hash, created_at, expires_at, last_seen_at, user_agent, ip)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, user_id, token_hash, now, expires_at, now, (user_agent or "")[:300], ip),
        )
        return sid

    async def by_token_hash(self, token_hash: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT s.*, u.username, u.display_name, u.role, u.disabled FROM sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (token_hash,))

    async def touch(self, session_id: str) -> None:
        await self.db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now_iso(), session_id))

    async def revoke(self, session_id: str, user_id: str | None = None) -> int:
        if user_id:
            return await self.db.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                                         (now_iso(), session_id, user_id))
        return await self.db.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                     (now_iso(), session_id))

    async def revoke_all_for_user(self, user_id: str, except_id: str | None = None) -> int:
        return await self.db.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND id IS NOT ?",
            (now_iso(), user_id, except_id))

    async def list_for_user(self, user_id: str) -> list[Row]:
        return await self.db.fetchall(
            "SELECT id, created_at, expires_at, last_seen_at, user_agent, ip FROM sessions"
            " WHERE user_id=? AND revoked_at IS NULL AND expires_at > ? ORDER BY last_seen_at DESC",
            (user_id, now_iso()))

    async def purge_expired(self) -> int:
        return await self.db.execute("DELETE FROM sessions WHERE expires_at < ? OR revoked_at IS NOT NULL",
                                     (now_iso(),))


class ApiTokenRepo(Repo):
    async def create(self, user_id: str, name: str, token_hash: str, scopes: list[str],
                     expires_at: str | None) -> Row:
        tid = new_id("tok")
        await self.db.execute(
            "INSERT INTO api_tokens(id, user_id, name, token_hash, scopes, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)", (tid, user_id, name, token_hash, jdump(scopes), now_iso(), expires_at))
        return await self.get(tid)  # type: ignore[return-value]

    async def get(self, token_id: str) -> Row | None:
        return decode(await self.db.fetchone(
            "SELECT id, user_id, name, scopes, created_at, expires_at, last_used_at, revoked_at FROM api_tokens"
            " WHERE id=?", (token_id,)), scopes=[])

    async def by_hash(self, token_hash: str) -> Row | None:
        return decode(await self.db.fetchone(
            "SELECT t.*, u.username, u.display_name, u.role, u.disabled FROM api_tokens t"
            " JOIN users u ON u.id = t.user_id WHERE t.token_hash=?", (token_hash,)), scopes=[])

    async def by_name(self, user_id: str, name: str) -> Row | None:
        return decode(await self.db.fetchone(
            "SELECT * FROM api_tokens WHERE user_id=? AND name=? AND revoked_at IS NULL", (user_id, name)), scopes=[])

    async def touch(self, token_id: str) -> None:
        await self.db.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now_iso(), token_id))

    async def list_for_user(self, user_id: str) -> list[Row]:
        rows = await self.db.fetchall(
            "SELECT id, name, scopes, created_at, expires_at, last_used_at FROM api_tokens"
            " WHERE user_id=? AND revoked_at IS NULL ORDER BY created_at DESC", (user_id,))
        return [decode(r, scopes=[]) for r in rows]  # type: ignore[misc]

    async def revoke(self, token_id: str, user_id: str) -> int:
        return await self.db.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (now_iso(), token_id, user_id))

    async def revoke_by_name(self, user_id: str, name: str) -> int:
        return await self.db.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE user_id=? AND name=? AND revoked_at IS NULL",
            (now_iso(), user_id, name))


def public_user(row: Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {k: row.get(k) for k in ("id", "username", "display_name", "role", "created_at")}
