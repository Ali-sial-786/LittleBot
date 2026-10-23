"""Rock Paper Scissors against LittleBot. Tap rock, paper or scissors (or say it, if something is
listening and turns speech into ``audio.utterance`` events); after "Rock... Paper... Scissors...
Shoot!" LittleBot shows its pick and the round's winner. LittleBot picks at random at the reveal,
so it never peeks at yours.

With the camera on and the hand models installed (``littlebot.games.hand_sign``) you play with your
hand instead: tap Go (or say "go"), show rock, paper or scissors to the camera on "Shoot!", and
LittleBot reads it on the robot. When it can't tell, it asks you to tap what you showed."""

from __future__ import annotations

import math
import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.games.base import Game

CHOICES = ("rock", "paper", "scissors")
BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
WORDS = {"rock": "rock", "rocks": "rock", "stone": "rock", "paper": "paper", "scissors": "scissors",
         "scissor": "scissors", "sizzors": "scissors"}
LABELS = {"rock": "Rock", "paper": "Paper", "scissors": "Scissors"}
COUNT_BEATS = ("Rock...", "Paper...", "Scissors...", "Shoot!")
BEAT_S = 0.42
REVEAL_S = 2.3
LOOK_GIVE_UP_S = 4.0  # the camera never answered: ask instead
GO_WORDS = {"go", "ready", "shoot", "play", "again", "start"}
GO_W, GO_H = 440, 120
BTN_W, BTN_H = 196, 140
BTN_Y = 322
BUBBLE_R = 76
BOT_X, YOU_X, ARENA_Y = 196, 524, 222


def outcome(you: str, bot: str) -> str:
    """"win" / "lose" / "draw" for the player."""
    if you == bot:
        return "draw"
    return "win" if BEATS[you] == bot else "lose"


def icon_sprite(name: str, size: int = 96) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            c = n / 2
            if name == "rock":
                stone = (178, 168, 190)
                lumps = [(0.0, 0.1, 0.42, 0.32), (-0.18, -0.04, 0.24, 0.22), (0.14, -0.08, 0.27, 0.24),
                         (0.26, 0.12, 0.18, 0.18), (-0.27, 0.14, 0.17, 0.17)]  # (dx, dy, rx, ry) blobby boulder
                for grow, col in ((0.045, art.inkier(stone, 0.4)), (0.0, stone)):
                    for dx, dy, rx, ry in lumps:
                        rect = pygame.Rect(0, 0, (rx + grow) * 2 * n, (ry + grow) * 2 * n)
                        rect.center = (int(c + dx * n), int(c + dy * n))
                        pygame.draw.ellipse(s, col, rect)
                shine = pygame.Rect(c - n * 0.3, c - n * 0.24, n * 0.22, n * 0.11)
                pygame.draw.ellipse(s, art.lighten(stone, 0.45), shine)
                belly = pygame.Rect(c - n * 0.34, c + n * 0.26, n * 0.68, n * 0.1)
                pygame.draw.ellipse(s, art.darken(stone, 0.08), belly)
                for dx, dy in ((0.24, 0.2), (0.3, 0.04), (-0.3, 0.22)):
                    pygame.draw.circle(s, art.darken(stone, 0.14), (c + dx * n, c + dy * n), n * 0.024)
            elif name == "paper":
                sheet = pygame.Surface((int(n * 0.66), int(n * 0.8)), pygame.SRCALPHA)
                r = sheet.get_rect()
                fold = n * 0.18
                body = [(0, 0), (r.w - fold, 0), (r.w, fold), (r.w, r.h), (0, r.h)]
                edge = art.inkier((236, 236, 250), 0.4)
                pygame.draw.polygon(sheet, edge, body)
                inset = [(4 * k, 4 * k), (r.w - fold - 2 * k, 4 * k), (r.w - 4 * k, fold + 2 * k),
                         (r.w - 4 * k, r.h - 4 * k), (4 * k, r.h - 4 * k)]
                pygame.draw.polygon(sheet, (255, 255, 255), inset)
                pygame.draw.polygon(sheet, (222, 226, 244), [(r.w - fold, 0), (r.w - fold, fold), (r.w, fold)])
                crease = [(r.w - fold, 2 * k), (r.w - fold, fold), (r.w - 2 * k, fold)]
                pygame.draw.lines(sheet, edge, False, crease, int(3 * k))
                for i in range(4):
                    y = r.h * (0.42 + i * 0.13)
                    pygame.draw.line(sheet, (150, 196, 255), (r.w * 0.16, y), (r.w * 0.84, y), int(3 * k))
                pygame.draw.line(sheet, (255, 150, 170), (r.w * 0.24, r.h * 0.08), (r.w * 0.24, r.h * 0.92), int(2 * k))
                sheet = pygame.transform.rotozoom(sheet, 8, 1.0)
                s.blit(sheet, sheet.get_rect(center=(int(c), int(c))))
            else:  # scissors
                steel, handle = (204, 214, 232), art.STRAWBERRY
                for sx in (-1, 1):
                    tip = (c + sx * n * 0.3, c - n * 0.42)
                    base_l = (c - sx * n * 0.07, c + n * 0.12)
                    base_r = (c - sx * n * 0.16, c + n * 0.04)
                    pygame.draw.polygon(s, art.inkier(steel, 0.45), [tip, base_l, base_r])
                    base = ((base_l[0] + base_r[0]) / 2, (base_l[1] + base_r[1]) / 2)
                    art.capsule(s, art.inkier(steel, 0.45), tip, base, n * 0.13)
                    art.capsule(s, steel, tip, base, n * 0.08)
                for sx in (-1, 1):
                    hc = (c - sx * n * 0.2, c + n * 0.27)
                    art.capsule(s, art.inkier(handle, 0.4), hc, (c, c + n * 0.06), n * 0.11)
                    art.capsule(s, handle, hc, (c, c + n * 0.06), n * 0.06)
                for sx in (-1, 1):
                    hc = (c - sx * n * 0.2, c + n * 0.27)
                    pygame.draw.circle(s, art.inkier(handle, 0.4), hc, n * 0.17)
                    pygame.draw.circle(s, handle, hc, n * 0.17 - 3 * k)
                    pygame.draw.circle(s, art.inkier(handle, 0.4), hc, n * 0.085 + 3 * k)
                    pygame.draw.circle(s, (0, 0, 0, 0), hc, n * 0.085)  # see-through finger hole
                    pygame.draw.circle(s, art.lighten(handle, 0.45), (hc[0] - n * 0.09, hc[1] - n * 0.07), n * 0.03)
                pygame.draw.circle(s, art.inkier(steel, 0.5), (c, c + n * 0.06), n * 0.05)
                pygame.draw.circle(s, art.WHITE, (c - n * 0.012, c + n * 0.05), n * 0.016)

        out = art.supersampled((size, size), draw)
        if name in ("rock", "paper"):
            art.cute_face(out, size / 2, size * (0.56 if name == "rock" else 0.5), size * 0.62)
        return out

    return art.cached(("rps", name, size), make)


class RockPaperScissors(Game):
    id = "rps"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None) -> None:
        super().__init__(host, size=size, seed=seed)
        self.phase = "pick"  # pick | count | look (camera) | ask (camera unsure) | reveal
        self.phase_t = 0.0
        self.camera = False  # play by showing a hand to the camera (decided at begin)
        self.look: Any = None
        self.seen = False  # this round's pick came from the camera
        self.you: str | None = None
        self.bot: str | None = None
        self.result: str | None = None
        self.wins = self.losses = self.draws = 0
        self.beat = -1
        self.eyes = art.EyeState(BOT_X, 108, 0.6, self.eye_color)

    def begin(self) -> None:
        super().begin()
        self.camera = self._camera_ready()
        self.host.sound("boop", volume=0.6)

    def _camera_ready(self) -> bool:
        ready = getattr(self.host, "hands_ready", None)
        try:
            return bool(ready()) if ready is not None else False
        except Exception:  # noqa: BLE001 - no camera: tapping still works
            return False

    # ------------------------------------------------------------ logic
    def set_phase(self, phase: str) -> None:
        self.phase, self.phase_t = phase, 0.0
        self.beat = -1

    def choose(self, choice: str) -> bool:
        if choice not in CHOICES:
            return False
        if self.phase == "ask":  # the camera wasn't sure: the player says what they showed
            self.you = choice
            self.reveal()
            return True
        if self.phase != "pick":
            return False
        self.you, self.bot, self.result, self.seen = choice, None, None, False
        self.set_phase("count")
        return True

    def go(self) -> bool:
        """A camera round: count down, then read the player's hand on "Shoot!"."""
        if self.phase != "pick":
            return False
        self.camera = self._camera_ready()
        if not self.camera:
            return False  # the camera was switched off meanwhile: back to tapping a choice
        self.you, self.bot, self.result, self.seen, self.look = None, None, None, False, None
        self.set_phase("count")
        return True

    def take_look(self) -> None:
        look = self.look
        if look is not None and getattr(look, "done", False):
            sign = getattr(look, "sign", None)
            if sign in CHOICES:
                self.you, self.seen = sign, True
                self.reveal()
            else:
                self.set_phase("ask")
                self.host.sound("boop", volume=0.6)
        elif self.phase_t >= LOOK_GIVE_UP_S:
            self.set_phase("ask")

    def reveal(self) -> None:
        assert self.you is not None
        self.bot = self.rng.choice(CHOICES)
        self.result = outcome(self.you, self.bot)
        self.set_phase("reveal")
        tell = getattr(self.host, "tell", None)
        if tell is not None:  # LittleBot's voice (in a voice chat) says what it picked and who won
            who = {"win": "they win", "lose": "you (LittleBot) win", "draw": "it's a draw"}[self.result]
            how = "showed the camera" if self.seen else "picked"
            tell(f"Rock Paper Scissors: they {how} {self.you}, you picked {self.bot} on the screen, so {who}. "
                 f"Score: them {self.wins + (self.result == 'win')}, you {self.losses + (self.result == 'lose')}.")
        if self.result == "win":
            self.wins += 1
            self.score += 1
            self.host.sound("yay")
            self.eyes.feel("surprised", 1.6)
            self.burst(YOU_X, ARENA_Y, list(art.CANDY), 30, speed=(150, 360), life=(0.7, 1.3), size=(6, 11),
                       kinds=("star", "confetti", "dot"), gravity=240)
        elif self.result == "lose":
            self.losses += 1
            self.host.sound("boing")
            self.eyes.feel("happy", 1.6)
            self.burst(BOT_X, ARENA_Y, [art.SKY, art.WHITE, art.LEMON], 16, speed=(120, 260), life=(0.6, 1.0),
                       size=(6, 10), kinds=("star", "dot"))
        else:
            self.draws += 1
            self.host.sound("boop")
            self.eyes.feel("happy", 1.0)

    def step(self, dt: float) -> None:
        self.phase_t += dt
        if self.phase == "count":
            b = int(self.phase_t // BEAT_S)
            if b != self.beat and b < len(COUNT_BEATS):
                self.beat = b
                self.host.sound("blip" if b < 3 else "click")
            if self.phase_t >= BEAT_S * 3 and not self.in_finale:
                if self.you is not None:
                    self.reveal()
                else:  # "Shoot!": now the hand shows its shape
                    self.look = self.host.see_hand()
                    self.set_phase("look")
        elif self.phase == "look" and not self.in_finale:
            self.take_look()
        elif self.phase == "reveal" and self.phase_t >= REVEAL_S and not self.in_finale:
            self.set_phase("pick")
        self.eyes.look(0.25 if self.phase == "pick" else 0.6, 0.7)
        self.eyes.update(dt, self.rng)

    def button_rect(self, i: int) -> pygame.Rect:
        cx = self.w / 2 + (i - 1) * (BTN_W + 24)
        return pygame.Rect(int(cx - BTN_W / 2), BTN_Y, BTN_W, BTN_H)

    def button_at(self, x: float, y: float) -> str | None:
        for i, name in enumerate(CHOICES):
            if self.button_rect(i).inflate(20, 24).collidepoint(x, y):
                return name
        return None

    def go_rect(self) -> pygame.Rect:
        return pygame.Rect(int(self.w / 2 - GO_W / 2), BTN_Y + 10, GO_W, GO_H)

    def shows_buttons(self) -> bool:
        """The three choice buttons (always when tapping; with the camera only to say what you showed)."""
        return not self.camera or self.phase == "ask" or (self.phase == "reveal" and not self.seen)

    def on_tap(self, x: float, y: float) -> bool:
        if self.camera and self.phase == "pick":
            return self.go()  # anywhere: big enough for small hands
        if not self.shows_buttons():
            return False
        choice = self.button_at(x, y)
        return self.choose(choice) if choice else False

    def on_key(self, name: str) -> bool:
        if name in ("space", "return") and self.camera:
            self.go()
            return True
        choice = {"1": "rock", "2": "paper", "3": "scissors", "r": "rock", "p": "paper", "s": "scissors"}.get(name)
        if choice is None:
            return False
        self.choose(choice)
        return True

    def hear(self, text: str) -> bool:
        words = re.findall(r"[a-z]+", text.lower())
        for word in words:
            if word in WORDS:
                return self.choose(WORDS[word])
        if self.camera and any(w in GO_WORDS for w in words):
            return self.go()
        return False

    # ------------------------------------------------------------ drawing
    def label(self) -> tuple[str, art.Color, art.Color]:
        if self.phase == "count":
            return COUNT_BEATS[min(3, max(0, int(self.phase_t // BEAT_S)))], art.INK, art.WHITE
        if self.phase == "look":
            return ("Shoot!" if self.phase_t < 0.5 else "Looking..."), art.INK, art.WHITE
        if self.phase == "ask":
            return "What did you show?", art.WHITE, art.STRAWBERRY
        if self.phase == "reveal":
            return {"win": ("You win!", art.WHITE, art.MINT), "lose": ("LittleBot wins!", art.WHITE, art.SKY),
                    "draw": ("Draw!", art.INK, art.LEMON)}[self.result or "draw"]
        return ("Ready?" if self.camera else "Pick one!"), art.WHITE, art.STRAWBERRY

    def draw_bubble(self, surf: pygame.Surface, x: float, who: str, choice: str | None) -> None:
        winner = self.phase == "reveal" and ((who == "you" and self.result == "win") or
                                             (who == "bot" and self.result == "lose"))
        if winner:
            art.blit_center(surf, art.glow(BUBBLE_R * 1.7, art.LEMON, 2.0), (x, ARENA_Y))
        color = (255, 250, 253) if not winner else art.lighten(art.LEMON, 0.55)
        art.blit_center(surf, art.circle(BUBBLE_R + 5, art.inkier((246, 196, 220), 0.25)), (x, ARENA_Y + 4))
        art.blit_center(surf, art.circle(BUBBLE_R, color), (x, ARENA_Y))
        if self.phase == "count" or (self.phase == "look" and who == "bot"):
            hop = abs(math.sin(self.phase_t / BEAT_S * math.pi)) * 22 if self.phase == "count" else 0
            art.blit_center(surf, icon_sprite("rock", 104), (x, ARENA_Y + 6 - hop))
        elif self.phase == "reveal" and choice is not None:
            k = art.ease_out_back(min(1.0, self.phase_t / 0.35), 2.2)
            art.blit_center(surf, art.scaled(icon_sprite(choice, 112), k), (x, ARENA_Y))
        else:
            q = art.text_outlined("?", 86, art.WHITE, art.INK_SOFT, 3)
            art.blit_center(surf, q, (x, ARENA_Y + math.sin(self.t * 3 + x) * 5))

    def render(self, surf: pygame.Surface) -> None:
        surf.blit(art.background(self.size, seed=17), (0, 0))
        self.eyes.draw(surf)
        art.blit_text(surf, "You", 36, (YOU_X, 112), art.INK)
        self.draw_bubble(surf, BOT_X, "bot", self.bot)
        self.draw_bubble(surf, YOU_X, "you", self.you if self.phase != "pick" else None)
        art.blit_center(surf, art.text_outlined("VS", 46, art.WHITE, art.INK, 3), (self.w / 2, ARENA_Y))
        if self.shows_buttons():
            self.render_buttons(surf)
        elif self.phase == "pick":
            r = self.go_rect()
            surf.blit(art.rounded_rect(GO_W, GO_H, 36, art.MINT, shadow=6, border=art.inkier(art.MINT, 0.2),
                                       border_w=4), r.topleft)
            art.blit_text(surf, "Go!", 52, (r.centerx, r.y + 46), art.WHITE)
            art.blit_text(surf, "Show me your hand on Shoot!", 24, (r.centerx, r.bottom - 22), art.INK)
        else:
            hint = (f"I saw {LABELS[self.you]}!" if self.phase == "reveal" and self.you
                    else "Show me rock, paper or scissors!")
            art.blit_center(surf, art.pill(hint, 30, art.INK, art.WHITE), (self.w / 2, BTN_Y + 70))
        text, fg, bg = self.label()
        art.blit_center(surf, art.pill(text, 32, fg, bg), (self.w / 2, 34))
        art.blit_center(surf, art.pill(f"You {self.wins}", 26, art.INK, art.WHITE), (self.w - 92, 34))
        art.blit_center(surf, art.pill(f"LittleBot {self.losses}", 26, art.INK, art.WHITE), (104, 34))

    def render_buttons(self, surf: pygame.Surface) -> None:
        asking = self.phase == "ask"
        for i, name in enumerate(CHOICES):
            r = self.button_rect(i)
            chosen = self.phase != "pick" and not asking and self.you == name
            dim = self.phase != "pick" and not asking and not chosen
            color = art.lighten(art.LEMON, 0.35) if chosen else ((246, 232, 240) if dim else art.WHITE)
            lift = -6 if chosen else 0
            surf.blit(art.rounded_rect(BTN_W, BTN_H, 30, color, shadow=6, border=(246, 196, 220) if not chosen else
                                       art.inkier(art.LEMON, 0.2), border_w=4), (r.x, r.y + lift))
            art.blit_center(surf, icon_sprite(name, 84), (r.centerx, r.y + 56 + lift), 140 if dim else None)
            art.blit_text(surf, LABELS[name], 30, (r.centerx, r.bottom - 20 + lift), art.INK_SOFT if dim else art.INK)

    def summary(self) -> str:
        return f"You {self.wins} - LittleBot {self.losses}" + (f" - draws {self.draws}" if self.draws else "")

    def finale_text(self) -> str:
        return "You're the champ!" if self.wins > self.losses else "Good game!"

    def status_extra(self) -> dict[str, Any]:
        return {"wins": self.wins, "losses": self.losses, "draws": self.draws, "phase": self.phase,
                "you": self.you, "bot": self.bot, "result": self.result,
                "mode": "camera" if self.camera else "tap", "seen": self.seen}
