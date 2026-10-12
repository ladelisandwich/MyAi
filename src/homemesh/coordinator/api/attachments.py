"""Image attachments (raw-body upload; no multipart parser needed).

Uploaded files are stored under ``<data>/attachments`` and only sent to models whose backend
reports vision support. Type is verified from the file's magic bytes, not the client's claim.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from homemesh.common.errors import NotFoundError, ValidationFailed
from homemesh.common.ids import new_id
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require

router = APIRouter(prefix="/api/v1/attachments", tags=["attachments"])

MAGIC = [(b"\x89PNG\r\n\x1a\n", "image/png", ".png"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
         (b"GIF87a", "image/gif", ".gif"), (b"GIF89a", "image/gif", ".gif")]


def sniff(data: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in MAGIC:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


@router.post("")
async def upload(request: Request, filename: str = "image", conversation_id: str | None = None,
                 principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    data = await request.body()
    if not data:
        raise ValidationFailed("Empty upload.")
    kind = sniff(data)
    if kind is None:
        raise ValidationFailed("Only PNG, JPEG, GIF and WebP images can be attached.", code="unsupported_type")
    if conversation_id and not await st.repos.conversations.get(conversation_id, principal.user_id):
        raise NotFoundError("Conversation not found.")
    mime, ext = kind
    aid = new_id("att")
    path = st.paths.attachments_dir / f"{aid}{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ")[:120] or f"image{ext}"
    await st.repos.db.execute(
        "INSERT INTO attachments(id, user_id, conversation_id, kind, filename, mime, size, sha256, storage_path,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, principal.user_id, conversation_id, "image", safe_name, mime, len(data),
         hashlib.sha256(data).hexdigest(), str(path), now_iso()))
    return {"id": aid, "filename": safe_name, "mime": mime, "size": len(data), "kind": "image"}


@router.get("/{attachment_id}")
async def download(attachment_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> Any:
    st = state(request)
    row = await st.repos.db.fetchone("SELECT * FROM attachments WHERE id=? AND user_id=?",
                                     (attachment_id, principal.user_id))
    if not row:
        raise NotFoundError("Attachment not found.")
    return FileResponse(row["storage_path"], media_type=row["mime"], filename=row["filename"])
