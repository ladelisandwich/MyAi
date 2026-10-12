"""Linux GPU discovery through ``/sys/class/drm`` (AMD amdgpu, Intel i915/xe, NVIDIA presence).

amdgpu exposes real VRAM totals/usage and busy percentage in sysfs, so AMD cards get
genuine telemetry without ROCm tools. Intel integrated GPUs share system RAM; we report
that instead of inventing a VRAM size. NVIDIA cards found here are only used as a
fallback signal when nvidia-smi is missing (driver utilities not installed / not passed
into a container).
"""

from __future__ import annotations

import re
from pathlib import Path

from homemesh.protocol.hardware import GPUInfo, GPUTelemetry

VENDORS = {"0x1002": "amd", "0x10de": "nvidia", "0x8086": "intel"}
_CARD_RE = re.compile(r"^card\d+$")


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None


def _read_int(p: Path) -> int | None:
    v = _read(p)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _temp_c(device: Path) -> float | None:
    hwmon = device / "hwmon"
    if not hwmon.is_dir():
        return None
    for h in sorted(hwmon.iterdir()):
        t = _read_int(h / "temp1_input")
        if t is not None:
            return t / 1000.0
    return None


def list_cards(drm_root: Path = Path("/sys/class/drm")) -> list[Path]:
    if not drm_root.is_dir():
        return []
    return sorted((p for p in drm_root.iterdir() if _CARD_RE.match(p.name)), key=lambda p: int(p.name[4:]))


def detect_sysfs(drm_root: Path = Path("/sys/class/drm"), kfd_path: Path = Path("/dev/kfd")) -> list[GPUInfo]:
    gpus: list[GPUInfo] = []
    seen_devices: set[str] = set()
    for idx, card in enumerate(list_cards(drm_root)):
        device = card / "device"
        vendor_id = (_read(device / "vendor") or "").lower()
        vendor = VENDORS.get(vendor_id)
        if vendor is None:
            continue
        try:
            real = str(device.resolve())
        except OSError:
            real = str(device)
        if real in seen_devices:
            continue
        seen_devices.add(real)
        device_id = (_read(device / "device") or "").lower()
        product = _read(device / "product_name")
        uevent = _read(device / "uevent") or ""
        driver = None
        for line in uevent.splitlines():
            if line.startswith("DRIVER="):
                driver = line.split("=", 1)[1]
        pci_slot = None
        for line in uevent.splitlines():
            if line.startswith("PCI_SLOT_NAME="):
                pci_slot = line.split("=", 1)[1]
        unavailable: dict[str, str] = {}
        name = product or f"{vendor.upper()} GPU [{vendor_id[2:]}:{device_id[2:]}]"
        g = GPUInfo(index=idx, vendor=vendor, name=name, source="sysfs", pci_bus_id=pci_slot,
                    driver_version=driver)
        if vendor == "amd":
            total = _read_int(device / "mem_info_vram_total")
            used = _read_int(device / "mem_info_vram_used")
            busy = _read_int(device / "gpu_busy_percent")
            g.vram_total_mb = total // (1024 * 1024) if total is not None else None
            g.vram_used_mb = used // (1024 * 1024) if used is not None else None
            if g.vram_total_mb is not None and g.vram_used_mb is not None:
                g.vram_free_mb = g.vram_total_mb - g.vram_used_mb
            g.utilization_percent = float(busy) if busy is not None else None
            g.temperature_c = _temp_c(device)
            # APUs report a small carve-out as "VRAM"; GTT (shared system RAM) is much larger.
            gtt = _read_int(device / "mem_info_gtt_total")
            if total is not None and gtt is not None and total <= 2 * 1024**3 and gtt > 4 * total:
                g.integrated = True
            else:
                g.integrated = False if total is not None else None
            g.compute_apis = ["rocm"] if kfd_path.exists() else []
            if not kfd_path.exists():
                unavailable["rocm"] = "/dev/kfd not present: ROCm compute is unavailable (Vulkan may still work)"
            if total is None:
                unavailable["vram_total_mb"] = "amdgpu did not expose mem_info_vram_total"
        elif vendor == "intel":
            lmem = _read_int(device / "lmem_total_bytes")  # discrete Arc (i915)
            if lmem:
                g.vram_total_mb = lmem // (1024 * 1024)
                g.integrated = False
            else:
                g.integrated = True
                unavailable["vram_total_mb"] = "integrated GPU shares system RAM (no dedicated VRAM)"
            g.temperature_c = _temp_c(device)
            unavailable.setdefault("utilization_percent", "not exposed via sysfs for Intel GPUs")
        elif vendor == "nvidia":
            g.integrated = False
            unavailable["vram_total_mb"] = "nvidia-smi not available; install NVIDIA driver utilities"
            if driver == "nouveau":
                unavailable["cuda"] = "open-source nouveau driver in use; CUDA requires the proprietary NVIDIA driver"
        g.unavailable = unavailable
        gpus.append(g)
    return gpus


def sample_sysfs(drm_root: Path = Path("/sys/class/drm")) -> list[GPUTelemetry]:
    out = []
    for idx, card in enumerate(list_cards(drm_root)):
        device = card / "device"
        if (_read(device / "vendor") or "").lower() != "0x1002":
            continue
        total = _read_int(device / "mem_info_vram_total")
        used = _read_int(device / "mem_info_vram_used")
        busy = _read_int(device / "gpu_busy_percent")
        out.append(GPUTelemetry(
            index=idx,
            utilization_percent=float(busy) if busy is not None else None,
            vram_used_mb=used // (1024 * 1024) if used is not None else None,
            vram_total_mb=total // (1024 * 1024) if total is not None else None,
            vram_free_mb=(total - used) // (1024 * 1024) if total is not None and used is not None else None,
            temperature_c=_temp_c(device),
        ))
    return out
