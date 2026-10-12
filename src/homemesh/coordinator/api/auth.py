"""Authentication API: first-run setup, login/logout, sessions, password, API tokens."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from homemesh.common.errors import AuthError, NotFoundError
from homemesh.coordinator.api.common import ip_of, state
from homemesh.coordinator.auth import SESSION_COOKIE, Principal, current_principal, require_owner
from homemesh.coordinator.db.repositories.users import public_user

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class SetupBody(BaseModel):
    setup_token: str = Field(max_length=200)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(default="", max_length=100)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    ttl_days: int | None = Field(default=90, ge=1, le=3650)


@router.get("/status")
async def auth_status(request: Request) -> dict[str, Any]:
    st = state(request)
    setup_required = await st.auth.setup_required()
    user = None
    try:
        user = (await st.auth.authenticate(request)).public()
    except Exception:
        user = None
    return {"setup_required": setup_required, "authenticated": user is not None, "user": user}


@router.post("/setup")
async def setup(body: SetupBody, request: Request, response: Response) -> dict[str, Any]:
    st = state(request)
    user = await st.auth.setup_owner(body.setup_token, body.username, body.password, body.display_name,
                                     ip=ip_of(request))
    await st.auth.ensure_cli_token()
    token, info = await st.auth.login(body.username, body.password, ip=ip_of(request),
                                      user_agent=request.headers.get("user-agent"))
    response.set_cookie(value=token, **st.auth.cookie_kwargs())
    return {"user": public_user(user), "expires_at": info["expires_at"]}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    st = state(request)
    if await st.auth.setup_required():
        raise AuthError("HomeMesh has not been set up yet.", code="setup_required")
    token, info = await st.auth.login(body.username, body.password, ip=ip_of(request),
                                      user_agent=request.headers.get("user-agent"))
    response.set_cookie(value=token, **st.auth.cookie_kwargs())
    return {"user": public_user(info["user"]), "expires_at": info["expires_at"]}


@router.post("/logout")
async def logout(request: Request, response: Response, principal: Principal = Depends(current_principal)) -> dict:
    await state(request).auth.logout(principal)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return principal.public()


@router.post("/password")
async def change_password(body: PasswordBody, request: Request,
                          principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    await state(request).auth.change_password(principal, body.current_password, body.new_password)
    return {"ok": True, "other_sessions_signed_out": True}


@router.get("/sessions")
async def sessions(request: Request, principal: Principal = Depends(current_principal)) -> list[dict[str, Any]]:
    rows = await state(request).repos.sessions.list_for_user(principal.user_id)
    for r in rows:
        r["current"] = r["id"] == principal.session_id
    return rows


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request,
                         principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    st = state(request)
    if not await st.repos.sessions.revoke(session_id, principal.user_id):
        raise NotFoundError("Session not found.")
    await st.repos.audit.add("user", principal.user_id, "auth.session_revoked", target_type="session",
                             target_id=session_id)
    return {"ok": True}


@router.get("/tokens")
async def list_tokens(request: Request, principal: Principal = Depends(require_owner())) -> list[dict[str, Any]]:
    return await state(request).repos.api_tokens.list_for_user(principal.user_id)


@router.post("/tokens")
async def create_token(body: TokenBody, request: Request,
                       principal: Principal = Depends(require_owner())) -> dict[str, Any]:
    token, row = await state(request).auth.create_api_token(principal.user_id, body.name, body.scopes, body.ttl_days)
    return {"token": token, "token_info": row, "note": "Store this token now; it cannot be shown again."}


@router.delete("/tokens/{token_id}")
async def revoke_token(token_id: str, request: Request, principal: Principal = Depends(require_owner())) -> dict:
    st = state(request)
    if not await st.repos.api_tokens.revoke(token_id, principal.user_id):
        raise NotFoundError("Token not found.")
    await st.repos.audit.add("user", principal.user_id, "auth.token_revoked", target_type="api_token",
                             target_id=token_id)
    return {"ok": True}
