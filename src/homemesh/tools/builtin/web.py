"""Internet tools: search the web, read a public page, open a link in the browser.

All three run on a computer (``location="node"``) and only on computers whose owner listed
them in ``worker.allowed_tools``; they are not in the default list.

* ``web_search`` asks the owner's own SearXNG instance (``web.searxng_url``), so no search
  API key or account is involved and queries go out through the owner's own computer.
* ``fetch_url`` downloads one public page through :mod:`homemesh.tools.webfetch`, which refuses
  every private, local or link-local address. Addresses the user did not supply and no earlier
  web result contained need an approval first (``url_argument``).
* ``open_url`` opens an ``http(s)`` link in the default browser of that computer and, as an
  action with side effects, always needs the user's approval.

Everything these tools return is untrusted web content; the chat engine hands it to the model
as data, never as instructions.
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator

from homemesh import __version__
from homemesh.common.config import WebSection
from homemesh.knowledge.extract import extract
from homemesh.tools.base import ToolContext, ToolDefinition
from homemesh.tools.desktop import open_in_browser
from homemesh.tools.htmltext import html_to_text
from homemesh.tools.urlsafety import UnsafeURL, host_label, normalize_url
from homemesh.tools.webfetch import FetchError, decode_body, fetch_public

# Tools whose results contain links the model may follow without asking (see ToolService._url_known).
WEB_RESULT_TOOLS = ("web_search", "fetch_url")
USER_AGENT = f"HomeMesh/{__version__} (self-hosted personal assistant; fetches pages at its owner's request)"
MAX_LINKS_RETURNED = 40
_TEXT_TYPES = {"application/json", "application/ld+json", "application/xml", "application/rss+xml",
               "application/atom+xml", "application/javascript", "application/x-yaml", "application/yaml"}


def urls_in_output(output: Any, limit: int = 500) -> list[str]:
    """Every ``url``/``final_url`` value in a web tool's stored output."""
    found: list[str] = []
    stack = [output]
    while stack and len(found) < limit:
        item = stack.pop()
        if isinstance(item, dict):
            for k, v in item.items():
                if k in ("url", "final_url") and isinstance(v, str):
                    found.append(v)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(item, list):
            stack.extend(item)
    return found


def _web(ctx: ToolContext) -> WebSection:
    return ctx.web if isinstance(ctx.web, WebSection) else WebSection()


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalized(v: str) -> str:
    try:
        return normalize_url(v)
    except UnsafeURL as exc:
        raise ValueError(str(exc)) from exc


# --------------------------------------------------------------------------- web_search
class SearchInput(BaseModel):
    query: str = Field(description="What to search the web for", min_length=1, max_length=400)
    category: Literal["general", "news", "videos", "science", "it"] = Field(
        default="general", description="Kind of results")
    time_range: Literal["day", "week", "month", "year"] | None = Field(
        default=None, description="Only results from this recent period")
    max_results: int | None = Field(default=None, ge=1, le=20, description="How many results to return")


class SearchUnavailable(OSError):
    pass


async def _web_search(ctx: ToolContext, args: SearchInput) -> dict[str, Any]:
    cfg = _web(ctx)
    if not cfg.searxng_url:
        raise SearchUnavailable("web search is not set up on this computer: web.searxng_url is empty "
                                "(see docs/WEB_ACCESS.md)")
    params = {"q": args.query, "format": "json", "pageno": "1", "categories": args.category,
              "safesearch": str(cfg.safesearch), "language": cfg.search_language}
    if args.time_range:
        params["time_range"] = args.time_range
    # text/html in Accept and an Accept-Language keep SearXNG's bot detection happy should the owner
    # ever turn its limiter on; the answer is JSON because of format=json.
    headers = {"Accept": "application/json, text/html;q=0.5", "User-Agent": USER_AGENT,
               "Accept-Language": "en;q=0.9, *;q=0.5"}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(cfg.timeout_s)) as client:
            r = await client.get(cfg.searxng_url + "/search", params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise SearchUnavailable(f"SearXNG at {cfg.searxng_url} did not answer within {cfg.timeout_s:.0f} s") from exc
    except httpx.HTTPError as exc:
        raise SearchUnavailable(f"SearXNG at {cfg.searxng_url} is not reachable ({type(exc).__name__}). Is it "
                                "running? On Bazzite: systemctl --user status homemesh-searxng") from exc
    if r.status_code == 403:
        raise SearchUnavailable("SearXNG refused to answer in JSON. Add json to search.formats in its settings.yml "
                                "(the HomeMesh SearXNG installer does this) and restart it.")
    if r.status_code == 429:
        raise SearchUnavailable("SearXNG's rate limiter blocked the request. For a private instance set "
                                "server.limiter: false in its settings.yml and restart it.")
    if r.status_code != 200:
        detail = ""
        with contextlib.suppress(ValueError, AttributeError):
            detail = f": {_clean(r.json().get('error'), 200)}"
        raise SearchUnavailable(f"SearXNG answered HTTP {r.status_code}{detail}")
    try:
        data = r.json()
    except ValueError as exc:
        raise SearchUnavailable("SearXNG did not return JSON; check web.searxng_url") from exc
    if not isinstance(data, dict):
        raise SearchUnavailable("SearXNG returned an unexpected answer")
    limit = args.max_results or cfg.max_results
    results: list[dict[str, Any]] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        try:
            url = normalize_url(str(item.get("url") or ""))
        except UnsafeURL:
            continue
        engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])
        entry = {"title": _clean(item.get("title"), 200), "url": url, "snippet": _clean(item.get("content"), 500),
                 "engines": [str(e) for e in engines][:5]}
        if item.get("publishedDate"):
            entry["published"] = str(item["publishedDate"])[:40]
        results.append(entry)
        if len(results) >= limit:
            break
    out: dict[str, Any] = {"query": args.query, "results": results}
    answers = [_clean(a.get("answer") if isinstance(a, dict) else a, 500) for a in data.get("answers") or []]
    if answers := [a for a in answers if a]:
        out["answers"] = answers[:3]
    boxes = [b for b in data.get("infoboxes") or [] if isinstance(b, dict)]
    if boxes:
        box = boxes[0]
        info: dict[str, Any] = {"title": _clean(box.get("infobox"), 200), "content": _clean(box.get("content"), 800)}
        try:
            info["url"] = normalize_url(str(box.get("id") or ""))
        except UnsafeURL:
            pass
        out["infobox"] = info
    if suggestions := [_clean(s, 100) for s in data.get("suggestions") or [] if s]:
        out["suggestions"] = suggestions[:5]
    unresponsive = []
    for e in data.get("unresponsive_engines") or []:
        if isinstance(e, (list, tuple)) and e:
            unresponsive.append(_clean(": ".join(str(x) for x in e[:2]), 120))
    if unresponsive:
        out["unresponsive_engines"] = unresponsive[:10]
    if not results:
        out["message"] = ("No results." + (" Some search engines did not answer, see unresponsive_engines."
                                           if unresponsive else ""))
    return out


# --------------------------------------------------------------------------- fetch_url
class FetchInput(BaseModel):
    url: str = Field(description="The http:// or https:// address of a public web page", max_length=2048)
    max_chars: int = Field(default=12000, ge=500, le=50000, description="Maximum characters of page text to return")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _normalized(v)


def _looks_like_html(body: bytes) -> bool:
    head = body[:1024].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or b"<html" in head


async def _fetch_url(ctx: ToolContext, args: FetchInput) -> dict[str, Any]:
    cfg = _web(ctx)
    res = await fetch_public(args.url, timeout_s=cfg.timeout_s, max_bytes=int(cfg.max_download_mb * 2**20),
                             user_agent=USER_AGENT, max_redirects=cfg.max_redirects)
    host = host_label(res.final_url)
    if res.status >= 400:
        raise FetchError(f"{host} answered HTTP {res.status} {res.reason}".strip())
    ctype = res.content_type
    title = description = ""
    links: list[dict[str, str]] = []
    if ctype in ("text/html", "application/xhtml+xml") or (not ctype and _looks_like_html(res.body)):
        page = html_to_text(decode_body(res.body, res.charset, html=True), res.final_url)
        title, description, text, links = page.title, page.description, page.text, page.links
    elif ctype == "application/pdf":
        ex = extract(Path("page.pdf"), res.body)
        if ex.status not in ("indexed", "empty"):
            raise FetchError(f"could not read the PDF from {host}: {ex.error or ex.status}"
                             + (" (it is larger than the download limit)" if res.truncated else ""))
        text = ex.text or "\n\n".join(f"[page {i}]\n{t}" for i, t in enumerate(ex.pages, start=1))
    elif ctype.startswith("text/") or ctype in _TEXT_TYPES or not ctype:
        text = decode_body(res.body, res.charset, html=False)
        if "\x00" in text[:2048]:
            raise FetchError(f"the link on {host} is a binary file, not a page HomeMesh can read")
    else:
        raise FetchError(f"the link on {host} is a {ctype} file, not a page HomeMesh can read")
    out: dict[str, Any] = {"url": res.url, "final_url": res.final_url, "status": res.status, "content_type": ctype,
                           "title": title, "content": text[:args.max_chars],
                           "truncated": len(text) > args.max_chars or res.truncated}
    if description:
        out["description"] = description
    if res.redirects:
        out["redirected"] = True
    if links:
        out["links"] = links[:MAX_LINKS_RETURNED]
    return out


# --------------------------------------------------------------------------- open_url
class OpenInput(BaseModel):
    url: str = Field(description="The http:// or https:// address to open, e.g. https://www.youtube.com",
                     max_length=2048)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _normalized(v)


_LOCAL_NAMES = re.compile(r"(^|\.)(localhost|local|lan|home|internal|intranet|home\.arpa)$")


def _open_url_note(args: OpenInput) -> str | None:
    host = host_label(args.url)
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        local = bool(_LOCAL_NAMES.search(host)) or "." not in host
    else:
        local = not ip.is_global
    if local:
        return f"This link points to a device on your own network or this computer ({host})."
    return None


async def _open_url(ctx: ToolContext, args: OpenInput) -> dict[str, Any]:
    how = await open_in_browser(args.url)
    return {"opened": args.url, "with": how,
            "note": "The link was handed to the browser; HomeMesh cannot see what the page shows."}


WEB_SEARCH = ToolDefinition(
    name="web_search", description="Search the web (through the owner's own SearXNG) and return result titles, links "
                                   "and snippets. Use fetch_url to read a result.",
    input_model=SearchInput, output_description="{query, results: [{title, url, snippet}], answers?, infobox?}",
    handler=_web_search, location="node", side_effect="read", permissions=["net.search"], timeout_s=40,
)
FETCH_URL = ToolDefinition(
    name="fetch_url", description="Read a public web page (or text/PDF file) and return its text and links. Pages on "
                                  "the home network are refused. Links the user did not give and no search result "
                                  "contained need the user's approval.",
    input_model=FetchInput, output_description="{final_url, title, content, truncated, links}", handler=_fetch_url,
    location="node", side_effect="read", permissions=["net.fetch"], timeout_s=60, url_argument="url",
)
OPEN_URL = ToolDefinition(
    name="open_url", description="Open a web link (http/https) in the default browser on one of the owner's "
                                 "computers, e.g. 'open YouTube'. Always asks the user to approve first.",
    input_model=OpenInput, output_description="{opened, with}", handler=_open_url, location="node",
    side_effect="write", permissions=["desktop.open_url"], timeout_s=20, approval_note=_open_url_note,
)
