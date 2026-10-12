"""Knowledge collections API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require, require_owner

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    node_id: str
    root_path: str = Field(min_length=1, max_length=1000)
    include_globs: list[str] = Field(default_factory=list, max_length=50)
    exclude_dirs: list[str] | None = Field(default=None, max_length=100)
    project_id: str | None = None
    scan_now: bool = True


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    collection_ids: list[str] | None = None
    limit: int = Field(default=8, ge=1, le=30)


@router.get("/collections")
async def list_collections(request: Request, principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    cols = await st.repos.collections.list(principal.user_id)
    nodes = {n["id"]: n for n in await st.repos.nodes.list()}
    for c in cols:
        n = nodes.get(c["node_id"])
        c["node_name"] = n["name"] if n else "(removed)"
        c["node_online"] = st.nodes.get(c["node_id"]) is not None
    return {"collections": cols, "embedding_model": await st.embeddings.model(),
            "nodes": [{"id": n["id"], "name": n["name"], "embedded": n.get("embedded"),
                       "allowed_roots": n.get("allowed_roots") or []} for n in nodes.values()]}


@router.post("/collections")
async def create_collection(body: CollectionCreate, request: Request,
                            principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    st = state(request)
    col = await st.knowledge.create_collection(principal.user_id, node_id=body.node_id, name=body.name,
                                               root_path=body.root_path, include_globs=body.include_globs,
                                               exclude_dirs=body.exclude_dirs, project_id=body.project_id)
    job = await st.knowledge.start_scan(principal.user_id, col["id"]) if body.scan_now else None
    return {"collection": col, "scan": job}


@router.get("/collections/{collection_id}")
async def get_collection(collection_id: str, request: Request,
                         principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    col = await st.knowledge.collection_detail(principal.user_id, collection_id)
    col["documents"] = await st.repos.documents.list(collection_id, limit=2000)
    return col


@router.post("/collections/{collection_id}/scan")
async def scan(collection_id: str, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    return await state(request).knowledge.start_scan(principal.user_id, collection_id)


@router.post("/collections/{collection_id}/embed")
async def embed_missing(collection_id: str, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    st = state(request)
    await st.knowledge.collection_detail(principal.user_id, collection_id)
    jobs = await st.knowledge.schedule_embeddings(collection_id, principal.user_id)
    return {"embedding_jobs": jobs, "embedding_model": await st.embeddings.model()}


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: str, request: Request,
                            principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    await state(request).knowledge.delete_collection(principal.user_id, collection_id)
    return {"ok": True, "note": "The index was removed. Your files were not touched."}


@router.post("/search")
async def search(body: SearchBody, request: Request, principal: Principal = Depends(require("chat"))) -> dict:
    import time

    t0 = time.perf_counter()
    hits = await state(request).knowledge.search(principal.user_id, body.query, collection_ids=body.collection_ids,
                                                 limit=body.limit)
    return {"results": hits, "took_ms": round((time.perf_counter() - t0) * 1000, 1),
            "mode": hits[0]["retrieval_mode"] if hits else None}
