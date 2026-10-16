"""The install record (``<venv>/homemesh-install.json``) and the build identity of the running HomeMesh.

Installs use a blue/green layout: every version lives in its own venv under ``<prefix>/venvs/<id>/`` and
the stable path ``<prefix>/venv`` is a symlink (Linux) / junction (Windows) to the current one. Each venv
carries its own record, written **before** the link flips: by ``homemesh update record-install`` (the
installers, run with the new venv's python) or by the runner during an update. The record never contains
remote URLs.

``build_info()`` says what this process runs, without running git (rule 1: never fabricated; unknown is
``None`` with a reason):

* a record whose ``stable_venv`` resolves to ``sys.prefix``: ``source`` = installer | updater, commit from
  the record;
* no record and the package lives in a git checkout (editable install): ``source="dev"``, commit unknown;
* anything else: ``source="unknown"`` with the reason.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import cache
from importlib import resources
from pathlib import Path
from types import EllipsisType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from homemesh.common.paths import AppPaths, default_paths
from homemesh.common.proc import run_quiet
from homemesh.common.timeutil import now_iso
from homemesh.protocol.update import BuildInfo, BuildOS, is_ref_name, is_sha, short_sha
from homemesh.update import consent, files
from homemesh.version import __version__

RECORD_NAME = "homemesh-install.json"
DEV_REASON = "editable/development install; update it with git pull"


@cache
def layout_version() -> int:
    """The install layout this code expects (``update/layout.txt``). A target commit whose layout.txt is
    higher needs the installer once (``update_needs_installer``)."""
    try:
        return int(resources.files("homemesh.update").joinpath("layout.txt").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 1


class InstallRecord(BaseModel):
    """``homemesh-install.json``. Additive-only file contract (unknown fields ignored)."""

    model_config = ConfigDict(extra="ignore")

    v: int = 1
    layout: int = 1
    commit: str | None = None  # full sha the venv was built from; None outside a git checkout
    commit_date: str | None = None  # committer date, ISO-8601
    dirty: bool | None = None  # the checkout had local changes to tracked files when this was built
    branch: str | None = None
    remote: str | None = None  # the remote's *name* (e.g. "origin"), never its URL
    source_dir: str | None = None  # absolute path of the checkout; None when not built from a checkout
    venv: str  # this venv: <prefix>/venvs/<id>
    stable_venv: str  # <prefix>/venv (the link launchers use)
    built_at: str
    built_by: Literal["installer", "updater"]
    run_id: str | None = None  # the update run that built it (built_by "updater")
    git: str | None = None  # absolute git executable recorded from the installer's terminal
    node_dir: str | None = None  # directory holding node / node.exe, recorded the same way
    python: str | None = None  # Python version that built the venv, e.g. "3.13.1"

    @field_validator("commit")
    @classmethod
    def _commit(cls, v: str | None) -> str | None:
        if v is not None and not is_sha(v):
            raise ValueError("commit must be a full lower-case hex sha")
        return v


def record_path(venv: str | os.PathLike[str] | None = None) -> Path:
    """``<venv>/homemesh-install.json``; ``venv`` defaults to ``sys.prefix`` (the running venv)."""
    return Path(venv if venv is not None else sys.prefix) / RECORD_NAME


def load(venv: str | os.PathLike[str] | None = None) -> InstallRecord | None:
    """The record of ``venv`` (default: the running one), or None if missing or invalid. Never raises."""
    data = files.read_json(record_path(venv))
    if data is None:
        return None
    try:
        return InstallRecord.model_validate(data)
    except Exception:
        return None


def write(record: InstallRecord, venv: str | os.PathLike[str] | None = None) -> Path:
    """Write ``record`` atomically into ``venv`` (default: ``record.venv``). Returns the file path."""
    target = record_path(venv if venv is not None else record.venv)
    files.atomic_write_json(target, record.model_dump(mode="json"))
    return target


def make_record(*, stable_venv: str | os.PathLike[str], built_by: Literal["installer", "updater"],
                venv: str | os.PathLike[str] | None = None, source_dir: str | os.PathLike[str] | None = None,
                commit: str | None = None, commit_date: str | None = None, dirty: bool | None = None,
                branch: str | None = None, remote: str | None = None, run_id: str | None = None,
                git: str | os.PathLike[str] | None = None, node_dir: str | os.PathLike[str] | None = None,
                built_at: str | None = None) -> InstallRecord:
    """A record for ``venv`` (default ``sys.prefix``), stamped with this code's layout and Python version.
    Paths are made absolute; ``remote``/``branch`` names that the runner would refuse are dropped.

    ``venv`` is stored **resolved** (the concrete ``<prefix>/venvs/<id>``, even when this runs through the
    stable link); ``stable_venv`` is stored as given (it *is* the link)."""

    def ab(p: str | os.PathLike[str] | None) -> str | None:
        return os.path.abspath(p) if p is not None else None

    return InstallRecord(
        layout=layout_version(), commit=commit, commit_date=commit_date, dirty=dirty,
        branch=branch if branch and is_ref_name(branch) else None,
        remote=remote if remote and is_ref_name(remote) else None,
        source_dir=ab(source_dir), venv=os.path.realpath(venv if venv is not None else sys.prefix),
        stable_venv=ab(stable_venv) or "", built_at=built_at or now_iso(), built_by=built_by, run_id=run_id,
        git=ab(git), node_dir=ab(node_dir), python=".".join(str(x) for x in sys.version_info[:3]))


def checkout_facts(source_dir: str | os.PathLike[str], git: str | os.PathLike[str] | None) -> dict[str, Any]:
    """``commit``, ``commit_date``, ``dirty``, ``branch``, ``remote`` of a checkout, for ``record-install``.

    Runs git (only at install time, never at runtime). Every value is None when unknown: no git, not a
    checkout, detached HEAD, no upstream remote, or a git error.
    """
    facts: dict[str, Any] = {"commit": None, "commit_date": None, "dirty": None, "branch": None, "remote": None}
    src = Path(source_dir)
    if git is None or not (src / ".git").exists():
        return facts
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "",
           "SSH_ASKPASS_REQUIRE": "never"}

    def g(*args: str) -> str | None:
        try:
            r = run_quiet([os.fspath(git), "-C", os.fspath(src), "-c", "core.quotepath=off", *args],
                          capture_output=True, check=False, timeout=30, env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace").strip()

    commit = g("rev-parse", "--verify", "HEAD^{commit}")
    if not is_sha(commit):
        return facts
    facts["commit"] = commit
    facts["commit_date"] = g("log", "-1", "--format=%cI", "HEAD") or None
    status = g("status", "--porcelain=v1", "--untracked-files=no")
    facts["dirty"] = None if status is None else bool(status)
    branch = g("symbolic-ref", "-q", "--short", "HEAD")
    if branch and is_ref_name(branch):
        facts["branch"] = branch
        remote = g("config", "--get", f"branch.{branch}.remote")
        facts["remote"] = remote if remote and is_ref_name(remote) else None
    return facts


# --------------------------------------------------------------------------- build identity

def current_os() -> BuildOS:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _same_dir(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def dev_checkout(package_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """The checkout root when the ``homemesh`` package is imported from ``<checkout>/src/homemesh``."""
    if package_dir is None:
        import homemesh

        package_dir = Path(homemesh.__file__).resolve().parent
    pkg = Path(package_dir)
    root = pkg.parent.parent
    if pkg.parent.name == "src" and (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def _cannot_update(rec: InstallRecord, os_name: BuildOS) -> str | None:
    if os_name == "other":
        return "remote updates work on Linux and Windows only"
    if not rec.source_dir:
        return "it was installed from a folder that is not a git checkout"
    if not (Path(rec.source_dir) / ".git").exists():
        return f"{rec.source_dir} is not a git checkout (any more)"
    return None


def build_info(paths: AppPaths | None = None, *, prefix: str | os.PathLike[str] | None = None,
               package_dir: str | os.PathLike[str] | None = None,
               record: InstallRecord | EllipsisType | None = ...) -> BuildInfo:
    """What this process runs and whether it can be updated remotely (``Hello.build``, ``homemesh version``).

    Cheap (one small file read, a few stats; no git). ``update_allowed`` is the consent marker now.
    ``prefix`` (default ``sys.prefix``), ``package_dir`` and ``record`` are for tests.

    **Never raises** (it runs for every hello): an unexpected error (e.g. a folder the user can't read;
    Python 3.11's ``Path.exists()`` raises ``PermissionError``) gives ``source="unknown"`` with the reason.
    """
    try:
        return _build_info(paths, prefix=prefix, package_dir=package_dir, record=record)
    except Exception as exc:  # the hello must go out; unknown is honest
        reason = f"could not read the install information ({type(exc).__name__}: {exc})"
        return BuildInfo(version=__version__, os=current_os(), source="unknown", reason=reason,
                         can_update=False, cannot_update_reason=reason, update_allowed=False)


def _build_info(paths: AppPaths | None, *, prefix: str | os.PathLike[str] | None,
                package_dir: str | os.PathLike[str] | None,
                record: InstallRecord | EllipsisType | None) -> BuildInfo:
    paths = paths or default_paths()
    venv = Path(prefix if prefix is not None else sys.prefix)
    rec = load(venv) if record is ... else record
    os_name = current_os()
    common: dict[str, Any] = {"version": __version__, "os": os_name, "update_allowed": consent.remote_allowed(paths),
                              "logs_dir": os.fspath(paths.logs_dir)}
    if rec is not None and _same_dir(rec.stable_venv, venv):
        why = _cannot_update(rec, os_name)
        reason = None
        if rec.commit is None:
            reason = why or "the install record names no commit"
        return BuildInfo(**common, commit=rec.commit, commit_date=rec.commit_date, branch=rec.branch,
                         dirty=rec.dirty, source=rec.built_by, reason=reason, installed_at=rec.built_at,
                         can_update=why is None, cannot_update_reason=why, source_dir=rec.source_dir)
    # A record file that exists but doesn't load still means "installed": never call that a dev install.
    checkout = dev_checkout(package_dir) if rec is None and not record_path(venv).exists() else None
    if checkout is not None:
        return BuildInfo(**common, source="dev", reason=DEV_REASON, can_update=False,
                         cannot_update_reason=DEV_REASON, source_dir=os.fspath(checkout))
    if rec is not None:
        reason = f"this HomeMesh runs from {venv}, not from the installed program folder {rec.stable_venv}"
    elif record_path(venv).exists():
        reason = f"the install record {record_path(venv)} is unreadable; run the installer again"
    else:
        reason = "installed without an install record (by an installer from before 0.3.0, or with pip)"
    return BuildInfo(**common, source="unknown", reason=reason, can_update=False, cannot_update_reason=reason)


def format_version_line(info: BuildInfo) -> str:
    """``homemesh version``: ``HomeMesh 0.3.0 (commit a38a3fa, branch main, installed 2026-10-13)`` or
    ``HomeMesh 0.3.0 (commit unknown: editable/development install)``."""
    if info.commit:
        details = [f"commit {short_sha(info.commit)}"]
        if info.branch:
            details.append(f"branch {info.branch}")
        if info.dirty:
            details.append("local changes")
        if info.installed_at:
            details.append(f"installed {info.installed_at[:10]}")
        return f"HomeMesh {info.version} ({', '.join(details)})"
    reason = (info.reason or "not recorded").split(";")[0].strip()
    return f"HomeMesh {info.version} (commit unknown: {reason})"


# --------------------------------------------------------------------------- launcher paths

def stable_python(stable_venv: str | os.PathLike[str], *, console: bool = False) -> Path:
    """The interpreter inside the stable venv path: Linux ``<stable>/bin/python``; Windows
    ``<stable>\\Scripts\\pythonw.exe`` (``python.exe`` with ``console=True``)."""
    base = Path(stable_venv)
    if sys.platform == "win32":
        return base / "Scripts" / ("python.exe" if console else "pythonw.exe")
    return base / "bin" / "python"


def service_python(record: InstallRecord | None = None) -> Path | None:
    """The interpreter ``cli/service.py::_python_for_service()`` should put in the unit / Run key: the one
    under the record's ``stable_venv``, so launchers follow the link instead of pinning one venv (a console
    script from ``~/.local/bin`` runs the concrete ``venvs/<id>`` python). None without a record (or when
    that file doesn't exist); the caller then keeps using ``sys.executable``."""
    rec = record if record is not None else load()
    if rec is None or not rec.stable_venv:
        return None
    exe = stable_python(rec.stable_venv)
    try:
        return exe if exe.exists() else None
    except OSError:
        return None
