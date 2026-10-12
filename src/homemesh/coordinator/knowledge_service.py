"""Document collections: authorisation, indexing jobs, embeddings and hybrid retrieval.

Flow: the owner adds a folder on a specific computer -> a ``knowledge.scan`` job runs on
that computer's worker (which enforces its own folder allow-list) -> extracted chunks are
stored here -> ``embed`` jobs run on whichever worker hosts the embedding model ->
retrieval fuses SQLite FTS5 keyword ranking with cosine similarity (reciprocal-rank fusion)
and returns real source references (file, lines/page).
"""

from __future__ import annotations

import time
from pathlib import Path, PurePath
from typing import Any

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import ConflictError, NotFoundError, PathNotAllowed, ValidationFailed
from homemesh.common.logging import get_logger
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.embeddings import EmbeddingService
from homemesh.coordinator.events import EventBus
from homemesh.coordinator.jobs import JobService, JobSpec, JobTypeHandlers
from homemesh.coordinator.nodes import NodeManager
from homemesh.knowledge.pathsafety import validate_new_root
from homemesh.knowledge.vectors import VectorIndex, rrf
from homemesh.protocol.jobs import DocumentRecord
from homemesh.scheduler.policy import TaskRequirements

log = get_logger(__name__)


def _norm(p: str) -> str:
    return str(PurePath(p)).rstrip("/\\")


def path_within(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    if p == r:
        return True
    sep = "\\" if "\\" in r and "/" not in r else "/"
    return p.startswith(r + sep)


class KnowledgeService:
    def __init__(self, cfg: HomeMeshConfig, repos: Repositories, jobs: JobService, nodes: NodeManager,
                 embeddings: EmbeddingService, bus: EventBus) -> None:
        self.cfg = cfg
        self.repos = repos
        self.jobs = jobs
        self.nodes = nodes
        self.embeddings = embeddings
        self.bus = bus
        self._index_cache: dict[str, tuple[int, VectorIndex, dict[str, str]]] = {}
        jobs.register_handlers("knowledge.scan", JobTypeHandlers(
            on_output=self._on_scan_output, on_completed=self._on_scan_completed, on_failed=self._on_scan_failed))
        embeddings.listeners.append(self._on_embeddings_stored)

    # ------------------------------------------------------------------ collections
    async def create_collection(self, user_id: str, *, node_id: str, name: str, root_path: str,
                                include_globs: list[str] | None = None, exclude_dirs: list[str] | None = None,
                                project_id: str | None = None) -> dict[str, Any]:
        node = await self.repos.nodes.get(node_id)
        if not node or node["status"] != "active":
            raise NotFoundError("That computer is not registered.")
        if not name.strip():
            raise ValidationFailed("Collection name is required.")
        if node.get("embedded"):
            # Same machine as the coordinator: validate locally (exists, not a disk root/home, not sensitive).
            try:
                root_path = str(validate_new_root(Path(root_path)))
            except PermissionError as exc:
                raise PathNotAllowed(str(exc)) from exc
        else:
            allowed = node.get("allowed_roots") or []
            if not any(path_within(root_path, r) for r in allowed):
                raise PathNotAllowed(
                    f"{root_path} is not inside a folder that {node['name']} allows HomeMesh to read.",
                    hint=f"On {node['name']}, run 'homemesh worker allow-path \"{root_path}\"' (or edit "
                         "worker.allowed_roots) and restart the worker. Folders are authorised on the computer that "
                         "holds them, never from the coordinator.")
        for c in await self.repos.collections.list(user_id):
            if c["node_id"] == node_id and _norm(c["root_path"]) == _norm(root_path):
                raise ConflictError("That folder is already a collection.")
        col = await self.repos.collections.create(
            user_id, node_id, name.strip()[:100], root_path, include_globs=include_globs or [],
            exclude_dirs=exclude_dirs if exclude_dirs is not None else list(self.cfg.knowledge.exclude_dirs),
            project_id=project_id)
        await self.repos.audit.add("user", user_id, "knowledge.collection_created", target_type="collection",
                                   target_id=col["id"], details={"node_id": node_id, "root": root_path})
        await self.nodes.push_trusted_roots(node_id)
        self.bus.publish("knowledge.changed", {"collection_id": col["id"]}, user_id=user_id)
        return col

    async def trusted_roots(self, node_id: str) -> list[str]:
        return await self.repos.collections.roots_for_node(node_id)

    async def delete_collection(self, user_id: str, collection_id: str) -> None:
        col = await self.repos.collections.get(collection_id, user_id)
        if not col:
            raise NotFoundError("Collection not found.")
        await self.repos.collections.delete(collection_id)
        self._index_cache.clear()
        await self.repos.audit.add("user", user_id, "knowledge.collection_deleted", target_type="collection",
                                   target_id=collection_id)
        await self.nodes.push_trusted_roots(col["node_id"])
        self.bus.publish("knowledge.changed", {"collection_id": collection_id}, user_id=user_id)

    async def start_scan(self, user_id: str, collection_id: str) -> dict[str, Any]:
        col = await self.repos.collections.get(collection_id, user_id)
        if not col:
            raise NotFoundError("Collection not found.")
        active = await self.repos.jobs.list(status=["queued", "dispatched", "running"], job_type="knowledge.scan")
        if any(j.get("collection_id") == collection_id for j in active):
            raise ConflictError("This collection is already being indexed.", code="scan_in_progress")
        known = await self.repos.documents.known_state(collection_id)
        k = self.cfg.knowledge
        payload = {"collection_id": collection_id, "root_path": col["root_path"],
                   "include_globs": col["include_globs"], "exclude_dirs": col["exclude_dirs"], "known": known,
                   "chunk_chars": k.chunk_chars, "chunk_overlap": k.chunk_overlap, "max_file_mb": k.max_file_mb}
        handle = await self.jobs.submit(JobSpec(
            "knowledge.scan", payload, TaskRequirements("knowledge.scan", "normal", needs_model=False,
                                                        pinned_node_id=col["node_id"]),
            priority="normal", user_id=user_id, collection_id=collection_id))
        await self.repos.collections.update(collection_id, status="indexing", error=None)
        self.bus.publish("knowledge.changed", {"collection_id": collection_id, "status": "indexing"}, user_id=user_id)
        return {"job_id": handle.job_id}

    # ------------------------------------------------------------------ scan job handlers
    async def _on_scan_output(self, row: dict[str, Any], msg: Any) -> None:
        if msg.kind != "document" or not msg.data:
            return
        collection_id = row.get("collection_id")
        if not collection_id:
            return
        rec = DocumentRecord.model_validate(msg.data)
        if rec.status == "unchanged":
            await self.repos.documents.touch_unchanged(collection_id, rec.rel_path, rec.mtime)
            return
        data = rec.model_dump()
        _, chunk_ids = await self.repos.documents.upsert_document(collection_id, data)
        if chunk_ids:
            self._index_cache.clear()

    async def _on_scan_completed(self, row: dict[str, Any], result: Any) -> None:
        collection_id = row.get("collection_id")
        if not collection_id or not result:
            return
        removed = await self.repos.documents.remove_missing(collection_id, result.get("seen", []))
        model = await self.embeddings.model()
        counts = await self.repos.collections.refresh_counts(collection_id, model)
        n_jobs = await self.schedule_embeddings(collection_id, row.get("user_id"))
        status = "indexed" if not n_jobs else "embedding"
        await self.repos.collections.update(collection_id, status=status, last_indexed_at=now_iso(),
                                            error=None if model else "No embedding model available: keyword search only.")
        log.info("Collection %s scanned: %s, %d removed, %d embedding job(s)", collection_id, result.get("stats"),
                 removed, n_jobs)
        self._index_cache.clear()
        self.bus.publish("knowledge.changed", {"collection_id": collection_id, "status": status, **counts},
                         user_id=row.get("user_id"))

    async def _on_scan_failed(self, row: dict[str, Any], info: Any) -> None:
        collection_id = row.get("collection_id")
        if collection_id:
            await self.repos.collections.update(collection_id, status="error", error=(info or {}).get("error"))
            self.bus.publish("knowledge.changed", {"collection_id": collection_id, "status": "error"},
                             user_id=row.get("user_id"))

    async def schedule_embeddings(self, collection_id: str | None, user_id: str | None) -> int:
        model = await self.embeddings.model()
        if not model:
            return 0
        missing = await self.repos.chunks.missing_embeddings(model, collection_id, limit=20000)
        return await self.embeddings.enqueue("chunk", [(r["id"], r["content"]) for r in missing],
                                             collection_id=collection_id, user_id=user_id)

    async def _on_embeddings_stored(self, owner_type: str, ids: list[str]) -> None:
        if owner_type != "chunk":
            return
        self._index_cache.clear()
        model = await self.embeddings.model()
        rows = await self.repos.chunks.get_many(ids[:1])
        if rows:
            cid = rows[0]["collection_id"]
            counts = await self.repos.collections.refresh_counts(cid, model)
            col = await self.repos.collections.get(cid)
            if col and counts["embedded_count"] >= counts["chunk_count"] and col["status"] == "embedding":
                await self.repos.collections.update(cid, status="indexed")
            self.bus.publish("knowledge.changed", {"collection_id": cid, **counts})

    # ------------------------------------------------------------------ retrieval
    async def _vector_index(self, model: str) -> tuple[VectorIndex, dict[str, str]]:
        cached = self._index_cache.get(model)
        if cached and cached[0] == self.embeddings.version:
            return cached[1], cached[2]
        ids, mat = await self.repos.embeddings.load_matrix("chunk", model)
        idx = VectorIndex(ids, mat)
        coll = {}
        if ids:
            for r in await self.repos.db.fetchall(
                    "SELECT id, collection_id FROM chunks WHERE id IN (SELECT owner_id FROM embeddings"
                    " WHERE owner_type='chunk' AND model=?)", (model,)):
                coll[r["id"]] = r["collection_id"]
        self._index_cache[model] = (self.embeddings.version, idx, coll)
        return idx, coll

    async def search(self, user_id: str, query: str, *, collection_ids: list[str] | None = None, limit: int = 5,
                     min_score: float = 0.0) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        cols = await self.repos.collections.list(user_id)
        allowed = [c["id"] for c in cols if collection_ids is None or c["id"] in collection_ids]
        if not allowed:
            return []
        fts = await self.repos.chunks.search_fts(query, allowed, limit=limit * 4)
        rankings = [[r["id"] for r in fts]]
        vec_scores: dict[str, float] = {}
        mode = "keyword"
        emb = await self.embeddings.embed_query(query)
        if emb:
            model, qvec = emb
            idx, coll = await self._vector_index(model)
            allowed_set = set(allowed)
            try:
                hits = idx.search(qvec, k=limit * 4, allowed={i for i, c in coll.items() if c in allowed_set})
            except ValueError as exc:
                log.warning("Vector search skipped: %s", exc)
                hits = []
            hits = [(i, s) for i, s in hits if s >= max(min_score, 0.2)]
            vec_scores = dict(hits)
            rankings.append([i for i, _ in hits])
            mode = "hybrid" if hits else "keyword"
        fused = rrf(rankings)[:limit]
        rows = await self.repos.chunks.get_many([i for i, _ in fused])
        score_by = dict(fused)
        out = []
        for r in rows:
            loc = f"p. {r['page']}" if r.get("page") else (
                f"lines {r['start_line']}-{r['end_line']}" if r.get("start_line") else "")
            out.append({
                "chunk_id": r["id"], "collection_id": r["collection_id"], "collection": r["collection_name"],
                "path": r["rel_path"], "abs_path": str(PurePath(r["root_path"]) / r["rel_path"]),
                "node_id": r["node_id"], "page": r.get("page"), "start_line": r.get("start_line"),
                "end_line": r.get("end_line"), "content": r["content"], "score": round(score_by.get(r["id"], 0), 4),
                "similarity": round(vec_scores[r["id"]], 4) if r["id"] in vec_scores else None,
                "source": f"{r['collection_name']}/{r['rel_path']}" + (f" ({loc})" if loc else ""),
            })
        log.debug("knowledge search (%s) took %.1f ms", mode, (time.perf_counter() - t0) * 1000)
        for o in out:
            o["retrieval_mode"] = mode
        return out

    async def collection_detail(self, user_id: str, collection_id: str) -> dict[str, Any]:
        col = await self.repos.collections.get(collection_id, user_id)
        if not col:
            raise NotFoundError("Collection not found.")
        col["status_counts"] = await self.repos.documents.status_counts(collection_id)
        col["embedding_model"] = await self.embeddings.model()
        return col
