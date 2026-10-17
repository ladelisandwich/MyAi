"""The worker agent's side of the files it shares with the update runner (design 7.4, 7.5, 11 C and F.2), each
checked against the **real runner code** rather than a description of it: the runner writes run.json phase by
phase and drains with ``_Apply.drain()``, the worker's ``UpdateManager`` reads, rejects work and writes
``drain.json`` / ``agent.json``, and the runner's own checks must accept what the worker wrote.

``test_update_contracts.py`` covers the same files written by ``update.files`` directly; these tests add what
``worker/updates.py`` does with them (when it drains, what it lists, which commit and process it names).
"""

import asyncio
import contextlib
import os
import threading
import typing

import pytest

from homemesh.common.config import HomeMeshConfig
from homemesh.common.instance import InstanceLock
from homemesh.protocol.update import PHASES, RESTART_PHASES, BuildInfo
from homemesh.update import files, record, runner
from homemesh.worker.updates import UpdateManager
from update_support import Layout, Repo

SHA = "b" * 40


@pytest.fixture
def env(tmp_path):
    return Layout(tmp_path, Repo(tmp_path / "repo"))


def _manager(env):
    cfg = HomeMeshConfig()
    cfg.node.name = "testbox"
    cfg.node.roles = ["worker"]

    async def send(msg):
        pass

    return UpdateManager(cfg, env.paths, send=send, jobs={}, job_types={})


def _runner(env, **timeouts):
    hooks = runner.Hooks()
    hooks.timeouts.update(timeouts)
    hooks.poll_s = 0.05
    args = env.args("apply", expected=SHA)
    run = runner._Apply(args, hooks)
    os.makedirs(run.run_dir, exist_ok=True)
    run.accept()
    return run, args


@contextlib.contextmanager
def _held(path):
    """A lock held the way its real holder holds it (HomeMesh: the instance lock; the runner: update.lock)."""
    lock = InstanceLock(path)
    assert lock.acquire()
    try:
        yield
    finally:
        lock.release()


async def _drain_with_worker(env, run, mgr, *, until):
    """Run the runner's drain step in a thread while the worker's watch loop refreshes, until ``until()``."""
    outcome: dict[str, object] = {}

    def drain():
        try:
            run.drain()
            outcome["ok"] = True
        except runner.Fail as f:
            outcome["fail"] = f.err

    t = threading.Thread(target=drain)
    t.start()
    try:
        while t.is_alive():
            await mgr.refresh()
            if until():
                break
            await asyncio.sleep(0.05)
    finally:
        await asyncio.to_thread(t.join, 15)
    return outcome


async def test_each_runner_phase_puts_the_worker_in_the_right_mode(env):
    run, args = _runner(env)
    mgr = _manager(env)
    with _held(files.lock_file(env.paths)):  # the runner holds update.lock for its whole life
        for phase in PHASES:
            run.phase(phase)
            await mgr.refresh()
            assert mgr.report is not None and mgr.report["phase"] == phase and mgr.report["status"] is None
            assert mgr.report["run_id"] == args["run_id"] and mgr.report["job_id"] == args["job_id"]
            assert mgr.draining is (phase in RESTART_PHASES), phase
            written = files.read_drain_file(env.paths, args["run_id"]) is not None
            assert written is (PHASES.index(phase) >= PHASES.index("draining")), phase  # from draining on
        run.finish("rolled_back", error=runner.issue("update_new_version_crashed", exit="exit code 1"),
                   installed=env.old_commit)
        await mgr.refresh()
    assert not mgr.draining and mgr.report["status"] == "rolled_back"
    assert mgr.report["error"]["code"] == "update_new_version_crashed"
    assert mgr.report["installed_commit"] == env.old_commit
    # a run the worker reports is never one it finalized by mistake while the runner held the lock
    assert files.read_run(env.paths, args["run_id"])["status"] == "rolled_back"


async def test_a_run_whose_runner_is_gone_is_finalized_by_the_worker(env, monkeypatch):
    run, args = _runner(env)
    run.phase("switching")
    info = BuildInfo(version="0.3.0", commit=env.old_commit, source="installer", os="linux", can_update=True,
                     update_allowed=True, source_dir=str(env.repo.src))
    monkeypatch.setattr(record, "build_info", lambda paths=None, **kw: info)
    mgr = _manager(env)
    await mgr.refresh()  # nobody holds update.lock: the runner died
    assert mgr.report["status"] == "interrupted" and mgr.report["installed_commit"] == env.old_commit
    assert mgr.report["error"]["code"] == "update_interrupted" and not mgr.draining
    # a later runner sees a final run and leaves it alone
    later, _ = _runner(env)
    assert files.read_run(env.paths, args["run_id"])["status"] == "interrupted"
    assert later.run["run_id"] != args["run_id"]


async def test_the_runner_drains_on_what_the_worker_lists(env):
    run, args = _runner(env, drain=10.0)
    mgr = _manager(env)
    mgr.job_types["job_chat"] = "chat"  # a chat still running on this computer
    mgr.job_types["job_upd"] = "update.apply"  # the update's own job never counts
    mgr.job_ids.add("job_upd")
    drain_file = files.drain_file(env.paths, args["run_id"])
    with _held(files.lock_file(env.paths)), _held(env.paths.lock_file):  # update helper + a running HomeMesh
        listed = asyncio.Event()

        def chat_listed():
            data = files.read_drain_file(env.paths, args["run_id"])
            if data and data["running"] == [{"job_id": "job_chat", "type": "chat"}]:
                listed.set()
                mgr.job_types.pop("job_chat")  # ... and now it finished
            return False

        outcome = await _drain_with_worker(env, run, mgr, until=chat_listed)
        assert listed.is_set() and outcome == {"ok": True}, outcome
    data = runner.read_json(drain_file)
    assert data["running"] == [] and data["pid"] == os.getpid()
    started = runner.parse_iso(files.read_run(env.paths, args["run_id"])["drain_started_at"])
    assert runner.parse_iso(data["at"]) >= started
    assert "drained: no jobs running" in env.log_text(args)


async def test_jobs_still_running_at_the_deadline_stop_the_update(env):
    run, _ = _runner(env, drain=0.6)
    mgr = _manager(env)
    mgr.job_types["job_embed"] = "embed"
    with _held(files.lock_file(env.paths)), _held(env.paths.lock_file):
        outcome = await _drain_with_worker(env, run, mgr, until=lambda: False)
    assert outcome["fail"]["code"] == "update_jobs_running" and "embed" in outcome["fail"]["message"]
    assert mgr.draining  # until the runner writes its final status


async def test_the_runner_accepts_the_agent_file_the_worker_writes(env, monkeypatch):
    info = BuildInfo(version="0.3.0", commit=SHA, source="updater", os="linux", can_update=True,
                     update_allowed=True, source_dir=str(env.repo.src))
    monkeypatch.setattr(record, "build_info", lambda paths=None, **kw: info)
    mgr = _manager(env)
    await mgr.build_info()
    me = runner._Proc(pid=os.getpid(), started=runner.proc_start_time(os.getpid()))
    for state in typing.get_args(files.AgentState):
        await mgr.set_agent_state(state, f"detail for {state}")
        agent = runner.read_json(files.agent_file(env.paths))
        assert runner._Apply._agent_is(agent, me), agent  # same process: pid and start time
        assert agent["state"] == state and agent["build_commit"] == SHA
        assert agent["detail"] == f"detail for {state}"
    # the states the runner's verify step acts on are exactly the ones the worker writes
    src = open(runner.__file__, encoding="utf-8").read()
    for state in ("connected", "rejected", "unreachable"):
        assert f'"{state}"' in src
    assert set(typing.get_args(files.AgentState)) == {"connecting", "connected", "rejected", "unreachable"}
