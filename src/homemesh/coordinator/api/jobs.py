"""Jobs and activity API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from homemesh.common.errors import NotFoundError
from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require
from homemesh.protocol.update import UPDATE_JOB_TYPES

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def redact_update_job(job: dict[str, Any]) -> dict[str, Any]:
    """``update.*`` rows show a summary only: a check result carries commit subjects, branch and folder names
    (the owner sees them on the Computers page), an apply result just the run id."""
    result = job.get("result_json")
    if not isinstance(result, dict):
        return job
    if job["type"] == "update.check":
        job["result_json"] = {"summary": {k: result.get(k) for k in ("upstream_commit", "behind", "can_update")}}
    elif job["type"] == "update.apply":
        job["result_json"] = {"run_id": result.get("run_id")}
    return job


@router.get("")
async def list_jobs(request: Request, status: str | None = None, type: str | None = None,
                    node_id: str | None = None, limit: int = 100, offset: int = 0,
                    principal: Principal = Depends(require("read"))) -> dict[str, Any]:
    st = state(request)
    statuses = status.split(",") if status else None
    items = await st.repos.jobs.list(status=statuses, job_type=type, node_id=node_id, limit=min(limit, 500),
                                     offset=offset)
    names = {n["id"]: n["name"] for n in await st.repos.nodes.list(include_revoked=True)}
    for j in items:
        j["node_name"] = names.get(j["node_id"]) if j.get("node_id") else None
    return {"items": items, "counts": await st.repos.jobs.counts_by_status()}


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request, principal: Principal = Depends(require("read"))) -> dict[str, Any]:
    st = state(request)
    job = await st.repos.jobs.get(job_id)
    if not job:
        raise NotFoundError("Job not found.")
    if job["type"] == "chat" and isinstance(job.get("payload_json"), dict):
        # Do not echo full prompts (they contain memories/documents) unless asked; show sizes instead.
        p = job["payload_json"]
        job["payload_json"] = {"model": p.get("model"), "backend": p.get("backend"),
                               "messages": len(p.get("messages") or []), "tools": len(p.get("tools") or []),
                               "options": p.get("options")}
    elif job["type"] == "embed" and isinstance(job.get("payload_json"), dict):
        p = job["payload_json"]
        job["payload_json"] = {"model": p.get("model"), "inputs": len(p.get("inputs") or [])}
        if isinstance(job.get("result_json"), dict):
            r = job["result_json"]
            job["result_json"] = {"model": r.get("model"), "dim": r.get("dim"),
                                  "vectors": len(r.get("embeddings") or [])}
    elif job["type"] == "knowledge.scan" and isinstance(job.get("payload_json"), dict):
        p = job["payload_json"]
        job["payload_json"] = {k: p.get(k) for k in ("collection_id", "root_path", "include_globs")} | {
            "known_documents": len(p.get("known") or {})}
    elif job["type"] in UPDATE_JOB_TYPES:
        redact_update_job(job)
    job["events"] = await st.repos.jobs.events(job_id)
    node = await st.repos.nodes.get(job["node_id"]) if job.get("node_id") else None
    job["node_name"] = node["name"] if node else None
    return job


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> dict:
    return await state(request).jobs.cancel(job_id, principal.user_id)
