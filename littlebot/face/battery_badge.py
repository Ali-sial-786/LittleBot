"""The face's little battery sign: a battery with its level (and a bolt while charging) in a corner, for a moment.

Shown when the charger is plugged in, when someone asks about the battery, and on waking from a doze while
the battery is low (``power.show`` from littlebot/power). ``BatteryBadge`` is pure (state and fading, no
pygame), fed from the bus by the display loop; ``draw_badge`` draws it in the same candy style as the edge
swipes' volume bar (littlebot/face/edge_controls.py): a cream pill, plum ink, cached sprites.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, NamedTuple

FADE_IN_SECONDS = 0.2
FADE_SECONDS = 0.4
MAX_SECONDS = 30.0
MARGIN = 14  # px from the face's top-left corner, on the 720x480 canvas
BADGE_W, BADGE_H = 168, 64
CANVAS_H = 480
LOW_PERCENT = 20
_CACHE: OrderedDict[tuple[Any, ...], Any] = OrderedDict()


class Badge(NamedTuple):
    percent: int | None  # None: level unknown (the sign still shows charging)
    charging: bool
    alpha: float


class BatteryBadge:
    """When the sign shows and what it shows. ``show`` may be called from any thread (bus callbacks)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._shown: tuple[int | None, bool, float, float] | None = None  # (percent, charging, since, until)

    def show(self, percent: Any, charging: Any, seconds: Any = 4.0) -> None:
        try:
            pct: int | None = None if percent is None else max(0, min(100, int(round(float(percent)))))
        except (TypeError, ValueError):
            pct = None
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            secs = 4.0
        secs = max(0.5, min(MAX_SECONDS, secs)) if math.isfinite(secs) else 4.0
        now = self.clock()
        with self._lock:
            since = self._shown[2] if self._shown is not None and self._shown[3] > now else now
            self._shown = (pct, bool(charging), since, now + secs)

    def on_event(self, event: Any) -> None:
        """A ``power.show`` event: {percent, charging, seconds}."""
        get = getattr(event, "get", None)
        if callable(get):
            self.show(get("percent"), get("charging") or get("charged"), get("seconds", 4.0))

    def hide(self) -> None:
        with self._lock:
            self._shown = None

    def current(self, now: float | None = None) -> Badge | None:
        now = self.clock() if now is None else now
        with self._lock:
            shown = self._shown
            if shown is None:
                return None
            pct, charging, since, until = shown
            if now >= until + FADE_SECONDS:
                self._shown = None
                return None
        if now < until:
            alpha = min(1.0, max(0.0, (now - since) / FADE_IN_SECONDS)) if FADE_IN_SECONDS > 0 else 1.0
        else:
            alpha = 1.0 - (now - until) / FADE_SECONDS
        return Badge(pct, charging, max(0.0, min(1.0, alpha)))

    @property
    def visible(self) -> bool:
        return self.current() is not None


def badge_rect(rect: Any) -> tuple[int, int, int, int]:
    """(x, y, w, h) the sign takes in the face ``rect`` (scaled with the face): its top-left corner."""
    s = rect.h / CANVAS_H
    m = round(MARGIN * s)
    return rect.x + m, rect.y + m, round(BADGE_W * s), round(BADGE_H * s)


def level_colour(percent: int | None) -> tuple[int, int, int]:
    from littlebot.games import art  # noqa: PLC0415

    if percent is None:
        return art.INK_SOFT
    if percent <= LOW_PERCENT:
        return art.STRAWBERRY
    if percent < 50:
        return art.LEMON
    return art.MINT


def draw_badge(surface: Any, rect: Any, badge: Badge, shade: float = 1.0) -> Any:
    """Draw ``badge`` in the top-left corner of the face ``rect``; returns the rect drawn into."""
    import pygame  # noqa: PLC0415

    x, y, w, h = badge_rect(rect)
    sprite = _sprite(badge.percent, badge.charging, w, h, round(shade * 20))
    alpha = round(255 * badge.alpha)
    if alpha < 255:
        sprite.set_alpha(alpha)
        surface.blit(sprite, (x, y))
        sprite.set_alpha(255)
    else:
        surface.blit(sprite, (x, y))
    return pygame.Rect(x, y, sprite.get_width(), sprite.get_height())


def _sprite(percent: int | None, charging: bool, w: int, h: int, shade_q: int) -> Any:
    key = (percent, charging, w, h, shade_q)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    surf = _make(percent, charging, max(24, w), max(10, h))
    if shade_q < 20:
        import pygame  # noqa: PLC0415

        k = max(0, min(255, int(255 * shade_q / 20)))
        surf = surf.copy()
        surf.fill((k, k, k), special_flags=pygame.BLEND_RGB_MULT)
    _CACHE[key] = surf
    while len(_CACHE) > 8:
        _CACHE.popitem(last=False)
    return surf


def _make(percent: int | None, charging: bool, w: int, h: int) -> Any:
    """A cream pill: a battery (ink outline, a coloured fill for the level, a bolt while charging), then "57%"."""
    import pygame  # noqa: PLC0415

    from littlebot.games import art  # noqa: PLC0415

    s = h / BADGE_H
    shadow = max(2, round(3 * s))
    panel = art.rounded_rect(w, h, h // 2, art.CREAM, shadow=shadow, border=art.mix(art.BG_BOTTOM, art.INK_SOFT, 0.25),
                             border_w=max(2, round(3 * s)))
    out = pygame.Surface(panel.get_size(), pygame.SRCALPHA)
    out.blit(panel, (0, 0))
    colour = level_colour(percent)
    bw, bh = round(64 * s), round(34 * s)
    fill = 0.0 if percent is None else percent / 100.0

    def battery(surf: Any, k: float) -> None:
        u = k
        W, H = bw * u, bh * u
        line = 4.0 * s * u
        nub_w, nub_h = 5.0 * s * u, H * 0.42
        body = pygame.Rect(0, 0, W - nub_w, H)
        pygame.draw.rect(surf, art.INK, body, border_radius=int(H * 0.26))
        inner = body.inflate(-2 * line, -2 * line)
        pygame.draw.rect(surf, art.CREAM, inner, border_radius=int(H * 0.18))
        pygame.draw.rect(surf, art.INK, pygame.Rect(body.right - 1, (H - nub_h) / 2, nub_w + 1, nub_h),
                         border_radius=int(nub_w * 0.5))
        gap = line * 0.7
        cell = inner.inflate(-2 * gap, -2 * gap)
        if fill > 0.005:
            fw = max(cell.h * 0.5, cell.w * min(1.0, fill))
            pygame.draw.rect(surf, colour, pygame.Rect(cell.x, cell.y, fw, cell.h), border_radius=int(cell.h * 0.25))
        if charging:  # a bolt over the middle, ink-outlined so it reads over any fill
            cx, cy, r = body.centerx, body.centery, H * 0.42
            bolt = [(cx + 0.15 * r, cy - r), (cx - 0.45 * r, cy + 0.12 * r), (cx - 0.02 * r, cy + 0.12 * r),
                    (cx - 0.18 * r, cy + r), (cx + 0.45 * r, cy - 0.15 * r), (cx + 0.02 * r, cy - 0.15 * r)]
            pygame.draw.polygon(surf, art.INK, bolt)
            shrink = [(cx + (px - cx) * 0.72, cy + (py - cy) * 0.72) for px, py in bolt]
            pygame.draw.polygon(surf, art.LEMON, shrink)

    icon = art.supersampled((bw, bh), battery)
    pad = round(14 * s)
    out.blit(icon, (pad, (h - bh) // 2))
    label = art.text("?" if percent is None else f"{percent}%", max(12, round(34 * s)), art.INK)
    lx = pad + bw + round(8 * s)
    out.blit(label, label.get_rect(midleft=(lx, h // 2 + round(1 * s))))
    return art._fast(out)
