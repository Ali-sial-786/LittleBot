"""Audio and camera devices: status, tests, volume, speech rate, face following."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.core.interfaces import SOUND_NAMES
from littlebot.dashboard.deps import DashState, get_dash
from littlebot.dashboard.models import AudioSettingsBody, FaceSimBody, MicTestBody, SayBody, SoundBody, VisionSettingsBody
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api", tags=["devices"])


# ----------------------------------------------------------------- audio
@router.get("/audio")
async def audio_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    audio = dash.ctx.audio
    s = dash.ctx.settings
    return {
        "available": audio is not None,
        "status": scrub(audio.status()) if audio is not None else {"status": "unavailable"},
        "settings": {"volume": s.get("audio.volume"), "tts_rate": s.get("audio.tts_rate")},
        "max_volume": dash.ctx.config.audio.speaker.max_volume,
        "sounds": list(SOUND_NAMES),
        "microphone_enabled": dash.ctx.privacy.microphone_enabled,
    }


@router.get("/audio/devices")
async def audio_devices(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    audio = dash.need("audio")
    devices = await asyncio.to_thread(audio.list_devices)  # may query PortAudio: keep off the loop
    return scrub(dict(devices or {}))


@router.put("/audio/settings")
async def audio_settings(body: AudioSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    s = dash.ctx.settings
    out: dict[str, Any] = {}
    if body.volume is not None:
        audio = dash.ctx.audio
        applied = audio.set_volume(body.volume) if audio is not None else body.volume
        out["volume"] = s.set("audio.volume", float(applied), source="dashboard")
    if body.tts_rate is not None:
        out["tts_rate"] = s.set("audio.tts_rate", float(body.tts_rate), source="dashboard")
    return {"ok": True, "settings": out}


@router.post("/audio/test-speaker")
async def test_speaker(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    result = await dash.need("audio").test_speaker()
    return {"ok": True, "result": scrub(dict(result or {}))}


@router.post("/audio/test-microphone")
async def test_microphone(body: MicTestBody | None = None, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    audio = dash.need("audio")
    if not dash.ctx.privacy.microphone_enabled:
        raise HTTPException(status_code=403, detail="The microphone is turned off in Privacy.")
    seconds = body.seconds if body is not None else 2.0
    result = await audio.test_microphone(seconds)
    return {"ok": True, "result": scrub(dict(result or {}))}


@router.post("/audio/say")
async def say(body: SayBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    audio = dash.need("audio")
    dash.spawn(audio.say(body.text), name="say")
    return {"ok": True}


@router.post("/audio/sound")
async def sound(body: SoundBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    audio = dash.need("audio")
    dash.spawn(audio.play_sound(body.name, wait=True), name="sound")
    return {"ok": True}


@router.post("/audio/stop")
async def stop_audio(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("audio").stop_output()
    return {"ok": True}


# ----------------------------------------------------------------- vision
@router.get("/vision")
async def vision_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    vision = dash.ctx.vision
    return {
        "available": vision is not None,
        "status": scrub(vision.status()) if vision is not None else {"camera": "unavailable"},
        "follow_faces": bool(dash.ctx.settings.get("vision.follow_faces")),
        "camera_enabled": dash.ctx.privacy.camera_enabled,
    }


@router.put("/vision/settings")
async def vision_settings(body: VisionSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.ctx.settings.set("vision.follow_faces", body.follow_faces, source="dashboard")
    if not body.follow_faces and dash.ctx.face is not None:
        dash.ctx.face.clear_gaze()
    return {"ok": True, "follow_faces": body.follow_faces}


@router.post("/vision/simulate-face")
async def simulate_face(body: FaceSimBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("vision").simulate_face(body.x, body.y, body.size)
    return {"ok": True}


@router.post("/vision/simulate-no-face")
async def simulate_no_face(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("vision").simulate_no_face()
    return {"ok": True}
