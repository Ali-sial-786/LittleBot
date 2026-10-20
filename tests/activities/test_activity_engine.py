"""Activity engine: lifecycle, prompts, events, leases, state, robustness, privacy."""

from __future__ import annotations

import asyncio
import threading
import textwrap

import pytest

from activity_helpers import activity_yaml, auto_answer, make_engine, wait_until
from littlebot.activities import create_service
from littlebot.activities.engine import ActivityEngine, HISTORY_TABLE
from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics, collect
from littlebot.core.interfaces import ActivityAPI
from littlebot.core.service import Service
from littlebot.core.state import RobotState
from littlebot.testing import FakeAudio


def steps(block: str) -> str:
    return textwrap.indent(textwrap.dedent(block).strip("\n"), "  ") + "\n"


SIMPLE = activity_yaml(steps("""
    - say: "Hello {child_name}!"
      expression: happy
    - sound: pop
    - say: "Bye!"
"""), id="simple")

# A prompt that waits (in real time) so the activity stays in progress.
LONG = activity_yaml(steps("""
    - say: "Start"
    - close_eyes
    - prompt: "Waiting for the grown-up"
      options: ["Go"]
      timeout: 30
    - open_eyes
    - say: "End"
"""), id="long_one")

BRANCHY = activity_yaml(steps("""
    - prompt: "Did she clap?"
      options: ["Clapped!", "Not yet"]
      timeout: 30
      branches:
        "Clapped!": [{say: "branch clapped"}]
        "Not yet": [{say: "branch not yet"}]
      on_timeout: [{say: "branch timeout"}]
    - say: "after"
"""), id="branchy")


async def run(engine: ActivityEngine, activity_id: str, timeout: float = 5.0) -> None:
    await engine.start(activity_id)
    assert await engine.wait_until_finished(timeout), "activity did not finish"


def finished_events(ctx):
    return [e for e in ctx.bus.recent(300, Topics.ACTIVITY_FINISHED)]


# ----------------------------------------------------------------- contract
def test_engine_implements_protocols(ctx):
    engine = create_service(ctx)
    assert isinstance(engine, ActivityAPI)
    assert isinstance(engine, Service)
    assert len(engine.library) >= 25 and not engine.library.errors
    assert engine.status()["running"] is False
    assert engine.health()["name"] == "activities"


async def test_service_lifecycle_start_and_stop(ctx):
    engine = make_engine(ctx, SIMPLE)
    health = await engine.start()  # no id = Service lifecycle start
    assert health["status"] == "running"
    await engine.stop()  # nothing running: no-op
    assert engine.status()["running"] is False


async def test_unknown_activity_raises_keyerror(ctx):
    engine = make_engine(ctx, SIMPLE)
    with pytest.raises(KeyError):
        await engine.start("does_not_exist")
    assert ctx.state.state == RobotState.IDLE


async def test_lease_refused_raises_permission_error(ctx):
    engine = make_engine(ctx, SIMPLE)
    ctx.arbiter.acquire("media", [Resource.AUDIO_OUT, Resource.SCREEN], Priority.MEDIA)
    with pytest.raises(PermissionError):
        await engine.start("simple")
    assert ctx.state.state == RobotState.IDLE
    assert engine.status()["running"] is False


async def test_state_refused_raises_permission_error_and_releases_lease(ctx):
    engine = make_engine(ctx, SIMPLE)
    ctx.state.transition(RobotState.ERROR, "boom")
    with pytest.raises(PermissionError):
        await engine.start("simple")
    assert ctx.arbiter.is_free(Resource.FACE)


# ----------------------------------------------------------------- lifecycle
async def test_full_run_events_state_and_lease(ctx):
    engine = make_engine(ctx, SIMPLE)
    seen = collect(ctx.bus, ["activity.*", Topics.STATE_CHANGED])
    status = await engine.start("simple", source="test")
    assert status["running"] and status["id"] == "simple" and status["total_steps"] == 3
    assert ctx.state.state == RobotState.PLAYING_ACTIVITY
    assert ctx.arbiter.holder(Resource.FACE).owner == "activities"
    assert await engine.wait_until_finished(5)
    topics = [e.topic for e in seen]
    assert topics[0] == Topics.STATE_CHANGED and seen[0].data["new"] == "PLAYING_ACTIVITY"
    assert Topics.ACTIVITY_STARTED in topics and topics.count(Topics.ACTIVITY_STEP) == 2
    fin = [e for e in seen if e.topic == Topics.ACTIVITY_FINISHED][0]
    assert fin.data["id"] == "simple" and fin.data["completed"] is True and fin.data["reason"] == "completed"
    assert ctx.state.state == RobotState.IDLE
    assert ctx.arbiter.is_free(Resource.FACE) and ctx.arbiter.is_free(Resource.AUDIO_OUT)
    assert ctx.audio.spoken == ["Hello friend!", "Bye!"]
    assert ctx.audio.sounds == ["pop"]
    st = engine.status()
    assert st["running"] is False and st["last"]["completed"] is True


async def test_state_not_clobbered_when_it_moved_on(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG)
    await engine.start("long_one")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    assert ctx.state.transition(RobotState.LISTENING, "someone listened")
    assert engine.respond("Go")
    assert await engine.wait_until_finished(5)
    assert ctx.state.state == RobotState.LISTENING  # release() did not force IDLE


async def test_stop_mid_activity_cleans_up(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG)
    seen = collect(ctx.bus, ["activity.*"])
    await engine.start("long_one")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    assert ctx.face.eyes_closed
    await engine.stop("parent stop")
    fin = [e for e in seen if e.topic == Topics.ACTIVITY_FINISHED]
    assert len(fin) == 1 and fin[0].data == {**fin[0].data, "completed": False, "reason": "parent stop"}
    assert any(e.topic == Topics.ACTIVITY_PROMPT_CLEARED and e.data.get("cancelled") for e in seen)
    assert not ctx.face.eyes_closed
    assert ctx.face.expression == "neutral"
    assert ctx.audio.called("stop_output")
    assert ctx.state.state == RobotState.IDLE
    assert ctx.arbiter.is_free(Resource.FACE)
    assert engine.status()["running"] is False
    assert engine.respond("Go") is False  # prompt is gone


async def test_starting_another_activity_replaces_the_running_one(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG, SIMPLE)
    seen = collect(ctx.bus, [Topics.ACTIVITY_FINISHED])
    await engine.start("long_one")
    await engine.start("simple")
    assert await engine.wait_until_finished(5)
    assert [(e.data["id"], e.data["reason"]) for e in seen] == [("long_one", "replaced"), ("simple", "completed")]
    assert ctx.state.state == RobotState.IDLE


async def test_stop_all_event_stops_activity(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG)
    await engine.start("long_one")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    assert await engine.wait_until_finished(2)
    fin = finished_events(ctx)[-1]
    assert fin.data["completed"] is False and fin.data["reason"] == "stop all"
    assert ctx.audio.called("stop_output") and not ctx.face.eyes_closed


async def test_stop_all_from_another_thread(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG)
    await engine.start("long_one")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    t = threading.Thread(target=lambda: ctx.bus.publish(Topics.STOP_ALL, {"source": "button"}))
    t.start()
    t.join()
    assert await engine.wait_until_finished(2)
    assert ctx.state.state == RobotState.IDLE


async def test_lease_preemption_stops_activity(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG)
    await engine.start("long_one")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    stops_before = len(ctx.audio.called("stop_output"))
    lease = ctx.arbiter.acquire("parent_face", [Resource.FACE], Priority.PARENT)
    assert lease is not None
    assert len(ctx.audio.called("stop_output")) > stops_before  # silenced synchronously
    assert await engine.wait_until_finished(2)
    fin = finished_events(ctx)[-1]
    assert fin.data["completed"] is False and fin.data["reason"] == "preempted by parent_face"
    assert ctx.arbiter.holder(Resource.FACE) is lease  # our cleanup did not touch the new holder
    assert not ctx.face.eyes_closed
    assert ctx.state.state == RobotState.IDLE


async def test_lower_priority_lease_cannot_interrupt(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG)
    await engine.start("long_one")
    assert ctx.arbiter.acquire("chat", [Resource.AUDIO_OUT], Priority.CONVERSATION) is None
    assert engine.status()["running"]
    await engine.stop()


async def test_sleep_state_stops_activity(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, LONG)
    await engine.start("long_one")
    ctx.state.transition(RobotState.SLEEPING, "parent sleep")
    assert await engine.wait_until_finished(2)
    assert finished_events(ctx)[-1].data["reason"] == "robot state sleeping"
    assert ctx.state.state == RobotState.SLEEPING


# ----------------------------------------------------------------- pause / resume
PAUSY = activity_yaml(steps("""
    - say: "one"
    - wait: 0.3
    - say: "two"
    - wait: 0.05
    - say: "three"
"""), id="pausy")


async def test_pause_and_resume(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, PAUSY)
    seen = collect(ctx.bus, [Topics.ACTIVITY_PAUSED, Topics.ACTIVITY_RESUMED])
    await engine.start("pausy")
    assert await wait_until(lambda: ctx.audio.spoken == ["one"])
    assert await engine.pause() is True
    assert await engine.pause() is True  # idempotent
    assert engine.status()["paused"] is True
    assert ctx.audio.called("stop_output")
    await asyncio.sleep(0.5)  # the 0.3 s wait is frozen while paused
    assert ctx.audio.spoken == ["one"]
    assert await engine.resume() is True
    assert await engine.resume() is False
    assert await engine.wait_until_finished(3)
    assert ctx.audio.spoken == ["one", "two", "three"]
    assert [e.topic for e in seen] == [Topics.ACTIVITY_PAUSED, Topics.ACTIVITY_RESUMED]


async def test_pause_requires_pausable_and_running(slow_ctx):
    ctx = slow_ctx
    fixed = activity_yaml(steps("- wait: 5"), id="fixed", pausable=False)
    engine = make_engine(ctx, fixed)
    assert await engine.pause() is False
    assert await engine.resume() is False
    await engine.start("fixed")
    assert await engine.pause() is False
    await engine.stop()


async def test_prompt_timeout_is_frozen_while_paused(slow_ctx):
    ctx = slow_ctx
    quick = activity_yaml(steps("""
        - prompt: "q"
          timeout: 1
          on_timeout: [{say: timed out}]
    """), id="quick")
    engine = make_engine(ctx, quick)
    await engine.start("quick")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    await engine.pause()
    await asyncio.sleep(1.3)
    assert engine.status()["running"] and ctx.audio.spoken == []
    assert engine.respond("Done")  # answering while paused is fine
    await engine.resume()
    assert await engine.wait_until_finished(2)
    assert ctx.audio.spoken == []


async def test_speech_interrupted_by_pause_is_repeated(ctx):
    class StoppableAudio(FakeAudio):
        """say() returns False while 'interrupted', like the real service after stop_output()."""

        def __init__(self, bus):
            super().__init__(bus)
            self.interrupt_next = False

        async def say(self, text, *, rate=None):
            await super().say(text, rate=rate)
            if self.interrupt_next:
                self.interrupt_next = False
                await engine.pause()
                return False
            return True

    ctx.audio = StoppableAudio(ctx.bus)
    engine = make_engine(ctx, activity_yaml(steps("- say: long sentence\n- say: next"), id="talky"))
    ctx.audio.interrupt_next = True
    await engine.start("talky")
    assert await wait_until(lambda: engine.status()["paused"])
    await asyncio.sleep(0.02)
    assert ctx.audio.spoken == ["long sentence"]
    await engine.resume()
    assert await engine.wait_until_finished(2)
    assert ctx.audio.spoken == ["long sentence", "long sentence", "next"]


# ----------------------------------------------------------------- caregiver prompts
@pytest.mark.parametrize("answer, expected", [
    ("Clapped!", "branch clapped"),
    ("clapped", "branch clapped"),  # case/punctuation-insensitive
    ("Not yet", "branch not yet"),
    ("2", "branch not yet"),  # 1-based option number
    ("done", "branch clapped"),  # generic confirm = first option
    (None, "branch timeout"),  # nobody answers (time_scale 0 -> immediate timeout)
])
async def test_prompt_branches(ctx, answer, expected):
    engine = make_engine(ctx, BRANCHY)
    prompts = auto_answer(ctx, lambda options: answer)
    cleared = collect(ctx.bus, [Topics.ACTIVITY_PROMPT_CLEARED])
    await run(engine, "branchy")
    assert ctx.audio.spoken == [expected, "after"]
    assert prompts[0]["options"] == ["Clapped!", "Not yet"] and prompts[0]["prompt_id"]
    assert cleared[0].data["prompt_id"] == prompts[0]["prompt_id"]
    if answer is None:
        assert cleared[0].data["timed_out"] is True and cleared[0].data["response"] is None


async def test_prompt_skip_continues_without_branch(ctx):
    engine = make_engine(ctx, BRANCHY)
    auto_answer(ctx, lambda options: "skip")
    await run(engine, "branchy")
    assert ctx.audio.spoken == ["after"]


async def test_respond_validation(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, BRANCHY)
    assert engine.respond("Clapped!") is False  # nothing running
    await engine.start("branchy")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    prompt = engine.status()["prompt"]
    assert prompt["text"] == "Did she clap?" and prompt["options"] == ["Clapped!", "Not yet"]
    assert engine.respond("Clapped!", prompt_id="stale-id") is False
    assert engine.respond("banana") is False
    assert engine.respond("Not yet", prompt_id=prompt["prompt_id"]) is True
    assert engine.respond("Clapped!") is False  # already answered
    assert await engine.wait_until_finished(2)
    assert ctx.audio.spoken == ["branch not yet", "after"]


async def test_caregiver_response_event_and_other_thread(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, BRANCHY)
    await engine.start("branchy")
    assert await wait_until(lambda: engine.status()["prompt"] is not None)
    pid = engine.status()["prompt"]["prompt_id"]
    t = threading.Thread(target=lambda: ctx.bus.publish(Topics.CAREGIVER_RESPONSE,
                                                         {"response": "Clapped!", "prompt_id": pid}))
    t.start()
    t.join()
    assert await engine.wait_until_finished(2)
    assert ctx.audio.spoken == ["branch clapped", "after"]


async def test_prompt_timeout_uses_config_default(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, activity_yaml(steps("- prompt: q"), id="dflt"))
    prompts = auto_answer(ctx, lambda o: None)
    await engine.start("dflt")
    assert await wait_until(lambda: bool(prompts))
    assert prompts[0]["timeout_seconds"] == ctx.config.activities.caregiver_timeout_seconds
    assert prompts[0]["options"] == ["Done"]
    await engine.stop()


# ----------------------------------------------------------------- wait_for
WAITER = activity_yaml(steps("""
    - wait_for: vision.person_present
      timeout: 5
      on_event: [{say: seen}]
      on_timeout: [{say: not seen}]
"""), id="waiter")

WAVE = activity_yaml(steps("""
    - wait_for: vision.gesture
      match: {gesture: wave}
      timeout: 5
      ask_caregiver: "Tap when she waves"
      caregiver_option: "Waved!"
      on_event: [{say: waved}]
      on_timeout: [{say: no wave}]
"""), id="wave")


async def test_wait_for_satisfied_by_bus_event(ctx):
    engine = make_engine(ctx, WAITER)

    def on_step(event):
        if event.get("kind") == "wait_for":
            ctx.bus.publish(Topics.VISION_PERSON_PRESENT, {"gaze_x": 0.5, "gaze_y": 0.5})

    ctx.bus.subscribe(Topics.ACTIVITY_STEP, on_step)
    await run(engine, "waiter")
    assert ctx.audio.spoken == ["seen"]


async def test_wait_for_event_from_another_thread(slow_ctx):
    ctx = slow_ctx
    engine = make_engine(ctx, WAITER)
    await engine.start("waiter")
    await asyncio.sleep(0.05)
    t = threading.Thread(target=lambda: ctx.bus.publish(Topics.VISION_PERSON_PRESENT, {}))
    t.start()
    t.join()
    assert await engine.wait_until_finished(2)
    assert ctx.audio.spoken == ["seen"]


async def test_wait_for_times_out(ctx):
    engine = make_engine(ctx, WAITER)
    await run(engine, "waiter")
    assert ctx.audio.spoken == ["not seen"]


async def test_wait_for_wave_gesture_event_filtered_by_match(ctx):
    ctx.settings.set("vision.gestures_enabled", True)
    engine = make_engine(ctx, WAVE)

    def on_step(event):
        if event.get("kind") == "wait_for":
            ctx.bus.publish(Topics.VISION_GESTURE, {"gesture": "motion", "confidence": 0.9})  # ignored
            ctx.bus.publish(Topics.VISION_GESTURE, {"gesture": "WAVE", "confidence": 0.8})

    ctx.bus.subscribe(Topics.ACTIVITY_STEP, on_step)
    cleared = collect(ctx.bus, [Topics.ACTIVITY_PROMPT_CLEARED])
    await run(engine, "wave")
    assert ctx.audio.spoken == ["waved"]
    assert cleared and cleared[0].data.get("timed_out") is None  # caregiver prompt withdrawn


async def test_wait_for_satisfied_by_caregiver_prompt(ctx):
    engine = make_engine(ctx, WAVE)
    prompts = auto_answer(ctx, lambda options: options[0])
    await run(engine, "wave")
    assert prompts[0]["options"] == ["Waved!"] and prompts[0]["kind"] == "wait_for"
    assert ctx.audio.spoken == ["waved"]


async def test_wait_for_skips_waiting_when_camera_is_off(slow_ctx):
    ctx = slow_ctx
    ctx.privacy.set(camera_enabled=False)
    engine = make_engine(ctx, WAITER)
    started = asyncio.get_running_loop().time()
    await engine.start("waiter")
    assert await engine.wait_until_finished(2)
    assert asyncio.get_running_loop().time() - started < 1.0  # did not wait the full 5 s
    assert ctx.audio.spoken == ["not seen"]


# ----------------------------------------------------------------- listen
LISTENER = activity_yaml(steps("""
    - listen: 5
      expect: ["{favourite_animal}"]
      store_as: heard
      on_match: [{say: "matched {heard}"}]
      on_heard: [{say: heard something}]
      on_timeout: [{say: nothing heard}]
"""), id="listener")


async def test_listen_match_heard_and_timeout(ctx):
    ctx.settings.set("profile", {"favourite_animals": ["cow"]})
    engine = make_engine(ctx, LISTENER)
    ctx.audio.inject_utterance("the cow says moo")
    await run(engine, "listener")
    ctx.audio.inject_utterance("banana")
    await run(engine, "listener")
    await run(engine, "listener")
    assert ctx.audio.spoken == ["matched the cow says moo", "heard something", "nothing heard"]


async def test_listen_with_microphone_disabled_is_a_timeout(ctx):
    ctx.privacy.set(microphone_enabled=False)
    engine = make_engine(ctx, LISTENER)
    ctx.audio.inject_utterance("cow")
    await run(engine, "listener")
    assert ctx.audio.spoken == ["nothing heard"]
    assert not ctx.audio.called("listen")


# ----------------------------------------------------------------- placeholders / variables
PERSONAL = activity_yaml(steps("""
    - say: "Hi {child_name}! I love {favourite_animal}s."
    - say: "For {child_name|baby} and me, marked with {child_initial}."
    - pick: animal
      from: animals
      prefer_profile: favourite_animals
    - say: "{animal.name} says {animal.sound}"
    - set: {colour: blue, line: "{child_name} likes blue"}
    - say: "{line}"
"""), id="personal")


async def test_placeholders_filled_from_profile(ctx):
    ctx.settings.set("profile", {"nickname": "Mia", "favourite_animals": ["Kitty"]})
    engine = make_engine(ctx, PERSONAL)
    await run(engine, "personal")
    assert ctx.audio.spoken == ["Hi Mia! I love Kittys.", "For Mia and me, marked with M.",
                                "Cat says meow", "Mia likes blue"]


async def test_placeholders_fall_back_safely(ctx):
    engine = make_engine(ctx, PERSONAL)
    await run(engine, "personal")
    spoken = ctx.audio.spoken
    assert spoken[0] == "Hi friend! I love puppys."
    assert spoken[1] == "For baby and me, marked with B."
    assert spoken[3] == "Friend likes blue"
    assert all("{" not in s for s in spoken)


async def test_profile_text_is_sanitised(ctx):
    ctx.settings.set("profile", {"nickname": "  {robot_name}\nMia  "})
    engine = make_engine(ctx, PERSONAL)
    await run(engine, "personal")
    assert ctx.audio.spoken[0].startswith("Hi robot_nameMia!")  # braces/control chars stripped, never re-rendered


async def test_seeded_choices_are_reproducible():
    from littlebot.testing import make_test_context

    script = activity_yaml(steps("""
        - repeat: 6
          steps:
            - choice:
                - say: a
                - say: b
                - say: c
              avoid_repeats: false
            - pick: x
              from: [red, blue, green, pink]
            - say: "{x}"
    """), id="rnd")
    results = []
    for _ in range(2):
        c = make_test_context()
        engine = make_engine(c, script, seed=42)
        await run(engine, "rnd")
        results.append(list(c.audio.spoken))
    assert results[0] == results[1]


async def test_choice_and_pick_avoid_repeats(ctx):
    script = activity_yaml(steps("""
        - repeat: 3
          steps:
            - choice:
                - say: a
                - say: b
                - say: c
        - repeat: 4
          steps:
            - pick: x
              from: [red, blue, green, pink]
            - say: "{x}"
    """), id="norepeat")
    engine = make_engine(ctx, script)
    await run(engine, "norepeat")
    assert sorted(ctx.audio.spoken[:3]) == ["a", "b", "c"]
    assert sorted(ctx.audio.spoken[3:]) == ["Blue", "Green", "Pink", "Red"]  # leading placeholder capitalised


async def test_for_each_and_round(ctx):
    script = activity_yaml(steps("""
        - for_each: n
          in: [5, 4, 3]
          steps: [{say: "{round}:{n}"}]
        - repeat: 2
          steps: [{say: "r{round}"}]
    """), id="loops")
    engine = make_engine(ctx, script)
    await run(engine, "loops")
    assert ctx.audio.spoken == ["1:5", "2:4", "3:3", "r1", "r2"]


async def test_end_step_completes_early(ctx):
    script = activity_yaml(steps("""
        - say: before
        - end: finished early
        - say: never
    """), id="ender")
    engine = make_engine(ctx, script)
    await run(engine, "ender")
    assert ctx.audio.spoken == ["before"]
    fin = finished_events(ctx)[-1]
    assert fin.data["completed"] is True and fin.data["reason"] == "finished early"


# ----------------------------------------------------------------- devices: missing / failing / estop
DEVICES = activity_yaml(steps("""
    - say: hi
      expression: happy
    - close_eyes
    - open_eyes
    - sound: pop
    - melody: "C4 E4 G4"
    - gesture: wave
      wait: true
    - gesture: dance
    - look: left
    - listen: 2
    - song: Tune
      lines: [{words: la la, notes: "C4 D4"}]
    - say: done
"""), id="devices")


async def test_runs_without_any_devices(ctx):
    ctx.face = ctx.audio = ctx.motion = None
    engine = make_engine(ctx, DEVICES)
    await run(engine, "devices")
    assert finished_events(ctx)[-1].data["completed"] is True
    assert ctx.state.state == RobotState.IDLE


async def test_failing_devices_do_not_crash_the_activity(ctx, caplog):
    class BrokenAudio(FakeAudio):
        async def say(self, text, *, rate=None):
            raise RuntimeError("speaker unplugged")

        async def play_sound(self, name, *, wait=True, volume=None):
            raise OSError("no device")

        async def play_melody(self, notes, *, tempo_bpm=100, wait=True):
            raise RuntimeError("synth broke")

        async def listen(self, *, timeout=None):
            raise RuntimeError("mic broke")

        def stop_output(self):
            raise RuntimeError("cannot stop")

    class BrokenFace:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("face broke")
            return boom

    class BrokenMotion:
        def status(self):
            return {"estop": False}

        def gestures(self):
            return ["wave", "dance"]

        async def run_gesture(self, name):
            raise RuntimeError("servo jammed")

        def cancel(self):
            raise RuntimeError("cannot cancel")

    ctx.audio = BrokenAudio(ctx.bus)
    ctx.face = BrokenFace()
    ctx.motion = BrokenMotion()
    engine = make_engine(ctx, DEVICES)
    with caplog.at_level("WARNING", logger="littlebot.activities"):
        await run(engine, "devices")
    assert finished_events(ctx)[-1].data["completed"] is True
    assert "audio.say failed" in caplog.text
    assert ctx.state.state == RobotState.IDLE
    # stopping a run with broken devices is also safe
    await engine.start("devices")
    await engine.stop("parent stop")
    assert ctx.state.state == RobotState.IDLE


async def test_gestures_are_optional_and_respect_estop(ctx):
    engine = make_engine(ctx, DEVICES)
    await run(engine, "devices")
    assert [a[0][0] for a in ctx.motion.called("run_gesture")] == ["wave", "dance"]
    ctx.motion.calls.clear()
    ctx.motion.emergency_stop("test")
    await run(engine, "devices")
    assert ctx.motion.called("run_gesture") == []


# ----------------------------------------------------------------- catalogue
CATALOGUE = [
    activity_yaml(steps("- say: a"), id="baby_game", min_age=6, max_age=18, category="social"),
    activity_yaml(steps("- say: b"), id="toddler_game", min_age=18, max_age=36, category="music"),
    activity_yaml(steps("- say: c"), id="preschool_game", min_age=36, max_age=72, category="music"),
    activity_yaml(steps("- say: d"), id="wide_game", min_age=6, max_age=72, category="cognitive"),
]


def test_list_filters_by_age_and_category(ctx):
    engine = make_engine(ctx, *CATALOGUE)
    assert {a["id"] for a in engine.list(age_months=10)} == {"baby_game", "wide_game"}
    assert {a["id"] for a in engine.list(age_months=18)} == {"toddler_game", "wide_game"}  # max is exclusive
    assert {a["id"] for a in engine.list(age_months=48, category="music")} == {"preschool_game"}
    assert engine.list(category="no_such_category") == []
    every = engine.list()
    assert len(every) == 4
    assert [a["id"] for a in every][0] in ("baby_game", "wide_game")  # unknown age: youngest first
    item = every[0]
    for key in ("id", "name", "description", "min_age_months", "max_age_months", "age_groups", "category",
                "participation", "parent_instructions", "safety_notes", "duration_seconds", "pausable"):
        assert key in item
    assert engine.list(age_months=10)[0]["suitable"] is True


def test_list_sorts_by_suitability_for_profile_age(ctx):
    ctx.settings.set("profile", {"age_months": 40})
    engine = make_engine(ctx, *CATALOGUE)
    ids = [a["id"] for a in engine.list()]
    assert ids[:2] == ["preschool_game", "wide_game"]
    assert {a["id"]: a["suitable"] for a in engine.list()}["baby_game"] is False


def test_preferences_hide_from_recommended_but_still_startable(ctx):
    ctx.settings.set("profile", {"age_months": 12, "activity_preferences": {
        "baby_game": {"enabled": False}, "wide_game": {"favourite": True}}})
    engine = make_engine(ctx, *CATALOGUE)
    rec = [a["id"] for a in engine.recommended()]
    assert rec == ["wide_game"]
    listed = {a["id"]: a for a in engine.list(age_months=12)}
    assert listed["baby_game"]["enabled"] is False and listed["wide_game"]["favourite"] is True
    assert engine.get("baby_game") is not None


async def test_disabled_activity_can_still_be_started(ctx):
    ctx.settings.set("profile", {"activity_preferences": {"baby_game": {"enabled": False}}})
    engine = make_engine(ctx, *CATALOGUE)
    await run(engine, "baby_game")
    assert ctx.audio.spoken == ["a"]


def test_get_returns_documented_dict(ctx):
    engine = make_engine(ctx, *CATALOGUE)
    item = engine.get("baby_game")
    assert item["age_groups"] == ["6–12 months", "12–18 months"]
    assert item["age_range"] == "6–18 months"
    assert item["step_count"] == 1 and item["outline"] == [{"kind": "say", "text": "a"}]
    assert engine.get("nope") is None


# ----------------------------------------------------------------- history (opt-in)
async def test_history_not_recorded_by_default(ctx):
    engine = make_engine(ctx, SIMPLE)
    assert ctx.privacy.record_history is False
    await run(engine, "simple")
    assert engine.history() == []
    assert ctx.db.query_one("SELECT name FROM sqlite_master WHERE name=?", (HISTORY_TABLE,)) is None


async def test_history_recorded_when_enabled_and_clearable(slow_ctx):
    ctx = slow_ctx
    ctx.privacy.set(record_history=True)
    engine = make_engine(ctx, SIMPLE, LONG)
    await engine.start("simple")
    assert await engine.wait_until_finished(5)
    await engine.start("long_one")
    await engine.stop("parent stop")
    rows = engine.history()
    assert [(r["activity_id"], r["completed"], r["reason"]) for r in rows] == [
        ("long_one", 0, "parent stop"), ("simple", 1, "completed")]
    assert set(rows[0]) == {"activity_id", "started", "finished", "completed", "reason", "source"}
    assert engine.clear_history() == 2
    assert engine.history() == []


# ----------------------------------------------------------------- peekaboo (real script)
async def test_peekaboo_closes_then_reveals_with_sound(timeline_ctx):
    ctx = timeline_ctx
    engine = create_service(ctx)
    ctx.activities = engine
    auto_answer(ctx, lambda options: options[0])
    await run(engine, "peekaboo")
    tl = ctx.extras["timeline"].events
    first_close = tl.index(("face.close_eyes", None))
    after = tl[first_close:]
    open_i = after.index(("face.open_eyes", None))
    surprised_i = after.index(("face.set_expression", "surprised"))
    sound_i = after.index(("audio.play_sound", "peekaboo"))
    happy_i = after.index(("face.set_expression", "happy"))
    assert open_i < surprised_i < sound_i < happy_i
    assert tl.count(("face.close_eyes", None)) == 3
    assert ctx.audio.sounds.count("peekaboo") >= 3
    assert any("Peekaboo" in s for s in ctx.audio.spoken)
    assert not ctx.face.eyes_closed


async def test_stop_immediately_after_start_still_cleans_up(ctx):
    """Cancelling before the first step runs must still finish the run (task never entered)."""
    engine = make_engine(ctx, SIMPLE)
    seen = collect(ctx.bus, [Topics.ACTIVITY_FINISHED])
    await engine.start("simple")
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    assert await engine.wait_until_finished(2)
    assert [e.data["reason"] for e in seen] == ["stop all"]
    assert ctx.state.state == RobotState.IDLE and ctx.arbiter.is_free(Resource.FACE)
    assert ctx.audio.spoken == []


def test_runs_on_the_robot_runtime_thread():
    """Through the composition root: start/respond/stop_all from the main thread, engine on the loop thread."""
    import time

    from littlebot.app.robot import LittleBot
    from littlebot.testing import make_test_context

    ctx = make_test_context(activities={"time_scale": 0.05}, dashboard={"enabled": False})
    bot = LittleBot(ctx)
    bot.build_subsystems(only={"activities"})
    assert isinstance(ctx.activities, ActivityEngine)
    bot.start()
    try:
        status = bot.runtime.run(ctx.activities.start("clapping_game", source="test"))
        assert status["running"] and ctx.state.state == RobotState.PLAYING_ACTIVITY
        deadline = time.time() + 5
        while time.time() < deadline and ctx.activities.status().get("prompt") is None:
            time.sleep(0.01)
        prompt = ctx.activities.status()["prompt"]
        assert prompt and ctx.activities.respond(prompt["options"][0], prompt["prompt_id"])
        result = bot.runtime.run(bot.stop_all("parent"))
        assert result["activities"]["running"] is False
        assert result["activities"]["last"]["reason"] == "stop all"
        assert result["services"]["activities"]["status"] == "running"
        bot.runtime.run(ctx.activities.start("peekaboo"))
    finally:
        bot.shutdown("test")  # stops the running activity cleanly
    assert ctx.state.state == RobotState.IDLE
