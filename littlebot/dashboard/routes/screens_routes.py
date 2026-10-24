"""Info cards on LittleBot's screen: what is showing, close it, the parent's switches, show one to try."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(prefix="/api/screens", tags=["screens"])

UNAVAILABLE = "Info cards are unavailable right now."
# Why a card couldn't be shown, for the parent (ScreenService.show error codes).
SHOW_ERRORS = {
    "disabled": "Info cards are switched off (Privacy page).",
    "busy": "LittleBot's screen is busy (a game, a video or an activity), or LittleBot is asleep.",
    "no_card": "That card couldn't be made (unknown kind, missing fields, or nothing to show).",
}


class ScreensSettingsBody(Body):
    enabled: bool | None = None  # info cards on the screen during voice chats
    web_pictures: bool | None = None  # pictures from the internet on the cards (strict safe search, never kept)


class ShowBody(Body):
    spec: dict[str, Any] = Field(..., description='A card spec, e.g. {"card": "weather", "place": "Toronto"}')


def _view(dash: DashState) -> dict[str, Any]:
    s = dash.ctx.settings
    screens = getattr(dash.ctx, "screens", None)
    if screens is None:
        return {"available": False, "enabled": bool(s.get("screens.enabled", True)),
                "web_pictures": bool(s.get("screens.web_pictures", True)), "active": False, "card": None}
    return screens.status()


@router.get("")
async def screens_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.put("/settings")
async def screens_settings(body: ScreensSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    for key, value in changes.items():
        dash.ctx.settings.set(f"screens.{key}", bool(value), source="dashboard")
    log.info("screen settings changed from dashboard: %s", changes)
    return {"ok": True, **_view(dash)}


@router.post("/close")
async def close_card(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    screens = getattr(dash.ctx, "screens", None)
    if screens is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    closed = bool(screens.close("parent"))
    return {"ok": True, "closed": closed, **_view(dash)}


@router.post("/show")
async def show_card(body: ShowBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Show a card from a spec (testing a card from the dashboard)."""
    screens = getattr(dash.ctx, "screens", None)
    if screens is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    if len(str(body.spec)) > 4000:
        raise HTTPException(status_code=413, detail="That card spec is too big.")
    result = await screens.show(body.spec, source="dashboard")
    if not result.get("ok"):
        err = str(result.get("error") or "")
        raise HTTPException(status_code=422 if err == "no_card" else 409,
                            detail=SHOW_ERRORS.get(err, "LittleBot can't show that card right now."))
    return {"ok": True, "showing": result.get("showing"), **_view(dash)}
