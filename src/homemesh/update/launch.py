"""Start the update runner outside HomeMesh, wait for it, and stop a check runner (design section 8).

Callers: ``worker/updates.py`` (async waits) and ``cli/update.py`` (sync waits).

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
    inside the unit: ``update_launch_failed``; never fall back to a child process there (it would live in
    ``homemesh.service``'s cgroup and die with ``systemctl --user stop``). Outside the unit a failed
    ``systemd-run`` falls back to the POSIX method.
  - POSIX otherwise: ``Popen(start_new_session=True, stdin=DEVNULL, stdout=log, stderr=log,
    cwd=<data>/update, env=launch_env())``.
  - Windows: ``Popen(creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB,
    …)``, retried without ``CREATE_BREAKAWAY_FROM_JOB`` on ``OSError`` winerror 5.
  - ``foreground=True`` (``homemesh update`` running a check as its child): POSIX ``start_new_session``,
    Windows ``CREATE_NO_WINDOW``; the CLI waits with :func:`wait_for_check_sync`.
* :func:`wait_for_accept` that times out before the runner wrote ``run.json`` **kills** the runner (tree),
  so ``update_launch_failed`` always means nothing was changed (lead decision; nothing changes before the
  runner's ``building`` phase anyway).
* Tests: ``HOMEMESH_UPDATE_RUNNER=<path to a stub script>`` replaces the runner script, honoured only when
  ``HOMEMESH_TESTING=1`` (:func:`runner_script`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import psutil

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import HomeMeshError
from homemesh.common.paths import AppPaths, default_paths
from homemesh.common.proc import run_quiet
from homemesh.protocol.update import (
    ACCEPT_TIMEOUT_S,
    APPLY_RUNTIME_MAX_S,
    CHECK_TIMEOUT_S,
    ID_MAX,
    UpdateCheckResult,
    UpdateOrigin,
    clean_line,
    clean_text,
    format_update_error,
    installer_command,
    redact,
)
from homemesh.update import files, record
from homemesh.update import runner as _runner
from homemesh.update.files import RunnerMode, ServiceKind

LaunchMethod = Literal["systemd", "posix", "windows", "foreground"]

UNIT_PREFIX = "homemesh-update"
RUNNER_ENV = "HOMEMESH_UPDATE_RUNNER"  # test hook, only with HOMEMESH_TESTING=1
TESTING_ENV = "HOMEMESH_TESTING"
CHECK_RUNTIME_MAX_S = int(CHECK_TIMEOUT_S)  # systemd RuntimeMaxSec for a check runner (300)
APPLY_RUNTIME_MAX_S_INT = int(APPLY_RUNTIME_MAX_S)  # systemd RuntimeMaxSec for an apply runner (10800)

SERVICE_UNIT = "homemesh.service"
CGROUP_FILE = "/proc/self/cgroup"  # read by inside_homemesh_unit(); tests point it elsewhere
_DROP_EXACT = frozenset({"__PYVENV_LAUNCHER__", "HOMEMESH_DETACHED", "SSH_ASKPASS"})
_DROP_PREFIXES = ("PYTHON", "VSCODE_", "GIT_")
# The runner's marker: at the start of a line (printed) or after the log timestamp (logged). Anchored, so the
# launcher's own header line (which names the job) can never be read as one.
_EXIT_MARKER_RE = re.compile(r"^(?:\S+ )?" + re.escape(_runner.EXIT_MARKER) + r" (-?[0-9]+)\b", re.MULTILINE)

CREATE_NO_WINDOW = _runner.CREATE_NO_WINDOW
CREATE_NEW_PROCESS_GROUP = _runner.CREATE_NEW_PROCESS_GROUP
CREATE_BREAKAWAY_FROM_JOB = _runner.CREATE_BREAKAWAY_FROM_JOB


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


# --------------------------------------------------------------------------- pure helpers

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


def _error(code: str, *, log_tail: str = "", unit: str | None = None, **fields: Any) -> UpdateLaunchError:
    """An :class:`UpdateLaunchError` with the table texts. On systemd the hint names the run's own unit
    instead of the ``homemesh-update-*`` pattern."""
    issue = format_update_error(code, **fields)
    hint = issue.hint
    if unit and hint:
        hint = hint.replace("'homemesh-update-*'", f"'{unit}'")
    return UpdateLaunchError(issue.message, code=issue.code, hint=hint, details={"log_tail": log_tail})


# --------------------------------------------------------------------------- environment probes

def _base_python_path() -> Path:
    exe = Path(getattr(sys, "_base_executable", "") or sys.executable)
    if sys.platform == "win32":
        exe = exe.with_name("python.exe")
    return exe


def base_python() -> Path:
    """The base interpreter (not the venv's): ``Path(sys._base_executable)``, on Windows its sibling
    ``python.exe``. Raises :class:`UpdateLaunchError` ``update_no_base_python`` if it doesn't exist."""
    exe = _base_python_path()
    try:
        ok = exe.is_file()
    except OSError:
        ok = False
    if not ok:
        rec = record.load()
        raise _error("update_no_base_python", path=os.fspath(exe), source_dir=rec.source_dir if rec else None)
    return exe


def _unit_file() -> Path:
    """``~/.config/systemd/user/homemesh.service`` (same as ``cli/service.py::unit_path()``)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / SERVICE_UNIT


def _systemd_user_manager() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        r = run_quiet(["systemctl", "--user", "show-environment"], capture_output=True, check=False, timeout=15,
                      stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def service_kind(paths: AppPaths) -> ServiceKind:
    """``systemd`` when the user unit file exists and ``systemctl --user show-environment`` succeeds (same
    test as ``cli/service.py``), ``windows`` on Windows, else ``posix``."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            has_unit = _unit_file().is_file()
        except OSError:
            has_unit = False
        if has_unit and _systemd_user_manager():
            return "systemd"
    return "posix"


def inside_homemesh_unit() -> bool:
    """True when this process runs in ``homemesh.service``'s cgroup (``/proc/self/cgroup`` ends in
    ``/homemesh.service``). Such a process must launch every runner with ``systemd-run``."""
    return _runner.inside_homemesh_unit(CGROUP_FILE)


def launch_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a non-systemd runner: HomeMesh's own minus ``PYTHON*``, ``__PYVENV_LAUNCHER__``,
    ``HOMEMESH_DETACHED``, ``VSCODE_*``, ``GIT_*``, ``SSH_ASKPASS``. (The runner uses it only to restart
    HomeMesh and run its CLI; git/npm/pip get an allowlisted environment built by the runner.)"""
    src = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for key, value in src.items():
        upper = key.upper()
        if upper in _DROP_EXACT or upper.startswith(_DROP_PREFIXES):
            continue
        out[key] = value
    return out


def resolve_git(record_git: str | None) -> Path:
    """The git executable for ``args.json``: the record's ``git`` if it is an existing executable file,
    else ``shutil.which("git")`` made absolute. Raises :class:`UpdateLaunchError` ``update_git_missing``."""
    for candidate in (record_git, shutil.which("git")):
        if not candidate:
            continue
        path = Path(candidate)
        try:
            if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
                return path
            if not path.is_absolute():
                path = path.absolute()
                if path.is_file() and os.access(path, os.X_OK):
                    return path
        except OSError:
            continue
    raise _error("update_git_missing")


def log_tail(path: Path, lines: int = 20) -> str:
    """The last ``lines`` lines of a runner log, decoded leniently and redacted. Never raises ("" if none)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            raw = fh.read()
        text = clean_text(redact(raw.decode("utf-8", "replace")), 1 << 20)
        kept = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(kept[-lines:]) if lines > 0 else ""
    except Exception:
        return ""


def _homemesh_home(paths: AppPaths) -> str | None:
    """``HOMEMESH_HOME`` for the processes the runner starts: the environment's (absolute), else the folder
    given with ``homemesh --home`` (paths in that layout but not the platform defaults)."""
    env_home = os.environ.get("HOMEMESH_HOME")
    if env_home:
        return os.fspath(Path(env_home).expanduser().resolve())
    try:
        if paths != default_paths() and paths.data_dir == paths.config_dir / "data":
            return os.fspath(paths.config_dir.resolve())
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------- start

def _prepare(paths: AppPaths, cfg: HomeMeshConfig, *, mode: RunnerMode, run_id: str, origin: UpdateOrigin,
             job_id: str | None, expected_commit: str | None = None, force: bool = False) -> files.RunnerArgs:
    rec = record.load()
    if rec is None or not rec.source_dir:
        reason = ("it has no install record (installed with pip, or by an installer from before 0.3.0)"
                  if rec is None else "it was installed from a folder that is not a git checkout")
        raise _error("update_not_supported", computer=cfg.node.name, reason=reason,
                     installer_command=installer_command(record.current_os(), rec.source_dir if rec else None,
                                                         pull=True))
    base_python()
    git = resolve_git(rec.git)
    service = service_kind(paths)
    try:
        args = files.build_runner_args(
            paths, mode=mode, run_id=run_id, computer=cfg.node.name, roles=list(cfg.node.roles),
            source_dir=rec.source_dir, stable_venv=rec.stable_venv, git=git, node_dir=rec.node_dir,
            service=service, unit_path=_unit_file() if service == "systemd" else None, job_id=job_id,
            origin=origin, expected_commit=expected_commit, force=force, homemesh_home=_homemesh_home(paths))
        files.write_runner_args(paths, args)
    except (ValueError, OSError) as exc:
        raise _error("update_launch_failed", detail=f"preparing the update failed: {exc}") from exc
    return args


def _systemd_env(args: files.RunnerArgs) -> dict[str, str]:
    """``--setenv`` values for the transient unit: PATH, HOME, the runner's allowlist, HOMEMESH_HOME."""
    env: dict[str, str] = {"PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    for name in _runner.TOOL_ENV_ALLOW:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if args.get("homemesh_home"):
        env["HOMEMESH_HOME"] = str(args["homemesh_home"])
    return {k: v for k, v in env.items() if "\n" not in v and "\x00" not in v}


def _start_systemd(handle: RunnerHandle, cmd: list[str], args: files.RunnerArgs) -> None:
    unit = unit_name(handle.mode, handle.run_id)
    runtime = APPLY_RUNTIME_MAX_S_INT if handle.mode == "apply" else CHECK_RUNTIME_MAX_S
    log = os.fspath(handle.log_file)
    argv = [shutil.which("systemd-run") or "/usr/bin/systemd-run", "--user", "--collect", "--quiet",
            f"--unit={unit}", "--service-type=exec", "-p", f"RuntimeMaxSec={runtime}",
            "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}"]
    argv += [f"--setenv={k}={v}" for k, v in _systemd_env(args).items()]
    argv += ["--", *cmd]
    try:
        r = run_quiet(argv, capture_output=True, check=False, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _error("update_launch_failed", detail=f"systemd-run failed: {exc}", log=log, unit=unit) from exc
    if r.returncode != 0:
        stderr = clean_text(redact((r.stderr or b"").decode("utf-8", "replace")), 300).strip()
        raise _error("update_launch_failed", detail=f"systemd-run failed: {stderr or r.returncode}", log=log,
                     unit=unit, log_tail=log_tail(handle.log_file))
    handle.method = "systemd"
    handle.unit = unit


def _start_child(handle: RunnerHandle, cmd: list[str], paths: AppPaths, *, foreground: bool) -> None:
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "cwd": os.fspath(files.update_dir(paths)),
                              "env": launch_env(), "close_fds": True}
    with files.open_private_append(handle.log_file) as log:
        kwargs["stdout"] = log
        kwargs["stderr"] = log
        try:
            if sys.platform == "win32":
                flags = CREATE_NO_WINDOW
                if not foreground:
                    flags |= CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
                try:
                    proc = subprocess.Popen(cmd, creationflags=flags, **kwargs)
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 5 or not flags & CREATE_BREAKAWAY_FROM_JOB:
                        raise
                    proc = subprocess.Popen(cmd, creationflags=flags & ~CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            else:
                proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
        except OSError as exc:
            raise _error("update_launch_failed", detail=f"starting {cmd[0]} failed: {exc}",
                         log=os.fspath(handle.log_file)) from exc
    handle.process = proc
    handle.pid = proc.pid
    handle.method = "foreground" if foreground else ("windows" if sys.platform == "win32" else "posix")


def _launch(paths: AppPaths, args: files.RunnerArgs, *, foreground: bool) -> RunnerHandle:
    mode: RunnerMode = args["mode"]
    run_id = args["run_id"]
    args_file = files.runner_args_path(paths, args)
    log_file = Path(args["log"])
    try:
        with files.open_private_append(log_file) as fh:
            job = clean_line(str(args["job_id"]), ID_MAX)  # from the hub: one line, whatever it holds
            fh.write(f"{'=' * 20} HomeMesh update {mode} {run_id} (job {job}, "
                     f"origin {args['origin']}) {'=' * 20}\n".encode())
    except OSError as exc:
        raise _error("update_launch_failed", detail=f"creating the log {log_file} failed: {exc}") from exc
    cmd = runner_command(base_python(), runner_script(), mode, args_file)
    handle = RunnerHandle(mode=mode, run_id=run_id, method="posix", args_file=args_file, log_file=log_file,
                          out_file=Path(args["out"]) if args["out"] else None,
                          run_file=files.run_file(paths, run_id) if mode == "apply" else None,
                          started_monotonic=time.monotonic())
    inside = inside_homemesh_unit()
    if inside or (not foreground and mode == "apply" and args["service"] == "systemd"):
        try:
            _start_systemd(handle, cmd, args)
            return handle
        except UpdateLaunchError:
            if inside:  # a child would live in homemesh.service's cgroup and die with it
                raise
            with contextlib.suppress(OSError), files.open_private_append(log_file) as fh:
                fh.write(b"systemd-run failed; starting the update helper as a background process instead\n")
    _start_child(handle, cmd, paths, foreground=foreground)
    return handle


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
    run_id = run_id or files.new_run_id()
    args = _prepare(paths, cfg, mode="check", run_id=run_id, origin=origin, job_id=job_id)
    return _launch(paths, args, foreground=foreground)


def start_apply(paths: AppPaths, cfg: HomeMeshConfig, *, run_id: str, expected_commit: str, force: bool,
                origin: UpdateOrigin, job_id: str | None = None) -> RunnerHandle:
    """Start an apply runner, detached (it must outlive HomeMesh), and return at once.

    ``run_id`` is generated by the caller (``files.new_run_id()``), never taken from the hub. Writes
    ``runs/<run_id>/args.json``, creates ``logs/update-<run_id>.log`` owner-only and launches (systemd: unit
    ``homemesh-update-apply-<run_id>``, ``RuntimeMaxSec=10800``). Follow with :func:`wait_for_accept`.
    Raises :class:`UpdateLaunchError` (``update_launch_failed`` with the journalctl hint on systemd).
    """
    args = _prepare(paths, cfg, mode="apply", run_id=run_id, origin=origin, job_id=job_id,
                    expected_commit=expected_commit, force=force)
    return _launch(paths, args, foreground=False)


# --------------------------------------------------------------------------- wait / stop

def _unit_state(unit: str) -> dict[str, str] | None:
    try:
        r = run_quiet(["systemctl", "--user", "show", "-p", "LoadState,ActiveState,ExecMainStatus",
                       f"{unit}.service"], capture_output=True, check=False, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out: dict[str, str] = {}
    for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def runner_exit_code(handle: RunnerHandle) -> int | None:
    """The runner's exit code if it has exited, else None (systemd: from the unit's ``ExecMainStatus`` once
    inactive; a unit already garbage-collected by ``--collect`` counts as exited with code None→-1)."""
    if handle.process is not None:
        return handle.process.poll()
    if handle.unit:
        state = _unit_state(handle.unit)
        if state is None:
            return None
        if state.get("LoadState") == "not-found":
            return -1
        if state.get("ActiveState") in ("inactive", "failed"):
            try:
                return int(state.get("ExecMainStatus") or -1)
            except ValueError:
                return -1
    return None


def _exit_code_from_log(path: Path) -> int | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = _EXIT_MARKER_RE.findall(text)
    return int(found[-1]) if found else None


def _busy_phase(args: dict[str, Any]) -> str:
    try:
        paths = AppPaths(config_dir=Path(args["config_dir"]), data_dir=Path(args["data_dir"]))
        latest = files.read_latest_run(paths)
    except (KeyError, TypeError, ValueError, OSError):
        latest = None
    if latest is not None and latest[1].get("status") is None and isinstance(latest[1].get("phase"), str):
        return latest[1]["phase"]
    return "checking for updates"


def _map_exit(handle: RunnerHandle, code: int) -> UpdateLaunchError:
    """The runner ended without its result file: say why, from its exit code (or the code it printed)."""
    if code < 0:  # systemd already collected the unit: the runner printed its exit code into the log
        printed = _exit_code_from_log(handle.log_file)
        code = printed if printed is not None else code
    tail = log_tail(handle.log_file)
    args = files.read_json(handle.args_file) or {}
    computer, source_dir = args.get("computer"), args.get("source_dir")
    if code == _runner.EXIT_BUSY:
        return _error("update_busy", computer=computer, phase=_busy_phase(args), log_tail=tail)
    if code == _runner.EXIT_NO_BASE_PYTHON:
        return _error("update_no_base_python", path=os.fspath(_base_python_path()), source_dir=source_dir,
                      log_tail=tail)
    detail = {
        _runner.EXIT_BAD_ARGS: "it rejected its arguments",
        _runner.EXIT_INSIDE_UNIT: "it was started inside homemesh.service",
        _runner.EXIT_OK: "it ended without writing a result",
    }.get(code, f"it ended with exit code {code}" if code >= 0 else "it ended without writing a result")
    return _error("update_launch_failed", detail=detail, log=os.fspath(handle.log_file), log_tail=tail,
                  unit=handle.unit)


def _duration(seconds: float) -> str:
    if seconds >= 120:
        return f"{round(seconds / 60)} minutes"
    n = max(1, round(seconds))
    return f"{n} second" if n == 1 else f"{n} seconds"


def _check_result(handle: RunnerHandle, data: dict[str, Any]) -> UpdateCheckResult:
    try:
        return UpdateCheckResult.model_validate(data)
    except Exception as exc:
        raise _error("update_launch_failed", detail="the check result could not be read",
                     log=os.fspath(handle.log_file), log_tail=log_tail(handle.log_file), unit=handle.unit) from exc


def _check_step(handle: RunnerHandle) -> UpdateCheckResult | None:
    if handle.out_file is None:
        raise _error("update_launch_failed", detail="not a check runner")
    data = files.read_json(handle.out_file)
    if data is not None:
        return _check_result(handle, data)
    code = runner_exit_code(handle)
    if code is None:
        return None
    data = files.read_json(handle.out_file)  # written right before it exited
    if data is not None:
        return _check_result(handle, data)
    raise _map_exit(handle, code)


def _check_timeout(handle: RunnerHandle, timeout_s: float) -> UpdateLaunchError:
    return _error("update_launch_failed", detail=f"the check did not finish in {_duration(timeout_s)}",
                  log=os.fspath(handle.log_file), log_tail=log_tail(handle.log_file), unit=handle.unit)


async def wait_for_check(handle: RunnerHandle, *, timeout_s: float = CHECK_TIMEOUT_S,
                         poll_s: float = 0.5) -> UpdateCheckResult:
    """Wait for ``checks/<run_id>.json`` and return it validated (``UpdateCheckResult``; ``ok`` may be false,
    the caller maps ``result.error`` to ``JobFailed``). Raises :class:`UpdateLaunchError` if the runner
    exits without writing it (exit code mapped as in :func:`wait_for_accept`, plus the log tail), if the
    file doesn't validate, or on timeout (``update_launch_failed``, "the check did not finish in 5 minutes"). Does not kill the runner on
    timeout or cancellation; the caller calls :func:`kill_check` (also on ``asyncio.CancelledError``)."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = await asyncio.to_thread(_check_step, handle)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise _check_timeout(handle, timeout_s)
        await asyncio.sleep(poll_s)


def wait_for_check_sync(handle: RunnerHandle, *, timeout_s: float = CHECK_TIMEOUT_S,
                        poll_s: float = 0.5) -> UpdateCheckResult:
    """:func:`wait_for_check` for the CLI (blocking)."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = _check_step(handle)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise _check_timeout(handle, timeout_s)
        time.sleep(poll_s)


def _read_run(handle: RunnerHandle) -> dict[str, Any] | None:
    if handle.run_file is None:
        raise _error("update_launch_failed", detail="not an apply runner")
    run = files.read_json(handle.run_file)
    return run if run is not None and isinstance(run.get("phase"), str) else None


def _accept_step(handle: RunnerHandle) -> dict[str, Any] | None:
    run = _read_run(handle)
    if run is not None:
        return run
    code = runner_exit_code(handle)
    if code is None:
        return None
    run = _read_run(handle)
    if run is not None:
        return run
    raise _map_exit(handle, code)


def _accept_timeout(handle: RunnerHandle, timeout_s: float) -> dict[str, Any]:
    """Timed out: if run.json appeared at the last moment, it was accepted after all; otherwise stop the
    runner and everything it started (it hasn't touched anything yet) and fail."""
    run = _read_run(handle)
    if run is not None:
        return run
    _kill_runner(handle)
    raise _error("update_launch_failed",
                 detail=f"the update helper did not start within {_duration(timeout_s)} and was stopped; "
                        "nothing was changed",
                 log=os.fspath(handle.log_file), log_tail=log_tail(handle.log_file), unit=handle.unit)


async def wait_for_accept(handle: RunnerHandle, *, timeout_s: float = ACCEPT_TIMEOUT_S,
                          poll_s: float = 0.25) -> dict[str, object]:
    """Wait until ``runs/<run_id>/run.json`` exists with a ``phase`` (any phase is ≥ ``accepted``) and
    return it (a run.json that already carries a final status is returned as is; the run reports it).

    If the runner exits first, map its exit code (``runner.EXIT_*``): ``EXIT_BUSY`` → ``update_busy``,
    ``EXIT_NO_BASE_PYTHON`` → ``update_no_base_python``, anything else → ``update_launch_failed``. On
    ``timeout_s``: the runner (tree) is killed, then ``update_launch_failed``. Every
    :class:`UpdateLaunchError` carries the redacted log tail; on systemd the hint names
    ``journalctl --user -u 'homemesh-update-apply-<run_id>' -n 50``."""
    deadline = time.monotonic() + timeout_s
    while True:
        run = await asyncio.to_thread(_accept_step, handle)
        if run is not None:
            return run
        if time.monotonic() >= deadline:
            return await asyncio.to_thread(_accept_timeout, handle, timeout_s)
        await asyncio.sleep(poll_s)


def wait_for_accept_sync(handle: RunnerHandle, *, timeout_s: float = ACCEPT_TIMEOUT_S,
                         poll_s: float = 0.25) -> dict[str, object]:
    """:func:`wait_for_accept` for the CLI (blocking)."""
    deadline = time.monotonic() + timeout_s
    while True:
        run = _accept_step(handle)
        if run is not None:
            return run
        if time.monotonic() >= deadline:
            return _accept_timeout(handle, timeout_s)
        time.sleep(poll_s)


def _kill_tree(pid: int) -> None:
    """Kill a runner started as a session/group leader and everything below it."""
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)  # start_new_session: its group id is its pid
    try:
        parent = psutil.Process(pid)
        procs = [*parent.children(recursive=True), parent]
    except psutil.Error:
        return
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.kill()
    with contextlib.suppress(psutil.Error):
        psutil.wait_procs(procs, timeout=5)


def _kill_runner(handle: RunnerHandle) -> None:
    """Never raises: a runner that is already gone is the goal, not an error."""
    if handle.unit:
        with contextlib.suppress(Exception):
            run_quiet(["systemctl", "--user", "stop", f"{handle.unit}.service"], capture_output=True, check=False,
                      timeout=60, stdin=subprocess.DEVNULL)
    proc = handle.process
    if proc is not None:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                _kill_tree(proc.pid)
                proc.wait(timeout=10)


def kill_check(handle: RunnerHandle) -> None:
    """Stop a check runner and everything it started: systemd ``systemctl --user stop <unit>.service``;
    otherwise a psutil tree kill (children first, then the runner). Idempotent, never raises. Never used on
    an apply runner (an apply can't be cancelled)."""
    _kill_runner(handle)
