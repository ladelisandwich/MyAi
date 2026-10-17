"""Helpers for the updater tests: real temp git repos (bare origin + clone), a fake blue/green install
layout, fakes for the runner's command table, the fake HomeMesh process (``fake_homemesh.py``) and a fake
``systemctl --user`` for homemesh.service.

Run as a script (``python update_support.py apply-child <spec.json>``) it runs one apply in a separate
process with these fakes, so a test can kill that process at any phase.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

import homemesh
from homemesh.common.paths import AppPaths
from homemesh.update import consent, files, record, runner
from homemesh.update.runner import CmdResult

GIT = shutil.which("git") or "git"
FAKE_HOMEMESH = Path(__file__).with_name("fake_homemesh.py")
HOMEMESH_SRC = Path(homemesh.__file__).resolve().parents[1]  # put on PYTHONPATH of the fake processes
OLD_TS = "20260101000000"
FAST_TIMEOUTS = {"drain": 1.5, "stop": 2.0, "forced_stop": 2.0, "instance_lock": 3.0, "ready": 10.0,
                 "connect": 1.5, "stable": 0.5}

BASE_FILES: dict[str, str] = {
    "pyproject.toml": "[project]\nname = \"homemesh-test\"\n",
    "src/homemesh/update/layout.txt": "1\n",
    "src/homemesh/coordinator/db/migrations/0001_initial.sql": "-- 1\n",
    "src/homemesh/coordinator/db/migrations/0002_two.sql": "-- 2\n",
    "src/homemesh/coordinator/db/migrations/0003_three.sql": "-- 3\n",
    "apps/web/index.html": "<html></html>\n",
    "behavior.json": "{}\n",
}


def git(*args: str, cwd: str | os.PathLike[str] | None = None, check: bool = True) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid", "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run([GIT, *args], cwd=cwd, env=env, capture_output=True, check=False)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr.decode('utf-8', 'replace')}")
    return r.stdout.decode("utf-8", "replace").strip()


class Repo:
    """``origin.git`` (bare), ``src`` (the install's checkout) and ``work`` (where "upstream" commits are made)."""

    def __init__(self, root: Path, base: dict[str, str] | None = None) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.src = root / "src"
        self.work = root / "work"
        root.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "--bare", "-b", "main", str(self.origin))
        git("clone", "-q", str(self.origin), str(self.work))
        git("-C", str(self.work), "checkout", "-q", "-B", "main")
        self.base = self.commit(base or BASE_FILES, "initial")
        git("clone", "-q", str(self.origin), str(self.src))

    def commit(self, changes: dict[str, str | bytes | None], message: str | bytes, *, push: bool = True) -> str:
        for rel, content in changes.items():
            path = self.work / rel
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        git("-C", str(self.work), "add", "-A")
        msg = self.root / "msg.txt"
        msg.write_bytes(message if isinstance(message, bytes) else message.encode("utf-8"))
        git("-C", str(self.work), "commit", "-q", "--allow-empty", "-F", str(msg))
        if push:
            git("-C", str(self.work), "push", "-q", "origin", "main")
        return git("-C", str(self.work), "rev-parse", "HEAD")

    def empty_commits(self, n: int) -> None:
        """``n`` empty commits on main in ``work`` with one ``git fast-import`` (not pushed)."""
        head = git("-C", str(self.work), "rev-parse", "HEAD")
        stream = []
        for i in range(n):
            msg = f"change {i}".encode()
            stream.append(b"commit refs/heads/main\n")
            stream.append(f"committer Test <test@example.invalid> {1700000000 + i} +0000\n".encode())
            stream.append(f"data {len(msg)}\n".encode() + msg + b"\n")
            if i == 0:
                stream.append(f"from {head}\n".encode())
            stream.append(b"\n")
        subprocess.run([GIT, "-C", str(self.work), "fast-import", "--quiet"], input=b"".join(stream), check=True,
                       capture_output=True)
        git("-C", str(self.work), "reset", "-q", "--hard", "main")

    def head(self) -> str:
        return git("-C", str(self.src), "rev-parse", "HEAD")


def make_link(target: Path, link: Path) -> None:
    """The stable ``venv`` path: a relative symlink (POSIX) or a junction (Windows, no admin needed)."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(os.path.relpath(target, link.parent), link)


class Layout:
    """A HomeMesh home (``HOMEMESH_HOME`` layout) and an install prefix with ``venvs/<id>`` + the ``venv`` link."""

    def __init__(self, tmp: Path, repo: Repo, *, roles: tuple[str, ...] = ("worker",),
                 commit: str | None = None) -> None:
        self.tmp, self.repo, self.roles = tmp, repo, roles
        self.home = tmp / "home"
        self.paths = AppPaths(config_dir=self.home, data_dir=self.home / "data")
        self.paths.ensure()
        self.paths.config_file.write_text("# test config\n", encoding="utf-8")
        self.paths.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.paths.worker_credentials_file.write_text('{"node_id": "n1"}\n', encoding="utf-8")
        self.prefix = tmp / "prefix"
        self.stable = self.prefix / "venv"
        self.pkg = tmp / "pkg"
        migrations = self.pkg / "coordinator" / "db" / "migrations"
        migrations.mkdir(parents=True)
        for name in ("0001_initial.sql", "0002_two.sql", "0003_three.sql"):
            (migrations / name).write_text("--\n", encoding="utf-8")
        (self.pkg / "web_dist").mkdir()
        (self.pkg / "web_dist" / "index.html").write_text("<html>installed</html>\n", encoding="utf-8")
        self.old_commit = commit or repo.head()
        self.old_venv = self.make_venv(self.old_commit)
        make_link(self.old_venv, self.stable)
        files.ensure_update_dir(self.paths)
        consent.allow(self.paths)
        self.procs: list[subprocess.Popen[bytes]] = []

    def make_venv(self, commit: str | None, ts: str = OLD_TS, behavior: dict[str, Any] | None = None) -> Path:
        venv = self.prefix / "venvs" / f"{commit[:12] if commit else 'nogit'}-{ts}"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("", encoding="utf-8")
        rec = record.make_record(venv=venv, stable_venv=self.stable, built_by="installer", source_dir=self.repo.src,
                                 commit=commit, branch="main", remote="origin", git=GIT,
                                 built_at="2026-01-01T00:00:00.000Z")
        record.write(rec)
        (venv / "fake-behavior.json").write_text(json.dumps(behavior or {}), encoding="utf-8")
        return venv

    def args(self, mode: str = "apply", *, expected: str | None = None, force: bool = False, origin: str = "remote",
             service: str = "posix", roles: tuple[str, ...] | None = None, node_dir: str | None = None,
             run_id: str | None = None, git_path: str | None = None) -> dict[str, Any]:
        run_id = run_id or files.new_run_id()
        a = files.build_runner_args(
            self.paths, mode=mode, run_id=run_id, computer="testbox", roles=list(roles or self.roles),
            source_dir=self.repo.src, stable_venv=self.stable, git=git_path or GIT, node_dir=node_dir,
            service=service, unit_path=(self.home / "homemesh.service") if service == "systemd" else None,
            job_id="job-1" if origin == "remote" else None, origin=origin,
            expected_commit=expected if mode == "apply" else None, force=force,
            current_venv=os.path.realpath(self.stable), current_package_dir=self.pkg,
            homemesh_home=str(self.home), consent_file=consent.marker_path(self.paths))
        path = files.write_runner_args(self.paths, a)
        files.open_private_append(Path(a["log"])).close()
        return runner.load_args(path, mode)

    # ----------------------------------------------------------------- the fake HomeMesh process
    def spawn(self, argv: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None
              ) -> subprocess.Popen[bytes]:
        """The runner's ``spawn`` hook: ``<stable>/bin/python -m homemesh run`` → the fake, running from the
        venv the link points to right now."""
        venv = os.path.realpath(os.path.dirname(os.path.dirname(argv[0])))
        return self.spawn_fake(venv, env=env)

    def spawn_fake(self, venv: str | os.PathLike[str] | None = None, *, env: dict[str, str] | None = None
                   ) -> subprocess.Popen[bytes]:
        venv = venv or os.path.realpath(self.stable)
        env = dict(env or os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(HOMEMESH_SRC), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        p = subprocess.Popen([sys.executable, str(FAKE_HOMEMESH), "run", "--home", str(self.home), "--venv", str(venv)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                             start_new_session=sys.platform != "win32")
        self.procs.append(p)
        return p

    def start_homemesh(self, timeout: float = 15.0) -> subprocess.Popen[bytes]:
        """Start the "installed" HomeMesh and wait until it is ready and connected."""
        p = self.spawn_fake()
        self.wait_running(p.pid, timeout)
        return p

    def wait_running(self, pid: int | None = None, timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rec = files.read_json(self.paths.pid_file) or {}
            agent = files.read_agent_file(self.paths) or {}
            if rec.get("ready") and (pid is None or rec.get("pid") == pid) and agent.get("pid") == rec.get("pid"):
                return rec
            time.sleep(0.05)
        raise AssertionError(f"fake HomeMesh did not become ready: {self.fake_log()}")

    def running_pid(self) -> int | None:
        """The fake HomeMesh holding the instance lock right now (PID record pid), or None."""
        rec = files.read_json(self.paths.pid_file) or {}
        pid = rec.get("pid")
        if isinstance(pid, int) and runner.lock_is_held(self.paths.lock_file):
            return pid
        return None

    def running_venv(self) -> str | None:
        """The venv the running fake HomeMesh runs from (from its registry entry)."""
        pid = self.running_pid()
        try:
            return (self.home / "fake-pids" / str(pid)).read_text(encoding="utf-8") if pid else None
        except OSError:
            return None

    def fake_log(self) -> str:
        try:
            return (self.home / "fake-homemesh.log").read_text(encoding="utf-8")
        except OSError:
            return ""

    def cleanup(self) -> None:
        """Kill every fake HomeMesh of this layout, including one a runner (or a runner in a child process)
        started moments ago that hasn't registered itself yet: found by its ``--home`` argument, twice."""
        for attempt in range(2):
            pids = {p.pid for p in self.procs}
            with contextlib.suppress(OSError):
                pids |= {int(n) for n in os.listdir(self.home / "fake-pids") if n.isdigit()}
            home = str(self.home)
            for proc in psutil.process_iter(["cmdline"]):
                with contextlib.suppress(psutil.Error, TypeError):
                    cmd = proc.info["cmdline"] or []
                    if str(FAKE_HOMEMESH) in cmd and home in cmd:
                        pids.add(proc.pid)
            pids.discard(os.getpid())
            for pid in pids:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)
            for p in self.procs:
                with contextlib.suppress(Exception):
                    p.wait(timeout=5)
            if attempt == 0:
                time.sleep(0.5)

    # ----------------------------------------------------------------- inspection
    def link_target(self) -> str:
        return os.path.realpath(self.stable)

    def venvs(self) -> list[str]:
        return sorted(os.listdir(self.prefix / "venvs"))

    def builds(self) -> list[str]:
        try:
            return sorted(os.listdir(self.prefix / "b"))
        except OSError:
            return []

    def run_json(self, run_id: str) -> dict[str, Any]:
        data = files.read_run(self.paths, run_id)
        assert data is not None, f"no run.json for {run_id}"
        return data

    def log_text(self, args: dict[str, Any]) -> str:
        return Path(args["log"]).read_text(encoding="utf-8", errors="replace")

    def make_db(self, version: int = 3) -> None:
        """A coordinator database in WAL mode at schema ``version``, closed cleanly."""
        with contextlib.closing(sqlite3.connect(self.paths.db_file)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                         " checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for v in range(1, version + 1):
                conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?)", (v, f"m{v}", "x", "then"))
            conn.execute("CREATE TABLE users (name TEXT)")
            conn.execute("INSERT INTO users VALUES ('owner')")
            conn.commit()

    def schema(self) -> int:
        with contextlib.closing(sqlite3.connect(self.paths.db_file)) as conn:
            return int(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0)


class Fakes:
    """Command-table fakes for venv/pip/homemesh CLI. Behaviour comes from the target commit's
    ``behavior.json`` (``pip_fail``, ``smoke_fail``, ``config_fail``, ``service_install_fail``), which the
    fake ``pip install`` copies from the extracted build tree into the new venv. ``fail[key] = CmdResult``
    forces a command's result."""

    KEYS = ("venv", "pip_upgrade", "pip_install", "homemesh_version", "config_validate", "service_install")

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail: dict[str, CmdResult] = {}

    @staticmethod
    def _behavior(python: str) -> dict[str, Any]:
        venv = os.path.dirname(os.path.dirname(python))
        try:
            return json.loads(Path(venv, "fake-behavior.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _make(self, key: str) -> Any:
        def fake(argv: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
                 timeout_s: float = 0) -> CmdResult:
            self.calls.append((key, list(argv)))
            if key in self.fail:
                return self.fail[key]
            if key == "venv":
                venv = Path(argv[-1])
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").write_text("", encoding="utf-8")
                return CmdResult(0)
            if key == "pip_install":
                src, venv = Path(argv[-1]), Path(os.path.dirname(os.path.dirname(argv[0])))
                behavior = src / "behavior.json"
                shutil.copyfile(behavior, venv / "fake-behavior.json") if behavior.exists() else None
                (venv / "installed-from.txt").write_text(str(src), encoding="utf-8")
                if self._behavior(argv[0]).get("pip_fail"):
                    return CmdResult(1, out="Collecting x\nERROR: No matching distribution found for x\n")
                return CmdResult(0, out="Successfully installed homemesh\n")
            flag = {"homemesh_version": "smoke_fail", "config_validate": "config_fail",
                    "service_install": "service_install_fail"}.get(key)
            if flag and self._behavior(argv[0]).get(flag):
                return CmdResult(1, out=f"{key} failed: broken\n")
            return CmdResult(0, out="ok\n")

        return fake

    def hooks(self, layout: Layout, *, systemd: Any = None) -> runner.Hooks:
        h = runner.Hooks()
        for key in self.KEYS:
            h.commands[key] = self._make(key)
        h.spawn = layout.spawn
        h.keep_awake = False
        h.handle_sigterm = False
        h.timeouts = dict(FAST_TIMEOUTS)
        h.poll_s = 0.05
        if systemd is not None:
            h.commands["systemctl"] = systemd
        return h


class FakeSystemd:
    """``systemctl --user … homemesh.service``: ``start`` runs the fake HomeMesh through the stable link (like
    ``ExecStart=<prefix>/venv/bin/python -m homemesh run``), ``stop`` sends SIGTERM and SIGKILL after
    ``stop_timeout``, ``Restart=on-failure`` respawns a failed process and counts ``NRestarts`` (at most
    ``burst`` times, then ``failed``)."""

    def __init__(self, layout: Layout, *, stop_timeout: float = 3.0, burst: int = 3) -> None:
        self.layout = layout
        self.stop_timeout = stop_timeout
        self.burst = burst
        self.proc: subprocess.Popen[bytes] | None = None
        self.active = "inactive"
        self.nrestarts = 0
        self.status = 0
        self.calls: list[list[str]] = []
        self._lock = threading.RLock()
        self._stopping = False
        self._closed = False
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def __call__(self, argv: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
                 timeout_s: float = 0) -> CmdResult:
        args = argv[2:]  # after "systemctl --user"
        self.calls.append(args)
        verb = args[0] if args else ""
        if verb == "show":
            with self._lock:
                pid = self.proc.pid if self.proc is not None and self.active == "active" else 0
                return CmdResult(0, out=(f"MainPID={pid}\nActiveState={self.active}\nSubState=running\n"
                                         f"NRestarts={self.nrestarts}\nExecMainStatus={self.status}\nExecMainCode=1\n"))
        if verb == "start":
            self.start()
        elif verb == "stop":
            self.stop()
        elif verb == "kill":
            with self._lock:
                if self.proc is not None and self.proc.poll() is None:
                    self.proc.kill()
        elif verb == "reset-failed":
            with self._lock:
                if self.active == "failed":
                    self.active = "inactive"
                self.nrestarts = 0 if self.active == "inactive" else self.nrestarts
        return CmdResult(0)

    def start(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self.proc = self.layout.spawn_fake()
            self.active = "active"

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        with self._lock:
            self.status = p.returncode if p is not None and p.returncode is not None else self.status
            self.active = "inactive"
            self.proc = None
            self._stopping = False

    def _watch(self) -> None:
        while not self._closed:
            time.sleep(0.05)
            with self._lock:
                p = self.proc
                if p is None or self._stopping or p.poll() is None:
                    continue
                self.status = p.returncode
                if p.returncode != 0 and self.nrestarts < self.burst:
                    self.nrestarts += 1
                    self.proc = self.layout.spawn_fake()
                    self.active = "active"
                else:
                    self.active = "failed" if p.returncode else "inactive"
                    self.proc = None

    def close(self) -> None:
        self._closed = True
        self.stop()


# --------------------------------------------------------------------------- a runner in its own process

def apply_child(spec_file: str) -> int:
    """Run one apply with the fakes; ``spec["kill_phase"]`` + ``spec["signal"]`` ("KILL"/"TERM") make the
    runner signal itself right after writing that phase."""
    spec = json.loads(Path(spec_file).read_text(encoding="utf-8"))
    layout = Layout.__new__(Layout)
    layout.home = Path(spec["home"])
    layout.paths = AppPaths(config_dir=layout.home, data_dir=layout.home / "data")
    layout.procs = []
    hooks = Fakes().hooks(layout)
    hooks.handle_sigterm = True
    sig = signal.SIGKILL if spec.get("signal") == "KILL" else signal.SIGTERM

    def on_phase(phase: str, run: dict[str, Any]) -> None:
        if phase == spec["kill_phase"]:
            os.kill(os.getpid(), sig)
            time.sleep(5)  # SIGTERM: the handler raises during this sleep

    hooks.on_phase = on_phase
    args = runner.load_args(Path(spec["args_file"]), "apply")
    return runner.run_apply(args, hooks)


if __name__ == "__main__":
    if sys.argv[1:2] == ["apply-child"]:
        sys.exit(apply_child(sys.argv[2]))
    sys.exit(2)
