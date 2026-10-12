"""Authentication and authorisation.

* **Owner account** with an Argon2id password. Created either by ``homemesh setup`` (CLI,
  direct database access on the coordinator machine) or in the web UI first-run screen,
  which requires a one-time *setup token* printed to the coordinator console and stored in
  ``<data>/secrets/setup-token.txt``. Without the token nobody on the network can claim an
  unconfigured installation.
* **Browser sessions**: opaque random tokens in an HttpOnly, SameSite=Strict cookie (Secure
  under TLS); only SHA-256 hashes are stored. State-changing requests with cookie auth must
  carry the ``X-HomeMesh-CSRF`` header, which a cross-site page cannot set without CORS.
* **API tokens** (``Authorization: Bearer hm_...``) with scopes ``read``, ``chat``,
  ``admin``. The CLI uses a locally stored admin token.
* **Nodes** authenticate separately (see ``nodes.py``) and never receive user privileges.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from fastapi import Depends, Request

from homemesh.common.errors import AuthError, ConflictError, PermissionDenied, ValidationFailed
from homemesh.common.logging import get_logger
from homemesh.common.paths import AppPaths, write_private_file
from homemesh.common.security import (
    constant_time_eq,
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    validate_password_strength,
    verify_password,
)
from homemesh.common.timeutil import iso, parse_iso, utcnow
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.middleware import RateLimiter

log = get_logger(__name__)

SESSION_COOKIE = "hm_session"
CSRF_HEADER = "x-homemesh-csrf"
ALL_SCOPES = ("read", "chat", "admin")
CLI_TOKEN_NAME = "cli-admin"  # noqa: S105 - token *name*, not a secret
# A dummy hash so that unknown usernames cost the same time as wrong passwords.
_DUMMY_HASH = hash_password("homemesh-timing-equaliser")


@dataclass
class Principal:
    user_id: str
    username: str
    display_name: str
    role: str
    kind: str  # session | token
    scopes: frozenset[str] = field(default_factory=frozenset)
    session_id: str | None = None
    token_id: str | None = None

    def has(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes

    def public(self) -> dict[str, Any]:
        return {"id": self.user_id, "username": self.username, "display_name": self.display_name,
                "role": self.role, "auth": self.kind, "scopes": sorted(self.scopes)}


class AuthService:
    def __init__(self, repos: Repositories, paths: AppPaths, *, session_ttl_hours: int,
                 login_attempts_per_minute: int, secure_cookies: bool) -> None:
        self.repos = repos
        self.paths = paths
        self.session_ttl = timedelta(hours=session_ttl_hours)
        self.login_limit = login_attempts_per_minute
        self.secure_cookies = secure_cookies
        self.limiter = RateLimiter()

    # ------------------------------------------------------------------ first run
    async def setup_required(self) -> bool:
        return await self.repos.users.count() == 0

    async def ensure_setup_token(self) -> str | None:
        """Create (or reuse) the one-time setup token while no owner exists."""
        if not await self.setup_required():
            if self.paths.setup_token_file.exists():
                self.paths.setup_token_file.unlink()
            return None
        f = self.paths.setup_token_file
        if f.exists():
            token = f.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = generate_token(18)
        write_private_file(f, token + "\n")
        return token

    async def setup_owner(self, setup_token: str, username: str, password: str, display_name: str = "",
                          ip: str | None = None) -> dict[str, Any]:
        self.limiter.check("setup", ip or "?", 10)
        if not await self.setup_required():
            raise ConflictError("HomeMesh is already set up.", code="already_setup")
        expected = await self.ensure_setup_token()
        if not expected or not constant_time_eq(setup_token.strip(), expected):
            await self.repos.audit.add("system", None, "auth.setup", outcome="failure", ip=ip,
                                       details={"reason": "bad setup token"})
            raise AuthError("The setup token is incorrect.", code="bad_setup_token",
                            hint="Find it in the coordinator's console output or in "
                                 f"{self.paths.setup_token_file}, or create the owner with 'homemesh setup'.")
        user = await self.create_owner(username, password, display_name)
        await self.repos.audit.add("user", user["id"], "auth.setup", ip=ip)
        return user

    async def create_owner(self, username: str, password: str, display_name: str = "") -> dict[str, Any]:
        username = username.strip()
        if not username or len(username) > 64 or not all(c.isalnum() or c in "._-" for c in username):
            raise ValidationFailed("Username must be 1-64 characters: letters, digits, '.', '_' or '-'.")
        if err := validate_password_strength(password):
            raise ValidationFailed(err, code="weak_password")
        if await self.repos.users.count() > 0:
            raise ConflictError("An owner account already exists.", code="already_setup")
        user = await self.repos.users.create(username, hash_password(password), display_name or username, "owner")
        if self.paths.setup_token_file.exists():
            self.paths.setup_token_file.unlink()
        return user

    # ------------------------------------------------------------------ sessions
    async def login(self, username: str, password: str, *, ip: str | None, user_agent: str | None
                    ) -> tuple[str, dict[str, Any]]:
        self.limiter.check("login-ip", ip or "?", self.login_limit)
        self.limiter.check("login-user", username.lower(), self.login_limit)
        user = await self.repos.users.by_username(username.strip())
        ok = verify_password(user["password_hash"] if user else _DUMMY_HASH, password)
        if not user or not ok or user.get("disabled"):
            await self.repos.audit.add("user", user["id"] if user else None, "auth.login", outcome="failure", ip=ip,
                                       details={"username": username[:64]})
            raise AuthError("Incorrect username or password.", code="invalid_credentials")
        if password_needs_rehash(user["password_hash"]):
            await self.repos.users.set_password(user["id"], hash_password(password))
        token = generate_token(32)
        expires = iso(utcnow() + self.session_ttl)
        sid = await self.repos.sessions.create(user["id"], hash_token(token), expires, user_agent, ip)
        self.limiter.reset("login-user", username.lower())
        await self.repos.audit.add("user", user["id"], "auth.login", ip=ip, details={"session_id": sid})
        return token, {"session_id": sid, "expires_at": expires, "user": user}

    async def logout(self, principal: Principal) -> None:
        if principal.session_id:
            await self.repos.sessions.revoke(principal.session_id)
            await self.repos.audit.add("user", principal.user_id, "auth.logout")

    async def change_password(self, principal: Principal, current: str, new: str) -> None:
        user = await self.repos.users.get(principal.user_id)
        if not user or not verify_password(user["password_hash"], current):
            raise AuthError("Current password is incorrect.", code="invalid_credentials")
        if err := validate_password_strength(new):
            raise ValidationFailed(err, code="weak_password")
        await self.repos.users.set_password(principal.user_id, hash_password(new))
        await self.repos.sessions.revoke_all_for_user(principal.user_id, except_id=principal.session_id)
        await self.repos.audit.add("user", principal.user_id, "auth.password_changed")

    # ------------------------------------------------------------------ tokens
    async def create_api_token(self, user_id: str, name: str, scopes: list[str], ttl_days: int | None
                               ) -> tuple[str, dict[str, Any]]:
        bad = [s for s in scopes if s not in ALL_SCOPES]
        if bad or not scopes:
            raise ValidationFailed(f"Invalid scopes {bad or scopes}; choose from {list(ALL_SCOPES)}.")
        token = generate_token(32, prefix="hm_")
        expires = iso(utcnow() + timedelta(days=ttl_days)) if ttl_days else None
        row = await self.repos.api_tokens.create(user_id, name[:100], hash_token(token), scopes, expires)
        await self.repos.audit.add("user", user_id, "auth.token_created", target_type="api_token",
                                   target_id=row["id"], details={"name": name, "scopes": scopes})
        return token, row

    async def ensure_cli_token(self) -> str | None:
        """Make sure the local CLI admin token file exists and is valid (owner must exist)."""
        owner = await self.repos.users.owner()
        if not owner:
            return None
        f = self.paths.cli_token_file
        if f.exists():
            token = f.read_text(encoding="utf-8").strip()
            row = await self.repos.api_tokens.by_hash(hash_token(token)) if token else None
            if row and not row.get("revoked_at"):
                return token
        await self.repos.api_tokens.revoke_by_name(owner["id"], CLI_TOKEN_NAME)
        token, _ = await self.create_api_token(owner["id"], CLI_TOKEN_NAME, ["admin"], None)
        write_private_file(f, token + "\n")
        return token

    # ------------------------------------------------------------------ request authentication
    async def authenticate(self, request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            row = await self.repos.api_tokens.by_hash(hash_token(token))
            if not row or row.get("revoked_at") or row.get("disabled"):
                raise AuthError("Invalid or revoked API token.", code="invalid_token")
            if row.get("expires_at") and parse_iso(row["expires_at"]) < utcnow():  # type: ignore[operator]
                raise AuthError("API token has expired.", code="token_expired")
            await self.repos.api_tokens.touch(row["id"])
            return Principal(row["user_id"], row["username"], row["display_name"], row["role"], "token",
                             frozenset(row["scopes"]), token_id=row["id"])
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            raise AuthError("Authentication required.", code="not_authenticated")
        row = await self.repos.sessions.by_token_hash(hash_token(cookie))
        if not row or row.get("revoked_at") or row.get("disabled"):
            raise AuthError("Your session has ended. Please sign in again.", code="session_invalid")
        if parse_iso(row["expires_at"]) < utcnow():  # type: ignore[operator]
            raise AuthError("Your session has expired. Please sign in again.", code="session_expired")
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            raise PermissionDenied("Missing CSRF header.", code="csrf_failed",
                                   hint="Requests from the HomeMesh web app send this automatically.")
        last = parse_iso(row.get("last_seen_at"))
        if last is None or (utcnow() - last).total_seconds() > 60:
            await self.repos.sessions.touch(row["id"])
        return Principal(row["user_id"], row["username"], row["display_name"], row["role"], "session",
                         frozenset(ALL_SCOPES), session_id=row["id"])

    def cookie_kwargs(self) -> dict[str, Any]:
        return {"key": SESSION_COOKIE, "httponly": True, "samesite": "strict", "secure": self.secure_cookies,
                "path": "/", "max_age": int(self.session_ttl.total_seconds())}


# ---------------------------------------------------------------------- FastAPI dependencies

async def current_principal(request: Request) -> Principal:
    svc: AuthService = request.app.state.hm.auth
    principal = await svc.authenticate(request)
    request.state.principal = principal
    return principal


def require(scope: str = "read"):
    async def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has(scope):
            raise PermissionDenied(f"This action requires the '{scope}' scope.", code="insufficient_scope")
        return principal

    return _dep


def require_owner():
    async def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role != "owner" or not principal.has("admin"):
            raise PermissionDenied("Only the owner can do this.", code="owner_required")
        return principal

    return _dep
