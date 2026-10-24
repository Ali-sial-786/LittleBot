"""The battery (littlebot/power): its state, the parent's settings, the face's battery sign and the simulated pack."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import PowerSettingsBody, PowerSimBody

router = APIRouter(prefix="/api/power", tags=["power"])

NOT_FITTED = "No battery monitor is fitted: LittleBot runs on mains power."


def power_view(ctx: Any) -> dict[str, Any]:
    """``ctx.power.status()``, or "not fitted" when there is no power service at all."""
    power = getattr(ctx, "power", None)
    if power is None:
        return {"fitted": False, "connected": False, "status": "unavailable", "detail": "not fitted", "kind": "none",
                "percent": None, "health": []}
    try:
        return power.status()
    except Exception:  # noqa: BLE001
        log.exception("power status failed")
        return {"fitted": False, "connected": False, "status": "error", "detail": "not available", "percent": None,
                "health": []}


@router.get("")
async def power_status(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return power_view(dash.ctx)


@router.put("/settings")
async def power_settings(body: PowerSettingsBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    values = {f"power.{k}": v for k, v in body.model_dump(exclude_none=True).items()}
    if not values:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    low = int(values.get("power.low_percent", dash.ctx.settings.get("power.low_percent", 20)))
    critical = int(values.get("power.critical_percent", dash.ctx.settings.get("power.critical_percent", 7)))
    if critical >= low:
        raise HTTPException(status_code=422, detail="The shutdown level must be below the low-battery level.")
    try:
        dash.ctx.settings.update(values, source="dashboard")
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    log.info("battery settings changed from the dashboard: %s", ", ".join(sorted(values)))
    return {"ok": True, **power_view(dash.ctx)}


@router.post("/show")
async def show_battery(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Show the battery sign on LittleBot's face for a moment."""
    power = getattr(dash.ctx, "power", None)
    if power is None or not getattr(power, "fitted", False):
        raise HTTPException(status_code=409, detail=NOT_FITTED)
    return {"ok": bool(power.show())}


@router.post("/simulate")
async def simulate(body: PowerSimBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Set the simulated pack (only with ``power.monitor: simulated``, the desktop preview)."""
    power = getattr(dash.ctx, "power", None)
    temp: Any = "keep" if "temperature_c" not in body.model_fields_set else body.temperature_c
    if power is None or not power.simulate(percent=body.percent, charging=body.charging, temperature_c=temp):
        raise HTTPException(status_code=409, detail="The battery isn't simulated (set power.monitor: simulated).")
    return {"ok": True, **power_view(dash.ctx)}
