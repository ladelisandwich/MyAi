"""Opt-in smoke tests against a REAL local Ollama (no mocks). Run with:

    HOMEMESH_SMOKE_CHAT_MODEL=llama3.2:3b HOMEMESH_SMOKE_EMBED_MODEL=nomic-embed-text pytest -m smoke

Defaults match the tiny models used during development (smollm2:135m, all-minilm). The tests are
skipped automatically when Ollama is not reachable or the models are not installed.
"""

import os

import httpx
import pytest

from harness import Harness, read_sse, wait_for
from homemesh.common.config import BackendConfig

pytestmark = pytest.mark.smoke
OLLAMA = os.environ.get("HOMEMESH_SMOKE_OLLAMA_URL", "http://127.0.0.1:11434")
CHAT_MODEL = os.environ.get("HOMEMESH_SMOKE_CHAT_MODEL", "smollm2:135m")
EMBED_MODEL = os.environ.get("HOMEMESH_SMOKE_EMBED_MODEL", "all-minilm:latest")


def _available() -> set[str]:
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=3, trust_env=False)
        return {m["name"] for m in r.json().get("models", [])}
    except Exception:
        return set()


@pytest.fixture
async def h(tmp_path):
    names = _available()
    if not names:
        pytest.skip(f"Ollama not reachable at {OLLAMA}")
    if CHAT_MODEL not in names:
        pytest.skip(f"chat model {CHAT_MODEL} not installed (ollama pull {CHAT_MODEL})")
    harness = Harness(tmp_path)
    harness.make_config_orig = harness.make_config

    def real_config(roles, **kw):
        cfg = harness.make_config_orig(roles, **kw)
        cfg.inference.mock_enabled = False
        cfg.inference.backends = [BackendConfig(name="ollama", type="ollama", url=OLLAMA)]
        return cfg

    harness.make_config = real_config
    await harness.start(roles=["coordinator", "worker"], queue_timeout_s=30)
    await harness.owner_client()

    async def online():
        nodes = await harness.nodes()
        return nodes and nodes[0]["online"] and nodes[0]["models"]

    await wait_for(online, 30, 0.2, "embedded worker with Ollama models")
    yield harness
    await harness.stop()


async def test_real_local_inference_streams_and_is_recorded(h):
    c = h.client
    models = (await c.get("/api/v1/models")).json()["models"]
    assert not any(m["mock"] for m in models)
    conv = (await c.post("/api/v1/conversations", json={"model": CHAT_MODEL})).json()
    async with c.stream("POST", f"/api/v1/conversations/{conv['id']}/messages",
                        json={"content": "Reply with one short sentence about the sky."}) as r:
        events = await read_sse(r)
    route = dict(events)["route"]
    assert route["mock"] is False and route["backend_type"] == "ollama" and route["model"] == CHAT_MODEL
    assert sum(1 for e, _ in events if e == "delta") >= 2
    done = dict(events)["done"]
    m = done["metrics"]
    assert m["completion_tokens"] > 0 and m["prompt_tokens"] > 0 and m["tokens_per_second"] > 0
    msg = (await c.get(f"/api/v1/conversations/{conv['id']}")).json()["messages"][-1]
    assert msg["status"] == "complete" and msg["mock"] is False and msg["backend"] == "ollama" and msg["content"]


async def test_real_embeddings_power_document_search(h, tmp_path):
    if EMBED_MODEL not in _available():
        pytest.skip(f"embedding model {EMBED_MODEL} not installed")
    c = h.client
    await c.put("/api/v1/models/roles", json={"roles": {"embedding": EMBED_MODEL}})
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "pets.md").write_text("Our cat is named Biscuit and she likes sleeping on the radiator.")
    (docs / "car.md").write_text("The car needs new winter tyres before November.")
    node = (await h.nodes())[0]
    col = (await c.post("/api/v1/knowledge/collections", json={"name": "Docs", "node_id": node["id"],
                                                               "root_path": str(docs)})).json()["collection"]

    async def ready():
        d = (await c.get(f"/api/v1/knowledge/collections/{col['id']}")).json()
        return d if d["status"] == "indexed" and d["embedded_count"] == d["chunk_count"] > 0 else None

    await wait_for(ready, 60, 0.5, "real embedding")
    res = (await c.post("/api/v1/knowledge/search", json={"query": "what is the name of our pet?"})).json()
    assert res["mode"] == "hybrid" and res["results"][0]["path"] == "pets.md"
