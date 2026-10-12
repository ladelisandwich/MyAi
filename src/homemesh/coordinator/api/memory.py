"""Memory management API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from homemesh.common.errors import NotFoundError
from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require

router = APIRouter(prefix="/api/v1/memories", tags=["memory"])


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    kind: str | None = None
    project_id: str | None = None
    retention_days: int | None = Field(default=None, ge=1, le=36500)


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    kind: str | None = None
    status: str | None = None
    project_id: str | None = None
    pinned: bool | None = None
    expires_at: str | None = None


@router.get("")
async def list_memories(request: Request, status: str | None = None, project_id: str | None = None,
                        kind: str | None = None, q: str | None = None, limit: int = 300,
                        principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    if q:
        items = await st.memory.search(principal.user_id, q, project_id=project_id, limit=min(limit, 50))
    else:
        statuses = status.split(",") if status else ["active", "candidate"]
        items = await st.repos.memories.list(principal.user_id, status=statuses, project_id=project_id, kind=kind,
                                             limit=min(limit, 1000), include_global=project_id is None)
    return {"items": items, "counts": await st.repos.memories.counts(principal.user_id)}


@router.post("")
async def create_memory(body: MemoryCreate, request: Request,
                        principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    mem, outcome = await state(request).memory.create(principal.user_id, body.content, kind=body.kind,
                                                      source="user_explicit", project_id=body.project_id,
                                                      retention_days=body.retention_days)
    return {"memory": mem, "outcome": outcome}


@router.get("/{memory_id}")
async def get_memory(memory_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> dict:
    mem = await state(request).repos.memories.get(memory_id, principal.user_id)
    if not mem:
        raise NotFoundError("Memory not found.")
    return mem


@router.patch("/{memory_id}")
async def update_memory(memory_id: str, body: MemoryUpdate, request: Request,
                        principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    return await state(request).memory.update(principal.user_id, memory_id, **body.model_dump())


@router.post("/{memory_id}/approve")
async def approve(memory_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> dict:
    return await state(request).memory.update(principal.user_id, memory_id, status="active")


@router.post("/{memory_id}/reject")
async def reject(memory_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> dict:
    return await state(request).memory.update(principal.user_id, memory_id, status="rejected")


@router.delete("/{memory_id}")
async def delete_memory(memory_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> dict:
    await state(request).memory.delete(principal.user_id, memory_id)
    return {"ok": True}
