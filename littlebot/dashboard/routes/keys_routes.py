"""API keys and tokens from the phone (littlebot/core/keys.py, docs/KEYS.md): which ones are set and where each one
comes from, save one (checked first), check the one in use, remove the one saved here.

A key is never returned, logged or put in an error: only its last four characters (``ends_with``). Log lines say
which key and what happened.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from littlebot.core import keys
from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import SecretBody
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api/keys", tags=["keys"])


class KeyBody(SecretBody):
    value: str = Field(..., max_length=1000)  # checked in keys.clean_value (an error never repeats it)


def _privacy(dash: DashState) -> dict[str, Any]:
    return {"cloud_enabled": dash.ctx.privacy.cloud_enabled, "offline_mode": dash.ctx.config.is_offline}


def _spec(name: str) -> keys.KeySpec:
    if name not in keys.BY_NAME:
        raise HTTPException(status_code=404, detail="There is no key with that name.")
    return keys.BY_NAME[name]


@router.get("")
async def list_keys(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return scrub({"keys": keys.status(dash.ctx.config), **_privacy(dash)})


async def _save(name: str, body: KeyBody, dash: DashState) -> dict[str, Any]:
    s = _spec(name)
    try:
        value = keys.clean_value(body.value)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    checked = await keys.check(dash.ctx, s.name, value)
    if checked["result"] == "refused":
        log.info("dashboard: %s key not saved (refused)", s.name)
        raise HTTPException(status_code=400, detail=f"{checked['detail']} Nothing was saved.")
    try:
        st = keys.save(dash.ctx, s.name, value)
    except OSError:
        log.warning("dashboard: %s key not saved (couldn't write the file)", s.name)
        raise HTTPException(status_code=500, detail="LittleBot couldn't save the key (writing the file failed).") from None
    log.info("dashboard: %s key saved (%s)", s.name, "checked OK" if checked["result"] == "ok" else "not checked")
    detail = "Saved. " + checked["detail"]
    if dash.ctx.config.is_offline:
        detail += " LittleBot is in offline mode: it uses the key once it isn't."
    elif not s.live:
        detail += " Restart LittleBot to use it."
    return scrub({"ok": True, "key": st, "check": checked, "detail": detail, "restart_needed": not s.live})


@router.put("/{name}")
async def save_key(name: str, body: KeyBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return await _save(name, body, dash)


@router.post("/{name}")
async def save_key_post(name: str, body: KeyBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return await _save(name, body, dash)


async def _remove(name: str, dash: DashState) -> dict[str, Any]:
    s = _spec(name)
    try:
        st = keys.remove(dash.ctx, s.name)
    except OSError:
        log.warning("dashboard: %s key not removed (couldn't delete the file)", s.name)
        raise HTTPException(status_code=500, detail="LittleBot couldn't delete the saved key.") from None
    if not st["removed"]:
        detail = f"There was no {s.label} key saved here."
    elif st["set"]:
        detail = f"Removed. LittleBot uses the {s.label} key from the " + (
            ".env file" if st["source"] == "env" else "older key file") + " again."
    else:
        detail = f"Removed. LittleBot has no {s.label} key now."
    log.info("dashboard: %s key %s", s.name, "removed" if st["removed"] else "remove asked (none saved)")
    return scrub({"ok": True, "key": st, "detail": detail, "restart_needed": bool(st["removed"]) and not s.live})


@router.delete("/{name}")
async def remove_key(name: str, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return await _remove(name, dash)


@router.post("/{name}/remove")
async def remove_key_post(name: str, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return await _remove(name, dash)


@router.post("/{name}/check")
async def check_key(name: str, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Try the key in use now with one cheap read-only request."""
    s = _spec(name)
    checked = await keys.check(dash.ctx, s.name)
    if checked["result"] == "missing":
        raise HTTPException(status_code=409, detail=checked["detail"])
    log.info("dashboard: %s key checked (%s)", s.name, checked["result"])
    return scrub({"ok": checked["result"] == "ok", "check": checked, "detail": checked["detail"],
                  "key": keys.key_status(dash.ctx.config, s)})
