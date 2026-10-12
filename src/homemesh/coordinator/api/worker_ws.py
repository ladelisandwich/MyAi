"""WebSocket endpoint that workers connect to (authentication happens in NodeManager)."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from homemesh.coordinator.middleware import client_ip

router = APIRouter()


@router.websocket("/api/v1/worker/ws")
async def worker_ws(ws: WebSocket) -> None:
    st = ws.app.state.hm
    await st.nodes.handle_websocket(ws, client_ip(ws, st.cfg.server.trusted_proxies))
