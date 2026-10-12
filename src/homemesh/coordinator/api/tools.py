"""Tools API: tool catalogue and policies, approval decisions, execution log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require, require_owner

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])


class PolicyBody(BaseModel):
    enabled: bool
    approval: str = Field(default="default", pattern="^(default|always)$")


class DecisionBody(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=500)


@router.get("")
async def list_tools(request: Request, principal: Principal = Depends(require("read"))) -> list[dict[str, Any]]:
    return await state(request).tools.list_tools()


@router.put("/{name}/policy")
async def set_policy(name: str, body: PolicyBody, request: Request,
                     principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    await state(request).tools.set_policy(name, body.enabled, body.approval, principal.user_id)
    return {"ok": True}


@router.get("/approvals")
async def approvals(request: Request, status: str | None = None,
                    principal: Principal = Depends(require("chat"))) -> list[dict[str, Any]]:
    return await state(request).repos.tools.list_approvals(status)


@router.post("/approvals/{approval_id}")
async def decide(approval_id: str, body: DecisionBody, request: Request,
                 principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    return await state(request).tools.decide(approval_id, body.approved, principal.user_id, body.reason)


@router.get("/executions")
async def executions(request: Request, limit: int = 100,
                     principal: Principal = Depends(require("read"))) -> list[dict[str, Any]]:
    return await state(request).repos.tools.list_executions(min(limit, 500))
