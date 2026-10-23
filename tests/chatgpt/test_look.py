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
    assert ctx.vision.photos == [{"max_width": 640, "quality": 75, "timeout": 3.0}]
    (call,) = provider.calls
    assert call["images"] == [JPEG]
    assert call["messages"] == [{"role": "user", "content": "Look at this photo from your camera. how many fingers am I holding?"}]
    assert "toy robot" in call["system"] and "count" in call["system"] and "baby" not in call["system"].lower()
    system = call["system"]
    assert "camera" in system and "count carefully" in system and "fingers" in system
    assert "Never say who a person is" in system and "40 words" in system and "too dark or blurry" in system
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
    assert provider.calls[0]["messages"][0]["content"] == f"Look at this photo from your camera. {DEFAULT_QUESTION}"
    await look.look("what is on the note, call 555-123-4567")
    sent = provider.calls[1]["messages"][0]["content"]
    assert "555-123-4567" not in sent
    await look.look("x" * 1000)
    assert len(provider.calls[2]["messages"][0]["content"].removeprefix("Look at this photo from your camera. ")) <= 300


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


async def test_endpoint_error_is_logged_with_status_and_code_only(setup, caplog):
    ctx, look, provider, events = setup
    err = ProviderError("chatgpt: request not accepted secret-message", status_code=400, kind="bad_request")
    err.api_error = {"status": 400, "type": "invalid_request_error", "code": "missing_required_parameter",
                     "param": "input[0].content[0].detail"}
    provider.error = err
    caplog.set_level(logging.DEBUG)
    out = await look.look("is my secret banana on the table?")
    assert out["error"] == "provider_error"
    assert ("camera look: ChatGPT failed (provider_error) [status=400 kind=bad_request type=invalid_request_error "
            "code=missing_required_parameter param=input[0].content[0].detail]") in caplog.text
    assert "banana" not in caplog.text and "fake photo" not in caplog.text and "secret-message" not in caplog.text
    caplog.clear()
    provider.error = ProviderTimeoutError("slow")
    await look.look("hi")
    assert "camera look: ChatGPT failed (timeout) [kind=" in caplog.text


# --------------------------------------------------------------------------- speed-ups
import json  # noqa: E402

import httpx  # noqa: E402
from chatgpt_helpers import signed_in_auth  # noqa: E402

import time  # noqa: E402

import numpy as np  # noqa: E402

from littlebot.chatgpt import look as look_mod  # noqa: E402
from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: E402
from littlebot.vision.hands import HandCount  # noqa: E402
from littlebot.vision.imaging import get_cv2  # noqa: E402

WARM = b"\xff\xd8\xff\xe0 warm photo"


class FastProvider(ImageProvider):
    """Takes the per-call options (as ChatGPTProvider does) and can stream its answer in pieces."""

    def __init__(self, *pieces: str, **kw: Any) -> None:
        super().__init__("".join(pieces) or "You are holding up three fingers.", **kw)
        self.pieces = list(pieces) or [self.text]
        self.options: list[dict[str, Any]] = []
        self.first_seen_before_return: list[Any] = []

    async def reply(self, messages, *, system, max_tokens, timeout, images=None, tools=None, run_tool=None,
                    max_tool_rounds=6, detail=None, effort=None, on_delta=None) -> ProviderReply:
        self.options.append({"detail": detail, "effort": effort, "streams": on_delta is not None})
        for piece in self.pieces:
            if on_delta is not None:
                on_delta(piece)
            await asyncio.sleep(0)
        return await super().reply(messages, system=system, max_tokens=max_tokens, timeout=timeout, images=images)


class WarmVision(FakeVision):
    def __init__(self, *, age: float = 0.1, **kw: Any) -> None:
        super().__init__(**kw)
        self.age = age
        self.warm: dict[str, Any] = {}
        self.asked_ages: list[float] = []

    def keep_photo_warm(self, owner, on=True, *, allowed=None, every=0.5, width=640, quality=75) -> None:
        if on:
            self.warm[owner] = {"allowed": allowed, "every": every, "width": width, "quality": quality}
        else:
            self.warm.pop(owner, None)

    def latest_photo(self, max_age: float = 1.0):
        self.asked_ages.append(max_age)
        if not self.warm or self.age > max_age:
            return None
        return WARM, self.age


class FakeCounter:
    def __init__(self, result: HandCount) -> None:
        self.result = result
        self.photos: list[bytes] = []

    def count_jpeg(self, jpeg: bytes) -> HandCount:
        self.photos.append(jpeg)
        return self.result


@pytest.fixture
def fast(ctx):
    allow(ctx)
    ctx.vision = WarmVision()
    provider = FastProvider()
    look = CameraLook(ctx, provider=provider)
    events: list[Any] = []
    ctx.bus.subscribe(Topics.VISION_PHOTO, events.append)
    yield ctx, look, provider, events
    look.keep_warm(False)


async def test_a_warm_photo_is_used_without_waiting_for_the_camera(fast):
    ctx, look, provider, events = fast
    look.keep_warm(True)
    spec = ctx.vision.warm["live"]
    assert spec["every"] == look_mod.WARM_EVERY_S and spec["width"] == 640 and spec["quality"] == 75
    assert spec["allowed"]() is True  # the consent switches are re-checked by the camera before each refresh
    out = await look.look("what colour is my teddy?")
    assert out["ok"] and ctx.vision.photos == []  # no new photo needed
    assert provider.calls[0]["images"] == [WARM]
    assert ctx.vision.asked_ages == [look_mod.WARM_MAX_AGE_S]
    assert [e.data for e in events] == [{"asked_by": "parent"}]  # still visibly a photo
    assert look.status()["photos_today"] == 1


async def test_hands_need_a_very_fresh_warm_photo(fast):
    ctx, look, provider, events = fast
    look.keep_warm(True)
    ctx.vision.age = 0.6  # fine for "what colour", too old for a hand that is still moving into place
    await look.look("what am I holding?")
    assert ctx.vision.asked_ages == [look_mod.WARM_MAX_AGE_HANDS_S]
    assert len(ctx.vision.photos) == 1 and provider.calls[0]["images"] == [JPEG]  # took a new one


async def test_no_warm_photo_unless_kept_warm_or_allowed(fast):
    ctx, look, provider, events = fast
    await look.look("what colour is it?")  # not kept warm: a new photo
    assert len(ctx.vision.photos) == 1 and ctx.vision.asked_ages == []
    look.keep_warm(True)
    ctx.privacy.set(ai_camera_enabled=False)
    assert ctx.vision.warm["live"]["allowed"]() is False  # the camera drops it on its next refresh
    look.keep_warm(False)
    assert ctx.vision.warm == {}


async def test_keep_warm_opens_the_chatgpt_connection_and_stops_with_the_call(fast, monkeypatch):
    ctx, look, provider, events = fast
    warmed: list[int] = []

    async def warm() -> bool:
        warmed.append(1)
        return True

    provider.warm = warm  # type: ignore[attr-defined]
    look.keep_warm(True)
    await asyncio.sleep(0.05)
    assert warmed == [1]
    task = look._keep_alive
    look.keep_warm(False)
    await asyncio.sleep(0)
    assert task is not None and task.cancelled() and look._keep_alive is None


async def test_fingers_are_counted_on_the_robot_when_it_is_sure(fast, caplog):
    ctx, look, provider, events = fast
    counter = look._counter = FakeCounter(HandCount(3, 1, True, per_hand=(3,)))
    firsts: list[str] = []
    caplog.set_level(logging.INFO)
    out = await look.look("How many fingers am I holding up?", on_first=firsts.append)
    assert out["ok"] and out["answer"] == "I see three fingers held up!" and out["source"] == "robot"
    assert firsts == [out["answer"]] and provider.calls == []  # instant, and the photo never left the robot
    assert counter.photos == [JPEG] and [e.data for e in events] == [{"asked_by": "parent"}]
    assert "no photo sent" in caplog.text and "photo sent to ChatGPT" not in caplog.text
    look._counter = FakeCounter(HandCount(1, 1, True, per_hand=(1,)))
    assert (await look.look("how many fingers?"))["answer"] == "I see one finger held up!"


@pytest.mark.parametrize("count", [HandCount(0, 0, False, "no_hand"), HandCount(0, 1, False, "fist"),
                                   HandCount(4, 1, False, "unclear_finger")])
async def test_unsure_finger_counts_go_to_chatgpt_with_high_detail(fast, count):
    ctx, look, provider, events = fast
    look._counter = FakeCounter(count)
    out = await look.look("how many fingers am I holding up?")
    assert out["source"] == "chatgpt" and out["answer"] == "You are holding up three fingers."
    assert provider.options == [{"detail": "high", "effort": look_mod.COUNT_EFFORT, "streams": True}]


async def test_other_questions_never_use_the_counter(fast):
    ctx, look, provider, events = fast
    counter = look._counter = FakeCounter(HandCount(5, 1, True))
    await look.look("how many toes and fingers does a cat have?")
    await look.look("what colour is my hat?")
    assert counter.photos == [] and len(provider.calls) == 2


async def test_simple_questions_get_a_lighter_request(fast):
    ctx, look, provider, events = fast
    await look.look("what colour is my hat?")
    await look.look("can you read this word?")
    assert provider.options == [{"detail": "low", "effort": look_mod.LOOK_EFFORT, "streams": True},
                                {"detail": "high", "effort": look_mod.COUNT_EFFORT, "streams": True}]


async def test_a_plain_provider_gets_no_new_options(setup):
    ctx, look, provider, events = setup  # ImageProvider.reply has no detail/effort/on_delta
    firsts: list[str] = []
    out = await look.look("what colour is my hat?", on_first=firsts.append)
    assert out["ok"] and firsts == [] and out["first"] is None


async def test_the_first_sentence_is_passed_on_while_the_rest_streams(ctx):
    allow(ctx)
    ctx.vision = FakeVision()
    provider = FastProvider("A red ball! ", "It is on the green rug", " next to a teddy.")
    look = CameraLook(ctx, provider=provider)
    seen: list[tuple[str, int]] = []
    out = await look.look("what is this?", on_first=lambda s: seen.append((s, len(provider.calls))))
    assert seen == [("A red ball!", 0)]  # before the reply was complete
    assert out["answer"] == "A red ball! It is on the green rug next to a teddy."
    assert out["first"] == "A red ball!" and out["rest"] == "It is on the green rug next to a teddy."


async def test_a_one_sentence_answer_is_passed_on_whole(ctx):
    allow(ctx)
    ctx.vision = FakeVision()
    look = CameraLook(ctx, provider=FastProvider("You are holding up ", "two fingers."))
    seen: list[str] = []
    out = await look.look("how many fingers?", on_first=seen.append)
    assert seen == ["You are holding up two fingers."] and out["rest"] is None


async def test_a_streamed_sentence_that_breaks_the_rules_is_never_passed_on(ctx):
    allow(ctx)
    ctx.vision = FakeVision()
    look = CameraLook(ctx, provider=FastProvider("I see a gun, bang bang kill! ", "And a teddy."))
    seen: list[str] = []
    out = await look.look("what is that?", on_first=seen.append)
    assert seen == [] and out["answer"] == FALLBACK and out["first"] is None


async def test_decimals_do_not_end_a_sentence_early(ctx):
    allow(ctx)
    ctx.vision = FakeVision()
    look = CameraLook(ctx, provider=FastProvider("It is about 1.", "5 metres away. ", "Wow!"))
    seen: list[str] = []
    await look.look("how far is the ball?", on_first=seen.append)
    assert seen == ["It is about 1.5 metres away."]


async def test_timing_is_logged_without_content(fast, caplog):
    ctx, look, provider, events = fast
    caplog.set_level(logging.INFO)
    await look.look("is my secret banana red?")
    line = next(r.getMessage() for r in caplog.records if "camera look timing" in r.getMessage())
    assert "photo=new" in line and "photo_ms=" in line and "chatgpt_ms=" in line and "first_token_ms=" in line
    assert "detail=low" in line and "banana" not in caplog.text and "fake photo" not in caplog.text


# --------------------------------------------------------------------------- the provider's per-call options
def sse(*events: dict) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


ANSWER = sse({"type": "response.output_text.delta", "delta": "Three "},
             {"type": "response.output_text.delta", "delta": "fingers!"},
             {"type": "response.completed", "response": {"usage": {"input_tokens": 9, "output_tokens": 3}}})


def chatgpt(tmp_path, handler) -> ChatGPTProvider:
    return ChatGPTProvider(signed_in_auth(tmp_path), models=["gpt-6-luna"], effort="low",
                           transport=httpx.MockTransport(handler))


async def test_provider_sends_detail_and_effort_and_streams_deltas(tmp_path):
    bodies: list[dict[str, Any]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(r.content))
        return httpx.Response(200, text=ANSWER)

    p = chatgpt(tmp_path, handler)
    deltas: list[str] = []
    reply = await p.reply([{"role": "user", "content": "how many?"}], system="x", max_tokens=10, timeout=5,
                          images=[JPEG], detail="low", effort="none", on_delta=deltas.append)
    assert reply.text == "Three fingers!" and deltas == ["Three ", "fingers!"]
    assert bodies[0]["reasoning"] == {"effort": "none"}
    assert bodies[0]["input"][0]["content"][0]["detail"] == "low"
    await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, images=[JPEG])
    assert bodies[1]["reasoning"] == {"effort": "low"} and bodies[1]["input"][0]["content"][0]["detail"] == "high"
    await p.aclose()


async def test_provider_falls_back_when_an_effort_is_refused_and_remembers(tmp_path):
    efforts: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        effort = json.loads(r.content)["reasoning"]["effort"]
        efforts.append(effort)
        if effort in ("none", "minimal"):
            return httpx.Response(400, json={"error": {
                "message": f"Unsupported value: '{effort}' is not supported with this model.",
                "type": "invalid_request_error", "param": "reasoning.effort", "code": "unsupported_value"}})
        return httpx.Response(200, text=ANSWER)

    p = chatgpt(tmp_path, handler)
    reply = await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, effort="none")
    assert reply.text == "Three fingers!" and efforts == ["none", "minimal", "low"]
    await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, effort="none")
    assert efforts[3:] == ["low"]  # remembered: no more refused requests
    assert p.model == "gpt-6-luna"  # an effort refusal is not a missing model
    await p.aclose()


async def test_provider_effort_refusal_in_codex_detail_shape(tmp_path):
    efforts: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        effort = json.loads(r.content)["reasoning"]["effort"]
        efforts.append(effort)
        if effort == "minimal":
            return httpx.Response(400, json={"detail": "Reasoning effort 'minimal' is not supported for this model."})
        return httpx.Response(200, text=ANSWER)

    p = chatgpt(tmp_path, handler)
    await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, effort="minimal")
    assert efforts == ["minimal", "low"]
    await p.aclose()


async def test_provider_gives_up_when_even_the_configured_effort_is_refused(tmp_path):
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "param": "reasoning.effort"}})

    p = chatgpt(tmp_path, handler)
    with pytest.raises(ProviderError) as e:
        await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, effort="none")
    assert e.value.kind == "bad_request" and e.value.api_error["param"] == "reasoning.effort"
    await p.aclose()


async def test_provider_warm_opens_a_kept_alive_connection_without_signing_in(tmp_path):
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(405)

    p = chatgpt(tmp_path, handler)
    assert await p.warm() is True
    (req,) = seen
    assert req.method == "HEAD" and "authorization" not in req.headers and not req.content
    assert p._get_client()._transport is not None  # one shared client: the connection is reused

    def broken(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    assert await chatgpt(tmp_path, broken).warm() is False
    await p.aclose()


async def test_a_listener_bug_does_not_break_the_answer(tmp_path):
    p = chatgpt(tmp_path, lambda r: httpx.Response(200, text=ANSWER))

    def boom(_text: str) -> None:
        raise RuntimeError("listener bug")

    reply = await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, on_delta=boom)
    assert reply.text == "Three fingers!"
    await p.aclose()


async def test_provider_goes_back_to_the_configured_effort_after_an_unexplained_400(tmp_path):
    efforts: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        effort = json.loads(r.content)["reasoning"]["effort"]
        efforts.append(effort)
        if effort != "low":
            return httpx.Response(400, json={"detail": "Unsupported value."})  # doesn't say what
        return httpx.Response(200, text=ANSWER)

    p = chatgpt(tmp_path, handler)
    reply = await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, effort="none")
    assert reply.text == "Three fingers!" and efforts == ["none", "low"]
    await p.reply([{"role": "user", "content": "hi"}], system="x", max_tokens=10, timeout=5, effort="minimal")
    assert efforts[2:] == ["low"]
    await p.aclose()


async def test_a_one_word_exclamation_waits_for_the_real_answer(ctx):
    allow(ctx)
    ctx.vision = FakeVision()
    look = CameraLook(ctx, provider=FastProvider("Oh! ", "Three fingers! ", "Well done."))
    seen: list[str] = []
    out = await look.look("how many fingers?", on_first=seen.append)
    assert seen == ["Oh! Three fingers!"] and out["rest"] == "Well done."


# --------------------------------------------------------------------------- the robot's count vs ChatGPT
class SlowCounter(FakeCounter):
    """Counts in its thread for ``delay`` seconds (a cold or busy robot)."""

    def __init__(self, result: HandCount, delay: float) -> None:
        super().__init__(result)
        self.delay = delay
        self.done_at: float | None = None

    def count_jpeg(self, jpeg: bytes) -> HandCount:
        time.sleep(self.delay)
        self.done_at = time.monotonic()
        return super().count_jpeg(jpeg)


class SlowProvider(FastProvider):
    """Streams ``pieces`` with ``gap`` seconds before each; records when it was asked and if it was cancelled."""

    def __init__(self, *pieces: str, gap: float = 0.0, error: BaseException | None = None) -> None:
        super().__init__(*pieces)
        self.gap, self.fail = gap, error
        self.asked_at: list[float] = []
        self.cancelled = False

    async def reply(self, messages, *, system, max_tokens, timeout, images=None, tools=None, run_tool=None,
                    max_tool_rounds=6, detail=None, effort=None, on_delta=None) -> ProviderReply:
        self.asked_at.append(time.monotonic())
        self.calls.append({"messages": messages, "system": system, "images": list(images or [])})
        if self.fail is not None:
            raise self.fail
        try:
            for piece in self.pieces:
                await asyncio.sleep(self.gap)
                if on_delta is not None:
                    on_delta(piece)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ProviderReply(text="".join(self.pieces), model="fake")


@pytest.fixture
def race(ctx):
    allow(ctx)
    ctx.vision = WarmVision()
    return ctx


async def test_a_quick_sure_count_answers_alone(race):
    provider = SlowProvider("Three fingers!")
    look = CameraLook(race, provider=provider, finger_counter=SlowCounter(HandCount(2, 1, True), delay=0.01))
    out = await look.look("how many fingers am I holding up?")
    assert out["source"] == "robot" and out["sent"] is False and provider.calls == []
    assert out["timing"]["count"] == "sure:2" and "chatgpt_ms" not in out["timing"]


async def test_a_slow_count_asks_chatgpt_too_and_a_sure_count_still_wins(race, caplog):
    caplog.set_level(logging.INFO)
    provider = SlowProvider("You are holding ", "up two fingers.", gap=1.0)
    look = CameraLook(race, provider=provider, finger_counter=SlowCounter(HandCount(2, 1, True), delay=0.4))
    firsts: list[str] = []
    rewarmed: list[int] = []

    async def warm() -> bool:
        rewarmed.append(1)
        return True

    provider.warm = warm  # type: ignore[attr-defined]
    look.keep_warm(True)  # a live call: the connection is kept open
    await asyncio.sleep(0.05)
    assert rewarmed == [1]
    try:
        out = await look.look("how many fingers am I holding up?", on_first=firsts.append)
        assert out["source"] == "robot" and out["answer"] == "I see two fingers held up!" and firsts == [out["answer"]]
        assert out["sent"] is True and provider.cancelled  # ChatGPT was asked after the head start, then not needed
        assert out["timing"]["chatgpt"] == "cancelled" and out["latency_ms"] < 900
        await asyncio.sleep(0.05)
        assert rewarmed == [1, 1]  # cancelling closed the connection: a new one is opened for the next look
        assert "ChatGPT's answer was not needed" in caplog.text
    finally:
        look.keep_warm(False)


async def test_an_unsure_slow_count_costs_nothing_chatgpt_was_already_asked(race):
    provider = SlowProvider("Three fingers!")
    counter = SlowCounter(HandCount(0, 1, False, "unclear_finger"), delay=0.4)
    look = CameraLook(race, provider=provider, finger_counter=counter)
    t0 = time.monotonic()
    out = await look.look("how many fingers am I holding up?")
    assert out["source"] == "chatgpt" and out["answer"] == "Three fingers!" and out["latency_ms"] < 350
    (asked,) = provider.asked_at
    assert look_mod.COUNT_HEAD_START_S <= asked - t0 < 0.35  # asked after the head start, not after the count
    await asyncio.sleep(0.4)
    assert counter.done_at is not None and asked < counter.done_at


async def test_chatgpt_saying_it_first_wins_over_a_late_sure_count(race):
    provider = SlowProvider("Three fingers! ", "Well done.", gap=0.05)
    look = CameraLook(race, provider=provider, finger_counter=SlowCounter(HandCount(2, 1, True), delay=0.5))
    firsts: list[str] = []
    out = await look.look("how many fingers?", on_first=firsts.append)
    assert firsts == ["Three fingers!"] and out["source"] == "chatgpt"  # the voice never hears two answers
    assert out["answer"] == "Three fingers! Well done." and not provider.cancelled


async def test_when_chatgpt_fails_a_slow_sure_count_still_answers(race):
    provider = SlowProvider(error=NetworkError("down"))
    look = CameraLook(race, provider=provider, finger_counter=SlowCounter(HandCount(3, 1, True), delay=0.3))
    out = await look.look("how many fingers?")
    assert out["ok"] and out["source"] == "robot" and out["answer"] == "I see three fingers held up!"
    assert out["timing"]["chatgpt"] == "failed:network"


async def test_a_zero_head_start_asks_both_at_once(race, monkeypatch):
    monkeypatch.setattr(look_mod, "COUNT_HEAD_START_S", 0.0)
    provider = SlowProvider("Three fingers!", gap=0.5)
    look = CameraLook(race, provider=provider, finger_counter=SlowCounter(HandCount(1, 1, True), delay=0.05))
    t0 = time.monotonic()
    out = await look.look("how many fingers?")
    assert out["source"] == "robot" and out["sent"] is True and provider.asked_at[0] - t0 < 0.04


# --------------------------------------------------------------------------- smaller uploads, the knobs, the log
async def test_low_detail_looks_send_a_512_px_photo(race):
    cv2 = get_cv2()
    if cv2 is None:
        pytest.skip("OpenCV needed")
    img = (np.random.default_rng(1).random((480, 640, 3)) * 255).astype("uint8")
    big = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])[1].tobytes()  # a warm 640 px photo
    race.vision.latest_photo = lambda max_age=1.0: (big, 0.05)  # type: ignore[method-assign]
    provider = FastProvider("A red ball!")
    look = CameraLook(race, provider=provider)
    look.keep_warm(True)
    try:
        await look.look("what colour is this ball?")  # low detail
        await look.look("can you read this word?")  # high detail
    finally:
        look.keep_warm(False)
    low, high = (cv2.imdecode(np.frombuffer(c["images"][0], np.uint8), cv2.IMREAD_COLOR) for c in provider.calls)
    assert low.shape[:2] == (384, 512) and high.shape[:2] == (480, 640)
    assert len(provider.calls[0]["images"][0]) < len(big)


async def test_a_new_photo_is_taken_at_the_size_the_question_needs(fast):
    ctx, look, provider, events = fast
    look._counter = FakeCounter(HandCount(0, 0, False, "no_hand"))
    await look.look("what colour is my hat?")
    await look.look("how many fingers am I holding up?")
    await look.look("what does this say?")
    assert [p["max_width"] for p in ctx.vision.photos] == [look_mod.LOW_DETAIL_WIDTH, 640, 640]


async def test_the_config_picks_the_look_models_detail_and_effort(ctx):
    allow(ctx)
    ctx.vision = FakeVision()
    cfg = ctx.config.chatgpt
    cfg.look_models = ["quick-model", cfg.models[0]]
    cfg.look_effort, cfg.look_count_effort, cfg.look_detail = "minimal", "low", "low"
    look = CameraLook(ctx)
    assert look.provider.models == ["quick-model", *cfg.models]  # tried first, then the usual ones (once each)
    assert (look.effort, look.count_effort, look.detail) == ("minimal", "low", "low")
    look._provider = provider = FastProvider()
    await look.look("how many fingers am I holding up?")
    assert provider.options == [{"detail": "low", "effort": "low", "streams": True}]


def test_the_config_rejects_unknown_look_settings():
    from pydantic import ValidationError

    from littlebot.core.config import ChatGPTConfig

    assert ChatGPTConfig().look_models == [] and ChatGPTConfig().look_detail == "auto"
    with pytest.raises(ValidationError):
        ChatGPTConfig(look_detail="ultra")


async def test_the_timing_says_where_the_time_went(fast, caplog):
    ctx, look, provider, events = fast
    caplog.set_level(logging.INFO)
    look.keep_warm(True)
    look._counter = FakeCounter(HandCount(0, 1, False, "fist"))
    out = await look.look("how many fingers am I holding up?", on_first=lambda _s: None)
    t = out["timing"]
    assert t["photo"] == "warm" and t["photo_age_ms"] == 100 and t["count"] == "unsure:fist" and "count_ms" in t
    assert t["model"] == "fake" and t["detail"] == "high" and t["effort"] == look_mod.COUNT_EFFORT
    assert {"photo_kb", "first_token_ms", "first_sentence_ms", "chatgpt_ms"} <= set(t)
    line = next(r.getMessage() for r in caplog.records if "camera look timing" in r.getMessage())
    assert line.endswith(look_mod.timing_text(t)) and "fingers" not in line


async def test_a_failed_photo_is_in_the_timing_too(setup, caplog):
    ctx, look, provider, events = setup
    caplog.set_level(logging.INFO)
    ctx.vision.error = PhotoUnavailable("timeout")
    out = await look.look("what is this?")
    assert out["timing"] == {"photo": "failed:photo_failed"} and "photo=failed:photo_failed" in caplog.text


async def test_keeping_warm_renews_the_sign_in_ahead_of_time(fast):
    ctx, look, provider, events = fast
    renewed: list[int] = []

    class Auth:
        async def bearer(self) -> str:
            renewed.append(1)
            return "token"

    provider.auth = Auth()  # type: ignore[attr-defined]
    look.keep_warm(True)
    await asyncio.sleep(0.05)
    assert renewed == [1]  # a token about to expire is refreshed now, not during the next look


def test_the_warm_photo_is_refreshed_with_every_frame_the_camera_reads(ctx):
    every_frame = 1.0 / float(ctx.config.camera.detect_fps)
    assert look_mod.WARM_EVERY_S < every_frame  # so it is never two frames old ...
    assert 2 * every_frame < look_mod.WARM_MAX_AGE_HANDS_S  # ... and a hand question finds it (a frame late, even)
