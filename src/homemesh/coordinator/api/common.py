"""Helpers shared by API routers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from homemesh.coordinator.middleware import client_ip
from homemesh.coordinator.state import CoordinatorState

SSE_KEEPALIVE_S = 15.0


def state(request: Request) -> CoordinatorState:
    return request.app.state.hm


def ip_of(request: Request) -> str:
    st = state(request)
    return client_ip(request, st.cfg.server.trusted_proxies)


def sse_format(event: str, data: Any, event_id: str | None = None) -> bytes:
    payload = json.dumps(data, default=str, separators=(",", ":"))
    out = ""
    if event_id:
        out += f"id: {event_id}\n"
    out += f"event: {event}\ndata: {payload}\n\n"
    return out.encode("utf-8")


async def _with_keepalive(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Interleave SSE comment keep-alives so proxies and browsers do not drop idle streams."""
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for chunk in source:
                await queue.put(chunk)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
            except TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if item is None:
                return
            yield item
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def sse_response(source: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(_with_keepalive(source), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})
