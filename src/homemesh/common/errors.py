"""Error types that carry a stable code and an actionable hint for users."""

from __future__ import annotations


class HomeMeshError(Exception):
    """Base error. ``code`` is stable and machine-readable; ``hint`` tells the user what to do."""

    code = "error"
    status_code = 500

    def __init__(self, message: str, *, code: str | None = None, hint: str | None = None,
                 status_code: int | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        if self.details:
            d["details"] = self.details
        return d


class ConfigError(HomeMeshError):
    code = "config_error"
    status_code = 500


class NotFoundError(HomeMeshError):
    code = "not_found"
    status_code = 404


class ValidationFailed(HomeMeshError):
    code = "validation_failed"
    status_code = 422


class AuthError(HomeMeshError):
    code = "unauthorized"
    status_code = 401


class PermissionDenied(HomeMeshError):
    code = "permission_denied"
    status_code = 403


class ConflictError(HomeMeshError):
    code = "conflict"
    status_code = 409


class RateLimited(HomeMeshError):
    code = "rate_limited"
    status_code = 429


class SchedulingError(HomeMeshError):
    """No eligible worker could take a job. ``details`` carries the scheduler's reasons."""

    code = "no_eligible_worker"
    status_code = 503


class BackendError(HomeMeshError):
    """An inference backend (Ollama, llama.cpp, ...) failed or is unreachable."""

    code = "backend_error"
    status_code = 502


class PathNotAllowed(PermissionDenied):
    code = "path_not_allowed"
