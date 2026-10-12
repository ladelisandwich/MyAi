"""Hardware detection orchestrator and live telemetry sampler.

``detect_hardware()`` builds a full :class:`HardwareReport` once at worker start-up (and
on demand). ``TelemetrySampler.sample()`` collects cheap live numbers for heartbeats.
OS-specific probing lives in the sibling modules; this module only combines results.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil

from homemesh.common.timeutil import now_iso
from homemesh.hardware.cpu_mem import detect_cpu, detect_disk, detect_memory
from homemesh.hardware.gpu_nvidia import detect_nvidia, sample_nvidia
from homemesh.hardware.osinfo import detect_os
from homemesh.protocol.hardware import GPUInfo, GPUTelemetry, HardwareReport, Telemetry


def _merge_gpus(primary: list[GPUInfo], secondary: list[GPUInfo]) -> list[GPUInfo]:
    """Combine nvidia-smi results with sysfs/WMI results without double-counting NVIDIA cards."""
    have_nvidia_smi = any(g.source == "nvidia-smi" for g in primary)
    merged = list(primary)
    for g in secondary:
        if g.vendor == "nvidia" and have_nvidia_smi:
            continue
        merged.append(g)
    for i, g in enumerate(merged):
        g.index = i
    return merged


def detect_gpus() -> tuple[list[GPUInfo], list[str]]:
    gpus, warnings = detect_nvidia()
    if sys.platform.startswith("linux"):
        from homemesh.hardware.gpu_linux_sysfs import detect_sysfs

        others = detect_sysfs()
        gpus = _merge_gpus(gpus, others)
        if any(g.vendor == "nvidia" and g.source == "sysfs" for g in gpus):
            warnings.append(
                "An NVIDIA GPU is present but nvidia-smi is not available, so VRAM cannot be measured. "
                "Install the NVIDIA driver utilities (on Bazzite use the -nvidia image; in containers pass "
                "the GPU with CDI: --device nvidia.com/gpu=all)."
            )
    elif sys.platform == "win32":
        from homemesh.hardware.gpu_windows import detect_windows_gpus

        others, w = detect_windows_gpus()
        warnings.extend(w)
        gpus = _merge_gpus(gpus, others)
    elif sys.platform == "darwin":
        from homemesh.hardware.gpu_macos import detect_macos_gpus

        gpus, warnings = detect_macos_gpus()
    if not gpus:
        warnings.append("No GPU detected; this node can still run CPU inference, embeddings and indexing.")
    return gpus, warnings


def mark_ignored_gpus(gpus: list[GPUInfo], patterns: list[str]) -> set[int]:
    """Flag GPUs whose name contains one of ``patterns`` (case-insensitive); return their indices."""
    pats = [p.strip().lower() for p in patterns if p.strip()]
    ignored: set[int] = set()
    for g in gpus:
        if any(p in g.name.lower() for p in pats):
            g.ignored = True
            ignored.add(g.index)
    return ignored


def detect_hardware(data_dir: Path | None = None) -> HardwareReport:
    gpus, warnings = detect_gpus()
    report = HardwareReport(
        os=detect_os(),
        cpu=detect_cpu(),
        memory=detect_memory(),
        gpus=gpus,
        disks=[detect_disk(data_dir)] if data_dir else [],
        warnings=warnings,
        collected_at=now_iso(),
    )
    return report


class TelemetrySampler:
    """Collects live utilisation. GPU sampling shells out to nvidia-smi, so callers should
    run :meth:`sample` in a thread (the worker does)."""

    def __init__(self, gpu_busy_threshold_percent: float = 85.0, window: int = 3) -> None:
        self.gpu_busy_threshold = gpu_busy_threshold_percent
        self.window = window
        self._busy_history: list[bool] = []
        self.ignore_indices: set[int] = set()  # GPUs excluded by worker.ignore_gpus
        psutil.cpu_percent(interval=None)  # prime the counter
        self._has_nvidia = None

    def sample(self, running_jobs: int = 0) -> Telemetry:
        vm = psutil.virtual_memory()
        try:
            load1 = psutil.getloadavg()[0]
        except (OSError, AttributeError):
            load1 = None
        gpus: list[GPUTelemetry] = sample_nvidia()
        if sys.platform.startswith("linux"):
            from homemesh.hardware.gpu_linux_sysfs import sample_sysfs

            offset = len(gpus)
            for g in sample_sysfs():
                g.index = offset + g.index
                gpus.append(g)
        max_util = max((g.utilization_percent for g in gpus
                        if g.utilization_percent is not None and g.index not in self.ignore_indices), default=None)
        # "External" busy: GPU loaded while we are idle -> someone else (a game) is using it.
        busy_now = bool(max_util is not None and max_util >= self.gpu_busy_threshold and running_jobs == 0)
        self._busy_history = ([*self._busy_history, busy_now])[-self.window:]
        external_busy = len(self._busy_history) >= 2 and all(self._busy_history[-2:])
        return Telemetry(
            cpu_percent=psutil.cpu_percent(interval=None),
            ram_total_mb=vm.total // (1024 * 1024),
            ram_available_mb=vm.available // (1024 * 1024),
            load_avg_1m=load1,
            gpus=gpus,
            external_gpu_busy=external_busy,
            sampled_at=now_iso(),
        )


def benchmark_detection() -> dict[str, float]:
    t0 = time.perf_counter()
    detect_hardware()
    return {"detect_hardware_ms": (time.perf_counter() - t0) * 1000}
