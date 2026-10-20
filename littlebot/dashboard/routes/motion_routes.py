"""Motion: gestures, joints, neutral pose, emergency stop and (guarded) physical arming."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import ARM_PHRASE, ArmBody, GestureBody, JointBody

router = APIRouter(prefix="/api/motion", tags=["motion"])


def _view(dash: DashState) -> dict[str, Any]:
    ctx = dash.ctx
    motion = ctx.motion
    cfg = ctx.config.motion
    physical = bool(cfg.physical_enabled)
    armed = bool(ctx.settings.get("motion.physical_armed")) and physical
    status = motion.status() if motion is not None else {"status": "unavailable", "joints": {}}
    return {
        "available": motion is not None,
        "status": status,
        "gestures": motion.gestures() if motion is not None else [],
        "physical_enabled": physical,
        "armed": armed,
        "simulation_only": not physical,
        "arm_phrase": ARM_PHRASE if physical else None,
        "servos": {name: {"neutral": s.neutral, "min": s.min_angle, "max": s.max_angle, "inverted": s.inverted}
                   for name, s in cfg.servos.items()},
    }


def _joint_limits(dash: DashState, joint: str) -> tuple[float, float]:
    status = dash.ctx.motion.status() if dash.ctx.motion is not None else {}
    j = (status.get("joints") or {}).get(joint)
    if isinstance(j, dict) and "min" in j and "max" in j:
        return float(j["min"]), float(j["max"])
    servo = dash.ctx.config.motion.servos.get(joint)
    if servo is None:
        raise HTTPException(status_code=404, detail="Unknown joint.")
    return float(servo.min_angle), float(servo.max_angle)


def _refuse_if_estopped(dash: DashState) -> None:
    status = dash.ctx.motion.status() if dash.ctx.motion is not None else {}
    if status.get("estop"):
        raise HTTPException(status_code=409, detail="Emergency stop is active. Reset it before moving again.")


@router.get("")
async def motion_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("/gesture")
async def gesture(body: GestureBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    motion = dash.need("motion")
    if body.name not in motion.gestures():
        raise HTTPException(status_code=404, detail="Unknown gesture.")
    _refuse_if_estopped(dash)
    dash.spawn(motion.run_gesture(body.name), name="motion")
    return {"ok": True, "started": body.name}


@router.post("/joint")
async def joint(body: JointBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    motion = dash.need("motion")
    lo, hi = _joint_limits(dash, body.joint)
    if not lo <= body.angle <= hi:
        raise HTTPException(status_code=422, detail=f"{body.joint} must stay between {lo:g}° and {hi:g}°.")
    _refuse_if_estopped(dash)
    dash.spawn(motion.move_joint(body.joint, body.angle, speed_dps=body.speed_dps), name="motion")
    return {"ok": True}


@router.post("/neutral")
async def neutral(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    motion = dash.need("motion")
    _refuse_if_estopped(dash)
    dash.spawn(motion.neutral(), name="motion")
    return {"ok": True}


@router.post("/cancel")
async def cancel(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    dash.need("motion").cancel()
    dash.cancel("motion")
    return {"ok": True}


@router.post("/estop")
async def emergency_stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    motion = dash.need("motion")
    motion.emergency_stop("dashboard")  # synchronous and immediate
    dash.cancel("motion")
    if dash.ctx.settings.get("motion.physical_armed"):
        dash.ctx.settings.set("motion.physical_armed", False, source="dashboard")
    log.warning("EMERGENCY STOP pressed on the dashboard")
    return {"ok": True, **_view(dash)}


@router.post("/estop/reset")
async def reset_emergency_stop(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ok = dash.need("motion").reset_emergency_stop()
    if not ok:
        raise HTTPException(status_code=409, detail="The emergency stop could not be reset. Check the motion system.")
    return {"ok": True, **_view(dash)}


@router.post("/arm")
async def arm(body: ArmBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    ctx = dash.ctx
    if not body.armed:
        ctx.settings.set("motion.physical_armed", False, source="dashboard")
        return {"ok": True, **_view(dash)}
    if not ctx.config.motion.physical_enabled:
        raise PermissionError(
            "Physical motion is disabled in the configuration (motion.physical_enabled: false). "
            "LittleBot runs in simulation only."
        )
    if body.confirmation.strip().upper() != ARM_PHRASE:
        raise HTTPException(status_code=422, detail=f'Type "{ARM_PHRASE}" to confirm.')
    _refuse_if_estopped(dash)
    ctx.settings.set("motion.physical_armed", True, source="dashboard")
    log.warning("physical motion ARMED from the dashboard")
    return {"ok": True, **_view(dash)}
