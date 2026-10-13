"""Machine-level configuration (``homemesh.toml``).

Two kinds of settings exist in HomeMesh:

* **Machine/deployment config** (this module): which roles this computer runs, ports, TLS,
  inference backends on this machine, worker resource limits. Stored in ``homemesh.toml``
  next to the data directory and validated with pydantic.
* **User/application settings** (model roles, assistant preferences, memory behaviour,
  tool policies): stored in the coordinator database so that they follow the user across
  devices. See ``homemesh.coordinator.settings_service``.

Environment overrides use ``HOMEMESH__SECTION__KEY=value`` (double underscores), e.g.
``HOMEMESH__SERVER__PORT=9000``. Values are parsed as JSON when possible, so lists can be
given as ``'["a","b"]'``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from homemesh.common.errors import ConfigError
from homemesh.common.paths import AppPaths, default_paths
from homemesh.common.toml_writer import dumps as toml_dumps

Role = Literal["coordinator", "worker", "client"]
Mode = Literal["performance", "balanced", "background", "paused"]
ALL_TASK_TYPES = ["chat", "embed", "knowledge.scan", "tool.exec", "model.manage"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class NodeSection(_Section):
    name: str = Field(default_factory=lambda: socket.gethostname() or "homemesh-node")
    roles: list[Role] = Field(default_factory=lambda: ["coordinator", "worker"])

    @field_validator("roles")
    @classmethod
    def _roles_nonempty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one role is required (coordinator, worker or client)")
        return sorted(set(v), key=["coordinator", "worker", "client"].index)


class ServerSection(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    public_url: str = ""
    tls_mode: Literal["off", "self_signed", "custom", "proxy"] = "off"
    tls_cert_file: str = ""
    tls_key_file: str = ""
    tls_extra_sans: list[str] = Field(default_factory=list)
    allow_insecure_lan: bool = False
    max_request_mb: float = Field(default=10, gt=0)
    max_upload_mb: float = Field(default=25, gt=0)
    session_ttl_hours: int = Field(default=24 * 7, ge=1)
    trusted_proxies: list[str] = Field(default_factory=list)
    discovery_enabled: bool = False
    discovery_port: int = Field(default=47781, ge=1, le=65535)
    queue_timeout_s: float = Field(default=30, gt=0)
    heartbeat_timeout_s: float = Field(default=35, gt=0)
    dispatch_ack_timeout_s: float = Field(default=15, gt=0)
    web_dist_dir: str = ""
    login_attempts_per_minute: int = Field(default=10, ge=1)
    max_queued_jobs: int = Field(default=5000, ge=10)


class WorkerSection(_Section):
    coordinator_url: str = "http://127.0.0.1:8765"
    mode: Mode = "balanced"
    max_concurrent_jobs: int = Field(default=2, ge=1, le=64)
    allow_cpu_inference: bool = True
    ram_reserve_mb: int = Field(default=2048, ge=0)
    vram_reserve_percent: float = Field(default=10, ge=0, le=90)
    gpu_busy_threshold_percent: float = Field(default=85, ge=1, le=100)
    task_types: list[str] = Field(default_factory=lambda: list(ALL_TASK_TYPES))
    allowed_roots: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=lambda: ["system_info", "list_directory", "read_file"])
    # GPUs HomeMesh should not count when scheduling, e.g. a weak secondary card that the model
    # server does not use. Case-insensitive parts of the GPU name, e.g. ["GTX 745"].
    ignore_gpus: list[str] = Field(default_factory=list)
    heartbeat_interval_s: float = Field(default=10, ge=1, le=120)
    allow_remote_mode_change: bool = True
    tls_ca_file: str = ""
    reconnect_max_delay_s: float = Field(default=60, ge=1)
    reconnect_max_attempts: int = Field(default=0, ge=0, description="0 = keep retrying forever (daemon)")

    @field_validator("task_types")
    @classmethod
    def _known_tasks(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in ALL_TASK_TYPES]
        if unknown:
            raise ValueError(f"unknown task types {unknown}; known: {ALL_TASK_TYPES}")
        return v


class BackendConfig(_Section):
    name: str
    type: Literal["ollama", "openai_compat", "mock"]
    url: str = ""
    enabled: bool = True
    api_key_env: str = ""
    # OpenAI-compatible servers cannot report capabilities; declare them per model, e.g.
    # declared_models = { "qwen2.5-7b-instruct" = ["completion", "tools"] }
    declared_models: dict[str, list[str]] = Field(default_factory=dict)
    # Approximate memory need per declared model in MB (optional, improves scheduling).
    declared_memory_mb: dict[str, int] = Field(default_factory=dict)
    timeout_s: float = Field(default=600, gt=0)
    # Ollama can proxy "cloud" models (remote_host). Those leave your network, so they are
    # hidden unless explicitly allowed here AND cloud use is enabled in [cloud].
    allow_remote_models: bool = False

    @model_validator(mode="after")
    def _url_required(self) -> BackendConfig:
        if self.type != "mock" and not self.url:
            raise ValueError(f"backend '{self.name}' of type {self.type} needs a url")
        return self


def _default_backends() -> list[BackendConfig]:
    return [BackendConfig(name="ollama", type="ollama", url="http://127.0.0.1:11434")]


class InferenceSection(_Section):
    backends: list[BackendConfig] = Field(default_factory=_default_backends)
    mock_enabled: bool = False
    allow_model_downloads: bool = True
    default_num_ctx: int = Field(default=8192, ge=512)
    max_output_tokens: int = Field(default=1024, ge=16)
    request_timeout_s: float = Field(default=600, gt=0)
    keep_alive: str = "10m"

    @field_validator("backends")
    @classmethod
    def _unique_names(cls, v: list[BackendConfig]) -> list[BackendConfig]:
        names = [b.name for b in v]
        if len(names) != len(set(names)):
            raise ValueError("backend names must be unique")
        return v


class KnowledgeSection(_Section):
    max_file_mb: float = Field(default=50, gt=0)
    chunk_chars: int = Field(default=2000, ge=200)
    chunk_overlap: int = Field(default=200, ge=0)
    embed_batch_size: int = Field(default=32, ge=1, le=512)
    exclude_dirs: list[str] = Field(default_factory=lambda: [
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".cache",
        ".tox", "dist", "build", ".idea", ".vscode", "target",
    ])


class LoggingSection(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["text", "json"] = "text"
    to_file: bool = True


class WebSection(_Section):
    """Settings for the internet tools (web_search, fetch_url, open_url).

    These tools only run on a computer that lists them in ``worker.allowed_tools``; nothing here
    turns them on by itself.
    """

    # Your own SearXNG instance, e.g. "http://127.0.0.1:8888" (see docs/WEB_ACCESS.md). Empty means
    # web_search is not set up on this computer.
    searxng_url: str = ""
    # "0" off, "1" moderate, "2" strict (SearXNG safe search levels).
    safesearch: Literal[0, 1, 2] = 1
    search_language: str = "auto"
    max_results: int = Field(default=8, ge=1, le=20)
    timeout_s: float = Field(default=20, gt=0, le=120)
    max_download_mb: float = Field(default=5, gt=0, le=50)
    max_redirects: int = Field(default=5, ge=0, le=10)

    @field_validator("searxng_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not v.lower().startswith(("http://", "https://")):
            raise ValueError("web.searxng_url must start with http:// or https://")
        return v


class CloudSection(_Section):
    """Optional cloud model integrations. Disabled by default; nothing in HomeMesh contacts
    a cloud AI service unless this is enabled AND a backend is configured for it."""

    enabled: bool = False


class HomeMeshConfig(_Section):
    node: NodeSection = Field(default_factory=NodeSection)
    server: ServerSection = Field(default_factory=ServerSection)
    worker: WorkerSection = Field(default_factory=WorkerSection)
    inference: InferenceSection = Field(default_factory=InferenceSection)
    knowledge: KnowledgeSection = Field(default_factory=KnowledgeSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    web: WebSection = Field(default_factory=WebSection)
    cloud: CloudSection = Field(default_factory=CloudSection)

    # ---- derived helpers -------------------------------------------------
    def has_role(self, role: str) -> bool:
        return role in self.node.roles

    @property
    def tls_enabled(self) -> bool:
        return self.server.tls_mode in ("self_signed", "custom")

    def bind_is_loopback(self) -> bool:
        return is_loopback_host(self.server.host)

    def local_base_url(self) -> str:
        """URL that processes on *this* machine use to reach the coordinator."""
        scheme = "https" if self.tls_enabled else "http"
        host = self.server.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{scheme}://{host}:{self.server.port}"

    def advertised_url(self) -> str:
        """URL other computers should use to reach this coordinator."""
        if self.server.public_url:
            return self.server.public_url.rstrip("/")
        scheme = "https" if self.tls_enabled else "http"
        host = self.server.host
        if host in ("0.0.0.0", "::", ""):
            host = guess_lan_ip() or "127.0.0.1"
        return f"{scheme}://{host}:{self.server.port}"

    def security_problems(self) -> list[str]:
        """Configuration combinations that would be unsafe. The coordinator refuses to start on these."""
        problems: list[str] = []
        if self.has_role("coordinator") and not self.bind_is_loopback():
            if self.server.tls_mode == "off" and not self.server.allow_insecure_lan:
                problems.append(
                    f"server.host={self.server.host!r} exposes HomeMesh to the network without TLS. "
                    "Run 'homemesh tls init' (sets tls_mode=self_signed), use tls_mode='proxy' behind a "
                    "trusted HTTPS reverse proxy bound to localhost, or explicitly set "
                    "server.allow_insecure_lan=true if you accept unencrypted LAN traffic."
                )
        if self.server.tls_mode == "custom" and not (self.server.tls_cert_file and self.server.tls_key_file):
            problems.append("server.tls_mode='custom' requires server.tls_cert_file and server.tls_key_file")
        if self.server.tls_mode == "proxy" and not self.bind_is_loopback():
            problems.append(
                "server.tls_mode='proxy' means TLS is terminated by a reverse proxy; bind server.host to "
                "127.0.0.1 so the plain-HTTP port is not reachable from the network."
            )
        return problems


def is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def guess_lan_ip() -> str | None:
    """Best-effort LAN IP of this machine. Uses a UDP 'connect' which sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.255.255", 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Loading / saving
# ---------------------------------------------------------------------------

ENV_PREFIX = "HOMEMESH__"


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX):].split("__") if p]
        if len(parts) < 2:
            continue
        try:
            value: Any = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            value = raw
        cursor = data
        for p in parts[:-1]:
            cursor = cursor.setdefault(p, {})
            if not isinstance(cursor, dict):
                raise ConfigError(f"Environment override {key} conflicts with a non-table value")
        cursor[parts[-1]] = value
    # Plain-text conveniences for containers/systemd, where JSON in environment values is awkward.
    if environ.get("HOMEMESH_MOCK", "").lower() in ("1", "true", "yes"):
        data.setdefault("inference", {})["mock_enabled"] = True
    if environ.get("HOMEMESH_OLLAMA_URL"):
        data.setdefault("inference", {})["backends"] = [
            {"name": "ollama", "type": "ollama", "url": environ["HOMEMESH_OLLAMA_URL"].strip()}]
    if environ.get("HOMEMESH_SEARXNG_URL"):
        data.setdefault("web", {})["searxng_url"] = environ["HOMEMESH_SEARXNG_URL"].strip()
    if environ.get("HOMEMESH_TLS_SANS"):
        data.setdefault("server", {})["tls_extra_sans"] = [
            s.strip() for s in environ["HOMEMESH_TLS_SANS"].split(",") if s.strip()]
    return data


def load_config(paths: AppPaths | None = None, *, environ: dict[str, str] | None = None,
                config_file: Path | None = None) -> HomeMeshConfig:
    paths = paths or default_paths()
    environ = dict(os.environ if environ is None else environ)
    path = config_file or paths.config_file
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"Configuration file {path} is not valid TOML: {exc}",
                hint="Fix the syntax error, or move the file away and run 'homemesh setup' to recreate it.",
            ) from exc
    data = _apply_env_overrides(data, environ)
    try:
        return HomeMeshConfig.model_validate(data)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError(
            f"Configuration in {path} is invalid:\n" + "\n".join(lines),
            hint="Run 'homemesh config validate' after editing, or see CONFIGURATION.md for valid settings.",
        ) from exc


def config_to_dict(cfg: HomeMeshConfig) -> dict[str, Any]:
    return cfg.model_dump(mode="json")


def save_config(cfg: HomeMeshConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# HomeMesh AI machine configuration.\n"
        "# See CONFIGURATION.md for every option. User preferences (model roles, memory\n"
        "# behaviour, assistant persona) live in the coordinator database, not here.\n\n"
    )
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(header + toml_dumps(config_to_dict(cfg)), encoding="utf-8")
    os.replace(tmp, path)
