"""The timer card: "set a timer for 10 minutes", "a 3-minute egg timer" (up to three at once).

Each timer is a big mm:ss inside a candy ring that drains as the time runs out. The card is sticky
while a timer runs (it stays after the chat ends). At zero it chimes a few times, gently, says "The egg
timer is done!" (and tells the live voice), shows "Done!" for a while and then goes away. A tap on a
finished timer stops it early.

A new timer asked for while a timer card is on screen joins that card (``build`` returns the card
already showing, or the one running behind another card). Time is kept on ``host.now()`` (monotonic), so
tests drive it with a fake clock.

When another card, a video or a game takes the screen, the timers carry on behind it (``runs_behind``; the
screen service keeps updating the card): they still chime and tell the voice at zero, and the card comes
back when the screen is free or a timer rings.
"""

from __future__ import annotations

import logging
import math
import re
import weakref
from dataclasses import dataclass
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, Card, CardHost, CardType, W, register
from littlebot.screens.cards import candy

log = logging.getLogger("littlebot.screens.timer")

MAX_TIMERS = 3
MAX_SECONDS = 24 * 3600
CHIMES = 3  # chimes when a timer is done...
CHIME_EVERY = 4.0  # ...this many seconds apart
DONE_SECONDS = 30.0  # "Done!" stays this long
RING_STEPS = 180  # the ring drains in this many steps (each a cached sprite)
COLORS: tuple[art.Color, ...] = (art.STRAWBERRY, art.SKY, art.GRAPE)
DONE_COLOR: art.Color = art.MINT
SEEN_S = 2.0  # a card updated this recently is on screen (a new timer joins it)
LAYOUT = {  # timers on the card -> (ring radius, ring width, centres)
    1: (158, 34, ((W // 2, 296),)),
    2: (122, 28, ((205, 304), (535, 304))),
    3: (100, 24, ((128, 310), (360, 310), (592, 310))),
}

_live: weakref.WeakSet[TimerCard] = weakref.WeakSet()


def clock_text(seconds: float) -> str:
    """ "9:05", "1:00:00" (whole seconds, rounded up: 0.2 s left still shows 0:01)."""
    s = max(0, math.ceil(seconds - 1e-6))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def spoken_length(seconds: float) -> str:
    """ "10 minutes", "1 hour 30 minutes", "45 seconds"."""
    s = round(seconds)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    parts = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return " ".join(parts) or "0 seconds"


def clean_label(label: Any) -> str:
    """ "Egg Timer" -> "egg"; "" for none."""
    s = " ".join(str(label or "").split())[:24]
    s = re.sub(r"\s*timer$", "", s, flags=re.IGNORECASE).strip()
    return s.lower() if s[1:].islower() or len(s) <= 1 else s


def _seconds(spec: dict[str, Any]) -> int | None:
    total = 0.0
    for key, k in (("seconds", 1), ("minutes", 60), ("hours", 3600)):
        v = spec.get(key)
        if v in (None, ""):
            continue
        try:
            total += float(v) * k
        except (TypeError, ValueError):
            return None
    total = round(total)
    return int(total) if 0 < total <= MAX_SECONDS else None


@dataclass
class Timer:
    label: str
    total: float
    color: art.Color
    end: float = 0.0  # host.now() when it runs out (running)
    left: float = 0.0  # seconds left (paused)
    state: str = "running"  # running | paused | ringing
    rang_at: float = 0.0
    chimes: int = 0

    def remaining(self, now: float) -> float:
        if self.state == "running":
            return max(0.0, self.end - now)
        return self.left if self.state == "paused" else 0.0

    @property
    def name(self) -> str:
        return f"{self.label} timer" if self.label else "timer"


def _ring(radius: int, width: int, color: art.Color, q: int) -> pygame.Surface:
    """A ring ``q``/RING_STEPS full (from twelve o'clock, clockwise) on a pale track."""
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = radius * k
            r_out, r_in, lw = radius * k, (radius - width) * k, max(2.0, 3 * k)
            track = art.lighten(color, 0.62)
            hole = (*track, 0)  # clear, but the track's colour: no dark fringe when it is scaled down
            pygame.draw.circle(s, track, (c, c), r_out - lw)
            pygame.draw.circle(s, hole, (c, c), r_in + lw)
            frac = q / RING_STEPS
            if frac <= 0:
                return
            for col, grow in ((art.inkier(color, 0.35), lw), (color, 0.0)):
                ro, ri = r_out - lw + grow, r_in + lw - grow
                if frac >= 1:
                    pygame.draw.circle(s, col, (c, c), ro)
                    pygame.draw.circle(s, hole, (c, c), ri)
                    continue
                n = max(6, int(120 * frac))
                angles = [2 * math.pi * frac * i / n for i in range(n + 1)]
                outer = [(c + ro * math.sin(a), c - ro * math.cos(a)) for a in angles]
                inner = [(c + ri * math.sin(a), c - ri * math.cos(a)) for a in reversed(angles)]
                pygame.draw.polygon(s, col, outer + inner)
                mid, cap = (ro + ri) / 2, (ro - ri) / 2
                for a in (0.0, angles[-1]):
                    pygame.draw.circle(s, col, (c + mid * math.sin(a), c - mid * math.cos(a)), cap)
            hl = (r_out + r_in) / 2
            if frac > 0.05:  # a little shine near the start
                pygame.draw.circle(s, art.lighten(color, 0.55), (c + hl * math.sin(0.12), c - hl * math.cos(0.12)),
                                   width * k * 0.14)

        return art.supersampled((2 * radius, 2 * radius), draw, ss=2)  # redrawn as it drains: keep it quick

    return art.cached(("timer_ring", radius, width, color, q), make)


class TimerCard(Card):
    """Up to three countdowns."""

    kind = "timer"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, ctx: Any = None) -> None:
        super().__init__(spec, host)
        self.ctx = ctx  # the robot it belongs to (a new timer for the same robot joins this card)
        self.timers: list[Timer] = []
        self.particles = art.Particles()
        self._seen = -1e9
        self._colors = 0
        seconds = _seconds(spec)
        if seconds:
            self.add_timer(seconds, spec.get("label"))
        _live.add(self)

    # ------------------------------------------------------------ timers
    @property
    def sticky(self) -> bool:  # type: ignore[override]
        return bool(self.timers) and not self.done

    @property
    def shown(self) -> bool:
        """On screen: the display loop updated it moments ago."""
        return not self.done and self.host.now() - self._seen <= SEEN_S

    @property
    def runs_behind(self) -> bool:
        """A timer still counts down (or is paused): when another card, a video or a game takes the screen, the
        screen service keeps this card going behind it (littlebot/screens/service.py)."""
        return not self.done and any(t.state != "ringing" for t in self.timers)

    @property
    def ringing(self) -> bool:
        """A timer has just finished: the card wants the screen back."""
        return not self.done and any(t.state == "ringing" for t in self.timers)

    def go_behind(self) -> None:
        """The card leaves the screen while its timers carry on: the ones that already rang are let go (they
        were heard; they must not pull the card straight back)."""
        rang = [t for t in self.timers if t.state == "ringing"]
        for t in rang:
            self.timers.remove(t)
        if rang:
            self._retitle()

    def add_timer(self, seconds: float, label: Any = "") -> Timer | None:
        if len(self.timers) >= MAX_TIMERS or seconds <= 0:
            return None
        color = COLORS[self._colors % len(COLORS)]
        self._colors += 1
        t = Timer(clean_label(label), float(seconds), color, end=self.host.now() + float(seconds))
        self.timers.append(t)
        self._retitle()
        return t

    def _retitle(self) -> None:
        if len(self.timers) == 1:
            name = self.timers[0].name
            self.title = name[:1].upper() + name[1:]
        else:
            self.title = f"{len(self.timers)} timers" if self.timers else "Timer"

    def _find(self, args: dict[str, Any]) -> list[Timer]:
        """The timers ``args["label"]`` names (all when it names none)."""
        want = clean_label(args.get("label")).lower()
        if not want:
            return list(self.timers)
        return [t for t in self.timers if t.label and (want in t.label.lower() or t.label.lower() in want)]

    def command(self, action: str, args: dict[str, Any]) -> bool:
        args = args if isinstance(args, dict) else {}
        now = self.host.now()
        if action in ("new", "start"):
            seconds = _seconds(args)
            return seconds is not None and self.add_timer(seconds, args.get("label")) is not None
        found = self._find(args)
        if not found:
            return False
        if action == "add":
            seconds = _seconds(args) if any(k in args for k in ("seconds", "minutes", "hours")) else None
            if seconds is None:
                try:
                    seconds = int(float(args.get("seconds")))  # a negative number takes time off
                except (TypeError, ValueError):
                    return False
            t = found[-1]  # the newest one it names
            if t.state == "ringing":  # "five more minutes": it starts again
                t.state, t.total, t.end, t.chimes = "running", float(max(1, seconds)), now + max(1, seconds), 0
            elif t.state == "paused":
                t.left = min(MAX_SECONDS, max(0.0, t.left + seconds))
                t.total = max(t.total + seconds, t.left, 1.0)
            else:
                t.end = min(now + MAX_SECONDS, max(now, t.end + seconds))
                t.total = max(t.total + seconds, t.end - now, 1.0)
            return True
        if action == "pause":
            for t in found:
                if t.state == "running":
                    t.state, t.left = "paused", t.remaining(now)
            return True
        if action == "resume":
            for t in found:
                if t.state == "paused":
                    t.state, t.end = "running", now + t.left
            return True
        if action in ("cancel", "stop"):
            for t in found:
                self.timers.remove(t)
            self._retitle()
            if not self.timers:
                self.done = True
            return True
        return False

    def tap(self, x: float, y: float) -> bool:
        rings = self._layout()
        for t, (cx, cy, r) in zip(list(self.timers), rings):
            if t.state == "ringing" and math.hypot(x - cx, y - cy) <= r + 20:
                self.timers.remove(t)  # stopped early
                self._retitle()
                if not self.timers:
                    self.done = True
                return True
        return False

    def key(self, name: str) -> bool:
        if name == "space":
            running = any(t.state == "running" for t in self.timers)
            return self.command("pause" if running else "resume", {})
        return False

    # ------------------------------------------------------------ time
    def update(self, dt: float) -> None:
        super().update(dt)
        now = self.host.now()
        self._seen = now
        self.particles.update(dt)
        for t in list(self.timers):
            if t.state == "running" and now >= t.end:
                self._ring(t, now)
            if t.state == "ringing":
                if t.chimes < CHIMES and now >= t.rang_at + t.chimes * CHIME_EVERY:
                    t.chimes += 1
                    self.host.sound("chime")
                if now - t.rang_at >= DONE_SECONDS:
                    self.timers.remove(t)
                    self._retitle()
        if not self.timers and not self.done:
            self.done = True
        self.mood = "happy" if any(t.state == "ringing" for t in self.timers) else "neutral"

    def _ring(self, t: Timer, now: float) -> None:
        t.state, t.rang_at, t.chimes = "ringing", now, 1
        self.host.sound("chime")
        self.host.say(f"The {t.name} is done!")
        self.host.tell(f"The {t.name} ({spoken_length(t.total)}) has just finished; the screen shows Done!")
        log.info("screens: a %s timer finished", spoken_length(t.total))
        for (cx, cy, r), other in zip(self._layout(), self.timers):
            if other is t:
                self.particles.burst(cx, cy, 26, art.CANDY, kinds=("star", "dot"), speed=(90, 230),
                                     life=(0.7, 1.3), radius=r * 0.6)

    # ------------------------------------------------------------ the voice
    def describe(self) -> str:
        now = self.host.now()
        parts = []
        for t in self.timers:
            name = t.name[:1].upper() + t.name[1:]
            if t.state == "ringing":
                parts.append(f"{name}: done!")
            elif t.state == "paused":
                parts.append(f"{name}: paused with {clock_text(t.remaining(now))} left")
            else:
                parts.append(f"{name}: {clock_text(t.remaining(now))} left")
        return "; ".join(parts) or "No timers"

    def status(self) -> dict[str, Any]:
        now = self.host.now()
        return {**super().status(), "timers": [{"label": t.label, "state": t.state, "left": round(t.remaining(now))}
                                               for t in self.timers]}

    # ------------------------------------------------------------ frames
    def _layout(self) -> list[tuple[int, int, int]]:
        n = max(1, min(MAX_TIMERS, len(self.timers)))
        r, _, centres = LAYOUT[n]
        return [(cx, cy, r) for cx, cy in centres]

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(art.background((W, 480), seed=3), (0, 0))
        dy = candy.slide_in(self.t)
        candy.header(surf, self.title, dy=dy * 0.5)
        n = max(1, min(MAX_TIMERS, len(self.timers)))
        r, width, _ = LAYOUT[n]
        now = self.host.now()
        for t, (cx, cy, _) in zip(self.timers, self._layout()):
            cy = int(cy + dy)
            inner = 2 * (r - width) * 0.84
            if t.state == "ringing":
                pulse = 0.5 + 0.5 * math.sin((now - t.rang_at) * 4.0)
                art.blit_center(surf, art.glow(r * 1.3, DONE_COLOR, 1.0), (cx, cy), int(90 + 150 * pulse))
                art.blit_center(surf, art.circle(r - width // 2, art.lighten(DONE_COLOR, 0.8)), (cx, cy))
                art.blit_center(surf, _ring(r, width, DONE_COLOR, RING_STEPS), (cx, cy))
                candy.blit_mid(surf, candy.fit("Done!", int(r * 0.62), inner), (cx, cy))
            else:
                left = t.remaining(now)
                q = math.ceil(RING_STEPS * min(1.0, left / max(1.0, t.total)))
                art.blit_center(surf, art.circle(r - width // 2, art.CREAM), (cx, cy))
                art.blit_center(surf, _ring(r, width, t.color, q), (cx, cy))
                big = candy.fit(clock_text(left), int(r * 0.7), inner, min_size=BODY_PX)
                if t.state == "paused":
                    candy.blit_mid(surf, big, (cx, cy - r * 0.12))
                    candy.blit_mid(surf, art.text("Paused", BODY_PX, art.INK), (cx, cy + r * 0.36))
                else:
                    candy.blit_mid(surf, big, (cx, cy))
            if n > 1:
                label = (t.label or "Timer")
                candy.blit_mid(surf, candy.fit(label[:1].upper() + label[1:], BODY_PX + 4, 2 * r + 16),
                               (cx, cy - r - 24))
        self.particles.draw(surf)


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> TimerCard | None:
    """A new timer card, or the timer card already on screen with this timer added to it."""
    seconds = _seconds(spec)
    if seconds is None:
        return None
    screens = getattr(ctx, "screens", None)
    current = getattr(screens, "current", None)  # the card the screen service shows now
    behind = [c for c in getattr(screens, "behind", None) or () if isinstance(c, TimerCard)]  # running unseen
    first = ([current] if isinstance(current, TimerCard) else []) + behind
    for card in first + (list(_live) if ctx is not None else []):
        if card.ctx is ctx and not card.done and (card in first or card.shown) and len(card.timers) < MAX_TIMERS:
            card.add_timer(seconds, spec.get("label"))
            return card
    return TimerCard(spec, host, ctx)


register(CardType(
    kind="timer",
    description="A countdown timer on screen ('set a timer for 10 minutes', 'a 3-minute egg timer'): a big "
                "mm:ss in a ring that drains; it chimes and says so when it is done. Up to 3 at once (a new "
                "timer joins the card already showing).",
    fields={
        "seconds": {"type": "integer", "description": "The whole length in seconds (10 minutes = 600, 1 hour "
                                                      "= 3600)."},
        "label": {"type": "string", "description": "What it is for, one or two words: 'egg', 'pasta', 'nap'. "
                                                   "Optional."},
    },
    build=build,
    required=("seconds",),
    actions={
        "add": "more (or less) time: {\"seconds\": 60, \"label\": \"egg\"}; on a finished timer it starts again",
        "new": "another timer on the same card: {\"seconds\": 300, \"label\": \"tea\"}",
        "pause": "pause: {\"label\": \"egg\"} (no label: all)",
        "resume": "carry on: {\"label\": \"egg\"} (no label: all)",
        "cancel": "stop and remove: {\"label\": \"egg\"} (no label: all)",
    },
))

# Example specs for scripts/screen_preview.py.
SAMPLES: list[dict[str, Any]] = [
    {"card": "timer", "seconds": 600, "label": "pasta"},
    {"card": "timer", "seconds": 180, "label": "egg"},
]

__all__ = ["SAMPLES", "Timer", "TimerCard", "clean_label", "clock_text"]
