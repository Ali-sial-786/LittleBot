"""The family's own "hey little bot" clips for the next wake word training (littlebot/audio/wake_clips.py).

``POST /upload?name=Ali`` takes one recording as the request body (the file as it is: m4a, mp3, wav ...; up to
``MAX_UPLOAD_BYTES``, app.py lets only this path send that much) and keeps each phrase found in it as a clip; the
recording itself is not kept. ``GET`` says how many clips each person has; ``DELETE /{name}`` removes theirs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from littlebot.audio import wake_clips
from littlebot.dashboard.deps import DashState, get_dash, log

router = APIRouter(prefix="/api/wake/clips", tags=["wake"])

UPLOAD_PATH = "/api/wake/clips/upload"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # a few minutes of a phone's recording is 2-10 MB, a WAV up to ~35 MB
_busy = asyncio.Lock()  # one recording at a time


def _names(dash: DashState) -> list[str]:
    """Who's who's names, offered on the page (anyone else can be typed in)."""
    try:
        from littlebot.vision.faces import get_faces  # noqa: PLC0415

        return [str(p["name"]) for p in get_faces(dash.ctx).store.people() if p.get("name")]
    except Exception:  # noqa: BLE001 - only suggestions
        return []


def _view(dash: DashState) -> dict[str, Any]:
    return {"people": wake_clips.summary(dash.ctx.config), "names": _names(dash),
            "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024), "max_minutes": wake_clips.MAX_SECONDS // 60}


def _model(dash: DashState) -> Any:
    """The wake word's own Vosk model (load_model shares the one in memory)."""
    from littlebot.audio.stt.vosk import load_model, model_present  # noqa: PLC0415
    from littlebot.audio.wakeword import wake_model_dir  # noqa: PLC0415

    path = wake_model_dir(dash.ctx.config)
    if path is None or not model_present(path):
        raise HTTPException(status_code=503, detail="The wake word's speech model isn't on LittleBot.")
    try:
        return load_model(path)
    except Exception:  # noqa: BLE001 - vosk missing or the model unreadable
        log.warning("wake clips: the speech model didn't load", exc_info=True)
        raise HTTPException(status_code=503, detail="The wake word's speech model didn't load.") from None


@router.get("")
async def clips(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("/upload")
async def upload(request: Request, name: str = Query(..., min_length=1, max_length=40),
                 dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if _busy.locked():
        raise HTTPException(status_code=409, detail="Another recording is being cut: try again in a minute.")
    async with _busy:
        data = await request.body()
        if not data:
            raise HTTPException(status_code=422, detail="No recording came.")

        def work() -> dict[str, Any]:
            return wake_clips.add_recording(dash.ctx.config, name, data, _model(dash))
        try:
            added = await asyncio.to_thread(work)
        except wake_clips.ClipError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        finally:
            del data
    log.info("wake clips: %d from a %.0f s recording of %s (%d in all)", added["found"], added["seconds"],
             added["name"], added["clips"])
    return {**_view(dash), "added": added}


@router.delete("/{name}")
async def delete(name: str = Path(..., min_length=1, max_length=40), dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    try:
        gone = wake_clips.delete(dash.ctx.config, name)
    except wake_clips.ClipError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    if not gone:
        raise HTTPException(status_code=404, detail="No clips of that name.")
    log.info("wake clips: %s's deleted", wake_clips.slug(name))
    return _view(dash)
