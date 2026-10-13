"""Windows GPU discovery for non-NVIDIA adapters (AMD, Intel) via PowerShell/CIM + registry.

``Win32_VideoController.AdapterRAM`` is a 32-bit field and caps at 4 GB, so the real
dedicated memory size is read from the display adapter class registry key
(``HardwareInformation.qwMemorySize``), which is readable without administrator rights.
Live VRAM usage for AMD/Intel is not available without vendor tools; it is reported as
unavailable rather than estimated. NVIDIA adapters are handled by nvidia-smi.
"""

from __future__ import annotations

import json
import subprocess

from homemesh.common.proc import run_quiet
from homemesh.protocol.hardware import GPUInfo

_PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$base = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
$reg = @()
Get-ChildItem $base | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {
  $p = Get-ItemProperty $_.PSPath
  $reg += [pscustomobject]@{ Desc = $p.DriverDesc; Mem = $p.'HardwareInformation.qwMemorySize'; MatchingId = $p.MatchingDeviceId }
}
$vc = Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, DriverVersion, PNPDeviceID, AdapterCompatibility
[pscustomobject]@{ controllers = @($vc); registry = @($reg) } | ConvertTo-Json -Depth 4 -Compress
"""


def _vendor(name: str, pnp: str) -> str:
    s = f"{name} {pnp}".upper()
    if "VEN_10DE" in s or "NVIDIA" in s:
        return "nvidia"
    if "VEN_1002" in s or "AMD" in s or "RADEON" in s:
        return "amd"
    if "VEN_8086" in s or "INTEL" in s:
        return "intel"
    return "other"


def parse_windows_gpu_json(text: str) -> list[GPUInfo]:
    data = json.loads(text)
    controllers = data.get("controllers") or []
    if isinstance(controllers, dict):
        controllers = [controllers]
    registry = data.get("registry") or []
    if isinstance(registry, dict):
        registry = [registry]
    gpus: list[GPUInfo] = []
    for idx, c in enumerate(controllers):
        name = (c.get("Name") or "GPU").strip()
        pnp = c.get("PNPDeviceID") or ""
        vendor = _vendor(name, pnp)
        if vendor == "nvidia":
            continue  # nvidia-smi provides accurate data
        unavailable = {"vram_used_mb": "live VRAM usage is not exposed by Windows for this vendor without vendor tools",
                       "utilization_percent": "not available via WMI"}
        total_mb = None
        for r in registry:
            mem = r.get("Mem")
            if r.get("Desc") and r["Desc"].strip() == name and isinstance(mem, int) and mem > 0:
                total_mb = mem // (1024 * 1024)
                break
        if total_mb is None:
            ram = c.get("AdapterRAM")
            if isinstance(ram, int) and ram > 0:
                total_mb = ram // (1024 * 1024)
                if ram >= 0xFFF00000:
                    unavailable["vram_total_mb_exact"] = "WMI caps AdapterRAM at 4 GB; true size may be larger"
        integrated = None
        if vendor == "intel" and "ARC" not in name.upper():
            integrated = True
            unavailable["vram_total_mb"] = "integrated GPU shares system RAM"
            total_mb = None
        elif vendor == "amd":
            integrated = bool(total_mb is not None and total_mb <= 2048 and "RX" not in name.upper())
        gpus.append(GPUInfo(index=idx, vendor=vendor, name=name, driver_version=c.get("DriverVersion"),
                            vram_total_mb=total_mb, integrated=integrated,
                            compute_apis=["directml"], source="wmi", unavailable=unavailable,
                            pci_bus_id=pnp or None))
    return gpus


def detect_windows_gpus() -> tuple[list[GPUInfo], list[str]]:
    try:
        # Ask PowerShell for UTF-8 and decode it ourselves: with text=True Python would use the ANSI
        # code page, and a driver or GPU name it can't decode would stop the worker from starting.
        proc = run_quiet(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + _PS_SCRIPT],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [f"Windows GPU query failed: {exc}"]
    out = (proc.stdout or b"").decode("utf-8", errors="replace").lstrip("\ufeff")
    if proc.returncode != 0 or not out.strip():
        return [], ["Windows GPU query returned no data (PowerShell/CIM unavailable)"]
    try:
        return parse_windows_gpu_json(out), []
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return [], [f"Could not parse Windows GPU information: {exc}"]
