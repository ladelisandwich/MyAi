"""Settings API: user/app settings (database) and a read-only view of machine configuration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from homemesh.common.config import config_to_dict
from homemesh.coordinator.api.common import state
from homemesh.coordinator.auth import Principal, require, require_owner

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


@router.get("")
async def get_settings(request: Request, principal: Principal = Depends(require("read"))) -> dict[str, Any]:
    st = state(request)
    return {"settings": await st.settings.all(), "features": features(st)}


def features(st: Any) -> dict[str, Any]:
    """What is implemented and enabled - the UI uses this to label unavailable features honestly."""
    return {
        "voice": {"available": False, "reason": "Voice input/output is planned (see ROADMAP.md); not implemented yet."},
        "image_attachments": {"available": True, "reason": "Images are sent only to models that report vision support."},
        "document_attachments": {"available": False,
                                 "reason": "Attach documents by indexing a folder on the Knowledge page."},
        "cloud_models": {"available": st.cfg.cloud.enabled,
                         "reason": "Disabled by default. Enable [cloud] in homemesh.toml to allow cloud models."},
        "model_downloads": {"available": st.cfg.inference.allow_model_downloads,
                            "reason": "Downloads contact the backend's model registry over the internet."},
        "discovery": {"available": st.cfg.server.discovery_enabled,
                      "reason": "LAN discovery is optional; workers can always join with the coordinator URL."},
        "mock_inference": {"available": st.cfg.inference.mock_enabled,
                           "reason": "Mock backend for testing only; it produces canned text."},
    }


@router.patch("/{section}")
async def update_section(section: str, patch: dict[str, Any], request: Request,
                         principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    return await state(request).settings.update(section, patch, principal.user_id)


@router.get("/machine/config")
async def machine_config(request: Request, principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    st = state(request)
    data = config_to_dict(st.cfg)
    return {"config": data, "config_file": str(st.paths.config_file), "data_dir": str(st.paths.data_dir),
            "note": "Machine configuration is edited in homemesh.toml on this computer (then restart)."}
