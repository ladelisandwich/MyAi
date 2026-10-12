"""Backup API: create and download backups. Restore is CLI-only (the coordinator must be stopped)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from homemesh.common.errors import NotFoundError, ValidationFailed
from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require_owner
from homemesh.coordinator.backup import create_backup, list_backups

router = APIRouter(prefix="/api/v1/backups", tags=["backups"])


class BackupBody(BaseModel):
    passphrase: str | None = Field(default=None, max_length=1024)
    include_attachments: bool = True


@router.get("")
async def backups(request: Request, principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    st = state(request)
    return {"backups": list_backups(st.paths.backups_dir), "directory": str(st.paths.backups_dir)}


@router.post("")
async def create(body: BackupBody, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    st = state(request)
    if body.passphrase is not None and 0 < len(body.passphrase) < 8:
        raise ValidationFailed("Use a passphrase of at least 8 characters (or none).")
    info = await asyncio.to_thread(create_backup, st.paths, passphrase=body.passphrase or None,
                                   include_attachments=body.include_attachments)
    await st.repos.audit.add("user", principal.user_id, "backup.created", target_type="backup",
                             target_id=info.path.name, details={"encrypted": bool(body.passphrase)})
    return {"name": info.path.name, "manifest": {k: v for k, v in info.manifest.items() if k != "files"}}


@router.get("/{name}")
async def download(name: str, request: Request, principal: Principal = Depends(require_owner())) -> FileResponse:
    st = state(request)
    if "/" in name or "\\" in name or not name.startswith("homemesh-backup-"):
        raise NotFoundError("Backup not found.")
    path = st.paths.backups_dir / name
    if not path.is_file():
        raise NotFoundError("Backup not found.")
    await st.repos.audit.add("user", principal.user_id, "backup.downloaded", target_type="backup", target_id=name)
    return FileResponse(path, media_type="application/octet-stream", filename=name)
