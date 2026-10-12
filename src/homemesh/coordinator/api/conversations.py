"""Conversations, messages, streaming chat (SSE), run re-attach/cancel, search, export, projects."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from homemesh.common.errors import NotFoundError
from homemesh.coordinator.api.common import sse_format, sse_response, state
from homemesh.coordinator.auth import Principal, require
from homemesh.coordinator.chat import ChatRun, SendRequest

router = APIRouter(prefix="/api/v1", tags=["conversations"])


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=200)
    project_id: str | None = None
    model: str | None = Field(default=None, max_length=200)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    model: str | None = Field(default=None, max_length=200)
    node_id: str | None = Field(default=None, max_length=100)
    clear_project: bool = False
    clear_model: bool = False
    clear_node: bool = False


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    category: str = Field(default="general", max_length=40)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=40)
    archived: bool | None = None


@router.get("/conversations")
async def list_conversations(request: Request, project_id: str | None = None, archived: bool = False,
                             limit: int = 200, offset: int = 0,
                             principal: Principal = Depends(require("chat"))) -> list[dict[str, Any]]:
    return await state(request).repos.conversations.list(principal.user_id, project_id=project_id,
                                                         include_archived=archived, limit=min(limit, 500),
                                                         offset=offset)


@router.post("/conversations")
async def create_conversation(body: ConversationCreate, request: Request,
                              principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    if body.project_id and not await st.repos.projects.get(body.project_id, principal.user_id):
        raise NotFoundError("Project not found.")
    conv = await st.repos.conversations.create(principal.user_id, body.title, body.project_id,
                                               {"model": body.model} if body.model else {})
    st.bus.publish("conversation.created", {"conversation_id": conv["id"]}, user_id=principal.user_id)
    return conv


@router.get("/conversations/search")
async def search_conversations(q: str, request: Request,
                               principal: Principal = Depends(require("chat"))) -> list[dict[str, Any]]:
    return await state(request).repos.conversations.search(principal.user_id, q[:500])


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, request: Request,
                           principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    conv = await st.repos.conversations.get(conv_id, principal.user_id)
    if not conv:
        raise NotFoundError("Conversation not found.")
    conv["messages"] = await st.repos.messages.list(conv_id)
    conv["active_run"] = next((r.message_id for r in st.chat.runs.values()
                               if r.conversation_id == conv_id and r.status == "streaming"), None)
    return conv


@router.patch("/conversations/{conv_id}")
async def update_conversation(conv_id: str, body: ConversationUpdate, request: Request,
                              principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    conv = await st.repos.conversations.get(conv_id, principal.user_id)
    if not conv:
        raise NotFoundError("Conversation not found.")
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title
        fields["title_source"] = "user"
    if body.project_id is not None:
        if not await st.repos.projects.get(body.project_id, principal.user_id):
            raise NotFoundError("Project not found.")
        fields["project_id"] = body.project_id
    if body.clear_project:
        fields["project_id"] = None
    for k in ("pinned", "archived"):
        v = getattr(body, k)
        if v is not None:
            fields[k] = v
    settings = dict(conv.get("settings_json") or {})
    if body.model is not None:
        settings["model"] = body.model
    if body.node_id is not None:
        settings["node_id"] = body.node_id
    if body.clear_model:
        settings.pop("model", None)
    if body.clear_node:
        settings.pop("node_id", None)
    if settings != (conv.get("settings_json") or {}):
        fields["settings_json"] = settings
    await st.repos.conversations.update(conv_id, principal.user_id, **fields)
    st.bus.publish("conversation.updated", {"conversation_id": conv_id}, user_id=principal.user_id)
    return await st.repos.conversations.get(conv_id, principal.user_id)  # type: ignore[return-value]


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, request: Request,
                              principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    if not await st.repos.conversations.delete(conv_id, principal.user_id):
        raise NotFoundError("Conversation not found.")
    await st.repos.audit.add("user", principal.user_id, "conversation.deleted", target_type="conversation",
                             target_id=conv_id)
    st.bus.publish("conversation.deleted", {"conversation_id": conv_id}, user_id=principal.user_id)
    return {"ok": True}


@router.get("/conversations/{conv_id}/export")
async def export_conversation(conv_id: str, request: Request,
                              principal: Principal = Depends(require("chat"))) -> JSONResponse:
    st = state(request)
    conv = await st.repos.conversations.get(conv_id, principal.user_id)
    if not conv:
        raise NotFoundError("Conversation not found.")
    conv["messages"] = await st.repos.messages.list(conv_id)
    return JSONResponse(conv, headers={"Content-Disposition": f'attachment; filename="{conv_id}.json"'})


async def _run_events(run: ChatRun) -> AsyncIterator[bytes]:
    async for ev in run.subscribe():
        yield sse_format(ev["event"], ev["data"])


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, body: SendRequest, request: Request,
                       principal: Principal = Depends(require("chat"))) -> Any:
    """Send a message and stream the assistant's reply as Server-Sent Events. Generation continues
    (and is saved) even if this request is disconnected."""
    run = await state(request).chat.send(principal.user_id, conv_id, body)
    return sse_response(_run_events(run))


@router.get("/chat/runs/{message_id}/events")
async def reattach(message_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> Any:
    run = state(request).chat.get_run(message_id, principal.user_id)
    if run is None:
        raise NotFoundError("No active response with that id (it may have finished).")
    return sse_response(_run_events(run))


@router.post("/chat/runs/{message_id}/cancel")
async def cancel_run(message_id: str, request: Request, principal: Principal = Depends(require("chat"))) -> dict:
    ok = await state(request).chat.cancel(message_id, principal.user_id)
    return {"ok": ok}


# ---------------------------------------------------------------------- projects

@router.get("/projects")
async def list_projects(request: Request, archived: bool = False,
                        principal: Principal = Depends(require("chat"))) -> list[dict[str, Any]]:
    return await state(request).repos.projects.list(principal.user_id, include_archived=archived)


@router.post("/projects")
async def create_project(body: ProjectBody, request: Request,
                         principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    p = await st.repos.projects.create(principal.user_id, body.name, body.description, body.category)
    st.bus.publish("project.changed", {"project_id": p["id"]}, user_id=principal.user_id)
    return p


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, request: Request,
                         principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    if not await st.repos.projects.get(project_id, principal.user_id):
        raise NotFoundError("Project not found.")
    await st.repos.projects.update(project_id, principal.user_id,
                                   **{k: v for k, v in body.model_dump().items() if v is not None})
    st.bus.publish("project.changed", {"project_id": project_id}, user_id=principal.user_id)
    return await st.repos.projects.get(project_id, principal.user_id)  # type: ignore[return-value]


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request,
                         principal: Principal = Depends(require("chat"))) -> dict[str, Any]:
    st = state(request)
    if not await st.repos.projects.delete(project_id, principal.user_id):
        raise NotFoundError("Project not found.")
    st.bus.publish("project.changed", {"project_id": project_id}, user_id=principal.user_id)
    return {"ok": True, "note": "Project memories were deleted; conversations were kept and unassigned."}
