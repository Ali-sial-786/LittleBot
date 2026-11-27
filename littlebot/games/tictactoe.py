"""Tic-Tac-Toe against LittleBot. You are X, LittleBot is O; who starts alternates each round.

LittleBot's skill adapts: it always takes a win that is right there, plays the perfect (minimax)
move with probability ``skill`` and otherwise plays like a beginner (blocks most of the time,
else a random square). Winning makes it a bit cleverer, losing makes it gentler, so children win
some and lose some; ``skill=1.0`` never loses (tests)."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Sequence

import pygame

from littlebot.games import art
from littlebot.games.base import Game

LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
HUMAN, BOT = "X", "O"
# The board for a 480 px tall canvas; a game scales it to its own (on the 1024x600 screen: cells of 141 px,
# "You" and LittleBot in panels either side).
CELL, GAP = 118, 12
BY = 80
SIDE_MIN_W = 150  # narrower room beside the board than this: no panels behind the scores
X_COLOR = art.STRAWBERRY
O_COLOR: art.Color = (64, 182, 236)
SKILL_START, SKILL_MIN, SKILL_MAX = 0.55, 0.25, 0.97


def winner(board: Sequence[str]) -> str | None:
    """"X" / "O" for a win, "draw" for a full board, None while playing."""
    for a, b, c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if all(board) else None


def winning_line(board: Sequence[str]) -> tuple[int, int, int] | None:
    for line in LINES:
        a, b, c = line
        if board[a] and board[a] == board[b] == board[c]:
            return line
    return None


@lru_cache(maxsize=None)
def _score(board: tuple[str, ...], turn: str, me: str, depth: int = 0) -> int:
    """Minimax value for ``me``: quicker wins score higher, later losses less badly (so a lost
    position is still defended - LittleBot blocks instead of giving up)."""
    w = winner(board)
    if w == me:
        return 10 - depth
    if w == "draw":
        return 0
    if w is not None:
        return depth - 10
    other = HUMAN if turn == BOT else BOT
    scores = [_score(board[:i] + (turn,) + board[i + 1:], other, me, depth + 1) for i in range(9) if not board[i]]
    return max(scores) if turn == me else min(scores)


def best_moves(board: Sequence[str], me: str) -> list[int]:
    """All optimal moves for ``me`` (minimax over the full game tree; tiny for 3x3)."""
    b = tuple(board)
    other = HUMAN if me == BOT else BOT
    options = [(i, _score(b[:i] + (me,) + b[i + 1:], other, me, 1)) for i in range(9) if not b[i]]
    if not options:
        return []
    top = max(s for _i, s in options)
    return [i for i, s in options if s == top]


def immediate_win(board: Sequence[str], who: str) -> int | None:
    for a, b, c in LINES:
        cells = (board[a], board[b], board[c])
        if cells.count(who) == 2 and cells.count("") == 1:
            return (a, b, c)[cells.index("")]
    return None


def x_sprite(size: int) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            m, lw = n * 0.2, n * 0.2
            strokes = (((m, m), (n - m, n - m)), ((n - m, m), (m, n - m)))
            for col, w in ((art.inkier(X_COLOR, 0.35), lw + 6 * k), (X_COLOR, lw)):
                for p0, p1 in strokes:
                    art.capsule(s, col, p0, p1, w)
            hl = art.lighten(X_COLOR, 0.45)
            art.capsule(s, hl, (m + lw * 0.05, m - lw * 0.1), (m + lw * 0.75, m + lw * 0.6), lw * 0.2)

        return art.supersampled((size, size), draw)

    return art.cached(("ttt_x", size), make)


def o_sprite(size: int) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            r = n * 0.36
            lw = n * 0.19
            pygame.draw.circle(s, art.inkier(O_COLOR, 0.35), (n / 2, n / 2), r + lw / 2 + 3 * k, int(lw + 6 * k))
            pygame.draw.circle(s, O_COLOR, (n / 2, n / 2), r + lw / 2, int(lw))
            arc = pygame.Rect(0, 0, (r + lw * 0.1) * 2, (r + lw * 0.1) * 2)
            arc.center = (int(n / 2), int(n / 2))
            pygame.draw.arc(s, art.lighten(O_COLOR, 0.5), arc, math.radians(110), math.radians(160), int(lw * 0.24))

        return art.supersampled((size, size), draw)

    return art.cached(("ttt_o", size), make)


class TicTacToe(Game):
    id = "tictactoe"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None,
                 skill: float = SKILL_START, adaptive: bool = True) -> None:
        super().__init__(host, size=size, seed=seed)
        self.board: list[str] = [""] * 9
        self.placed: list[float] = [0.0] * 9
        self.turn = HUMAN
        self.starter = HUMAN
        self.phase = "play"  # play | thinking | over
        self.phase_t = 0.0
        self.think_for = 0.8
        self.glance_t = 0.0
        self.result: str | None = None
        self.line: tuple[int, int, int] | None = None
        self.skill = float(skill)
        self.adaptive = adaptive
        self.wins = self.losses = self.draws = 0
        self.rounds = 0
        k = self.k = self.h / 480  # px scale (1.25 on the 1024x600 screen)
        self.cell, self.gap = int(round(CELL * k * 0.96)), int(round(GAP * k * 0.96))
        self.board_px = 3 * self.cell + 2 * self.gap
        self.bx, self.by = (self.w - self.board_px) // 2, int(round(BY * k))
        side = int(min(250 * k, self.bx - 40 * k))
        self.side_w = side if side >= SIDE_MIN_W else 0
        self.eyes = art.EyeState(self.w - self.bx / 2, self.side_y(0.2), 0.62 * k, self.eye_color)

    def side_y(self, f: float) -> float:
        """A height in the side panels, ``f`` of the way down (they are as tall as the board's panel)."""
        top = self.by - 14 * self.k
        return top + f * (self.board_px + 28 * self.k)

    def begin(self) -> None:
        super().begin()
        self.host.sound("boop", volume=0.6)

    # ------------------------------------------------------------ rules
    def new_round(self) -> None:
        self.board = [""] * 9
        self.placed = [0.0] * 9
        self.result, self.line = None, None
        self.starter = BOT if self.starter == HUMAN else HUMAN
        self.turn = self.starter
        self.set_phase("thinking" if self.turn == BOT else "play")

    def set_phase(self, phase: str) -> None:
        self.phase, self.phase_t = phase, 0.0
        if phase == "thinking":
            self.think_for = self.rng.uniform(0.6, 1.0)

    def place(self, cell: int, who: str) -> bool:
        if not 0 <= cell < 9 or self.board[cell] or self.phase == "over":
            return False
        self.board[cell] = who
        self.placed[cell] = self.t
        self.host.sound("pop" if who == HUMAN else "boop", volume=0.8)
        res = winner(self.board)
        if res is not None:
            self.finish_round(res)
        else:
            self.turn = BOT if who == HUMAN else HUMAN
            self.set_phase("thinking" if self.turn == BOT else "play")
        return True

    def finish_round(self, res: str) -> None:
        self.result = res
        self.line = winning_line(self.board)
        self.rounds += 1
        self.set_phase("over")
        if res == HUMAN:
            self.wins += 1
            self.score += 1
            self.particles.confetti(self.w, 60)
            self.host.sound("tada")
            self.eyes.feel("surprised", 1.6)
            if self.adaptive:
                self.skill = min(SKILL_MAX, self.skill + 0.15)
        elif res == BOT:
            self.losses += 1
            self.host.sound("boing")
            self.eyes.feel("happy", 1.6)
            if self.adaptive:
                self.skill = max(SKILL_MIN, self.skill - 0.2)
        else:
            self.draws += 1
            self.host.sound("boop")
            self.eyes.feel("happy", 1.0)
            if self.adaptive:
                self.skill = min(SKILL_MAX, self.skill + 0.04)

    def choose_move(self) -> int:
        b = self.board
        win = immediate_win(b, BOT)
        if win is not None:
            return win
        empty = [i for i in range(9) if not b[i]]
        if self.rng.random() < self.skill:
            return self.rng.choice(best_moves(b, BOT))
        block = immediate_win(b, HUMAN)
        if block is not None and self.rng.random() < 0.6:
            return block
        return self.rng.choice(empty)

    def step(self, dt: float) -> None:
        self.phase_t += dt
        if self.phase == "thinking":
            self.glance_t -= dt
            empty = [i for i in range(9) if not self.board[i]]
            if self.glance_t <= 0 and empty:
                self.glance_t = 0.28
                cx, cy = self.cell_center(self.rng.choice(empty))
                self.eyes.look_at_point(cx, cy, 300 * self.k)
            if self.phase_t >= self.think_for and not self.in_finale:
                cell = self.choose_move()
                cx, cy = self.cell_center(cell)
                self.eyes.look_at_point(cx, cy, 300 * self.k)
                self.place(cell, BOT)
        elif self.phase == "over" and self.phase_t >= 2.6 and not self.in_finale:
            self.new_round()
        elif self.phase == "play":
            self.eyes.look(-0.6, 0.6)
        self.eyes.update(dt, self.rng)

    def cell_rect(self, i: int) -> pygame.Rect:
        c, g = self.cell, self.gap
        return pygame.Rect(self.bx + (i % 3) * (c + g), self.by + (i // 3) * (c + g), c, c)

    def cell_center(self, i: int) -> tuple[float, float]:
        r = self.cell_rect(i)
        return float(r.centerx), float(r.centery)

    def cell_at(self, x: float, y: float) -> int | None:
        for i in range(9):
            if self.cell_rect(i).inflate(self.gap, self.gap).collidepoint(x, y):
                return i
        return None

    def on_tap(self, x: float, y: float) -> bool:
        if self.phase != "play" or self.turn != HUMAN:
            return False
        cell = self.cell_at(x, y)
        if cell is None or self.board[cell]:
            return False
        return self.place(cell, HUMAN)

    def on_key(self, name: str) -> bool:
        if name in "123456789" and len(name) == 1:
            if self.phase == "play" and self.turn == HUMAN:
                self.place(int(name) - 1, HUMAN)
            return True
        return False

    # ------------------------------------------------------------ drawing
    def label(self) -> tuple[str, art.Color, art.Color]:
        if self.phase == "over":
            if self.result == HUMAN:
                return "You win!", art.WHITE, art.MINT
            if self.result == BOT:
                return "LittleBot wins!", art.WHITE, O_COLOR
            return "It's a draw!", art.INK, art.WHITE
        if self.turn == HUMAN:
            return "Your turn!", art.WHITE, X_COLOR
        return "LittleBot's turn...", art.INK, art.WHITE

    def render(self, surf: pygame.Surface) -> None:
        k = self.k
        surf.blit(art.background(self.size, seed=8), (0, 0))
        pad = int(14 * k)
        panel = art.rounded_rect(self.board_px + 2 * pad, self.board_px + 2 * pad, int(34 * k), (255, 244, 249),
                                 shadow=6, border=(246, 196, 220), border_w=4)
        surf.blit(panel, (self.bx - pad, self.by - pad))
        mark_px = int(self.cell * 0.81)
        for i in range(9):
            r = self.cell_rect(i)
            win_cell = self.line is not None and i in self.line
            color = (255, 255, 255) if not win_cell else art.lighten(art.LEMON, 0.45)
            surf.blit(art.rounded_rect(self.cell, self.cell, int(24 * k), color, shadow=4), r.topleft)
            mark = self.board[i]
            if mark:
                spr = x_sprite(mark_px) if mark == HUMAN else o_sprite(mark_px)
                age = self.t - self.placed[i]
                if age < 0.35:
                    spr = art.scaled(spr, art.ease_out_back(age / 0.35, 2.2))
                art.blit_center(surf, spr, (r.centerx, r.centery + 1))
        if self.line is not None:
            self.draw_win_line(surf)
        # the two players either side of the board
        lx, rx = self.bx / 2, self.w - self.bx / 2
        if self.side_w:
            for x in (lx, rx):
                w, h = self.side_w, self.board_px + 2 * pad
                surf.blit(art.rounded_rect(w, h, int(30 * k), art.CREAM, shadow=4, border=(246, 196, 220), border_w=3),
                          (int(x - w / 2), self.by - pad))
        sy = self.side_y
        art.blit_center(surf, x_sprite(int(80 * k)), (lx, sy(0.2)))
        art.blit_text(surf, "You", int(36 * k), (lx, sy(0.42)), art.INK)
        art.blit_center(surf, art.text_outlined(str(self.wins), int(80 * k), art.WHITE, X_COLOR, 4), (lx, sy(0.6)))
        self.eyes.draw(surf)
        art.blit_text(surf, "LittleBot", int(32 * k), (rx, sy(0.42)), art.INK)
        art.blit_center(surf, art.text_outlined(str(self.losses), int(80 * k), art.WHITE, O_COLOR, 4), (rx, sy(0.6)))
        art.blit_center(surf, o_sprite(int(52 * k)), (rx, sy(0.84)))
        if self.draws:
            art.blit_center(surf, art.pill(f"Draws {self.draws}", int(28 * k), art.INK_SOFT, art.WHITE, pad=(16, 6)),
                            (lx, sy(0.84)))
        elif self.rounds:
            art.blit_center(surf, art.pill(f"Round {self.rounds + 1}", int(28 * k), art.INK_SOFT, art.WHITE,
                                           pad=(16, 6)), (lx, sy(0.84)))
        text, fg, bg = self.label()
        art.blit_center(surf, art.pill(text, int(32 * k), fg, bg, pad=(22, 8)), (self.w / 2, (self.by - pad) / 2))

    def draw_win_line(self, surf: pygame.Surface) -> None:
        a, _b, c = self.line or (0, 0, 0)
        (x0, y0), (x1, y1) = self.cell_center(a), self.cell_center(c)
        k = self.k
        ext = 46 * k
        d = math.hypot(x1 - x0, y1 - y0)
        ux, uy = (x1 - x0) / d, (y1 - y0) / d
        x0, y0, x1, y1 = x0 - ux * ext, y0 - uy * ext, x1 + ux * ext, y1 + uy * ext
        p = art.ease_out_cubic(self.phase_t / 0.4)
        color = X_COLOR if self.result == HUMAN else O_COLOR
        end = (x0 + (x1 - x0) * p, y0 + (y1 - y0) * p)
        art.capsule(surf, art.inkier(color, 0.3), (x0, y0), end, 24 * k)
        art.capsule(surf, art.lighten(color, 0.3), (x0, y0), end, 16 * k)
        art.capsule(surf, art.lighten(color, 0.7), (x0, y0 - 3 * k), (end[0], end[1] - 3 * k), 4 * k)

    def summary(self) -> str:
        return f"You {self.wins} - LittleBot {self.losses}" + (f" - draws {self.draws}" if self.draws else "")

    def finale_text(self) -> str:
        if self.wins > self.losses:
            return "You're the champ!"
        return "Good game!"

    def status_extra(self) -> dict[str, Any]:
        return {"wins": self.wins, "losses": self.losses, "draws": self.draws, "phase": self.phase,
                "turn": self.turn, "board": "".join(c or "." for c in self.board), "skill": round(self.skill, 2)}
