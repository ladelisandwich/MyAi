"""Run helper programs without flashing a console window on Windows."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# subprocess.CREATE_NO_WINDOW only exists on Windows builds of Python.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def run_quiet(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` that never opens a console window on Windows.

    HomeMesh usually runs as a windowless ``pythonw.exe`` process (autostart, ``homemesh start``).
    Windows gives every console program such a process starts its own new console window, so
    without ``CREATE_NO_WINDOW`` each nvidia-smi call (every heartbeat) or PowerShell query would
    pop up a terminal window for a moment.
    """
    if sys.platform == "win32":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def spawn_quiet(args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    """Start a helper program without waiting for it, without a console window and without
    sharing HomeMesh's standard streams (no shell is involved)."""
    if sys.platform == "win32":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    for stream in ("stdin", "stdout", "stderr"):
        kwargs.setdefault(stream, subprocess.DEVNULL)
    return subprocess.Popen(args, **kwargs)
