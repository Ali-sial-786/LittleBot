"""Expression parameter sets: completeness, ranges, interpolation."""

from __future__ import annotations

from dataclasses import fields

import pytest

from littlebot.core.interfaces import EXPRESSIONS, FACE_STATES
from littlebot.face.expressions import (
    DEFAULT_EXPRESSION_FOR_STATE, EXPRESSION_PARAMS, EXTRAS, EYE_RANGES, EyeParams, ExpressionParams, clamp_eye,
    get_expression, lerp_expression,
)


def test_every_expression_has_a_parameter_set():
    assert set(EXPRESSION_PARAMS) == set(EXPRESSIONS)
    for name in EXPRESSIONS:
        p = get_expression(name)
        assert isinstance(p, ExpressionParams)
        assert isinstance(p.left, EyeParams) and isinstance(p.right, EyeParams)


@pytest.mark.parametrize("name", EXPRESSIONS)
def test_parameters_are_complete_and_in_range(name):
    p = EXPRESSION_PARAMS[name]
    for eye in (p.left, p.right):
        assert {f.name for f in fields(EyeParams)} >= set(EYE_RANGES)
        for field_name, (lo, hi) in EYE_RANGES.items():
            v = getattr(eye, field_name)
            assert lo <= v <= hi, f"{name}.{field_name}={v} outside [{lo}, {hi}]"
        assert clamp_eye(eye) == eye
    assert set(p.extras) <= set(EXTRAS)
    assert 0.0 <= p.tint_mix <= 1.0
    assert 0.3 <= p.brightness <= 1.2
    assert -1.0 <= p.gaze_x <= 1.0 and -1.0 <= p.gaze_y <= 1.0
    assert p.blink_rate > 0 and p.blink_speed > 0
    assert all(0 <= c <= 255 for c in p.tint)


def test_expressions_are_visually_distinct():
    items = list(EXPRESSION_PARAMS.items())
    for i, (a_name, a) in enumerate(items):
        for b_name, b in items[i + 1:]:
            assert a != b, f"{a_name} and {b_name} are identical"


def test_signature_features():
    E = EXPRESSION_PARAMS
    assert E["happy"].left.smile > 0.4 and E["happy"].right.smile > 0.4  # "^ ^"
    assert E["surprised"].left.scale > 1.05  # wide
    assert E["sleepy"].left.lid_top >= 0.4 and "zzz" in E["sleepy"].extras  # heavy lids
    assert E["sad"].left.lid_angle > 5 and E["sad"].tint_mix > 0.2  # drooping, bluer
    assert E["sad"].tint[2] > E["sad"].tint[0]
    assert E["curious"].left.scale != E["curious"].right.scale  # one eye bigger
    assert E["thinking"].gaze_y < 0 and E["thinking"].gaze_x != 0 and "dots" in E["thinking"].extras
    assert "ring" in E["listening"].extras
    assert "mouth" in E["speaking"].extras
    assert E["confused"].left != E["confused"].right and "question" in E["confused"].extras
    assert E["playful"].left.smile != E["playful"].right.smile  # wink-ish
    assert {"hearts", "blush"} <= E["affectionate"].extras
    assert {"sparkles", "confetti"} <= E["celebrating"].extras


def test_default_expression_for_every_state():
    assert set(DEFAULT_EXPRESSION_FOR_STATE) == set(FACE_STATES)
    assert set(DEFAULT_EXPRESSION_FOR_STATE.values()) <= set(EXPRESSIONS)


def test_lerp_endpoints_and_midpoint():
    a, b = EXPRESSION_PARAMS["neutral"], EXPRESSION_PARAMS["happy"]
    assert lerp_expression(a, b, 0.0) == a
    assert lerp_expression(a, b, 1.0) == b
    mid = lerp_expression(a, b, 0.5)
    assert a.left.smile < mid.left.smile < b.left.smile
    assert mid.left.smile == pytest.approx((a.left.smile + b.left.smile) / 2)
    assert mid.extras == b.extras


def test_lerp_tint_never_passes_through_unrelated_colour():
    a, b = EXPRESSION_PARAMS["neutral"], EXPRESSION_PARAMS["sad"]
    mid = lerp_expression(a, b, 0.3)
    # neutral has no tint, so the in-between tint is the sad tint itself (only the mix grows)
    assert mid.tint == pytest.approx(b.tint)
    assert 0 < mid.tint_mix < b.tint_mix


def test_clamp_eye():
    e = EyeParams(lid_top=3.0, tilt=-90, smile=-1)
    c = clamp_eye(e)
    assert c.lid_top == EYE_RANGES["lid_top"][1]
    assert c.tilt == EYE_RANGES["tilt"][0]
    assert c.smile == 0.0
