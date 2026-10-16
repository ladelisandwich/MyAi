"""Windows behaviour of the updater (design 8.2, 9.2, 11 E.2; test plan 18.1), skipped elsewhere: the junction
swap and rollback while a process runs from the old venv, a runner started with the section 8 flags
outliving its parent's kill-on-close job, the Job-object kill of a ``cmd /c`` + ``ping -t`` tree, and no
console window for anything the updater starts.

Two of them have a POSIX twin that runs on Linux (own session instead of the Job object, ``start_new_session``
instead of ``CREATE_NO_WINDOW``); the POSIX process-group kill is in ``test_update_contracts.py``.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

from homemesh.update import files, launch, runner
from update_support import HOMEMESH_SRC, Layout, Repo

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-only (junctions, Job objects, creation flags)")


@pytest.fixture
def env(tmp_path):
    layout = Layout(tmp_path, Repo(tmp_path / "repo"))
    yield layout
    layout.cleanup()


def _same(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


# --------------------------------------------------------------------------- junctions (E.2, G.2, A.3)

@windows_only
def test_junction_swap_and_rollback_while_a_process_runs_from_the_old_venv(env):
    new_venv = env.make_venv("b" * 40, ts="20270101000000")
    held = env.old_venv / "held.txt"
    held.write_text("old")
    # Like `homemesh logs -f` from the old venv: cwd inside it and a file in it open the whole time.
    proc = subprocess.Popen([sys.executable, "-c", "import sys, time\nf = open(sys.argv[1], 'rb')\n"
                             "print('ready', flush=True)\ntime.sleep(60)\n", str(held)],
                            cwd=str(env.old_venv), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            creationflags=runner.CREATE_NO_WINDOW)
    try:
        assert proc.stdout.readline().strip() == b"ready"
        run = runner._Apply(env.args("apply", expected="a" * 40), runner.Hooks())
        run.point_link(str(new_venv))
        assert runner._is_link(env.stable) and _same(env.stable, new_venv)
        for leftover in (".next", ".old"):
            assert not os.path.lexists(str(env.stable) + leftover)
        assert proc.poll() is None and held.read_text() == "old"  # the old venv was never touched
        assert (env.stable / "homemesh-install.json").is_file()  # reads go through the junction
        run.point_link(str(env.old_venv))  # the rollback flips back the same way
        assert runner._is_link(env.stable) and _same(env.stable, env.old_venv)
        assert (new_venv / "homemesh-install.json").is_file(), "removing a junction must not touch its target"
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


@windows_only
@pytest.mark.parametrize("left, expect_new", [((".next", ".old"), True), ((".old",), False)])
def test_a_junction_swap_cut_between_the_renames_is_repaired(env, left, expect_new):
    new_venv = env.make_venv("b" * 40, ts="20270101000000")
    stable = str(env.stable)
    os.rename(stable, stable + ".old")  # "venv" → "venv.old" happened, "venv.next" → "venv" did not
    if ".next" in left:
        import _winapi

        _winapi.CreateJunction(str(new_venv), stable + ".next")
    run = runner._Apply(env.args("apply", expected="a" * 40), runner.Hooks())
    run.repair_link()
    assert runner._is_link(stable) and _same(stable, new_venv if expect_new else env.old_venv)


# --------------------------------------------------------------------------- the runner outlives its parent (8.2)

PARENT = r'''
import json, os, sys
from pathlib import Path
from homemesh.update import launch, runner

spec = json.loads(Path(sys.argv[1]).read_text())
in_job = None
if sys.platform == "win32":
    import ctypes
    # This parent sits in a kill-on-close job (like a terminal, or the venv launcher HomeMesh runs under):
    # when it exits, everything still in the job is killed.
    k32 = runner._kernel32()
    job = runner._WinJob(k32.GetCurrentProcess(),
                         runner.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | runner.JOB_OBJECT_LIMIT_BREAKAWAY_OK)
handle = launch.RunnerHandle(mode="check", run_id=spec["run_id"], method="posix", args_file=Path(spec["args"]),
                             log_file=Path(spec["log"]), out_file=None, run_file=None)
from homemesh.common.paths import AppPaths
paths = AppPaths(config_dir=Path(spec["home"]), data_dir=Path(spec["home"]) / "data")
launch._start_child(handle, [sys.executable, "-c", spec["child"], spec["pidfile"]], paths, foreground=False)
if sys.platform == "win32":
    result = ctypes.c_int(0)
    ctypes.WinDLL("kernel32").IsProcessInJob(ctypes.c_void_p(int(handle.process._handle)),
                                             ctypes.c_void_p(job.handle), ctypes.byref(result))
    in_job = bool(result.value)
print(json.dumps({"pid": handle.pid, "in_job": in_job}), flush=True)
if sys.platform != "win32":
    import time
    time.sleep(60)  # the test kills this parent's whole process group
'''

CHILD = ("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n")


def test_a_runner_started_with_the_launch_flags_outlives_its_parent(env, tmp_path):
    rid = files.new_run_id()
    spec = tmp_path / "spec.json"
    pidfile = tmp_path / "child.pid"
    spec.write_text(json.dumps({"run_id": rid, "args": str(tmp_path / "a.json"), "home": str(env.home),
                                "log": str(files.check_log_file(env.paths, rid)), "child": CHILD,
                                "pidfile": str(pidfile)}))
    script = tmp_path / "parent.py"
    script.write_text(PARENT)
    child_env = {**os.environ, "PYTHONPATH": str(HOMEMESH_SRC)}
    kwargs = {"creationflags": runner.CREATE_NO_WINDOW} if WINDOWS else {"start_new_session": True}
    parent = subprocess.Popen([sys.executable, str(script), str(spec)], stdout=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, env=child_env, **kwargs)
    child = None
    try:
        info = json.loads(parent.stdout.readline())
        child = info["pid"]
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "the runner stand-in never started"
        if WINDOWS:
            if info["in_job"]:
                pytest.skip("this test runner's own job forbids breaking away; launch fell back to staying in it")
            assert parent.wait(timeout=30) == 0  # exiting closes its kill-on-close job
        else:
            os.killpg(parent.pid, signal.SIGKILL)  # the parent and everything in its process group
            parent.wait(timeout=30)
        time.sleep(1.0)
        assert _alive(child), "the runner died with its parent"
    finally:
        if parent.poll() is None:
            parent.kill()
        if child is not None:
            with contextlib.suppress(psutil.Error):
                psutil.Process(child).kill()


# --------------------------------------------------------------------------- the Job-object kill (9.2)

@windows_only
def test_a_timeout_kills_cmd_and_ping_through_the_job_object(env):
    run = runner._Check(env.args("check"), runner.Hooks())
    comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    t0 = time.time()
    res = run.cmd("x", [comspec, "/d", "/c", "ping -t 127.0.0.1"], timeout_s=3.0, env=run.tool_env())
    assert res.timed_out and "127.0.0.1" in res.out  # ping really ran
    deadline = time.monotonic() + 10
    while True:
        left = [p.pid for p in psutil.process_iter(["name", "create_time"])
                if (p.info["name"] or "").lower() == "ping.exe" and (p.info["create_time"] or 0) >= t0 - 1]
        if not left or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert left == [], "ping.exe survived the timeout"


# --------------------------------------------------------------------------- no console windows (8.2, 9.2)

def test_everything_the_updater_starts_is_windowless_or_in_its_own_session(env, tmp_path, monkeypatch):
    env.repo.commit({"x": "1"}, "a change")
    calls: list[dict] = []
    real = subprocess.Popen

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            calls.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    args = env.args("check")
    assert runner.run_check(args, runner.Hooks()) == runner.EXIT_OK  # git, as the check runs it
    assert files.read_check_result(env.paths, args["run_id"])["ok"] is True
    started = runner._spawn_detached([sys.executable, "-c", "pass"], env=dict(os.environ), cwd=str(tmp_path))
    started.wait(timeout=30)  # `homemesh run` as the apply runner starts it
    rid = files.new_run_id()
    handle = launch.RunnerHandle(mode="check", run_id=rid, method="posix", args_file=tmp_path / "a.json",
                                 log_file=files.check_log_file(env.paths, rid), out_file=None, run_file=None)
    for foreground in (False, True):  # the runner itself, from the worker and from the CLI
        launch._start_child(handle, [sys.executable, "-c", "pass"], env.paths, foreground=foreground)
        handle.process.wait(timeout=30)
    assert len(calls) >= 6
    for kw in calls:
        if WINDOWS:
            assert kw.get("creationflags", 0) & (runner.CREATE_NO_WINDOW | runner.DETACHED_PROCESS), kw
        else:
            assert kw.get("start_new_session") is True, kw
        assert kw.get("stdin") == subprocess.DEVNULL
