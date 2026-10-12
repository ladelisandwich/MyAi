"""Ollama backend adapter (native API, verified against Ollama 0.40.x).

Endpoints used: ``/api/version``, ``/api/tags``, ``/api/show``, ``/api/ps``, ``/api/chat``
(NDJSON streaming), ``/api/embed``, ``/api/pull`` (streaming progress), ``/api/delete`` and
``/api/generate`` (load / unload via ``keep_alive``).

Ollama can list "cloud" models that are proxied to a remote service (``remote_host``).
Those are flagged ``remote=True`` and refused unless explicitly allowed, because they would
send prompts outside the home network.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from homemesh.common.errors import BackendError
from homemesh.common.logging import get_logger
from homemesh.inference.base import ChatChunk, EmbedResult, InferenceBackend, PullProgress
from homemesh.inference.capabilities import (
    context_length_from_model_info,
    embedding_length_from_model_info,
    estimate_memory_bytes,
    infer_capabilities,
    normalize_capabilities,
)
from homemesh.protocol.jobs import ChatJobPayload, GenerationMetrics, ToolCall
from homemesh.protocol.models import BackendStatus, ModelInfo

log = get_logger(__name__)

NS_PER_MS = 1_000_000


def _ns_to_ms(v: Any) -> float | None:
    return round(v / NS_PER_MS, 3) if isinstance(v, int | float) and v > 0 else None


def metrics_from_final(chunk: dict[str, Any]) -> GenerationMetrics:
    eval_count = chunk.get("eval_count")
    eval_ns = chunk.get("eval_duration")
    tps = None
    if isinstance(eval_count, int) and isinstance(eval_ns, int) and eval_ns > 0:
        tps = round(eval_count / (eval_ns / 1e9), 2)
    return GenerationMetrics(
        load_ms=_ns_to_ms(chunk.get("load_duration")),
        prompt_tokens=chunk.get("prompt_eval_count"),
        completion_tokens=eval_count,
        prompt_eval_ms=_ns_to_ms(chunk.get("prompt_eval_duration")),
        eval_ms=_ns_to_ms(eval_ns),
        tokens_per_second=tps,
    )


_MISSING_MODEL = re.compile(r"model ['\"]?[^'\"]*['\"]? not found|try pulling it")

class OllamaBackend(InferenceBackend):
    type = "ollama"
    supports_pull = True
    supports_load_unload = True
    supports_embeddings = True

    def __init__(self, name: str, url: str, *, timeout_s: float = 600, allow_remote_models: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(name, url.rstrip("/"))
        self.allow_remote_models = allow_remote_models
        # trust_env=False: local/LAN backend traffic must never be routed via an HTTP proxy.
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(connect=5.0, read=timeout_s, write=30.0, pool=10.0),
            trust_env=False,
            transport=transport,
        )
        self._show_cache: dict[str, dict[str, Any]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ helpers
    def _wrap(self, exc: Exception, what: str) -> BackendError:
        if isinstance(exc, httpx.ConnectError):
            return BackendError(
                f"Cannot reach Ollama at {self.url} ({what}).", code="backend_unreachable",
                hint="Start Ollama ('ollama serve' or its service) or fix the backend url in homemesh.toml.",
            )
        if isinstance(exc, httpx.TimeoutException):
            return BackendError(f"Ollama at {self.url} timed out during {what}.", code="backend_timeout",
                                hint="The model may still be loading; large models can take minutes on first use.")
        if isinstance(exc, BackendError):
            return exc
        return BackendError(f"Ollama request failed during {what}: {exc}", code="backend_error")

    @staticmethod
    def _error_from_response(status: int, body: str, model: str | None = None) -> BackendError:
        try:
            msg = json.loads(body).get("error", body)
        except (json.JSONDecodeError, AttributeError):
            msg = body
        msg = str(msg).strip() or f"HTTP {status}"
        low = msg.lower()
        # Only Ollama's own "model ... not found" (HTTP 404) means the model is missing. Runner errors can
        # also contain "not found" (e.g. a library file) and must not be reported as a missing model.
        if status == 404 or _MISSING_MODEL.search(low):
            return BackendError(f"Model {model!r} is not installed in Ollama: {msg}", code="model_not_found",
                                hint=f"Download it with 'ollama pull {model}' or from the Models page.",
                                status_code=404)
        if "does not support" in low:
            return BackendError(f"Model {model!r} is incompatible with this request: {msg}",
                                code="model_incompatible", status_code=400)
        if "out of memory" in low or "insufficient" in low or "requires more system memory" in low:
            return BackendError(f"Ollama could not load {model!r}: {msg}", code="insufficient_memory",
                                hint="Choose a smaller/quantized model, close other GPU applications, or "
                                     "use a node with more memory.")
        if "llama-server" in low or "llama runner" in low or "runner process" in low:
            return BackendError(
                f"Ollama could not start its model runner for {model!r} on this computer: {msg}",
                code="backend_runner_failed",
                hint="This is a problem with Ollama on that computer, not with the model download. Check Ollama's "
                     "server.log (Windows: %LOCALAPPDATA%\\Ollama\\server.log; Linux: journalctl --user -u ollama). "
                     "Common causes: a GPU driver too old for the card, antivirus or Smart App Control blocking "
                     "Ollama's files, or a GPU backend that crashes. To test without GPUs, set CUDA_VISIBLE_DEVICES=-1 "
                     "and OLLAMA_VULKAN=0 for Ollama and restart it.")
        return BackendError(f"Ollama error (HTTP {status}): {msg}", code="backend_http_error")

    # ------------------------------------------------------------------ status / models
    async def health(self) -> BackendStatus:
        try:
            r = await self._client.get("/api/version", timeout=5.0)
            r.raise_for_status()
            return self.status(True, version=r.json().get("version"))
        except Exception as exc:
            return self.status(False, error=self._wrap(exc, "health check").message)

    async def _show(self, model: str, digest: str | None) -> dict[str, Any]:
        key = f"{model}@{digest}"
        if key in self._show_cache:
            return self._show_cache[key]
        r = await self._client.post("/api/show", json={"model": model}, timeout=30.0)
        if r.status_code != 200:
            return {}
        data = r.json()
        # Drop large fields we never use.
        for k in ("license", "modelfile", "template", "tensors"):
            data.pop(k, None)
        self._show_cache[key] = data
        return data

    async def running(self) -> dict[str, dict[str, Any]]:
        try:
            r = await self._client.get("/api/ps", timeout=10.0)
            r.raise_for_status()
            return {m.get("name") or m.get("model"): m for m in r.json().get("models", [])}
        except Exception:
            return {}

    async def list_models(self) -> list[ModelInfo]:
        try:
            r = await self._client.get("/api/tags", timeout=15.0)
            r.raise_for_status()
            tags = r.json().get("models", [])
        except Exception as exc:
            raise self._wrap(exc, "listing models") from exc
        running = await self.running()
        models: list[ModelInfo] = []
        for t in tags:
            name = t.get("name") or t.get("model")
            if not name:
                continue
            details = t.get("details") or {}
            caps = normalize_capabilities(t.get("capabilities"))
            ctx = details.get("context_length")
            emb_len = details.get("embedding_length")
            cap_source = "backend"
            if not caps or ctx is None:
                show = await self._show(name, t.get("digest"))
                caps = caps or normalize_capabilities(show.get("capabilities"))
                info = show.get("model_info")
                ctx = ctx or context_length_from_model_info(info)
                emb_len = emb_len or embedding_length_from_model_info(info)
            if not caps:
                caps = infer_capabilities(name, details.get("family"))
                cap_source = "inferred"
            remote_host = t.get("remote_host")
            warnings: list[str] = []
            if remote_host:
                warnings.append(f"Cloud model: inference runs on {remote_host}, not on this computer.")
            if cap_source == "inferred":
                warnings.append("Backend did not report capabilities; tools/vision are disabled for this model.")
            run = running.get(name)
            size = t.get("size")
            m = ModelInfo(
                name=name, backend=self.name, backend_type=self.type, digest=t.get("digest"),
                size_bytes=size if isinstance(size, int) and not remote_host else None,
                family=details.get("family"), parameter_size=details.get("parameter_size"),
                quantization=details.get("quantization_level"), format=details.get("format"),
                context_length=ctx, embedding_length=emb_len, capabilities=caps, capability_source=cap_source,
                remote=bool(remote_host), remote_host=remote_host, warnings=warnings,
            )
            if run:
                m.loaded = True
                m.loaded_size_bytes = run.get("size")
                m.loaded_vram_bytes = run.get("size_vram")
                m.loaded_context_length = run.get("context_length")
            m.estimated_memory_bytes = estimate_memory_bytes(m.size_bytes, m.loaded_size_bytes)
            models.append(m)
        return models

    # ------------------------------------------------------------------ chat
    def _chat_body(self, payload: ChatJobPayload) -> dict[str, Any]:
        messages = []
        for m in payload.messages:
            msg: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.images:
                msg["images"] = m.images
            if m.tool_calls:
                msg["tool_calls"] = [{"function": {"name": tc.name, "arguments": tc.arguments}} for tc in m.tool_calls]
            if m.role == "tool" and m.tool_name:
                msg["tool_name"] = m.tool_name
            messages.append(msg)
        body: dict[str, Any] = {"model": payload.model, "messages": messages, "stream": True}
        opts = payload.options.model_dump(exclude_none=True)
        if opts:
            body["options"] = opts
        if payload.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.parameters}}
                             for t in payload.tools]
        if payload.think is not None:
            body["think"] = payload.think
        if payload.keep_alive:
            body["keep_alive"] = payload.keep_alive
        return body

    async def chat(self, payload: ChatJobPayload) -> AsyncIterator[ChatChunk]:  # type: ignore[override]
        body = self._chat_body(payload)
        t0 = time.perf_counter()
        try:
            async with self._client.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise self._error_from_response(resp.status_code, text, payload.model)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("ignoring malformed line from Ollama: %r", line[:200])
                        continue
                    if "error" in data:
                        raise self._error_from_response(500, json.dumps(data), payload.model)
                    msg = data.get("message") or {}
                    if msg.get("thinking"):
                        yield ChatChunk(kind="thinking", text=msg["thinking"])
                    if msg.get("content"):
                        yield ChatChunk(kind="content", text=msg["content"])
                    if msg.get("tool_calls"):
                        calls = []
                        for tc in msg["tool_calls"]:
                            fn = tc.get("function") or {}
                            args = fn.get("arguments") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {"_raw": args}
                            calls.append(ToolCall(id=tc.get("id"), name=fn.get("name", ""), arguments=args))
                        yield ChatChunk(kind="tool_calls", tool_calls=calls)
                    if data.get("done"):
                        metrics = metrics_from_final(data)
                        metrics.total_ms = round((time.perf_counter() - t0) * 1000, 2)
                        yield ChatChunk(kind="done", finish_reason=data.get("done_reason"), metrics=metrics,
                                        model=data.get("model"))
                        return
        except BackendError:
            raise
        except Exception as exc:
            raise self._wrap(exc, "chat") from exc
        raise BackendError("Ollama closed the stream before finishing the response.", code="backend_stream_ended")

    # ------------------------------------------------------------------ embeddings
    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        t0 = time.perf_counter()
        try:
            r = await self._client.post("/api/embed", json={"model": model, "input": inputs, "truncate": True})
        except Exception as exc:
            raise self._wrap(exc, "embedding") from exc
        if r.status_code != 200:
            raise self._error_from_response(r.status_code, r.text, model)
        data = r.json()
        embs = data.get("embeddings")
        if not isinstance(embs, list) or len(embs) != len(inputs):
            raise BackendError("Ollama returned an unexpected embedding response.", code="backend_bad_response")
        return EmbedResult(
            embeddings=embs, model=data.get("model", model),
            metrics=GenerationMetrics(load_ms=_ns_to_ms(data.get("load_duration")),
                                      prompt_tokens=data.get("prompt_eval_count"),
                                      total_ms=round((time.perf_counter() - t0) * 1000, 2)),
        )

    # ------------------------------------------------------------------ model management
    async def pull(self, model: str) -> AsyncIterator[PullProgress]:  # type: ignore[override]
        try:
            async with self._client.stream("POST", "/api/pull", json={"model": model, "stream": True},
                                           timeout=httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=10.0)) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise self._error_from_response(resp.status_code, text, model)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise BackendError(f"Download of {model!r} failed: {data['error']}", code="pull_failed",
                                           hint="Check the model name and this node's internet access.")
                    yield PullProgress(status=data.get("status", ""), completed=data.get("completed"),
                                       total=data.get("total"), digest=data.get("digest"))
        except BackendError:
            raise
        except Exception as exc:
            raise self._wrap(exc, f"pulling {model}") from exc

    async def delete(self, model: str) -> None:
        try:
            r = await self._client.request("DELETE", "/api/delete", json={"model": model})
        except Exception as exc:
            raise self._wrap(exc, f"deleting {model}") from exc
        if r.status_code != 200:
            raise self._error_from_response(r.status_code, r.text, model)

    async def load(self, model: str, keep_alive: str | None = None) -> None:
        body: dict[str, Any] = {"model": model}
        if keep_alive:
            body["keep_alive"] = keep_alive
        try:
            r = await self._client.post("/api/generate", json=body)
            if r.status_code == 400 and "does not support generate" in r.text:
                # Embedding-only models cannot be loaded through /api/generate; an empty /api/embed
                # request loads them (verified against Ollama 0.40.3).
                r = await self._client.post("/api/embed", json={**body, "input": []})
        except Exception as exc:
            raise self._wrap(exc, f"loading {model}") from exc
        if r.status_code != 200:
            raise self._error_from_response(r.status_code, r.text, model)

    async def unload(self, model: str) -> None:
        try:
            r = await self._client.post("/api/generate", json={"model": model, "keep_alive": 0})
        except Exception as exc:
            raise self._wrap(exc, f"unloading {model}") from exc
        if r.status_code != 200:
            raise self._error_from_response(r.status_code, r.text, model)
