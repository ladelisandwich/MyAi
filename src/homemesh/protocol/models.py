"""Model and inference-backend descriptions advertised by workers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Capability vocabulary. Matches Ollama's /api/show "capabilities" values where they overlap.
CAP_COMPLETION = "completion"  # text generation / chat
CAP_TOOLS = "tools"  # native tool/function calling
CAP_VISION = "vision"  # image inputs
CAP_EMBEDDING = "embedding"  # produces embeddings
CAP_THINKING = "thinking"  # separate reasoning stream
CAP_INSERT = "insert"  # fill-in-the-middle
KNOWN_CAPABILITIES = {CAP_COMPLETION, CAP_TOOLS, CAP_VISION, CAP_EMBEDDING, CAP_THINKING, CAP_INSERT}

CapabilitySource = Literal["backend", "inferred", "declared", "mock"]


class ModelInfo(BaseModel):
    """A model available through one backend on one node."""

    name: str
    backend: str  # backend instance name from config, e.g. "ollama"
    backend_type: str  # ollama | openai_compat | mock
    digest: str | None = None
    size_bytes: int | None = None  # size on disk
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    format: str | None = None
    context_length: int | None = None  # maximum context supported by the model
    embedding_length: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    capability_source: CapabilitySource = "backend"
    # Ollama "cloud" models (remote_host set) run on a remote service, not on this machine.
    remote: bool = False
    remote_host: str | None = None
    loaded: bool = False
    loaded_size_bytes: int | None = None  # actual resident size when loaded (Ollama /api/ps)
    loaded_vram_bytes: int | None = None
    loaded_context_length: int | None = None
    estimated_memory_bytes: int | None = None  # conservative estimate used by the scheduler
    mock: bool = False
    warnings: list[str] = Field(default_factory=list)

    def has(self, cap: str) -> bool:
        return cap in self.capabilities


class BackendStatus(BaseModel):
    name: str
    type: str
    url: str = ""
    ok: bool
    version: str | None = None
    error: str | None = None
    mock: bool = False
    supports_pull: bool = False
    supports_load_unload: bool = False
    supports_embeddings: bool = False
