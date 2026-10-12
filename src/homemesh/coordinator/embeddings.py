"""Embedding orchestration: query embeddings (interactive) and background batch embedding.

Embedding work runs on whichever worker hosts the embedding model - often a different
computer from the one that scanned the documents or runs the chat model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homemesh.common.errors import HomeMeshError
from homemesh.common.logging import get_logger
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.jobs import JobService, JobSpec, JobTypeHandlers
from homemesh.coordinator.model_registry import ModelRegistry
from homemesh.protocol.models import CAP_EMBEDDING
from homemesh.scheduler.policy import TaskRequirements

log = get_logger(__name__)


class EmbeddingService:
    def __init__(self, repos: Repositories, jobs: JobService, models: ModelRegistry, batch_size: int = 32) -> None:
        self.repos = repos
        self.jobs = jobs
        self.models = models
        self.batch_size = batch_size
        self.version = 0  # bumped whenever stored vectors change (vector index cache key)
        self.listeners: list[Callable[[str, list[str]], Any]] = []
        jobs.register_handlers("embed", JobTypeHandlers(on_completed=self._on_completed))

    async def model(self) -> str | None:
        return await self.models.role_model("embedding")

    def _requirements(self, model: str, priority: str) -> TaskRequirements:
        return TaskRequirements("embed", priority, model=model, required_capabilities=frozenset({CAP_EMBEDDING}))

    async def embed_query(self, text: str) -> tuple[str, list[float]] | None:
        """Embed a search query now. Returns None (keyword search only) if no embedding model works."""
        model = await self.model()
        if not model:
            return None
        try:
            handle = await self.jobs.run_to_completion(
                JobSpec("embed", {"model": model, "backend": "", "inputs": [text[:4000]]},
                        self._requirements(model, "interactive"), priority="interactive"), timeout_s=60)
        except HomeMeshError as exc:
            log.info("Query embedding unavailable (%s); falling back to keyword search", exc.message)
            return None
        if handle.status != "completed" or not handle.result:
            log.info("Query embedding failed: %s", handle.error)
            return None
        embs = handle.result.get("embeddings") or []
        return (model, embs[0]) if embs else None

    async def enqueue(self, owner_type: str, items: list[tuple[str, str]], *, collection_id: str | None = None,
                      user_id: str | None = None) -> int:
        """Queue background embedding jobs for (owner_id, text) pairs. Returns number of jobs."""
        model = await self.model()
        if not model or not items:
            return 0
        n = 0
        for i in range(0, len(items), self.batch_size):
            batch = items[i:i + self.batch_size]
            await self.jobs.submit(JobSpec(
                "embed", {"model": model, "backend": "", "inputs": [t[:8000] for _, t in batch],
                          "refs": [f"{owner_type}:{oid}" for oid, _ in batch]},
                self._requirements(model, "background"), priority="background", user_id=user_id,
                collection_id=collection_id))
            n += 1
        return n

    async def _on_completed(self, row: dict[str, Any], result: Any) -> None:
        if not result or not result.get("refs"):
            return
        model = result.get("model") or row.get("model")
        grouped: dict[str, list[tuple[str, list[float]]]] = {}
        for ref, vec in zip(result["refs"], result.get("embeddings") or [], strict=False):
            owner_type, _, owner_id = ref.partition(":")
            if owner_type in ("chunk", "memory") and owner_id:
                grouped.setdefault(owner_type, []).append((owner_id, vec))
        for owner_type, items in grouped.items():
            await self.repos.embeddings.store(owner_type, items, model)
            self.version += 1
            for listener in self.listeners:
                res = listener(owner_type, [i for i, _ in items])
                if hasattr(res, "__await__"):
                    await res
