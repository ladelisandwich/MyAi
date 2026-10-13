"""Consent to remote updates: a marker file in the config folder, read live.

``homemesh update allow`` creates it, ``homemesh update disallow`` deletes it; both take effect at once.
The worker reads it on every ``update.check`` / ``update.apply`` job, and the runner (origin ``remote``)
before the fetch and again just before stopping HomeMesh. A local ``homemesh update`` never needs it.

Deliberately **not** a ``homemesh.toml`` key: every ``HOMEMESH__SECTION__KEY`` environment variable maps
onto the config (so an environment variable could turn it on), ``save_config`` rewrites the whole file
from the env-merged model, and every config section is ``extra="forbid"`` (a key added by a later version
would break a rollback). No API route, settings section or protocol message can create this file.

This module is the only place that knows the marker's file name; the runner gets the full path in
``args.json`` (``consent_file``).
"""

from __future__ import annotations

import contextlib
import getpass
import os
from pathlib import Path

from homemesh.common.paths import AppPaths
from homemesh.common.timeutil import now_iso
from homemesh.update import files

_MARKER_NAME = "remote-updates.allowed"


def marker_path(paths: AppPaths) -> Path:
    return paths.config_dir / _MARKER_NAME


def remote_allowed(paths: AppPaths) -> bool:
    """Is the marker present right now? (A regular file; a dangling link doesn't count.)"""
    return marker_path(paths).is_file()


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def allow(paths: AppPaths) -> Path:
    """Create the marker (owner-only; content ``allowed at <ISO> by <user>``). Idempotent; the caller
    checks that HomeMesh is set up (the config folder exists). Returns the marker path."""
    target = marker_path(paths)
    files.atomic_write_bytes(target, f"allowed at {now_iso()} by {_who()}\n".encode())
    return target


def disallow(paths: AppPaths) -> bool:
    """Delete the marker. True if it existed. Never follows a link (a link itself is removed)."""
    target = marker_path(paths)
    existed = target.is_file() or target.is_symlink()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(target)
    return existed
