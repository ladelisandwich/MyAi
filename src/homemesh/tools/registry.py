"""Tool registry: lookup, argument validation and per-tool policy."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from homemesh.tools.base import ToolDefinition
from homemesh.tools.builtin import BUILTIN_TOOLS


class ToolArgumentError(ValueError):
    pass


class ToolRegistry:
    def __init__(self, tools: list[ToolDefinition] | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for t in tools if tools is not None else BUILTIN_TOOLS:
            self.register(t)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    @staticmethod
    def validate_args(tool: ToolDefinition, arguments: dict[str, Any]) -> BaseModel:
        if not isinstance(arguments, dict):
            raise ToolArgumentError("tool arguments must be a JSON object")
        try:
            return tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            msgs = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()[:5])
            raise ToolArgumentError(f"invalid arguments for {tool.name}: {msgs}") from exc
