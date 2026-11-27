"""The built-in cards. Importing this registers them (littlebot.screens.base.register).

Each module defines its Card subclass(es) and calls ``register(CardType(...))`` at import.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger("littlebot.screens")

_MODULES = ("weather", "clock", "timer", "maths", "definition", "explain", "recipe", "facts", "games",
            # the rethink's cards (a module not written yet is skipped)
            "lists", "reminders", "calendar", "briefing", "home", "glance", "note", "music", "quiz", "word_game",
            "game_board",
            # LittleBot's own photos and videos (littlebot/capture): never offered to the director
            "gallery",
            # LittleBot's own settings panel (a 5 s hold on the screen): never offered to the director
            "settings")

for _name in _MODULES:
    try:
        importlib.import_module(f"littlebot.screens.cards.{_name}")
    except ModuleNotFoundError as e:
        if e.name != f"littlebot.screens.cards.{_name}":
            raise
