"""/api/camera: the parent's live camera view and snapshot - login required, privacy switch obeyed, frames from
the vision thread's (mock) camera, work stopped when the viewer goes away, video pause reported, nothing kept."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import numpy as np
import pytest

from littlebot.core.events import Topics, collect
from littlebot.core.state import RobotState
from littlebot.vision import service as vs
from littlebot.vision.camera import MockCamera
from littlebot.vision.imaging import get_cv2
from littlebot.vision.service import VisionService

from dashboard_fakes import make_client

pytestmark = pytest.mark.skipif(get_cv2() is None, reason="OpenCV needed to encode JPEG")

VIEWER = "dashboard-test-viewer-01"


@pytest.fixture
async def vision(dctx):
    """The real VisionService (its own thread) on a 640x480 mock camera, in place of the dashboard's fake."""
    cam = MockCamera(640, 480, face=(0.5, 0.5, 0.3))
    svc = VisionService(dctx, camera_factory=lambda kind: cam)
    svc.camera = cam  # for the tests
    dctx.vision = svc
    await svc.start()
    yield svc
    await svc.stop()


def decode(data: bytes) -> np.ndarray:
    cv2 = get_cv2()
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


async def frame(client: Any, viewer: str = VIEWER, after: int = 0) -> Any:
    return await client.get("/api/camera/frame", params={"viewer": viewer, "after": after})


async def wait_for(pred: Any, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


# ------------------------------------------------------------------ login
async def test_camera_refuses_without_login(client, vision):
    for method, path, kw in (
        ("GET", "/api/camera", {}),
        ("GET", "/api/camera/frame", {"params": {"viewer": VIEWER}}),
        ("POST", "/api/camera/stop", {"json": {"viewer": VIEWER}}),
        ("POST", "/api/camera/snapshot", {"json": {"viewer": VIEWER}}),
    ):
        r = await client.request(method, path, **kw)
        assert r.status_code == 401, (method, path)
        assert r.json()["code"] == "auth_required" and not r.headers["content-type"].startswith("image/")
    await asyncio.sleep(0.3)
    assert vision.status()["live_view"]["viewers"] == 0 and vision._view_seq == 0  # nothing started


async def test_a_logged_out_session_gets_nothing(authed, vision):
    r = await frame(authed)
    assert r.status_code == 200
    assert (await authed.post("/api/logout")).status_code == 200
    r = await frame(authed, viewer="another-viewer-0001")
    assert r.status_code == 401


async def test_snapshot_needs_the_csrf_token(authed, vision):
    assert (await frame(authed)).status_code == 200
    token = authed.headers.pop("X-CSRF-Token")
    try:
        r = await authed.post("/api/camera/snapshot", json={"viewer": VIEWER})
        assert r.status_code == 403 and r.json()["code"] == "csrf"
    finally:
        authed.headers["X-CSRF-Token"] = token


# ------------------------------------------------------------------ frames
async def test_live_frames_come_from_the_mock_camera(authed, vision, dctx):
    seen = collect(dctx.bus, [Topics.VISION_LIVE_VIEW])
    r = await frame(authed)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"  # the browser keeps nothing either
    seq = int(r.headers["x-frame-seq"])
    assert decode(r.content).shape[:2] == (360, vs.LIVE_VIEW_WIDTH)
    r2 = await frame(authed, after=seq)
    assert r2.status_code == 200 and int(r2.headers["x-frame-seq"]) > seq
    assert vision.camera.open_count == 1  # the vision thread's own camera
    d = (await authed.get("/api/camera")).json()
    assert d["available"] and d["blocked"] is None and d["simulated"] is True
    assert d["live_view"]["viewers"] == 1 and d["live_view"]["max_fps"] == vs.LIVE_VIEW_MAX_FPS
    assert seen and seen[0].data == {"active": True, "viewers": 1}


async def test_rejects_a_bad_viewer_id(authed, vision):
    for bad in ("", "short", "has spaces in it", "x" * 65, "../../etc/passwd"):
        r = await authed.get("/api/camera/frame", params={"viewer": bad})
        assert r.status_code == 422
    assert vision.status()["live_view"]["viewers"] == 0


# ------------------------------------------------------------------ privacy
async def test_camera_off_in_privacy_serves_no_frames(authed, vision):
    assert (await frame(authed)).status_code == 200
    r = await authed.put("/api/privacy", json={"camera_enabled": False})
    assert r.status_code == 200
    assert vision.status()["live_view"]["viewers"] == 0 and vision._view_frame is None  # dropped at once
    r = await frame(authed)
    assert r.status_code == 403
    assert r.json()["detail"] == "Camera is off (Privacy)." and r.json()["code"] == "camera_off"
    assert not r.headers["content-type"].startswith("image/")
    d = (await authed.get("/api/camera")).json()
    assert d["blocked"] == "camera_off" and d["blocked_detail"] == "Camera is off (Privacy)."
    r = await authed.post("/api/camera/snapshot", json={"viewer": VIEWER})
    assert r.status_code == 403 and r.json()["code"] == "camera_off"
    seq = vision._view_seq
    await asyncio.sleep(0.5)
    assert vision._view_seq == seq and vision.status()["camera"] == "disabled"
    assert (await authed.put("/api/privacy", json={"camera_enabled": True})).status_code == 200
    assert await wait_for(lambda: vision.status()["camera"] == "ok")
    assert (await frame(authed, viewer="after-privacy-on-01")).status_code == 200


async def test_privacy_off_while_a_request_waits(authed, vision, dctx):
    r = await frame(authed)
    seq = int(r.headers["x-frame-seq"])
    waiting = asyncio.create_task(frame(authed, after=seq + 1000))  # waits for the next new frame
    await asyncio.sleep(0)
    dctx.privacy.set(camera_enabled=False)
    r = await asyncio.wait_for(waiting, 3.0)
    assert r.status_code == 403 and r.json()["code"] == "camera_off"


# ------------------------------------------------------------------ the viewer goes away
async def test_work_stops_when_the_viewer_goes_away(authed, vision, dctx, monkeypatch):
    monkeypatch.setattr(vs, "LIVE_VIEW_IDLE", 0.4)
    seen = collect(dctx.bus, [Topics.VISION_LIVE_VIEW])
    assert (await frame(authed)).status_code == 200
    assert vision.status()["live_view"]["viewers"] == 1
    # the page closes / the phone locks: no more requests
    assert await wait_for(lambda: vision.status()["live_view"]["viewers"] == 0)
    assert vision._view_frame is None and seen[-1].data == {"active": False, "viewers": 0}
    seq = vision._view_seq
    await asyncio.sleep(0.5)
    assert vision._view_seq == seq  # nothing is encoded any more


async def test_stop_button_stops_at_once(authed, vision, dctx):
    seen = collect(dctx.bus, [Topics.VISION_LIVE_VIEW])
    assert (await frame(authed)).status_code == 200
    r = await authed.post("/api/camera/stop", json={"viewer": VIEWER})
    assert r.json() == {"ok": True, "stopped": True}
    assert vision.status()["live_view"]["viewers"] == 0 and seen[-1].data["active"] is False
    r = await frame(authed)  # a late request from the stopped page does not restart it
    assert r.status_code == 409 and r.json()["code"] == "stopped"
    assert vision.status()["live_view"]["viewers"] == 0
    r = await authed.post("/api/camera/stop", json={"viewer": VIEWER})
    assert r.json()["stopped"] is False


# ------------------------------------------------------------------ paused / no camera / busy
async def test_video_playing_reports_paused_instead_of_hanging(authed, vision, dctx):
    assert (await frame(authed)).status_code == 200
    assert dctx.state.transition(RobotState.PLAYING_MEDIA, "video")
    t0 = time.monotonic()
    r = await frame(authed, after=10_000)
    assert time.monotonic() - t0 < 1.0
    assert r.status_code == 409 and r.json()["code"] == "paused" and "video" in r.json()["detail"]
    assert await wait_for(lambda: vision.status()["detect_mode"] == "paused")
    assert (await authed.get("/api/camera")).json()["blocked"] == "paused"
    dctx.state.release(RobotState.PLAYING_MEDIA)
    assert await wait_for(lambda: vision.status()["detect_mode"] == "active")
    assert (await frame(authed)).status_code == 200  # back by itself


async def test_no_camera(authed, dctx):
    svc = VisionService(dctx, camera_factory=lambda kind: MockCamera(available=False))
    dctx.vision = svc
    await svc.start()
    try:
        assert await wait_for(lambda: svc.status()["camera"] in ("unavailable", "disconnected"))
        r = await frame(authed)
        assert r.status_code == 503 and r.json()["code"] == "no_camera"
        assert (await authed.get("/api/camera")).json()["blocked"] == "no_camera"
    finally:
        await svc.stop()


async def test_robot_without_live_view(authed, dctx):
    r = await frame(authed)  # the dashboard fakes' vision has no live view
    assert r.status_code == 503 and r.json()["code"] == "unavailable"
    dctx.vision = None
    r = await frame(authed)
    assert r.status_code == 503 and r.json()["code"] == "unavailable"
    assert (await authed.get("/api/camera")).json()["available"] is False


# ------------------------------------------------------------------ snapshot
async def test_snapshot_only_while_watching_and_only_to_the_browser(authed, vision, monkeypatch, tmp_path):
    cv2 = get_cv2()
    writes: list[Any] = []
    monkeypatch.setattr(cv2, "imwrite", lambda *a, **kw: writes.append(a) or False)
    r = await authed.post("/api/camera/snapshot", json={"viewer": VIEWER})
    assert r.status_code == 409 and r.json()["code"] == "not_watching"  # the robot's camera sign must be on
    assert (await frame(authed)).status_code == 200
    r = await authed.post("/api/camera/snapshot", json={"viewer": VIEWER})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-disposition"].startswith('attachment; filename="littlebot-')
    assert r.headers["cache-control"] == "no-store"
    assert decode(r.content).shape[:2] == (480, 640)  # the full picture
    assert writes == []


# ------------------------------------------------------------------ log
async def test_start_and_stop_are_logged_without_image_data(authed, vision, caplog):
    caplog.set_level(logging.INFO, logger="littlebot")
    assert (await frame(authed)).status_code == 200
    await authed.post("/api/camera/stop", json={"viewer": VIEWER})
    text = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("camera live view started: the dashboard (127.0.0.1) is watching") for m in text)
    assert any(m.startswith("camera live view stopped: the dashboard (127.0.0.1) (stopped on the dashboard)")
               for m in text)
    assert all("\xff\xd8" not in m and len(m) < 400 for m in text)


async def test_another_device_can_watch_too(app, authed, vision):
    async with make_client(app, ip="192.168.1.50") as other:
        assert (await other.get("/api/camera/frame", params={"viewer": "other-device-0001"})).status_code == 401
    assert (await frame(authed)).status_code == 200
    assert (await frame(authed, viewer="second-tab-viewer-01")).status_code == 200
    assert vision.status()["live_view"]["viewers"] == 2
