"""Rock, paper or scissors, read from the camera on the robot (Rock Paper Scissors' "show me" mode).

It uses the finger counter's hand models (``littlebot.vision.hands``, ``scripts/download_models.py
hands``): a few camera frames just after "Shoot!", each read in about 0.1 s. The frames stay in
memory for that read only; nothing is sent anywhere or kept. Without the models, a camera, or with
the camera switched off in Privacy, the game is played by tapping as before.

The answer is the hand's shape once it has settled: people pump a fist through "Rock... Paper...
Scissors...", so the first frame is taken a moment after "Shoot!" and two frames in a row must agree.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from littlebot.vision.hands import load_finger_counter

log = logging.getLogger("littlebot.games")

SIGNS = ("rock", "paper", "scissors")
SETTLE_S = 0.3  # after "Shoot!": the hand is still moving into its shape
LOOK_S = 1.4  # give up (and ask) when no two frames in a row agreed by then
PHOTO_TIMEOUT_S = 1.0
# A finger is straight when its tip is this much further from the wrist than its middle joint, folded
# when it is no further than this. The finger counter's own rules are stricter (it would rather say
# "unsure" than miscount) and call almost every fist unclear. Measured on TensorFlow's rock-paper-scissors
# test pictures (372 hands): folded fingers 0.80-1.16 (5th-95th percentile), straight ones 1.23-1.48.
STRAIGHT_RATIO, FOLDED_RATIO = 1.2, 1.12
_FINGERS = ((6, 8), (10, 12), (14, 16), (18, 20))  # index..little: (middle joint, tip), MediaPipe order


def fingers_straight(world: Any) -> list[bool | None]:
    """[index, middle, ring, little]: True straight, False folded, None in between (3D joints)."""
    w = np.asarray(world, dtype=np.float64).reshape(21, 3)
    out: list[bool | None] = []
    for mid, tip in _FINGERS:
        base = float(np.linalg.norm(w[mid] - w[0]))
        ratio = float(np.linalg.norm(w[tip] - w[0])) / base if base > 1e-6 else 0.0
        out.append(True if ratio >= STRAIGHT_RATIO else False if 0 < ratio <= FOLDED_RATIO else None)
    return out


def sign_from_fingers(fingers: Sequence[bool | None]) -> str | None:
    """rock / paper / scissors from [index, middle, ring, little] (True straight, False folded, None
    unclear); the thumb doesn't matter. None when the hand shows none of them clearly."""
    fingers = list(fingers)
    up = sum(1 for s in fingers if s is True)
    down = sum(1 for s in fingers if s is False)
    if down == 4 or (down == 3 and up == 0):
        return "rock"
    if up == 4 or (up == 3 and down == 0):
        return "paper"
    index, middle, ring, little = fingers
    if index is True and middle is True and ring is not True and little is not True and down >= 1:
        return "scissors"
    return None


def read_sign(counter: Any, img: Any) -> tuple[str | None, str]:
    """(sign, why-not) for the biggest hand in a BGR image."""
    lock = getattr(counter, "_lock", None) or contextlib.nullcontext()
    with lock:  # the counter's networks run one image at a time
        palms = counter.palms(img)
        if not palms:
            return None, "no_hand"
        palm = max(palms, key=lambda p: float((p[2] - p[0]) * (p[3] - p[1])))
        hand = counter.landmarks(img, palm)
    if hand is None:
        return None, "unclear_hand"
    sign = sign_from_fingers(fingers_straight(hand[1]))
    return (sign, "") if sign else (None, "unclear_sign")


@dataclass
class HandLook:
    """One look for the game to poll: ``done`` once ``sign`` (or ``why`` it has none) is known."""

    done: bool = False
    sign: str | None = None
    why: str = ""
    reads: list[str | None] = field(default_factory=list)

    def finish(self, sign: str | None, why: str = "") -> None:
        self.sign, self.why, self.done = sign, why, True


def blocked_reason(ctx: Any) -> str | None:
    """Why the game can't look right now (None = it can): camera switch, camera, hand models."""
    try:
        if not ctx.privacy.camera_enabled:
            return "camera_off"
    except Exception:  # noqa: BLE001
        return "camera_off"
    vision = getattr(ctx, "vision", None)
    if vision is None:
        return "no_camera"
    try:
        camera = (vision.status() or {}).get("camera") if hasattr(vision, "status") else None
    except Exception:  # noqa: BLE001
        camera = None
    if camera in ("unavailable", "disconnected"):  # unplugged: tapping instead of a look per round
        return "no_camera"
    if load_finger_counter() is None:
        return "no_models"
    return None


async def look_for_sign(ctx: Any, look: HandLook, *, settle: float = SETTLE_S, limit: float = LOOK_S) -> None:
    """Fill in ``look`` from camera frames: the first sign seen in two frames in a row."""
    try:
        reason = blocked_reason(ctx)
        if reason is not None:
            look.finish(None, reason)
            return
        cv2 = _cv2()
        counter = load_finger_counter()
        await asyncio.sleep(settle)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            jpeg = await ctx.vision.capture_photo(max_width=640, quality=80, timeout=PHOTO_TIMEOUT_S)
            img = await asyncio.to_thread(_decode, cv2, jpeg)
            sign, why = await asyncio.to_thread(read_sign, counter, img) if img is not None else (None, "decode")
            look.reads.append(sign)
            if sign is not None and look.reads[-2:] == [sign, sign]:
                look.finish(sign)
                return
            look.why = why or "unsteady"
        look.finish(None, look.why or "unsteady")
    except asyncio.CancelledError:
        look.finish(None, "stopped")
        raise
    except Exception as e:  # noqa: BLE001 - the game then asks for a tap instead
        code = getattr(e, "reason", None) or type(e).__name__
        look.finish(None, str(code))
    finally:
        log.info("rock paper scissors: camera saw %s (%s; reads %s)", look.sign or "nothing sure", look.why or "ok",
                 look.reads)


def _cv2() -> Any:
    from littlebot.vision.imaging import get_cv2  # noqa: PLC0415

    return get_cv2()


def _decode(cv2: Any, jpeg: bytes) -> Any:
    return cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR) if cv2 is not None else None
