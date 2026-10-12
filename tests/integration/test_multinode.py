"""Coordinator + two simulated workers over real WebSockets."""

import asyncio
import sqlite3
from contextlib import closing

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from harness import Harness, read_sse, wait_for
from homemesh.common.errors import AuthError
from homemesh.protocol import messages as pm
from homemesh.worker.enroll import join

pytestmark = pytest.mark.slow
CHAT = {"mock-chat": ["completion", "tools"]}


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    await harness.start(roles=["coordinator"], heartbeat_timeout_s=4.0)
    await harness.owner_client()
    yield harness
    await harness.stop()


async def _send(c, conv_id, content, **extra):
    async with c.stream("POST", f"/api/v1/conversations/{conv_id}/messages", json={"content": content, **extra}) as r:
        assert r.status_code == 200, await r.aread()
        return await read_sse(r)


async def test_enrollment_rejects_bad_reused_and_expired_codes(h, tmp_path):
    from harness import paths_for

    p = paths_for(tmp_path / "w")
    p.ensure()
    with pytest.raises(AuthError, match="unknown pairing code"):
        await asyncio.to_thread(join, h.base_url, "AAAA-BBBB-CCCC", p, node_name="x")
    code = await h.pairing_code()
    creds = await asyncio.to_thread(join, h.base_url, code, p, node_name="first")
    assert creds.node_id.startswith("node_")
    with pytest.raises(AuthError, match="already used"):
        await asyncio.to_thread(join, h.base_url, code, p, node_name="second")
    code2 = await h.pairing_code()
    with closing(sqlite3.connect(h.paths.db_file, isolation_level=None)) as db:
        db.execute("UPDATE pairing_tokens SET expires_at='2000-01-01T00:00:00.000Z' WHERE used_at IS NULL")
    with pytest.raises(AuthError, match="expired"):
        await asyncio.to_thread(join, h.base_url, code2, p, node_name="late")
    audit = (await h.client.get("/api/v1/system/audit?action=node.enroll")).json()
    assert sum(1 for a in audit if a["outcome"] == "failure") >= 3


async def test_insecure_http_join_to_remote_host_is_refused(tmp_path):
    from harness import paths_for
    from homemesh.worker.enroll import EnrollmentError

    with pytest.raises(EnrollmentError, match="unencrypted HTTP"):
        join("http://192.168.1.50:8765", "AAAA-BBBB-CCCC", paths_for(tmp_path), node_name="x")
    with pytest.raises(EnrollmentError, match="fingerprint is required"):
        join("https://192.168.1.50:8765", "AAAA-BBBB-CCCC", paths_for(tmp_path), node_name="x")


async def test_routing_respects_models_and_manual_node_choice(h):
    a = await h.add_worker("alpha", models=CHAT)
    b = await h.add_worker("beta", models={"mock-chat": ["completion", "tools"], "mock-vision": ["completion", "vision"]})
    nodes = {n["name"]: n for n in await h.nodes()}
    assert nodes["alpha"]["online"] and nodes["beta"]["online"]
    assert nodes["alpha"]["hardware"]["cpu"]["logical_cores"]  # real hardware report from the worker
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    # model only installed on beta
    route = dict(await _send(c, conv["id"], "hi", model="mock-vision"))["route"]
    assert route["node_name"] == "beta"
    # manual node selection is honoured
    await c.patch(f"/api/v1/conversations/{conv['id']}", json={"node_id": a.node_id})
    route = dict(await _send(c, conv["id"], "hi"))["route"]
    assert route["node_id"] == a.node_id and route["manual_node"]
    await c.patch(f"/api/v1/conversations/{conv['id']}", json={"node_id": b.node_id})
    assert dict(await _send(c, conv["id"], "hi"))["route"]["node_id"] == b.node_id


async def test_worker_crash_mid_stream_fails_clearly_and_next_request_reroutes(h):
    a = await h.add_worker("alpha", models=CHAT)
    await h.add_worker("beta", models=CHAT)
    c = h.client
    await c.patch(f"/api/v1/nodes/{a.node_id}", json={"weight": 10})  # prefer alpha
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = []

    async def chat():
        async with c.stream("POST", f"/api/v1/conversations/{conv['id']}/messages",
                            json={"content": "[[mock:slow]] a long answer please"}) as r:
            ev = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    import json

                    events.append((ev, json.loads(line[5:])))
                    if ev == "delta" and sum(1 for e, _ in events if e == "delta") == 2:
                        await a.crash()

    await asyncio.wait_for(chat(), 30)
    assert dict(events)["route"]["node_name"] == "alpha"
    err = dict(events)["error"]
    assert err["code"] == "worker_disconnected" and "alpha" in err["message"] and err["hint"]
    msg = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"][-1]
    assert msg["status"] == "failed" and msg["content"].startswith("[MOCK]")  # partial text kept
    assert (await c.get("/api/v1/system/health")).json()["status"] == "ok"
    await h.wait_node_online(a.node_id, online=False)
    nodes = {n["name"]: n for n in await h.nodes()}
    assert not nodes["alpha"]["online"] and nodes["alpha"]["last_disconnect_reason"]
    nxt = await _send(c, conv["id"], "are you still there?")
    assert dict(nxt)["route"]["node_name"] == "beta" and dict(nxt)["done"]["status"] == "complete"


async def test_revoked_node_is_disconnected_and_cannot_return(h):
    b = await h.add_worker("beta", models=CHAT)
    assert (await h.client.post(f"/api/v1/nodes/{b.node_id}/revoke")).status_code == 200
    await wait_for(lambda: b.agent.state == "stopped", 10, 0.1, "worker to stop after revocation")
    assert "revoked" in (b.agent.last_error or "")
    assert all(n["id"] != b.node_id for n in await h.nodes())
    from homemesh.worker.credentials import WorkerCredentials

    creds = WorkerCredentials.load(b.paths.worker_credentials_file)
    with pytest.raises(ConnectionClosed) as exc:
        async with connect(h.base_url.replace("http", "ws") + "/api/v1/worker/ws", proxy=None,
                           additional_headers={"Authorization": f"HomeMesh-Node {creds.node_id}:{creds.node_secret}"}) as ws:
            await ws.recv()
            await ws.recv()
    assert exc.value.rcvd.code == pm.CLOSE_REVOKED


async def test_invalid_node_credentials_rejected(h):
    for header in ("HomeMesh-Node node_x:forged-secret", "Bearer whatever", ""):
        with pytest.raises(ConnectionClosed) as exc:
            async with connect(h.base_url.replace("http", "ws") + "/api/v1/worker/ws", proxy=None,
                               additional_headers={"Authorization": header} if header else {}) as ws:
                await ws.recv()
                await ws.recv()
        assert exc.value.rcvd.code == pm.CLOSE_UNAUTHORIZED
    # a normal user session cannot impersonate a node either
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False) as anon:
        assert (await anon.get("/api/v1/worker/ws")).status_code in (400, 403, 404, 405, 426)


async def test_missed_heartbeats_mark_node_offline_then_worker_reconnects(h):
    a = await h.add_worker("alpha", models=CHAT)
    original = a.agent.send

    async def drop_heartbeats(msg):
        if getattr(msg, "type", "") == "heartbeat":
            return
        await original(msg)

    a.agent.send = drop_heartbeats  # type: ignore[method-assign]
    await h.wait_node_online(a.node_id, online=False, timeout=15)
    node = next(n for n in await h.nodes() if n["id"] == a.node_id)
    assert "heartbeat timeout" in node["last_disconnect_reason"]
    a.agent.send = original  # type: ignore[method-assign]
    await h.wait_node_online(a.node_id, online=True, timeout=20)  # automatic reconnection with backoff


async def test_paused_node_gets_no_work_and_pinned_request_explains(h):
    a = await h.add_worker("alpha", models=CHAT)
    await h.add_worker("beta", models=CHAT)
    c = h.client
    r = await c.patch(f"/api/v1/nodes/{a.node_id}", json={"mode": "paused"})
    assert r.status_code == 200 and r.json()["mode"] == "paused"
    await wait_for(lambda: a.agent.policy.mode == "paused", 5, 0.05, "worker to apply pause")
    conv = (await c.post("/api/v1/conversations", json={})).json()
    for _ in range(3):
        assert dict(await _send(c, conv["id"], "hi"))["route"]["node_name"] == "beta"
    await c.patch(f"/api/v1/conversations/{conv['id']}", json={"node_id": a.node_id})
    err = dict(await _send(c, conv["id"], "hi"))["error"]
    assert err["code"] == "no_eligible_worker" and "paused" in err["message"]
    assert err["hint"] and "Resume" in err["hint"]


async def test_capacity_limits_and_clear_busy_error(h):
    await h.add_worker("alpha", models=CHAT, max_concurrent_jobs=1)
    c = h.client
    convs = [(await c.post("/api/v1/conversations", json={})).json() for _ in range(2)]
    first = asyncio.create_task(_send(c, convs[0]["id"], "[[mock:slow]] long"))
    await asyncio.sleep(0.5)
    second = await _send(c, convs[1]["id"], "hello")  # waits queue_timeout (3s) for capacity, then fails clearly
    err = dict(second)["error"]
    assert err["code"] == "no_eligible_worker" and "running 1/1" in err["message"]
    assert "busy" in (err.get("hint") or "")
    assert dict(await first)["done"]["status"] == "complete"


async def test_job_cancel_via_jobs_api(h):
    await h.add_worker("alpha", models=CHAT)
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    task = asyncio.create_task(_send(c, conv["id"], "[[mock:slow]] long"))

    async def running_job():
        jobs = (await c.get("/api/v1/jobs?status=running")).json()["items"]
        return jobs[0]["id"] if jobs else None

    job_id = await wait_for(running_job, 10, 0.05, "running job")
    assert (await c.post(f"/api/v1/jobs/{job_id}/cancel")).status_code == 200
    events = await asyncio.wait_for(task, 20)
    assert events[-1][0] == "cancelled"
    assert (await c.get(f"/api/v1/jobs/{job_id}")).json()["status"] == "cancelled"


async def test_worker_cannot_inject_output_into_another_nodes_job(h):
    a = await h.add_worker("alpha", models=CHAT)
    b = await h.add_worker("beta", models=CHAT)
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    await c.patch(f"/api/v1/conversations/{conv['id']}", json={"node_id": a.node_id})
    task = asyncio.create_task(_send(c, conv["id"], "[[mock:slow]] long story"))

    async def running_job():
        jobs = (await c.get("/api/v1/jobs?status=running")).json()["items"]
        return jobs[0]["id"] if jobs else None

    job_id = await wait_for(running_job, 10, 0.05, "running job")
    await b.agent.send(pm.JobOutput(job_id=job_id, seq=999, kind="content", delta=" INJECTED-BY-BETA "))
    await b.agent.send(pm.JobCompleted(job_id=job_id, result={"content": "forged"}))
    events = await asyncio.wait_for(task, 30)
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert "INJECTED" not in text and dict(events)["done"]["status"] == "complete"
    msg = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"][-1]
    assert "INJECTED" not in msg["content"] and msg["node_id"] == a.node_id


async def test_model_management_job_runs_on_target_node(h):
    a = await h.add_worker("alpha", models=CHAT)
    c = h.client
    r = await c.post("/api/v1/models/manage", json={"action": "pull", "node_id": a.node_id, "backend": "mock",
                                                    "model": "tinyllama:1b"})
    job_id = r.json()["job_id"]

    async def done():
        j = (await c.get(f"/api/v1/jobs/{job_id}")).json()
        return j if j["status"] in ("completed", "failed") else None

    job = await wait_for(done, 15, 0.1, "pull job")
    assert job["status"] == "completed" and job["node_id"] == a.node_id

    async def has_model():
        models = (await c.get("/api/v1/models")).json()["models"]
        return any(m["name"] == "tinyllama:1b" for m in models)

    await wait_for(has_model, 15, 0.2, "model registry update")
    bad = await c.post("/api/v1/models/manage", json={"action": "pull", "node_id": a.node_id, "backend": "mock",
                                                      "model": "bad name; rm -rf /"})
    assert bad.status_code == 422
