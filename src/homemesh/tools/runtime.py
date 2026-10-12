"""Execution of node-located tools on a worker.

The worker re-validates everything: the tool must be in this computer's
``worker.allowed_tools``, arguments must match the schema, file access is confined by the
local path policy, and write tools must carry an approval id issued by the coordinator after
the user approved the call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from homemesh.protocol.jobs import ToolExecPayload, ToolExecResult
from homemesh.tools.base import ToolContext
from homemesh.tools.registry import ToolArgumentError, ToolRegistry


def make_worker_tool_runner(policy: Any, workspace_dir: Path | None = None
                            ) -> Callable[[ToolExecPayload], Awaitable[ToolExecResult]]:
    registry = ToolRegistry()

    async def run(payload: ToolExecPayload) -> ToolExecResult:
        tool = registry.get(payload.tool)
        if tool is None or tool.location != "node":
            return ToolExecResult(ok=False, error=f"unknown node tool '{payload.tool}'")
        if payload.tool not in policy.cfg.allowed_tools:
            return ToolExecResult(ok=False, error=f"tool '{payload.tool}' is not allowed on this computer")
        if not tool.supported_here():
            return ToolExecResult(ok=False, error=f"tool '{payload.tool}' is not supported on this operating system")
        if tool.requires_approval and not payload.approval_id:
            return ToolExecResult(ok=False, error="this tool changes files and requires a user approval")
        try:
            args = registry.validate_args(tool, payload.arguments)
        except ToolArgumentError as exc:
            return ToolExecResult(ok=False, error=str(exc))
        ctx = ToolContext(path_policy=policy.path_policy(), workspace_dir=workspace_dir)
        try:
            output = await asyncio.wait_for(tool.handler(ctx, args), timeout=tool.timeout_s)
            return ToolExecResult(ok=True, output=output)
        except TimeoutError:
            return ToolExecResult(ok=False, error=f"tool timed out after {tool.timeout_s:.0f}s")
        except (PermissionError, FileNotFoundError, FileExistsError, NotADirectoryError, ValueError, OSError) as exc:
            return ToolExecResult(ok=False, error=str(exc))

    return run
