"""Inference backend interface.

Every backend (Ollama, OpenAI-compatible servers such as llama.cpp's ``llama-server`` and
vLLM, and the test-only mock) implements :class:`InferenceBackend`. Workers talk to
backends running on *their own* machine; the coordinator never calls a backend directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from homemesh.common.errors import BackendError
from homemesh.protocol.jobs import ChatJobPayload, GenerationMetrics, ToolCall
from homemesh.protocol.models import BackendStatus, ModelInfo


@dataclass
class ChatChunk:
    kind: Literal["content", "thinking", "tool_calls", "done"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    metrics: GenerationMetrics | None = None  # backend-reported numbers (on "done")
    model: str | None = None  # model name as reported by the backend


@dataclass
class EmbedResult:
    embeddings: list[list[float]]
    model: str
    metrics: GenerationMetrics = field(default_factory=GenerationMetrics)


@dataclass
class PullProgress:
    status: str
    completed: int | None = None
    total: int | None = None
    digest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "completed": self.completed, "total": self.total, "digest": self.digest}


class NotSupported(BackendError):
    code = "not_supported"
    status_code = 400


class InferenceBackend(ABC):
    type: str = "abstract"
    is_mock: bool = False
    supports_pull: bool = False
    supports_load_unload: bool = False
    supports_embeddings: bool = False

    def __init__(self, name: str, url: str = "") -> None:
        self.name = name
        self.url = url

    @abstractmethod
    async def health(self) -> BackendStatus: ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    @abstractmethod
    def chat(self, payload: ChatJobPayload) -> AsyncIterator[ChatChunk]:
        """Stream a chat completion. The final chunk has ``kind == 'done'``."""

    async def embed(self, model: str, inputs: list[str]) -> EmbedResult:
        raise NotSupported(f"backend '{self.name}' does not support embeddings")

    def pull(self, model: str) -> AsyncIterator[PullProgress]:
        raise NotSupported(f"backend '{self.name}' cannot download models")

    async def delete(self, model: str) -> None:
        raise NotSupported(f"backend '{self.name}' cannot delete models")

    async def load(self, model: str, keep_alive: str | None = None) -> None:
        raise NotSupported(f"backend '{self.name}' cannot load/unload models on demand")

    async def unload(self, model: str) -> None:
        raise NotSupported(f"backend '{self.name}' cannot load/unload models on demand")

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass

    def status(self, ok: bool, version: str | None = None, error: str | None = None) -> BackendStatus:
        return BackendStatus(
            name=self.name, type=self.type, url=self.url, ok=ok, version=version, error=error,
            mock=self.is_mock, supports_pull=self.supports_pull,
            supports_load_unload=self.supports_load_unload, supports_embeddings=self.supports_embeddings,
        )
