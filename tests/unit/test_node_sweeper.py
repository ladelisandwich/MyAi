"""Heartbeat sweeper: dead workers are dropped, but a computer waking from sleep is not a reason
to drop every worker (on Windows the monotonic clock keeps counting while the PC sleeps)."""

import asyncio
from types import SimpleNamespace

from homemesh.coordinator import nodes as nodes_mod
from homemesh.coordinator.nodes import NodeManager


async def test_sleep_gap_gives_workers_a_fresh_timeout_but_dead_workers_still_go(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(nodes_mod, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    dropped: list[str] = []

    async def close(**_kw):
        return None

    async def drop(conn, reason):
        dropped.append(f"{conn.name}: {reason}")

    conn = SimpleNamespace(name="desk", last_heartbeat=1000.0, ws=SimpleNamespace(close=close))
    fake = SimpleNamespace(cfg=SimpleNamespace(server=SimpleNamespace(heartbeat_timeout_s=1.0)),
                           connections={"n1": conn}, _drop=drop)
    sweeper = asyncio.create_task(NodeManager._sweep_loop(fake))  # ticks every 1 s of real time
    await asyncio.sleep(0)  # let it start (and read the clock) before the "sleep"
    try:
        clock["t"] = 1600.0  # the PC slept for ten minutes before the first tick
        await asyncio.sleep(1.3)
        assert dropped == [] and conn.last_heartbeat == 1600.0
        clock["t"] = 1600.5
        await asyncio.sleep(1.0)
        assert dropped == []  # within the fresh timeout
        clock["t"] = 1603.0  # no heartbeat for 3 s with the process awake: really gone
        await asyncio.sleep(1.0)
        assert dropped == ["desk: heartbeat timeout (1s)"]
    finally:
        sweeper.cancel()
