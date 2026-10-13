"""Readable text and links from an HTML page (standard library only).

Scripts, styles, form controls, hidden elements and page chrome (navigation, banners, footers) are
dropped; block elements become line breaks; headings and list items keep a light Markdown shape so
the model sees the page structure. When the page marks its main content (``<main>``,
``role="main"``) only that is returned; ``<article>`` text is used only when it is most of the page
(comment threads are articles too). Lists that are almost entirely links (menus, language pickers,
"see also" boxes) are left out of the text. Links are made absolute, only ``http(s)`` links are kept,
and links from the main content come first.

Like a browser, the parser closes elements whose end tags HTML lets authors leave out (``<p>``,
``<li>``, ``<td>``, ``<tr>``, ``</head>``…), and every step costs the same however deep the page is
nested, so the work stays linear in the size of the page. Only the first :data:`MAX_HTML_CHARS` of a
page are read.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from homemesh.tools.urlsafety import UnsafeURL, normalize_url

_SKIP = {"script", "style", "noscript", "template", "svg", "math", "canvas", "iframe", "object", "embed",
         "head", "select", "button", "textarea", "dialog"}
_CHROME = {"nav", "footer", "aside"}
_CHROME_ROLES = {"navigation", "banner", "contentinfo", "search", "menu", "menubar", "complementary", "dialog"}
_BLOCK = {"p", "div", "section", "article", "main", "header", "br", "hr", "tr", "table", "ul", "ol", "dl", "dt",
          "dd", "blockquote", "pre", "figure", "figcaption", "address", "details", "summary", "li", "h1", "h2",
          "h3", "h4", "h5", "h6", "td", "th", "form", "fieldset"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
         "wbr"}
_HEAD_CONTENT = {"title", "meta", "link", "style", "script", "base", "noscript", "template"}
# Starting one of these closes an open <p> (HTML's "close a p element" rule).
_CLOSES_P = {"address", "article", "aside", "blockquote", "details", "dialog", "div", "dl", "fieldset", "figcaption",
             "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "menu", "nav",
             "ol", "p", "pre", "section", "table", "ul"}
# Optional end tags: starting the key closes the nearest open element of these kinds, but only within
# the container named on the right (a nested list keeps the outer list's item open).
_IMPLIED_END = {
    "li": ({"li"}, {"ul", "ol", "menu"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "tr": ({"tr", "td", "th"}, {"table", "thead", "tbody", "tfoot"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "thead": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tbody": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tfoot": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
}
_P_SCOPE = {"td", "th", "li", "table", "div", "body", "html", "blockquote", "section", "article", "main",
            "button", "dd", "dt"}
MAX_DEPTH = 512
# How far up the open elements an implied end tag looks (a browser looks all the way; real pages
# nest an item's content far less deeply than this, and the bound keeps each step cheap).
_IMPLIED_END_REACH = 32
# Longer pages are cut (marked truncated); the extracted text is far shorter than this anyway.
MAX_HTML_CHARS = 2_000_000
MIN_MAIN_CHARS = 200
MAX_LINKS = 60
_COLLECT_LINKS = 400
_EXAMINE_LINKS = 2000  # hrefs looked at, duplicates included (checking a link isn't free)
# A list with at least this many items whose text is at least this share link text is navigation.
_LINK_LIST_ITEMS = 3
_LINK_LIST_DENSITY = 0.75
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_SPACES = re.compile(r"\s+")


@dataclass
class PageText:
    title: str = ""
    text: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    description: str = ""
    truncated: bool = False


@dataclass
class _Open:
    """What an open (not skipped) element switched on, so its end switches exactly that off."""

    main: bool = False
    article: bool = False
    chrome: bool = False
    list_start: tuple[int, int, int, int] | None = None  # (out, main_out, article_out, links) for ul/ol
    items: int = 0
    text_chars: int = 0
    link_chars: int = 0


class _Extractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.main_out: list[str] = []
        self.article_out: list[str] = []
        self.title: list[str] = []
        self._title_done = False
        self.description = ""
        self.links: list[tuple[int, dict[str, str]]] = []  # (priority, link); 0 = main content
        self._seen_links: set[str] = set()
        self._seen_hrefs: set[tuple[str, str]] = set()
        self._main_depth = 0
        self._article_depth = 0
        self._chrome_depth = 0
        self._skip_depth = 0
        self._in_title = False
        self._in_pre = 0
        self._link: tuple[str, list[str]] | None = None
        # Open elements by name (with a count per name and, per entry, whether a <p> is open in
        # "button scope" there), plus what each non-skipped one switched on. Text is counted only for
        # the innermost open list and added to the enclosing one when it ends.
        self._stack: list[str] = []
        self._counts: Counter[str] = Counter()
        self._p_open: list[bool] = []
        self._open: list[_Open] = []
        self._lists: list[_Open] = []

    # ------------------------------------------------------------------ structure
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if self._counts["head"] and tag not in _HEAD_CONTENT:
            self._close_through("head")  # </head> is optional; <body> or content ends the head
        if tag == "base" and a.get("href"):
            self.base_url = urljoin(self.base_url, a["href"])
        if tag == "meta" and a.get("name", "").lower() == "description" and not self.description:
            self.description = a.get("content", "").strip()[:500]
        if tag == "title":
            # Only the document's own title, not <title> elements of inline SVG icons.
            self._in_title = not self._title_done and not self._counts["svg"] and not self._counts["math"]
        if tag in _VOID:
            if tag in ("br", "hr"):
                if tag == "hr":
                    self._close_p()
                self._emit("\n")
            return
        if tag in _CLOSES_P:
            self._close_p()
        if tag in _IMPLIED_END:
            closes, scope = _IMPLIED_END[tag]
            for name in self._stack[:-_IMPLIED_END_REACH - 1:-1]:
                if name in scope:
                    break
                if name in closes:
                    self._close_through(name)
                    break
        if len(self._stack) >= MAX_DEPTH:
            return  # absurdly deep markup: keep its text, stop tracking structure
        hidden = "hidden" in a or a.get("aria-hidden") == "true" or _HIDDEN_STYLE.search(a.get("style", ""))
        self._push(tag)
        if tag in _SKIP or hidden or self._skip_depth:
            self._skip_depth += 1
            return
        role = a.get("role", "").lower()
        el = _Open(main=tag == "main" or role == "main", article=tag == "article",
                   chrome=tag in _CHROME or role in _CHROME_ROLES or (tag == "header" and not self._main_depth))
        if tag in ("ul", "ol"):
            el.list_start = (len(self.out), len(self.main_out), len(self.article_out), len(self.links))
            self._lists.append(el)
        elif tag == "li" and self._lists:
            self._lists[-1].items += 1
        self._open.append(el)
        self._main_depth += el.main
        self._article_depth += el.article
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
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        if tag == "p" and not self._counts["p"]:
            return  # a stray </p> (or one already closed implicitly)
        self._close_through(tag)

    def _push(self, tag: str) -> None:
        self._stack.append(tag)
        self._counts[tag] += 1
        if tag == "p":
            self._p_open.append(True)
        elif tag in _P_SCOPE:
            self._p_open.append(False)
        else:
            self._p_open.append(bool(self._p_open) and self._p_open[-1])

    def _close_p(self) -> None:
        if self._p_open and self._p_open[-1]:
            self._close_through("p")

    def _close_through(self, tag: str) -> None:
        if not self._counts[tag]:
            return
        while self._stack:
            open_tag = self._stack.pop()
            self._p_open.pop()
            self._counts[open_tag] -= 1
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
        self._article_depth -= el.article
        self._chrome_depth -= el.chrome
        if tag == "pre" and self._in_pre:
            self._in_pre -= 1
        if el.list_start is not None:
            if self._lists and self._lists[-1] is el:
                self._lists.pop()
            if self._lists:  # the enclosing list contains this one's text too
                self._lists[-1].text_chars += el.text_chars
                self._lists[-1].link_chars += el.link_chars
            if el.items >= _LINK_LIST_ITEMS and el.text_chars and el.link_chars / el.text_chars >= _LINK_LIST_DENSITY:
                out_at, main_at, article_at, links_at = el.list_start
                del self.out[out_at:]
                del self.main_out[main_at:]
                del self.article_out[article_at:]
                # Its links stay available, but behind the content links.
                for i in range(links_at, len(self.links)):
                    if self.links[i][0] == 0:
                        self.links[i] = (1, self.links[i][1])
        if tag in _BLOCK:
            self._emit("\n")

    # ------------------------------------------------------------------ content
    def _emit(self, text: str) -> None:
        if self._chrome_depth and text.strip():
            return  # menus, banners and footers: keep their links, not their text
        self.out.append(text)
        if self._main_depth:
            self.main_out.append(text)
        if self._article_depth:
            self.article_out.append(text)

    def _add_link(self, href: str, text: str) -> None:
        if (len(self.links) >= _COLLECT_LINKS or len(self._seen_hrefs) >= _EXAMINE_LINKS or href.startswith("#")
                or (self.base_url, href) in self._seen_hrefs):
            return
        self._seen_hrefs.add((self.base_url, href))
        try:
            url = normalize_url(urljoin(self.base_url, href))
        except (UnsafeURL, ValueError):
            return
        if url in self._seen_links:
            return
        self._seen_links.add(url)
        priority = 0 if (self._main_depth or self._article_depth) and not self._chrome_depth else 1
        self.links.append((priority, {"text": text[:120], "url": url}))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
            return
        if self._skip_depth:
            return
        if self._lists:
            n = len(data.strip())
            self._lists[-1].text_chars += n
            if self._link is not None:
                self._lists[-1].link_chars += n
        if self._link is not None:
            self._link[1].append(data)
        self._emit(data if self._in_pre else _SPACES.sub(" ", data))


def html_to_text(html: str, base_url: str) -> PageText:
    truncated = len(html) > MAX_HTML_CHARS
    p = _Extractor(base_url)
    try:
        p.feed(html[:MAX_HTML_CHARS] if truncated else html)
        p.close()
    except (AssertionError, ValueError):
        pass  # badly broken markup: keep what was parsed so far
    full = _tidy("".join(p.out))
    main = _tidy("".join(p.main_out))
    articles = _tidy("".join(p.article_out))
    if len(main) >= MIN_MAIN_CHARS:
        text, focused = main, True
    elif len(articles) >= MIN_MAIN_CHARS and len(articles) >= 0.5 * len(full):
        text, focused = articles, True
    else:
        text, focused = full, False
    ranked = sorted(enumerate(p.links), key=lambda x: (x[1][0] if focused else 0, x[0]))
    links = [lk for _, (_, lk) in ranked][:MAX_LINKS]
    title = " ".join("".join(p.title).split())[:300]
    return PageText(title=title, text=text, links=links, description=p.description, truncated=truncated)


_EMPTY_MARK = re.compile(r"^(?:#+|-|\|)\s*$")


def _tidy(raw: str) -> str:
    lines = []
    for ln in raw.split("\n"):
        ln = ln.rstrip() if ln.startswith("    ") else ln.strip()
        if _EMPTY_MARK.match(ln):
            ln = ""  # a heading or list item whose text was all menus/scripts/images
        lines.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
