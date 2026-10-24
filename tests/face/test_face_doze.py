"""Dozing: the awake / doze decision (pure, fake clock), the controller's bus wiring, and the doze look."""

from __future__ import annotations

import logging

import pytest
from face_stubs import make_animator, step

from littlebot.core.events import Topics
from littlebot.face import controller as C
from littlebot.face.controller import FaceController
from littlebot.face.doze import AWAKE_ON_ARRIVAL_S, DOZE_AFTER_S, DozeTimer
from littlebot.face.frame import MIN_LINE


# ------------------------------------------------------------------ the pure decision
def test_defaults_match_the_parents_request():
    assert DOZE_AFTER_S == 30.0 and AWAKE_ON_ARRIVAL_S == 20.0


def test_dozes_after_30_seconds_of_quiet():
    d = DozeTimer(now=0.0)
    assert d.update(29.9) is None and not d.dozing
    assert d.update(30.0) == ("doze", "nothing happening for 30 s")
    assert d.dozing
    assert d.update(31.0) is None  # only reports changes


def test_someone_walking_in_wakes_it_for_20_seconds():
    d = DozeTimer(now=0.0)
    d.update(40.0)
    assert d.dozing
    d.arrival(40.0)
    assert d.update(40.0) == ("wake", "someone walked in")
    d.update(59.9)
    assert not d.dozing
    assert d.update(60.0) == ("doze", "nothing happening for 30 s")


def test_arrival_does_not_cut_a_longer_awake_spell_short():
    d = DozeTimer(now=0.0)
    d.activity(10.0, "the wake word")
    d.arrival(12.0)  # awake until 32 anyway, then until 40 (10 + 30)
    d.update(39.9)
    assert not d.dozing
    d.update(40.0)
    assert d.dozing


def test_a_still_person_does_not_keep_it_awake_forever():
    d = DozeTimer(now=0.0)
    d.arrival(100.0)  # walked in; then sat still (no more events)
    d.update(119.0)
    assert not d.dozing
    d.update(120.0)
    assert d.dozing
    d.arrival(150.0, "someone moved")  # they moved
    assert d.update(150.0) == ("wake", "someone moved")


def test_a_chat_keeps_it_awake_then_it_dozes_30_seconds_after():
    d = DozeTimer(now=0.0)
    d.begin("chat", 10.0, "a voice chat")
    d.update(500.0)
    assert not d.dozing
    d.end("chat", 500.0)
    d.update(529.9)
    assert not d.dozing
    d.update(530.0)
    assert d.dozing


@pytest.mark.parametrize("key, why", [("card", "a card on the screen"), ("game", "a game"),
                                      ("activity", "an activity"), ("media", "a video")])
def test_something_on_the_screen_never_dozes(key, why):
    d = DozeTimer(now=0.0)
    d.begin(key, 1.0, why)
    for t in (31.0, 600.0, 5000.0):
        d.update(t)
        assert not d.dozing
    assert d.holding(5000.0) == [key]
    d.end(key, 5000.0)
    d.update(5030.0)
    assert d.dozing


def test_touching_wakes_it():
    d = DozeTimer(now=0.0)
    d.update(31.0)
    assert d.dozing
    d.activity(31.0, "someone touched the screen")
    assert d.update(31.0) == ("wake", "someone touched the screen")
    d.update(60.9)
    assert not d.dozing
    d.update(61.0)
    assert d.dozing


def test_switched_off_never_dozes_and_wakes_at_once():
    d = DozeTimer(enabled=False, now=0.0)
    for t in (31.0, 1000.0, 100000.0):
        assert d.update(t) is None and not d.dozing
    d.configure(enabled=True)
    assert d.update(100000.0) == ("doze", "nothing happening for 30 s")
    d.configure(enabled=False)
    assert d.update(100001.0) == ("wake", "dozing switched off")


def test_glance_keeps_it_awake_while_it_plays():
    d = DozeTimer(now=0.0)
    d.update(40.0)
    d.glance(40.0, 5.0, "the face was asked to show something")
    assert d.update(40.0) == ("wake", "the face was asked to show something")
    d.update(44.9)
    assert not d.dozing
    d.update(45.0)
    assert d.dozing


def test_a_lost_finished_event_cannot_keep_it_awake_forever():
    d = DozeTimer(now=0.0)
    d.begin("speech", 0.0, "LittleBot is talking", max_seconds=600.0)
    d.update(629.0)
    assert not d.dozing
    d.update(630.0)
    assert d.dozing and d.holding(630.0) == []


def test_clear_forgets_every_hold():
    d = DozeTimer(now=0.0)
    d.begin("card", 0.0, "a card on the screen")
    d.begin("game", 0.0, "a game")
    d.clear(100.0)
    d.update(129.0)
    assert not d.dozing
    d.update(130.0)
    assert d.dozing


def test_settings_are_clamped():
    d = DozeTimer(doze_after=1, awake_on_arrival=-5)
    assert d.doze_after == 5.0 and d.awake_on_arrival == 0.0
    d.configure(doze_after="nonsense", awake_on_arrival=1e9)
    assert d.doze_after == 5.0 and d.awake_on_arrival == 3600.0
    d.configure(doze_after=float("nan"))
    assert d.doze_after == 5.0
    st = d.status(0.0)
    assert set(st) >= {"enabled", "dozing", "doze_after", "awake_on_arrival", "holding", "quiet_seconds"}


# ------------------------------------------------------------------ the controller's wiring
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def dozer(face_ctx, clock):
    fc = FaceController(face_ctx, seed=1, clock=clock)
    face_ctx.face = fc
    yield fc
    fc.stop_ticker()


def advance(fc: FaceController, clock: Clock, seconds: float, dt: float = 0.25) -> None:
    for _ in range(round(seconds / dt)):
        clock.t += dt
        fc.update(dt)


def test_controller_dozes_and_wakes_with_plain_log_lines(dozer, clock, face_ctx, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.face")
    advance(dozer, clock, 29.0)
    assert not dozer.dozing and dozer.status()["dozing"] is False
    advance(dozer, clock, 1.5)
    assert dozer.dozing and dozer.status()["dozing"] is True
    assert "face: dozing (nothing happening for 30 s)" in caplog.text
    advance(dozer, clock, 10.0)
    st = dozer.status()
    assert st["expression"] == "sleepy" and st["eyes_closed"] is True and st["state"] == "idle"
    face_ctx.bus.publish(Topics.VISION_PERSON_PRESENT, {"gaze_x": 0.5, "gaze_y": 0.5})
    advance(dozer, clock, 0.25)
    assert not dozer.dozing
    assert "face: awake (someone walked in)" in caplog.text
    advance(dozer, clock, 19.0)
    assert not dozer.dozing
    advance(dozer, clock, 1.5)
    assert dozer.dozing


def test_a_voice_chat_keeps_the_face_awake(dozer, clock, face_ctx):
    bus = face_ctx.bus
    bus.publish("audio.wake_word", {"phrase": "hey littlebot"})
    bus.publish("live.call_started", {})
    advance(dozer, clock, 120.0, dt=1.0)
    assert not dozer.dozing
    bus.publish("live.call_ended", {"reason": "goodbye"})
    advance(dozer, clock, 29.0, dt=1.0)
    assert not dozer.dozing
    advance(dozer, clock, 2.0, dt=1.0)
    assert dozer.dozing


def test_busy_robot_states_keep_it_awake(dozer, clock, face_ctx):
    face_ctx.state.transition("LISTENING", "test")
    advance(dozer, clock, 100.0, dt=1.0)
    assert not dozer.dozing
    face_ctx.state.to_idle("test")
    advance(dozer, clock, 31.0, dt=1.0)
    assert dozer.dozing
    face_ctx.state.transition("SPEAKING", "test")
    advance(dozer, clock, 0.25)
    assert not dozer.dozing


@pytest.mark.parametrize("start, end", [
    ("screen.card_shown", "screen.card_closed"), ("game.started", "game.finished"),
    (Topics.ACTIVITY_STARTED, Topics.ACTIVITY_FINISHED), (Topics.MEDIA_STARTED, Topics.MEDIA_STOPPED),
    (Topics.AUDIO_SPEECH_STARTED, Topics.AUDIO_SPEECH_FINISHED), (Topics.AUDIO_SOUND_STARTED, Topics.AUDIO_SOUND_FINISHED),
])
def test_screen_owners_and_sounds_keep_it_awake(dozer, clock, face_ctx, start, end):
    face_ctx.bus.publish(start, {})
    advance(dozer, clock, 300.0, dt=2.0)
    assert not dozer.dozing
    face_ctx.bus.publish(end, {})
    advance(dozer, clock, 28.0, dt=2.0)
    assert not dozer.dozing
    advance(dozer, clock, 4.0, dt=2.0)
    assert dozer.dozing


def test_stop_all_drops_what_was_on(dozer, clock, face_ctx):
    face_ctx.bus.publish("screen.card_shown", {})
    face_ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    advance(dozer, clock, 31.0, dt=1.0)
    assert dozer.dozing


def test_face_commands_wake_it_but_a_calm_reset_does_not(dozer, clock):
    advance(dozer, clock, 31.0, dt=1.0)
    assert dozer.dozing
    dozer.set_expression("neutral")  # STOP ALL calms the face: no reason to wake up
    dozer.open_eyes()
    dozer.track_gaze(0.3, 0.4)  # following a face that sits still
    advance(dozer, clock, 1.0)
    assert dozer.dozing
    dozer.set_expression("happy", hold=8.0)  # a tap, a parent's button, a greeting...
    advance(dozer, clock, 0.25)
    assert not dozer.dozing and dozer.status()["expression"] == "happy"
    advance(dozer, clock, 7.0)
    assert not dozer.dozing
    advance(dozer, clock, 1.5)
    assert dozer.dozing


def test_tap_reaction_keeps_it_awake_for_the_full_wait(dozer, clock, face_ctx):
    advance(dozer, clock, 31.0, dt=1.0)
    # what DisplayLoop.tap_reaction does: look, happy, bounce, and the giggle sound plays
    dozer.look_at(0.4, 0.5, hold=1.5)
    dozer.set_expression("happy", hold=1.6)
    dozer.play_animation("bounce")
    face_ctx.bus.publish(Topics.AUDIO_SOUND_STARTED, {"name": "giggle"})
    advance(dozer, clock, 1.0)
    face_ctx.bus.publish(Topics.AUDIO_SOUND_FINISHED, {"name": "giggle", "interrupted": False})
    assert not dozer.dozing
    advance(dozer, clock, 29.0, dt=1.0)
    assert not dozer.dozing
    advance(dozer, clock, 1.5)
    assert dozer.dozing


def test_movement_and_a_new_face_wake_it_but_a_still_face_does_not(dozer, clock, face_ctx):
    bus = face_ctx.bus

    def faces(*gazes):
        bus.publish(Topics.VISION_FACES, {"faces": [{"gaze_x": x, "gaze_y": y, "primary": i == 0, "w": 50, "h": 50}
                                                    for i, (x, y) in enumerate(gazes)]})

    for _ in range(40):  # someone sits still in view, with a little detection jitter
        faces((0.5, 0.5))
        faces((0.52, 0.49))
        advance(dozer, clock, 1.0)
    assert dozer.dozing
    faces((0.85, 0.5))  # they moved across the room
    advance(dozer, clock, 0.25)
    assert not dozer.dozing
    advance(dozer, clock, 21.0, dt=1.0)
    assert dozer.dozing
    faces((0.85, 0.5), (0.2, 0.4))  # a second face for one frame: could be a false detection
    advance(dozer, clock, 0.25)
    assert dozer.dozing
    faces((0.85, 0.5), (0.2, 0.4))  # ... seen again: someone new walked in
    advance(dozer, clock, 0.25)
    assert not dozer.dozing


def test_settings_switch_it_off_and_change_the_wait(dozer, clock, face_ctx):
    advance(dozer, clock, 31.0, dt=1.0)
    assert dozer.dozing
    face_ctx.settings.set("face.doze_enabled", False)
    advance(dozer, clock, 0.25)
    assert not dozer.dozing
    advance(dozer, clock, 600.0, dt=5.0)
    assert not dozer.dozing
    face_ctx.settings.set("face.doze_after_seconds", 90)
    face_ctx.settings.set("face.doze_enabled", True)
    face_ctx.bus.publish("audio.wake_word", {})
    advance(dozer, clock, 85.0, dt=1.0)
    assert not dozer.dozing
    advance(dozer, clock, 6.0, dt=1.0)
    assert dozer.dozing
    assert face_ctx.settings.set("face.doze_after_seconds", 1) == 5  # clamped
    assert face_ctx.settings.set("face.awake_on_arrival_seconds", 99999) == 3600
    with pytest.raises(ValueError):
        face_ctx.settings.set("face.doze_after_seconds", "soon")


def test_dozing_lowers_the_frame_rate(dozer, clock, face_ctx):
    advance(dozer, clock, 45.0, dt=0.5)
    assert dozer.dozing
    assert dozer.target_fps() == min(C.DOZE_FPS, face_ctx.config.display.idle_fps)
    assert dozer.target_fps() < face_ctx.config.display.idle_fps
    face_ctx.bus.publish(Topics.VISION_PERSON_PRESENT, {})
    advance(dozer, clock, 0.25)
    assert dozer.target_fps() == face_ctx.config.display.fps  # the wake-up stretch is smooth


def test_health_reports_the_doze_timer(dozer):
    h = dozer.health()
    assert h["doze"]["enabled"] is True and h["doze"]["doze_after"] == 30.0


# ------------------------------------------------------------------ the look
def test_dozing_look_droops_closes_and_floats_zzz():
    a = make_animator(seed=2)
    step(a, 0.5)
    open_h = a.frame().eyes[0].h
    a.set_doze(True)
    step(a, 2.5)
    f = a.frame()
    assert a.expression == "sleepy" and MIN_LINE < f.eyes[0].h < open_h  # drooping, still open
    step(a, 6.0)
    f = a.frame()
    assert all(e.h <= MIN_LINE + 1.0 and e.closed_curve > 0.0 for e in f.eyes)  # closed, peaceful arcs
    assert any(s.kind == "z" for s in f.sprites)  # zzz floating up
    assert f.mouth is not None and f.mouth.round > 0.5  # the sleepy little "o"
    assert not a.is_animating()  # settled: the display can run at the low doze frame rate


def test_no_zzz_until_the_eyes_are_closing():
    a = make_animator(seed=2)
    a.set_doze(True)
    step(a, 1.0)
    assert not any(s.kind == "z" for s in a.frame().sprites)


def test_dozing_eyes_flutter_now_and_then():
    a = make_animator(seed=4)
    a.set_doze(True)
    lids = []
    for _ in range(60 * 4):
        step(a, 0.25)
        lids.append(a.doze_lid)
    assert max(lids[40:]) == pytest.approx(1.0)
    assert min(lids[40:]) < 0.75  # a sleepy flutter (half open) somewhere in a minute


def test_waking_is_a_gentle_stretch_and_blink_not_a_jump():
    a = make_animator(seed=2)
    a.set_doze(True)
    step(a, 10.0)
    blinks = a.blink_count
    a.set_doze(False)
    assert a.waking and a.is_animating()
    step(a, 0.1)
    assert a.doze_lid > 0.8  # the lids lift slowly ...
    assert a._pop_start is None  # ... with no "boing"
    step(a, 0.9)
    assert a.doze_lid == 0.0 and a.expression == "neutral"
    step(a, 1.0)
    assert not a.waking and a.blink_count > blinks  # a soft blink to finish
    assert all(e.h > 50 for e in a.frame().eyes)


def test_doze_shows_only_on_the_idle_face():
    a = make_animator(seed=2)
    a.set_doze(True)
    step(a, 10.0)
    a.set_state("listening")  # e.g. the wake word, before the timer catches up
    step(a, 1.0)
    assert a.doze_lid == 0.0 and a.expression == "listening"
    a.set_state("idle")
    step(a, 0.5)
    assert a.expression == "sleepy"  # still dozing: back to the sleepy idle face


def test_a_held_expression_outlives_dozing_off():
    a = make_animator(seed=2)
    a.set_expression("happy", hold=3.0)
    a.set_doze(True)
    step(a, 1.0)
    assert a.expression == "happy"
    step(a, 2.5)
    assert a.expression == "sleepy"


def test_going_to_sleep_from_a_doze_keeps_the_eyes_closed():
    a = make_animator(seed=2)
    a.set_doze(True)
    step(a, 10.0)
    a.set_state("sleeping")
    a.set_doze(False)
    assert not a.waking  # no stretch: it is going deeper, not waking
    for _ in range(30):
        step(a, 1 / 30)
        assert a.lid_closure() > 0.75
