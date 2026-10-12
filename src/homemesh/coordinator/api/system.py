"""System health, dashboard status, metrics, audit log and TLS CA download."""

from __future__ import annotations

import statistics
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from homemesh.common.errors import NotFoundError
from homemesh.common.timeutil import utcnow
from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require, require_owner
from homemesh.coordinator.db.migrate import LATEST_SCHEMA_VERSION, current_version
from homemesh.version import PROTOCOL_VERSION, __version__

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/system/health")
async def health(request: Request) -> dict[str, Any]:
    """Unauthenticated liveness check. Reveals no personal data."""
    st = state(request)
    db_ok = True
    try:
        await st.db.fetchval("SELECT 1")
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "version": __version__, "protocol_version": PROTOCOL_VERSION,
            "database": "ok" if db_ok else "error", "setup_required": await st.auth.setup_required(),
            "tls": st.cfg.server.tls_mode}


@router.get("/system/status")
async def status(request: Request, principal: Principal = Depends(require("read"))) -> dict[str, Any]:
    st = state(request)
    nodes = [st.nodes.node_summary(n) for n in await st.repos.nodes.list()]
    models = await st.models.list()
    counts = await st.repos.jobs.counts_by_status()
    integrity = await st.db.integrity_check()
    schema = await st.db.run(current_version)
    backends_down = [{"node": n["name"], "backend": b.get("name"), "error": b.get("error")}
                     for n in nodes if n["online"] for b in (n.get("backends") or []) if not b.get("ok")]
    warnings = []
    if not any(n["online"] for n in nodes):
        warnings.append("No worker is online: chat and indexing cannot run.")
    if not any(m["available"] and "completion" in m["capabilities"] for m in models):
        warnings.append("No chat model is available. Install one with Ollama (e.g. 'ollama pull llama3.2') or "
                        "from the Models page.")
    for b in backends_down:
        warnings.append(f"Backend {b['backend']} on {b['node']} is unreachable: {b['error']}")
    if st.cfg.inference.mock_enabled:
        warnings.append("MOCK inference is enabled: responses from mock models are canned text, not AI output.")
    return {
        "coordinator": {"version": __version__, "protocol_version": PROTOCOL_VERSION, "node_name": st.cfg.node.name,
                        "uptime_s": int((utcnow() - st.started_at).total_seconds()), "tls_mode": st.cfg.server.tls_mode,
                        "url": st.cfg.advertised_url(), "database": {"integrity": integrity, "schema_version": schema,
                                                                      "latest_schema_version": LATEST_SCHEMA_VERSION},
                        "mock_enabled": st.cfg.inference.mock_enabled, "cloud_enabled": st.cfg.cloud.enabled,
                        "tls_fingerprint": st.tls_fingerprint},
        "nodes": {"total": len(nodes), "online": sum(1 for n in nodes if n["online"]),
                  "items": [{k: n[k] for k in ("id", "name", "online", "mode", "telemetry", "running_jobs", "embedded",
                                               "heartbeat_age_s")} for n in nodes]},
        "models": {"total": len(models), "available": sum(1 for m in models if m["available"])},
        "jobs": counts,
        "active_jobs": await st.repos.jobs.list(status=["queued", "dispatched", "running"], limit=20),
        "recent_errors": await st.repos.jobs.recent_errors(8),
        "warnings": warnings,
    }


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(p / 100 * (len(values) - 1))))
    return round(values[k], 2)


@router.get("/system/metrics")
async def metrics(request: Request, principal: Principal = Depends(require("read"))) -> dict[str, Any]:
    """Aggregated measured metrics from completed jobs. Only values actually reported are used."""
    st = state(request)
    rows = await st.repos.jobs.metrics_summary(500)
    groups: dict[tuple[str, str, str], dict[str, list[float]]] = {}
    names = {n["id"]: n["name"] for n in await st.repos.nodes.list(include_revoked=True)}
    for r in rows:
        key = (r["type"], names.get(r["node_id"], r["node_id"] or "?"), r["model"] or "-")
        g = groups.setdefault(key, {})
        for k, v in (r["metrics_json"] or {}).items():
            if isinstance(v, int | float):
                g.setdefault(k, []).append(float(v))
    out = []
    for (jtype, node, model), g in groups.items():
        out.append({"type": jtype, "node": node, "model": model,
                     "samples": max((len(v) for v in g.values()), default=0),
                     "stats": {k: {"median": round(statistics.median(v), 2), "p90": _pct(v, 90), "n": len(v)}
                               for k, v in g.items()}})
    out.sort(key=lambda x: (x["type"], x["node"], x["model"]))
    return {"groups": out, "note": "Values are measured per job; metrics a backend does not report are omitted."}


@router.get("/system/audit")
async def audit(request: Request, limit: int = 200, action: str | None = None,
                principal: Principal = Depends(require_owner())) -> list[dict[str, Any]]:
    return await state(request).repos.audit.list(min(limit, 1000), action)


@router.get("/tls/ca", response_class=PlainTextResponse)
async def tls_ca(request: Request) -> PlainTextResponse:
    """The coordinator's local CA certificate (public). Workers verify its fingerprint before trusting it."""
    st = state(request)
    path = st.paths.tls_dir / "ca.pem"
    if not st.cfg.tls_enabled or not path.exists():
        raise NotFoundError("This coordinator does not use the HomeMesh local CA.")
    return PlainTextResponse(path.read_text(encoding="ascii"), media_type="application/x-pem-file")
