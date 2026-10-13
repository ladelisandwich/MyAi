"""Nodes, node credentials, pairing tokens, hardware reports and the model registry."""

from __future__ import annotations

from typing import Any

from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.database import Row
from homemesh.coordinator.db.repositories.base import Repo, decode, jdump

_NODE_JSON = {"hardware_json": None, "telemetry_json": None, "limits_json": {}, "task_types": [],
              "allowed_roots": [], "allowed_tools": [], "backends_json": [], "build_json": None,
              "update_check_json": None}


class NodeRepo(Repo):
    async def create(self, name: str, *, embedded: bool = False, mode: str = "balanced") -> Row:
        nid = new_id("node")
        now = now_iso()
        await self.db.execute(
            "INSERT INTO nodes(id, name, embedded, mode, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (nid, name, int(embedded), mode, now, now))
        return await self.get(nid)  # type: ignore[return-value]

    async def get(self, node_id: str) -> Row | None:
        return decode(await self.db.fetchone("SELECT * FROM nodes WHERE id=?", (node_id,)), **_NODE_JSON)

    async def list(self, include_revoked: bool = False) -> list[Row]:
        sql = "SELECT * FROM nodes" + ("" if include_revoked else " WHERE status='active'") + " ORDER BY created_at"
        return [decode(r, **_NODE_JSON) for r in await self.db.fetchall(sql)]  # type: ignore[misc]

    async def embedded_node(self) -> Row | None:
        return decode(await self.db.fetchone(
            "SELECT * FROM nodes WHERE embedded=1 AND status='active' ORDER BY created_at LIMIT 1"), **_NODE_JSON)

    async def on_hello(self, node_id: str, *, name: str, os_name: str, arch: str, hostname: str,
                       software_version: str, protocol_version: int, hardware: dict[str, Any],
                       limits: dict[str, Any], task_types: list[str], allowed_roots: list[str],
                       allowed_tools: list[str], backends: list[dict[str, Any]], mode: str | None) -> None:
        now = now_iso()

        def _tx(conn: Any) -> None:
            conn.execute(
                "UPDATE nodes SET os=?, arch=?, hostname=?, software_version=?, protocol_version=?, hardware_json=?,"
                " limits_json=?, task_types=?, allowed_roots=?, allowed_tools=?, backends_json=?, connected=1,"
                " last_connected_at=?, last_seen_at=?, updated_at=?" + (", mode=?" if mode else "") + " WHERE id=?",
                (os_name, arch, hostname, software_version, protocol_version, jdump(hardware), jdump(limits),
                 jdump(task_types), jdump(allowed_roots), jdump(allowed_tools), jdump(backends), now, now, now,
                 *([mode] if mode else []), node_id))
            conn.execute("INSERT INTO hardware_reports(node_id, created_at, report_json) VALUES (?,?,?)",
                         (node_id, now, jdump(hardware)))
            # keep only the 20 most recent hardware reports per node
            conn.execute("DELETE FROM hardware_reports WHERE node_id=? AND id NOT IN "
                         "(SELECT id FROM hardware_reports WHERE node_id=? ORDER BY id DESC LIMIT 20)",
                         (node_id, node_id))

        await self.db.transaction(_tx)

    async def on_heartbeat(self, node_id: str, telemetry: dict[str, Any]) -> None:
        now = now_iso()
        await self.db.execute("UPDATE nodes SET telemetry_json=?, last_seen_at=?, connected=1 WHERE id=?",
                              (jdump(telemetry), now, node_id))

    async def set_build(self, node_id: str, build: dict[str, Any] | None) -> None:
        """What the node runs (``Hello.build`` parsed as ``protocol.update.BuildInfo`` and dumped, or
        ``{"invalid": true}``), stored on every hello; None for a worker without the updater."""
        await self.db.execute("UPDATE nodes SET build_json=? WHERE id=?", (jdump(build), node_id))

    async def set_update_check(self, node_id: str, check: dict[str, Any] | None) -> None:
        """The owner's last update check for this node: ``{job_id, checked_at, result}`` or
        ``{job_id, checked_at, error: {code, message, hint}}`` (decoded as ``update_check_json``)."""
        await self.db.execute("UPDATE nodes SET update_check_json=? WHERE id=?", (jdump(check), node_id))

    async def set_backends(self, node_id: str, backends: list[dict[str, Any]]) -> None:
        await self.db.execute("UPDATE nodes SET backends_json=?, updated_at=? WHERE id=?",
                              (jdump(backends), now_iso(), node_id))

    async def mark_disconnected(self, node_id: str, reason: str) -> None:
        await self.db.execute("UPDATE nodes SET connected=0, last_disconnect_reason=?, updated_at=? WHERE id=?",
                              (reason[:300], now_iso(), node_id))

    async def mark_all_disconnected(self, reason: str) -> None:
        await self.db.execute("UPDATE nodes SET connected=0, last_disconnect_reason=? WHERE connected=1",
                              (reason,))

    async def update(self, node_id: str, **fields: Any) -> None:
        allowed = {"name", "mode", "weight", "allowed_roots"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"cannot update node field {k}")
            sets.append(f"{k}=?")
            vals.append(jdump(v) if k == "allowed_roots" else v)
        if not sets:
            return
        sets.append("updated_at=?")
        vals.append(now_iso())
        await self.db.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=?", (*vals, node_id))

    async def revoke(self, node_id: str) -> None:
        now = now_iso()

        def _tx(conn: Any) -> None:
            conn.execute("UPDATE nodes SET status='revoked', revoked_at=?, connected=0, updated_at=? WHERE id=?",
                         (now, now, node_id))
            conn.execute("UPDATE node_credentials SET revoked_at=? WHERE node_id=? AND revoked_at IS NULL",
                         (now, node_id))
            conn.execute("DELETE FROM models WHERE node_id=?", (node_id,))

        await self.db.transaction(_tx)

    async def delete(self, node_id: str) -> None:
        await self.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))

    # ------------------------------------------------------------------ credentials
    async def add_credential(self, node_id: str, secret_hash: str) -> str:
        cid = new_id("cred")
        await self.db.execute("INSERT INTO node_credentials(id, node_id, secret_hash, created_at) VALUES (?,?,?,?)",
                              (cid, node_id, secret_hash, now_iso()))
        return cid

    async def credential_by_hash(self, secret_hash: str) -> Row | None:
        return await self.db.fetchone(
            "SELECT c.*, n.status AS node_status, n.name AS node_name FROM node_credentials c"
            " JOIN nodes n ON n.id = c.node_id WHERE c.secret_hash=?", (secret_hash,))

    async def touch_credential(self, cred_id: str) -> None:
        await self.db.execute("UPDATE node_credentials SET last_used_at=? WHERE id=?", (now_iso(), cred_id))

    async def revoke_credentials(self, node_id: str, except_id: str | None = None) -> int:
        return await self.db.execute(
            "UPDATE node_credentials SET revoked_at=? WHERE node_id=? AND revoked_at IS NULL AND id IS NOT ?",
            (now_iso(), node_id, except_id))

    # ------------------------------------------------------------------ pairing
    async def create_pairing(self, code_hash: str, expires_at: str, label: str, created_by: str | None) -> Row:
        pid = new_id("pair")
        await self.db.execute(
            "INSERT INTO pairing_tokens(id, code_hash, label, created_by, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?)", (pid, code_hash, label, created_by, now_iso(), expires_at))
        return await self.db.fetchone("SELECT id, label, created_at, expires_at FROM pairing_tokens WHERE id=?",
                                      (pid,))  # type: ignore[return-value]

    async def pairing_by_hash(self, code_hash: str) -> Row | None:
        return await self.db.fetchone("SELECT * FROM pairing_tokens WHERE code_hash=?", (code_hash,))

    async def consume_pairing(self, pairing_id: str, node_id: str) -> bool:
        """Atomically mark a pairing token used. Returns False if it was already used/revoked."""
        n = await self.db.execute(
            "UPDATE pairing_tokens SET used_at=?, used_by_node_id=? WHERE id=? AND used_at IS NULL"
            " AND revoked_at IS NULL", (now_iso(), node_id, pairing_id))
        return n == 1

    async def list_pairings(self) -> list[Row]:
        return await self.db.fetchall(
            "SELECT id, label, created_at, expires_at, used_at, used_by_node_id, revoked_at FROM pairing_tokens"
            " ORDER BY created_at DESC LIMIT 50")

    async def revoke_pairing(self, pairing_id: str) -> int:
        return await self.db.execute("UPDATE pairing_tokens SET revoked_at=? WHERE id=? AND used_at IS NULL",
                                     (now_iso(), pairing_id))

    # ------------------------------------------------------------------ models registry
    async def replace_models(self, node_id: str, models: list[dict[str, Any]]) -> None:
        now = now_iso()

        def _tx(conn: Any) -> None:
            conn.execute("DELETE FROM models WHERE node_id=?", (node_id,))
            conn.executemany(
                "INSERT INTO models(node_id, backend, name, backend_type, info_json, capabilities, loaded, remote,"
                " mock, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(node_id, m["backend"], m["name"], m["backend_type"], jdump(m), jdump(m.get("capabilities", [])),
                  int(bool(m.get("loaded"))), int(bool(m.get("remote"))), int(bool(m.get("mock"))), now)
                 for m in models])

        await self.db.transaction(_tx)

    async def list_models(self, node_id: str | None = None) -> list[Row]:
        if node_id:
            rows = await self.db.fetchall("SELECT * FROM models WHERE node_id=? ORDER BY name", (node_id,))
        else:
            rows = await self.db.fetchall("SELECT * FROM models ORDER BY name, node_id")
        return [decode(r, info_json={}, capabilities=[]) for r in rows]  # type: ignore[misc]

    async def hardware_history(self, node_id: str, limit: int = 5) -> list[Row]:
        rows = await self.db.fetchall(
            "SELECT created_at, report_json FROM hardware_reports WHERE node_id=? ORDER BY id DESC LIMIT ?",
            (node_id, limit))
        return [decode(r, report_json={}) for r in rows]  # type: ignore[misc]
