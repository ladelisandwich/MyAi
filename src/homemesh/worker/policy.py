"""Worker-side enforcement of local resource and security policy.

The coordinator already schedules according to these rules, but a worker re-checks every
job it receives: the owner of *this* computer decides what HomeMesh may do on it, and a
compromised or buggy coordinator must not be able to override that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homemesh.common.config import WorkerSection
from homemesh.knowledge.pathsafety import PathPolicy
from homemesh.scheduler.profiles import effective_concurrency, get_profile


@dataclass
class Verdict:
    ok: bool
    code: str = ""
    reason: str = ""


@dataclass
class LocalPolicy:
    cfg: WorkerSection
    embedded: bool = False
    mode: str = "balanced"
    trusted_roots: list[str] = field(default_factory=list)  # embedded worker: coordinator collection roots

    def __post_init__(self) -> None:
        self.mode = self.cfg.mode

    def allowed_roots(self) -> list[str]:
        roots = list(self.cfg.allowed_roots)
        if self.embedded:
            roots += [r for r in self.trusted_roots if r not in roots]
        return roots

    def path_policy(self) -> PathPolicy:
        return PathPolicy([Path(r) for r in self.allowed_roots()])

    def check(self, job_type: str, priority: str, payload: dict[str, Any], running: int,
              external_gpu_busy: bool) -> Verdict:
        profile = get_profile(self.mode)
        if self.mode == "paused":
            return Verdict(False, "paused", "this worker is paused")
        if job_type not in self.cfg.task_types:
            return Verdict(False, "unsupported", f"this worker does not accept '{job_type}' jobs")
        if priority not in profile.accepts and priority not in profile.last_resort:
            return Verdict(False, "mode", f"{self.mode} mode does not accept {priority} jobs")
        cap = effective_concurrency(self.mode, self.cfg.max_concurrent_jobs)
        if running >= cap:
            return Verdict(False, "capacity", f"already running {running}/{cap} jobs")
        if job_type in ("chat", "embed") and external_gpu_busy and profile.respect_external_gpu_busy:
            return Verdict(False, "busy", "GPU is in use by another application")
        if job_type == "knowledge.scan":
            root = payload.get("root_path", "")
            try:
                self.path_policy().check_root(Path(root))
            except PermissionError as exc:
                return Verdict(False, "path_not_allowed", str(exc))
        if job_type == "tool.exec":
            tool = payload.get("tool", "")
            if tool not in self.cfg.allowed_tools:
                return Verdict(False, "tool_not_allowed",
                               f"tool '{tool}' is not in worker.allowed_tools on this computer")
        return Verdict(True)
