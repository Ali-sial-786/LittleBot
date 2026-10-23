"""Local finger counting: "how many fingers am I holding up?" answered on the robot in about 0.1 s.

Two small models from the OpenCV Model Zoo (Apache-2.0, converted from Google MediaPipe Hands),
run with OpenCV's DNN module like the YuNet face detector (nothing to compile or pip-install):

* ``palm_detection_mediapipe_2023feb.onnx``      (3.9 MB): where the palms are (192x192 input);
* ``handpose_estimation_mediapipe_2023feb.onnx`` (4.1 MB): 21 joints per hand (224x224 input).

Get them with ``scripts/download_models.py hands``. Without them ``load_finger_counter`` returns
None and every look goes to ChatGPT as before.

Privacy: it only finds hand joints in the one photo it is given, in memory. Nothing is kept,
stored or sent, and it says nothing about who anyone is. It answers only when it is sure: one or
two clearly visible hands, every finger clearly up or clearly folded. Anything else (no hand, a
blurred or side-on hand, a finger half bent, a fist, a hand cut off by the frame) is "unsure", and
ChatGPT answers instead.

Checked on MediaPipe's sample hand photos (0, 1, 2 and 2x5 fingers) placed in a 640x480 frame in
1,800 variants (shrunk to a quarter, turned +-30 degrees, mirrored, dark, blurred, noisy): it was sure
for 68% and wrong for 4 of those 1,216 (all small AND dark or noisy). Time on an x86 VM: ~40-80 ms
per photo with 2 OpenCV threads (palm ~30-45 ms + ~10-16 ms per hand); YuNet runs 1.5x slower on the
Pi 5 than on that VM, so expect ~60-130 ms there. The first count after loading takes ~1.5-2x that
(far more with cold disk caches), so ``load_finger_counter`` warms both networks up; a live call loads
it when it starts. Memory: ~26 MB once loaded (~20 ms to load, ~70 ms to warm up on that VM).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from littlebot.vision.imaging import get_cv2, quiet_opencv

log = logging.getLogger("littlebot.vision.hands")

PALM_MODEL = "models/palm_detection_mediapipe_2023feb.onnx"
HAND_MODEL = "models/handpose_estimation_mediapipe_2023feb.onnx"

PALM_INPUT = 192
HAND_INPUT = 224
PALM_SCORE = 0.6  # palm detector score (after sigmoid) to count a hand
PALM_DOUBT = 0.2  # a weaker palm elsewhere in the picture may be a hand it would miss: then unsure
PALM_NMS = 0.3
HAND_PRESENCE = 0.9  # landmark model's "this is a hand" confidence needed to count it
MAX_HANDS = 2
# Finger states from the metric 3D joints (so they don't depend on how the hand is turned). Index to
# little finger: "up" when the tip is well beyond the middle joint (tip-to-wrist over middle-joint-to-
# wrist distance) AND the finger is fairly straight (bend summed over its two upper joints); "folded"
# when the tip is back near the palm AND it is clearly bent. Anything in between is unsure, and so is
# the whole count. Measured on MediaPipe's sample hands: up = ratio 1.31-1.59 / bend 10-52 deg,
# folded = ratio 0.60-0.83 / bend 122-185 deg.
UP_RATIO, DOWN_RATIO = 1.15, 0.95
UP_BEND, DOWN_BEND = 75.0, 100.0
# Thumb: up when its tip points away from the hand (tip-to-little-knuckle over thumb-joint-to-little-
# knuckle distance) and its last joint is fairly straight; folded when it is tucked across the palm.
# Measured: up = 1.15-1.37 / 10-31 deg, folded = 0.73-0.75 / 54-75 deg.
THUMB_UP_RATIO, THUMB_DOWN_RATIO = 1.05, 0.9
THUMB_UP_BEND = 40.0
EDGE_MARGIN = 0.01  # joints this close (fraction of the frame) to the border: the hand is cut off

_FINGERS = ((5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20))  # index..little: knuckle..tip
_PALM_BASE, _PALM_MIDDLE = 0, 2  # palm-detector keypoints used to turn the hand upright


@dataclass(frozen=True)
class HandCount:
    """What the counter saw. ``fingers`` is only meaningful when ``sure``."""

    fingers: int
    hands: int
    sure: bool
    why: str = ""  # for the log: why it is unsure (labels only, never image content)
    ms: float = 0.0
    per_hand: tuple[int, ...] = field(default_factory=tuple)


def _anchors() -> np.ndarray:
    """SSD anchor centres of MediaPipe's 192x192 palm detector: strides 8 (2/cell) and 16 (6/cell)."""
    out: list[tuple[float, float]] = []
    for stride, per_cell in ((8, 2), (16, 6)):
        n = PALM_INPUT // stride
        for y in range(n):
            for x in range(n):
                out.extend([((x + 0.5) / n, (y + 0.5) / n)] * per_cell)
    return np.asarray(out, dtype=np.float32)  # (2016, 2)


def _angle(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-6 or nb < 1e-6:
        return 180.0
    return float(np.degrees(np.arccos(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))))


def finger_states(world: np.ndarray) -> list[bool | None]:
    """[thumb, index, middle, ring, little]: True = up, False = folded, None = can't tell.

    ``world``: the 21 metric 3D joints (MediaPipe order). Angles in 3D don't depend on how the hand is
    turned in the picture, so a hand held sideways or pointing down counts the same."""
    w = np.asarray(world, dtype=np.float64).reshape(21, 3)

    def dist(a: int, b: int) -> float:
        return float(np.linalg.norm(w[a] - w[b]))

    def state(ratio: float, bend: float, up_ratio: float, down_ratio: float, up_bend: float,
              down_bend: float | None) -> bool | None:
        if ratio >= up_ratio and bend <= up_bend:
            return True
        if ratio <= down_ratio and (down_bend is None or bend >= down_bend):
            return False
        return None

    base = dist(3, 17)
    states: list[bool | None] = [
        None if base < 1e-6 else state(dist(4, 17) / base, _angle(w[3] - w[2], w[4] - w[3]),
                                       THUMB_UP_RATIO, THUMB_DOWN_RATIO, THUMB_UP_BEND, None)]
    for knuckle, pip, dip, tip in _FINGERS:
        mid = dist(pip, 0)
        bend = _angle(w[pip] - w[knuckle], w[dip] - w[pip]) + _angle(w[dip] - w[pip], w[tip] - w[dip])
        states.append(None if mid < 1e-6 else state(dist(tip, 0) / mid, bend, UP_RATIO, DOWN_RATIO, UP_BEND,
                                                    DOWN_BEND))
    return states


class FingerCounter:
    """Palm detector + hand landmarks via ``cv2.dnn``. Not thread-safe: one ``count`` at a time (locked)."""

    def __init__(self, palm_model: str | Path, hand_model: str | Path) -> None:
        cv2 = get_cv2()
        if cv2 is None:
            raise RuntimeError("OpenCV is not installed")
        with quiet_opencv():
            self._palm = cv2.dnn.readNet(str(palm_model))
            self._hand = cv2.dnn.readNet(str(hand_model))
        self._palm_out = list(self._palm.getUnconnectedOutLayersNames())
        self._hand_out = ["Identity", "Identity_1", "Identity_2", "Identity_3"]  # joints, presence, handedness, 3D
        self._anchors = _anchors()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ public
    def count_jpeg(self, jpeg: bytes) -> HandCount:
        cv2 = get_cv2()
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR) if cv2 is not None else None
        if img is None:
            return HandCount(0, 0, False, "decode")
        return self.count(img)

    def count(self, img: np.ndarray) -> HandCount:
        """Raised fingers on the (at most two) hands in a BGR image; ``sure`` only when there is no doubt."""
        t0 = time.perf_counter()
        with self._lock:
            out = self._count(img)
        ms = (time.perf_counter() - t0) * 1000
        return HandCount(out.fingers, out.hands, out.sure, out.why, round(ms, 1), out.per_hand)

    def warm_up(self) -> None:
        """Run both networks once on blank input, so the first real count isn't the slow one (OpenCV
        sets up each network on its first run: ~1.5-2x a normal count, far more with cold caches)."""
        with self._lock:
            for net, size in ((self._palm, PALM_INPUT), (self._hand, HAND_INPUT)):
                net.setInput(np.zeros((1, size, size, 3), dtype=np.float32))
                net.forward(self._palm_out if net is self._palm else self._hand_out)

    # ------------------------------------------------------------ internals
    def _count(self, img: np.ndarray) -> HandCount:
        found = self.palms(img, floor=PALM_DOUBT)
        palms = [p for p in found if p[-1] >= PALM_SCORE]
        if not palms:
            return HandCount(0, 0, False, "no_hand")
        if len(palms) < len(found):  # e.g. a second hand half hidden: counting one would be wrong
            return HandCount(0, len(palms), False, "maybe_another_hand")
        if len(palms) > MAX_HANDS:
            return HandCount(0, len(palms), False, "too_many_hands")
        h, w = img.shape[:2]
        per_hand: list[int] = []
        for palm in palms:
            hand = self.landmarks(img, palm)
            if hand is None:
                return HandCount(0, len(palms), False, "unclear_hand")
            joints, world, presence = hand
            margin = EDGE_MARGIN * max(w, h)
            if (joints[:, 0].min() < margin or joints[:, 1].min() < margin or joints[:, 0].max() > w - margin
                    or joints[:, 1].max() > h - margin):
                return HandCount(0, len(palms), False, "hand_cut_off")
            states = finger_states(world)
            if any(s is None for s in states):
                return HandCount(0, len(palms), False, "unclear_finger")
            per_hand.append(sum(1 for s in states if s))
        total = sum(per_hand)
        if total == 0:
            return HandCount(0, len(per_hand), False, "fist", per_hand=tuple(per_hand))  # ChatGPT says it better
        return HandCount(total, len(per_hand), True, per_hand=tuple(per_hand))

    def palms(self, img: np.ndarray, *, floor: float = PALM_SCORE) -> list[np.ndarray]:
        """Palms scoring ``floor`` or more, best first: [x1, y1, x2, y2, 14 keypoint coords, score] in pixels."""
        cv2 = get_cv2()
        h, w = img.shape[:2]
        ratio = PALM_INPUT / max(h, w)
        nh, nw = max(1, int(h * ratio)), max(1, int(w * ratio))
        small = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        top, left = (PALM_INPUT - nh) // 2, (PALM_INPUT - nw) // 2
        pad = cv2.copyMakeBorder(small, top, PALM_INPUT - nh - top, left, PALM_INPUT - nw - left,
                                 cv2.BORDER_CONSTANT, value=(0, 0, 0))
        blob = cv2.cvtColor(pad, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis] / 255.0
        self._palm.setInput(blob)
        outs = self._palm.forward(self._palm_out)
        reg = next(o for o in outs if o.shape[-1] == 18)[0]
        score = 1.0 / (1.0 + np.exp(-np.clip(next(o for o in outs if o.shape[-1] == 1)[0, :, 0], -50, 50)))
        keep = np.where(score >= floor)[0]
        if keep.size == 0:
            return []
        scale = PALM_INPUT / ratio  # model units (0..1 of the padded square) -> image pixels
        bias = np.array([left, top], dtype=np.float32) / ratio
        anchors = self._anchors[keep]
        r = reg[keep]
        cxy = r[:, 0:2] / PALM_INPUT + anchors
        wh = r[:, 2:4] / PALM_INPUT
        boxes = np.concatenate([(cxy - wh / 2) * scale - bias, (cxy + wh / 2) * scale - bias], axis=1)
        kps = (r[:, 4:].reshape(-1, 7, 2) / PALM_INPUT + anchors[:, None, :]) * scale - bias
        rects = [[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in boxes]
        idx = cv2.dnn.NMSBoxes(rects, score[keep].astype(float).tolist(), floor, PALM_NMS)
        idx = np.asarray(idx).reshape(-1)
        return [np.r_[boxes[i], kps[i].reshape(-1), score[keep][i]] for i in idx]

    def landmarks(self, img: np.ndarray, palm: np.ndarray) -> tuple[np.ndarray, np.ndarray, float] | None:
        """(21x2 image-pixel joints, 21x3 metric world joints, presence) for one palm, or None if unsure."""
        cv2 = get_cv2()
        box = palm[0:4].reshape(2, 2)
        kps = palm[4:18].reshape(7, 2)
        # Turn the hand upright (wrist below the middle finger), as MediaPipe does.
        p1, p2 = kps[_PALM_BASE], kps[_PALM_MIDDLE]
        angle = float(np.degrees(np.pi / 2 - np.arctan2(-(p2[1] - p1[1]), p2[0] - p1[0])))
        angle = (angle + 180.0) % 360.0 - 180.0
        center = box.mean(axis=0)
        rot = cv2.getRotationMatrix2D((float(center[0]), float(center[1])), angle, 1.0)
        pts = np.c_[kps, np.ones(7)] @ rot.T  # palm keypoints in the rotated image
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        size = hi - lo
        mid = (lo + hi) / 2 + np.array([0.0, -0.4]) * size  # the fingers are above the palm
        side = float(max(size) * 3.0)  # palm -> whole hand
        if side < 8:
            return None
        # One affine warp: rotate around the palm and cut the side x side square around ``mid`` to 224x224.
        s = HAND_INPUT / side
        full = np.vstack([rot, [0.0, 0.0, 1.0]])
        crop = np.array([[s, 0.0, HAND_INPUT / 2 - s * mid[0]], [0.0, s, HAND_INPUT / 2 - s * mid[1]], [0.0, 0.0, 1.0]])
        m = (crop @ full)[:2]
        patch = cv2.warpAffine(img, m, (HAND_INPUT, HAND_INPUT), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
        blob = cv2.cvtColor(patch, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis] / 255.0
        self._hand.setInput(blob)
        joints, presence, _handedness, world = self._hand.forward(self._hand_out)
        presence_f = float(np.asarray(presence).reshape(-1)[0])
        if presence_f < HAND_PRESENCE:
            return None
        j = np.asarray(joints, dtype=np.float64).reshape(21, 3)[:, :2]
        inv = cv2.invertAffineTransform(m)
        joints_img = np.c_[j, np.ones(21)] @ inv.T
        return joints_img, np.asarray(world, dtype=np.float64).reshape(21, 3), presence_f


def model_paths(root: Path | None = None) -> tuple[Path, Path]:
    from littlebot.core.paths import PROJECT_ROOT  # noqa: PLC0415

    base = root or PROJECT_ROOT
    return base / PALM_MODEL, base / HAND_MODEL


_shared: FingerCounter | None = None
_shared_lock = threading.Lock()
_load_failed = False


def load_finger_counter(root: Path | None = None) -> FingerCounter | None:
    """The shared counter, loaded and warmed up once (~50-100 ms; a live call does it when it starts);
    None when the models or OpenCV are missing."""
    global _shared, _load_failed
    with _shared_lock:
        if _shared is not None or _load_failed:
            return _shared
        palm, hand = model_paths(root)
        if get_cv2() is None or not palm.is_file() or not hand.is_file():
            return None  # checked again next time: the models may be downloaded while running
        try:
            t0 = time.perf_counter()
            counter = FingerCounter(palm, hand)
            t1 = time.perf_counter()
            counter.warm_up()
            _shared = counter
            log.info("finger counter loaded in %.0f ms, warmed up in %.0f ms", (t1 - t0) * 1000,
                     (time.perf_counter() - t1) * 1000)
        except Exception as e:  # noqa: BLE001 - a broken model must never break looking
            _load_failed = True
            log.warning("finger counter unavailable (%s)", type(e).__name__)
        return _shared


def number_word(n: int) -> str:
    words = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    return words[n] if 0 <= n < len(words) else str(n)


__all__ = ["FingerCounter", "HandCount", "finger_states", "load_finger_counter", "number_word"]
