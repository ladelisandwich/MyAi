"""Coordinator side of remote updates (UpdateService, job plumbing, node reports): every rule of the hub, with
a real database, JobService and NodeManager and in-memory worker connections (no sockets)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import ConflictError, NotFoundError, ValidationFailed
from homemesh.common.paths import AppPaths
from homemesh.common.security import hash_token
from homemesh.common.timeutil import iso, utcnow
from homemesh.coordinator import update_service as us_mod
from homemesh.coordinator.api.jobs import redact_update_job
from homemesh.coordinator.db.database import Database
from homemesh.coordinator.db.migrate import migrate
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.events import EventBus
from homemesh.coordinator.jobs import TRANSIENT_REJECTIONS, JobService
from homemesh.coordinator.nodes import NodeConnection, NodeManager, update_view
from homemesh.coordinator.update_service import UpdateService
from homemesh.protocol import messages as pm
from homemesh.protocol.hardware import CPUInfo, HardwareReport, MemoryInfo, OSInfo, Telemetry
from homemesh.protocol.jobs import IDEMPOTENT_JOB_TYPES
from homemesh.protocol.update import (
    APPLY_JOB_TIMEOUT_S,
    CHECK_JOB_TIMEOUT_S,
    DISMISS_AFTER_S,
    HINT_MAX,
    MESSAGE_MAX,
    UPDATE_ERRORS,
    BuildInfo,
    UpdateCheckResult,
    UpdateRunReport,
    format_update_error,
)
from homemesh.scheduler.policy import TaskRequirements, schedule

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40
RUN = "u20261016120000-0123abcd"
RUN2 = "u20261016130000-89abcdef"
OWNER = "usr_owner"


# --------------------------------------------------------------------------- fixtures and builders

class FakeWS:
    """Just enough of a starlette WebSocket for NodeManager: records what the hub sends."""

    def __init__(self, headers: dict[str, str] | None = None, *, fail_send: bool = False) -> None:
        self.headers = headers or {}
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[Any] = []
        self.closed: tuple[int, str] | None = None
        self.fail_send = fail_send

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise WebSocketDisconnect(1000)
        return item

    async def send_text(self, text: str) -> None:
        if self.fail_send:
            raise ConnectionError("socket gone")
        self.sent.append(pm.parse_coordinator_message(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    def dispatched(self, job_type: str) -> list[pm.JobDispatch]:
        return [m for m in self.sent if isinstance(m, pm.JobDispatch) and m.job_type == job_type]


@dataclass
class Env:
    repos: Repositories
    nodes: NodeManager
    jobs: JobService
    updates: UpdateService
    events: list[tuple[str, dict[str, Any]]]
    paths: AppPaths


@pytest.fixture
async def env(tmp_path):
    cfg = HomeMeshConfig()
    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    db = Database(tmp_path / "hm.sqlite3")
    await db.open()
    await db.run(lambda conn: migrate(conn))
    repos = Repositories.create(db)
    bus = EventBus()
    events: list[tuple[str, dict[str, Any]]] = []
    publish = bus.publish

    def spy(event_type: str, data: dict[str, Any] | None = None, **kw: Any) -> None:
        events.append((event_type, data or {}))
        publish(event_type, data, **kw)

    bus.publish = spy  # type: ignore[method-assign]
    nodes = NodeManager(cfg, paths, repos, bus)
    jobs = JobService(cfg, repos, nodes, bus)
    updates = UpdateService(repos, nodes, jobs, bus)
    yield Env(repos, nodes, jobs, updates, events, paths)
    await jobs.stop()
    await db.close()


def build(**kw: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"version": "0.3.0", "commit": SHA_A, "branch": "main", "source": "installer",
                              "os": "linux", "can_update": True, "update_allowed": True,
                              "source_dir": "/home/me/MyAi", "logs_dir": "/home/me/.local/share/homemesh/logs"}
    fields.update(kw)
    return BuildInfo(**fields).model_dump(mode="json")


def check_result(upstream: str = SHA_B, **kw: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"ok": True, "checked_at": "2026-10-16T12:00:00Z", "allowed": True,
                              "installed_commit": SHA_A, "upstream_commit": upstream, "behind": 2, "ahead": 0,
                              "branch": "main", "remote": "origin", "upstream_ref": "origin/main",
                              "commits": [{"sha": upstream, "subject": "Fix things"}], "can_update": True}
    fields.update(kw)
    return fields


def report(job_id: str | None, phase: str = "building", status: str | None = None, **kw: Any) -> UpdateRunReport:
    fields: dict[str, Any] = {"run_id": RUN, "job_id": job_id, "origin": "remote" if job_id else "local",
                              "phase": phase, "status": status, "from_commit": SHA_A, "to_commit": SHA_B,
                              "started_at": "2026-10-16T12:00:00Z", "updated_at": "2026-10-16T12:01:00Z",
                              "log": "/home/me/.local/share/homemesh/logs/update-" + kw.get("run_id", RUN) + ".log"}
    fields.update(kw)
    return UpdateRunReport.model_validate(fields)


def hello(node_id: str, name: str, **kw: Any) -> pm.Hello:
    return pm.Hello(node_id=node_id, node_name=name, software_version="0.3.0", protocol_versions=[1],
                    hardware=HardwareReport(os=OSInfo(system="Linux"), cpu=CPUInfo(), memory=MemoryInfo()),
                    backends=[], models=[], task_types=kw.pop("task_types", ["tool.exec"]), mode="balanced",
                    limits=pm.NodeLimits(max_concurrent_jobs=kw.pop("max_jobs", 1)), **kw)


async def add_node(env: Env, name: str, *, b: dict[str, Any] | None = None, checked: dict[str, Any] | None = None,
                   online: bool = True, os_name: str = "Linux", mode: str = "balanced",
                   live: UpdateRunReport | None = None, fail_send: bool = False, max_jobs: int = 1) -> str:
    row = await env.repos.nodes.create(name)
    node_id = row["id"]
    await env.repos.nodes.on_hello(node_id, name=name, os_name=os_name, arch="x86_64", hostname=name,
                                   software_version="0.3.0", protocol_version=1, hardware={}, limits={},
                                   task_types=["tool.exec"], allowed_roots=[], allowed_tools=[], backends=[],
                                   mode=mode)
    await env.repos.nodes.set_build(node_id, b)
    if checked is not None:
        await env.repos.nodes.set_update_check(node_id, {"job_id": "job_old", "checked_at": "2026-10-16T11:00:00Z",
                                                         "result": checked})
    if online:
        conn = NodeConnection(node_id=node_id, name=name, ws=FakeWS(fail_send=fail_send),  # type: ignore[arg-type]
                              protocol_version=1, hello=hello(node_id, name, max_jobs=max_jobs))
        conn.update_live = live
        env.nodes.connections[node_id] = conn
    return node_id


def ws(env: Env, node_id: str) -> FakeWS:
    return env.nodes.connections[node_id].ws  # type: ignore[return-value]


async def audit(env: Env, action: str) -> list[dict[str, Any]]:
    return [a for a in await env.repos.audit.list(500, "node.update") if a["action"] == action]


async def apply_job(env: Env, node_id: str) -> str:
    """Start an update on a node that has a valid check; returns the update (= job) id."""
    out = await env.updates.apply(node_id, SHA_B, False, OWNER)
    return out["update_id"]


# --------------------------------------------------------------------------- job plumbing

async def test_update_jobs_timeouts_not_idempotent_and_never_requeued(env):
    assert env.jobs.timeout_for("update.check") == CHECK_JOB_TIMEOUT_S == 330
    assert env.jobs.timeout_for("update.apply") == APPLY_JOB_TIMEOUT_S == 60
    assert not {"update.check", "update.apply"} & IDEMPOTENT_JOB_TYPES
    assert "updating" in TRANSIENT_REJECTIONS
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    (dispatch,) = ws(env, node_id).dispatched("update.apply")
    assert dispatch.timeout_s == 60 and dispatch.payload == {"expected_commit": SHA_B, "force": False}
    # a coordinator restart interrupts the job instead of re-queueing it; the update row is left to the reports
    await env.jobs.recover()
    job = await env.repos.jobs.get(update_id)
    assert job["status"] == "interrupted" and job["error_code"] == "coordinator_restarted"
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "requested" and row["active"]


async def test_cancel_refuses_apply_but_allows_check(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    with pytest.raises(ConflictError) as exc:
        await env.jobs.cancel(update_id, OWNER)
    issue = format_update_error("update_not_cancellable")
    assert exc.value.code == "update_not_cancellable" and exc.value.message == issue.message
    assert exc.value.hint == issue.hint and exc.value.status_code == 409
    other = await add_node(env, "laptop", b=build())
    job_id = (await env.updates.check(other, OWNER))["job_id"]
    assert (await env.jobs.cancel(job_id, OWNER))["status"] == "cancelling"
    assert any(isinstance(m, pm.JobCancel) and m.job_id == job_id for m in ws(env, other).sent)


async def test_update_jobs_dont_use_a_concurrency_slot(env):
    node_id = await add_node(env, "pc", b=build(), max_jobs=1)
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    conn = env.nodes.connections[node_id]
    assert job_id in conn.assigned_jobs and job_id in conn.control_jobs
    (snap,) = await env.nodes.snapshots()
    assert snap.running_jobs == 0 and snap.max_concurrent_jobs == 1
    decision = schedule(TaskRequirements("tool.exec", "interactive", needs_model=False), [snap])
    assert decision.chosen is not None and decision.chosen.node_id == node_id
    # still listed as running on the node, and released when it ends
    row = await env.repos.nodes.get(node_id)
    summary = env.nodes.node_summary(row)
    assert summary["running_jobs"] == [job_id]
    # without the lookups the nodes API does, the update row isn't guessed as "none"
    assert "update" not in summary and "update_check_pending" not in summary
    assert env.nodes.node_summary(row, update_row=None, update_check_pending=job_id)["update_check_pending"] == job_id
    await env.jobs.on_worker_message(node_id, pm.JobAccepted(job_id=job_id))
    await env.jobs.on_worker_message(node_id, pm.JobCompleted(job_id=job_id, result=check_result()))
    assert job_id not in conn.assigned_jobs and job_id not in conn.control_jobs


async def test_paused_node_still_gets_update_jobs_directly(env):
    node_id = await add_node(env, "pc", b=build(), mode="paused")
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    (dispatch,) = ws(env, node_id).dispatched("update.check")
    assert dispatch.job_id == job_id and dispatch.payload == {} and dispatch.timeout_s == 330
    job = await env.repos.jobs.get(job_id)
    assert job["status"] == "dispatched" and job["requested_node_id"] == node_id and job["max_attempts"] == 1


# --------------------------------------------------------------------------- check

async def test_check_rules(env):
    with pytest.raises(NotFoundError):
        await env.updates.check("node_missing", OWNER)
    off = await add_node(env, "offline-pc", b=build(), online=False)
    with pytest.raises(ConflictError) as exc:
        await env.updates.check(off, OWNER)
    assert exc.value.code == "node_offline" and "offline-pc" in exc.value.message
    old = await add_node(env, "old-pc", b=None, os_name="Windows")
    with pytest.raises(ConflictError) as exc:
        await env.updates.check(old, OWNER)
    assert exc.value.code == "update_not_supported"
    assert exc.value.message == "old-pc can't be updated remotely: it runs a HomeMesh version without the updater."
    assert "install.ps1 -Upgrade" in exc.value.hint and "git pull --ff-only" in exc.value.hint
    # an unreadable build may still come from an updater-capable worker: the worker decides
    odd = await add_node(env, "odd-pc", b={"invalid": True})
    assert (await env.updates.check(odd, OWNER))["job_id"]
    node_id = await add_node(env, "pc", b=build())
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    assert env.updates.check_pending(node_id) == job_id
    with pytest.raises(ConflictError) as exc:
        await env.updates.check(node_id, OWNER)  # a check is already running there
    assert exc.value.code == "update_busy" and "checking for updates" in exc.value.message
    audits = await audit(env, "node.update_check_requested")
    assert sum(a["outcome"] == "success" for a in audits) == 2
    assert {a["details_json"]["code"] for a in audits if a["outcome"] == "failure"} == {
        "not_found", "node_offline", "update_not_supported", "update_busy"}


async def test_check_refused_while_this_node_updates(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    await apply_job(env, node_id)
    with pytest.raises(ConflictError) as exc:
        await env.updates.check(node_id, OWNER)
    assert exc.value.code == "update_busy" and "pc" in exc.value.message
    # a local run (homemesh update on that computer, no row) blocks a check there too
    local = await add_node(env, "laptop", b=build(), live=report(None, phase="building"))
    with pytest.raises(ConflictError) as exc:
        await env.updates.check(local, OWNER)
    assert exc.value.code == "update_busy" and "building the new version" in exc.value.message


async def test_two_concurrent_checks_on_one_node_send_one_job(env):
    node_id = await add_node(env, "pc", b=build())
    results = await asyncio.gather(env.updates.check(node_id, OWNER), env.updates.check(node_id, OWNER),
                                   return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ConflictError) and r.code == "update_busy" for r in results) == 1
    assert len(ws(env, node_id).dispatched("update.check")) == 1


async def test_check_result_is_stored_capped_with_the_hub_clock(env):
    node_id = await add_node(env, "pc", b=build())
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    env.events.clear()
    commits = [{"sha": SHA_B, "subject": "\x1b[2Kevil\r\x07" + "x" * 300}] + [{"sha": SHA_C, "subject": "c"}] * 60
    result = check_result(commits=commits, commits_truncated=True, checked_at="1999-01-01T00:00:00Z",
                          problems=[{"code": "no_linger", "message": "m" * 900, "blocking": False}])
    await env.jobs.on_worker_message(node_id, pm.JobAccepted(job_id=job_id))
    await env.jobs.on_worker_message(node_id, pm.JobCompleted(job_id=job_id, result=result))
    stored = (await env.repos.nodes.get(node_id))["update_check_json"]
    assert stored["job_id"] == job_id and "error" not in stored
    assert stored["checked_at"].startswith(str(utcnow().year))  # coordinator clock, not the worker's
    res = stored["result"]
    assert res["upstream_commit"] == SHA_B and res["can_update"] is True and len(res["commits"]) == 50
    assert res["commits"][0]["subject"] == "[2Kevil" + "x" * 193  # control characters gone, 200 chars
    assert len(res["problems"][0]["message"]) == MESSAGE_MAX
    assert env.updates.check_pending(node_id) is None
    assert ("node.updated", {"node_id": node_id}) in env.events
    summary = env.nodes.node_summary(await env.repos.nodes.get(node_id))
    assert summary["update_check"]["result"]["upstream_commit"] == SHA_B and summary["build"]["commit"] == SHA_A


async def test_check_failure_is_stored_with_worker_hint_or_table_hint(env):
    node_id = await add_node(env, "pc", b=build())
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    await env.jobs.on_worker_message(node_id, pm.JobAccepted(job_id=job_id))
    await env.jobs.on_worker_message(node_id, pm.JobFailed(job_id=job_id, code="update_not_allowed",
                                                           error="Remote updates are off on pc. " + "z" * 900))
    err = (await env.repos.nodes.get(node_id))["update_check_json"]["error"]
    assert err["code"] == "update_not_allowed" and len(err["message"]) == MESSAGE_MAX
    assert err["hint"] == "On pc, run `homemesh update allow` (takes effect immediately)."  # from the table
    assert env.updates.check_pending(node_id) is None
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    await env.jobs.on_worker_message(node_id, pm.JobFailed(job_id=job_id, code="update_dirty_checkout",
                                                           error="dirty", hint="h\x1b" + "h" * 900))
    err = (await env.repos.nodes.get(node_id))["update_check_json"]["error"]
    # capped to HINT_MAX by the job service, then the escape character is stripped
    assert err == {"code": "update_dirty_checkout", "message": "dirty", "hint": "h" * (HINT_MAX - 1)}
    # the job row keeps the hint too (capped)
    events = await env.repos.jobs.events(job_id)
    assert any(e["kind"] == "failed" and len((e["data_json"] or {}).get("hint", "")) == HINT_MAX for e in events)


async def test_cancelled_check_keeps_the_previous_result_and_unreadable_result_is_an_error(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    await env.jobs.on_worker_message(node_id, pm.JobCancelled(job_id=job_id))
    assert (await env.repos.nodes.get(node_id))["update_check_json"]["job_id"] == "job_old"
    assert env.updates.check_pending(node_id) is None
    job_id = (await env.updates.check(node_id, OWNER))["job_id"]
    await env.jobs.on_worker_message(node_id, pm.JobCompleted(job_id=job_id, result={"ok": True}))
    err = (await env.repos.nodes.get(node_id))["update_check_json"]["error"]
    assert err["code"] == "update_check_unreadable" and "pc" in err["message"]


async def test_check_dispatch_failure_frees_the_node_and_says_offline(env):
    node_id = await add_node(env, "pc", b=build(), fail_send=True)
    with pytest.raises(ConflictError) as exc:
        await env.updates.check(node_id, OWNER)
    assert exc.value.code == "node_offline"
    assert env.updates.check_pending(node_id) is None
    assert (await env.repos.nodes.get(node_id))["update_check_json"]["error"]["code"] == "dispatch_failed"


# --------------------------------------------------------------------------- apply

async def test_apply_rules(env):
    off = await add_node(env, "offline-pc", b=build(), checked=check_result(), online=False)
    with pytest.raises(ConflictError) as exc:
        await env.updates.apply(off, SHA_B, False, OWNER)
    assert exc.value.code == "node_offline"
    dev = await add_node(env, "dev-pc", b=build(source="dev", commit=None, can_update=False,
                                                reason="editable/development install; update it with git pull"),
                         checked=check_result())
    with pytest.raises(ConflictError) as exc:
        await env.updates.apply(dev, SHA_B, False, OWNER)
    assert exc.value.code == "update_not_supported" and "editable/development install" in exc.value.message
    assert "cd /home/me/MyAi && git pull --ff-only && ./deploy/linux/install.sh --upgrade" == exc.value.hint.split(
        ": ", 1)[1]
    odd = await add_node(env, "odd-pc", b={"invalid": True}, checked=check_result())
    with pytest.raises(ConflictError) as exc:
        await env.updates.apply(odd, SHA_B, False, OWNER)
    assert exc.value.code == "update_not_supported" and "can't read" in exc.value.message
    with pytest.raises(ValidationFailed):
        await env.updates.apply(odd, "B" * 40, False, OWNER)
    # the update must match a successful check that can update to exactly this commit
    required = format_update_error("update_check_required", computer="pc").message
    node_id = await add_node(env, "pc", b=build())
    for stored in (None, {"job_id": "j", "checked_at": "t", "error": {"code": "update_dirty_checkout"}},
                   {"job_id": "j", "checked_at": "t", "result": check_result(upstream=SHA_C)},
                   {"job_id": "j", "checked_at": "t", "result": check_result(behind=0, can_update=False)}):
        await env.repos.nodes.set_update_check(node_id, stored)
        with pytest.raises(ConflictError) as exc:
            await env.updates.apply(node_id, SHA_B, False, OWNER)
        assert exc.value.code == "update_check_required" and exc.value.message == required
    assert await env.repos.db.fetchval("SELECT COUNT(*) FROM node_updates") == 0
    await env.repos.nodes.set_update_check(node_id, {"job_id": "j", "checked_at": "t", "result": check_result()})
    out = await env.updates.apply(node_id, SHA_B, True, OWNER)
    assert out["state"] == "requested"
    row = await env.repos.updates.get(out["update_id"])
    assert row["node_id"] == node_id and row["from_commit"] == SHA_A and row["force"] is True
    assert row["expected_commit"] == SHA_B and row["requested_by"] == OWNER and row["active"]
    (dispatch,) = ws(env, node_id).dispatched("update.apply")
    assert dispatch.job_id == out["update_id"]  # the row id is the job id
    assert dispatch.payload == {"expected_commit": SHA_B, "force": True}
    ok = [a for a in await audit(env, "node.update_requested") if a["outcome"] == "success"]
    assert len(ok) == 1 and ok[0]["details_json"]["update_id"] == out["update_id"]


async def test_one_update_in_the_mesh(env):
    hub = await add_node(env, "hub", b=build(), checked=check_result())
    pc = await add_node(env, "gaming-pc", b=build(), checked=check_result())
    results = await asyncio.gather(env.updates.apply(hub, SHA_B, False, OWNER),
                                   env.updates.apply(pc, SHA_B, False, OWNER), return_exceptions=True)
    created = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, ConflictError)]
    assert len(created) == 1 and len(refused) == 1 and refused[0].code == "update_busy"
    winner = (await env.repos.updates.get(created[0]["update_id"]))["node_id"]
    other_name = "hub" if winner == hub else "gaming-pc"
    assert refused[0].message == f"Another computer ({other_name}) is updating."
    assert await env.repos.db.fetchval("SELECT COUNT(*) FROM node_updates") == 1
    # the same node again: the non-mesh message
    with pytest.raises(ConflictError) as exc:
        await env.updates.apply(winner, SHA_B, False, OWNER)
    assert exc.value.message.startswith(f"Another update or check is running on {other_name} (")


async def test_apply_refused_while_any_computer_reports_a_local_run_or_checks(env):
    await add_node(env, "laptop", b=build(), live=report(None, phase="stopping"))
    pc = await add_node(env, "pc", b=build(), checked=check_result())
    with pytest.raises(ConflictError) as exc:
        await env.updates.apply(pc, SHA_B, False, OWNER)
    assert exc.value.code == "update_busy" and exc.value.message == "Another computer (laptop) is updating."
    env.nodes.connections[next(iter(env.nodes.connections))].update_live = report(None, phase="cleanup",
                                                                                  status="ok")
    await env.updates.check(pc, OWNER)
    with pytest.raises(ConflictError) as exc:
        await env.updates.apply(pc, SHA_B, False, OWNER)
    assert exc.value.code == "update_busy" and "checking for updates" in exc.value.message


async def test_apply_job_completed_marks_the_row_running(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    await env.jobs.on_worker_message(node_id, pm.JobAccepted(job_id=update_id))
    await env.jobs.on_worker_message(node_id, pm.JobCompleted(job_id=update_id,
                                                              result={"run_id": RUN, "state": "started"}))
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "running" and row["run_id"] == RUN and row["active"]


async def test_apply_result_with_a_bad_run_id_changes_nothing(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    await env.jobs.on_worker_message(node_id, pm.JobCompleted(job_id=update_id,
                                                              result={"run_id": "../../etc", "state": "started"}))
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "requested" and row["run_id"] is None and row["active"]


async def test_worker_refusal_fails_the_row_with_code_capped_text_and_hint(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    await env.jobs.on_worker_message(node_id, pm.JobAccepted(job_id=update_id))
    await env.jobs.on_worker_message(node_id, pm.JobFailed(job_id=update_id, code="update_not_allowed",
                                                           error="\x1b[31mRemote updates are off" + "!" * 800))
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "failed" and not row["active"] and row["finished_at"]
    assert row["error_code"] == "update_not_allowed"
    assert row["error"].startswith("[31mRemote updates are off") and len(row["error"]) == MESSAGE_MAX
    assert row["hint"] == UPDATE_ERRORS["update_not_allowed"][1].format(computer="pc")
    done = await audit(env, "node.update_finished")
    assert len(done) == 1 and done[0]["outcome"] == "failure" and done[0]["details_json"]["state"] == "failed"
    # the mesh is free again
    other = await add_node(env, "laptop", b=build(), checked=check_result())
    assert (await env.updates.apply(other, SHA_B, False, OWNER))["state"] == "requested"


async def test_launch_failure_keeps_the_worker_hint(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    hint = "journalctl --user -u 'homemesh-update-apply-u20261016120000-0123abcd' -n 50"
    await env.jobs.on_worker_message(node_id, pm.JobFailed(job_id=update_id, code="update_launch_failed",
                                                           error="The update helper could not be started: boom.",
                                                           hint=hint))
    row = await env.repos.updates.get(update_id)
    assert (row["state"], row["error_code"], row["hint"]) == ("failed", "update_launch_failed", hint)
    # a guess: should the runner have started after all, its reports win
    await env.updates.on_update_report(node_id, report(update_id, phase="building"), hello=False)
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "running" and row["active"] and row["error_code"] is None
    await env.updates.on_update_report(node_id, report(update_id, "cleanup", "already_up_to_date"), hello=False)
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "already_up_to_date" and not row["active"]
    assert [a["outcome"] for a in await audit(env, "node.update_finished")] == ["success", "failure"]


async def test_dispatch_failure_fails_the_row(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result(), fail_send=True)
    with pytest.raises(ConflictError) as exc:
        await env.updates.apply(node_id, SHA_B, False, OWNER)
    assert exc.value.code == "node_offline"
    (row,) = await env.repos.updates.list_for_node(node_id)
    assert row["state"] == "failed" and row["error_code"] == "dispatch_failed" and not row["active"]
    assert await env.repos.updates.get_active() is None


@pytest.mark.parametrize("code", ["worker_disconnected", "timeout", "coordinator_restarted", "worker_unresponsive"])
async def test_unknown_outcomes_leave_the_row_to_the_reports(env, code):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    # the computer is connected and reports this very run, so it obviously started
    env.nodes.connections[node_id].update_live = report(update_id, phase="prechecks")
    await env.updates._apply_failed({"id": update_id}, {"status": "failed", "error_code": code,
                                                        "error": "lost", "hint": None})
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "requested" and row["active"] and row["error_code"] is None


async def test_worker_disconnect_during_apply_keeps_the_row_active(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    await env.jobs.on_worker_message(node_id, pm.JobAccepted(job_id=update_id))
    del env.nodes.connections[node_id]
    await env.jobs.on_node_disconnected(node_id, "disconnected (code 1006)")
    assert (await env.repos.jobs.get(update_id))["error_code"] == "worker_disconnected"
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "requested" and row["active"]


# --------------------------------------------------------------------------- reports

async def test_reports_advance_phases_and_finalise_once(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    env.events.clear()
    await env.updates.on_update_report(node_id, report(update_id, phase="building"), hello=False)
    row = await env.repos.updates.get(update_id)
    assert (row["state"], row["phase"], row["run_id"]) == ("running", "building", RUN) and row["last_report_at"]
    assert row["report_json"]["phase"] == "building" and ("node.updated", {"node_id": node_id}) in env.events
    # a phase this version doesn't know (a newer runner) is stored as text
    await env.updates.on_update_report(node_id, report(update_id, phase="warming_caches"), hello=False)
    assert (await env.repos.updates.get(update_id))["phase"] == "warming_caches"
    final = report(update_id, phase="cleanup", status="ok", installed_commit=SHA_B, finished_at="2026-10-16T12:09:00Z",
                   warnings=[{"code": "checkout_not_advanced", "message": "w", "hint": "h"}])
    await env.updates.on_update_report(node_id, final, hello=False)
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "ok" and not row["active"] and row["finished_at"] and row["error_code"] is None
    view = update_view(row)
    assert view["installed_commit"] == SHA_B and view["warnings"][0]["code"] == "checkout_not_advanced"
    assert view["log"].endswith(f"update-{RUN}.log") and view["error"] is None and view["run_id"] == RUN
    # hello repeats the last report on every reconnect: no second audit, no change
    await env.updates.on_update_report(node_id, final, hello=True)
    done = await audit(env, "node.update_finished")
    assert len(done) == 1 and done[0]["outcome"] == "success" and done[0]["details_json"]["to_commit"] == SHA_B


async def test_final_failure_report_carries_the_runner_error(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    err = format_update_error("update_new_version_crashed", exit="exit code 3", log="/l/update.log")
    await env.updates.on_update_report(node_id, report(update_id, phase="rolling_back", status="rolled_back",
                                                       error=err.model_dump()), hello=False)
    row = await env.repos.updates.get(update_id)
    assert (row["state"], row["error_code"], row["error"], row["hint"]) == (
        "rolled_back", "update_new_version_crashed", err.message, err.hint)
    assert update_view(row)["error"] == {"code": err.code, "message": err.message, "hint": err.hint}
    assert (await audit(env, "node.update_finished"))[0]["outcome"] == "failure"


async def test_a_node_cannot_report_on_another_nodes_update(env):
    pc = await add_node(env, "pc", b=build(), checked=check_result())
    intruder = await add_node(env, "intruder", b=build())
    update_id = await apply_job(env, pc)
    await env.updates.on_update_report(intruder, report(update_id, phase="cleanup", status="ok"), hello=False)
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "requested" and row["report_json"] is None


async def test_late_report_overrides_abandoned_and_failed_guesses(env, monkeypatch):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    env.nodes.connections.pop(node_id)  # offline, no word
    later = utcnow() + timedelta(seconds=DISMISS_AFTER_S + 1)
    monkeypatch.setattr(us_mod, "utcnow", lambda: later)
    assert (await env.updates.dismiss(node_id, OWNER))["state"] == "abandoned"
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "abandoned" and row["error_code"] == "update_abandoned" and not row["active"]
    await env.updates.on_update_report(node_id, report(update_id, phase="cleanup", status="ok"), hello=True)
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "ok" and row["error_code"] is None


async def test_snapshot_marks_a_restarting_node_as_updating(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    conn = env.nodes.connections[node_id]
    for phase, updating in (("building", False), ("draining", True), ("verifying", True)):
        await env.nodes._handle_message(conn, pm.Heartbeat(seq=1, telemetry=Telemetry(), mode="balanced",
                                                           update=report(update_id, phase=phase).to_wire()))
        (snap,) = await env.nodes.snapshots()
        assert snap.updating is updating
        assert (await env.repos.updates.get(update_id))["phase"] == phase
    d = schedule(TaskRequirements("tool.exec", "interactive", needs_model=False), [snap])
    assert d.chosen is None and [r.code for r in d.rejections] == ["updating"]
    await env.nodes._handle_message(conn, pm.Heartbeat(seq=2, telemetry=Telemetry(), mode="balanced",
                                                       update=report(update_id, "cleanup", "ok").to_wire()))
    assert not (await env.nodes.snapshots())[0].updating


async def test_unchanged_heartbeat_reports_are_not_reprocessed(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    conn = env.nodes.connections[node_id]
    calls: list[Any] = []
    original = env.nodes.on_update_report

    async def counting(*a: Any, **kw: Any) -> None:
        calls.append(a)
        await original(*a, **kw)

    env.nodes.on_update_report = counting
    wire = report(update_id, phase="building").to_wire()
    for seq in range(3):
        await env.nodes._handle_message(conn, pm.Heartbeat(seq=seq, telemetry=Telemetry(), mode="balanced",
                                                           update=wire))
    assert len(calls) == 1
    # an unreadable report is noted (and doesn't count as "no run")
    await env.nodes._handle_message(conn, pm.Heartbeat(seq=9, telemetry=Telemetry(), mode="balanced",
                                                       update={"run_id": "nope"}))
    assert len(calls) == 2 and conn.update_live is None and conn.update_unreadable
    assert (await env.repos.updates.get(update_id))["state"] == "running"


# --------------------------------------------------------------------------- not started

async def test_not_started_when_the_computer_reconnects_without_this_run(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    env.nodes.connections.pop(node_id)
    await env.jobs.on_node_disconnected(node_id, "gone")  # apply job: worker_disconnected, row unchanged
    assert (await env.repos.updates.get(update_id))["active"]
    await add_node_connection(env, node_id, "pc")
    await env.updates.on_update_report(node_id, None, hello=True)
    row = await env.repos.updates.get(update_id)
    issue = format_update_error("update_not_started", computer="pc")
    assert (row["state"], row["error_code"], row["error"], row["hint"]) == (
        "not_started", "update_not_started", issue.message, issue.hint)
    assert not row["active"]
    # ... and a report of the run after all still wins
    await env.updates.on_update_report(node_id, report(update_id, phase="building"), hello=False)
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "running" and row["active"]


async def test_not_started_when_reporting_another_run_after_a_timeout(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    env.nodes.connections[node_id].update_live = report("job_previous", phase="cleanup", status="ok", run_id=RUN2)
    for t in env.jobs._timers.get(update_id, []):  # fire the 60 s timeout now
        t.cancel()
    handle = env.jobs.handles[update_id]
    await env.jobs._finish(update_id, handle, "failed", error_code="timeout", error="time limit")
    assert (await env.repos.updates.get(update_id))["state"] == "not_started"


async def test_unreadable_report_never_concludes_not_started(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    await env.jobs.on_node_disconnected(node_id, "gone")
    await env.updates.on_update_report(node_id, None, hello=True, unreadable=True)
    assert (await env.repos.updates.get(update_id))["state"] == "requested"


async def test_a_completed_apply_job_is_never_not_started(env):
    """The runner accepted the run (the worker saw run.json): a report about another run is a report cache a
    moment old (or a later local run); the row waits for its own reports or for Dismiss."""
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    await env.jobs.on_worker_message(node_id, pm.JobCompleted(job_id=update_id, result={"run_id": RUN}))
    await env.updates.on_update_report(node_id, report("job_previous", "cleanup", "ok", run_id=RUN2), hello=False)
    await env.updates.on_update_report(node_id, None, hello=True)
    row = await env.repos.updates.get(update_id)
    assert row["state"] == "running" and row["active"]


async def test_a_running_apply_job_is_never_not_started(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    await env.updates.on_update_report(node_id, None, hello=True)
    assert (await env.repos.updates.get(update_id))["state"] == "requested"


async def add_node_connection(env: Env, node_id: str, name: str) -> NodeConnection:
    conn = NodeConnection(node_id=node_id, name=name, ws=FakeWS(), protocol_version=1,  # type: ignore[arg-type]
                          hello=hello(node_id, name))
    env.nodes.connections[node_id] = conn
    return conn


# --------------------------------------------------------------------------- dismiss

async def test_dismiss_timing(env, monkeypatch):
    with pytest.raises(NotFoundError):
        await env.updates.dismiss("node_missing", OWNER)
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    with pytest.raises(NotFoundError):
        await env.updates.dismiss(node_id, OWNER)  # nothing in progress
    update_id = await apply_job(env, node_id)
    await env.updates.on_update_report(node_id, report(update_id, phase="building"), hello=False)
    reported = (await env.repos.updates.get(update_id))["last_report_at"]
    # connected and still reporting this run: never dismissable, however old the last change
    env.nodes.connections[node_id].update_live = report(update_id, phase="building")
    monkeypatch.setattr(us_mod, "utcnow", lambda: utcnow() + timedelta(hours=5))
    with pytest.raises(ConflictError) as exc:
        await env.updates.dismiss(node_id, OWNER)
    assert exc.value.code == "update_still_reporting" and exc.value.message == "This update reported 0 min ago."
    env.nodes.connections.pop(node_id)
    from homemesh.common.timeutil import parse_iso

    base = parse_iso(reported)
    monkeypatch.setattr(us_mod, "utcnow", lambda: base + timedelta(seconds=DISMISS_AFTER_S - 30))
    with pytest.raises(ConflictError) as exc:
        await env.updates.dismiss(node_id, OWNER)
    assert exc.value.code == "update_still_reporting" and exc.value.message == "This update reported 14 min ago."
    assert exc.value.hint == "Dismiss is possible after 15 minutes without word."
    monkeypatch.setattr(us_mod, "utcnow", lambda: base + timedelta(seconds=DISMISS_AFTER_S))
    assert await env.updates.dismiss(node_id, OWNER) == {"update_id": update_id, "state": "abandoned"}
    row = await env.repos.updates.get(update_id)
    issue = format_update_error("update_abandoned", computer="pc")
    assert (row["state"], row["error"], row["hint"]) == ("abandoned", issue.message, issue.hint)
    assert [a["outcome"] for a in await audit(env, "node.update_dismissed")] == ["success", "failure", "failure",
                                                                                 "failure", "failure"]


async def test_dismiss_counts_from_the_request_when_nothing_was_reported(env, monkeypatch):
    node_id = await add_node(env, "pc", b=build(), checked=check_result(), online=True)
    update_id = await apply_job(env, node_id)
    env.nodes.connections.pop(node_id)
    requested = (await env.repos.updates.get(update_id))["requested_at"]
    from homemesh.common.timeutil import parse_iso

    monkeypatch.setattr(us_mod, "utcnow", lambda: parse_iso(requested) + timedelta(minutes=16))
    assert (await env.updates.dismiss(node_id, OWNER))["state"] == "abandoned"


# --------------------------------------------------------------------------- hello / connection handling

async def _connect(env: Env, node_id: str, secret: str, msg: pm.Hello) -> tuple[FakeWS, asyncio.Task[None]]:
    sock = FakeWS({"authorization": f"{pm.AUTH_SCHEME} {node_id}:{secret}"})
    await sock.incoming.put(pm.dump(msg))
    task = asyncio.create_task(env.nodes.handle_websocket(sock, "127.0.0.1"))  # type: ignore[arg-type]
    return sock, task


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_hello_stores_build_and_hands_its_report_to_the_service(env):
    node_id = await add_node(env, "pc", b=build(), checked=check_result())
    update_id = await apply_job(env, node_id)
    env.nodes.connections.pop(node_id)
    await env.repos.nodes.add_credential(node_id, hash_token("s3cret"))
    final = report(update_id, "cleanup", "ok", installed_commit=SHA_B)
    sock, task = await _connect(env, node_id, "s3cret", hello(node_id, "pc", build=build(commit=SHA_B).copy(),
                                                              update=final.to_wire()))

    async def finished() -> bool:
        return (await env.repos.updates.get(update_id))["state"] == "ok"

    await _until(finished)
    assert isinstance(sock.sent[0], pm.Welcome)
    node = await env.repos.nodes.get(node_id)
    assert node["build_json"]["commit"] == SHA_B and node["build_json"]["source"] == "installer"
    summary = env.nodes.node_summary(node, update_row=await env.repos.updates.get(update_id))
    assert summary["update"]["state"] == "ok" and summary["update_live"]["status"] == "ok"
    assert summary["update_check_pending"] is None
    await sock.incoming.put(None)
    await task
    # reconnect with an unreadable build and no updater at all
    sock, task = await _connect(env, node_id, "s3cret", hello(node_id, "pc", build={"version": 3}))
    await _until(lambda: _build_is(env, node_id, {"invalid": True}))
    await sock.incoming.put(None)
    await task
    sock, task = await _connect(env, node_id, "s3cret", hello(node_id, "pc"))
    await _until(lambda: _build_is(env, node_id, None))
    await sock.incoming.put(None)
    await task


async def _build_is(env: Env, node_id: str, value: Any) -> bool:
    return (await env.repos.nodes.get(node_id))["build_json"] == value


async def test_protocol_mismatch_is_remembered_as_the_disconnect_reason(env):
    node_id = await add_node(env, "old-pc", b=None, online=False)
    await env.repos.nodes.add_credential(node_id, hash_token("s3cret"))
    sock, task = await _connect(env, node_id, "s3cret", hello(node_id, "old-pc").model_copy(
        update={"protocol_versions": [7, 8]}))
    await task
    assert sock.closed == (pm.CLOSE_PROTOCOL_INCOMPATIBLE, "protocol incompatible")
    assert isinstance(sock.sent[0], pm.ErrorMessage) and sock.sent[0].code == "protocol_incompatible"
    node = await env.repos.nodes.get(node_id)
    assert node["last_disconnect_reason"] == "protocol_incompatible: worker supports [7, 8], hub supports [1]"
    assert env.nodes.node_summary(node)["last_disconnect_reason"].startswith("protocol_incompatible:")


# --------------------------------------------------------------------------- API views

def test_jobs_api_redacts_update_rows():
    check = {"type": "update.check", "result_json": check_result(commits=[{"sha": SHA_B, "subject": "secret"}])}
    assert redact_update_job(check)["result_json"] == {
        "summary": {"upstream_commit": SHA_B, "behind": 2, "can_update": True}}
    apply = {"type": "update.apply", "result_json": {"run_id": RUN, "state": "started", "extra": "x"}}
    assert redact_update_job(apply)["result_json"] == {"run_id": RUN}
    failed = {"type": "update.check", "result_json": None}
    assert redact_update_job(failed)["result_json"] is None


def test_update_view_without_a_report():
    row = {"id": "job_1", "state": "requested", "phase": None, "run_id": None, "from_commit": SHA_A,
           "expected_commit": SHA_B, "force": False, "requested_at": iso(), "last_report_at": None,
           "finished_at": None, "error_code": None, "error": None, "hint": None, "report_json": None}
    view = update_view(row)
    assert view["warnings"] == [] and view["log"] is None and view["installed_commit"] is None
    assert view["error"] is None and view["state"] == "requested" and update_view(None) is None


def test_check_result_model_is_what_the_service_stores():
    # the stored result is the model's normalised dump: can_update is recomputed (behind 0 -> False)
    parsed = UpdateCheckResult.model_validate(check_result(behind=0, can_update=True))
    assert parsed.can_update is False
