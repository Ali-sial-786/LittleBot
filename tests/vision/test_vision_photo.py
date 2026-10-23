"""VisionService.capture_photo: one JPEG of the next frame, on request only, never kept."""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from littlebot.core.clock import ManualClock
from littlebot.core.interfaces import VisionAPI
from littlebot.core.state import RobotState
from littlebot.testing import make_test_context
from littlebot.vision.camera import FixtureCamera, MockCamera
from littlebot.vision.imaging import get_cv2
from littlebot.vision.service import PhotoUnavailable, VisionService

pytestmark = pytest.mark.skipif(get_cv2() is None, reason="OpenCV needed to encode JPEG")


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


def build(ctx, camera=None, clock=None):
    clock = clock or ManualClock(100.0)
    cam = camera if camera is not None else MockCamera(320, 240, face=(0.5, 0.5, 0.3))
    return VisionService(ctx, camera_factory=lambda kind: cam, clock=clock.now), cam, clock


def decode(data: bytes) -> np.ndarray:
    cv2 = get_cv2()
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


async def photo_with_cycle(svc: VisionService, clock: ManualClock, **kw: Any) -> bytes:
    task = asyncio.create_task(svc.capture_photo(**kw))
    await asyncio.sleep(0)  # the request is queued
    svc.process_once(clock.now())
    return await task


async def test_capture_photo_returns_jpeg_of_the_next_frame(make_ctx):
    ctx = make_ctx()
    svc, cam, clock = build(ctx)
    await svc.start(run_thread=False)
    data = await photo_with_cycle(svc, clock)
    assert isinstance(data, bytes) and data.startswith(b"\xff\xd8")
    img = decode(data)
    assert img.shape[:2] == (240, 320)  # small camera: never upscaled
    assert svc._photo_requests == []  # nothing pending or kept
    assert isinstance(svc, VisionAPI)
    await svc.stop()


async def test_photo_is_downscaled_and_forces_one_extra_read(make_ctx):
    ctx = make_ctx(camera={"detect_fps": 1.0})
    svc, cam, clock = build(ctx, camera=MockCamera(1280, 720))
    await svc.start(run_thread=False)
    svc.process_once(clock.now())  # opens the camera, reads the first frame
    clock.advance(0.1)  # the next regular frame is not due for ~0.9 s
    svc.process_once(clock.now())
    before = cam.frames_read
    data = await photo_with_cycle(svc, clock, max_width=320, quality=60)
    assert cam.frames_read == before + 1  # read right away for the photo
    assert decode(data).shape[:2] == (180, 320)
    await svc.stop()


async def test_photo_from_a_fixture_camera(make_ctx):
    ctx = make_ctx()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :320] = (0, 0, 255)
    svc, cam, clock = build(ctx, camera=FixtureCamera([frame], loop=True))
    await svc.start(run_thread=False)
    data = await photo_with_cycle(svc, clock, max_width=768)
    img = decode(data)
    assert img.shape[:2] == (480, 640)
    assert img[240, 100, 2] > 200 and img[240, 500, 2] < 50  # it really is that frame
    await svc.stop()


async def test_photo_with_the_real_capture_thread(make_ctx):
    ctx = make_ctx()
    svc = VisionService(ctx, camera_factory=lambda kind: MockCamera(640, 480))
    await svc.start()
    try:
        data = await svc.capture_photo(max_width=320, timeout=3.0)
        assert data.startswith(b"\xff\xd8") and decode(data).shape[1] == 320
    finally:
        await svc.stop()


async def test_refuses_when_camera_privacy_is_off(make_ctx):
    ctx = make_ctx()
    svc, cam, clock = build(ctx)
    ctx.privacy.set(camera_enabled=False)
    await svc.start(run_thread=False)
    with pytest.raises(PhotoUnavailable) as e:
        await svc.capture_photo(timeout=0.5)
    assert e.value.reason == "camera_off"
    assert cam.frames_read == 0
    await svc.stop()


async def test_privacy_off_while_waiting_fails_the_request(make_ctx):
    ctx = make_ctx()
    svc, cam, clock = build(ctx)
    await svc.start(run_thread=False)
    svc.process_once(clock.now())
    task = asyncio.create_task(svc.capture_photo(timeout=2.0))
    await asyncio.sleep(0)
    ctx.privacy.set(camera_enabled=False)
    svc.process_once(clock.now())
    with pytest.raises(PhotoUnavailable) as e:
        await task
    assert e.value.reason == "camera_off"
    await svc.stop()


async def test_no_camera(make_ctx):
    ctx = make_ctx(camera={"backend": "none"})
    svc = VisionService(ctx)
    await svc.start(run_thread=False)
    with pytest.raises(PhotoUnavailable) as e:
        await svc.capture_photo(timeout=0.5)
    assert e.value.reason == "no_camera"
    await svc.stop()


async def test_unavailable_camera_is_no_camera(make_ctx):
    ctx = make_ctx()
    svc, cam, clock = build(ctx, camera=MockCamera(available=False))
    await svc.start(run_thread=False)
    svc.process_once(clock.now())
    with pytest.raises(PhotoUnavailable) as e:
        await svc.capture_photo(timeout=0.5)
    assert e.value.reason == "no_camera"
    await svc.stop()


async def test_times_out_cleanly_when_no_frame_arrives(make_ctx):
    ctx = make_ctx()
    svc, cam, clock = build(ctx)
    await svc.start(run_thread=False)  # nobody runs a cycle: no frame ever comes
    with pytest.raises(PhotoUnavailable) as e:
        await svc.capture_photo(timeout=0.2)
    assert e.value.reason == "timeout"
    assert svc._photo_requests == []
    svc.process_once(clock.now())  # a late frame does not fail or resurrect anything
    assert svc._photo_requests == []
    await svc.stop()


class SilentCamera(MockCamera):
    def read(self, timeout: float = 1.0):
        return None


async def test_threaded_timeout_with_a_silent_camera(make_ctx):
    ctx = make_ctx(camera={"stall_seconds": 30.0})
    svc = VisionService(ctx, camera_factory=lambda kind: SilentCamera())
    await svc.start()
    try:
        with pytest.raises(PhotoUnavailable) as e:
            await svc.capture_photo(timeout=0.3)
        assert e.value.reason == "timeout"
    finally:
        await svc.stop()


async def test_photo_while_media_pauses_detection(make_ctx):
    ctx = make_ctx()
    svc, cam, clock = build(ctx)
    await svc.start(run_thread=False)
    svc.process_once(clock.now())
    assert ctx.state.transition(RobotState.PLAYING_MEDIA, "video")
    svc.process_once(clock.now())
    assert svc.status()["detect_mode"] == "paused"
    before = cam.frames_read
    data = await photo_with_cycle(svc, clock)
    assert data.startswith(b"\xff\xd8") and cam.frames_read == before + 1
    await svc.stop()


def test_dark_photos_are_brightened_and_bright_ones_left_alone():
    import numpy as np

    from littlebot.vision.imaging import brighten_if_dark

    dark = np.full((48, 64, 3), 25, np.uint8)
    dark[10:20, 10:20] = 60  # a dim hand
    out = brighten_if_dark(dark)
    assert out.mean() > 80 and out[15, 15, 0] > out[30, 40, 0]  # brighter, contrast kept
    bright = np.full((48, 64, 3), 140, np.uint8)
    assert brighten_if_dark(bright) is bright
    black = np.zeros((48, 64, 3), np.uint8)
    assert brighten_if_dark(black) is black  # nothing to lift (lens covered)


def test_shrink_jpeg_for_a_model_that_looks_at_less():
    from littlebot.vision.imaging import shrink_jpeg

    cv2 = get_cv2()
    if cv2 is None:
        pytest.skip("OpenCV needed")
    img = (np.random.default_rng(0).random((480, 640, 3)) * 255).astype(np.uint8)
    big = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])[1].tobytes()
    small = shrink_jpeg(big, 512, 75)
    out = cv2.imdecode(np.frombuffer(small, np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[:2] == (384, 512) and len(small) < len(big)
    assert shrink_jpeg(small, 512) is small  # already small enough: untouched
    assert shrink_jpeg(b"not a jpeg", 512) == b"not a jpeg"
