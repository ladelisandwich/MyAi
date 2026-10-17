"""HomeMesh update runner: all git, npm, pip and venv work of an update, outside the HomeMesh process.

Invocation (``update/launch.py``)::

    <base python> -I -S -X utf8 runner.py {check|apply} --args <args.json>

Rules for this file:

* **Standard library only, one file.** It runs with ``-I -S`` from the base interpreter, so it can't import
  ``homemesh`` or a sibling module (``tests/unit/test_update_runner_stdlib.py`` enforces it). Whatever it
  shares with HomeMesh is duplicated here and kept in agreement by ``tests/unit/test_update_contracts.py``:
  the ``args.json`` fields and rules (``homemesh.update.files.RunnerArgs``), the file layout and atomic
  writes of ``homemesh.update.files``, ``run.json`` / ``latest.json`` / ``checks/<run_id>.json``
  (``homemesh.protocol.update.UpdateRunReport`` / ``UpdateCheckResult``), the install record
  (``homemesh.update.record.InstallRecord``), the patterns and the redactor/sanitizers of
  ``homemesh.protocol.update`` (``SHA_PATTERN``, ``RUN_ID_PATTERN``, ``VENV_ID_PATTERN``,
  ``REF_NAME_PATTERN``, ``redact``, ``clean_line``, ``clean_text``), the error codes and message/hint texts
  of ``UPDATE_ERRORS`` / ``UPDATE_WARNINGS``, and the lock primitive of ``common.instance.InstanceLock``.
* It is the **old** version's code that runs; the process it starts is the new version. Every file it
  shares with HomeMesh only grows additively (``"v": 1``, unknown fields ignored).
* It refuses a venv interpreter (:func:`running_from_venv`; exit :data:`EXIT_NO_BASE_PYTHON`) and refuses to run
  inside ``homemesh.service``'s cgroup (exit :data:`EXIT_INSIDE_UNIT`).
* Captured command output goes to ``runs/<run_id>/cmd-<n>.out`` (names pruning recognises; rewritten
  redacted once read), never pipes. The run log only ever receives redacted text.

How an apply run goes (design section 11; ``run.json`` is written before each action, rule 5):
``accepted`` → ``prechecks`` → ``building`` (HomeMesh keeps running; a failure deletes the new venv and
changes nothing else) → ``draining`` → ``stopping`` (from here the run is *committed*: every later failure
ends with some HomeMesh started) → ``switching`` (flip the ``venv`` link) → ``starting`` → ``verifying``
→ ``cleanup``; or ``rolling_back`` (flip back, restore the database through the backup API).

Everything the runner does to the outside world goes through :class:`Hooks` (command table, clock, sleep,
process spawning, timeouts), so tests can run the whole apply flow on Linux without systemd, pip or the
network: ``run_apply(args, hooks)``. In production ``main()`` passes no hooks.

Exit codes are the ``EXIT_*`` constants below; ``launch.wait_for_accept`` / ``wait_for_check`` map them to
error codes when the runner exits without a result file (the runner also writes ``EXIT_MARKER <code>`` into
its log, because systemd forgets a collected unit's exit status).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

EXIT_OK = 0  # finished; the outcome (any final status, or a check result) is in the result file
EXIT_FAILED = 1  # unexpected crash after the result file existed; the file says what happened
EXIT_BAD_ARGS = 2  # command line or args.json invalid: nothing was written
EXIT_BUSY = 3  # the update lock is held by another runner (update_busy): nothing was written
EXIT_INSIDE_UNIT = 4  # started inside homemesh.service's cgroup (update_launch_failed): nothing was written
EXIT_NO_BASE_PYTHON = 5  # not the base interpreter (update_no_base_python): nothing was written

MODES = ("check", "apply")

# Written into the runner log (stderr only when the log can't be written) on every early exit, at the start
# of a line or after the log's timestamp; launch.py reads it when systemd already garbage-collected the unit
# (``--collect``) and its exit status is gone.
EXIT_MARKER = "homemesh-update-runner-exit"


class ArgsError(Exception):
    """``args.json`` failed validation; the message names the field."""


# =========================================================================== shared vocabulary (copies)
# Kept identical to homemesh.protocol.update by tests/unit/test_update_contracts.py.

_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_RUN_ID_RE = re.compile(r"u[0-9]{14}-[0-9a-f]{8}")
_VENV_ID_RE = re.compile(r"(?:[0-9a-f]{12}|nogit|legacy)-[0-9]{14}")
_REF_NAME_RE = re.compile(r"[A-Za-z0-9._/-]+")
_BUILD_DIR_RE = re.compile(r"[0-9a-f]{8}")


def is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def is_run_id(value: object) -> bool:
    return isinstance(value, str) and _RUN_ID_RE.fullmatch(value) is not None


def is_venv_id(value: object) -> bool:
    return isinstance(value, str) and _VENV_ID_RE.fullmatch(value) is not None


def is_ref_name(value: object) -> bool:
    """A remote or branch name the runner may pass to git (after ``--end-of-options``)."""
    return (isinstance(value, str) and _REF_NAME_RE.fullmatch(value) is not None
            and not value.startswith(("-", ".", "/")) and not value.endswith(("/", ".", ".lock"))
            and ".." not in value and "//" not in value)


def short_sha(sha: str | None) -> str | None:
    return sha[:7] if isinstance(sha, str) and sha else None


CODE_MAX = 64
MESSAGE_MAX = 500
HINT_MAX = 500
SUBJECT_MAX = 200
NAME_MAX = 200
PATH_MAX = 260
TIME_MAX = 40
ID_MAX = 100
MAX_WARNINGS = 10
MAX_PROBLEMS = 20
MAX_COMMITS = 50
MAX_DIRTY_FILES = 50

_C0C1 = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_C0C1_KEEP_NL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_URL_USERINFO = re.compile(r"://[^/@\s]+@")
_GH_TOKEN = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")


def redact(text: str) -> str:
    """Remove credentials from tool output: URL userinfo and GitHub tokens."""
    return _GH_TOKEN.sub("***", _URL_USERINFO.sub("://***@", text))


def clean_line(text: str, limit: int) -> str:
    """One line of untrusted text: C0/C1 control characters removed (plain space kept), capped."""
    return _C0C1.sub("", text)[:limit]


def clean_text(text: str, limit: int) -> str:
    """Multi-line untrusted text: control characters except tab and newline removed, capped."""
    return _C0C1_KEEP_NL.sub("", text)[:limit]


PHASES = ("accepted", "prechecks", "building", "draining", "stopping", "switching", "starting", "verifying",
          "rolling_back", "cleanup")
FINAL_STATUSES = frozenset({"ok", "ok_unconfirmed", "already_up_to_date", "rolled_back", "failed", "interrupted"})
PHASE_LABELS = {
    "accepted": "starting the update",
    "prechecks": "checking",
    "building": "building the new version",
    "draining": "waiting for running jobs",
    "stopping": "stopping HomeMesh",
    "switching": "switching versions",
    "starting": "starting the new version",
    "verifying": "verifying the new version",
    "rolling_back": "undoing the update",
    "cleanup": "cleaning up",
}

# The codes this runner writes (copied verbatim from protocol.update.UPDATE_ERRORS / UPDATE_WARNINGS).
ERRORS: dict[str, tuple[str, str]] = {
    "update_not_allowed": (
        "Remote updates are off on {computer}.",
        "On {computer}, run `homemesh update allow` (takes effect immediately)."),
    "update_git_missing": (
        "git was not found or is older than 2.24.",
        "Install git (Windows: `winget install Git.Git`) and run the installer once so its location is "
        "recorded."),
    "update_launch_failed": (
        "The update helper could not be started: {detail}.",
        "See {log} (Linux: `journalctl --user -u 'homemesh-update-*' -n 50`)."),
    "update_not_git_checkout": (
        "{source_dir} is not a git checkout.",
        "Install from a git clone (INSTALLATION.md) to use updates."),
    "update_detached_head": (
        "{source_dir} is not on a branch.",
        "`git -C {source_dir} switch <branch>`"),
    "update_no_upstream": (
        "Branch {branch} has no upstream.",
        "`git -C {source_dir} branch --set-upstream-to=origin/{branch}`"),
    "update_bad_ref_name": (
        'Remote or branch name "{name}" is not accepted.',
        "Use letters, digits, `.`, `_`, `/`, `-`."),
    "update_dirty_checkout": (
        "{source_dir} has local changes: {files}.",
        "Commit or discard them (`git -C {source_dir} status`), then check again."),
    "update_git_locked": (
        "Another git command is running in {source_dir}, or one crashed and left `.git/index.lock`.",
        "Close other git programs; if none runs, delete that file."),
    "update_unsafe_repository": (
        "git refuses {source_dir} because another user owns it.",
        "If you trust it: `git config --global --add safe.directory {source_dir}`"),
    "update_auth_failed": (
        "Signing in to the git server failed or is needed.",
        "On {computer} run `git -C {source_dir} fetch` once in a terminal (Linux: `gh auth setup-git`; "
        "Windows: sign in when Git Credential Manager asks)."),
    "update_fetch_failed": (
        "Could not fetch from {remote}: {detail}.",
        "Check the network on {computer}; nothing was changed."),
    "update_upstream_changed": (
        "The branch moved since the check (now {short}).",
        "Check again and review the new changes."),
    "update_diverged": (
        "{source_dir} has commits that aren't on {upstream_ref}; no fast-forward possible.",
        "`git -C {source_dir} log {upstream_ref}..HEAD`; push or reset them."),
    "update_low_disk": (
        "Not enough free disk space ({free} free, {need} needed).",
        "Free space on {volume}."),
    "update_needs_installer": (
        "This update needs the installer to run once by hand.",
        "{installer_command}"),
    "update_node_missing": (
        "The web app changed and Node.js was not found on {computer}.",
        "Install Node.js 20.19+ and run the installer once so its folder is recorded."),
    "update_web_build_failed": (
        "Building the web app failed: {lines}.",
        "Nothing was changed. See {log}."),
    "update_venv_failed": (
        "Creating the new program folder failed: {detail}.",
        "Nothing was changed. See {log}."),
    "update_install_failed": (
        "Installing the new version failed: {lines}.",
        "Nothing was changed. pip needs PyPI; check the network. See {log}."),
    "update_config_incompatible": (
        "The new version rejects this computer's configuration: {lines}.",
        "Nothing was changed. Fix homemesh.toml (`homemesh config validate`)."),
    "update_smoke_failed": (
        "The new version doesn't run: {detail}.",
        "Nothing was changed. See {log}."),
    "update_jobs_running": (
        "{n} job(s) still ran after 2 minutes ({types}); nothing was changed.",
        'Try later, or choose "Update anyway" (they will be stopped).'),
    "update_stop_failed": (
        "HomeMesh did not stop; nothing was changed.",
        "It keeps running the old version. See {log}."),
    "update_switch_failed": (
        "Switching to the new version failed: {detail}.",
        "HomeMesh was started again with the old version."),
    "update_db_snapshot_failed": (
        "Could not copy the database before the update: {detail}.",
        "HomeMesh was started again with the old version; check free space."),
    "update_start_timeout": (
        "The new version did not become ready in {n} minutes.",
        "It was undone (see the status). See {log}."),
    "update_new_version_crashed": (
        "The new version stopped right after starting ({exit}).",
        "It was undone. See {log} and homemesh.log."),
    "update_new_version_rejected": (
        "The new version started but the hub refused it: {detail}.",
        "It was undone. Update the hub first if versions differ."),
    "update_not_confirmed": (
        "The new version did not connect to the hub within {n} s.",
        "It was undone. See {log}."),
    "update_db_restore_failed": (
        "The database could not be restored to its pre-update state.",
        'HomeMesh is stopped. Follow "Restore the database by hand" in TROUBLESHOOTING.md; the copy is '
        "{path}."),
    "update_rollback_failed": (
        "The update failed and the previous version did not start either. HomeMesh is stopped on {computer}.",
        "Run the installer from {source_dir}: {installer_command}. Logs: {log}."),
    "update_interrupted": (
        "The update was interrupted while {phase} (shutdown, logoff or the helper was stopped).",
        "{computer} now runs {commit}. Check for updates again."),
}

WARNINGS: dict[str, tuple[str, str]] = {
    "remote_url_has_credentials": (
        "The address of git remote {remote} on {computer} contains a password or token.",
        "Remove it from the URL and let a credential helper store it (`git -C {source_dir} remote -v`)."),
    "no_linger": (
        "Logging out of {computer} stops HomeMesh and a running update.",
        "To keep them running, run `loginctl enable-linger $USER` on it once."),
    "installed_commit_unknown": (
        "The installed commit is not known in {source_dir}; changes are counted from the checkout's current "
        "commit.",
        "The list of changes may be incomplete; updating is still safe."),
    "checkout_not_advanced": (
        "The update worked, but {source_dir} was not moved to the new commit.",
        "Run `git -C {source_dir} merge --ff-only` once; HomeMesh itself is up to date."),
    "not_confirmed": (
        "The new version runs, but the hub couldn't be reached to confirm it.",
        "It shows up when {computer} reconnects; if it doesn't, see {log}."),
    "web_ui_missing": (
        "The web app is not built on {computer}; this version runs without it.",
        "Install Node.js 20.19+ and run the installer once so the web app is built."),
    "not_started": (
        "HomeMesh was not running, so the new version was not started.",
        "Start it with `homemesh start`."),
}

_DEFAULT_FIELDS: dict[str, str] = {
    "computer": "this computer",
    "source_dir": "the HomeMesh source folder",
    "log": "the update log",
    "installer_command": "run the installer again (docs/UPDATING.md)",
    "phase": "working",
    "commit": "an unknown version",
    "short": "this version",
    "detail": "no details",
    "lines": "no output",
    "reason": "unknown reason",
}


class _Fields(dict):  # type: ignore[type-arg]
    def __missing__(self, key: str) -> str:
        return _DEFAULT_FIELDS.get(key, f"<{key}>")


def _render(template: str, fields: dict[str, Any]) -> str:
    try:
        return template.format_map(_Fields(fields))
    except Exception:
        return template


def issue(code: str, **fields: Any) -> dict[str, Any]:
    """``{code, message, hint}`` for an error or warning code, like ``protocol.update.format_update_error``
    (same texts and defaults), with the result also redacted. Never raises."""
    try:
        values = {k: v for k, v in fields.items() if v is not None and v != ""}
        phase = values.get("phase")
        if isinstance(phase, str) and phase in PHASE_LABELS:
            values["phase"] = PHASE_LABELS[phase]
        entry = ERRORS.get(code) or WARNINGS.get(code)
        if entry is None:
            message, hint = f"The update failed ({code}).", "See {log}."
        else:
            message, hint = entry
        return {"code": clean_line(str(code), CODE_MAX),
                "message": clean_text(redact(_render(message, values)), MESSAGE_MAX),
                "hint": clean_text(redact(_render(hint, values)), HINT_MAX)}
    except Exception:
        return {"code": "update_error", "message": "The update failed.", "hint": None}


def installer_command(os_name: str | None, source_dir: str | None, *, pull: bool = False) -> str:
    """Copy of ``protocol.update.installer_command``."""
    if os_name == "windows":
        where = f'cd "{source_dir}"' if source_dir else "cd <HomeMesh source folder>"
        pull_step = "git pull --ff-only; " if pull else ""
        return f"{where}; {pull_step}powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade"
    if source_dir:
        src = shlex.quote(source_dir)
        if pull:
            return f"cd {src} && git pull --ff-only && ./deploy/linux/install.sh --upgrade"
        return f"{shlex.quote(source_dir.rstrip('/') + '/deploy/linux/install.sh')} --upgrade"
    pull_step = "git pull --ff-only && " if pull else ""
    return f"cd <HomeMesh source folder> && {pull_step}./deploy/linux/install.sh --upgrade"


def _os_name() -> str:
    return "windows" if sys.platform == "win32" else "linux"


# =========================================================================== limits (design 9.2)

TIMEOUTS: dict[str, float] = {
    "git": 30.0,  # any git command except fetch and archive
    "git_fetch": 120.0,
    "git_archive": 120.0,  # git archive + extraction
    "npm_ci": 900.0,
    "npm_build": 600.0,
    "venv": 180.0,
    "pip_upgrade": 300.0,
    "pip_install": 1200.0,
    "homemesh_cli": 60.0,  # homemesh version / config validate / service install (new or old venv)
    "systemctl": 150.0,  # one systemctl call (stop waits for the unit's own TimeoutStopSec)
    "drain": 120.0,
    "stop": 120.0,  # graceful stop
    "forced_stop": 15.0,
    "instance_lock": 10.0,
    "ready": 1800.0,  # new process alive but not ready yet (database migrations)
    "connect": 180.0,  # agent.json "connected" after ready (roles with a worker)
    "stable": 30.0,  # roles without a worker: alive and unchanged this long
}
SLEEP_GAP_S = 10.0  # one poll step longer than this means the computer slept: not counted
POLL_S = 1.0
MIN_FREE_BYTES = 2 * 1024 ** 3
LINGER_DIR = "/var/lib/systemd/linger"
UNIT = "homemesh.service"
AUTH_PATTERNS = ("terminal prompts disabled", "Authentication failed", "Permission denied (publickey)",
                 "could not read Username", "could not read Password")
MIGRATIONS_GIT_DIR = "src/homemesh/coordinator/db/migrations/"
LAYOUT_GIT_PATH = "src/homemesh/update/layout.txt"
_MIGRATION_RE = re.compile(r"([0-9]{4})_[a-z0-9_]+\.sql")

# Allowlisted environment for git, npm, node, pip and systemctl (design 9.1). launch.py keeps the same list
# for systemd-run --setenv.
TOOL_ENV_ALLOW = ("HOME", "USER", "LOGNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR",
                  "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                  "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                  "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG")

# Windows process creation flags
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# =========================================================================== time


def now_iso(dt: datetime | None = None) -> str:
    """UTC ISO-8601 with milliseconds and ``Z`` (same as ``homemesh.common.timeutil.iso``)."""
    dt = dt or datetime.now(UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class AwakeDeadline:
    """A deadline in *awake* time (design 9.3): polled at least every second; a step longer than
    :data:`SLEEP_GAP_S` means the computer slept, and that gap is not counted."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = float(seconds)
        self.clock = clock
        self.used = 0.0
        self.last = clock()

    def _tick(self) -> None:
        now = self.clock()
        step = now - self.last
        self.last = now
        if 0 < step <= SLEEP_GAP_S:
            self.used += step

    def expired(self) -> bool:
        self._tick()
        return self.used >= self.seconds

    def remaining(self) -> float:
        self._tick()
        return max(0.0, self.seconds - self.used)


# =========================================================================== files (copies of update/files.py)

MAX_FILE_BYTES = 1024 * 1024
REPLACE_RETRY_S = 2.0
KEEP_RUNS = 5
KEEP_CHECKS = 5
RUN_DIR_FILES = frozenset({"args.json", "run.json", "drain.json", "pre-update.sqlite3", "pre-update.sqlite3-journal",
                           "homemesh.toml.bak", "worker-credentials.json.bak"})
SNAPSHOT_FILES = frozenset({"pre-update.sqlite3", "pre-update.sqlite3-journal", "homemesh.toml.bak",
                            "worker-credentials.json.bak"})
KEEP_RUN_ERROR_CODES = frozenset({"update_db_restore_failed"})
CAPTURE_FILE_RE = re.compile(r"cmd-[0-9]{1,6}\.out")
TEMP_FILE_RE = re.compile(r"\.[A-Za-z0-9._-]{1,200}\.tmp")
_CHECK_FILE_RE = re.compile(r"(u[0-9]{14}-[0-9a-f]{8})(\.args)?\.json")
RECORD_NAME = "homemesh-install.json"
SNAPSHOT_NAME = "pre-update.sqlite3"
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link(path: str | os.PathLike[str]) -> bool:
    """A symlink, or (Windows) any reparse point such as a junction."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return sys.platform == "win32" and bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _is_real_dir(path: str | os.PathLike[str]) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not _is_link(path)


def _is_real_file(path: str | os.PathLike[str]) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _lexists(path: str | os.PathLike[str]) -> bool:
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def _refuse_link(path: str | os.PathLike[str]) -> None:
    if _is_link(path):
        raise OSError(errno.ELOOP, "refusing to write through a link", str(path))


def _replace_with_retry(src: str, dst: str | os.PathLike[str]) -> None:
    deadline = time.monotonic() + REPLACE_RETRY_S
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _fsync_dir(path: str | os.PathLike[str]) -> None:
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


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Replace ``path`` atomically and durably, owner-only; refuses a link destination."""
    path = Path(path)
    _refuse_link(path)
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


def atomic_write_json(path: str | os.PathLike[str], obj: Any) -> None:
    data = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_write_bytes(path, data.encode("utf-8"))


def read_json(path: str | os.PathLike[str], *, max_bytes: int = MAX_FILE_BYTES) -> dict[str, Any] | None:
    """A JSON object, or None if missing/unreadable/too large/undecodable. Opens, reads, closes at once."""
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


def create_private(path: str | os.PathLike[str]) -> int:
    """Create a new owner-only file (``O_CREAT|O_EXCL``, 0600, never through a link); returns the fd."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY
    return os.open(path, flags, 0o600)


def _write_new_private(path: str | os.PathLike[str], data: bytes) -> None:
    fd = create_private(path)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


class FileLock:
    """Copy of ``homemesh.common.instance.InstanceLock``: flock on POSIX, ``msvcrt.locking`` on byte 0 on
    Windows. Non-blocking; the OS releases it when the process ends."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh: Any = None

    def acquire(self) -> bool:
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


def lock_is_held(path: str | os.PathLike[str]) -> bool:
    """Is another process holding this lock? (Probes without disturbing it; never call it on a lock this
    process holds itself: the probe would see its own lock.)"""
    probe = FileLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True


# --------------------------------------------------------------------------- pruning (copy of files.prune_*)

def _unlink_file(path: str | os.PathLike[str]) -> bool:
    if not _is_real_file(path):
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def _list_names(path: str | os.PathLike[str]) -> list[str]:
    with os.scandir(path) as it:
        return [e.name for e in it]


def _remove_run_dir(path: str) -> bool:
    try:
        names = _list_names(path)
    except OSError:
        return False
    for name in names:
        if name in RUN_DIR_FILES or CAPTURE_FILE_RE.fullmatch(name) or TEMP_FILE_RE.fullmatch(name):
            _unlink_file(os.path.join(path, name))
    try:
        os.rmdir(path)
    except OSError:
        return False
    return True


def _error_code(run: dict[str, Any]) -> str | None:
    err = run.get("error")
    return err.get("code") if isinstance(err, dict) else None


def prune_runs(update_dir: str, logs_dir: str, keep: int = KEEP_RUNS) -> list[str]:
    """Same rules as ``homemesh.update.files.prune_runs``."""
    base = os.path.join(update_dir, "runs")
    try:
        names = sorted(n for n in _list_names(base) if is_run_id(n) and _is_real_dir(os.path.join(base, n)))
    except OSError:
        return []
    if not names:
        return []
    latest = read_json(os.path.join(update_dir, "latest.json")) or {}
    protected = {latest.get("run_id") if is_run_id(latest.get("run_id")) else None}
    for name in names:
        run = read_json(os.path.join(base, name, "run.json"))
        if run is not None and (run.get("status") is None or _error_code(run) in KEEP_RUN_ERROR_CODES):
            protected.add(name)
    newest = names[-1]
    removed: list[str] = []
    for name in names[:-keep] if keep > 0 else names:
        if name in protected:
            continue
        if _remove_run_dir(os.path.join(base, name)):
            removed.append(name)
            _unlink_file(os.path.join(logs_dir, f"update-{name}.log"))
    for name in names:
        if name != newest and name not in removed and name not in protected and _is_real_dir(os.path.join(base, name)):
            for snap in SNAPSHOT_FILES:
                _unlink_file(os.path.join(base, name, snap))
    return removed


def prune_checks(update_dir: str, logs_dir: str, keep: int = KEEP_CHECKS) -> list[str]:
    """Same rules as ``homemesh.update.files.prune_checks``."""
    base = os.path.join(update_dir, "checks")
    by_id: dict[str, list[str]] = {}
    try:
        names = _list_names(base)
    except OSError:
        return []
    for name in names:
        m = _CHECK_FILE_RE.fullmatch(name)
        if m:
            by_id.setdefault(m.group(1), []).append(name)
    ids = sorted(by_id)
    removed: list[str] = []
    for run_id in ids[:-keep] if keep > 0 else ids:
        for name in by_id[run_id]:
            _unlink_file(os.path.join(base, name))
        _unlink_file(os.path.join(logs_dir, f"update-check-{run_id}.log"))
        removed.append(run_id)
    return removed


def _rmtree(path: str) -> bool:
    """Delete a directory tree the runner created (never through a link). Windows: long-path prefix,
    read-only files, short retries (an antivirus scan briefly holds new files). True if it is gone."""
    if not _lexists(path):
        return True
    if not _is_real_dir(path):
        return False
    target = path
    if sys.platform == "win32" and not target.startswith("\\\\?\\"):
        target = "\\\\?\\" + os.path.abspath(target)

    def on_error(func: Any, p: str, _exc: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)

    for attempt in range(3):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=on_error)
            else:
                shutil.rmtree(target, onerror=on_error)
        except OSError:
            pass
        if not _lexists(path):
            return True
        time.sleep(0.5 * (attempt + 1))
    return not _lexists(path)


# =========================================================================== processes


def inside_homemesh_unit(cgroup_file: str = "/proc/self/cgroup") -> bool:
    """True when this process lives in ``homemesh.service``'s cgroup (Linux)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open(cgroup_file, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    return any(line.rstrip().endswith("/" + UNIT) for line in lines)


_BOOT_TIME: float | None = None


def _linux_boot_time() -> float | None:
    global _BOOT_TIME
    if _BOOT_TIME is None:
        try:
            with open("/proc/stat", encoding="ascii", errors="replace") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        _BOOT_TIME = float(line.split()[1])
                        break
        except (OSError, ValueError):
            return None
    return _BOOT_TIME


def _linux_stat(pid: int) -> tuple[str, float] | None:
    """``(state, start time as epoch seconds)`` from ``/proc/<pid>/stat`` (like psutil's create_time)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    try:
        rest = raw[raw.rindex(")") + 2:].split()
        state, ticks = rest[0], int(rest[19])
        boot = _linux_boot_time()
        if boot is None:
            return None
        return state, boot + ticks / os.sysconf("SC_CLK_TCK")
    except (ValueError, IndexError, OSError):
        return None


def _kernel32() -> Any:
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    vp, u32, i32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    k32.OpenProcess.argtypes, k32.OpenProcess.restype = [u32, i32, u32], vp
    k32.CloseHandle.argtypes, k32.CloseHandle.restype = [vp], i32
    k32.GetProcessTimes.argtypes = [vp] + [ctypes.POINTER(ctypes.c_uint64)] * 4
    k32.GetProcessTimes.restype = i32
    k32.WaitForSingleObject.argtypes, k32.WaitForSingleObject.restype = [vp, u32], u32
    k32.TerminateProcess.argtypes, k32.TerminateProcess.restype = [vp, u32], i32
    k32.CreateJobObjectW.argtypes, k32.CreateJobObjectW.restype = [vp, ctypes.c_wchar_p], vp
    k32.SetInformationJobObject.argtypes, k32.SetInformationJobObject.restype = [vp, i32, vp, u32], i32
    k32.AssignProcessToJobObject.argtypes, k32.AssignProcessToJobObject.restype = [vp, vp], i32
    k32.TerminateJobObject.argtypes, k32.TerminateJobObject.restype = [vp, u32], i32
    k32.SetThreadExecutionState.argtypes, k32.SetThreadExecutionState.restype = [u32], u32
    k32.GetCurrentProcess.argtypes, k32.GetCurrentProcess.restype = [], vp
    return k32


_WIN_SYNCHRONIZE = 0x00100000
_WIN_QUERY_LIMITED = 0x1000
_WIN_TERMINATE = 0x0001
_WIN_WAIT_OBJECT_0 = 0
_WIN_EPOCH_DELTA = 11644473600.0


def _win_open(pid: int) -> int | None:
    try:
        k32 = _kernel32()
        h = k32.OpenProcess(_WIN_SYNCHRONIZE | _WIN_QUERY_LIMITED | _WIN_TERMINATE, 0, pid)
        if not h:
            h = k32.OpenProcess(_WIN_SYNCHRONIZE | _WIN_QUERY_LIMITED, 0, pid)
        return int(h) if h else None
    except (OSError, AttributeError, ValueError):
        return None


def _win_start_time(handle: int) -> float | None:
    import ctypes

    try:
        k32 = _kernel32()
        c, e, k, u = (ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64())
        if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return c.value / 1e7 - _WIN_EPOCH_DELTA
    except (OSError, AttributeError, ValueError):
        return None


def _win_close(handle: int | None) -> None:
    if handle:
        with contextlib.suppress(Exception):
            _kernel32().CloseHandle(handle)


def proc_start_time(pid: int) -> float | None:
    """Start time (epoch seconds) of a live process, comparable to ``psutil.Process(pid).create_time()``;
    None if it is gone, a zombie, or this platform can't tell."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        st = _linux_stat(pid)
        if st is None or st[0] in ("Z", "X"):
            return None
        return st[1]
    if sys.platform == "win32":
        h = _win_open(pid)
        if h is None:
            return None
        try:
            if _kernel32().WaitForSingleObject(h, 0) == _WIN_WAIT_OBJECT_0:
                return None
            return _win_start_time(h)
        finally:
            _win_close(h)
    return None


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800


class _WinJob:
    """A Job object that kills every process in it when terminated or closed (design 9.2).

    ``process`` is a process handle (``int(popen._handle)``); ``limits`` the job's limit flags."""

    def __init__(self, process: int, limits: int = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) -> None:
        import ctypes

        self.handle: int | None = None
        k32 = _kernel32()

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                       "OtherOperationCount", "ReadTransferCount",
                                                       "WriteTransferCount", "OtherTransferCount")]

        class _Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")  # type: ignore[attr-defined]
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = limits
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")  # type: ignore[attr-defined]
        if not k32.AssignProcessToJobObject(job, process):
            k32.CloseHandle(job)
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")  # type: ignore[attr-defined]
        self.handle = int(job)

    def terminate(self) -> None:
        if self.handle:
            with contextlib.suppress(Exception):
                _kernel32().TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            _win_close(self.handle)
            self.handle = None


def _kill_group(popen: subprocess.Popen[bytes], job: _WinJob | None) -> None:
    """Kill a command and everything it started (POSIX: its process group; Windows: its Job object)."""
    if sys.platform == "win32":
        if job is not None:
            job.terminate()
        with contextlib.suppress(OSError):
            popen.kill()
        return
    try:
        os.killpg(popen.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(OSError):
            popen.kill()


def _disk_free(path: str) -> int:
    return shutil.disk_usage(path).free


def _display(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def venv_python(venv: str, *, windowless: bool = False) -> str:
    """The interpreter of a venv: ``bin/python``; Windows ``Scripts\\python.exe`` (``pythonw.exe``)."""
    if sys.platform == "win32":
        return os.path.join(venv, "Scripts", "pythonw.exe" if windowless else "python.exe")
    return os.path.join(venv, "bin", "python")


# =========================================================================== hooks and commands


@dataclass
class CmdResult:
    """Outcome of one external command. ``out``/``err`` are the raw (unredacted) decoded output: callers
    redact before anything leaves the runner. Without ``capture``, ``out`` is the combined output's tail."""

    returncode: int
    out: str = ""
    err: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, lines: int = 20, chars: int = 400) -> str:
        text = "\n".join(x for x in (self.out, self.err) if x)
        kept = [ln.rstrip() for ln in text.splitlines() if ln.strip()][-lines:]
        return clean_text(redact("\n".join(kept)), 4000)[-chars:].strip()


class Hooks:
    """The runner's contact points with the outside world. Production uses the defaults; tests replace them.

    * ``commands``: key → fake ``fn(argv, *, cwd, env, timeout_s) -> CmdResult`` used instead of running the
      command. Keys: ``git`` (every git call except) ``git_fetch``/``git_archive``, ``venv``, ``pip_upgrade``,
      ``pip_install``, ``npm_ci``, ``npm_build``, ``homemesh_version``, ``config_validate``,
      ``service_install``, ``systemctl``, ``mklink``.
    * ``spawn(argv, *, env, cwd)``: start ``homemesh run`` detached; returns a ``Popen``-like object.
    * ``monotonic``/``sleep``: the clock behind awake-time deadlines; ``timeouts`` overrides :data:`TIMEOUTS`.
    * ``on_phase(phase, run)``: called right after each phase was written to run.json.
    """

    def __init__(self) -> None:
        self.commands: dict[str, Callable[..., CmdResult]] = {}
        self.spawn: Callable[..., Any] | None = None
        self.monotonic: Callable[[], float] = time.monotonic
        self.sleep: Callable[[float], None] = time.sleep
        self.timeouts: dict[str, float] = {}
        self.on_phase: Callable[[str, dict[str, Any]], None] | None = None
        self.inside_homemesh_unit: Callable[[], bool] = inside_homemesh_unit
        self.keep_awake: bool = True
        self.handle_sigterm: bool = True
        self.base_python: str | None = None
        self.linger_dir: str = LINGER_DIR
        self.disk_free: Callable[[str], int] = _disk_free
        self.poll_s: float = POLL_S


class Fail(Exception):
    """A step failed with a known error (``{code, message, hint}``)."""

    def __init__(self, err: dict[str, Any]) -> None:
        super().__init__(err.get("message", ""))
        self.err = err


class Interrupted(BaseException):
    """SIGTERM (systemd ``RuntimeMaxSec``, the user manager stopping): unwinds into the top-level guard."""


class _Pump:
    """Copies complete new lines of a growing output file into the run log, redacted."""

    def __init__(self, path: str, sink: Callable[[str], None]) -> None:
        self.path, self.sink, self.pos, self.buf = path, sink, 0, b""

    def pump(self, final: bool = False) -> None:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                data = fh.read(4 * 1024 * 1024)
        except OSError:
            return
        self.pos += len(data)
        self.buf += data
        if final:
            chunk, self.buf = self.buf, b""
        else:
            cut = self.buf.rfind(b"\n")
            if cut < 0 and len(self.buf) < 65536:
                return
            cut = cut if cut >= 0 else len(self.buf) - 1
            chunk, self.buf = self.buf[:cut + 1], self.buf[cut + 1:]
        if chunk:
            self.sink(chunk.decode("utf-8", "replace"))


def _read_text(path: str, *, tail: int | None = None) -> str:
    try:
        with open(path, "rb") as fh:
            if tail is not None:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - tail))
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


# =========================================================================== shared runner machinery


@dataclass
class _Upstream:
    branch: str
    remote: str
    rbranch: str

    @property
    def ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.rbranch}"

    @property
    def upstream_ref(self) -> str:
        return f"{self.remote}/{self.rbranch}"


@dataclass
class _Facts:
    """What section 10 steps 6-10 found for one upstream commit."""

    sha: str
    upstream_date: str | None
    head: str | None
    installed: str | None
    base: str
    behind: int
    ahead: int
    commits: list[dict[str, Any]]
    commits_truncated: bool
    web_changed: bool | None
    node_found: bool | None
    migrations: bool | None
    target_schema: int
    target_layout: int
    problems: list[dict[str, Any]]


class _Run:
    """Common machinery of check and apply: logging, environments, commands, git, section 10 checks."""

    mode = "check"

    def __init__(self, args: dict[str, Any], hooks: Hooks | None) -> None:
        self.a = args
        self.h = hooks or Hooks()
        self.windows = sys.platform == "win32"
        self.src: str = args["source_dir"]
        self.update_dir: str = args["update_dir"]
        self.python: str = self.h.base_python or sys.executable
        self._cmd_n = 0
        self._ssh_configured: bool | None = None
        self.git_version: tuple[int, int] = (0, 0)
        self._record: dict[str, Any] | bool | None = False  # False: not loaded yet

    # ----------------------------------------------------------------- basics
    def t(self, name: str) -> float:
        return float(self.h.timeouts.get(name, TIMEOUTS[name]))

    def log(self, text: str) -> bool:
        """Append redacted, timestamped lines to the run log. False if the log can't be written."""
        stamp = now_iso()
        lines = text.splitlines() or [""]
        out = "".join(f"{stamp} {clean_line(redact(line), 4000)}\n" for line in lines)
        try:
            fd = os.open(self.a["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW | _O_BINARY, 0o600)
            with os.fdopen(fd, "ab") as fh:
                fh.write(out.encode("utf-8", "replace"))
        except OSError:
            return False
        return True

    def _log_output(self, text: str) -> None:
        self.log("\n".join("  | " + line for line in text.splitlines()))

    def fields(self) -> dict[str, Any]:
        return {"computer": self.a["computer"], "source_dir": self.src, "log": self.a["log"]}

    def fail(self, code: str, **fields: Any) -> Fail:
        return Fail(issue(code, **{**self.fields(), **fields}))

    def warning(self, code: str, **fields: Any) -> dict[str, Any]:
        return issue(code, **{**self.fields(), **fields})

    def consent_given(self) -> bool:
        try:
            return os.path.isfile(self.a["consent_file"])
        except (OSError, ValueError):
            return False

    def early_exit(self, code: int, reason: str) -> int:
        # stderr only if the log can't be written: every launch method makes stderr this same log, and on
        # Windows that inherited handle has its own file position (the end of the launcher's header; there
        # is no kernel-level append for it), so a second copy written through it would overwrite the lines
        # logged since.
        if not self.log(f"{EXIT_MARKER} {code}: {reason}"):
            print(f"{EXIT_MARKER} {code}: {reason}", file=sys.stderr, flush=True)
        return code

    # ----------------------------------------------------------------- environments (design 9.1)
    def node_dir(self) -> str | None:
        d = self.a.get("node_dir")
        return d if d and os.path.isdir(d) else None

    def tool_env(self, *, git: bool = False, path_first: tuple[str, ...] = ()) -> dict[str, str]:
        env: dict[str, str] = {}
        names = TOOL_ENV_ALLOW if not self.windows else tuple(dict.fromkeys(n.upper() for n in TOOL_ENV_ALLOW))
        for name in names:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        dirs = [d for d in (*path_first, self.node_dir(), os.path.dirname(self.a["git"])) if d]
        dirs = list(dict.fromkeys(dirs))
        if self.windows:
            env["PATH"] = ";".join([*dirs, os.environ.get("PATH", "")])
        else:
            env["PATH"] = ":".join([*dirs, "/usr/local/bin", "/usr/bin", "/bin"])
        if git:
            env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="", SSH_ASKPASS_REQUIRE="never")
            if not self.windows:
                env["LC_ALL"] = "C.UTF-8"
            if self._ssh_configured is False:
                env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
        return env

    def homemesh_env(self, *, detached: bool = False) -> dict[str, str]:
        """For ``<venv python> -m homemesh …``: the launch environment (HomeMesh's own, or the systemd
        ``--setenv`` set), ``HOMEMESH_HOME`` from args.json, ``HOMEMESH_DETACHED`` only for ``run``."""
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("PYTHON") and k.upper() not in ("__PYVENV_LAUNCHER__", "HOMEMESH_DETACHED")}
        home = self.a.get("homemesh_home")
        if home:
            env["HOMEMESH_HOME"] = home
        else:
            env.pop("HOMEMESH_HOME", None)
        if detached:
            env["HOMEMESH_DETACHED"] = "1"
        return env

    # ----------------------------------------------------------------- commands (design 9.2)
    def _capture_path(self) -> str:
        self._cmd_n += 1
        return os.path.join(self.update_dir, "checks", f".{self.a['run_id']}.cmd-{self._cmd_n}.tmp")

    def _done_with_capture(self, path: str, text: str) -> None:
        for _ in range(20):  # Windows: an antivirus scan can hold a file that was just written, briefly
            if _unlink_file(path) or not _lexists(path):
                return
            time.sleep(0.05)

    def cmd(self, key: str, argv: list[str], *, timeout_s: float, env: dict[str, str], cwd: str | None = None,
            capture: bool = False, quiet: bool = False, trace: bool = True) -> CmdResult:
        """Run one external command (no shell, no pipes, absolute executable) on an awake-time deadline;
        every timeout kills the command's whole tree. ``quiet`` keeps its stdout out of the log. ``trace=False``
        is for status polls: neither the command nor its output is logged (only a failure is) and its
        capture files are deleted; the caller logs what changed."""
        argv = [os.fspath(a) for a in argv]
        if trace:
            self.log("$ " + _display(argv))
        fake = self.h.commands.get(key)
        if fake is not None:
            res = fake(list(argv), cwd=cwd, env=dict(env), timeout_s=timeout_s)
            for text in ((res.out if not quiet else ""), res.err):
                if text.strip() and trace:
                    self._log_output(text[-4000:])
        else:
            res = self._exec(argv, timeout_s=timeout_s, cwd=cwd or self.update_dir, env=env, capture=capture,
                             quiet=quiet, trace=trace)
        what = "" if trace else f"$ {_display(argv)}"
        if res.timed_out:
            self.log(f"{what}  -> timed out after {timeout_s:.0f} s; stopped it and everything it started")
        elif res.returncode != 0:
            self.log(f"{what}  -> exit code {res.returncode}")
        return res

    def _exec(self, argv: list[str], *, timeout_s: float, cwd: str, env: dict[str, str], capture: bool,
              quiet: bool, trace: bool = True) -> CmdResult:
        out_path = self._capture_path()
        err_path = self._capture_path() if capture else None
        out_fd = create_private(out_path)
        err_fd = create_private(err_path) if err_path else None
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": out_fd,
                                  "stderr": err_fd if err_fd is not None else subprocess.STDOUT,
                                  "cwd": cwd, "env": env, "close_fds": True}
        if self.windows:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        try:
            popen = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            for p in (out_path, err_path):
                if p:
                    _unlink_file(p)
            self.log(f"  -> could not start: {exc}")
            return CmdResult(127, err=f"{type(exc).__name__}: {exc}")
        finally:
            os.close(out_fd)
            if err_fd is not None:
                os.close(err_fd)
        job: _WinJob | None = None
        if self.windows:
            try:
                job = _WinJob(int(popen._handle))  # type: ignore[attr-defined]
            except Exception as exc:  # a grandchild may then escape a timeout kill; accepted (9.2)
                self.log(f"  (no Job object: {exc})")
        pump = _Pump(err_path or out_path, self._log_output if trace else (lambda _text: None))
        deadline = AwakeDeadline(timeout_s, self.h.monotonic)
        step, timed_out = 0.01, False
        try:
            while True:
                try:
                    popen.wait(timeout=step)
                    break
                except subprocess.TimeoutExpired:
                    pass
                pump.pump()
                if deadline.expired():
                    timed_out = True
                    _kill_group(popen, job)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        popen.wait(timeout=30)
                    break
                step = min(1.0, step * 2)
        except BaseException:  # SIGTERM (Interrupted) while waiting: don't leave pip/npm/git running
            _kill_group(popen, job)
            if job is not None:
                job.close()
            raise
        if job is not None:
            job.close()
        pump.pump(final=True)
        if capture:
            out = _read_text(out_path)
            err = _read_text(err_path) if err_path else ""
            if out.strip() and not quiet and trace:
                self._log_output(out[:4000])
        else:
            out, err = _read_text(out_path, tail=65536), ""
        # A status poll's captures are deleted (the check runner's way), never kept in the run folder.
        done = self._done_with_capture if trace else (lambda path, text: _Run._done_with_capture(self, path, text))
        done(out_path, out)
        if err_path:
            done(err_path, err)
        rc = popen.returncode if popen.returncode is not None else -9
        return CmdResult(rc, out=out, err=err, timed_out=timed_out)

    # ----------------------------------------------------------------- git
    def git(self, *args: str, key: str = "git", timeout: str = "git", quiet: bool = False) -> CmdResult:
        argv = [self.a["git"], "-C", self.src, "-c", "core.quotepath=off", "-c", "i18n.logOutputEncoding=UTF-8",
                *args]
        return self.cmd(key, argv, timeout_s=self.t(timeout), env=self.tool_env(git=True), capture=True,
                        quiet=quiet)

    def rev_parse_eoo(self) -> tuple[str, ...]:
        """``--end-of-options`` for ``git rev-parse``, which honours it only from git 2.30 (the commands
        built on parse_options or setup_revisions have it since 2.24). Older rev-parse would read it as a
        revision; its arguments here are ``refs/remotes/<validated names>`` or constants, which can't be read
        as options anyway."""
        return ("--end-of-options",) if self.git_version >= (2, 30) else ()

    def _git_failure(self, res: CmdResult) -> Fail:
        """Section 10 step 3 stderr mapping (also used for other local git failures)."""
        text = res.err + res.out
        if res.timed_out or "index.lock" in text:
            return self.fail("update_git_locked")
        if "dubious ownership" in text:
            return self.fail("update_unsafe_repository")
        return self.fail("update_not_git_checkout")

    def installed_record(self) -> dict[str, Any] | None:
        """The install record of the venv HomeMesh runs from (``current_venv``)."""
        if self._record is False:
            self._record = read_json(os.path.join(self.a["current_venv"], RECORD_NAME))
        return self._record if isinstance(self._record, dict) else None

    def check_git(self) -> None:
        """Step 2: git exists and is at least 2.24 (``--end-of-options``)."""
        git = self.a["git"]
        if not (os.path.isfile(git) and os.access(git, os.X_OK)):
            raise self.fail("update_git_missing")
        res = self.cmd("git", [git, "--version"], timeout_s=self.t("git"), env=self.tool_env(git=True),
                       capture=True)
        m = re.search(r"(\d+)\.(\d+)", res.out)
        if not res.ok or m is None or (int(m.group(1)), int(m.group(2))) < (2, 24):
            raise self.fail("update_git_missing")
        self.git_version = (int(m.group(1)), int(m.group(2)))
        if self._ssh_configured is None:
            self._ssh_configured = self.git("config", "--get", "core.sshCommand", quiet=True).ok

    def source_state(self) -> _Upstream:
        """Step 3: on a branch with a valid upstream, no local changes to tracked files."""
        if not _lexists(os.path.join(self.src, ".git")):
            raise self.fail("update_not_git_checkout")
        res = self.git("symbolic-ref", "-q", "--short", "HEAD")
        if res.returncode == 1 and not res.timed_out and not res.err.strip():
            raise self.fail("update_detached_head")
        if not res.ok:
            raise self._git_failure(res)
        branch = res.out.strip()
        if not is_ref_name(branch):
            raise self.fail("update_bad_ref_name", name=clean_line(branch, NAME_MAX))
        res = self.git("config", "--get", f"branch.{branch}.remote")
        remote = res.out.strip()
        if res.returncode == 1 or not remote:
            raise self.fail("update_no_upstream", branch=branch)
        if not res.ok:
            raise self._git_failure(res)
        if not is_ref_name(remote):  # git also accepts a URL here (branch.<name>.remote): never report one
            raise self.fail("update_bad_ref_name",
                            name=clean_line(_REMOTE_ADDRESS.sub("<remote address>", remote), NAME_MAX))
        res = self.git("rev-parse", "--symbolic-full-name", "@{u}")
        if not res.ok:
            if "upstream" in res.err and not res.timed_out:
                raise self.fail("update_no_upstream", branch=branch)
            raise self._git_failure(res)
        full = res.out.strip()
        prefix = f"refs/remotes/{remote}/"
        if not full.startswith(prefix):
            raise self.fail("update_no_upstream", branch=branch)
        rbranch = full[len(prefix):]
        if not is_ref_name(rbranch):
            raise self.fail("update_bad_ref_name", name=clean_line(rbranch, NAME_MAX))
        res = self.git("status", "--porcelain=v1", "-z", "--untracked-files=no")
        if not res.ok:
            raise self._git_failure(res)
        files = _porcelain_files(res.out)
        if files:
            shown = [clean_line(f, PATH_MAX) for f in files[:MAX_DIRTY_FILES]]
            more = f" and {len(files) - len(shown)} more" if len(files) > len(shown) else ""
            raise self.fail("update_dirty_checkout", files=", ".join(shown) + more)
        return _Upstream(branch=branch, remote=remote, rbranch=rbranch)

    def fetch(self, up: _Upstream) -> None:
        """Step 4."""
        res = self.git("fetch", "--no-recurse-submodules", "--end-of-options", up.remote, up.rbranch,
                       key="git_fetch", timeout="git_fetch")
        if res.ok:
            return
        text = res.err + res.out
        if any(p in text for p in AUTH_PATTERNS):
            raise self.fail("update_auth_failed")
        if res.timed_out:
            detail = f"no answer within {self.t('git_fetch'):.0f} s"
        else:
            detail = _fetch_detail(text) or f"git exited with code {res.returncode}"
        raise self.fail("update_fetch_failed", remote=up.remote, detail=detail.rstrip("."))

    def resolve_upstream(self, up: _Upstream) -> str:
        """Step 5: the sha the runner itself resolved (the payload never reaches git)."""
        res = self.git("rev-parse", "--verify", *self.rev_parse_eoo(), f"{up.ref}^{{commit}}")
        sha = res.out.strip()
        if not res.ok or not is_sha(sha):
            raise self.fail("update_fetch_failed", remote=up.remote,
                            detail=f"{up.upstream_ref} could not be resolved after the fetch")
        return sha

    def _git_int(self, *args: str) -> int:
        res = self.git(*args)
        try:
            return int(res.out.strip())
        except ValueError:
            raise self._git_failure(res) from None

    def find_node(self) -> tuple[str, str] | None:
        """``(node, npm-cli.js)`` (design 11 B.4): ``node_dir`` from the record/args, else ``node`` on PATH."""
        node_dir = self.node_dir()
        if node_dir is None:
            found = shutil.which("node", path=os.environ.get("PATH", os.defpath))
            node_dir = os.path.dirname(os.path.abspath(found)) if found else None
        if node_dir is None:
            return None
        node = os.path.join(node_dir, "node.exe" if self.windows else "node")
        if not os.path.isfile(node):
            return None
        candidates = []
        npm = os.path.realpath(os.path.join(node_dir, "npm"))
        if npm.endswith("npm-cli.js"):
            candidates.append(npm)
        candidates.append(os.path.join(node_dir, "node_modules", "npm", "bin", "npm-cli.js"))
        candidates.append(os.path.normpath(os.path.join(node_dir, "..", "lib", "node_modules", "npm", "bin",
                                                        "npm-cli.js")))
        for c in candidates:
            if os.path.isfile(c):
                return node, c
        return None

    def link_ok(self) -> bool:
        """The stable ``venv`` path is a link/junction to ``<prefix>/venvs/<id>`` (the blue/green layout)."""
        stable = self.a["stable_venv"]
        if not _is_link(stable):
            return False
        target = os.path.realpath(stable)
        venvs = os.path.realpath(os.path.join(self.a["prefix"], "venvs"))
        return (os.path.normcase(os.path.dirname(target)) == os.path.normcase(venvs)
                and is_venv_id(os.path.basename(target)) and _is_real_dir(target))

    def inspect(self, sha: str, up: _Upstream) -> _Facts:
        """Section 10 steps 6-10 for ``sha``: counts, commits, web/node, migrations, layout, disk, linger."""
        problems: list[dict[str, Any]] = []

        def problem(code: str, blocking: bool, **fields: Any) -> None:
            if not any(p["code"] == code for p in problems):
                problems.append({**self.warning(code, **fields), "blocking": blocking})

        rec = self.installed_record() or {}
        installed = rec.get("commit") if is_sha(rec.get("commit")) else None
        head_res = self.git("rev-parse", "--verify", "HEAD^{commit}")
        head = head_res.out.strip() if head_res.ok and is_sha(head_res.out.strip()) else None
        base = None
        if installed and self.git("cat-file", "-e", "--end-of-options", f"{installed}^{{commit}}").ok:
            base = installed
        if base is None:
            if head is None:
                raise self._git_failure(head_res)
            base = head
            problem("installed_commit_unknown", False)
        behind = self._git_int("rev-list", "--count", "--end-of-options", f"{base}..{sha}")
        ahead = self._git_int("rev-list", "--count", "--end-of-options", f"{sha}..HEAD")
        if ahead > 0:
            problem("update_diverged", True, upstream_ref=up.upstream_ref)
        res = self.git("log", "-z", f"--max-count={MAX_COMMITS + 1}", "--format=%H%x00%s%x00%cI",
                       "--end-of-options", f"{base}..{sha}")
        if not res.ok:
            raise self._git_failure(res)
        tokens = res.out.split("\x00")
        commits = []
        for i in range(0, len(tokens) - 2, 3):
            c_sha = tokens[i].strip()
            if is_sha(c_sha):
                commits.append({"sha": c_sha, "subject": clean_line(redact(tokens[i + 1]), SUBJECT_MAX),
                                "date": clean_line(tokens[i + 2].strip(), TIME_MAX) or None})
        truncated = len(commits) > MAX_COMMITS
        commits = commits[:MAX_COMMITS]
        date_res = self.git("log", "-1", "--format=%cI", "--end-of-options", sha)
        upstream_date = (clean_line(date_res.out.strip(), TIME_MAX) or None) if date_res.ok else None

        roles = self.a["roles"]
        web_changed: bool | None = None
        node_found: bool | None = None
        migrations: bool | None = None
        target_schema = 0
        res = self.git("ls-tree", "--name-only", "--end-of-options", sha, "--", MIGRATIONS_GIT_DIR)
        if res.ok:
            for name in res.out.splitlines():
                m = _MIGRATION_RE.fullmatch(name.strip().rsplit("/", 1)[-1])
                if m:
                    target_schema = max(target_schema, int(m.group(1)))
        if "coordinator" in roles:
            diff = self.git("diff", "--quiet", "--end-of-options", base, sha, "--", "apps/web")
            web_changed = diff.returncode != 0  # 1 = changed; an error also means "rebuild to be safe"
            node_found = self.find_node() is not None
            if web_changed and not node_found:
                problem("update_node_missing", True)
            migrations = target_schema > _max_migration(self.a["current_package_dir"])
        layout_res = self.git("show", "--end-of-options", f"{sha}:{LAYOUT_GIT_PATH}")
        target_layout = 1
        if layout_res.ok:
            try:
                target_layout = int(layout_res.out.strip())
            except ValueError:
                target_layout = 1 << 30  # unreadable: let the installer decide
        rec_layout = rec.get("layout") if isinstance(rec.get("layout"), int) else 1
        if target_layout > rec_layout or not self.link_ok():
            problem("update_needs_installer", True,
                    installer_command=installer_command(_os_name(), self.src, pull=True))
        self._disk_problem(problem, migrations=bool(migrations))
        if sys.platform.startswith("linux") and self.a["service"] == "systemd":
            # logind creates the linger folder only when someone enables lingering: a missing folder means
            # "off" for everyone, which is exactly the case to warn about (Bazzite's default).
            user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
            if not user:
                with contextlib.suppress(Exception):
                    import pwd

                    user = pwd.getpwuid(os.getuid()).pw_name
            if user and not os.path.exists(os.path.join(self.h.linger_dir, user)):
                problem("no_linger", False)
        url = self.git("config", "--get", f"remote.{up.remote}.url", quiet=True)
        if url.ok and _url_has_credentials(url.out.strip()):
            problem("remote_url_has_credentials", False, remote=up.remote)
        return _Facts(sha=sha, upstream_date=upstream_date, head=head, installed=installed, base=base,
                      behind=behind, ahead=ahead, commits=commits, commits_truncated=truncated,
                      web_changed=web_changed, node_found=node_found, migrations=migrations,
                      target_schema=target_schema, target_layout=target_layout, problems=problems[:MAX_PROBLEMS])

    def _disk_problem(self, problem: Callable[..., None], *, migrations: bool) -> None:
        prefix = self.a["prefix"]
        try:
            free = self.h.disk_free(prefix)
        except OSError:
            return
        need = MIN_FREE_BYTES
        db = self.a.get("db_file")
        db_size = 0
        if migrations and db:
            with contextlib.suppress(OSError):
                db_size = os.path.getsize(db)
        same_volume = True
        if db_size and db:
            with contextlib.suppress(OSError):
                same_volume = os.stat(prefix).st_dev == os.stat(os.path.dirname(db)).st_dev
        if same_volume:
            need += db_size
        if free < need:
            problem("update_low_disk", True, free=_human_bytes(free), need=_human_bytes(need), volume=prefix)
            return
        if db_size and not same_volume and db:
            try:
                data_free = self.h.disk_free(os.path.dirname(db))
            except OSError:
                return
            if data_free < db_size:
                problem("update_low_disk", True, free=_human_bytes(data_free), need=_human_bytes(db_size),
                        volume=os.path.dirname(db))


_REMOTE_ADDRESS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]*|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s'\"]*")


def _fetch_detail(text: str) -> str:
    """The reason from git's fetch output, for ``update_fetch_failed``: its first ``fatal:``/``error:`` line
    (else the last line), with remote addresses masked (design 9.6: the hub sees the remote's name only)."""
    lines = [ln.strip() for ln in redact(text).splitlines() if ln.strip()]
    pick = next((ln for ln in lines if ln.lower().startswith(("fatal:", "error:"))), lines[-1] if lines else "")
    for prefix in ("fatal:", "error:"):
        if pick.lower().startswith(prefix):
            pick = pick[len(prefix):].strip()
    return clean_line(_REMOTE_ADDRESS.sub("<remote address>", pick), 200)


def _porcelain_files(out: str) -> list[str]:
    """Paths from ``git status --porcelain=v1 -z`` (a rename's second, original path is skipped)."""
    entries = out.split("\x00")
    files: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        files.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            i += 1
    return files


def _max_migration(package_dir: str) -> int:
    try:
        names = _list_names(os.path.join(package_dir, "coordinator", "db", "migrations"))
    except OSError:
        return 0
    found = [int(m.group(1)) for n in names if (m := _MIGRATION_RE.fullmatch(n))]
    return max(found, default=0)


def _url_has_credentials(url: str) -> bool:
    """A remote URL with a password or a token in it (``https://user:pw@``, ``https://<token>@``). A bare
    ssh user (``ssh://git@host``) is not a credential."""
    m = re.match(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/@\s]+)@", url)
    if not m:
        return False
    scheme, userinfo = m.group(1).lower(), m.group(2)
    return ":" in userinfo or scheme in ("http", "https", "ftp", "ftps") or bool(_GH_TOKEN.search(userinfo))


# =========================================================================== check mode (design 10)


class _Check(_Run):
    mode = "check"

    def main(self) -> int:
        if self.h.inside_homemesh_unit():
            return self.early_exit(EXIT_INSIDE_UNIT, "refusing to run inside homemesh.service")
        lock = FileLock(os.path.join(self.update_dir, "update.lock"))
        if not lock.acquire():
            return self.early_exit(EXIT_BUSY, "another update or check is running (update.lock is held)")
        try:
            os.makedirs(os.path.join(self.update_dir, "checks"), exist_ok=True)
            self.log(f"check {self.a['run_id']} (origin {self.a['origin']}) of {self.src}")
            try:
                result = self._check()
            except Fail as f:
                self.log(f"check failed: {f.err['code']}: {f.err['message']}")
                result = {"v": 1, "ok": False, "checked_at": now_iso(), "allowed": self.consent_given(),
                          "error": f.err}
            atomic_write_json(self.a["out"], result)
            self.log(f"check result written: ok={result['ok']} can_update={result.get('can_update')}")
            return EXIT_OK
        except Exception:  # a bug or a full disk: the traceback goes to the log redacted, never raw
            self.log("unexpected error:\n" + traceback.format_exc())
            return self.early_exit(EXIT_FAILED, "the check crashed (see the log above)")
        finally:
            lock.release()

    def _check(self) -> dict[str, Any]:
        if self.a["origin"] == "remote" and not self.consent_given():
            raise self.fail("update_not_allowed")
        self.check_git()
        up = self.source_state()
        self.fetch(up)
        sha = self.resolve_upstream(up)
        facts = self.inspect(sha, up)
        rec = self.installed_record() or {}
        blocking = any(p["blocking"] for p in facts.problems)
        return {"v": 1, "ok": True, "checked_at": now_iso(), "allowed": self.consent_given(),
                "installed_commit": facts.installed,
                "installed_dirty": rec.get("dirty") if isinstance(rec.get("dirty"), bool) else None,
                "head_commit": facts.head, "branch": up.branch, "remote": up.remote,
                "upstream_ref": up.upstream_ref, "upstream_commit": sha, "upstream_date": facts.upstream_date,
                "behind": facts.behind, "ahead": facts.ahead, "commits": facts.commits,
                "commits_truncated": facts.commits_truncated, "web_changed": facts.web_changed,
                "node_found": facts.node_found, "migrations": facts.migrations,
                "can_update": facts.behind > 0 and not blocking, "problems": facts.problems}


# =========================================================================== apply mode (design 11)


@dataclass
class _Proc:
    """A HomeMesh process identified by PID **and** start time (a bare PID is never trusted)."""

    pid: int
    started: float | None
    popen: Any = None  # when this runner started it
    handle: int | None = None  # Windows process handle (kept so the identity can't be reused)


@dataclass
class _Started:
    kind: str  # "systemd" | "process"
    wall: float  # time.time() just before starting
    popen: Any = None
    main_pid: int = 0
    nrestarts: int = 0
    start_ok: bool = True
    detail: str = ""


class _KeepAwake:
    """Keep the computer awake for the whole apply run (design 9.3); every failure is ignored."""

    def __init__(self, run: _Apply) -> None:
        self.r = run
        self.proc: subprocess.Popen[bytes] | None = None
        self.lifeline: int | None = None  # write end of the pipe the inhibitor's `cat` reads
        self.win = False

    def start(self) -> None:
        try:
            if sys.platform == "win32":
                es_continuous, es_system_required = 0x80000000, 0x00000001
                self.win = bool(_kernel32().SetThreadExecutionState(es_continuous | es_system_required))
            elif sys.platform.startswith("linux"):
                exe = shutil.which("systemd-inhibit", path="/usr/bin:/bin:/usr/local/bin")
                cat = shutil.which("cat", path="/usr/bin:/bin")
                if exe and cat:
                    # The inhibitor lives exactly as long as this runner: `cat` reads a pipe whose write end
                    # only this process holds, and ends at EOF, i.e. when the runner exits in any way (a
                    # SIGKILL or the OOM killer included). `sleep 10800` blocked sleep for up to three hours
                    # after a runner that was killed outright (the POSIX launch is its own session, so nothing
                    # else stops it).
                    read_fd, self.lifeline = os.pipe()
                    try:
                        self.proc = subprocess.Popen(
                            [exe, "--what=sleep:idle", "--who=HomeMesh", "--why=Updating HomeMesh", "--mode=block",
                             cat], stdin=read_fd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True, env=self.r.tool_env())
                    finally:
                        os.close(read_fd)
        except Exception as exc:
            self.r.log(f"keep-awake not available: {exc}")

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self.win:
                _kernel32().SetThreadExecutionState(0x80000000)
                self.win = False
        if self.lifeline is not None:
            with contextlib.suppress(OSError):
                os.close(self.lifeline)  # EOF: cat and with it systemd-inhibit end by themselves
            self.lifeline = None
            if self.proc is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=3)
        if self.proc is not None:
            with contextlib.suppress(OSError):
                if self.proc.poll() is None:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                        self.proc.wait(timeout=3)
            self.proc = None


class _Apply(_Run):
    mode = "apply"

    def __init__(self, args: dict[str, Any], hooks: Hooks | None) -> None:
        super().__init__(args, hooks)
        self.run_id: str = args["run_id"]
        self.run_dir = os.path.join(self.update_dir, "runs", self.run_id)
        self.run_file = os.path.join(self.run_dir, "run.json")
        self.run: dict[str, Any] | None = None
        self.warnings: list[dict[str, Any]] = []
        self.instance_lock = FileLock(args["instance_lock"])
        self.systemd = args["service"] == "systemd"
        self.has_worker = "worker" in args["roles"]
        self.is_hub = "coordinator" in args["roles"]
        self.sha: str | None = None
        self.up: _Upstream | None = None
        self.facts: _Facts | None = None
        self.keep_stopped = False  # db restore failed / rollback failed: the guard must not start anything
        self.last_started: _Started | None = None  # the HomeMesh this runner started last (F.1)
        self._armed = False
        self._interrupted = False
        self._unit_logged: tuple[str, ...] | None = None  # the last homemesh.service state written to the log

    # ----------------------------------------------------------------- capture files live in the run dir
    def _capture_path(self) -> str:
        self._cmd_n += 1
        return os.path.join(self.run_dir, f"cmd-{self._cmd_n}.out")

    def _done_with_capture(self, path: str, text: str) -> None:
        # Kept for diagnosis, but only redacted: the raw output can hold a token from a remote URL.
        with contextlib.suppress(OSError):
            if _is_real_file(path):
                with open(path, "wb") as fh:
                    fh.write(clean_text(redact(text), 1 << 22).encode("utf-8", "replace"))

    # ----------------------------------------------------------------- run.json
    def save(self, **changes: Any) -> None:
        assert self.run is not None
        self.run.update(changes)
        self.run["warnings"] = self.warnings[:MAX_WARNINGS]
        self.run["updated_at"] = now_iso()
        atomic_write_json(self.run_file, self.run)

    def phase(self, name: str, **changes: Any) -> None:
        self.log(f"phase {name}")
        self.save(phase=name, **changes)
        if self.h.on_phase is not None:
            self.h.on_phase(name, self.run or {})

    def warn(self, code: str, **fields: Any) -> None:
        if not any(w["code"] == code for w in self.warnings):
            self.warnings.append(self.warning(code, **fields))
            self.log(f"warning {code}")
            if self.run is not None:
                self.save()

    def set_error(self, err: dict[str, Any]) -> None:
        assert self.run is not None
        if self.run.get("error") is None:
            self.save(error=err)

    def finish(self, status: str, *, error: dict[str, Any] | None = None, override: bool = False,
               installed: str | None = None, phase: str | None = None) -> None:
        assert self.run is not None
        changes: dict[str, Any] = {"status": status, "finished_at": now_iso(), "installed_commit": installed}
        if error is not None:
            if self.run.get("error") is None:
                changes["error"] = error
            elif override and self.run["error"].get("code") != error.get("code"):
                changes["first_error"] = self.run["error"]
                changes["error"] = error
        if phase is not None:
            changes["phase"] = phase
        self.save(**changes)
        err = self.run.get("error") or {}
        self.log(f"final status {status}" + (f" ({err.get('code')}: {err.get('message')})" if err else ""))

    # ----------------------------------------------------------------- venv link
    @property
    def stable(self) -> str:
        return self.a["stable_venv"]

    def link_target(self) -> str | None:
        return os.path.realpath(self.stable) if _lexists(self.stable) else None

    def record_commit(self, venv: str | None) -> str | None:
        if not venv:
            return None
        rec = read_json(os.path.join(venv, RECORD_NAME)) or {}
        return rec.get("commit") if is_sha(rec.get("commit")) else None

    def _same(self, a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))

    def repair_link(self) -> None:
        """A.3: a link swap interrupted between two renames (Windows) leaves ``venv.next``/``venv.old``."""
        if _lexists(self.stable):
            return
        for suffix in (".next", ".old"):
            cand = self.stable + suffix
            if _lexists(cand) and _is_link(cand):
                os.rename(cand, self.stable)
                self.log(f"repaired the program link: renamed {cand} to {self.stable}")
                return

    def point_link(self, target: str) -> None:
        """Make the stable path point at ``target`` (``<prefix>/venvs/<id>``) atomically (E.2)."""
        if self.windows:
            self._junction_swap(target)
            return
        nxt = self.stable + ".next"
        if _lexists(nxt):
            if not _is_link(nxt):
                raise OSError(errno.EEXIST, "not a link; refusing to replace it", nxt)
            os.unlink(nxt)
        rel = os.path.relpath(target, os.path.dirname(self.stable))
        if self._same(os.path.dirname(target), os.path.join(self.a["prefix"], "venvs")):
            # Always "venvs/<id>", like the installers: the rollback passes the realpath of the old target,
            # and where a parent of the prefix is itself a symlink (Bazzite: /home -> var/home) relpath()
            # would otherwise write "../../../../../var/home/<user>/…/venvs/<id>".
            rel = os.path.join("venvs", os.path.basename(target))
        os.symlink(rel, nxt)
        os.replace(nxt, self.stable)
        _fsync_dir(os.path.dirname(self.stable))

    def _junction_swap(self, target: str) -> None:
        nxt, old = self.stable + ".next", self.stable + ".old"
        for p in (nxt, old):
            if _lexists(p):
                if not _is_link(p):
                    raise OSError(errno.EEXIST, "not a junction; refusing to replace it", p)
                os.rmdir(p)  # removes the junction only
        self._create_junction(os.path.abspath(target), nxt)
        _rename_retry(self.stable, old)
        try:
            _rename_retry(nxt, self.stable)
        except OSError:
            _rename_retry(old, self.stable)
            with contextlib.suppress(OSError):
                os.rmdir(nxt)
            raise
        with contextlib.suppress(OSError):
            os.rmdir(old)

    def _create_junction(self, target: str, link: str) -> None:
        try:
            import _winapi

            _winapi.CreateJunction(target, link)
            return
        except (ImportError, AttributeError, OSError) as exc:
            self.log(f"CreateJunction failed ({exc}); trying mklink /J")
        comspec = os.environ.get("COMSPEC") or os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"),
                                                             "System32", "cmd.exe")
        res = self.cmd("mklink", [comspec, "/d", "/c", "mklink", "/J", link, target], timeout_s=self.t("git"),
                       env=self.tool_env(), capture=True)
        if not res.ok or not _lexists(link):
            raise OSError(errno.EIO, f"could not create the junction {link}")

    # ----------------------------------------------------------------- HomeMesh processes
    def lock_free(self) -> bool:
        """No HomeMesh holds the instance lock (only meaningful while this runner doesn't hold it)."""
        if self.instance_lock.held:
            return True
        return not lock_is_held(self.a["instance_lock"])

    def identify(self) -> _Proc | None:
        """The running HomeMesh from the PID record, accepted only if its start time matches (D.2)."""
        rec = read_json(self.a["pid_file"]) or {}
        pid, started = rec.get("pid"), rec.get("started")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if not isinstance(started, (int, float)) or isinstance(started, bool):
            return None
        handle = _win_open(pid) if self.windows else None
        actual = _win_start_time(handle) if handle else proc_start_time(pid)
        if actual is None or abs(actual - float(started)) >= 1.0:
            _win_close(handle)
            return None
        return _Proc(pid=pid, started=actual, handle=handle)

    def alive(self, p: _Proc | None) -> bool:
        if p is None:
            return False
        if p.popen is not None and p.popen.poll() is not None:
            return False
        if self.windows and p.handle:
            with contextlib.suppress(Exception):
                return int(_kernel32().WaitForSingleObject(p.handle, 0)) != _WIN_WAIT_OBJECT_0
            return False
        actual = proc_start_time(p.pid)
        if actual is None:
            if not sys.platform.startswith("linux") and not self.windows:
                try:
                    os.kill(p.pid, 0)
                    return True
                except OSError:
                    return False
            return False
        return p.started is None or abs(actual - p.started) < 1.0

    def systemctl(self, *args: str, timeout: str = "systemctl", trace: bool = True) -> CmdResult:
        exe = shutil.which("systemctl", path="/usr/bin:/bin:/usr/local/bin") or "/usr/bin/systemctl"
        return self.cmd("systemctl", [exe, "--user", *args], timeout_s=self.t(timeout), env=self.tool_env(),
                        capture=True, trace=trace)

    _UNIT_KEYS = ("ActiveState", "SubState", "MainPID", "NRestarts", "ExecMainStatus")

    def unit_show(self) -> dict[str, str]:
        """``systemctl --user show homemesh.service``. Polled every second while verifying (up to 30 minutes
        of migrations), so only a change of state reaches the log, and no capture file is kept."""
        res = self.systemctl("show", "-p", "MainPID,ActiveState,SubState,NRestarts,ExecMainStatus,ExecMainCode",
                             UNIT, trace=False)
        out: dict[str, str] = {}
        for line in res.out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        state = tuple(out.get(k, "") for k in self._UNIT_KEYS)
        if res.ok and state != self._unit_logged:
            self._unit_logged = state
            self.log(f"{UNIT}: " + " ".join(f"{k}={v or '-'}" for k, v in zip(self._UNIT_KEYS, state, strict=True)))
        return out

    def _request_stop(self, ident: _Proc | None) -> None:
        with contextlib.suppress(OSError):
            atomic_write_bytes(self.a["stop_request_file"], str(ident.pid if ident else "").encode())
        if ident is not None and not self.windows and self.alive(ident):
            with contextlib.suppress(OSError):
                os.kill(ident.pid, signal.SIGTERM)

    def _remove_stop_request(self) -> None:
        _unlink_file(self.a["stop_request_file"])

    def _force_kill(self, ident: _Proc | None) -> None:
        if ident is None or not self.alive(ident):  # identity re-checked right before
            return
        if self.windows:
            with contextlib.suppress(Exception):
                if ident.handle:
                    _kernel32().TerminateProcess(ident.handle, 1)
            return
        with contextlib.suppress(OSError):
            os.kill(ident.pid, signal.SIGKILL)

    def _wait_stopped(self, ident: _Proc | None, seconds: float) -> bool:
        deadline = AwakeDeadline(seconds, self.h.monotonic)
        while True:
            if self.lock_free() and not self.alive(ident):
                if ident is not None and ident.popen is not None:
                    with contextlib.suppress(Exception):
                        ident.popen.wait(timeout=0)
                return True
            if deadline.expired():
                return False
            self.h.sleep(min(0.25, self.h.poll_s))

    def stop_homemesh(self, known: _Proc | None = None) -> bool:
        """D.2-D.6 (also used by the rollback, G.1): stop HomeMesh and take the instance lock. False if it
        did not stop (the stop request is withdrawn so it doesn't stop later)."""
        ident = self.identify() or known
        if self.systemd:
            self.systemctl("stop", UNIT)  # always: cancels a queued auto-restart; no-op when inactive
        if not self.lock_free() or self.alive(ident):
            ident = self.identify() or ident
            self._request_stop(ident)
            if not self._wait_stopped(ident, self.t("stop")):
                self.log("HomeMesh did not stop in time; stopping it forcibly")
                if self.systemd:
                    self.systemctl("kill", "-s", "SIGKILL", UNIT)
                    self.systemctl("stop", UNIT)
                self._force_kill(self.identify() or ident)
                if not self._wait_stopped(ident, self.t("forced_stop")):
                    self._remove_stop_request()
                    return False
        deadline = AwakeDeadline(self.t("instance_lock"), self.h.monotonic)
        while not self.instance_lock.acquire():
            if deadline.expired():
                self._remove_stop_request()
                return False
            self.h.sleep(min(0.25, self.h.poll_s))
        self._remove_stop_request()
        return True

    def start_homemesh(self) -> _Started:
        """F.1: start whatever the stable link points to (never through the venv's CLI)."""
        self.instance_lock.release()
        wall = time.time()
        if self.systemd:
            self.systemctl("reset-failed", UNIT)
            res = self.systemctl("start", UNIT)
            show = self.unit_show()
            return _Started(kind="systemd", wall=wall, main_pid=_int(show.get("MainPID")),
                            nrestarts=_int(show.get("NRestarts")), start_ok=res.ok,
                            detail=clean_line(res.tail(3, 200), 200))
        argv = [venv_python(self.stable, windowless=self.windows), "-m", "homemesh", "run"]
        env = self.homemesh_env(detached=True)
        self.log("$ " + _display(argv))
        try:
            if self.h.spawn is not None:
                popen = self.h.spawn(argv, env=env, cwd=self.a["data_dir"])
            else:
                popen = _spawn_detached(argv, env=env, cwd=self.a["data_dir"])
        except OSError as exc:
            self.log(f"  -> could not start HomeMesh: {exc}")
            return _Started(kind="process", wall=wall, start_ok=False, detail=f"could not start it: {exc}")
        self.last_started = _Started(kind="process", wall=wall, popen=popen)
        return self.last_started

    def started_one_is_booting(self) -> bool:
        """The process this runner started last is alive but may not hold the instance lock yet (Python is
        still starting): starting another one would only race it for the lock."""
        s = self.last_started
        return s is not None and s.popen is not None and s.popen.poll() is None

    def _dead(self, s: _Started, proc: _Proc | None) -> str | None:
        """Why the started HomeMesh is no longer the same running process, or None while it is."""
        if not s.start_ok:
            return s.detail or "it could not be started"
        if s.kind == "systemd":
            show = self.unit_show()
            state = show.get("ActiveState", "")
            if state not in ("active", "reloading"):
                status = show.get("ExecMainStatus", "")
                return f"{state or 'not running'}, exit status {status or 'unknown'}"
            if _int(show.get("NRestarts")) != s.nrestarts:
                return "systemd restarted it"
            if s.main_pid and _int(show.get("MainPID")) != s.main_pid:
                return "systemd restarted it"
            return None
        rc = s.popen.poll() if s.popen is not None else None
        if rc is not None:
            return f"exit code {rc}"
        if proc is not None and not self.alive(proc):
            return "it exited"
        return None

    def _ready_proc(self, s: _Started) -> _Proc | None:
        """The PID record of a process started by this start, once it says ``ready``."""
        rec = read_json(self.a["pid_file"]) or {}
        if rec.get("ready") is not True:
            return None
        ident = self.identify()
        if ident is None:
            return None
        if ((ident.started is not None and ident.started < s.wall - 2.0)
                or (s.kind == "systemd" and s.main_pid and ident.pid != s.main_pid)):
            _win_close(ident.handle)
            return None
        if s.popen is not None:
            ident.popen = s.popen if ident.pid == getattr(s.popen, "pid", None) else None
        return ident

    def verify(self, s: _Started, *, commit: str | None, rollback: bool = False) -> tuple[str, dict[str, Any] | None]:
        """F.2. Returns ``("ok"|"unconfirmed"|"failed", error)``. For a rollback (the known-good old version)
        ``ok`` only needs the process to be ready and to stay alive while the agent connects."""
        ready = AwakeDeadline(self.t("ready"), self.h.monotonic)
        proc: _Proc | None = None
        connect: AwakeDeadline | None = None
        seen: set[str] = set()
        while True:
            why = self._dead(s, proc)
            if why is not None:
                return "failed", self.warning("update_new_version_crashed", exit=why)
            if proc is None:
                proc = self._ready_proc(s)
                if proc is not None:
                    self.log(f"HomeMesh is ready (process {proc.pid})")
                    connect = AwakeDeadline(self.t("connect") if self.has_worker else self.t("stable"),
                                            self.h.monotonic)
                elif ready.expired():
                    return "failed", self.warning("update_start_timeout", n=round(self.t("ready") / 60))
            if proc is not None and connect is not None:
                if self.has_worker:
                    agent = read_json(os.path.join(self.update_dir, "agent.json")) or {}
                    if self._agent_is(agent, proc):
                        state = agent.get("state")
                        if isinstance(state, str) and state not in seen:
                            self.log(f"agent: {state} {clean_line(str(agent.get('detail') or ''), 200)}")
                            seen.add(state)
                        if state == "connected" and (rollback or agent.get("build_commit") == commit):
                            return "ok", None
                        if state == "rejected" and not rollback:
                            return "failed", self.warning(
                                "update_new_version_rejected",
                                detail=clean_line(str(agent.get("detail") or "no reason given"), 300))
                    if connect.expired():
                        if rollback:
                            return "ok", None
                        if not self.is_hub and "unreachable" in seen and "connected" not in seen:
                            return "unconfirmed", None
                        return "failed", self.warning("update_not_confirmed", n=round(self.t("connect")))
                elif connect.expired():
                    return "ok", None
            self.h.sleep(self.h.poll_s)

    @staticmethod
    def _agent_is(agent: dict[str, Any], proc: _Proc) -> bool:
        if agent.get("pid") != proc.pid:
            return False
        started = agent.get("process_started")
        if isinstance(started, (int, float)) and proc.started is not None:
            return abs(float(started) - proc.started) < 1.0
        return True

    # ----------------------------------------------------------------- database (E.1, G.3)
    def schema_version(self, db: str) -> int:
        with contextlib.closing(sqlite3.connect(db, timeout=30)) as conn:
            try:
                row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row[0] or 0) if row else 0

    def snapshot_db(self, db: str) -> str:
        dest = os.path.join(self.run_dir, SNAPSHOT_NAME)
        os.close(create_private(dest))
        with contextlib.closing(sqlite3.connect(db, timeout=30)) as src, \
                contextlib.closing(sqlite3.connect(dest)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # a plain file: no -wal/-shm next to the snapshot
        return dest

    def restore_db(self, snapshot: str, db: str) -> None:
        """Through the backup API into the live database: a file copy would leave the new version's -wal
        behind, and SQLite would replay the migration from it."""
        with contextlib.closing(sqlite3.connect(snapshot, timeout=30)) as snap, \
                contextlib.closing(sqlite3.connect(db, timeout=30)) as live:
            snap.backup(live)

    def _backup_files(self) -> None:
        for src, name in ((self.a["config_file"], "homemesh.toml.bak"),
                          (self.a["worker_credentials_file"], "worker-credentials.json.bak")):
            if _is_real_file(src):
                with open(src, "rb") as fh:
                    data = fh.read()
                _write_new_private(os.path.join(self.run_dir, name), data)

    def _restore_files(self) -> None:
        for dst, name in ((self.a["config_file"], "homemesh.toml.bak"),
                          (self.a["worker_credentials_file"], "worker-credentials.json.bak")):
            bak = os.path.join(self.run_dir, name)
            if not _is_real_file(bak):
                continue
            try:
                with open(bak, "rb") as fh:
                    saved = fh.read()
                current = None
                if _is_real_file(dst):
                    with open(dst, "rb") as fh:
                        current = fh.read()
                if current != saved:
                    atomic_write_bytes(dst, saved)
                    self.log(f"restored {dst} from its pre-update copy")
            except OSError as exc:
                self.log(f"could not restore {dst}: {exc}")

    # ----------------------------------------------------------------- build artefacts
    def _rm_venv(self, path: str | None) -> None:
        if not path or self._same(path, self.link_target()):
            return
        if (os.path.normcase(os.path.dirname(os.path.abspath(path)))
                != os.path.normcase(os.path.abspath(os.path.join(self.a["prefix"], "venvs")))
                or not is_venv_id(os.path.basename(path))):
            return
        if not _rmtree(path):
            self.log(f"could not delete {path} (the next update retries)")

    def _rm_build(self, path: str | None) -> None:
        if not path:
            return
        if (os.path.normcase(os.path.dirname(os.path.abspath(path)))
                != os.path.normcase(os.path.abspath(os.path.join(self.a["prefix"], "b")))
                or not _BUILD_DIR_RE.fullmatch(os.path.basename(path))):
            return
        if not _rmtree(path):
            self.log(f"could not delete {path} (the next update retries)")

    def discard_build(self) -> None:
        if self.run is None:
            return
        self._rm_build(self.run.get("build_dir"))
        self._rm_venv(self.run.get("to_venv"))

    # ----------------------------------------------------------------- the run
    def main(self) -> int:
        if self.h.inside_homemesh_unit():
            return self.early_exit(EXIT_INSIDE_UNIT, "refusing to run inside homemesh.service")
        if _lexists(self.run_file):
            return self.early_exit(EXIT_BAD_ARGS, f"run {self.run_id} already exists")
        update_lock = FileLock(os.path.join(self.update_dir, "update.lock"))
        if not update_lock.acquire():
            return self.early_exit(EXIT_BUSY, "another update or check is running (update.lock is held)")
        awake = _KeepAwake(self)
        previous_handler: Any = None
        try:
            try:
                self.repair_link()
            except OSError as exc:  # prechecks then refuse the broken layout (update_needs_installer)
                self.log(f"could not repair the program link: {exc}")
            try:
                self.accept()
            except Exception as exc:
                self.log("could not record the run:\n" + traceback.format_exc())
                if self.run is not None:
                    with contextlib.suppress(Exception):
                        self.finish("failed", error=self.warning("update_launch_failed",
                                                                 detail=clean_line(f"recording the run failed: {exc}",
                                                                                   200)),
                                    installed=self.run.get("from_commit"))
                return self.early_exit(EXIT_FAILED, "could not record the run")
            if self.h.keep_awake:
                awake.start()
            if self.h.handle_sigterm and hasattr(signal, "SIGTERM"):
                with contextlib.suppress(ValueError, OSError):  # not the main thread (tests)
                    previous_handler = signal.signal(signal.SIGTERM, self._on_sigterm)
                    self._armed = True
            try:
                self.flow()
                return EXIT_OK
            except Interrupted:
                self.log("stopped by SIGTERM")
                return EXIT_FAILED
            except Exception:
                self.log("unexpected error:\n" + traceback.format_exc())
                return EXIT_FAILED
            finally:
                self._armed = False
                self.guard()
        finally:
            if previous_handler is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signal.SIGTERM, previous_handler)
            awake.stop()
            self.instance_lock.release()
            update_lock.release()

    def _on_sigterm(self, _signum: int, _frame: Any) -> None:
        if self._armed and not self._interrupted:
            self._interrupted = True
            raise Interrupted()

    def accept(self) -> None:
        """A.4: run.json (phase accepted), then latest.json."""
        os.makedirs(self.run_dir, exist_ok=True)
        self._finalize_previous()
        target = self.link_target()
        now = now_iso()
        self.run = {
            "v": 1, "run_id": self.run_id, "job_id": self.a["job_id"], "origin": self.a["origin"],
            "expected_commit": self.a["expected_commit"], "force": bool(self.a["force"]), "phase": "accepted",
            "status": None, "committed": False, "switched": False,
            "from_commit": self.record_commit(target) or self.record_commit(self.a["current_venv"]),
            "to_commit": None, "from_venv": target, "to_venv": None, "build_dir": None, "was_running": None,
            "drain_deadline": None, "db_snapshot": None, "pre_schema": None, "runner_pid": os.getpid(),
            "started_at": now, "updated_at": now, "finished_at": None, "error": None, "warnings": [],
            "log": self.a["log"], "installed_commit": None,
        }
        atomic_write_json(self.run_file, self.run)
        atomic_write_json(os.path.join(self.update_dir, "latest.json"), {"v": 1, "run_id": self.run_id})
        self.log(f"apply {self.run_id} (origin {self.a['origin']}, job {self.a['job_id']}) accepted; "
                 f"target {self.a['expected_commit']}")
        if self.h.on_phase is not None:
            self.h.on_phase("accepted", self.run)

    def _finalize_previous(self) -> None:
        """The run latest.json names has no final status but we hold the update lock: its runner is dead.
        Finalize it like ``files.finalize_dead_run`` before it stops being the latest."""
        latest = read_json(os.path.join(self.update_dir, "latest.json")) or {}
        rid = latest.get("run_id")
        if not is_run_id(rid) or rid == self.run_id:
            return
        path = os.path.join(self.update_dir, "runs", rid, "run.json")
        run = read_json(path)
        if run is None or run.get("status") is not None:
            return
        commit = self.record_commit(self.link_target())
        now = now_iso()
        run.update(status="interrupted", installed_commit=commit, finished_at=now, updated_at=now,
                   error=issue("update_interrupted", phase=run.get("phase"), computer=self.a["computer"],
                               commit=short_sha(commit)))
        with contextlib.suppress(OSError):
            atomic_write_json(path, run)
            self.log(f"finalized the earlier run {rid} as interrupted")

    def flow(self) -> None:
        try:
            self.prechecks()
            if self.run and self.run.get("status") is not None:
                return
            self.build()
            self.require_consent()  # B.8
            self.drain()
            self.require_consent()  # design 4: again immediately before stopping HomeMesh
        except Fail as f:
            self.discard_build()
            self.finish("failed", error=f.err, installed=self.run.get("from_commit") if self.run else None)
            return
        if not self.stop_for_update():
            return
        if not self.switch():
            return
        self.start_and_verify()

    # ----------------------------------------------------------------- B. prechecks, building
    def require_consent(self) -> None:
        """Remote runs only: the owner's consent marker must still be there (read live, design 4)."""
        if self.a["origin"] == "remote" and not self.consent_given():
            raise self.fail("update_not_allowed")

    def prechecks(self) -> None:
        self.phase("prechecks")
        self.require_consent()  # before the fetch
        self.check_git()
        self.up = self.source_state()
        self.fetch(self.up)
        sha = self.resolve_upstream(self.up)
        if sha != self.a["expected_commit"]:  # string comparison; from here on only `sha` is used
            raise self.fail("update_upstream_changed", short=short_sha(sha))
        self.sha = sha
        self.save(to_commit=sha)
        self.facts = self.inspect(sha, self.up)
        assert self.run is not None
        if self.run.get("from_commit") == sha:  # B.2: nothing to build, nothing is stopped
            self.ff_checkout(sha)
            self.finish("already_up_to_date", installed=sha)
            return
        for p in self.facts.problems:
            if p["blocking"]:
                raise Fail({k: v for k, v in p.items() if k != "blocking"})
        for p in self.facts.problems:
            if not any(w["code"] == p["code"] for w in self.warnings):
                self.warnings.append({k: v for k, v in p.items() if k != "blocking"})
        self.save()

    def ff_checkout(self, sha: str) -> None:
        """``git merge --ff-only <sha>`` in the checkout; best effort (warning ``checkout_not_advanced``)."""
        res = self.git("merge", "--ff-only", "--end-of-options", sha)
        if not res.ok:
            self.warn("checkout_not_advanced")

    def build(self) -> None:
        assert self.sha is not None and self.facts is not None
        prefix = self.a["prefix"]
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        to_venv = os.path.join(prefix, "venvs", f"{self.sha[:12]}-{ts}")
        build = os.path.join(prefix, "b", secrets.token_hex(4))
        self.phase("building", to_venv=to_venv, build_dir=build)
        try:
            os.makedirs(os.path.join(prefix, "venvs"), exist_ok=True)
            os.makedirs(build)
            if _lexists(to_venv):
                raise OSError(errno.EEXIST, "already exists", to_venv)
        except OSError as exc:
            raise self.fail("update_venv_failed", detail=clean_line(str(exc), 200)) from None
        tar = os.path.join(build, "src.tar")
        res = self.git("archive", "--format=tar", "-o", tar, "--end-of-options", self.sha, key="git_archive",
                       timeout="git_archive")
        if not res.ok:
            raise self.fail("update_venv_failed", detail="git archive failed: " + res.tail(3, 200))
        src_root = os.path.join(build, "src")
        try:
            _extract(tar, src_root)
        except (OSError, tarfile.TarError, ValueError) as exc:
            raise self.fail("update_venv_failed", detail=f"unpacking the source failed: {exc}") from None
        with contextlib.suppress(OSError):
            os.unlink(tar)
        if self.is_hub:
            self.build_web(src_root)
        res = self.cmd("venv", [self.python, "-m", "venv", to_venv], timeout_s=self.t("venv"),
                       env=self.tool_env(), cwd=build)
        if not res.ok:
            raise self.fail("update_venv_failed", detail=res.tail(5, 300) or f"exit code {res.returncode}")
        py = venv_python(to_venv)
        pip = [py, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
        res = self.cmd("pip_upgrade", [*pip, "--upgrade", "pip"], timeout_s=self.t("pip_upgrade"),
                       env=self.tool_env(), cwd=build)
        if not res.ok:
            self.log("upgrading pip failed; continuing with the pip that came with the venv")
        res = self.cmd("pip_install", [*pip, src_root], timeout_s=self.t("pip_install"), env=self.tool_env(),
                       cwd=build)
        if not res.ok:
            raise self.fail("update_install_failed", lines=res.tail(20) or f"exit code {res.returncode}")
        self.write_record(to_venv)
        env = self.homemesh_env()
        res = self.cmd("homemesh_version", [py, "-m", "homemesh", "version"], timeout_s=self.t("homemesh_cli"),
                       env=env, cwd=self.update_dir, capture=True)
        if not res.ok:
            raise self.fail("update_smoke_failed", detail=res.tail(5, 300) or f"exit code {res.returncode}")
        res = self.cmd("config_validate", [py, "-m", "homemesh", "config", "validate"],
                       timeout_s=self.t("homemesh_cli"), env=env, cwd=self.update_dir, capture=True)
        if not res.ok:
            raise self.fail("update_config_incompatible", lines=res.tail(10, 300) or f"exit code {res.returncode}")

    def build_web(self, src_root: str) -> None:
        """B.4 (hub only): reuse the installed UI when apps/web didn't change, else build it with node +
        npm-cli.js (no PowerShell, no .cmd)."""
        assert self.facts is not None
        dest = os.path.join(src_root, "src", "homemesh", "web_dist")
        installed_ui = os.path.join(self.a["current_package_dir"], "web_dist")
        if not self.facts.web_changed and os.path.isfile(os.path.join(installed_ui, "index.html")):
            try:
                shutil.copytree(installed_ui, dest, dirs_exist_ok=True)
            except OSError as exc:
                raise self.fail("update_web_build_failed", lines=f"copying the installed web app: {exc}") from None
            return
        node = self.find_node()
        if node is None:
            if self.facts.web_changed:
                raise self.fail("update_node_missing")
            self.warn("web_ui_missing")
            return
        node_exe, npm_cli = node
        web = os.path.join(src_root, "apps", "web")
        env = self.tool_env(path_first=(os.path.dirname(node_exe),))
        for key, args in (("npm_ci", ["ci", "--no-audit", "--no-fund"]), ("npm_build", ["run", "build"])):
            res = self.cmd(key, [node_exe, npm_cli, *args], timeout_s=self.t(key), env=env, cwd=web)
            if not res.ok:
                raise self.fail("update_web_build_failed", lines=res.tail(20) or f"exit code {res.returncode}")
        dist = os.path.join(web, "dist")
        if not os.path.isfile(os.path.join(dist, "index.html")):
            raise self.fail("update_web_build_failed", lines="the build produced no dist/index.html")
        try:
            shutil.copytree(dist, dest, dirs_exist_ok=True)
        except OSError as exc:
            raise self.fail("update_web_build_failed", lines=f"copying the built web app: {exc}") from None

    def write_record(self, to_venv: str) -> None:
        """B.6: ``<to_venv>/homemesh-install.json`` (``homemesh.update.record.InstallRecord``)."""
        assert self.sha is not None and self.facts is not None and self.up is not None
        current = self.installed_record() or {}
        git = current.get("git") if isinstance(current.get("git"), str) and current.get("git") else self.a["git"]
        node_dir = current.get("node_dir") if isinstance(current.get("node_dir"), str) else self.a.get("node_dir")
        record = {"v": 1, "layout": self.facts.target_layout if self.facts.target_layout < (1 << 30) else 1,
                  "commit": self.sha, "commit_date": self.facts.upstream_date, "dirty": False,
                  "branch": self.up.branch, "remote": self.up.remote, "source_dir": self.src,
                  "venv": os.path.realpath(to_venv), "stable_venv": self.stable, "built_at": now_iso(),
                  "built_by": "updater", "run_id": self.run_id, "git": git, "node_dir": node_dir,
                  "python": ".".join(str(x) for x in sys.version_info[:3])}
        try:
            atomic_write_json(os.path.join(to_venv, RECORD_NAME), record)
        except OSError as exc:
            raise self.fail("update_venv_failed", detail=f"writing the install record failed: {exc}") from None

    # ----------------------------------------------------------------- C. draining
    def drain(self) -> None:
        assert self.run is not None
        running = not self.lock_free()
        self.save(was_running=running)
        if not running or not self.has_worker or self.a["force"]:
            return
        started = datetime.now(UTC)
        self.phase("draining", drain_started_at=now_iso(started),
                   drain_deadline=now_iso(started + timedelta(seconds=self.t("drain"))))
        drain_file = os.path.join(self.run_dir, "drain.json")
        deadline = AwakeDeadline(self.t("drain"), self.h.monotonic)
        last: list[dict[str, Any]] | None = None
        while True:
            data = read_json(drain_file)
            at = parse_iso(data.get("at")) if data else None
            if data is not None and at is not None and at >= started - timedelta(seconds=1) \
                    and isinstance(data.get("running"), list):
                jobs = [j for j in data["running"] if isinstance(j, dict)]
                if not jobs:
                    self.log("drained: no jobs running")
                    return
                last = jobs
            if self.lock_free():
                self.log("HomeMesh stopped by itself while draining")
                return
            if deadline.expired():
                break
            self.h.sleep(self.h.poll_s)
        if last:
            types = ", ".join(sorted({clean_line(str(j.get("type") or "?"), 40) for j in last}))
            raise self.fail("update_jobs_running", n=len(last), types=types)
        raise self.fail("update_jobs_running", n="Some", types="the worker did not confirm it stopped taking jobs")

    # ----------------------------------------------------------------- D. stopping
    def stop_for_update(self) -> bool:
        assert self.run is not None
        changes: dict[str, Any] = {"committed": True}
        if self.run.get("was_running") is None:
            changes["was_running"] = not self.lock_free()
        self.phase("stopping", **changes)
        if self.stop_homemesh():
            return True
        self.discard_build()
        self.finish("failed", error=self.warning("update_stop_failed"), installed=self.run.get("from_commit"))
        return False

    # ----------------------------------------------------------------- E. switching
    def switch(self) -> bool:
        assert self.run is not None and self.facts is not None
        self.phase("switching")
        db = self.a.get("db_file")
        try:
            if db and _is_real_file(db):
                pre = self.schema_version(db)
                self.save(pre_schema=pre)
                if self.facts.target_schema > pre:
                    self.save(db_snapshot=self.snapshot_db(db))
                    self.log(f"database snapshot written (schema {pre}; the new version has "
                             f"{self.facts.target_schema})")
            self._backup_files()
        except (OSError, sqlite3.Error) as exc:
            self.restart_old(self.warning("update_db_snapshot_failed", detail=clean_line(str(exc), 200)))
            return False
        to_venv = self.run["to_venv"]
        self.save(from_venv=self.link_target(), to_venv=to_venv)
        try:
            self.point_link(to_venv)
        except OSError as exc:
            self.restart_old(self.warning("update_switch_failed", detail=clean_line(str(exc), 200)))
            return False
        self.save(switched=True)
        self.log(f"switched {self.stable} -> {to_venv}")
        if self.systemd:
            res = self.service_install()
            if not res.ok:
                self.rollback(self.warning("update_switch_failed",
                                           detail="refreshing the service unit failed: " + res.tail(3, 200)))
                return False
        return True

    def service_install(self) -> CmdResult:
        """``<stable_venv>/bin/python -m homemesh service install`` (refreshes the unit; E.3 and G.5)."""
        return self.cmd("service_install", [venv_python(self.stable), "-m", "homemesh", "service", "install"],
                        timeout_s=self.t("homemesh_cli"), env=self.homemesh_env(), cwd=self.update_dir,
                        capture=True)

    def restart_old(self, err: dict[str, Any]) -> None:
        """A failure after stopping but before the link flipped: start the old version again; final failed."""
        assert self.run is not None
        self.set_error(err)
        self.discard_build()
        if self.run.get("was_running"):
            self.phase("rolling_back")
            started = self.start_homemesh()
            outcome, _ = self.verify(started, commit=self.run.get("from_commit"), rollback=True)
            if outcome == "failed":
                self.keep_stopped = True
                self.finish("failed", error=self._rollback_failed(), override=True)
                return
        self.finish("failed", installed=self.run.get("from_commit"))

    def _rollback_failed(self) -> dict[str, Any]:
        return self.warning("update_rollback_failed",
                            installer_command=installer_command(_os_name(), self.src, pull=False))

    # ----------------------------------------------------------------- F. starting, verifying
    def start_and_verify(self) -> None:
        assert self.run is not None
        if not self.run.get("was_running"):
            self.instance_lock.release()
            self.warn("not_started")
            self.cleanup("ok")
            return
        self.phase("starting")
        started = self.start_homemesh()
        self.phase("verifying")
        outcome, err = self.verify(started, commit=self.sha)
        if outcome == "ok":
            self.cleanup("ok")
        elif outcome == "unconfirmed":
            self.warn("not_confirmed")
            self.cleanup("ok_unconfirmed")
        else:
            self.rollback(err or self.warning("update_not_confirmed", n=round(self.t("connect"))))

    # ----------------------------------------------------------------- G. rollback
    def rollback(self, err: dict[str, Any]) -> None:
        assert self.run is not None
        self.set_error(err)  # the original error stays the run's error
        self.phase("rolling_back")
        if not self.instance_lock.held and not self.stop_homemesh():
            self.keep_stopped = True
            self.finish("failed", error=self._rollback_failed(), override=True, installed=None)
            return
        if self.run.get("switched"):
            from_venv = self.run.get("from_venv")
            try:
                if not from_venv or not _is_real_dir(from_venv):
                    raise OSError(errno.ENOENT, "the previous version's folder is missing", from_venv)
                self.point_link(from_venv)
            except OSError as exc:
                self.log(f"could not switch back: {exc}")
                self.keep_stopped = True
                self.finish("failed", error=self._rollback_failed(), override=True, installed=None)
                return
            self.save(switched=False)
            self.log(f"switched back {self.stable} -> {from_venv}")
        snapshot, db, pre = self.run.get("db_snapshot"), self.a.get("db_file"), self.run.get("pre_schema")
        if snapshot and db and isinstance(pre, int):
            try:
                if self.schema_version(db) != pre:
                    self.restore_db(snapshot, db)
                    self.log("database restored from the pre-update snapshot")
                ok = self.schema_version(db) == pre
            except (OSError, sqlite3.Error) as exc:
                self.log(f"database restore failed: {exc}")
                ok = False
            if not ok:
                self.keep_stopped = True
                self.finish("failed", error=self.warning("update_db_restore_failed", path=snapshot),
                            override=True, installed=None)
                return
        self._restore_files()
        if self.systemd:
            res = self.service_install()
            if not res.ok:
                self.log("refreshing the service unit with the old version failed; continuing")
        if self.run.get("was_running"):
            started = self.start_homemesh()
            outcome, _ = self.verify(started, commit=self.run.get("from_commit"), rollback=True)
            if outcome == "failed":
                self.keep_stopped = True
                self.finish("failed", error=self._rollback_failed(), override=True, installed=None)
                self._rm_venv(self.run.get("to_venv"))
                return
        else:
            self.instance_lock.release()
        self.finish("rolled_back", installed=self.run.get("from_commit"))
        self._rm_build(self.run.get("build_dir"))
        self._rm_venv(self.run.get("to_venv"))

    # ----------------------------------------------------------------- H. cleanup
    def cleanup(self, status: str) -> None:
        assert self.run is not None and self.sha is not None
        self.finish(status, installed=self.sha, phase="cleanup")  # the outcome first
        for step in (lambda: self.ff_checkout(self.sha or ""), lambda: self._rm_build(self.run.get("build_dir")),
                     self.prune_builds, self.prune_venvs,
                     lambda: prune_runs(self.update_dir, self.a["logs_dir"]),
                     lambda: prune_checks(self.update_dir, self.a["logs_dir"])):
            try:
                step()
            except Exception as exc:
                self.log(f"cleanup step failed: {exc}")

    def prune_venvs(self) -> None:
        """Keep the link target and the previous version (this run's ``from_venv``, else the newest other
        venv); delete the other real ``venvs/<id>`` directories (H.2). A leftover from an interrupted run
        must not push the previous version out."""
        venvs = os.path.join(self.a["prefix"], "venvs")
        target = self.link_target()
        previous = (self.run or {}).get("from_venv")
        names = [n for n in _list_names(venvs) if is_venv_id(n) and _is_real_dir(os.path.join(venvs, n))]
        others = sorted((n for n in names if not self._same(os.path.join(venvs, n), target)),
                        key=lambda n: (n.rsplit("-", 1)[-1], n))
        keep = [n for n in others if self._same(os.path.join(venvs, n), previous)] or others[-1:]
        for name in others:
            if name not in keep:
                self._rm_venv(os.path.join(venvs, name))

    def prune_builds(self) -> None:
        """Build dirs left by interrupted runs (only this runner builds; it holds the update lock)."""
        base = os.path.join(self.a["prefix"], "b")
        with contextlib.suppress(OSError):
            for name in _list_names(base):
                if _BUILD_DIR_RE.fullmatch(name) and _is_real_dir(os.path.join(base, name)):
                    self._rm_build(os.path.join(base, name))

    # ----------------------------------------------------------------- I. the top-level guard
    def guard(self) -> None:
        """Ends every run that didn't reach a final status (an exception, SIGTERM): if it was committed and
        no HomeMesh runs, start whatever the link points to; then write ``interrupted``."""
        if self.run is None or self.run.get("status") is not None:
            return
        try:
            with contextlib.suppress(OSError):
                self.repair_link()  # a SIGTERM between the two renames of a Windows junction swap
            if self.run.get("committed") and self.run.get("was_running") and not self.keep_stopped:
                self.instance_lock.release()
                if self.started_one_is_booting():
                    self.log("the HomeMesh started before the interruption is still running; leaving it")
                elif self.lock_free():
                    self.log("starting HomeMesh again after the interruption")
                    self.start_homemesh()
                    self.save(guard_started=True)
            if not self.run.get("switched"):
                self.discard_build()
        except BaseException as exc:
            self.log(f"guard: {exc!r}")
        commit = self.record_commit(self.link_target())
        with contextlib.suppress(BaseException):
            self.finish("interrupted", installed=commit,
                        error=self.warning("update_interrupted", phase=self.run.get("phase"),
                                           commit=short_sha(commit)))


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _rename_retry(src: str, dst: str, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.rename(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _spawn_detached(argv: list[str], *, env: dict[str, str], cwd: str) -> subprocess.Popen[bytes]:
    """Start ``homemesh run`` so it outlives this runner (same flags as ``cli/service.py::start``)."""
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "env": env, "cwd": cwd, "close_fds": True}
    if sys.platform != "win32":
        return subprocess.Popen(argv, start_new_session=True, **kwargs)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 5:  # the job forbids breakaway
            raise
        return subprocess.Popen(argv, creationflags=flags, **kwargs)


def _extract(tar_path: str, dest: str) -> None:
    """Unpack ``git archive`` output; refuses absolute names, ``..``, links pointing outside and anything
    that would be written through a link.

    Without ``tarfile.data_filter`` (Python 3.11.0-3.11.3) the same rules are applied member by member, each
    checked against what is already on disk (a symlink extracted earlier is resolved for real, as the data
    filter does), and only the member types ``git archive`` writes are accepted: no hard links, devices or
    FIFOs."""
    os.makedirs(dest)
    with tarfile.open(tar_path, "r:") as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
            return
        root = os.path.realpath(dest)

        def inside(path: str) -> bool:
            real = os.path.realpath(path)
            return real == root or real.startswith(root + os.sep)

        trusted = {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
        for m in tf.getmembers():
            name = m.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/") or re.match(r"[A-Za-z]:", name):
                raise ValueError(f"unsafe path in archive: {m.name}")
            if not (m.isreg() or m.isdir() or m.issym()):
                raise ValueError(f"unexpected member type in archive: {m.name}")
            if not inside(os.path.join(dest, name)):
                raise ValueError(f"path through a link in archive: {m.name}")
            if m.issym() and not inside(os.path.join(dest, os.path.dirname(name), m.linkname)):
                raise ValueError(f"link pointing outside the archive: {m.name}")
            tf.extract(m, dest, **trusted)  # checked above, against the disk as it is now


# =========================================================================== args.json (design 8.1)

ARGS_KEYS = ("v", "mode", "run_id", "job_id", "origin", "expected_commit", "force", "computer", "source_dir",
             "prefix", "stable_venv", "current_venv", "current_package_dir", "data_dir", "config_dir", "logs_dir",
             "update_dir", "config_file", "db_file", "worker_credentials_file", "pid_file", "instance_lock",
             "stop_request_file", "consent_file", "roles", "service", "unit_path", "git", "node_dir",
             "homemesh_home", "log", "out")
_PATH_KEYS = ("source_dir", "prefix", "stable_venv", "current_venv", "current_package_dir", "data_dir", "config_dir",
              "logs_dir", "update_dir", "config_file", "worker_credentials_file", "pid_file", "instance_lock",
              "stop_request_file", "consent_file", "git", "log")
_OPTIONAL_PATH_KEYS = ("db_file", "unit_path", "node_dir", "homemesh_home", "out")
_ROLES = frozenset({"coordinator", "worker", "client"})


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def load_args(path: Path, mode: str) -> dict[str, Any]:
    """Read and validate ``args.json`` field by field (``homemesh.update.files.RunnerArgs`` rules); its
    ``mode`` must equal the command-line ``mode``. Raises :class:`ArgsError`.

    Existence of ``git``, ``node_dir`` and ``source_dir/.git`` is checked by the steps themselves, so they
    end in their own error codes (``update_git_missing`` …) instead of a bare launch failure."""
    data = read_json(path)
    if data is None:
        raise ArgsError("not a readable JSON object")

    def need(ok: bool, key: str, what: str) -> None:
        if not ok:
            raise ArgsError(f"{key}: {what}")

    need(data.get("v") == 1, "v", "must be 1")
    need(data.get("mode") in MODES and data.get("mode") == mode, "mode", f"must be {mode!r}")
    need(is_run_id(data.get("run_id")), "run_id", "not a run id")
    job_id = data.get("job_id")
    need(job_id is None or (isinstance(job_id, str) and 0 < len(job_id) <= ID_MAX
                            and clean_line(job_id, ID_MAX) == job_id), "job_id", "must be null or a short id")
    need(data.get("origin") in ("remote", "local"), "origin", "must be remote or local")
    need(isinstance(data.get("force"), bool), "force", "must be true or false")
    if mode == "apply":
        need(is_sha(data.get("expected_commit")), "expected_commit", "must be a full commit sha")
    else:
        need(data.get("expected_commit") is None, "expected_commit", "must be null for a check")
        need(data.get("force") is False, "force", "must be false for a check")
    computer = data.get("computer")
    need(isinstance(computer, str) and 0 < len(computer) <= NAME_MAX, "computer", "must be a short name")
    for key in _PATH_KEYS:
        value = data.get(key)
        need(isinstance(value, str) and os.path.isabs(value) and "\x00" not in value, key, "must be an absolute path")
    for key in _OPTIONAL_PATH_KEYS:
        value = data.get(key)
        need(value is None or (isinstance(value, str) and os.path.isabs(value) and "\x00" not in value), key,
             "must be null or an absolute path")
    roles = data.get("roles")
    need(isinstance(roles, list) and bool(roles) and all(isinstance(r, str) for r in roles)
         and set(roles) <= _ROLES, "roles", "must be a non-empty list of coordinator/worker/client")
    need(data.get("service") in ("systemd", "windows", "posix"), "service", "must be systemd, windows or posix")
    need((data.get("service") == "systemd") == (data.get("unit_path") is not None), "unit_path",
         "is required with service systemd, and only then")
    need(data.get("db_file") is None or "coordinator" in roles, "db_file", "only with the coordinator role")
    need(os.path.isdir(data["source_dir"]), "source_dir", "must be an existing folder")
    need(_same_path(data["prefix"], os.path.dirname(data["stable_venv"])), "prefix",
         "must be the folder holding stable_venv")
    need(_same_path(data["update_dir"], os.path.join(data["data_dir"], "update")), "update_dir",
         "must be <data_dir>/update")
    need(os.path.isdir(data["update_dir"]), "update_dir", "must exist")
    run_id = data["run_id"]
    if mode == "apply":
        need(_same_path(data["log"], os.path.join(data["logs_dir"], f"update-{run_id}.log")), "log",
             "must be <logs>/update-<run_id>.log")
        need(data.get("out") is None, "out", "must be null for apply")
    else:
        need(_same_path(data["log"], os.path.join(data["logs_dir"], f"update-check-{run_id}.log")), "log",
             "must be <logs>/update-check-<run_id>.log")
        need(isinstance(data.get("out"), str)
             and _same_path(data["out"], os.path.join(data["update_dir"], "checks", f"{run_id}.json")), "out",
             "must be <update>/checks/<run_id>.json")
    return dict(data)


def running_from_venv() -> bool:
    """Started through a venv's interpreter? ``sys.prefix != sys.base_prefix`` alone can't tell under ``-S``
    (site.py is what switches the prefix), so also look for the venv's ``pyvenv.cfg`` next to (or one level
    above) the unresolved ``sys.executable``, which is how CPython itself recognises a venv (on Windows the
    venv launcher passes its own path as ``sys.executable``)."""
    if sys.prefix != sys.base_prefix:
        return True
    exe = sys.executable
    if not exe:
        return False
    here = os.path.dirname(os.path.abspath(exe))
    return any(os.path.isfile(os.path.join(d, "pyvenv.cfg")) for d in (here, os.path.dirname(here)))


def run_check(args: dict[str, Any], hooks: Hooks | None = None) -> int:
    """Check mode (design section 10): write ``args["out"]``; return an ``EXIT_*`` code."""
    return _Check(args, hooks).main()


def run_apply(args: dict[str, Any], hooks: Hooks | None = None) -> int:
    """Apply mode (design section 11): phases accepted → … → cleanup with run.json written before each
    action; return an ``EXIT_*`` code."""
    return _Apply(args, hooks).main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homemesh-update-runner",
                                     description="HomeMesh update runner (started by HomeMesh; not for direct use)")
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--args", required=True, dest="args_file", metavar="ARGS_JSON",
                        help="absolute path of the args.json written by the launcher")
    ns = parser.parse_args(argv)  # exits with EXIT_BAD_ARGS (2) on a bad command line
    if running_from_venv():
        print(f"{EXIT_MARKER} {EXIT_NO_BASE_PYTHON}: must run with the base Python, not the venv interpreter "
              f"{sys.executable}", file=sys.stderr, flush=True)
        return EXIT_NO_BASE_PYTHON
    try:
        args = load_args(Path(ns.args_file), ns.mode)
    except ArgsError as exc:
        print(f"homemesh update runner: invalid {ns.args_file}: {exc}", file=sys.stderr)
        print(f"{EXIT_MARKER} {EXIT_BAD_ARGS}: invalid args.json", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    if ns.mode == "check":
        return run_check(args)
    return run_apply(args)


if __name__ == "__main__":
    sys.exit(main())
