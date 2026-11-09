"""VisionService.add_frame_sink (a video being recorded, littlebot/capture): every frame at the sink's rate, on the
vision thread, only while the camera is allowed; a broken sink never breaks vision."""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.core.clock import ManualClock
from littlebot.testing import make_test_context
from littlebot.vision.camera import MockCamera
from littlebot.vision.service import PhotoUnavailable, VisionService


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


async def _started(ctx: Any) -> tuple[VisionService, MockCamera, ManualClock]:
    clock = ManualClock(100.0)
    cam = MockCamera(320, 240)
    svc = VisionService(ctx, camera_factory=lambda kind: cam, clock=clock.now)
    await svc.start(run_thread=False)
    svc.process_once(clock.now())  # opens the camera
    return svc, cam, clock


def _run(svc: VisionService, clock: ManualClock, seconds: float, step: float = 0.005) -> None:
    end = clock.now() + seconds - 1e-9
    while clock.now() < end:
        svc.process_once(clock.now())
        clock.advance(step)


async def test_a_sink_gets_full_frames_at_its_rate(made) -> None:
    ctx = made(camera={"detect_fps": 5.0})
    svc, cam, clock = await _started(ctx)
    got: list[tuple[Any, float]] = []
    svc.add_frame_sink("capture", lambda frame, now: got.append((frame.shape, now)), fps=30)
    assert svc.frame_interval == pytest.approx(1 / 30)
    _run(svc, clock, 1.0)
    assert 28 <= len(got) <= 32
    assert got[0][0][:2] == (240, 320)  # the camera's own size, not detection's
    assert svc.status()["recording"] == ["capture"]
    assert svc.remove_frame_sink("capture") is True and svc.remove_frame_sink("capture") is False
    n = len(got)
    _run(svc, clock, 0.5)
    assert len(got) == n and svc.status()["recording"] == []
    assert svc.frame_interval == pytest.approx(1 / 5)
    await svc.stop()


async def test_not_while_the_camera_is_off(made) -> None:
    ctx = made()
    svc, cam, clock = await _started(ctx)
    ctx.privacy.set(camera_enabled=False)
    with pytest.raises(PhotoUnavailable) as e:
        svc.add_frame_sink("capture", lambda frame, now: None)
    assert e.value.reason == "camera_off"
    await svc.stop()


async def test_no_camera_in_the_config(made) -> None:
    ctx = made(camera={"backend": "none"})
    svc = VisionService(ctx, camera_factory=lambda kind: MockCamera(320, 240))
    with pytest.raises(PhotoUnavailable) as e:
        svc.add_frame_sink("capture", lambda frame, now: None)
    assert e.value.reason == "no_camera"


async def test_a_broken_sink_is_dropped_and_vision_carries_on(made) -> None:
    ctx = made()
    svc, cam, clock = await _started(ctx)
    good: list[float] = []

    def broken(frame: Any, now: float) -> None:
        raise RuntimeError("boom")

    svc.add_frame_sink("broken", broken, fps=30)
    svc.add_frame_sink("good", lambda frame, now: good.append(now), fps=30)
    _run(svc, clock, 2.0)
    assert "broken" not in svc._sinks and "good" in svc._sinks  # noqa: SLF001
    assert len(good) > 50 and svc.status()["camera"] not in ("disconnected", "unavailable")
    await svc.stop()
