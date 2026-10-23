"""Game base class and the host interface games use for sound, speech and the camera.

A game is a plain object driven by the display loop (or tests):

* ``update(dt)`` advances it by ``dt`` seconds (pure logic, no drawing);
* ``draw(surface)`` paints the whole ``size`` canvas (720x480 by default);
* ``tap(x, y)`` / ``key(name)`` / ``hear(text)`` deliver input in canvas pixels / pygame key
  names / recognised speech.

Games never touch the robot directly: side effects go through a :class:`GameHost`
(``littlebot.games.service`` provides the real one; :class:`RecordingHost` records calls for
tests and screenshots). That keeps every game testable without a window or a speaker.

When the session's time is up the service calls :meth:`Game.wrap_up`: the game plays a short
finale (confetti, LittleBot's happy eyes, a summary line) and then sets ``finished``.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Protocol, Sequence

from littlebot.games import art

FINALE_SECONDS = 3.2


class GameHost(Protocol):
    """What a game may ask of the robot. Every method is fire-and-forget and never blocks."""

    eye_color: art.Color

    def sound(self, name: str, *, volume: float | None = None, max_wait: float | None = None) -> None:
        """Play a built-in sound effect (``core.interfaces.SOUND_NAMES``). Dropped if the speaker is
        busy for longer than ``max_wait`` seconds (default: a short, snappy limit)."""

    def melody(self, notes: list[tuple[str, float]], *, tempo_bpm: float = 120) -> None:
        """Play a short synthesised tune, e.g. ``[("C5", 0.5)]``."""

    def say(self, text: str) -> None:
        """Speak a few words (animal noises, "Your turn!")."""

    def child_gaze(self) -> tuple[float, float] | None:
        """Where LittleBot should look to look at the child (normalised screen coords), if a face is seen."""

    def hands_ready(self) -> bool:
        """True when the camera can read a hand sign now (camera switched on, hand models installed)."""

    def see_hand(self) -> Any:
        """Start reading the hand sign the player shows (``hand_sign.HandLook``: poll ``done``)."""

    def now(self) -> float:
        """Monotonic seconds (reaction timing)."""


class RecordingHost:
    """A host that just records what a game asked for (tests, screenshots, headless runs)."""

    def __init__(self, *, eye_color: art.Color = art.EYE_DEFAULT, clock: Any = None) -> None:
        self.eye_color = eye_color
        self.sounds: list[str] = []
        self.melodies: list[list[tuple[str, float]]] = []
        self.spoken: list[str] = []
        self.gaze: tuple[float, float] | None = None
        self.can_see = False  # hands_ready()
        self.hand_signs: list[str | None] = []  # what see_hand() "sees", one per look (then None)
        self.looks: list[Any] = []
        self._clock = clock or time.monotonic

    def sound(self, name: str, *, volume: float | None = None, max_wait: float | None = None) -> None:
        self.sounds.append(name)

    def melody(self, notes: list[tuple[str, float]], *, tempo_bpm: float = 120) -> None:
        self.melodies.append(list(notes))

    def say(self, text: str) -> None:
        self.spoken.append(text)

    def child_gaze(self) -> tuple[float, float] | None:
        return self.gaze

    def hands_ready(self) -> bool:
        return self.can_see

    def see_hand(self) -> Any:
        from littlebot.games.hand_sign import HandLook  # noqa: PLC0415

        look = HandLook()
        sign = self.hand_signs.pop(0) if self.hand_signs else None
        look.finish(sign, "" if sign else "no_hand")
        self.looks.append(look)
        return look

    def now(self) -> float:
        return float(self._clock())


class Game:
    """Base class. Subclasses override ``step``, ``render``, ``on_tap`` (and optionally ``on_key``,
    ``hear``, ``summary``, ``status_extra``, ``finale_text``)."""

    id = ""

    def __init__(self, host: GameHost | None = None, *, size: tuple[int, int] = (art.W, art.H),
                 seed: int | None = None) -> None:
        self.host: GameHost = host if host is not None else RecordingHost()
        self.w, self.h = int(size[0]), int(size[1])
        self.size = (self.w, self.h)
        self.rng = random.Random(seed)
        self.t = 0.0
        self.finished = False
        self.finale_t: float | None = None  # seconds into the finale, None while playing
        self.score = 0
        self.taps = 0
        self.particles = art.Particles(random.Random(self.rng.random()))
        color = getattr(self.host, "eye_color", art.EYE_DEFAULT)
        self.eye_color: art.Color = (int(color[0]), int(color[1]), int(color[2])) if color else art.EYE_DEFAULT
        self.started = False

    # ------------------------------------------------------------ lifecycle
    def begin(self) -> None:
        """Called once when the game appears on screen (intro sound, first round)."""
        self.started = True

    def wrap_up(self) -> None:
        """Time is up: play the finale, then set ``finished``."""
        if self.finale_t is not None or self.finished:
            return
        self.finale_t = 0.0
        self.particles.confetti(self.w, 70)
        self.host.sound("tada", max_wait=1.5)

    def close(self) -> None:
        """The game left the screen (stopped / replaced). Must be cheap."""

    @property
    def in_finale(self) -> bool:
        return self.finale_t is not None

    # ------------------------------------------------------------ driving
    def update(self, dt: float) -> None:
        dt = max(0.0, min(float(dt), 0.25))
        self.t += dt
        self.step(dt)
        self.particles.update(dt)
        if self.finale_t is not None:
            self.finale_t += dt
            if self.finale_t >= FINALE_SECONDS:
                self.finished = True

    def draw(self, surf: Any) -> None:
        self.render(surf)
        if self.finale_t is None:
            self.particles.draw(surf)
        else:
            self.draw_finale(surf)  # draws the particles (confetti) over its veil

    def tap(self, x: float, y: float) -> bool:
        """A tap / click at canvas pixel (x, y). Returns True if the game used it."""
        if self.finale_t is not None or self.finished:
            return False
        self.taps += 1
        return bool(self.on_tap(float(x), float(y)))

    def key(self, name: str) -> bool:
        """A key press (pygame key name, e.g. "space", "1", "left"). Returns True if used."""
        if self.finale_t is not None or self.finished:
            return False
        return bool(self.on_key(name))

    def hear(self, text: str) -> bool:
        """Recognised speech while the game runs. Returns True if the game used it."""
        return False

    # ------------------------------------------------------------ to override
    def step(self, dt: float) -> None:
        pass

    def render(self, surf: Any) -> None:
        surf.blit(art.background(self.size), (0, 0))

    def on_tap(self, x: float, y: float) -> bool:
        return False

    def on_key(self, name: str) -> bool:
        return False

    def summary(self) -> str:
        """One short line for the dashboard ("12 bubbles popped")."""
        return ""

    def finale_text(self) -> str:
        return "Yay!"

    def status_extra(self) -> dict[str, Any]:
        return {}

    def status(self) -> dict[str, Any]:
        return {"score": self.score, "taps": self.taps, "summary": self.summary(), "finale": self.in_finale,
                **self.status_extra()}

    # ------------------------------------------------------------ shared drawing
    def draw_finale(self, surf: Any) -> None:
        p = art.ease_out_cubic((self.finale_t or 0.0) / 0.5)
        surf.blit(art.veil(self.size, art.BG_TOP, int(200 * p)), (0, 0))
        bob = math.sin((self.finale_t or 0.0) * 6) * 6
        art.draw_eyes(surf, (self.w / 2, self.h * 0.42 + bob), 1.25 * (0.6 + 0.4 * art.ease_out_back(p)),
                      mood="happy", color=self.eye_color)
        title = art.text_outlined(self.finale_text(), 76, art.WHITE, art.INK, 4)
        art.blit_center(surf, title, (self.w / 2, self.h * 0.72), int(255 * p))
        line = self.summary() if self.taps else ""  # nobody played along: just celebrate
        if line:
            art.blit_center(surf, art.pill(line, 30, art.INK, art.WHITE), (self.w / 2, self.h * 0.86), int(255 * p))
        self.particles.draw(surf)  # confetti over the veil

    def burst(self, x: float, y: float, colors: Sequence[art.Color], n: int = 14, **kw: Any) -> None:
        self.particles.burst(x, y, n, colors, **kw)
