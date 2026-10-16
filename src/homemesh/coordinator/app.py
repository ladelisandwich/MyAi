"""FastAPI application factory for the coordinator."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import HomeMeshError
from homemesh.common.logging import get_logger
from homemesh.common.paths import AppPaths
from homemesh.coordinator.api import (
    attachments,
    auth,
    backups,
    conversations,
    events,
    jobs,
    knowledge,
    memory,
    models,
    nodes,
    settings,
    system,
    tools,
    updates,
    worker_ws,
)
from homemesh.coordinator.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from homemesh.coordinator.state import CoordinatorState
from homemesh.version import __version__

log = get_logger(__name__)


def find_web_dist(cfg: HomeMeshConfig) -> Path | None:
    candidates = []
    if cfg.server.web_dist_dir:
        candidates.append(Path(cfg.server.web_dist_dir))
    candidates.append(Path(__file__).resolve().parent.parent / "web_dist")  # packaged build
    candidates.append(Path(__file__).resolve().parents[3] / "apps" / "web" / "dist")  # source checkout
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return None


def create_app(cfg: HomeMeshConfig, paths: AppPaths, *, state: CoordinatorState | None = None) -> FastAPI:
    st = state or CoordinatorState(cfg, paths)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await st.start()
        if st.setup_token:
            log.warning("HomeMesh is not set up yet. Open %s and use this one-time setup token: %s "
                        "(or run 'homemesh setup' on this computer).", cfg.local_base_url(), st.setup_token)
        try:
            yield
        finally:
            await st.stop()

    # FastAPI's OpenTelemetry instrumentation is switched off explicitly: HomeMesh sends no telemetry
    # anywhere, even if OTEL/FASTAPI_OTEL_* variables happen to be set in the environment.
    app = FastAPI(title="HomeMesh AI", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json",
                  telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False,
                             "auto_configure": False})
    app.state.hm = st

    @app.exception_handler(HomeMeshError)
    async def _hm_error(request: Request, exc: HomeMeshError) -> JSONResponse:
        headers = {}
        if exc.code == "rate_limited" and exc.details.get("retry_after_s"):
            headers["Retry-After"] = str(exc.details["retry_after_s"])
        return JSONResponse({"error": exc.to_dict()}, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        msgs = []
        for e in exc.errors()[:10]:
            loc = ".".join(str(p) for p in e.get("loc", []) if p != "body")
            msgs.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        return JSONResponse({"error": {"code": "validation_failed", "message": "; ".join(msgs)}}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"error": {"code": f"http_{exc.status_code}", "message": str(exc.detail)}},
                            status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": {"code": "internal_error",
                                       "message": "An unexpected error occurred. See the coordinator log for details."}},
                            status_code=500)

    for r in (auth, system, nodes, updates, models, conversations, memory, knowledge, jobs, tools, settings, events,
              attachments, backups):
        app.include_router(r.router)
    app.include_router(worker_ws.router)

    web_dist = find_web_dist(cfg)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path.startswith("api/"):
            return JSONResponse({"error": {"code": "not_found", "message": "Unknown API endpoint."}}, status_code=404)
        if web_dist is None:
            return JSONResponse({"error": {"code": "web_ui_missing", "message":
                                "The web interface has not been built. Run scripts/build_web.sh (or build_web.ps1); "
                                "the API is available under /api."}}, status_code=503)
        target = (web_dist / full_path).resolve()
        if full_path and target.is_file() and web_dist.resolve() in target.parents:
            headers = {}
            if full_path.startswith("assets/"):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif full_path in ("sw.js", "manifest.webmanifest"):
                headers["Cache-Control"] = "no-cache"
            return FileResponse(target, headers=headers)
        return FileResponse(web_dist / "index.html", headers={"Cache-Control": "no-cache"})

    hsts = cfg.server.tls_mode in ("self_signed", "custom")
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=int(cfg.server.max_request_mb * 1024 * 1024),
                       upload_max_bytes=int(cfg.server.max_upload_mb * 1024 * 1024),
                       upload_prefixes=("/api/v1/attachments",))
    return app
