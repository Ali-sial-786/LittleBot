"""The built-in cards. Importing this registers them (littlebot.screens.base.register).

Each module defines its Card subclass(es) and calls ``register(CardType(...))`` at import.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger("littlebot.screens")

_MODULES = ("weather", "clock", "timer", "maths", "definition", "explain", "recipe", "facts")

for _name in _MODULES:
    try:
        importlib.import_module(f"littlebot.screens.cards.{_name}")
    except ModuleNotFoundError as e:
        if e.name != f"littlebot.screens.cards.{_name}":
            raise
