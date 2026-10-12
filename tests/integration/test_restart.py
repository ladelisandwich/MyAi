"""Coordinator restart: workers reconnect, data persists, interrupted work is reconciled honestly."""

import sqlite3

import pytest

from harness import Harness, read_sse

pytestmark = pytest.mark.slow


async def test_restart_preserves_data_and_workers_reconnect(tmp_path):
    h = Harness(tmp_path)
    await h.start(roles=["coordinator"])
    c = await h.owner_client()
    w = await h.add_worker("alpha", models={"mock-chat": ["completion"]})
    conv = (await c.post("/api/v1/conversations", json={})).json()
    async with c.stream("POST", f"/api/v1/conversations/{conv['id']}/messages", json={"content": "persist me"}) as r:
        await read_sse(r)
    await c.aclose()
    h.client = None
    h.stop_event.set()
    await h.task
    assert w.agent.state in ("reconnecting", "connecting")

    # Simulate work that was in flight when the coordinator died uncleanly.
    with sqlite3.connect(h.paths.db_file) as db:
        db.execute("INSERT INTO messages(id, conversation_id, role, content, status, created_at, updated_at)"
                   " VALUES ('msg_crash', ?, 'assistant', 'half an ans', 'streaming', 'z', 'z')", (conv["id"],))
        for jid, jtype, idem in (("job_chat", "chat", 0), ("job_embed", "embed", 1)):
            db.execute("INSERT INTO jobs(id, type, status, priority, payload_json, idempotent, max_attempts, attempts,"
                       " created_at, updated_at, node_id) VALUES (?,?,?,?,?,?,3,1,'t','t',?)",
                       (jid, jtype, "running", "background", '{"model": "mock-embed", "backend": "mock", "inputs": ["x"]}',
                        idem, w.node_id))

    await h.start(roles=["coordinator"])
    c = await h.owner_client()
    await h.wait_node_online(w.node_id, timeout=20)
    conv2 = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()
    statuses = {m["id"]: m["status"] for m in conv2["messages"]}
    assert statuses["msg_crash"] == "interrupted"
    assert [m["content"] for m in conv2["messages"]][:1] == ["persist me"]
    chat_job = (await c.get("/api/v1/jobs/job_chat")).json()
    assert chat_job["status"] == "interrupted" and chat_job["error_code"] == "coordinator_restarted"
    embed_job = (await c.get("/api/v1/jobs/job_embed")).json()
    assert embed_job["status"] in ("queued", "dispatched", "running", "completed", "failed")
    assert embed_job["status"] != "interrupted"  # idempotent work is retried, not dropped
    async with c.stream("POST", f"/api/v1/conversations/{conv['id']}/messages", json={"content": "still works?"}) as r:
        events = await read_sse(r)
    assert dict(events)["done"]["status"] == "complete"
    await h.stop()


async def test_worker_restart_rejoins_with_same_identity(tmp_path):
    h = Harness(tmp_path)
    await h.start(roles=["coordinator"])
    await h.owner_client()
    w = await h.add_worker("alpha", models={"mock-chat": ["completion"]})
    await w.stop()
    await h.wait_node_online(w.node_id, online=False)
    from homemesh.inference.mock import MockBackend
    from homemesh.worker.agent import WorkerAgent

    cfg = h.make_config(["worker"], name="alpha")
    cfg.worker.coordinator_url = h.base_url
    agent = WorkerAgent(cfg, w.paths, backends={"mock": MockBackend(models={"mock-chat": ["completion"]})})
    import asyncio

    task = asyncio.create_task(agent.run())
    await h.wait_node_online(w.node_id)
    assert len(await h.nodes()) == 1  # same node, not a duplicate registration
    await agent.stop()
    await task
    await h.stop()
