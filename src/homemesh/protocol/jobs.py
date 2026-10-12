"""Job payload and result schemas for every task type a worker can execute.

The coordinator validates payloads before dispatch and workers validate them again on
receipt, so a malformed or malicious job is rejected on both sides.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

JobType = Literal["chat", "embed", "knowledge.scan", "tool.exec", "model.manage"]
JobStatus = Literal["queued", "dispatched", "running", "completed", "failed", "cancelled", "interrupted"]
JobPriority = Literal["interactive", "normal", "background"]

TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled", "interrupted"}

# Idempotent job types may be retried automatically after a worker disconnect or a
# coordinator restart. Chat is NOT idempotent (it would duplicate a visible answer and may
# trigger tool calls), so it is never retried automatically.
IDEMPOTENT_JOB_TYPES = {"embed", "knowledge.scan"}


# --------------------------------------------------------------------------- chat

class ToolCall(BaseModel):
    id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    images: list[str] | None = None  # base64-encoded images (only for vision-capable models)
    tool_calls: list[ToolCall] | None = None
    tool_name: str | None = None  # for role == "tool": which tool produced this result


class ToolSpec(BaseModel):
    """Function definition passed to a tool-capable model (OpenAI/Ollama 'function' format)."""

    name: str
    description: str
    parameters: dict[str, Any]


class GenerationOptions(BaseModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0)
    num_ctx: int | None = Field(default=None, ge=256)
    num_predict: int | None = Field(default=None, ge=1)
    seed: int | None = None
    stop: list[str] | None = None
    repeat_penalty: float | None = Field(default=None, ge=0)


class GenerationMetrics(BaseModel):
    """Timing/throughput numbers. ``None`` means the backend did not report the value."""

    ttft_ms: float | None = None  # measured by the worker: request sent -> first token
    total_ms: float | None = None  # measured by the worker
    load_ms: float | None = None  # backend-reported model load time
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prompt_eval_ms: float | None = None
    eval_ms: float | None = None
    tokens_per_second: float | None = None
    dispatch_overhead_ms: float | None = None  # coordinator-measured dispatch -> ack


class ChatJobPayload(BaseModel):
    model: str
    backend: str
    messages: list[ChatMessage]
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    tools: list[ToolSpec] | None = None
    think: bool | None = None
    keep_alive: str | None = None


class ChatJobResult(BaseModel):
    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    model: str
    backend: str
    backend_type: str
    mock: bool = False
    metrics: GenerationMetrics = Field(default_factory=GenerationMetrics)


# --------------------------------------------------------------------------- embeddings

class EmbedJobPayload(BaseModel):
    model: str
    backend: str
    inputs: list[str] = Field(min_length=1, max_length=512)
    # Opaque references echoed back so the coordinator can store vectors without lookups.
    refs: list[str] | None = None


class EmbedJobResult(BaseModel):
    model: str
    backend: str
    dim: int
    embeddings: list[list[float]]
    refs: list[str] | None = None
    mock: bool = False
    metrics: GenerationMetrics = Field(default_factory=GenerationMetrics)


# --------------------------------------------------------------------------- knowledge

class KnownDocument(BaseModel):
    size: int
    mtime: float
    content_hash: str


class KnowledgeScanPayload(BaseModel):
    collection_id: str
    root_path: str
    include_globs: list[str] = Field(default_factory=list)  # empty = all supported types
    exclude_dirs: list[str] = Field(default_factory=list)
    known: dict[str, KnownDocument] = Field(default_factory=dict)  # rel_path -> last indexed state
    chunk_chars: int = 2000
    chunk_overlap: int = 200
    max_file_mb: float = 50


class ChunkRecord(BaseModel):
    ordinal: int
    content: str
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None


DocumentStatus = Literal["indexed", "unchanged", "needs_ocr", "unsupported", "error", "too_large", "empty"]


class DocumentRecord(BaseModel):
    rel_path: str
    size: int
    mtime: float
    content_hash: str | None = None
    mime: str | None = None
    status: DocumentStatus
    error: str | None = None
    page_count: int | None = None
    chunks: list[ChunkRecord] = Field(default_factory=list)
    # When a document has many chunks it is sent in several parts; part 0 replaces any
    # existing chunks, later parts append.
    part: int = 0
    final_part: bool = True


class KnowledgeScanResult(BaseModel):
    collection_id: str
    seen: list[str]
    stats: dict[str, int] = Field(default_factory=dict)
    duration_ms: float | None = None


# --------------------------------------------------------------------------- tools

class ToolExecPayload(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None


class ToolExecResult(BaseModel):
    ok: bool
    output: Any = None
    error: str | None = None


# --------------------------------------------------------------------------- model management

class ModelManagePayload(BaseModel):
    action: Literal["pull", "delete", "load", "unload", "refresh"]
    backend: str
    model: str | None = None
    keep_alive: str | None = None


class ModelManageResult(BaseModel):
    action: str
    model: str | None = None
    ok: bool
    message: str | None = None
    duration_ms: float | None = None


PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "chat": ChatJobPayload,
    "embed": EmbedJobPayload,
    "knowledge.scan": KnowledgeScanPayload,
    "tool.exec": ToolExecPayload,
    "model.manage": ModelManagePayload,
}

RESULT_SCHEMAS: dict[str, type[BaseModel]] = {
    "chat": ChatJobResult,
    "embed": EmbedJobResult,
    "knowledge.scan": KnowledgeScanResult,
    "tool.exec": ToolExecResult,
    "model.manage": ModelManageResult,
}


def validate_payload(job_type: str, payload: dict[str, Any]) -> BaseModel:
    schema = PAYLOAD_SCHEMAS.get(job_type)
    if schema is None:
        raise ValueError(f"unknown job type: {job_type}")
    return schema.model_validate(payload)
