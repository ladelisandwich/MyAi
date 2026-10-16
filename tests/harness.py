"""Integration-test harness: a real coordinator (uvicorn on a free port) and real workers that
enroll with pairing codes and connect over WebSockets - only the inference backend is mocked.

:class:`SimulatedWorker` is a scripted computer for protocol-level tests (e.g. the remote updater): it enrolls
and speaks the worker protocol over a real socket, but the test decides every message it sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from homemesh.common.config import BackendConfig, HomeMeshConfig
from homemesh.common.paths import AppPaths
from homemesh.inference.mock import MockBackend
from homemesh.protocol import messages as pm
from homemesh.protocol.hardware import CPUInfo, HardwareReport, MemoryInfo, OSInfo, Telemetry
from homemesh.protocol.models import ModelInfo
from homemesh.runtime import run_roles
from homemesh.version import SUPPORTED_PROTOCOL_VERSIONS, __version__
from homemesh.worker.agent import WorkerAgent
from homemesh.worker.enroll import join

PASSWORD = "correct horse battery staple"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def paths_for(root: Path) -> AppPaths:
    return AppPaths(config_dir=root, data_dir=root / "data")


async def wait_for(predicate: Any, timeout: float = 10.0, interval: float = 0.05, what: str = "condition") -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(interval)


async def read_sse(resp: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event = None
    async for line in resp.aiter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line[5:])))
    return events


@dataclass
class WorkerHandle:
    name: str
    agent: WorkerAgent
    task: asyncio.Task[Any]
    paths: AppPaths
    backend: MockBackend
    node_id: str = ""

    async def stop(self) -> None:
        await self.agent.stop()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self.task, 10)

    async def crash(self) -> None:
        """Simulate a crash / power loss: drop the TCP connection without a close frame."""
        ws = self.agent._ws
        if ws is not None and ws.transport is not None:
            ws.transport.abort()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.task


class SimulatedWorker:
    """A computer that enrolls and speaks the worker protocol over a real socket, but runs no agent: the test
    scripts what it says (``Hello.build`` / ``Hello.update``, heartbeat reports, answers to jobs).

    Its hardware report is an empty placeholder (hostname ``simulated``) and it offers no task types unless
    told to, so the scheduler never sends it real work by accident. ``models`` may list mock models (labelled
    ``mock=True``) to make it a scheduling candidate.
    """

    def __init__(self, harness: Harness, name: str, node_id: str, secret: str, *, build: dict[str, Any] | None,
                 update: dict[str, Any] | None, models: list[ModelInfo], task_types: list[str],
                 os_system: str, heartbeat_s: float) -> None:
        self.harness, self.name, self.node_id, self.secret = harness, name, node_id, secret
        self.build, self.report = build, update
        self.models, self.task_types, self.os_system = models, task_types, os_system
        self.heartbeat_s = heartbeat_s
        self.ws: ClientConnection | None = None
        self.welcome: pm.Welcome | None = None
        self.dispatches: asyncio.Queue[pm.JobDispatch] = asyncio.Queue()
        self.cancels: list[pm.JobCancel] = []
        self.running: list[str] = []  # job ids it says it runs (hello.running_jobs, heartbeats)
        self._seq = 0
        self._tasks: list[asyncio.Task[Any]] = []

    def hello(self, protocol_versions: list[int] | None = None) -> pm.Hello:
        return pm.Hello(
            node_id=self.node_id, node_name=self.name, software_version=__version__,
            protocol_versions=protocol_versions or list(SUPPORTED_PROTOCOL_VERSIONS),
            hardware=HardwareReport(os=OSInfo(system=self.os_system, hostname="simulated"), cpu=CPUInfo(),
                                    memory=MemoryInfo()),
            backends=[], models=self.models, task_types=self.task_types, mode="balanced",
            limits=pm.NodeLimits(max_concurrent_jobs=1), running_jobs=list(self.running), build=self.build,
            update=self.report)

    async def connect(self, *, protocol_versions: list[int] | None = None) -> pm.Welcome | pm.ErrorMessage:
        """Open the socket and send the hello. Returns the welcome (then heartbeats run every
        ``heartbeat_s``) or the coordinator's error message (the socket is closed after it)."""
        url = self.harness.base_url.replace("http", "ws", 1) + "/api/v1/worker/ws"
        self.ws = await ws_connect(url, proxy=None, additional_headers={
            "Authorization": f"{pm.AUTH_SCHEME} {self.node_id}:{self.secret}"})
        await self.ws.send(pm.dump(self.hello(protocol_versions)))
        msg = pm.parse_coordinator_message(await asyncio.wait_for(self.ws.recv(), 10))
        if isinstance(msg, pm.Welcome):
            self.welcome = msg
            self._tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._heartbeats())]
        return msg

    async def _reader(self) -> None:
        assert self.ws is not None
        with contextlib.suppress(ConnectionClosed):
            async for raw in self.ws:
                msg = pm.parse_coordinator_message(raw)
                if isinstance(msg, pm.JobDispatch):
                    await self.dispatches.put(msg)
                elif isinstance(msg, pm.JobCancel):
                    self.cancels.append(msg)

    async def _heartbeats(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            while True:
                await self.heartbeat()
                await asyncio.sleep(self.heartbeat_s)

    async def heartbeat(self) -> None:
        self._seq += 1
        await self.send(pm.Heartbeat(seq=self._seq, telemetry=Telemetry(), running_jobs=list(self.running),
                                     mode="balanced", update=self.report))

    async def set_report(self, report: dict[str, Any] | None) -> None:
        """Change the update report and send it in a heartbeat right away."""
        self.report = report
        await self.heartbeat()

    async def send(self, msg: Any) -> None:
        assert self.ws is not None
        await self.ws.send(pm.dump(msg))

    async def next_dispatch(self, job_type: str | None = None, timeout: float = 10) -> pm.JobDispatch:
        while True:
            msg = await asyncio.wait_for(self.dispatches.get(), timeout)
            if job_type is None or msg.job_type == job_type:
                return msg

    async def accept(self, job_id: str) -> None:
        self.running.append(job_id)
        await self.send(pm.JobAccepted(job_id=job_id))

    async def complete(self, job_id: str, result: dict[str, Any]) -> None:
        if job_id in self.running:
            self.running.remove(job_id)
        await self.send(pm.JobCompleted(job_id=job_id, result=result))

    async def fail(self, job_id: str, code: str, error: str, hint: str | None = None) -> None:
        if job_id in self.running:
            self.running.remove(job_id)
        await self.send(pm.JobFailed(job_id=job_id, code=code, error=error, hint=hint))

    async def disconnect(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None


@dataclass
class Harness:
    root: Path
    port: int = field(default_factory=free_port)
    task: asyncio.Task[Any] | None = None
    stop_event: asyncio.Event | None = None
    client: httpx.AsyncClient | None = None
    workers: list[WorkerHandle] = field(default_factory=list)
    simulated: list[SimulatedWorker] = field(default_factory=list)
    cfg: HomeMeshConfig | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def paths(self) -> AppPaths:
        return paths_for(self.root / "coordinator")

    def make_config(self, roles: list[str], **overrides: Any) -> HomeMeshConfig:
        cfg = HomeMeshConfig()
        cfg.node.name = overrides.pop("name", "coordinator")
        cfg.node.roles = roles  # type: ignore[assignment]
        cfg.server.port = self.port
        cfg.server.heartbeat_timeout_s = overrides.pop("heartbeat_timeout_s", 6.0)
        cfg.server.queue_timeout_s = overrides.pop("queue_timeout_s", 3.0)
        cfg.server.dispatch_ack_timeout_s = 5.0
        cfg.server.login_attempts_per_minute = overrides.pop("login_attempts_per_minute", 10)
        cfg.worker.heartbeat_interval_s = overrides.pop("heartbeat_interval_s", 1.0)
        cfg.worker.reconnect_max_delay_s = 1.0
        cfg.inference.mock_enabled = True
        cfg.inference.backends = [BackendConfig(name="mock", type="mock")]
        cfg.logging.to_file = False
        for k, v in overrides.items():
            section, _, key = k.partition("__")
            setattr(getattr(cfg, section), key, v)
        return cfg

    async def start(self, roles: list[str] | None = None, **overrides: Any) -> None:
        self.cfg = self.make_config(roles or ["coordinator"], **overrides)
        self.stop_event = asyncio.Event()
        self.task = asyncio.create_task(run_roles(self.cfg, self.paths, install_signal_handlers=False,
                                                  stop_event=self.stop_event))

        async def up() -> bool:
            try:
                async with httpx.AsyncClient(trust_env=False) as c:
                    r = await c.get(self.base_url + "/api/v1/system/health", timeout=1)
                    return r.status_code == 200
            except httpx.HTTPError:
                return False

        await wait_for(up, 15, 0.1, "coordinator to start")

    async def owner_client(self) -> httpx.AsyncClient:
        c = httpx.AsyncClient(base_url=self.base_url, trust_env=False, timeout=30, headers={"X-HomeMesh-CSRF": "1"})
        status = (await c.get("/api/v1/auth/status")).json()
        if status["setup_required"]:
            token = self.paths.setup_token_file.read_text().strip()
            r = await c.post("/api/v1/auth/setup", json={"setup_token": token, "username": "owner",
                                                          "password": PASSWORD})
            assert r.status_code == 200, r.text
        else:
            r = await c.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})
            assert r.status_code == 200, r.text
        self.client = c
        return c

    async def pairing_code(self) -> str:
        assert self.client is not None
        r = await self.client.post("/api/v1/nodes/pairing", json={"label": "test"})
        assert r.status_code == 200, r.text
        return r.json()["code"]

    async def add_worker(self, name: str, *, models: dict[str, list[str]] | None = None, token_delay: float = 0.005,
                         wait_online: bool = True, web: dict[str, Any] | None = None,
                         **worker_overrides: Any) -> WorkerHandle:
        code = await self.pairing_code()
        wroot = self.root / f"worker-{name}"
        wpaths = paths_for(wroot)
        wpaths.ensure()
        creds = await asyncio.to_thread(join, self.base_url, code, wpaths, node_name=name)
        cfg = self.make_config(["worker"], name=name)
        cfg.worker.coordinator_url = self.base_url
        for k, v in worker_overrides.items():
            setattr(cfg.worker, k, v)
        for k, v in (web or {}).items():
            setattr(cfg.web, k, v)
        backend = MockBackend("mock", token_delay_s=token_delay, models=models)
        agent = WorkerAgent(cfg, wpaths, backends={"mock": backend})
        task = asyncio.create_task(agent.run(), name=f"worker-{name}")
        handle = WorkerHandle(name, agent, task, wpaths, backend, creds.node_id)
        self.workers.append(handle)
        if wait_online:
            await self.wait_node_online(creds.node_id)
        return handle

    async def add_simulated_worker(self, name: str, *, build: dict[str, Any] | None = None,
                                   update: dict[str, Any] | None = None, models: list[ModelInfo] | None = None,
                                   task_types: list[str] | None = None, os_system: str = "Linux",
                                   heartbeat_s: float = 0.3, connect: bool = True) -> SimulatedWorker:
        """Enroll a :class:`SimulatedWorker` with a pairing code and (by default) connect it."""
        assert self.client is not None
        r = await self.client.post("/api/v1/nodes/enroll", json={"code": await self.pairing_code(), "name": name})
        assert r.status_code == 200, r.text
        creds = r.json()
        sim = SimulatedWorker(self, name, creds["node_id"], creds["node_secret"], build=build, update=update,
                              models=models or [], task_types=task_types or [], os_system=os_system,
                              heartbeat_s=heartbeat_s)
        self.simulated.append(sim)
        if connect:
            welcome = await sim.connect()
            assert isinstance(welcome, pm.Welcome), welcome
        return sim

    async def nodes(self) -> list[dict[str, Any]]:
        assert self.client is not None
        return (await self.client.get("/api/v1/nodes")).json()

    async def wait_node_online(self, node_id: str, online: bool = True, timeout: float = 15) -> None:
        async def check() -> bool:
            return any(n["id"] == node_id and n["online"] == online and (not online or n["models"])
                       for n in await self.nodes())

        await wait_for(check, timeout, 0.1, f"node {node_id} online={online}")

    async def stop(self) -> None:
        for w in self.workers:
            with contextlib.suppress(Exception):
                await w.stop()
        for sim in self.simulated:
            with contextlib.suppress(Exception):
                await sim.disconnect()
        if self.client is not None:
            await self.client.aclose()
        if self.stop_event is not None:
            self.stop_event.set()
        if self.task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.task, 20)
