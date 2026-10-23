"""On-screen games: list them, start one on LittleBot's screen, stop it."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from littlebot.dashboard.deps import DashState, get_dash

router = APIRouter(prefix="/api/games", tags=["games"])

GAME_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
UNAVAILABLE = "Games are unavailable right now."


class GameStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minutes: float | None = Field(None, ge=0.5, le=15, description="Session length (default: the game's own)")


def _games(dash: DashState) -> Any:
    games = getattr(dash.ctx, "games", None)
    if games is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)
    return games


@router.get("")
async def list_games(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    games = getattr(dash.ctx, "games", None)
    if games is None:
        return {"available": False, "items": [], "status": {"available": False, "running": False}}
    return {"available": True, "items": games.list(), "status": games.status(),
            "child_age_months": dash.ctx.settings.child_age_months()}


@router.get("/status")
async def game_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    games = getattr(dash.ctx, "games", None)
    return games.status() if games is not None else {"available": False, "running": False}


@router.post("/stop")
async def stop_game(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    games = _games(dash)
    stopped = bool(games.stop("parent stop"))
    return {"ok": True, "stopped": stopped, "status": games.status()}


@router.post("/{game_id}/start")
async def start_game(game_id: str, body: GameStartBody | None = Body(None),
                     dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    games = _games(dash)
    if not GAME_ID_RE.match(game_id) or games.get(game_id) is None:
        raise HTTPException(status_code=404, detail="Unknown game.")
    dash.cancel("face_demo")
    ok = await games.start(game_id, source="dashboard", minutes=body.minutes if body is not None else None)
    status = games.status()
    if not ok:
        detail = status.get("last_error") or "LittleBot can't start that game right now."
        raise HTTPException(status_code=409, detail=detail)
    return {"ok": True, "status": status}
