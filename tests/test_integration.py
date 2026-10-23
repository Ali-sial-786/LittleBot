"""End-to-end integration: the REAL subsystems wired together (mock devices, headless).

These tests build the whole robot through the composition root exactly like
``python -m littlebot`` does (minus the window and the HTTP listener) and check
that the modules cooperate: activities drive the face and audio, the camera
drives the gaze, conversation drives the state machine, privacy switches turn
devices off, STOP calms everything and shutdown leaves no threads behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import pytest

from littlebot.app.robot import LittleBot
from littlebot.core.context import build_core_context
from littlebot.core.events import Topics, collect
from littlebot.core.state import RobotState
from littlebot.hardware.system import MockSystemMonitor
from littlebot.testing import make_test_config


def wait_until(pred: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(interval)
    return pred()


def build_robot(**overrides: Any) -> LittleBot:
    overrides.setdefault("dashboard", {"enabled": False})
    cfg = make_test_config(**overrides)
    ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
    bot = LittleBot(ctx)
    bot.build_subsystems()
    return bot


@pytest.fixture
def robot():
    bot = build_robot()
    bot.start()
    yield bot
    bot.shutdown("test end")


def test_all_subsystems_build_and_report_status(robot: LittleBot):
    ctx = robot.ctx
    for name in ("face", "audio", "vision", "motion", "media", "brain", "activities"):
        assert getattr(ctx, name) is not None, f"{name} did not build"
    st = robot.status()
    for name in ("face", "audio", "vision", "motion", "media", "brain", "activities"):
        assert st[name].get("status") != "unavailable", name
        assert "error" != st[name].get("status"), (name, st[name])
    assert st["face"]["expression"] == "neutral"
    assert st["motion"]["simulation_only"] is True
    assert st["state"]["state"] == "IDLE"
    healthy = lambda: all(h["status"] in ("running", "degraded")  # noqa: E731
                          for h in robot.status()["services"].values())
    assert wait_until(healthy, 5), robot.status()["services"]


def test_peekaboo_drives_real_face_and_audio(robot: LittleBot):
    ctx = robot.ctx
    seen = collect(ctx.bus, ["face.*", "audio.*", "activity.*", Topics.STATE_CHANGED])
    robot.runtime.run(ctx.activities.start("peekaboo"))
    assert robot.runtime.run(ctx.activities.wait_until_finished(20), timeout=30)
    topics = [e.topic for e in seen]
    assert Topics.ACTIVITY_STARTED in topics and Topics.ACTIVITY_FINISHED in topics
    finished = [e for e in seen if e.topic == Topics.ACTIVITY_FINISHED][-1]
    assert finished.data["completed"] is True
    # Activities run instantly here (time_scale 0) but the headless face applies queued commands on its
    # 10 Hz ticker, so its expression events can arrive just after the activity ends.
    wait_until(lambda: any(e.topic == Topics.FACE_EXPRESSION for e in seen), timeout=3)
    expressions = [e.data["expression"] for e in seen if e.topic == Topics.FACE_EXPRESSION]
    assert "surprised" in expressions or "happy" in expressions
    sounds = [e.data["name"] for e in seen if e.topic == Topics.AUDIO_SOUND_STARTED]
    assert "peekaboo" in sounds
    assert any(e.topic == Topics.AUDIO_SPEECH_STARTED for e in seen)
    states = [e.data["new"] for e in seen if e.topic == Topics.STATE_CHANGED]
    assert states[0] == "PLAYING_ACTIVITY" and states[-1] == "IDLE"
    assert ctx.face.status()["eyes_closed"] is False
    assert ctx.arbiter.snapshot()["face"] is None  # lease released


def test_every_activity_runs_on_the_real_robot(robot: LittleBot):
    """Each scripted activity completes against the real face/audio/motion (prompts auto-skip)."""
    ctx = robot.ctx
    ids = [a["id"] for a in ctx.activities.list()]
    assert len(ids) >= 25

    def answer(event):
        ctx.activities.respond("skip", event.data.get("prompt_id"))

    sub = ctx.bus.subscribe(Topics.ACTIVITY_PROMPT, answer)
    try:
        for aid in ids:
            robot.runtime.run(ctx.activities.start(aid))
            ok = robot.runtime.run(ctx.activities.wait_until_finished(20), timeout=30)
            assert ok, f"{aid} did not finish"
            assert not ctx.activities.status()["running"], aid
    finally:
        sub.cancel()
    assert ctx.state.state == RobotState.IDLE


def test_camera_face_moves_the_eyes(robot: LittleBot):
    ctx = robot.ctx
    seen = collect(ctx.bus, [Topics.VISION_FACES, Topics.VISION_PERSON_PRESENT])
    # A face on the left of the camera image appears on the robot's right -> gaze_x > 0.5 (mirrored).
    for _ in range(20):
        ctx.vision.simulate_face(0.15, 0.5)
        time.sleep(0.05)
    assert wait_until(lambda: any(e.topic == Topics.VISION_FACES for e in seen), 3)
    face_ev = [e for e in seen if e.topic == Topics.VISION_FACES][-1]
    target_x = face_ev.data["faces"][0]["gaze_x"]
    assert target_x > 0.6  # mirrored
    gaze = wait_until(lambda: ctx.face.status()["gaze"]["x"] > 0.58 and ctx.face.status()["gaze"], 4)
    assert gaze, ctx.face.status()
    assert wait_until(lambda: any(e.topic == Topics.VISION_PERSON_PRESENT for e in seen), 3)


def test_mock_conversation_drives_states_and_speech(robot: LittleBot):
    ctx = robot.ctx
    seen = collect(ctx.bus, [Topics.STATE_CHANGED, Topics.BRAIN_REPLY, Topics.AUDIO_SPEECH_STARTED])
    result = robot.runtime.run(ctx.brain.handle_utterance("what does a cow say?", source="simulated"))
    assert result["reply"] and result["provider"] == "mock"
    states = [e.data["new"] for e in seen if e.topic == Topics.STATE_CHANGED]
    assert "THINKING" in states and "SPEAKING" in states and states[-1] == "IDLE"
    assert any(e.topic == Topics.BRAIN_REPLY for e in seen)
    assert any(e.topic == Topics.AUDIO_SPEECH_STARTED for e in seen)
    st = ctx.brain.status()
    assert st["active_provider"] == "mock" and st["cloud_allowed"] is False


def test_push_to_talk_with_simulated_microphone(robot: LittleBot):
    ctx = robot.ctx
    threading.Timer(0.2, ctx.audio.inject_utterance, args=("hello littlebot",)).start()
    result = robot.runtime.run(ctx.brain.converse_once(), timeout=30)
    assert result.get("heard") == "hello littlebot"
    assert result.get("reply")
    assert ctx.state.state == RobotState.IDLE


def test_conversation_refused_while_activity_holds_speaker(robot: LittleBot):
    ctx = robot.ctx
    lease = ctx.arbiter.acquire("activity:test", ["face", "audio_out"], 50)
    try:
        result = robot.runtime.run(ctx.brain.handle_utterance("hi"))
        assert result.get("error") == "busy"
    finally:
        assert lease is not None
        lease.release()


def test_video_hides_face_and_face_returns_when_video_ends(robot: LittleBot):
    ctx = robot.ctx
    ctx.settings.set("profile", {"age_months": 30})
    item = ctx.media.add_item("https://www.youtube.com/watch?v=jNQXAC9IVRw", title="Approved video")
    with pytest.raises(ValueError):
        ctx.media.add_item("https://example.com/watch?v=jNQXAC9IVRw")
    seen = collect(ctx.bus, ["media.*", Topics.FACE_VISIBLE, Topics.STATE_CHANGED])
    robot.runtime.run(ctx.media.play(item["id"]))
    assert ctx.state.state == RobotState.PLAYING_MEDIA
    assert wait_until(lambda: ctx.face.status()["visible"] is False, 3), ctx.face.status()
    # The robot's own player page polls the bridge and reports the video ended.
    desired = ctx.media.player_state()
    assert desired["item"]["video_id"] == "jNQXAC9IVRw" and desired["action"] == "play"
    robot.runtime.run(ctx.media.handle_player_event({"type": "playing", "load_id": desired["load_id"]}))
    assert ctx.media.status()["state"] == "playing"
    robot.runtime.run(ctx.media.handle_player_event({"type": "ended", "load_id": desired["load_id"]}))
    assert wait_until(lambda: ctx.state.state == RobotState.IDLE, 3)
    assert wait_until(lambda: ctx.face.status()["visible"] is True, 3), ctx.face.status()
    stopped = [e for e in seen if e.topic == Topics.MEDIA_STOPPED]
    assert stopped and stopped[-1].data["reason"] == "ended"
    assert all(v is None for v in ctx.arbiter.snapshot().values())


def test_video_blocked_for_infants_without_parent_authorisation(robot: LittleBot):
    ctx = robot.ctx
    ctx.settings.set("profile", {"age_months": 10})
    item = ctx.media.add_item("https://youtu.be/jNQXAC9IVRw")
    with pytest.raises(PermissionError):
        robot.runtime.run(ctx.media.play(item["id"]))
    assert ctx.state.state == RobotState.IDLE
    robot.runtime.run(ctx.media.play(item["id"], parent_override=True))
    assert ctx.state.state == RobotState.PLAYING_MEDIA
    robot.runtime.run(ctx.media.stop("stopped"))
    assert ctx.state.state == RobotState.IDLE


def test_motion_gesture_and_emergency_stop(robot: LittleBot):
    ctx = robot.ctx
    seen = collect(ctx.bus, ["motion.*"])
    assert robot.runtime.run(ctx.motion.run_gesture("wave"), timeout=30)
    assert any(e.topic == Topics.MOTION_POSITIONS for e in seen)
    assert any(e.topic == Topics.MOTION_GESTURE_FINISHED for e in seen)
    ctx.motion.emergency_stop("test")
    assert ctx.motion.status()["estop"] is True
    assert robot.runtime.run(ctx.motion.run_gesture("wave"), timeout=10) is False
    assert ctx.motion.reset_emergency_stop()
    assert ctx.motion.status()["estop"] is False


def test_stop_all_interrupts_a_running_activity(robot: LittleBot):
    ctx = robot.ctx
    # Real-time activity so it is still running when we press STOP.
    ctx.clock.scale = 1.0
    robot.runtime.run(ctx.activities.start("peekaboo"))
    assert wait_until(lambda: ctx.activities.status()["running"], 2)
    robot.runtime.run(robot.stop_all("test"))
    assert wait_until(lambda: not ctx.activities.status()["running"], 3)
    assert ctx.state.state == RobotState.IDLE
    assert ctx.face.status()["eyes_closed"] is False
    assert all(v is None for v in ctx.arbiter.snapshot().values())


def test_privacy_switches_turn_devices_off(robot: LittleBot):
    ctx = robot.ctx
    ctx.privacy.set(camera_enabled=False, microphone_enabled=False)
    assert wait_until(lambda: ctx.vision.status()["camera"] == "disabled", 3), ctx.vision.status()
    ctx.audio.inject_utterance("are you there")
    assert robot.runtime.run(ctx.audio.listen(timeout=0.5)) is None
    assert ctx.audio.status()["microphone"]["status"] == "disabled"
    ctx.privacy.set(camera_enabled=True)
    assert wait_until(lambda: ctx.vision.status()["camera"] == "ok", 5), ctx.vision.status()


def test_sleep_and_wake(robot: LittleBot):
    ctx = robot.ctx
    assert robot.sleep()
    assert wait_until(lambda: ctx.face.status()["state"] == "sleeping", 2)
    ctx.vision.simulate_face(0.5, 0.5)
    assert wait_until(lambda: ctx.state.state == RobotState.IDLE, 5)


def test_missing_real_devices_degrade_without_crashing():
    bot = build_robot(
        camera={"backend": "real", "device": 7},
        audio={"microphone": {"backend": "real"}, "speaker": {"backend": "sounddevice"}},
    )
    bot.start()
    try:
        assert wait_until(lambda: bot.status()["vision"]["camera"] in ("unavailable", "disconnected"), 5), \
            bot.status()["vision"]
        st = bot.status()
        assert st["audio"]["speaker"]["status"] in ("unavailable", "disconnected")
        assert st["audio"]["microphone"]["status"] in ("unavailable", "disconnected")
        # The rest of the robot still works.
        bot.runtime.run(bot.ctx.activities.start("peekaboo"))
        assert bot.runtime.run(bot.ctx.activities.wait_until_finished(20), timeout=30)
    finally:
        bot.shutdown("test")


def test_offline_mode_never_uses_cloud():
    bot = build_robot(mode="offline", ai={"provider": "anthropic"})
    bot.start()
    try:
        ctx = bot.ctx
        with pytest.raises(PermissionError):
            ctx.privacy.set(cloud_enabled=True)
        ctx.settings.set("ai.enabled", True)
        st = ctx.brain.status()
        assert st["active_provider"] == "mock" and st["cloud_allowed"] is False
        assert "offline" in st["reason"].lower()
    finally:
        bot.shutdown("test")


def test_shutdown_leaves_no_littlebot_threads():
    before = {t.ident for t in threading.enumerate()}
    bot = build_robot()
    bot.start()
    bot.runtime.run(bot.ctx.motion.run_gesture("wave"), timeout=30)
    bot.shutdown("test")
    leftover = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
    assert wait_until(lambda: not [t for t in threading.enumerate() if t.ident not in before and t.is_alive()], 5), leftover


@pytest.mark.slow
def test_cli_offline_headless_smoke(tmp_path):
    """`python -m littlebot --mode offline --headless --run-for N` starts and exits cleanly."""
    env = dict(os.environ, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy", LITTLEBOT__DASHBOARD__PORT="0")
    proc = subprocess.run(
        [sys.executable, "-m", "littlebot", "--mode", "offline", "--headless", "--run-for", "4",
         "--data-dir", str(tmp_path), "--no-dashboard"],
        capture_output=True, text=True, timeout=90, env=env,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "started in offline mode" in proc.stderr
    assert "shutdown complete" in proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr[-3000:]
