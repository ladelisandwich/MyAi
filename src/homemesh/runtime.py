"""Run one or more HomeMesh roles in a single process.

* ``coordinator`` - the API server, database and web UI (uvicorn).
* ``worker`` - the job executor. When it runs alongside the coordinator it is the
  *embedded* worker: it is provisioned with local credentials automatically and connects to
  the coordinator over loopback using exactly the same protocol as a remote worker.
* ``client`` - nothing to run; the web UI is served by the coordinator.

Shutdown is graceful: SIGINT/SIGTERM stop the server, the worker reports shutdown and running
jobs are cancelled cooperatively.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys

import uvicorn

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import ConfigError
from homemesh.common.logging import get_logger, setup_logging
from homemesh.common.paths import AppPaths, write_private_file

log = get_logger(__name__)


def _ssl_kwargs(cfg: HomeMeshConfig, paths: AppPaths) -> dict[str, str]:
    if cfg.server.tls_mode == "self_signed":
        from homemesh.coordinator.tls import ensure_tls

        mat = ensure_tls(paths.tls_dir, cfg.server.tls_extra_sans)
        log.info("TLS enabled (local CA fingerprint %s)", mat.ca_fingerprint)
        return {"ssl_certfile": str(mat.cert), "ssl_keyfile": str(mat.key)}
    if cfg.server.tls_mode == "custom":
        return {"ssl_certfile": cfg.server.tls_cert_file, "ssl_keyfile": cfg.server.tls_key_file}
    return {}


async def run_roles(cfg: HomeMeshConfig, paths: AppPaths, roles: list[str] | None = None,
                    *, install_signal_handlers: bool = True, stop_event: asyncio.Event | None = None) -> int:
    roles = roles or list(cfg.node.roles)
    paths.ensure()
    problems = cfg.security_problems() if "coordinator" in roles else []
    if problems:
        for p in problems:
            log.error("Refusing to start: %s", p)
        return 2
    server: uvicorn.Server | None = None
    agent = None
    tasks: list[asyncio.Task[object]] = []
    stop = stop_event or asyncio.Event()

    if "coordinator" in roles:
        from homemesh.coordinator.app import create_app
        from homemesh.coordinator.state import CoordinatorState

        state = CoordinatorState(cfg, paths)
        app = create_app(cfg, paths, state=state)
        uv_cfg = uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port, log_config=None,
                                access_log=False, proxy_headers=bool(cfg.server.trusted_proxies),
                                forwarded_allow_ips=",".join(cfg.server.trusted_proxies) or None,
                                ws_max_size=16 * 1024 * 1024, timeout_graceful_shutdown=10,
                                **_ssl_kwargs(cfg, paths))
        server = uvicorn.Server(uv_cfg)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]  # we handle signals
        tasks.append(asyncio.create_task(server.serve(), name="uvicorn"))
        write_private_file(paths.pid_file, str(os.getpid()))

        if "worker" in roles:
            async def start_embedded_worker() -> None:
                from homemesh.worker.agent import WorkerAgent
                from homemesh.worker.credentials import WorkerCredentials

                nonlocal agent
                await state.ready.wait()
                # uvicorn exposes no "started" event; poll its flag briefly (bounded to 60s).
                for _ in range(1200):
                    if server.started:  # type: ignore[union-attr]
                        break
                    await asyncio.sleep(0.05)
                data = await state.nodes.ensure_embedded_node(cfg.node.name, cfg.local_base_url())
                creds = WorkerCredentials(**{k: data[k] for k in ("coordinator_url", "node_id", "node_secret")},
                                          embedded=True)
                agent = WorkerAgent(cfg, paths, embedded=True, credentials=creds)
                state.extra["embedded_worker"] = agent
                await agent.run()

            tasks.append(asyncio.create_task(start_embedded_worker(), name="embedded-worker"))
    elif "worker" in roles:
        from homemesh.worker.agent import WorkerAgent

        agent = WorkerAgent(cfg, paths)
        try:
            _ = agent.credentials
        except ConfigError as exc:
            log.error("%s %s", exc.message, exc.hint or "")
            return 2
        write_private_file(paths.pid_file, str(os.getpid()))
        tasks.append(asyncio.create_task(agent.run(), name="worker"))
    else:
        log.error("Nothing to run: this computer is configured for roles %s. The 'client' role only needs a "
                  "browser pointed at the coordinator.", roles)
        return 2

    loop = asyncio.get_running_loop()
    if install_signal_handlers:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop.set))

    stopper = asyncio.create_task(stop.wait(), name="stop-wait")
    done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
    exit_code = 0
    for t in done:
        if t is not stopper and t.exception():
            log.error("Component %s crashed: %s", t.get_name(), t.exception())
            exit_code = 1
    log.info("Shutting down HomeMesh...")
    if agent is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(agent.stop(), timeout=10)
    if server is not None:
        server.should_exit = True
    for t in tasks:
        if not t.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=15)
    stopper.cancel()
    with contextlib.suppress(OSError):
        paths.pid_file.unlink()
    return exit_code


def run_foreground(cfg: HomeMeshConfig, paths: AppPaths, roles: list[str] | None = None) -> int:
    setup_logging(cfg.logging.level, cfg.logging.format, paths.logs_dir, cfg.logging.to_file)
    try:
        return asyncio.run(run_roles(cfg, paths, roles))
    except KeyboardInterrupt:
        return 0
