"""Incremental folder scanner (runs on the worker that hosts the files).

For each supported file under the collection root it decides whether the document is
unchanged (same size and mtime, or same SHA-256), and otherwise extracts and chunks it. The
resulting :class:`DocumentRecord` objects are streamed to the coordinator, which stores
them and schedules embedding jobs - possibly on a different computer.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from homemesh.knowledge.chunking import chunk_pages, chunk_text
from homemesh.knowledge.extract import extract, supported
from homemesh.knowledge.pathsafety import PathPolicy, is_sensitive
from homemesh.protocol.jobs import DocumentRecord, KnowledgeScanPayload, KnowledgeScanResult

MAX_FILES = 50_000
CHUNKS_PER_MESSAGE = 200


def _included(rel: str, include_globs: list[str]) -> bool:
    if not include_globs:
        return True
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(os.path.basename(rel), g) for g in include_globs)


def iter_files(root: Path, exclude_dirs: list[str], include_globs: list[str]) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    excluded = set(exclude_dirs)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in excluded and not d.startswith(".") and not is_sensitive(Path(dirpath, d)))
        for fn in sorted(filenames):
            p = Path(dirpath, fn)
            if fn.startswith(".") or p.is_symlink() or is_sensitive(p):
                continue
            rel = p.relative_to(root).as_posix()
            if not supported(p) or not _included(rel, include_globs):
                continue
            out.append((p, rel))
            if len(out) >= MAX_FILES:
                return out
    return out


def process_file(path: Path, rel: str, payload: KnowledgeScanPayload) -> DocumentRecord:
    st = path.stat()
    known = payload.known.get(rel)
    if known and known.size == st.st_size and abs(known.mtime - st.st_mtime) < 1e-6:
        return DocumentRecord(rel_path=rel, size=st.st_size, mtime=st.st_mtime, content_hash=known.content_hash,
                              status="unchanged")
    if st.st_size > payload.max_file_mb * 1024 * 1024:
        return DocumentRecord(rel_path=rel, size=st.st_size, mtime=st.st_mtime, status="too_large",
                              error=f"file is larger than {payload.max_file_mb:g} MB")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if known and known.content_hash == digest:
        return DocumentRecord(rel_path=rel, size=st.st_size, mtime=st.st_mtime, content_hash=digest,
                              status="unchanged")
    ex = extract(path, data)
    rec = DocumentRecord(rel_path=rel, size=st.st_size, mtime=st.st_mtime, content_hash=digest, mime=ex.mime,
                         status=ex.status, error=ex.error)  # type: ignore[arg-type]
    if ex.pages:
        rec.page_count = len(ex.pages)
        if ex.status == "indexed":
            rec.chunks = chunk_pages(ex.pages, payload.chunk_chars, payload.chunk_overlap)
    elif ex.status == "indexed":
        rec.chunks = chunk_text(ex.text, payload.chunk_chars, payload.chunk_overlap, header=ex.csv_header)
    if rec.status == "indexed" and not rec.chunks:
        rec.status = "empty"
    return rec


async def scan(payload: KnowledgeScanPayload, policy: PathPolicy,
               emit: Callable[[DocumentRecord], Awaitable[None]],
               progress: Callable[[float, str], Awaitable[None]] | None = None) -> KnowledgeScanResult:
    t0 = time.perf_counter()
    root = policy.check_root(Path(payload.root_path))
    files = await asyncio.to_thread(iter_files, root, payload.exclude_dirs, payload.include_globs)
    stats: dict[str, int] = {}
    seen: list[str] = []
    for n, (path, rel) in enumerate(files, start=1):
        try:
            rec = await asyncio.to_thread(process_file, path, rel, payload)
        except OSError as exc:
            rec = DocumentRecord(rel_path=rel, size=0, mtime=0.0, status="error", error=f"cannot read file: {exc}")
        seen.append(rel)
        stats[rec.status] = stats.get(rec.status, 0) + 1
        if len(rec.chunks) > CHUNKS_PER_MESSAGE:
            parts = [rec.chunks[i:i + CHUNKS_PER_MESSAGE] for i in range(0, len(rec.chunks), CHUNKS_PER_MESSAGE)]
            for idx, part in enumerate(parts):
                await emit(rec.model_copy(update={"chunks": part, "part": idx, "final_part": idx == len(parts) - 1}))
        else:
            await emit(rec)
        if progress and (n % 10 == 0 or n == len(files)):
            await progress(n / max(len(files), 1), f"{n}/{len(files)} files")
    return KnowledgeScanResult(collection_id=payload.collection_id, seen=seen, stats=stats,
                               duration_ms=round((time.perf_counter() - t0) * 1000, 1))
