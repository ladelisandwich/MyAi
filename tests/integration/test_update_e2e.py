"""End-to-end updates on Linux with the real launcher, the real runner process and real tools.

* ``test_offline_*`` (always run on Linux): the runner is started by ``update.launch`` exactly as HomeMesh
  starts it (base interpreter, ``-I -S``, own session, ``args.json``); it fetches with real git, builds real
  venvs with ``python -m venv`` and real ``pip``, flips the real ``venv`` symlink, stops and starts real
  processes and fast-forwards the checkout. What it installs is a small stand-in HomeMesh
  (``tests/update_standin/``) with an in-tree build backend that has no build requirements, so ``pip`` needs
  no package index: these tests use no network.
* ``test_real_homemesh_*`` (opt-in: ``HOMEMESH_UPDATE_E2E=1``): the same with **this** HomeMesh (the working
  tree, committed into a temporary repository), installed from PyPI (hatchling, FastAPI, …) and, on the hub,
  with the web app built by npm: it needs network access and takes a few minutes. It runs a coordinator-only
  node, because the worker agent's ``agent.json`` (the runner's proof that a worker connected) is written by
  the worker part of the updater.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from homemesh.common.paths import AppPaths
from homemesh.protocol.update import UpdateCheckResult, UpdateRunReport
from homemesh.update import consent, files, launch, record, runner
from update_support import GIT, HOMEMESH_SRC, Repo

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="Linux end-to-end (symlink layout, /proc); Windows is covered by CI's "
                                       "remote-update job")

STANDIN = Path(__file__).resolve().parents[1] / "update_standin"
REPO_ROOT = HOMEMESH_SRC.parent
BASE_PYTHON = launch._base_python_path()


def _base_python_has_venv() -> bool:
    try:
        r = subprocess.run([str(BASE_PYTHON), "-c", "import ensurepip, venv"], capture_output=True, timeout=60,
                           check=False, env={"PATH": "/usr/bin:/bin"})
    except OSError:
        return False
    return r.returncode == 0


needs_venv = pytest.mark.skipif(not _base_python_has_venv(),
                                reason=f"{BASE_PYTHON} can't create venvs (Debian/Ubuntu: python3-venv missing)")


def _wait(predicate: Any, timeout: float, what: str, interval: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")
        time.sleep(interval)


def _proc_alive(pid: int) -> bool:
    return runner.proc_start_time(pid) is not None


class Install:
    """A HomeMesh home + an install prefix with a real venv per version behind the ``venv`` symlink."""

    def __init__(self, tmp: Path, repo: Repo, *, roles: list[str]) -> None:
        self.tmp, self.repo, self.roles = tmp, repo, roles
        self.home = tmp / "home"
        self.paths = AppPaths(config_dir=self.home, data_dir=self.home / "data")
        self.paths.ensure()
        files.ensure_update_dir(self.paths)
        self.prefix = tmp / "prefix"
        self.stable = self.prefix / "venv"
        (self.prefix / "venvs").mkdir(parents=True)
        self.registry = self.home / "standin-pids"
        self.registry.mkdir()
        self.pids: set[int] = set()

    def standin_files(self) -> dict[str, str]:
        """The stand-in package as repository files; its file locations come from HomeMesh's own code."""
        cfg = {"instance_lock": str(self.paths.lock_file), "pid_file": str(self.paths.pid_file),
               "stop_request_file": str(self.paths.stop_request_file), "agent_file": str(files.agent_file(self.paths)),
               "latest_file": str(files.latest_file(self.paths)), "runs_dir": str(files.runs_dir(self.paths)),
               "registry": str(self.registry)}
        return {
            "pyproject.toml": '[build-system]\nrequires = []\nbuild-backend = "standin_backend"\n'
                              'backend-path = ["_build"]\n',
            "_build/standin_backend.py": (STANDIN / "backend.py").read_text(encoding="utf-8"),
            "src/homemesh/__init__.py": "",
            "src/homemesh/__main__.py": (STANDIN / "main.py").read_text(encoding="utf-8"),
            "src/homemesh/standin.json": json.dumps(cfg, indent=1) + "\n",
            "src/homemesh/behavior.json": "{}\n",
            "src/homemesh/update/layout.txt": "1\n",
        }

    def venv_for(self, commit: str) -> Path:
        return self.prefix / "venvs" / f"{commit[:12]}-20260101000000"

    def install(self, commit: str, env: dict[str, str]) -> Path:
        """What the installer does: a new venv with the checkout installed, its record, the link to it."""
        venv = self.venv_for(commit)
        subprocess.run([str(BASE_PYTHON), "-m", "venv", str(venv)], check=True, env=env, capture_output=True)
        r = subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "--no-input",
                            "--disable-pip-version-check", str(self.repo.src)], env=env, capture_output=True,
                           check=False)
        assert r.returncode == 0, r.stdout.decode() + r.stderr.decode()
        record.write(record.make_record(venv=venv, stable_venv=self.stable, built_by="installer",
                                        source_dir=self.repo.src, commit=commit, branch="main", remote="origin",
                                        git=GIT))
        os.symlink(os.path.relpath(venv, self.prefix), self.stable)
        return venv

    def package_dir(self) -> Path:
        found = list(Path(os.path.realpath(self.stable)).glob("lib/python3*/site-packages/homemesh"))
        assert len(found) == 1, found
        return found[0]

    def start(self, env: dict[str, str]) -> int:
        """``<prefix>/venv/bin/python -m homemesh run`` in the background, like `homemesh start` does."""
        p = subprocess.Popen([str(self.stable / "bin" / "python"), "-m", "homemesh", "run"], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.pids.add(p.pid)
        _wait(lambda: (files.read_json(self.paths.pid_file) or {}).get("ready"), 30, "the stand-in to be ready")
        return p.pid

    def running(self) -> tuple[int, str] | None:
        """``(pid, realpath of the venv it runs from)`` of the stand-in holding the instance lock."""
        rec = files.read_json(self.paths.pid_file) or {}
        pid = rec.get("pid")
        if not isinstance(pid, int) or not runner.lock_is_held(self.paths.lock_file):
            return None
        self.pids.add(pid)
        try:
            return pid, (self.registry / str(pid)).read_text(encoding="utf-8")
        except OSError:
            return None

    def args(self, mode: str, *, expected: str | None = None) -> files.RunnerArgs:
        a = files.build_runner_args(
            self.paths, mode=mode, run_id=files.new_run_id(), computer="e2e-box", roles=self.roles,
            source_dir=self.repo.src, stable_venv=self.stable, git=GIT, node_dir=None, service="posix",
            job_id="job-e2e", origin="remote", expected_commit=expected,
            current_venv=os.path.realpath(self.stable), current_package_dir=self.package_dir(),
            homemesh_home=str(self.home))
        files.write_runner_args(self.paths, a)
        return a

    def cleanup(self) -> None:
        with contextlib.suppress(OSError):
            self.pids |= {int(n) for n in os.listdir(self.registry) if n.isdigit()}
        for pid in self.pids:
            if _proc_alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """A worker install of the stand-in at commit A, running, plus the environment HomeMesh would hand the
    runner. ``HOME`` holds a pip config with ``no-index`` so nothing can reach a package index."""
    home_dir = tmp_path / "user-home"
    (home_dir / ".config" / "pip").mkdir(parents=True)
    (home_dir / ".config" / "pip" / "pip.conf").write_text("[global]\nno-index = true\n")
    for name in [n for n in os.environ if n.startswith(("PIP_", "PYTHON", "GIT_"))] + [
            launch.RUNNER_ENV, launch.TESTING_ENV, "HOMEMESH_HOME", "VIRTUAL_ENV"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo_root = tmp_path / "repo"
    install = Install(tmp_path, None, roles=["worker"])  # type: ignore[arg-type]
    repo = Repo(repo_root, base=install.standin_files())
    install.repo = repo
    consent.allow(install.paths)
    env = {**os.environ, "HOMEMESH_HOME": str(install.home)}
    install.install(repo.base, env)
    install.start(env)
    yield install
    install.cleanup()


def _behavior(repo: Repo, behavior: dict[str, Any], message: str) -> str:
    return repo.commit({"src/homemesh/behavior.json": json.dumps(behavior) + "\n"}, message)


def _apply(install: Install, expected: str, timeout: float = 240) -> tuple[dict[str, Any], dict[str, Any]]:
    """Start the apply runner the way HomeMesh does, wait for its outcome and for the runner to exit."""
    args = install.args("apply", expected=expected)
    handle = launch._launch(install.paths, args, foreground=False)
    assert handle.method == "posix" and handle.process is not None
    accepted = launch.wait_for_accept_sync(handle, timeout_s=60)
    assert accepted["run_id"] == args["run_id"]
    run = _wait(lambda: (lambda r: r if r and r.get("status") else None)(files.read_run(install.paths,
                                                                                       args["run_id"])),
                timeout, f"run {args['run_id']} to finish:\n{launch.log_tail(handle.log_file, 40)}", 0.25)
    assert handle.process.wait(timeout=60) == runner.EXIT_OK, launch.log_tail(handle.log_file, 40)
    return run, args


@needs_venv
def test_offline_real_runner_updates_rolls_back_and_reports(offline):
    install, repo = offline, offline.repo
    a = repo.base
    a_venv = install.venv_for(a)
    old_pid, old_venv = install.running()
    assert old_venv == str(a_venv)

    # A real check, through the launcher, sees the new commit.
    b = repo.commit({"src/homemesh/version.txt": "B\n"}, "B: Łódź 修复 → a change")
    check = install.args("check")
    handle = launch._launch(install.paths, check, foreground=False)
    result = launch.wait_for_check_sync(handle, timeout_s=120)
    assert isinstance(result, UpdateCheckResult) and result.ok and result.can_update
    assert result.behind == 1 and result.upstream_commit == b and result.installed_commit == a
    assert [c.subject for c in result.commits] == ["B: Łódź 修复 → a change"] and result.problems == []

    # Update to B: built while A keeps running, then A is stopped, the link flipped and B started.
    run, args = _apply(install, b)
    assert run["status"] == "ok", (run["error"], launch.log_tail(Path(args["log"]), 40))
    assert run["error"] is None and run["warnings"] == [] and run["phase"] == "cleanup"
    assert run["from_commit"] == a and run["to_commit"] == b and run["installed_commit"] == b
    assert run["committed"] and run["switched"] and run["was_running"] is True
    b_venv = Path(run["to_venv"])
    assert b_venv.parent == install.prefix / "venvs" and b_venv.name.startswith(b[:12])
    assert os.readlink(install.stable) == os.path.join("venvs", b_venv.name)
    rec = record.load(b_venv)
    assert rec is not None and rec.commit == b and rec.built_by == "updater" and rec.run_id == args["run_id"]
    assert rec.stable_venv == str(install.stable) and rec.venv == str(b_venv) and rec.git == GIT
    assert (install.package_dir() / "version.txt").read_text() == "B\n"  # really installed by pip
    assert not _proc_alive(old_pid)
    new_pid, new_venv = install.running()
    assert new_venv == str(b_venv) and new_pid != old_pid
    assert repo.head() == b  # the checkout is fast-forwarded only after success
    assert a_venv.is_dir() and sorted(os.listdir(install.prefix / "b")) == []  # previous kept, build gone
    log = Path(args["log"])
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text(encoding="utf-8")
    for phase in ("prechecks", "building", "draining", "stopping", "switching", "starting", "verifying"):
        assert f"phase {phase}" in text
    assert "drained: no jobs running" in text and "final status ok" in text
    report = UpdateRunReport.model_validate(files.read_latest_report(install.paths))
    assert report.final and report.status == "ok" and report.job_id == "job-e2e" and report.installed_commit == b

    # C crashes at start: the runner flips back to B, starts it again and says so.
    c = _behavior(repo, {"exit_at_start": 3}, "C: broken")
    run, args = _apply(install, c)
    assert run["status"] == "rolled_back", (run["error"], launch.log_tail(Path(args["log"]), 40))
    assert run["error"]["code"] == "update_new_version_crashed" and "exit code 3" in run["error"]["message"]
    assert run["installed_commit"] == b and not run["switched"]
    assert os.path.realpath(install.stable) == str(b_venv)
    pid, venv = install.running()
    assert venv == str(b_venv) and pid != new_pid
    assert not Path(run["to_venv"]).exists() and repo.head() == b  # C's venv deleted, git untouched
    assert sorted(os.listdir(install.prefix / "venvs")) == sorted([a_venv.name, b_venv.name])


@needs_venv
def test_offline_a_refused_configuration_changes_nothing(offline):
    install, repo = offline, offline.repo
    pid, venv = install.running()
    d = _behavior(repo, {"config_fail": True}, "D: rejects the configuration")
    run, _args = _apply(install, d)
    assert run["status"] == "failed" and run["error"]["code"] == "update_config_incompatible"
    assert "unknown role 'stand-in'" in run["error"]["message"] and not run["committed"]
    assert install.running() == (pid, venv) and repo.head() == repo.base
    assert sorted(os.listdir(install.prefix / "venvs")) == [install.venv_for(repo.base).name]
    assert sorted(os.listdir(install.prefix / "b")) == []


# --------------------------------------------------------------------------- the real HomeMesh (network)

online = pytest.mark.skipif(os.environ.get("HOMEMESH_UPDATE_E2E") != "1",
                            reason="installs HomeMesh's dependencies from PyPI (network, minutes): set "
                                   "HOMEMESH_UPDATE_E2E=1 to run it")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _snapshot_repo(dest: Path) -> dict[str, str | bytes]:
    """This working tree (tracked and untracked, not ignored files) as repository content."""
    names = subprocess.run([GIT, "-C", str(REPO_ROOT), "ls-files", "-z", "-co", "--exclude-standard"],
                           capture_output=True, check=True).stdout.decode().split("\0")
    out: dict[str, str | bytes] = {}
    for name in filter(None, names):
        p = REPO_ROOT / name
        if p.is_file() and not p.is_symlink():
            out[name] = p.read_bytes()
    return out


def _health(port: int) -> bool:
    import httpx

    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/v1/system/health", timeout=5, trust_env=False).status_code \
            == 200
    except httpx.HTTPError:
        return False


@online
@needs_venv
def test_real_homemesh_update_and_rollback(tmp_path, monkeypatch):
    for name in [n for n in os.environ if n.startswith(("PYTHON", "GIT_"))] + [
            launch.RUNNER_ENV, launch.TESTING_ENV, "HOMEMESH_HOME", "VIRTUAL_ENV"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))  # never this user's systemd unit
    repo = Repo(tmp_path / "repo", base=_snapshot_repo(tmp_path))  # type: ignore[arg-type]
    install = Install(tmp_path, repo, roles=["coordinator"])
    env = {**os.environ, "HOMEMESH_HOME": str(install.home)}
    stable_bin = install.stable / "bin"
    try:
        install.install(repo.base, env)
        port = _free_port()
        subprocess.run([str(stable_bin / "homemesh"), "setup", "--mode", "coordinator", "--non-interactive",
                        "--username", "owner", "--password-stdin", "--mock", "--no-service", "--local-only",
                        "--port", str(port)], input=b"correct-horse-battery-e2e\n", env=env, check=True,
                       capture_output=True)
        consent.allow(install.paths)
        subprocess.run([str(stable_bin / "homemesh"), "start"], env=env, check=True, capture_output=True)
        _wait(lambda: _health(port), 60, "the hub to answer")
        b = repo.commit({"docs/UPDATE_E2E.md": "B\n"}, "e2e B: Łódź 修复 →")

        launcher = (
            "import json, sys\n"
            "from homemesh.common.config import load_config\n"
            "from homemesh.common.paths import default_paths\n"
            "from homemesh.update import files, launch\n"
            "paths = default_paths(); cfg = load_config(paths)\n"
            "h = launch.start_apply(paths, cfg, run_id=files.new_run_id(), expected_commit=sys.argv[1], force=False,\n"
            "                       origin='remote', job_id='job-e2e')\n"
            "run = launch.wait_for_accept_sync(h, timeout_s=60)\n"
            "print(json.dumps({'run_id': run['run_id'], 'method': h.method}))\n")

        def apply(sha: str) -> dict[str, Any]:
            r = subprocess.run([str(stable_bin / "python"), "-c", launcher, sha], env=env, capture_output=True,
                               check=False)
            assert r.returncode == 0, r.stderr.decode()
            started = json.loads(r.stdout.decode().strip().splitlines()[-1])
            assert started["method"] == "posix"
            run = _wait(lambda: (lambda x: x if x and x.get("status") else None)(
                files.read_run(install.paths, started["run_id"])), 1800, "the update to finish", 1.0)
            # The outcome is written first (H.1); the runner then cleans up and releases the update lock.
            _wait(lambda: not files.update_lock_held(install.paths), 300, "the runner to finish its cleanup")
            return run

        run = apply(b)
        assert run["status"] == "ok", run
        assert run["installed_commit"] == b and repo.head() == b
        info = subprocess.run([str(stable_bin / "python"), "-c", "from homemesh.update.record import build_info; "
                               "import json; print(build_info().model_dump_json())"], env=env, capture_output=True,
                              check=True)
        built = json.loads(info.stdout)
        assert built["source"] == "updater" and built["commit"] == b and built["can_update"]
        assert _health(port)

        broken = (REPO_ROOT / "src/homemesh/runtime.py").read_text(encoding="utf-8").replace(
            "    roles = roles or list(cfg.node.roles)\n",
            "    raise RuntimeError('e2e: this version is broken on purpose')\n", 1)
        c = repo.commit({"src/homemesh/runtime.py": broken}, "e2e C: crashes at start")
        run = apply(c)
        assert run["status"] == "rolled_back" and run["error"]["code"] == "update_new_version_crashed", run
        assert run["installed_commit"] == b and repo.head() == b
        _wait(lambda: _health(port), 60, "the hub to answer again after the rollback")
    finally:
        with contextlib.suppress(Exception):
            subprocess.run([str(stable_bin / "homemesh"), "stop", "--force"], env=env, capture_output=True,
                           timeout=120, check=False)
        install.cleanup()
        shutil.rmtree(tmp_path / "prefix", ignore_errors=True)
