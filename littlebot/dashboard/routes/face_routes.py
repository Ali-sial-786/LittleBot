"""Face controls: expressions, blinks, gaze, animations, a short demo sequence, and dozing / sleep settings."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS, FACE_STATES
from littlebot.dashboard.deps import DashState, get_dash
from littlebot.dashboard.models import (
    AnimationBody,
    BlinkBody,
    ExpressionBody,
    FaceSleepBody,
    GazeBody,
)

log = logging.getLogger("littlebot.dashboard")

router = APIRouter(prefix="/api/face", tags=["face"])

# FaceSleepBody field -> setting key
SLEEP_SETTINGS = {
    "doze_enabled": "face.doze_enabled",
    "doze_after_seconds": "face.doze_after_seconds",
    "awake_on_arrival_seconds": "face.awake_on_arrival_seconds",
    "sleep_after_idle_minutes": "system.sleep_after_idle_minutes",
}


def _sleep_view(dash: DashState) -> dict[str, Any]:
    settings = dash.ctx.settings
    out: dict[str, Any] = {field: settings.get(key) for field, key in SLEEP_SETTINGS.items()}
    face = dash.ctx.face
    out["dozing"] = bool(getattr(face, "dozing", False) is True)
    return out

# (seconds to wait after the step, action, args). Kept short and gentle (~15 s at normal speed).
DEMO_SEQUENCE: tuple[tuple[float, str, tuple[Any, ...]], ...] = (
    (0.2, "open_eyes", ()),
    (1.6, "set_expression", ("happy",)),
    (0.4, "play_animation", ("bounce",)),
    (1.0, "look_at", (0.2, 0.45)),
    (1.0, "look_at", (0.8, 0.45)),
    (0.6, "clear_gaze", ()),
    (1.4, "set_expression", ("curious",)),
    (0.4, "blink", (True,)),
    (1.4, "set_expression", ("surprised",)),
    (1.6, "set_expression", ("playful",)),
    (0.6, "play_animation", ("wiggle",)),
    (1.6, "set_expression", ("affectionate",)),
    (0.6, "play_animation", ("hearts",)),
    (1.6, "set_expression", ("celebrating",)),
    (0.6, "play_animation", ("confetti",)),
    (1.6, "set_expression", ("sleepy",)),
    (1.2, "close_eyes", ()),
    (0.4, "open_eyes", ()),
    (0.6, "set_expression", ("neutral",)),
    (0.0, "clear_gaze", ()),
)


async def run_demo(dash: DashState) -> None:
    face = dash.ctx.face
    if face is None:
        return
    clock = dash.ctx.clock
    try:
        for wait, action, args in DEMO_SEQUENCE:
            getattr(face, action)(*args)
            await clock.sleep(wait)
    finally:
        try:  # leave the face in a calm state even if cancelled (STOP)
            face.open_eyes()
        except Exception:  # noqa: BLE001
            pass


@router.get("")
async def face_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    face = dash.ctx.face
    return {
        "available": face is not None,
        "status": face.status() if face is not None else {"status": "unavailable"},
        "expressions": list(EXPRESSIONS),
        "animations": list(FACE_ANIMATIONS),
        "states": list(FACE_STATES),
        "demo_running": "face_demo" in dash.named_tasks,
        "sleep": _sleep_view(dash),
    }


@router.put("/sleep")
async def sleep_settings(body: FaceSleepBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Dozing when nothing is happening (a look only) and the deeper sleep after a long quiet spell."""
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    for field, value in changes.items():
        dash.ctx.settings.set(SLEEP_SETTINGS[field], value, source="dashboard")
    log.info("doze / sleep settings changed from dashboard: %s", changes)
    return {"ok": True, "sleep": _sleep_view(dash)}


@router.post("/expression")
async def set_expression(body: ExpressionBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    face = dash.need("face")
    dash.cancel("face_demo")
    face.set_expression(body.name, hold=body.hold)
    return {"ok": True, "expression": body.name}


@router.post("/blink")
async def blink(body: BlinkBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("face").blink(double=body.double)
    return {"ok": True}


@router.post("/gaze")
async def gaze(body: GazeBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("face").look_at(body.x, body.y, hold=body.hold)
    return {"ok": True, "x": body.x, "y": body.y}


@router.post("/gaze/clear")
async def clear_gaze(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("face").clear_gaze()
    return {"ok": True}


@router.post("/animation")
async def animation(body: AnimationBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("face").play_animation(body.name)
    return {"ok": True, "animation": body.name}


@router.post("/demo")
async def demo(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("face")
    dash.spawn(run_demo(dash), name="face_demo")
    return {"ok": True, "started": True, "steps": len(DEMO_SEQUENCE)}


@router.post("/demo/stop")
async def demo_stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    stopped = dash.cancel("face_demo")
    face = dash.ctx.face
    if face is not None:  # don't leave the face on whatever expression the demo had reached
        face.open_eyes()
        face.clear_gaze()
        face.set_expression("neutral")
    return {"ok": True, "stopped": stopped}
