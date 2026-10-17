"""Coordinator service container: builds and wires all services, owns their lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homemesh.common.config import HomeMeshConfig
from homemesh.common.logging import get_logger
from homemesh.common.paths import AppPaths
from homemesh.common.timeutil import iso, utcnow
from homemesh.coordinator.auth import AuthService
from homemesh.coordinator.chat import ChatService
from homemesh.coordinator.db.database import Database
from homemesh.coordinator.db.migrate import migrate
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.embeddings import EmbeddingService
from homemesh.coordinator.events import EventBus
from homemesh.coordinator.jobs import JobService
from homemesh.coordinator.knowledge_service import KnowledgeService
from homemesh.coordinator.memory_service import MemoryService
from homemesh.coordinator.model_registry import ModelRegistry
from homemesh.coordinator.nodes import NodeManager
from homemesh.coordinator.settings_service import SettingsService
from homemesh.coordinator.tool_service import ToolService
from homemesh.coordinator.update_service import UpdateService

log = get_logger(__name__)


@dataclass
class ToolServices:
    """The subset of coordinator services that coordinator-side tools may use."""

    memory: MemoryService
    knowledge: KnowledgeService


class CoordinatorState:
    def __init__(self, cfg: HomeMeshConfig, paths: AppPaths) -> None:
        self.cfg = cfg
        self.paths = paths
        self.db = Database(paths.db_file)
        self.repos = Repositories.create(self.db)
        self.bus = EventBus()
        self.auth = AuthService(self.repos, paths, session_ttl_hours=cfg.server.session_ttl_hours,
                                login_attempts_per_minute=cfg.server.login_attempts_per_minute,
                                secure_cookies=cfg.server.tls_mode != "off")
        self.settings = SettingsService(self.repos, self.bus)
        self.nodes = NodeManager(cfg, paths, self.repos, self.bus)
        self.jobs = JobService(cfg, self.repos, self.nodes, self.bus)
        self.updates = UpdateService(self.repos, self.nodes, self.jobs, self.bus)
        self.models = ModelRegistry(self.repos, self.nodes, self.settings, allow_mock=cfg.inference.mock_enabled,
                                    allow_remote=cfg.cloud.enabled)
        self.embeddings = EmbeddingService(self.repos, self.jobs, self.models, cfg.knowledge.embed_batch_size)
        self.memory = MemoryService(self.repos, self.embeddings, self.bus)
        self.knowledge = KnowledgeService(cfg, self.repos, self.jobs, self.nodes, self.embeddings, self.bus)
        self.nodes.trusted_roots_provider = self.knowledge.trusted_roots
        self.tools = ToolService(self.repos, self.jobs, self.nodes, self.settings, self.bus,
                                 ToolServices(self.memory, self.knowledge))
        self.chat = ChatService(cfg, self.repos, self.jobs, self.models, self.settings, self.memory, self.knowledge,
                                self.tools, self.bus)
        self.setup_token: str | None = None
        self.tls_fingerprint: str | None = None
        self.started_at = utcnow()
        self._maintenance: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self.extra: dict[str, Any] = {}
        self.discovery: Any = None

    async def start(self) -> None:
        self.paths.ensure()
        await self.db.open()
        applied = await self.db.run(lambda conn: migrate(conn, self.paths.backups_dir))
        if applied:
            log.info("Applied database migrations: %s", applied)
        await self.nodes.start()
        await self.jobs.start()
        await self.tools.start()
        self.setup_token = await self.auth.ensure_setup_token()
        await self.auth.ensure_cli_token()
        if self.cfg.tls_enabled and (self.paths.tls_dir / "ca.pem").exists():
            from homemesh.coordinator.tls import ca_fingerprint

            self.tls_fingerprint = ca_fingerprint(self.paths.tls_dir / "ca.pem")
        self._maintenance = asyncio.create_task(self._maintenance_loop(), name="maintenance")
        if self.cfg.server.discovery_enabled:
            from homemesh.coordinator.discovery import DiscoveryResponder
            from homemesh.version import PROTOCOL_VERSION, __version__

            self.discovery = DiscoveryResponder(self.cfg.server.discovery_port, {
                "name": self.cfg.node.name, "url": self.cfg.advertised_url(), "version": __version__,
                "protocol": PROTOCOL_VERSION, "tls_fingerprint": self.tls_fingerprint})
            await self.discovery.start()
        self.ready.set()

    async def stop(self) -> None:
        if self.discovery is not None:
            self.discovery.stop()
        if self._maintenance:
            self._maintenance.cancel()
        await self.chat.shutdown()
        await self.jobs.stop()
        await self.nodes.stop()
        await self.db.close()

    async def _maintenance_loop(self) -> None:
        """Retention and housekeeping, hourly."""
        while True:
            try:
                retention = await self.settings.section("retention")
                await self.repos.sessions.purge_expired()
                await self.repos.jobs.purge_finished_before(iso(utcnow() - timedelta(days=retention["jobs_days"])))
                await self.repos.audit.purge_before(iso(utcnow() - timedelta(days=retention["audit_days"])))
                await self.repos.memories.purge_expired()
                await self.repos.tools.expire_stale()
            except Exception:
                log.exception("Maintenance failed")
            await asyncio.sleep(3600)
