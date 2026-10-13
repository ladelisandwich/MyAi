"""Readable text and links from an HTML page (standard library only).

Scripts, styles, forms, hidden elements and page chrome (navigation, banners, footers) are
dropped; block elements become line breaks; headings and list items keep a light Markdown shape
so the model sees the page structure. When the page marks its main content (``<main>``,
``<article>``, ``role="main"``), only that is returned. Lists that are almost entirely links
(menus, language pickers, "see also" boxes) are left out of the text. Links are made absolute,
only ``http(s)`` links are kept, and links from the main content come first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from homemesh.tools.urlsafety import UnsafeURL, normalize_url

_SKIP = {"script", "style", "noscript", "template", "svg", "math", "canvas", "iframe", "object", "embed",
         "head", "select", "button", "form", "dialog"}
_CHROME = {"nav", "footer", "aside"}
_CHROME_ROLES = {"navigation", "banner", "contentinfo", "search", "menu", "menubar", "complementary", "dialog"}
_MAIN = {"main", "article"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "br", "hr", "tr", "table", "ul", "ol", "dl", "dt",
          "dd", "blockquote", "pre", "figure", "figcaption", "address", "details", "summary", "li", "h1", "h2",
          "h3", "h4", "h5", "h6", "td", "th"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
         "wbr"}
MIN_MAIN_CHARS = 200
MAX_LINKS = 60
_COLLECT_LINKS = 400
# A list with at least this many items whose text is at least this share link text is navigation.
_LINK_LIST_ITEMS = 3
_LINK_LIST_DENSITY = 0.75


@dataclass
class PageText:
    title: str = ""
    text: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    description: str = ""


@dataclass
class _Open:
    """What an open (not skipped) element switched on, so its end tag switches exactly that off."""

    main: bool = False
    chrome: bool = False
    list_start: tuple[int, int, int] | None = None  # (len(out), len(main_out), len(links)) for ul/ol
    items: int = 0
    text_chars: int = 0
    link_chars: int = 0


class _Extractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.main_out: list[str] = []
        self.title: list[str] = []
        self.description = ""
        self.links: list[tuple[int, dict[str, str]]] = []  # (priority, link); 0 = main content
        self._seen_links: set[str] = set()
        self._main_depth = 0
        self._chrome_depth = 0
        self._skip_depth = 0
        self._in_title = False
        self._in_pre = 0
        self._link: tuple[str, list[str]] | None = None
        # The parser does not balance tags; track open elements by name so stray end tags in broken
        # markup cannot switch skipping off early or leave it on forever.
        self._stack: list[str] = []
        self._open: list[_Open] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "base" and a.get("href"):
            self.base_url = urljoin(self.base_url, a["href"])
        if tag == "meta" and a.get("name", "").lower() == "description" and not self.description:
            self.description = a.get("content", "").strip()[:500]
        if tag == "title":
            self._in_title = True
        if tag in _VOID:
            if tag in ("br", "hr"):
                self._emit("\n")
            return
        hidden = "hidden" in a or a.get("aria-hidden") == "true" or re.search(
            r"display\s*:\s*none|visibility\s*:\s*hidden", a.get("style", ""), re.I)
        self._stack.append(tag)
        if tag in _SKIP or hidden or self._skip_depth:
            self._skip_depth += 1
            return
        role = a.get("role", "").lower()
        el = _Open(main=tag in _MAIN or role == "main",
                   chrome=tag in _CHROME or role in _CHROME_ROLES or (tag == "header" and not self._main_depth))
        if tag in ("ul", "ol"):
            el.list_start = (len(self.out), len(self.main_out), len(self.links))
        elif tag == "li":
            for o in reversed(self._open):
                if o.list_start is not None:
                    o.items += 1
                    break
        self._open.append(el)
        self._main_depth += el.main
        self._chrome_depth += el.chrome
        if tag == "pre":
            self._in_pre += 1
        if tag in _BLOCK:
            self._emit("\n")
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("#" * int(tag[1]) + " ")
        elif tag == "li":
            self._emit("- ")
        elif tag == "a" and a.get("href"):
            self._link = (a["href"], [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag not in self._stack:
            return
        while self._stack:
            open_tag = self._stack.pop()
            self._close(open_tag)
            if open_tag == tag:
                break

    def _close(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
            return
        el = self._open.pop() if self._open else _Open()
        if tag == "a" and self._link is not None:
            href, parts = self._link
            self._link = None
            self._add_link(href, " ".join("".join(parts).split()))
        self._main_depth -= el.main
        self._chrome_depth -= el.chrome
        if tag == "pre" and self._in_pre:
            self._in_pre -= 1
        if el.list_start is not None and el.items >= _LINK_LIST_ITEMS and el.text_chars \
                and el.link_chars / el.text_chars >= _LINK_LIST_DENSITY:
            out_at, main_at, _links_at = el.list_start
            del self.out[out_at:]
            del self.main_out[main_at:]
            # Its links stay available, but behind the content links.
            self.links = [(max(p, 1) if i >= _links_at else p, lk) for i, (p, lk) in enumerate(self.links)]
        if tag in _BLOCK:
            self._emit("\n")

    def _emit(self, text: str) -> None:
        if self._chrome_depth and text.strip():
            return  # menus, banners and footers: keep their links, not their text
        self.out.append(text)
        if self._main_depth:
            self.main_out.append(text)

    def _add_link(self, href: str, text: str) -> None:
        if len(self.links) >= _COLLECT_LINKS or href.startswith("#"):
            return
        try:
            url = normalize_url(urljoin(self.base_url, href))
        except (UnsafeURL, ValueError):
            return
        if url in self._seen_links:
            return
        self._seen_links.add(url)
        priority = 0 if self._main_depth and not self._chrome_depth else 1
        self.links.append((priority, {"text": text[:120], "url": url}))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
            return
        if self._skip_depth:
            return
        n = len(data.strip())
        in_link = self._link is not None
        if self._link is not None:
            self._link[1].append(data)
        for o in self._open:
            if o.list_start is not None:
                o.text_chars += n
                o.link_chars += n if in_link else 0
        self._emit(data if self._in_pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str, base_url: str) -> PageText:
    p = _Extractor(base_url)
    try:
        p.feed(html)
        p.close()
    except (AssertionError, ValueError):
        pass  # badly broken markup: keep what was parsed so far
    main = _tidy("".join(p.main_out))
    text = main if len(main) >= MIN_MAIN_CHARS else _tidy("".join(p.out))
    has_main = len(main) >= MIN_MAIN_CHARS
    ranked = sorted(enumerate(p.links), key=lambda x: (x[1][0] if has_main else 0, x[0]))
    links = [lk for _, (_, lk) in ranked][:MAX_LINKS]
    title = " ".join("".join(p.title).split())[:300]
    return PageText(title=title, text=text, links=links, description=p.description)


_EMPTY_MARK = re.compile(r"^(?:#+|-|\|)\s*$")


def _tidy(raw: str) -> str:
    lines = []
    for ln in raw.split("\n"):
        ln = ln.rstrip() if ln.startswith("    ") else ln.strip()
        if _EMPTY_MARK.match(ln):
            ln = ""  # a heading or list item whose text was all menus/scripts/images
        lines.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
