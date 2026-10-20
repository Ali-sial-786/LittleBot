"""Activities: browse (age / category filters), run controls, caregiver prompts, preferences."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from littlebot.core.interfaces import ACTIVITY_CATEGORIES, AGE_GROUPS
from littlebot.dashboard.deps import DashState, get_dash
from littlebot.dashboard.models import ACTIVITY_ID_RE, ActivityPrefsBody, RespondBody

router = APIRouter(prefix="/api/activities", tags=["activities"])

MAX_PREFS = 500
_GROUP_RANGES = {key: (lo, hi) for key, lo, hi, _label in AGE_GROUPS}


def _check_id(activity_id: str) -> str:
    if not ACTIVITY_ID_RE.match(activity_id):
        raise HTTPException(status_code=404, detail="Unknown activity.")
    return activity_id


def age_label(lo: Any, hi: Any) -> str:
    try:
        lo_i, hi_i = int(lo), int(hi)
    except (TypeError, ValueError):
        return ""

    def fmt(m: int) -> str:
        return f"{m // 12} yr" if m >= 24 and m % 12 == 0 else f"{m} mo"

    if lo_i >= 24 and hi_i % 12 == 0 and lo_i % 12 == 0:
        return f"{lo_i // 12}–{hi_i // 12} years"
    return f"{lo_i}–{hi_i} months" if hi_i <= 36 else f"{fmt(lo_i)} – {fmt(hi_i)}"


def _prefs(dash: DashState) -> dict[str, dict[str, bool]]:
    prof = dash.ctx.settings.get("profile") or {}
    prefs = prof.get("activity_preferences") or {}
    return prefs if isinstance(prefs, dict) else {}


def _decorate(item: dict[str, Any], prefs: dict[str, dict[str, bool]], age: int | None) -> dict[str, Any]:
    out = dict(item)
    p = prefs.get(str(item.get("id")), {})
    out["enabled"] = bool(p.get("enabled", True))
    out["favourite"] = bool(p.get("favourite", False))
    lo, hi = item.get("min_age_months"), item.get("max_age_months")
    out["age_label"] = age_label(lo, hi)
    try:
        out["suitable_now"] = age is not None and int(lo) <= age < int(hi)
    except (TypeError, ValueError):
        out["suitable_now"] = False
    return out


@router.get("")
async def list_activities(
    dash: DashState = Depends(get_dash),
    age_months: int | None = Query(None, ge=0, le=120),
    category: str | None = Query(None, max_length=32),
    age_group: str | None = Query(None, max_length=32),
    favourites: bool = False,
) -> dict[str, Any]:
    acts = dash.ctx.activities
    if category is not None and category not in ACTIVITY_CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown category.")
    if age_group is not None and age_group not in _GROUP_RANGES:
        raise HTTPException(status_code=422, detail="Unknown age group.")
    child_age = dash.ctx.settings.child_age_months()
    meta = {
        "categories": list(ACTIVITY_CATEGORIES),
        "age_groups": [{"key": k, "min": lo, "max": hi, "label": label} for k, lo, hi, label in AGE_GROUPS],
        "child_age_months": child_age,
        "available": acts is not None,
    }
    if acts is None:
        return {"items": [], **meta}
    items = acts.list(age_months=age_months, category=category)
    if age_group is not None:
        g_lo, g_hi = _GROUP_RANGES[age_group]
        items = [i for i in items if _overlaps(i, g_lo, g_hi)]
    prefs = _prefs(dash)
    out = [_decorate(i, prefs, child_age) for i in items]
    if favourites:
        out = [i for i in out if i["favourite"]]
    out.sort(key=lambda i: (not i["favourite"], str(i.get("name", "")).lower()))
    return {"items": out, **meta}


def _overlaps(item: dict[str, Any], lo: int, hi: int) -> bool:
    try:
        a, b = int(item.get("min_age_months", 0)), int(item.get("max_age_months", 999))
    except (TypeError, ValueError):
        return True
    return a < hi and lo < b


@router.get("/status")
async def activity_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    acts = dash.ctx.activities
    return acts.status() if acts is not None else {"status": "unavailable", "running": False}


@router.post("/stop")
async def stop_activity(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    await dash.need("activities").stop("parent stop")
    return {"ok": True, "status": dash.ctx.activities.status()}


@router.post("/pause")
async def pause_activity(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = await dash.need("activities").pause()
    return {"ok": bool(ok), "status": dash.ctx.activities.status()}


@router.post("/resume")
async def resume_activity(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = await dash.need("activities").resume()
    return {"ok": bool(ok), "status": dash.ctx.activities.status()}


@router.post("/respond")
async def respond(body: RespondBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = dash.need("activities").respond(body.response, body.prompt_id)
    if not ok:
        raise HTTPException(status_code=409, detail="That question has already been answered or has expired.")
    return {"ok": True, "status": dash.ctx.activities.status()}


@router.get("/{activity_id}")
async def get_activity(activity_id: str, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    acts = dash.need("activities")
    item = acts.get(_check_id(activity_id))
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown activity.")
    return _decorate(item, _prefs(dash), dash.ctx.settings.child_age_months())


@router.post("/{activity_id}/start")
async def start_activity(activity_id: str, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    acts = dash.need("activities")
    dash.cancel("face_demo")
    status = await acts.start(_check_id(activity_id), source="dashboard")
    return {"ok": True, "status": status}


@router.put("/{activity_id}/preferences")
async def set_preferences(activity_id: str, body: ActivityPrefsBody,
                          dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    acts = dash.need("activities")
    if acts.get(_check_id(activity_id)) is None:
        raise HTTPException(status_code=404, detail="Unknown activity.")
    settings = dash.ctx.settings
    profile = settings.get("profile") or {}
    prefs = dict(profile.get("activity_preferences") or {})
    current = dict(prefs.get(activity_id) or {"enabled": True, "favourite": False})
    if body.enabled is not None:
        current["enabled"] = body.enabled
    if body.favourite is not None:
        current["favourite"] = body.favourite
    current = {"enabled": bool(current.get("enabled", True)), "favourite": bool(current.get("favourite", False))}
    if current == {"enabled": True, "favourite": False}:
        prefs.pop(activity_id, None)  # default: keep storage minimal
    else:
        if activity_id not in prefs and len(prefs) >= MAX_PREFS:
            raise HTTPException(status_code=422, detail="Too many saved activity preferences.")
        prefs[activity_id] = current
    profile["activity_preferences"] = prefs
    settings.set("profile", profile, source="dashboard")
    return {"ok": True, "id": activity_id, **current}
