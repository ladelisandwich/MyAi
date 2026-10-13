"""URL validation for the web tools.

Two separate questions are answered here:

* Is this a well-formed public ``http(s)`` link that is safe to hand to a browser or an HTTP
  client? (:func:`normalize_url`) Anything else (``file:``, ``javascript:``, Windows paths,
  user names/passwords in the link, control characters) is refused.
* Is this IP address on the public internet? (:func:`is_public_address`) ``fetch_url`` only
  connects to such addresses, so a model (or a web page talking to it) can never reach the
  router, Ollama, other HomeMesh computers or cloud metadata services.

The known-URL helpers (:func:`url_key`, :func:`extract_urls`) let the coordinator ask for
approval before fetching an address that neither the user nor a web result supplied, which
would otherwise be a way to send private data out in a URL.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit

import idna

MAX_URL_LENGTH = 2048
_DEFAULT_PORTS = {"http": 80, "https": 443}
# NAT64 well-known prefix (RFC 6052): 64:ff9b::a.b.c.d reaches IPv4 a.b.c.d.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# Special-purpose ranges (IANA registries), listed explicitly because ``ipaddress.is_global`` differs
# between Python builds (e.g. Ubuntu's 3.12.3 calls 3fff::/20 global). Checked in addition to it.
_DENY = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/96", "::ffff:0:0/96", "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "2002::/16",
    "3fff::/20", "5f00::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
)]
_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f\"<>\\^`{|}]")
# Characters left as they are when re-quoting path and query (already-encoded %XX stays intact).
_SAFE_PATH = "/%:@!$&'()*+,;=-._~"
_SAFE_QUERY = _SAFE_PATH + "?"
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")


class UnsafeURL(ValueError):
    """The link is malformed or not allowed (wrong scheme, credentials, local address...)."""


def normalize_url(url: str) -> str:
    """Return a canonical ``http(s)`` URL or raise :class:`UnsafeURL`.

    The result has a lower-case scheme and IDNA-encoded host, no user info, no fragment and
    only printable ASCII, so it can be passed to a browser or HTTP client without any part
    being interpreted as something other than a web address.
    """
    if not isinstance(url, str):
        raise UnsafeURL("the link must be text")
    url = url.strip()
    if not url:
        raise UnsafeURL("the link is empty")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURL(f"the link is longer than {MAX_URL_LENGTH} characters")
    # Percent-encode non-ASCII path/query characters but refuse spaces, quotes, backslashes and
    # control characters outright: browsers and the Windows shell treat those specially.
    if _FORBIDDEN_CHARS.search(url):
        raise UnsafeURL("the link contains spaces, quotes, backslashes or control characters")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURL(f"the link is malformed ({exc})") from exc
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise UnsafeURL("only http:// and https:// links are allowed")
    if not url.lower().startswith(scheme + "://"):
        raise UnsafeURL("the link must start with http:// or https://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURL("links with a user name or password are not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("the link has no host name")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("the link has an invalid port") from exc
    host = host.rstrip(".")
    if not host:
        raise UnsafeURL("the link has no host name")
    if "%" in host:
        raise UnsafeURL("the link has an invalid host name")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None:
        try:
            # IDNA 2008 with UTS #46 mapping, as browsers and httpx do ("faß.de" stays a different
            # name from "fass.de"; Python's built-in "idna" codec is the older IDNA 2003). Mapping
            # comes first, so fullwidth digits or zero-width characters can't hide an IP address.
            ascii_host = idna.encode(host, uts46=True).decode("ascii").lower().rstrip(".")
        except (idna.IDNAError, UnicodeError) as exc:
            raise UnsafeURL("the link has an invalid host name") from exc
        if re.fullmatch(r"[0-9a-fx.]+", ascii_host) and re.search(r"\d", ascii_host.rsplit(".", 1)[-1]):
            # Browsers read "2130706433", "0x7f.1" or "127.1" as IPv4 addresses (WHATWG URL IPv4
            # parser); write them out so the approval card shows the address that is really used.
            try:
                ip = ipaddress.IPv4Address(socket.inet_aton(ascii_host))
            except OSError as exc:
                raise UnsafeURL("the link has an invalid host name") from exc
        elif not ascii_host or len(ascii_host) > 253 or not all(
                _LABEL.fullmatch(label) for label in ascii_host.split(".")):
            raise UnsafeURL("the link has an invalid host name")
    if ip is not None:
        host_out = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    else:
        host_out = ascii_host
    netloc = host_out if port in (None, _DEFAULT_PORTS[scheme]) else f"{host_out}:{port}"
    path = quote(parts.path or "/", safe=_SAFE_PATH)
    query = quote(parts.query, safe=_SAFE_QUERY)
    out = urlunsplit((scheme, netloc, path, query, ""))
    if len(out) > MAX_URL_LENGTH:
        raise UnsafeURL(f"the link is longer than {MAX_URL_LENGTH} characters")
    return out


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_address(address: str) -> bool:
    """True only for globally routable unicast addresses.

    Loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7), link-local (169.254/16 including
    cloud metadata, fe80::/10), carrier-grade NAT/Tailscale (100.64/10), multicast, reserved,
    documentation and benchmarking ranges are all refused. IPv4 addresses wrapped in IPv6
    (mapped, NAT64) are judged by the IPv4 address inside; Teredo and 6to4 are refused.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.teredo is not None or ip.sixtofour is not None:
            return False  # tunnelling prefixes (Teredo, deprecated 6to4) are never needed for a web page
        inner = _embedded_ipv4(ip)
        if inner is not None:
            ip = inner
    if any(ip.version == net.version and ip in net for net in _DENY):
        return False
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return False
    return ip.is_global


def host_label(url: str) -> str:
    return urlsplit(url).hostname or url


def url_key(url: str) -> str | None:
    """Comparison key for "has this address been seen in the conversation?".

    Only the scheme (http/https), a leading ``www.``, one trailing slash and the fragment are
    ignored. Host, port, the rest of the path and the query must match exactly, so a model cannot
    hide data in a known link (extra path, query, or a run of slashes).
    """
    try:
        norm = normalize_url(url)
    except UnsafeURL:
        return None
    parts = urlsplit(norm)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path[:-1] if parts.path.endswith("/") else parts.path
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


# A link ends at whitespace, ASCII quotes/angle brackets, or Unicode punctuation that surrounds or
# ends text in many languages: CJK full stop, comma and brackets, curly and low quotes, guillemets.
_STOP = "\\s<>\"'`\u3002\uff0c\u3001\u300c\u300d\u300e\u300f\uff08\uff09\u201c\u201d\u201e\u201f\u00ab\u00bb\u2039\u203a"
_URL_RE = re.compile(rf"https?://[^{_STOP}]+", re.IGNORECASE)
# Bare domains such as "example.com/page" typed by the user (TLD of 2+ letters).
_BARE_RE = re.compile(rf"(?<![\w@./-])(?:[a-z0-9](?:[a-z0-9-]{{0,62}}[a-z0-9])?\.)+[a-z]{{2,63}}(?:/[^{_STOP}]*)?",
                      re.IGNORECASE)
_TRAILING = ".,;:!?*"
_PAIRS = {")": "(", "]": "[", "}": "{"}


def _trim(raw: str) -> str:
    """Drop sentence punctuation after a link, and a closing bracket only if it has no partner in the
    link (so ``https://en.wikipedia.org/wiki/Python_(programming_language)`` stays whole)."""
    while raw:
        last = raw[-1]
        if last in _TRAILING:
            raw = raw[:-1]
        elif last in _PAIRS and raw.count(last) > raw.count(_PAIRS[last]):
            raw = raw[:-1]
        else:
            break
    return raw


def extract_urls(text: str, limit: int = 200) -> list[str]:
    """Links (with or without ``http(s)://``) mentioned in free text, normalised."""
    out: list[str] = []
    if not text:
        return out
    found = [m.group(0) for m in _URL_RE.finditer(text)]
    rest = _URL_RE.sub(" ", text)
    found += ["https://" + m.group(0) for m in _BARE_RE.finditer(rest)]
    for raw in found:
        try:
            out.append(normalize_url(_trim(raw)))
        except UnsafeURL:
            continue
        if len(out) >= limit:
            break
    return out
