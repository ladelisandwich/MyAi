"""The runner's apply mode (design section 11) against a fake blue/green prefix, real git repos, the fake
HomeMesh process (``tests/fake_homemesh.py``) and an injected command table (venv/pip/homemesh CLI/systemctl
stubbed): every phase, every failure path and the rollback, on Linux without systemd, pip or the network."""

import contextlib
import io
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from homemesh.protocol.update import UpdateRunReport
from homemesh.update import consent, files, runner
from homemesh.update.runner import CmdResult
from update_support import Fakes, FakeSystemd, Layout, Repo, git

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX apply flow (symlink layout, signals); "
                                "the Windows paths have their own tests")

SUPPORT = Path(__file__).resolve().parents[1] / "update_support.py"


@pytest.fixture
def env(tmp_path):
    repo = Repo(tmp_path / "repo")
    layout = Layout(tmp_path, repo)
    yield layout
    layout.cleanup()


@pytest.fixture
def hub(tmp_path):
    repo = Repo(tmp_path / "repo")
    layout = Layout(tmp_path, repo, roles=("coordinator", "worker"))
    layout.make_db(3)
    yield layout
    layout.cleanup()


def _apply(layout, fakes=None, *, systemd=None, hooks=None, on_phase=None, **kw):
    fakes = fakes or Fakes()
    hooks = hooks or fakes.hooks(layout, systemd=systemd)
    phases = []

    def record(phase, run):
        phases.append(phase)
        if on_phase is not None:
            on_phase(phase, run)

    hooks.on_phase = record
    args = layout.args("apply", **kw)
    code = runner.run_apply(args, hooks)
    run = layout.run_json(args["run_id"])
    return code, run, phases, args


def _commit(layout, behavior=None, extra=None, message="B: a change"):
    return layout.repo.commit({"behavior.json": json.dumps(behavior or {}), **(extra or {})}, message)


def _untouched(layout, pid, run, old_head):
    """A failure before the stop: link, running HomeMesh and git untouched; the new build deleted."""
    assert run["status"] == "failed" and not run["switched"]
    assert layout.link_target() == str(layout.old_venv)
    assert layout.running_pid() == pid
    assert layout.repo.head() == old_head
    assert layout.venvs() == [layout.old_venv.name], "the new venv must be deleted"
    assert layout.builds() == []
    assert not files.update_lock_held(layout.paths)


# --------------------------------------------------------------------------- happy paths

def test_happy_path_phase_sequence_switches_and_verifies(env):
    old = env.start_homemesh()
    b = _commit(env)
    code, run, phases, args = _apply(env, expected=b)
    assert code == runner.EXIT_OK
    assert phases == ["accepted", "prechecks", "building", "draining", "stopping", "switching", "starting",
                      "verifying"]
    assert run["status"] == "ok" and run["phase"] == "cleanup" and run["error"] is None
    assert run["committed"] and run["switched"] and run["was_running"] is True
    assert run["from_commit"] == env.old_commit and run["to_commit"] == b and run["installed_commit"] == b
    assert run["job_id"] == "job-1" and run["origin"] == "remote" and run["runner_pid"] == os.getpid()
    target = env.link_target()
    assert os.path.basename(target).startswith(b[:12]) and target == run["to_venv"]
    assert os.readlink(env.stable) == os.path.join("venvs", os.path.basename(target))  # relative link
    rec = json.loads(Path(target, "homemesh-install.json").read_text())
    assert rec["commit"] == b and rec["built_by"] == "updater" and rec["run_id"] == args["run_id"]
    assert rec["stable_venv"] == str(env.stable) and rec["venv"] == target and rec["layout"] == 1
    assert rec["branch"] == "main" and rec["remote"] == "origin" and "://" not in json.dumps(rec)
    assert old.wait(timeout=5) == 0  # stopped gracefully
    assert env.running_venv() == target
    assert env.repo.head() == b  # git is advanced only after success
    assert env.builds() == []
    assert env.old_venv.name in env.venvs()  # the previous version is kept
    report = UpdateRunReport.model_validate(run)
    assert report.final and report.installed_commit == b and report.expected_commit == b
    assert files.read_latest_run_id(env.paths) == args["run_id"]
    assert not files.update_lock_held(env.paths)
    assert not env.paths.stop_request_file.exists()


def test_not_running_is_updated_but_not_started(env):
    b = _commit(env)
    _code, run, phases, _ = _apply(env, expected=b)
    assert run["status"] == "ok" and [w["code"] for w in run["warnings"]] == ["not_started"]
    assert "draining" not in phases and "verifying" not in phases and run["was_running"] is False
    assert env.running_pid() is None and env.link_target() == run["to_venv"]


def test_already_up_to_date_stops_nothing(env):
    pid = env.start_homemesh().pid
    _code, run, phases, _ = _apply(env, expected=env.old_commit)
    assert run["status"] == "already_up_to_date" and phases == ["accepted", "prechecks"]
    assert run["installed_commit"] == env.old_commit and not run["committed"]
    assert env.running_pid() == pid and env.link_target() == str(env.old_venv)
    assert env.builds() == [] and env.venvs() == [env.old_venv.name]


def test_systemd_path_stops_refreshes_the_unit_and_starts(env):
    systemd = FakeSystemd(env)
    try:
        systemd.start()
        env.wait_running()
        b = _commit(env)
        fakes = Fakes()
        _code, run, _phases, _ = _apply(env, fakes, systemd=systemd, service="systemd", expected=b)
    finally:
        systemd.close()
    assert run["status"] == "ok", run["error"]
    verbs = [c[0] for c in systemd.calls if c[0] != "show"]
    assert verbs.index("stop") < verbs.index("reset-failed") < verbs.index("start")
    installs = [argv for key, argv in fakes.calls if key == "service_install"]
    assert installs == [[str(env.stable / "bin" / "python"), "-m", "homemesh", "service", "install"]]
    assert run["installed_commit"] == b


def test_local_origin_needs_no_consent(env):
    consent.disallow(env.paths)
    b = _commit(env)
    _code, run, _, _ = _apply(env, expected=b, origin="local")
    assert run["status"] == "ok" and run["job_id"] is None and run["origin"] == "local"


# --------------------------------------------------------------------------- failures before the stop

@pytest.mark.parametrize("case", ["git_archive", "venv", "pip_install", "homemesh_version", "config_validate"])
def test_a_failure_at_every_build_step_changes_nothing(env, case):
    pid = env.start_homemesh().pid
    head = env.repo.head()
    fakes = Fakes()
    behavior = {}
    if case in ("git_archive", "venv"):
        fakes.fail[case] = CmdResult(1, err=f"{case}: disk on fire\n")
    else:
        behavior = {"pip_install": {"pip_fail": True}, "homemesh_version": {"smoke_fail": True},
                    "config_validate": {"config_fail": True}}[case]
    b = _commit(env, behavior)
    hooks = fakes.hooks(env)
    if case == "git_archive":
        hooks.commands["git_archive"] = lambda argv, **kw: CmdResult(128, err="fatal: archive failed\n")
    _code, run, phases, _args = _apply(env, fakes, hooks=hooks, expected=b)
    expected_code = {"git_archive": "update_venv_failed", "venv": "update_venv_failed",
                     "pip_install": "update_install_failed", "homemesh_version": "update_smoke_failed",
                     "config_validate": "update_config_incompatible"}[case]
    assert run["error"]["code"] == expected_code and "stopping" not in phases
    assert run["error"]["message"] and "Nothing was changed" in run["error"]["hint"]
    if case == "pip_install":
        assert "No matching distribution" in run["error"]["message"]
    _untouched(env, pid, run, head)
    assert run["installed_commit"] == env.old_commit


def test_upstream_moved_since_the_check(env):
    pid = env.start_homemesh().pid
    head = env.repo.head()
    b = _commit(env)
    c = _commit(env, message="C: newer")
    _code, run, phases, _ = _apply(env, expected=b)
    assert run["error"]["code"] == "update_upstream_changed" and c[:7] in run["error"]["message"]
    assert phases == ["accepted", "prechecks"]
    _untouched(env, pid, run, head)


def test_consent_withdrawn_during_the_build(env):
    pid = env.start_homemesh().pid
    head = env.repo.head()
    b = _commit(env)

    def on_phase(phase, run):
        if phase == "building":
            consent.disallow(env.paths)

    _code, run, _phases, _ = _apply(env, on_phase=on_phase, expected=b)
    assert run["error"]["code"] == "update_not_allowed" and "testbox" in run["error"]["message"]
    _untouched(env, pid, run, head)


def test_no_consent_refused_before_the_fetch(env):
    consent.disallow(env.paths)
    b = _commit(env)
    fetches_before = env.repo.head()
    _code, run, phases, args = _apply(env, expected=b)
    assert run["error"]["code"] == "update_not_allowed" and phases == ["accepted", "prechecks"]
    assert "fetch" not in env.log_text(args)
    assert fetches_before == env.repo.head()


def test_legacy_real_directory_needs_the_installer(env):
    pid = env.start_homemesh().pid
    real = env.prefix / "legacy-venv"
    os.rename(env.old_venv, real)
    env.stable.unlink()
    os.rename(real, env.stable)  # an install from before the blue/green layout: venv is a real directory
    b = _commit(env)
    _code, run, phases, _ = _apply(env, expected=b)
    assert run["error"]["code"] == "update_needs_installer" and "install.sh --upgrade" in run["error"]["hint"]
    assert "building" not in phases and env.running_pid() == pid and env.stable.is_dir()


def _tar(path, members):
    with tarfile.open(path, "w") as tf:
        for name, kind, value in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(value.encode())
                tf.addfile(info, io.BytesIO(value.encode()))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE}[kind]
            info.linkname = value or ""
            tf.addfile(info)
    return str(path)


@pytest.mark.parametrize("filtered", [True, False], ids=["data-filter", "no-data-filter"])
@pytest.mark.parametrize("members", [
    # a hard link's target is relative to the archive root, not to the member's folder
    [("a/b/evil", "hard", "../victim"), ("a/b/evil", "file", "pwned")],
    # each link looks inside on its own; together they lead out, and a file is written through them
    [("s", "sym", "."), ("t", "sym", "s/s/../../outside"), ("t/pwned", "file", "pwned")],
    [("abs", "sym", "/"), ("abs/pwned", "file", "pwned")],
    [("a/../../pwned", "file", "pwned")],
], ids=["hardlink", "symlink-chain", "absolute-link", "dotdot"])
def test_unpacking_the_source_never_writes_outside_the_build(tmp_path, monkeypatch, filtered, members):
    """B.3: what git archive writes is unpacked with tarfile's data filter, or (Python 3.11.0-3.11.3, which
    lack it) with the runner's own checks. Neither lets an archive write outside the build folder."""
    if not filtered:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    victim = tmp_path / "victim"
    victim.write_text("safe")
    (tmp_path / "outside").mkdir()
    build = tmp_path / "build"
    build.mkdir()
    with pytest.raises((ValueError, tarfile.TarError, OSError)):  # all three end as update_venv_failed
        runner._extract(_tar(tmp_path / "x.tar", members), str(build / "src"))
    assert victim.read_text() == "safe"
    assert list((tmp_path / "outside").iterdir()) == [] and not (tmp_path / "pwned").exists()


@pytest.mark.parametrize("filtered", [True, False], ids=["data-filter", "no-data-filter"])
def test_unpacking_keeps_what_git_archive_writes(tmp_path, monkeypatch, filtered):
    if not filtered:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    tar = _tar(tmp_path / "x.tar", [("pyproject.toml", "file", "[project]\n"), ("docs/", "dir", None),
                                    ("docs/a.md", "file", "a"), ("README.md", "sym", "docs/a.md"),
                                    ("docs/up", "sym", "../pyproject.toml")])
    runner._extract(tar, str(tmp_path / "src"))
    assert (tmp_path / "src" / "README.md").read_text() == "a"
    assert (tmp_path / "src" / "docs" / "up").read_text() == "[project]\n"


def test_a_new_commit_pushed_after_the_prechecks_is_not_used(env):
    env.start_homemesh()
    b = _commit(env)
    late = {}

    def on_phase(phase, run):
        if phase == "building":  # the branch moves (and is even fetched) while building
            late["c"] = _commit(env, message="C: pushed during the build")
            git("-C", str(env.repo.src), "fetch", "-q", "origin")

    _code, run, _phases, _ = _apply(env, on_phase=on_phase, expected=b)
    assert run["status"] == "ok"
    rec = json.loads(Path(env.link_target(), "homemesh-install.json").read_text())
    assert rec["commit"] == b != late["c"]
    assert env.repo.head() == b  # the fast-forward used the verified sha, not the moved ref
    assert git("-C", str(env.repo.src), "rev-parse", "origin/main") == late["c"]


# --------------------------------------------------------------------------- the web app (hub only, B.4)

def _fake_node(tmp_path):
    """A Node.js folder in the Linux layout (``bin/node`` + ``lib/node_modules/npm/bin/npm-cli.js``)."""
    node_dir = tmp_path / "node" / "bin"
    node_dir.mkdir(parents=True)
    (node_dir / "node").write_text("")
    cli = tmp_path / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    return node_dir, cli


def _web_hooks(layout, fakes, *, build_fails=False):
    """npm stubs (``ci`` / ``run build`` writes dist/index.html) and a pip stub that records which web app
    the build tree carries into the new venv."""
    hooks = fakes.hooks(layout)
    seen = {"npm": [], "ui": None}

    def npm(key):
        def fake(argv, *, cwd=None, env=None, timeout_s=0):
            seen["npm"].append((key, argv, cwd, env["PATH"]))
            if key == "npm_build":
                if build_fails:
                    return CmdResult(1, out="vite v7\nerror TS2304: Cannot find name 'x'.\n")
                Path(cwd, "dist").mkdir()
                Path(cwd, "dist", "index.html").write_text("<html>rebuilt</html>")
            return CmdResult(0, out="ok\n")
        return fake

    hooks.commands["npm_ci"] = npm("npm_ci")
    hooks.commands["npm_build"] = npm("npm_build")
    pip = hooks.commands["pip_install"]

    def pip_install(argv, **kw):
        ui = Path(argv[-1], "src", "homemesh", "web_dist", "index.html")
        seen["ui"] = ui.read_text() if ui.is_file() else None
        return pip(argv, **kw)

    hooks.commands["pip_install"] = pip_install
    return hooks, seen


def test_hub_rebuilds_a_changed_web_app_with_node_and_npm_cli(hub, tmp_path):
    hub.start_homemesh()
    node_dir, cli = _fake_node(tmp_path)
    b = _commit(hub, extra={"apps/web/src/new.ts": "export {}\n"})
    fakes = Fakes()
    hooks, seen = _web_hooks(hub, fakes)
    _code, run, _, _ = _apply(hub, fakes, hooks=hooks, expected=b, node_dir=str(node_dir))
    assert run["status"] == "ok", run["error"]
    assert [(k, argv[1:]) for k, argv, _cwd, _path in seen["npm"]] == [
        ("npm_ci", [str(cli), "ci", "--no-audit", "--no-fund"]), ("npm_build", [str(cli), "run", "build"])]
    for _k, argv, cwd, path in seen["npm"]:
        assert argv[0] == str(node_dir / "node") and cwd.endswith(os.path.join("src", "apps", "web"))
        assert path.split(os.pathsep)[0] == str(node_dir)  # npm scripts find node without a PATH entry
    assert seen["ui"] == "<html>rebuilt</html>"


def test_hub_reuses_the_installed_web_app_when_it_did_not_change(hub, tmp_path):
    hub.start_homemesh()
    node_dir, _cli = _fake_node(tmp_path)
    b = _commit(hub)
    fakes = Fakes()
    hooks, seen = _web_hooks(hub, fakes)
    _code, run, _, _ = _apply(hub, fakes, hooks=hooks, expected=b, node_dir=str(node_dir))
    assert run["status"] == "ok" and seen["npm"] == []
    assert seen["ui"] == "<html>installed</html>\n"


def test_hub_web_build_failure_changes_nothing(hub, tmp_path):
    pid = hub.start_homemesh().pid
    head = hub.repo.head()
    node_dir, _cli = _fake_node(tmp_path)
    b = _commit(hub, extra={"apps/web/src/new.ts": "export {}\n"})
    fakes = Fakes()
    hooks, _seen = _web_hooks(hub, fakes, build_fails=True)
    _code, run, phases, _ = _apply(hub, fakes, hooks=hooks, expected=b, node_dir=str(node_dir))
    assert run["error"]["code"] == "update_web_build_failed" and "TS2304" in run["error"]["message"]
    assert "venv" not in [k for k, _ in fakes.calls]  # failed before the venv was even created
    assert "stopping" not in phases
    _untouched(hub, pid, run, head)


def test_hub_without_node_or_installed_ui_warns_and_continues(hub, tmp_path, monkeypatch):
    hub.start_homemesh()
    shutil.rmtree(hub.pkg / "web_dist")  # this install never had a web app
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # no node anywhere
    b = _commit(hub)  # apps/web unchanged
    _code, run, _, _ = _apply(hub, expected=b)
    assert run["status"] == "ok" and "web_ui_missing" in [w["code"] for w in run["warnings"]]


def test_hub_web_change_without_node_is_refused_in_the_prechecks(hub, tmp_path, monkeypatch):
    pid = hub.start_homemesh().pid
    head = hub.repo.head()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    b = _commit(hub, extra={"apps/web/src/new.ts": "export {}\n"})
    _code, run, phases, _ = _apply(hub, expected=b)
    assert run["error"]["code"] == "update_node_missing" and phases == ["accepted", "prechecks"]
    _untouched(hub, pid, run, head)


# --------------------------------------------------------------------------- draining

def test_drain_waits_for_running_jobs_then_gives_up(env):
    env.old_venv.joinpath("fake-behavior.json").write_text(json.dumps(
        {"running_jobs": [{"job_id": "j1", "type": "chat"}, {"job_id": "j2", "type": "embed"}]}))
    pid = env.start_homemesh().pid
    head = env.repo.head()
    b = _commit(env)
    _code, run, phases, _ = _apply(env, expected=b)
    assert phases[-1] == "draining" and run["drain_deadline"] and run["was_running"] is True
    assert run["error"]["code"] == "update_jobs_running"
    assert run["error"]["message"].startswith("2 job(s)") and "chat, embed" in run["error"]["message"]
    _untouched(env, pid, run, head)


def test_force_skips_draining_and_stops_running_jobs(env):
    env.old_venv.joinpath("fake-behavior.json").write_text(json.dumps(
        {"running_jobs": [{"job_id": "j1", "type": "chat"}]}))
    env.start_homemesh()
    b = _commit(env)
    _code, run, phases, _ = _apply(env, expected=b, force=True)
    assert "draining" not in phases and run["status"] == "ok" and run["force"] is True


def test_no_worker_role_skips_draining(tmp_path):
    repo = Repo(tmp_path / "repo")
    layout = Layout(tmp_path, repo, roles=("coordinator",))
    try:
        layout.start_homemesh()
        b = _commit(layout)
        _code, run, phases, _ = _apply(layout, expected=b)
        assert "draining" not in phases and run["status"] == "ok"  # alive and stable, no agent needed
    finally:
        layout.cleanup()


def test_drain_deadline_is_awake_time(env):
    """A suspend in the middle of draining (one poll step of an hour) doesn't count against the 120 s."""
    env.old_venv.joinpath("fake-behavior.json").write_text(json.dumps(
        {"running_jobs": [{"job_id": "j1", "type": "chat"}]}))
    env.start_homemesh()
    b = _commit(env)
    fakes = Fakes()
    hooks = fakes.hooks(env)
    hooks.timeouts["drain"] = 120.0
    clock = {"t": 0.0, "sleeps": 0}
    hooks.monotonic = lambda: clock["t"]

    def sleep(seconds):
        clock["sleeps"] += 1
        clock["t"] += 3600.0 if clock["sleeps"] == 5 else 1.0  # the 5th poll step: the laptop slept
        time.sleep(0.001)

    hooks.sleep = sleep
    _code, run, _phases, _ = _apply(env, fakes, hooks=hooks, expected=b)
    assert run["error"]["code"] == "update_jobs_running"
    assert 119 <= clock["sleeps"] <= 122, clock  # ~120 awake seconds, not cut short by the hour-long gap
    assert clock["t"] > 3600


def test_consent_withdrawn_while_draining_stops_before_stopping(env):
    pid = env.start_homemesh().pid
    head = env.repo.head()
    b = _commit(env)

    def on_phase(phase, run):
        if phase == "draining":  # the owner runs `homemesh update disallow` while the jobs finish
            consent.disallow(env.paths)

    _code, run, phases, _ = _apply(env, on_phase=on_phase, expected=b)
    assert run["error"]["code"] == "update_not_allowed" and "stopping" not in phases
    _untouched(env, pid, run, head)


# --------------------------------------------------------------------------- stopping

def test_forced_stop_when_homemesh_ignores_the_request(env):
    env.old_venv.joinpath("fake-behavior.json").write_text(json.dumps({"ignore_stop": True}))
    old = env.start_homemesh()
    b = _commit(env)
    _code, run, _, args = _apply(env, expected=b)
    assert run["status"] == "ok"
    assert old.wait(timeout=5) == -signal.SIGKILL
    assert "stopping it forcibly" in env.log_text(args)


def test_stop_failed_when_the_process_cannot_be_identified(env):
    env.old_venv.joinpath("fake-behavior.json").write_text(json.dumps({"ignore_stop": True}))
    pid = env.start_homemesh().pid
    rec = json.loads(env.paths.pid_file.read_text())
    env.paths.pid_file.write_text(json.dumps({**rec, "started": rec["started"] - 100}))  # PID reused?
    head = env.repo.head()
    b = _commit(env)
    _code, run, _phases, _ = _apply(env, expected=b)
    assert run["error"]["code"] == "update_stop_failed" and run["committed"] and run["status"] == "failed"
    assert env.running_pid() == pid and env.link_target() == str(env.old_venv)  # nothing killed on a bare PID
    assert not env.paths.stop_request_file.exists()  # withdrawn, so it doesn't stop later
    assert env.venvs() == [env.old_venv.name] and env.builds() == [] and env.repo.head() == head


def test_db_snapshot_failure_restarts_the_old_version(hub):
    hub.start_homemesh()
    head = hub.repo.head()
    b = _commit(hub, extra={"src/homemesh/coordinator/db/migrations/0004_four.sql": "-- 4\n"})

    def on_phase(phase, run):
        if phase == "switching":  # something already sits where the snapshot goes
            (files.run_dir(hub.paths, run["run_id"]) / "pre-update.sqlite3").mkdir()

    _code, run, phases, _ = _apply(hub, on_phase=on_phase, expected=b)
    assert run["status"] == "failed" and run["error"]["code"] == "update_db_snapshot_failed"
    assert phases[phases.index("switching"):] == ["switching", "rolling_back"]  # restarting the old one
    assert run["installed_commit"] == hub.old_commit and not run["switched"]
    assert hub.link_target() == str(hub.old_venv) and hub.running_venv() == str(hub.old_venv)
    assert hub.venvs() == [hub.old_venv.name] and hub.builds() == [] and hub.repo.head() == head
    assert hub.schema() == 3


def test_switch_failure_restarts_the_old_version(env):
    env.start_homemesh()
    b = _commit(env)
    blocker = Path(str(env.stable) + ".next")

    def on_phase(phase, run):
        if phase == "switching":  # a real folder where the new link is prepared: never replaced
            blocker.mkdir()

    _code, run, _phases, _ = _apply(env, on_phase=on_phase, expected=b)
    assert run["status"] == "failed" and run["error"]["code"] == "update_switch_failed"
    assert "not a link" in run["error"]["message"] and "old version" in run["error"]["hint"]
    assert env.link_target() == str(env.old_venv) and env.running_venv() == str(env.old_venv)
    assert blocker.is_dir() and env.venvs() == [env.old_venv.name]


# --------------------------------------------------------------------------- verifying and rollback

@pytest.mark.parametrize("behavior, code", [
    ({"exit_at_start": 3}, "update_new_version_crashed"),
    ({"agent": "rejected", "detail": "protocol mismatch (4406)"}, "update_new_version_rejected"),
    ({"exit_after": 0.3, "agent": "none"}, "update_new_version_crashed"),
])
def test_rollback_flips_back_and_restarts_the_old_version(env, behavior, code):
    env.start_homemesh()
    head = env.repo.head()
    b = _commit(env, behavior)
    _, run, phases, _ = _apply(env, expected=b)
    assert phases[-1] == "rolling_back" and "verifying" in phases
    assert run["status"] == "rolled_back" and run["error"]["code"] == code
    if code == "update_new_version_rejected":
        assert "protocol mismatch (4406)" in run["error"]["message"]
    if behavior.get("exit_at_start"):
        assert "exit code 3" in run["error"]["message"]
    assert run["switched"] is False and run["installed_commit"] == env.old_commit
    assert env.link_target() == str(env.old_venv) and env.running_venv() == str(env.old_venv)
    assert env.venvs() == [env.old_venv.name] and env.builds() == []
    assert env.repo.head() == head  # git untouched on failure


def test_rollback_writes_the_link_as_venvs_id_under_a_symlinked_home(tmp_path):
    """Bazzite's /home is a symlink to var/home. The rollback flips back to the realpath of the old target,
    and the link must still read ``venvs/<id>`` like the installers write it, not a ../../var/home/… path."""
    (tmp_path / "var-home").mkdir()
    home = tmp_path / "home"
    home.symlink_to(tmp_path / "var-home")
    layout = Layout(home, Repo(home / "repo"))
    try:
        layout.start_homemesh()
        b = _commit(layout, {"exit_at_start": 3})
        _, run, phases, _ = _apply(layout, expected=b)
        assert run["status"] == "rolled_back" and "switching" in phases
        assert os.readlink(layout.stable) == os.path.join("venvs", layout.old_venv.name)
        assert layout.running_venv() == os.path.realpath(layout.old_venv)
    finally:
        layout.cleanup()


def test_unreachable_hub_from_a_worker_keeps_the_new_version_unconfirmed(env):
    env.start_homemesh()
    b = _commit(env, {"agent": "unreachable", "detail": "connection refused"})
    _code, run, _, _ = _apply(env, expected=b)
    assert run["status"] == "ok_unconfirmed" and [w["code"] for w in run["warnings"]] == ["not_confirmed"]
    assert env.link_target() == run["to_venv"] and run["installed_commit"] == b


def test_unreachable_on_the_hub_itself_is_rolled_back(hub):
    hub.start_homemesh()
    b = _commit(hub, {"agent": "unreachable"})
    _code, run, _, _ = _apply(hub, expected=b)
    assert run["status"] == "rolled_back" and run["error"]["code"] == "update_not_confirmed"


def test_never_ready_is_a_start_timeout(env):
    env.start_homemesh()
    b = _commit(env, {"never_ready": True})
    fakes = Fakes()
    hooks = fakes.hooks(env)
    hooks.timeouts["ready"] = 1.5
    _code, run, _, _ = _apply(env, fakes, hooks=hooks, expected=b)
    assert run["status"] == "rolled_back" and run["error"]["code"] == "update_start_timeout"


def test_systemd_restart_count_change_is_a_crash(env):
    systemd = FakeSystemd(env)
    try:
        systemd.start()
        env.wait_running()
        b = _commit(env, {"exit_after": 0.5, "agent": "none"})  # dies before it connects
        _code, run, _, _ = _apply(env, systemd=systemd, service="systemd", expected=b)
        assert run["status"] == "rolled_back" and run["error"]["code"] == "update_new_version_crashed"
        assert env.running_venv() == str(env.old_venv)
        assert systemd.active == "active"
    finally:
        systemd.close()
    assert b


def test_polling_the_unit_logs_changes_only_and_keeps_no_capture_files(env, tmp_path, monkeypatch):
    """verify() asks systemd for the unit's state every second, for up to half an hour of migrations: the log
    gets one line per change, not the command and its output each time, and the run folder no files."""
    states = tmp_path / "states.txt"
    states.write_text("active 4242\nactive 4242\nactive 4242\nactivating 0\nactivating 0\n")
    fake = tmp_path / "systemctl"
    fake.write_text(f"#!{sys.executable}\nimport pathlib\np = pathlib.Path({str(states)!r})\n"
                    "lines = p.read_text().splitlines()\nstate, pid = lines[0].split()\n"
                    "p.write_text('\\n'.join(lines[1:] or lines) + '\\n')\n"
                    "print(f'MainPID={pid}\\nActiveState={state}\\nSubState=running\\nNRestarts=0\\n"
                    "ExecMainStatus=0\\nExecMainCode=0')\n")
    fake.chmod(0o755)
    real_which = shutil.which
    monkeypatch.setattr(runner.shutil, "which",
                        lambda name, *a, **kw: str(fake) if name == "systemctl" else real_which(name, *a, **kw))
    args = env.args("apply", expected="a" * 40, service="systemd")
    run = runner._Apply(args, runner.Hooks())
    shown = [run.unit_show() for _ in range(5)]
    assert [s["ActiveState"] for s in shown] == ["active"] * 3 + ["activating"] * 2 and shown[0]["MainPID"] == "4242"
    log = env.log_text(args)
    assert log.count("homemesh.service: ActiveState=") == 2 and "ActiveState=activating SubState=running" in log
    assert " show -p " not in log
    assert sorted(p.name for p in files.run_dir(env.paths, args["run_id"]).iterdir()) == ["args.json"]
    run.systemctl("reset-failed", "homemesh.service")  # anything else is still logged and kept
    assert "reset-failed homemesh.service" in env.log_text(args)
    assert any(p.name.startswith("cmd-") for p in files.run_dir(env.paths, args["run_id"]).iterdir())


def test_rollback_failed_when_the_old_version_does_not_start_either(env):
    env.start_homemesh()
    b = _commit(env, {"exit_at_start": 3})

    def on_phase(phase, run):
        if phase == "switching":  # e.g. the OS Python changed under the old venv
            env.old_venv.joinpath("fake-behavior.json").write_text(json.dumps({"exit_at_start": 4}))

    _code, run, _, _ = _apply(env, on_phase=on_phase, expected=b)
    assert run["status"] == "failed" and run["error"]["code"] == "update_rollback_failed"
    assert run["first_error"]["code"] == "update_new_version_crashed"
    assert "HomeMesh is stopped on testbox" in run["error"]["message"]
    assert "install.sh" in run["error"]["hint"] and run["installed_commit"] is None
    assert env.running_pid() is None and env.link_target() == str(env.old_venv)


def test_service_install_failure_after_the_switch_rolls_back(env):
    systemd = FakeSystemd(env)
    try:
        systemd.start()
        env.wait_running()
        b = _commit(env, {"service_install_fail": True})
        _code, run, phases, _ = _apply(env, systemd=systemd, service="systemd", expected=b)
        assert run["status"] == "rolled_back" and run["error"]["code"] == "update_switch_failed"
        assert "starting" not in phases[:phases.index("rolling_back")]
        assert env.link_target() == str(env.old_venv) and env.running_venv() == str(env.old_venv)
    finally:
        systemd.close()
    assert b


# --------------------------------------------------------------------------- database and backups

def test_migrated_then_killed_new_version_is_restored_through_the_backup_api(hub, tmp_path):
    hub.start_homemesh()
    b = _commit(hub, {"migrate_to": 4, "crash_after_migrate": True},
                {"src/homemesh/coordinator/db/migrations/0004_four.sql": "-- 4\n"})
    code, run, _, args = _apply(hub, expected=b)
    assert run["status"] == "rolled_back" and run["error"]["code"] == "update_new_version_crashed"
    assert run["pre_schema"] == 3 and run["db_snapshot"] == str(files.run_dir(hub.paths, args["run_id"])
                                                               / "pre-update.sqlite3")
    snap = Path(run["db_snapshot"])
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600
    assert not Path(str(snap) + "-wal").exists()
    assert hub.schema() == 3
    with sqlite3.connect(hub.paths.db_file) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "added_by_v4" not in tables and conn.execute("SELECT name FROM users").fetchall() == [("owner",)]
    assert hub.running_venv() == str(hub.old_venv)

    # Why the backup API: the same situation restored by copying the file brings the migration back.
    demo = tmp_path / "demo"
    demo.mkdir()
    db = demo / "homemesh.sqlite3"
    shutil.copyfile(snap, demo / "snapshot.sqlite3")
    with contextlib.closing(sqlite3.connect(demo / "snapshot.sqlite3")) as s, contextlib.closing(
            sqlite3.connect(db)) as d:
        s.backup(d)
    code = ("import os, signal, sqlite3, sys\n"
            "c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL')\n"
            "c.execute(\"INSERT INTO schema_migrations VALUES (4, 'm4', 'x', 'now')\"); c.commit()\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n")
    subprocess.run([sys.executable, "-c", code, str(db)], check=False)
    assert Path(str(db) + "-wal").exists()
    shutil.copyfile(demo / "snapshot.sqlite3", db)  # the naive restore
    with contextlib.closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 4  # replayed


def test_no_snapshot_when_the_target_adds_no_migration(hub):
    hub.start_homemesh()
    b = _commit(hub)
    _code, run, _, args = _apply(hub, expected=b)
    assert run["status"] == "ok" and run["pre_schema"] == 3 and run["db_snapshot"] is None
    run_dir = files.run_dir(hub.paths, args["run_id"])
    assert not (run_dir / "pre-update.sqlite3").exists()
    assert (run_dir / "homemesh.toml.bak").exists()  # config and credentials are always copied
    assert stat.S_IMODE((run_dir / "worker-credentials.json.bak").stat().st_mode) == 0o600


def test_config_and_credentials_restored_only_when_changed(env):
    env.start_homemesh()
    config_before = env.paths.config_file.read_bytes()
    creds_before = env.paths.worker_credentials_file.read_bytes()
    creds_mtime = env.paths.worker_credentials_file.stat().st_mtime_ns
    b = _commit(env, {"exit_at_start": 3})

    def on_phase(phase, run):
        if phase == "starting":  # the new version rewrites the config, then crashes
            env.paths.config_file.write_text("[changed]\nby = 'new version'\n")

    _code, run, _, args = _apply(env, on_phase=on_phase, expected=b)
    assert run["status"] == "rolled_back"
    assert env.paths.config_file.read_bytes() == config_before
    assert env.paths.worker_credentials_file.read_bytes() == creds_before
    assert env.paths.worker_credentials_file.stat().st_mtime_ns == creds_mtime  # untouched
    log = env.log_text(args)
    assert "restored" in log and "homemesh.toml" in log and "worker-credentials.json from" not in log


def test_db_restore_failure_keeps_homemesh_stopped(hub):
    hub.start_homemesh()
    b = _commit(hub, {"migrate_to": 4, "crash_after_migrate": True},
                {"src/homemesh/coordinator/db/migrations/0004_four.sql": "-- 4\n"})

    def on_phase(phase, run):
        if phase == "starting" and run.get("db_snapshot"):
            Path(run["db_snapshot"]).write_bytes(b"not a database at all" * 100)

    _code, run, _, args = _apply(hub, on_phase=on_phase, expected=b)
    assert run["status"] == "failed" and run["error"]["code"] == "update_db_restore_failed"
    assert run["db_snapshot"] in run["error"]["hint"] and run["first_error"]["code"] == "update_new_version_crashed"
    time.sleep(0.5)
    assert hub.running_pid() is None  # HomeMesh stays stopped; the guard must not start it
    # the run (and its snapshot) survive pruning
    for _ in range(6):
        rid = files.new_run_id()
        files.run_dir(hub.paths, rid).mkdir(parents=True)
        files.atomic_write_json(files.run_file(hub.paths, rid), {"run_id": rid, "status": "ok"})
        time.sleep(0.01)
    runner.prune_runs(str(files.update_dir(hub.paths)), str(hub.paths.logs_dir))
    assert files.run_dir(hub.paths, args["run_id"]).is_dir()


# --------------------------------------------------------------------------- accept, locks, cleanup

def test_busy_inside_unit_and_reused_run_id_write_nothing(env):
    b = _commit(env)
    hooks = Fakes().hooks(env)
    args = env.args("apply", expected=b)
    lock = files.update_lock(env.paths)
    assert lock.acquire()
    try:
        assert runner.run_apply(args, hooks) == runner.EXIT_BUSY
    finally:
        lock.release()
    assert files.read_run(env.paths, args["run_id"]) is None and files.read_latest_run_id(env.paths) is None
    hooks.inside_homemesh_unit = lambda: True
    assert runner.run_apply(args, hooks) == runner.EXIT_INSIDE_UNIT
    assert files.read_run(env.paths, args["run_id"]) is None
    hooks.inside_homemesh_unit = lambda: False
    assert runner.run_apply(args, hooks) == runner.EXIT_OK
    assert runner.run_apply(args, hooks) == runner.EXIT_BAD_ARGS  # the same run twice
    assert runner.EXIT_MARKER in Path(args["log"]).read_text()


def test_a_run_that_cannot_be_recorded_fails_cleanly(env, monkeypatch):
    pid = env.start_homemesh().pid
    b = _commit(env)
    real = runner.atomic_write_json

    def failing(path, obj):
        if os.path.basename(path) == "latest.json":
            raise OSError(28, "No space left on device")
        return real(path, obj)

    monkeypatch.setattr(runner, "atomic_write_json", failing)
    code, run, phases, args = _apply(env, expected=b)
    assert code == runner.EXIT_FAILED and phases == []
    assert run["status"] == "failed" and run["error"]["code"] == "update_launch_failed"
    assert "No space left" in run["error"]["message"] and run["installed_commit"] == env.old_commit
    assert f"{runner.EXIT_MARKER} {runner.EXIT_FAILED}" in env.log_text(args)
    assert env.running_pid() == pid and not files.update_lock_held(env.paths)


def test_a_dead_runs_latest_run_is_finalized_before_a_new_run(env):
    dead = files.new_run_id()
    files.run_dir(env.paths, dead).mkdir(parents=True)
    files.atomic_write_json(files.run_file(env.paths, dead), {
        "v": 1, "run_id": dead, "job_id": "old", "origin": "remote", "phase": "building", "status": None,
        "started_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z"})
    files.write_latest(env.paths, dead)
    b = _commit(env)
    _, run, _, _ = _apply(env, expected=b)
    old = files.read_run(env.paths, dead)
    assert old["status"] == "interrupted" and old["error"]["code"] == "update_interrupted"
    assert old["installed_commit"] == env.old_commit and "building the new version" in old["error"]["message"]
    assert run["status"] == "ok"


def test_interrupted_link_swap_is_repaired_at_accept(env):
    os.rename(env.stable, str(env.stable) + ".next")
    b = _commit(env)
    _code, run, _, args = _apply(env, expected=b)
    assert run["status"] == "ok" and os.path.islink(env.stable) and not os.path.lexists(str(env.stable) + ".next")
    assert "repaired the program link" in env.log_text(args)


def test_cleanup_prunes_venvs_builds_and_runs(env):
    # leftovers: an older venv, a newer half-built one from an interrupted run, a build dir, many old runs
    older = env.make_venv("a" * 40, ts="20250101000000")
    leftover = env.make_venv("c" * 40, ts="20270101000000")
    (env.prefix / "b" / "0badc0de").mkdir(parents=True)
    (env.prefix / "venvs" / "not-a-venv-id").mkdir()
    for i in range(7):
        rid = f"u2026010100000{i}-0000000{i}"
        files.run_dir(env.paths, rid).mkdir(parents=True)
        files.atomic_write_json(files.run_file(env.paths, rid), {"run_id": rid, "status": "ok"})
    b = _commit(env)
    _, run, _, _ = _apply(env, expected=b)
    assert run["status"] == "ok"
    names = env.venvs()
    assert os.path.basename(run["to_venv"]) in names and env.old_venv.name in names  # current + previous
    assert older.name not in names and leftover.name not in names and "not-a-venv-id" in names
    assert env.builds() == []
    assert len([n for n in os.listdir(files.runs_dir(env.paths)) if files.is_run_id(n)]) == 5


def test_log_and_run_files_hold_no_raw_tool_output_tokens(env):
    env.start_homemesh()
    b = _commit(env, {"pip_fail": True})
    fakes = Fakes()
    fakes.fail["pip_install"] = CmdResult(1, out="ERROR: https://user:ghp_SECRETtoken123@pypi.example/simple\n")
    _code, run, _, args = _apply(env, fakes, expected=b)
    assert run["error"]["code"] == "update_install_failed"
    texts = [env.log_text(args), json.dumps(run)]
    run_dir = files.run_dir(env.paths, args["run_id"])
    texts += [p.read_text(errors="replace") for p in run_dir.iterdir() if p.is_file()]
    for text in texts:
        assert "ghp_SECRETtoken123" not in text and "user:" not in text


KEEP_AWAKE_CHILD = r'''
import os, sys, time
from homemesh.update import runner
fake, how, pidfile = sys.argv[1], sys.argv[2], sys.argv[3]
real = runner.shutil.which
runner.shutil.which = lambda name, *a, **kw: fake if name == "systemd-inhibit" else real(name, *a, **kw)


class Run:
    def tool_env(self):
        return dict(os.environ)

    def log(self, text):
        print(text, flush=True)


awake = runner._KeepAwake(Run())
awake.start()
deadline = time.monotonic() + 15
while not os.path.exists(pidfile) and time.monotonic() < deadline:  # the inhibitor's command is running
    time.sleep(0.05)
time.sleep(0.2)
if how == "stop":
    awake.stop()
os._exit(0)  # "kill": gone without stop() or any finally, like a SIGKILL
'''


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="systemd-inhibit is Linux-only")
@pytest.mark.parametrize("how", ["stop", "kill"])
def test_the_sleep_inhibitor_ends_with_the_runner(tmp_path, how):
    """9.3: the inhibitor must not outlive the runner, also when the runner is killed outright."""
    pidfile = tmp_path / "inhibitor.pid"
    fake = tmp_path / "systemd-inhibit"
    fake.write_text(f'#!/bin/sh\necho $$ > {pidfile}\nwhile [ "${{1#--}}" != "$1" ]; do shift; done\nexec "$@"\n')
    fake.chmod(0o755)
    script = tmp_path / "child.py"
    script.write_text(KEEP_AWAKE_CHILD)
    env = {**os.environ, "PYTHONPATH": str(Path(runner.__file__).parents[2])}
    proc = subprocess.run([sys.executable, str(script), str(fake), how, str(pidfile)], env=env, capture_output=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    deadline = time.monotonic() + 15
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    while runner.proc_start_time(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    alive = runner.proc_start_time(pid) is not None
    if alive:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    assert not alive, "the sleep inhibitor outlived the runner"


# --------------------------------------------------------------------------- interruptions

def _child(layout, b, phase, sig, tmp_path):
    args = layout.args("apply", expected=b)
    spec = tmp_path / f"spec-{phase}.json"
    spec.write_text(json.dumps({"home": str(layout.home), "args_file": str(files.args_file(layout.paths,
                                                                                         args["run_id"])),
                                "kill_phase": phase, "signal": sig}))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(runner.__file__).parents[2]),
                                                        str(SUPPORT.parent), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run([sys.executable, str(SUPPORT), "apply-child", str(spec)], env=env, timeout=60,
                          capture_output=True, check=False)
    return args, proc


@pytest.mark.parametrize("phase", ["accepted", "prechecks", "building", "draining", "stopping", "switching",
                                   "starting", "verifying"])
def test_killed_at_any_phase_leaves_a_complete_venv_and_the_agent_finalizes(env, tmp_path, phase):
    env.start_homemesh()
    b = _commit(env)
    args, proc = _child(env, b, phase, "KILL", tmp_path)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    run = env.run_json(args["run_id"])
    assert run["phase"] == phase and run["status"] is None
    target = env.link_target()
    assert Path(target, "homemesh-install.json").is_file()  # old or new, always complete
    assert target == (run["to_venv"] if phase in ("starting", "verifying") else str(env.old_venv))
    # The next HomeMesh start (autostart) finalizes it with the commit it runs.
    commit = json.loads(Path(target, "homemesh-install.json").read_text())["commit"]
    final = files.finalize_dead_run(env.paths, installed_commit=commit, computer="testbox")
    assert final["status"] == "interrupted" and final["installed_commit"] == commit
    assert final["error"]["code"] == "update_interrupted" and commit[:7] in final["error"]["hint"]


@pytest.mark.parametrize("phase", ["building", "switching", "verifying"])
def test_sigterm_is_guarded(env, tmp_path, phase):
    env.start_homemesh()
    b = _commit(env)
    args, proc = _child(env, b, phase, "TERM", tmp_path)
    assert proc.returncode == runner.EXIT_FAILED, proc.stderr.decode()
    run = env.run_json(args["run_id"])
    assert run["status"] == "interrupted" and run["error"]["code"] == "update_interrupted"
    assert run["finished_at"] and not files.update_lock_held(env.paths)
    target = env.link_target()
    assert run["installed_commit"] == json.loads(Path(target, "homemesh-install.json").read_text())["commit"]
    if phase == "building":
        assert env.venvs() == [env.old_venv.name] and env.builds() == []  # the half build is removed
        assert env.running_pid() is not None  # never stopped
    else:  # committed: some HomeMesh runs whatever the link points to
        if phase == "verifying":  # the runner had started the new version already: never a second one
            assert "guard_started" not in run
        else:
            assert run.get("guard_started") is True
        env.wait_running()
        assert env.running_venv() == target
