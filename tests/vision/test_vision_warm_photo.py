"""VisionService.keep_photo_warm / latest_photo: one recent JPEG in memory during a live call, only while allowed."""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from littlebot.core.clock import ManualClock
from littlebot.core.state import RobotState
from littlebot.testing import make_test_context
from littlebot.vision.camera import MockCamera
from littlebot.vision.imaging import get_cv2
from littlebot.vision.service import VisionService

pytestmark = pytest.mark.skipif(get_cv2() is None, reason="OpenCV needed to encode JPEG")


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


async def started(ctx, camera=None):
    clock = ManualClock(100.0)
    cam = camera or MockCamera(640, 480, face=(0.5, 0.5, 0.3))
    svc = VisionService(ctx, camera_factory=lambda kind: cam, clock=clock.now)
    await svc.start(run_thread=False)
    svc.process_once(clock.now())  # opens the camera
    return svc, cam, clock


def tick(svc: VisionService, clock: ManualClock, dt: float) -> None:
    clock.advance(dt)
    svc.process_once(clock.now())


def decode(data: bytes) -> np.ndarray:
    cv2 = get_cv2()
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


async def test_nothing_is_kept_unless_asked(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    for _ in range(5):
        tick(svc, clock, 0.2)
    assert svc.latest_photo() is None and svc._warm_photo is None and svc.status()["photo_warm"] is False
    await svc.stop()


async def test_keeps_one_recent_photo_and_refreshes_it(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    svc.keep_photo_warm("live", True, every=0.5, width=320, quality=70)
    assert svc.status()["photo_warm"] is False  # the snapshot refreshes on the next cycle
    tick(svc, clock, 0.2)
    assert svc.status()["photo_warm"] is True
    jpeg, age = svc.latest_photo(max_age=1.0)
    assert jpeg.startswith(b"\xff\xd8") and age == pytest.approx(0.0)
    assert decode(jpeg).shape[:2] == (240, 320)
    tick(svc, clock, 0.2)  # not due yet: the same photo, getting older
    same, age = svc.latest_photo(max_age=1.0)
    assert same is jpeg and age == pytest.approx(0.2)
    assert svc.latest_photo(max_age=0.1) is None  # too old for this caller
    tick(svc, clock, 0.35)  # due: replaced by a newer frame
    newer, age = svc.latest_photo(max_age=1.0)
    assert newer is not jpeg and age == pytest.approx(0.0)
    await svc.stop()


async def test_stopping_drops_it_at_once(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    svc.keep_photo_warm("live", True)
    svc.keep_photo_warm("dashboard", True)
    tick(svc, clock, 0.2)
    svc.keep_photo_warm("live", False)
    assert svc.latest_photo() is not None  # another owner still wants it
    svc.keep_photo_warm("dashboard", False)
    assert svc.latest_photo() is None and svc._warm_photo is None
    tick(svc, clock, 1.0)
    assert svc._warm_photo is None  # and no new one is made
    await svc.stop()


async def test_consent_check_runs_before_every_refresh(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    allowed = {"ok": True}
    svc.keep_photo_warm("live", True, allowed=lambda: allowed["ok"], every=0.3)
    tick(svc, clock, 0.2)
    assert svc.latest_photo() is not None
    allowed["ok"] = False
    tick(svc, clock, 0.35)
    assert svc.latest_photo() is None and svc._warm_photo is None  # dropped, not just hidden
    allowed["ok"] = True
    tick(svc, clock, 0.35)
    assert svc.latest_photo() is not None
    await svc.stop()


async def test_a_crashing_consent_check_means_no(made):
    ctx = made()
    svc, cam, clock = await started(ctx)

    def broken() -> bool:
        raise RuntimeError("boom")

    svc.keep_photo_warm("live", True, allowed=broken)
    tick(svc, clock, 0.2)
    assert svc.latest_photo() is None
    await svc.stop()


async def test_camera_privacy_off_drops_it(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    svc.keep_photo_warm("live", True)
    tick(svc, clock, 0.2)
    assert svc.latest_photo() is not None
    ctx.privacy.set(camera_enabled=False)
    assert svc.latest_photo() is None  # at once, before the vision thread even runs
    tick(svc, clock, 0.2)
    assert svc._warm_photo is None and cam.release_count == 1
    await svc.stop()


async def test_video_playback_drops_it(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    svc.keep_photo_warm("live", True)
    tick(svc, clock, 0.2)
    assert ctx.state.transition(RobotState.PLAYING_MEDIA, "video")
    tick(svc, clock, 0.2)
    assert svc.status()["detect_mode"] == "paused" and svc.latest_photo() is None and svc._warm_photo is None
    await svc.stop()


async def test_a_lost_camera_drops_it(made):
    ctx = made(camera={"stall_seconds": 0.5})
    svc, cam, clock = await started(ctx)
    svc.keep_photo_warm("live", True)
    tick(svc, clock, 0.2)
    assert svc.latest_photo() is not None
    cam.fail()
    tick(svc, clock, 0.2)
    tick(svc, clock, 0.6)
    assert svc._camera is None and svc._warm_photo is None and svc.latest_photo() is None
    await svc.stop()


async def test_vision_stop_forgets_everything(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    svc.keep_photo_warm("live", True)
    tick(svc, clock, 0.2)
    await svc.stop()
    assert svc._warm_photo is None and svc._warm_owners == {} and svc.latest_photo() is None


async def test_nothing_is_written_to_disk(made, monkeypatch):
    cv2 = get_cv2()
    writes: list[Any] = []
    monkeypatch.setattr(cv2, "imwrite", lambda *a, **kw: writes.append(a) or False)
    ctx = made()
    svc, cam, clock = await started(ctx)
    svc.keep_photo_warm("live", True)
    for _ in range(6):
        tick(svc, clock, 0.2)
    assert writes == [] and svc.latest_photo() is not None
    await svc.stop()


async def test_with_the_real_vision_thread(made):
    ctx = made()
    svc = VisionService(ctx, camera_factory=lambda kind: MockCamera(640, 480))
    await svc.start()
    try:
        svc.keep_photo_warm("live", True, every=0.1)
        for _ in range(100):
            if svc.latest_photo(max_age=1.0):
                break
            await asyncio.sleep(0.02)
        jpeg, age = svc.latest_photo(max_age=1.0)
        assert jpeg.startswith(b"\xff\xd8") and 0.0 <= age <= 1.0
    finally:
        await svc.stop()


async def test_withdrawn_consent_hides_it_even_before_the_next_refresh(made):
    ctx = made()
    svc, cam, clock = await started(ctx)
    allowed = {"ok": True}
    svc.keep_photo_warm("live", True, allowed=lambda: allowed["ok"], every=5.0)
    tick(svc, clock, 0.2)
    assert svc.latest_photo() is not None
    allowed["ok"] = False
    assert svc.latest_photo() is None
    await svc.stop()
