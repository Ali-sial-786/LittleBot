"""Body language: bus events (assistant.*, state, vision, cards) -> face glances / nods and gestures."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from littlebot.app.behaviours import Behaviours
from littlebot.core.arbiter import Priority, Resource
from littlebot.core.clock import ManualClock
from littlebot.core.events import Topics, collect
from littlebot.core.state import RobotState
from littlebot.motion.body import CENTRE, EXPRESSION_GESTURES, BodyLanguage
from littlebot.motion.controller import MotionController
from littlebot.testing import FakeMotion, make_test_context

from motion_testkit import run_until


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class RecordingMotion(FakeMotion):
    """FakeMotion that also remembers the priority / owner of every command."""

    def __init__(self) -> None:
        super().__init__()
        self.priorities: list[tuple[str, Any, Any]] = []
        self.release = asyncio.Event()
        self.slow = False

    async def run_gesture(self, name: str, *, priority: int | None = None, owner: str | None = None) -> bool:
        self.priorities.append((name, priority, owner))
        if self.slow:
            self._rec("run_gesture", name)
            await self.release.wait()
            return True
        return await super().run_gesture(name, priority=priority, owner=owner)

    def look_at(self, x: float, y: float, *, priority: int | None = None, owner: str | None = None) -> bool:
        self.priorities.append(("look_at", priority, owner))
        return super().look_at(x, y, priority=priority, owner=owner)

    async def neutral(self, *, priority: int | None = None, owner: str | None = None) -> bool:
        self.priorities.append(("neutral", priority, owner))
        return await super().neutral(priority=priority, owner=owner)


async def settle(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.fixture
async def rig():
    """(ctx, body, clock): fakes for face + motion, an injected clock, idle motion off unless a test sets it."""
    made: list[tuple[Any, BodyLanguage]] = []

    async def _make(*, motion: Any = "recording", **motion_cfg: Any):
        motion_cfg.setdefault("idle_motion_seconds", 0)
        ctx = make_test_context(motion=motion_cfg)
        if motion == "recording":
            ctx.motion = RecordingMotion()
        elif motion is None:
            ctx.motion = None
        clock = Clock()
        body = BodyLanguage(ctx, clock=clock, rng=random.Random(1), poll_seconds=3600)
        await body.start()
        made.append((ctx, body))
        return ctx, body, clock

    yield _make
    for ctx, body in made:
        await body.stop()
        ctx.db.close()


def gestures(ctx) -> list[str]:
    return [a[0] for a, _k in ctx.motion.called("run_gesture")]


def face_calls(ctx, name: str) -> list[tuple[tuple, dict]]:
    return ctx.face.called(name)


def publish(ctx, topic: str, data: dict | None = None) -> None:
    ctx.bus.publish(topic, data or {}, source="test")


def face_event(x: float, y: float) -> dict:
    return {"faces": [{"x": 10, "y": 10, "w": 40, "h": 40, "gaze_x": x, "gaze_y": y, "primary": True}],
            "frame_w": 320, "frame_h": 240, "simulated": True}


# ----------------------------------------------------------------- listening
async def test_listening_looks_at_the_tracked_person_and_perks_up(rig):
    ctx, body, clock = await rig()
    publish(ctx, Topics.VISION_FACES, face_event(0.3, 0.4))
    ctx.motion.calls.clear()
    publish(ctx, "assistant.phase", {"phase": "listening"})
    await settle()
    assert face_calls(ctx, "look_at") == [((0.3, 0.4), {"hold": 1.5})]
    assert gestures(ctx) == ["perk_up"]
    assert ctx.motion.called("look_at") == [((0.3, 0.4), {})]
    assert all(p == Priority.AMBIENT for _n, p, _o in ctx.motion.priorities)
    # "hearing" is the same mode: no second perk
    publish(ctx, "assistant.phase", {"phase": "hearing"})
    await settle()
    assert gestures(ctx) == ["perk_up"]


async def test_listening_without_a_face_looks_at_the_centre(rig):
    ctx, body, clock = await rig()
    publish(ctx, Topics.VISION_FACES, face_event(0.2, 0.2))
    clock.advance(5.0)  # that face is old news
    publish(ctx, "assistant.phase", {"phase": "listening"})
    await settle()
    assert face_calls(ctx, "look_at") == [(CENTRE, {"hold": 1.5})]


async def test_robot_state_drives_the_same_modes_once(rig):
    ctx, body, clock = await rig()
    ctx.state.transition(RobotState.LISTENING, "test")
    publish(ctx, "assistant.phase", {"phase": "listening"})  # both pipelines saying the same thing
    await settle()
    assert gestures(ctx) == ["perk_up"] and len(face_calls(ctx, "look_at")) == 1
    clock.advance(2.0)
    ctx.state.transition(RobotState.THINKING, "test")
    await settle()
    assert body.mode == "thinking" and gestures(ctx) == ["perk_up", "look_up_think"]


async def test_perk_up_not_repeated_on_quick_follow_up_turns(rig):
    ctx, body, clock = await rig()
    for phase in ("listening", "thinking", "speaking", "listening"):
        publish(ctx, "assistant.phase", {"phase": phase})
        await settle()
        clock.advance(2.0)
    assert gestures(ctx).count("perk_up") == 1  # within PERK_EVERY_S of the first


# ----------------------------------------------------------------- thinking
async def test_thinking_glances_up_and_aside_and_again_when_long(rig):
    ctx, body, clock = await rig()
    publish(ctx, "assistant.phase", {"phase": "thinking"})
    await settle()
    (x, y), kw = face_calls(ctx, "look_at")[0]
    assert y < 0.4 and abs(x - 0.5) > 0.1 and kw["hold"] > 0
    assert gestures(ctx) == ["look_up_think"]
    clock.advance(1.0)
    body.poll()
    assert len(face_calls(ctx, "look_at")) == 1  # not yet
    clock.advance(2.5)
    body.poll()
    (x2, y2), _ = face_calls(ctx, "look_at")[1]
    assert y2 < 0.4 and (x2 - 0.5) * (x - 0.5) < 0  # the other side
    # the head does not follow faces while it "thinks"
    ctx.motion.calls.clear()
    publish(ctx, Topics.VISION_FACES, face_event(0.9, 0.5))
    assert ctx.motion.called("look_at") == []


# ----------------------------------------------------------------- speaking
@pytest.mark.parametrize("expression,gesture", sorted(EXPRESSION_GESTURES.items()))
async def test_sentence_expressions_drive_gestures(rig, expression, gesture):
    ctx, body, clock = await rig()
    publish(ctx, "assistant.phase", {"phase": "speaking"})
    publish(ctx, "assistant.sentence", {"index": 0, "chars": 30, "expression": expression, "last": False})
    await settle()
    assert gestures(ctx) == [gesture]


def test_the_required_expression_mapping():
    assert EXPRESSION_GESTURES["celebrating"] == "celebrate"
    assert EXPRESSION_GESTURES["happy"] == EXPRESSION_GESTURES["excited"] == "arm_wiggle"
    assert EXPRESSION_GESTURES["curious"] == "tilt_curious"
    assert EXPRESSION_GESTURES["sad"] == "droop"
    assert EXPRESSION_GESTURES["confused"] == "head_shake"


async def test_nods_on_sentence_ends_and_a_closing_nod(rig):
    ctx, body, clock = await rig()
    publish(ctx, "assistant.phase", {"phase": "speaking"})
    publish(ctx, "assistant.sentence", {"index": 0, "chars": 30, "expression": None, "last": False})
    await settle()
    assert gestures(ctx) == [] and face_calls(ctx, "play_animation") == []  # nothing ended yet
    clock.advance(2.0)
    publish(ctx, "assistant.sentence", {"index": 1, "chars": 30, "expression": None, "last": True})
    await settle()
    assert gestures(ctx) == ["nod_small"] and face_calls(ctx, "play_animation") == [(("nod",), {})]
    clock.advance(2.0)
    publish(ctx, "assistant.phase", {"phase": "listening"})  # the answer ended: closing nod
    await settle()
    assert gestures(ctx)[:2] == ["nod_small", "nod_small"]
    assert len(face_calls(ctx, "play_animation")) == 2


async def test_heard_gets_a_small_face_nod(rig):
    ctx, body, clock = await rig()
    publish(ctx, "assistant.heard", {"words": 5, "ms": 1800})
    await settle()
    assert face_calls(ctx, "play_animation") == [(("nod",), {})]


async def test_everything_is_rate_limited(rig):
    ctx, body, clock = await rig()
    publish(ctx, "assistant.phase", {"phase": "speaking"})
    starts: list[float] = []
    for i in range(12):  # a sentence every 0.5 s for 6 s, each with an expression
        before = len(gestures(ctx))
        publish(ctx, "assistant.sentence", {"index": i, "chars": 10, "expression": "happy", "last": False})
        await settle()
        if len(gestures(ctx)) > before:
            starts.append(clock())
        clock.advance(0.5)
    assert 3 <= len(starts) <= 4
    assert all(b - a >= 1.5 for a, b in zip(starts, starts[1:]))
    nods = len(face_calls(ctx, "play_animation"))
    assert nods <= 4  # face nods have the same limit


# ----------------------------------------------------------------- cards, interruptions, sessions
async def test_a_card_on_the_screen_gets_pointed_at(rig):
    ctx, body, clock = await rig()
    publish(ctx, "screen.card_shown", {"kind": "weather", "title": "Weather", "source": "voice"})
    await settle()
    assert gestures(ctx) == ["point_screen"]


async def test_interrupted_stops_our_gesture_and_looks_at_the_person(rig):
    ctx, body, clock = await rig()
    ctx.motion.slow = True
    publish(ctx, "assistant.phase", {"phase": "speaking"})
    publish(ctx, "assistant.sentence", {"index": 0, "chars": 30, "expression": "celebrating", "last": False})
    await settle()
    task = next(iter(body._tasks))  # noqa: SLF001 - the running celebrate
    publish(ctx, Topics.VISION_FACES, face_event(0.6, 0.5))
    face_before = len(face_calls(ctx, "look_at"))
    publish(ctx, "assistant.interrupted", {})
    await settle()
    assert task.cancelled()
    assert ctx.motion.called("neutral") and ("neutral", Priority.AMBIENT, None) in ctx.motion.priorities
    assert face_calls(ctx, "look_at")[face_before:] == [((0.6, 0.5), {"hold": 1.5})]
    publish(ctx, "assistant.phase", {"phase": "listening"})  # no closing nod after an interruption
    await settle()
    assert face_calls(ctx, "play_animation") == []


@pytest.mark.parametrize("topic,data", [("assistant.session", {"active": False, "reason": "idle"}),
                                        ("live.call_ended", {})])
async def test_session_end_goes_neutral(rig, topic, data):
    ctx, body, clock = await rig()
    publish(ctx, "assistant.session", {"active": True})
    publish(ctx, "assistant.phase", {"phase": "listening"})
    await settle()
    publish(ctx, topic, data)
    await settle()
    assert ctx.motion.called("neutral") and face_calls(ctx, "clear_gaze")
    assert body.mode == "idle" and body.status()["session"] is False


# ----------------------------------------------------------------- idle
async def test_idle_head_follows_a_detected_face(rig):
    ctx, body, clock = await rig()
    publish(ctx, Topics.VISION_FACES, face_event(0.8, 0.3))
    assert ctx.motion.called("look_at") == [((0.8, 0.3), {})]
    assert ctx.motion.priorities[-1] == ("look_at", Priority.AMBIENT, None)
    ctx.settings.set("vision.follow_faces", False)
    publish(ctx, Topics.VISION_FACES, face_event(0.8, 0.3))
    assert len(ctx.motion.called("look_at")) == 1
    publish(ctx, Topics.VISION_FACES, {"faces": []})  # nothing to follow, no error
    assert face_calls(ctx, "look_at") == []  # the eyes follow faces through Behaviours (track_gaze), not here


async def test_occasional_tiny_idle_motion(rig):
    ctx, body, clock = await rig(idle_motion_seconds=60)
    body.poll()
    await settle()
    assert gestures(ctx) == []
    clock.advance(90)  # 60 s +- 40 %
    body.poll()
    await settle()
    assert gestures(ctx) == ["idle_shift"]
    assert ctx.motion.priorities[-1] == ("idle_shift", Priority.AMBIENT, None)
    body.poll()
    clock.advance(20)
    body.poll()
    await settle()
    assert gestures(ctx) == ["idle_shift"]  # rare
    clock.advance(90)
    ctx.face.dozing = True  # a dozing face keeps still
    body.poll()
    await settle()
    assert gestures(ctx) == ["idle_shift"]
    ctx.face.dozing = False
    publish(ctx, "assistant.session", {"active": True})  # never during a conversation
    await settle()
    clock.advance(200)
    body.poll()
    await settle()
    assert gestures(ctx) == ["idle_shift"]


async def test_no_idle_motion_when_switched_off(rig):
    ctx, body, clock = await rig(idle_motion_seconds=0)
    clock.advance(10_000)
    body.poll()
    await settle()
    assert gestures(ctx) == []


# ----------------------------------------------------------------- safety + no hardware
async def test_emergency_stop_blocks_every_movement(rig):
    ctx, body, clock = await rig()
    publish(ctx, Topics.MOTION_ESTOP, {"active": True, "source": "test"})
    await settle()
    for topic, data in [("assistant.phase", {"phase": "listening"}), ("assistant.phase", {"phase": "thinking"}),
                        ("assistant.phase", {"phase": "speaking"}),
                        ("assistant.sentence", {"index": 1, "expression": "celebrating", "last": True}),
                        ("screen.card_shown", {"kind": "x"}), ("assistant.interrupted", {}),
                        ("assistant.session", {"active": False}), (Topics.VISION_FACES, face_event(0.9, 0.9))]:
        publish(ctx, topic, data)
        await settle()
        clock.advance(2.0)
    body.poll()
    assert ctx.motion.calls == []
    assert face_calls(ctx, "look_at") and face_calls(ctx, "play_animation")  # the face still reacts
    publish(ctx, Topics.MOTION_ESTOP, {"active": False, "source": "reset"})
    publish(ctx, "screen.card_shown", {"kind": "x"})
    await settle()
    assert gestures(ctx) == ["point_screen"]


async def test_estop_already_latched_at_start(rig):
    ctx, rig_body, _clock = await rig()
    await rig_body.stop()
    motion = RecordingMotion()
    motion.estop = True
    ctx.motion = motion
    body = BodyLanguage(ctx, clock=Clock(), poll_seconds=3600)
    await body.start()
    try:
        publish(ctx, "screen.card_shown", {"kind": "x"})
        await settle()
        assert motion.calls == []
    finally:
        await body.stop()


async def test_face_parts_work_without_any_motion(rig):
    ctx, body, clock = await rig(motion=None)
    publish(ctx, Topics.VISION_FACES, face_event(0.4, 0.4))
    for phase in ("listening", "thinking", "speaking"):
        publish(ctx, "assistant.phase", {"phase": phase})
        await settle()
        clock.advance(2.0)
    publish(ctx, "assistant.sentence", {"index": 1, "expression": "happy", "last": True})
    publish(ctx, "screen.card_shown", {"kind": "x"})
    publish(ctx, "assistant.interrupted", {})
    publish(ctx, "assistant.session", {"active": False})
    await settle()
    assert len(face_calls(ctx, "look_at")) >= 3 and face_calls(ctx, "play_animation") == [(("nod",), {})]
    assert body.status()["motion"] is False and body.status()["gestures"] == 0


async def test_activity_lease_overrides_body_language(rig):
    ctx, body, clock = await rig()
    lease = ctx.arbiter.acquire("activity", [Resource.MOTION], Priority.ACTIVITY)
    publish(ctx, "assistant.phase", {"phase": "listening"})
    publish(ctx, Topics.VISION_FACES, face_event(0.9, 0.5))
    await settle()
    assert ctx.motion.calls == [] and face_calls(ctx, "look_at")
    lease.release()
    clock.advance(2.0)
    publish(ctx, "screen.card_shown", {"kind": "x"})
    await settle()
    assert gestures(ctx) == ["point_screen"]


@pytest.mark.parametrize("quiet", ["game", "media"])
async def test_quiet_during_games_and_videos(rig, quiet):
    ctx, body, clock = await rig()
    if quiet == "game":
        publish(ctx, "game.started", {"id": "pop"})
    else:
        ctx.state.transition(RobotState.PLAYING_MEDIA, "test")
    await settle()
    for phase in ("listening", "thinking", "speaking"):
        publish(ctx, "assistant.phase", {"phase": phase})
        await settle()
        clock.advance(2.0)
    publish(ctx, "screen.card_shown", {"kind": "x"})
    await settle()
    assert ctx.motion.calls == []
    assert ctx.face.calls == []


async def test_stop_all_calms_everything(rig):
    ctx, body, clock = await rig()
    ctx.motion.slow = True
    publish(ctx, "screen.card_shown", {"kind": "x"})
    await settle()
    task = next(iter(body._tasks))  # noqa: SLF001
    publish(ctx, Topics.STOP_ALL, {"source": "test"})
    await settle()
    assert task.cancelled() and body.mode == "idle"
    publish(ctx, "screen.card_shown", {"kind": "x"})  # nothing new right after a STOP
    await settle()
    assert gestures(ctx) == ["point_screen"]


# ----------------------------------------------------------------- wiring + the real controller
async def test_behaviours_start_body_language_when_configured():
    ctx = make_test_context()
    b = Behaviours(ctx)
    assert isinstance(b.body, BodyLanguage)
    await b.start()
    try:
        ctx.bus.publish("screen.card_shown", {"kind": "x"})
        await settle()
        assert ctx.motion.called("run_gesture") == [(("point_screen",), {})]
    finally:
        await b.stop()
        ctx.db.close()
    ctx = make_test_context(motion={"body_language": False})
    assert Behaviours(ctx).body is None
    ctx.db.close()


async def test_with_the_motion_controller_at_ambient_priority(tmp_path):
    ctx = make_test_context(motion={"calibration_file": str(tmp_path / "none.yaml"), "settle_seconds": 0.0,
                                    "idle_motion_seconds": 0})
    controller = MotionController(ctx, clock=ManualClock())
    ctx.motion = controller
    await controller.start()
    events = collect(ctx.bus, ["motion.gesture_*"])
    body = BodyLanguage(ctx, clock=Clock(), poll_seconds=3600)
    await body.start()
    try:
        publish(ctx, "assistant.phase", {"phase": "listening"})
        await run_until(lambda: any(e.topic == Topics.MOTION_GESTURE_STARTED for e in events))
        assert events[0].data == {"gesture": "perk_up"}
        holder = ctx.arbiter.holder(Resource.MOTION)
        assert holder is not None and holder.priority == Priority.AMBIENT
        # an activity takes the motors: body language's movement stops at once
        lease = ctx.arbiter.acquire("activity", [Resource.MOTION], Priority.ACTIVITY)
        await run_until(lambda: any(e.topic == Topics.MOTION_GESTURE_FINISHED for e in events))
        assert events[-1].data == {"gesture": "perk_up", "cancelled": True}
        lease.release()
        # an emergency stop: refused by body language and by the controller
        controller.emergency_stop("test")
        await settle()
        assert body.status()["estop"] is True
    finally:
        await body.stop()
        await controller.stop()
        ctx.db.close()
