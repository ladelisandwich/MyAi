"""Job execution on a worker: chat, embeddings, document scans, tools and model management."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from homemesh.common.errors import BackendError, HomeMeshError
from homemesh.common.logging import get_logger
from homemesh.inference.base import InferenceBackend
from homemesh.protocol import messages as pm
from homemesh.protocol.jobs import (
    ChatJobPayload,
    ChatJobResult,
    EmbedJobPayload,
    EmbedJobResult,
    GenerationMetrics,
    KnowledgeScanPayload,
    ModelManagePayload,
    ModelManageResult,
    ToolExecPayload,
    ToolExecResult,
)
from homemesh.protocol.models import ModelInfo

log = get_logger(__name__)

Send = Callable[[Any], Awaitable[None]]


class JobFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Executor:
    def __init__(self, backends: dict[str, InferenceBackend], policy: Any, *, allow_model_downloads: bool,
                 remote_allowed: Callable[[str], bool], models_cache: Callable[[], list[ModelInfo]],
                 tool_runner: Callable[[ToolExecPayload], Awaitable[ToolExecResult]] | None = None) -> None:
        self.backends = backends
        self.policy = policy
        self.allow_model_downloads = allow_model_downloads
        self.remote_allowed = remote_allowed
        self.models_cache = models_cache
        self.tool_runner = tool_runner

    def _backend(self, name: str) -> InferenceBackend:
        b = self.backends.get(name)
        if b is None:
            raise JobFailure("backend_not_found", f"inference backend '{name}' is not configured on this worker")
        return b

    def _model_info(self, backend: str, model: str) -> ModelInfo | None:
        for m in self.models_cache():
            if m.backend == backend and m.name == model:
                return m
        return None

    # ------------------------------------------------------------------ chat
    async def chat(self, job_id: str, payload: ChatJobPayload, send: Send, partial: dict[str, Any]) -> dict[str, Any]:
        backend = self._backend(payload.backend)
        info = self._model_info(payload.backend, payload.model)
        if info and info.remote and not self.remote_allowed(payload.backend):
            raise JobFailure("remote_model_disallowed",
                             f"{payload.model} is a cloud model; cloud use is disabled on this worker")
        content: list[str] = []
        thinking: list[str] = []
        tool_calls = []
        seq = 0
        t0 = time.perf_counter()
        ttft = None
        finish = None
        backend_metrics = GenerationMetrics()
        partial["content"] = ""
        async for chunk in backend.chat(payload):
            if chunk.kind in ("content", "thinking") and ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
            if chunk.kind == "content":
                content.append(chunk.text)
                partial["content"] += chunk.text
                seq += 1
                await send(pm.JobOutput(job_id=job_id, seq=seq, kind="content", delta=chunk.text))
            elif chunk.kind == "thinking":
                thinking.append(chunk.text)
                seq += 1
                await send(pm.JobOutput(job_id=job_id, seq=seq, kind="thinking", delta=chunk.text))
            elif chunk.kind == "tool_calls":
                tool_calls.extend(chunk.tool_calls)
                seq += 1
                await send(pm.JobOutput(job_id=job_id, seq=seq, kind="tool_calls",
                                        data={"tool_calls": [tc.model_dump() for tc in chunk.tool_calls]}))
            elif chunk.kind == "done":
                finish = chunk.finish_reason
                if chunk.metrics:
                    backend_metrics = chunk.metrics
        total = (time.perf_counter() - t0) * 1000
        metrics = backend_metrics.model_copy(update={
            "ttft_ms": round(ttft, 2) if ttft is not None else backend_metrics.ttft_ms,
            "total_ms": round(total, 2),
        })
        if payload.tools is None and tool_calls:
            tool_calls = []  # tools were not offered; never act on unsolicited calls
        result = ChatJobResult(content="".join(content), thinking="".join(thinking), tool_calls=tool_calls,
                               finish_reason=finish, model=payload.model, backend=payload.backend,
                               backend_type=backend.type, mock=backend.is_mock, metrics=metrics)
        return result.model_dump()

    # ------------------------------------------------------------------ embeddings
    async def embed(self, payload: EmbedJobPayload) -> dict[str, Any]:
        backend = self._backend(payload.backend)
        info = self._model_info(payload.backend, payload.model)
        if info and info.remote and not self.remote_allowed(payload.backend):
            raise JobFailure("remote_model_disallowed", f"{payload.model} is a cloud model; cloud use is disabled")
        res = await backend.embed(payload.model, payload.inputs)
        dim = len(res.embeddings[0]) if res.embeddings else 0
        return EmbedJobResult(model=payload.model, backend=payload.backend, dim=dim, embeddings=res.embeddings,
                              refs=payload.refs, mock=backend.is_mock, metrics=res.metrics).model_dump()

    # ------------------------------------------------------------------ knowledge
    async def knowledge_scan(self, job_id: str, payload: KnowledgeScanPayload, send: Send) -> dict[str, Any]:
        from homemesh.knowledge.scanner import scan

        seq = 0

        async def emit(rec: Any) -> None:
            nonlocal seq
            seq += 1
            await send(pm.JobOutput(job_id=job_id, seq=seq, kind="document", data=rec.model_dump()))

        async def progress(frac: float, msg: str) -> None:
            await send(pm.JobProgress(job_id=job_id, progress=frac, message=msg))

        try:
            result = await scan(payload, self.policy.path_policy(), emit, progress)
        except PermissionError as exc:
            raise JobFailure("path_not_allowed", str(exc)) from exc
        return result.model_dump()

    # ------------------------------------------------------------------ tools
    async def tool_exec(self, payload: ToolExecPayload) -> dict[str, Any]:
        if self.tool_runner is None:
            raise JobFailure("unsupported", "tool execution is not available on this worker")
        res = await self.tool_runner(payload)
        return res.model_dump()

    # ------------------------------------------------------------------ model management
    async def model_manage(self, job_id: str, payload: ModelManagePayload, send: Send) -> dict[str, Any]:
        backend = self._backend(payload.backend)
        t0 = time.perf_counter()
        if payload.action == "pull":
            if not self.allow_model_downloads:
                raise JobFailure("downloads_disabled", "model downloads are disabled on this worker "
                                                       "(inference.allow_model_downloads = false)")
            if not payload.model:
                raise JobFailure("invalid_payload", "model name required")
            last_sent = 0.0
            async for p in backend.pull(payload.model):
                frac = (p.completed / p.total) if p.completed and p.total else None
                now = time.monotonic()
                if now - last_sent > 0.5 or p.status == "success":
                    last_sent = now
                    await send(pm.JobProgress(job_id=job_id, progress=frac, message=p.status, data=p.as_dict()))
            msg = f"downloaded {payload.model}"
        elif payload.action == "delete":
            await backend.delete(payload.model or "")
            msg = f"deleted {payload.model}"
        elif payload.action == "load":
            await backend.load(payload.model or "", payload.keep_alive)
            msg = f"loaded {payload.model}"
        elif payload.action == "unload":
            await backend.unload(payload.model or "")
            msg = f"unloaded {payload.model}"
        else:
            msg = "refreshed"
        return ModelManageResult(action=payload.action, model=payload.model, ok=True, message=msg,
                                 duration_ms=round((time.perf_counter() - t0) * 1000, 1)).model_dump()


def failure_from_exception(exc: BaseException) -> JobFailure:
    if isinstance(exc, JobFailure):
        return exc
    if isinstance(exc, BackendError):
        retryable = exc.code in ("backend_unreachable", "backend_timeout")
        msg = exc.message + (f" Hint: {exc.hint}" if exc.hint else "")
        return JobFailure(exc.code, msg, retryable=retryable)
    if isinstance(exc, HomeMeshError):
        return JobFailure(exc.code, exc.message)
    if isinstance(exc, ValueError):
        return JobFailure("invalid_payload", str(exc))
    log.exception("Unexpected job error", exc_info=exc)
    return JobFailure("worker_error", f"unexpected error on worker: {type(exc).__name__}: {exc}")
