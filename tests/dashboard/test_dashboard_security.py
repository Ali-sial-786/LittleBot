"""Dashboard hardening: Host allowlist (DNS rebinding), forwarded headers, global login/setup
lockout, request-body caps, sessions revoked from another process, setup-code burning."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat

import httpx
import pytest

from littlebot.core.events import Topics, collect
from littlebot.core.storage import Database
from littlebot.dashboard import DashboardService, create_app
from littlebot.dashboard.app import MAX_BODY_BYTES
from littlebot.dashboard.auth import (
    GLOBAL_LOCKOUT_SECONDS, PASSWORD_KEY, SESSION_COOKIE, SETUP_CODE_MAX_FAILURES_PER_CLIENT, AuthManager,
    FailureLockout,
)
from littlebot.dashboard.deps import split_host
from littlebot.testing import make_test_context

from dashboard_fakes import PASSWORD, do_setup, install_fakes, make_client

J = {"Content-Type": "application/json"}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _client(app, base_url: str, ip: str = "127.0.0.1", **kw) -> httpx.AsyncClient:
    """A test client that does NOT add 'testserver' to the allowlist (unlike make_client)."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 50000)), base_url=base_url, **kw)


@pytest.fixture
def bare_ctx():
    """A context with no extra allowed hosts (the production default)."""
    ctx = make_test_context()
    install_fakes(ctx)
    yield ctx
    ctx.db.close()


# ------------------------------------------------------------------ Host allowlist (finding 3)
async def test_rebinding_host_is_refused_before_any_route(bare_ctx):
    app = create_app(bare_ctx)
    code = app.state.auth.issue_setup_code()
    evil = "attacker.example:8765"
    async with _client(app, f"http://{evil}") as c:
        r = await c.post("/api/setup", headers={"Origin": f"http://{evil}"},
                         json={"setup_code": code, "password": PASSWORD})
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("text/plain") and "host" in r.text.lower()
        assert SESSION_COOKIE not in r.headers.get("set-cookie", "")
        for path in ("/", "/api/session", "/api/health", "/static/app.css", "/player", "/api/status"):
            assert (await c.get(path)).status_code == 400, path
    assert bare_ctx.db.kv_get(PASSWORD_KEY) is None  # nothing was claimed
    assert app.state.auth.setup_code_active  # and the code was not touched


async def test_local_names_are_allowed_others_are_not(bare_ctx):
    app = create_app(bare_ctx)
    port = bare_ctx.config.dashboard.port
    hostname = socket.gethostname().lower()
    short = hostname.split(".")[0]
    allowed = ["localhost", f"localhost:{port}", f"127.0.0.1:{port}", "127.0.0.1", f"[::1]:{port}", "[::1]",
               f"127.0.0.2:{port}", "LOCALHOST.", hostname, f"{hostname}:{port}", f"{short}.local:{port}",
               # loopback on another port (an SSH tunnel): a rebinding page never sends "localhost"
               f"127.0.0.1:{port + 1}", f"localhost:{port + 1}"]
    refused = ["testserver", "attacker.example", f"attacker.example:{port}", f"localhost.attacker.example:{port}",
               f"127.0.0.1.nip.io:{port}", f"{hostname}:{port + 1}",
               f"192.168.1.20:{port}",  # LAN IP literal while LAN access is off
               "", "local host", "::1", f"{short}.local.attacker.example"]
    async with _client(app, f"http://127.0.0.1:{port}") as c:
        for host in allowed:
            assert (await c.get("/api/health", headers={"Host": host})).status_code == 200, host
        for host in refused:
            assert (await c.get("/api/health", headers={"Host": host})).status_code == 400, host


async def test_configured_extra_hosts(bare_ctx):
    bare_ctx.config.dashboard.allowed_hosts = ["LittleBot.Home", "tunnel.example:9000", "http://proxy.lan:8443/"]
    bare_ctx.config.media.player_base_url = "http://kiosk.lan:8765"
    app = create_app(bare_ctx)
    port = bare_ctx.config.dashboard.port
    async with _client(app, f"http://127.0.0.1:{port}") as c:
        for host in ("littlebot.home", f"littlebot.home:{port}", "tunnel.example:9000", "proxy.lan:8443",
                     "kiosk.lan:8765"):
            assert (await c.get("/api/health", headers={"Host": host})).status_code == 200, host
        for host in ("littlebot.home:9999", "tunnel.example", f"tunnel.example:{port}", "proxy.lan"):
            assert (await c.get("/api/health", headers={"Host": host})).status_code == 400, host


async def test_lan_ip_hosts_only_with_lan_access(monkeypatch):
    import littlebot.dashboard.deps as deps

    monkeypatch.setattr(deps, "lan_addresses", lambda: ["192.0.2.55"])
    for lan in (False, True):
        ctx = make_test_context(dashboard={"lan_access": lan})
        app = create_app(ctx)
        port = ctx.config.dashboard.port
        try:
            # The connection arrived on 192.0.2.10 (ASGI "server"), Host names that same address.
            async with _client(app, f"http://192.0.2.10:{port}") as c:
                assert (await c.get("/api/health")).status_code == (200 if lan else 400)
                # The address lan_addresses() reports is ours too ...
                r = await c.get("/api/health", headers={"Host": f"192.0.2.55:{port}"})
                assert r.status_code == (200 if lan else 400)
                # ... but an arbitrary other IP is not, and neither is a foreign name.
                assert (await c.get("/api/health", headers={"Host": f"198.51.100.7:{port}"})).status_code == 400
                assert (await c.get("/api/health", headers={"Host": f"evil.example:{port}"})).status_code == 400
        finally:
            ctx.db.close()


async def test_testserver_only_when_configured(bare_ctx):
    app = create_app(bare_ctx)
    async with _client(app, "http://testserver") as c:
        assert (await c.get("/api/health")).status_code == 400
    bare_ctx.config.dashboard.allowed_hosts.append("testserver")
    async with _client(app, "http://testserver") as c:
        assert (await c.get("/api/health")).status_code == 200


def test_split_host():
    assert split_host("Example.COM.:8080") == ("example.com", 8080)
    assert split_host("[::1]:8765") == ("::1", 8765)
    assert split_host("localhost") == ("localhost", None)
    for bad in ("", "::1", "a:b", "x:70000", "a/b", "user@host", "[::1", "[::1]x"):
        assert split_host(bad) is None, bad


# ------------------------------------------------------------------ forwarded headers + global lockout (finding 4)
async def test_real_server_ignores_x_forwarded_for():
    ctx = make_test_context()
    install_fakes(ctx)
    svc = DashboardService(ctx, port=0)
    await svc.start()
    try:
        assert svc._server.config.proxy_headers is False
        await svc.auth.complete_setup(svc.auth.issue_setup_code(), PASSWORD)
        limit = ctx.config.dashboard.login_attempts_per_minute
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{svc.port}", timeout=5, trust_env=False) as c:
            codes = [(await c.post("/api/login", json={"password": f"wrong {i}"},
                                   headers={"X-Forwarded-For": f"10.9.0.{i + 1}", "Forwarded": f"for=10.8.0.{i + 1}"})
                      ).status_code for i in range(limit + 3)]
            assert codes[:limit] == [401] * limit and codes[limit:] == [429] * 3  # one client, however it says it is
            # A Host the robot does not answer to is refused on the real server too.
            assert (await c.get("/api/health", headers={"Host": "attacker.example"})).status_code == 400
    finally:
        await svc.stop()
        ctx.db.close()


def test_failure_lockout_window_and_relock():
    clock = Clock()
    lock = FailureLockout(limit=3, window=60, lockout=30, now=clock)
    lock.failure()
    lock.failure()
    assert lock.retry_after() is None
    lock.failure()
    assert lock.retry_after() == pytest.approx(30)
    clock.t += 31
    assert lock.retry_after() is None
    lock.failure()  # the window (60 s) is still full: locks again straight away
    assert lock.retry_after() == pytest.approx(30)
    clock.t += 61
    lock.failure()  # old failures expired
    assert lock.retry_after() is None


async def test_global_login_lockout_across_many_clients(dctx):
    clock = Clock()
    auth = AuthManager(dctx.db, attempts_per_minute=5, monotonic=clock)
    app = create_app(dctx, auth=auth)
    async with make_client(app) as c:
        await do_setup(c, app)
    limit = auth.login_limiter.lockout.limit
    assert limit == 20
    statuses = []
    for i in range(limit):  # 4 failures from each of 5 devices: every one under the per-device limit
        async with make_client(app, f"10.1.0.{i // 4}") as c:
            statuses.append((await c.post("/api/login", json={"password": "guess guess"})).status_code)
    assert statuses == [401] * limit
    async with make_client(app, "10.2.0.1") as fresh:  # a new device, even with the right password, waits
        r = await fresh.post("/api/login", json={"password": PASSWORD})
        assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= GLOBAL_LOCKOUT_SECONDS
        # Refused attempts are not failures: they neither extend the lockout nor count per device.
        for _ in range(10):
            assert (await fresh.post("/api/login", json={"password": "guess guess"})).status_code == 429
        assert len(auth.login_limiter.lockout._fails) == limit
        clock.t += GLOBAL_LOCKOUT_SECONDS + 1
        r = await fresh.post("/api/login", json={"password": PASSWORD})
        assert r.status_code == 200


async def test_change_password_failures_do_not_lock_out_login(authed, app):
    auth = app.state.auth
    for _ in range(3):
        r = await authed.post("/api/auth/password", json={"current_password": "nope nope", "new_password": "new pass 12"})
        assert r.status_code == 403
    assert len(auth.login_limiter.lockout._fails) == 0


# ------------------------------------------------------------------ request body caps (finding 5)
async def _raw_asgi(app, method: str, path: str, headers: list[tuple[bytes, bytes]], chunks: list[bytes]):
    """Drive the ASGI app directly; returns (status, body, number of receive() calls)."""
    calls = 0
    pending = list(chunks)

    async def receive():
        nonlocal calls
        calls += 1
        if not pending:
            await asyncio.sleep(3600)  # never happens in a passing test
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    out: list[dict] = []

    async def send(message):
        out.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8765"), *headers], "client": ("10.0.0.1", 5000),
             "server": ("127.0.0.1", 8765)}
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    start = next(m for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body, calls


async def test_oversized_content_length_rejected_without_reading(app):
    huge = str(300 * 1024 * 1024).encode()
    for path in ("/api/login", "/api/setup", "/api/status", "/", "/player/api/event"):
        status, headers, body, calls = await _raw_asgi(
            app, "POST", path, [(b"content-type", b"application/json"), (b"content-length", huge)], [b"{}"])
        assert status == 413, path
        assert calls == 0, path  # not a single body byte was read
        assert headers[b"connection"] == b"close"
        assert json.loads(body)["code"] == "too_large"


async def test_chunked_body_is_cut_off_at_the_cap(app):
    chunk = b" " * 4096
    n_chunks = 300  # 1.2 MB, sent without Content-Length
    status, _headers, body, calls = await _raw_asgi(
        app, "POST", "/api/login", [(b"content-type", b"application/json"), (b"transfer-encoding", b"chunked")],
        [b'{"password": "'] + [chunk] * n_chunks + [b'"}'])
    assert status == 413 and json.loads(body)["code"] == "too_large"
    assert calls <= MAX_BODY_BYTES // len(chunk) + 2  # stopped reading right after the cap


async def test_chunked_upload_through_httpx(client):
    async def gen():
        for _ in range(100):
            yield b"x" * 1024

    r = await client.post("/api/login", content=gen(), headers=J)
    assert r.status_code == 413
    # Small bodies are unaffected.
    assert (await client.post("/api/login", json={"password": PASSWORD})).status_code == 409  # not set up yet


async def test_player_api_has_a_smaller_cap(app):
    from littlebot.media.player_routes import MAX_BODY_BYTES as PLAYER_MAX

    assert PLAYER_MAX < MAX_BODY_BYTES
    big = json.dumps({"type": "heartbeat", "detail": "x" * PLAYER_MAX}).encode()
    status, *_ = await _raw_asgi(app, "POST", "/player/api/event",
                                 [(b"content-type", b"application/json"), (b"content-length", str(len(big)).encode())],
                                 [big])
    assert status == 413
    # The largest legitimate event (500 playlist ids) still fits.
    ok = json.dumps({"type": "playing", "load_id": "x" * 64, "video_id": "abcdefghijk",
                     "playlist_ids": ["abcdefghijk"] * 500, "detail": "d" * 200}).encode()
    status, *_ = await _raw_asgi(app, "POST", "/player/api/event",
                                 [(b"content-type", b"application/json"), (b"content-length", str(len(ok)).encode())],
                                 [ok])
    assert status != 413


async def test_invalid_content_length(app):
    status, *_ = await _raw_asgi(app, "POST", "/api/login",
                                 [(b"content-type", b"application/json"), (b"content-length", b"lots")], [b"{}"])
    assert status == 400


# ------------------------------------------------------------------ sessions vs. reset-password (finding 7)
async def test_session_row_deleted_elsewhere_ends_the_session(authed, dctx):
    assert (await authed.get("/api/status")).status_code == 200
    dctx.db.execute("DELETE FROM dashboard_sessions")  # what another process (the CLI) does
    assert (await authed.get("/api/status")).status_code == 401
    assert (await authed.post("/api/stop")).status_code == 401


async def test_reset_password_cli_while_running(tmp_path, caplog, capsys):
    from littlebot.dashboard.__main__ import main

    ctx = make_test_context(dashboard={"allowed_hosts": ["testserver"]})
    install_fakes(ctx)
    db = Database(tmp_path / "littlebot.db")  # the robot's own connection to the real database file
    auth = AuthManager(db)
    announced: list[str | None] = []
    auth.on_setup_code = announced.append
    app = create_app(ctx, auth=auth)
    try:
        async with make_client(app) as c:
            await do_setup(c, app)
            assert (await c.get("/api/status")).status_code == 200
            # The parent forgot the password and runs the CLI on the robot while LittleBot keeps running.
            assert main(["reset-password", "--data-dir", str(tmp_path), "--yes", "--port", "1"]) == 0
            assert "logged out" in capsys.readouterr().out
            assert (await c.get("/api/status")).status_code == 401  # old session ended at once
            announced.clear()
            with caplog.at_level(logging.WARNING, logger="littlebot.dashboard.auth"):
                info = (await c.get("/api/session")).json()
            assert info["authenticated"] is False and info["setup_required"] is True
            lines = [ln for ln in caplog.text.splitlines() if "setup code:" in ln]
            assert len(lines) == 1 and auth.setup_code_active
            code = lines[0].split("setup code:")[1].strip()
            assert announced == [code]  # also announced for the robot's screen
            # A second visit does not churn the code.
            await c.get("/api/session")
            assert announced == [code]
            r = await c.post("/api/setup", json={"setup_code": code, "password": "brand new password"})
            assert r.status_code == 200
        assert stat.S_IMODE(os.stat(tmp_path / "littlebot.db").st_mode) & 0o077 == 0  # still owner-only
    finally:
        db.close()
        ctx.db.close()


async def test_reset_password_cli_notices_a_running_dashboard(tmp_path, capsys):
    from littlebot.dashboard.__main__ import main

    db = Database(tmp_path / "littlebot.db")
    auth = AuthManager(db)
    await auth.complete_setup(auth.issue_setup_code(), PASSWORD)
    auth.create_session()
    db.close()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        assert main(["reset-password", "--data-dir", str(tmp_path), "--yes", "--port", str(port)]) == 0
    finally:
        listener.close()
    out = capsys.readouterr().out
    assert "seems to be running" in out and "No restart needed" in out


async def test_running_service_announces_new_code_after_reset():
    ctx = make_test_context()
    install_fakes(ctx)
    first = DashboardService(ctx, port=0)
    await first.auth.complete_setup(first.auth.issue_setup_code(), PASSWORD)
    svc = DashboardService(ctx, port=0)
    seen = collect(ctx.bus, [Topics.DASHBOARD_SETUP])
    await svc.start()
    try:
        assert seen == []  # configured: nothing to announce at start
        AuthManager(ctx.db).reset_password()  # e.g. the CLI
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{svc.port}", timeout=5, trust_env=False) as c:
            assert (await c.get("/api/session")).json()["setup_required"] is True
        assert seen and seen[-1].data["active"] is True and len(seen[-1].data["code"]) == 9
    finally:
        await svc.stop()
        ctx.db.close()


# ------------------------------------------------------------------ setup-code burning (finding 12)
async def test_one_client_cannot_burn_the_setup_code(dctx):
    auth = AuthManager(dctx.db, attempts_per_minute=100)  # take the per-client rate limit out of the picture
    app = create_app(dctx, auth=auth)
    code = auth.issue_setup_code()
    async with make_client(app, "10.3.0.1") as neighbour:
        for _ in range(30):
            r = await neighbour.post("/api/setup", json={"setup_code": "ZZZZ-ZZZZ", "password": PASSWORD})
            assert r.status_code == 401
    assert auth._setup_failures == SETUP_CODE_MAX_FAILURES_PER_CLIENT
    async with make_client(app, "10.3.0.2") as parent:
        r = await parent.post("/api/setup", json={"setup_code": code, "password": PASSWORD})
        assert r.status_code == 200


async def test_many_clients_do_burn_the_setup_code(dctx):
    auth = AuthManager(dctx.db, attempts_per_minute=100)
    app = create_app(dctx, auth=auth)
    code = auth.issue_setup_code()
    for i in range(4):  # 3 + 3 + 3 + 1 counted failures
        async with make_client(app, f"10.4.0.{i}") as c:
            for _ in range(3):
                await c.post("/api/setup", json={"setup_code": "ZZZZ-ZZZZ", "password": PASSWORD})
    assert auth.setup_code_active
    async with make_client(app, "10.4.1.1") as parent:
        r = await parent.post("/api/setup", json={"setup_code": code, "password": PASSWORD})
        assert r.status_code == 401  # burned; a new code was logged


async def test_rate_limited_setup_attempts_are_not_counted(client, app):
    auth = app.state.auth
    code = auth.issue_setup_code()
    limit = app.state.ctx.config.dashboard.login_attempts_per_minute
    codes = [(await client.post("/api/setup", json={"setup_code": "WXYZ-WXYZ", "password": PASSWORD})).status_code
             for _ in range(25)]
    assert codes == [401] * limit + [429] * (25 - limit)
    assert len(auth.setup_limiter.lockout._fails) == limit  # only attempts that were checked
    assert auth._setup_failures == SETUP_CODE_MAX_FAILURES_PER_CLIENT
    assert auth._check_setup_code(code, client="10.9.9.9")  # the parent's code still works


async def test_global_setup_lockout(dctx):
    clock = Clock()
    auth = AuthManager(dctx.db, attempts_per_minute=5, monotonic=clock)
    app = create_app(dctx, auth=auth)
    auth.issue_setup_code()
    n = auth.setup_limiter.lockout.limit
    for i in range(n):  # 5 devices x 4 wrong codes, each under the per-device limit
        async with make_client(app, f"10.5.0.{i // 4}") as c:
            assert (await c.post("/api/setup", json={"setup_code": "ZZZZ-ZZZZ", "password": PASSWORD})).status_code == 401
    async with make_client(app, "10.5.9.9") as parent:
        r = await parent.post("/api/setup", json={"setup_code": "ABCD-EFGH", "password": PASSWORD})
        assert r.status_code == 429
        clock.t += GLOBAL_LOCKOUT_SECONDS + 1
        r = await parent.post("/api/setup", json={"setup_code": "ABCD-EFGH", "password": PASSWORD})
        assert r.status_code == 401
