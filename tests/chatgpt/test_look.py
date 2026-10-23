"""CameraLook: ChatGPT answers one question from one still photo, only with the parent's consent."""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
from typing import Any

import pytest

from littlebot.brain.providers.base import (
    AuthenticationError, BaseProvider, NetworkError, ProviderError, ProviderReply, ProviderTimeoutError,
    RateLimitError,
)
from littlebot.chatgpt.look import DEFAULT_QUESTION, ERRORS, FALLBACK, CameraLook, get_look
from littlebot.core.events import Topics
from littlebot.testing import make_test_context
from littlebot.vision.service import PhotoUnavailable

JPEG = b"\xff\xd8\xff\xe0 fake photo"


class ImageProvider(BaseProvider):
    name = "chatgpt"
    is_cloud = True
    model = "fake"

    def __init__(self, text: str = "You are holding up three fingers.", *, error: BaseException | None = None,
                 signed_in: bool = True) -> None:
        self.text = text
        self.error = error
        self.signed_in = signed_in
        self.calls: list[dict[str, Any]] = []

    def configured(self) -> bool:
        return self.signed_in

    async def reply(self, messages, *, system, max_tokens, timeout, images=None) -> ProviderReply:
        self.calls.append({"messages": messages, "system": system, "images": list(images or [])})
        if self.error is not None:
            raise self.error
        return ProviderReply(text=self.text, model="fake")


class FakeVision:
    def __init__(self, *, camera: str = "ok", error: BaseException | None = None) -> None:
        self.camera = camera
        self.error = error
        self.photos: list[dict[str, Any]] = []

    async def capture_photo(self, *, max_width: int = 768, quality: int = 80, timeout: float = 3.0) -> bytes:
        self.photos.append({"max_width": max_width, "quality": quality, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return JPEG

    def status(self) -> dict[str, Any]:
        return {"camera": self.camera}


def allow(ctx) -> None:
    ctx.privacy.set(cloud_enabled=True, camera_enabled=True, ai_camera_enabled=True)


@pytest.fixture
def setup(ctx):
    allow(ctx)
    ctx.vision = FakeVision()
    provider = ImageProvider()
    events: list[Any] = []
    ctx.bus.subscribe(Topics.VISION_PHOTO, events.append)
    return ctx, CameraLook(ctx, provider=provider), provider, events


async def test_look_sends_one_photo_and_answers(setup):
    ctx, look, provider, events = setup
    out = await look.look("how many fingers am I holding?")
    assert out["ok"] is True and out["answer"] == "You are holding up three fingers."
    assert out["error"] is None and out["detail"] is None and isinstance(out["latency_ms"], int)
    assert ctx.vision.photos == [{"max_width": 768, "quality": 80, "timeout": 3.0}]
    (call,) = provider.calls
    assert call["images"] == [JPEG]
    assert call["messages"] == [{"role": "user", "content": "how many fingers am I holding?"}]
    system = call["system"]
    assert "camera" in system and "Count things carefully" in system and "fingers" in system
    assert "Never identify people" in system and "40 words" in system and "If you can't tell" in system
    st = look.status()
    assert st["photos_today"] == 1 and st["last_at"] is not None and st["enabled"] is True and st["blocked"] is None
    # visible: the face peeks and the dashboard hears about it (without the question or the image)
    assert ctx.face.called("play_animation") == [(("peek",), {})]
    assert [e.data for e in events] == [{"asked_by": "parent"}]


async def test_event_and_logs_never_contain_the_question_or_image(setup, caplog):
    ctx, look, provider, events = setup
    caplog.set_level(logging.DEBUG)
    await look.look("is my secret banana on the table?", asked_by="live")
    assert [dict(e.data) for e in events] == [{"asked_by": "live"}]
    assert "photo sent to ChatGPT (asked_by=live)" in caplog.text
    assert "banana" not in caplog.text and "fake photo" not in caplog.text
    assert "banana" not in repr([e.data for e in events])


async def test_odd_asked_by_is_normalised(setup):
    ctx, look, provider, events = setup
    await look.look("hi", asked_by="Mum <script>")
    assert events[0].data == {"asked_by": "other"}


async def test_empty_question_asks_what_it_sees_and_pii_is_scrubbed(setup):
    ctx, look, provider, events = setup
    await look.look("   ")
    assert provider.calls[0]["messages"][0]["content"] == DEFAULT_QUESTION
    await look.look("what is on the note, call 555-123-4567")
    sent = provider.calls[1]["messages"][0]["content"]
    assert "555-123-4567" not in sent
    await look.look("x" * 1000)
    assert len(provider.calls[2]["messages"][0]["content"]) <= 300


async def test_unsafe_answer_is_replaced_by_a_safe_fallback(setup):
    ctx, look, provider, events = setup
    provider.text = "I see a gun. Bang bang, kill!"
    out = await look.look("what is that?")
    assert out == {**out, "ok": True, "answer": FALLBACK, "error": None}
    provider.text = ""
    assert (await look.look("what is that?"))["answer"] == FALLBACK
    provider.text = " ".join(["word"] * 80)
    assert len((await look.look("what is that?"))["answer"].split()) <= 40


def make(**overrides: Any):
    c = make_test_context(**overrides)
    c.vision = FakeVision()
    return c


@pytest.mark.parametrize("code", ["cloud_off", "camera_off", "ai_camera_off", "not_signed_in", "no_camera",
                                  "no_camera_device"])
async def test_each_gate_blocks_before_any_photo(code):
    ctx = make()
    try:
        allow(ctx)
        provider = ImageProvider()
        if code == "cloud_off":
            ctx.privacy.set(cloud_enabled=False)
        elif code == "camera_off":
            ctx.privacy.set(camera_enabled=False)
        elif code == "ai_camera_off":
            ctx.privacy.set(ai_camera_enabled=False)
        elif code == "not_signed_in":
            provider.signed_in = False
        elif code == "no_camera":
            ctx.vision = None
        else:
            ctx.vision.camera, code = "unavailable", "no_camera"
        events: list[Any] = []
        ctx.bus.subscribe(Topics.VISION_PHOTO, events.append)
        look = CameraLook(ctx, provider=provider)
        assert look.blocked_reason() == code
        out = await look.look("how many fingers?")
        assert out["ok"] is False and out["error"] == code and out["detail"] == ERRORS[code] and out["answer"] is None
        assert provider.calls == [] and events == [] and ctx.face.called("play_animation") == []
        if ctx.vision is not None:
            assert ctx.vision.photos == []
        st = look.status()
        assert st["blocked"] == code and st["blocked_detail"] == ERRORS[code] and st["photos_today"] == 0
    finally:
        ctx.db.close()


async def test_offline_mode_blocks_and_the_switch_cannot_be_turned_on():
    ctx = make(mode="offline")
    try:
        assert ctx.privacy.ai_camera_enabled is False
        with pytest.raises(PermissionError):
            ctx.privacy.set(ai_camera_enabled=True)
        look = CameraLook(ctx, provider=ImageProvider())
        assert look.blocked_reason() == "offline"
        assert (await look.look("hi"))["error"] == "offline"
    finally:
        ctx.db.close()


async def test_switch_is_off_by_default_and_in_the_privacy_snapshot(ctx):
    assert ctx.privacy.ai_camera_enabled is False
    assert ctx.settings.get("privacy.ai_camera_enabled") is False
    assert ctx.privacy.snapshot()["ai_camera_enabled"] is False
    changes: list[Any] = []
    ctx.bus.subscribe(Topics.PRIVACY_CHANGED, changes.append)
    assert ctx.privacy.set(ai_camera_enabled=True)["ai_camera_enabled"] is True
    assert changes and changes[-1].data["ai_camera_enabled"] is True
    ctx.privacy.set(cloud_enabled=True)
    ctx.vision = FakeVision()
    look = CameraLook(ctx, provider=ImageProvider())
    assert look.status() == {"enabled": True, "blocked": None, "blocked_detail": None, "photos_today": 0,
                             "last_at": None}


@pytest.mark.parametrize("error, code", [
    (ProviderTimeoutError("slow"), "timeout"),
    (asyncio.TimeoutError(), "timeout"),
    (NetworkError("down"), "network"),
    (RateLimitError("busy"), "rate_limited"),
    (RateLimitError("limit", kind="usage_limit"), "usage_limit"),
    (AuthenticationError("gone", kind="expired"), "expired"),
    (AuthenticationError("no", kind="not_signed_in"), "not_signed_in"),
    (AuthenticationError("no"), "auth"),
    (ProviderError("refused", kind="refused"), "refused"),
    (ProviderError("plan", kind="plan_not_included"), "plan_not_included"),
    (ProviderError("boom", kind="server_error"), "provider_error"),
])
async def test_provider_errors_map_to_friendly_codes(setup, error, code):
    ctx, look, provider, events = setup
    provider.error = error
    out = await look.look("how many fingers?")
    assert out["ok"] is False and out["error"] == code and out["detail"] == ERRORS[code] and out["answer"] is None
    assert "boom" not in out["detail"]
    assert look.status()["photos_today"] == 1  # the photo was taken (and visibly so) before the failure


@pytest.mark.parametrize("reason, code", [("camera_off", "camera_off"), ("no_camera", "no_camera"),
                                          ("timeout", "photo_failed"), ("unavailable", "photo_failed")])
async def test_photo_failures(setup, reason, code):
    ctx, look, provider, events = setup
    ctx.vision.error = PhotoUnavailable(reason)
    out = await look.look("what do you see?")
    assert out["ok"] is False and out["error"] == code and out["detail"] == ERRORS[code]
    assert provider.calls == [] and events == [] and look.status()["photos_today"] == 0


async def test_one_look_at_a_time(setup):
    ctx, look, provider, events = setup
    gate = asyncio.Event()
    real = provider.reply

    async def slow(*a, **kw):
        await gate.wait()
        return await real(*a, **kw)

    provider.reply = slow  # type: ignore[method-assign]
    first = asyncio.create_task(look.look("one"))
    await asyncio.sleep(0.01)
    assert (await look.look("two"))["error"] == "busy"
    gate.set()
    assert (await first)["ok"] is True
    assert len(ctx.vision.photos) == 1


async def test_get_look_is_shared(ctx):
    a = get_look(ctx)
    assert get_look(ctx) is a and ctx.extras["camera_look"] is a


async def test_real_camera_photo_is_sent_and_nothing_is_written_to_disk(ctx, monkeypatch):
    """End to end with the real VisionService + mock camera: JPEG reaches the provider, no file is written."""
    from littlebot.vision.camera import MockCamera
    from littlebot.vision.imaging import get_cv2
    from littlebot.vision.service import VisionService

    if get_cv2() is None:
        pytest.skip("OpenCV needed")
    allow(ctx)
    vision = VisionService(ctx, camera_factory=lambda kind: MockCamera(640, 480, face=(0.5, 0.5, 0.3)))
    ctx.vision = vision
    await vision.start()
    writes: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file, mode="r", *a, **kw):
        if any(m in mode for m in "wax+"):
            writes.append(str(file))
        return real_open(file, mode, *a, **kw)

    def spy_os_open(path, flags, *a, **kw):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writes.append(str(path))
        return real_os_open(path, flags, *a, **kw)

    cv2 = get_cv2()
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(cv2, "imwrite", lambda *a, **kw: writes.append("cv2.imwrite") or False)
    try:
        await asyncio.sleep(0.05)
        provider = ImageProvider("Two fingers!")
        out = await CameraLook(ctx, provider=provider).look("how many fingers?")
    finally:
        monkeypatch.undo()
        await vision.stop()
    assert out["ok"] is True and out["answer"] == "Two fingers!"
    (image,) = provider.calls[0]["images"]
    assert image.startswith(b"\xff\xd8") and len(image) > 1000
    assert writes == []
