"""macOS GPU discovery via ``system_profiler`` (architecture placeholder; untested on real Macs).

Apple Silicon uses unified memory: the GPU shares system RAM, so no separate VRAM size is
reported. Metal is the compute API used by Ollama/llama.cpp on macOS.
"""

from __future__ import annotations

import json
import subprocess

from homemesh.common.proc import run_quiet
from homemesh.protocol.hardware import GPUInfo


def parse_system_profiler(text: str) -> list[GPUInfo]:
    data = json.loads(text)
    gpus = []
    for idx, d in enumerate(data.get("SPDisplaysDataType", [])):
        name = d.get("sppci_model") or d.get("_name") or "GPU"
        vendor_raw = (d.get("spdisplays_vendor") or "").lower()
        vendor = "apple" if "apple" in vendor_raw or name.startswith("Apple") else (
            "amd" if "amd" in vendor_raw or "ati" in vendor_raw else
            "intel" if "intel" in vendor_raw else "nvidia" if "nvidia" in vendor_raw else "other")
        unavailable = {}
        vram_total = None
        vram = d.get("spdisplays_vram") or d.get("spdisplays_vram_shared")
        if vendor == "apple":
            unavailable["vram_total_mb"] = "Apple Silicon uses unified memory shared with the CPU"
        elif isinstance(vram, str) and vram.split()[0].isdigit():
            amount = int(vram.split()[0])
            vram_total = amount * 1024 if "GB" in vram else amount
        gpus.append(GPUInfo(index=idx, vendor=vendor, name=name, vram_total_mb=vram_total,
                            integrated=vendor in ("apple", "intel"), compute_apis=["metal"],
                            source="system_profiler", unavailable=unavailable))
    return gpus


def detect_macos_gpus() -> tuple[list[GPUInfo], list[str]]:
    try:
        proc = run_quiet(["system_profiler", "SPDisplaysDataType", "-json"], capture_output=True,
                              text=True, timeout=30, check=False)
        return parse_system_profiler(proc.stdout), []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [], [f"macOS GPU query failed: {exc}"]
