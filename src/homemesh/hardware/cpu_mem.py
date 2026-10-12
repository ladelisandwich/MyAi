"""CPU, RAM and disk detection (cross-platform via psutil, plus per-OS CPU model lookup)."""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

import psutil

from homemesh.common.proc import run_quiet
from homemesh.protocol.hardware import CPUInfo, DiskInfo, MemoryInfo

_INTERESTING_FLAGS = ("avx", "avx2", "avx512f", "avx512_vnni", "avx_vnni", "fma", "f16c", "amx_tile",
                      "neon", "asimd", "sve")


def parse_proc_cpuinfo(text: str) -> tuple[str | None, str | None, list[str]]:
    model = vendor = None
    flags: list[str] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = (p.strip() for p in line.split(":", 1))
        if key == "model name" and model is None:
            model = val
        elif key == "vendor_id" and vendor is None:
            vendor = val
        elif key in ("flags", "Features") and not flags:
            have = set(val.split())
            flags = [f for f in _INTERESTING_FLAGS if f in have]
        elif key == "Hardware" and model is None:  # some ARM boards
            model = val
    return model, vendor, flags


def _cpu_model_windows() -> str | None:
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        return platform.processor() or None


def _cpu_model_macos() -> str | None:
    try:
        out = run_quiet(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                             timeout=5, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def detect_cpu() -> CPUInfo:
    info = CPUInfo(
        physical_cores=psutil.cpu_count(logical=False),
        logical_cores=psutil.cpu_count(logical=True),
    )
    try:
        freq = psutil.cpu_freq()
        if freq and freq.max:
            info.max_freq_mhz = float(freq.max)
    except (OSError, NotImplementedError, AttributeError):
        pass
    if sys.platform.startswith("linux"):
        try:
            model, vendor, flags = parse_proc_cpuinfo(Path("/proc/cpuinfo").read_text(errors="ignore"))
            info.model, info.vendor, info.flags = model, vendor, flags
        except OSError:
            pass
    elif sys.platform == "win32":
        info.model = _cpu_model_windows()
    elif sys.platform == "darwin":
        info.model = _cpu_model_macos()
    if not info.model:
        info.model = platform.processor() or None
    return info


def detect_memory() -> MemoryInfo:
    vm = psutil.virtual_memory()
    try:
        swap_total = psutil.swap_memory().total // (1024 * 1024)
    except (OSError, RuntimeError):
        swap_total = None
    return MemoryInfo(total_mb=vm.total // (1024 * 1024), available_mb=vm.available // (1024 * 1024),
                      swap_total_mb=swap_total)


def detect_disk(path: Path) -> DiskInfo:
    try:
        path.mkdir(parents=True, exist_ok=True)
        du = psutil.disk_usage(str(path))
        return DiskInfo(path=str(path), total_mb=du.total // (1024 * 1024), free_mb=du.free // (1024 * 1024))
    except OSError:
        return DiskInfo(path=str(path))
