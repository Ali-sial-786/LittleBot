"""Frame helpers shared by the vision modules.

* ``get_cv2()``            - OpenCV if importable, else None (vision is an optional extra).
* ``resize_to_width`` / ``to_gray`` - cheap pre-processing, with numpy fallbacks.
* synthetic drawing        - numpy-only shapes used by the mock camera and the tests.
  Nothing here ever loads or stores a photo of a real person.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import time
from collections import deque
from typing import Any, Iterator

import numpy as np

log = logging.getLogger("littlebot.vision")

Color = tuple[int, int, int]


@functools.lru_cache(maxsize=1)
def get_cv2() -> Any | None:
    """Return the ``cv2`` module, or None when OpenCV is not installed / broken."""
    try:
        import cv2  # noqa: PLC0415 - optional heavy dependency

        return cv2
    except Exception as e:  # noqa: BLE001 - ImportError or a broken native lib
        log.info("OpenCV unavailable (%s); vision runs in mock / degraded mode", e)
        return None


@contextlib.contextmanager
def quiet_opencv() -> Iterator[None]:
    """Temporarily raise OpenCV's native log level to ERROR (e.g. harmless DNN backend warnings)."""
    cv2 = get_cv2()
    lg = getattr(getattr(cv2, "utils", None), "logging", None) if cv2 is not None else None
    if lg is None:
        yield
        return
    try:
        old = lg.getLogLevel()
        lg.setLogLevel(lg.LOG_LEVEL_ERROR)
    except Exception:  # noqa: BLE001
        yield
        return
    try:
        yield
    finally:
        lg.setLogLevel(old)


def opencv_version() -> str | None:
    cv2 = get_cv2()
    return getattr(cv2, "__version__", None) if cv2 is not None else None


# ---------------------------------------------------------------------------
# Pre-processing
# ---------------------------------------------------------------------------

def resize_to_width(frame: np.ndarray, width: int) -> np.ndarray:
    """Downscale (never upscale) keeping the aspect ratio. Returns the input if already small."""
    h, w = frame.shape[:2]
    if width <= 0 or w <= width:
        return frame
    new_h = max(1, int(round(h * width / w)))
    cv2 = get_cv2()
    if cv2 is not None:
        return cv2.resize(frame, (width, new_h), interpolation=cv2.INTER_AREA)
    # numpy fallback: nearest-neighbour sampling (good enough for mocks / tests)
    ys = (np.arange(new_h) * h / new_h).astype(np.intp)
    xs = (np.arange(width) * w / width).astype(np.intp)
    return frame[ys][:, xs]


def to_gray(frame: np.ndarray) -> np.ndarray:
    """BGR (or already-gray) uint8 frame -> single-channel uint8."""
    if frame.ndim == 2:
        return frame
    if frame.shape[2] == 1:
        return frame[:, :, 0]
    cv2 = get_cv2()
    if cv2 is not None:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    b, g, r = frame[:, :, 0], frame[:, :, 1], frame[:, :, 2]
    return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.uint8)


def ensure_bgr(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 3 and frame.shape[2] == 3:
        return frame
    gray = frame if frame.ndim == 2 else frame[:, :, 0]
    return np.repeat(gray[:, :, None], 3, axis=2)


# ---------------------------------------------------------------------------
# Rate measurement
# ---------------------------------------------------------------------------

class RateMeter:
    """Events per second over a sliding window (thread-safe enough for stats: single writer)."""

    def __init__(self, window: float = 2.0) -> None:
        self.window = float(window)
        self._ts: deque[float] = deque(maxlen=512)

    def tick(self, now: float | None = None) -> None:
        self._ts.append(time.monotonic() if now is None else now)

    def rate(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        ts = [t for t in list(self._ts) if now - t <= self.window]
        if len(ts) < 2:
            return 0.0
        span = ts[-1] - ts[0]
        return (len(ts) - 1) / span if span > 0 else 0.0

    def reset(self) -> None:
        self._ts.clear()


# ---------------------------------------------------------------------------
# Synthetic drawing (numpy only)
# ---------------------------------------------------------------------------

def blank_frame(width: int, height: int, color: Color = (70, 60, 50), *, gradient: bool = True) -> np.ndarray:
    """A plain BGR frame; a soft vertical gradient makes it look less artificial to detectors."""
    frame = np.empty((height, width, 3), dtype=np.uint8)
    frame[:] = np.asarray(color, dtype=np.uint8)
    if gradient and height > 1:
        ramp = np.linspace(-15, 15, height, dtype=np.float32)[:, None, None]
        frame = np.clip(frame.astype(np.float32) + ramp, 0, 255).astype(np.uint8)
    return frame


def draw_rect(frame: np.ndarray, x: int, y: int, w: int, h: int, color: Color | int) -> None:
    """Filled axis-aligned rectangle in pixel coords (clipped, in place)."""
    H, W = frame.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(W, int(x + w)), min(H, int(y + h))
    if x1 > x0 and y1 > y0:
        frame[y0:y1, x0:x1] = color


def draw_ellipse(frame: np.ndarray, cx: float, cy: float, rx: float, ry: float, color: Color | int) -> None:
    """Filled ellipse in pixel coords (clipped, in place). Only touches its bounding box."""
    H, W = frame.shape[:2]
    if rx <= 0 or ry <= 0:
        return
    x0, x1 = max(0, int(cx - rx)), min(W, int(cx + rx) + 1)
    y0, y1 = max(0, int(cy - ry)), min(H, int(cy + ry) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    yy, xx = np.ogrid[y0:y1, x0:x1]
    mask = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
    frame[y0:y1, x0:x1][mask] = color


def draw_cartoon_face(frame: np.ndarray, cx: float, cy: float, width: float,
                      skin: Color = (140, 170, 215)) -> None:
    """A simple face-like pattern (skin oval, dark eyes, mouth) centred at pixel (cx, cy).

    It is a test pattern for the mock camera, not a realistic face: real detectors
    may or may not fire on it, which is why mock pipelines use ``MockDetector``.
    """
    rx, ry = width / 2.0, width * 0.62
    draw_ellipse(frame, cx, cy, rx, ry, skin)
    eye_dx, eye_y, eye_r = width * 0.2, cy - ry * 0.18, max(1.0, width * 0.07)
    dark = (40, 30, 30)
    draw_ellipse(frame, cx - eye_dx, eye_y, eye_r * 1.3, eye_r, dark)
    draw_ellipse(frame, cx + eye_dx, eye_y, eye_r * 1.3, eye_r, dark)
    draw_ellipse(frame, cx, cy + ry * 0.45, width * 0.18, max(1.0, width * 0.04), (60, 50, 120))


def brighten_if_dark(img: Any, *, dark_below: float = 70.0, target: float = 115.0) -> Any:
    """Lift a dim photo (mean brightness < ``dark_below`` of 255) with a gamma curve so a vision model
    can make out hands and toys in a dim room. Bright enough photos are returned unchanged."""
    cv2 = get_cv2()
    if cv2 is None or img is None or getattr(img, "size", 0) == 0:
        return img
    import numpy as np  # noqa: PLC0415

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    mean = float(gray.mean())
    if mean >= dark_below or mean < 1.0:
        return img
    gamma = max(0.25, min(1.0, np.log(target / 255.0) / np.log(mean / 255.0)))
    lut = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)], dtype=np.uint8)
    return cv2.LUT(img, lut)
