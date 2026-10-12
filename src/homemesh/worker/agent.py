"""The worker daemon: connects to the coordinator and executes jobs.

Connection loop:

1. Open ``wss://<coordinator>/api/v1/worker/ws`` with the node credential (outbound only;
   HTTP proxies are bypassed so LAN traffic never leaves the network).
2. Send ``hello`` (hardware, backends, models, limits); wait for ``welcome``.
3. Send heartbeats with live telemetry; refresh the model list periodically.
4. Execute dispatched jobs concurrently (bounded by the local policy).
5. On disconnect, cancel running jobs (their results can no longer be delivered) and
   reconnect with exponential backoff and jitter. After a coordinator restart the worker
   simply reconnects; the coordinator re-queues idempotent work.

Fatal conditions (credentials revoked/invalid, incompatible protocol) stop the loop with a
clear message instead of retrying forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import ssl
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from homemesh.common.config import HomeMeshConfig
from homemesh.common.logging import get_logger
from homemesh.common.paths import AppPaths
from homemesh.hardware.detect import TelemetrySampler, detect_hardware
from homemesh.inference.base import InferenceBackend
from homemesh.inference.factory import build_backends
from homemesh.protocol import messages as pm
from homemesh.protocol.hardware import HardwareReport, Telemetry
from homemesh.protocol.jobs import (
    ChatJobPayload,
    EmbedJobPayload,
    KnowledgeScanPayload,
    ModelManagePayload,
    ToolExecPayload,
)
from homemesh.protocol.models import BackendStatus, ModelInfo
from homemesh.version import SUPPORTED_PROTOCOL_VERSIONS, __version__
from homemesh.worker.credentials import WorkerCredentials
from homemesh.worker.executor import Executor, JobFailure, failure_from_exception
from homemesh.worker.policy import LocalPolicy

log = get_logger(__name__)

MODEL_REFRESH_INTERVAL_S = 60.0
FATAL_CLOSE_CODES = {pm.CLOSE_UNAUTHORIZED: "credentials rejected", pm.CLOSE_REVOKED: "node revoked",
                     pm.CLOSE_PROTOCOL_INCOMPATIBLE: "protocol incompatible",
                     pm.CLOSE_SUPERSEDED: "another process connected with the same node identity"}


class FatalWorkerError(Exception):
    pass


def ws_url(coordinator_url: str) -> str:
    base = coordinator_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/api/v1/worker/ws"


def make_ssl_context(url: str, ca_file: str | None) -> ssl.SSLContext | None:
    if not url.startswith(("https://", "wss://")):
        return None
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class WorkerAgent:
    def __init__(self, cfg: HomeMeshConfig, paths: AppPaths, *, embedded: bool = False,
                 credentials: WorkerCredentials | None = None,
                 backends: dict[str, InferenceBackend] | None = None,
                 hardware: HardwareReport | None = None) -> None:
        self.cfg = cfg
        self.paths = paths
        self.embedded = embedded
        self._credentials = credentials
        self.backends = backends if backends is not None else build_backends(cfg)
        self.policy = LocalPolicy(cfg.worker, embedded=embedded)
        self.sampler = TelemetrySampler(cfg.worker.gpu_busy_threshold_percent)
        self._hardware = hardware
        self._hardware_at = time.monotonic() if hardware else 0.0
        self.models: list[ModelInfo] = []
        self.backend_status: list[BackendStatus] = []
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self._ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._refresh_requested = asyncio.Event()
        self._last_telemetry: Telemetry | None = None
        self.node_id: str | None = None
        self.state = "starting"
        self.last_error: str | None = None
        from homemesh.tools.runtime import make_worker_tool_runner

        self.executor = Executor(
            self.backends, self.policy, allow_model_downloads=cfg.inference.allow_model_downloads,
            remote_allowed=self._remote_allowed, models_cache=lambda: self.models,
            tool_runner=make_worker_tool_runner(self.policy, paths.workspace_dir),
        )

    # ------------------------------------------------------------------ helpers
    def _remote_allowed(self, backend: str) -> bool:
        b = self.backends.get(backend)
        return bool(getattr(b, "allow_remote_models", False)) and self.cfg.cloud.enabled

    @property
    def credentials(self) -> WorkerCredentials:
        if self._credentials is None:
            self._credentials = WorkerCredentials.load(self.paths.worker_credentials_file)
        return self._credentials

    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout_s: float = 10.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout_s)

    async def hardware(self) -> HardwareReport:
        if self._hardware is None or time.monotonic() - self._hardware_at > 600:
            self._hardware = await asyncio.to_thread(detect_hardware, self.paths.data_dir)
            self._hardware_at = time.monotonic()
        return self._hardware

    async def refresh_models(self) -> bool:
        """Re-read models and backend health. Returns True if anything changed."""
        statuses: list[BackendStatus] = []
        models: list[ModelInfo] = []
        for b in self.backends.values():
            st = await b.health()
            statuses.append(st)
            if st.ok:
                try:
                    models.extend(await asyncio.wait_for(b.list_models(), timeout=30))
                except Exception as exc:
                    st.ok = False
                    st.error = str(getattr(exc, "message", exc))
        old_key = sorted((m.backend, m.name, m.loaded) for m in self.models)
        new_key = sorted((m.backend, m.name, m.loaded) for m in models)
        changed = old_key != new_key or [s.ok for s in statuses] != [s.ok for s in self.backend_status]
        self.models, self.backend_status = models, statuses
        return changed

    async def send(self, msg: Any) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected to the coordinator")
        async with self._send_lock:
            await ws.send(pm.dump(msg))

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        attempt = 0
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                delay = 1.0
                attempt = 0
            except FatalWorkerError as exc:
                self.state = "stopped"
                self.last_error = str(exc)
                log.error("Worker stopped: %s", exc)
                return
            except (OSError, ConnectionClosed, InvalidStatus, TimeoutError, ssl.SSLError) as exc:
                self.last_error = self._describe(exc)
                log.warning("Coordinator connection problem: %s", self.last_error)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Unexpected worker error")
            finally:
                self._connected.clear()
                self._ws = None
                await self._cancel_all_jobs("connection to coordinator lost")
            if self._stop.is_set():
                break
            attempt += 1
            if self.cfg.worker.reconnect_max_attempts and attempt >= self.cfg.worker.reconnect_max_attempts:
                self.state = "stopped"
                log.error("Giving up after %d reconnection attempts", attempt)
                return
            self.state = "reconnecting"
            sleep_for = min(delay, self.cfg.worker.reconnect_max_delay_s) * random.uniform(0.8, 1.2)  # noqa: S311
            log.info("Reconnecting to coordinator in %.1fs (attempt %d)", sleep_for, attempt)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            delay = min(delay * 2, self.cfg.worker.reconnect_max_delay_s)
        self.state = "stopped"

    @staticmethod
    def _describe(exc: BaseException) -> str:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return (f"TLS certificate verification failed ({exc.verify_message}). The coordinator certificate "
                    "does not match the pinned CA; re-join with the current fingerprint if the coordinator was "
                    "reinstalled.")
        if isinstance(exc, InvalidStatus):
            return f"coordinator rejected the connection (HTTP {exc.response.status_code})"
        if isinstance(exc, ConnectionRefusedError):
            return "connection refused (is the coordinator running and reachable?)"
        return f"{type(exc).__name__}: {exc}"

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await self.send(pm.WorkerShutdown(reason="worker stopping"))
                await ws.close()
        await self._cancel_all_jobs("worker stopping")
        for b in self.backends.values():
            with contextlib.suppress(Exception):
                await b.close()

    async def _session(self) -> None:
        creds = self.credentials
        url = ws_url(creds.coordinator_url)
        ca_file = creds.ca_file or self.cfg.worker.tls_ca_file or None
        if self.embedded and creds.coordinator_url.startswith("https://"):
            ca_file = str(self.paths.tls_dir / "ca.pem")
        self.state = "connecting"
        try:
            ws_cm = connect(
                url, additional_headers={"Authorization": f"{pm.AUTH_SCHEME} {creds.node_id}:{creds.node_secret}",
                                         pm.PROTOCOL_HEADER: ",".join(map(str, SUPPORTED_PROTOCOL_VERSIONS))},
                ssl=make_ssl_context(creds.coordinator_url, ca_file), proxy=None, max_size=pm.MAX_FRAME_BYTES,
                open_timeout=15, ping_interval=20, ping_timeout=30, close_timeout=5,
                user_agent_header=f"HomeMesh-Worker/{__version__}",
            )
        except InvalidURI as exc:
            raise FatalWorkerError(f"Invalid coordinator URL {creds.coordinator_url!r}: {exc}") from exc
        async with ws_cm as ws:
            self._ws = ws
            await self.refresh_models()
            hw = await self.hardware()
            limits = pm.NodeLimits(
                max_concurrent_jobs=self.cfg.worker.max_concurrent_jobs,
                allow_cpu_inference=self.cfg.worker.allow_cpu_inference,
                ram_reserve_mb=self.cfg.worker.ram_reserve_mb,
                vram_reserve_percent=self.cfg.worker.vram_reserve_percent,
                gpu_busy_threshold_percent=self.cfg.worker.gpu_busy_threshold_percent)
            await self.send(pm.Hello(
                node_id=creds.node_id, node_name=self.cfg.node.name, software_version=__version__,
                protocol_versions=list(SUPPORTED_PROTOCOL_VERSIONS), hardware=hw, backends=self.backend_status,
                models=self.models, task_types=self.cfg.worker.task_types, mode=self.policy.mode, limits=limits,
                allowed_roots=self.cfg.worker.allowed_roots, allowed_tools=self.cfg.worker.allowed_tools,
                allow_remote_mode_change=self.cfg.worker.allow_remote_mode_change,
                running_jobs=list(self.jobs), embedded=self.embedded))
            welcome = await self._expect_welcome(ws)
            self.node_id = welcome.node_id
            if self.cfg.worker.allow_remote_mode_change:
                self.policy.mode = welcome.mode
            if welcome.trusted_roots is not None and self.embedded:
                self.policy.trusted_roots = welcome.trusted_roots
            self.state = "connected"
            self.last_error = None
            self._connected.set()
            log.info("Connected to coordinator %s as %s (protocol v%d, mode %s)", creds.coordinator_url,
                     welcome.node_name, welcome.protocol_version, self.policy.mode)
            hb = asyncio.create_task(self._heartbeat_loop(welcome.heartbeat_interval_s), name="worker-heartbeat")
            mr = asyncio.create_task(self._model_refresh_loop(), name="worker-model-refresh")
            try:
                async for raw in ws:
                    await self._on_message(raw)
            except ConnectionClosed as exc:
                self._check_fatal(exc.rcvd.code if exc.rcvd else None, exc.rcvd.reason if exc.rcvd else "")
                raise
            finally:
                hb.cancel()
                mr.cancel()
            code = ws.close_code
            self._check_fatal(code, ws.close_reason or "")

    def _check_fatal(self, code: int | None, reason: str) -> None:
        if code in FATAL_CLOSE_CODES:
            hint = ""
            if code in (pm.CLOSE_UNAUTHORIZED, pm.CLOSE_REVOKED):
                hint = " Re-pair this computer with 'homemesh worker join'."
            raise FatalWorkerError(f"Coordinator closed the connection: {FATAL_CLOSE_CODES[code]} ({reason}).{hint}")

    async def _expect_welcome(self, ws: ClientConnection) -> pm.Welcome:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
        except ConnectionClosed as exc:
            self._check_fatal(exc.rcvd.code if exc.rcvd else None, exc.rcvd.reason if exc.rcvd else "")
            raise
        msg = pm.parse_coordinator_message(raw)
        if isinstance(msg, pm.ErrorMessage):
            if msg.fatal:
                raise FatalWorkerError(f"{msg.message} [{msg.code}]")
            raise ConnectionError(msg.message)
        if not isinstance(msg, pm.Welcome):
            raise ConnectionError(f"expected welcome, got {msg.type}")
        return msg

    # ------------------------------------------------------------------ background loops
    async def _heartbeat_loop(self, interval: float) -> None:
        seq = 0
        while True:
            try:
                tel = await asyncio.to_thread(self.sampler.sample, len(self.jobs))
                self._last_telemetry = tel
                seq += 1
                await self.send(pm.Heartbeat(seq=seq, telemetry=tel, running_jobs=list(self.jobs),
                                             mode=self.policy.mode))
            except (ConnectionError, ConnectionClosed):
                return
            except Exception:
                log.exception("Heartbeat failed")
            await asyncio.sleep(interval)

    async def _model_refresh_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._refresh_requested.wait(), timeout=MODEL_REFRESH_INTERVAL_S)
            forced = self._refresh_requested.is_set()
            self._refresh_requested.clear()
            try:
                changed = await self.refresh_models()
                if changed or forced:
                    await self.send(pm.CapabilitiesUpdate(backends=self.backend_status, models=self.models))
            except (ConnectionError, ConnectionClosed):
                return
            except Exception:
                log.exception("Model refresh failed")

    def request_refresh(self) -> None:
        self._refresh_requested.set()

    # ------------------------------------------------------------------ messages
    async def _on_message(self, raw: str | bytes) -> None:
        try:
            msg = pm.parse_coordinator_message(raw)
        except ValidationError as exc:
            log.warning("Ignoring invalid message from coordinator: %s", exc.errors()[:2])
            return
        if isinstance(msg, pm.JobDispatch):
            await self._on_dispatch(msg)
        elif isinstance(msg, pm.JobCancel):
            task = self.jobs.get(msg.job_id)
            if task:
                log.info("Cancelling job %s: %s", msg.job_id, msg.reason)
                task.cancel()
        elif isinstance(msg, pm.NodeConfig):
            if msg.mode and self.cfg.worker.allow_remote_mode_change:
                log.info("Mode changed by coordinator: %s -> %s", self.policy.mode, msg.mode)
                self.policy.mode = msg.mode
            if msg.trusted_roots is not None and self.embedded:
                self.policy.trusted_roots = msg.trusted_roots
        elif isinstance(msg, pm.CredentialRotate):
            creds = self.credentials
            creds.node_secret = msg.new_secret
            creds.save(self.paths.worker_credentials_file)
            await self.send(pm.CredentialRotated(rotation_id=msg.rotation_id))
            log.info("Node credential rotated")
        elif isinstance(msg, pm.ModelsRefresh):
            self.request_refresh()
        elif isinstance(msg, pm.Ping):
            await self.send(pm.Pong(ping_id=msg.id))
        elif isinstance(msg, pm.CoordinatorShutdown):
            log.info("Coordinator is shutting down: %s", msg.reason)
        elif isinstance(msg, pm.ErrorMessage):
            log.warning("Coordinator error [%s]: %s", msg.code, msg.message)
            if msg.fatal:
                raise FatalWorkerError(f"{msg.message} [{msg.code}]")

    async def _on_dispatch(self, msg: pm.JobDispatch) -> None:
        busy = bool(self._last_telemetry and self._last_telemetry.external_gpu_busy)
        verdict = self.policy.check(msg.job_type, msg.priority, msg.payload, len(self.jobs), busy)
        if not verdict.ok:
            log.info("Rejecting job %s (%s): %s", msg.job_id, msg.job_type, verdict.reason)
            await self.send(pm.JobRejected(job_id=msg.job_id, code=verdict.code, reason=verdict.reason))
            return
        if msg.job_id in self.jobs:
            return  # duplicate dispatch; already running
        await self.send(pm.JobAccepted(job_id=msg.job_id))
        self.jobs[msg.job_id] = asyncio.create_task(self._run_job(msg), name=f"job-{msg.job_id}")

    async def _run_job(self, msg: pm.JobDispatch) -> None:
        partial: dict[str, Any] = {}
        job_id = msg.job_id
        loaded_before = {m.name for m in self.models if m.loaded}
        try:
            async with asyncio.timeout(msg.timeout_s):
                result = await self._execute(msg, partial)
            await self.send(pm.JobCompleted(job_id=job_id, result=result, metrics=result.get("metrics")
                                            if isinstance(result.get("metrics"), dict) else None))
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.send(pm.JobCancelled(job_id=job_id, partial=partial or None))
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self.send(pm.JobFailed(job_id=job_id, code="timeout",
                                             error=f"job exceeded {msg.timeout_s:.0f}s", partial=partial or None))
        except Exception as exc:
            f = failure_from_exception(exc)
            log.warning("Job %s (%s) failed: [%s] %s", job_id, msg.job_type, f.code, f.message)
            with contextlib.suppress(Exception):
                await self.send(pm.JobFailed(job_id=job_id, code=f.code, error=f.message, retryable=f.retryable,
                                             partial=partial or None))
        finally:
            self.jobs.pop(job_id, None)
            model = msg.payload.get("model")
            if msg.job_type in ("chat", "embed", "model.manage") and (model not in loaded_before
                                                                      or msg.job_type == "model.manage"):
                self.request_refresh()

    async def _execute(self, msg: pm.JobDispatch, partial: dict[str, Any]) -> dict[str, Any]:
        t = msg.job_type
        if t == "chat":
            return await self.executor.chat(msg.job_id, ChatJobPayload.model_validate(msg.payload), self.send, partial)
        if t == "embed":
            return await self.executor.embed(EmbedJobPayload.model_validate(msg.payload))
        if t == "knowledge.scan":
            return await self.executor.knowledge_scan(msg.job_id, KnowledgeScanPayload.model_validate(msg.payload),
                                                      self.send)
        if t == "tool.exec":
            return await self.executor.tool_exec(ToolExecPayload.model_validate(msg.payload))
        if t == "model.manage":
            return await self.executor.model_manage(msg.job_id, ModelManagePayload.model_validate(msg.payload),
                                                    self.send)
        raise JobFailure("unsupported", f"unknown job type {t}")

    async def _cancel_all_jobs(self, reason: str) -> None:
        tasks = list(self.jobs.values())
        if tasks:
            log.info("Cancelling %d running job(s): %s", len(tasks), reason)
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self.jobs.clear()

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "node_id": self.node_id, "mode": self.policy.mode, "running_jobs": list(self.jobs),
                "last_error": self.last_error, "models": len(self.models),
                "backends": [b.model_dump() for b in self.backend_status]}


def worker_ca_path(paths: AppPaths) -> Path:
    return paths.secrets_dir / "coordinator-ca.pem"
