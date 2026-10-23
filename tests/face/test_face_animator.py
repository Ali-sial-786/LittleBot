"""Pure animation logic: transitions, blinks, gaze, lids, holds, speech, animations, fps hints."""

from __future__ import annotations

import math
import random

import pytest
from face_stubs import make_animator, step

from littlebot.core.events import Topics
from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS, FACE_STATES
from littlebot.face import animator as A
from littlebot.face.effects import ANIMATION_DURATIONS, animation_motion
from littlebot.face.expressions import DEFAULT_EXPRESSION_FOR_STATE, EXPRESSION_PARAMS
from littlebot.face.frame import MIN_LINE, NATIVE_H, NATIVE_W
from littlebot.face.mouth import VISEMES


def calm(**kw):
    """Animator without random blinks / wandering, for exact assertions."""
    a = make_animator(idle_gaze=False, **kw)
    a.next_blink = 1e9
    return a


# ------------------------------------------------------------------ transitions
def test_transition_interpolates_smoothly_and_finishes_in_time():
    a = calm(transition_seconds=0.25)
    target = EXPRESSION_PARAMS["happy"].left.smile
    assert a.set_expression("happy")
    values = []
    dt = 0.01
    for _ in range(25):
        a.update(dt)
        values.append(a.params.left.smile)
    assert all(b >= v - 1e-9 for v, b in zip(values, values[1:])), "monotonic"
    assert 0 < values[5] < target  # genuinely in between
    assert values[-1] == pytest.approx(target)
    assert not a.transitioning
    # no big jumps between frames
    assert max(b - v for v, b in zip([0.0] + values, values)) < target * 0.2


def test_retarget_mid_transition_is_continuous():
    a = calm(transition_seconds=0.3)
    a.set_expression("surprised")
    step(a, 0.15, 0.01)
    before = a.params.left.scale
    a.set_expression("sad")
    a.update(0.01)
    assert abs(a.params.left.scale - before) < 0.05
    step(a, 0.3, 0.01)
    assert a.params == EXPRESSION_PARAMS["sad"]


def test_zero_transition_time_is_instant():
    a = calm(transition_seconds=0.0)
    a.set_expression("surprised")
    assert a.params == EXPRESSION_PARAMS["surprised"]


def test_extras_fade_in_and_out():
    a = calm()
    a.set_expression("thinking")
    a.update(0.05)
    assert 0 < a.extras_alpha["dots"] < 1
    step(a, 0.5)
    assert a.extras_alpha["dots"] == 1.0
    a.set_expression("neutral")
    step(a, 0.5)
    assert a.extras_alpha["dots"] == 0.0


# ------------------------------------------------------------------ blinking
def test_blink_amount_shape():
    assert A.blink_amount(-0.1) == 0.0
    assert A.blink_amount(0.0) == 0.0
    assert A.blink_amount(A.BLINK_CLOSE + A.BLINK_HOLD / 2) == 1.0
    assert A.blink_amount(A.blink_duration()) == 0.0
    # double blink closes twice
    dt = 0.002
    samples = [A.blink_amount(i * dt, double=True) for i in range(int(A.blink_duration(double=True) / dt) + 2)]
    closures = sum(1 for p, c in zip(samples, samples[1:]) if p < 0.99 <= c)
    assert closures == 2


def test_blinks_are_scheduled_within_configured_interval():
    a = make_animator(seed=42, idle_gaze=False, blink_interval=(2.0, 3.0), double_blink_chance=0.0)
    starts = []
    dt = 1 / 100
    last = 0
    for _ in range(int(30 / dt)):
        a.update(dt)
        if a.blink_count != last:
            starts.append(a.t)
            last = a.blink_count
    assert 8 <= len(starts) <= 16
    assert 2.0 - dt <= starts[0] <= 3.0 + dt
    gaps = [b - p for p, b in zip(starts, starts[1:])]
    assert all(2.0 - 2 * dt <= g <= 3.0 + 2 * dt for g in gaps), gaps


def test_blink_schedule_is_deterministic_with_seed():
    def run(seed):
        a = make_animator(seed=seed)
        out = []
        for _ in range(600):
            a.update(1 / 30)
            out.append((a.blink_count, round(a.gx, 6), round(a.gy, 6)))
        return out

    assert run(7) == run(7)
    assert run(7) != run(8)


def test_double_blink_chance():
    always = make_animator(seed=3, double_blink_chance=1.0, blink_interval=(0.5, 0.6))
    never = make_animator(seed=3, double_blink_chance=0.0, blink_interval=(0.5, 0.6))
    for a in (always, never):
        events = []
        for _ in range(300):
            a.update(1 / 60)
            events += [d for t, d in a.pop_events() if t == Topics.FACE_BLINK]
        assert events
        if a is always:
            assert all(e["double"] for e in events)
        else:
            assert not any(e["double"] for e in events)


def test_manual_blink_and_double_blink():
    a = calm()
    a.blink()
    assert a.blinking
    step(a, A.BLINK_CLOSE + 0.01, 0.005)
    assert a.current_blink() > 0.9
    step(a, 0.3, 0.005)
    assert not a.blinking
    a.blink(double=True)
    seen_open_between = False
    was_closed = False
    for _ in range(int(A.blink_duration(double=True) / 0.005)):
        a.update(0.005)
        b = a.current_blink()
        if b > 0.95:
            if seen_open_between:
                break
            was_closed = True
        elif was_closed and b < 0.05:
            seen_open_between = True
    assert seen_open_between and a.current_blink() > 0.95


def test_no_blinking_while_eyes_closed_or_sleeping():
    a = make_animator(seed=2, blink_interval=(0.3, 0.4))
    a.close_eyes()
    step(a, 0.5)
    count = a.blink_count
    step(a, 3.0)
    assert a.blink_count == count
    a.open_eyes()
    a.set_state("sleeping")
    step(a, 3.0)
    assert a.blink_count == count


# ------------------------------------------------------------------ gaze
def test_gaze_is_clamped_to_screen():
    a = calm()
    a.look_at(5.0, -3.0, hold=10)
    for _ in range(120):
        a.update(1 / 60)
        x, y = a.gaze()
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
    x, y = a.gaze()
    assert x == pytest.approx(1.0, abs=0.03) and y == pytest.approx(0.0, abs=0.03)


def test_gaze_eases_without_overshoot():
    a = calm()
    a._micro_target = (0.0, 0.0)  # noqa: SLF001
    A_MICRO = A.MICRO_AMPLITUDE
    A.MICRO_AMPLITUDE = (0.0, 0.0)
    try:
        a.look_at(0.9, 0.5, hold=10)
        xs = []
        for _ in range(60):
            a.update(1 / 60)
            xs.append(a.gaze()[0])
    finally:
        A.MICRO_AMPLITUDE = A_MICRO
    assert 0.5 < xs[2] < xs[10] < xs[-1]  # moves smoothly
    assert all(b >= p - 1e-9 for p, b in zip(xs, xs[1:]))  # monotonic (critically damped)
    assert max(xs) <= 0.9 + 1e-6  # no overshoot
    assert xs[-1] == pytest.approx(0.9, abs=0.02)


def test_spring_is_stable_with_large_steps():
    x, v = 0.0, 0.0
    for _ in range(20):
        x, v = A._spring(x, v, 1.0, 30.0, 0.5)  # noqa: SLF001
        assert -0.01 <= x <= 1.01
    assert x == pytest.approx(1.0, abs=1e-3)


def test_explicit_look_overrides_tracking_then_reverts():
    a = calm(look_hold_seconds=1.0)
    for _ in range(60):
        a.track_gaze(0.9, 0.5)
        a.update(1 / 60)
    assert a.gaze()[0] == pytest.approx(0.9, abs=0.05)
    assert a.gaze_source == "track"
    a.look_at(0.1, 0.5)  # hold = config default (1.0 s)
    for _ in range(54):  # 0.9 s, tracking keeps updating meanwhile
        a.track_gaze(0.9, 0.5)
        a.update(1 / 60)
    assert a.gaze_source == "explicit"
    assert a.gaze()[0] == pytest.approx(0.1, abs=0.05)
    for _ in range(90):
        a.track_gaze(0.9, 0.5)
        a.update(1 / 60)
    assert a.gaze_source == "track"
    assert a.gaze()[0] == pytest.approx(0.9, abs=0.05)


def test_explicit_hold_parameter():
    a = calm(look_hold_seconds=5.0)
    a.look_at(0.2, 0.2, hold=0.5)
    step(a, 0.4)
    assert a.gaze_source == "explicit"
    step(a, 0.2)
    assert a.gaze_source != "explicit"


def test_tracking_times_out_and_clear_gaze():
    a = calm()
    a.track_gaze(0.1, 0.9)
    step(a, 0.5)
    assert a.gaze_source == "track"
    step(a, A.TRACK_TIMEOUT + 0.1)
    assert a.gaze_source == "idle"
    a.look_at(0.0, 0.0, hold=30)
    a.track_gaze(1.0, 1.0)
    a.clear_gaze()
    step(a, 1.0)
    assert a.gaze_source == "idle"
    assert a.gaze() == pytest.approx((0.5, 0.5), abs=0.04)


def test_idle_wandering_moves_the_eyes():
    a = make_animator(seed=11)
    xs = []
    for _ in range(600):
        a.update(1 / 60)
        xs.append(a.gaze()[0])
    assert max(xs) - min(xs) > 0.15
    assert all(0.0 <= x <= 1.0 for x in xs)


def test_non_finite_inputs_rejected():
    a = calm()
    assert not a.look_at(float("nan"), 0.5)
    assert not a.track_gaze(0.5, float("inf"))
    a.update(float("nan"))
    a.update(-1)
    assert a.t == 0.0


# ------------------------------------------------------------------ eyelids
def test_close_and_open_eyes():
    a = calm()
    full = a.frame().eyes[0].h
    a.close_eyes()
    step(a, A.CLOSE_SECONDS + 0.05)
    f = a.frame()
    assert a.closure == 1.0
    assert all(e.h == pytest.approx(MIN_LINE) for e in f.eyes)
    assert all(e.closed_curve > 0 for e in f.eyes)
    a.open_eyes()
    a.update(1 / 60)
    assert a.is_animating()
    step(a, A.OPEN_SECONDS + A.POP_SECONDS + 0.1)
    assert a.closure == 0.0
    assert a.frame().eyes[0].h == pytest.approx(full, rel=0.05)


def test_sleeping_closes_eyes_and_waking_opens_them():
    a = calm()
    a.set_state("sleeping")
    step(a, 1.0)
    assert a.closure == 1.0 and a.expression == "sleepy"
    assert "zzz" in a.params.extras and a.extras_alpha["zzz"] == 1.0
    a.set_state("idle")
    step(a, 1.0)
    assert a.closure == 0.0 and a.expression == "neutral"


# ------------------------------------------------------------------ expressions / states
def test_hold_reverts_to_state_default():
    a = calm()
    a.set_expression("happy", hold=1.0)
    step(a, 0.9)
    assert a.expression == "happy"
    step(a, 0.2)
    assert a.expression == "neutral"
    a.set_state("listening")
    a.set_expression("surprised", hold=0.5)
    step(a, 0.6)
    assert a.expression == "listening"


def test_held_expression_outlives_talking_state_changes():
    """'Make a happy face' while LittleBot talks: listening/speaking/thinking flips must not wipe it."""
    a = calm()
    a.set_expression("happy", hold=2.0)
    for state in ("speaking", "listening", "thinking", "speaking"):
        a.set_state(state)
        step(a, 0.2)
        assert a.expression == "happy", state
    step(a, 1.5)  # hold over: the current state's own expression comes back
    assert a.expression == "speaking"


def test_sleep_error_and_hidden_still_take_over_a_held_expression():
    for state in ("sleeping", "error", "hidden"):
        a = calm()
        a.set_expression("happy", hold=5.0)
        a.set_state(state)
        assert a.expression == DEFAULT_EXPRESSION_FOR_STATE[state], state


def test_unheld_expression_still_follows_the_state():
    a = calm()
    a.set_expression("happy")
    a.set_state("listening")
    assert a.expression == "listening"


def test_new_expression_cancels_hold():
    a = calm()
    a.set_expression("happy", hold=0.5)
    a.set_expression("sad")
    step(a, 1.0)
    assert a.expression == "sad"


def test_state_defaults_and_events():
    a = calm()
    for state in FACE_STATES:
        assert a.set_state(state)
        assert a.expression == DEFAULT_EXPRESSION_FOR_STATE[state]
    events = a.pop_events()
    vis = [d["visible"] for t, d in events if t == Topics.FACE_VISIBLE]
    assert vis == [False]  # entered hidden (last state)
    a.set_state("idle")
    assert [d for t, d in a.pop_events() if t == Topics.FACE_VISIBLE] == [{"visible": True}]


def test_same_state_keeps_explicit_expression():
    a = calm()
    a.set_expression("happy")
    a.set_state("idle")
    assert a.expression == "happy"


def test_expression_event_only_on_change():
    a = calm()
    a.set_expression("happy")
    a.set_expression("happy")
    a.set_expression("happy", hold=3)
    evs = [d for t, d in a.pop_events() if t == Topics.FACE_EXPRESSION]
    assert evs == [{"expression": "happy"}]


def test_unknown_names_are_rejected_without_side_effects():
    a = calm()
    a.set_expression("happy")
    assert not a.set_expression("grumpy")
    assert not a.set_state("dancing")
    assert not a.play_animation("explode")
    assert a.expression == "happy" and a.state == "idle" and a.animations == []


def test_hidden_fades_and_is_calm():
    a = calm()
    a.set_state("hidden")
    assert not a.visible
    a.update(0.1)
    assert 0 < a.hidden_amount < 1 and a.is_animating()
    step(a, 1.0)
    assert a.hidden_amount == 1.0
    assert not a.is_animating()
    assert any(s.kind == "play" for s in a.frame().sprites)


def test_error_state_is_gentle_lavender_confused():
    a = calm()
    a.set_state("error")
    step(a, 1.5)
    assert a.expression == "confused"
    r, g, b = a.eye_color()
    assert b >= r and b >= 200  # soft blue/lavender, never red
    assert r < 200 or g > 150


# ------------------------------------------------------------------ speech / listening
def test_speaking_synthesizes_pulse_without_level_events():
    a = calm()
    a.speech_started(2.0)
    assert a.talking
    levels = []
    for _ in range(120):
        a.update(1 / 60)
        levels.append(a.level)
    assert max(levels) - min(levels[30:]) > 0.3
    assert a.talk_mix == 1.0
    assert a.frame().mouth is not None
    a.speech_finished()
    step(a, 0.6)
    assert not a.talking and a.level < 0.02 and a.talk_mix == 0.0
    m = a.frame().mouth
    assert m is not None and m.shape == "" and m.open < 0.05  # back to the resting smile, still visible


def test_speaking_follows_level_events():
    a = calm()
    a.speech_started()
    for _ in range(12):
        a.speech_level(0.9)
        a.update(1 / 60)
    assert a.level > 0.8
    for _ in range(12):
        a.speech_level(0.0)
        a.update(1 / 60)
    assert a.level < 0.15
    a.speech_level("bogus")  # ignored
    a.speech_level(float("nan"))


def test_speaking_safety_timeout():
    a = calm()
    a.speech_started(1.0)
    step(a, A.SPEECH_MIN_TIMEOUT + 0.5, 0.05)
    assert not a.speech_active


def test_listening_indicator():
    a = calm()
    a.listening_started()
    step(a, 0.5)
    assert a.frame().ring > 0
    a.listening_stopped()
    step(a, 0.5)
    assert a.frame().ring == 0.0
    a.set_state("listening")
    step(a, 0.5)
    assert a.frame().ring > 0


# ------------------------------------------------------------------ one-shot animations
@pytest.mark.parametrize("name", FACE_ANIMATIONS)
def test_animation_plays_and_finishes(name):
    a = calm()
    assert a.play_animation(name)
    a.update(0.01)
    assert name in a.active_animations()
    assert a.is_animating()
    step(a, ANIMATION_DURATIONS[name] + 0.05, 0.02)
    assert name not in a.active_animations()


@pytest.mark.parametrize("name", FACE_ANIMATIONS)
def test_animation_motion_starts_and_ends_at_identity(name):
    for u in (0.0, 1.0):
        m = animation_motion(name, u)
        assert m.dx == pytest.approx(0, abs=1e-6) and m.dy == pytest.approx(0, abs=1e-6)
        assert m.sx == pytest.approx(1, abs=1e-6) and m.sy == pytest.approx(1, abs=1e-6)
        assert m.rot == pytest.approx(0, abs=1e-6)
        assert m.gx == pytest.approx(0, abs=1e-6) and m.gy == pytest.approx(0, abs=1e-6)
    assert animation_motion(name, 1.0).open_mul == pytest.approx(1.0)


def test_particle_animations_produce_sprites():
    kinds = {"confetti": "confetti", "hearts": "heart", "sparkle": "sparkle", "zzz": "z"}
    for anim, kind in kinds.items():
        a = calm()
        a.play_animation(anim)
        step(a, ANIMATION_DURATIONS[anim] * 0.5)
        assert any(s.kind == kind for s in a.frame().sprites), anim


def test_replaying_an_animation_restarts_it():
    a = calm()
    a.play_animation("bounce")
    step(a, 0.3)
    a.play_animation("bounce")
    assert a.active_animations() == ["bounce"]
    assert a.animations[0].start == pytest.approx(a.t)


def test_peek_opens_closed_eyes():
    a = calm()
    a.close_eyes()
    step(a, 0.5)
    a.play_animation("peek")
    assert not a.eyes_closed
    step(a, ANIMATION_DURATIONS["peek"] + 0.3)
    assert a.closure == 0.0


# ------------------------------------------------------------------ fps hint
def test_calm_face_allows_idle_fps():
    a = calm()
    step(a, 2.0)
    assert not a.is_animating()
    a.blink()
    a.update(0.01)
    assert a.is_animating()
    step(a, 0.5)
    assert not a.is_animating()
    a.set_expression("celebrating")  # confetti + bounce: always busy
    step(a, 1.0)
    assert a.is_animating()


# ------------------------------------------------------------------ frames
def test_frames_stay_sane_under_random_commands():
    rng = random.Random(1234)
    a = make_animator(seed=5)
    for _ in range(400):
        r = rng.random()
        if r < 0.1:
            a.set_expression(rng.choice(EXPRESSIONS), hold=rng.choice([None, 0.5, 2.0]))
        elif r < 0.15:
            a.set_state(rng.choice(FACE_STATES))
        elif r < 0.25:
            a.look_at(rng.uniform(-1, 2), rng.uniform(-1, 2), hold=rng.choice([None, 0.3]))
        elif r < 0.3:
            a.play_animation(rng.choice(FACE_ANIMATIONS))
        elif r < 0.33:
            (a.close_eyes if rng.random() < 0.5 else a.open_eyes)()
        elif r < 0.36:
            a.speech_started() if rng.random() < 0.5 else a.speech_finished()
        a.update(rng.uniform(0.0, 0.1))
        f = a.frame()
        assert f.width == NATIVE_W and f.height == NATIVE_H
        for e in f.eyes:
            for v in (e.cx, e.cy, e.w, e.h, e.radius, e.tilt):
                assert math.isfinite(v)
            assert -40 <= e.cx <= NATIVE_W + 40 and -40 <= e.cy <= NATIVE_H + 80
            assert e.h >= MIN_LINE - 1e-6 and e.w > 10
            assert 0 <= e.radius <= max(e.w, e.h) / 2 + 1e-6
            assert 0 <= e.lid_top <= 1 and 0 <= e.smile <= 1
        assert all(0 <= c <= 255 for c in f.color)
        for s in f.sprites:
            assert math.isfinite(s.x) and math.isfinite(s.y) and 0 <= s.alpha <= 1.0 + 1e-6


# ------------------------------------------------------------------ mouth: always there, lip-synced
@pytest.mark.parametrize("name", EXPRESSIONS)
def test_mouth_is_shown_for_every_expression(name):
    a = calm()
    a.set_expression(name)
    step(a, 0.5)
    m = a.frame().mouth
    assert m is not None and m.alpha == 1.0 and m.width > 15
    assert 0 < m.cx < NATIVE_W and 0 < m.cy < NATIVE_H


@pytest.mark.parametrize("state", [s for s in FACE_STATES if s != "hidden"])
def test_mouth_is_shown_in_every_visible_state(state):
    a = calm()
    a.set_state(state)
    step(a, 1.5)
    assert a.frame().mouth is not None


def test_mouth_is_only_hidden_with_the_whole_face():
    a = calm()
    a.set_state("hidden")
    step(a, 0.2)
    assert a.frame().mouth is not None  # still fading out with the face
    step(a, 1.0)
    assert a.frame().mouth is None
    a.set_state("idle")
    a.update(0.05)
    assert a.frame().mouth is not None


@pytest.mark.parametrize("name", FACE_ANIMATIONS)
def test_mouth_stays_on_screen_during_every_animation(name):
    a = calm()
    step(a, 0.3)
    a.play_animation(name)
    duration = ANIMATION_DURATIONS[name]
    for _ in range(12):
        step(a, duration / 12, 1 / 60)
        m = a.frame().mouth
        assert m is not None and m.alpha == 1.0
        assert 0 < m.cx < NATIVE_W
        assert m.cy < NATIVE_H - 8  # never pushed off the bottom (peek-a-boo ducks down 70 px)


def test_resting_mouths_match_the_mood():
    def mouth(name):
        a = calm()
        a.set_expression(name)
        step(a, 0.5)
        return a.frame().mouth

    assert mouth("neutral").smile > 0.2 and mouth("neutral").open < 0.05  # a little smile
    assert mouth("happy").smile > 0.8 and mouth("happy").open > 0.2  # big open smile
    assert mouth("celebrating").open > mouth("happy").open
    assert mouth("sad").smile < -0.4  # frown
    assert mouth("surprised").round > 0.9 and mouth("surprised").open > 0.4  # "O"
    assert mouth("playful").tongue > 0.5  # cheeky tongue
    assert mouth("thinking").angle != 0.0  # lopsided "hmm"


def test_sleeping_mouth_is_a_small_breathing_o():
    a = calm()
    a.set_state("sleeping")
    step(a, 2.0)
    opens = []
    for _ in range(90):
        a.update(1 / 15)
        opens.append(a.frame().mouth.open)
    m = a.frame().mouth
    assert m.round > 0.9 and m.width < 30
    assert 0.0 < min(opens) < max(opens) < 0.3  # breathes


def test_peek_says_boo_with_an_open_mouth():
    a = calm()
    step(a, 0.3)
    rest = a.frame().mouth.open
    a.play_animation("peek")
    step(a, ANIMATION_DURATIONS["peek"] * 0.65, 1 / 60)
    assert a.frame().mouth.open > rest + 0.3


def _talk(a, shape, level=0.8, frames=6, dt=1 / 30):
    for _ in range(frames):
        a.speech_level(level, shape)
        a.update(dt)
    return a.frame().mouth


@pytest.mark.parametrize("shape", VISEMES)
def test_speech_shape_events_shape_the_mouth(shape):
    a = calm()
    a.speech_started(3.0)
    m = _talk(a, shape)
    assert a.mouth_shape == shape and m.shape == shape
    if shape == "closed":
        assert m.open < 0.05
    elif shape == "small":
        assert 0.1 < m.open < 0.4 and m.teeth < 0.1 and m.round < 0.4
    elif shape == "ah":
        assert m.open > 0.6 and m.round < 0.1
    elif shape == "ee":
        assert m.teeth > 0.8 and m.width > 60 and m.open < 0.45
    elif shape == "oo":
        assert m.round > 0.8 and m.width < 40 and m.open > 0.3


def test_mouth_follows_the_sound_quickly_and_morphs_smoothly():
    a = calm()
    a.speech_started(3.0)
    _talk(a, "closed", 0.0, frames=10)
    opens = []
    for _ in range(4):  # 4 frames at 30 fps
        a.speech_level(0.9, "ah")
        a.update(1 / 30)
        opens.append(a.frame().mouth.open)
    assert opens[0] > 0.2  # visibly opening on the very next frame
    assert opens[2] > 0.8 * opens[-1] > 0.5  # nearly there within ~100 ms
    assert all(b >= x for x, b in zip(opens, opens[1:]))  # no flicker: a smooth morph
    rounds = []
    for _ in range(6):
        a.speech_level(0.9, "oo")
        a.update(1 / 30)
        rounds.append(a.frame().mouth.round)
    assert 0 < rounds[0] < rounds[-1] and rounds[-1] > 0.9


def test_level_only_events_still_work():
    a = calm()
    a.speech_started(3.0)
    assert _talk(a, None, 0.95).open > 0.6  # loud: wide open
    assert _talk(a, None, 0.0, frames=12).open < 0.05  # silence: closed
    m = _talk(a, "bogus", 0.3)  # unknown shape: guessed from the loudness
    assert a.mouth_shape == "small" and 0.05 < m.open < 0.4


def test_stale_shape_falls_back_to_the_talking_pulse():
    a = calm()
    a.speech_started(3.0)
    _talk(a, "oo", 0.8)
    step(a, A.LEVEL_TIMEOUT + 0.3, 1 / 30)  # the audio went quiet on the bus
    assert a.mouth_shape in ("closed", "small", "ah")


def test_talking_keeps_the_mood_of_the_expression():
    happy, sad = calm(), calm()
    happy.set_expression("happy")
    sad.set_expression("sad")
    for a in (happy, sad):
        step(a, 0.5)
        a.speech_started(3.0)
    assert _talk(happy, "small").smile > _talk(sad, "small").smile
