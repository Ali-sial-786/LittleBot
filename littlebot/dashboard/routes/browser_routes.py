"""The browser view on LittleBot's screen (Google with SafeSearch, grown-ups only): what it shows, close it,
the parent's switch."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(prefix="/api/browser", tags=["browser"])

UNAVAILABLE = "The browser view is unavailable right now."


class BrowserSettingsBody(Body):
    enabled: bool | None = None  # the browser view may open when a grown-up asks for it
    keep_signins: bool | None = None  # it keeps its cookies (Google's "you're a person"), history and cache


def _view(dash: DashState) -> dict[str, Any]:
    browser = getattr(dash.ctx, "browser", None)
    if browser is None:
        return {"available": False, "enabled": bool(dash.ctx.settings.get("browser.enabled", True)),
                "keep_signins": bool(dash.ctx.settings.get("browser.keep_signins", False)), "saved": False,
                "detail": "not running", "active": False, "showing": None}
    return browser.status()


@router.get("")
async def browser_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.put("/settings")
async def browser_settings(body: BrowserSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if body.enabled is None and body.keep_signins is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    if body.enabled is not None:
        dash.ctx.settings.set("browser.enabled", bool(body.enabled), source="dashboard")
        log.info("browser view switched %s from dashboard", "on" if body.enabled else "off")
    if body.keep_signins is not None:
        dash.ctx.settings.set("browser.keep_signins", bool(body.keep_signins), source="dashboard")
        log.info("browser view: keeping its cookies switched %s from dashboard", "on" if body.keep_signins else "off")
        browser = getattr(dash.ctx, "browser", None)
        if not body.keep_signins and browser is not None:
            await browser.forget()  # off: nothing kept
    return {"ok": True, **_view(dash)}


@router.post("/forget")
async def forget_browser(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Delete the saved browser (its cookies, history and cache); the next search starts a fresh one."""
    browser = getattr(dash.ctx, "browser", None)
    if browser is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    forgot = bool(await browser.forget())
    return {"ok": True, "forgot": forgot, **_view(dash)}


@router.post("/close")
async def close_browser(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    browser = getattr(dash.ctx, "browser", None)
    if browser is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    closed = bool(browser.close("parent"))
    return {"ok": True, "closed": closed, **_view(dash)}
