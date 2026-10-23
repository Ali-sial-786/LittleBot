"""Local finger counting (littlebot.vision.hands): sure answers only, everything else is "unsure"."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from littlebot.vision import hands as H
from littlebot.vision.hands import FingerCounter, HandCount, finger_states, load_finger_counter, number_word
from littlebot.vision.imaging import get_cv2

# --------------------------------------------------------------------------- synthetic hands (metres)
KNUCKLES = {5: (0.03, 0.09), 9: (0.01, 0.095), 13: (-0.01, 0.09), 17: (-0.03, 0.085)}


def hand(up: tuple[bool, bool, bool, bool, bool], *, half_bent: int | None = None) -> np.ndarray:
    """21 world joints: palm in the z=0 plane, fingers pointing +y. ``up``: thumb, index..little."""
    w = np.zeros((21, 3))
    thumb_up, *fingers = up
    if thumb_up:  # straight out to the side
        w[1:5] = [(0.02, 0.02, 0), (0.045, 0.035, 0), (0.07, 0.05, 0), (0.095, 0.065, 0)]
    else:  # tucked across the palm
        w[1:5] = [(0.02, 0.02, 0), (0.04, 0.03, 0.01), (0.03, 0.05, 0.03), (0.0, 0.06, 0.035)]
    for i, (mcp, is_up) in enumerate(zip((5, 9, 13, 17), fingers, strict=True)):
        x, y = KNUCKLES[mcp]
        base = np.array([x, y, 0.0])
        w[mcp] = base
        if i == half_bent:  # bent halfway: neither clearly up nor folded
            pts = [base + (0, 0.03, 0.025), base + (0, 0.035, 0.05), base + (0, 0.03, 0.07)]
        elif is_up:
            pts = [base + (0, 0.04, 0), base + (0, 0.065, 0), base + (0, 0.085, 0)]
        else:  # folded: forward at the knuckle, then back towards the palm
            pts = [base + (0, 0, 0.04), base + (0, -0.025, 0.04), base + (0, -0.04, 0.025)]
        w[mcp + 1:mcp + 4] = pts
    return w


def test_open_hand_fist_and_numbers():
    assert finger_states(hand((True, True, True, True, True))) == [True] * 5
    assert finger_states(hand((False, False, False, False, False))) == [False] * 5
    assert finger_states(hand((False, True, True, False, False))) == [False, True, True, False, False]
    assert finger_states(hand((True, False, False, False, False))) == [True, False, False, False, False]


def test_a_half_bent_finger_is_unsure():
    states = finger_states(hand((False, True, True, True, False), half_bent=2))
    assert states[3] is None and states[:3] == [False, True, True]


def test_turning_the_hand_does_not_change_the_count():
    w = hand((False, True, True, True, False))
    a = np.radians(70)
    rot = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    tilt = np.array([[1, 0, 0], [0, np.cos(a / 2), -np.sin(a / 2)], [0, np.sin(a / 2), np.cos(a / 2)]])
    assert finger_states(w @ rot.T @ tilt.T) == finger_states(w) == [False, True, True, True, False]


def test_degenerate_joints_are_unsure_not_a_crash():
    assert finger_states(np.zeros((21, 3))) == [None] * 5


def test_palm_anchors_match_mediapipe():
    a = H._anchors()
    assert a.shape == (2016, 2)
    np.testing.assert_allclose(a[0], [1 / 48, 1 / 48], atol=1e-6)
    np.testing.assert_allclose(a[1], a[0])
    np.testing.assert_allclose(a[2], [3 / 48, 1 / 48], atol=1e-6)
    np.testing.assert_allclose(a[1152], [1 / 24, 1 / 24], atol=1e-6)  # first 16 px cell: 6 anchors
    np.testing.assert_allclose(a[-1], [23 / 24, 23 / 24], atol=1e-6)


def test_number_words():
    assert [number_word(n) for n in (1, 2, 5, 10, 11)] == ["one", "two", "five", "ten", "11"]


# --------------------------------------------------------------------------- the counting rules (models stubbed)
FRAME = np.zeros((480, 640, 3), dtype=np.uint8)


def palm(score: float, x: float = 300) -> np.ndarray:
    return np.r_[[x, 200, x + 60, 260], np.zeros(14), score]


def stub(palms: list[np.ndarray], hands: list[tuple[np.ndarray, np.ndarray, float] | None]) -> FingerCounter:
    fc = object.__new__(FingerCounter)
    fc._lock = threading.Lock()
    found = iter(hands)
    fc.palms = lambda img, floor=H.PALM_SCORE: [p for p in palms if p[-1] >= floor]  # type: ignore[method-assign]
    fc.landmarks = lambda img, p: next(found)  # type: ignore[method-assign]
    return fc


def seen(world: np.ndarray, *, x0: float = 250, y0: float = 150) -> tuple[np.ndarray, np.ndarray, float]:
    joints = np.c_[np.linspace(x0, x0 + 120, 21), np.linspace(y0, y0 + 150, 21)]
    return joints, world, 0.98


def test_counts_both_hands():
    fc = stub([palm(0.9, 100), palm(0.8, 400)],
              [seen(hand((True,) * 5), x0=60), seen(hand((False, True, True, False, False)), x0=380)])
    out = fc.count(FRAME)
    assert out == HandCount(7, 2, True, "", out.ms, (5, 2)) and out.ms >= 0


@pytest.mark.parametrize("palms, hands, why", [
    ([], [], "no_hand"),
    ([palm(0.9), palm(0.3, 500)], [seen(hand((True,) * 5))], "maybe_another_hand"),  # a weak second palm
    ([palm(0.9, 50), palm(0.9, 250), palm(0.9, 450)], [], "too_many_hands"),
    ([palm(0.9)], [None], "unclear_hand"),
    ([palm(0.9)], [seen(hand((True,) * 5), x0=2)], "hand_cut_off"),
    ([palm(0.9)], [seen(hand((False, True, True, True, False), half_bent=1))], "unclear_finger"),
    ([palm(0.9)], [seen(hand((False,) * 5))], "fist"),  # ChatGPT describes a fist better than "zero"
])
def test_anything_doubtful_is_unsure(palms, hands, why):
    out = stub(palms, hands).count(FRAME)
    assert out.sure is False and out.why == why


def test_a_photo_that_is_not_a_jpeg_is_unsure():
    if get_cv2() is None:
        pytest.skip("OpenCV needed")
    assert stub([], []).count_jpeg(b"not a jpeg").why == "decode"


def test_no_models_no_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "_shared", None)
    monkeypatch.setattr(H, "_load_failed", False)
    assert load_finger_counter(tmp_path) is None


def test_a_broken_model_file_is_reported_once(tmp_path, monkeypatch, caplog):
    if get_cv2() is None:
        pytest.skip("OpenCV needed")
    monkeypatch.setattr(H, "_shared", None)
    monkeypatch.setattr(H, "_load_failed", False)
    palm_path, hand_path = H.model_paths(tmp_path)
    palm_path.parent.mkdir(parents=True)
    palm_path.write_bytes(b"not onnx")
    hand_path.write_bytes(b"not onnx")
    assert load_finger_counter(tmp_path) is None and load_finger_counter(tmp_path) is None
    assert caplog.text.count("finger counter unavailable") == 1


class FakeNet:
    def __init__(self) -> None:
        self.inputs: list[tuple[int, ...]] = []

    def setInput(self, blob: np.ndarray) -> None:  # noqa: N802 - cv2.dnn's name
        self.inputs.append(blob.shape)

    def forward(self, names: list[str]) -> list[np.ndarray]:
        return []


def test_warm_up_runs_both_networks_once():
    fc = object.__new__(FingerCounter)
    fc._lock = threading.Lock()
    fc._palm, fc._hand = FakeNet(), FakeNet()
    fc._palm_out, fc._hand_out = ["palm"], ["hand"]
    fc.warm_up()
    assert fc._palm.inputs == [(1, H.PALM_INPUT, H.PALM_INPUT, 3)]
    assert fc._hand.inputs == [(1, H.HAND_INPUT, H.HAND_INPUT, 3)]


def test_loading_also_warms_up(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(H, "_shared", None)
    monkeypatch.setattr(H, "_load_failed", False)
    monkeypatch.setattr(H, "get_cv2", lambda: object())
    palm_path, hand_path = H.model_paths(tmp_path)
    palm_path.parent.mkdir(parents=True)
    palm_path.write_bytes(b"x")
    hand_path.write_bytes(b"x")
    warmed: list[int] = []

    class Loaded:
        def __init__(self, palm, hand) -> None:
            pass

        def warm_up(self) -> None:
            warmed.append(1)

    monkeypatch.setattr(H, "FingerCounter", Loaded)
    caplog.set_level("INFO", logger="littlebot.vision.hands")
    counter = load_finger_counter(tmp_path)
    assert isinstance(counter, Loaded) and warmed == [1] and load_finger_counter(tmp_path) is counter
    assert "finger counter loaded in" in caplog.text and "warmed up in" in caplog.text


def test_the_real_models_if_installed():
    """On a robot with ``scripts/download_models.py hands`` run: loads, finds no hand in a blank frame, fast."""
    if get_cv2() is None:
        pytest.skip("OpenCV needed")
    palm_path, hand_path = H.model_paths()
    if not palm_path.is_file() or not hand_path.is_file():
        pytest.skip("hand models not downloaded")
    fc = FingerCounter(palm_path, hand_path)
    fc.warm_up()
    t0 = time.perf_counter()
    out = fc.count(FRAME)
    assert out.sure is False and out.why == "no_hand"
    assert time.perf_counter() - t0 < 2.0
