"""FaceController: FaceAPI contract, bus integration, thread-safety, headless ticker, fps policy."""

from __future__ import annotations

import json
import logging
import random
import threading
import time

import pytest
from face_stubs import step

from littlebot.core.events import Topics, collect
from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS, FACE_STATES, FaceAPI
from littlebot.face import controller as C
from littlebot.face import create_service
from littlebot.face.controller import FaceController


def test_create_service_returns_face_api(face_ctx):
    svc = create_service(face_ctx)
    assert isinstance(svc, FaceController)
    assert isinstance(svc, FaceAPI)
    assert callable(svc.track_gaze)
    h = svc.health()
    assert h["name"] == "face" and h["status"] == "stopped"


def test_importing_package_does_not_import_pygame():
    import subprocess
    import sys

    code = ("import sys, littlebot.face, littlebot.face.controller, littlebot.face.animator; "
            "print('pygame' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stderr


def test_status_shape(controller):
    st = controller.status()
    assert set(st) >= {"expression", "state", "gaze", "eyes_closed", "fps", "visible"}
    assert st["expression"] == "neutral" and st["state"] == "idle"
    assert set(st["gaze"]) == {"x", "y"}
    assert 0 <= st["gaze"]["x"] <= 1 and 0 <= st["gaze"]["y"] <= 1
    assert isinstance(st["eyes_closed"], bool) and isinstance(st["visible"], bool)
    assert isinstance(st["fps"], float)
    json.dumps(st)


def test_status_reflects_commands_immediately(controller):
    controller.set_expression("happy")
    controller.set_state("listening")
    controller.close_eyes()
    st = controller.status()
    # set_state after set_expression resets to the state's default expression
    assert st["expression"] == "listening" and st["state"] == "listening" and st["eyes_closed"] is True
    controller.open_eyes()
    controller.set_expression("sad")
    st = controller.status()
    assert st["expression"] == "sad" and st["eyes_closed"] is False


def test_api_methods_only_enqueue(controller):
    t0 = controller.animator.t
    for e in EXPRESSIONS:
        controller.set_expression(e)
    controller.look_at(0.2, 0.3)
    assert controller.animator.expression == "neutral"  # not applied yet
    assert len(controller._cmds) == len(EXPRESSIONS) + 1  # noqa: SLF001
    controller.update(0.01)
    assert controller.animator.expression == EXPRESSIONS[-1]
    assert controller.animator.t == pytest.approx(t0 + 0.01)


def test_unknown_names_are_logged_and_ignored(controller, caplog):
    with caplog.at_level(logging.WARNING, logger="littlebot.face"):
        controller.set_expression("grumpy")
        controller.play_animation("explode")
        controller.set_state("dancing")
        controller.look_at(float("nan"), 0.5)
        controller.set_expression("happy", hold=float("inf"))
    assert "grumpy" in caplog.text and "explode" in caplog.text and "dancing" in caplog.text
    st = controller.status()
    assert st["expression"] == "happy" and st["state"] == "idle" and st["animations"] == []


def test_face_events_published(controller, face_ctx):
    seen = collect(face_ctx.bus, ["face.*"])
    controller.set_expression("happy")
    controller.set_expression("happy")
    controller.update(0.01)
    controller.set_state("hidden")
    controller.update(0.01)
    controller.set_state("idle")
    controller.update(0.01)
    exprs = [e.data["expression"] for e in seen if e.topic == Topics.FACE_EXPRESSION]
    vis = [e.data["visible"] for e in seen if e.topic == Topics.FACE_VISIBLE]
    assert exprs == ["happy", "neutral", "neutral"][: len(exprs)] and exprs[0] == "happy"
    assert vis == [False, True]
    assert all(e.source == "face" for e in seen)


def test_hold_reverts_via_controller(controller, face_ctx):
    seen = collect(face_ctx.bus, [Topics.FACE_EXPRESSION])
    controller.set_expression("surprised", hold=0.5)
    step(controller, 0.4)
    assert controller.status()["expression"] == "surprised"
    step(controller, 0.2)
    assert controller.status()["expression"] == "neutral"
    assert [e.data["expression"] for e in seen] == ["surprised", "neutral"]


def test_track_gaze_is_soft_and_look_at_wins(controller):
    for _ in range(40):
        controller.track_gaze(0.9, 0.5)
        controller.update(1 / 30)
    assert controller.status()["gaze"]["x"] == pytest.approx(0.9, abs=0.06)
    controller.look_at(0.1, 0.5, hold=1.0)
    for _ in range(20):
        controller.track_gaze(0.9, 0.5)
        controller.update(1 / 30)
    assert controller.status()["gaze"]["x"] == pytest.approx(0.1, abs=0.06)
    for _ in range(40):
        controller.track_gaze(0.9, 0.5)
        controller.update(1 / 30)
    assert controller.status()["gaze"]["x"] == pytest.approx(0.9, abs=0.06)


def test_speaking_responds_to_bus_events(controller, face_ctx):
    bus = face_ctx.bus
    bus.publish(Topics.AUDIO_SPEECH_STARTED, {"text": "hi", "duration_estimate": 1.0})
    controller.update(1 / 30)
    assert controller.status()["talking"] is True
    for _ in range(10):
        bus.publish(Topics.AUDIO_VISEME, {"level": 1.0})
        controller.update(1 / 60)
    assert controller.animator.level > 0.8
    assert controller.frame().mouth is not None
    bus.publish(Topics.AUDIO_VISEME, {"level": "garbage"})
    bus.publish(Topics.AUDIO_SPEECH_FINISHED, {"text": "hi", "interrupted": False})
    step(controller, 0.5)
    assert controller.status()["talking"] is False
    assert controller.animator.level < 0.05


def test_listening_responds_to_bus_events(controller, face_ctx):
    face_ctx.bus.publish(Topics.AUDIO_LISTENING_STARTED, {})
    assert controller.status()["listening"] is True
    face_ctx.bus.publish(Topics.AUDIO_LISTENING_STOPPED, {})
    assert controller.status()["listening"] is False


def test_thread_safety_smoke(controller):
    errors: list[BaseException] = []
    stop = threading.Event()

    def hammer(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(400):
                r = rng.random()
                if r < 0.2:
                    controller.set_expression(rng.choice(EXPRESSIONS), hold=rng.choice([None, 0.2]))
                elif r < 0.3:
                    controller.set_state(rng.choice(FACE_STATES))
                elif r < 0.45:
                    controller.look_at(rng.random(), rng.random(), hold=0.3)
                elif r < 0.55:
                    controller.track_gaze(rng.random(), rng.random())
                elif r < 0.62:
                    controller.play_animation(rng.choice(FACE_ANIMATIONS))
                elif r < 0.7:
                    controller.blink(rng.random() < 0.5)
                elif r < 0.78:
                    (controller.close_eyes if rng.random() < 0.5 else controller.open_eyes)()
                elif r < 0.85:
                    controller.clear_gaze()
                else:
                    st = controller.status()
                    assert st["expression"] in EXPRESSIONS
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def driver() -> None:
        try:
            while not stop.is_set():
                controller.update(1 / 60)
                controller.frame()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    d = threading.Thread(target=driver)
    d.start()
    workers = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(30)
    stop.set()
    d.join(10)
    assert not errors, errors
    st = controller.status()
    assert st["state"] in FACE_STATES and st["expression"] in EXPRESSIONS
    assert controller.dropped_commands == 0 or controller.dropped_commands < 3200


def test_command_queue_is_bounded(controller):
    for _ in range(C.MAX_COMMANDS + 50):
        controller.blink()
    assert len(controller._cmds) <= C.MAX_COMMANDS  # noqa: SLF001
    assert controller.dropped_commands == 50


async def test_headless_ticker_start_stop_and_display_takeover(controller):
    await controller.start()
    try:
        assert controller.ticker_running
        assert controller.health()["status"] == "running"
        t0 = controller.animator.t
        time.sleep(0.35)
        t1 = controller.animator.t
        assert t1 - t0 > 0.15  # ~10 Hz updates happened
        controller.set_expression("happy")
        time.sleep(0.25)
        assert controller.animator.expression == "happy"  # applied by the ticker
        controller.attach_display()
        time.sleep(0.15)  # let an in-flight tick finish
        t2 = controller.animator.t
        time.sleep(0.3)
        assert controller.animator.t == t2  # display owns update() now
        controller.detach_display()
        time.sleep(0.3)
        assert controller.animator.t > t2
    finally:
        await controller.stop()
    assert not controller.ticker_running
    assert controller.health()["status"] == "stopped"
    t3 = controller.animator.t
    time.sleep(0.25)
    assert controller.animator.t == t3


async def test_stop_is_idempotent(controller):
    await controller.stop()
    await controller.start()
    await controller.stop()
    await controller.stop()
    assert not controller.ticker_running


def test_fps_policy(controller, face_ctx):
    cfg = face_ctx.config.display
    controller.animator.cfg = controller.animator.cfg.model_copy(update={"idle_gaze": False})
    controller.animator.next_blink = 1e9
    step(controller, 1.5)
    assert controller.wants_high_fps() is False
    assert controller.target_fps() == cfg.idle_fps
    controller.play_animation("bounce")
    controller.update(0.01)
    assert controller.target_fps() == cfg.fps
    step(controller, 1.0)
    controller.set_state("hidden")
    step(controller, 1.0)
    assert controller.target_fps() <= min(C.HIDDEN_FPS, cfg.idle_fps)
    assert controller.status()["visible"] is False


def test_fps_measurement(controller, pygame_display):
    import pygame

    surf = pygame.Surface((480, 320))
    assert controller.fps == 0.0
    for _ in range(10):
        controller.update(1 / 60)
        controller.render(surf)
        time.sleep(0.01)
    assert controller.fps > 5
    assert controller.render_ms > 0
    assert controller.status()["fps"] == controller.fps


def test_robot_builds_real_face(face_ctx):
    """app.robot builds the face via create_service and its status() is the FaceAPI one."""
    from littlebot.app.robot import LittleBot

    bot = LittleBot(face_ctx)
    bot.build_subsystems(only={"face"})
    assert isinstance(face_ctx.face, FaceController)
    st = bot.status()
    assert st["face"]["expression"] == "neutral"
    assert st["services"]["face"]["status"] == "stopped"
    face_ctx.face.stop_ticker()
