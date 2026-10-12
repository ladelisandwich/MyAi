import asyncio

from homemesh.common.config import WorkerSection
from homemesh.coordinator.discovery import DiscoveryResponder, discover
from homemesh.worker.policy import LocalPolicy


def test_local_policy_enforces_owner_rules(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "other").mkdir()
    pol = LocalPolicy(WorkerSection(max_concurrent_jobs=1, allowed_roots=[str(tmp_path / "docs")],
                                    allowed_tools=["read_file"], task_types=["chat", "knowledge.scan", "tool.exec"]))
    assert pol.check("chat", "interactive", {}, 0, False).ok
    assert pol.check("chat", "interactive", {}, 1, False).code == "capacity"
    assert pol.check("embed", "background", {}, 0, False).code == "unsupported"
    assert pol.check("chat", "interactive", {}, 0, True).code == "busy"  # balanced backs off for games
    assert pol.check("knowledge.scan", "normal", {"root_path": str(tmp_path / "docs")}, 0, False).ok
    assert pol.check("knowledge.scan", "normal", {"root_path": str(tmp_path / "other")}, 0, False).code == "path_not_allowed"
    assert pol.check("tool.exec", "interactive", {"tool": "write_file"}, 0, False).code == "tool_not_allowed"
    pol.mode = "paused"
    assert pol.check("chat", "interactive", {}, 0, False).code == "paused"
    pol.mode = "background"
    assert pol.check("chat", "interactive", {}, 0, False).ok  # last resort is allowed
    pol.mode = "performance"
    assert pol.check("chat", "interactive", {}, 0, True).ok  # performance ignores external GPU use


def test_embedded_worker_trusts_coordinator_collection_roots(tmp_path):
    (tmp_path / "notes").mkdir()
    pol = LocalPolicy(WorkerSection(), embedded=True, trusted_roots=[str(tmp_path / "notes")])
    assert pol.check("knowledge.scan", "normal", {"root_path": str(tmp_path / "notes")}, 0, False).ok
    remote = LocalPolicy(WorkerSection(), embedded=False, trusted_roots=[str(tmp_path / "notes")])
    assert remote.check("knowledge.scan", "normal", {"root_path": str(tmp_path / "notes")}, 0, False).code == "path_not_allowed"


async def test_discovery_round_trip():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = DiscoveryResponder(port, {"name": "hub", "url": "https://10.0.0.5:8765", "version": "0.1.0",
                                  "tls_fingerprint": "AA:BB"})
    await r.start()
    try:
        found = await asyncio.to_thread(discover, port, 1.0, ("127.0.0.1",))
    finally:
        r.stop()
    assert found and found[0]["url"] == "https://10.0.0.5:8765" and found[0]["tls_fingerprint"] == "AA:BB"
