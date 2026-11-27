"""Rock Paper Scissors against LittleBot. Tap rock, paper or scissors (or say it, if something is
listening and turns speech into ``audio.utterance`` events); after "Rock... Paper... Scissors...
Shoot!" LittleBot shows its pick and the round's winner. LittleBot locks its pick in as the countdown
starts, so it never peeks at yours. First to three wins the match.

With the camera on and the hand models installed (``littlebot.games.hand_sign``) you play with your
hand instead: tap Go (or say "go"), show rock, paper or scissors to the camera on "Shoot!", and
LittleBot reads it on the robot. When it can't tell, it says why and asks what you showed.

LittleBot picks at random (fair for little ones) until someone says "tricky mode": then it learns
the player's habits (what they play after a win or a loss, what they play most) and tries to beat
them, still before it sees their hand. "Easy mode" goes back to random.

In a voice chat each round's result is said in LittleBot's voice (``host.tell``: a note for a voice
that words it itself, and the ready line ``say`` for one that says it as it is)."""

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
RULES = {"rock": "Rock smashes scissors", "scissors": "Scissors cut paper", "paper": "Paper wraps rock"}  # winner's
COUNT_BEATS = ("Rock...", "Paper...", "Scissors...", "Shoot!")
BEAT_S = 0.42
REVEAL_S = 2.3
NEXT_AFTER_S = 0.8  # a new pick or "go" during the reveal starts the next round, once the result has been seen
MATCH_TO = 3  # first to three wins the match
MATCH_S = 3.0  # the match's celebration
LOOK_GIVE_UP_S = 4.0  # the camera never answered: ask instead
TRICKY_RANDOM = 0.2  # tricky mode still picks at random this often (it can't be read back so easily)
TRICKY_FADE = 0.85  # each older round counts this much less in tricky mode's guess
GO_WORDS = {"go", "ready", "shoot", "play", "again", "start", "more", "another", "next", "rematch"}
# Words that may come with a move ("I'll go with rock") or with "go" ("okay, one more time!") and still be only
# about the game: anything else ("play some jazz", "rock music") is not a move and goes to LittleBot's voice.
FILLER = frozenset("i i'm im i'll ill gonna going to do pick picked choose chose go with take say my choice is it's "
                   "its um uh er hmm okay ok then the a an and let's lets how about so this that".split())
GO_FILLER = FILLER | frozenset("one yes yeah yep sure alright all right let us time please me now we round game "
                               "match".split())
TRICKY_ON = re.compile(r"\b(?:tricky|hard|harder|difficult) (?:mode|level)\b|\bplay (?:hard|harder|for real|properly)\b|"
                       r"\btry (?:to beat me|your best|your hardest)\b|\bbe tricky\b|\bdon'?t (?:go easy|let me win)\b")
TRICKY_OFF = re.compile(r"\b(?:easy|normal|random) (?:mode|level)\b|\bplay (?:easy|nice|normal)\b|\bgo easy\b|"
                        r"\bbe nice\b|\bstop being tricky\b")
# The layout for a 480 px tall canvas; a game scales it to its own (x1.25 on the 1024x600 screen) and puts
# LittleBot's and your bubbles a quarter of the way in from either side, "VS" between them.
GO_W, GO_H = 440, 120
BTN_W, BTN_H = 196, 140
BTN_Y = 322
BTN_GAP = 24
BUBBLE_R = 76
ARENA_Y = 222
BOT_AT, YOU_AT = 0.27, 0.73  # the two bubbles, as a share of the width


def outcome(you: str, bot: str) -> str:
    """"win" / "lose" / "draw" for the player."""
    if you == bot:
        return "draw"
    return "win" if BEATS[you] == bot else "lose"


def beater(move: str) -> str:
    """The move that beats ``move``."""
    return next(m for m in CHOICES if BEATS[m] == move)


def heard_move(text: str) -> str | None:
    """The move said in ``text`` when that is all it says ("Rock!", "I choose stone", "I'll go with paper")."""
    words = re.findall(r"[a-z']+", text.lower())
    moves = {WORDS[w] for w in words if w in WORDS}
    if len(moves) != 1 or len(words) > 7 or any(w not in WORDS and w not in FILLER for w in words):
        return None
    return moves.pop()


def heard_go(text: str) -> bool:
    """``text`` only says to start a round ("go", "ready", "one more time", "let's play again")."""
    words = re.findall(r"[a-z']+", text.lower())
    return (0 < len(words) <= 6 and any(w in GO_WORDS for w in words)
            and all(w in GO_WORDS or w in GO_FILLER for w in words))


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
        self.phase = "pick"  # pick | count | look (camera) | ask (camera unsure) | reveal | match (someone won it)
        self.phase_t = 0.0
        self.camera = False  # play by showing a hand to the camera (decided at begin)
        self.look: Any = None
        self.seen = False  # this round's pick came from the camera
        self.you: str | None = None
        self.bot: str | None = None
        self.result: str | None = None
        self.wins = self.losses = self.draws = 0  # every round this game
        self.match_you = self.match_bot = 0  # rounds won in this match (first to MATCH_TO)
        self.matches = {"you": 0, "bot": 0}
        self.match_winner: str | None = None  # "you" / "bot" once this round ended the match
        self.tricky = False
        self.history: list[tuple[str, str]] = []  # (their move, the result for them), oldest first
        self._locked: str | None = None  # LittleBot's pick, made as the countdown starts
        self._said: str | None = None  # a move they said during a camera round's countdown
        self.beat = -1
        k = self.k = self.h / 480  # px scale (1.25 on the 1024x600 screen)
        self.bot_x, self.you_x, self.arena_y = self.w * BOT_AT, self.w * YOU_AT, ARENA_Y * k
        self.bubble_r = BUBBLE_R * k
        self.btn_w, self.btn_h, self.btn_y = int(BTN_W * k), int(BTN_H * k), int(BTN_Y * k)
        self.go_w, self.go_h = int(GO_W * k), int(GO_H * k)
        self.eyes = art.EyeState(self.bot_x, 108 * k, 0.6 * k, self.eye_color)

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

    def _tell(self, note: str, say: str) -> None:
        """What LittleBot's voice says in a voice chat: ``note`` for a voice that words it itself, ``say`` as is."""
        tell = getattr(self.host, "tell", None)
        if tell is None:
            return
        try:
            tell(note, say=say)
        except TypeError:  # a host whose tell() takes only the note
            tell(note)

    def _ready(self) -> bool:
        """A new round may start: waiting for one, or the last one's result (or the match's) has been seen."""
        if self.phase == "pick":
            return True
        if self.phase in ("reveal", "match") and (self.phase == "match" or self.phase_t >= NEXT_AFTER_S) \
                and not self.in_finale:
            if self.phase == "match" or self.match_winner:
                self._new_match()
            self.set_phase("pick")
            return True
        return False

    def _new_match(self) -> None:
        self.match_you = self.match_bot = 0
        self.match_winner = None

    def _start_count(self) -> None:
        self._locked = self.pick()  # before "Shoot!" and before their hand is seen: it can't peek
        self._said = None
        self.set_phase("count")

    def choose(self, choice: str) -> bool:
        if choice not in CHOICES:
            return False
        if self.phase == "ask":  # the camera wasn't sure: the player says what they showed
            self.you = choice
            self.reveal()
            return True
        if not self._ready():
            return False
        self.you, self.bot, self.result, self.seen = choice, None, None, False
        self._start_count()
        return True

    def go(self) -> bool:
        """A camera round: count down, then read the player's hand on "Shoot!"."""
        if not self._camera_ready():
            self.camera = False  # the camera was switched off meanwhile: back to tapping a choice
            return False
        if not self._ready():
            return False
        self.camera = True
        self.you, self.bot, self.result, self.seen, self.look = None, None, None, False, None
        self._start_count()
        return True

    def pick(self) -> str:
        """LittleBot's pick for the next round: at random, or in tricky mode the move that beats their likely one."""
        if self.tricky and self.rng.random() >= TRICKY_RANDOM:
            guess = self.guess()
            if guess is not None:
                return beater(guess)
        return self.rng.choice(CHOICES)

    def guess(self) -> str | None:
        """Their likely next move: what they played after the same move and result before, and what they play
        most (recent rounds count more). None before the first round."""
        h = self.history
        if not h:
            return None
        weight = {c: 0.0 for c in CHOICES}
        for i, (move, _result) in enumerate(h):
            fade = TRICKY_FADE ** (len(h) - 1 - i)
            weight[move] += 0.5 * fade
            if i > 0 and h[i - 1] == h[-1]:
                weight[move] += 2.0 * fade
        best = max(weight.values())
        return self.rng.choice([c for c in CHOICES if weight[c] == best])

    def set_tricky(self, on: bool) -> None:
        if on == self.tricky:
            say = "Tricky mode is already on." if on else "I'm already picking at random."
            self._tell(f"Rock Paper Scissors: {say}", say)
            return
        self.tricky = on
        self.host.sound("sparkle" if on else "boop")
        self.eyes.feel("surprised" if on else "happy", 1.2)
        say = ("Tricky mode! I'll watch what you like to play, and try to beat it." if on
               else "Easy mode: I'll pick at random again.")
        self._tell(f"Rock Paper Scissors: tricky mode is now {'on' if on else 'off'}. Say it in a few words.", say)

    def take_look(self) -> None:
        look = self.look
        if look is not None and getattr(look, "done", False):
            sign = getattr(look, "sign", None)
            if sign in CHOICES:
                self.you, self.seen = sign, True
                self.reveal()
            elif self._said is not None:  # the camera couldn't tell, but they said it as they showed it
                self.you = self._said
                self.reveal()
            else:
                self.ask(str(getattr(look, "why", "") or ""))
        elif self.phase_t >= LOOK_GIVE_UP_S:
            self.ask("timeout")

    def ask(self, why: str) -> None:
        """The camera couldn't read the hand: say why, and ask what they showed (said or tapped)."""
        self.set_phase("ask")
        self.host.sound("boop", volume=0.6)
        if why == "no_hand":
            say = "I couldn't see your hand. What did you show?"
        else:
            say = "Hmm, I couldn't tell which one that was. Rock, paper or scissors?"
        self._tell("Rock Paper Scissors: the camera couldn't read their hand, so LittleBot asks what they showed "
                   "(they say it or tap it). Ask them in a few words.", say)

    def reveal(self) -> None:
        assert self.you is not None
        self.bot = self._locked or self.pick()
        self._locked = None
        self.result = outcome(self.you, self.bot)
        self.history.append((self.you, self.result))
        self.set_phase("reveal")
        if self.result == "win":
            self.wins += 1
            self.match_you += 1
            self.score += 1
            self.host.sound("yay")
            self.eyes.feel("surprised", 1.6)
            k = self.k
            self.burst(self.you_x, self.arena_y, list(art.CANDY), 30, speed=(150 * k, 360 * k), life=(0.7, 1.3),
                       size=(6 * k, 11 * k), kinds=("star", "confetti", "dot"), gravity=240 * k)
        elif self.result == "lose":
            self.losses += 1
            self.match_bot += 1
            self.host.sound("boing")
            self.eyes.feel("happy", 1.6)
            k = self.k
            self.burst(self.bot_x, self.arena_y, [art.SKY, art.WHITE, art.LEMON], 16, speed=(120 * k, 260 * k),
                       life=(0.6, 1.0), size=(6 * k, 10 * k), kinds=("star", "dot"))
        else:
            self.draws += 1
            self.host.sound("boop")
            self.eyes.feel("happy", 1.0)
        if max(self.match_you, self.match_bot) >= MATCH_TO:
            self.match_winner = "you" if self.match_you > self.match_bot else "bot"
            self.matches[self.match_winner] += 1
        self._tell(self.note(), self.line())

    def note(self) -> str:
        """The round for a voice that words it itself (GPT-Live)."""
        who = {"win": "they win", "lose": "you (LittleBot) win", "draw": "it's a draw"}[self.result or "draw"]
        how = "showed the camera" if self.seen else "picked"
        match = ""
        if self.match_winner:
            match = f" That wins the match for {'them' if self.match_winner == 'you' else 'you (LittleBot)'}."
        return (f"Rock Paper Scissors: they {how} {self.you}, you picked {self.bot} on the screen, so {who}. "
                f"Score in this first-to-{MATCH_TO} match: them {self.match_you}, you {self.match_bot}.{match}")

    def line(self) -> str:
        """The round in LittleBot's own words, ready to say: "I saw rock. Paper wraps rock: I win! 2 to 1 to me." """
        you, bot = self.you or "rock", self.bot or "rock"
        saw = f"I saw {you}. " if self.seen else ""
        if self.result == "draw":
            head = f"{saw}We both picked {you}: it's a draw!" if not self.seen else f"{saw}Me too: it's a draw!"
        elif self.result == "win":
            head = f"{saw}{RULES[you]}: you win!"
        else:
            head = f"{saw}{RULES[bot]}: I win!"
        a, b = self.match_you, self.match_bot
        again = "Say go" if self.camera else "Pick again"
        if self.match_winner == "you":
            return f"{head} That's {MATCH_TO}! You win the match, {a} to {b}. {again} for a rematch."
        if self.match_winner == "bot":
            return f"{head} That's {MATCH_TO} for me: I win the match, {b} to {a}! {again} for a rematch."
        if a == b:
            score = f"{a} all." if a else ""
        else:
            score = f"{a} to {b} to you." if a > b else f"{b} to {a} to me."
        point = " Match point!" if max(a, b) == MATCH_TO - 1 else ""
        return f"{head} {score}{point}".strip()

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
            if self.match_winner:
                self.set_phase("match")
                self.host.sound("tada", max_wait=1.0)
                x = self.you_x if self.match_winner == "you" else self.bot_x
                k = self.k
                self.burst(x, self.arena_y, list(art.CANDY), 40, speed=(160 * k, 380 * k), life=(0.8, 1.5),
                           size=(6 * k, 12 * k), kinds=("star", "confetti", "dot"), gravity=220 * k)
                self.eyes.feel("surprised" if self.match_winner == "you" else "happy", 2.0)
            else:
                self.set_phase("pick")
        elif self.phase == "match" and self.phase_t >= MATCH_S and not self.in_finale:
            self._new_match()
            self.set_phase("pick")
        self.eyes.look(0.25 if self.phase == "pick" else 0.6, 0.7)
        self.eyes.update(dt, self.rng)

    def button_rect(self, i: int) -> pygame.Rect:
        cx = self.w / 2 + (i - 1) * (self.btn_w + BTN_GAP * self.k)
        return pygame.Rect(int(cx - self.btn_w / 2), self.btn_y, self.btn_w, self.btn_h)

    def button_at(self, x: float, y: float) -> str | None:
        for i, name in enumerate(CHOICES):
            if self.button_rect(i).inflate(int(20 * self.k), int(24 * self.k)).collidepoint(x, y):
                return name
        return None

    def go_rect(self) -> pygame.Rect:
        return pygame.Rect(int(self.w / 2 - self.go_w / 2), int(self.btn_y + 10 * self.k), self.go_w, self.go_h)

    def shows_buttons(self) -> bool:
        """The three choice buttons (always when tapping; with the camera only to say what you showed)."""
        return not self.camera or self.phase == "ask" or (self.phase == "reveal" and not self.seen)

    def on_tap(self, x: float, y: float) -> bool:
        if self.camera and (self.phase in ("pick", "match") or (self.phase == "reveal" and self.seen)):
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
        """Their words: a move, "go", or tricky / easy mode. True: the game used them (nothing for the voice to
        answer); words that only say more than that ("play some jazz") are left for the voice."""
        low = text.lower()
        if TRICKY_ON.search(low) or TRICKY_OFF.search(low):
            self.set_tricky(bool(TRICKY_ON.search(low)))
            return True
        move = heard_move(text)
        if move is not None:
            if self.phase in ("count", "look") and self.you is None:
                self._said = move  # said while showing it: used if the camera can't tell
                return True
            return self.choose(move) or self.phase in ("count", "look", "reveal")
        if not heard_go(text):
            return False
        if self.go() or self.phase in ("count", "look", "ask", "reveal", "match"):
            return True  # a round started (or one is on, or its result is being shown)
        self._tell("Rock Paper Scissors: they said go, but the camera can't see now: they say or tap their pick.",
                   "Say rock, paper or scissors!")
        return True

    # ------------------------------------------------------------ drawing
    def label(self) -> tuple[str, art.Color, art.Color]:
        if self.phase == "count":
            return COUNT_BEATS[min(3, max(0, int(self.phase_t // BEAT_S)))], art.INK, art.WHITE
        if self.phase == "look":
            return ("Shoot!" if self.phase_t < 0.5 else "Looking..."), art.INK, art.WHITE
        if self.phase == "ask":
            return "What did you show?", art.WHITE, art.STRAWBERRY
        if self.phase == "match":
            if self.match_winner == "you":
                return "You win the match!", art.WHITE, art.MINT
            return "LittleBot wins the match!", art.WHITE, art.SKY
        if self.phase == "reveal":
            return {"win": ("You win!", art.WHITE, art.MINT), "lose": ("LittleBot wins!", art.WHITE, art.SKY),
                    "draw": ("Draw!", art.INK, art.LEMON)}[self.result or "draw"]
        if max(self.match_you, self.match_bot) == MATCH_TO - 1:
            return "Match point!", art.WHITE, art.GRAPE
        return ("Ready?" if self.camera else "Pick one!"), art.WHITE, art.STRAWBERRY

    def draw_bubble(self, surf: pygame.Surface, x: float, who: str, choice: str | None) -> None:
        z, ay, br = self.k, self.arena_y, self.bubble_r
        winner = self.phase in ("reveal", "match") and ((who == "you" and self.result == "win") or
                                                        (who == "bot" and self.result == "lose"))
        if winner:
            art.blit_center(surf, art.glow(br * 1.7, art.LEMON, 2.0), (x, ay))
        color = (255, 250, 253) if not winner else art.lighten(art.LEMON, 0.55)
        art.blit_center(surf, art.circle(br + 5 * z, art.inkier((246, 196, 220), 0.25)), (x, ay + 4 * z))
        art.blit_center(surf, art.circle(br, color), (x, ay))
        if self.phase == "count" or (self.phase == "look" and who == "bot"):
            hop = abs(math.sin(self.phase_t / BEAT_S * math.pi)) * 22 * z if self.phase == "count" else 0
            art.blit_center(surf, icon_sprite("rock", int(104 * z)), (x, ay + 6 * z - hop))
        elif self.phase in ("reveal", "match") and choice is not None:
            k = art.ease_out_back(min(1.0, self.phase_t / 0.35), 2.2) if self.phase == "reveal" else 1.0
            art.blit_center(surf, art.scaled(icon_sprite(choice, int(112 * z)), k), (x, ay))
        else:
            q = art.text_outlined("?", int(86 * z), art.WHITE, art.INK_SOFT, 3)
            art.blit_center(surf, q, (x, ay + math.sin(self.t * 3 + x) * 5 * z))

    def render(self, surf: pygame.Surface) -> None:
        k = self.k
        surf.blit(art.background(self.size, seed=17), (0, 0))
        self.eyes.draw(surf)
        art.blit_text(surf, "You", int(36 * k), (self.you_x, 112 * k), art.INK)
        self.draw_bubble(surf, self.bot_x, "bot", self.bot)
        self.draw_bubble(surf, self.you_x, "you", self.you if self.phase != "pick" else None)
        art.blit_center(surf, art.text_outlined("VS", int(52 * k), art.WHITE, art.INK, 4), (self.w / 2, self.arena_y))
        if self.draws:
            art.blit_center(surf, art.pill(f"Draws {self.draws}", int(26 * k), art.INK_SOFT, art.WHITE, pad=(14, 6)),
                            (self.w / 2, self.arena_y + 60 * k))
        if self.shows_buttons():
            self.render_buttons(surf)
        elif self.phase == "pick":
            r = self.go_rect()
            surf.blit(art.rounded_rect(self.go_w, self.go_h, int(36 * k), art.MINT, shadow=6,
                                       border=art.inkier(art.MINT, 0.2), border_w=4), r.topleft)
            art.blit_text(surf, "Go!", int(52 * k), (r.centerx, r.y + 46 * k), art.WHITE)
            art.blit_text(surf, "Show me your hand on Shoot!", int(26 * k), (r.centerx, r.bottom - 24 * k), art.INK)
        else:
            if self.phase == "match":
                hint = "Say go (or tap) for a rematch!"
            elif self.phase == "reveal" and self.you:
                hint = f"I saw {LABELS[self.you]}!"
            else:
                hint = "Show me rock, paper or scissors!"
            art.blit_center(surf, art.pill(hint, int(30 * k), art.INK, art.WHITE, pad=(20, 8)),
                            (self.w / 2, self.btn_y + 70 * k))
        text, fg, bg = self.label()
        y = 36 * k
        art.blit_center(surf, art.pill(text, int(32 * k), fg, bg, pad=(22, 8)), (self.w / 2, y))
        score = int(28 * k)
        you = art.pill(f"You {self.match_you}", score, art.INK, art.WHITE, pad=(18, 7))
        bot = art.pill(f"LittleBot {self.match_bot}", score, art.INK, art.WHITE, pad=(18, 7))
        art.blit_center(surf, you, (self.w - 20 * k - you.get_width() / 2, y))
        art.blit_center(surf, bot, (20 * k + bot.get_width() / 2, y))
        if self.tricky:  # it is reading their habits: say so on the screen too
            badge = art.pill("Tricky mode", int(24 * k), art.WHITE, art.GRAPE, pad=(16, 6))
            art.blit_center(surf, badge, (20 * k + badge.get_width() / 2, y + bot.get_height() / 2 + 6 * k
                                          + badge.get_height() / 2))

    def render_buttons(self, surf: pygame.Surface) -> None:
        asking = self.phase == "ask"
        for i, name in enumerate(CHOICES):
            r = self.button_rect(i)
            chosen = self.phase != "pick" and not asking and self.you == name
            dim = self.phase != "pick" and not asking and not chosen
            color = art.lighten(art.LEMON, 0.35) if chosen else ((246, 232, 240) if dim else art.WHITE)
            k = self.k
            lift = -6 * k if chosen else 0
            surf.blit(art.rounded_rect(r.w, r.h, int(30 * k), color, shadow=6, border=(246, 196, 220) if not chosen else
                                       art.inkier(art.LEMON, 0.2), border_w=4), (r.x, r.y + lift))
            art.blit_center(surf, icon_sprite(name, int(84 * k)), (r.centerx, r.y + 56 * k + lift), 140 if dim else None)
            art.blit_text(surf, LABELS[name], int(32 * k), (r.centerx, r.bottom - 22 * k + lift),
                          art.INK_SOFT if dim else art.INK)

    def summary(self) -> str:
        line = f"You {self.wins} - LittleBot {self.losses}" + (f" - draws {self.draws}" if self.draws else "")
        if self.matches["you"] or self.matches["bot"]:
            line += f" (matches {self.matches['you']}-{self.matches['bot']})"
        return line

    def finale_text(self) -> str:
        ahead = (self.matches["you"] > self.matches["bot"] if self.matches["you"] != self.matches["bot"]
                 else self.wins > self.losses)
        return "You're the champ!" if ahead else "Good game!"

    def brain_note(self) -> str:
        """For LittleBot's voice while the game is on: how it is played, the score, and that the game calls it."""
        play = ("they say go (or tap) and show rock, paper or scissors to the camera on Shoot!; if it can't tell, "
                "they say or tap what they showed") if self.camera else \
            "the camera can't see now (don't mention it): they say or tap rock, paper or scissors"
        return (f"Rock Paper Scissors is on your screen: {play}; first to {MATCH_TO} wins the match. The game picks "
                f"for you (before it sees theirs) and says each result by itself: never pick, guess or call a round "
                f"yourself. This match: them {self.match_you}, you {self.match_bot}; matches won: them "
                f"{self.matches['you']}, you {self.matches['bot']}. Tricky mode (you learn their habits and try to "
                f"beat them) is {'on' if self.tricky else 'off'}: 'tricky mode' turns it on, 'easy mode' off.")

    def intro(self) -> str:
        """The one short sentence a voice says as the game starts (a long intro is talked over and lost)."""
        if self.camera:
            return "It's on! Say go, then show me your hand on shoot."
        return "It's on! Say rock, paper or scissors."

    def status_extra(self) -> dict[str, Any]:
        reveal = self.phase in ("reveal", "match")
        return {"wins": self.wins, "losses": self.losses, "draws": self.draws, "phase": self.phase,
                "you": self.you, "bot": self.bot if reveal else None, "result": self.result,
                "mode": "camera" if self.camera else "tap", "seen": self.seen,
                "match": {"you": self.match_you, "bot": self.match_bot, "to": MATCH_TO},
                "matches": dict(self.matches), "tricky": self.tricky, "how": self.brain_note(), "intro": self.intro()}
