"""System inspection tool (read-only resource usage of a node)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel

from homemesh.hardware.gpu_nvidia import sample_nvidia
from homemesh.tools.base import ToolContext, ToolDefinition


class SystemInfoInput(BaseModel):
    pass


def _collect() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(Path.home().anchor or "/")
    gpus = [g.model_dump(exclude_none=True) for g in sample_nvidia()]
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "logical_cpus": psutil.cpu_count(),
        "ram_total_mb": vm.total // 2**20, "ram_available_mb": vm.available // 2**20, "ram_used_percent": vm.percent,
        "disk_total_gb": round(du.total / 2**30, 1), "disk_free_gb": round(du.free / 2**30, 1),
        "nvidia_gpus": gpus or "none reported",
        "uptime_s": int(time.time() - psutil.boot_time()),
    }


async def _system_info(ctx: ToolContext, args: SystemInfoInput) -> dict[str, Any]:
    return await asyncio.to_thread(_collect)


SYSTEM_INFO = ToolDefinition(
    name="system_info", description="Report current CPU, memory, disk and NVIDIA GPU usage of a HomeMesh computer.",
    input_model=SystemInfoInput, output_description="resource usage numbers", handler=_system_info,
    location="node", side_effect="read", permissions=["system.inspect"], timeout_s=15,
)
