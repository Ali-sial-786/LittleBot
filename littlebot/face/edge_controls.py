"""MX Player-style edge swipes on the face: the right edge sets the speaker volume, the left the brightness.

``EdgeControls`` is pure (no pygame, no services): the display loop feeds it the press, the moves and
the release in normalised face coordinates (0..1) and it decides whether the finger is sliding a level.
A press in an edge zone followed by a mostly-vertical move past a small slop starts the control; up
raises, down lowers, relative to where the finger started, and a sweep of ``SWEEP`` of the screen's
height covers the level's whole range. Values are applied through ``Level`` callbacks, rate limited,
with an audible/visible ``feedback`` at most every ``FEEDBACK_INTERVAL``. It also keeps the little
bar's state: shown while sliding and ``SHOW_SECONDS`` after the last change, then faded out.

``draw_bar`` draws that bar in the games' candy style (littlebot/games/art.py). ``Brightness`` dims the
screen: through a real backlight (``/sys/class/backlight/*``) when one is writable without root,
otherwise in software, by a cached black veil over the face that is re-made only when the level changes.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

log = logging.getLogger("littlebot.face.edge_controls")

# Tuned for the MPI3508 3.5" screen shown at 720x480: a fingertip is ~60 px there.
EDGE_ZONE = 0.14  # of the face width, on each side (~100 px)
CANVAS = (720, 480)  # px the slop is measured in (normalised moves are scaled to this)
SLOP_PX = 12.0  # a finger must move this far before it is a slide (else: a tap or a hold)
VERTICAL = 1.3  # |dy| >= VERTICAL * |dx|: mostly vertical (within ~37 degrees of straight up/down)
SWEEP = 0.70  # of the face height: one sweep covers a level's whole range
APPLY_INTERVAL = 0.1  # apply a sliding level at most ~10 times a second
FEEDBACK_INTERVAL = 0.25  # the volume blip at most every 0.25 s
STILL_SECONDS = 0.3  # a finger resting this long in an edge zone is a tap / hold: its usual reaction plays
SHOW_SECONDS = 3.0  # the bar stays this long after the last change...
FADE_SECONDS = 0.3  # ...then fades out
ECHO_SECONDS = 1.0  # a setting change this soon after our own is our own echo (no bar restart)
KEY_STEP = 0.1  # keyboard nudges (desktop simulator)
BRIGHTNESS_FLOOR = 0.15  # software dimming never goes darker (never black)
BACKLIGHT_DIR = "/sys/class/backlight"

IDLE, PENDING, ACTIVE, REJECTED = "idle", "pending", "active", "rejected"


@dataclass
class Level:
    """One controllable level: how to read it, apply it, its range and optional feedback(value, final)."""

    name: str
    get: Callable[[], float]
    apply: Callable[[float], Any]
    lo: float = 0.0
    hi: float = 1.0
    feedback: Callable[[float, bool], Any] | None = None

    def clamp(self, value: float) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = self.hi
        if not math.isfinite(v):
            v = self.hi
        return round(max(self.lo, min(self.hi, v)), 3)

    def fill(self, value: float) -> float:
        """How full the bar is: a fraction of the level's ceiling."""
        return max(0.0, min(1.0, value / self.hi)) if self.hi > 0 else 0.0


class Swipe(NamedTuple):
    """A finished change: which level, from and to."""

    name: str
    start: float
    end: float


class Bar(NamedTuple):
    """What the bar shows now: the level, its side, value, fill (0..1) and opacity (0..1)."""

    name: str
    side: str
    value: float
    fill: float
    alpha: float


class EdgeControls:
    """The edge-swipe gesture and the bar's state. Feed ``press`` / ``move`` / ``release``; ``tick`` per frame."""

    def __init__(self, left: Level | None = None, right: Level | None = None, *,
                 clock: Callable[[], float] = time.monotonic, zone: float = EDGE_ZONE,
                 slop_px: float = SLOP_PX, canvas: tuple[int, int] = CANVAS, sweep: float = SWEEP,
                 apply_interval: float = APPLY_INTERVAL, feedback_interval: float = FEEDBACK_INTERVAL) -> None:
        self.sides: dict[str, Level | None] = {"left": left, "right": right}
        self.clock = clock
        self.zone = zone
        self.slop_px = slop_px
        self.canvas = canvas
        self.sweep = max(0.05, sweep)
        self.apply_interval = apply_interval
        self.feedback_interval = feedback_interval
        self.state = IDLE
        self.side: str | None = None  # the side being pressed / slid
        self.value = 0.0  # the level as the finger sets it
        self.pressed_at = 0.0
        self._x0 = self._y0 = 0.0
        self._start = 0.0
        self._applied: float | None = None
        self._applied_at = -math.inf
        self._fed: float | None = None
        self._fed_at = -math.inf
        self._own_at = -math.inf  # last change we made (to tell our own setting echo from others)
        self._bar: tuple[str, float, float] | None = None  # (name, value, when last changed)

    # ------------------------------------------------------------------ queries
    def level(self, name: str) -> Level | None:
        for lv in self.sides.values():
            if lv is not None and lv.name == name:
                return lv
        return None

    def side_of(self, name: str) -> str | None:
        return next((s for s, lv in self.sides.items() if lv is not None and lv.name == name), None)

    def zone_at(self, nx: float) -> str | None:
        """"left" / "right" when ``nx`` is in an edge zone that controls something."""
        side = "left" if nx <= self.zone else "right" if nx >= 1.0 - self.zone else None
        return side if side is not None and self.sides.get(side) is not None else None

    @property
    def active(self) -> bool:
        return self.state == ACTIVE

    @property
    def tracking(self) -> bool:
        """A finger is down in an edge zone (deciding, or sliding)."""
        return self.state in (PENDING, ACTIVE)

    # ------------------------------------------------------------------ the gesture
    def press(self, nx: float, ny: float) -> bool:
        """A finger went down. True when it is in an edge zone (a slide may follow)."""
        self.state, self.side = IDLE, None
        side = self.zone_at(nx)
        if side is None:
            return False
        lv = self.sides[side]
        assert lv is not None
        try:
            start = lv.clamp(lv.get())
        except Exception:  # noqa: BLE001 - a level we cannot read is not controlled
            log.debug("could not read %s", lv.name, exc_info=True)
            return False
        self.state, self.side = PENDING, side
        self._x0, self._y0 = nx, ny
        self._start = self.value = self._applied = self._fed = start
        self.pressed_at = self.clock()
        return True

    def move(self, nx: float, ny: float) -> str:
        """The finger moved: returns the state ("active" while sliding, "rejected" when it is not a slide)."""
        if self.state == PENDING:
            dx = (nx - self._x0) * self.canvas[0]
            dy = (ny - self._y0) * self.canvas[1]
            if math.hypot(dx, dy) <= self.slop_px:
                return self.state
            if abs(dy) < VERTICAL * abs(dx):
                self.state = REJECTED  # sideways: not ours
                return self.state
            self.state = ACTIVE
            self._show(self._level().name, self.value, self.clock())
        if self.state == ACTIVE:
            self._slide(ny)
            self.tick()
        return self.state

    def release(self, nx: float, ny: float) -> Swipe | None:
        """The finger came up. A ``Swipe`` when it slid a level (applied in full by now), else None."""
        if self.state != ACTIVE:
            self.state, self.side = IDLE, None
            return None
        self._slide(ny)
        return self._finish()

    def cancel(self) -> Swipe | None:
        """Stop following the finger (something else took the screen); a slide keeps the level it reached."""
        if self.state == ACTIVE:
            return self._finish()
        self.state, self.side = IDLE, None
        return None

    def tick(self) -> None:
        """Apply a level that waited for the rate limit, and give feedback (call every frame)."""
        if self.state != ACTIVE:
            return
        lv = self._level()
        now = self.clock()
        if self.value != self._applied and now - self._applied_at >= self.apply_interval:
            self._apply(lv, now)
        if lv.feedback is not None and self._applied == self.value and self.value != self._fed \
                and now - self._fed_at >= self.feedback_interval:
            self._feed(lv, now, final=False)

    def nudge(self, name: str, delta: float) -> Swipe | None:
        """Step a level by ``delta`` (keyboard): applied at once, with feedback and the bar."""
        lv = self.level(name)
        if lv is None:
            return None
        try:
            start = lv.clamp(lv.get())
        except Exception:  # noqa: BLE001
            return None
        end = lv.clamp(start + delta)
        now = self.clock()
        try:
            lv.apply(end)
        except Exception:  # noqa: BLE001
            log.exception("could not set %s", lv.name)
            return None
        self._own_at = now
        if lv.feedback is not None:
            self._call_feedback(lv, end, True)
        self._show(name, end, now)
        return Swipe(name, start, end)

    # ------------------------------------------------------------------ the bar
    def show(self, name: str, value: float) -> bool:
        """A level changed some other way (dashboard, voice): show the bar. False when it is our own echo."""
        lv = self.level(name)
        if lv is None:
            return False
        now = self.clock()
        if self.state == ACTIVE and lv is self._level():
            return False  # the finger is in charge; a late echo of an earlier value must not jump the bar
        if self._bar is not None and self._bar[0] == name and now - self._own_at < ECHO_SECONDS:
            return False
        self._show(name, lv.clamp(value), now)
        return True

    def bar(self, now: float | None = None) -> Bar | None:
        """The bar to draw now, or None when it is gone."""
        if self._bar is None:
            return None
        name, value, at = self._bar
        lv, side = self.level(name), self.side_of(name)
        if lv is None or side is None:
            return None
        now = self.clock() if now is None else now
        if self.state == ACTIVE and self._level() is lv:
            alpha = 1.0
        else:
            age = now - at
            if age >= SHOW_SECONDS + FADE_SECONDS:
                self._bar = None
                return None
            alpha = 1.0 if age <= SHOW_SECONDS else 1.0 - (age - SHOW_SECONDS) / FADE_SECONDS
        return Bar(name, side, value, lv.fill(value), max(0.0, min(1.0, alpha)))

    # ------------------------------------------------------------------ internals
    def _level(self) -> Level:
        lv = self.sides.get(self.side or "")
        assert lv is not None
        return lv

    def _slide(self, ny: float) -> None:
        lv = self._level()
        ny = max(0.0, min(1.0, ny))
        target = lv.clamp(self._start + (self._y0 - ny) / self.sweep * (lv.hi - lv.lo))
        target = round(target, 2)  # whole percent: finger jitter doesn't spam changes
        target = max(lv.lo, min(lv.hi, target))
        if target != self.value:
            self.value = target
            self._show(lv.name, target, self.clock())

    def _finish(self) -> Swipe | None:
        lv = self._level()
        now = self.clock()
        if self.value != self._applied:
            self._apply(lv, now)
        if lv.feedback is not None and self.value != self._fed:
            self._feed(lv, now, final=True)
        swipe = Swipe(lv.name, self._start, self.value)
        self._show(lv.name, self.value, now)
        self.state, self.side = IDLE, None
        return swipe

    def _apply(self, lv: Level, now: float) -> None:
        self._applied, self._applied_at, self._own_at = self.value, now, now
        try:
            lv.apply(self.value)
        except Exception:  # noqa: BLE001 - a failing setter must not break the touch screen
            log.exception("could not set %s", lv.name)

    def _feed(self, lv: Level, now: float, *, final: bool) -> None:
        self._fed, self._fed_at = self.value, now
        self._call_feedback(lv, self.value, final)

    @staticmethod
    def _call_feedback(lv: Level, value: float, final: bool) -> None:
        try:
            assert lv.feedback is not None
            lv.feedback(value, final)
        except Exception:  # noqa: BLE001
            log.debug("%s feedback failed", lv.name, exc_info=True)

    def _show(self, name: str, value: float, now: float) -> None:
        self._bar = (name, value, now)


# ====================================================================== brightness
class Backlight:
    """A sysfs backlight (``brightness`` / ``max_brightness``) we may write without root."""

    def __init__(self, path: Path, max_raw: int) -> None:
        self.path = path
        self.max_raw = max_raw

    @property
    def name(self) -> str:
        return self.path.name

    def write(self, level: float) -> bool:
        raw = max(1, min(self.max_raw, round(level * self.max_raw)))
        try:
            (self.path / "brightness").write_text(f"{raw}\n")
            return True
        except OSError as e:
            log.warning("backlight %s: could not write brightness (%s); using software dimming", self.name, e)
            return False

    def read(self) -> float | None:
        try:
            return int((self.path / "brightness").read_text().strip()) / self.max_raw
        except (OSError, ValueError):
            return None


def find_backlight(sysfs: str | os.PathLike[str] | None = BACKLIGHT_DIR) -> Backlight | None:
    """The first backlight under ``sysfs`` whose brightness this user may write (no sudo, ever)."""
    if sysfs is None:
        return None
    root = Path(sysfs)
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return None
    for d in dirs:
        bright, top = d / "brightness", d / "max_brightness"
        try:
            max_raw = int(top.read_text().strip())
        except (OSError, ValueError):
            continue
        if max_raw <= 0 or not bright.exists():
            continue
        if not os.access(bright, os.W_OK):
            log.info("backlight %s needs root to change; using software dimming", d.name)
            continue
        log.info("backlight %s found (max %d)", d.name, max_raw)
        return Backlight(d, max_raw)
    return None


class Brightness:
    """The screen's brightness, BRIGHTNESS_FLOOR..1: a real backlight when writable, else a black veil."""

    def __init__(self, level: float = 1.0, *, sysfs: str | os.PathLike[str] | None = None) -> None:
        self.backlight = find_backlight(sysfs)
        self.level = self.clamp(level)
        self._veil: Any = None
        self._veil_key: tuple[Any, ...] | None = None

    @staticmethod
    def clamp(value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 1.0
        return round(max(BRIGHTNESS_FLOOR, min(1.0, v)), 3) if math.isfinite(v) else 1.0

    @property
    def mode(self) -> str:
        return "backlight" if self.backlight is not None else "software dimming"

    def set(self, level: float) -> float:
        level = self.clamp(level)
        if self.backlight is not None and not self.backlight.write(level):
            self.backlight = None  # fall back for good
        self.level = level
        return level

    def apply(self, surface: Any, rect: Any = None) -> bool:
        """Software dimming: darken ``rect`` of ``surface`` (the drawn frame). True when it dimmed."""
        if self.backlight is not None or self.level >= 0.999:
            return False
        import pygame

        rect = rect or surface.get_rect()
        alpha = round((1.0 - self.level) * 255)
        key = (tuple(rect.size), alpha)
        if self._veil_key != key:  # one cached surface, re-made only when the level (or size) changes
            veil = pygame.Surface(rect.size)
            veil.fill((0, 0, 0))
            veil.set_alpha(alpha)
            self._veil, self._veil_key = veil, key
        surface.blit(self._veil, rect.topleft)
        return True


# ====================================================================== the bar
BAR_X = 0.2  # bar centre, as a fraction of the width from its edge: just inside the zone, beside the finger
BAR_W, BAR_H = 96, 340  # px on the 720x480 canvas (~0.4 x 1.4 inch on the 3.5" screen)
TRACK_W, TRACK_TOP, TRACK_H = 30, 72, 190
ICON = 60
_COLOURS = {"volume": ((78, 192, 240), (150, 224, 255)),  # SKY, light
            "brightness": ((255, 160, 72), (255, 210, 72))}  # TANGERINE, LEMON
_BAR_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()


def draw_bar(surface: Any, rect: Any, bar: Bar, shade: float = 1.0) -> Any:
    """Draw ``bar`` on its side of ``rect`` (scaled with the face). Returns the rect drawn into.

    ``shade`` < 1 darkens it (drawn over software dimming it stays readable, but not glaring at night).
    """
    from littlebot.games import art

    s = rect.h / CANVAS[1]
    pct = round(bar.value * 100)
    sprite = _bar_sprite(bar.name, pct, round(bar.fill * 200), round(s, 2), round(shade * 20))
    off = BAR_X * rect.w
    cx = rect.x + off if bar.side == "left" else rect.right - off
    return art.blit_center(surface, sprite, (cx, rect.centery), alpha=round(255 * bar.alpha))


def _bar_sprite(name: str, pct: int, fill_q: int, s: float, shade_q: int = 20) -> Any:
    key = (name, pct, fill_q, s, shade_q)
    hit = _BAR_CACHE.get(key)
    if hit is not None:
        _BAR_CACHE.move_to_end(key)
        return hit
    surf = _make_bar(name, pct, fill_q / 200, s)
    if shade_q < 20:
        import pygame

        k = max(0, min(255, int(255 * shade_q / 20)))
        surf = surf.copy()
        surf.fill((k, k, k), special_flags=pygame.BLEND_RGB_MULT)  # darker, same shape (alpha untouched)
    _BAR_CACHE[key] = surf
    while len(_BAR_CACHE) > 8:
        _BAR_CACHE.popitem(last=False)
    return surf


def _make_bar(name: str, pct: int, fill: float, s: float) -> Any:
    """A cream pill with the percentage on top, a candy-coloured track and the level's icon below."""
    import pygame

    from littlebot.games import art

    w, h = round(BAR_W * s), round(BAR_H * s)
    shadow = max(2, round(4 * s))
    panel = art.rounded_rect(w, h, w // 2, art.CREAM, shadow=shadow, border=art.mix(art.BG_BOTTOM, art.INK_SOFT, 0.25),
                             border_w=max(2, round(3 * s)))
    out = pygame.Surface(panel.get_size(), pygame.SRCALPHA)
    out.blit(panel, (0, 0))
    strong, light = _COLOURS.get(name, _COLOURS["volume"])
    label = art.text(f"{pct}%", max(14, round(40 * s)), art.INK)
    out.blit(label, label.get_rect(center=(w // 2, round(40 * s))))
    tw, th, ty = round(TRACK_W * s), round(TRACK_H * s), round(TRACK_TOP * s)
    tx = (w - tw) // 2
    out.blit(art.rounded_rect(tw, th, tw // 2, art.mix(art.BG_BOTTOM, art.INK_SOFT, 0.14)), (tx, ty))
    if fill > 0.001:
        fh = max(tw, round(th * fill))  # never shorter than round: it stays a pill
        out.blit(art.rounded_rect(tw, fh, tw // 2, strong, shine=False), (tx, ty + th - fh))
        hl = max(2, tw // 4)  # a soft highlight down the left of the fill
        if fh > tw:
            stripe = art.rounded_rect(hl, fh - tw // 2, hl // 2, light)
            out.blit(stripe, (tx + max(2, tw // 5), ty + th - fh + tw // 4))
    icon = _icon(name, round(ICON * s), fill, strong)
    out.blit(icon, icon.get_rect(center=(w // 2, ty + th + (h - ty - th) // 2)))
    return art._fast(out)


def _icon(name: str, size: int, fill: float, colour: tuple[int, int, int]) -> Any:
    import pygame

    from littlebot.games import art

    ink = art.INK

    def speaker(surf: Any, k: float) -> None:
        u = size * k
        pygame.draw.rect(surf, ink, pygame.Rect(0.08 * u, 0.36 * u, 0.2 * u, 0.28 * u),
                         border_radius=int(0.05 * u))
        pygame.draw.polygon(surf, ink, [(0.24 * u, 0.36 * u), (0.5 * u, 0.13 * u), (0.5 * u, 0.87 * u),
                                        (0.24 * u, 0.64 * u)])
        if fill <= 0.001:  # muted: a little cross instead of waves
            art.capsule(surf, ink, (0.64 * u, 0.36 * u), (0.9 * u, 0.64 * u), 0.08 * u)
            art.capsule(surf, ink, (0.64 * u, 0.64 * u), (0.9 * u, 0.36 * u), 0.08 * u)
            return
        waves = 1 if fill < 0.34 else 2 if fill < 0.67 else 3
        for i in range(waves):
            _arc(surf, colour if i == waves - 1 else ink, (0.5 * u, 0.5 * u), (0.16 + 0.13 * i) * u, 0.075 * u, 48)

    def sun(surf: Any, k: float) -> None:
        u = size * k
        c = (0.5 * u, 0.5 * u)
        for i in range(8):
            a = i * math.pi / 4
            p0 = (c[0] + math.cos(a) * 0.32 * u, c[1] + math.sin(a) * 0.32 * u)
            p1 = (c[0] + math.cos(a) * 0.45 * u, c[1] + math.sin(a) * 0.45 * u)
            art.capsule(surf, colour, p0, p1, 0.085 * u)
        pygame.draw.circle(surf, ink, c, 0.24 * u)
        pygame.draw.circle(surf, colour, c, 0.18 * u)

    return art.supersampled((size, size), speaker if name == "volume" else sun)


def _arc(surf: Any, colour: tuple[int, int, int], c: tuple[float, float], r: float, width: float,
         half_deg: float) -> None:
    """A thick arc opening to the right with round ends (a sound wave)."""
    import pygame

    n = 16
    angles = [math.radians(-half_deg + 2 * half_deg * i / n) for i in range(n + 1)]
    outer = [(c[0] + math.cos(a) * (r + width / 2), c[1] + math.sin(a) * (r + width / 2)) for a in angles]
    inner = [(c[0] + math.cos(a) * (r - width / 2), c[1] + math.sin(a) * (r - width / 2)) for a in reversed(angles)]
    pygame.draw.polygon(surf, colour, outer + inner)
    for a in (angles[0], angles[-1]):  # round ends
        pygame.draw.circle(surf, colour, (c[0] + math.cos(a) * r, c[1] + math.sin(a) * r), width / 2)
