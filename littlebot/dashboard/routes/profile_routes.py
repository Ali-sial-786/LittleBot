"""Child profile: minimal, optional information used to choose age-appropriate content."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from littlebot.dashboard.deps import DashState, get_dash
from littlebot.dashboard.models import LANGUAGES, ProfileBody
from littlebot.safety.policy import age_group_label

router = APIRouter(prefix="/api/profile", tags=["profile"])

_FIELDS = ("nickname", "birth_month", "age_months", "language", "favourite_animals", "favourite_songs", "interests",
           "remembered")


def _view(dash: DashState) -> dict[str, Any]:
    ctx = dash.ctx
    prof = ctx.settings.get("profile") or {}
    age = ctx.settings.child_age_months()
    policy = ctx.age_policy()
    if prof.get("age_months") not in (None, ""):
        source = "age_months"
    elif prof.get("birth_month"):
        source = "birth_month"
    else:
        source = "unset"
    return {
        "profile": {k: prof.get(k) for k in _FIELDS},
        "age_months": age,
        "age_source": source,
        "age_group": policy.group,
        "age_group_label": age_group_label(policy.group),
        "video_allowed": policy.video_allowed(False),
        "cloud_ai_age_ok": policy.cloud_ai_allowed(),
        "languages": LANGUAGES,
    }


@router.get("")
async def get_profile(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.put("")
async def update_profile(body: ProfileBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    settings = dash.ctx.settings
    profile = settings.get("profile") or {}
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if key in ("favourite_animals", "favourite_songs", "interests", "remembered") and value is None:
            value = []
        if key == "nickname" and value is None:
            value = ""
        if key == "birth_month" and value is None:
            value = ""
        profile[key] = value
    # Birth month and a manual age are alternatives: setting one clears the other.
    if changes.get("birth_month") and "age_months" not in changes:
        profile["age_months"] = None
    if changes.get("age_months") is not None and "birth_month" not in changes:
        profile["birth_month"] = ""
    settings.set("profile", profile, source="dashboard")
    return {"ok": True, **_view(dash)}


@router.post("/clear")
async def clear_profile(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Forget everything in the child profile (activity favourites included)."""
    dash.ctx.settings.reset("profile")
    return {"ok": True, **_view(dash)}
