"""Worker side of remote updates (design section 7): gating, the jobs, the watch loop, agent.json, hello.

The runner and its launcher are stubbed here (``update/launch.py`` is tested on its own); everything else
is real: consent marker, update lock, run files, the agent and, at the end, a coordinator over WebSockets.
"""

import asyncio
import contextlib
import json
import os
import ssl
from pathlib import Path
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import HomeMeshError
from homemesh.inference.mock import MockBackend
from homemesh.protocol import messages as pm
from homemesh.protocol.update import BuildInfo, UpdateCheckResult, format_update_error, is_run_id
from homemesh.update import consent, files, launch, record
from homemesh.worker.agent import FatalWorkerError, WorkerAgent
from homemesh.worker.updates import UPDATING_REASON, UpdateManager

SHA = "a" * 40
SHA2 = "b" * 40
SHA3 = "c" * 40
RUN = "u20261016120000-0123abcd"


def _cfg() -> HomeMeshConfig:
    cfg = HomeMeshConfig()
    cfg.node.name = "box"
    return cfg


def _manager(home, cfg=None):
    sent = []

    async def send(msg):
        sent.append(msg)

    jobs, types = {}, {}
    return UpdateManager(cfg or _cfg(), home, send=send, jobs=jobs, job_types=types), sent


async def _dispatch(mgr, job_type, payload, job_id="job_1"):
    await mgr.on_dispatch(pm.JobDispatch(job_id=job_id, job_type=job_type, priority="interactive",
                                         payload=payload, timeout_s=330))
    task = mgr.jobs.get(job_id)
    if task is not None:
        await task
    assert job_id not in mgr.jobs and job_id not in mgr.job_ids and job_id not in mgr.job_types


def _updatable(monkeypatch, commit=SHA):
    info = BuildInfo(version="0.3.0", commit=commit, source="installer", os="linux", can_update=True,
                     update_allowed=True, source_dir="/src/MyAi", logs_dir="/logs")
    monkeypatch.setattr(record, "build_info", lambda paths=None, **kw: info)
    return info


def _check_result(**over):
    data = {"v": 1, "ok": True, "checked_at": "2026-10-16T12:00:00Z", "allowed": False, "installed_commit": SHA,
            "upstream_commit": SHA2, "upstream_ref": "origin/main", "branch": "main", "remote": "origin",
            "behind": 2, "ahead": 0, "can_update": True,
            "commits": [{"sha": SHA2, "subject": "Fix it", "date": "2026-10-16T11:00:00Z"}]}
    data.update(over)
    return UpdateCheckResult.model_validate(data)


class FakeLaunch:
    """Stands in for update/launch.py (filled by the runner implementer); records every call."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.calls: list[tuple] = []
        self.killed: list[str] = []
        self.result = _check_result()
        self.check_error: Exception | None = None
        self.check_hangs = False
        self.accept_error: Exception | None = None
        self.accept_hangs = False
        self.start_error: Exception | None = None

    def _handle(self, mode, run_id):
        return launch.RunnerHandle(mode=mode, run_id=run_id, method="posix", args_file=self.tmp / "a.json",
                                   log_file=self.tmp / f"update-{run_id}.log", out_file=None, run_file=None)

    def start_check(self, paths, cfg, *, origin, job_id=None, run_id=None, foreground=False):
        self.calls.append(("start_check", origin, job_id, foreground))
        if self.start_error:
            raise self.start_error
        return self._handle("check", run_id or files.new_run_id())

    async def wait_for_check(self, handle, *, timeout_s=300, poll_s=0.5):
        self.calls.append(("wait_for_check", timeout_s))
        if self.check_hangs:
            await asyncio.sleep(3600)
        if self.check_error:
            raise self.check_error
        return self.result

    def kill_check(self, handle):
        self.killed.append(handle.run_id)

    def start_apply(self, paths, cfg, *, run_id, expected_commit, force, origin, job_id=None):
        self.calls.append(("start_apply", run_id, expected_commit, force, origin, job_id))
        if self.start_error:
            raise self.start_error
        return self._handle("apply", run_id)

    async def wait_for_accept(self, handle, *, timeout_s=15, poll_s=0.25):
        self.calls.append(("wait_for_accept", timeout_s))
        if self.accept_hangs:
            await asyncio.sleep(3600)
        if self.accept_error:
            raise self.accept_error
        return {"run_id": handle.run_id, "phase": "accepted"}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = FakeLaunch(tmp_path)
    for name in ("start_check", "wait_for_check", "kill_check", "start_apply", "wait_for_accept"):
        monkeypatch.setattr(launch, name, getattr(f, name))
    return f


def _write_run(home, run_id=RUN, **fields):
    files.ensure_update_dir(home)
    files.run_dir(home, run_id).mkdir(parents=True, exist_ok=True)
    run = {"v": 1, "run_id": run_id, "job_id": "job_0", "origin": "remote", "expected_commit": SHA2,
           "force": False, "phase": "building", "status": None, "from_commit": SHA, "to_commit": SHA2,
           "started_at": "2026-10-16T12:00:00Z", "updated_at": "2026-10-16T12:01:00Z", "finished_at": None,
           "error": None, "warnings": [], "log": str(files.apply_log_file(home, run_id)), "runner_pid": 1}
    run.update(fields)
    files.atomic_write_json(files.run_file(home, run_id), run)
    files.write_latest(home, run_id)
    return run


def _failed(sent):
    assert isinstance(sent[0], pm.JobAccepted), "update jobs are always accepted first"
    assert isinstance(sent[-1], pm.JobFailed), sent[-1]
    return sent[-1]


# --------------------------------------------------------------------------- gates


async def test_malformed_payloads_are_accepted_then_failed(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    for job_type, payload in (("update.check", {"x": 1}), ("update.apply", {"expected_commit": "abc1234"}),
                              ("update.apply", {"expected_commit": SHA.upper()}),
                              ("update.apply", {"expected_commit": SHA, "run_id": RUN}),
                              ("update.apply", {"expected_commit": "origin/main"}),
                              ("update.apply", {"expected_commit": SHA, "force": "yes"})):
        mgr, sent = _manager(home)
        await _dispatch(mgr, job_type, payload)
        f = _failed(sent)
        assert f.code == "update_invalid_request" and f.hint, (payload, f)
    assert fake.calls == [], "nothing may be started for a malformed request"


async def test_consent_is_read_live_for_every_job(home, fake, monkeypatch):
    _updatable(monkeypatch)
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.check", {})
    f = _failed(sent)
    assert f.code == "update_not_allowed" and f.error == "Remote updates are off on box."
    assert "homemesh update allow" in f.hint
    consent.allow(home)  # no restart, no reconnect
    sent.clear()
    await _dispatch(mgr, "update.check", {}, job_id="job_2")
    assert isinstance(sent[-1], pm.JobCompleted)
    consent.disallow(home)
    sent.clear()
    await _dispatch(mgr, "update.apply", {"expected_commit": SHA2}, job_id="job_3")
    assert _failed(sent).code == "update_not_allowed"
    assert [c[0] for c in fake.calls] == ["start_check", "wait_for_check"]


async def test_an_install_that_cannot_update_says_why_and_how(home, fake):
    consent.allow(home)
    info = record.build_info(home)  # this test run: an editable or pip install, never updatable remotely
    assert not info.can_update
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.apply", {"expected_commit": SHA2})
    f = _failed(sent)
    assert f.code == "update_not_supported" and f.error.startswith("box can't be updated remotely: ")
    assert (info.cannot_update_reason or info.reason).split(";")[0] in f.error
    assert "install" in f.hint and fake.calls == []


async def test_busy_while_a_runner_holds_the_update_lock(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    files.ensure_update_dir(home)
    lock = files.update_lock(home)
    assert lock.acquire()
    try:
        mgr, sent = _manager(home)
        await _dispatch(mgr, "update.check", {})
        f = _failed(sent)
        assert f.code == "update_busy" and "(checking for updates)" in f.error
        _write_run(home, phase="building")
        sent.clear()
        await _dispatch(mgr, "update.apply", {"expected_commit": SHA2})
        assert "(building the new version)" in _failed(sent).error
    finally:
        lock.release()
    assert fake.calls == []


async def test_checks_are_rate_limited(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.check", {})
    assert isinstance(sent[-1], pm.JobCompleted)
    sent.clear()
    await _dispatch(mgr, "update.check", {}, job_id="job_2")
    f = _failed(sent)
    assert f.code == "update_rate_limited" and f.error == "box checked less than 30 seconds ago."
    mgr._last_check_launch -= 31
    sent.clear()
    await _dispatch(mgr, "update.check", {}, job_id="job_3")
    assert isinstance(sent[-1], pm.JobCompleted)


# --------------------------------------------------------------------------- update.check


async def test_check_reports_the_live_consent_and_prunes_old_checks(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    pruned = []
    monkeypatch.setattr(files, "prune_checks", lambda paths, keep=5: pruned.append(paths) or [])
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.check", {}, job_id="job_c")
    done = sent[-1]
    assert isinstance(done, pm.JobCompleted) and done.job_id == "job_c"
    assert done.result["allowed"] is True  # the runner wrote False; the marker exists now
    assert done.result["upstream_commit"] == SHA2 and done.result["behind"] == 2 and done.result["can_update"]
    UpdateCheckResult.model_validate(done.result)
    assert fake.calls[0] == ("start_check", "remote", "job_c", False) and fake.calls[1] == ("wait_for_check", 300.0)
    assert pruned == [home] and fake.killed == []


async def test_a_check_the_runner_could_not_do_fails_with_its_code(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    issue = format_update_error("update_dirty_checkout", source_dir="/src/MyAi", files="a.py")
    fake.result = UpdateCheckResult(ok=False, error=issue)
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.check", {})
    f = _failed(sent)
    assert (f.code, f.error, f.hint) == (issue.code, issue.message, issue.hint)


async def test_a_runner_that_dies_fails_the_check_with_its_log_and_is_killed(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    i = format_update_error("update_launch_failed", detail="the runner exited with code 1")
    fake.check_error = launch.UpdateLaunchError(i.message, code=i.code, hint=i.hint,
                                                details={"log_tail": "Traceback …\nOSError: disk full"})
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.check", {})
    f = _failed(sent)
    assert f.code == "update_launch_failed" and f.error.startswith(i.message) and "disk full" in f.error
    assert len(f.error) <= 500 and f.hint == i.hint and len(fake.killed) == 1


async def test_cancelling_a_check_kills_the_runner(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    fake.check_hangs = True
    mgr, sent = _manager(home)
    await mgr.on_dispatch(pm.JobDispatch(job_id="job_x", job_type="update.check", priority="interactive",
                                         payload={}, timeout_s=330))
    task = mgr.jobs["job_x"]
    for _ in range(100):
        if any(c[0] == "wait_for_check" for c in fake.calls):
            break
        await asyncio.sleep(0.01)
    task.cancel()  # job cancel, disconnect or worker stopping
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert isinstance(sent[-1], pm.JobCancelled) and len(fake.killed) == 1 and "job_x" not in mgr.jobs


# --------------------------------------------------------------------------- update.apply


async def test_apply_starts_the_runner_with_a_run_id_of_its_own(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.apply", {"expected_commit": SHA2, "force": True}, job_id="job_a")
    done = sent[-1]
    assert isinstance(done, pm.JobCompleted) and done.result["state"] == "started"
    run_id = done.result["run_id"]
    assert is_run_id(run_id)
    assert fake.calls[0] == ("start_apply", run_id, SHA2, True, "remote", "job_a")
    assert fake.calls[1] == ("wait_for_accept", 15.0)


async def test_apply_refuses_a_commit_that_was_just_rolled_back(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    _write_run(home, status="rolled_back", to_commit=SHA2, phase="rolling_back")
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.apply", {"expected_commit": SHA2})
    f = _failed(sent)
    assert f.code == "update_rolled_back_commit" and f.error == "bbbbbbb failed on box before and was undone."
    assert "homemesh update" in f.hint and fake.calls == []
    sent.clear()
    await _dispatch(mgr, "update.apply", {"expected_commit": SHA3}, job_id="job_2")
    assert isinstance(sent[-1], pm.JobCompleted)


async def test_apply_launch_failures_keep_their_code(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    i = format_update_error("update_busy", computer="box", phase="checking for updates")
    fake.accept_error = launch.UpdateLaunchError(i.message, code=i.code, hint=i.hint)
    mgr, sent = _manager(home)
    await _dispatch(mgr, "update.apply", {"expected_commit": SHA2})
    assert (_failed(sent).code, _failed(sent).error) == ("update_busy", i.message)
    fake.accept_error = None
    fake.start_error = RuntimeError("unexpected")
    sent.clear()
    await _dispatch(mgr, "update.apply", {"expected_commit": SHA2}, job_id="job_2")
    f = _failed(sent)
    assert f.code == "update_launch_failed" and "RuntimeError: unexpected" in f.error


async def test_an_apply_ignores_cancel_but_a_check_obeys_it(home, fake, monkeypatch):
    _updatable(monkeypatch)
    consent.allow(home)
    fake.accept_hangs = fake.check_hangs = True
    agent = WorkerAgent(_cfg(), home, backends={"mock": MockBackend("mock")})
    sent = []

    async def send(msg):
        sent.append(msg)

    agent.send = send  # type: ignore[method-assign]
    agent.updates.send = send
    for job_id, job_type, payload in (("job_a", "update.apply", {"expected_commit": SHA2}),
                                      ("job_c", "update.check", {})):
        await agent._on_dispatch(pm.JobDispatch(job_id=job_id, job_type=job_type, priority="interactive",
                                                payload=payload, timeout_s=60))
    for _ in range(200):
        if {c[0] for c in fake.calls} >= {"wait_for_accept", "wait_for_check"}:
            break
        await asyncio.sleep(0.01)
    apply_task, check_task = agent.jobs["job_a"], agent.jobs["job_c"]
    await agent._on_message(pm.dump(pm.JobCancel(job_id="job_a")))
    await agent._on_message(pm.dump(pm.JobCancel(job_id="job_c")))
    with contextlib.suppress(asyncio.CancelledError):
        await check_task
    await asyncio.sleep(0.05)
    assert not apply_task.done(), "the update helper runs detached; its job can't be cancelled"
    assert any(isinstance(m, pm.JobCancelled) and m.job_id == "job_c" for m in sent) and len(fake.killed) == 1
    await agent._cancel_all_jobs("test over")  # a disconnect still ends the wait
    assert apply_task.done() and agent.jobs == {} and agent.updates.job_ids == set()


# --------------------------------------------------------------------------- watch loop


async def test_watch_finalizes_a_run_whose_runner_is_gone(home, monkeypatch):
    _updatable(monkeypatch, commit=SHA)
    _write_run(home, phase="switching")  # runner killed (logoff, power loss) while switching
    mgr, _ = _manager(home)
    await mgr.refresh()
    run = files.read_run(home, RUN)
    assert run["status"] == "interrupted" and run["error"]["code"] == "update_interrupted"
    assert run["installed_commit"] == SHA and "while switching versions" in run["error"]["message"]
    assert mgr.report["status"] == "interrupted" and mgr.report["installed_commit"] == SHA
    assert mgr.report["run_id"] == RUN and not mgr.draining


async def test_watch_leaves_a_live_run_alone(home, monkeypatch):
    _updatable(monkeypatch)
    _write_run(home, phase="building")
    lock = files.update_lock(home)
    assert lock.acquire()
    try:
        mgr, _ = _manager(home)
        await mgr.refresh()
        assert files.read_run(home, RUN)["status"] is None
        assert mgr.report["phase"] == "building" and mgr.report["status"] is None and not mgr.draining
    finally:
        lock.release()


async def test_draining_rejects_new_jobs_and_lists_the_running_ones(home, monkeypatch):
    _updatable(monkeypatch)
    cfg = _cfg()
    agent = WorkerAgent(cfg, home, backends={"mock": MockBackend("mock")})
    sent = []

    async def send(msg):
        sent.append(msg)

    agent.send = send  # type: ignore[method-assign]
    agent.updates.send = send
    chat = asyncio.create_task(asyncio.sleep(3600))
    upd = asyncio.create_task(asyncio.sleep(3600))
    agent.jobs.update({"job_chat": chat, "job_upd": upd})
    agent.job_types.update({"job_chat": "chat", "job_upd": "update.check"})
    agent.updates.job_ids.add("job_upd")
    assert agent.updates.running_count() == 1  # update jobs don't use capacity
    _write_run(home, phase="draining")
    lock = files.update_lock(home)
    assert lock.acquire()
    try:
        await agent.updates.refresh()
        assert agent.updates.draining
        drain = files.read_drain_file(home, RUN)
        assert drain["v"] == 1 and drain["pid"] == os.getpid()
        assert drain["running"] == [{"job_id": "job_chat", "type": "chat"}]
        await agent._on_dispatch(pm.JobDispatch(job_id="job_new", job_type="chat", priority="interactive",
                                                payload={}, timeout_s=60))
        assert isinstance(sent[-1], pm.JobRejected) and sent[-1].code == "updating"
        assert sent[-1].reason == UPDATING_REASON
        # Still restarting after the drain: keep rejecting, but drain.json is only written while draining.
        _write_run(home, phase="stopping", committed=True)
        files.drain_file(home, RUN).unlink()
        await agent.updates.refresh()
        assert agent.updates.draining and not files.drain_file(home, RUN).exists()
        _write_run(home, phase="draining", status="failed",
                   error=format_update_error("update_jobs_running", n=1, types="chat").model_dump())
        await agent.updates.refresh()
        assert not agent.updates.draining
    finally:
        lock.release()
        chat.cancel()
        upd.cancel()
    agent.policy.mode = "paused"
    sent.clear()
    await agent._on_dispatch(pm.JobDispatch(job_id="job_u", job_type="update.check", priority="interactive",
                                            payload={}, timeout_s=330))
    assert isinstance(sent[0], pm.JobAccepted), "update jobs are accepted while paused"
    await asyncio.gather(*[t for t in agent.jobs.values() if t is not chat and t is not upd],
                         return_exceptions=True)
    assert sent[-1].code == "update_not_allowed"


def test_connection_failures_map_to_agent_states():
    def closed(code):
        return ConnectionClosed(Close(code, "x"), None)

    response = SimpleNamespace(status_code=403, headers={}, body=b"")
    cases = [
        (FatalWorkerError("revoked"), "rejected"),
        (closed(1002), "rejected"),  # the hub could not read our hello
        (closed(pm.CLOSE_PROTOCOL_INCOMPATIBLE), "rejected"),
        (closed(1001), "unreachable"),
        (closed(pm.CLOSE_COORDINATOR_SHUTDOWN), "unreachable"),
        (InvalidStatus(response), "rejected"),
        (InvalidStatus(SimpleNamespace(status_code=502, headers={}, body=b"")), "unreachable"),
        (ssl.SSLCertVerificationError("bad cert"), "rejected"),
        (ssl.SSLError("eof"), "unreachable"),
        (ConnectionRefusedError(), "unreachable"),
        (TimeoutError(), "unreachable"),
        (ValueError("bug in the new version"), "rejected"),
    ]
    for exc, state in cases:
        assert WorkerAgent.agent_state_for(exc) == state, exc


# --------------------------------------------------------------------------- hello, heartbeat, agent.json


@pytest.mark.slow
async def test_hello_and_heartbeats_carry_build_and_report_and_agent_json_follows(tmp_path):
    from harness import Harness, paths_for, wait_for
    from homemesh.worker.credentials import WorkerCredentials
    from homemesh.worker.enroll import join

    h = Harness(tmp_path)
    await h.start(roles=["coordinator"])
    try:
        await h.owner_client()
        wpaths = paths_for(tmp_path / "w")
        wpaths.ensure()
        creds = await asyncio.to_thread(join, h.base_url, await h.pairing_code(), wpaths, node_name="w1")
        run = _write_run(wpaths, status="ok", phase="cleanup", finished_at="2026-10-16T12:05:00Z")
        cfg = h.make_config(["worker"], name="w1")
        cfg.worker.coordinator_url = h.base_url
        agent = WorkerAgent(cfg, wpaths, backends={"mock": MockBackend("mock")})
        seen = []
        real_send = agent.send

        async def spy(msg):
            seen.append(msg)
            await real_send(msg)

        agent.send = spy  # type: ignore[method-assign]
        agent.updates.send = spy
        task = asyncio.create_task(agent.run())
        try:
            await agent.wait_connected(15)
            hello = next(m for m in seen if isinstance(m, pm.Hello))
            info = record.build_info(wpaths)
            assert hello.build == info.to_wire() and hello.build["v"] == 1
            assert hello.update["run_id"] == RUN and hello.update["status"] == "ok"
            assert hello.update["to_commit"] == run["to_commit"]
            await wait_for(lambda: any(isinstance(m, pm.Heartbeat) for m in seen), 10, 0.05, "a heartbeat")
            hb = next(m for m in seen if isinstance(m, pm.Heartbeat))
            assert hb.update == hello.update
            agent_file = files.read_agent_file(wpaths)
            assert agent_file["state"] == "connected" and agent_file["pid"] == os.getpid()
            assert agent_file["build_commit"] == info.commit and agent_file["v"] == 1
        finally:
            await agent.stop()
            await asyncio.wait_for(task, 10)

        # A worker whose identity the hub refuses writes "rejected": the runner rolls an update back on that.
        bad = WorkerCredentials(coordinator_url=h.base_url, node_id=creds.node_id, node_secret="wrong")
        agent = WorkerAgent(cfg, wpaths, credentials=bad, backends={"mock": MockBackend("mock")})
        await asyncio.wait_for(agent.run(), 15)
        agent_file = files.read_agent_file(wpaths)
        assert agent_file["state"] == "rejected" and "Invalid node credentials" in agent_file["detail"]
    finally:
        await h.stop()


async def test_an_unreachable_hub_is_recorded_as_unreachable(home, monkeypatch):
    from homemesh.worker.credentials import WorkerCredentials

    cfg = _cfg()
    cfg.worker.reconnect_max_attempts = 1
    creds = WorkerCredentials(coordinator_url="http://127.0.0.1:9", node_id="node_x", node_secret="s")
    agent = WorkerAgent(cfg, home, credentials=creds, backends={"mock": MockBackend("mock")})
    await asyncio.wait_for(agent.run(), 30)
    data = json.loads(files.agent_file(home).read_text(encoding="utf-8"))
    assert data["state"] == "unreachable" and data["pid"] == os.getpid()


def test_launch_errors_are_homemesh_errors():
    # The CLI relies on this: an UpdateLaunchError is printed like every other HomeMeshError.
    assert issubclass(launch.UpdateLaunchError, HomeMeshError)
