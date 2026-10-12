"""Live event stream for the UI (Server-Sent Events)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request

from homemesh.coordinator.api.common import sse_format, sse_response, state
from homemesh.coordinator.auth import Principal, require

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.get("/events")
async def events(request: Request, principal: Principal = Depends(require("read"))) -> Any:
    st = state(request)
    sub = st.bus.subscribe(principal.user_id)

    async def gen() -> AsyncIterator[bytes]:
        try:
            yield sse_format("hello", {"user": principal.username})
            async for ev in sub:
                if await request.is_disconnected():
                    return
                yield sse_format(ev["type"], {**ev["data"], "ts": ev["ts"]})
        finally:
            sub.close()

    return sse_response(gen())
