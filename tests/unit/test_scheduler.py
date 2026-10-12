from homemesh.scheduler.policy import ModelOnNode, NodeSnapshot, TaskRequirements, model_names_match, schedule
from homemesh.scheduler.profiles import effective_concurrency

GB = 1024**3
CHAT = frozenset({"completion"})


def model(name="llama3.1:8b", size_gb=4.9, loaded=False, caps=CHAT, **kw):
    return ModelOnNode(name=name, backend="ollama", backend_type="ollama", capabilities=frozenset(caps),
                       size_bytes=int(size_gb * GB), estimated_memory_bytes=int(size_gb * GB * 1.25 + 512 * 2**20),
                       loaded=loaded, **kw)


def node(name, models=(), *, vram_total=None, vram_free=None, ram=32000, mode="balanced", running=0, max_jobs=2,
         online=True, cpu=True, busy=False, weight=0, hb=1.0):
    return NodeSnapshot(node_id=f"id-{name}", name=name, online=online, mode=mode,
                        task_types=frozenset({"chat", "embed", "knowledge.scan", "tool.exec"}), models=list(models),
                        heartbeat_age_s=hb, max_concurrent_jobs=max_jobs, running_jobs=running, ram_available_mb=ram,
                        ram_reserve_mb=2048, vram_free_mb=vram_free, vram_total_mb=vram_total, vram_reserve_percent=10,
                        has_gpu=vram_total is not None, allow_cpu_inference=cpu, external_gpu_busy=busy, weight=weight)


def chat_req(**kw):
    kw.setdefault("required_capabilities", CHAT)
    return TaskRequirements("chat", "interactive", **kw)


def test_prefers_gpu_node_with_room_over_cpu_node():
    gpu = node("gaming-pc", [model()], vram_total=10240, vram_free=9500)
    cpu = node("laptop", [model()], ram=32000)
    d = schedule(chat_req(model="llama3.1:8b"), [cpu, gpu])
    assert d.chosen.node_name == "gaming-pc" and d.chosen.placement == "gpu"


def test_model_bigger_than_free_vram_is_not_assumed_to_fit():
    # 9 GB file on a 10 GB card with 3 GB already used: does not fit on the GPU alone.
    gpu = node("pc", [model(size_gb=9.0)], vram_total=10240, vram_free=7000, ram=4000, cpu=False)
    d = schedule(chat_req(), [gpu])
    assert d.chosen is None
    assert d.rejections[0].code == "insufficient_vram"


def test_partial_offload_when_cpu_allowed():
    pc = node("pc", [model(size_gb=9.0)], vram_total=10240, vram_free=7000, ram=24000)
    d = schedule(chat_req(), [pc])
    assert d.chosen.placement == "partial"


def test_insufficient_memory_everywhere_gives_reasons():
    small = node("pi", [model(size_gb=40)], ram=8000)
    d = schedule(chat_req(), [small])
    assert d.chosen is None and d.rejections[0].code == "insufficient_memory"
    assert "needs ~" in d.rejections[0].message


def test_loaded_model_wins_and_needs_no_memory():
    a = node("a", [model(loaded=True)], vram_total=10240, vram_free=500)
    b = node("b", [model()], vram_total=24576, vram_free=24000)
    d = schedule(chat_req(model="llama3.1:8b"), [a, b])
    assert d.chosen.node_name == "a" and d.chosen.placement == "loaded"


def test_offline_paused_stale_and_capacity_are_rejected():
    nodes = [node("off", [model()], online=False), node("paused", [model()], mode="paused"),
             node("stale", [model()], hb=100), node("full", [model()], running=2, max_jobs=2)]
    d = schedule(chat_req(), nodes)
    assert d.chosen is None
    assert {r.code for r in d.rejections} == {"offline", "paused", "stale_heartbeat", "at_capacity"}


def test_missing_capability_is_never_faked():
    n = node("pc", [model(caps={"completion"})], vram_total=10240, vram_free=9000)
    d = schedule(chat_req(required_capabilities=frozenset({"completion", "vision"})), [n])
    assert d.chosen is None and d.rejections[0].code == "missing_capability"
    assert "vision" in d.rejections[0].message


def test_requested_model_not_installed():
    d = schedule(chat_req(model="qwen2.5:14b"), [node("pc", [model()])])
    assert d.rejections[0].code == "model_not_on_node"


def test_manual_node_pin_is_honoured_or_fails_without_rerouting():
    a = node("a", [model()], vram_total=10240, vram_free=9500)
    b = node("b", [model()], mode="paused")
    d = schedule(chat_req(pinned_node_id="id-b"), [a, b])
    assert d.chosen is None and d.rejections[0].node_name == "b"


def test_preferred_node_is_soft():
    a = node("a", [model()], vram_total=10240, vram_free=9500)
    b = node("b", [model()], ram=64000)
    assert schedule(chat_req(preferred_node_id="id-b"), [a, b]).chosen.node_name == "b"


def test_remote_and_mock_models_excluded_unless_allowed():
    n = node("pc", [model("gpt-oss:120b-cloud", remote=True), model("mock-chat", mock=True)])
    d = schedule(chat_req(allow_mock=False), [n])
    assert d.chosen is None
    assert {r.code for r in d.rejections} == {"remote_model_disallowed", "mock_disallowed"}
    assert schedule(chat_req(allow_mock=True), [n]).chosen.model == "mock-chat"


def test_real_model_beats_mock():
    n = node("pc", [model("mock-chat", mock=True, size_gb=0.01), model("llama3.2:3b", size_gb=2.0)], ram=32000)
    assert schedule(chat_req(), [n]).chosen.model == "llama3.2:3b"


def test_background_mode_only_last_resort_for_chat():
    bg = node("bg", [model()], mode="background", vram_total=24576, vram_free=24000, max_jobs=4)
    fg = node("fg", [model()], ram=32000)
    d = schedule(chat_req(), [bg, fg])
    assert d.chosen.node_name == "fg"
    d2 = schedule(chat_req(), [bg])
    assert d2.chosen.node_name == "bg" and any("last resort" in n for n in d2.chosen.notes)


def test_external_gpu_busy_backs_off_in_balanced_but_not_performance():
    busy = node("gamer", [model()], vram_total=10240, vram_free=9000, busy=True)
    assert schedule(chat_req(), [busy]).rejections[0].code == "gpu_busy_external"
    busy.mode = "performance"
    assert schedule(chat_req(), [busy]).chosen is not None


def test_weight_breaks_ties():
    a = node("a", [model()], ram=32000, weight=0)
    b = node("b", [model()], ram=32000, weight=5)
    assert schedule(chat_req(), [a, b]).chosen.node_name == "b"


def test_queue_load_spreads_work():
    a = node("a", [model()], ram=32000, running=1)
    b = node("b", [model()], ram=32000, running=0)
    assert schedule(chat_req(), [a, b]).chosen.node_name == "b"


def test_unknown_vram_telemetry_uses_ram_check_with_note():
    n = node("amd-win", [model()], vram_total=16384, vram_free=None, ram=32000)
    c = schedule(chat_req(), [n]).chosen
    assert c.placement == "partial" or c.placement == "cpu"
    assert any("VRAM telemetry unavailable" in x for x in c.notes)


def test_role_default_model_preferred_in_auto_mode():
    n = node("pc", [model("llama3.2:3b", size_gb=2), model("qwen2.5:14b", size_gb=9)], ram=64000)
    assert schedule(chat_req(role_default_model="llama3.2:3b"), [n]).chosen.model == "llama3.2:3b"


def test_tasks_without_models_need_only_node():
    n = node("nas", [], ram=4000)
    d = schedule(TaskRequirements("knowledge.scan", "normal", needs_model=False, pinned_node_id="id-nas"), [n])
    assert d.chosen.node_name == "nas" and d.chosen.placement == "n/a"


def test_embedding_capability_routing():
    a = node("a", [model()], ram=32000)
    b = node("b", [model("nomic-embed-text", size_gb=0.3, caps={"embedding"})], ram=8000)
    d = schedule(TaskRequirements("embed", "background", model="nomic-embed-text",
                                  required_capabilities=frozenset({"embedding"})), [a, b])
    assert d.chosen.node_name == "b"


def test_decision_serialisable_and_summarised():
    d = schedule(chat_req(), [node("off", [model()], online=False)])
    assert "offline" in d.summary()
    assert d.to_dict()["chosen"] is None


def test_model_name_latest_tag_equivalence():
    assert model_names_match("all-minilm", "all-minilm:latest")
    assert not model_names_match("llama3", "llama3:70b")


def test_profile_concurrency_caps():
    assert effective_concurrency("performance", 4) == 4
    assert effective_concurrency("balanced", 4) == 2
    assert effective_concurrency("background", 4) == 1
    assert effective_concurrency("paused", 4) == 0
