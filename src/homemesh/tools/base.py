"""Tool framework primitives.

Every tool declares what it does and what it may touch. The registry and the coordinator's
tool service enforce these declarations; the model can only *request* a tool call - it can
never grant itself permissions, and text inside documents, memories or tool output is
never interpreted as a permission change.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from homemesh.protocol.jobs import ToolSpec

SideEffect = Literal["none", "read", "write", "destructive"]
Location = Literal["coordinator", "node"]


@dataclass
class ToolContext:
    user_id: str | None = None
    conversation_id: str | None = None
    node_id: str | None = None
    # Coordinator-side services (memory, knowledge) for coordinator tools.
    services: Any = None
    # Node-side: authorised folders and the workspace where files may be created.
    path_policy: Any = None
    workspace_dir: Path | None = None
    # Node-side: the computer's [web] settings (WebSection) for the internet tools.
    web: Any = None


Handler = Callable[[ToolContext, Any], Awaitable[Any]]


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_description: str
    handler: Handler
    permissions: list[str] = field(default_factory=list)
    os: list[str] = field(default_factory=lambda: ["any"])
    location: Location = "coordinator"
    timeout_s: float = 30.0
    side_effect: SideEffect = "none"
    audit: bool = True
    # Name of an argument holding a web address the tool contacts. A call whose address did not
    # come from the user or an earlier web result in the same conversation needs the user's
    # approval, so a model cannot quietly send data out by putting it into a URL.
    url_argument: str | None = None
    # Optional extra line for the approval card, computed from the validated arguments.
    approval_note: Callable[[Any], str | None] | None = None

    @property
    def requires_approval(self) -> bool:
        """Write and destructive tools always need explicit approval from the user."""
        return self.side_effect in ("write", "destructive")

    def supported_here(self) -> bool:
        if "any" in self.os:
            return True
        plat = "windows" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
        return plat in self.os

    def spec(self) -> ToolSpec:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, parameters=schema)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description, "input_schema": self.input_model.model_json_schema(),
            "output": self.output_description, "permissions": self.permissions, "os": self.os,
            "location": self.location, "timeout_s": self.timeout_s, "side_effect": self.side_effect,
            "audit": self.audit, "requires_approval": self.requires_approval,
            "url_argument": self.url_argument,
        }
