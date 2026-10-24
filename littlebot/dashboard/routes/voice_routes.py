"""Record the activity voices in the ChatGPT voice (the Activities page's button).

The same recording as ``scripts/record_voice_lines.py --missing``, run on the robot with its own
ChatGPT sign-in: every fixed line LittleBot speaks (``littlebot.app.spoken_lines``) that isn't
recorded yet, the activities that suit the child's age first. Each line is saved as soon as it
matches (``data/voice_lines``) and used at once. The robot's speaker and microphone aren't used,
so LittleBot keeps playing meanwhile. Lines with the child's nickname are only recorded with the
parent's OK (``with_name``, default: the ``ai.share_child_name`` setting): it sends the name to ChatGPT.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(prefix="/api/voice-lines", tags=["voice lines"])

JOB_KEY = "voice_lines_job"
CACHE_KEY = "voice_lines_cache"
CACHE_S = 15.0  # the card polls every few seconds while recording: work the lines out again at most this often


class RecordVoicesBody(Body):
    with_name: bool | None = None  # also the lines with the child's nickname (None: the AI page's sharing switch)
    again: bool = False  # re-record lines that are recorded already


def _job(dash: DashState) -> Any:
    return dash.ctx.extras.get(JOB_KEY)


def _store(dash: DashState) -> Any:
    from littlebot.audio.voice_lines import get_store  # noqa: PLC0415

    return get_store(dash.ctx)


def _lines(dash: DashState) -> list[Any]:
    """Every fixed line (``spoken_lines.collect``), worked out again when the profile changes or after CACHE_S."""
    from littlebot.app.spoken_lines import collect  # noqa: PLC0415

    ctx = dash.ctx
    library = getattr(ctx.activities, "library", None)
    key = (json.dumps(ctx.settings.get("profile") or {}, sort_keys=True, default=str), id(library),
           len(library) if library is not None else 0)
    cached = ctx.extras.get(CACHE_KEY)
    if cached is not None and cached[0] == key and time.monotonic() - cached[1] < CACHE_S:
        return cached[2]
    lines = collect(ctx.config, ctx.settings, library=library)
    ctx.extras[CACHE_KEY] = (key, time.monotonic(), lines)
    return lines


def _blocked(dash: DashState) -> tuple[str, str] | None:
    """Why recording can't start now: (code, what to tell the parent), or None."""
    ctx = dash.ctx
    if ctx.config.is_offline:
        return "offline", "LittleBot is in offline mode: recording needs the internet."
    if not ctx.privacy.cloud_enabled:
        return "cloud_off", "Turn on cloud communication in Privacy first."
    from littlebot.chatgpt import get_auth  # noqa: PLC0415
    from littlebot.chatgpt.live import ERRORS, aiortc_error  # noqa: PLC0415

    if not get_auth(ctx).signed_in:
        return "not_signed_in", "Sign in with ChatGPT on the AI page first: the recording uses that voice."
    if aiortc_error():
        return "unavailable", ERRORS["unavailable"]
    return None


def _view(dash: DashState, lines: list[Any]) -> dict[str, Any]:
    from littlebot.app.spoken_lines import estimate_seconds  # noqa: PLC0415

    ctx = dash.ctx
    store = _store(dash)
    recorded = store.recorded([x.text for x in lines])
    missing = [x for x in lines if x.hash not in recorded]
    share = bool(ctx.settings.get("ai.share_child_name", False))
    profile = ctx.settings.get("profile") or {}
    nickname = str(profile.get("nickname") or "").strip() if isinstance(profile, dict) else ""
    plain = [x for x in missing if not x.uses_name]
    blocked = _blocked(dash)
    job = _job(dash)
    return {
        "total": len(lines), "recorded": len(recorded), "missing": len(missing),
        "named": sum(1 for x in lines if x.uses_name), "named_missing": len(missing) - len(plain),
        "nickname": nickname, "share_name": share,
        "minutes": round(estimate_seconds(missing) / 60, 1),
        "minutes_without_name": round(estimate_seconds(plain) / 60, 1),
        "for_age_missing": sum(1 for x in missing if x.group == "activities" and x.suitable),
        "voice": ctx.config.chatgpt.live_voice,
        "blocked": blocked[0] if blocked else None, "blocked_detail": blocked[1] if blocked else None,
        "job": job.status() if job is not None else None,
    }


@router.get("")
async def voice_lines(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    lines = await asyncio.to_thread(_lines, dash)
    return await asyncio.to_thread(_view, dash, lines)


@router.post("/record")
async def record(body: RecordVoicesBody | None = None, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    from littlebot.chatgpt import get_auth  # noqa: PLC0415
    from littlebot.chatgpt.voice_record import VoiceLineJob  # noqa: PLC0415

    body = body or RecordVoicesBody()
    job = _job(dash)
    if job is not None and job.running:
        raise HTTPException(status_code=409, detail="The voices are being recorded already.")
    blocked = _blocked(dash)
    if blocked:
        raise HTTPException(status_code=403, detail=blocked[1])
    live = dash.ctx.live
    if live is not None and (live.status() or {}).get("active"):
        raise HTTPException(status_code=409, detail="Finish the voice chat first, then record the voices.")
    lines = await asyncio.to_thread(_lines, dash)
    with_name = bool(dash.ctx.settings.get("ai.share_child_name", False)) if body.with_name is None else body.with_name
    todo = [x for x in lines if with_name or not x.uses_name]
    if not body.again:
        recorded = await asyncio.to_thread(_store(dash).recorded, [x.text for x in todo])
        todo = [x for x in todo if x.hash not in recorded]
    if todo:
        job = VoiceLineJob(_store(dash), get_auth(dash.ctx), dash.ctx.config.chatgpt, todo)
        dash.ctx.extras[JOB_KEY] = job
        dash.spawn(job.run(), name="voice-lines")
        log.info("recording %d voice lines in the ChatGPT voice (from the dashboard; with the name: %s)",
                 len(todo), with_name)
    return {"ok": True, "started": bool(todo), **await asyncio.to_thread(_view, dash, lines)}


@router.post("/stop")
async def stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    job = _job(dash)
    if job is not None and job.running:
        job.stop()
        log.info("recording the voice lines: stop asked from the dashboard")
    return {"ok": True, "job": job.status() if job is not None else None}
