"""Model registry API: installed models per node, roles, download/delete/load/unload jobs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from homemesh.common.errors import NotFoundError, ValidationFailed
from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require, require_owner
from homemesh.coordinator.jobs import JobSpec
from homemesh.coordinator.model_registry import ROLE_CAPABILITY
from homemesh.coordinator.settings_service import MODEL_ROLES
from homemesh.scheduler.policy import TaskRequirements

router = APIRouter(prefix="/api/v1/models", tags=["models"])


class ManageBody(BaseModel):
    action: str = Field(pattern="^(pull|delete|load|unload)$")
    node_id: str
    backend: str
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")


class RolesBody(BaseModel):
    roles: dict[str, str | None]


@router.get("")
async def list_models(request: Request, principal: Principal = Depends(require("read"))) -> dict[str, Any]:
    st = state(request)
    nodes = [st.nodes.node_summary(n) for n in await st.repos.nodes.list()]
    backends = [{"node_id": n["id"], "node_name": n["name"], "online": n["online"], **b}
                for n in nodes for b in (n.get("backends") or [])]
    return {"models": await st.models.list(), "roles": await st.settings.section("model_roles"),
            "backends": backends, "role_capabilities": ROLE_CAPABILITY,
            "downloads_note": "Downloading a model contacts the model registry of the chosen backend over the "
                              "internet. Everything else runs locally."}


@router.put("/roles")
async def set_roles(body: RolesBody, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    st = state(request)
    unknown = [r for r in body.roles if r not in MODEL_ROLES]
    if unknown:
        raise ValidationFailed(f"Unknown model roles: {unknown}")
    warnings = []
    for role, model in body.roles.items():
        if not model:
            continue
        info = await st.models.model_info(model)
        if info is None:
            warnings.append(f"{model} is not installed on any node yet.")
            continue
        cap = ROLE_CAPABILITY.get(role)
        if cap and cap not in info["capabilities"]:
            raise ValidationFailed(f"{model} cannot be the {role} model: it does not support '{cap}' "
                                   f"(it reports {info['capabilities']}).", code="model_incompatible")
    roles = await st.settings.update("model_roles", body.roles, principal.user_id)
    return {"roles": roles, "warnings": warnings}


@router.post("/manage")
async def manage(body: ManageBody, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    st = state(request)
    conn = st.nodes.get(body.node_id)
    if conn is None:
        raise NotFoundError("That node is offline or unknown.")
    if not any(b["name"] == body.backend for b in conn.backends):
        raise NotFoundError(f"Backend {body.backend!r} is not configured on {conn.name}.")
    handle = await st.jobs.submit(JobSpec(
        "model.manage", {"action": body.action, "backend": body.backend, "model": body.model,
                         "keep_alive": st.cfg.inference.keep_alive},
        TaskRequirements("model.manage", "normal", needs_model=False, pinned_node_id=body.node_id),
        priority="normal", user_id=principal.user_id))
    await st.repos.audit.add("user", principal.user_id, f"model.{body.action}", target_type="model",
                             target_id=body.model, details={"node_id": body.node_id, "backend": body.backend})
    return {"job_id": handle.job_id}
