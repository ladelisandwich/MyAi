"""Files of the remote updater under ``<data>/update/`` and the ``args.json`` launcher → runner contract.

Layout (a private directory, created with :func:`ensure_update_dir`)::

    update.lock                 held by every check and apply runner for its whole life (update lock)
    latest.json                 {"v": 1, "run_id": "…"}  pointer to the newest apply run (never select by mtime)
    agent.json                  written by the worker agent on every connection state change (AgentFile)
    runs/<run_id>/args.json     launcher → apply runner (RunnerArgs)
    runs/<run_id>/run.json      single writer: the runner, or the agent finalizing a dead run while holding
                                update.lock (RunFile)
    runs/<run_id>/drain.json    agent → runner while the run is in phase "draining" (DrainFile)
    runs/<run_id>/pre-update.sqlite3, homemesh.toml.bak, worker-credentials.json.bak   (runner, only when needed)
    runs/<run_id>/cmd-<n>.out   captured output of one runner command (read after it exits, then kept)
    checks/<run_id>.args.json   launcher → check runner (RunnerArgs)
    checks/<run_id>.json        check runner → worker (protocol.update.UpdateCheckResult)

Logs: ``<logs>/update-<run_id>.log`` (apply) and ``<logs>/update-check-<run_id>.log`` (check), owner-only.

Every file here is a cross-version contract (docs/UPDATING.md "File contracts"): the runner that executes is
the *old* version's, the process it starts is the new one. Fields are only ever added (``"v": 1``); readers
ignore unknown fields. ``runner.py`` is stdlib-only and can't import this module, so it carries its own copy
of these rules; ``tests/unit/test_update_contracts.py`` keeps the two in agreement.

Writes are atomic (temp file in the same directory, fsync, ``os.replace`` retried for up to 2 s on Windows
``PermissionError``, then fsync of the directory on POSIX), owner-only (0600 on POSIX), and refuse a
destination that is a symlink. Reads are tolerant: a missing, partial, oversized or undecodable file reads
as ``None`` ("unknown, read again later").
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import EllipsisType
from typing import IO, Any, Literal, TypedDict

import psutil

from homemesh.common.instance import InstanceLock, lock_is_held
from homemesh.common.paths import AppPaths, ensure_private_dir
from homemesh.common.timeutil import now_iso
from homemesh.protocol.update import (
    AGENT_DETAIL_MAX,
    UpdateOrigin,
    UpdateRunReport,
    clean_line,
    format_update_error,
    is_run_id,
    is_sha,
    short_sha,
)

RunnerMode = Literal["check", "apply"]
ServiceKind = Literal["systemd", "windows", "posix"]
AgentState = Literal["connecting", "connected", "rejected", "unreachable"]

MAX_FILE_BYTES = 1024 * 1024  # tolerant reads refuse anything larger
REPLACE_RETRY_S = 2.0
KEEP_RUNS = 5
KEEP_CHECKS = 5

# Exact names the runner may create inside a run directory (pruning deletes only these and the patterns).
RUN_DIR_FILES = frozenset({"args.json", "run.json", "drain.json", "pre-update.sqlite3", "pre-update.sqlite3-journal",
                           "homemesh.toml.bak", "worker-credentials.json.bak"})
SNAPSHOT_FILES = frozenset({"pre-update.sqlite3", "pre-update.sqlite3-journal", "homemesh.toml.bak",
                            "worker-credentials.json.bak"})
# Runs that ended with one of these error codes are never pruned: their snapshot is the owner's only copy of
# the pre-update database (the hint of update_db_restore_failed names it). The owner deletes them by hand.
KEEP_RUN_ERROR_CODES = frozenset({"update_db_restore_failed"})
CAPTURE_FILE_RE = re.compile(r"cmd-[0-9]{1,6}\.out")  # runner command output captures
TEMP_FILE_RE = re.compile(r"\.[A-Za-z0-9._-]{1,200}\.tmp")  # atomic-write temp files: .<name>.<random>.tmp
_CHECK_FILE_RE = re.compile(r"(u[0-9]{14}-[0-9a-f]{8})(\.args)?\.json")


# --------------------------------------------------------------------------- layout

def update_dir(paths: AppPaths) -> Path:
    return paths.data_dir / "update"


def ensure_update_dir(paths: AppPaths) -> Path:
    """Create ``<data>/update`` (private), ``runs/``, ``checks/`` and the logs folder. Returns update_dir."""
    base = update_dir(paths)
    ensure_private_dir(base)
    (base / "runs").mkdir(exist_ok=True)
    (base / "checks").mkdir(exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    return base


def lock_file(paths: AppPaths) -> Path:
    return update_dir(paths) / "update.lock"


def latest_file(paths: AppPaths) -> Path:
    return update_dir(paths) / "latest.json"


def agent_file(paths: AppPaths) -> Path:
    return update_dir(paths) / "agent.json"


def runs_dir(paths: AppPaths) -> Path:
    return update_dir(paths) / "runs"


def checks_dir(paths: AppPaths) -> Path:
    return update_dir(paths) / "checks"


def _checked(run_id: str) -> str:
    if not is_run_id(run_id):
        raise ValueError(f"invalid update run id: {run_id!r}")
    return run_id


def run_dir(paths: AppPaths, run_id: str) -> Path:
    return runs_dir(paths) / _checked(run_id)


def run_file(paths: AppPaths, run_id: str) -> Path:
    return run_dir(paths, run_id) / "run.json"


def args_file(paths: AppPaths, run_id: str) -> Path:
    return run_dir(paths, run_id) / "args.json"


def drain_file(paths: AppPaths, run_id: str) -> Path:
    return run_dir(paths, run_id) / "drain.json"


def check_args_file(paths: AppPaths, run_id: str) -> Path:
    return checks_dir(paths) / f"{_checked(run_id)}.args.json"


def check_result_file(paths: AppPaths, run_id: str) -> Path:
    return checks_dir(paths) / f"{_checked(run_id)}.json"


def apply_log_file(paths: AppPaths, run_id: str) -> Path:
    return paths.logs_dir / f"update-{_checked(run_id)}.log"


def check_log_file(paths: AppPaths, run_id: str) -> Path:
    return paths.logs_dir / f"update-check-{_checked(run_id)}.log"


def new_run_id(now: datetime | None = None) -> str:
    """``u`` + UTC ``yyyymmddHHMMSS`` + ``-`` + 8 random hex digits. Ids sort chronologically."""
    now = now or datetime.now(UTC)
    return f"u{now.astimezone(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(4)}"


# --------------------------------------------------------------------------- atomic, owner-only writes

def _refuse_symlink(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st):
        raise OSError(errno.ELOOP, "refusing to write through a link", str(path))


def _is_reparse_point(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)) if sys.platform == "win32" else False


def _replace_with_retry(src: str, dst: Path) -> None:
    """``os.replace``, retried for up to 2 s: on Windows a reader holding the file open blocks it briefly."""
    deadline = time.monotonic() + REPLACE_RETRY_S
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically and durably; the file is owner-only (0600 on POSIX).

    Raises ``OSError`` (``ELOOP``) if ``path`` is a symlink. The parent directory must exist.
    """
    path = Path(path)
    _refuse_symlink(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)  # O_EXCL, 0600
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any) -> None:
    """:func:`atomic_write_bytes` of ``obj`` as indented UTF-8 JSON (no NaN)."""
    data = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_write_bytes(path, data.encode("utf-8"))


def open_private_append(path: Path) -> IO[bytes]:
    """Open a log file for appending, creating it owner-only (0600 on POSIX). Refuses symlinks."""
    path = Path(path)
    _refuse_symlink(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab")


# --------------------------------------------------------------------------- tolerant reads

def read_json(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> dict[str, Any] | None:
    """A JSON object from ``path``, or None if missing, unreadable, too large, undecodable or not an object.

    Opens, reads and closes at once (Windows: never hold a file the runner wants to replace).
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- update lock

def update_lock(paths: AppPaths) -> InstanceLock:
    """The update lock (same OS primitive as the instance lock: flock / msvcrt.locking on byte 0).

    Call ``acquire()`` (non-blocking) and ``release()``; only the agent's dead-run finalizer takes it on the
    HomeMesh side. The runner holds it for its whole life.
    """
    return InstanceLock(lock_file(paths))


def update_lock_held(paths: AppPaths) -> bool:
    """Is a check or apply runner alive right now? Probes without disturbing the holder."""
    lf = lock_file(paths)
    if not lf.exists():
        return False
    return lock_is_held(lf)


# --------------------------------------------------------------------------- latest pointer, runs, reports

class LatestFile(TypedDict):
    v: int  # 1
    run_id: str


def read_latest_run_id(paths: AppPaths) -> str | None:
    data = read_json(latest_file(paths))
    if not data:
        return None
    run_id = data.get("run_id")
    return run_id if is_run_id(run_id) else None


def write_latest(paths: AppPaths, run_id: str) -> None:
    """Point ``latest.json`` at ``run_id`` (the runner does this itself; used by tests and tools)."""
    atomic_write_json(latest_file(paths), LatestFile(v=1, run_id=_checked(run_id)))


def read_run(paths: AppPaths, run_id: str) -> dict[str, Any] | None:
    return read_json(run_file(paths, run_id))


def read_latest_run(paths: AppPaths) -> tuple[str, dict[str, Any]] | None:
    """``(run_id, run.json)`` of the run ``latest.json`` names, or None."""
    run_id = read_latest_run_id(paths)
    if run_id is None:
        return None
    run = read_run(paths, run_id)
    return (run_id, run) if run is not None else None


def report_from_run(run: dict[str, Any]) -> UpdateRunReport | None:
    """The report subset of a ``run.json`` dict, or None if it doesn't validate (never raises)."""
    try:
        return UpdateRunReport.model_validate(run)
    except Exception:
        return None


def read_latest_report(paths: AppPaths) -> dict[str, Any] | None:
    """The latest run as the ``Hello.update`` / ``Heartbeat.update`` dict (size-capped), or None.

    Cheap enough for the agent's 2-second watch loop (two small file reads).
    """
    latest = read_latest_run(paths)
    if latest is None:
        return None
    report = report_from_run(latest[1])
    return report.to_wire() if report is not None else None


def update_in_progress(paths: AppPaths) -> dict[str, Any] | None:
    """``{"run_id", "phase", "log"}`` while an apply runner holds the update lock and its run has no final
    status, else None. ``homemesh status/start/run`` print "An update is in progress (run <id>, <phase>;
    log <path>)" instead of "not running" / "already running" when this is set."""
    latest = read_latest_run(paths)
    if latest is None or latest[1].get("status") is not None:
        return None
    if not update_lock_held(paths):
        return None
    run_id, run = latest
    return {"run_id": run_id, "phase": run.get("phase"), "log": run.get("log")}


def finalize_dead_run(paths: AppPaths, *, installed_commit: str | None,
                      computer: str | None = None) -> dict[str, Any] | None:
    """If the latest run has no final status and no runner holds the update lock, the runner is gone
    (killed, logoff, power loss): mark the run ``interrupted`` and return the new run.json; else None.

    Called by the agent's watch loop with ``installed_commit = build_info().commit``. Holds the update lock
    while it writes, so it can't race a runner (the runner takes the lock before writing latest.json and
    writes its final status before releasing it).
    """
    latest = read_latest_run(paths)
    if latest is None or latest[1].get("status") is not None:
        return None
    lock = update_lock(paths)
    if not lock.acquire():
        return None
    try:
        run_id = latest[0]
        run = read_run(paths, run_id)
        if run is None or run.get("status") is not None:
            return None
        now = now_iso()
        commit = installed_commit if is_sha(installed_commit) else None
        err = format_update_error("update_interrupted", phase=run.get("phase"), computer=computer,
                                  commit=short_sha(commit))
        run.update(status="interrupted", error=err.model_dump(), installed_commit=commit, finished_at=now,
                   updated_at=now)
        atomic_write_json(run_file(paths, run_id), run)
        return run
    finally:
        lock.release()


# --------------------------------------------------------------------------- agent.json and drain.json

class AgentFile(TypedDict):
    """``agent.json``: the worker agent's connection state; the runner's proof that a new version works."""

    v: int  # 1
    pid: int  # os.getpid() of the HomeMesh process
    process_started: float | None  # psutil create_time() of that process (PID reuse guard)
    build_commit: str | None  # build_info().commit of the running code
    state: AgentState
    detail: str  # <= 300 chars, e.g. the close reason
    at: str  # ISO timestamp


def write_agent_file(paths: AppPaths, *, state: AgentState, build_commit: str | None, detail: str = "",
                     pid: int | None = None, process_started: float | None = None) -> None:
    """Write ``agent.json`` atomically (best effort is the caller's choice: this raises OSError)."""
    pid = pid or os.getpid()
    if process_started is None:
        try:
            process_started = psutil.Process(pid).create_time()
        except (psutil.Error, OSError):
            process_started = None
    if not update_dir(paths).is_dir():
        ensure_update_dir(paths)
    atomic_write_json(agent_file(paths), AgentFile(
        v=1, pid=pid, process_started=process_started, build_commit=build_commit, state=state,
        detail=clean_line(detail or "", AGENT_DETAIL_MAX), at=now_iso()))


def read_agent_file(paths: AppPaths) -> dict[str, Any] | None:
    return read_json(agent_file(paths))


class DrainJob(TypedDict):
    job_id: str
    type: str


class DrainFile(TypedDict):
    """``runs/<run_id>/drain.json``: written by the agent every 2 s while the run is in phase "draining"."""

    v: int  # 1
    pid: int
    running: list[DrainJob]  # jobs still running, update jobs excluded
    at: str  # ISO timestamp; the runner ignores a file older than the start of its draining phase


def write_drain_file(paths: AppPaths, run_id: str, running: Sequence[DrainJob]) -> None:
    atomic_write_json(drain_file(paths, run_id), DrainFile(
        v=1, pid=os.getpid(), running=[DrainJob(job_id=j["job_id"], type=j["type"]) for j in running],
        at=now_iso()))


def read_drain_file(paths: AppPaths, run_id: str) -> dict[str, Any] | None:
    return read_json(drain_file(paths, run_id))


def read_check_result(paths: AppPaths, run_id: str) -> dict[str, Any] | None:
    """Raw ``checks/<run_id>.json`` (validate with ``protocol.update.UpdateCheckResult``)."""
    return read_json(check_result_file(paths, run_id))


class RunFile(TypedDict, total=False):
    """``runs/<run_id>/run.json``, written by the apply runner before each action (design section 11).

    ``protocol.update.UpdateRunReport`` is the subset sent to the hub. ``status`` is null until final:
    ok | ok_unconfirmed | already_up_to_date | rolled_back | failed | interrupted.
    """

    v: int  # 1
    run_id: str
    job_id: str | None  # the update.apply job id; None for a local `homemesh update`
    origin: UpdateOrigin
    expected_commit: str
    force: bool
    phase: str  # protocol.update.UpdatePhase
    status: str | None
    committed: bool  # from phase "stopping" on: every later failure ends with some HomeMesh started
    switched: bool  # the venv link points at to_venv
    from_commit: str | None
    to_commit: str | None
    from_venv: str | None
    to_venv: str | None
    was_running: bool | None
    drain_deadline: str | None
    db_snapshot: str | None
    pre_schema: int | None
    runner_pid: int
    started_at: str
    updated_at: str
    finished_at: str | None
    error: dict[str, Any] | None  # {code, message, hint}; the first error stays the run's error
    warnings: list[dict[str, Any]]  # [{code, message, hint}] <= 10
    log: str  # absolute path of the run log
    installed_commit: str | None  # written by the agent's finalizer (and may be written by the runner)


# --------------------------------------------------------------------------- pruning

def _is_real_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not _is_reparse_point(st)


def _is_real_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _unlink_file(path: Path) -> bool:
    """Delete a regular file (never a link target, never a directory). True if it was deleted."""
    if not _is_real_file(path):
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def _list_names(path: Path) -> list[str]:
    with os.scandir(path) as it:  # closed before any rmdir (an open handle blocks it on Windows)
        return [e.name for e in it]


def _remove_run_dir(path: Path) -> bool:
    try:
        names = _list_names(path)
    except OSError:
        return False
    for name in names:
        if name in RUN_DIR_FILES or CAPTURE_FILE_RE.fullmatch(name) or TEMP_FILE_RE.fullmatch(name):
            _unlink_file(path / name)
    try:
        os.rmdir(path)  # fails, harmlessly, if anything unexpected is left
    except OSError:
        return False
    return True


def prune_runs(paths: AppPaths, keep: int = KEEP_RUNS) -> list[str]:
    """Keep the newest ``keep`` run directories (by run id, i.e. by start time; never by mtime), delete the
    snapshot and backups from all but the newest, and delete each pruned run's log. Never touches the run
    ``latest.json`` names, a run without a final status, or a run that ended with an error code in
    :data:`KEEP_RUN_ERROR_CODES` (``update_db_restore_failed``: its snapshot is the only pre-update copy).
    Only exact generated names are deleted, after ``lstat`` checks (links and junctions are skipped). Best
    effort; returns the pruned run ids.

    The apply runner prunes with the same rules in its cleanup phase (it can't import this module).
    """
    base = runs_dir(paths)
    try:
        names = sorted(n for n in _list_names(base) if is_run_id(n) and _is_real_dir(base / n))
    except OSError:
        return []
    if not names:
        return []
    protected = {read_latest_run_id(paths)}
    for name in names:
        run = read_json(base / name / "run.json")
        if run is not None and (run.get("status") is None or _error_code(run) in KEEP_RUN_ERROR_CODES):
            protected.add(name)
    newest = names[-1]
    removed: list[str] = []
    for name in names[:-keep] if keep > 0 else names:
        if name in protected:
            continue
        if _remove_run_dir(base / name):
            removed.append(name)
            _unlink_file(apply_log_file(paths, name))
    for name in names:
        if name != newest and name not in removed and name not in protected and _is_real_dir(base / name):
            for snap in SNAPSHOT_FILES:
                _unlink_file(base / name / snap)
    return removed


def _error_code(run: dict[str, Any]) -> str | None:
    err = run.get("error")
    return err.get("code") if isinstance(err, dict) else None


def prune_checks(paths: AppPaths, keep: int = KEEP_CHECKS) -> list[str]:
    """Keep the files of the newest ``keep`` checks in ``checks/`` (and their logs); delete the rest.

    The check runner doesn't prune; the worker calls this after each check it ran. Returns pruned run ids.
    """
    base = checks_dir(paths)
    by_id: dict[str, list[str]] = {}
    try:
        names = _list_names(base)
    except OSError:
        return []
    for name in names:  # anything else (e.g. an in-flight atomic write's temp file) is left alone
        m = _CHECK_FILE_RE.fullmatch(name)
        if m:
            by_id.setdefault(m.group(1), []).append(name)
    ids = sorted(by_id)
    removed: list[str] = []
    for run_id in ids[:-keep] if keep > 0 else ids:
        for name in by_id[run_id]:
            _unlink_file(base / name)
        _unlink_file(check_log_file(paths, run_id))
        removed.append(run_id)
    return removed


# --------------------------------------------------------------------------- args.json (launcher → runner)

class RunnerArgs(TypedDict):
    """``args.json``, written owner-only by ``update/launch.py`` and validated **field by field** by
    ``runner.py`` before it does anything (a bad file: exit 2, write nothing). Same version on both sides
    (the runner runs from the venv that wrote it), but treated as untrusted input anyway.

    Rules the runner enforces: ``v == 1``; ``mode`` matches the command line; ``run_id`` matches
    ``protocol.update.RUN_ID_PATTERN``; every path is absolute; ``source_dir`` contains ``.git``; ``git`` is
    an existing executable file; ``node_dir`` (if set) is an existing directory; ``expected_commit`` is a
    full sha for apply and null for check; ``out`` is set for check and null for apply; ``roles`` ⊆
    {coordinator, worker, client}; ``service`` is one of systemd | windows | posix (``unit_path`` set iff
    systemd). Unknown keys are ignored.
    """

    v: int  # 1
    mode: RunnerMode
    run_id: str
    job_id: str | None  # update.apply / update.check job id; None for the local CLI
    origin: UpdateOrigin  # remote: consent marker re-checked before the fetch and before stopping HomeMesh
    expected_commit: str | None  # apply: full sha the owner saw in the check; check: None
    force: bool  # apply: skip draining; check: False
    computer: str  # node name, used in messages ("Remote updates are off on <computer>.")
    source_dir: str  # the git checkout this install was built from (record.source_dir)
    prefix: str  # dirname(stable_venv): holds venvs/ and b/
    stable_venv: str  # <prefix>/venv, the symlink/junction path itself (not resolved)
    current_venv: str  # realpath(sys.prefix): the venv this HomeMesh runs from (holds the install record)
    current_package_dir: str  # realpath of the running homemesh package: web_dist/, coordinator/db/migrations/
    data_dir: str
    config_dir: str
    logs_dir: str
    update_dir: str  # <data>/update
    config_file: str  # homemesh.toml (backed up before switching)
    db_file: str | None  # coordinator role only
    worker_credentials_file: str  # secrets/worker-credentials.json (backed up before switching)
    pid_file: str  # PID record {pid, started, ready}
    instance_lock: str  # <data>/homemesh.lock
    stop_request_file: str  # <data>/homemesh.stop-request
    consent_file: str  # the remote-update consent marker (only update/consent.py knows its name)
    roles: list[str]
    service: ServiceKind
    unit_path: str | None  # systemd unit file, when service == "systemd"
    git: str  # absolute path of the git executable
    node_dir: str | None  # directory holding node / node.exe, if known
    homemesh_home: str | None  # HOMEMESH_HOME (absolute), set for HomeMesh processes the runner starts
    log: str  # this run's log file
    out: str | None  # check: checks/<run_id>.json; apply: None


RUNNER_ARGS_KEYS: tuple[str, ...] = tuple(RunnerArgs.__annotations__)
_ROLES = frozenset({"coordinator", "worker", "client"})


def _abs(value: str | os.PathLike[str]) -> str:
    p = Path(value)
    if not p.is_absolute():
        raise ValueError(f"path must be absolute: {value}")
    return os.fspath(p)


def build_runner_args(
    paths: AppPaths, *, mode: RunnerMode, run_id: str, computer: str, roles: Sequence[str],
    source_dir: str | os.PathLike[str], stable_venv: str | os.PathLike[str], git: str | os.PathLike[str],
    node_dir: str | os.PathLike[str] | None, service: ServiceKind, unit_path: str | os.PathLike[str] | None = None,
    job_id: str | None = None, origin: UpdateOrigin = "local", expected_commit: str | None = None,
    force: bool = False, current_venv: str | os.PathLike[str] | None = None,
    current_package_dir: str | os.PathLike[str] | None = None,
    homemesh_home: str | EllipsisType | None = ..., consent_file: str | os.PathLike[str] | None = None,
) -> RunnerArgs:
    """Assemble ``args.json`` for one runner (no I/O except resolving the current venv/package).

    ``current_venv`` defaults to ``realpath(sys.prefix)``, ``current_package_dir`` to the resolved
    ``homemesh`` package, ``homemesh_home`` to ``$HOMEMESH_HOME`` made absolute exactly as
    ``common.paths.default_paths`` does (the runner starts HomeMesh from another working directory, so a
    relative value would point it at another data folder), ``consent_file`` to
    ``consent.marker_path(paths)``. ``log`` and ``out`` are derived from the mode and run id. Raises
    ``ValueError`` for inconsistent arguments (programming errors, not user errors).
    """
    if mode not in ("check", "apply"):
        raise ValueError(f"invalid runner mode: {mode}")
    _checked(run_id)
    if origin not in ("remote", "local"):
        raise ValueError(f"invalid origin: {origin}")
    if mode == "apply":
        if not is_sha(expected_commit):
            raise ValueError("apply needs a full expected_commit sha")
    elif expected_commit is not None or force:
        raise ValueError("a check takes no expected_commit and no force")
    if not set(roles) <= _ROLES or not roles:
        raise ValueError(f"invalid roles: {roles}")
    if service not in ("systemd", "windows", "posix"):
        raise ValueError(f"invalid service kind: {service}")
    if (service == "systemd") != (unit_path is not None):
        raise ValueError("unit_path is required with service 'systemd' and only then")
    if current_venv is None:
        current_venv = os.path.realpath(sys.prefix)
    if current_package_dir is None:
        import homemesh

        current_package_dir = Path(homemesh.__file__).resolve().parent
    if homemesh_home is ...:
        env_home = os.environ.get("HOMEMESH_HOME")
        homemesh_home = os.fspath(Path(env_home).expanduser().resolve()) if env_home else None
    if consent_file is None:
        from homemesh.update import consent  # consent imports this module; import late

        consent_file = consent.marker_path(paths)
    stable = Path(_abs(stable_venv))
    has_coordinator = "coordinator" in roles
    log = apply_log_file(paths, run_id) if mode == "apply" else check_log_file(paths, run_id)
    return RunnerArgs(
        v=1, mode=mode, run_id=run_id, job_id=job_id, origin=origin, expected_commit=expected_commit,
        force=bool(force), computer=computer, source_dir=_abs(source_dir), prefix=os.fspath(stable.parent),
        stable_venv=os.fspath(stable), current_venv=_abs(current_venv),
        current_package_dir=_abs(current_package_dir), data_dir=_abs(paths.data_dir),
        config_dir=_abs(paths.config_dir), logs_dir=_abs(paths.logs_dir), update_dir=_abs(update_dir(paths)),
        config_file=_abs(paths.config_file), db_file=_abs(paths.db_file) if has_coordinator else None,
        worker_credentials_file=_abs(paths.worker_credentials_file), pid_file=_abs(paths.pid_file),
        instance_lock=_abs(paths.lock_file), stop_request_file=_abs(paths.stop_request_file),
        consent_file=_abs(consent_file), roles=list(roles), service=service,
        unit_path=_abs(unit_path) if unit_path is not None else None, git=_abs(git),
        node_dir=_abs(node_dir) if node_dir is not None else None,
        homemesh_home=_abs(homemesh_home) if homemesh_home is not None else None,
        log=_abs(log), out=_abs(check_result_file(paths, run_id)) if mode == "check" else None,
    )


def runner_args_path(paths: AppPaths, args: RunnerArgs) -> Path:
    """Where ``args`` goes: ``runs/<run_id>/args.json`` (apply) or ``checks/<run_id>.args.json`` (check)."""
    if args["mode"] == "apply":
        return args_file(paths, args["run_id"])
    return check_args_file(paths, args["run_id"])


def write_runner_args(paths: AppPaths, args: RunnerArgs) -> Path:
    """Create the update directories and write ``args`` owner-only. Returns the file path.

    For apply this creates ``runs/<run_id>/`` (the runner writes run.json there once it accepted the run).
    """
    ensure_update_dir(paths)
    target = runner_args_path(paths, args)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, dict(args))
    return target
