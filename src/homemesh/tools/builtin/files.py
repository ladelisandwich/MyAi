"""File tools that run on a node, confined to that node's authorised folders.

``list_directory`` and ``read_file`` are read-only and limited to ``worker.allowed_roots``
(plus collection roots for the built-in worker). ``write_file`` can only create files in
the node's HomeMesh workspace folder, never overwrites, and always needs user approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from homemesh.knowledge.extract import extract
from homemesh.knowledge.pathsafety import PathPolicy, is_sensitive
from homemesh.tools.base import ToolContext, ToolDefinition

MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_LIST = 500


class ListInput(BaseModel):
    path: str = Field(description="Absolute path of a folder inside an authorised folder")


async def _list_directory(ctx: ToolContext, args: ListInput) -> dict[str, Any]:
    policy: PathPolicy = ctx.path_policy
    target = policy.resolve(Path(args.path))
    if not target.is_dir():
        raise NotADirectoryError(f"{target} is not a folder")
    entries = []
    for p in sorted(target.iterdir())[:MAX_LIST]:
        if p.name.startswith(".") or is_sensitive(p):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append({"name": p.name, "type": "dir" if p.is_dir() else "file", "size": st.st_size})
    return {"path": str(target), "entries": entries, "truncated": len(entries) >= MAX_LIST}


class ReadInput(BaseModel):
    path: str = Field(description="Absolute path of a text, Markdown, code, JSON, CSV or PDF file")
    max_chars: int = Field(default=20000, ge=100, le=100000)


async def _read_file(ctx: ToolContext, args: ReadInput) -> dict[str, Any]:
    policy: PathPolicy = ctx.path_policy
    target = policy.resolve(Path(args.path))
    if not target.is_file():
        raise FileNotFoundError(f"{target} is not a file")
    size = target.stat().st_size
    if size > 20 * 1024 * 1024:
        raise ValueError("file is larger than 20 MB")
    data = target.read_bytes()
    ex = extract(target, data if target.suffix.lower() == ".pdf" else data[:MAX_READ_BYTES])
    if ex.status not in ("indexed", "empty"):
        return {"path": str(target), "status": ex.status, "error": ex.error, "content": ""}
    text = ex.text or "\n\n".join(f"[page {i}]\n{t}" for i, t in enumerate(ex.pages, start=1))
    return {"path": str(target), "status": "ok", "content": text[:args.max_chars],
            "truncated": len(text) > args.max_chars or size > MAX_READ_BYTES}


class WriteInput(BaseModel):
    filename: str = Field(description="File name to create in the HomeMesh workspace (no folders)", max_length=120)
    content: str = Field(description="Text content", max_length=MAX_WRITE_BYTES)


async def _write_file(ctx: ToolContext, args: WriteInput) -> dict[str, Any]:
    if ctx.workspace_dir is None:
        raise PermissionError("no workspace folder is configured on this node")
    name = Path(args.filename).name
    if name != args.filename or name.startswith(".") or not name:
        raise PermissionError("only a plain file name (no folders, not hidden) is allowed")
    ctx.workspace_dir.mkdir(parents=True, exist_ok=True)
    target = ctx.workspace_dir / name
    if target.exists():
        raise FileExistsError(f"{name} already exists in the workspace; HomeMesh never overwrites files")
    target.write_text(args.content, encoding="utf-8")
    return {"created": str(target), "bytes": len(args.content.encode("utf-8"))}


LIST_DIRECTORY = ToolDefinition(
    name="list_directory", description="List files in a folder that the owner authorised on this computer.",
    input_model=ListInput, output_description="{path, entries: [{name, type, size}]}", handler=_list_directory,
    location="node", side_effect="read", permissions=["fs.read"], timeout_s=15,
)
READ_FILE = ToolDefinition(
    name="read_file", description="Read a text/Markdown/code/JSON/CSV/PDF file inside an authorised folder.",
    input_model=ReadInput, output_description="{path, status, content, truncated}", handler=_read_file,
    location="node", side_effect="read", permissions=["fs.read"], timeout_s=30,
)
WRITE_FILE = ToolDefinition(
    name="write_file", description="Create a new text file in the HomeMesh workspace folder (never overwrites). "
                                   "Requires the user's approval.",
    input_model=WriteInput, output_description="{created, bytes}", handler=_write_file,
    location="node", side_effect="write", permissions=["fs.write.workspace"], timeout_s=15,
)
