"""Live camera view for the parent (the Camera page): small JPEG frames the page asks for one after another,
and a one-off snapshot the parent downloads to their own device.

Frames come from the vision thread's camera (it is never opened a second time) via
``VisionService.live_view_frame``: the vision thread encodes them only while a page keeps asking, and stops
a few seconds after the last request. Nothing is written to disk or logged here; responses are
``Cache-Control: no-store`` (set for every /api path) so the browser does not keep them either. The camera
privacy switch is checked on every request, before and after waiting for a frame. Start and stop of
watching are logged (no image data) and shown on the robot's own screen.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from littlebot.dashboard.deps import DashState, client_ip, get_dash, log
from littlebot.dashboard.models import Body
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api/camera", tags=["camera"])

VIEWER_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"  # random id chosen by each open Camera page
FRAME_WAIT_SECONDS = 2.0
SNAPSHOT_WIDTH = 640  # the full camera picture
SNAPSHOT_QUALITY = 85
SNAPSHOT_TIMEOUT = 3.0

# Why there is no picture, in the parent's words (the page shows these), and the HTTP status for each.
REASONS: dict[str, tuple[int, str]] = {
    "camera_off": (403, "Camera is off (Privacy)."),
    "paused": (409, "Paused while a video plays on LittleBot. The picture comes back when the video ends."),
    "no_camera": (503, "No camera is connected right now. LittleBot keeps trying."),
    "busy": (409, "Too many devices are watching. Close the camera view on another device."),
    "timeout": (504, "Waiting for the camera…"),
    "stopped": (409, "The live view was stopped."),
    "not_watching": (409, "Start the live view first, then take a snapshot."),
    "unavailable": (503, "The live camera view isn't available on this robot."),
}


class ViewerBody(Body):
    viewer: str = Field(pattern=VIEWER_PATTERN)


def _blocked(code: str, detail: str = "") -> JSONResponse:
    status, message = REASONS.get(code, REASONS["unavailable"])
    body: dict[str, Any] = {"detail": message, "code": code if code in REASONS else "unavailable"}
    if detail:
        body["why"] = str(detail)[:200]  # the technical reason (e.g. "no camera at /dev/video0"), for the page
    return JSONResponse(body, status_code=status)


def _failed(e: Exception, what: str) -> JSONResponse:
    reason = getattr(e, "reason", None)
    if reason is None:  # not PhotoUnavailable: a bug or a broken driver
        log.exception("%s failed", what)
    return _blocked(str(reason or "unavailable"), str(getattr(e, "detail", "")))


def _vision(dash: DashState) -> Any:
    vision = dash.ctx.vision
    return vision if vision is not None and callable(getattr(vision, "live_view_frame", None)) else None


def _blocked_now(dash: DashState, vision: Any, status: dict[str, Any]) -> str | None:
    """Why a live view would not show a picture right now (None: it can)."""
    if not dash.ctx.privacy.camera_enabled:
        return "camera_off"
    if vision is None:
        return "unavailable"
    if status.get("detect_mode") == "paused":
        return "paused"
    if status.get("camera") in ("unavailable", "disconnected") or status.get("backend") == "none":
        return "no_camera"
    return None


@router.get("")
async def camera_info(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    vision = _vision(dash)
    status: dict[str, Any] = {}
    if dash.ctx.vision is not None:
        try:
            status = dict(dash.ctx.vision.status())
        except Exception:  # noqa: BLE001 - never break the page
            log.exception("vision status failed")
    blocked = _blocked_now(dash, vision, status)
    return scrub({
        "available": vision is not None,
        "camera_enabled": dash.ctx.privacy.camera_enabled,
        "camera": status.get("camera", "unavailable"),
        "detail": status.get("detail"),
        "backend": status.get("backend"),
        "simulated": status.get("backend") in ("mock", "fixture"),
        "resolution": status.get("resolution"),
        "mode": status.get("detect_mode"),
        "blocked": blocked,
        "blocked_detail": REASONS[blocked][1] if blocked else None,
        "live_view": status.get("live_view") or {"viewers": 0, "fps": 0.0},
    })


@router.get("/frame")
async def frame(request: Request, viewer: str = Query(pattern=VIEWER_PATTERN), after: int = Query(0, ge=0),
                dash: DashState = Depends(get_dash)) -> Response:
    """The next live-view JPEG for this page (``after`` = the last frame it showed). Waits up to ~2 s for one."""
    vision = _vision(dash)
    if vision is None:
        return _blocked("unavailable")
    if not dash.ctx.privacy.camera_enabled:
        return _blocked("camera_off")
    try:
        jpeg, seq = await vision.live_view_frame(viewer, after=after, label=f"the dashboard ({client_ip(request)})",
                                                 timeout=FRAME_WAIT_SECONDS)
    except Exception as e:  # noqa: BLE001 - PhotoUnavailable(reason) or a broken camera
        return _failed(e, "live view frame")
    if not dash.ctx.privacy.camera_enabled:  # switched off while this request waited
        return _blocked("camera_off")
    return Response(jpeg, media_type="image/jpeg", headers={"X-Frame-Seq": str(int(seq))})


@router.post("/stop")
async def stop(body: ViewerBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    vision = _vision(dash)
    stopped = bool(vision.stop_live_view(body.viewer)) if vision is not None else False
    return {"ok": True, "stopped": stopped}


@router.post("/snapshot")
async def snapshot(body: ViewerBody, request: Request, dash: DashState = Depends(get_dash)) -> Response:
    """One full-size JPEG for the parent to save on their own device. Taken only now, never kept on the robot.
    Only from a page that is watching the live view, so the robot's screen shows the camera sign meanwhile."""
    vision = _vision(dash)
    if vision is None or not callable(getattr(vision, "capture_photo", None)):
        return _blocked("unavailable")
    if not dash.ctx.privacy.camera_enabled:
        return _blocked("camera_off")
    if not vision.watching(body.viewer):
        return _blocked("not_watching")
    try:
        jpeg = await vision.capture_photo(max_width=SNAPSHOT_WIDTH, quality=SNAPSHOT_QUALITY, timeout=SNAPSHOT_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - PhotoUnavailable(reason) or a broken camera
        return _failed(e, "camera snapshot")
    if not dash.ctx.privacy.camera_enabled:
        return _blocked("camera_off")
    log.info("camera snapshot sent to the dashboard (%s); nothing kept on the robot", client_ip(request))
    name = time.strftime("littlebot-%Y%m%d-%H%M%S.jpg")
    return Response(jpeg, media_type="image/jpeg", headers={"Content-Disposition": f'attachment; filename="{name}"'})
