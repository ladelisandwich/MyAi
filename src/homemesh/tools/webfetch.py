"""Fetch public web pages without letting a model reach the home network.

Every hop (the first request and each redirect) is checked the same way:

1. the link must be a plain ``http(s)`` URL (:func:`normalize_url`);
2. the host name is resolved once and **every** address it resolves to must be public
   (:func:`is_public_address`), so a name cannot point at the router, Ollama, another HomeMesh
   computer or a cloud metadata service;
3. the connection goes to exactly the address that was checked (the URL is rewritten to the
   IP, with the original ``Host`` header and TLS server name), so a DNS answer that changes
   between the check and the connection ("DNS rebinding") cannot slip through. TLS certificates
   are still verified against the real host name.

Each hop uses a fresh client so no connection (and no TLS session for another name) is reused.
Downloads are capped in size and time; proxies from the environment are ignored.
"""

from __future__ import annotations

import asyncio
import codecs
import re
import socket
import ssl
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx

from homemesh.tools.urlsafety import UnsafeURL, is_public_address, normalize_url

Resolver = Callable[[str, int], Awaitable[list[str]]]
REDIRECT_CODES = {301, 302, 303, 307, 308}


class FetchError(ValueError):
    """The page could not be fetched; the message is safe to show to the user and the model."""


class BlockedAddress(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    reason: str
    content_type: str
    charset: str | None
    body: bytes
    truncated: bool
    redirects: list[str] = field(default_factory=list)


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"could not find the address of {host} ({exc.strerror or exc})") from exc
    out: list[str] = []
    for *_, sockaddr in infos:
        ip = str(sockaddr[0]).split("%", 1)[0]
        if ip not in out:
            out.append(ip)
    return out


def _netloc_for(ip: str, port: int | None) -> str:
    host = f"[{ip}]" if ":" in ip else ip
    return f"{host}:{port}" if port else host


def _host_header(parts: SplitResult) -> str:
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port else host


async def fetch_public(url: str, *, timeout_s: float = 20.0, max_bytes: int = 5 * 2**20, user_agent: str,
                       accept: str = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                       max_redirects: int = 5, resolver: Resolver | None = None,
                       address_allowed: Callable[[str], bool] = is_public_address,
                       verify: ssl.SSLContext | bool = True) -> FetchResult:
    resolve = resolver or system_resolver
    try:
        current = normalize_url(url)
    except UnsafeURL as exc:
        raise FetchError(str(exc)) from exc
    first = current
    redirects: list[str] = []
    for _hop in range(max_redirects + 1):
        parts = urlsplit(current)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port < 1024 and port not in (80, 443):
            raise BlockedAddress(f"port {port} is not a web port; HomeMesh only fetches web pages")
        try:
            addresses = await asyncio.wait_for(resolve(host, port), timeout_s)
        except TimeoutError as exc:
            raise FetchError(f"looking up {host} took longer than {timeout_s:.0f} seconds") from exc
        if not addresses:
            raise FetchError(f"could not find the address of {host}")
        blocked = [a for a in addresses if not address_allowed(a)]
        if blocked:
            raise BlockedAddress(
                f"{host} points to a private or local network address ({blocked[0]}). HomeMesh only fetches "
                "pages on the public internet, never your home network.")
        last_error: Exception | None = None
        for ip in addresses:
            target = urlunsplit((parts.scheme, _netloc_for(ip, parts.port), parts.path or "/", parts.query, ""))
            extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
            headers = {"Host": _host_header(parts), "User-Agent": user_agent, "Accept": accept,
                       "Accept-Language": "en;q=0.9, *;q=0.5", "Accept-Encoding": "gzip, deflate"}
            try:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, verify=verify,
                                             timeout=httpx.Timeout(timeout_s)) as client:
                    request = client.build_request("GET", target, headers=headers, extensions=extensions)
                    response = await client.send(request, stream=True)
                    try:
                        if response.status_code in REDIRECT_CODES and response.headers.get("location"):
                            try:
                                nxt = normalize_url(urljoin(current, response.headers["location"]))
                            except ValueError as exc:  # includes UnsafeURL
                                raise FetchError(f"the page redirected to a link that is not allowed ({exc})") from exc
                            redirects.append(nxt)
                            current = nxt
                            break
                        body, truncated = await _read_bounded(response, max_bytes)
                        ctype = response.headers.get("content-type", "")
                        return FetchResult(url=first, final_url=current, status=response.status_code,
                                           reason=response.reason_phrase, content_type=ctype.split(";")[0].strip().lower(),
                                           charset=_charset(ctype), body=bytes(body), truncated=truncated,
                                           redirects=redirects)
                    finally:
                        await response.aclose()
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                continue
            except httpx.TimeoutException as exc:
                raise FetchError(f"{host} did not answer within {timeout_s:.0f} seconds") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"could not fetch {host}: {_describe(exc)}") from exc
        else:
            raise FetchError(f"could not connect to {host}: {_describe(last_error)}")
    raise FetchError(f"too many redirects (more than {max_redirects})")


class _Inflater:
    """Bounded gzip/deflate decoder. httpx's own decoders inflate each network chunk completely,
    so a few hundred KB of compressed zeros would become hundreds of MB in memory before any size
    check; here no call ever produces more than the bytes still allowed."""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self._first = True
        wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
        self._d = zlib.decompressobj(wbits)

    def feed(self, data: bytes, limit: int) -> bytes:
        if self._first and self.encoding == "deflate" and data:
            self._first = False
            # "deflate" is zlib-wrapped by the standard, but some servers send raw deflate.
            if (data[0] & 0x0F) != 8 or (data[0] << 8 | (data[1] if len(data) > 1 else 0)) % 31:
                self._d = zlib.decompressobj(-zlib.MAX_WBITS)
        out = bytearray()
        pending = data
        while len(out) < limit:
            chunk = self._d.decompress(pending, limit - len(out))
            out += chunk
            pending = self._d.unconsumed_tail
            if not pending or not chunk:
                break
        return bytes(out)


async def _read_bounded(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """The response body, decoded, at most ``max_bytes`` (also at most that many bytes on the wire)."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding in ("", "identity"):
        inflater = None
    elif encoding in ("gzip", "x-gzip", "deflate"):
        inflater = _Inflater("deflate" if encoding == "deflate" else "gzip")
    else:
        raise FetchError(f"the site sent the page in an unsupported encoding ({encoding[:40]})")
    body = bytearray()
    wire = 0
    try:
        async for raw in response.aiter_raw():
            wire += len(raw)
            body += inflater.feed(raw, max_bytes - len(body)) if inflater else raw[:max_bytes - len(body)]
            if len(body) >= max_bytes or wire >= max_bytes:
                return bytes(body[:max_bytes]), True
    except zlib.error as exc:
        raise FetchError(f"the page's compressed data is damaged ({exc})") from exc
    return bytes(body), False


def _describe(exc: BaseException | None) -> str:
    if exc is None:
        return "no address worked"
    text = str(exc) or type(exc).__name__
    if "CERTIFICATE_VERIFY_FAILED" in text or isinstance(exc.__cause__, ssl.SSLCertVerificationError):
        return "its HTTPS certificate is not valid for this name (HomeMesh never skips certificate checks)"
    return text[:300]


def _charset(content_type: str) -> str | None:
    m = re.search(r"charset\s*=\s*\"?([\w.:-]+)", content_type, re.I)
    return m.group(1) if m else None


def decode_body(body: bytes, charset: str | None, html: bool) -> str:
    """Decode with the declared charset, a ``<meta charset>`` in the first bytes, or UTF-8."""
    candidates: list[str] = []
    if charset:
        candidates.append(charset)
    if html:
        m = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", body[:4096], re.I)
        if m:
            candidates.append(m.group(1).decode("ascii", "ignore"))
    if body.startswith(codecs.BOM_UTF8):
        candidates.insert(0, "utf-8-sig")
    candidates.append("utf-8")
    for enc in candidates:
        try:
            codecs.lookup(enc)
        except LookupError:
            continue
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")
