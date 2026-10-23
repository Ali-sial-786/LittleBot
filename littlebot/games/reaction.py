"""Reaction Race: LittleBot's round face turns red ("Wait..."), then green - tap as fast as you can!
Shows your time in milliseconds. Five tries make a round (best and average are shown); tapping
before green says "Too soon!" and doesn't count.

Timing: the green moment is stamped when green is first *drawn* (``host.now()``), and the tap
when it reaches the game, so a result includes the screen's and the touch panel's latency
(about one frame, ~33 ms at 30 fps) - the same for everyone."""

from __future__ import annotations

import math
from typing import Any

import pygame

from littlebot.games import art
from littlebot.games.base import Game

TRIES = 5
WAIT_RANGE = (1.3, 3.6)
TOO_SLOW = 3.0
LIGHT_R = 150
GREEN: art.Color = (72, 204, 120)
RED: art.Color = (255, 92, 116)
COLORS: dict[str, art.Color] = {
    "ready": (255, 244, 249), "wait": RED, "go": GREEN, "result": (88, 170, 255), "too_soon": art.TANGERINE,
    "too_slow": (214, 170, 200), "summary": art.LEMON,
}


def rating(ms: float) -> str:
    if ms < 250:
        return "Lightning fast!"
    if ms < 330:
        return "Super quick!"
    if ms < 450:
        return "Nice one!"
    return "Good try!"


def light_sprite(color: art.Color) -> pygame.Surface:
    def make() -> pygame.Surface:
        size = 2 * LIGHT_R + 12
        out = pygame.Surface((size, size + 8), pygame.SRCALPHA)
        art.blit_center(out, art.circle(LIGHT_R + 2, art.INK, 40), (size / 2, size / 2 + 8))  # soft plum shadow

        def draw(s: pygame.Surface, k: float) -> None:
            c = size * k / 2
            pygame.draw.circle(s, art.inkier(color, 0.32), (c, c), LIGHT_R * k)
            pygame.draw.circle(s, color, (c, c), (LIGHT_R - 7) * k)
            pygame.draw.circle(s, art.lighten(color, 0.2), (c, c - 10 * k), (LIGHT_R - 26) * k)
            pygame.draw.circle(s, color, (c, c + 6 * k), (LIGHT_R - 30) * k)
            pygame.draw.ellipse(s, art.lighten(color, 0.55), pygame.Rect(c - 82 * k, c - 122 * k, 70 * k, 30 * k))

        out.blit(art.supersampled((size, size), draw, ss=2), (0, 0))
        return out

    return art.cached(("reaction_light", color), make)


class ReactionRace(Game):
    id = "reaction"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None) -> None:
        super().__init__(host, size=size, seed=seed)
        self.phase = "ready"  # ready | wait | go | result | too_soon | too_slow | summary
        self.phase_t = 0.0
        self.delay = 2.0
        self.go_at: float | None = None
        self.go_drawn = False
        self.times: list[int] = []  # this round's results (ms)
        self.best_ms: int | None = None
        self.last_ms: int | None = None
        self.too_soon = 0
        self.rounds = 0
        self.new_best = False
        self.cx, self.cy = self.w / 2, self.h / 2 + 22
        self.eyes = art.EyeState(self.cx, self.cy - 38, 0.9, self.eye_color)

    def begin(self) -> None:
        super().begin()
        self.host.sound("boop", volume=0.6)

    # ------------------------------------------------------------ logic
    def set_phase(self, phase: str) -> None:
        self.phase, self.phase_t = phase, 0.0
        if phase == "wait":
            self.delay = self.rng.uniform(*WAIT_RANGE)
            self.go_at, self.go_drawn = None, False

    def step(self, dt: float) -> None:
        self.phase_t += dt
        p = self.phase
        if p == "wait" and self.phase_t >= self.delay and not self.in_finale:
            self.set_phase("go")
            self.go_at = self.host.now()  # refined when green is first drawn
            self.eyes.feel("surprised", 0.8)
        elif p == "go" and self.phase_t >= TOO_SLOW:
            self.set_phase("too_slow")
            self.host.sound("boop")
        elif p in ("too_soon", "too_slow") and self.phase_t >= 1.7:
            self.set_phase("wait")
        elif p == "result" and self.phase_t >= 1.9:
            self.set_phase("summary" if len(self.times) >= TRIES else "wait")
            if self.phase == "summary":
                self.rounds += 1
                self.host.sound("tada")
                self.particles.confetti(self.w, 50)
                self.eyes.feel("happy", 2.0)
        elif p == "summary" and self.phase_t >= 8.0:
            self.set_phase("ready")
        if p == "wait":
            self.eyes.look(0, 0.8)
        elif p in ("ready", "summary"):
            self.eyes.look(math.sin(self.t * 1.3) * 0.5, 0.5)
        self.eyes.update(dt, self.rng)

    def press(self) -> str:
        """The player tapped. Returns what happened: start | too_soon | result | next | new_round | ignored."""
        p = self.phase
        if p == "ready":
            self.times = []
            self.set_phase("wait")
            self.host.sound("blip")
            return "start"
        if p == "wait":
            self.too_soon += 1
            self.set_phase("too_soon")
            self.host.sound("uhoh")
            self.eyes.feel("sad", 1.4)
            return "too_soon"
        if p == "go":
            now = self.host.now()
            ms = int(round(max(0.0, now - (self.go_at if self.go_at is not None else now)) * 1000))
            self.last_ms = ms
            self.times.append(ms)
            self.new_best = self.best_ms is None or ms < self.best_ms
            self.best_ms = ms if self.new_best else self.best_ms
            self.score = len(self.times) + 5 * self.rounds
            self.set_phase("result")
            self.host.sound("success" if self.new_best and len(self.times) + self.rounds > 1 else "pop")
            self.eyes.feel("happy", 1.6)
            self.burst(self.cx, self.cy, [GREEN, art.WHITE, art.LEMON], 22, speed=(160, 340), life=(0.5, 1.0),
                       size=(5, 10), kinds=("star", "dot"), radius=LIGHT_R * 0.7)
            return "result"
        if p == "result" and self.phase_t > 0.5:
            self.set_phase("summary" if len(self.times) >= TRIES else "wait")
            return "next"
        if p == "summary" and self.phase_t > 0.8:
            self.times = []
            self.set_phase("wait")
            self.host.sound("blip")
            return "new_round"
        return "ignored"

    def on_tap(self, x: float, y: float) -> bool:
        return self.press() != "ignored"

    def on_key(self, name: str) -> bool:
        if name in ("space", "return"):
            self.press()
            return True
        return False

    @property
    def average_ms(self) -> int | None:
        return int(round(sum(self.times) / len(self.times))) if self.times else None

    # ------------------------------------------------------------ drawing
    def render(self, surf: pygame.Surface) -> None:
        surf.blit(art.background(self.size, seed=13), (0, 0))
        p = self.phase
        color = COLORS[p]
        if p == "go":
            if not self.go_drawn:  # stamp the moment green reaches the screen
                self.go_drawn = True
                self.go_at = self.host.now()
            pulse = 1.0 + 0.06 * math.sin(self.t * 14)
            art.blit_center(surf, art.glow(LIGHT_R * 1.75 * pulse, art.WHITE, 2.6), (self.cx, self.cy))  # white: mint+pink=grey
        light = light_sprite(color)
        pop = 1.0
        if p in ("go", "result") and self.phase_t < 0.25:
            pop = 1.0 + 0.06 * math.sin(self.phase_t / 0.25 * math.pi)
        art.blit_center(surf, art.scaled(light, pop) if pop != 1.0 else light, (self.cx, self.cy + 4))
        dark_text = p in ("ready", "summary", "too_slow")
        fg = art.INK if dark_text else art.WHITE
        if p == "result" and self.last_ms is not None:
            art.blit_center(surf, art.text_outlined(f"{self.last_ms} ms", 74, art.WHITE, art.inkier(color, 0.5), 3),
                            (self.cx, self.cy - 30))
            art.blit_text(surf, rating(self.last_ms), 34, (self.cx, self.cy + 36), fg)
            if self.new_best:
                art.blit_center(surf, art.pill("New best!", 26, art.INK, art.LEMON), (self.cx, self.cy + 84))
        elif p == "summary":
            art.blit_text(surf, "Round done!", 40, (self.cx, self.cy - 64), art.INK)
            avg = self.average_ms
            art.blit_text(surf, f"Average {avg} ms" if avg is not None else "", 34, (self.cx, self.cy - 18), art.INK)
            best = f"Best {min(self.times)} ms" if self.times else ""
            art.blit_text(surf, best, 34, (self.cx, self.cy + 22), art.INK)
            art.blit_text(surf, "Tap to play again", 28, (self.cx, self.cy + 76), art.INK_SOFT)
        else:
            self.eyes.draw(surf)
            text = {"ready": "Tap to start!", "wait": "Wait...", "go": "TAP!", "too_soon": "Too soon!",
                    "too_slow": "Too slow!"}[p]
            size = 64 if p == "go" else 40
            if p == "go":
                art.blit_center(surf, art.text_outlined(text, size, art.WHITE, art.inkier(GREEN, 0.5), 3),
                                (self.cx, self.cy + 66))
            else:
                art.blit_text(surf, text, size, (self.cx, self.cy + 62), fg)
        self.draw_hud(surf)

    def draw_hud(self, surf: pygame.Surface) -> None:
        tries = len(self.times)
        shown = tries if self.phase == "result" else tries + 1
        label = f"Try {min(max(shown, 1), TRIES)} of {TRIES}" if self.phase not in ("summary", "ready") else (
            "Reaction Race" if self.phase == "ready" else "Great round!")
        art.blit_center(surf, art.pill(label, 30, art.INK, art.WHITE), (self.w / 2, 34))
        if self.best_ms is not None:
            art.blit_center(surf, art.pill(f"Best {self.best_ms} ms", 26, art.INK, art.LEMON), (100, 34))
        # five result slots down the right side
        for i in range(TRIES):
            y = 120 + i * 62
            x = self.w - 76
            if i < tries:
                ms = self.times[i]
                art.blit_center(surf, art.pill(f"{ms}", 26, art.WHITE, GREEN if ms < 330 else art.SKY), (x, y))
            else:
                art.blit_center(surf, art.circle(13, (255, 255, 255), 200), (x, y))
                art.blit_center(surf, art.ring(13, 3, art.INK_SOFT), (x, y))

    def summary(self) -> str:
        return f"Best {self.best_ms} ms" if self.best_ms is not None else "No times yet"

    def finale_text(self) -> str:
        return "Speedy!" if self.best_ms is not None and self.best_ms < 330 else "Well done!"

    def status_extra(self) -> dict[str, Any]:
        return {"phase": self.phase, "times": list(self.times), "best_ms": self.best_ms, "last_ms": self.last_ms,
                "average_ms": self.average_ms, "too_soon": self.too_soon, "rounds": self.rounds}
