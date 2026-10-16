"""Remote updates (owner only; docs/API.md "Remote updates", docs/UPDATING.md).

Checks happen only when the owner clicks; nothing here runs on a timer. The worker and the update runner on
the target computer re-check everything that matters (consent, update lock, tracked branch, commit sha).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from homemesh.coordinator.api.common import ip_of, state
from homemesh.coordinator.auth import Principal, require_owner
from homemesh.protocol.update import UpdateApplyPayload

router = APIRouter(prefix="/api/v1/nodes", tags=["updates"])


@router.post("/{node_id}/update/check", status_code=202)
async def check_for_updates(node_id: str, request: Request,
                            principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    """Ask the computer to check the branch it tracks. The result lands on the node (``update_check``)."""
    return await state(request).updates.check(node_id, principal.user_id, ip=ip_of(request))


@router.post("/{node_id}/update/apply", status_code=202)
async def apply_update(node_id: str, body: UpdateApplyPayload, request: Request,
                       principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    """Install ``expected_commit`` (the upstream tip the last check showed); progress arrives as reports."""
    return await state(request).updates.apply(node_id, body.expected_commit, body.force, principal.user_id,
                                              ip=ip_of(request))


@router.post("/{node_id}/update/dismiss")
async def dismiss_update(node_id: str, request: Request,
                         principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    """Stop waiting for an update that has not reported for 15 minutes ("outcome unknown")."""
    return await state(request).updates.dismiss(node_id, principal.user_id, ip=ip_of(request))
