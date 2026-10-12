"""Operating-system identification (distro, immutable OS, container detection)."""

from __future__ import annotations

import os
import platform
import socket
import sys
from pathlib import Path

from homemesh.protocol.hardware import OSInfo


def parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _in_container() -> bool:
    if Path("/run/.containerenv").exists() or Path("/.dockerenv").exists():
        return True
    try:
        cg = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
        return any(k in cg for k in ("docker", "containerd", "kubepods", "libpod"))
    except OSError:
        return False


def detect_os() -> OSInfo:
    info = OSInfo(
        system=platform.system(),
        release=platform.release(),
        version=platform.version(),
        hostname=socket.gethostname(),
        arch=platform.machine(),
        python=sys.version.split()[0],
    )
    if info.system == "Linux":
        for candidate in ("/etc/os-release", "/usr/lib/os-release"):
            p = Path(candidate)
            if p.exists():
                rel = parse_os_release(p.read_text(encoding="utf-8", errors="ignore"))
                info.distro_id = rel.get("ID")
                info.distro_name = rel.get("PRETTY_NAME") or rel.get("NAME")
                variant = rel.get("VARIANT_ID", "")
                if info.distro_id and "bazzite" in (info.distro_id + variant).lower():
                    info.distro_id = "bazzite"
                break
        info.immutable = Path("/run/ostree-booted").exists()
        info.in_container = _in_container()
    elif info.system == "Windows":
        info.distro_name = f"Windows {platform.release()} ({platform.version()})"
    elif info.system == "Darwin":
        info.distro_name = f"macOS {platform.mac_ver()[0]}"
    if os.environ.get("HOMEMESH_IN_CONTAINER") == "1":
        info.in_container = True
    return info
