"""Remote updates on the hub side: a real coordinator and simulated computers over real sockets (build info
in the hello, update jobs, run reports in heartbeats and hellos). The worker's own update code is not
involved: the simulated computer says exactly what a worker would, including cases a real worker reaches only
rarely. ``test_update_flow.py`` runs the same flow with real worker agents and the real launcher."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from harness import Harness, SimulatedWorker, read_sse, wait_for
from homemesh.protocol import messages as pm
from homemesh.protocol.models import ModelInfo
from homemesh.protocol.update import BuildInfo, UpdateRunReport, format_update_error

pytestmark = pytest.mark.slow

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40
RUN = "u20261016120000-0123abcd"
MOCK_CHAT = [ModelInfo(name="mock-chat", backend="mock", backend_type="mock", capabilities=["completion"], mock=True)]


def build(**kw: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"version": "0.3.0", "commit": SHA_A, "branch": "main", "source": "installer",
                              "os": "linux", "can_update": True, "update_allowed": True,
                              "source_dir": "/home/me/MyAi", "logs_dir": "/home/me/.local/share/homemesh/logs"}
    fields.update(kw)
    return BuildInfo(**fields).to_wire()


def check_result(upstream: str = SHA_B) -> dict[str, Any]:
    return {"v": 1, "ok": True, "checked_at": "2026-10-16T12:00:00Z", "allowed": True, "installed_commit": SHA_A,
            "upstream_commit": upstream, "behind": 1, "ahead": 0, "branch": "main", "remote": "origin",
            "upstream_ref": "origin/main", "commits": [{"sha": upstream, "subject": "CI: Łódź 修复 →"}],
            "commits_truncated": False, "can_update": True, "problems": []}


def report(job_id: str | None, phase: str, status: str | None = None, **kw: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {"run_id": RUN, "job_id": job_id, "origin": "remote", "phase": phase, "status": status,
                              "expected_commit": SHA_B, "from_commit": SHA_A, "to_commit": SHA_B,
                              "started_at": "2026-10-16T12:00:00Z", "updated_at": "2026-10-16T12:00:05Z",
                              "log": f"/home/me/.local/share/homemesh/logs/update-{RUN}.log"}
    fields.update(kw)
    return UpdateRunReport.model_validate(fields).to_wire()


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    await harness.start(roles=["coordinator"], heartbeat_timeout_s=4.0)
    await harness.owner_client()
    yield harness
    await harness.stop()


async def node(h: Harness, node_id: str) -> dict[str, Any]:
    r = await h.client.get(f"/api/v1/nodes/{node_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def until(h: Harness, node_id: str, predicate: Any, what: str, timeout: float = 10) -> dict[str, Any]:
    async def check() -> dict[str, Any] | None:
        n = await node(h, node_id)
        return n if predicate(n) else None

    return await wait_for(check, timeout, 0.05, what)


async def checked(h: Harness, sim: SimulatedWorker) -> str:
    """Run a successful check (upstream B) through the API and the simulated computer."""
    r = await h.client.post(f"/api/v1/nodes/{sim.node_id}/update/check")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    dispatch = await sim.next_dispatch("update.check")
    assert dispatch.job_id == job_id
    await sim.accept(job_id)
    await sim.complete(job_id, check_result())
    await until(h, sim.node_id, lambda n: (n["update_check"] or {}).get("job_id") == job_id, "check result")
    return job_id


async def finished_audits(h: Harness) -> list[dict[str, Any]]:
    return (await h.client.get("/api/v1/system/audit?action=node.update_finished")).json()


async def test_check_apply_reports_and_reconnect(h):
    sim = await h.add_simulated_worker("alpha", build=build(), models=MOCK_CHAT, task_types=["chat"])
    c = h.client
    n = await until(h, sim.node_id, lambda n: n["online"] and n["build"], "alpha online with build")
    assert n["build"]["commit"] == SHA_A and n["build"]["source_dir"] == "/home/me/MyAi"
    assert n["update"] is None and n["update_check"] is None and n["update_live"] is None
    assert n["update_check_pending"] is None

    # ---- check: placed directly, one at a time, result stored on the node
    r = await c.post(f"/api/v1/nodes/{sim.node_id}/update/check")
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    dispatch = await sim.next_dispatch("update.check")
    assert dispatch.job_id == job_id and dispatch.payload == {} and dispatch.timeout_s == 330
    assert (await node(h, sim.node_id))["update_check_pending"] == job_id
    r = await c.post(f"/api/v1/nodes/{sim.node_id}/update/check")
    assert r.status_code == 409 and r.json()["error"]["code"] == "update_busy"
    r = await c.post(f"/api/v1/nodes/{sim.node_id}/update/apply", json={"expected_commit": SHA_B})
    assert r.status_code == 409 and r.json()["error"]["code"] == "update_check_required"
    await sim.accept(job_id)
    await sim.complete(job_id, check_result())
    n = await until(h, sim.node_id, lambda n: n["update_check"] and n["update_check_pending"] is None, "check")
    assert n["update_check"]["result"]["upstream_commit"] == SHA_B
    assert n["update_check"]["result"]["commits"][0]["subject"] == "CI: Łódź 修复 →"
    job = (await c.get(f"/api/v1/jobs/{job_id}")).json()  # the jobs page shows a summary only
    assert job["status"] == "completed"
    assert job["result_json"] == {"summary": {"upstream_commit": SHA_B, "behind": 1, "can_update": True}}

    # ---- apply: must match the check; strict body
    for body in ({"expected_commit": SHA_C}, {"expected_commit": SHA_B[:7]}, {"expected_commit": SHA_B, "x": 1},
                 {"expected_commit": SHA_B, "force": "yes"}):
        r = await c.post(f"/api/v1/nodes/{sim.node_id}/update/apply", json=body)
        assert r.status_code in (409, 422), (body, r.text)
        assert r.json()["error"]["code"] in ("update_check_required", "validation_failed")
    r = await c.post(f"/api/v1/nodes/{sim.node_id}/update/apply", json={"expected_commit": SHA_B, "force": False})
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "requested"
    update_id = r.json()["update_id"]
    dispatch = await sim.next_dispatch("update.apply")
    assert dispatch.job_id == update_id and dispatch.payload == {"expected_commit": SHA_B, "force": False}
    r = await c.post(f"/api/v1/jobs/{update_id}/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "update_not_cancellable"
    r = await c.post(f"/api/v1/nodes/{sim.node_id}/update/apply", json={"expected_commit": SHA_B})
    assert r.status_code == 409 and r.json()["error"]["code"] == "update_busy"
    await sim.accept(update_id)
    await sim.complete(update_id, {"run_id": RUN, "state": "started"})
    n = await until(h, sim.node_id, lambda n: n["update"]["state"] == "running", "row running")
    assert n["update"]["run_id"] == RUN and n["update"]["expected_commit"] == SHA_B
    assert n["update"]["from_commit"] == SHA_A

    # ---- reports in heartbeats move the row; a draining computer gets no new work
    await sim.set_report(report(update_id, "building"))
    await until(h, sim.node_id, lambda n: n["update"]["phase"] == "building", "phase building")
    await sim.set_report(report(update_id, "draining"))
    n = await until(h, sim.node_id, lambda n: (n["update_live"] or {}).get("phase") == "draining", "draining")
    assert n["update"]["phase"] == "draining"
    conv = (await c.post("/api/v1/conversations", json={})).json()
    async with c.stream("POST", f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}) as resp:
        events = dict(await read_sse(resp))
    err = events["error"]
    assert err["code"] == "no_eligible_worker" and "restarting for an update" in err["message"]
    assert "restarting for an update" in err["hint"]
    assert not sim.dispatches.qsize()  # the chat never reached the updating computer

    final = report(update_id, "cleanup", "ok", installed_commit=SHA_B, finished_at="2026-10-16T12:04:00Z",
                   warnings=[{"code": "checkout_not_advanced", "message": "not advanced", "hint": "merge"}])
    await sim.set_report(final)
    n = await until(h, sim.node_id, lambda n: n["update"]["state"] == "ok", "row ok")
    assert n["update"]["finished_at"] and n["update"]["installed_commit"] == SHA_B and n["update"]["error"] is None
    assert n["update"]["warnings"][0]["code"] == "checkout_not_advanced"
    assert n["update"]["log"].endswith(f"update-{RUN}.log")
    assert len(await finished_audits(h)) == 1

    # ---- the new version reconnects and repeats its last report: nothing changes, nothing is audited twice
    await sim.disconnect()
    await until(h, sim.node_id, lambda n: not n["online"], "alpha offline")
    sim.build = build(commit=SHA_B, source="updater")
    welcome = await sim.connect()
    assert isinstance(welcome, pm.Welcome)
    n = await until(h, sim.node_id, lambda n: n["online"] and n["build"]["commit"] == SHA_B, "new build")
    assert n["update"]["state"] == "ok" and n["update_live"]["status"] == "ok"
    assert len(await finished_audits(h)) == 1
    # the hub's view of the chat node is back to normal
    assert (await c.get("/api/v1/system/health")).json()["status"] == "ok"


async def test_update_not_started_when_the_computer_comes_back_without_the_run(h):
    sim = await h.add_simulated_worker("beta", build=build())
    await checked(h, sim)
    r = await h.client.post(f"/api/v1/nodes/{sim.node_id}/update/apply", json={"expected_commit": SHA_B})
    update_id = r.json()["update_id"]
    await sim.next_dispatch("update.apply")
    await sim.accept(update_id)
    await sim.disconnect()  # gone before saying whether the runner started
    n = await until(h, sim.node_id, lambda n: not n["online"], "beta offline")
    assert n["update"]["state"] == "requested"  # unknown: the reports decide
    job = (await h.client.get(f"/api/v1/jobs/{update_id}")).json()
    assert job["status"] == "failed" and job["error_code"] == "worker_disconnected"
    sim.running.clear()
    await sim.connect()  # a hello without any run report
    n = await until(h, sim.node_id, lambda n: n["update"]["state"] == "not_started", "not started")
    issue = format_update_error("update_not_started", computer="beta")
    assert n["update"]["error"] == {"code": "update_not_started", "message": issue.message, "hint": issue.hint}
    r = await h.client.post(f"/api/v1/nodes/{sim.node_id}/update/dismiss")
    assert r.status_code == 404  # nothing in progress any more


async def test_refusals_from_the_computer_are_stored_with_their_hints(h):
    sim = await h.add_simulated_worker("gamma", build=build())
    # the owner ran "homemesh update disallow" there: checks and applies are refused at once, no restart needed
    r = await h.client.post(f"/api/v1/nodes/{sim.node_id}/update/check")
    job_id = r.json()["job_id"]
    await sim.next_dispatch("update.check")
    await sim.accept(job_id)
    issue = format_update_error("update_not_allowed", computer="gamma")
    await sim.fail(job_id, issue.code, issue.message, issue.hint)
    n = await until(h, sim.node_id, lambda n: (n["update_check"] or {}).get("error"), "check error")
    assert n["update_check"]["error"] == issue.model_dump()
    await checked(h, sim)
    r = await h.client.post(f"/api/v1/nodes/{sim.node_id}/update/apply", json={"expected_commit": SHA_B})
    update_id = r.json()["update_id"]
    await sim.next_dispatch("update.apply")
    await sim.accept(update_id)
    await sim.fail(update_id, issue.code, issue.message)  # no hint sent: the hub fills it from the table
    n = await until(h, sim.node_id, lambda n: n["update"]["state"] == "failed", "row failed")
    assert n["update"]["error"] == issue.model_dump()
    # owner only: an API token without the admin scope can't start anything
    token = (await h.client.post("/api/v1/auth/tokens", json={"name": "ro", "scopes": ["read"]})).json()["token"]
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False,
                                 headers={"Authorization": f"Bearer {token}"}) as ro:
        for path in ("check", "apply", "dismiss"):
            r = await ro.post(f"/api/v1/nodes/{sim.node_id}/update/{path}", json={"expected_commit": SHA_B})
            assert r.status_code == 403 and r.json()["error"]["code"] == "owner_required"
        assert (await ro.get(f"/api/v1/nodes/{sim.node_id}")).json()["update"]["state"] == "failed"
    # a browser session must send the CSRF header
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False, cookies=h.client.cookies) as no_csrf:
        r = await no_csrf.post(f"/api/v1/nodes/{sim.node_id}/update/check")
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"


async def test_dismiss_is_refused_while_the_update_is_fresh(h):
    sim = await h.add_simulated_worker("delta", build=build())
    await checked(h, sim)
    r = await h.client.post(f"/api/v1/nodes/{sim.node_id}/update/apply", json={"expected_commit": SHA_B})
    update_id = r.json()["update_id"]
    await sim.next_dispatch("update.apply")
    await sim.accept(update_id)
    await sim.complete(update_id, {"run_id": RUN, "state": "started"})
    await sim.set_report(report(update_id, "prechecks"))
    await until(h, sim.node_id, lambda n: n["update"]["phase"] == "prechecks", "prechecks")
    r = await h.client.post(f"/api/v1/nodes/{sim.node_id}/update/dismiss")
    assert r.status_code == 409 and r.json()["error"]["code"] == "update_still_reporting"
    # a local run on another computer would also have blocked a second update; here the mesh is busy anyway
    other = await h.add_simulated_worker("epsilon", build=build())
    await checked(h, other)
    r = await h.client.post(f"/api/v1/nodes/{other.node_id}/update/apply", json={"expected_commit": SHA_B})
    assert r.status_code == 409 and r.json()["error"]["message"] == "Another computer (delta) is updating."


async def test_protocol_mismatch_tells_the_card_what_happened(h):
    sim = await h.add_simulated_worker("old-pc", build=None, connect=False)
    msg = await sim.connect(protocol_versions=[99])
    assert isinstance(msg, pm.ErrorMessage) and msg.code == "protocol_incompatible"
    n = await until(h, sim.node_id, lambda n: n["last_disconnect_reason"], "disconnect reason")
    assert n["last_disconnect_reason"] == "protocol_incompatible: worker supports [99], hub supports [1]"
    assert not n["online"] and n["build"] is None
