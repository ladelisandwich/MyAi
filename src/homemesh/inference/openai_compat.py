"""Adapter for OpenAI-compatible local servers: llama.cpp ``llama-server`` and vLLM.

These servers expose ``/v1/models``, ``/v1/chat/completions`` (SSE streaming) and
``/v1/embeddings`` but do **not** describe model capabilities. HomeMesh therefore only
advertises what the owner declares in ``homemesh.toml``::

    [[inference.backends]]
    name = "llamacpp"
    type = "openai_compat"
    url = "http://127.0.0.1:8080/v1"
    declared_models = { "qwen2.5-7b-instruct" = ["completion", "tools"] }

Undeclared models are advertised as plain ``completion`` models with a warning.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from homemesh.common.errors import BackendError
from homemesh.inference.base import ChatChunk, EmbedResult, InferenceBackend
from homemesh.inference.capabilities import normalize_capabilities
from homemesh.protocol.jobs import ChatJobPayload, GenerationMetrics, ToolCall
from homemesh.protocol.models import CAP_COMPLETION, CAP_EMBEDDING, BackendStatus, ModelInfo


class OpenAICompatBackend(InferenceBackend):
    type = "openai_compat"
    supports_embeddings = True

    def __init__(self, name: str, url: str, *, declared_models: dict[str, list[str]] | None = None,
                 declared_memory_mb: dict[str, int] | None = None, api_key_env: str = "",
                 timeout_s: float = 600, transport: httpx.AsyncBaseTransport | None = None) -> None:
        base = url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        super().__init__(name, base)
        self.declared = {k: normalize_capabilities(v) for k, v in (declared_models or {}).items()}
        self.declared_memory_mb = declared_memory_mb or {}
        headers = {}
        if api_key_env and os.environ.get(api_key_env):
            headers["Authorization"] = f"Bearer {os.environ[api_key_env]}"
        self._client = httpx.AsyncClient(base_url=base, headers=headers, trust_env=False, transport=transport,
                                         timeout=httpx.Timeout(connect=5.0, read=timeout_s, write=30.0, pool=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    def _wrap(self, exc: Exception, what: str) -> BackendError:
        if isinstance(exc, httpx.ConnectError):
            return BackendError(f"Cannot reach {self.name} at {self.url} ({what}).", code="backend_unreachable",
                                hint="Start the server (e.g. 'llama-server -m model.gguf --port 8080') or fix the url.")
        if isinstance(exc, httpx.TimeoutException):
            return BackendError(f"{self.name} timed out during {what}.", code="backend_timeout")
        if isinstance(exc, BackendError):
            return exc
        return BackendError(f"{self.name} request failed during {what}: {exc}", code="backend_error")

    async def health(self) -> BackendStatus:
        try:
            r = await self._client.get("/models", timeout=5.0)
            r.raise_for_status()
            return self.status(True)
        except Exception as exc:
            return self.status(False, error=self._wrap(exc, "health check").message)

    async def list_models(self) -> list[ModelInfo]:
        try:
            r = await self._client.get("/models", timeout=15.0)
            r.raise_for_status()
            data = r.json().get("data", [])
        except Exception as exc:
            raise self._wrap(exc, "listing models") from exc
        out = []
        for m in data:
            mid = m.get("id")
            if not mid:
                continue
            caps = self.declared.get(mid)
            warnings = []
            source = "declared"
            if not caps:
                caps = [CAP_COMPLETION]
                source = "inferred"
                warnings.append("Capabilities not declared in homemesh.toml; only plain chat is enabled.")
            mem_mb = self.declared_memory_mb.get(mid)
            ctx = None
            meta = m.get("meta") or {}
            if isinstance(meta.get("n_ctx_train"), int):
                ctx = meta["n_ctx_train"]
            if isinstance(m.get("max_model_len"), int):  # vLLM
                ctx = m["max_model_len"]
            out.append(ModelInfo(
                name=mid, backend=self.name, backend_type=self.type, capabilities=caps, capability_source=source,
                context_length=ctx, warnings=warnings,
                # A server like llama-server/vLLM keeps its model resident: treat it as loaded.
                loaded=True, estimated_memory_bytes=mem_mb * 1024 * 1024 if mem_mb else None,
            ))
        return out

    async def chat(self, payload: ChatJobPayload) -> AsyncIterator[ChatChunk]:  # type: ignore[override]
        messages: list[dict[str, Any]] = []
        for m in payload.messages:
            msg: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.images:
                msg["content"] = [{"type": "text", "text": m.content}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}} for img in m.images]
            if m.tool_calls:
                msg["tool_calls"] = [{"id": tc.id or f"call_{i}", "type": "function",
                                      "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                                     for i, tc in enumerate(m.tool_calls)]
            if m.role == "tool":
                msg["tool_call_id"] = m.tool_name or "tool"
            messages.append(msg)
        body: dict[str, Any] = {"model": payload.model, "messages": messages, "stream": True,
                                "stream_options": {"include_usage": True}}
        o = payload.options
        if o.temperature is not None:
            body["temperature"] = o.temperature
        if o.top_p is not None:
            body["top_p"] = o.top_p
        if o.num_predict is not None:
            body["max_tokens"] = o.num_predict
        if o.seed is not None:
            body["seed"] = o.seed
        if o.stop:
            body["stop"] = o.stop
        if payload.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                                "parameters": t.parameters}} for t in payload.tools]
        t0 = time.perf_counter()
        pending_tools: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        timings: dict[str, Any] = {}
        finish = None
        try:
            async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", errors="replace")
                    code = "model_not_found" if resp.status_code == 404 else "backend_http_error"
                    raise BackendError(f"{self.name} error (HTTP {resp.status_code}): {text[:500]}", code=code)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_s = line[5:].strip()
                    if data_s == "[DONE]":
                        break
                    try:
                        data = json.loads(data_s)
                    except json.JSONDecodeError:
                        continue
                    if data.get("usage"):
                        usage = data["usage"]
                    if data.get("timings"):
                        timings = data["timings"]
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("reasoning_content"):
                            yield ChatChunk(kind="thinking", text=delta["reasoning_content"])
                        if delta.get("content"):
                            yield ChatChunk(kind="content", text=delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = pending_tools.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] += fn.get("name") or ""
                            slot["args"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except BackendError:
            raise
        except Exception as exc:
            raise self._wrap(exc, "chat") from exc
        if pending_tools:
            calls = []
            for slot in pending_tools.values():
                try:
                    args = json.loads(slot["args"]) if slot["args"] else {}
                except json.JSONDecodeError:
                    args = {"_raw": slot["args"]}
                calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))
            yield ChatChunk(kind="tool_calls", tool_calls=calls)
        metrics = GenerationMetrics(
            total_ms=round((time.perf_counter() - t0) * 1000, 2),
            prompt_tokens=usage.get("prompt_tokens") or timings.get("prompt_n"),
            completion_tokens=usage.get("completion_tokens") or timings.get("predicted_n"),
            prompt_eval_ms=timings.get("prompt_ms"), eval_ms=timings.get("predicted_ms"),
            tokens_per_second=round(timings["predicted_per_second"], 2) if timings.get("predicted_per_second") else None,
        )
        yield ChatChunk(kind="done", finish_reason=finish, metrics=metrics, model=payload.model)

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        if CAP_EMBEDDING not in self.declared.get(model, []):
            raise BackendError(f"Model {model!r} is not declared as an embedding model for backend {self.name}.",
                               code="model_incompatible",
                               hint="Add it to declared_models with [\"embedding\"] in homemesh.toml.")
        t0 = time.perf_counter()
        try:
            r = await self._client.post("/embeddings", json={"model": model, "input": inputs})
        except Exception as exc:
            raise self._wrap(exc, "embedding") from exc
        if r.status_code != 200:
            raise BackendError(f"{self.name} embedding error (HTTP {r.status_code}): {r.text[:300]}",
                               code="backend_http_error")
        data = sorted(r.json().get("data", []), key=lambda d: d.get("index", 0))
        return EmbedResult(embeddings=[d["embedding"] for d in data], model=model,
                           metrics=GenerationMetrics(total_ms=round((time.perf_counter() - t0) * 1000, 2)))
