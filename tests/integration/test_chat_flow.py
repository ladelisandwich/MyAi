"""Standalone mode (coordinator + embedded worker): streaming chat, persistence, memory, cancel, failures."""

import asyncio

import pytest

from harness import Harness, read_sse, wait_for

pytestmark = pytest.mark.slow


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    await harness.start(roles=["coordinator", "worker"])
    await harness.owner_client()
    await wait_for(lambda: _online(harness), 15, 0.1, "embedded worker")
    yield harness
    await harness.stop()


async def _online(h):
    nodes = await h.nodes()
    return nodes and nodes[0]["online"] and nodes[0]["models"]


async def _send(c, conv_id, content, **extra):
    async with c.stream("POST", f"/api/v1/conversations/{conv_id}/messages", json={"content": content, **extra}) as r:
        assert r.status_code == 200, await r.aread()
        return await read_sse(r)


async def test_streaming_chat_is_persisted_with_true_provenance(h):
    c = h.client
    nodes = await h.nodes()
    assert nodes[0]["embedded"] and nodes[0]["hardware"]["memory"]["total_mb"] > 0
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _send(c, conv["id"], "Hello assistant")
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[1] == "route" and "context" in kinds and kinds[-1] == "done"
    assert kinds.count("delta") > 3
    route = dict(events)["route"]
    assert route["model"] == "mock-chat" and route["mock"] is True and route["node_id"] == nodes[0]["id"]
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert text.startswith("[MOCK]") and "Hello assistant" in text
    done = dict(events)["done"]
    assert done["metrics"]["ttft_ms"] is not None and done["metrics"]["dispatch_overhead_ms"] is not None
    conv2 = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()
    assert conv2["title"] == "Hello assistant"
    user, assistant = conv2["messages"]
    assert user["role"] == "user" and assistant["status"] == "complete" and assistant["content"] == text
    assert assistant["mock"] is True and assistant["backend"] == "mock" and assistant["node_id"] == nodes[0]["id"]
    assert assistant["job_id"]
    job = (await c.get(f"/api/v1/jobs/{assistant['job_id']}")).json()
    assert job["status"] == "completed" and job["node_id"] == nodes[0]["id"]
    assert any(e["kind"] == "dispatched" for e in job["events"])
    assert job["decision_json"]["chosen"]["node_id"] == nodes[0]["id"]


async def test_history_continuity_and_regenerate(h):
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    await _send(c, conv["id"], "first question")
    events = await _send(c, conv["id"], "second question")
    ctx = dict(events)["context"]
    assert ctx["history_included"] == 2  # previous user + assistant messages were included
    msgs = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"]
    last = msgs[-1]
    regen = await _send(c, conv["id"], "", regenerate_of=last["id"])
    assert dict(regen)["done"]["status"] == "complete"
    msgs2 = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"]
    assert len(msgs2) == 4 and msgs2[-1]["id"] != last["id"] and msgs2[-2]["content"] == "second question"


async def test_explicit_memory_is_saved_and_used_across_conversations(h):
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _send(c, conv["id"], "Remember that my home server is called Atlas")
    saved = dict(events)["memory_saved"]
    assert saved["outcome"] == "created" and saved["content"] == "My home server is called Atlas"
    mems = (await c.get("/api/v1/memories")).json()["items"]
    assert mems[0]["source"] == "user_explicit" and mems[0]["status"] == "active"
    again = await _send(c, conv["id"], "remember that my home server is called Atlas!")
    assert dict(again)["memory_saved"]["outcome"] == "duplicate"
    # A brand-new conversation (as if from another device) retrieves it.
    conv2 = (await c.post("/api/v1/conversations", json={})).json()
    ctx = dict(await _send(c, conv2["id"], "What is my home server called?"))["context"]
    assert [m["content"] for m in ctx["memories"]] == ["My home server is called Atlas"]
    # With memory disabled for the request, nothing is injected.
    ctx_off = dict(await _send(c, conv2["id"], "What is my home server called?", use_memory=False))["context"]
    assert ctx_off["memories"] == []


async def test_memory_management_api(h):
    c = h.client
    r = (await c.post("/api/v1/memories", json={"content": "I prefer metric units"})).json()
    mid = r["memory"]["id"]
    assert r["memory"]["kind"] == "preference"
    upd = (await c.patch(f"/api/v1/memories/{mid}", json={"content": "I prefer metric units and 24h time"})).json()
    assert upd["source"] == "user_edited"
    hits = (await c.get("/api/v1/memories?q=units")).json()["items"]
    assert hits and hits[0]["id"] == mid
    assert (await c.delete(f"/api/v1/memories/{mid}")).status_code == 200
    assert (await c.get(f"/api/v1/memories/{mid}")).status_code == 404


async def test_cancel_mid_stream_keeps_partial_text(h):
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    collected = []

    async def run():
        async with c.stream("POST", f"/api/v1/conversations/{conv['id']}/messages",
                            json={"content": "[[mock:slow]] tell me a long story"}) as r:
            event = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    collected.append(event)
                    if event == "delta" and collected.count("delta") == 2:
                        mid = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["active_run"]
                        assert (await c.post(f"/api/v1/chat/runs/{mid}/cancel")).json()["ok"]

    await asyncio.wait_for(run(), 30)
    assert collected[-1] == "cancelled"
    msg = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"][-1]
    assert msg["status"] == "cancelled" and msg["content"].startswith("[MOCK]")
    job = (await c.get(f"/api/v1/jobs/{msg['job_id']}")).json()
    assert job["status"] == "cancelled"


async def test_backend_failure_is_reported_without_corrupting_history(h):
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _send(c, conv["id"], "[[mock:fail]] please fail")
    err = dict(events)["error"]
    assert err["code"] == "backend_error" and "mid-stream" in err["message"]
    msgs = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"]
    assert msgs[-1]["status"] == "failed" and msgs[-1]["error_code"] == "backend_error"
    # the conversation keeps working
    ok = await _send(c, conv["id"], "hello again")
    assert dict(ok)["done"]["status"] == "complete"
    # the failed assistant reply is not fed back to the model (only the earlier user message is)
    assert dict(ok)["context"]["history_included"] == 1


async def test_requested_model_that_does_not_exist_fails_clearly(h):
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    err = dict(await _send(c, conv["id"], "hi", model="llama-not-installed:70b"))["error"]
    assert err["code"] == "no_eligible_worker" and "not installed" in err["message"] and err["hint"]


async def test_image_routes_only_to_vision_model(h):
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    att = (await c.post(f"/api/v1/attachments?filename=x.png&conversation_id={conv['id']}", content=png,
                        headers={"Content-Type": "image/png"})).json()
    route = dict(await _send(c, conv["id"], "what is in this image?", attachment_ids=[att["id"]]))["route"]
    assert route["model"] == "mock-vision"
    bad = await c.post("/api/v1/attachments?filename=x.png", content=b"not an image",
                       headers={"Content-Type": "image/png"})
    assert bad.status_code == 422


async def test_conversation_search_export_and_delete(h):
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    await _send(c, conv["id"], "The quick brown zebra jumps")
    hits = (await c.get("/api/v1/conversations/search?q=zebra")).json()
    assert hits and hits[0]["conversation_id"] == conv["id"]
    exported = (await c.get(f"/api/v1/conversations/{conv['id']}/export")).json()
    assert len(exported["messages"]) == 2
    assert (await c.delete(f"/api/v1/conversations/{conv['id']}")).status_code == 200
    assert (await c.get(f"/api/v1/conversations/{conv['id']}")).status_code == 404


async def test_status_metrics_and_settings(h):
    c = h.client
    st = (await c.get("/api/v1/system/status")).json()
    assert st["nodes"]["online"] == 1 and any("MOCK" in w for w in st["warnings"])
    s = (await c.get("/api/v1/settings")).json()
    assert s["features"]["voice"]["available"] is False
    r = await c.patch("/api/v1/settings/assistant", json={"name": "Jarvis"})
    assert r.json()["name"] == "Jarvis"
    assert (await c.patch("/api/v1/settings/assistant", json={"bogus": 1})).status_code == 422
    conv = (await c.post("/api/v1/conversations", json={})).json()
    await _send(c, conv["id"], "hi")
    m = (await c.get("/api/v1/system/metrics")).json()
    assert m["groups"] and m["groups"][0]["type"] == "chat"
