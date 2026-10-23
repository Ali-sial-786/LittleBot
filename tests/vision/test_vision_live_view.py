"""VisionService live view (the dashboard's Camera page): frames only while someone watches, never kept,
never in the way of detection, the warm photo or photos, and stopped by privacy, video and departure."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
import pytest

from littlebot.core.clock import ManualClock
from littlebot.core.events import Topics, collect
from littlebot.core.state import RobotState
from littlebot.testing import make_test_context
from littlebot.vision import service as vs
from littlebot.vision.camera import MockCamera
from littlebot.vision.detector import MockDetector
from littlebot.vision.imaging import get_cv2
from littlebot.vision.service import PhotoUnavailable, VisionService

pytestmark = pytest.mark.skipif(get_cv2() is None, reason="OpenCV needed to encode JPEG")

V1 = "viewer-one-0001"
V2 = "viewer-two-0002"


@pytest.fixture
def made():
    contexts = []

    def _make(**overrides: Any):
        c = make_test_context(**overrides)
        contexts.append(c)
        return c

    yield _make
    for c in contexts:
        c.db.close()


async def started(ctx, camera=None, detector=None, clock=None):
    clock = clock or ManualClock(100.0)
    cam = camera or MockCamera(640, 480, face=(0.5, 0.5, 0.3))
    svc = VisionService(ctx, camera_factory=lambda kind: cam, clock=clock.now, detector=detector)
    await svc.start(run_thread=False)
    svc.process_once(clock.now())  # opens the camera, reads the first frame
    return svc, cam, clock


def tick(svc: VisionService, clock: ManualClock, dt: float) -> None:
    clock.advance(dt)
    svc.process_once(clock.now())


async def next_frame(svc: VisionService, clock: ManualClock, viewer: str = V1, after: int = 0,
                     dt: float = 1 / 6, cycles: int = 20) -> tuple[bytes, int]:
    """Ask for a frame like the dashboard does, driving vision cycles until it arrives."""
    task = asyncio.create_task(svc.live_view_frame(viewer, after=after, label="test", timeout=5.0))
    for _ in range(cycles):
        await asyncio.sleep(0)
        if task.done():
            break
        tick(svc, clock, dt)
        await asyncio.sleep(0)
        if task.done():
            break
    return await task


def decode(data: bytes) -> np.ndarray:
    cv2 = get_cv2()
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


# ------------------------------------------------------------------ nothing unless asked
async def test_nothing_is_encoded_while_nobody_watches(made):
    ctx = made()
    seen = collect(ctx.bus, [Topics.VISION_LIVE_VIEW])
    svc, cam, clock = await started(ctx)
    for _ in range(12):
        tick(svc, clock, 1 / 6)
    assert svc._view_frame is None and svc._view_seq == 0
    assert svc.status()["live_view"]["viewers"] == 0 and seen == []
    await svc.stop()


# ------------------------------------------------------------------ frames from the mock camera
async def test_a_viewer_gets_small_jpegs_from_the_one_camera(made):
    ctx = made()
    seen = collect(ctx.bus, [Topics.VISION_LIVE_VIEW])
    svc, cam, clock = await started(ctx)
    jpeg, seq = await next_frame(svc, clock)
    assert jpeg.startswith(b"\xff\xd8") and seq == 1
    assert decode(jpeg).shape[:2] == (360, vs.LIVE_VIEW_WIDTH)  # 640x480 camera -> 480x360
    assert [e.data for e in seen] == [{"active": True, "viewers": 1}]
    jpeg2, seq2 = await next_frame(svc, clock, after=seq)
    assert seq2 > seq and jpeg2 is not jpeg  # the next call waits for a NEWER frame
    assert cam.open_count == 1  # the vision thread's own camera, never opened a second time
    live = svc.status()["live_view"]
    assert live["viewers"] == 1 and live["encode_ms"] is not None and live["frame_kb"] > 0
    await svc.stop()


async def test_a_fresh_frame_is_returned_at_once_and_shared_by_viewers(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    jpeg, seq = await next_frame(svc, clock, V1)
    same, seq_b = await svc.live_view_frame(V2, after=0)  # no cycle needed: the newest frame is fresh
    assert same is jpeg and seq_b == seq
    assert svc._view_seq == 1  # encoded once for both
    clock.advance(vs.LIVE_VIEW_MAX_AGE + 0.1)  # too old for a new request: it waits for the next frame
    with pytest.raises(PhotoUnavailable) as e:
        await svc.live_view_frame(V2, after=0, timeout=0.05)
    assert e.value.reason == "timeout"
    await svc.stop()


async def test_a_dim_room_is_lifted_a_little_and_a_bright_one_left_alone(made):
    from littlebot.vision.camera import FixtureCamera

    for level, brighter in ((25, True), (150, False)):
        ctx = made()
        frame = np.full((480, 640, 3), level, dtype=np.uint8)
        svc, cam, clock = await started(ctx, camera=FixtureCamera([frame]), detector=MockDetector())
        jpeg, _ = await next_frame(svc, clock)
        mean = float(decode(jpeg).mean())
        assert (mean > level + 25) if brighter else abs(mean - level) < 3
        await svc.stop()


async def test_frame_rate_is_capped(made):
    ctx = made(vision={"gestures_enabled": True, "gesture_fps": 15.0})  # vision reads 15 frames a second
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock, dt=1 / 15)
    first = svc._view_seq
    for _ in range(45):  # 3 s, the viewer keeps asking
        tick(svc, clock, 1 / 15)
        await svc.live_view_frame(V1, after=0)
    assert (svc._view_seq - first) <= vs.LIVE_VIEW_MAX_FPS * 3 + 1
    assert svc._view_seq - first >= 5 * 3
    await svc.stop()


async def test_a_sleeping_robot_reads_enough_frames_for_a_smooth_picture(made):
    ctx = made()
    ctx.state.transition(RobotState.SLEEPING, "night")
    svc, cam, clock = await started(ctx)
    assert svc.status()["detect_mode"] == "reduced"
    await next_frame(svc, clock, dt=0.05, cycles=40)
    before = cam.frames_read
    for _ in range(40):  # 2 s
        tick(svc, clock, 0.05)
        await svc.live_view_frame(V1, after=0)
    assert cam.frames_read - before >= vs.LIVE_VIEW_MIN_FPS * 2 - 1  # instead of 1.5 frames a second
    await svc.stop()


# ------------------------------------------------------------------ never in anybody's way
async def _detection_times(made, *, watch: bool, state: RobotState | None = None) -> tuple[list[float], int]:
    ctx = made()
    if state is not None:
        ctx.state.transition(state, "test")
    clock = ManualClock(100.0)
    times: list[float] = []
    det = MockDetector(lambda i: (times.append(clock.now()), [])[1])
    svc, cam, clock = await started(ctx, detector=det, clock=clock)
    end = clock.now() + 10.0
    while clock.now() < end:
        if watch:
            try:
                await svc.live_view_frame(V1, after=0, timeout=0.001)
            except PhotoUnavailable:
                pass
        delay = svc.process_once(clock.now())
        clock.advance(max(0.01, min(delay, 0.05)))
    frames = cam.frames_read
    await svc.stop()
    return times, frames


async def test_a_viewer_never_changes_face_detection(made):
    alone, frames_alone = await _detection_times(made, watch=False)
    watched, frames_watched = await _detection_times(made, watch=True)
    assert len(alone) >= 58
    assert watched == alone  # the very same detections at the very same moments
    assert frames_watched == frames_alone  # and not one extra camera read while awake


async def test_a_viewer_never_changes_detection_while_the_robot_sleeps(made):
    alone, _ = await _detection_times(made, watch=False, state=RobotState.SLEEPING)
    watched, frames = await _detection_times(made, watch=True, state=RobotState.SLEEPING)
    assert abs(len(watched) - len(alone)) <= 1  # still 1.5 detections a second...
    assert frames >= 45  # ...while the picture gets >= 5 frames a second


async def test_detection_runs_before_the_live_view_frame_is_encoded(made, monkeypatch):
    ctx = made()
    order: list[str] = []
    det = MockDetector(lambda i: (order.append("detect"), [])[1])
    svc, cam, clock = await started(ctx, detector=det)
    real = svc._refresh_live_view
    monkeypatch.setattr(svc, "_refresh_live_view", lambda f, now: (order.append("view"), real(f, now))[1])
    await next_frame(svc, clock)
    i = order.index("view")
    assert "detect" in order[:i]  # the cycle that encoded the view frame detected first
    await svc.stop()


async def test_the_warm_photo_keeps_its_own_consent_and_size(made):
    """The live view is a separate slot: it never keeps the AI's warm photo alive, nor changes its size."""
    ctx = made()
    svc, cam, clock = await started(ctx)
    consent = {"ok": False}
    svc.keep_photo_warm("live", True, allowed=lambda: consent["ok"], every=0.1, width=640)
    await next_frame(svc, clock)
    tick(svc, clock, 0.2)
    assert svc.latest_photo() is None and svc._warm_photo is None  # consent off: no warm photo, viewer or not
    consent["ok"] = True
    tick(svc, clock, 0.2)
    warm, _age = svc.latest_photo(max_age=1.0)
    assert decode(warm).shape[1] == 640  # the warm photo keeps its own size
    view, _seq = await next_frame(svc, clock, after=svc._view_seq)
    assert decode(view).shape[1] == vs.LIVE_VIEW_WIDTH and view is not warm
    await svc.stop()


async def test_photos_still_work_while_someone_watches(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    task = asyncio.create_task(svc.capture_photo(max_width=640, quality=80))
    await asyncio.sleep(0)
    tick(svc, clock, 0.01)  # a requested photo reads a frame right away, viewer or not
    photo = await task
    assert decode(photo).shape[:2] == (480, 640)
    await svc.stop()


async def test_no_lease_is_held(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    assert all(v is None for v in ctx.arbiter.snapshot().values())
    await svc.stop()


# ------------------------------------------------------------------ stops when nobody watches
async def test_a_viewer_that_stops_asking_is_forgotten(made, caplog):
    caplog.set_level(logging.INFO, logger="littlebot")
    ctx = made()
    seen = collect(ctx.bus, [Topics.VISION_LIVE_VIEW])
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    tick(svc, clock, vs.LIVE_VIEW_IDLE - 0.5)
    assert svc.status()["live_view"]["viewers"] == 1
    tick(svc, clock, 1.0)  # no request for longer than LIVE_VIEW_IDLE
    assert svc.status()["live_view"]["viewers"] == 0 and svc._view_frame is None
    assert seen[-1].data == {"active": False, "viewers": 0}
    encoded = svc._view_seq
    for _ in range(12):
        tick(svc, clock, 1 / 6)
    assert svc._view_seq == encoded  # no more work
    assert svc.frame_interval == pytest.approx(1 / 6)  # back to vision's own rate
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "camera live view started: test is watching" in text
    assert "camera live view stopped: test (stopped asking for frames)" in text
    await svc.stop()


async def test_stop_drops_the_viewer_at_once_and_for_good(made):
    ctx = made()
    seen = collect(ctx.bus, [Topics.VISION_LIVE_VIEW])
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    waiting = asyncio.create_task(svc.live_view_frame(V1, after=svc._view_seq))
    await asyncio.sleep(0)
    assert svc.stop_live_view(V1) is True
    with pytest.raises(PhotoUnavailable) as e:
        await waiting
    assert e.value.reason == "stopped"
    assert svc._view_frame is None and seen[-1].data == {"active": False, "viewers": 0}
    with pytest.raises(PhotoUnavailable) as e:  # a request still on its way can't bring it back
        await svc.live_view_frame(V1)
    assert e.value.reason == "stopped" and svc.status()["live_view"]["viewers"] == 0
    assert svc.stop_live_view(V1) is False
    await svc.stop()


async def test_too_many_viewers(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    for i in range(vs.LIVE_VIEW_MAX_VIEWERS - 1):
        await svc.live_view_frame(f"other-viewer-{i:04d}")
    with pytest.raises(PhotoUnavailable) as e:
        await svc.live_view_frame("one-too-many-0000")
    assert e.value.reason == "busy"
    await svc.stop()


# ------------------------------------------------------------------ privacy, video, camera
async def test_privacy_off_ends_the_view_at_once(made):
    ctx = made()
    seen = collect(ctx.bus, [Topics.VISION_LIVE_VIEW])
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    waiting = asyncio.create_task(svc.live_view_frame(V1, after=svc._view_seq))
    await asyncio.sleep(0)
    ctx.privacy.set(camera_enabled=False)  # before the vision thread even runs
    with pytest.raises(PhotoUnavailable) as e:
        await waiting
    assert e.value.reason == "camera_off"
    assert svc._view_frame is None and svc.status()["live_view"]["viewers"] == 0
    assert seen[-1].data == {"active": False, "viewers": 0}
    with pytest.raises(PhotoUnavailable) as e:
        await svc.live_view_frame(V2)
    assert e.value.reason == "camera_off"
    tick(svc, clock, 0.2)
    assert cam.release_count == 1 and svc._view_seq == 1
    ctx.privacy.set(camera_enabled=True)
    tick(svc, clock, 0.01)
    jpeg, _ = await next_frame(svc, clock, V2)  # works again once the parent turns it back on
    assert jpeg.startswith(b"\xff\xd8")
    await svc.stop()


async def test_a_video_pauses_the_view_with_a_reason_instead_of_hanging(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    waiting = asyncio.create_task(svc.live_view_frame(V1, after=svc._view_seq, timeout=30.0))
    await asyncio.sleep(0)
    assert ctx.state.transition(RobotState.PLAYING_MEDIA, "video")
    with pytest.raises(PhotoUnavailable) as e:  # the next request is answered right away
        await svc.live_view_frame(V2)
    assert e.value.reason == "paused"
    tick(svc, clock, 0.2)
    with pytest.raises(PhotoUnavailable) as e:  # the waiting one is answered by the next cycle
        await asyncio.wait_for(waiting, 1.0)
    assert e.value.reason == "paused"
    assert svc._view_frame is None and svc.status()["live_view"]["viewers"] == 0
    reads = cam.frames_read
    tick(svc, clock, 1.0)
    assert cam.frames_read == reads  # paused means paused: nothing read for the view either
    ctx.state.release(RobotState.PLAYING_MEDIA)
    jpeg, _ = await next_frame(svc, clock)
    assert jpeg.startswith(b"\xff\xd8")
    await svc.stop()


async def test_a_lost_camera_ends_the_view(made):
    ctx = made(camera={"stall_seconds": 0.5})
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    waiting = asyncio.create_task(svc.live_view_frame(V1, after=svc._view_seq, timeout=30.0))
    await asyncio.sleep(0)
    cam.fail()
    tick(svc, clock, 0.2)
    tick(svc, clock, 0.6)
    with pytest.raises(PhotoUnavailable) as e:
        await asyncio.wait_for(waiting, 1.0)
    assert e.value.reason == "no_camera" and svc._view_frame is None
    with pytest.raises(PhotoUnavailable) as e:
        await svc.live_view_frame(V1)
    assert e.value.reason == "no_camera"
    await svc.stop()


async def test_no_camera_configured(made):
    ctx = made(camera={"backend": "none"})
    svc, cam, clock = await started(ctx)
    with pytest.raises(PhotoUnavailable) as e:
        await svc.live_view_frame(V1)
    assert e.value.reason == "no_camera"
    await svc.stop()


async def test_vision_stop_forgets_everything(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    await next_frame(svc, clock)
    waiting = asyncio.create_task(svc.live_view_frame(V1, after=svc._view_seq, timeout=30.0))
    await asyncio.sleep(0)
    await svc.stop()
    with pytest.raises(PhotoUnavailable):
        await asyncio.wait_for(waiting, 1.0)
    assert svc._viewers == {} and svc._view_frame is None and svc._view_waiters == []


async def test_frames_are_never_written_or_logged(made, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    cv2 = get_cv2()
    writes: list[Any] = []
    monkeypatch.setattr(cv2, "imwrite", lambda *a, **kw: writes.append(a) or False)
    ctx = made()
    events = collect(ctx.bus, ["*"])
    svc, cam, clock = await started(ctx)
    seq = 0
    for _ in range(5):
        _jpeg, seq = await next_frame(svc, clock, after=seq)
    svc.stop_live_view(V1)
    await svc.stop()
    assert writes == []
    for r in caplog.records:
        msg = r.getMessage()
        assert "\xff\xd8" not in msg and "b'" not in msg and len(msg) < 400
    for e in events:
        assert not any(isinstance(v, (bytes, bytearray)) for v in e.data.values())


async def test_with_the_real_vision_thread(made):
    ctx = made()
    svc = VisionService(ctx, camera_factory=lambda kind: MockCamera(640, 480))
    await svc.start()
    try:
        jpeg, seq = await svc.live_view_frame(V1, timeout=3.0)
        jpeg2, seq2 = await svc.live_view_frame(V1, after=seq, timeout=3.0)
        assert jpeg.startswith(b"\xff\xd8") and seq2 > seq
        assert svc.stop_live_view(V1)
    finally:
        await svc.stop()
