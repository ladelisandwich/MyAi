"""One HomeMesh process per data folder, and a PID record that can't be confused with another program.

* :class:`InstanceLock` is an OS file lock held for the whole life of ``homemesh run``. The OS
  releases it when the process ends for any reason (including Windows ending a windowless
  ``pythonw.exe`` at logoff), so it never goes stale. A second process for the same data folder
  sees the lock and exits before it touches the database.
* The PID file records the process's start time next to its PID. Windows reuses PIDs quickly, so a
  bare PID left behind by a killed process can later belong to a browser or a game; a PID is only
  trusted if that process started at the recorded time. Nothing is ever stopped on a bare PID.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import IO

import psutil


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> bool:
        """Take the lock without waiting. False if another process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None


def lock_is_held(path: Path) -> bool:
    """Is a HomeMesh process running for this lock file (checked without disturbing it)?"""
    probe = InstanceLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True


def _create_time(pid: int) -> float | None:
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return None
        return p.create_time()
    except (psutil.Error, OSError):
        return None


def write_pid_record(path: Path, pid: int | None = None) -> None:
    pid = pid or os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"pid": pid, "started": _create_time(pid)}), encoding="utf-8")
    os.replace(tmp, path)


def read_pid_record(path: Path) -> int | None:
    """The PID of the running HomeMesh process, or None. A PID is returned only if a process with
    that PID exists AND started at the recorded time (otherwise the number was reused)."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        pid, started = data.get("pid"), data.get("started")
    elif isinstance(data, int):
        pid, started = data, None  # file from an older version: bare PID
    else:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    actual = _create_time(pid)
    if actual is None:
        return None
    if isinstance(started, (int, float)):
        return pid if abs(actual - started) < 1.0 else None
    # Older format without a start time: accept it only if the process really is HomeMesh.
    try:
        cmdline = " ".join(psutil.Process(pid).cmdline()).lower()
    except (psutil.Error, OSError):
        return None
    return pid if "homemesh" in cmdline else None
