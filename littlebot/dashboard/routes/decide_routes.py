"""Fast decision models (littlebot/decide): the provider, each feature's mode, and how they are doing. Keys stay in
.env on the robot; only whether one is set reaches this page."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body
from littlebot.decide import FEATURES, get_decider

router = APIRouter(prefix="/api/decide", tags=["decide"])


class DecideSettingsBody(Body):
    provider: Literal["off", "jev", "openai"] | None = None
    modes: dict[str, Literal["off", "shadow", "on"]] | None = None  # {feature: mode}; only the features given change
    log_context: bool | None = None  # the log says what each decision was shown and who was recognised


@router.get("")
async def decide_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return get_decider(dash.ctx).status()


@router.put("/settings")
async def decide_settings(body: DecideSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if body.provider is None and not body.modes and body.log_context is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    settings = dash.ctx.settings
    if body.modes:
        unknown = sorted(set(body.modes) - set(FEATURES))
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown feature: {', '.join(unknown)[:80]}.")
        modes = dict(settings.get("decide.modes", {}) or {})
        modes.update(body.modes)
        settings.set("decide.modes", modes, source="dashboard")
    if body.provider is not None:
        settings.set("decide.provider", body.provider, source="dashboard")
    if body.log_context is not None:
        settings.set("decide.log_context", body.log_context, source="dashboard")
    log.info("decision settings changed from dashboard: provider=%s modes=%s log_context=%s", body.provider,
             body.modes, body.log_context)
    return {"ok": True, **get_decider(dash.ctx).status()}
