"""Simulator: inject speech, caregiver answers and camera events without real devices."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.core.events import Topics
from littlebot.dashboard.deps import DashState, get_dash
from littlebot.dashboard.models import CameraSimBody, RespondBody, SpeechSimBody
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api/sim", tags=["simulator"])

BRAIN_TIMEOUT_SECONDS = 30.0


def _activity_wants_speech(dash: DashState) -> bool:
    ctx = dash.ctx
    try:
        if ctx.activities is not None and ctx.activities.status().get("running"):
            return True
        if ctx.audio is not None and ctx.audio.status().get("listening"):
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


@router.post("/speech")
async def speech(body: SpeechSimBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Pretend the child said something.

    ``route``: ``microphone`` feeds the text to the audio input (satisfies a pending
    listen(), e.g. inside an activity); ``brain`` runs a conversation turn;
    ``auto`` picks the microphone while an activity is running or LittleBot is
    listening, otherwise the conversation engine.
    """
    ctx = dash.ctx
    route = body.route
    if route == "auto":
        route = "microphone" if (_activity_wants_speech(dash) or ctx.brain is None) else "brain"
    if route == "microphone":
        dash.need("audio").inject_utterance(body.text)
        return {"ok": True, "route": "microphone", "reply": None}
    brain = dash.need("brain")
    try:
        result = await asyncio.wait_for(
            brain.handle_utterance(body.text, source="simulated", speak=body.speak), BRAIN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as e:
        raise HTTPException(status_code=504, detail="LittleBot took too long to answer.") from e
    return {"ok": True, "route": "brain", **scrub(dict(result or {}))}


@router.post("/caregiver")
async def caregiver(body: RespondBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = dash.need("activities").respond(body.response, body.prompt_id)
    if not ok:
        raise HTTPException(status_code=409, detail="There is no caregiver question waiting right now.")
    return {"ok": True}


@router.post("/camera")
async def camera(body: CameraSimBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ctx = dash.ctx
    vision = ctx.vision
    if vision is not None:
        if body.event == "face":
            vision.simulate_face(body.x, body.y, body.size)
        else:
            vision.simulate_no_face()
        return {"ok": True, "via": "vision"}
    # No vision module: publish the equivalent events so behaviours can still be exercised.
    if body.event == "face":
        gx, gy = body.x, body.y
        ctx.bus.publish(Topics.VISION_FACES, {
            "faces": [{"x": body.x, "y": body.y, "w": body.size, "h": body.size, "gaze_x": gx, "gaze_y": gy}],
            "frame_w": 1.0, "frame_h": 1.0}, source="simulator")
        ctx.bus.publish(Topics.VISION_PERSON_PRESENT, {"gaze_x": gx, "gaze_y": gy}, source="simulator")
    else:
        ctx.bus.publish(Topics.VISION_FACES, {"faces": [], "frame_w": 1.0, "frame_h": 1.0}, source="simulator")
        ctx.bus.publish(Topics.VISION_PERSON_LEFT, {}, source="simulator")
    return {"ok": True, "via": "events"}
