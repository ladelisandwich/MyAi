"""User/application settings stored in the coordinator database.

These follow the user across every client (unlike ``homemesh.toml``, which is per machine).
Each section is validated by a pydantic model; unknown keys are rejected.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from homemesh.common.errors import ValidationFailed
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.events import EventBus

MODEL_ROLES = ("chat", "reasoning", "coding", "fast", "embedding", "vision", "stt", "tts")


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantSettings(_S):
    name: str = Field(default="HomeMesh", max_length=60)
    custom_instructions: str = Field(default="", max_length=4000)
    response_style: str = Field(default="balanced", pattern="^(concise|balanced|detailed)$")


class UserProfileSettings(_S):
    preferred_name: str = Field(default="", max_length=60)
    about: str = Field(default="", max_length=2000)
    timezone: str = Field(default="", max_length=60)


class ModelRoleSettings(_S):
    chat: str | None = None
    reasoning: str | None = None
    coding: str | None = None
    fast: str | None = None
    embedding: str | None = None
    vision: str | None = None
    stt: str | None = None
    tts: str | None = None


class GenerationSettings(_S):
    temperature: float | None = Field(default=0.7, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_output_tokens: int = Field(default=1024, ge=16, le=32768)
    num_ctx: int | None = Field(default=None, ge=512, le=1_048_576)
    think: bool | None = None


class MemorySettings(_S):
    enabled: bool = True
    retrieval_limit: int = Field(default=6, ge=0, le=30)
    detect_explicit: bool = True
    auto_extract: bool = False
    default_retention_days: int | None = Field(default=None, ge=1)


class KnowledgeSettings(_S):
    enabled: bool = True
    retrieval_limit: int = Field(default=5, ge=0, le=20)
    min_score: float = Field(default=0.0, ge=0, le=1)


class ToolSettings(_S):
    enabled: bool = True
    max_rounds: int = Field(default=5, ge=1, le=10)
    approval_timeout_s: int = Field(default=300, ge=10, le=3600)


class RetentionSettings(_S):
    jobs_days: int = Field(default=30, ge=1)
    audit_days: int = Field(default=180, ge=7)
    archived_conversations_days: int | None = Field(default=None, ge=1)


SECTIONS: dict[str, type[_S]] = {
    "assistant": AssistantSettings,
    "user_profile": UserProfileSettings,
    "model_roles": ModelRoleSettings,
    "generation": GenerationSettings,
    "memory": MemorySettings,
    "knowledge": KnowledgeSettings,
    "tools": ToolSettings,
    "retention": RetentionSettings,
}


class SettingsService:
    def __init__(self, repos: Repositories, bus: EventBus) -> None:
        self.repos = repos
        self.bus = bus
        self._cache: dict[str, Any] | None = None

    async def all(self) -> dict[str, Any]:
        if self._cache is None:
            stored = await self.repos.settings.all()
            out = {}
            for name, model in SECTIONS.items():
                raw = stored.get(name) or {}
                try:
                    out[name] = model.model_validate(raw).model_dump()
                except ValidationError:
                    out[name] = model().model_dump()  # corrupt section: fall back to defaults
            self._cache = out
        return {k: dict(v) for k, v in self._cache.items()}

    async def section(self, name: str) -> dict[str, Any]:
        return (await self.all())[name]

    async def update(self, name: str, patch: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
        model = SECTIONS.get(name)
        if model is None:
            raise ValidationFailed(f"Unknown settings section '{name}'.")
        current = await self.section(name)
        merged = {**current, **patch}
        try:
            value = model.model_validate(merged).model_dump()
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            raise ValidationFailed(f"Invalid {name} settings: {msg}") from exc
        await self.repos.settings.set(name, value)
        self._cache = None
        await self.repos.audit.add("user", actor, "settings.updated", target_type="settings", target_id=name,
                                   details={"keys": sorted(patch)})
        self.bus.publish("settings.updated", {"section": name})
        return value
