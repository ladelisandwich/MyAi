"""A minimal HTTP/1.1 test server (optionally HTTPS) for tests that need a real socket."""

import asyncio
import ssl


class Server:
    """Minimal HTTP/1.1 server: routes path -> (status, headers, body). Records request headers."""

    def __init__(self, routes, ssl_ctx=None):
        self.routes = routes
        self.ssl_ctx = ssl_ctx
        self.requests: list[dict[str, str]] = []
        self.port = 0
        self._server = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self.ssl_ctx)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        _method, target, _ = lines[0].split(" ", 2)
        headers = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
        self.requests.append({"target": target, **headers})
        path = target.split("?", 1)[0]
        status, extra, body = self.routes.get(path, (404, {}, b"not found"))
        if callable(body):
            body = body(target)
        reason = {200: "OK", 301: "Moved", 302: "Found", 403: "Forbidden", 404: "Not Found"}.get(status, "X")
        out = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}", "Connection: close"]
        out += [f"{k}: {v}" for k, v in extra.items()]
        writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body)
        try:
            await writer.drain()
        finally:
            writer.close()
