"""Doorbell and camera alerts (littlebot/skills/doorbell.py, docs/DOORBELL.md).

* ``POST /hooks/doorbell/{secret}``: the web address a phone (MacroDroid, Home Assistant's Companion app) or Home
  Assistant calls when the doorbell rings or a camera sees someone. Not under /api, so it needs no login (a phone
  can't log in): the secret in the address is the password. A wrong or missing secret gets the same plain 404 as any
  unknown page. The body is at most ``MAX_HOOK_BYTES``: JSON, a form or plain text. The ``Host`` allowlist still
  applies (the app's RequestGuard). Nothing it sends is logged, only what kind of event it was and which camera.
* ``/api/doorbell`` (behind the login like every /api page): the settings, the full address to copy into the phone,
  a test ring, and a new address (the old one stops working).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body

router = APIRouter(tags=["doorbell"])

MAX_HOOK_BYTES = 4 * 1024
HOOK_PATH = "/hooks/doorbell/"
LAN_HELP = ("LAN access is off, so a phone or Home Assistant can't reach LittleBot. Turn it on: add "
            "\"dashboard: lan_access: true\" to config/local.yaml (or start LittleBot with --lan) and restart.")


class DoorbellSettingsBody(Body):
    enabled: bool | None = None
    events: list[str] | None = Field(default=None, max_length=10)
    at_night: bool | None = None


def _skill(dash: DashState) -> Any:
    hub = getattr(dash.ctx, "skills", None)
    skill = hub.get("doorbell") if hub is not None and hasattr(hub, "get") else None
    if skill is None or not skill.available():
        return None
    return skill


def _need(dash: DashState) -> Any:
    skill = _skill(dash)
    if skill is None:
        raise HTTPException(status_code=503, detail="Door alerts are switched off (skills.doorbell in the config).")
    return skill


def _not_found() -> JSONResponse:
    # The same answer as any page that doesn't exist: nothing says whether the address was nearly right.
    return JSONResponse({"detail": "Not Found"}, status_code=404, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- the hook (no login: the secret is the password)
@router.post(HOOK_PATH + "{token}", include_in_schema=False)
async def doorbell_hook(token: str, request: Request, dash: DashState = Depends(get_dash)) -> JSONResponse:
    from littlebot.skills.doorbell import token_ok  # noqa: PLC0415

    skill = _skill(dash)
    if skill is None or not token_ok(dash.ctx, token):
        return _not_found()
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_HOOK_BYTES:
        return JSONResponse({"detail": "That request is too large."}, status_code=413,
                            headers={"Cache-Control": "no-store", "Connection": "close"})
    raw = await request.body()
    if len(raw) > MAX_HOOK_BYTES:
        return JSONResponse({"detail": "That request is too large."}, status_code=413,
                            headers={"Cache-Control": "no-store", "Connection": "close"})
    event = read_event(raw, request.headers.get("content-type") or "")
    res = skill.handle(event)
    return JSONResponse({"ok": True, "said": bool(res.get("said"))}, headers={"Cache-Control": "no-store"})


def read_event(raw: bytes, content_type: str) -> dict[str, Any]:
    """JSON, a form (title=...&text=...) or plain text -> a dict for the skill. Never raises."""
    text = raw.decode("utf-8", "replace").strip()
    ctype = content_type.split(";")[0].strip().lower()
    if not text:
        return {}
    if ctype == "application/x-www-form-urlencoded":
        form = parse_qs(text, keep_blank_values=False, max_num_fields=20)
        return {k: v[0] for k, v in form.items() if v}
    if ctype.endswith("json") or text.startswith("{"):  # MacroDroid may send JSON as text/plain
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return {str(k): v for k, v in list(data.items())[:20] if isinstance(v, (str, int, float, bool))}
        if isinstance(data, str):
            return {"text": data}
    return {"text": text}


# ---------------------------------------------------------------- the dashboard (behind the login)
def _hook_urls(dash: DashState, request: Request, token: str) -> tuple[list[str], bool]:
    """Every address the hook answers on, the ones a phone can use first; and whether a phone can reach it."""
    lan = bool(getattr(dash.ctx.config.dashboard, "lan_access", False))
    bases = [u for u in dash.urls if u] or [str(request.base_url)]
    outside = [b for b in bases if (urlsplit(b).hostname or "") not in ("127.0.0.1", "localhost", "::1")]
    inside = [b for b in bases if b not in outside]
    return [b.rstrip("/") + HOOK_PATH + token for b in outside + inside], lan and bool(outside)


def _view(dash: DashState, request: Request, skill: Any) -> dict[str, Any]:
    from littlebot.skills.doorbell import KINDS, hook_token  # noqa: PLC0415

    urls, reachable = _hook_urls(dash, request, hook_token(dash.ctx))
    st = skill.status()
    return {"available": True, "enabled": st["enabled"], "events": st["events"], "at_night": st["at_night"],
            "kinds": list(KINDS), "url": urls[0], "urls": urls, "reachable": reachable,
            "lan_access": bool(getattr(dash.ctx.config.dashboard, "lan_access", False)),
            "lan_help": None if reachable else LAN_HELP, "last": st["last"]}


@router.get("/api/doorbell")
async def doorbell_info(request: Request, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """The settings and the full hook address (with its secret: this page is behind the login)."""
    skill = _skill(dash)
    if skill is None:
        return {"available": False}
    return _view(dash, request, skill)


@router.put("/api/doorbell/settings")
async def doorbell_settings(body: DoorbellSettingsBody, request: Request,
                            dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    skill = _need(dash)
    res = skill.change(body.model_dump(exclude_none=True), source="dashboard")
    if res.startswith("error"):
        raise HTTPException(status_code=422, detail=res.removeprefix("error: ").capitalize())
    return _view(dash, request, skill)


@router.post("/api/doorbell/test")
async def doorbell_test(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """A pretend doorbell press: said (and shown) like a real one, but not remembered."""
    skill = _need(dash)
    res = skill.handle({"event": "doorbell"}, test=True)
    log.info("dashboard: doorbell test (%s)", "said" if res.get("said") else res.get("why"))
    return {"ok": True, "said": bool(res.get("said")), "why": res.get("why")}


@router.post("/api/doorbell/rotate")
async def doorbell_rotate(request: Request, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """A new secret address: the old one stops working at once (put the new one in the phone)."""
    from littlebot.skills.doorbell import new_token  # noqa: PLC0415

    skill = _need(dash)
    new_token(dash.ctx)
    return _view(dash, request, skill)
