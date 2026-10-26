"""VisionService: pipeline -> events, throttling, simulation, disconnects, privacy, lifecycle.

Most tests drive ``process_once(now)`` with a manual clock (deterministic, no
threads); the lifecycle tests run the real capture thread with short timings.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from littlebot.core.clock import ManualClock
from littlebot.core.events import collect, Topics
from littlebot.core.interfaces import VisionAPI
from littlebot.core.service import ServiceStatus
from littlebot.testing import make_test_context
from littlebot.vision import create_service
from littlebot.vision.camera import CameraError, MockCamera
from littlebot.vision.detector import Detection, MockDetector
from littlebot.vision.gestures import GestureDetector, GestureEvent
from littlebot.vision.service import VisionService

FAST = {"present_after_seconds": 0.5, "left_after_seconds": 1.0, "simulated_face_seconds": 0.5}


@pytest.fixture
def make_ctx():
    made = []

    def _make(**overrides: Any):
        c = make_test_context(**overrides)
        made.append(c)
        return c

    yield _make
    for c in made:
        c.db.close()


def build(ctx, *, camera=None, detector=None, factory=None, gestures=None, clock=None):
    clock = clock or ManualClock(100.0)
    cam = camera if camera is not None else MockCamera(320, 240)
    svc = VisionService(ctx, camera_factory=factory or (lambda kind: cam), detector=detector,
                        gestures=gestures, clock=clock.now)
    return svc, cam, clock


def run_for(svc: VisionService, clock: ManualClock, seconds: float, step: float = 0.01) -> None:
    end = clock.now() + seconds - 1e-9
    while clock.now() < end:
        svc.process_once(clock.now())
        clock.advance(step)


def topics(events, topic):
    return [e for e in events if e.topic == topic]


# ------------------------------------------------------------ throttling
async def test_detection_is_throttled_to_detect_fps(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 5.0})
    det = MockDetector()
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    run_for(svc, clock, 2.0)
    assert det.calls in (10, 11)
    assert cam.frames_read == det.calls  # latest-frame-only: no reading frames we do not process
    assert svc.status()["detect_fps"] == pytest.approx(5.0, abs=0.3)
    # the loop asks to sleep until the next frame is due instead of spinning
    delay = svc.process_once(clock.now())
    assert 0.0 <= delay <= 0.2
    await svc.stop()


async def test_gestures_raise_frame_rate_but_not_detection_rate(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 5.0}, vision={"gesture_fps": 10.0})
    det = MockDetector()
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    ctx.settings.set("vision.gestures_enabled", True)
    run_for(svc, clock, 2.0)
    assert det.calls in (10, 11)
    assert cam.frames_read in (20, 21)
    assert svc.status()["gestures_enabled"] is True
    await svc.stop()


# --------------------------------------------------- pipeline -> events
async def test_mock_detector_pipeline_events_and_gaze(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 10.0}, vision=FAST)
    events = collect(ctx.bus, ["vision.*"])
    det = MockDetector()
    det.set_faces([Detection.centred(0.8, 0.5, 0.2)])  # child on the image's right
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    t0 = clock.now()
    run_for(svc, clock, 1.0)
    faces = topics(events, Topics.VISION_FACES)
    assert 8 <= len(faces) <= 11
    f = faces[-1].data
    assert (f["frame_w"], f["frame_h"]) == (320, 240) and f["simulated"] is False
    face = f["faces"][0]
    assert face["primary"] is True
    assert (face["x"], face["w"]) == (224, 64)
    assert face["gaze_x"] == pytest.approx(0.2)  # mirrored: image right -> gaze toward screen-left
    assert face["gaze_y"] < 0.5  # aimed at the eye line, slightly above the box centre
    present = topics(events, Topics.VISION_PERSON_PRESENT)
    assert len(present) == 1 and present[0].data["gaze_x"] == pytest.approx(0.2)
    assert present[0].ts  # published once, after the hysteresis delay
    st = svc.status()
    assert st["person_present"] and st["faces"] == 1 and st["gaze"]["x"] == pytest.approx(0.2)
    assert st["detector"] == "mock" and st["camera"] == "ok"

    det.set_faces([])
    run_for(svc, clock, 0.9)
    assert not topics(events, Topics.VISION_PERSON_LEFT)  # hysteresis: not yet
    run_for(svc, clock, 0.3)
    assert len(topics(events, Topics.VISION_PERSON_LEFT)) == 1
    empties = [e for e in topics(events, Topics.VISION_FACES) if not e.data["faces"]]
    assert len(empties) == 1  # "no faces" is published once, on the change
    assert svc.status()["person_present"] is False and svc.status()["gaze"] is None
    assert clock.now() - t0 < 2.5
    await svc.stop()


async def test_no_faces_means_no_face_events(make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.VISION_FACES, Topics.VISION_PERSON_PRESENT])
    svc, cam, clock = build(ctx, detector=MockDetector())
    await svc.start(run_thread=False)
    run_for(svc, clock, 2.0)
    assert events == []
    await svc.stop()


async def test_face_events_are_rate_capped(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 50.0})
    events = collect(ctx.bus, [Topics.VISION_FACES])
    det = MockDetector()
    det.set_faces([Detection.centred(0.5, 0.5, 0.2)])
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    run_for(svc, clock, 1.0, step=0.005)
    assert det.calls > 40
    assert len(events) <= 16
    await svc.stop()


async def test_mock_camera_ground_truth_detector_is_used_automatically(make_ctx):
    ctx = make_ctx(camera={"mock_face": True}, vision=FAST)
    events = collect(ctx.bus, [Topics.VISION_FACES])
    svc = create_service(ctx)  # default factory: config backend "mock"
    await svc.start(run_thread=False)
    t = time.monotonic()
    for i in range(10):
        svc.process_once(t + i * 0.2)
    assert svc.status()["detector"] == "mock" and svc.status()["backend"] == "mock"
    assert events and events[-1].data["faces"]
    await svc.stop()


# ------------------------------------------------------------ simulation
async def test_simulate_face_drives_events_then_expires(make_ctx):
    ctx = make_ctx(vision={**FAST, "present_after_seconds": 0.2, "left_after_seconds": 0.3})
    events = collect(ctx.bus, ["vision.*"])
    svc, cam, clock = build(ctx, detector=MockDetector())
    await svc.start(run_thread=False)
    svc.process_once(clock.now())
    svc.simulate_face(0.25, 0.4, size=0.3)
    assert svc.status()["simulated"] is False  # snapshot refreshes on the next cycle
    run_for(svc, clock, 0.4)  # presence is evaluated at tracker ticks (1/detect_fps)
    st = svc.status()
    assert st["simulated"] is True and st["person_present"] is True
    faces = topics(events, Topics.VISION_FACES)
    assert faces and faces[0].data["simulated"] is True
    assert faces[0].data["faces"][0]["gaze_x"] == pytest.approx(0.75)
    assert len(topics(events, Topics.VISION_PERSON_PRESENT)) == 1
    run_for(svc, clock, 0.15)  # expires at 0.5 s
    assert svc.status()["simulated"] is False
    assert not topics(events, Topics.VISION_PERSON_LEFT)
    run_for(svc, clock, 0.5)  # + left_after (+ one tick)
    assert len(topics(events, Topics.VISION_PERSON_LEFT)) == 1
    await svc.stop()


async def test_refreshed_simulated_face_never_leaves(make_ctx):
    ctx = make_ctx(vision={**FAST, "left_after_seconds": 0.3})
    events = collect(ctx.bus, [Topics.VISION_PERSON_LEFT, Topics.VISION_PERSON_PRESENT])
    svc, cam, clock = build(ctx, detector=MockDetector())
    await svc.start(run_thread=False)
    for _ in range(10):
        svc.simulate_face(0.5, 0.5)
        run_for(svc, clock, 0.3)
    assert [e.topic for e in events] == [Topics.VISION_PERSON_PRESENT]
    svc.simulate_no_face()
    run_for(svc, clock, 0.5)
    assert [e.topic for e in events] == [Topics.VISION_PERSON_PRESENT, Topics.VISION_PERSON_LEFT]
    await svc.stop()


async def test_simulated_face_overrides_camera_then_camera_resumes(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 10.0}, vision={**FAST, "gaze_smoothing": 0.0})
    events = collect(ctx.bus, [Topics.VISION_FACES])
    det = MockDetector()
    det.set_faces([Detection.centred(0.8, 0.5, 0.2)])
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    run_for(svc, clock, 0.3)
    assert events[-1].data["faces"][0]["gaze_x"] == pytest.approx(0.2)
    svc.simulate_face(0.1, 0.5)
    run_for(svc, clock, 0.3)
    last = events[-1].data
    assert last["simulated"] is True and len(last["faces"]) == 1
    assert last["faces"][0]["gaze_x"] == pytest.approx(0.9)
    run_for(svc, clock, 0.5)  # simulated face expired
    last = events[-1].data
    assert last["simulated"] is False and last["faces"][0]["gaze_x"] == pytest.approx(0.2)
    await svc.stop()


async def test_simulation_works_without_any_camera(make_ctx):
    ctx = make_ctx(camera={"backend": "none"}, vision={**FAST, "simulated_face_seconds": 2.0})
    events = collect(ctx.bus, ["vision.*"])
    calls = []
    svc, _, clock = build(ctx, factory=lambda kind: calls.append(kind))
    await svc.start(run_thread=False)
    svc.simulate_face(0.5, 0.5)
    run_for(svc, clock, 0.75)
    assert calls == []
    st = svc.status()
    assert st["camera"] == "disabled" and st["backend"] == "none" and st["person_present"]
    assert topics(events, Topics.VISION_CAMERA_STATUS)[0].data["status"] == "disabled"
    assert svc.health()["status"] == ServiceStatus.DISABLED.value
    await svc.stop()


# -------------------------------------------------- disconnect / reconnect
async def test_camera_disconnect_then_reconnect(make_ctx):
    ctx = make_ctx(camera={"reconnect_seconds": 1.0, "detect_fps": 10.0})
    events = collect(ctx.bus, [Topics.VISION_CAMERA_STATUS])
    svc, cam, clock = build(ctx, detector=MockDetector())
    await svc.start(run_thread=False)
    run_for(svc, clock, 0.5)
    assert [e.data["status"] for e in events] == ["ok"]
    cam.fail()  # cable pulled
    run_for(svc, clock, 0.5)
    assert [e.data["status"] for e in events] == ["ok", "disconnected"]
    assert svc.status()["camera"] == "disconnected" and svc.status()["fps"] == 0.0
    assert cam.release_count == 1 and not cam.is_open
    run_for(svc, clock, 0.5)
    assert cam.open_count == 1  # waits reconnect_seconds before retrying
    run_for(svc, clock, 0.5)
    assert cam.open_count == 2  # retried, still failing -> released again
    cam.recover()  # plugged back in
    run_for(svc, clock, 1.5)
    assert [e.data["status"] for e in events][-1] == "ok"
    assert svc.status()["camera"] == "ok" and cam.is_open
    await svc.stop()
    assert not cam.is_open


class FlakyCamera(MockCamera):
    """Read timeouts without a failure signal (slow auto-exposure in a dim room, or a silent hang)."""

    def __init__(self, every: int = 2) -> None:
        super().__init__()
        self.every, self.n = every, 0

    def read(self, timeout: float = 1.0):
        self.n += 1
        return None if self.every == 0 or self.n % self.every else super().read(timeout)


async def test_slow_camera_is_not_a_disconnect(make_ctx):
    ctx = make_ctx(camera={"stall_seconds": 1.0})
    events = collect(ctx.bus, [Topics.VISION_CAMERA_STATUS])
    svc, cam, clock = build(ctx, camera=FlakyCamera(every=3), detector=MockDetector())
    await svc.start(run_thread=False)
    run_for(svc, clock, 5.0)
    assert [e.data["status"] for e in events] == ["ok"] and cam.release_count == 0
    await svc.stop()


async def test_silent_camera_is_a_disconnect_after_stall_seconds(make_ctx):
    ctx = make_ctx(camera={"stall_seconds": 1.0, "reconnect_seconds": 10.0})
    events = collect(ctx.bus, [Topics.VISION_CAMERA_STATUS])
    svc, cam, clock = build(ctx, camera=FlakyCamera(every=0), detector=MockDetector())
    await svc.start(run_thread=False)
    run_for(svc, clock, 0.9)
    assert [e.data["status"] for e in events] == ["ok"]
    run_for(svc, clock, 0.4)
    assert [e.data["status"] for e in events] == ["ok", "disconnected"] and cam.release_count == 1
    await svc.stop()


async def test_unavailable_camera_retries_and_never_raises(make_ctx):
    ctx = make_ctx(camera={"backend": "real", "reconnect_seconds": 0.5})
    attempts = []
    real_cam = MockCamera()

    def factory(kind):
        attempts.append(kind)
        if len(attempts) < 4:
            raise CameraError("no camera at /dev/video0")
        return real_cam

    events = collect(ctx.bus, [Topics.VISION_CAMERA_STATUS])
    svc, _, clock = build(ctx, factory=factory, detector=MockDetector())
    await svc.start(run_thread=False)
    run_for(svc, clock, 1.2)
    assert attempts == ["real"] * 3
    assert svc.status()["camera"] == "unavailable" and "retrying" in svc.status()["detail"]
    assert svc.health()["status"] == ServiceStatus.DEGRADED.value
    run_for(svc, clock, 0.5)
    assert [e.data["status"] for e in events] == ["unavailable", "ok"]
    await svc.stop()


async def test_auto_backend_without_camera_falls_back_to_mock_in_simulation(make_ctx):
    ctx = make_ctx(camera={"backend": "auto", "device": "/dev/video-littlebot-missing"})
    svc = create_service(ctx)
    await svc.start(run_thread=False)
    svc.process_once()
    st = svc.status()
    assert st["camera"] == "ok" and st["backend"] == "mock" and st["detector"] == "mock"
    await svc.stop()


async def test_auto_backend_on_the_pi_reports_unavailable(make_ctx):
    ctx = make_ctx(mode="pi", camera={"backend": "auto", "device": "/dev/video-littlebot-missing"})
    svc = create_service(ctx)
    await svc.start(run_thread=False)
    svc.process_once()
    st = svc.status()
    assert st["camera"] == "unavailable" and st["backend"] == "opencv"
    assert json.dumps(st)
    await svc.stop()


async def test_detector_errors_are_contained(make_ctx):
    ctx = make_ctx()

    def boom(i):
        raise RuntimeError("detector exploded")

    det = MockDetector(boom)
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    run_for(svc, clock, 1.0)
    assert det.calls >= 5 and svc.status()["camera"] == "ok"
    await svc.stop()


# ------------------------------------------------------------- privacy
async def test_privacy_toggle_releases_and_reopens_camera(make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.VISION_CAMERA_STATUS])
    det = MockDetector()
    det.set_faces([Detection.centred(0.5, 0.5, 0.2)])
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    run_for(svc, clock, 0.5)
    assert cam.is_open
    calls = det.calls
    ctx.privacy.set(camera_enabled=False)
    svc.process_once(clock.now())
    assert not cam.is_open and cam.release_count == 1
    assert events[-1].data["status"] == "disabled" and "privacy" in events[-1].data["detail"]
    run_for(svc, clock, 1.0)
    assert det.calls == calls and cam.open_count == 1  # no processing at all while disabled
    assert svc.status()["camera"] == "disabled" and svc.status()["fps"] == 0.0
    ctx.privacy.set(camera_enabled=True)
    svc.process_once(clock.now())
    assert cam.is_open and cam.open_count == 2 and events[-1].data["status"] == "ok"
    await svc.stop()


async def test_camera_disabled_at_start(make_ctx):
    ctx = make_ctx(privacy={"camera_enabled": False})
    svc, cam, clock = build(ctx, detector=MockDetector())
    await svc.start(run_thread=False)
    run_for(svc, clock, 0.5)
    assert cam.open_count == 0 and svc.status()["camera"] == "disabled"
    await svc.stop()


# ------------------------------------------------------------- gestures
class ScriptedGestures(GestureDetector):
    def __init__(self) -> None:
        super().__init__()
        self.updates = 0
        self.faces: list[Detection | None] = []

    def update(self, frame, now, face=None):
        self.updates += 1
        self.faces.append(face)
        return [GestureEvent("wave", 0.8)] if self.updates == 3 else []


async def test_gestures_only_when_enabled_in_settings(make_ctx):
    ctx = make_ctx()
    events = collect(ctx.bus, [Topics.VISION_GESTURE])
    g = ScriptedGestures()
    det = MockDetector()
    det.set_faces([Detection.centred(0.5, 0.5, 0.2)])
    svc, cam, clock = build(ctx, detector=det, gestures=g)
    await svc.start(run_thread=False)
    run_for(svc, clock, 0.5)
    assert g.updates == 0 and svc.status()["gestures_enabled"] is False
    ctx.settings.set("vision.gestures_enabled", True)
    run_for(svc, clock, 1.0)
    assert g.updates >= 3 and g.faces[-1] is not None
    assert [e.data for e in events] == [{"gesture": "wave", "confidence": 0.8}]
    ctx.settings.set("vision.gestures_enabled", False)
    n = g.updates
    run_for(svc, clock, 0.5)
    assert g.updates == n
    await svc.stop()


# --------------------------------------------------------------- status
async def test_status_contract_and_protocol(make_ctx):
    ctx = make_ctx()
    svc = create_service(ctx)
    assert isinstance(svc, VisionAPI)
    st = svc.status()  # before start: still well-formed
    expected = {"camera", "backend", "detector", "device", "resolution", "fps", "detect_fps", "faces",
                "person_present", "gaze", "gestures_enabled", "simulated"}
    assert expected <= set(st)
    await svc.start(run_thread=False)
    svc.simulate_face(0.5, 0.5)
    svc.process_once()
    st = svc.status()
    assert expected <= set(st) and json.loads(json.dumps(st)) == st
    assert st["resolution"] == "320x240" and isinstance(st["gaze"], dict)
    st["gaze"]["x"] = 99  # callers get a copy
    assert svc.status()["gaze"]["x"] != 99
    await svc.stop()


# ------------------------------------------------- threaded lifecycle
async def wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


async def test_thread_lifecycle_start_stop(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 20.0})
    cam = MockCamera()
    svc = VisionService(ctx, camera_factory=lambda kind: cam, detector=MockDetector())
    await svc.stop()  # stop before start is fine
    await svc.start()
    await svc.start()  # idempotent
    assert svc.thread_alive
    assert await wait_until(lambda: cam.frames_read >= 3)
    thread = svc._thread
    t0 = time.monotonic()
    await svc.stop()
    assert time.monotonic() - t0 < 2.0
    assert not thread.is_alive() and not svc.thread_alive
    assert not cam.is_open and cam.release_count == 1
    assert svc.health()["status"] == "stopped"
    await svc.stop()  # idempotent
    await svc.start()  # restartable
    assert await wait_until(lambda: cam.is_open)
    await svc.stop()
    assert not cam.is_open


async def test_threaded_privacy_off_releases_camera_immediately(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 2.0})  # slow cycle: release must not wait for it
    statuses: list[str] = []
    ctx.bus.subscribe(Topics.VISION_CAMERA_STATUS, lambda e: statuses.append(e.data["status"]))
    cam = MockCamera()
    svc = VisionService(ctx, camera_factory=lambda kind: cam, detector=MockDetector())
    await svc.start()
    try:
        assert await wait_until(lambda: cam.is_open and cam.frames_read >= 1)
        await asyncio.sleep(0.05)  # thread is now sleeping until the next (slow) cycle
        t0 = time.monotonic()
        ctx.privacy.set(camera_enabled=False)
        assert await wait_until(lambda: not cam.is_open, timeout=1.0)
        assert time.monotonic() - t0 < 0.3
        assert statuses[-1] == "disabled" and svc.status()["camera"] == "disabled"
        ctx.privacy.set(camera_enabled=True)
        assert await wait_until(lambda: cam.is_open and statuses[-1] == "ok", timeout=1.0)
    finally:
        await svc.stop()


async def test_threaded_disconnect_and_reconnect(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 20.0, "reconnect_seconds": 0.2})
    statuses: list[str] = []
    ctx.bus.subscribe(Topics.VISION_CAMERA_STATUS, lambda e: statuses.append(e.data["status"]))
    cam = MockCamera()
    svc = VisionService(ctx, camera_factory=lambda kind: cam, detector=MockDetector())
    await svc.start()
    try:
        assert await wait_until(lambda: statuses == ["ok"])
        cam.fail()
        assert await wait_until(lambda: "disconnected" in statuses)
        cam.recover()
        assert await wait_until(lambda: statuses[-1] == "ok" and len(statuses) == 3)
    finally:
        await svc.stop()


async def test_threaded_simulated_face_present_then_left(make_ctx):
    ctx = make_ctx(camera={"backend": "none"},
                   vision={"present_after_seconds": 0.05, "left_after_seconds": 0.1, "simulated_face_seconds": 0.2})
    svc = create_service(ctx)
    await svc.start()
    try:
        waiter = asyncio.ensure_future(ctx.bus.wait_for(Topics.VISION_PERSON_PRESENT, timeout=2.0))
        await asyncio.sleep(0)
        svc.simulate_face(0.3, 0.5)
        ev = await waiter
        assert ev is not None and ev.data["gaze_x"] == pytest.approx(0.7)
        left = await ctx.bus.wait_for(Topics.VISION_PERSON_LEFT, timeout=2.0)
        assert left is not None
    finally:
        await svc.stop()


async def test_eyes_follow_simulated_face_via_behaviours(make_ctx):
    from littlebot.app.behaviours import Behaviours

    ctx = make_ctx(camera={"backend": "none"}, vision={"present_after_seconds": 0.05})
    behaviours = Behaviours(ctx)
    await behaviours.start()
    svc = create_service(ctx)
    await svc.start()
    try:
        svc.simulate_face(0.2, 0.5)

        def gaze_calls():  # behaviours prefers the soft track_gaze() when the face offers it
            return ctx.face.called("track_gaze") or ctx.face.called("look_at")

        assert await wait_until(lambda: bool(gaze_calls()), timeout=2.0)
        (x, y), _ = gaze_calls()[-1]
        assert x == pytest.approx(0.8) and 0.0 < y < 0.5  # robot looks to its right, at eye level
        assert ctx.face.gaze == (x, y)
    finally:
        await svc.stop()
        await behaviours.stop()


# ----------------------------------------------- robot-state work levels
async def test_media_pauses_detection_but_keeps_camera_and_presence(make_ctx):
    from littlebot.core.state import RobotState

    ctx = make_ctx(camera={"detect_fps": 10.0}, vision={**FAST, "left_after_seconds": 1.0})
    events = collect(ctx.bus, ["vision.*"])
    det = MockDetector()
    det.set_faces([Detection.centred(0.5, 0.5, 0.2)])
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    run_for(svc, clock, 1.0)
    assert svc.status()["person_present"] and svc.status()["detect_mode"] == "active"
    calls, reads, n_events = det.calls, cam.frames_read, len(events)

    assert ctx.state.transition(RobotState.PLAYING_MEDIA, "video")
    run_for(svc, clock, 5.0)  # much longer than left_after_seconds
    st = svc.status()
    assert st["detect_mode"] == "paused" and st["robot_state"] == "PLAYING_MEDIA"
    assert det.calls == calls and cam.frames_read == reads  # nothing read, decoded or detected
    assert cam.is_open and cam.release_count == 0  # camera stays open: resuming is instant
    assert len(events) == n_events  # no faces / left events while the video plays
    assert st["person_present"]  # presence frozen, not "left"
    assert svc.process_once(clock.now()) == pytest.approx(1.0)  # idle sleep while paused

    assert ctx.state.transition(RobotState.IDLE, "video ended")
    svc.process_once(clock.now())
    assert det.calls == calls + 1  # detection resumes immediately
    run_for(svc, clock, 1.0)
    assert svc.status()["detect_mode"] == "active"
    assert not topics(events, Topics.VISION_PERSON_LEFT)  # still there: no fake leave/arrive pair
    assert len(topics(events, Topics.VISION_PERSON_PRESENT)) == 1
    await svc.stop()


async def test_media_pause_can_be_disabled(make_ctx):
    from littlebot.core.state import RobotState

    ctx = make_ctx(camera={"detect_fps": 5.0}, vision={"pause_during_media": False})
    det = MockDetector()
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    ctx.state.transition(RobotState.PLAYING_MEDIA, "video")
    run_for(svc, clock, 2.0)
    assert svc.status()["detect_mode"] == "active" and det.calls in (10, 11)
    await svc.stop()


async def test_sleeping_reduces_detection_and_an_arriving_person_wakes_the_robot(make_ctx):
    from littlebot.app.behaviours import Behaviours
    from littlebot.core.state import RobotState

    ctx = make_ctx(camera={"detect_fps": 6.0}, vision={"sleep_detect_fps": 1.5, "present_after_seconds": 0.6})
    behaviours = Behaviours(ctx)
    await behaviours.start()
    g = ScriptedGestures()
    det = MockDetector()
    svc, cam, clock = build(ctx, detector=det, gestures=g)
    await svc.start(run_thread=False)
    ctx.settings.set("vision.gestures_enabled", True)
    try:
        assert ctx.state.transition(RobotState.SLEEPING, "idle timeout")
        calls = det.calls
        run_for(svc, clock, 4.0)
        st = svc.status()
        assert st["detect_mode"] == "reduced" and st["robot_state"] == "SLEEPING"
        assert det.calls - calls in (6, 7)  # 1.5 Hz instead of 6 Hz
        assert g.updates == 0 and cam.frames_read == det.calls  # no gestures, no extra frames
        assert st["detect_fps"] == pytest.approx(1.5, abs=0.2)

        det.set_faces([Detection.centred(0.5, 0.5, 0.2)])  # someone comes to the sleeping robot
        run_for(svc, clock, 2.0)
        assert ctx.state.state == RobotState.IDLE  # behaviours woke it on vision.person_present
        assert svc.status()["detect_mode"] == "active"
    finally:
        await svc.stop()
        await behaviours.stop()


async def test_low_detection_rates_still_report_presence(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 1.0}, vision={"present_after_seconds": 0.6})
    events = collect(ctx.bus, [Topics.VISION_PERSON_PRESENT])
    det = MockDetector()
    det.set_faces([Detection.centred(0.5, 0.5, 0.2)])
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    run_for(svc, clock, 2.5)
    assert len(events) == 1  # 1 s between detections is not mistaken for "a new person each time"
    await svc.stop()


async def test_threaded_state_change_pauses_capture(make_ctx):
    from littlebot.core.state import RobotState

    ctx = make_ctx(camera={"detect_fps": 20.0})
    cam = MockCamera()
    svc = VisionService(ctx, camera_factory=lambda kind: cam, detector=MockDetector())
    await svc.start()
    try:
        assert await wait_until(lambda: cam.frames_read >= 3)
        ctx.state.transition(RobotState.PLAYING_MEDIA, "video")
        assert await wait_until(lambda: svc.status()["detect_mode"] == "paused", timeout=1.0)
        n = cam.frames_read
        await asyncio.sleep(0.3)
        assert cam.frames_read == n and cam.is_open
        ctx.state.transition(RobotState.IDLE, "done")
        assert await wait_until(lambda: cam.frames_read > n + 2, timeout=1.0)
    finally:
        await svc.stop()


async def test_the_browser_open_reduces_detection_but_photos_still_work(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 6.0}, vision={"sleep_detect_fps": 1.5})
    det = MockDetector()
    svc, cam, clock = build(ctx, detector=det)
    await svc.start(run_thread=False)
    try:
        ctx.bus.publish("browser.opened", {"source": "tool", "host": "www.google.com", "search": True})
        calls = det.calls
        run_for(svc, clock, 4.0)
        assert svc.status()["detect_mode"] == "reduced" and det.calls - calls in (6, 7)  # 1.5 Hz, not 6
        assert svc._mode_for(svc._robot_state) != "paused"  # a camera look during the chat still gets a photo
        ctx.bus.publish("browser.closed", {"reason": "nobody used it"})
        run_for(svc, clock, 1.0)
        assert svc.status()["detect_mode"] == "active"
    finally:
        await svc.stop()
