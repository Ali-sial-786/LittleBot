"""DashboardService lifecycle on a real asyncio loop and a real (ephemeral) TCP port."""

from __future__ import annotations

import asyncio
import logging
import socket
from types import SimpleNamespace

import httpx
import pytest

from littlebot.core.events import Topics
from littlebot.core.service import ServiceStatus
from littlebot.dashboard import DashboardService, create_service, resolve_bind_host
from littlebot.dashboard.deps import dashboard_urls
from littlebot.testing import make_test_context

from dashboard_fakes import PASSWORD, install_fakes


def test_bind_host_defaults_to_localhost():
    cfg = make_test_context().config.dashboard
    assert resolve_bind_host(cfg) == "127.0.0.1"


def test_bind_host_lan_only_when_opted_in():
    assert resolve_bind_host(SimpleNamespace(host="127.0.0.1", lan_access=True)) == "0.0.0.0"
    # A non-loopback host without lan_access is refused (never exposed by accident).
    assert resolve_bind_host(SimpleNamespace(host="0.0.0.0", lan_access=False)) == "127.0.0.1"
    assert resolve_bind_host(SimpleNamespace(host="192.168.1.5", lan_access=False)) == "127.0.0.1"
    assert resolve_bind_host(SimpleNamespace(host="localhost", lan_access=False)) == "127.0.0.1"
    assert resolve_bind_host(SimpleNamespace(host="127.0.0.2", lan_access=False)) == "127.0.0.2"


def test_service_uses_config(ctx):
    svc = create_service(ctx)
    assert isinstance(svc, DashboardService)
    assert svc.host == "127.0.0.1" and svc.port == ctx.config.dashboard.port
    lan = make_test_context(dashboard={"lan_access": True})
    assert create_service(lan).host == "0.0.0.0"
    lan.db.close()


def test_dashboard_urls():
    assert dashboard_urls("127.0.0.1", 8765) == ["http://127.0.0.1:8765/"]
    urls = dashboard_urls("0.0.0.0", 8765)
    assert urls[-1] == "http://127.0.0.1:8765/"


async def test_service_serves_on_real_port_and_stops(caplog):
    ctx = make_test_context()
    install_fakes(ctx)
    svc = DashboardService(ctx, port=0)
    with caplog.at_level(logging.WARNING, logger="littlebot.dashboard.auth"):
        await svc.start()
    try:
        assert svc.service_status == ServiceStatus.RUNNING, svc.status_detail
        assert svc.port != 0
        # The first-run setup code went to the console / log, nowhere else.
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "setup code" in log_text.lower() and svc.auth.setup_code_active
        setup_lines = [line for line in log_text.splitlines() if "setup code:" in line]
        assert len(setup_lines) == 1  # `grep setup` on the log shows the code
        code = setup_lines[0].split("setup code:")[1].strip()
        assert len(code) == 9 and code[4] == "-"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{svc.port}", timeout=5, trust_env=False) as c:
            assert (await c.get("/api/health")).json() == {"ok": True}
            assert (await c.get("/api/status")).status_code == 401
            r = await c.post("/api/setup", json={"setup_code": code, "password": PASSWORD})
            assert r.status_code == 200, r.text
            c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
            assert (await c.get("/api/status")).json()["state"]["state"] == "IDLE"
            assert (await c.post("/api/face/expression", json={"name": "happy"})).status_code == 200
            index = await c.get("/")
            assert index.status_code == 200 and index.headers["x-frame-options"] == "DENY"
        assert svc.health()["status"] == "running" and svc.health()["lan_access"] is False
    finally:
        await svc.stop()
    assert svc.service_status == ServiceStatus.STOPPED
    with pytest.raises(httpx.HTTPError):
        async with httpx.AsyncClient(timeout=1, trust_env=False) as c:
            await c.get(f"http://127.0.0.1:{svc.port}/api/health")
    await svc.stop()  # idempotent
    ctx.db.close()


async def test_port_in_use_fails_gracefully():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    ctx = make_test_context()
    try:
        svc = DashboardService(ctx, port=port)
        await svc.start()  # must not raise or kill the loop
        assert svc.service_status == ServiceStatus.FAILED
        assert "in use" in svc.status_detail
        await asyncio.sleep(0)  # loop still alive
        await svc.stop()
        assert svc.service_status == ServiceStatus.FAILED
    finally:
        blocker.close()
        ctx.db.close()


async def test_service_does_not_log_setup_code_once_configured(caplog):
    ctx = make_test_context()
    first = DashboardService(ctx, port=0)
    code = first.auth.issue_setup_code()
    await first.auth.complete_setup(code, PASSWORD)
    svc = DashboardService(ctx, port=0)
    with caplog.at_level(logging.WARNING, logger="littlebot.dashboard.auth"):
        await svc.start()
    try:
        assert svc.service_status == ServiceStatus.RUNNING
        assert "setup code" not in caplog.text.lower()
    finally:
        await svc.stop()
        ctx.db.close()


async def test_stop_all_event_cancels_dashboard_demo():
    ctx = make_test_context(activities={"time_scale": 1.0})
    svc = DashboardService(ctx, port=0)
    await svc.start()
    try:
        from littlebot.dashboard.routes.face_routes import run_demo

        task = svc.dash.spawn(run_demo(svc.dash), name="face_demo")
        await asyncio.sleep(0.05)
        assert not task.done()
        ctx.bus.publish(Topics.STOP_ALL, {"source": "button"})
        for _ in range(100):
            if task.done():
                break
            await asyncio.sleep(0.02)
        assert task.cancelled() or task.done()
        assert ctx.face.eyes_closed is False
    finally:
        await svc.stop()
        ctx.db.close()


async def test_reset_password_cli(tmp_path, capsys):
    from littlebot.core.storage import Database
    from littlebot.dashboard.__main__ import main
    from littlebot.dashboard.auth import AuthManager

    db = Database(tmp_path / "littlebot.db")
    auth = AuthManager(db)
    await auth.complete_setup(auth.issue_setup_code(), PASSWORD)
    auth.create_session()
    db.close()
    assert main(["reset-password", "--data-dir", str(tmp_path), "--yes"]) == 0
    db = Database(tmp_path / "littlebot.db")
    try:
        fresh = AuthManager(db)
        assert fresh.setup_required and fresh.active_sessions() == 0
    finally:
        db.close()
    assert main(["reset-password", "--data-dir", str(tmp_path / "missing"), "--yes"]) == 1
