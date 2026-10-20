"""Dashboard security: first-run setup, login, sessions, CSRF, rate limits, headers, secrets."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.routing import APIRoute

from littlebot.dashboard.app import PUBLIC_API_PATHS
from littlebot.dashboard.routes import ROUTERS
from littlebot.dashboard.auth import (
    PASSWORD_KEY, SESSION_COOKIE, AuthManager, hash_password, normalise_setup_code, verify_password,
)

from dashboard_fakes import FAKE_ANTHROPIC_KEY, FAKE_OPENAI_KEY, PASSWORD, do_setup, make_client


# ------------------------------------------------------------------ setup
async def test_fresh_install_requires_setup(client):
    r = await client.get("/api/session")
    assert r.status_code == 200
    assert r.json()["authenticated"] is False
    assert r.json()["setup_required"] is True
    assert "csrf_token" not in r.json()


async def test_setup_rejects_wrong_code_and_stores_nothing(client, app, dctx):
    app.state.auth.issue_setup_code()
    r = await client.post("/api/setup", json={"setup_code": "AAAA-BBBB", "password": PASSWORD})
    assert r.status_code == 401
    assert "setup code" in r.json()["detail"].lower()
    assert dctx.db.kv_get(PASSWORD_KEY) is None
    assert SESSION_COOKIE not in r.cookies


async def test_setup_without_issued_code_is_rejected(client):
    r = await client.post("/api/setup", json={"setup_code": "ABCD-EFGH", "password": PASSWORD})
    assert r.status_code == 401


async def test_setup_rejects_short_password(client, app):
    code = app.state.auth.issue_setup_code()
    r = await client.post("/api/setup", json={"setup_code": code, "password": "short"})
    assert r.status_code == 422
    assert "8" in r.json()["detail"]


async def test_setup_success_sets_secure_cookie_and_logs_in(client, app, dctx):
    code = app.state.auth.issue_setup_code()
    r = await client.post("/api/setup", json={"setup_code": code.lower().replace("-", " "), "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authenticated"] is True and body["csrf_token"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # plain http on the LAN
    # Password is stored hashed with scrypt, never in plain text.
    rec = dctx.db.kv_get(PASSWORD_KEY)
    assert rec["algo"] == "scrypt" and PASSWORD not in repr(rec)
    s = await client.get("/api/session")
    assert s.json()["authenticated"] is True
    st = await client.get("/api/status")
    assert st.status_code == 200


async def test_setup_cannot_be_run_twice(authed, app):
    code = app.state.auth.issue_setup_code()
    r = await authed.post("/api/setup", json={"setup_code": code, "password": "another password"})
    assert r.status_code == 409
    async with make_client(app, "10.0.0.9") as other:
        r2 = await other.post("/api/setup", json={"setup_code": code, "password": "another password"})
        assert r2.status_code == 409
        assert (await other.post("/api/login", json={"password": PASSWORD})).status_code == 200


async def test_secure_cookie_over_https(app):
    import httpx

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="https://robot.local") as c:
        code = app.state.auth.issue_setup_code()
        r = await c.post("/api/setup", json={"setup_code": code, "password": PASSWORD})
        assert r.status_code == 200
        assert "secure" in r.headers["set-cookie"].lower()


def test_setup_code_is_burned_after_too_many_failures(dctx):
    auth = AuthManager(dctx.db, attempts_per_minute=100)
    code = auth.issue_setup_code()
    for _ in range(10):
        assert not auth._check_setup_code("ZZZZ-ZZZZ")
    assert not auth._check_setup_code(code)  # old code no longer valid; a new one was logged
    assert auth.setup_code_active


def test_setup_code_format_and_normalisation(dctx):
    auth = AuthManager(dctx.db)
    code = auth.issue_setup_code()
    assert len(code) == 9 and code[4] == "-"
    assert normalise_setup_code(f" {code.lower()} ") == code.replace("-", "")
    assert auth._check_setup_code(code.lower())


def test_password_hash_is_salted_and_verifies():
    a, b = hash_password("same password"), hash_password("same password")
    assert a["salt"] != b["salt"] and a["hash"] != b["hash"]
    assert verify_password("same password", a)
    assert not verify_password("same passworD", a)
    assert not verify_password("anything", None)


# ------------------------------------------------------------------ login
async def test_login_success_and_failure(app):
    async with make_client(app) as c:
        await do_setup(c, app)
    async with make_client(app, "10.0.0.2") as c:
        r = await c.post("/api/login", json={"password": "wrong password"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Incorrect password."
        assert SESSION_COOKIE not in r.cookies
        r = await c.post("/api/login", json={"password": PASSWORD})
        assert r.status_code == 200 and r.json()["csrf_token"]
        assert (await c.get("/api/status")).status_code == 200


async def test_login_before_setup_is_refused(client):
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 409


async def test_login_rate_limited_per_ip(app):
    async with make_client(app) as c:
        await do_setup(c, app)
    limit = app.state.ctx.config.dashboard.login_attempts_per_minute
    async with make_client(app, "10.0.0.3") as attacker:
        for _ in range(limit):
            r = await attacker.post("/api/login", json={"password": "guess"})
            assert r.status_code == 401
        r = await attacker.post("/api/login", json={"password": PASSWORD})
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
    async with make_client(app, "10.0.0.4") as other:  # another device is unaffected
        assert (await other.post("/api/login", json={"password": PASSWORD})).status_code == 200


async def test_setup_attempts_rate_limited(client, app):
    app.state.auth.issue_setup_code()
    codes = [(await client.post("/api/setup", json={"setup_code": "WXYZ-WXYZ", "password": PASSWORD})).status_code
             for _ in range(7)]
    assert codes[:5] == [401] * 5 and 429 in codes


async def test_login_requires_json_and_same_origin(app):
    async with make_client(app) as c:
        await do_setup(c, app)
    async with make_client(app, "10.0.0.5") as c:
        r = await c.post("/api/login", content=b"password=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415
        r = await c.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        r = await c.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "http://testserver"})
        assert r.status_code == 200


async def test_validation_errors_do_not_echo_passwords(client):
    secret = "x" * 300 + "-my-secret-password"
    r = await client.post("/api/login", json={"password": secret})
    assert r.status_code == 422
    assert "my-secret-password" not in r.text


# ------------------------------------------------------------------ sessions
async def test_logout_invalidates_session(authed):
    cookie = authed.cookies.get(SESSION_COOKIE)
    r = await authed.post("/api/logout")
    assert r.status_code == 200
    assert (await authed.get("/api/status")).status_code == 401
    authed.cookies.set(SESSION_COOKIE, cookie)  # replaying the old cookie does not work either
    assert (await authed.get("/api/status")).status_code == 401


async def test_logout_all_ends_every_session(app):
    async with make_client(app) as a:
        await do_setup(a, app)
        async with make_client(app, "10.0.0.6") as b:
            r = await b.post("/api/login", json={"password": PASSWORD})
            b.headers["X-CSRF-Token"] = r.json()["csrf_token"]
            assert (await b.get("/api/auth/sessions")).json()["active_sessions"] == 2
            assert (await a.post("/api/auth/logout-all")).status_code == 200
            assert (await a.get("/api/status")).status_code == 401
            assert (await b.get("/api/status")).status_code == 401


async def test_session_expires(authed, app, dctx):
    auth = app.state.auth
    assert (await authed.get("/api/status")).status_code == 200
    real = time.time
    auth._now = lambda: real() + dctx.config.dashboard.session_hours * 3600 + 5
    r = await authed.get("/api/status")
    assert r.status_code == 401 and r.json()["code"] == "auth_required"


async def test_sessions_are_stored_hashed(authed, dctx):
    token = authed.cookies.get(SESSION_COOKIE)
    rows = dctx.db.query("SELECT * FROM dashboard_sessions")
    assert len(rows) == 1
    assert token not in repr(rows)


async def test_sessions_survive_restart(authed, dctx):
    from littlebot.dashboard import create_app

    app2 = create_app(dctx)  # simulates a restart against the same database
    async with make_client(app2) as c:
        c.cookies.set(SESSION_COOKIE, authed.cookies.get(SESSION_COOKIE))
        assert (await c.get("/api/status")).status_code == 200


async def test_change_password(app):
    async with make_client(app) as a:
        await do_setup(a, app)
        async with make_client(app, "10.0.0.7") as b:
            await b.post("/api/login", json={"password": PASSWORD})
            r = await a.post("/api/auth/password", json={"current_password": "nope nope", "new_password": "new password 1"})
            assert r.status_code == 403
            r = await a.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"})
            assert r.status_code == 422
            r = await a.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "new password 1"})
            assert r.status_code == 200
            assert (await a.get("/api/status")).status_code == 200  # this device stays logged in
            assert (await b.get("/api/status")).status_code == 401  # others are logged out
    async with make_client(app, "10.0.0.8") as c:
        assert (await c.post("/api/login", json={"password": PASSWORD})).status_code == 401
        assert (await c.post("/api/login", json={"password": "new password 1"})).status_code == 200


# ------------------------------------------------------------------ CSRF
async def test_csrf_missing_or_wrong_is_rejected(authed, fakes):
    good = authed.headers.pop("X-CSRF-Token")
    r = await authed.post("/api/face/expression", json={"name": "happy"})
    assert r.status_code == 403 and r.json()["code"] == "csrf"
    r = await authed.post("/api/face/expression", json={"name": "happy"}, headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    r = await authed.post("/api/stop", headers={"X-CSRF-Token": good + "x"})
    assert r.status_code == 403
    assert fakes["robot"].called("stop_all") == []
    r = await authed.post("/api/face/expression", json={"name": "happy"}, headers={"X-CSRF-Token": good})
    assert r.status_code == 200


async def test_cross_origin_mutation_rejected_even_with_token(authed):
    r = await authed.post("/api/stop", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "origin"
    r = await authed.post("/api/stop", headers={"Referer": "http://evil.example/page"})
    assert r.status_code == 403
    r = await authed.post("/api/stop", headers={"Origin": "null"})
    assert r.status_code == 403
    r = await authed.post("/api/stop", headers={"Origin": "http://testserver", "Referer": "http://testserver/#/home"})
    assert r.status_code == 200


async def test_csrf_token_is_per_session(app):
    async with make_client(app) as a:
        data = await do_setup(a, app)
        async with make_client(app, "10.0.0.11") as b:
            r = await b.post("/api/login", json={"password": PASSWORD})
            assert r.json()["csrf_token"] != data["csrf_token"]
            b.headers["X-CSRF-Token"] = data["csrf_token"]  # someone else's token
            assert (await b.post("/api/stop")).status_code == 403


# ------------------------------------------------------------------ coverage of protected routes
def _protected_routes(app=None):
    """Every API route the dashboard defines (taken from the routers the app includes)."""
    for router in ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute) and route.path.startswith("/api") and route.path not in PUBLIC_API_PATHS:
                for method in route.methods:
                    yield method, route.path


def _fill(path: str) -> str:
    return path.replace("{activity_id}", "peekaboo").replace("{item_id}", "1")


async def test_every_protected_route_requires_auth(client, app):
    routes = sorted(set(_protected_routes(app)))
    assert len(routes) > 60
    for method, path in routes:
        r = await client.request(method, _fill(path), json={})
        assert r.status_code == 401, (method, path, r.status_code)
    # Unknown API paths don't reveal anything either.
    assert (await client.get("/api/does-not-exist")).status_code == 401


async def test_public_endpoints_are_minimal(client):
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


# ------------------------------------------------------------------ headers
async def test_security_headers(client):
    for path in ("/", "/api/health", "/static/app.css", "/api/status"):
        r = await client.get(path)
        h = r.headers
        assert h["x-content-type-options"] == "nosniff", path
        assert h["referrer-policy"] == "same-origin", path
        assert h["x-frame-options"] == "DENY", path
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self'" in csp, path
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert (await client.get("/api/health")).headers["cache-control"] == "no-store"


async def test_index_and_static_served(client):
    r = await client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert '<script type="module" src="/static/js/app.js">' in r.text
    assert "<script>" not in r.text  # no inline scripts (CSP)
    for path in ("/static/js/app.js", "/static/js/api.js", "/static/js/ui.js", "/static/js/pages/home.js",
                 "/static/favicon.svg", "/setup", "/login"):
        assert (await client.get(path)).status_code == 200, path
    assert (await client.get("/static/../auth.py")).status_code == 404


def test_frontend_has_no_external_resources():
    from littlebot.dashboard.app import STATIC_DIR

    for f in STATIC_DIR.rglob("*"):
        if f.suffix in (".html", ".css", ".js"):
            text = f.read_text(encoding="utf-8")
            # Allowed: the SVG namespace string and approved-video thumbnails (also allowed by the CSP).
            for ok in ("http://www.w3.org/2000/svg", "https://i.ytimg.com/vi/"):
                text = text.replace(ok, "")
            loads = re.findall(r"""(?:src|href)\s*[:=]\s*[`'"]https?://|url\(\s*['"]?https?://|from\s+['"]https?://"""
                               r"""|fetch\(\s*[`'"]https?://|@import""", text)
            assert not loads, (f.name, loads)
            assert "innerHTML" not in text and "eval(" not in text, f.name


# ------------------------------------------------------------------ secrets
async def test_api_keys_never_returned(authed, app, dctx, fakes):
    dctx.bus.publish("test.leak", {"openai_api_key": FAKE_OPENAI_KEY, "note": f"key {FAKE_ANTHROPIC_KEY}"})
    texts = []
    for method, path in sorted(set(_protected_routes(app))):
        if method == "GET":
            r = await authed.get(_fill(path))
            texts.append(r.text)
    for path in ("/api/session", "/api/health"):
        texts.append((await authed.get(path)).text)
    texts.append((await authed.put("/api/ai/settings", json={"provider": "anthropic"})).text)
    texts.append((await authed.post("/api/ai/test", json={"provider": "mock"})).text)
    blob = "\n".join(texts)
    assert FAKE_ANTHROPIC_KEY not in blob and FAKE_OPENAI_KEY not in blob
    assert "TESTSECRET" not in blob
    ai = (await authed.get("/api/ai")).json()
    assert ai["keys"] == {"has_anthropic_key": True, "has_openai_key": True}
    cfg = (await authed.get("/api/system/config")).json()
    assert cfg["has_anthropic_key"] is True
    assert "anthropic_api_key" not in cfg
