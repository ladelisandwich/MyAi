"""Resource profiles (operating modes) for worker nodes.

Profiles express *how much* of a computer HomeMesh may use. They are enforced twice: by
the coordinator's scheduler when choosing a node, and by the worker itself when a job
arrives (defence in depth). HomeMesh never kills other processes or steals GPU memory -
it only declines or defers its own work.
"""

from __future__ import annotations

from dataclasses import dataclass

PRIORITY_RANK = {"interactive": 0, "normal": 1, "background": 2}


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    accepts: frozenset[str]  # job priorities accepted without penalty
    last_resort: frozenset[str]  # priorities accepted only when no other node qualifies
    concurrency_cap: int | None  # caps the node's configured max_concurrent_jobs (None = no cap)
    vram_reserve_percent_min: float  # at least this much VRAM is left free
    ram_reserve_mb_min: int
    respect_external_gpu_busy: bool  # back off when another program is using the GPU


PROFILES: dict[str, Profile] = {
    "performance": Profile(
        name="performance",
        description="Prioritise AI throughput: full configured concurrency, small memory reserves.",
        accepts=frozenset({"interactive", "normal", "background"}), last_resort=frozenset(),
        concurrency_cap=None, vram_reserve_percent_min=5, ram_reserve_mb_min=1024,
        respect_external_gpu_busy=False,
    ),
    "balanced": Profile(
        name="balanced",
        description="Share the computer: moderate memory reserves, pause GPU work while games/apps use the GPU.",
        accepts=frozenset({"interactive", "normal", "background"}), last_resort=frozenset(),
        concurrency_cap=2, vram_reserve_percent_min=10, ram_reserve_mb_min=2048,
        respect_external_gpu_busy=True,
    ),
    "background": Profile(
        name="background",
        description="Low-priority work only (indexing, embeddings); chat only if no other computer can help.",
        accepts=frozenset({"normal", "background"}), last_resort=frozenset({"interactive"}),
        concurrency_cap=1, vram_reserve_percent_min=25, ram_reserve_mb_min=4096,
        respect_external_gpu_busy=True,
    ),
    "paused": Profile(
        name="paused",
        description="Accept no new jobs; stay connected and keep reporting status.",
        accepts=frozenset(), last_resort=frozenset(), concurrency_cap=0,
        vram_reserve_percent_min=100, ram_reserve_mb_min=0, respect_external_gpu_busy=True,
    ),
}


def get_profile(mode: str) -> Profile:
    return PROFILES.get(mode, PROFILES["balanced"])


def effective_concurrency(mode: str, configured: int) -> int:
    p = get_profile(mode)
    if p.concurrency_cap is None:
        return configured
    return min(configured, p.concurrency_cap)
