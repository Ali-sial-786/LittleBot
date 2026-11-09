"""Memory Lights: LittleBot lights up coloured pads, each with its own tone; copy the pattern.
Each round adds one more step. A mistake shows the right pad, then a new game starts (your best
round is kept). If you wait a long time LittleBot shows the pattern again - no penalty."""

from __future__ import annotations

from typing import Any

import pygame

from littlebot.games import art
from littlebot.games.base import Game

PAD_COLORS: tuple[art.Color, ...] = ((86, 204, 128), (255, 96, 124), (255, 200, 64), (84, 156, 255))
PAD_ICONS = ("circle", "heart", "star", "diamond")
PAD_NOTES = ("E4", "A4", "C#5", "E5")
# The board for a 480 px tall canvas; a game scales it to its own (on the 1024x600 screen: pads of 250 x 226 in
# the middle, the round and the best score in panels either side).
PAD_W, PAD_H, GAP, RADIUS = 200, 190, 16, 40
GRID_Y = 76
HUB_R = 74
SIDE_MIN_W = 150  # narrower room beside the pads than this: the numbers alone, no panels
DOTS_PER_ROW = 5
PRESS_LIT = 0.3
REPLAY_AFTER = 9.0


def pad_sprite(i: int, lit: bool, size: tuple[int, int] = (PAD_W, PAD_H)) -> pygame.Surface:
    pw, ph = int(size[0]), int(size[1])

    def make() -> pygame.Surface:
        z = pw / PAD_W  # the design's px at this size
        base = PAD_COLORS[i]
        color = art.lighten(base, 0.1) if lit else art.mix(base, (240, 214, 228), 0.4)
        drop = int(round(6 * z))
        out = pygame.Surface((pw, ph + drop), pygame.SRCALPHA)
        out.blit(art.rounded_rect(pw, ph, int(RADIUS * z), art.inkier(base, 0.5 if lit else 0.3)), (0, drop))

        def draw(s: pygame.Surface, ss: float) -> None:
            k = ss * z
            rect = s.get_rect()
            pygame.draw.rect(s, art.inkier(color, 0.3), rect, border_radius=int(RADIUS * k))
            inner = rect.inflate(-int(8 * k), -int(8 * k))
            pygame.draw.rect(s, color, inner, border_radius=int((RADIUS - 4) * k))
            if lit:  # radial light from the middle (blit blends; draw.* on SRCALPHA would not)
                s.blit(art.glow(inner.w * 0.62, art.WHITE, 2.2), art.glow(inner.w * 0.62, art.WHITE, 2.2).get_rect(
                    center=inner.center))
            top = pygame.Rect(inner.x + 16 * k, inner.y + 10 * k, inner.w - 32 * k, inner.h * 0.18)
            pygame.draw.rect(s, art.mix(color, art.WHITE, 0.32 if lit else 0.22), top, border_radius=int(top.h / 2))
            c = inner.center
            ink = art.WHITE if lit else art.mix(color, art.WHITE, 0.45)
            ic = PAD_ICONS[i]
            if ic == "circle":
                pygame.draw.circle(s, ink, c, 30 * k)
            elif ic == "heart":
                pygame.draw.polygon(s, ink, art.heart_points(c[0], c[1] + 2 * k, 72 * k))
            elif ic == "star":
                pygame.draw.polygon(s, ink, art.star_points(c[0], c[1], 36 * k, 16 * k))
            else:
                d = 34 * k
                pygame.draw.polygon(s, ink, [(c[0], c[1] - d), (c[0] + d * 0.8, c[1]), (c[0], c[1] + d),
                                             (c[0] - d * 0.8, c[1])])

        out.blit(art.supersampled((pw, ph), draw, ss=2), (0, 0))
        return out

    return art.cached(("memory_pad", i, lit, pw, ph), make)


class MemoryLights(Game):
    id = "memory"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None) -> None:
        super().__init__(host, size=size, seed=seed)
        self.sequence: list[int] = []
        self.phase = "intro"  # intro | show | input | success | fail
        self.phase_t = 0.0
        self.show_i = 0
        self.input_i = 0
        self.best = 0
        self.games = 0
        self.lit: dict[int, float] = {}  # pad -> seconds left lit
        self.fail_pad: int | None = None
        self.idle = 0.0
        k = self.k = self.h / 480  # px scale (1.25 on the 1024x600 screen)
        self.pad_w, self.pad_h = int(round(PAD_W * k)), int(round(PAD_H * k * 0.95))
        self.gap, self.radius, self.hub_r = int(round(GAP * k)), int(round(RADIUS * k)), HUB_R * k
        self.grid_x = (self.w - (2 * self.pad_w + self.gap)) // 2
        self.grid_y = int(round(GRID_Y * k))
        side = int(min(240 * k, self.grid_x - 40 * k))
        self.side_w = side if side >= SIDE_MIN_W else 0
        self.eyes = art.EyeState(self.w / 2, self.grid_y + self.pad_h + self.gap / 2, 0.52 * k, self.eye_color)

    @property
    def round(self) -> int:
        return len(self.sequence)

    def begin(self) -> None:
        super().begin()
        self.new_game()

    # ------------------------------------------------------------ logic
    def new_game(self) -> None:
        self.sequence = [self.rng.randrange(4)]
        self.games += 1
        self.set_phase("intro")

    def set_phase(self, phase: str) -> None:
        self.phase, self.phase_t = phase, 0.0
        if phase == "show":
            self.show_i = 0
        elif phase == "input":
            self.input_i = 0
            self.idle = 0.0

    def on_time(self) -> float:
        return max(0.24, 0.5 - 0.02 * len(self.sequence))

    def light(self, pad: int, seconds: float) -> None:
        self.lit[pad] = seconds
        self.host.melody([(PAD_NOTES[pad], seconds * 2.0)], tempo_bpm=120)

    def step(self, dt: float) -> None:
        self.phase_t += dt
        for pad in list(self.lit):
            self.lit[pad] -= dt
            if self.lit[pad] <= 0:
                del self.lit[pad]
        on, gap = self.on_time(), 0.16
        if self.phase == "intro" and self.phase_t >= 1.0:
            self.set_phase("show")
        elif self.phase == "show":
            slot = on + gap
            i = int((self.phase_t - 0.3) // slot) if self.phase_t >= 0.3 else -1
            if 0 <= i < len(self.sequence) and i >= self.show_i:
                self.show_i = i + 1
                self.light(self.sequence[i], on)
            if self.phase_t >= 0.3 + slot * len(self.sequence):
                self.set_phase("input")
        elif self.phase == "input":
            self.idle += dt
            if self.idle >= REPLAY_AFTER and not self.in_finale:
                self.set_phase("show")  # gentle reminder, no penalty
        elif self.phase == "success" and self.phase_t >= 0.9:
            self.sequence.append(self.rng.randrange(4))
            self.set_phase("show")
        elif self.phase == "fail":
            if self.fail_pad is not None and self.phase_t < 1.6:
                blink = int(self.phase_t / 0.32) % 2 == 0
                if blink:
                    self.lit[self.fail_pad] = max(self.lit.get(self.fail_pad, 0.0), 0.05)
            if self.phase_t >= 2.3:
                self.new_game()
        lit = [p for p, s in self.lit.items() if s > 0]
        if lit:
            cx, cy = self.pad_center(lit[-1])
            self.eyes.look_at_point(cx, cy, 160 * self.k)
        elif self.phase == "input":
            self.eyes.look(0, 0.5)
        else:
            self.eyes.look(0, 0)
        self.eyes.update(dt, self.rng)

    def press(self, pad: int) -> str:
        """The player pressed ``pad``. Returns "ignored" | "ok" | "round" (round complete) | "fail"."""
        if self.phase != "input" or not 0 <= pad < 4:
            return "ignored"
        self.idle = 0.0
        self.light(pad, PRESS_LIT)
        expected = self.sequence[self.input_i]
        if pad != expected:
            self.fail_pad = expected
            self.host.sound("uhoh", max_wait=1.0)  # right after the pad's tone
            self.eyes.feel("sad", 1.8)
            self.set_phase("fail")
            return "fail"
        self.input_i += 1
        if self.input_i < len(self.sequence):
            return "ok"
        done = len(self.sequence)
        new_best = done > self.best
        self.best = max(self.best, done)
        self.score = self.best
        self.eyes.feel("happy", 0.9)
        k = self.k
        self.burst(self.w / 2, self.eyes.y, list(PAD_COLORS) + [art.WHITE], 18, speed=(120 * k, 300 * k),
                   life=(0.5, 1.0), size=(5 * k, 10 * k), kinds=("star", "dot"))
        if done % 5 == 0:
            self.host.sound("tada", max_wait=1.0)
        elif new_best and self.games > 1:
            self.host.sound("success", max_wait=1.0)
        self.set_phase("success")
        return "round"

    def pad_rect(self, i: int) -> pygame.Rect:
        col, row = i % 2, i // 2
        return pygame.Rect(self.grid_x + col * (self.pad_w + self.gap), self.grid_y + row * (self.pad_h + self.gap),
                           self.pad_w, self.pad_h)

    def pad_center(self, i: int) -> tuple[float, float]:
        r = self.pad_rect(i)
        return float(r.centerx), float(r.centery)

    def pad_at(self, x: float, y: float) -> int | None:
        if art.dist(x, y, self.eyes.x, self.eyes.y) < self.hub_r * 0.8:
            return None
        for i in range(4):
            if self.pad_rect(i).inflate(self.gap, self.gap).collidepoint(x, y):
                return i
        return None

    def on_tap(self, x: float, y: float) -> bool:
        pad = self.pad_at(x, y)
        if pad is None:
            return False
        self.press(pad)
        return True

    def on_key(self, name: str) -> bool:
        pad = {"1": 0, "2": 1, "3": 2, "4": 3}.get(name)
        if pad is None:
            return False
        self.press(pad)
        return True

    # ------------------------------------------------------------ drawing
    def phase_label(self) -> str:
        return {"intro": "Get ready!", "show": "Watch me!", "input": "Your turn!", "success": "Great!",
                "fail": "Oops!"}.get(self.phase, "")

    def render(self, surf: pygame.Surface) -> None:
        k = self.k
        surf.blit(art.background(self.size, seed=5), (0, 0))
        size = (self.pad_w, self.pad_h)
        for i in range(4):
            lit = self.lit.get(i, 0.0) > 0
            r = self.pad_rect(i)
            if lit:
                art.blit_center(surf, art.glow(self.pad_w * 0.9, PAD_COLORS[i], 2.4), r.center)
                spr = art.scaled(pad_sprite(i, True, size), 1.05)
            else:
                spr = pad_sprite(i, False, size)
            art.blit_center(surf, spr, (r.centerx, r.centery + 3 * k))
        hub_c = (self.eyes.x, self.eyes.y)
        art.blit_center(surf, art.circle(self.hub_r + 6 * k, art.inkier((255, 236, 244), 0.3)), hub_c)
        art.blit_center(surf, art.circle(self.hub_r, (255, 240, 247)), hub_c)
        self.eyes.draw(surf)
        label = self.phase_label()
        if label:
            fg = art.WHITE if self.phase in ("input", "success") else art.INK
            bg = art.MINT if self.phase == "success" else (art.STRAWBERRY if self.phase == "fail" else
                                                            (art.SKY if self.phase == "input" else art.WHITE))
            art.blit_center(surf, art.pill(label, int(34 * k), fg, bg, pad=(22, 8)), (self.w / 2, self.grid_y / 2))
        rnd = max(1, len(self.sequence))
        if not self.side_w:  # a narrow canvas: the two numbers alone
            sides = ((self.grid_x / 2, "ROUND", rnd), (self.w - self.grid_x / 2, "BEST", self.best))
            for x, title, value in sides:
                art.blit_text(surf, title, int(28 * k), (x, 206 * k), art.INK_SOFT)
                art.blit_center(surf, art.text_outlined(str(value), int(82 * k), art.WHITE, art.INK, 3), (x, 262 * k))
            return
        left = self.grid_x / 2
        self._side_panel(surf, left, "Round", rnd)
        self.draw_steps(surf, left, self.panel_y(0.6))
        right = self.w - left
        self._side_panel(surf, right, "Best", self.best)
        art.blit_text(surf, "round" if self.best == 1 else "rounds", int(30 * k), (right, self.panel_y(0.52)),
                      art.INK_SOFT)
        art.blit_center(surf, art.pill(f"Game {max(1, self.games)}", int(30 * k), art.INK, art.lighten(art.LEMON, 0.3),
                                       pad=(18, 7)), (right, self.panel_y(0.8)))

    def panel_y(self, f: float) -> float:
        """A height in the side panels (as tall as the pads), ``f`` of the way down."""
        return self.grid_y + f * (2 * self.pad_h + self.gap)

    def _side_panel(self, surf: pygame.Surface, x: float, title: str, value: int) -> None:
        """A cream panel beside the pads with ``title`` and a big ``value`` in its upper half."""
        k, w = self.k, self.side_w
        h = 2 * self.pad_h + self.gap
        surf.blit(art.rounded_rect(w, h, int(30 * k), art.CREAM, shadow=4, border=(246, 196, 220), border_w=3),
                  (int(x - w / 2), self.grid_y))
        art.blit_text(surf, title, int(34 * k), (x, self.panel_y(0.12)), art.INK_SOFT)
        art.blit_center(surf, art.text_outlined(str(value), int(96 * k), art.WHITE, art.INK, 4), (x, self.panel_y(0.32)))

    def draw_steps(self, surf: pygame.Surface, x: float, y: float) -> None:
        """The pattern's steps as dots under the round number: each lights up as it is shown or copied."""
        k = self.k
        n = max(1, len(self.sequence))
        done = {"input": self.input_i, "show": self.show_i, "success": n}.get(self.phase, 0)
        per_row = min(DOTS_PER_ROW, n)
        rows = -(-n // DOTS_PER_ROW)
        pitch = min(34 * k, (self.side_w - 30 * k) / per_row)
        r = pitch * 0.36
        max_rows = max(1, int((self.panel_y(1.0) - 24 * k - y) // pitch))
        if rows > max_rows:  # a very long pattern: a number instead of dots
            label = f"{done} of {n}" if self.phase in ("show", "input") else f"{n} steps"
            art.blit_text(surf, label, int(30 * k), (x, y), art.INK)
            return
        for i in range(n):
            row, col = divmod(i, DOTS_PER_ROW)
            in_row = min(DOTS_PER_ROW, n - row * DOTS_PER_ROW)
            cx = x + (col - (in_row - 1) / 2) * pitch
            cy = y + row * pitch
            if i < done:
                art.blit_center(surf, art.circle(r, PAD_COLORS[self.sequence[i]]), (cx, cy))
            else:
                art.blit_center(surf, art.circle(r, (255, 255, 255), 220), (cx, cy))
                art.blit_center(surf, art.ring(r, max(2, r * 0.22), art.INK_SOFT), (cx, cy))

    def summary(self) -> str:
        return f"Best: round {self.best}" if self.best else "Round 1"

    def finale_text(self) -> str:
        return "Super memory!" if self.best >= 5 else "Well played!"

    def status_extra(self) -> dict[str, Any]:
        return {"round": len(self.sequence), "best": self.best, "phase": self.phase, "games": self.games}
