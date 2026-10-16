"""``homemesh update …``, ``homemesh version`` and the "update in progress" messages of status/start/run.

The update runner is stubbed (``update/launch.py`` has its own tests); files, lock, marker and record are real.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

from homemesh.cli import main as cli
from homemesh.common.config import HomeMeshConfig, save_config
from homemesh.common.instance import InstanceLock
from homemesh.common.paths import AppPaths
from homemesh.protocol.update import BuildInfo, UpdateCheckResult, format_update_error
from homemesh.update import consent, files, launch, record
from homemesh.version import __version__

SHA = "a" * 40
SHA2 = "b" * 40
RUN = "u20261016120000-0123abcd"
CHECK = "u20261016115900-89abcdef"


@pytest.fixture
def paths(tmp_path, monkeypatch):
    home = tmp_path / "hm"
    monkeypatch.setenv("HOMEMESH_HOME", str(home))  # main() exports --home; restored after the test
    return AppPaths(config_dir=home, data_dir=home / "data")


def hm(paths, *args):
    return cli.main(["--home", str(paths.config_dir), *args])


def _set_up(paths, roles=("worker",)):
    cfg = HomeMeshConfig()
    cfg.node.name = "box"
    cfg.node.roles = list(roles)
    save_config(cfg, paths.config_file)


def _write_run(paths, run_id=RUN, **fields):
    files.ensure_update_dir(paths)
    files.run_dir(paths, run_id).mkdir(parents=True, exist_ok=True)
    run = {"v": 1, "run_id": run_id, "job_id": None, "origin": "local", "expected_commit": SHA2, "force": False,
           "phase": "building", "status": None, "from_commit": SHA, "to_commit": SHA2,
           "started_at": "2026-10-16T12:00:00Z", "updated_at": "2026-10-16T12:01:00Z", "finished_at": None,
           "error": None, "warnings": [], "log": str(files.apply_log_file(paths, run_id)), "runner_pid": 1}
    run.update(fields)
    files.atomic_write_json(files.run_file(paths, run_id), run)
    files.write_latest(paths, run_id)
    return run


def _updatable(monkeypatch, commit=SHA):
    info = BuildInfo(version=__version__, commit=commit, source="installer", os="linux", can_update=True,
                     source_dir="/src/MyAi", logs_dir="/logs", branch="main")
    monkeypatch.setattr(record, "build_info", lambda paths=None, **kw: info)
    return info


# --------------------------------------------------------------------------- allow / disallow / version


def test_allow_and_disallow_need_setup_and_take_effect_now(paths, capsys):
    assert hm(paths, "update", "allow") == 2
    assert not consent.remote_allowed(paths) and not paths.config_file.exists()
    _set_up(paths)
    capsys.readouterr()
    assert hm(paths, "update", "allow") == 0
    out = capsys.readouterr().out
    assert consent.remote_allowed(paths) and "Remote updates are allowed on box" in out and "takes effect now" in out
    assert hm(paths, "update", "disallow") == 0
    assert not consent.remote_allowed(paths) and "off on box" in capsys.readouterr().out
    assert hm(paths, "update", "disallow") == 0
    assert "already off" in capsys.readouterr().out


def test_version_prints_the_build_line(paths, capsys):
    assert hm(paths, "version") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [record.format_version_line(record.build_info(paths))]  # one line (CI and installers)
    assert lines[0].startswith(f"HomeMesh {__version__} (")
    assert hm(paths, "--json", "version") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == __version__ and data["protocol"] == 1 and data["source"] in ("dev", "unknown")
    assert data["commit"] is None  # never guessed (no install record in a test run)


def test_record_install_is_not_listed_in_the_help(paths, capsys):
    with pytest.raises(SystemExit):
        hm(paths, "update", "--help")
    out = capsys.readouterr().out
    assert "allow" in out and "status" in out and "record-install" not in out and "SUPPRESS" not in out


# --------------------------------------------------------------------------- status


def test_status_works_without_setup_and_reads_only_files(paths, capsys, monkeypatch):
    from homemesh.cli import client

    monkeypatch.setattr(client.CoordinatorClient, "local", lambda *a, **k: pytest.fail("must not contact anything"))
    assert hm(paths, "update", "status") == 0
    out = capsys.readouterr().out
    assert "Remote updates: off" in out and "Latest update: none yet." in out
    assert not paths.config_file.exists() and not files.update_dir(paths).exists()

    err = format_update_error("update_new_version_crashed", exit="exit code 1", log="/l.log")
    warn = format_update_error("checkout_not_advanced", source_dir="/src/MyAi")
    _write_run(paths, status="rolled_back", phase="rolling_back", origin="remote", job_id="job_1",
               error=err.model_dump(), warnings=[warn.model_dump()], finished_at="2026-10-16T12:09:00Z")
    files.atomic_write_json(files.check_result_file(paths, CHECK), {
        "v": 1, "ok": True, "checked_at": "2026-10-16T11:59:30Z", "upstream_commit": SHA2, "behind": 2,
        "upstream_ref": "origin/main", "can_update": True})
    consent.allow(paths)
    assert hm(paths, "update", "status") == 0
    out = capsys.readouterr().out
    assert "Remote updates: allowed" in out
    assert f"Latest update: run {RUN} (from the hub), started 2026-10-16 12:00:00 UTC" in out
    assert "Failed and was undone; still on aaaaaaa (finished 2026-10-16 12:09:00 UTC)" in out
    assert err.message in out and err.hint in out and warn.message in out
    assert str(files.apply_log_file(paths, RUN)) in out
    assert "Last check (2026-10-16 11:59:30 UTC): 2 new changes (bbbbbbb on origin/main)" in out

    for args in (("update", "status", "--json"), ("--json", "update", "status")):
        assert hm(paths, *args) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["latest_run"]["status"] == "rolled_back" and data["latest_run_id"] == RUN
        assert data["remote_updates_allowed"] is True and data["in_progress"] is False
        assert data["last_check"]["behind"] == 2 and data["build"]["version"] == __version__


def test_status_of_a_running_and_of_a_vanished_update(paths, capsys):
    _write_run(paths, phase="building")
    lock = files.update_lock(paths)
    assert lock.acquire()
    try:
        assert hm(paths, "update", "status") == 0
        assert "In progress: building the new version" in capsys.readouterr().out
        assert hm(paths, "update", "status", "--json") == 0
        data = json.loads(capsys.readouterr().out)
        assert data["in_progress"] is True and data["helper_running"] is True
    finally:
        lock.release()
    assert hm(paths, "update", "status") == 0  # the runner is gone; status only reads, it doesn't finalize
    out = capsys.readouterr().out
    assert "stopped while building the new version without finishing" in out
    assert files.read_run(paths, RUN)["status"] is None


# --------------------------------------------------------------------------- start / run / status / restart


def test_start_run_status_restart_say_an_update_is_in_progress(paths, capsys, monkeypatch):
    from homemesh import runtime
    from homemesh.cli import service

    _set_up(paths)
    _write_run(paths, phase="switching")
    line = f"An update is in progress (run {RUN}, switching versions; log {files.apply_log_file(paths, RUN)})"
    monkeypatch.setattr(service, "start", lambda p: pytest.fail("start must not race the update helper"))
    monkeypatch.setattr(service, "stop", lambda *a, **k: pytest.fail("restart must not stop it"))
    started = []
    monkeypatch.setattr(runtime, "run_foreground", lambda cfg, p, roles=None: started.append(roles) or 0)
    update_lock = files.update_lock(paths)
    instance = InstanceLock(paths.lock_file)
    assert update_lock.acquire()
    try:
        capsys.readouterr()
        assert hm(paths, "start") == 1
        assert line in capsys.readouterr().out
        assert hm(paths, "restart") == 1
        assert line in capsys.readouterr().out
        assert hm(paths, "status") == 3
        assert line in capsys.readouterr().out
        assert instance.acquire()  # the helper holds the instance lock between stop and start
        try:
            assert hm(paths, "run") == 2
            assert line in capsys.readouterr().out and started == []
        finally:
            instance.release()
        assert hm(paths, "run") == 0 and started == [None]  # the helper starting the new version
    finally:
        update_lock.release()
    # Without a runner the run is no longer "in progress": start works as usual.
    monkeypatch.setattr(service, "start", lambda p: (True, "Started."))
    assert hm(paths, "start") == 0 and "Started." in capsys.readouterr().out


# --------------------------------------------------------------------------- record-install


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_record_install_writes_this_venvs_record(paths, tmp_path, capsys, monkeypatch):
    git = shutil.which("git")
    src = tmp_path / "MyAi"
    src.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "first")):
        subprocess.run([git, "-C", str(src), *args], check=True, capture_output=True, env=env)
    head = subprocess.run([git, "-C", str(src), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    prefix = tmp_path / "prefix"
    venv = prefix / "venvs" / f"{head[:12]}-20261016120000"
    venv.mkdir(parents=True)
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    monkeypatch.setattr(sys, "prefix", str(venv))  # the new venv's python runs this
    capsys.readouterr()
    assert hm(paths, "update", "record-install", "--source", str(src), "--stable-venv", str(prefix / "venv"),
              "--git", git, "--node-dir", str(node_dir), "--built-by", "installer") == 0
    out = capsys.readouterr().out
    rec = record.load(venv)
    assert rec is not None and rec.commit == head and rec.branch == "main" and rec.dirty is False
    assert rec.built_by == "installer" and rec.source_dir == os.path.abspath(src)
    assert rec.venv == os.path.realpath(venv) and rec.stable_venv == os.path.abspath(prefix / "venv")
    assert rec.git == os.path.abspath(git) and rec.node_dir == os.path.abspath(node_dir)
    assert rec.layout == record.layout_version() and f"commit {head[:7]}, branch main" in out
    assert not paths.config_file.exists()  # no setup needed, nothing created

    # Not a git checkout (an unpacked release), a git that isn't there, a Node.js folder that isn't there.
    plain = tmp_path / "release"
    plain.mkdir()
    assert hm(paths, "update", "record-install", "--source", str(plain), "--stable-venv", str(prefix / "venv"),
              "--git", str(tmp_path / "nogit"), "--node-dir", str(tmp_path / "nonode")) == 0
    out = capsys.readouterr().out
    rec = record.load(venv)
    assert rec.source_dir is None and rec.commit is None and rec.git is None and rec.node_dir is None
    assert "not a git checkout" in out and "not recorded" in out


def test_record_install_refuses_while_an_update_runs(paths, tmp_path, capsys, monkeypatch):
    venv = tmp_path / "venvs" / "nogit-20261016120000"
    venv.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(venv))
    _write_run(paths, phase="building")
    lock = files.update_lock(paths)
    assert lock.acquire()
    try:
        assert hm(paths, "update", "record-install", "--source", str(tmp_path), "--stable-venv",
                  str(tmp_path / "venv")) == 1
    finally:
        lock.release()
    assert "An update is in progress" in capsys.readouterr().out and record.load(venv) is None


# --------------------------------------------------------------------------- homemesh update / update check


class Runner:
    """Stub of the launcher used by ``homemesh update``."""

    def __init__(self, tmp_path, result):
        self.tmp = tmp_path
        self.result = result
        self.calls = []
        self.check_error = None

    def handle(self, mode, run_id):
        return launch.RunnerHandle(mode=mode, run_id=run_id, method="foreground", args_file=self.tmp / "a",
                                   log_file=self.tmp / f"update-{run_id}.log", out_file=None, run_file=None)

    def start_check(self, paths, cfg, *, origin, job_id=None, run_id=None, foreground=False):
        self.calls.append(("start_check", origin, job_id, foreground))
        return self.handle("check", files.new_run_id())

    def wait_for_check_sync(self, handle, *, timeout_s=300, poll_s=0.5):
        if self.check_error:
            raise self.check_error
        return self.result

    def kill_check(self, handle):
        self.calls.append(("kill_check",))

    def start_apply(self, paths, cfg, *, run_id, expected_commit, force, origin, job_id=None):
        self.calls.append(("start_apply", expected_commit, force, origin, job_id))
        return self.handle("apply", run_id)

    def wait_for_accept_sync(self, handle, *, timeout_s=15, poll_s=0.25):
        self.calls.append(("wait_for_accept_sync", timeout_s))
        return {"phase": "accepted"}


def _result(**over):
    data = {"v": 1, "ok": True, "checked_at": "2026-10-16T12:00:00Z", "installed_commit": SHA, "branch": "main",
            "upstream_ref": "origin/main", "upstream_commit": SHA2, "upstream_date": "2026-10-15T09:00:00+02:00",
            "behind": 3, "ahead": 0, "can_update": True, "migrations": True,
            "commits": [{"sha": SHA2, "subject": "Łódź 修复 →"}, {"sha": "c" * 40, "subject": "Two"}],
            "commits_truncated": False}
    data.update(over)
    return UpdateCheckResult.model_validate(data)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    r = Runner(tmp_path, _result())
    for name in ("start_check", "wait_for_check_sync", "kill_check", "start_apply", "wait_for_accept_sync"):
        monkeypatch.setattr(launch, name, getattr(r, name))
    return r


def test_update_check_shows_the_changes_and_installs_nothing(paths, runner, capsys, monkeypatch):
    _set_up(paths)
    _updatable(monkeypatch)
    capsys.readouterr()
    assert hm(paths, "update", "check") == 0
    out = capsys.readouterr().out
    assert "Installed: aaaaaaa (branch main)" in out and "Newest:    bbbbbbb on origin/main (2026-10-15)" in out
    assert "3 new changes:" in out and "bbbbbbb  Łódź 修复 →" in out and "… and 1 more" in out
    assert "The database will be upgraded" in out and "Install it with: homemesh update" in out
    assert runner.calls == [("start_check", "local", None, True)]


def test_update_asks_then_starts_the_helper_in_the_background(paths, runner, capsys, monkeypatch):
    _set_up(paths)
    _updatable(monkeypatch)
    answers = iter(["n", "y"])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or next(answers))
    assert hm(paths, "update") == 2
    assert "Nothing was changed." in capsys.readouterr().out
    assert prompts[0].startswith("Install bbbbbbb now? HomeMesh is not running here and stays stopped.")
    assert not any(c[0] == "start_apply" for c in runner.calls)
    assert hm(paths, "update") == 0
    out = capsys.readouterr().out
    assert ("start_apply", SHA2, False, "local", None) in runner.calls
    assert ("wait_for_accept_sync", 15.0) in runner.calls
    assert "Updating in the background. Follow it with: homemesh update status (log: " in out
    runner.calls.clear()
    assert hm(paths, "update", "--yes") == 0 and len(prompts) == 2  # --yes doesn't ask
    assert runner.calls[-2][0] == "start_apply"


def test_update_does_nothing_when_up_to_date_or_blocked(paths, runner, capsys, monkeypatch):
    _set_up(paths)
    _updatable(monkeypatch)
    runner.result = _result(behind=0, commits=[], upstream_commit=SHA)
    assert hm(paths, "update", "--yes") == 0
    assert "✓ Up to date (aaaaaaa)." in capsys.readouterr().out
    problem = {**format_update_error("update_dirty_checkout", source_dir="/src/MyAi", files="x.py").model_dump(),
               "blocking": True}
    runner.result = _result(problems=[problem])
    assert hm(paths, "update", "--yes") == 1
    out = capsys.readouterr().out
    assert "/src/MyAi has local changes: x.py." in out and "can't be installed" in out
    runner.result = UpdateCheckResult(ok=False, error=format_update_error("update_auth_failed", computer="box"))
    assert hm(paths, "update", "--yes") == 1
    assert "Signing in to the git server failed" in capsys.readouterr().out
    assert not any(c[0] == "start_apply" for c in runner.calls)


def test_a_failed_or_interrupted_check_stops_the_runner(paths, runner, capsys, monkeypatch):
    _set_up(paths)
    _updatable(monkeypatch)
    i = format_update_error("update_launch_failed", detail="the check did not finish in 5 minutes")
    runner.check_error = launch.UpdateLaunchError(i.message, code=i.code, hint=i.hint)
    assert hm(paths, "update", "check") == 1
    assert i.message in capsys.readouterr().err and ("kill_check",) in runner.calls
    runner.calls.clear()
    runner.check_error = KeyboardInterrupt()
    assert hm(paths, "update", "check") == 130 and ("kill_check",) in runner.calls


def test_update_refuses_when_busy_unsupported_or_not_set_up(paths, runner, capsys, monkeypatch):
    assert hm(paths, "update") == 2 and runner.calls == []
    _set_up(paths)
    capsys.readouterr()
    assert hm(paths, "update", "check") == 1  # this test run is not an installed, updatable copy
    assert "box can't be updated remotely" in capsys.readouterr().out
    _updatable(monkeypatch)
    files.ensure_update_dir(paths)
    lock = files.update_lock(paths)
    assert lock.acquire()
    try:
        assert hm(paths, "update", "check") == 1
        assert "Another update or check is running on box (checking for updates)." in capsys.readouterr().out
    finally:
        lock.release()
    assert runner.calls == []
