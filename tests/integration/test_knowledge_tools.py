"""Distributed document indexing (scan on one computer, embed on another), RAG with real sources,
and the tool approval workflow."""

import asyncio
import json
import os

import pytest

from harness import Harness, read_sse, wait_for
from pdfutil import make_pdf

pytestmark = pytest.mark.slow


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    await harness.start(roles=["coordinator"])
    await harness.owner_client()
    yield harness
    await harness.stop()


def _docs(root):
    root.mkdir(parents=True)
    (root / "network.md").write_text("# Home network\n\nThe guest wifi network is called Orchard and uses WPA3.\n"
                                     "The NAS lives at 192.168.1.20 and backs up every night at 02:00.\n")
    (root / "garden.txt").write_text("Tomatoes need watering twice a week in summer.\n")
    (root / "manual.pdf").write_bytes(make_pdf(["Dishwasher manual", "Error E24 means the drain hose is blocked."]))
    (root / "scan.pdf").write_bytes(make_pdf(["", ""]))
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    return root


async def _send(c, conv_id, content, **extra):
    async with c.stream("POST", f"/api/v1/conversations/{conv_id}/messages", json={"content": content, **extra}) as r:
        assert r.status_code == 200, await r.aread()
        return await read_sse(r)


async def test_scan_on_one_node_embed_on_another_and_cited_answers(h, tmp_path):
    docs = _docs(tmp_path / "shared" / "docs")
    files_node = await h.add_worker("nas", models={"mock-chat": ["completion", "tools"]},
                                    allowed_roots=[str(tmp_path / "shared")])
    gpu_node = await h.add_worker("gpu-pc", models={"mock-embed": ["embedding"]})
    c = h.client
    # Folder outside what the NAS allows is rejected up front.
    bad = await c.post("/api/v1/knowledge/collections", json={"name": "x", "node_id": files_node.node_id,
                                                               "root_path": str(tmp_path / "elsewhere")})
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "path_not_allowed"
    r = await c.post("/api/v1/knowledge/collections", json={"name": "Home docs", "node_id": files_node.node_id,
                                                             "root_path": str(docs)})
    assert r.status_code == 200, r.text
    col_id = r.json()["collection"]["id"]

    async def indexed():
        col = (await c.get(f"/api/v1/knowledge/collections/{col_id}")).json()
        return col if col["status"] == "indexed" and col["embedded_count"] == col["chunk_count"] > 0 else None

    col = await wait_for(indexed, 30, 0.2, "indexing + embedding")
    docs_by_path = {d["rel_path"]: d for d in col["documents"]}
    assert docs_by_path["network.md"]["status"] == "indexed"
    assert docs_by_path["manual.pdf"]["status"] == "indexed" and docs_by_path["manual.pdf"]["page_count"] == 2
    assert docs_by_path["scan.pdf"]["status"] == "needs_ocr"
    assert not any(".ssh" in p for p in docs_by_path)  # hidden/credential folders never indexed
    jobs = (await c.get("/api/v1/jobs?status=completed&limit=200")).json()["items"]
    scan_nodes = {j["node_id"] for j in jobs if j["type"] == "knowledge.scan"}
    embed_nodes = {j["node_id"] for j in jobs if j["type"] == "embed"}
    assert scan_nodes == {files_node.node_id} and embed_nodes == {gpu_node.node_id}

    res = (await c.post("/api/v1/knowledge/search", json={"query": "what is the guest wifi called"})).json()
    assert res["mode"] == "hybrid"
    top = res["results"][0]
    assert top["path"] == "network.md" and "Orchard" in top["content"] and top["start_line"] == 1
    res_pdf = (await c.post("/api/v1/knowledge/search", json={"query": "dishwasher error E24 drain"})).json()
    assert res_pdf["results"][0]["path"] == "manual.pdf" and res_pdf["results"][0]["page"] == 2

    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _send(c, conv["id"], "What is the guest wifi network called?")
    ctx = dict(events)["context"]
    assert ctx["sources"] and ctx["sources"][0]["id"] == "S1" and ctx["sources"][0]["path"] == "network.md"
    assert "Home docs/network.md (lines 1-" in ctx["sources"][0]["source"]
    msg = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"][-1]
    assert msg["sources_json"][0]["path"] == "network.md"

    # Incremental re-index: change one file, delete another.
    (docs / "garden.txt").unlink()
    (docs / "network.md").write_text("# Home network\n\nThe guest wifi is now called Meadow.\n")
    await c.post(f"/api/v1/knowledge/collections/{col_id}/scan")

    async def reindexed():
        col = (await c.get(f"/api/v1/knowledge/collections/{col_id}")).json()
        paths = {d["rel_path"] for d in col["documents"]}
        return col if col["status"] == "indexed" and "garden.txt" not in paths else None

    await wait_for(reindexed, 30, 0.2, "re-index")
    res = (await c.post("/api/v1/knowledge/search", json={"query": "guest wifi name"})).json()
    assert "Meadow" in res["results"][0]["content"]
    assert not any("Orchard" in r["content"] for r in res["results"])
    assert (await c.delete(f"/api/v1/knowledge/collections/{col_id}")).status_code == 200
    assert docs.exists() and (docs / "network.md").exists()  # files untouched


async def test_keyword_search_works_without_embedding_model(h, tmp_path):
    docs = _docs(tmp_path / "d")
    node = await h.add_worker("solo", models={"mock-chat": ["completion"]}, allowed_roots=[str(tmp_path / "d")])
    c = h.client
    col_id = (await c.post("/api/v1/knowledge/collections", json={"name": "D", "node_id": node.node_id,
                                                                   "root_path": str(docs)})).json()["collection"]["id"]

    async def indexed():
        col = (await c.get(f"/api/v1/knowledge/collections/{col_id}")).json()
        return col if col["status"] == "indexed" else None

    col = await wait_for(indexed, 20, 0.2, "indexing")
    assert col["embedded_count"] == 0 and "keyword search only" in (col["error"] or "")
    res = (await c.post("/api/v1/knowledge/search", json={"query": "tomatoes watering"})).json()
    assert res["mode"] == "keyword" and res["results"][0]["path"] == "garden.txt"


async def _chat_with_approval(c, conv_id, content, decide):
    events = []

    async def run():
        async with c.stream("POST", f"/api/v1/conversations/{conv_id}/messages", json={"content": content}) as r:
            ev = None
            async for line in r.aiter_lines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:])
                    events.append((ev, data))
                    if ev == "approval_required" and decide is not None:
                        resp = await c.post(f"/api/v1/tools/approvals/{data['approval_id']}", json={"approved": decide})
                        assert resp.status_code == 200

    await asyncio.wait_for(run(), 30)
    return events


async def test_write_tool_requires_approval_and_runs_on_node(h, tmp_path):
    node = await h.add_worker("desk", models={"mock-chat": ["completion", "tools"]},
                              allowed_tools=["system_info", "list_directory", "read_file", "write_file"])
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    args = {"filename": "shopping.txt", "content": "milk, eggs"}
    events = await _chat_with_approval(c, conv["id"], f"[[mock:tool:write_file {json.dumps(args)}]]", decide=True)
    kinds = [e for e, _ in events]
    assert kinds.index("tool_call") < kinds.index("approval_required") < kinds.index("tool_result")
    result = dict(events)["tool_result"]
    assert result["ok"] is True and result["result"]["node"] == "desk"
    created = node.paths.workspace_dir / "shopping.txt"
    assert created.read_text() == "milk, eggs"
    assert dict(events)["done"]["status"] == "complete"
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert "Tool write_file returned" in text
    # Overwrite is refused even with approval
    events = await _chat_with_approval(c, conv["id"], f"[[mock:tool:write_file {json.dumps(args)}]]", decide=True)
    assert dict(events)["tool_result"]["ok"] is False
    execs = (await c.get("/api/v1/tools/executions")).json()
    assert {x["status"] for x in execs} >= {"succeeded", "failed"}


async def test_denied_tool_does_not_run(h):
    node = await h.add_worker("desk", models={"mock-chat": ["completion", "tools"]},
                              allowed_tools=["write_file"])
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    args = {"filename": "evil.txt", "content": "x"}
    events = await _chat_with_approval(c, conv["id"], f"[[mock:tool:write_file {json.dumps(args)}]]", decide=False)
    res = dict(events)["tool_result"]
    assert res["ok"] is False and "denied" in res["result"]["error"]
    assert not (node.paths.workspace_dir / "evil.txt").exists()
    audit = (await c.get("/api/v1/system/audit?action=tool.")).json()
    assert any(a["action"] == "tool.approval_denied" for a in audit)


async def test_tools_not_allowed_on_any_node_are_not_offered(h):
    await h.add_worker("locked", models={"mock-chat": ["completion", "tools"]}, allowed_tools=[])
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _chat_with_approval(c, conv["id"], '[[mock:tool:write_file {"filename": "a", "content": "b"}]]',
                                       decide=None)
    ctx = dict(events)["context"]
    assert "write_file" not in ctx["tools_offered"] and "calculator" in ctx["tools_offered"]
    assert "tool_call" not in [e for e, _ in events]  # the model cannot call what was not offered


async def test_read_only_coordinator_tool_runs_without_approval(h):
    await h.add_worker("desk", models={"mock-chat": ["completion", "tools"]})
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _chat_with_approval(c, conv["id"], '[[mock:tool:calculator {"expression": "6*7"}]]', decide=None)
    assert "approval_required" not in [e for e, _ in events]
    res = dict(events)["tool_result"]
    assert res["ok"] and res["result"]["output"]["result"] == 42
    # Owner can require approval for read tools too
    await c.put("/api/v1/tools/calculator/policy", json={"enabled": True, "approval": "always"})
    events = await _chat_with_approval(c, conv["id"], '[[mock:tool:calculator {"expression": "1+1"}]]', decide=True)
    assert "approval_required" in [e for e, _ in events]
    await c.put("/api/v1/tools/calculator/policy", json={"enabled": False, "approval": "default"})
    events = await _chat_with_approval(c, conv["id"], '[[mock:tool:calculator {"expression": "1+1"}]]', decide=None)
    assert "calculator" not in dict(events)["context"]["tools_offered"]


async def test_tool_arguments_are_validated(h):
    await h.add_worker("desk", models={"mock-chat": ["completion", "tools"]}, allowed_tools=["read_file"])
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    outside = json.dumps(os.path.abspath("/etc/shadow"))  # absolute on every OS (C:\\etc\\shadow on Windows)
    events = await _chat_with_approval(c, conv["id"], f'[[mock:tool:read_file {{"path": {outside}}}]]', decide=None)
    res = dict(events)["tool_result"]["result"]
    assert res["ok"] is False and ("not allowed" in res["error"] or "No folders" in res["error"]
                                   or "outside" in res["error"])
    events = await _chat_with_approval(c, conv["id"], '[[mock:tool:calculator {"expr": 1}]]', decide=None)
    assert "invalid arguments" in dict(events)["tool_result"]["result"]["error"]
