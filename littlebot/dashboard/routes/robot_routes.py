"""Robot-wide controls: status, STOP, sleep / wake, and system information."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from littlebot import __version__
from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import PowerBody
from littlebot.dashboard.scrub import scrub, scrub_status

router = APIRouter(prefix="/api", tags=["robot"])


@router.get("/status")
async def status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return scrub_status(dash.status())


@router.post("/stop")
async def stop_all(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """The big red STOP button."""
    log.info("parent pressed STOP")
    st = await dash.stop_all("dashboard")
    return {"ok": True, "status": scrub_status(st)}


@router.post("/sleep")
async def sleep(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = dash.sleep("dashboard")
    if not ok:
        raise HTTPException(status_code=409, detail="LittleBot can't sleep right now (stop the current activity first).")
    return {"ok": True, "state": dash.ctx.state.snapshot()}


@router.post("/wake")
async def wake(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = dash.wake("dashboard")
    return {"ok": bool(ok), "state": dash.ctx.state.snapshot()}


# ----------------------------------------------------------------- system
@router.get("/system")
async def system_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ctx = dash.ctx
    robot = dash.robot
    services: dict[str, Any] = {}
    for name, svc in getattr(robot, "services", []) or []:
        health = getattr(svc, "health", None)
        try:
            services[name] = health() if callable(health) else {"name": name, "status": "running"}
        except Exception:  # noqa: BLE001
            services[name] = {"name": name, "status": "error"}
    dash_svc = getattr(robot, "dashboard", None)
    if dash_svc is not None and callable(getattr(dash_svc, "health", None)):
        services["dashboard"] = dash_svc.health()
    cfg = ctx.config.dashboard
    return scrub({
        "name": ctx.config.robot_name,
        "version": __version__,
        "mode": ctx.config.mode.value,
        "offline": ctx.config.is_offline,
        "services": services,
        "monitor": ctx.monitor.status(),
        "dashboard": {
            "lan_access": cfg.lan_access,
            "bind_host": dash.bind_host,
            "port": dash.port or cfg.port,
            "urls": dash.urls,
            "session_hours": cfg.session_hours,
            "login_attempts_per_minute": cfg.login_attempts_per_minute,
        },
    })


@router.get("/system/config")
async def system_config(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Effective configuration without secrets (only has_*_key booleans)."""
    return scrub(dash.ctx.config.public_dict())


@router.get("/system/events")
async def system_events(dash: DashState = Depends(get_dash), n: int = Query(100, ge=1, le=300),
                        pattern: str = Query("*", max_length=40, pattern=r"^[a-z_.*]+$")) -> dict[str, Any]:
    now = time.monotonic()
    events = dash.ctx.bus.recent(n, pattern)
    return {"events": [
        {"seq": e.seq, "topic": e.topic, "source": e.source, "age_seconds": round(max(0.0, now - e.ts), 1),
         "data": scrub(dict(e.data))}
        for e in reversed(events)
    ]}


@router.post("/system/power")
async def power(body: PowerBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Shutdown / restart: asks the robot's main loop to exit (a service manager restarts it)."""
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Please confirm before shutting LittleBot down.")
    robot = dash.robot
    request_shutdown = getattr(robot, "request_shutdown", None)
    if not callable(request_shutdown):
        raise HTTPException(status_code=503, detail="Shutdown is not available in this setup.")
    reason = f"dashboard {body.action}"
    log.warning("parent requested %s from the dashboard", body.action)
    # Reply first, then exit, so the browser gets the confirmation.
    asyncio.get_running_loop().call_later(0.5, _call_safely, request_shutdown, reason)
    detail = ("LittleBot is shutting down." if body.action == "shutdown" else
              "LittleBot is restarting. If it was started by hand (not as a service), start it again.")
    return {"ok": True, "action": body.action, "detail": detail}


def _call_safely(fn: Any, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001
        log.exception("shutdown request failed")
