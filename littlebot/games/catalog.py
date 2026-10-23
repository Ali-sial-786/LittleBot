"""The game catalogue: metadata only (cheap to import - no pygame).

Each entry names the module and class that implement the game; the service imports it only
when the game starts.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GameInfo:
    id: str
    name: str
    description: str
    ages: str  # human label for parents, e.g. "6 months+"
    min_age_months: int
    audience: str  # "little" (babies / toddlers: no reading, cause and effect) | "big" (kids and grown-ups)
    minutes: float  # default session length
    emoji: str  # dashboard tile
    how_to: str  # one line for grown-ups
    module: str
    cls: str

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "description": self.description, "ages": self.ages,
                "min_age_months": self.min_age_months, "audience": self.audience, "minutes": self.minutes,
                "emoji": self.emoji, "how_to": self.how_to}

    def load(self) -> type:
        return getattr(importlib.import_module(self.module), self.cls)


GAMES: tuple[GameInfo, ...] = (
    GameInfo("bubbles", "Bubble Pop", "Colourful bubbles float up the screen. Tap them to pop them - "
             "or watch them pop on their own.", "6 months+", 6, "little", 3.0, "🫧",
             "Tap a bubble to pop it; tapping empty space blows a new one. A golden star bubble comes now and then.",
             "littlebot.games.bubbles", "BubblePop"),
    GameInfo("peekaboo", "Peekaboo Friends", "Animal friends hide behind toy blocks and pop out to say "
             "moo, woof and quack.", "6 months+", 6, "little", 3.0, "🐮",
             "Animals peek out by themselves. Tap a block to find who is hiding, tap an animal to make it giggle.",
             "littlebot.games.peekaboo", "PeekabooFriends"),
    GameInfo("colours", "Colour Burst", "Every tap paints a splash of colour or a smiling shape, with a "
             "musical note.", "6 months+", 6, "little", 3.0, "🎨",
             "Tap anywhere. Left side plays low notes, right side high ones. LittleBot paints too if nobody taps.",
             "littlebot.games.colour_burst", "ColourBurst"),
    GameInfo("star", "Follow the Star", "LittleBot's big eyes follow a twinkly star around the screen - "
             "and peek at your child through the camera.", "6 months+", 6, "little", 3.0, "⭐",
             "Watch the eyes follow the star. Tap the star to catch it, or tap anywhere to send it there.",
             "littlebot.games.follow_star", "FollowTheStar"),
    GameInfo("memory", "Memory Lights", "LittleBot lights up coloured pads with tones. Copy the pattern - "
             "it grows by one each round.", "3 years+", 36, "big", 5.0, "🟩",
             "Watch the pads light up, then tap them in the same order. A mistake starts a new game.",
             "littlebot.games.memory", "MemoryLights"),
    GameInfo("tictactoe", "Tic-Tac-Toe", "Noughts and crosses against LittleBot. It gets cleverer when "
             "you win and gentler when you lose.", "4 years+", 48, "big", 5.0, "❌",
             "You are X. Tap a square. Take turns starting.",
             "littlebot.games.tictactoe", "TicTacToe"),
    GameInfo("reaction", "Reaction Race", "Wait for red to turn green, then tap as fast as you can. "
             "Shows your time in milliseconds.", "4 years+", 48, "big", 5.0, "⚡",
             "Tap to start, wait for green, tap! Five tries per round; tapping too soon doesn't count.",
             "littlebot.games.reaction", "ReactionRace"),
    GameInfo("rps", "Rock Paper Scissors", "Pick rock, paper or scissors - LittleBot shows its pick "
             "after the countdown.", "4 years+", 48, "big", 5.0, "✂️",
             "Tap a choice (or say it if LittleBot is listening). Rock beats scissors, scissors beat paper, "
             "paper beats rock.",
             "littlebot.games.rps", "RockPaperScissors"),
)

BY_ID: dict[str, GameInfo] = {g.id: g for g in GAMES}
