"""Coordinator <-> worker WebSocket protocol (version 1).

Transport: the worker opens a WebSocket to ``/api/v1/worker/ws`` on the coordinator and
authenticates with the ``Authorization: HomeMesh-Node <node_id>:<secret>`` header. All
frames are UTF-8 JSON objects with a common envelope::

    {"v": 1, "type": "job.output", "id": "msg_...", "ts": "2026-...Z", ...type-specific fields}

``type`` selects the schema below (a pydantic discriminated union). Unknown types are
rejected with an ``error`` message rather than silently ignored. See docs/PROTOCOL.md.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.protocol.hardware import HardwareReport, Telemetry
from homemesh.protocol.jobs import GenerationMetrics
from homemesh.protocol.models import BackendStatus, ModelInfo
from homemesh.protocol.update import MAX_BUILD_BYTES, MAX_REPORT_BYTES, lenient_dict
from homemesh.version import PROTOCOL_VERSION

# WebSocket close codes (4000-4999 are application-defined).
CLOSE_UNAUTHORIZED = 4401
CLOSE_REVOKED = 4403
CLOSE_PROTOCOL_INCOMPATIBLE = 4406
CLOSE_SUPERSEDED = 4409
CLOSE_COORDINATOR_SHUTDOWN = 4503

AUTH_SCHEME = "HomeMesh-Node"
PROTOCOL_HEADER = "X-HomeMesh-Protocol"
MAX_FRAME_BYTES = 16 * 1024 * 1024


class _Msg(BaseModel):
    v: int = PROTOCOL_VERSION
    id: str = Field(default_factory=lambda: new_id("msg"))
    ts: str = Field(default_factory=now_iso)


# --------------------------------------------------------------------------- worker -> coordinator

class NodeLimits(BaseModel):
    max_concurrent_jobs: int = 1
    allow_cpu_inference: bool = True
    ram_reserve_mb: int = 2048
    vram_reserve_percent: float = 10
    gpu_busy_threshold_percent: float = 85


class Hello(_Msg):
    type: Literal["hello"] = "hello"
    node_id: str
    node_name: str
    software_version: str
    protocol_versions: list[int]
    hardware: HardwareReport
    backends: list[BackendStatus]
    models: list[ModelInfo]
    task_types: list[str]
    mode: str
    limits: NodeLimits
    allowed_roots: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    allow_remote_mode_change: bool = True
    running_jobs: list[str] = Field(default_factory=list)  # jobs still running from a previous connection
    embedded: bool = False  # runs inside the coordinator process (standalone mode)
    # Remote updater (additive; older coordinators ignore them). Plain dicts on purpose: the coordinator
    # parses them with protocol.update.BuildInfo / UpdateRunReport separately, so a bad value can never
    # invalidate the hello. None = an older worker, or a value that was not a dict or was too large.
    build: dict[str, Any] | None = None  # protocol.update.BuildInfo, <= 4 KB
    update: dict[str, Any] | None = None  # protocol.update.UpdateRunReport of the latest run, <= 8 KB

    @field_validator("build", mode="before")
    @classmethod
    def _lenient_build(cls, v: Any) -> dict[str, Any] | None:
        return lenient_dict(v, MAX_BUILD_BYTES)  # never raises

    @field_validator("update", mode="before")
    @classmethod
    def _lenient_update(cls, v: Any) -> dict[str, Any] | None:
        return lenient_dict(v, MAX_REPORT_BYTES)  # never raises


class Heartbeat(_Msg):
    type: Literal["heartbeat"] = "heartbeat"
    seq: int
    telemetry: Telemetry
    running_jobs: list[str] = Field(default_factory=list)
    mode: str
    update: dict[str, Any] | None = None  # latest protocol.update.UpdateRunReport, <= 8 KB (see Hello.update)

    @field_validator("update", mode="before")
    @classmethod
    def _lenient_update(cls, v: Any) -> dict[str, Any] | None:
        return lenient_dict(v, MAX_REPORT_BYTES)  # never raises


class CapabilitiesUpdate(_Msg):
    type: Literal["capabilities.update"] = "capabilities.update"
    backends: list[BackendStatus]
    models: list[ModelInfo]


class JobAccepted(_Msg):
    type: Literal["job.accepted"] = "job.accepted"
    job_id: str


class JobRejected(_Msg):
    type: Literal["job.rejected"] = "job.rejected"
    job_id: str
    code: str  # paused | busy | unsupported | invalid_payload | path_not_allowed | tool_not_allowed | capacity
    reason: str


class JobProgress(_Msg):
    type: Literal["job.progress"] = "job.progress"
    job_id: str
    progress: float | None = None  # 0..1 when known
    message: str | None = None
    data: dict[str, Any] | None = None


class JobOutput(_Msg):
    type: Literal["job.output"] = "job.output"
    job_id: str
    seq: int
    kind: Literal["content", "thinking", "tool_calls", "document"]
    delta: str | None = None
    data: dict[str, Any] | None = None


class JobCompleted(_Msg):
    type: Literal["job.completed"] = "job.completed"
    job_id: str
    result: dict[str, Any]
    metrics: GenerationMetrics | None = None


class JobFailed(_Msg):
    type: Literal["job.failed"] = "job.failed"
    job_id: str
    code: str
    error: str
    retryable: bool = False
    partial: dict[str, Any] | None = None
    hint: str | None = None  # what the user can do (update.* jobs send one; additive, older peers ignore it)


class JobCancelled(_Msg):
    type: Literal["job.cancelled"] = "job.cancelled"
    job_id: str
    partial: dict[str, Any] | None = None


class CredentialRotated(_Msg):
    type: Literal["credential.rotated"] = "credential.rotated"
    rotation_id: str


class WorkerShutdown(_Msg):
    type: Literal["shutdown"] = "shutdown"
    reason: str = ""


class Pong(_Msg):
    type: Literal["pong"] = "pong"
    ping_id: str


WorkerMessage = Annotated[
    Hello | Heartbeat | CapabilitiesUpdate | JobAccepted | JobRejected | JobProgress | JobOutput
    | JobCompleted | JobFailed | JobCancelled | CredentialRotated | WorkerShutdown | Pong,
    Field(discriminator="type"),
]

# --------------------------------------------------------------------------- coordinator -> worker


class Welcome(_Msg):
    type: Literal["welcome"] = "welcome"
    protocol_version: int
    node_id: str
    node_name: str
    coordinator_version: str
    heartbeat_interval_s: float
    mode: str  # mode the coordinator has stored for this node (may override worker default)
    server_time: str = Field(default_factory=now_iso)
    trusted_roots: list[str] | None = None  # embedded worker only: collection roots it may scan


class JobDispatch(_Msg):
    type: Literal["job.dispatch"] = "job.dispatch"
    job_id: str
    job_type: str
    priority: str
    payload: dict[str, Any]
    timeout_s: float


class JobCancel(_Msg):
    type: Literal["job.cancel"] = "job.cancel"
    job_id: str
    reason: str = "cancelled by user"


class CredentialRotate(_Msg):
    type: Literal["credential.rotate"] = "credential.rotate"
    rotation_id: str
    new_secret: str


class NodeConfig(_Msg):
    type: Literal["node.config"] = "node.config"
    mode: str | None = None
    trusted_roots: list[str] | None = None


class ModelsRefresh(_Msg):
    type: Literal["models.refresh"] = "models.refresh"


class Ping(_Msg):
    type: Literal["ping"] = "ping"


class CoordinatorShutdown(_Msg):
    type: Literal["shutdown"] = "shutdown"
    reason: str = ""
    reconnect_after_s: float = 2.0


class ErrorMessage(_Msg):
    type: Literal["error"] = "error"
    code: str
    message: str
    fatal: bool = False


CoordinatorMessage = Annotated[
    Welcome | JobDispatch | JobCancel | CredentialRotate | NodeConfig | ModelsRefresh | Ping
    | CoordinatorShutdown | ErrorMessage,
    Field(discriminator="type"),
]

_worker_adapter: TypeAdapter[Any] = TypeAdapter(WorkerMessage)
_coordinator_adapter: TypeAdapter[Any] = TypeAdapter(CoordinatorMessage)


def parse_worker_message(raw: str | bytes) -> Any:
    return _worker_adapter.validate_json(raw)


def parse_coordinator_message(raw: str | bytes) -> Any:
    return _coordinator_adapter.validate_json(raw)


def dump(msg: BaseModel) -> str:
    return msg.model_dump_json(exclude_none=False)


def negotiate_version(offered: list[int], supported: tuple[int, ...]) -> int | None:
    common = set(offered) & set(supported)
    return max(common) if common else None
