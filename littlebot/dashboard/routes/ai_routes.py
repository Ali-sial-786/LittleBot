"""Cloud AI conversation settings. API keys are never returned - only has_*_key booleans."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash
from littlebot.dashboard.models import AISettingsBody, ProviderTestBody
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api/ai", tags=["ai"])

_KEYS = ("enabled", "provider", "max_words", "max_requests_per_hour", "max_requests_per_day", "daily_budget_usd")


def _view(dash: DashState) -> dict[str, Any]:
    ctx = dash.ctx
    public = ctx.config.public_dict()
    brain = ctx.brain
    policy = ctx.age_policy()
    providers: list[dict[str, Any]] = []
    brain_status: dict[str, Any] = {"status": "unavailable"}
    if brain is not None:
        try:
            providers = list(brain.providers())
        except Exception:  # noqa: BLE001
            providers = []
        try:
            brain_status = dict(brain.status())
        except Exception:  # noqa: BLE001
            brain_status = {"status": "error"}
    return scrub({
        "available": brain is not None,
        "settings": {k: ctx.settings.get(f"ai.{k}") for k in _KEYS},
        "keys": {"has_anthropic_key": public["has_anthropic_key"], "has_openai_key": public["has_openai_key"]},
        "models": {"anthropic": ctx.config.ai.anthropic_model, "openai": ctx.config.ai.openai_model},
        "brain": brain_status,
        "providers": providers,
        "policy": {
            "age_months": policy.age_months,
            "min_age_months_for_cloud": policy.min_age_months_for_cloud_ai,
            "cloud_ai_age_ok": policy.cloud_ai_allowed(),
            "max_reply_words": policy.max_reply_words(int(ctx.settings.get("ai.max_words") or 30)),
        },
        "privacy": {"cloud_enabled": ctx.privacy.cloud_enabled, "offline_mode": ctx.config.is_offline},
    })


@router.get("")
async def ai_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.put("/settings")
async def update_ai(body: AISettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ctx = dash.ctx
    if body.enabled and ctx.config.is_offline:
        raise PermissionError("Cloud AI is unavailable in offline mode.")
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            ctx.settings.set(f"ai.{key}", value, source="dashboard")
    return {"ok": True, **_view(dash)}


@router.get("/providers")
async def providers(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    brain = dash.ctx.brain
    return {"providers": scrub(list(brain.providers())) if brain is not None else []}


@router.post("/test")
async def test_provider(body: ProviderTestBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    brain = dash.need("brain")
    if body.provider != "mock" and not dash.ctx.privacy.cloud_enabled:
        raise HTTPException(status_code=403,
                            detail="Cloud communication is turned off in Privacy. Turn it on to test a cloud provider.")
    result = await brain.test_provider(body.provider)
    return {"ok": True, "result": scrub(dict(result or {}))}
