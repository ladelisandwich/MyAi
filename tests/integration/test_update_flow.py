"""Remote updates end to end with real worker agents (design 18.2): a real coordinator, real
``WorkerAgent``/``UpdateManager`` over real sockets, the real launcher, and for checks the real runner against
a temporary git repository (bare origin + the install's checkout, one commit behind).

Applies run ``tests/update_stub_runner.py`` instead of the runner (``HOMEMESH_UPDATE_RUNNER`` with
``HOMEMESH_TESTING=1``): it keeps the runner's file contracts with the runner's own lock and atomic writes but
builds, stops and starts nothing, so the test plays HomeMesh being stopped and started on the new version.
What these tests replace: the install record is read from a fake ``<prefix>/venvs/<id>`` (this process runs
from the developer's venv, not from an installed one), the launch method is the plain background process
(never this machine's systemd), and the runner arguments name the fake install's package folder.

``test_update_hub_flow.py`` covers the hub's rules with scripted computers; ``test_update_e2e.py`` the real
runner's apply with real venvs and pip.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

from harness import Harness, WorkerHandle, read_sse, wait_for
from homemesh.protocol.update import format_update_error, is_run_id
from homemesh.update import consent, files, launch, record, runner
from update_support import Layout, Repo

pytestmark = pytest.mark.slow

SUBJECT = "CI: Łódź 修复 →"
NEW_TS = "20260202000000"
STUB = Path(__file__).resolve().parents[1] / "update_stub_runner.py"
STUB_DIR_ENV = "HOMEMESH_TEST_STUB_DIR"


class Box:
    """The computer that gets updated: its HomeMesh home, its install (``<prefix>/venv`` → ``venvs/<id>``
    with an install record), its checkout one commit behind origin, and the stub runner's controls."""

    def __init__(self, tmp: Path) -> None:
        self.repo = Repo(tmp / "repo")
        self.layout = Layout(tmp / "alpha", self.repo)  # also runs `homemesh update allow` there
        self.layout.paths.worker_credentials_file.unlink()  # enrolled for real below
        self.paths = self.layout.paths
        self.old = self.layout.old_commit
        self.target = self.repo.commit({"README.md": "B\n"}, SUBJECT)  # pushed to origin
        self.installed: Path = self.layout.old_venv  # the venv "HomeMesh" runs from right now
        self.stub_dir = tmp / "stub"
        self.stub_dir.mkdir()

    def plan(self, **plan: Any) -> None:
        for p in self.stub_dir.glob("release-*"):
            p.unlink()
        (self.stub_dir / "plan.json").write_text(json.dumps({"runner": str(Path(runner.__file__).resolve()),
                                                             **plan}), encoding="utf-8")

    def release(self, phase: str) -> None:
        (self.stub_dir / f"release-{phase}").write_text("go", encoding="utf-8")

    def switch_to_new_version(self) -> None:
        """What the runner's switch does for the next start: HomeMesh runs from a venv built from the target."""
        self.installed = self.layout.make_venv(self.target, ts=NEW_TS)

    def run(self, run_id: str) -> dict[str, Any]:
        return files.read_run(self.paths, run_id) or {}

    def stubs(self) -> list[psutil.Process]:
        out = []
        for p in psutil.Process().children(recursive=True):
            with contextlib.suppress(psutil.Error):
                if str(STUB) in " ".join(p.cmdline()):
                    out.append(p)
        return out


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path / "mesh")
    await harness.start(roles=["coordinator"], heartbeat_timeout_s=4.0)
    await harness.owner_client()
    yield harness
    await harness.stop()


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Box(tmp_path)
    real_load, real_build_info, real_args = record.load, record.build_info, files.build_runner_args

    def load(venv: Any = None) -> record.InstallRecord | None:
        if venv is None or Path(venv) == b.layout.stable:
            return real_load(b.installed)  # this process "runs from" the install's stable venv
        return real_load(venv)

    def build_info(paths: Any = None, **kw: Any) -> Any:
        if paths == b.paths:
            kw.setdefault("prefix", b.layout.stable)
        return real_build_info(paths, **kw)

    def build_runner_args(paths: Any, **kw: Any) -> Any:
        if paths == b.paths:
            kw.setdefault("current_venv", b.installed)
            kw.setdefault("current_package_dir", b.layout.pkg)
        return real_args(paths, **kw)

    monkeypatch.setattr(record, "load", load)
    monkeypatch.setattr(record, "build_info", build_info)
    monkeypatch.setattr(files, "build_runner_args", build_runner_args)
    monkeypatch.setattr(launch, "service_kind", lambda paths: "windows" if sys.platform == "win32" else "posix")
    monkeypatch.setattr(launch, "inside_homemesh_unit", lambda: False)
    monkeypatch.delenv("HOMEMESH_HOME", raising=False)
    monkeypatch.setenv(launch.RUNNER_ENV, str(STUB))
    monkeypatch.setenv(launch.TESTING_ENV, "1")
    monkeypatch.setenv(STUB_DIR_ENV, str(b.stub_dir))
    b.plan(apply="ok")
    yield b
    for phase in ("accept", "accepted", "prechecks", "building", "draining", "stopping", "verifying"):
        b.release(phase)
    _, alive = psutil.wait_procs(b.stubs(), timeout=10)
    for p in alive:
        with contextlib.suppress(psutil.Error):
            p.kill()
    b.layout.cleanup()


# --------------------------------------------------------------------------- helpers

async def node(h: Harness, node_id: str) -> dict[str, Any]:
    r = await h.client.get(f"/api/v1/nodes/{node_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def until(h: Harness, node_id: str, predicate: Any, what: str, timeout: float = 20) -> dict[str, Any]:
    async def check() -> dict[str, Any] | None:
        n = await node(h, node_id)
        return n if predicate(n) else None

    return await wait_for(check, timeout, 0.1, what)


def row(n: dict[str, Any]) -> dict[str, Any]:
    return n.get("update") or {}


async def check_ok(h: Harness, box: Box, worker: WorkerHandle) -> dict[str, Any]:
    """A real check on the computer: the hub stores what the runner found."""
    r = await h.client.post(f"/api/v1/nodes/{worker.node_id}/update/check")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    n = await until(h, worker.node_id, lambda n: (n["update_check"] or {}).get("job_id") == job_id
                    and n["update_check_pending"] is None, "the check result", timeout=60)
    assert "result" in n["update_check"], n["update_check"]
    return n["update_check"]["result"]


async def check_error(h: Harness, worker: WorkerHandle) -> dict[str, Any]:
    r = await h.client.post(f"/api/v1/nodes/{worker.node_id}/update/check")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    n = await until(h, worker.node_id, lambda n: (n["update_check"] or {}).get("job_id") == job_id,
                    "the check outcome", timeout=30)
    assert "error" in n["update_check"], n["update_check"]
    return n["update_check"]["error"]


async def apply(h: Harness, box: Box, worker: WorkerHandle) -> str:
    r = await h.client.post(f"/api/v1/nodes/{worker.node_id}/update/apply",
                            json={"expected_commit": box.target, "force": False})
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "requested"
    return r.json()["update_id"]


async def finished_audits(h: Harness, update_id: str) -> list[dict[str, Any]]:
    rows = (await h.client.get("/api/v1/system/audit?action=node.update_finished")).json()
    return [a for a in rows if (a.get("details_json") or {}).get("update_id") == update_id]


async def chat_route(h: Harness, **extra: Any) -> dict[str, Any]:
    conv = (await h.client.post("/api/v1/conversations", json={})).json()
    if extra:
        await h.client.patch(f"/api/v1/conversations/{conv['id']}", json=extra)
    async with h.client.stream("POST", f"/api/v1/conversations/{conv['id']}/messages",
                               json={"content": "hi"}) as resp:
        assert resp.status_code == 200, await resp.aread()
        events = dict(await read_sse(resp))
    assert "done" in events, events
    return events["route"]


# --------------------------------------------------------------------------- tests

async def test_check_apply_restart_and_reconnect(h, box):
    alpha = await h.add_worker("alpha", paths=box.paths)
    beta = await h.add_worker("beta")
    c = h.client
    n = await until(h, alpha.node_id, lambda n: (n["build"] or {}).get("commit") == box.old, "alpha's build")
    build = n["build"]
    assert build["source"] == "installer" and build["can_update"] and build["update_allowed"]
    assert build["source_dir"] == str(box.repo.src) and build["logs_dir"] == str(box.paths.logs_dir)
    assert n["update"] is None and n["update_live"] is None

    # ---- check: the worker launches the real check runner, which fetches from the checkout's own remote
    result = await check_ok(h, box, alpha)
    assert result["ok"] and result["allowed"] and result["can_update"] and result["problems"] == []
    assert result["installed_commit"] == box.old and result["head_commit"] == box.old
    assert result["upstream_commit"] == box.target and result["behind"] == 1 and result["ahead"] == 0
    assert result["branch"] == "main" and result["remote"] == "origin" and result["upstream_ref"] == "origin/main"
    assert [cm["subject"] for cm in result["commits"]] == [SUBJECT]
    checks = sorted(p.name for p in files.checks_dir(box.paths).iterdir())
    assert len(checks) == 2 and checks[0].endswith(".args.json") and checks[1].endswith(".json"), checks
    check_id = checks[1][:-len(".json")]
    assert files.check_log_file(box.paths, check_id).is_file()
    assert not files.update_lock_held(box.paths)  # the check runner is gone

    # ---- apply: the launcher starts the (stub) runner detached; the job ends once it accepted the run
    box.plan(apply="ok", verify="agent", hold=["draining"])
    update_id = await apply(h, box, alpha)
    n = await until(h, alpha.node_id, lambda n: row(n).get("state") == "running"
                    and row(n).get("phase") == "draining", "the update draining alpha")
    run_id = row(n)["run_id"]
    assert is_run_id(run_id) and n["update_live"]["run_id"] == run_id and n["update_live"]["job_id"] == update_id
    assert row(n)["from_commit"] == box.old and row(n)["expected_commit"] == box.target
    assert (await c.get(f"/api/v1/jobs/{update_id}")).json()["result_json"] == {"run_id": run_id}
    run = box.run(run_id)
    assert run["job_id"] == update_id and run["origin"] == "remote" and run["expected_commit"] == box.target

    # the agent stops taking work and tells the runner, in drain.json, that nothing runs any more
    assert alpha.agent.updates.draining
    drain = await wait_for(lambda: files.read_drain_file(box.paths, run_id), 10, 0.1, "drain.json")
    assert drain["running"] == [] and drain["pid"] == os.getpid()
    # the scheduler skips the draining computer: the chat goes to beta
    route = await chat_route(h)
    assert route["node_id"] == beta.node_id
    assert {"node_id": alpha.node_id, "code": "updating"} in [
        {k: r[k] for k in ("node_id", "code")} for r in route["decision"]["rejections"]]

    # ---- the runner stops HomeMesh, switches and starts the new version, then waits for its agent
    box.release("draining")
    await wait_for(lambda: box.run(run_id).get("phase") == "verifying", 20, 0.1, "phase verifying")
    await alpha.stop()
    await h.wait_node_online(alpha.node_id, online=False)
    box.switch_to_new_version()
    alpha = await h.restart_worker(alpha, stop=False)
    n = await until(h, alpha.node_id, lambda n: row(n).get("state") == "ok", "the update finished ok", timeout=30)
    assert row(n)["installed_commit"] == box.target and row(n)["finished_at"] and row(n)["error"] is None
    assert row(n)["run_id"] == run_id and row(n)["log"] == str(files.apply_log_file(box.paths, run_id))
    assert n["build"]["commit"] == box.target and n["update_live"]["status"] == "ok"
    agent = files.read_agent_file(box.paths)
    assert agent["state"] == "connected" and agent["build_commit"] == box.target and agent["pid"] == os.getpid()
    assert box.run(run_id)["status"] == "ok"
    await wait_for(lambda: not files.update_lock_held(box.paths), 10, 0.1, "the runner to exit")
    audits = await finished_audits(h, update_id)
    assert len(audits) == 1 and audits[0]["outcome"] == "success"
    assert not alpha.agent.updates.draining

    # ---- a reconnect repeats the final report in the hello: nothing changes, nothing is recorded twice
    alpha = await h.restart_worker(alpha)
    n = await until(h, alpha.node_id, lambda n: n["online"] and (n["update_live"] or {}).get("status") == "ok",
                    "alpha back with its last report")
    assert row(n)["state"] == "ok" and row(n)["finished_at"]
    assert len(await finished_audits(h, update_id)) == 1
    route = await chat_route(h, node_id=alpha.node_id)  # alpha takes work again
    assert route["node_id"] == alpha.node_id


async def test_not_started_then_an_outcome_first_heard_in_the_hello(h, box):
    alpha = await h.add_worker("alpha", paths=box.paths)
    await check_ok(h, box, alpha)

    # ---- the computer goes away while the helper is starting; the helper ends without starting the run
    box.plan(apply="exit_before_accept")
    update_id = await apply(h, box, alpha)
    await wait_for(lambda: alpha.agent.updates.job_ids, 10, 0.05, "the worker to take the apply job")
    await alpha.crash()
    n = await until(h, alpha.node_id, lambda n: not n["online"], "alpha offline")
    assert row(n)["state"] == "requested"  # unknown: the helper may have started
    job = (await h.client.get(f"/api/v1/jobs/{update_id}")).json()
    assert job["status"] == "failed" and job["error_code"] == "worker_disconnected"
    box.release("accept")
    await wait_for(lambda: not box.stubs() and not files.update_lock_held(box.paths), 15, 0.1, "the helper to exit")
    alpha = await h.restart_worker(alpha, stop=False)  # its hello reports no run
    n = await until(h, alpha.node_id, lambda n: row(n).get("state") == "not_started", "not started")
    issue = format_update_error("update_not_started", computer="alpha")
    assert row(n)["error"] == {"code": issue.code, "message": issue.message, "hint": issue.hint}
    assert files.read_latest_run(box.paths) is None

    # ---- the next update finishes while HomeMesh is down: the hub learns the outcome from the hello
    box.plan(apply="ok", verify="gate", hold=["verifying"])
    update_id = await apply(h, box, alpha)  # the stored check still matches
    n = await until(h, alpha.node_id, lambda n: row(n).get("id") == update_id and row(n).get("phase") == "verifying",
                    "phase verifying")
    run_id = row(n)["run_id"]
    await alpha.stop()
    await h.wait_node_online(alpha.node_id, online=False)
    box.switch_to_new_version()
    box.release("verifying")
    await wait_for(lambda: box.run(run_id).get("status") == "ok" and not files.update_lock_held(box.paths), 15, 0.1,
                   "the run to end while alpha is down")
    n = await node(h, alpha.node_id)
    assert row(n)["state"] == "running" and row(n)["phase"] == "verifying"  # the hub hasn't heard yet
    alpha = await h.restart_worker(alpha, stop=False)
    n = await until(h, alpha.node_id, lambda n: row(n).get("state") == "ok", "the outcome from the hello")
    assert row(n)["installed_commit"] == box.target and n["build"]["commit"] == box.target
    assert len(await finished_audits(h, update_id)) == 1


async def test_refusals_take_effect_at_once_without_a_restart(h, box):
    alpha = await h.add_worker("alpha", paths=box.paths)
    assert (await check_ok(h, box, alpha))["allowed"]
    worker_id = alpha.node_id

    # the update helper ends before it recorded a run: the worker says so, with the helper's log
    box.plan(apply="exit_before_accept")
    box.release("accept")
    update_id = await apply(h, box, alpha)
    n = await until(h, worker_id, lambda n: row(n).get("id") == update_id and row(n)["state"] == "failed",
                    "the launch failure")
    err = row(n)["error"]
    assert err["code"] == "update_launch_failed" and "exit code 1" in err["message"], err
    assert "exiting without a run" in err["message"]  # the redacted log tail travels with the message
    assert str(box.paths.logs_dir) in err["hint"]
    assert files.read_latest_run(box.paths) is None

    # `homemesh update disallow` on that computer: the next job is refused by the running worker
    consent.disallow(box.paths)
    update_id = await apply(h, box, alpha)
    n = await until(h, worker_id, lambda n: row(n).get("id") == update_id and row(n)["state"] == "failed",
                    "the apply refused")
    issue = format_update_error("update_not_allowed", computer="alpha")
    assert row(n)["id"] == update_id and row(n)["error"] == issue.model_dump()
    job = (await h.client.get(f"/api/v1/jobs/{update_id}")).json()
    assert job["status"] == "failed" and job["error_code"] == "update_not_allowed"
    assert files.read_latest_run(box.paths) is None and not box.stubs()  # nothing was started
    assert await check_error(h, alpha) == issue.model_dump()

    # allowed again, but another update helper holds the update lock (here: this test)
    consent.allow(box.paths)
    lock = files.update_lock(box.paths)
    assert lock.acquire()
    try:
        err = await check_error(h, alpha)
    finally:
        lock.release()
    assert err == format_update_error("update_busy", computer="alpha", phase="checking for updates").model_dump()
    # and a check less than 30 s after the last one that ran is refused too
    assert (await check_error(h, alpha))["code"] == "update_rate_limited"
    assert alpha.agent.updates.job_ids == set()
