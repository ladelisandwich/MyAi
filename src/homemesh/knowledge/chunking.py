"""Structure-aware text chunking with line and page references.

Chunks follow paragraph and heading boundaries where possible, are at most ``chunk_chars``
long (plus an optional header line), and overlap by roughly ``overlap`` characters (whole
lines) so that facts spanning a boundary remain retrievable. Every chunk records its line
range (or page) so answers can cite precise sources.
"""

from __future__ import annotations

from homemesh.protocol.jobs import ChunkRecord


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Group line indexes into blocks separated by blank lines or Markdown headings."""
    blocks: list[tuple[int, int]] = []
    start = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        is_blank = not stripped
        is_heading = stripped.startswith("#") and stripped.lstrip("#").startswith(" ")
        if is_heading and start is not None:
            blocks.append((start, i))
            start = None
        if is_blank:
            if start is not None:
                blocks.append((start, i))
                start = None
            continue
        if start is None:
            start = i
    if start is not None:
        blocks.append((start, len(lines)))
    return blocks


def chunk_text(text: str, chunk_chars: int = 2000, overlap: int = 200, *, page: int | None = None,
               header: str | None = None, start_ordinal: int = 0) -> list[ChunkRecord]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Pre-split very long lines so a single line never exceeds the chunk size.
    norm: list[tuple[str, int]] = []  # (text, original 1-based line number)
    for idx, line in enumerate(text.split("\n"), start=1):
        if len(line) <= chunk_chars:
            norm.append((line, idx))
        else:
            norm.extend((line[j:j + chunk_chars], idx) for j in range(0, len(line), chunk_chars))
    plain = [t for t, _ in norm]
    prefix = (header + "\n") if header else ""
    chunks: list[ChunkRecord] = []
    cur: list[int] = []
    cur_len = 0
    fresh = False  # True when cur holds lines not yet emitted in a chunk

    def flush(keep_overlap: bool = True) -> None:
        nonlocal cur, cur_len, fresh
        if not cur or not fresh:
            return
        body = "\n".join(plain[i] for i in cur).strip("\n")
        if body.strip():
            chunks.append(ChunkRecord(ordinal=start_ordinal + len(chunks), content=prefix + body,
                                      start_line=norm[cur[0]][1], end_line=norm[cur[-1]][1], page=page))
        keep: list[int] = []
        kept = 0
        if keep_overlap and overlap > 0:
            for i in reversed(cur):
                if kept + len(plain[i]) + 1 > overlap:
                    break
                keep.insert(0, i)
                kept += len(plain[i]) + 1
        cur, cur_len, fresh = keep, kept, False

    def add(i: int) -> None:
        nonlocal cur_len, fresh
        cur.append(i)
        cur_len += len(plain[i]) + 1
        fresh = True

    for b_start, b_end in _blocks(plain):
        block = list(range(b_start, b_end))
        block_len = sum(len(plain[i]) + 1 for i in block)
        if cur_len + block_len > chunk_chars:
            if fresh:
                flush()
            if cur_len + block_len > chunk_chars and not fresh:
                cur, cur_len = [], 0  # overlap would push us over the limit; drop it
        if block_len > chunk_chars:
            for i in block:
                if cur_len + len(plain[i]) + 1 > chunk_chars and fresh:
                    flush()
                add(i)
        else:
            for i in block:
                add(i)
    flush(keep_overlap=False)
    return chunks


def chunk_pages(pages: list[str], chunk_chars: int = 2000, overlap: int = 200) -> list[ChunkRecord]:
    out: list[ChunkRecord] = []
    for pno, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        out.extend(chunk_text(page_text, chunk_chars, overlap, page=pno, start_ordinal=len(out)))
    return out
