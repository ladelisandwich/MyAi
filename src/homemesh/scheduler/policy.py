"""Pure, deterministic job placement policy.

The scheduler receives a snapshot of every node and the requirements of one task and
returns a :class:`Decision`: the chosen (node, model) pair, all ranked alternatives, and a
machine-readable reason for every node/model that was rejected. Nothing here performs I/O,
so the policy is fully unit-testable and its output is stored with each job to explain
*why* a computer was (not) chosen.

Placement steps (in order):

1. node online with a fresh heartbeat
2. operating mode / resource profile accepts the job priority
3. node advertises the task type
4. node has spare concurrency
5. model present with all required capabilities (remote/cloud and mock models excluded
   unless explicitly allowed)
6. memory fit: a loaded model needs nothing new; otherwise a conservative estimate must
   fit in free VRAM minus the reserve (GPU), VRAM + RAM (partial offload) or RAM (CPU)
7. score = queue load + model-load cost + CPU penalty + mode penalty - user preference
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from homemesh.scheduler.profiles import effective_concurrency, get_profile

MIB = 1024 * 1024
PLACEMENT_RANK = {"n/a": 0, "loaded": 0, "gpu": 1, "partial": 2, "unknown": 3, "cpu": 4}


@dataclass
class ModelOnNode:
    name: str
    backend: str
    backend_type: str
    capabilities: frozenset[str]
    size_bytes: int | None = None
    estimated_memory_bytes: int | None = None
    loaded: bool = False
    context_length: int | None = None
    remote: bool = False
    mock: bool = False
    parameter_size_b: float | None = None


@dataclass
class NodeSnapshot:
    node_id: str
    name: str
    online: bool
    mode: str
    task_types: frozenset[str]
    models: list[ModelOnNode] = field(default_factory=list)
    heartbeat_age_s: float | None = None
    max_concurrent_jobs: int = 1
    running_jobs: int = 0
    ram_available_mb: int | None = None
    ram_reserve_mb: int = 2048
    vram_free_mb: int | None = None  # sum over discrete GPUs; None = unknown
    vram_total_mb: int | None = None
    vram_reserve_percent: float = 10
    has_gpu: bool = False
    allow_cpu_inference: bool = True
    external_gpu_busy: bool = False
    weight: int = 0  # owner preference (-10..10); higher = preferred
    embedded: bool = False


@dataclass
class TaskRequirements:
    task_type: str
    priority: str = "interactive"
    model: str | None = None
    backend: str | None = None
    required_capabilities: frozenset[str] = frozenset()
    needs_model: bool = True
    pinned_node_id: str | None = None  # manual override: this node or nothing
    preferred_node_id: str | None = None  # soft preference
    role_default_model: str | None = None  # owner's default for this task role
    allow_remote_models: bool = False
    allow_mock: bool = True
    min_context: int | None = None


@dataclass
class Candidate:
    node_id: str
    node_name: str
    model: str | None
    backend: str | None
    backend_type: str | None
    score: float
    placement: str  # loaded | gpu | partial | cpu | unknown | n/a
    estimated_memory_mb: int | None = None
    mock: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class Rejection:
    node_id: str
    node_name: str
    code: str
    message: str
    model: str | None = None


@dataclass
class Decision:
    chosen: Candidate | None
    candidates: list[Candidate]
    rejections: list[Rejection]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chosen": asdict(self.chosen) if self.chosen else None,
            "candidates": [asdict(c) for c in self.candidates[:10]],
            "rejections": [asdict(r) for r in self.rejections[:50]],
        }

    def summary(self) -> str:
        if self.chosen:
            c = self.chosen
            return f"{c.model or c.backend or 'task'} on {c.node_name} ({c.placement}, score {c.score:.0f})"
        if not self.rejections:
            return "no worker nodes are connected"
        return "; ".join(f"{r.node_name}: {r.message}" for r in self.rejections[:5])


def model_names_match(wanted: str, have: str) -> bool:
    if wanted == have:
        return True
    w = wanted if ":" in wanted else wanted + ":latest"
    h = have if ":" in have else have + ":latest"
    return w == h


def _mb(b: int | None) -> int | None:
    return None if b is None else int(b // MIB)


def _placement(node: NodeSnapshot, model: ModelOnNode, profile_vram_reserve: float, profile_ram_reserve: int
               ) -> tuple[str | None, str | None, list[str]]:
    """Return (placement, rejection_code, notes). placement None means rejected."""
    notes: list[str] = []
    if model.loaded:
        return "loaded", None, ["model already loaded"]
    est_mb = _mb(model.estimated_memory_bytes)
    ram_reserve = max(node.ram_reserve_mb, profile_ram_reserve)
    ram_avail = (node.ram_available_mb - ram_reserve) if node.ram_available_mb is not None else None
    vram_avail = None
    if node.vram_free_mb is not None and node.vram_total_mb:
        reserve_pct = max(node.vram_reserve_percent, profile_vram_reserve)
        vram_avail = node.vram_free_mb - int(node.vram_total_mb * reserve_pct / 100)
    if est_mb is None:
        notes.append("memory requirement unknown; fit not verified")
        return "unknown", None, notes
    if vram_avail is not None and est_mb <= vram_avail:
        return "gpu", None, notes
    if node.has_gpu and vram_avail is None:
        notes.append("VRAM telemetry unavailable; checked system RAM only")
    if not node.allow_cpu_inference:
        return None, "insufficient_vram", [
            f"needs ~{est_mb} MB but only {max(vram_avail or 0, 0)} MB VRAM is free after reserve and CPU "
            "inference is disabled on this node"]
    if vram_avail is not None and vram_avail > 0 and ram_avail is not None and est_mb <= vram_avail + ram_avail:
        notes.append(f"partial GPU offload ({vram_avail} MB VRAM + system RAM)")
        return "partial", None, notes
    if ram_avail is not None and est_mb <= ram_avail:
        return "cpu", None, notes
    have = ram_avail if ram_avail is not None else "unknown"
    return None, "insufficient_memory", [
        f"needs ~{est_mb} MB; available after reserves: VRAM {vram_avail if vram_avail is not None else 'n/a'} MB, "
        f"RAM {have} MB"]


def schedule(req: TaskRequirements, nodes: list[NodeSnapshot], *, heartbeat_timeout_s: float = 35.0) -> Decision:
    candidates: list[Candidate] = []
    rejections: list[Rejection] = []
    is_chat = req.task_type == "chat"

    for node in nodes:
        if req.pinned_node_id and node.node_id != req.pinned_node_id:
            continue

        def reject(code: str, msg: str, model: str | None = None, _node: NodeSnapshot = node) -> None:
            rejections.append(Rejection(_node.node_id, _node.name, code, msg, model))

        if not node.online:
            reject("offline", "node is offline")
            continue
        if node.heartbeat_age_s is not None and node.heartbeat_age_s > heartbeat_timeout_s:
            reject("stale_heartbeat", f"no heartbeat for {node.heartbeat_age_s:.0f}s")
            continue
        profile = get_profile(node.mode)
        if node.mode == "paused":
            reject("paused", "node is paused")
            continue
        if req.task_type not in node.task_types:
            reject("task_type_unsupported", f"node does not accept '{req.task_type}' jobs")
            continue
        last_resort = False
        if req.priority not in profile.accepts:
            if req.priority in profile.last_resort:
                last_resort = True
            else:
                reject("mode_excludes_priority", f"{node.mode} mode does not accept {req.priority} jobs")
                continue
        cap = effective_concurrency(node.mode, node.max_concurrent_jobs)
        if node.running_jobs >= cap:
            reject("at_capacity", f"running {node.running_jobs}/{cap} jobs")
            continue
        uses_gpu_models = req.needs_model and req.task_type in ("chat", "embed")
        if (uses_gpu_models and node.external_gpu_busy and profile.respect_external_gpu_busy and node.has_gpu):
            reject("gpu_busy_external", "GPU is busy with another application (game/render); "
                                        f"{node.mode} mode backs off")
            continue

        base = 100.0 * node.running_jobs / max(cap, 1)
        base -= node.weight * 10
        if last_resort:
            base += 1000
        if node.mode == "performance":
            base -= 5
        if req.preferred_node_id and node.node_id == req.preferred_node_id:
            base -= 500

        if not req.needs_model:
            candidates.append(Candidate(node.node_id, node.name, None, None, None, base, "n/a",
                                        notes=["last resort (mode)"] if last_resort else []))
            continue

        eligible: list[ModelOnNode] = []
        near_misses: list[tuple[str, str, str]] = []
        for m in node.models:
            if req.model and not model_names_match(req.model, m.name):
                continue
            if req.backend and m.backend != req.backend:
                continue
            missing = req.required_capabilities - m.capabilities
            if missing:
                near_misses.append(("missing_capability", m.name,
                                    f"model {m.name} lacks {', '.join(sorted(missing))}"))
                continue
            if m.remote and not req.allow_remote_models:
                near_misses.append(("remote_model_disallowed", m.name,
                                    f"{m.name} is a cloud model (runs outside your network); cloud use is disabled"))
                continue
            if m.mock and not req.allow_mock:
                near_misses.append(("mock_disallowed", m.name, f"{m.name} is a mock model"))
                continue
            if req.min_context and m.context_length and m.context_length < req.min_context:
                near_misses.append(("context_too_small", m.name,
                                    f"{m.name} context {m.context_length} < required {req.min_context}"))
                continue
            eligible.append(m)
        if not eligible:
            if near_misses:
                for code, mname, msg in near_misses[:3]:
                    reject(code, msg, mname)
            elif req.model:
                reject("model_not_on_node", f"model {req.model} is not installed on this node", req.model)
            else:
                caps = ", ".join(sorted(req.required_capabilities)) or "any"
                reject("no_suitable_model", f"no installed model offers: {caps}")
            continue

        for m in eligible:
            placement, code, notes = _placement(node, m, profile.vram_reserve_percent_min, profile.ram_reserve_mb_min)
            if placement is None:
                reject(code or "insufficient_memory", notes[0] if notes else "does not fit", m.name)
                continue
            size_gb = (m.estimated_memory_bytes or m.size_bytes or 0) / (1024 ** 3)
            score = base
            if placement == "gpu":
                score += 20 + size_gb * 5
            elif placement == "partial":
                score += 60 + size_gb * 8
            elif placement == "cpu":
                score += (40 if not is_chat else 80) + size_gb * 10
            elif placement == "unknown":
                score += 50
            if is_chat and not req.model:
                if req.role_default_model and model_names_match(req.role_default_model, m.name):
                    score -= 300
                if m.parameter_size_b:
                    score -= min(m.parameter_size_b, 70.0) * 2  # prefer more capable models when they fit
            if m.mock:
                score += 200  # real models always win over mock ones when both exist
                notes = [*notes, "MOCK model"]
            if last_resort:
                notes = [*notes, f"last resort: node is in {node.mode} mode"]
            candidates.append(Candidate(
                node.node_id, node.name, m.name, m.backend, m.backend_type, round(score, 2), placement,
                estimated_memory_mb=_mb(m.estimated_memory_bytes), mock=m.mock, notes=notes,
            ))

    candidates.sort(key=lambda c: (c.score, PLACEMENT_RANK.get(c.placement, 9), c.node_name, c.model or ""))
    if req.pinned_node_id and not any(n.node_id == req.pinned_node_id for n in nodes):
        rejections.append(Rejection(req.pinned_node_id, req.pinned_node_id, "unknown_node",
                                    "the selected node is not registered"))
    return Decision(chosen=candidates[0] if candidates else None, candidates=candidates, rejections=rejections)
