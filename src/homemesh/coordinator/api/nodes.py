"""Node registry API: list, pairing/enrollment, mode, rename, revoke, credential rotation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from homemesh.common.errors import NotFoundError
from homemesh.coordinator.api.common import ip_of, state
from homemesh.coordinator.auth import Principal, require, require_owner

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


class PairBody(BaseModel):
    label: str = Field(default="", max_length=100)
    ttl_s: int = Field(default=600, ge=60, le=3600)


class EnrollBody(BaseModel):
    code: str = Field(min_length=4, max_length=40)
    name: str = Field(default="", max_length=100)


class NodeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    mode: str | None = None
    weight: int | None = Field(default=None, ge=-10, le=10)


@router.get("")
async def list_nodes(request: Request, principal: Principal = Depends(require("read"))) -> list[dict[str, Any]]:
    st = state(request)
    return [st.nodes.node_summary(n) for n in await st.repos.nodes.list()]


@router.get("/{node_id}")
async def get_node(node_id: str, request: Request, principal: Principal = Depends(require("read"))) -> dict:
    st = state(request)
    row = await st.repos.nodes.get(node_id)
    if not row or row["status"] != "active":
        raise NotFoundError("Node not found.")
    out = st.nodes.node_summary(row)
    out["recent_jobs"] = await st.repos.jobs.list(node_id=node_id, limit=20)
    return out


@router.post("/pairing")
async def create_pairing(body: PairBody, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    st = state(request)
    info = await st.nodes.create_pairing_code(body.label, principal.user_id, body.ttl_s)
    info["coordinator_url"] = st.cfg.advertised_url()
    info["tls_fingerprint"] = st.tls_fingerprint
    cmd = f"homemesh worker join --coordinator {info['coordinator_url']} --code {info['code']}"
    if st.tls_fingerprint:
        cmd += f" --fingerprint {st.tls_fingerprint}"
    info["join_command"] = cmd
    return info


@router.get("/pairing/list")
async def list_pairings(request: Request, principal: Principal = Depends(require_owner())) -> list[dict[str, Any]]:
    return await state(request).repos.nodes.list_pairings()


@router.delete("/pairing/{pairing_id}")
async def revoke_pairing(pairing_id: str, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    if not await state(request).repos.nodes.revoke_pairing(pairing_id):
        raise NotFoundError("Pairing code not found or already used.")
    return {"ok": True}


@router.post("/enroll")
async def enroll(body: EnrollBody, request: Request) -> dict[str, Any]:
    """Called by a new worker with a one-time pairing code (no user session)."""
    st = state(request)
    return await st.nodes.enroll(body.code, body.name, ip_of(request))


@router.patch("/{node_id}")
async def update_node(node_id: str, body: NodeUpdate, request: Request,
                      principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    st = state(request)
    row = await st.repos.nodes.get(node_id)
    if not row or row["status"] != "active":
        raise NotFoundError("Node not found.")
    if body.mode is not None:
        await st.nodes.set_mode(node_id, body.mode, principal.user_id)
    fields = {k: v for k, v in (("name", body.name), ("weight", body.weight)) if v is not None}
    if fields:
        await st.repos.nodes.update(node_id, **fields)
        conn = st.nodes.get(node_id)
        if conn and body.name:
            conn.name = body.name
        await st.repos.audit.add("user", principal.user_id, "node.updated", target_type="node", target_id=node_id,
                                 details=fields)
        st.bus.publish("node.updated", {"node_id": node_id, **fields})
    return st.nodes.node_summary(await st.repos.nodes.get(node_id))  # type: ignore[arg-type]


@router.post("/{node_id}/revoke")
async def revoke_node(node_id: str, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    await state(request).nodes.revoke(node_id, principal.user_id)
    return {"ok": True}


@router.post("/{node_id}/rotate-credential")
async def rotate(node_id: str, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    await state(request).nodes.rotate_credential(node_id, principal.user_id)
    return {"ok": True}


@router.post("/{node_id}/refresh")
async def refresh(node_id: str, request: Request, principal: Principal = Depends(require("admin"))) -> dict:
    return {"ok": await state(request).nodes.refresh_models(node_id)}
