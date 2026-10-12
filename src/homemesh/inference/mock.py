"""Clearly labelled MOCK inference backend for automated tests and UI development.

This backend performs **no AI inference**. Every response starts with ``[MOCK]`` and all
results are flagged ``mock=True`` through the protocol, API and UI so mock output can never
be mistaken for real model output. It is only active when ``inference.mock_enabled = true``
(or ``HOMEMESH_MOCK=1``).

Test directives (in the last user message):

* ``[[mock:fail]]``            - fail mid-stream with a backend error
* ``[[mock:slow]]``            - stream slowly (for cancellation / disconnect tests)
* ``[[mock:tool:NAME {json}]]`` - request a tool call (only when tools were offered)
* ``[[mock:oom]]``             - fail as if the model did not fit in memory
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from collections.abc import AsyncIterator

from homemesh.common.errors import BackendError
from homemesh.inference.base import ChatChunk, EmbedResult, InferenceBackend, PullProgress
from homemesh.protocol.jobs import ChatJobPayload, GenerationMetrics, ToolCall
from homemesh.protocol.models import (
    CAP_COMPLETION,
    CAP_EMBEDDING,
    CAP_TOOLS,
    CAP_VISION,
    BackendStatus,
    ModelInfo,
)

MOCK_PREFIX = "[MOCK]"
MOCK_EMBED_DIM = 64
_TOOL_RE = re.compile(r"\[\[mock:tool:([a-zA-Z0-9_.-]+)\s*(\{.*?\})?\]\]", re.S)
_WORD_RE = re.compile(r"[a-z0-9]+")

MOCK_MODELS = {
    "mock-chat": [CAP_COMPLETION, CAP_TOOLS],
    "mock-vision": [CAP_COMPLETION, CAP_VISION],
    "mock-embed": [CAP_EMBEDDING],
}


def mock_embedding(text: str, dim: int = MOCK_EMBED_DIM) -> list[float]:
    """Deterministic bag-of-words hashing embedding: similar texts share dimensions."""
    vec = [0.0] * dim
    for w in _WORD_RE.findall(text.lower()):
        h = int.from_bytes(hashlib.sha256(w.encode()).digest()[:4], "big")
        vec[h % dim] += 1.0 if (h >> 31) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class MockBackend(InferenceBackend):
    type = "mock"
    is_mock = True
    supports_pull = True
    supports_load_unload = True
    supports_embeddings = True

    def __init__(self, name: str = "mock", token_delay_s: float | None = None) -> None:
        super().__init__(name, "mock://")
        env_delay = os.environ.get("HOMEMESH_MOCK_DELAY")
        self.token_delay_s = token_delay_s if token_delay_s is not None else float(env_delay or 0.01)
        self.loaded: set[str] = set()
        self.extra_models: dict[str, list[str]] = {}

    async def health(self) -> BackendStatus:
        return self.status(True, version="mock")

    async def list_models(self) -> list[ModelInfo]:
        out = []
        for name, caps in {**MOCK_MODELS, **self.extra_models}.items():
            out.append(ModelInfo(
                name=name, backend=self.name, backend_type=self.type, size_bytes=10 * 1024 * 1024,
                family="mock", parameter_size="0B", context_length=4096,
                embedding_length=MOCK_EMBED_DIM if CAP_EMBEDDING in caps else None,
                capabilities=list(caps), capability_source="mock", mock=True,
                loaded=name in self.loaded, estimated_memory_bytes=64 * 1024 * 1024,
                warnings=["MOCK model: produces canned text, no AI inference is performed."],
            ))
        return out

    async def chat(self, payload: ChatJobPayload) -> AsyncIterator[ChatChunk]:  # type: ignore[override]
        if payload.model not in MOCK_MODELS and payload.model not in self.extra_models:
            raise BackendError(f"Model {payload.model!r} is not installed in the mock backend.",
                               code="model_not_found", status_code=404)
        t0 = time.perf_counter()
        last_user = next((m for m in reversed(payload.messages) if m.role == "user"), None)
        last = payload.messages[-1] if payload.messages else None
        text_in = last_user.content if last_user else ""
        delay = self.token_delay_s
        if "[[mock:slow]]" in text_in:
            delay = max(delay, 0.2)
        if "[[mock:oom]]" in text_in:
            raise BackendError("mock: model requires more memory than available", code="insufficient_memory")
        self.loaded.add(payload.model)

        # Tool round trip: if the last message is a tool result, acknowledge it.
        if last is not None and last.role == "tool":
            reply = f"{MOCK_PREFIX} Tool {last.tool_name or 'tool'} returned: {last.content[:300]}"
        else:
            m = _TOOL_RE.search(text_in)
            if m and payload.tools and any(t.name == m.group(1) for t in payload.tools):
                args = json.loads(m.group(2)) if m.group(2) else {}
                yield ChatChunk(kind="tool_calls", tool_calls=[ToolCall(name=m.group(1), arguments=args)])
                yield ChatChunk(kind="done", finish_reason="tool_calls",
                                metrics=GenerationMetrics(total_ms=round((time.perf_counter() - t0) * 1000, 2)),
                                model=payload.model)
                return
            has_system_context = any(m.role == "system" and "[S1]" in m.content for m in payload.messages)
            reply = f"{MOCK_PREFIX} This is a canned response from the mock backend (no AI model ran). "
            reply += f"You said: {text_in.strip()[:200]}"
            if has_system_context:
                reply += " Sources were provided to me: [S1]."
            if any(m.images for m in payload.messages):
                reply += " (An image was attached.)"

        words = reply.split(" ")
        first = True
        ttft = None
        for i, w in enumerate(words):
            if "[[mock:fail]]" in text_in and i == max(1, len(words) // 2):
                raise BackendError("mock: simulated backend failure mid-stream", code="backend_error")
            await asyncio.sleep(delay)
            if first:
                ttft = (time.perf_counter() - t0) * 1000
                first = False
            yield ChatChunk(kind="content", text=w + (" " if i < len(words) - 1 else ""))
        total = (time.perf_counter() - t0) * 1000
        yield ChatChunk(kind="done", finish_reason="stop", model=payload.model, metrics=GenerationMetrics(
            ttft_ms=round(ttft or total, 2), total_ms=round(total, 2), completion_tokens=len(words),
            prompt_tokens=sum(len(m.content.split()) for m in payload.messages),
            tokens_per_second=round(len(words) / (total / 1000), 2) if total > 0 else None,
        ))

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        if model != "mock-embed" and CAP_EMBEDDING not in self.extra_models.get(model, []):
            raise BackendError(f"Model {model!r} does not support embeddings.", code="model_incompatible")
        return EmbedResult(embeddings=[mock_embedding(t) for t in inputs], model=model)

    async def pull(self, model: str) -> AsyncIterator[PullProgress]:  # type: ignore[override]
        total = 100
        for done in range(0, total + 1, 25):
            await asyncio.sleep(self.token_delay_s)
            yield PullProgress(status="downloading", completed=done, total=total)
        self.extra_models[model] = [CAP_COMPLETION]
        yield PullProgress(status="success")

    async def delete(self, model: str) -> None:
        self.extra_models.pop(model, None)

    async def load(self, model: str, keep_alive: str | None = None) -> None:
        self.loaded.add(model)

    async def unload(self, model: str) -> None:
        self.loaded.discard(model)
