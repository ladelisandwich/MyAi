"""Optional LAN discovery (disabled by default).

When ``server.discovery_enabled = true`` the coordinator answers UDP broadcast probes on
``server.discovery_port`` with its name, URL, version and TLS fingerprint. A worker can then
run ``homemesh worker discover`` instead of typing the address.

Discovery is a convenience only and is **not trusted**: anyone on the LAN could answer a
probe. Joining still requires the one-time pairing code from the real coordinator, and the
owner must compare the fingerprint shown by ``discover`` with the one on the coordinator's
screen. Workers never rely on discovery for anything else, and it does not work across
routed subnets or VPNs (use the coordinator URL there).
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections import defaultdict
from typing import Any

from homemesh.common.logging import get_logger

log = get_logger(__name__)
PROBE = b"HOMEMESH_DISCOVER v1"
MAX_REPLIES_PER_SOURCE_PER_MIN = 30


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        self.transport: asyncio.DatagramTransport | None = None
        self._hits: dict[str, list[float]] = defaultdict(list)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.strip() != PROBE or self.transport is None:
            return
        now = time.monotonic()
        hits = [t for t in self._hits[addr[0]] if now - t < 60]
        if len(hits) >= MAX_REPLIES_PER_SOURCE_PER_MIN:
            return
        hits.append(now)
        self._hits[addr[0]] = hits
        self.transport.sendto(json.dumps(self.info).encode("utf-8"), addr)


class DiscoveryResponder:
    def __init__(self, port: int, info: dict[str, Any]) -> None:
        self.port = port
        self.info = {"service": "homemesh", **info}
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Responder(self.info), local_addr=("0.0.0.0", self.port), allow_broadcast=True)
            log.info("LAN discovery responder listening on UDP %d", self.port)
        except OSError as exc:
            log.warning("LAN discovery could not start on UDP %d: %s", self.port, exc)

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()


def discover(port: int, timeout_s: float = 2.0, targets: tuple[str, ...] = ("255.255.255.255",)) -> list[dict[str, Any]]:
    """Broadcast a probe and collect coordinator replies (blocking, used by the CLI)."""
    results: dict[str, dict[str, Any]] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
        for t in targets:
            try:
                s.sendto(PROBE, (t, port))
            except OSError as exc:
                log.debug("discovery probe to %s failed: %s", t, exc)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                info = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(info, dict) and info.get("service") == "homemesh" and isinstance(info.get("url"), str):
                info["responder"] = addr[0]
                results[info["url"]] = info
    return list(results.values())
