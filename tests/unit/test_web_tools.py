"""Internet tools: URL safety, SSRF-proof fetching, SearXNG search and opening links."""

import asyncio
import json
import ssl
import sys
from functools import partial

import pytest

from homemesh.common.config import WebSection
from homemesh.protocol.jobs import ToolExecPayload
from homemesh.tools import desktop, webfetch
from homemesh.tools.base import ToolContext
from homemesh.tools.builtin import web
from homemesh.tools.htmltext import html_to_text
from homemesh.tools.registry import ToolArgumentError, ToolRegistry
from homemesh.tools.urlsafety import UnsafeURL, extract_urls, is_public_address, normalize_url, url_key
from homemesh.tools.webfetch import BlockedAddress, FetchError, fetch_public
from httpstub import Server

# --------------------------------------------------------------------------- URL rules


@pytest.mark.parametrize(("raw", "expected"), [
    ("https://www.YouTube.com", "https://www.youtube.com/"),
    ("HTTP://Example.com:80/a?b=1#frag", "http://example.com/a?b=1"),
    ("https://example.com:8443/x", "https://example.com:8443/x"),
    ("https://de.wikipedia.org/wiki/Köln", "https://de.wikipedia.org/wiki/K%C3%B6ln"),
    ("https://bücher.de/", "https://xn--bcher-kva.de/"),
    ("https://faß.de/", "https://xn--fa-hia.de/"),  # IDNA 2008, as browsers do (not "fass.de")
    ("http://2130706433/", "http://127.0.0.1/"),  # browsers read these as IPv4 addresses
    ("http://0x7f.1/", "http://127.0.0.1/"),
    ("http://[::1]:8080/x", "http://[::1]:8080/x"),
])
def test_normalize_url_accepts_and_canonicalises(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("http://0x7f.1\u200b:11434/api/tags", "http://127.0.0.1:11434/api/tags"),  # zero-width space
    ("http://0xc0.0xa8.1.1\u00ad/admin", "http://192.168.1.1/admin"),  # soft hyphen
    ("http://\uff11\uff12\uff17.0.0.1/", "http://127.0.0.1/"),  # fullwidth digits
    ("http://127\u30020.0.1/", "http://127.0.0.1/"),  # ideographic full stop
])
def test_hidden_ip_addresses_are_written_out_and_normalising_is_stable(raw, expected):
    assert normalize_url(raw) == expected
    assert normalize_url(expected) == expected


@pytest.mark.parametrize("raw", [
    "youtube.com", "file:///C:/Windows/System32/calc.exe", "javascript:alert(1)", "ms-settings:", "search-ms:query=x",
    "https:example.com", "https://user:pw@example.com/", "https://example.com\\@evil.com", "\\\\server\\share",
    " https://a b.com", "https://example.com/a b", "https://example.com/\x00", "https://-bad.com/", "http://999.1.1.1/",
    "https://fe80::1%25eth0/", "https://" + "a" * 2100 + ".com", "",
])
def test_normalize_url_refuses_anything_but_plain_web_links(raw):
    with pytest.raises(UnsafeURL):
        normalize_url(raw)


@pytest.mark.parametrize(("address", "public"), [
    ("8.8.8.8", True), ("93.184.215.14", True), ("2606:4700::1111", True), ("::ffff:8.8.8.8", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.50.187", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("255.255.255.255", False),
    ("224.0.0.1", False), ("192.0.2.1", False), ("198.18.0.1", False), ("::1", False), ("fe80::1", False),
    ("fc00::1", False), ("fec0::1", False), ("::ffff:127.0.0.1", False), ("::ffff:192.168.1.1", False),
    ("64:ff9b::7f00:1", False), ("2002:7f00:1::", False), ("2001::1", False), ("not-an-ip", False),
    ("::127.0.0.1", False), ("64:ff9b::a9fe:a9fe", False), ("64:ff9b::808:808", True), ("64:ff9b:1::a00:1", False),
    ("ff02::1", False), ("ff0e::1", False), ("fd7a:115c:a1e0::1", False), ("240.0.0.1", False),
    ("2001:db8::1", False), ("100.100.100.200", False), ("fe80::1%eth0", False), ("2002:808:808::", False),
    ("2127.0.0.1", False), ("127.1", False), ("3fff::1", False), ("5f00::1", False), ("::ffff:100.64.0.1", False),
    ("2a00:1450:4001:80b::200e", True),
])
def test_only_public_addresses_are_public(address, public):
    assert is_public_address(address) is public


def test_url_key_ignores_scheme_www_and_one_trailing_slash_but_nothing_else():
    assert url_key("https://www.youtube.com/") == url_key("http://youtube.com") == "youtube.com"
    assert url_key("https://example.com/page") == url_key("https://example.com/page/")
    assert url_key("https://example.com/page") != url_key("https://example.com/page" + "/" * 9)  # no slash code
    assert url_key("https://example.com/a?x=1") != url_key("https://example.com/a?x=1&leak=secret")
    assert url_key("https://example.com/a") != url_key("https://example.com/a/secret")
    assert url_key("javascript:alert(1)") is None


def test_extract_urls_finds_full_and_bare_links():
    text = "open youtube.com and read https://example.org/a?b=1). Mail me at foo@bar.com, e.g. www.bbc.co.uk/news."
    assert extract_urls(text) == ["https://example.org/a?b=1", "https://youtube.com/", "https://www.bbc.co.uk/news"]


@pytest.mark.parametrize(("text", "url"), [
    ("Read https://en.wikipedia.org/wiki/Python_(programming_language) please",
     "https://en.wikipedia.org/wiki/Python_(programming_language)"),
    ("(see https://a.example/x).", "https://a.example/x"),
    ("see https://example.com/__init__", "https://example.com/__init__"),
    ("\u8bf7\u9605\u8bfb https://example.com/page\u3002\u8c22\u8c22", "https://example.com/page"),
    ("\u201ehttps://example.de/seite\u201c", "https://example.de/seite"),
    ("x [link](https://md.example/p) y", "https://md.example/p"),
])
def test_extract_urls_keeps_the_link_the_user_meant(text, url):
    assert extract_urls(text) == [url]


def test_html_to_text_keeps_content_and_links_drops_scripts_and_chrome():
    html = """<html><head><title> My  Page </title><meta name="description" content="About things">
    <script>alert("x")</script><style>p{}</style></head><body>
    <nav>Home | About <a href="/about">About</a></nav>
    <h1>Hello</h1><p>First&nbsp;paragraph with <a href="https://other.example/x">a link</a>.</p>
    <div hidden>secret hidden text</div><ul><li>one</li><li>two</li></ul>
    <a href="javascript:evil()">bad</a><a href="#top">top</a>
    <footer>Copyright</footer></body></html>"""
    page = html_to_text(html, "https://site.example/dir/page")
    assert page.title == "My Page" and page.description == "About things"
    assert "# Hello" in page.text and "First paragraph with a link." in page.text
    assert "- one" in page.text and "- two" in page.text
    for gone in ("alert", "secret hidden", "Copyright", "Home | About"):
        assert gone not in page.text
    assert [lk["url"] for lk in page.links] == ["https://site.example/about", "https://other.example/x"]


def test_html_to_text_survives_broken_markup():
    page = html_to_text("<div><p>unclosed <b>bold <i>text</div></span><p>after", "https://x.example/")
    assert "unclosed bold text" in page.text and "after" in page.text


# --------------------------------------------------------------------------- SSRF-safe fetching


def fake_dns(table):
    async def resolve(host, port):
        if host not in table:
            raise FetchError(f"could not find the address of {host}")
        return table[host]
    return resolve


def loopback_is_public(ip):
    """Test stand-in for is_public_address: treat the local test server as a public site."""
    return ip == "127.0.0.1"


UA = "HomeMesh-test"


async def test_fetch_refuses_local_addresses_with_the_real_policy():
    async with Server({"/": (200, {"Content-Type": "text/plain"}, b"router admin")}) as s:
        with pytest.raises(BlockedAddress, match="private or local network"):
            await fetch_public(f"http://127.0.0.1:{s.port}/", user_agent=UA)
        with pytest.raises(BlockedAddress):
            await fetch_public(f"http://localhost:{s.port}/", user_agent=UA)
        assert s.requests == []  # never even connected


async def test_fetch_connects_to_the_checked_address_with_the_real_host_header():
    async with Server({"/page": (200, {"Content-Type": "text/html; charset=utf-8"}, b"<p>hello</p>")}) as s:
        res = await fetch_public(f"http://site.test:{s.port}/page?q=1", user_agent=UA,
                                 resolver=fake_dns({"site.test": ["127.0.0.1"]}), address_allowed=loopback_is_public)
    assert res.status == 200 and res.body == b"<p>hello</p>" and res.charset == "utf-8"
    assert res.final_url == f"http://site.test:{s.port}/page?q=1"
    assert s.requests[0]["host"] == f"site.test:{s.port}" and s.requests[0]["target"] == "/page?q=1"
    assert s.requests[0]["user-agent"] == UA


async def test_fetch_refuses_a_name_with_any_private_answer():
    async with Server({}) as s:
        dns = fake_dns({"mixed.test": ["127.0.0.1", "10.0.0.7"]})
        with pytest.raises(BlockedAddress, match=r"10\.0\.0\.7"):
            await fetch_public(f"http://mixed.test:{s.port}/", user_agent=UA, resolver=dns,
                               address_allowed=loopback_is_public)
        assert s.requests == []


async def test_every_redirect_is_checked_again():
    def routes(port):
        return {
            "/to-private": (302, {"Location": f"http://evil.test:{port}/admin"}, b""),
            "/to-file": (302, {"Location": "file:///etc/passwd"}, b""),
            "/loop": (301, {"Location": "/loop"}, b""),
            "/ok": (302, {"Location": "/final"}, b""),
            "/final": (200, {"Content-Type": "text/plain"}, b"done"),
        }
    async with Server({}) as s:
        s.routes = routes(s.port)
        dns = fake_dns({"site.test": ["127.0.0.1"], "evil.test": ["192.168.1.1"]})
        kw = {"user_agent": UA, "resolver": dns, "address_allowed": loopback_is_public}
        with pytest.raises(BlockedAddress, match=r"192\.168\.1\.1"):
            await fetch_public(f"http://site.test:{s.port}/to-private", **kw)
        with pytest.raises(FetchError, match="not allowed"):
            await fetch_public(f"http://site.test:{s.port}/to-file", **kw)
        with pytest.raises(FetchError, match="too many redirects"):
            await fetch_public(f"http://site.test:{s.port}/loop", max_redirects=3, **kw)
        res = await fetch_public(f"http://site.test:{s.port}/ok", **kw)
        assert res.body == b"done" and res.redirects == [f"http://site.test:{s.port}/final"]
        assert not any(r["target"] == "/admin" for r in s.requests)


async def test_download_size_is_capped():
    async with Server({"/big": (200, {"Content-Type": "text/plain"}, b"x" * 50_000)}) as s:
        res = await fetch_public(f"http://site.test:{s.port}/big", user_agent=UA, max_bytes=1000,
                                 resolver=fake_dns({"site.test": ["127.0.0.1"]}), address_allowed=loopback_is_public)
    assert res.truncated and len(res.body) == 1000


async def test_compressed_pages_are_decoded_within_the_size_limit():
    import gzip
    import zlib

    page = b"<p>" + b"hello " * 2000 + b"</p>"
    bomb = gzip.compress(b"\0" * 50_000_000)  # ~50 KB on the wire, 50 MB inflated
    routes = {
        "/gz": (200, {"Content-Type": "text/html", "Content-Encoding": "gzip"}, gzip.compress(page)),
        "/zlib": (200, {"Content-Type": "text/html", "Content-Encoding": "deflate"}, zlib.compress(page)),
        "/raw-deflate": (200, {"Content-Type": "text/html", "Content-Encoding": "deflate"},
                         zlib.compress(page, wbits=-15)),
        "/bomb": (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, bomb),
        "/double": (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip, gzip"}, gzip.compress(gzip.compress(b"x"))),
        "/br": (200, {"Content-Type": "text/plain", "Content-Encoding": "br"}, b"\x0b\x00\x80hello\x03"),
    }
    async with Server(routes) as s:
        kw = {"user_agent": UA, "resolver": fake_dns({"site.test": ["127.0.0.1"]}), "address_allowed": loopback_is_public}
        for path in ("/gz", "/zlib", "/raw-deflate"):
            res = await fetch_public(f"http://site.test:{s.port}{path}", **kw)
            assert res.body == page and not res.truncated, path
        import tracemalloc

        tracemalloc.start()
        try:
            res = await fetch_public(f"http://site.test:{s.port}/bomb", max_bytes=100_000, **kw)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert res.truncated and len(res.body) == 100_000 and set(res.body) == {0}
        assert peak < 10_000_000, f"inflating used {peak} bytes"  # never the 50 MB the bomb expands to
        for path in ("/double", "/br"):
            with pytest.raises(FetchError, match="unsupported encoding"):
                await fetch_public(f"http://site.test:{s.port}{path}", **kw)
    assert "gzip, deflate" in s.requests[0]["accept-encoding"]


async def test_only_web_ports_are_fetched():
    dns = fake_dns({"site.test": ["127.0.0.1"]})
    for port in (22, 25, 445):
        with pytest.raises(BlockedAddress, match=f"port {port}"):
            await fetch_public(f"http://site.test:{port}/", user_agent=UA, resolver=dns,
                               address_allowed=loopback_is_public)


def _tls_contexts(tmp_path, names):
    from homemesh.coordinator.tls import _create_ca, _create_leaf

    ca, ca_key = _create_ca(tmp_path)
    _create_leaf(tmp_path, ca, ca_key, names)
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(tmp_path / "server.pem", tmp_path / "server.key")
    client = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    return server, client


async def test_https_verifies_the_certificate_against_the_host_name_not_the_ip(tmp_path):
    server_ctx, client_ctx = _tls_contexts(tmp_path, ["site.test"])
    dns = fake_dns({"site.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]})
    async with Server({"/": (200, {"Content-Type": "text/plain"}, b"secure")}, ssl_ctx=server_ctx) as s:
        res = await fetch_public(f"https://site.test:{s.port}/", user_agent=UA, resolver=dns,
                                 address_allowed=loopback_is_public, verify=client_ctx)
        assert res.body == b"secure"
        # Same server, same IP, but a name the certificate does not cover: refused.
        with pytest.raises(FetchError, match="certificate"):
            await fetch_public(f"https://other.test:{s.port}/", user_agent=UA, resolver=dns,
                               address_allowed=loopback_is_public, verify=client_ctx)


# --------------------------------------------------------------------------- fetch_url tool


@pytest.fixture
def local_fetch(monkeypatch):
    """Let the fetch_url tool reach the test server as if it were a public site."""
    dns = fake_dns({"site.test": ["127.0.0.1"]})
    monkeypatch.setattr(web, "fetch_public", partial(fetch_public, resolver=dns, address_allowed=loopback_is_public))


async def _run_tool(tool, args, ctx=None):
    parsed = ToolRegistry.validate_args(tool, args)
    return await tool.handler(ctx or ToolContext(web=WebSection()), parsed)


async def test_fetch_url_tool_returns_readable_text_and_links(local_fetch):
    html = (b"<html><head><title>Recipe</title></head><body><article><h2>Pancakes</h2><p>Mix flour and milk.</p>"
            b"<a href='/more'>More recipes</a></article></body></html>")
    async with Server({"/r": (200, {"Content-Type": "text/html"}, html),
                       "/img": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
                       "/missing": (404, {"Content-Type": "text/html"}, b"nope")}) as s:
        out = await _run_tool(web.FETCH_URL, {"url": f"http://site.test:{s.port}/r"})
        assert out["title"] == "Recipe" and "## Pancakes" in out["content"] and "Mix flour" in out["content"]
        assert out["links"] == [{"text": "More recipes", "url": f"http://site.test:{s.port}/more"}]
        with pytest.raises(FetchError, match="image/png"):
            await _run_tool(web.FETCH_URL, {"url": f"http://site.test:{s.port}/img"})
        with pytest.raises(FetchError, match="HTTP 404"):
            await _run_tool(web.FETCH_URL, {"url": f"http://site.test:{s.port}/missing"})


async def test_fetch_url_reads_pdfs(local_fetch):
    from pdfutil import make_pdf

    async with Server({"/doc.pdf": (200, {"Content-Type": "application/pdf"}, make_pdf(["Dishwasher warranty terms",
                                                                                "The warranty is valid for two years."]))}) as s:
        out = await _run_tool(web.FETCH_URL, {"url": f"http://site.test:{s.port}/doc.pdf"})
    assert "valid for two years" in out["content"]


async def test_fetch_url_tool_uses_the_real_ssrf_policy_by_default():
    with pytest.raises(BlockedAddress):
        await _run_tool(web.FETCH_URL, {"url": "http://127.0.0.1:9/"})


# --------------------------------------------------------------------------- web_search tool


SEARX_JSON = {
    "query": "pancakes", "number_of_results": 0,
    "results": [
        {"url": "https://recipes.example/pancakes", "title": "Best  pancakes", "content": "Fluffy\npancakes",
         "engine": "duckduckgo", "engines": ["duckduckgo", "brave"], "publishedDate": None},
        {"url": "javascript:alert(1)", "title": "evil", "content": "x", "engine": "bad"},
        {"url": "https://other.example/", "title": "Other", "content": "", "engine": "brave"},
    ],
    "answers": [], "infoboxes": [{"infobox": "Pancake", "content": "A flat cake", "id": "https://en.wikipedia.org/wiki/Pancake"}],
    "suggestions": ["crepes"], "unresponsive_engines": [["google", "CAPTCHA"]],
}


async def test_web_search_queries_searxng_json_and_cleans_results():
    async with Server({"/search": (200, {"Content-Type": "application/json"}, json.dumps(SEARX_JSON).encode())}) as s:
        ctx = ToolContext(web=WebSection(searxng_url=f"http://127.0.0.1:{s.port}"))
        out = await _run_tool(web.WEB_SEARCH, {"query": "pancakes", "time_range": "week"}, ctx)
    q = s.requests[0]["target"]
    assert "format=json" in q and "q=pancakes" in q and "time_range=week" in q and "safesearch=1" in q
    assert [r["url"] for r in out["results"]] == ["https://recipes.example/pancakes", "https://other.example/"]
    assert out["results"][0]["title"] == "Best pancakes" and out["results"][0]["snippet"] == "Fluffy pancakes"
    assert out["infobox"]["url"] == "https://en.wikipedia.org/wiki/Pancake"
    assert out["unresponsive_engines"] == ["google: CAPTCHA"] and out["suggestions"] == ["crepes"]
    assert set(web.result_urls("web_search", out)) == {"https://recipes.example/pancakes", "https://other.example/",
                                                       "https://en.wikipedia.org/wiki/Pancake"}
    page = {"url": "https://a.example/", "final_url": "https://a.example/home", "links": [{"url": "https://evil.example/"}]}
    assert web.result_urls("fetch_url", page) == ["https://a.example/", "https://a.example/home"]  # never page links


async def test_web_search_explains_setup_problems():
    with pytest.raises(web.SearchUnavailable, match="searxng_url is empty"):
        await _run_tool(web.WEB_SEARCH, {"query": "x"})
    async with Server({"/search": (403, {}, b"Forbidden")}) as s:
        ctx = ToolContext(web=WebSection(searxng_url=f"http://127.0.0.1:{s.port}"))
        with pytest.raises(web.SearchUnavailable, match=r"search\.formats"):
            await _run_tool(web.WEB_SEARCH, {"query": "x"}, ctx)
        port = s.port
    ctx = ToolContext(web=WebSection(searxng_url=f"http://127.0.0.1:{port}"))  # server gone
    with pytest.raises(web.SearchUnavailable, match="not reachable"):
        await _run_tool(web.WEB_SEARCH, {"query": "x"}, ctx)


# --------------------------------------------------------------------------- open_url tool


async def test_open_url_always_needs_approval_and_only_takes_web_links(monkeypatch):
    tool = web.OPEN_URL
    assert tool.requires_approval and tool.location == "node" and "open_url" not in WebSection.model_fields
    with pytest.raises(ToolArgumentError):
        ToolRegistry.validate_args(tool, {"url": "file:///C:/Windows/System32/calc.exe"})
    assert ToolRegistry.validate_args(tool, {"url": "https://www.YouTube.com"}).url == "https://www.youtube.com/"

    async def dns(host, port):
        return {"www.youtube.com": ["142.250.1.1"], "192.168.1.1.nip.io": ["192.168.1.1"]}[host]

    monkeypatch.setattr(web, "system_resolver", dns)
    note = web._open_url_note
    assert await note(ToolRegistry.validate_args(tool, {"url": "https://www.youtube.com"})) is None
    for local in ("http://192.168.1.1/", "http://router.lan/", "http://nas.local:5000/", "http://localhost:8080/",
                  "http://fritz.box/", "http://router.localdomain/", "http://192.168.1.1.nip.io/"):
        assert "your own network" in await note(ToolRegistry.validate_args(tool, {"url": local})), local


async def test_worker_refuses_open_url_without_an_approval(monkeypatch):
    from homemesh.common.config import WorkerSection
    from homemesh.tools.runtime import make_worker_tool_runner
    from homemesh.worker.policy import LocalPolicy

    opened = []

    async def fake_open(url):
        opened.append(url)
        return "test browser"

    monkeypatch.setattr(web, "open_in_browser", fake_open)
    policy = LocalPolicy(WorkerSection(allowed_tools=["open_url"]))
    run = make_worker_tool_runner(policy, web=WebSection())
    res = await run(ToolExecPayload(tool="open_url", arguments={"url": "https://www.youtube.com"}))
    assert not res.ok and "approval" in res.error and opened == []
    res = await run(ToolExecPayload(tool="open_url", arguments={"url": "https://www.youtube.com"}, approval_id="a1"))
    assert res.ok and res.output["opened"] == "https://www.youtube.com/" and opened == ["https://www.youtube.com/"]
    # Not allowed on this computer: refused even with an approval.
    run2 = make_worker_tool_runner(LocalPolicy(WorkerSection(allowed_tools=[])), web=WebSection())
    res = await run2(ToolExecPayload(tool="open_url", arguments={"url": "https://x.example"}, approval_id="a2"))
    assert not res.ok and "not allowed" in res.error


async def test_open_in_browser_on_windows_uses_the_shell_open_verb(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop.os, "startfile", lambda url: calls.append(url), raising=False)
    assert await desktop.open_in_browser("https://www.youtube.com/") == "Windows default browser"
    assert calls == ["https://www.youtube.com/"]
    with pytest.raises(ValueError):
        await desktop.open_in_browser("C:\\Windows\\System32\\calc.exe")
    assert calls == ["https://www.youtube.com/"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shell scripts as stand-ins for xdg-open/systemd-run")
async def test_open_in_browser_on_linux_uses_the_desktop_session(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    log = tmp_path / "opened.txt"
    opener = tmp_path / "xdg-open"
    opener.write_text(f'#!/bin/sh\nprintf "%s" "$*" >> "{log}"\nexit "${{FAKE_EXIT:-0}}"\n')
    opener.chmod(0o755)
    runner = tmp_path / "systemd-run"  # stand-in: records that it was used, then runs the command
    runner.write_text(f'#!/bin/sh\nprintf "via-user-manager " >> "{log}"\nwhile [ "$1" != "--" ]; do shift; done\n'
                      'shift\nexec "$@"\n')
    runner.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    # No display anywhere: say so.
    monkeypatch.setattr(desktop, "_user_manager_has_desktop", lambda: False)
    with pytest.raises(desktop.BrowserUnavailable, match="no graphical desktop"):
        await desktop.open_in_browser("https://example.com/")
    # Autostarted service without a display, desktop running: go through the user manager.
    monkeypatch.setattr(desktop, "_user_manager_has_desktop", lambda: True)
    assert await desktop.open_in_browser("https://example.com/x") == "default browser"
    assert log.read_text() == "via-user-manager https://example.com/x"
    # Started inside the desktop session: xdg-open directly.
    log.unlink()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert await desktop.open_in_browser("https://example.com/a?b=1") == "default browser"
    assert log.read_text() == "https://example.com/a?b=1"
    monkeypatch.setenv("FAKE_EXIT", "3")
    with pytest.raises(OSError, match="exit code 3"):
        await desktop.open_in_browser("https://example.com/")


def test_fetch_policy_refuses_this_computers_own_networks_even_with_global_ipv6(monkeypatch):
    import socket
    from types import SimpleNamespace

    addr = SimpleNamespace
    interfaces = {"eth0": [
        addr(family=socket.AF_INET, address="192.168.50.10", netmask="255.255.255.0"),
        addr(family=socket.AF_INET6, address="2a02:8109:b6c0:1d00::10", netmask="ffff:ffff:ffff:ffff::"),
        addr(family=socket.AF_INET6, address="fe80::1%eth0", netmask=None),
    ], "wan": [addr(family=socket.AF_INET, address="93.184.215.14", netmask="255.255.255.255")]}
    monkeypatch.setattr(webfetch.psutil, "net_if_addrs", lambda: interfaces)
    allowed = webfetch.default_address_policy()
    assert not allowed("2a02:8109:b6c0:1d00:3e7c:3fff:fe12:3456")  # a NAS on the same IPv6 /64
    assert not allowed("2a02:8109:b6c0:1d00::10")  # this computer
    assert not allowed("93.184.215.14")  # this computer's public address
    assert allowed("2606:4700::1111") and allowed("8.8.8.8")


async def test_malformed_redirects_slow_pages_and_split_deflate_headers():
    import zlib

    payload = zlib.compress(b"hello world " * 100)
    routes = {
        "/bad-redirect": (302, {"Location": "http:next-page"}, b""),
        "/drip": (200, {"Content-Type": "text/plain"}, [(0.3, b"x")] * 30),
        "/split": (200, {"Content-Type": "text/plain", "Content-Encoding": "deflate"},
                   [(0.0, payload[:1]), (0.2, payload[1:])]),
    }
    async with Server(routes) as s:
        kw = {"user_agent": UA, "resolver": fake_dns({"site.test": ["127.0.0.1"]}), "address_allowed": loopback_is_public}
        with pytest.raises(FetchError, match="malformed redirect"):
            await fetch_public(f"http://site.test:{s.port}/bad-redirect", **kw)
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(FetchError, match="within 1 seconds"):
            await fetch_public(f"http://site.test:{s.port}/drip", timeout_s=1, **kw)
        assert asyncio.get_running_loop().time() - t0 < 3  # one deadline per page, not per read
        res = await fetch_public(f"http://site.test:{s.port}/split", **kw)
        assert res.body == b"hello world " * 100


def test_web_tools_are_not_allowed_anywhere_by_default():
    from homemesh.common.config import WorkerSection

    assert not {"web_search", "fetch_url", "open_url"} & set(WorkerSection().allowed_tools)
    assert webfetch.system_resolver is not None


# --------------------------------------------------------------------------- CLI


def test_cli_allow_tool_and_set_searxng(tmp_path, monkeypatch, capsys):
    from homemesh.cli.main import main
    from homemesh.common.config import HomeMeshConfig, load_config, save_config
    from homemesh.common.paths import default_paths

    monkeypatch.delenv("HOMEMESH_SEARXNG_URL", raising=False)
    home = ["--home", str(tmp_path)]
    # Not set up yet: nothing is written (no default coordinator config appears), nothing starts.
    for cmd in (["web", "set-searxng", "http://127.0.0.1:8888"], ["worker", "allow-tool", "web_search"],
                ["restart"]):
        assert main([*home, *cmd]) == 2
        assert "not set up on this computer yet" in capsys.readouterr().out
    assert not default_paths(tmp_path).config_file.exists()
    cfg = HomeMeshConfig()
    cfg.node.roles = ["worker"]
    save_config(cfg, default_paths(tmp_path).config_file)
    assert main([*home, "worker", "allow-tool", "shell"]) == 2
    assert "not a tool that runs on a computer" in capsys.readouterr().out
    assert main([*home, "worker", "allow-tool", "web_search"]) == 0
    assert "homemesh web set-searxng" in capsys.readouterr().out
    assert main([*home, "worker", "allow-tool", "open_url"]) == 0
    assert main([*home, "web", "set-searxng", "ftp://x"]) == 2
    assert main([*home, "web", "set-searxng", "http://127.0.0.1:8888/"]) == 0
    cfg = load_config(default_paths(tmp_path))
    assert {"web_search", "open_url"} <= set(cfg.worker.allowed_tools) and cfg.web.searxng_url == "http://127.0.0.1:8888"
    assert main([*home, "worker", "disallow-tool", "open_url"]) == 0
    assert main([*home, "web", "set-searxng", "none"]) == 0
    cfg = load_config(default_paths(tmp_path))
    assert "open_url" not in cfg.worker.allowed_tools and cfg.web.searxng_url == ""


async def test_cli_web_test_runs_a_real_search_against_the_configured_searxng(tmp_path, monkeypatch, capsys):
    from homemesh.cli.main import main

    async with Server({"/search": (200, {"Content-Type": "application/json"}, json.dumps(SEARX_JSON).encode())}) as s:
        monkeypatch.setenv("HOMEMESH_SEARXNG_URL", f"http://127.0.0.1:{s.port}")
        code = await asyncio.to_thread(main, ["--home", str(tmp_path), "web", "test", "pancakes"])
    out = capsys.readouterr().out
    assert code == 0 and "2 result(s)" in out and "https://recipes.example/pancakes" in out
    assert "engine did not answer: google: CAPTCHA" in out and "allow-tool web_search" in out
