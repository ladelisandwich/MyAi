import json

import httpx
import pytest

from homemesh.common.errors import BackendError
from homemesh.inference.capabilities import (
    estimate_memory_bytes,
    infer_capabilities,
    parameter_size_to_billions,
)
from homemesh.inference.mock import MockBackend, mock_embedding
from homemesh.inference.ollama import OllamaBackend
from homemesh.inference.openai_compat import OpenAICompatBackend
from homemesh.protocol.jobs import ChatJobPayload, ChatMessage, ToolSpec

TAGS = {"models": [
    {"name": "llama3.1:8b", "model": "llama3.1:8b", "size": 4920753328, "digest": "abc",
     "details": {"format": "gguf", "family": "llama", "parameter_size": "8.0B", "quantization_level": "Q4_K_M",
                 "context_length": 131072}, "capabilities": ["completion", "tools"]},
    {"name": "old-model:latest", "model": "old-model:latest", "size": 1000, "details": {"family": "llama"}},
    {"name": "nomic-embed-text:latest", "size": 274302450, "details": {"family": "nomic-bert"}},
    {"name": "gpt-oss:120b-cloud", "model": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com:443",
     "remote_model": "gpt-oss:120b", "details": {"family": "gptoss"}, "capabilities": ["completion", "tools"]},
]}
SHOW = {"old-model:latest": {"model_info": {"llama.context_length": 4096}},
        "nomic-embed-text:latest": {"capabilities": ["embedding"], "model_info": {"nomic-bert.context_length": 2048,
                                                                                "nomic-bert.embedding_length": 768}}}
PS = {"models": [{"name": "llama3.1:8b", "size": 6654289920, "size_vram": 6654289920, "context_length": 8192}]}


def ollama_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.40.3"})
    if path == "/api/tags":
        return httpx.Response(200, json=TAGS)
    if path == "/api/ps":
        return httpx.Response(200, json=PS)
    if path == "/api/show":
        name = json.loads(request.content)["model"]
        return httpx.Response(200, json=SHOW.get(name, {}))
    if path == "/api/chat":
        body = json.loads(request.content)
        if body["model"] == "missing":
            return httpx.Response(404, json={"error": "model 'missing' not found"})
        if body["model"] == "notools":
            return httpx.Response(400, json={"error": "registry.ollama.ai/library/notools does not support tools"})
        lines = [
            {"model": body["model"], "message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"model": body["model"], "message": {"role": "assistant", "content": "lo"}, "done": False},
        ]
        if body.get("tools"):
            lines.append({"model": body["model"], "message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "calculator", "arguments": {"expression": "2+2"}}}]}, "done": False})
        if body["model"] == "midfail":
            lines.append({"error": "an error was encountered while running the model"})
        lines.append({"model": body["model"], "message": {"role": "assistant", "content": ""}, "done": True,
                      "done_reason": "stop", "total_duration": 998649281, "load_duration": 781965049,
                      "prompt_eval_count": 36, "prompt_eval_duration": 70451000, "eval_count": 9,
                      "eval_duration": 139282000})
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines) + "\n")
    if path == "/api/embed":
        body = json.loads(request.content)
        return httpx.Response(200, json={"model": body["model"], "embeddings": [[0.1, 0.2]] * len(body["input"]),
                                         "load_duration": 1000000, "prompt_eval_count": 3})
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def ollama():
    return OllamaBackend("ollama", "http://ollama.test", transport=httpx.MockTransport(ollama_handler))


async def test_ollama_models_capabilities_and_memory(ollama):
    models = {m.name: m for m in await ollama.list_models()}
    llama = models["llama3.1:8b"]
    assert llama.capabilities == ["completion", "tools"] and llama.capability_source == "backend"
    assert llama.loaded and llama.loaded_vram_bytes == 6654289920
    assert llama.estimated_memory_bytes == 6654289920  # measured resident size, not a guess
    old = models["old-model:latest"]
    assert old.capabilities == ["completion"] and old.capability_source == "inferred"
    assert "tools" not in old.capabilities and old.context_length == 4096
    assert any("did not report capabilities" in w for w in old.warnings)
    emb = models["nomic-embed-text:latest"]
    assert emb.capabilities == ["embedding"] and emb.embedding_length == 768
    cloud = models["gpt-oss:120b-cloud"]
    assert cloud.remote and cloud.size_bytes is None and "Cloud model" in cloud.warnings[0]


async def test_ollama_streaming_and_backend_metrics(ollama):
    p = ChatJobPayload(model="llama3.1:8b", backend="ollama", messages=[ChatMessage(role="user", content="hi")])
    chunks = [c async for c in ollama.chat(p)]
    assert "".join(c.text for c in chunks if c.kind == "content") == "Hello"
    done = chunks[-1]
    assert done.kind == "done" and done.finish_reason == "stop"
    m = done.metrics
    assert m.completion_tokens == 9 and m.prompt_tokens == 36
    assert m.load_ms == pytest.approx(781.965, rel=1e-3)
    assert m.tokens_per_second == pytest.approx(9 / 0.139282, rel=1e-3)


async def test_ollama_tool_calls_parsed(ollama):
    p = ChatJobPayload(model="llama3.1:8b", backend="ollama", messages=[ChatMessage(role="user", content="2+2?")],
                       tools=[ToolSpec(name="calculator", description="calc", parameters={"type": "object"})])
    calls = [c for c in [c async for c in ollama.chat(p)] if c.kind == "tool_calls"]
    assert calls[0].tool_calls[0].name == "calculator" and calls[0].tool_calls[0].arguments == {"expression": "2+2"}


@pytest.mark.parametrize("model,code", [("missing", "model_not_found"), ("notools", "model_incompatible"),
                                        ("midfail", "backend_http_error")])
async def test_ollama_errors_are_classified(ollama, model, code):
    p = ChatJobPayload(model=model, backend="ollama", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(BackendError) as exc:
        async for _ in ollama.chat(p):
            pass
    assert exc.value.code == code


async def test_ollama_unreachable_is_actionable():
    b = OllamaBackend("ollama", "http://127.0.0.1:1")
    st = await b.health()
    assert not st.ok and "Cannot reach Ollama" in st.error
    with pytest.raises(BackendError) as exc:
        await b.list_models()
    assert exc.value.code == "backend_unreachable" and "ollama serve" in exc.value.hint
    await b.close()


async def test_ollama_embed(ollama):
    r = await ollama.embed("nomic-embed-text", ["a", "b"])
    assert len(r.embeddings) == 2 and r.metrics.prompt_tokens == 3


def openai_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "qwen2.5-7b"}, {"id": "undeclared"}]})
    if request.url.path == "/v1/chat/completions":
        events = [
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "calcu", "arguments": '{"expr'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "lator", "arguments": 'ession": "1+1"}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
        ]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    return httpx.Response(404)


async def test_openai_compat_declared_capabilities_and_tool_fragments():
    b = OpenAICompatBackend("llamacpp", "http://llama.test:8080", declared_models={"qwen2.5-7b": ["completion", "tools"]},
                            transport=httpx.MockTransport(openai_handler))
    models = {m.name: m for m in await b.list_models()}
    assert models["qwen2.5-7b"].capabilities == ["completion", "tools"]
    assert models["undeclared"].capabilities == ["completion"] and models["undeclared"].warnings
    p = ChatJobPayload(model="qwen2.5-7b", backend="llamacpp", messages=[ChatMessage(role="user", content="x")],
                       tools=[ToolSpec(name="calculator", description="c", parameters={})])
    chunks = [c async for c in b.chat(p)]
    tc = next(c for c in chunks if c.kind == "tool_calls").tool_calls[0]
    assert tc.name == "calculator" and tc.arguments == {"expression": "1+1"}
    assert chunks[-1].metrics.prompt_tokens == 12 and chunks[-1].metrics.completion_tokens == 5
    with pytest.raises(BackendError):
        await b.embed("qwen2.5-7b", ["x"])  # not declared as an embedding model


async def test_mock_backend_is_clearly_labelled():
    m = MockBackend(token_delay_s=0)
    models = await m.list_models()
    assert all(x.mock for x in models)
    p = ChatJobPayload(model="mock-chat", backend="mock", messages=[ChatMessage(role="user", content="hello")])
    text = "".join([c.text async for c in m.chat(p) if c.kind == "content"])
    assert text.startswith("[MOCK]") and "no AI model ran" in text


def test_mock_embeddings_are_semantic_enough():
    import numpy as np

    a, b, c = (np.array(mock_embedding(t)) for t in ("green tea is great", "I love green tea", "GPU drivers on Linux"))
    assert a @ b > a @ c


def test_capability_inference_never_adds_tools_or_vision():
    assert infer_capabilities("llava:7b", "llama") == ["completion"]
    assert infer_capabilities("nomic-embed-text", "nomic-bert") == ["embedding"]
    assert infer_capabilities("mxbai-embed-large", None) == ["embedding"]


def test_memory_estimate_is_conservative():
    size = 4 * 1024**3
    est = estimate_memory_bytes(size)
    assert est > size * 1.2
    assert estimate_memory_bytes(size, loaded_size_bytes=5 * 1024**3) == 5 * 1024**3
    assert estimate_memory_bytes(None) is None


def test_parameter_size_parsing():
    assert parameter_size_to_billions("8.0B") == 8.0
    assert parameter_size_to_billions("134.52M") == pytest.approx(0.13452)
    assert parameter_size_to_billions(None) is None
