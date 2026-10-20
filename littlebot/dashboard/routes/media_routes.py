"""Media: approved YouTube list, playback controls, volume and screen-time limits."""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import (
    MediaAddBody, MediaPlayBody, MediaReorderBody, MediaSettingsBody, MediaUpdateBody, VolumeBody,
)

router = APIRouter(prefix="/api/media", tags=["media"])

_SETTING_KEYS = ("enabled", "session_limit_minutes", "daily_limit_minutes", "allow_under_min_age", "volume")


def _media_settings(dash: DashState) -> dict[str, Any]:
    s = dash.ctx.settings
    return {k: s.get(f"media.{k}") for k in _SETTING_KEYS}


def _policy(dash: DashState) -> dict[str, Any]:
    pol = dash.ctx.age_policy()
    allow = bool(dash.ctx.settings.get("media.allow_under_min_age"))
    return {
        "age_months": pol.age_months,
        "min_age_months": pol.min_age_months_for_video,
        "under_min_age": not pol.video_allowed(False),
        "parent_authorised": allow,
        "video_allowed": pol.video_allowed(allow),
    }


@router.get("")
async def media_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    media = dash.ctx.media
    return {
        "available": media is not None,
        "items": media.list_items() if media is not None else [],
        "status": media.status() if media is not None else {"state": "unavailable"},
        "settings": _media_settings(dash),
        "policy": _policy(dash),
        "supports_update": media is not None and callable(getattr(media, "update_item", None)),
        "supports_reorder": media is not None and callable(getattr(media, "reorder_items", None)),
    }


@router.post("/items", status_code=201)
async def add_item(body: MediaAddBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    media = dash.need("media")
    # add_item may validate / look things up: keep it off the event loop.
    call = functools.partial(media.add_item, body.url, body.title)
    if body.kind is not None:
        call = functools.partial(media.add_item, body.url, body.title, kind=body.kind)
    item = await asyncio.to_thread(call)
    return {"ok": True, "item": item}


@router.patch("/items/{item_id}")
async def update_item(item_id: int, body: MediaUpdateBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    media = dash.need("media")
    if body.title is None and body.enabled is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    updater = getattr(media, "update_item", None)
    if callable(updater):
        try:
            item = await asyncio.to_thread(functools.partial(updater, item_id, title=body.title, enabled=body.enabled))
        except KeyError as e:
            raise HTTPException(status_code=404, detail="That video is not in the approved list.") from e
        if not item:
            raise HTTPException(status_code=404, detail="That video is not in the approved list.")
        return {"ok": True, "item": item}
    if body.title is None:
        raise HTTPException(status_code=501, detail="Switching single videos on/off is not supported by this player.")
    # Fallback for media modules without update_item(): re-add with the new title.
    existing = next((i for i in media.list_items() if int(i.get("id", -1)) == item_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="That video is not in the approved list.")
    if not media.remove_item(item_id):
        raise HTTPException(status_code=404, detail="That video is not in the approved list.")
    try:
        item = await asyncio.to_thread(media.add_item, existing["url"], body.title)
    except Exception:
        log.exception("re-adding media item during rename failed; restoring")
        await asyncio.to_thread(media.add_item, existing["url"], existing.get("title"))
        raise
    return {"ok": True, "item": item}


@router.post("/reorder")
async def reorder(body: MediaReorderBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    media = dash.need("media")
    fn = getattr(media, "reorder_items", None)
    if not callable(fn):
        raise HTTPException(status_code=501, detail="Reordering is not supported by this player.")
    items = await asyncio.to_thread(fn, list(body.ids))
    return {"ok": True, "items": items if isinstance(items, list) else media.list_items()}


@router.delete("/items/{item_id}")
async def remove_item(item_id: int, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    media = dash.need("media")
    if not media.remove_item(item_id):
        raise HTTPException(status_code=404, detail="That video is not in the approved list.")
    return {"ok": True}


@router.post("/play")
async def play(body: MediaPlayBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    media = dash.need("media")
    dash.cancel("face_demo")
    # The age gate and limits are enforced by the media module (using the
    # parent's media.allow_under_min_age setting); the dashboard never bypasses them.
    status = await media.play(body.item_id, parent_override=False)
    return {"ok": True, "status": status}


@router.post("/pause")
async def pause(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = await dash.need("media").pause()
    return {"ok": bool(ok), "status": dash.ctx.media.status()}


@router.post("/resume")
async def resume(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = await dash.need("media").resume()
    return {"ok": bool(ok), "status": dash.ctx.media.status()}


@router.post("/stop")
async def stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    await dash.need("media").stop("stopped")
    return {"ok": True, "status": dash.ctx.media.status()}


@router.post("/skip")
async def skip(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    status = await dash.need("media").skip()
    return {"ok": True, "status": status}


@router.post("/volume")
async def volume(body: VolumeBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    media = dash.need("media")
    applied = media.set_volume(body.volume)
    dash.ctx.settings.set("media.volume", float(applied), source="dashboard")
    return {"ok": True, "volume": applied}


@router.put("/settings")
async def update_settings(body: MediaSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    settings = dash.ctx.settings
    if body.allow_under_min_age and not body.confirm_under_min_age:
        raise HTTPException(
            status_code=422,
            detail="Please confirm that you understand video is not recommended under 18 months.",
        )
    session = body.session_limit_minutes
    daily = body.daily_limit_minutes
    eff_session = session if session is not None else settings.get("media.session_limit_minutes")
    eff_daily = daily if daily is not None else settings.get("media.daily_limit_minutes")
    if eff_daily and eff_session and eff_session > eff_daily:
        raise HTTPException(status_code=422, detail="The session limit can't be longer than the daily limit.")
    changes = {
        "media.enabled": body.enabled,
        "media.session_limit_minutes": session,
        "media.daily_limit_minutes": daily,
        "media.allow_under_min_age": body.allow_under_min_age,
    }
    for key, value in changes.items():
        if value is not None:
            settings.set(key, value, source="dashboard")
    if body.allow_under_min_age:
        log.warning("parent authorised video for a child under the recommended minimum age")
    if body.enabled is False and dash.ctx.media is not None:
        await dash.ctx.media.stop("stopped")
    return {"ok": True, "settings": _media_settings(dash), "policy": _policy(dash)}
