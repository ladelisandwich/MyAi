"""Persistent memory: explicit saves, optional candidates, hybrid retrieval and management."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homemesh.common.errors import NotFoundError, ValidationFailed
from homemesh.common.logging import get_logger
from homemesh.common.timeutil import iso, utcnow
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.embeddings import EmbeddingService
from homemesh.coordinator.events import EventBus
from homemesh.knowledge.vectors import VectorIndex, rrf
from homemesh.memory.text import MAX_MEMORY_CHARS, classify_kind, content_hash, is_near_duplicate

log = get_logger(__name__)
KINDS = ("fact", "preference", "project", "note")


class MemoryService:
    def __init__(self, repos: Repositories, embeddings: EmbeddingService, bus: EventBus) -> None:
        self.repos = repos
        self.embeddings = embeddings
        self.bus = bus

    async def create(self, user_id: str, content: str, *, kind: str | None = None, source: str = "user_explicit",
                     status: str = "active", project_id: str | None = None, conversation_id: str | None = None,
                     message_id: str | None = None, retention_days: int | None = None,
                     confidence: float | None = None) -> tuple[dict[str, Any], str]:
        """Create a memory unless an equivalent one exists. Returns (memory, outcome) where outcome is
        'created', 'duplicate' or 'similar'."""
        content = content.strip()
        if not content or len(content) > MAX_MEMORY_CHARS:
            raise ValidationFailed(f"Memory text must be 1-{MAX_MEMORY_CHARS} characters.")
        kind = kind or classify_kind(content)
        if kind not in KINDS:
            raise ValidationFailed(f"Unknown memory kind {kind!r}.")
        if project_id and not await self.repos.projects.get(project_id, user_id):
            raise NotFoundError("Project not found.")
        h = content_hash(content)
        existing = await self.repos.memories.by_hash(user_id, h, project_id)
        if existing:
            return existing, "duplicate"
        for cand in await self.repos.memories.search_fts(user_id, content, project_id=project_id, limit=10):
            if (cand.get("project_id") == project_id) and is_near_duplicate(cand["content"], content):
                return cand, "similar"
        expires = iso(utcnow() + timedelta(days=retention_days)) if retention_days else None
        mem = await self.repos.memories.create(
            user_id, content, h, kind=kind, source=source, status=status, project_id=project_id,
            conversation_id=conversation_id, message_id=message_id, confidence=confidence, expires_at=expires)
        await self.repos.audit.add("user" if source.startswith("user") else "model", user_id, "memory.created",
                                   target_type="memory", target_id=mem["id"],
                                   details={"source": source, "status": status})
        if status == "active":
            await self.embeddings.enqueue("memory", [(mem["id"], content)], user_id=user_id)
        self.bus.publish("memory.changed", {"memory_id": mem["id"], "action": "created"}, user_id=user_id)
        return mem, "created"

    async def update(self, user_id: str, memory_id: str, **fields: Any) -> dict[str, Any]:
        mem = await self.repos.memories.get(memory_id, user_id)
        if not mem:
            raise NotFoundError("Memory not found.")
        patch: dict[str, Any] = {}
        if "content" in fields and fields["content"] is not None:
            content = str(fields["content"]).strip()
            if not content or len(content) > MAX_MEMORY_CHARS:
                raise ValidationFailed(f"Memory text must be 1-{MAX_MEMORY_CHARS} characters.")
            patch["content"] = content
            patch["normalized_hash"] = content_hash(content)
            # A corrected memory is now the user's own statement.
            patch["source"] = "user_edited"
        for k in ("kind", "status", "project_id", "pinned", "expires_at"):
            if k in fields and fields[k] is not None:
                patch[k] = fields[k]
        if "kind" in patch and patch["kind"] not in KINDS:
            raise ValidationFailed("Unknown memory kind.")
        if "status" in patch and patch["status"] not in ("active", "archived", "rejected", "candidate"):
            raise ValidationFailed("Unknown memory status.")
        await self.repos.memories.update(memory_id, user_id, **patch)
        updated = await self.repos.memories.get(memory_id, user_id)
        assert updated is not None
        if "content" in patch or (patch.get("status") == "active" and mem["status"] != "active"):
            await self.embeddings.enqueue("memory", [(memory_id, updated["content"])], user_id=user_id)
        await self.repos.audit.add("user", user_id, "memory.updated", target_type="memory", target_id=memory_id,
                                   details={"fields": sorted(patch)})
        self.bus.publish("memory.changed", {"memory_id": memory_id, "action": "updated"}, user_id=user_id)
        return updated

    async def delete(self, user_id: str, memory_id: str) -> None:
        if not await self.repos.memories.delete(memory_id, user_id):
            raise NotFoundError("Memory not found.")
        await self.repos.audit.add("user", user_id, "memory.deleted", target_type="memory", target_id=memory_id)
        self.bus.publish("memory.changed", {"memory_id": memory_id, "action": "deleted"}, user_id=user_id)

    async def search(self, user_id: str, query: str, *, project_id: str | None = None, limit: int = 6,
                     use_vectors: bool = True) -> list[dict[str, Any]]:
        """Hybrid (keyword + semantic) search over active memories."""
        fts = await self.repos.memories.search_fts(user_id, query, project_id=project_id, limit=limit * 3)
        rankings = [[m["id"] for m in fts]]
        by_id = {m["id"]: m for m in fts}
        if use_vectors:
            emb = await self.embeddings.embed_query(query)
            if emb:
                model, vec = emb
                ids, mat = await self.repos.embeddings.load_matrix("memory", model, user_id=user_id)
                if ids:
                    try:
                        hits = VectorIndex(ids, mat).search(vec, k=limit * 3)
                    except ValueError as exc:
                        log.warning("Memory vector search skipped: %s", exc)
                        hits = []
                    # keep semantic hits that are reasonably similar
                    rankings.append([i for i, s in hits if s >= 0.35])
        fused = rrf(rankings)
        out = []
        for mid, score in fused[: limit * 2]:
            m = by_id.get(mid) or await self.repos.memories.get(mid, user_id)
            if not m or m["status"] != "active":
                continue
            if project_id and m.get("project_id") not in (None, project_id):
                continue
            if not project_id and m.get("project_id"):
                continue
            m = dict(m)
            m["score"] = round(score, 4)
            out.append(m)
            if len(out) >= limit:
                break
        return out

    async def for_context(self, user_id: str, query: str, project_id: str | None, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        pinned = [m for m in await self.repos.memories.list(user_id, status=["active"], project_id=project_id,
                                                            limit=50) if m.get("pinned")]
        found = await self.search(user_id, query, project_id=project_id, limit=limit)
        seen: set[str] = set()
        out = []
        for m in pinned + found:
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
        out = out[: max(limit, len(pinned))]
        await self.repos.memories.mark_used([m["id"] for m in out])
        return out
