"""Launching the runner (design section 8): the environment probes, the three launch methods (systemd-run,
POSIX child, Windows flags), the waits and their exit-code mapping, and the kill on an accept timeout."""

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from homemesh.common.config import HomeMeshConfig
from homemesh.common.paths import AppPaths
from homemesh.protocol.update import installer_command
from homemesh.update import files, launch, record, runner
from update_support import Layout, Repo

SHA = "c" * 40

STUB = r'''
import json, os, subprocess, sys, time
mode, args_file = sys.argv[1], sys.argv[3]
args = json.load(open(args_file, encoding="utf-8"))
what = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub-mode.txt")).read().strip()
print("stub output with a token ghp_LEAKEDtoken123 and https://u:pw@host/x", flush=True)
if what == "accept":
    with open(os.path.join(os.path.dirname(args_file), "run.json"), "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "run_id": args["run_id"], "phase": "accepted", "status": None}, fh)
    sys.exit(0)
if what == "result":
    with open(args["out"], "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "ok": False, "error": {"code": "update_fetch_failed", "message": "m", "hint": "h"}}, fh)
    sys.exit(0)
if what == "badresult":
    with open(args["out"], "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "ok": True}, fh)
    sys.exit(0)
if what.startswith("exit"):
    code = int(what[4:])
    print(f"homemesh-update-runner-exit {code}: stub", file=sys.stderr, flush=True)
    sys.exit(code)
if what == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "grandchild.pid"), "w") as fh:
        fh.write(str(child.pid))
    time.sleep(120)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    layout = Layout(tmp_path, Repo(tmp_path / "repo"))
    rec = record.load(layout.old_venv)
    monkeypatch.setattr(record, "load", lambda venv=None: rec)
    monkeypatch.setenv("HOMEMESH_HOME", str(layout.home))
    monkeypatch.setattr(launch, "service_kind", lambda paths: "windows" if sys.platform == "win32" else "posix")
    monkeypatch.setattr(launch, "inside_homemesh_unit", lambda: False)
    layout.cfg = HomeMeshConfig.model_validate({"node": {"name": "testbox", "roles": ["worker"]}})
    return layout


@pytest.fixture
def stub(tmp_path, monkeypatch):
    script = tmp_path / "stub" / "runner_stub.py"
    script.parent.mkdir()
    script.write_text(STUB, encoding="utf-8")
    monkeypatch.setenv(launch.RUNNER_ENV, str(script))
    monkeypatch.setenv(launch.TESTING_ENV, "1")

    def mode(what):
        (script.parent / "stub-mode.txt").write_text(what)
        return script.parent

    return mode


def _gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- probes

def test_base_python_is_the_base_interpreter():
    exe = launch.base_python()
    assert exe.is_file() and exe == launch._base_python_path()
    assert not (exe.parent / "pyvenv.cfg").exists() and not (exe.parent.parent / "pyvenv.cfg").exists()


def test_base_python_on_windows_is_the_console_python(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "pythonw.exe"), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert launch._base_python_path() == tmp_path / "python.exe"


def test_missing_base_python(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "gone" / "python3.13"), raising=False)
    monkeypatch.setattr(record, "load", lambda venv=None: None)
    with pytest.raises(launch.UpdateLaunchError) as exc:
        launch.base_python()
    assert exc.value.code == "update_no_base_python" and "gone" in exc.value.message


def test_launch_env_drops_what_would_confuse_the_runner():
    env = launch.launch_env({"PATH": "/bin", "HOME": "/h", "PYTHONPATH": "x", "PYTHONHOME": "y",
                             "__PYVENV_LAUNCHER__": "z", "HOMEMESH_DETACHED": "1", "VSCODE_PID": "1",
                             "GIT_DIR": "/evil", "GIT_ASKPASS": "a", "SSH_ASKPASS": "a", "HOMEMESH_HOME": "/hm",
                             "HOMEMESH__SERVER__PORT": "1", "LANG": "C"})
    assert env == {"PATH": "/bin", "HOME": "/h", "HOMEMESH_HOME": "/hm", "HOMEMESH__SERVER__PORT": "1", "LANG": "C"}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="cgroups are Linux-only")
@pytest.mark.parametrize("content, inside", [
    ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/homemesh.service\n", True),
    ("12:pids:/user.slice\n1:name=systemd:/user.slice/user@1000.service/homemesh.service\n", True),
    ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/homemesh-update-apply-u1.service\n", False),
    ("0::/user.slice/user-1000.slice/session-2.scope\n", False)])
def test_inside_homemesh_unit(monkeypatch, tmp_path, content, inside):
    f = tmp_path / "cgroup"
    f.write_text(content)
    monkeypatch.setattr(launch, "CGROUP_FILE", str(f))
    assert launch.inside_homemesh_unit() is inside
    assert runner.inside_homemesh_unit(str(f)) is inside


def test_resolve_git(monkeypatch, tmp_path):
    real = launch.resolve_git(None)
    assert real.is_absolute() and real.is_file()
    assert launch.resolve_git(str(real)) == real
    assert launch.resolve_git(str(tmp_path / "nope")) == real  # stale record → PATH
    monkeypatch.setattr(launch.shutil, "which", lambda name, *a, **kw: None)
    with pytest.raises(launch.UpdateLaunchError) as exc:
        launch.resolve_git(str(tmp_path / "nope"))
    assert exc.value.code == "update_git_missing"


def test_log_tail_is_redacted_and_never_raises(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"".join(f"line {i}\n".encode() for i in range(50))
                    + b"push https://me:ghp_tokentoken@github.com/a/b \xff\x1b[31m\n"
                    + b"  | fatal: unable to access 'https://github.com/me/private.git/': Could not resolve host\n"
                    + b"  | fatal: 'git@github.com:me/private.git' does not appear to be a git repository\n")
    tail = launch.log_tail(log, lines=5)
    assert tail.splitlines()[:2] == ["line 48", "line 49"]
    assert "ghp_" not in tail and "me:" not in tail and "\x1b" not in tail
    assert "github.com/" not in tail and "github.com:" not in tail and "private.git" not in tail
    assert "unable to access '<remote address>': Could not resolve host" in tail
    assert launch.log_tail(tmp_path / "missing.log") == ""


def test_service_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if sys.platform == "win32":
        assert launch.service_kind(None) == "windows"
        return
    assert launch.service_kind(None) == "posix"  # no unit file
    unit = tmp_path / "systemd" / "user" / "homemesh.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(launch, "_systemd_user_manager", lambda: False)
    assert launch.service_kind(None) == "posix"
    monkeypatch.setattr(launch, "_systemd_user_manager", lambda: True)
    assert launch.service_kind(None) == ("systemd" if sys.platform.startswith("linux") else "posix")


def test_homemesh_home_for_the_started_processes(monkeypatch, tmp_path):
    paths = AppPaths(config_dir=tmp_path / "hm", data_dir=tmp_path / "hm" / "data")
    monkeypatch.setenv("HOMEMESH_HOME", str(tmp_path / "env-home"))
    assert launch._homemesh_home(paths) == str((tmp_path / "env-home").resolve())
    monkeypatch.delenv("HOMEMESH_HOME")
    assert launch._homemesh_home(paths) == str((tmp_path / "hm").resolve())  # `homemesh --home`
    from homemesh.common.paths import default_paths

    assert launch._homemesh_home(default_paths()) is None


# --------------------------------------------------------------------------- the real runner, launched

def test_start_check_runs_the_real_runner(env):
    env.repo.commit({"x": "1"}, "one change")
    handle = launch.start_check(env.paths, env.cfg, origin="remote", job_id="job-7")
    assert handle.mode == "check" and handle.method in ("posix", "windows") and handle.pid
    result = launch.wait_for_check_sync(handle, timeout_s=120)
    assert result.ok and result.behind == 1 and result.allowed is True
    args = files.read_json(handle.args_file)
    assert args["job_id"] == "job-7" and args["origin"] == "remote" and args["homemesh_home"] == str(env.home)
    assert args["computer"] == "testbox" and args["roles"] == ["worker"]
    assert handle.log_file == files.check_log_file(env.paths, handle.run_id)
    if os.name == "posix":
        assert stat.S_IMODE(handle.log_file.stat().st_mode) == 0o600
    assert "HomeMesh update check" in handle.log_file.read_text()
    handle.process.wait(timeout=30)
    assert launch.runner_exit_code(handle) == runner.EXIT_OK


async def test_async_wait_for_check_in_the_foreground(env):
    handle = launch.start_check(env.paths, env.cfg, origin="local", foreground=True)
    assert handle.method == "foreground"
    result = await launch.wait_for_check(handle, timeout_s=120)
    assert result.ok and result.behind == 0 and result.can_update is False


def test_no_install_record_is_not_supported(env, monkeypatch):
    monkeypatch.setattr(record, "load", lambda venv=None: None)
    with pytest.raises(launch.UpdateLaunchError) as exc:
        launch.start_check(env.paths, env.cfg, origin="remote")
    assert exc.value.code == "update_not_supported" and "testbox" in exc.value.message
    assert exc.value.hint == "Update it by hand once: " + installer_command(record.current_os(), None, pull=True)


# --------------------------------------------------------------------------- waits, with stub runners

def test_wait_for_accept_returns_the_run(env, stub):
    stub("accept")
    rid = files.new_run_id()
    handle = launch.start_apply(env.paths, env.cfg, run_id=rid, expected_commit=SHA, force=False, origin="remote",
                                job_id="job-1")
    run = launch.wait_for_accept_sync(handle, timeout_s=30)
    assert run["run_id"] == rid and run["phase"] == "accepted"
    assert handle.run_file == files.run_file(env.paths, rid) and handle.out_file is None
    args = files.read_json(files.args_file(env.paths, rid))
    assert args["expected_commit"] == SHA and args["mode"] == "apply" and args["out"] is None


async def test_async_wait_for_accept(env, stub):
    stub("accept")
    handle = launch.start_apply(env.paths, env.cfg, run_id=files.new_run_id(), expected_commit=SHA, force=True,
                                origin="local")
    run = await launch.wait_for_accept(handle, timeout_s=30)
    assert run["phase"] == "accepted"


@pytest.mark.parametrize("what, code, text", [
    ("exit3", "update_busy", "checking for updates"),
    ("exit5", "update_no_base_python", "was not found"),
    ("exit2", "update_launch_failed", "rejected its arguments"),
    ("exit0", "update_launch_failed", "without writing a result"),
    ("exit1", "update_launch_failed", "exit code 1"),
])
def test_runner_exit_codes_are_mapped(env, stub, what, code, text):
    stub(what)
    handle = launch.start_apply(env.paths, env.cfg, run_id=files.new_run_id(), expected_commit=SHA, force=False,
                                origin="remote", job_id="j")
    with pytest.raises(launch.UpdateLaunchError) as exc:
        launch.wait_for_accept_sync(handle, timeout_s=30)
    assert exc.value.code == code and text in exc.value.message
    tail = exc.value.details["log_tail"]
    assert "stub output" in tail and "ghp_LEAKED" not in tail and "u:pw@" not in tail


def test_accept_timeout_kills_the_runner_tree(env, stub):
    folder = stub("hang")
    handle = launch.start_apply(env.paths, env.cfg, run_id=files.new_run_id(), expected_commit=SHA, force=False,
                                origin="remote", job_id="j")
    deadline = time.monotonic() + 20
    while not (folder / "grandchild.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int((folder / "grandchild.pid").read_text())
    with pytest.raises(launch.UpdateLaunchError) as exc:
        launch.wait_for_accept_sync(handle, timeout_s=1.5)
    assert exc.value.code == "update_launch_failed"
    assert "did not start within 2 seconds and was stopped; nothing was changed" in exc.value.message
    assert handle.process.poll() is not None
    assert _gone(grandchild), "the runner's child survived"


def test_kill_check_stops_the_tree_and_is_idempotent(env, stub):
    folder = stub("hang")
    handle = launch.start_check(env.paths, env.cfg, origin="remote")
    deadline = time.monotonic() + 20
    while not (folder / "grandchild.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int((folder / "grandchild.pid").read_text())
    launch.kill_check(handle)
    launch.kill_check(handle)
    assert handle.process.poll() is not None and _gone(grandchild)
    with pytest.raises(launch.UpdateLaunchError):
        launch.wait_for_check_sync(handle, timeout_s=5)


@pytest.mark.parametrize("what, text", [("exit0", "without writing a result"),
                                        ("badresult", "could not be read")])
def test_check_without_a_valid_result(env, stub, what, text):
    stub(what)
    handle = launch.start_check(env.paths, env.cfg, origin="remote")
    with pytest.raises(launch.UpdateLaunchError) as exc:
        launch.wait_for_check_sync(handle, timeout_s=30)
    assert exc.value.code == "update_launch_failed" and text in exc.value.message


def test_check_error_result_is_returned_for_the_caller_to_map(env, stub):
    stub("result")
    handle = launch.start_check(env.paths, env.cfg, origin="remote")
    result = launch.wait_for_check_sync(handle, timeout_s=30)
    assert result.ok is False and result.error.code == "update_fetch_failed"


async def test_check_timeout(env, stub):
    stub("hang")
    handle = launch.start_check(env.paths, env.cfg, origin="remote")
    try:
        with pytest.raises(launch.UpdateLaunchError) as exc:
            await launch.wait_for_check(handle, timeout_s=0.5, poll_s=0.1)
        assert "the check did not finish in 1 second" in exc.value.message
        assert handle.process.poll() is None  # the caller decides to kill it
    finally:
        launch.kill_check(handle)
    await asyncio.sleep(0)


# --------------------------------------------------------------------------- systemd-run

class _RunQuiet:
    def __init__(self, rc=0, stderr=b""):
        self.calls, self.rc, self.stderr = [], rc, stderr

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.rc if argv[0].endswith("systemd-run") else 0, b"", self.stderr)


def _no_popen(*a, **kw):
    raise AssertionError("must not start a child process")


def test_apply_inside_the_unit_goes_through_systemd_run(env, monkeypatch):
    fake = _RunQuiet()
    monkeypatch.setattr(launch, "run_quiet", fake)
    monkeypatch.setattr(launch, "inside_homemesh_unit", lambda: True)
    monkeypatch.setattr(launch.subprocess, "Popen", _no_popen)
    rid = files.new_run_id()
    handle = launch.start_apply(env.paths, env.cfg, run_id=rid, expected_commit=SHA, force=False, origin="remote",
                                job_id="j")
    argv = fake.calls[-1]
    log = str(files.apply_log_file(env.paths, rid))
    assert argv[1:6] == ["--user", "--collect", "--quiet", f"--unit=homemesh-update-apply-{rid}",
                         "--service-type=exec"]
    assert argv[6:12] == ["-p", "RuntimeMaxSec=10800", "-p", f"StandardOutput=append:{log}", "-p",
                          f"StandardError=append:{log}"]
    setenv = [a for a in argv if a.startswith("--setenv=")]
    assert f"--setenv=HOMEMESH_HOME={env.home}" in setenv and any(a.startswith("--setenv=PATH=") for a in setenv)
    assert not any(a.startswith(("--setenv=PYTHON", "--setenv=GIT_")) for a in setenv)
    cmd = argv[argv.index("--") + 1:]
    assert cmd[1:5] == ["-I", "-S", "-X", "utf8"] and cmd[5] == str(Path(runner.__file__).resolve())
    assert cmd[6:] == ["apply", "--args", str(files.args_file(env.paths, rid))]
    assert handle.method == "systemd" and handle.unit == f"homemesh-update-apply-{rid}" and handle.process is None


def test_check_inside_the_unit_uses_a_short_runtime(env, monkeypatch):
    fake = _RunQuiet()
    monkeypatch.setattr(launch, "run_quiet", fake)
    monkeypatch.setattr(launch, "inside_homemesh_unit", lambda: True)
    monkeypatch.setattr(launch.subprocess, "Popen", _no_popen)
    handle = launch.start_check(env.paths, env.cfg, origin="remote")
    assert "RuntimeMaxSec=300" in fake.calls[-1] and handle.unit.startswith("homemesh-update-check-u")


def test_systemd_run_failure_inside_the_unit_never_falls_back(env, monkeypatch):
    monkeypatch.setattr(launch, "run_quiet", _RunQuiet(rc=1, stderr=b"Failed to start transient service unit"))
    monkeypatch.setattr(launch, "inside_homemesh_unit", lambda: True)
    monkeypatch.setattr(launch.subprocess, "Popen", _no_popen)
    rid = files.new_run_id()
    with pytest.raises(launch.UpdateLaunchError) as exc:
        launch.start_apply(env.paths, env.cfg, run_id=rid, expected_commit=SHA, force=False, origin="remote")
    assert exc.value.code == "update_launch_failed" and "Failed to start transient" in exc.value.message
    assert f"journalctl --user -u 'homemesh-update-apply-{rid}' -n 50" in exc.value.hint


def test_systemd_run_failure_outside_the_unit_falls_back_to_a_child(env, stub, monkeypatch):
    stub("accept")
    monkeypatch.setattr(launch, "run_quiet", _RunQuiet(rc=1))
    monkeypatch.setattr(launch, "service_kind", lambda paths: "systemd")
    monkeypatch.setattr(launch, "_unit_file", lambda: env.home / "homemesh.service")
    handle = launch.start_apply(env.paths, env.cfg, run_id=files.new_run_id(), expected_commit=SHA, force=False,
                                origin="local")
    assert handle.method in ("posix", "windows") and handle.process is not None
    assert launch.wait_for_accept_sync(handle, timeout_s=30)["phase"] == "accepted"
    assert "starting the update helper as a background process instead" in handle.log_file.read_text()


def test_systemd_exit_codes(env, monkeypatch, tmp_path):
    handle = launch.RunnerHandle(mode="apply", run_id="u20261013120000-0123abcd", method="systemd",
                                 args_file=tmp_path / "args.json", log_file=tmp_path / "x.log", out_file=None,
                                 run_file=tmp_path / "run.json", unit="homemesh-update-apply-u20261013120000-0123abcd")
    states = iter([{"LoadState": "loaded", "ActiveState": "active"},
                   {"LoadState": "loaded", "ActiveState": "failed", "ExecMainStatus": "3"},
                   {"LoadState": "not-found", "ActiveState": "inactive"}, None])
    monkeypatch.setattr(launch, "_unit_state", lambda unit: next(states))
    assert launch.runner_exit_code(handle) is None
    assert launch.runner_exit_code(handle) == 3
    assert launch.runner_exit_code(handle) == -1  # collected: the code is gone ...
    assert launch.runner_exit_code(handle) is None
    handle.log_file.write_text(f"{runner.EXIT_MARKER} {runner.EXIT_BUSY}: another update\n")
    assert launch._map_exit(handle, -1).code == "update_busy"  # ... but the runner printed it


# --------------------------------------------------------------------------- Windows creation flags

class _Recorder:
    def __init__(self, fail_first_with_winerror=None):
        self.calls, self.fail = [], fail_first_with_winerror

    def __call__(self, cmd, **kw):
        self.calls.append(kw)
        if self.fail is not None and len(self.calls) == 1:
            err = OSError(13, "Access is denied")
            err.winerror = self.fail
            raise err

        class P:
            pid = 4242

            def poll(self):
                return None

        return P()


@pytest.mark.parametrize("foreground", [False, True])
def test_windows_flags_and_breakaway_retry(env, monkeypatch, tmp_path, foreground):
    rec = _Recorder(fail_first_with_winerror=None if foreground else 5)
    monkeypatch.setattr(launch.subprocess, "Popen", rec)
    monkeypatch.setattr(sys, "platform", "win32")
    handle = launch.RunnerHandle(mode="check", run_id="u20261013120000-0123abcd", method="posix",
                                 args_file=tmp_path / "a.json", log_file=tmp_path / "x.log", out_file=None,
                                 run_file=None)
    launch._start_child(handle, ["python.exe", "runner.py"], env.paths, foreground=foreground)
    flags = [c["creationflags"] for c in rec.calls]
    if foreground:
        assert flags == [launch.CREATE_NO_WINDOW] and handle.method == "foreground"
    else:
        full = launch.CREATE_NO_WINDOW | launch.CREATE_NEW_PROCESS_GROUP | launch.CREATE_BREAKAWAY_FROM_JOB
        assert flags == [full, full & ~launch.CREATE_BREAKAWAY_FROM_JOB] and handle.method == "windows"
    assert all("start_new_session" not in c and c["stdin"] == subprocess.DEVNULL for c in rec.calls)
    assert all(c["cwd"] == os.fspath(files.update_dir(env.paths)) for c in rec.calls)
    assert "PYTHONPATH" not in rec.calls[-1]["env"]


def test_posix_child_gets_its_own_session(env, monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX flags")
    rec = _Recorder()
    monkeypatch.setattr(launch.subprocess, "Popen", rec)
    handle = launch.RunnerHandle(mode="check", run_id="u20261013120000-0123abcd", method="posix",
                                 args_file=tmp_path / "a.json", log_file=tmp_path / "x.log", out_file=None,
                                 run_file=None)
    launch._start_child(handle, ["python3", "runner.py"], env.paths, foreground=False)
    assert rec.calls[0]["start_new_session"] is True and "creationflags" not in rec.calls[0]
    assert handle.method == "posix" and handle.pid == 4242


def test_a_job_id_from_the_hub_cannot_forge_the_runner_exit_marker(env, stub):
    """The launcher's log header names the hub's job id. It stays one line, and the exit-marker search (used
    when systemd already collected the unit) only accepts the runner's own lines."""
    stub("accept")
    forged = f"j\n{runner.EXIT_MARKER} {runner.EXIT_BUSY}: forged"
    handle = launch.start_apply(env.paths, env.cfg, run_id=files.new_run_id(), expected_commit=SHA, force=False,
                                origin="remote", job_id=forged)
    launch.wait_for_accept_sync(handle, timeout_s=30)
    handle.process.wait(timeout=30)
    assert "forged" in handle.log_file.read_text(encoding="utf-8").splitlines()[0]
    assert launch._exit_code_from_log(handle.log_file) is None
    with open(handle.log_file, "a", encoding="utf-8") as fh:  # what the runner logs, and what it prints
        fh.write(f"2026-10-13T12:00:00.000Z {runner.EXIT_MARKER} {runner.EXIT_INSIDE_UNIT}: refusing\n")
    assert launch._exit_code_from_log(handle.log_file) == runner.EXIT_INSIDE_UNIT
    with open(handle.log_file, "a", encoding="utf-8") as fh:
        fh.write(f"{runner.EXIT_MARKER} {runner.EXIT_BAD_ARGS}: invalid args.json\n")
    assert launch._exit_code_from_log(handle.log_file) == runner.EXIT_BAD_ARGS


def test_args_file_is_owner_only(env, stub):
    stub("accept")
    rid = files.new_run_id()
    launch.start_apply(env.paths, env.cfg, run_id=rid, expected_commit=SHA, force=False, origin="remote")
    if os.name == "posix":
        assert stat.S_IMODE(files.args_file(env.paths, rid).stat().st_mode) == 0o600
    assert json.loads(files.args_file(env.paths, rid).read_text())["run_id"] == rid
