"""Authentication, authorisation and HTTP hardening against a running coordinator."""

import httpx
import pytest

from harness import PASSWORD, Harness

pytestmark = pytest.mark.slow


@pytest.fixture
async def h(tmp_path):
    harness = Harness(tmp_path)
    await harness.start(login_attempts_per_minute=5)
    yield harness
    await harness.stop()


async def test_setup_requires_token_and_happens_once(h):
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False) as c:
        st = (await c.get("/api/v1/auth/status")).json()
        assert st["setup_required"] and not st["authenticated"]
        r = await c.post("/api/v1/auth/setup", json={"setup_token": "wrong", "username": "x", "password": PASSWORD})
        assert r.status_code == 401 and r.json()["error"]["code"] == "bad_setup_token"
        token = h.paths.setup_token_file.read_text().strip()
        r = await c.post("/api/v1/auth/setup", json={"setup_token": token, "username": "owner", "password": "short"})
        assert r.status_code == 422
        r = await c.post("/api/v1/auth/setup", json={"setup_token": token, "username": "owner", "password": PASSWORD})
        assert r.status_code == 200
        r = await c.post("/api/v1/auth/setup", json={"setup_token": token, "username": "evil", "password": PASSWORD})
        assert r.status_code in (401, 409)
        assert not h.paths.setup_token_file.exists()


async def test_protected_endpoints_require_auth(h):
    await h.owner_client()
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False) as anon:
        for path in ("/api/v1/conversations", "/api/v1/nodes", "/api/v1/memories", "/api/v1/jobs", "/api/v1/models",
                     "/api/v1/settings", "/api/v1/system/status", "/api/v1/knowledge/collections", "/api/v1/backups",
                     "/api/v1/system/audit", "/api/v1/tools"):
            r = await anon.get(path)
            assert r.status_code == 401, path
        r = await anon.post("/api/v1/nodes/pairing", json={})
        assert r.status_code == 401
        # health is public but reveals no personal data
        health = (await anon.get("/api/v1/system/health")).json()
        assert set(health) == {"status", "version", "protocol_version", "database", "setup_required", "tls"}


async def test_cookie_session_needs_csrf_header_for_writes(h):
    c = await h.owner_client()
    r = await c.post("/api/v1/conversations", json={}, headers={"X-HomeMesh-CSRF": ""})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    assert (await c.post("/api/v1/conversations", json={})).status_code == 200


async def test_login_rate_limit_and_generic_errors(h):
    await h.owner_client()
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False) as c:
        codes = []
        for _ in range(7):
            r = await c.post("/api/v1/auth/login", json={"username": "owner", "password": "wrong password"})
            codes.append(r.status_code)
        # the successful setup login also counts towards the per-IP window
        assert codes[:4] == [401] * 4 and codes[-1] == 429
        r = await c.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
        assert r.status_code in (401, 429)


async def test_api_token_scopes_and_revocation(h):
    c = await h.owner_client()
    tok = (await c.post("/api/v1/auth/tokens", json={"name": "ro", "scopes": ["read"]})).json()["token"]
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False,
                                 headers={"Authorization": f"Bearer {tok}"}) as t:
        assert (await t.get("/api/v1/nodes")).status_code == 200
        r = await t.post("/api/v1/conversations", json={})  # needs 'chat'
        assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
        assert (await t.post("/api/v1/nodes/pairing", json={})).status_code == 403
        tokens = (await c.get("/api/v1/auth/tokens")).json()
        tid = next(x["id"] for x in tokens if x["name"] == "ro")
        assert (await c.delete(f"/api/v1/auth/tokens/{tid}")).status_code == 200
        assert (await t.get("/api/v1/nodes")).status_code == 401
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False,
                                 headers={"Authorization": "Bearer hm_forged"}) as bad:
        assert (await bad.get("/api/v1/nodes")).status_code == 401


async def test_logout_and_password_change_end_sessions(h):
    c = await h.owner_client()
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False, headers={"X-HomeMesh-CSRF": "1"}) as other:
        assert (await other.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})).status_code == 200
        r = await c.post("/api/v1/auth/password", json={"current_password": PASSWORD,
                                                        "new_password": "another long passphrase"})
        assert r.status_code == 200
        assert (await other.get("/api/v1/nodes")).status_code == 401  # other device signed out
    assert (await c.get("/api/v1/nodes")).status_code == 200  # current session kept
    await c.post("/api/v1/auth/logout")
    assert (await c.get("/api/v1/nodes")).status_code == 401


async def test_request_size_limit_and_security_headers(h):
    c = await h.owner_client()
    r = await c.post("/api/v1/conversations", content=b"{" + b" " * (11 * 1024 * 1024) + b"}",
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = await c.get("/api/v1/system/health")
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_validation_errors_are_readable(h):
    c = await h.owner_client()
    r = await c.post("/api/v1/memories", json={"content": ""})
    assert r.status_code == 422 and "content" in r.json()["error"]["message"]


async def test_cross_user_isolation_by_id(h):
    c = await h.owner_client()
    r = await c.get("/api/v1/conversations/conv_doesnotexist")
    assert r.status_code == 404
    r = await c.get("/api/v1/backups/../../etc/passwd")
    assert r.status_code in (404, 405)


async def test_audit_log_records_security_events(h):
    c = await h.owner_client()
    async with httpx.AsyncClient(base_url=h.base_url, trust_env=False) as anon:
        await anon.post("/api/v1/auth/login", json={"username": "owner", "password": "nope nope nope"})
    events = (await c.get("/api/v1/system/audit")).json()
    actions = {(e["action"], e["outcome"]) for e in events}
    assert ("auth.setup", "success") in actions and ("auth.login", "failure") in actions
