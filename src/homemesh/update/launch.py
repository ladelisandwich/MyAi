"""Start the update runner outside HomeMesh, wait for it, and stop a check runner (design section 8).

INTERFACE STUB: the signatures and contracts below are fixed; the bodies marked ``NotImplementedError`` are
filled in by the runner+launch implementer. Callers: ``worker/updates.py`` (async waits) and
``cli/update.py`` (sync waits).

How a runner is started:

* Interpreter: :func:`base_python` (``sys._base_executable``; on Windows the console ``python.exe`` next to
  it, never ``pythonw.exe``). Missing → :class:`UpdateLaunchError` ``update_no_base_python``.
* Script: :func:`runner_script` — ``homemesh/update/runner.py`` of the **current** venv, run in place.
* Command: ``<base python> -I -S -X utf8 <runner.py> {check|apply} --args <args.json>`` (:func:`runner_command`).
* ``args.json`` comes from ``files.build_runner_args`` and is written with ``files.write_runner_args``
  (owner-only); the log (``files.apply_log_file`` / ``files.check_log_file``) is created owner-only with
  ``files.open_private_append`` before launch.
* Method (:func:`service_kind`, :func:`inside_homemesh_unit`):
  - apply with service ``systemd``, or **any** runner launched from inside ``homemesh.service``:
    ``systemd-run --user --collect --quiet --unit=<unit_name()> --service-type=exec -p RuntimeMaxSec=…
    -p StandardOutput=append:<log> -p StandardError=append:<log> --setenv=… -- <command>``. If that fails
    inside the unit: ``update_launch_failed``; never fall back to a child process there.
  - POSIX otherwise: ``Popen(start_new_session=True, stdin=DEVNULL, stdout=log, stderr=log,
    cwd=<data>/update, env=launch_env())``.
  - Windows: ``Popen(creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
    …)``, retried without ``CREATE_BREAKAWAY_FROM_JOB`` on ``OSError`` winerror 5.
  - ``foreground=True`` (``homemesh update`` running a check as its child): POSIX ``start_new_session``,
    Windows ``CREATE_NO_WINDOW``; the CLI waits with :func:`wait_for_check_sync`.
* Tests: ``HOMEMESH_UPDATE_RUNNER=<path to a stub script>`` replaces the runner script, honoured only when
  ``HOMEMESH_TESTING=1`` (:func:`runner_script`).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import HomeMeshError
from homemesh.common.paths import AppPaths
from homemesh.protocol.update import (
    ACCEPT_TIMEOUT_S,
    APPLY_RUNTIME_MAX_S,
    CHECK_TIMEOUT_S,
    UpdateCheckResult,
    UpdateOrigin,
)
from homemesh.update.files import RunnerMode, ServiceKind

LaunchMethod = Literal["systemd", "posix", "windows", "foreground"]

UNIT_PREFIX = "homemesh-update"
RUNNER_ENV = "HOMEMESH_UPDATE_RUNNER"  # test hook, only with HOMEMESH_TESTING=1
TESTING_ENV = "HOMEMESH_TESTING"
CHECK_RUNTIME_MAX_S = int(CHECK_TIMEOUT_S)  # systemd RuntimeMaxSec for a check runner (300)
APPLY_RUNTIME_MAX_S_INT = int(APPLY_RUNTIME_MAX_S)  # systemd RuntimeMaxSec for an apply runner (10800)


class UpdateLaunchError(HomeMeshError):
    """The runner could not be started, or didn't do what the waiter expected.

    ``code`` is an update error code (``update_launch_failed``, ``update_no_base_python``,
    ``update_git_missing``, ``update_not_supported``, …); ``message``/``hint`` come from
    ``protocol.update.format_update_error``. ``details["log_tail"]`` holds the last lines of the runner log,
    already redacted (``protocol.update.redact``). The worker turns it into ``JobFailed(code, error=message,
    hint=hint)``; the CLI prints message and hint.
    """

    code = "update_launch_failed"
    status_code = 500


@dataclass
class RunnerHandle:
    """A started runner. Pass it back to the wait/kill functions; don't poke at the fields from outside."""

    mode: RunnerMode
    run_id: str
    method: LaunchMethod
    args_file: Path
    log_file: Path
    out_file: Path | None  # check: checks/<run_id>.json; apply: None
    run_file: Path | None  # apply: runs/<run_id>/run.json; check: None
    unit: str | None = None  # systemd unit name (method "systemd")
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)  # non-systemd methods
    pid: int | None = None
    started_monotonic: float = 0.0


# --------------------------------------------------------------------------- pure helpers (implemented)

def unit_name(mode: RunnerMode, run_id: str) -> str:
    """``homemesh-update-check-<run_id>`` / ``homemesh-update-apply-<run_id>`` (without ``.service``)."""
    return f"{UNIT_PREFIX}-{mode}-{run_id}"


def runner_script() -> Path:
    """The runner script to execute: ``update/runner.py`` of this venv, resolved; or, only when
    ``HOMEMESH_TESTING=1``, the file named by ``HOMEMESH_UPDATE_RUNNER``."""
    override = os.environ.get(RUNNER_ENV)
    if override and os.environ.get(TESTING_ENV) == "1":
        return Path(override).resolve()
    from homemesh.update import runner

    return Path(runner.__file__).resolve()


def runner_command(python: Path, script: Path, mode: RunnerMode, args_file: Path) -> list[str]:
    """``[<python>, -I, -S, -X, utf8, <script>, <mode>, --args, <args_file>]``."""
    return [os.fspath(python), "-I", "-S", "-X", "utf8", os.fspath(script), mode, "--args", os.fspath(args_file)]


# --------------------------------------------------------------------------- environment probes (stubs)

def base_python() -> Path:
    """The base interpreter (not the venv's): ``Path(sys._base_executable)``, on Windows its sibling
    ``python.exe``. Raises :class:`UpdateLaunchError` ``update_no_base_python`` if it doesn't exist."""
    raise NotImplementedError


def service_kind(paths: AppPaths) -> ServiceKind:
    """``systemd`` when the user unit file exists and ``systemctl --user show-environment`` succeeds (same
    test as ``cli/service.py``), ``windows`` on Windows, else ``posix``."""
    raise NotImplementedError


def inside_homemesh_unit() -> bool:
    """True when this process runs in ``homemesh.service``'s cgroup (``/proc/self/cgroup`` ends in
    ``/homemesh.service``). Such a process must launch every runner with ``systemd-run``."""
    raise NotImplementedError


def launch_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a non-systemd runner: HomeMesh's own minus ``PYTHON*``, ``__PYVENV_LAUNCHER__``,
    ``HOMEMESH_DETACHED``, ``VSCODE_*``, ``GIT_*``, ``SSH_ASKPASS``. (The runner uses it only to restart
    HomeMesh and run its CLI; git/npm/pip get an allowlisted environment built by the runner.)"""
    raise NotImplementedError


def resolve_git(record_git: str | None) -> Path:
    """The git executable for ``args.json``: the record's ``git`` if it is an existing executable file,
    else ``shutil.which("git")`` made absolute. Raises :class:`UpdateLaunchError` ``update_git_missing``."""
    raise NotImplementedError


def log_tail(path: Path, lines: int = 20) -> str:
    """The last ``lines`` lines of a runner log, decoded leniently and redacted. Never raises ("" if none)."""
    raise NotImplementedError


# --------------------------------------------------------------------------- start (stubs)

def start_check(paths: AppPaths, cfg: HomeMeshConfig, *, origin: UpdateOrigin, job_id: str | None = None,
                run_id: str | None = None, foreground: bool = False) -> RunnerHandle:
    """Start a check runner and return at once.

    Loads the install record (``record.load()``; none → ``update_not_supported``), resolves base python and
    git, generates ``run_id`` (``files.new_run_id()``) unless given, writes ``checks/<run_id>.args.json``
    (``files.build_runner_args(mode="check", …)``), creates the check log owner-only and launches with the
    method described in the module docstring (``foreground=True``: a child of this process, for the CLI).
    Does **not** check consent, the update lock or the rate limit (the worker does; the runner re-checks
    the lock and, for origin ``remote``, consent). Raises :class:`UpdateLaunchError`.
    """
    raise NotImplementedError


def start_apply(paths: AppPaths, cfg: HomeMeshConfig, *, run_id: str, expected_commit: str, force: bool,
                origin: UpdateOrigin, job_id: str | None = None) -> RunnerHandle:
    """Start an apply runner, detached (it must outlive HomeMesh), and return at once.

    ``run_id`` is generated by the caller (``files.new_run_id()``), never taken from the hub. Writes
    ``runs/<run_id>/args.json``, creates ``logs/update-<run_id>.log`` owner-only and launches (systemd: unit
    ``homemesh-update-apply-<run_id>``, ``RuntimeMaxSec=10800``). Follow with :func:`wait_for_accept`.
    Raises :class:`UpdateLaunchError` (``update_launch_failed`` with the journalctl hint on systemd).
    """
    raise NotImplementedError


# --------------------------------------------------------------------------- wait / stop (stubs)

def runner_exit_code(handle: RunnerHandle) -> int | None:
    """The runner's exit code if it has exited, else None (systemd: from the unit's ``ExecMainStatus`` once
    inactive; a unit already garbage-collected by ``--collect`` counts as exited with code None→-1)."""
    raise NotImplementedError


async def wait_for_check(handle: RunnerHandle, *, timeout_s: float = CHECK_TIMEOUT_S,
                         poll_s: float = 0.5) -> UpdateCheckResult:
    """Wait for ``checks/<run_id>.json`` and return it validated (``UpdateCheckResult``; ``ok`` may be false,
    the caller maps ``result.error`` to ``JobFailed``). Raises :class:`UpdateLaunchError` if the runner
    exits without writing it (exit code mapped as in :func:`wait_for_accept`, plus the log tail), if the
    file doesn't validate, or on timeout (``update_launch_failed``, "the check did not finish in 5 minutes"). Does not kill the runner on
    timeout or cancellation; the caller calls :func:`kill_check` (also on ``asyncio.CancelledError``)."""
    raise NotImplementedError


def wait_for_check_sync(handle: RunnerHandle, *, timeout_s: float = CHECK_TIMEOUT_S,
                        poll_s: float = 0.5) -> UpdateCheckResult:
    """:func:`wait_for_check` for the CLI (blocking)."""
    raise NotImplementedError


async def wait_for_accept(handle: RunnerHandle, *, timeout_s: float = ACCEPT_TIMEOUT_S,
                          poll_s: float = 0.25) -> dict[str, object]:
    """Wait until ``runs/<run_id>/run.json`` exists with a ``phase`` (any phase is ≥ ``accepted``) and
    return it (a run.json that already carries a final status is returned as is; the run reports it).

    If the runner exits first, map its exit code (``runner.EXIT_*``): ``EXIT_BUSY`` → ``update_busy``,
    ``EXIT_NO_BASE_PYTHON`` → ``update_no_base_python``, anything else → ``update_launch_failed``. On
    ``timeout_s``: ``update_launch_failed``. Every :class:`UpdateLaunchError` carries the redacted log tail;
    on systemd the hint names ``journalctl --user -u 'homemesh-update-apply-<run_id>' -n 50``."""
    raise NotImplementedError


def wait_for_accept_sync(handle: RunnerHandle, *, timeout_s: float = ACCEPT_TIMEOUT_S,
                         poll_s: float = 0.25) -> dict[str, object]:
    """:func:`wait_for_accept` for the CLI (blocking)."""
    raise NotImplementedError


def kill_check(handle: RunnerHandle) -> None:
    """Stop a check runner and everything it started: systemd ``systemctl --user stop <unit>.service``;
    otherwise a psutil tree kill (children first, then the runner). Idempotent, never raises. Never used on
    an apply runner (an apply can't be cancelled)."""
    raise NotImplementedError
