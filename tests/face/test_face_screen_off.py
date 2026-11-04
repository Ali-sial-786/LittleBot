"""The screen going dark after a quiet spell: the decision (pure, fake clock), the controller, and the display
loop switching the robot screen's signal with wlopm (faked)."""

from __future__ import annotations

import logging
import subprocess

import pytest
from face_stubs import StubRobot

from littlebot.core.events import Topics
from littlebot.face import controller as C
from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.face.doze import MIN_SCREEN_OFF_AFTER_S, SCREEN_OFF_AFTER_S, DozeTimer, screen_seconds
from littlebot.testing import make_test_context


# ------------------------------------------------------------------ the pure decision
def test_default_is_three_minutes():
    assert SCREEN_OFF_AFTER_S == 180.0 and DozeTimer().screen_off_after == 180.0


def test_goes_dark_after_three_quiet_minutes_and_lights_on_anything():
    d = DozeTimer(now=0.0)
    assert d.update_screen(179.9) is None and not d.screen_dark
    assert d.update_screen(180.0) == ("off", "nothing happening for 180 s")
    assert d.update_screen(500.0) is None  # only reports changes
    d.activity(500.0, "the wake word")
    assert d.update_screen(500.0) == ("on", "the wake word")
    d.update_screen(679.9)
    assert not d.screen_dark
    d.update_screen(680.0)
    assert d.screen_dark


def test_someone_walking_in_lights_it_only_for_a_while():
    d = DozeTimer(now=0.0)
    d.update_screen(200.0)
    d.arrival(200.0)
    assert d.update_screen(200.0) == ("on", "someone walked in")
    d.update_screen(219.9)
    assert not d.screen_dark
    assert d.update_screen(220.0) == ("off", "nothing happening for 180 s")  # long quiet already


@pytest.mark.parametrize("key", ["chat", "card", "media", "game", "activity", "state"])
def test_something_going_on_keeps_it_lit(key):
    d = DozeTimer(now=0.0)
    d.begin(key, 1.0, "busy")
    d.update_screen(5000.0)
    assert not d.screen_dark
    d.end(key, 5000.0)
    d.update_screen(5179.9)
    assert not d.screen_dark
    d.update_screen(5180.0)
    assert d.screen_dark


def test_asleep_the_sleeping_state_does_not_keep_it_lit_or_light_it():
    d = DozeTimer(now=0.0)
    d.update_screen(200.0)
    assert d.screen_dark
    d.begin("state", 600.0, "LittleBot went to sleep", poke=False)  # the idle timeout, 10 minutes in
    assert d.update_screen(600.0, asleep=True) is None and d.screen_dark
    d.end("state", 900.0)  # woke up (someone walked in)
    assert d.update_screen(900.0) == ("on", "starting up")


def test_asleep_right_after_a_chat_goes_dark_three_minutes_later():
    d = DozeTimer(now=0.0)
    d.begin("chat", 10.0, "a voice chat")
    d.end("chat", 60.0)  # "stop talking": the chat ends and it goes to sleep at once
    d.begin("state", 60.0, "LittleBot went to sleep", poke=False)
    d.update_screen(239.9, asleep=True)
    assert not d.screen_dark
    d.update_screen(240.0, asleep=True)
    assert d.screen_dark
    d.update_screen(240.0, asleep=False)  # the same hold, not asleep: something is on
    assert not d.screen_dark


def test_never_when_set_to_zero_and_lit_at_once_when_switched_off():
    d = DozeTimer(screen_off_after=0, now=0.0)
    assert d.update_screen(100000.0) is None and not d.screen_dark
    d.configure(screen_off_after=180)
    assert d.update_screen(100000.0) == ("off", "nothing happening for 180 s")
    d.configure(screen_off_after=0)
    assert d.update_screen(100001.0) == ("on", "the screen no longer goes off")


def test_independent_of_dozing():
    d = DozeTimer(enabled=False, now=0.0)
    d.update(200.0)
    assert not d.dozing
    d.update_screen(200.0)
    assert d.screen_dark


def test_screen_setting_is_clamped():
    assert screen_seconds(10) == MIN_SCREEN_OFF_AFTER_S == 30.0
    assert screen_seconds(-5) == 0.0 and screen_seconds(0) == 0.0
    assert screen_seconds(1e9) == 3600.0
    assert screen_seconds("soon") == SCREEN_OFF_AFTER_S and screen_seconds(float("nan")) == SCREEN_OFF_AFTER_S
    st = DozeTimer(now=0.0).status(0.0)
    assert st["screen_off_after"] == 180.0 and st["screen_dark"] is False


# ------------------------------------------------------------------ the controller
class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def face(face_ctx, clock):
    fc = FaceController(face_ctx, seed=1, clock=clock)
    face_ctx.face = fc
    yield fc
    fc.stop_ticker()


def advance(fc: FaceController, clock: Clock, seconds: float, dt: float = 1.0) -> None:
    for _ in range(round(seconds / dt)):
        clock.t += dt
        fc.update(dt)


def test_controller_darkens_and_lights_with_plain_log_lines(face, clock, face_ctx, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.face")
    seen: list[dict] = []
    face_ctx.bus.subscribe(Topics.FACE_SCREEN, lambda e: seen.append(dict(e.data)))
    advance(face, clock, 179.0)
    assert not face.screen_off and face.status()["screen_on"] is True
    advance(face, clock, 1.5, dt=0.5)
    assert face.screen_off and face.status()["screen_on"] is False
    assert "face: screen off (nothing happening for 180 s)" in caplog.text
    assert face.target_fps() == C.SCREEN_OFF_FPS
    face_ctx.bus.publish("audio.wake_word", {"phrase": "hey little bot"})
    advance(face, clock, 0.25, dt=0.25)
    assert not face.screen_off
    assert "face: screen on (the wake word)" in caplog.text
    assert seen == [{"on": False, "reason": "nothing happening for 180 s"}, {"on": True, "reason": "the wake word"}]


def test_a_touch_lights_it(face, clock):
    advance(face, clock, 181.0)
    assert face.screen_off
    face.activity(D.SCREEN_TOUCH)
    advance(face, clock, 0.25, dt=0.25)
    assert not face.screen_off and not face.dozing


def test_asleep_it_stays_dark_and_waking_lights_it(face, clock, face_ctx):
    advance(face, clock, 181.0)
    assert face.screen_off
    face_ctx.state.transition("SLEEPING", "idle timeout")
    advance(face, clock, 5.0)
    assert face.screen_off  # going to sleep doesn't light it for another three minutes
    face_ctx.state.transition("LISTENING", "wake word")
    advance(face, clock, 0.25, dt=0.25)
    assert not face.screen_off


def test_sent_to_sleep_it_darkens_three_minutes_later(face, clock, face_ctx):
    face_ctx.state.transition("LISTENING", "wake word")
    advance(face, clock, 30.0)
    face_ctx.state.to_idle("chat ended")
    face_ctx.state.transition("SLEEPING", "asked to stop")
    advance(face, clock, 179.0)
    assert not face.screen_off
    advance(face, clock, 2.0)
    assert face.screen_off


def test_screen_setting_changes_and_validation(face, clock, face_ctx):
    face_ctx.settings.set("face.screen_off_after_seconds", 0)
    advance(face, clock, 600.0, dt=5.0)
    assert not face.screen_off
    assert face_ctx.settings.set("face.screen_off_after_seconds", 10) == 30  # clamped
    face_ctx.bus.publish("audio.wake_word", {})
    advance(face, clock, 29.0)
    assert not face.screen_off
    advance(face, clock, 2.0)
    assert face.screen_off
    with pytest.raises(ValueError):
        face_ctx.settings.set("face.screen_off_after_seconds", "soon")
    assert face.health()["doze"]["screen_off_after"] == 30.0


# ------------------------------------------------------------------ wlopm and the display loop
class _Run:
    def __init__(self, returncode=0, stderr="", raise_=None):
        self.calls: list[list[str]] = []
        self.returncode, self.stderr, self.raise_ = returncode, stderr, raise_

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if self.raise_ is not None:
            raise self.raise_
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


def test_set_output_power_runs_wlopm(caplog):
    run = _Run()
    assert D.set_output_power("HDMI-A-1", False, run=run, env={}) is True
    assert D.set_output_power("HDMI-A-1", True, run=run, env={}) is True
    assert run.calls == [["wlopm", "--off", "HDMI-A-1"], ["wlopm", "--on", "HDMI-A-1"]]
    with caplog.at_level(logging.WARNING, logger="littlebot.face.display"):
        assert D.set_output_power("HDMI-A-1", False, run=_Run(1, "no such output"), env={}) is False
        assert D.set_output_power("HDMI-A-1", False, run=_Run(raise_=subprocess.TimeoutExpired("wlopm", 5)),
                                  env={}) is False
    assert "no such output" in caplog.text


def test_screen_power_only_for_the_robots_own_screen():
    env, which = {"WAYLAND_DISPLAY": "wayland-0"}, (lambda _: "/usr/bin/wlopm")
    assert isinstance(D.make_screen_power("HDMI-A-1", fullscreen=True, sim_mode=False, env=env, which=which),
                      D.ScreenPower)
    assert D.make_screen_power("HDMI-A-1", fullscreen=False, sim_mode=False, env=env, which=which) is None
    assert D.make_screen_power("HDMI-A-1", fullscreen=True, sim_mode=True, env=env, which=which) is None
    assert D.make_screen_power("HDMI-A-1", fullscreen=True, sim_mode=False, env={}, which=which) is None
    assert D.make_screen_power("HDMI-A-1", fullscreen=True, sim_mode=False, env=env, which=lambda _: None) is None


class _Switch:
    def __init__(self, results=None):
        self.calls: list[tuple[str, bool]] = []
        self.results = list(results or [])

    def __call__(self, output, on):
        self.calls.append((output, on))
        return self.results.pop(0) if self.results else True


def test_screen_power_switches_once_per_change_and_retries_later():
    sw = _Switch()
    sp = D.ScreenPower("HDMI-A-1", switch=sw, start=lambda fn: fn())
    sp.want(True, 0.0)
    assert sw.calls == [] and not sp.dark  # already on
    sp.want(False, 1.0)
    sp.want(False, 2.0)
    assert sw.calls == [("HDMI-A-1", False)] and sp.dark
    sp.want(True, 3.0)
    assert sw.calls[-1] == ("HDMI-A-1", True) and not sp.dark
    bad, t = _Switch([False]), [10.0]
    sp = D.ScreenPower("HDMI-A-1", switch=bad, start=lambda fn: fn(), clock=lambda: t[0])
    sp.want(False, 10.0)
    assert not sp.dark and len(bad.calls) == 1
    sp.want(False, 10.0 + D.SCREEN_RETRY_SECONDS - 0.1)
    assert len(bad.calls) == 1  # not hammered
    sp.want(False, 10.0 + D.SCREEN_RETRY_SECONDS)
    assert sp.dark and len(bad.calls) == 2


def test_screen_power_follows_the_newest_wish_and_restores():
    sp: D.ScreenPower

    def switch(output, on):
        calls.append(on)
        if len(calls) == 1:
            sp.want(True, 0.0)  # lit again while the "off" call was running
        return True

    calls: list[bool] = []
    sp = D.ScreenPower("HDMI-A-1", switch=switch, start=lambda fn: fn())
    sp.want(False, 0.0)
    assert calls == [False, True] and not sp.dark
    sp.dark = True
    sp.restore()
    assert calls[-1] is True and not sp.dark


pygame = pytest.importorskip("pygame")


@pytest.fixture
def dark_window():
    clock = Clock()
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.0})
    ctx.face = FaceController(ctx, seed=2, clock=clock)
    ctx.audio = None
    loop = D.DisplayLoop(StubRobot(ctx), max_frames=1)
    loop.open()
    sw = _Switch()
    loop.screen_power = D.ScreenPower("HDMI-A-1", switch=sw, start=lambda fn: fn())
    loop.touch_bounce_s = 0.0
    yield loop, ctx, clock, sw
    loop.close()
    ctx.face.stop_ticker()
    ctx.db.close()


def _frame(loop) -> None:
    loop.max_frames = loop.frames + 1
    loop.run()


def test_the_loop_darkens_the_screen_and_stops_drawing(dark_window):
    loop, ctx, clock, sw = dark_window
    _frame(loop)
    assert sw.calls == [] and len(loop._frame_stamps) == 1
    clock.t += 181.0
    _frame(loop)
    assert sw.calls == [("HDMI-A-1", False)] and loop.screen_dark
    drawn = len(loop._frame_stamps)
    _frame(loop)
    assert len(loop._frame_stamps) == drawn  # nothing drawn while dark
    assert loop.target_fps() == C.SCREEN_OFF_FPS
    ctx.bus.publish("audio.wake_word", {})
    _frame(loop)
    assert sw.calls[-1] == ("HDMI-A-1", True) and not loop.screen_dark


def test_the_first_touch_on_the_dark_screen_only_lights_it(dark_window, caplog):
    loop, ctx, clock, sw = dark_window
    clock.t += 181.0
    _frame(loop)
    assert loop.screen_dark
    centre = loop.face_rect.center
    with caplog.at_level(logging.INFO, logger="littlebot.face.display"):
        pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=centre, button=1))
        loop._handle_events()
    assert "touch on the dark screen: lighting it" in caplog.text
    assert ctx.face.status()["expression"] != "happy"  # no giggle for a finger that couldn't see
    _frame(loop)
    assert not loop.screen_dark and sw.calls[-1] == ("HDMI-A-1", True)
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=centre, button=1))
    loop._handle_events()
    assert loop._press is None and ctx.face.status()["expression"] != "happy"
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=centre, button=1))  # a real tap now
    loop._handle_events()
    assert ctx.face.status()["expression"] == "happy"


def test_closing_the_window_lights_the_screen(dark_window):
    loop, ctx, clock, sw = dark_window
    clock.t += 181.0
    _frame(loop)
    assert loop.screen_dark
    loop.close()
    assert sw.calls[-1] == ("HDMI-A-1", True) and not loop.screen_dark
