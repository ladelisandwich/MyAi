"""Node registry, worker connections, heartbeats and enrollment.

Workers connect *outbound* to the coordinator over an authenticated WebSocket, so worker
machines never need an open listening port. The NodeManager keeps the live view of every
connected node (hardware, telemetry, models, running jobs) and turns it into scheduler
snapshots. Connection state is mirrored to the database so it survives restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import AuthError, ConflictError, NotFoundError, ValidationFailed
from homemesh.common.ids import new_id
from homemesh.common.logging import get_logger
from homemesh.common.paths import AppPaths, write_private_file
from homemesh.common.security import (
    generate_pairing_code,
    generate_token,
    hash_token,
    normalize_pairing_code,
)
from homemesh.common.timeutil import iso, now_iso, parse_iso, utcnow
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.events import EventBus
from homemesh.coordinator.middleware import RateLimiter
from homemesh.inference.capabilities import parameter_size_to_billions
from homemesh.protocol import messages as pm
from homemesh.protocol.hardware import HardwareReport, Telemetry
from homemesh.protocol.models import ModelInfo
from homemesh.scheduler.policy import ModelOnNode, NodeSnapshot
from homemesh.version import SUPPORTED_PROTOCOL_VERSIONS, __version__

log = get_logger(__name__)

HELLO_TIMEOUT_S = 15.0
PAIRING_TTL_S = 600



def vram_view(hw: HardwareReport, tel: Telemetry | None) -> tuple[int | None, int | None, bool]:
    """(total VRAM, free VRAM, has GPU) over the discrete GPUs that count for scheduling.

    Free VRAM is only reported when *every* counted GPU reports it. On Windows only NVIDIA cards
    give live usage, so an AMD card next to an NVIDIA one would otherwise be silently left out and
    the node would look smaller than it is; "unknown" makes the scheduler check RAM and say so.
    """
    counted = [g for g in hw.gpus if not g.integrated and g.vram_total_mb and not g.ignored]
    total = sum(g.vram_total_mb or 0 for g in counted) or None
    if not counted:
        return total, None, False
    idx = {g.index for g in counted}
    if tel and tel.gpus:
        frees = {g.index: g.vram_free_mb for g in tel.gpus if g.index in idx and g.vram_free_mb is not None}
    else:
        frees = {g.index: g.vram_free_mb for g in counted if g.vram_free_mb is not None}
    free = sum(v or 0 for v in frees.values()) if len(frees) == len(idx) else None
    return total, free, True


@dataclass
class NodeConnection:
    node_id: str
    name: str
    ws: WebSocket
    protocol_version: int
    hello: pm.Hello
    connected_at: float = field(default_factory=time.monotonic)
    last_heartbeat: float = field(default_factory=time.monotonic)
    telemetry: Telemetry | None = None
    models: list[ModelInfo] = field(default_factory=list)
    backends: list[dict[str, Any]] = field(default_factory=list)
    # job_id -> (model, MB reserved because the model was not loaded when the job was placed)
    assigned_jobs: dict[str, tuple[str | None, int]] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    async def send(self, msg: Any) -> None:
        async with self.send_lock:
            await self.ws.send_text(pm.dump(msg))


class NodeManager:
    def __init__(self, cfg: HomeMeshConfig, paths: AppPaths, repos: Repositories, bus: EventBus) -> None:
        self.cfg = cfg
        self.paths = paths
        self.repos = repos
        self.bus = bus
        self.connections: dict[str, NodeConnection] = {}
        self.limiter = RateLimiter()
        self._sweeper: asyncio.Task[None] | None = None
        # Callbacks wired by the coordinator state (JobService etc.).
        self.on_job_message: Callable[[str, Any], Awaitable[None]] | None = None
        self.on_node_connected: Callable[[str], Awaitable[None]] | None = None
        self.on_node_disconnected: Callable[[str, str], Awaitable[None]] | None = None
        self.trusted_roots_provider: Callable[[str], Awaitable[list[str]]] | None = None
        self._pending_rotations: dict[str, tuple[str, str]] = {}  # rotation_id -> (node_id, secret_hash)
        self.stopping = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.repos.nodes.mark_all_disconnected("coordinator restarted")
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="node-heartbeat-sweeper")

    async def stop(self) -> None:
        self.stopping = True
        if self._sweeper:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        for conn in list(self.connections.values()):
            with contextlib.suppress(Exception):
                await conn.send(pm.CoordinatorShutdown(reason="coordinator shutting down"))
                await conn.ws.close(code=pm.CLOSE_COORDINATOR_SHUTDOWN, reason="coordinator shutting down")
        self.connections.clear()

    async def _sweep_loop(self) -> None:
        timeout = self.cfg.server.heartbeat_timeout_s
        interval = max(1.0, timeout / 5)
        last_tick = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            gap, last_tick = now - last_tick, now
            if gap > 3 * interval + 5:
                # This process was not running (the computer slept or hibernated; on Windows the
                # monotonic clock keeps counting through sleep). Missing heartbeats say nothing about
                # the workers then: give every connection a fresh timeout instead of dropping them all,
                # including the built-in worker, and failing their jobs.
                log.info("No heartbeat check for %.0fs (computer asleep?); giving connected computers %.0fs "
                         "to check in", gap, timeout)
                for conn in self.connections.values():
                    conn.last_heartbeat = max(conn.last_heartbeat, now)
                continue
            for conn in list(self.connections.values()):
                if now - conn.last_heartbeat > timeout:
                    log.warning("Node %s missed heartbeats for %.0fs; marking offline", conn.name,
                                now - conn.last_heartbeat)
                    await self._drop(conn, f"heartbeat timeout ({timeout:.0f}s)")
                    with contextlib.suppress(Exception):
                        await conn.ws.close(code=1001, reason="heartbeat timeout")

    # ------------------------------------------------------------------ enrollment
    async def create_pairing_code(self, label: str, created_by: str | None, ttl_s: int = PAIRING_TTL_S
                                  ) -> dict[str, Any]:
        code = generate_pairing_code()
        expires = iso(utcnow() + timedelta(seconds=ttl_s))
        row = await self.repos.nodes.create_pairing(hash_token(code), expires, label[:100], created_by)
        await self.repos.audit.add("user" if created_by else "cli", created_by, "node.pairing_created",
                                   target_type="pairing", target_id=row["id"], details={"label": label})
        return {"id": row["id"], "code": code, "expires_at": expires, "label": label}

    async def enroll(self, code: str, node_name: str, ip: str | None) -> dict[str, Any]:
        self.limiter.check("enroll", ip or "?", 10)
        norm = normalize_pairing_code(code)
        row = await self.repos.nodes.pairing_by_hash(hash_token(norm))
        reason = None
        if not row:
            reason = "unknown pairing code"
        elif row.get("used_at"):
            reason = "pairing code was already used"
        elif row.get("revoked_at"):
            reason = "pairing code was revoked"
        elif parse_iso(row["expires_at"]) < utcnow():  # type: ignore[operator]
            reason = "pairing code has expired"
        if reason:
            await self.repos.audit.add("node", None, "node.enroll", outcome="failure", ip=ip,
                                       details={"reason": reason, "name": node_name[:100]})
            raise AuthError(f"Enrollment rejected: {reason}.", code="pairing_invalid",
                            hint="Create a new pairing code on the coordinator ('homemesh node pair' or the "
                                 "Nodes page). Codes are single-use and expire after 10 minutes.")
        assert row is not None
        name = (node_name or row.get("label") or "worker").strip()[:100]
        node = await self.repos.nodes.create(name)
        if not await self.repos.nodes.consume_pairing(row["id"], node["id"]):
            await self.repos.nodes.delete(node["id"])
            raise AuthError("Enrollment rejected: pairing code was already used.", code="pairing_invalid")
        secret = generate_token(32)
        await self.repos.nodes.add_credential(node["id"], hash_token(secret))
        await self.repos.audit.add("node", node["id"], "node.enrolled", ip=ip, target_type="node",
                                   target_id=node["id"], details={"name": name})
        self.bus.publish("node.enrolled", {"node_id": node["id"], "name": name})
        return {"node_id": node["id"], "node_name": name, "node_secret": secret,
                "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS)}

    async def ensure_embedded_node(self, name: str, coordinator_url: str) -> dict[str, Any]:
        """Provision credentials for the worker that runs inside the coordinator process.

        Local trust: both share the same data directory, so no pairing code is needed.
        """
        f = self.paths.worker_credentials_file
        if f.exists():
            try:
                creds = json.loads(f.read_text(encoding="utf-8"))
                cred = await self.repos.nodes.credential_by_hash(hash_token(creds.get("node_secret", "")))
                if (cred and cred["node_id"] == creds.get("node_id") and not cred.get("revoked_at")
                        and cred.get("node_status") == "active"):
                    node = await self.repos.nodes.get(cred["node_id"])
                    if node and node.get("embedded"):
                        if creds.get("coordinator_url") != coordinator_url:
                            creds["coordinator_url"] = coordinator_url
                            write_private_file(f, json.dumps(creds, indent=2))
                        return creds
            except (json.JSONDecodeError, OSError):
                log.warning("Worker credentials file %s is unreadable; re-provisioning the local worker", f)
        existing = await self.repos.nodes.embedded_node()
        node = existing or await self.repos.nodes.create(name, embedded=True, mode=self.cfg.worker.mode)
        await self.repos.nodes.revoke_credentials(node["id"])
        secret = generate_token(32)
        await self.repos.nodes.add_credential(node["id"], hash_token(secret))
        creds = {"coordinator_url": coordinator_url, "node_id": node["id"], "node_secret": secret,
                 "embedded": True, "enrolled_at": now_iso()}
        write_private_file(f, json.dumps(creds, indent=2))
        await self.repos.audit.add("system", None, "node.embedded_provisioned", target_type="node",
                                   target_id=node["id"])
        return creds

    # ------------------------------------------------------------------ websocket handling
    async def _authenticate(self, ws: WebSocket) -> tuple[str, str] | None:
        header = ws.headers.get("authorization", "")
        if not header.startswith(pm.AUTH_SCHEME + " ") or ":" not in header:
            return None
        node_id, _, secret = header[len(pm.AUTH_SCHEME) + 1:].strip().partition(":")
        cred = await self.repos.nodes.credential_by_hash(hash_token(secret))
        if not cred or cred["node_id"] != node_id:
            return None
        if cred.get("revoked_at") or cred.get("node_status") != "active":
            return "revoked", node_id
        await self.repos.nodes.touch_credential(cred["id"])
        return "ok", node_id

    async def handle_websocket(self, ws: WebSocket, ip: str | None) -> None:
        await ws.accept()
        auth = await self._authenticate(ws)
        if auth is None or auth[0] != "ok":
            revoked = auth is not None
            await self.repos.audit.add("node", auth[1] if auth else None, "node.connect", outcome="failure", ip=ip,
                                       details={"reason": "revoked" if revoked else "bad credentials"})
            with contextlib.suppress(Exception):
                await ws.send_text(pm.dump(pm.ErrorMessage(
                    code="revoked" if revoked else "unauthorized", fatal=True,
                    message="This node was removed from HomeMesh; re-pair it with 'homemesh worker join'."
                    if revoked else "Invalid node credentials.")))
                await ws.close(code=pm.CLOSE_REVOKED if revoked else pm.CLOSE_UNAUTHORIZED,
                               reason="revoked" if revoked else "unauthorized")
            return
        node_id = auth[1]
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=HELLO_TIMEOUT_S)
            hello = pm.parse_worker_message(raw)
            if not isinstance(hello, pm.Hello) or hello.node_id != node_id:
                raise ValueError("the first message must be a hello for the authenticated node")
        except (TimeoutError, ValidationError, ValueError, WebSocketDisconnect) as exc:
            log.warning("Node %s did not send a valid hello: %s", node_id, exc)
            with contextlib.suppress(Exception):
                await ws.close(code=1002, reason="expected hello")
            return
        version = pm.negotiate_version(hello.protocol_versions, SUPPORTED_PROTOCOL_VERSIONS)
        if version is None:
            msg = (f"Protocol mismatch: worker supports {hello.protocol_versions}, coordinator supports "
                   f"{list(SUPPORTED_PROTOCOL_VERSIONS)}. Upgrade the older side so both run the same HomeMesh version.")
            with contextlib.suppress(Exception):
                await ws.send_text(pm.dump(pm.ErrorMessage(code="protocol_incompatible", message=msg, fatal=True)))
                await ws.close(code=pm.CLOSE_PROTOCOL_INCOMPATIBLE, reason="protocol incompatible")
            await self.repos.audit.add("node", node_id, "node.connect", outcome="failure", ip=ip,
                                       details={"reason": "protocol incompatible"})
            return

        node = await self.repos.nodes.get(node_id)
        assert node is not None
        mode = node["mode"]
        first_connect = node.get("last_connected_at") is None
        if first_connect or not hello.allow_remote_mode_change:
            mode = hello.mode
        await self.repos.nodes.on_hello(
            node_id, name=node["name"], os_name=hello.hardware.os.system, arch=hello.hardware.os.arch,
            hostname=hello.hardware.os.hostname, software_version=hello.software_version, protocol_version=version,
            hardware=hello.hardware.model_dump(mode="json"), limits=hello.limits.model_dump(),
            task_types=hello.task_types, allowed_roots=hello.allowed_roots, allowed_tools=hello.allowed_tools,
            backends=[b.model_dump() for b in hello.backends], mode=mode,
        )
        await self.repos.nodes.replace_models(node_id, [m.model_dump(mode="json") for m in hello.models])
        conn = NodeConnection(node_id=node_id, name=node["name"], ws=ws, protocol_version=version, hello=hello,
                              models=hello.models, backends=[b.model_dump() for b in hello.backends])
        old = self.connections.get(node_id)
        if old is not None:
            log.warning("Computer %s connected again; closing its older connection (two HomeMesh processes with "
                        "the same identity, or a reconnect before the old link timed out)", node["name"])
            old.closed = True
            with contextlib.suppress(Exception):
                await old.ws.close(code=pm.CLOSE_SUPERSEDED, reason="superseded by a new connection")
        self.connections[node_id] = conn
        trusted = None
        if hello.embedded and self.trusted_roots_provider:
            trusted = await self.trusted_roots_provider(node_id)
        await conn.send(pm.Welcome(protocol_version=version, node_id=node_id, node_name=node["name"],
                                   coordinator_version=__version__,
                                   heartbeat_interval_s=self.cfg.worker.heartbeat_interval_s, mode=mode,
                                   trusted_roots=trusted))
        await self.repos.audit.add("node", node_id, "node.connect", ip=ip,
                                   details={"version": hello.software_version, "protocol": version})
        log.info("Node %s (%s) connected: %d model(s), %d GPU(s), mode=%s", node["name"], node_id,
                 len(hello.models), len(hello.hardware.gpus), mode)
        self.bus.publish("node.connected", {"node_id": node_id, "name": node["name"]})
        if self.on_node_connected:
            await self.on_node_connected(node_id)
        reason = "connection closed"
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = pm.parse_worker_message(raw)
                except ValidationError as exc:
                    log.warning("Invalid message from node %s: %s", conn.name, exc.errors()[:2])
                    await conn.send(pm.ErrorMessage(code="invalid_message", message="message failed validation"))
                    continue
                await self._handle_message(conn, msg)
        except WebSocketDisconnect as exc:
            reason = f"disconnected (code {exc.code})"
        except Exception as exc:
            log.exception("Error while handling node %s", conn.name)
            reason = f"error: {exc}"
        finally:
            if self.connections.get(node_id) is conn:
                await self._drop(conn, reason)

    async def _drop(self, conn: NodeConnection, reason: str) -> None:
        if self.connections.get(conn.node_id) is not conn:
            return
        del self.connections[conn.node_id]
        conn.closed = True
        if self.stopping:
            return  # coordinator shutdown: state is reconciled on next start
        await self.repos.nodes.mark_disconnected(conn.node_id, reason)
        log.info("Node %s disconnected: %s", conn.name, reason)
        self.bus.publish("node.disconnected", {"node_id": conn.node_id, "name": conn.name, "reason": reason})
        if self.on_node_disconnected:
            await self.on_node_disconnected(conn.node_id, reason)

    async def _handle_message(self, conn: NodeConnection, msg: Any) -> None:
        if isinstance(msg, pm.Heartbeat):
            conn.last_heartbeat = time.monotonic()
            conn.telemetry = msg.telemetry
            await self.repos.nodes.on_heartbeat(conn.node_id, msg.telemetry.model_dump(mode="json"))
            self.bus.publish("node.telemetry", {"node_id": conn.node_id, "telemetry": msg.telemetry.model_dump(),
                                                "running_jobs": msg.running_jobs, "mode": msg.mode})
        elif isinstance(msg, pm.CapabilitiesUpdate):
            conn.last_heartbeat = time.monotonic()
            conn.models = msg.models
            conn.backends = [b.model_dump() for b in msg.backends]
            # A model that is now loaded no longer needs a memory reservation.
            loaded = {m.name for m in msg.models if m.loaded}
            for job_id, (model, reserved_mb) in list(conn.assigned_jobs.items()):
                if reserved_mb and model in loaded:
                    conn.assigned_jobs[job_id] = (model, 0)
            await self.repos.nodes.replace_models(conn.node_id, [m.model_dump(mode="json") for m in msg.models])
            await self.repos.nodes.set_backends(conn.node_id, conn.backends)
            self.bus.publish("node.models", {"node_id": conn.node_id, "count": len(msg.models)})
        elif isinstance(msg, pm.CredentialRotated):
            pending = self._pending_rotations.pop(msg.rotation_id, None)
            if pending and pending[0] == conn.node_id:
                cred = await self.repos.nodes.credential_by_hash(pending[1])
                if cred:
                    await self.repos.nodes.revoke_credentials(conn.node_id, except_id=cred["id"])
                    await self.repos.audit.add("node", conn.node_id, "node.credential_rotated")
        elif isinstance(msg, pm.WorkerShutdown):
            log.info("Node %s is shutting down: %s", conn.name, msg.reason)
        elif isinstance(msg, pm.Pong):
            conn.last_heartbeat = time.monotonic()
        elif isinstance(msg, pm.Hello):
            await conn.send(pm.ErrorMessage(code="unexpected_hello", message="already connected"))
        elif self.on_job_message is not None:
            conn.last_heartbeat = time.monotonic()
            await self.on_job_message(conn.node_id, msg)

    # ------------------------------------------------------------------ commands
    def get(self, node_id: str) -> NodeConnection | None:
        return self.connections.get(node_id)

    async def send(self, node_id: str, msg: Any) -> bool:
        conn = self.connections.get(node_id)
        if conn is None or conn.closed:
            return False
        try:
            await conn.send(msg)
            return True
        except Exception as exc:
            log.warning("Failed to send to node %s: %s", conn.name, exc)
            return False

    async def set_mode(self, node_id: str, mode: str, actor: str | None) -> None:
        if mode not in ("performance", "balanced", "background", "paused"):
            raise ValidationFailed(f"Unknown mode {mode!r}")
        node = await self.repos.nodes.get(node_id)
        if not node or node["status"] != "active":
            raise NotFoundError("Node not found.")
        conn = self.connections.get(node_id)
        if conn and not conn.hello.allow_remote_mode_change:
            raise ConflictError("This worker does not allow its mode to be changed remotely.",
                                code="remote_mode_change_disabled",
                                hint="Change worker.mode in that computer's homemesh.toml instead.")
        await self.repos.nodes.update(node_id, mode=mode)
        await self.send(node_id, pm.NodeConfig(mode=mode))
        await self.repos.audit.add("user", actor, "node.mode_changed", target_type="node", target_id=node_id,
                                   details={"mode": mode})
        self.bus.publish("node.updated", {"node_id": node_id, "mode": mode})

    async def push_trusted_roots(self, node_id: str) -> None:
        conn = self.connections.get(node_id)
        if conn and conn.hello.embedded and self.trusted_roots_provider:
            await self.send(node_id, pm.NodeConfig(trusted_roots=await self.trusted_roots_provider(node_id)))

    async def revoke(self, node_id: str, actor: str | None) -> None:
        node = await self.repos.nodes.get(node_id)
        if not node:
            raise NotFoundError("Node not found.")
        await self.repos.nodes.revoke(node_id)
        await self.repos.audit.add("user", actor, "node.revoked", target_type="node", target_id=node_id)
        conn = self.connections.get(node_id)
        if conn:
            with contextlib.suppress(Exception):
                await conn.send(pm.ErrorMessage(code="revoked", message="This node was removed from HomeMesh.",
                                                fatal=True))
                await conn.ws.close(code=pm.CLOSE_REVOKED, reason="revoked")
            await self._drop(conn, "revoked by owner")
        self.bus.publish("node.revoked", {"node_id": node_id})

    async def rotate_credential(self, node_id: str, actor: str | None) -> None:
        conn = self.connections.get(node_id)
        if not conn:
            raise ConflictError("The node must be online to rotate its credential.", code="node_offline")
        secret = generate_token(32)
        secret_hash = hash_token(secret)
        await self.repos.nodes.add_credential(node_id, secret_hash)
        rotation_id = new_id("rot")
        self._pending_rotations[rotation_id] = (node_id, secret_hash)
        await conn.send(pm.CredentialRotate(rotation_id=rotation_id, new_secret=secret))
        await self.repos.audit.add("user", actor, "node.credential_rotation_requested", target_type="node",
                                   target_id=node_id)

    async def refresh_models(self, node_id: str) -> bool:
        return await self.send(node_id, pm.ModelsRefresh())

    # ------------------------------------------------------------------ scheduler snapshots
    async def snapshots(self) -> list[NodeSnapshot]:
        rows = {n["id"]: n for n in await self.repos.nodes.list()}
        snaps: list[NodeSnapshot] = []
        now = time.monotonic()
        for node_id, row in rows.items():
            conn = self.connections.get(node_id)
            if conn is None:
                snaps.append(NodeSnapshot(node_id=node_id, name=row["name"], online=False, mode=row["mode"],
                                          task_types=frozenset(row.get("task_types") or []),
                                          weight=row.get("weight") or 0))
                continue
            hw: HardwareReport = conn.hello.hardware
            tel = conn.telemetry
            limits = conn.hello.limits
            vram_total, vram_free, has_gpu = vram_view(hw, tel)
            ram_avail = (tel.ram_available_mb if tel and tel.ram_available_mb is not None
                         else hw.memory.available_mb)
            reserved = sum(mb for _, mb in conn.assigned_jobs.values())
            if reserved:
                if vram_free is not None:
                    vram_free -= reserved
                elif ram_avail is not None:
                    ram_avail -= reserved
            models = [ModelOnNode(
                name=m.name, backend=m.backend, backend_type=m.backend_type,
                capabilities=frozenset(m.capabilities), size_bytes=m.size_bytes,
                estimated_memory_bytes=m.estimated_memory_bytes, loaded=m.loaded, context_length=m.context_length,
                remote=m.remote, mock=m.mock, parameter_size_b=parameter_size_to_billions(m.parameter_size),
            ) for m in conn.models]
            snaps.append(NodeSnapshot(
                node_id=node_id, name=row["name"], online=True, mode=row["mode"],
                task_types=frozenset(conn.hello.task_types), models=models,
                heartbeat_age_s=now - conn.last_heartbeat, max_concurrent_jobs=limits.max_concurrent_jobs,
                running_jobs=len(conn.assigned_jobs), ram_available_mb=ram_avail,
                ram_reserve_mb=limits.ram_reserve_mb, vram_free_mb=vram_free, vram_total_mb=vram_total,
                vram_reserve_percent=limits.vram_reserve_percent, has_gpu=has_gpu,
                allow_cpu_inference=limits.allow_cpu_inference,
                external_gpu_busy=bool(tel.external_gpu_busy) if tel else False,
                weight=row.get("weight") or 0, embedded=conn.hello.embedded,
            ))
        return snaps

    def node_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        """Public view of a node row merged with live connection data."""
        conn = self.connections.get(row["id"])
        out = {k: row.get(k) for k in ("id", "name", "status", "embedded", "mode", "weight", "os", "arch", "hostname",
                                       "software_version", "protocol_version", "created_at", "last_connected_at",
                                       "last_seen_at", "last_disconnect_reason", "task_types", "allowed_roots",
                                       "allowed_tools")}
        out["hardware"] = row.get("hardware_json")
        out["limits"] = row.get("limits_json")
        out["backends"] = row.get("backends_json")
        out["online"] = conn is not None
        out["telemetry"] = conn.telemetry.model_dump() if conn and conn.telemetry else row.get("telemetry_json")
        out["running_jobs"] = list(conn.assigned_jobs) if conn else []
        out["heartbeat_age_s"] = round(time.monotonic() - conn.last_heartbeat, 1) if conn else None
        out["allow_remote_mode_change"] = conn.hello.allow_remote_mode_change if conn else None
        out["models"] = [m.model_dump() for m in conn.models] if conn else []
        return out
