"""NVIDIA GPU detection via ``nvidia-smi`` (ships with the driver on Linux and Windows).

nvidia-smi is preferred over NVML bindings: it needs no extra Python dependency, works
the same on Linux, Bazzite (nvidia images) and Windows, and reports real VRAM numbers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from homemesh.common.proc import run_quiet
from homemesh.protocol.hardware import GPUInfo, GPUTelemetry

QUERY_FIELDS = [
    "index", "name", "uuid", "driver_version", "memory.total", "memory.used", "memory.free",
    "utilization.gpu", "temperature.gpu", "pci.bus_id", "compute_cap",
]
TELEMETRY_FIELDS = ["index", "utilization.gpu", "memory.used", "memory.free", "memory.total", "temperature.gpu"]


def find_nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        for candidate in (
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        ):
            if candidate.exists():
                return str(candidate)
    return None


def _num(value: str) -> float | None:
    v = value.strip()
    if not v or v.startswith("[") or v.upper() in ("N/A", "NOT SUPPORTED"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_query_output(text: str, fields: list[str]) -> list[dict[str, str]]:
    rows = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(fields):
            # GPU names never contain commas in practice; skip malformed lines rather than guess.
            continue
        rows.append(dict(zip(fields, parts, strict=True)))
    return rows


def rows_to_gpus(rows: list[dict[str, str]]) -> list[GPUInfo]:
    gpus = []
    for r in rows:
        unavailable: dict[str, str] = {}
        total = _num(r.get("memory.total", ""))
        used = _num(r.get("memory.used", ""))
        free = _num(r.get("memory.free", ""))
        util = _num(r.get("utilization.gpu", ""))
        temp = _num(r.get("temperature.gpu", ""))
        for key, val in (("vram_total_mb", total), ("vram_used_mb", used), ("utilization_percent", util),
                         ("temperature_c", temp)):
            if val is None:
                unavailable[key] = "not reported by nvidia-smi"
        cc = r.get("compute_cap", "").strip()
        gpus.append(GPUInfo(
            index=int(_num(r.get("index", "0")) or 0),
            vendor="nvidia",
            name=r.get("name", "NVIDIA GPU"),
            uuid=r.get("uuid") or None,
            driver_version=r.get("driver_version") or None,
            vram_total_mb=int(total) if total is not None else None,
            vram_used_mb=int(used) if used is not None else None,
            vram_free_mb=int(free) if free is not None else None,
            utilization_percent=util,
            temperature_c=temp,
            integrated=False,
            compute_apis=["cuda"],
            compute_capability=cc if cc and not cc.startswith("[") else None,
            pci_bus_id=r.get("pci.bus_id") or None,
            source="nvidia-smi",
            unavailable=unavailable,
        ))
    return gpus


def _run(binary: str, fields: list[str]) -> subprocess.CompletedProcess[str]:
    return run_quiet(
        [binary, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15, check=False,
    )


def detect_nvidia() -> tuple[list[GPUInfo], list[str]]:
    """Return (gpus, warnings)."""
    binary = find_nvidia_smi()
    if not binary:
        return [], []
    warnings: list[str] = []
    try:
        fields = list(QUERY_FIELDS)
        proc = _run(binary, fields)
        if proc.returncode != 0 and "compute_cap" in (proc.stderr + proc.stdout):
            fields.remove("compute_cap")  # older drivers do not know this field
            proc = _run(binary, fields)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip().splitlines()
            warnings.append(
                "nvidia-smi is installed but failed: " + (msg[0] if msg else f"exit code {proc.returncode}")
                + ". Check that the NVIDIA kernel driver is loaded (and, in containers, that the GPU is "
                "passed through, e.g. Podman '--device nvidia.com/gpu=all')."
            )
            return [], warnings
        return rows_to_gpus(parse_query_output(proc.stdout, fields)), warnings
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [f"nvidia-smi could not be executed: {exc}"]


def sample_nvidia() -> list[GPUTelemetry]:
    binary = find_nvidia_smi()
    if not binary:
        return []
    try:
        proc = _run(binary, TELEMETRY_FIELDS)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for r in parse_query_output(proc.stdout, TELEMETRY_FIELDS):
        def _int(key: str, row: dict[str, str] = r) -> int | None:
            v = _num(row.get(key, ""))
            return int(v) if v is not None else None
        out.append(GPUTelemetry(
            index=_int("index") or 0,
            utilization_percent=_num(r.get("utilization.gpu", "")),
            vram_used_mb=_int("memory.used"),
            vram_free_mb=_int("memory.free"),
            vram_total_mb=_int("memory.total"),
            temperature_c=_num(r.get("temperature.gpu", "")),
        ))
    return out
