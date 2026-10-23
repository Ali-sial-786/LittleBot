"""Cartoon eye anatomy (pure geometry, no pygame): pupil placement and catchlights."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from face_stubs import make_animator, step

from littlebot.core.interfaces import EXPRESSIONS
from littlebot.face.frame import MIN_LINE, RIM, EyeFrame, catchlights, open_span_at, pupil_geometry, rotate

GAZES = [(gx, gy) for gx in (-1.0, -0.5, 0.0, 0.5, 1.0) for gy in (-1.0, 0.0, 1.0)]


def _eyes(expression: str) -> tuple[EyeFrame, EyeFrame]:
    a = make_animator(seed=3, idle_gaze=False)
    a.next_blink = 1e9
    a.set_expression(expression)
    step(a, 1.0, 1 / 30)
    return a.frame().eyes


def _outline_only(e: EyeFrame) -> EyeFrame:
    return replace(e, lid_top=0.0, lid_bottom=0.0, smile=0.0)


@pytest.mark.parametrize("name", EXPRESSIONS)
def test_pupil_stays_inside_the_eye_outline_for_any_gaze(name):
    for e0 in _eyes(name):
        for squash in (1.0, 0.6, 0.35):
            for gx, gy in GAZES:
                e = replace(e0, h=max(MIN_LINE, e0.h * squash), look_x=gx, look_y=gy)
                p = pupil_geometry(e)
                if p is None:
                    continue
                ox, oy, rx, ry, alpha = p
                assert 0.0 < alpha <= 1.0
                cx, cy = rotate(ox, oy, -e.tilt)  # back to eye-local coordinates
                plain = _outline_only(e)
                for i in range(48):
                    t = 2 * math.pi * i / 48
                    x, y = cx + rx * math.cos(t), cy + ry * math.sin(t)
                    top, bottom = open_span_at(plain, x)
                    # inside the outline and its rim (lids may cover the pupil, that is fine)
                    assert abs(x) <= e.w / 2 - RIM + 0.5, (name, gx, gy, squash)
                    assert top + RIM - 0.5 <= y <= bottom - RIM + 0.5, (name, gx, gy, squash)


def test_pupil_follows_the_gaze():
    e = _eyes("neutral")[0]
    look = {k: pupil_geometry(replace(e, look_x=x, look_y=y))
            for k, (x, y) in {"c": (0, 0), "l": (-1, 0), "r": (1, 0), "u": (0, -1), "d": (0, 1)}.items()}
    assert look["l"][0] < look["c"][0] - 8 and look["r"][0] > look["c"][0] + 8
    assert look["u"][1] < look["c"][1] - 8 and look["d"][1] > look["c"][1] + 8


def test_catchlights_stay_put_and_inside_the_open_eye():
    for name in EXPRESSIONS:
        for e0 in _eyes(name):
            base = catchlights(e0)
            for gx, gy in GAZES:
                e = replace(e0, look_x=gx, look_y=gy)
                assert catchlights(e) == base  # they belong to the light, not the pupil
            for ox, oy, r in base:
                x, y = rotate(ox, oy, -e0.tilt)
                top, bottom = open_span_at(e0, x)
                assert abs(x) + r <= e0.w / 2, name
                assert top <= y - r and y + r <= bottom, name


def test_no_pupil_on_happy_crescents_or_shut_eyes():
    for name in ("happy", "affectionate", "celebrating"):
        assert all(pupil_geometry(e) is None for e in _eyes(name))
    e = _eyes("neutral")[0]
    assert pupil_geometry(e) is not None and len(catchlights(e)) == 2
    shut = replace(e, h=MIN_LINE)  # bottom of a blink
    assert pupil_geometry(shut) is None and catchlights(shut) == []
    asleep = replace(shut, closed_curve=1.0)
    assert pupil_geometry(asleep) is None and catchlights(asleep) == []


def test_expression_pupil_sizes_shape_the_mood():
    def rx(name: str) -> float:
        p = pupil_geometry(_eyes(name)[1])
        assert p is not None
        return p[2] / _eyes(name)[1].w

    assert rx("surprised") < rx("neutral") < rx("sad")  # wide-eyed vs soft "puppy" eyes
