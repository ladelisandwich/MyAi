"""The runner's copy of the lock primitive excludes HomeMesh's ``InstanceLock`` across processes, both ways,
for the instance lock and the update lock. The runner side runs exactly as in production: the runner file
executed by the base interpreter with ``-I -S`` (loaded with runpy, so nothing from homemesh is imported)."""

import subprocess
import sys
import time

import pytest

from homemesh.common.instance import InstanceLock, lock_is_held
from homemesh.update import files, runner

RUNNER_HOLDER = """
import runpy, sys
ns = runpy.run_path(sys.argv[1])
lock = ns["FileLock"](sys.argv[2])
print("held" if lock.acquire() else "busy", flush=True)
sys.stdin.read()
"""

HOMEMESH_HOLDER = """
import sys
from homemesh.common.instance import InstanceLock
lock = InstanceLock(__import__("pathlib").Path(sys.argv[1]))
print("held" if lock.acquire() else "busy", flush=True)
sys.stdin.read()
"""


def _base_python():
    return getattr(sys, "_base_executable", None) or sys.executable


def _hold(argv):
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = p.stdout.readline().decode().strip()
    return p, line


def _release(p):
    p.stdin.close()
    p.wait(timeout=10)


@pytest.mark.parametrize("name", ["homemesh.lock", "update.lock"])
def test_runner_lock_excludes_homemesh_lock(tmp_path, name):
    path = tmp_path / name
    p, state = _hold([_base_python(), "-I", "-S", "-c", RUNNER_HOLDER, runner.__file__, str(path)])
    try:
        assert state == "held", p.stderr.read().decode()
        assert InstanceLock(path).acquire() is False
        assert lock_is_held(path) is True
    finally:
        _release(p)
    lock = InstanceLock(path)
    assert lock.acquire() is True  # released by the OS when the holder ended
    lock.release()


@pytest.mark.parametrize("name", ["homemesh.lock", "update.lock"])
def test_homemesh_lock_excludes_runner_lock(tmp_path, name):
    path = tmp_path / name
    p, state = _hold([sys.executable, "-c", HOMEMESH_HOLDER, str(path)])
    try:
        assert state == "held", p.stderr.read().decode()
        assert runner.FileLock(path).acquire() is False
        assert runner.lock_is_held(path) is True
    finally:
        _release(p)
    lock = runner.FileLock(path)
    assert lock.acquire() is True and lock.held
    lock.release()
    assert not lock.held


def test_runner_holding_the_update_lock_blocks_finalizing(home):
    files.ensure_update_dir(home)
    rid = files.new_run_id()
    files.run_dir(home, rid).mkdir(parents=True)
    files.atomic_write_json(files.run_file(home, rid), {"v": 1, "run_id": rid, "origin": "remote", "phase": "building",
                                                        "status": None, "started_at": "x", "updated_at": "x"})
    files.write_latest(home, rid)
    p, state = _hold([_base_python(), "-I", "-S", "-c", RUNNER_HOLDER, runner.__file__, str(files.lock_file(home))])
    try:
        assert state == "held"
        assert files.update_lock_held(home) is True
        assert files.finalize_dead_run(home, installed_commit=None) is None  # a live runner is never finalized
        assert files.read_run(home, rid)["status"] is None
    finally:
        _release(p)
    deadline = time.monotonic() + 5
    while files.update_lock_held(home) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert files.finalize_dead_run(home, installed_commit=None)["status"] == "interrupted"
