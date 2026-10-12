"""HTTP hardening middleware: request size limits, security headers, rate limiting, client IP."""

from __future__ import annotations

import ipaddress
import time
from collections import defaultdict, deque
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from homemesh.common.errors import RateLimited

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"
)


class BodySizeLimitMiddleware:
    """Rejects bodies over the limit with 413, using Content-Length when present and counting
    streamed bytes otherwise. Upload endpoints get a separate (larger) limit."""

    def __init__(self, app: ASGIApp, max_bytes: int, upload_max_bytes: int, upload_prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.upload_max_bytes = upload_max_bytes
        self.upload_prefixes = upload_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        limit = self.upload_max_bytes if path.startswith(self.upload_prefixes) else self.max_bytes
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await _send_413(send, limit)
                        return
                except ValueError:
                    pass
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    raise _TooLarge()
            return msg

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await _send_413(send, limit)


class _TooLarge(Exception):
    pass


async def _send_413(send: Send, limit: int) -> None:
    body = (b'{"error":{"code":"request_too_large","message":"Request body exceeds the limit of '
            + str(limit).encode() + b' bytes."}}')
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), geolocation=(), microphone=(self)"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"content-security-policy", CSP.encode()),
                ]
                if self.hsts:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                for k, v in extra:
                    if k not in existing:
                        headers.append((k, v))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RateLimiter:
    """Sliding-window limiter keyed by (bucket, key). In-memory, which is fine for a single
    coordinator; limits reset on restart."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, bucket: str, key: str, limit: int, window_s: float = 60.0) -> None:
        now = time.monotonic()
        q = self._hits[(bucket, key)]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            retry = int(window_s - (now - q[0])) + 1
            raise RateLimited(f"Too many attempts. Try again in {retry} seconds.",
                              details={"retry_after_s": retry})
        q.append(now)

    def reset(self, bucket: str, key: str) -> None:
        self._hits.pop((bucket, key), None)


def client_ip(scope_or_request: Any, trusted_proxies: list[str]) -> str:
    """Client IP, honouring X-Forwarded-For only when the direct peer is a trusted proxy."""
    client = getattr(scope_or_request, "client", None)
    peer = client.host if client else "unknown"
    if not trusted_proxies:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
        trusted = any(peer_ip in ipaddress.ip_network(p, strict=False) for p in trusted_proxies)
    except ValueError:
        trusted = False
    if trusted:
        fwd = scope_or_request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer
