"""Privacy: camera / microphone / cloud switches, history clearing, stop interactions."""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import PrivacyBody

router = APIRouter(prefix="/api/privacy", tags=["privacy"])


def _view(dash: DashState) -> dict[str, Any]:
    ctx = dash.ctx
    return {
        **ctx.privacy.snapshot(),
        "stores_audio": bool(ctx.config.audio.store_recordings),
        "stores_transcripts": bool(ctx.config.ai.store_transcripts),
        "history_clearable": any(callable(getattr(getattr(ctx, n, None), "clear_history", None))
                                 for n in ("activities", "brain")),
    }


@router.get("")
async def privacy_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.put("")
async def update_privacy(body: PrivacyBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    flags = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not flags:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    dash.ctx.privacy.set(**flags)  # PermissionError (cloud in offline mode) -> 403
    log.info("privacy changed from dashboard: %s", flags)
    return {"ok": True, **_view(dash)}


@router.post("/clear-history")
async def clear_history(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    cleared: list[str] = []
    for name in ("activities", "brain"):
        svc = getattr(dash.ctx, name, None)
        fn = getattr(svc, "clear_history", None)
        if callable(fn):
            result = fn()
            if inspect.isawaitable(result):
                await result
            cleared.append(name)
    return {"ok": True, "cleared": cleared,
            "detail": "History cleared." if cleared else "There was no stored history to clear."}


@router.post("/stop-interactions")
async def stop_interactions(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    await dash.stop_all("privacy")
    return {"ok": True}
