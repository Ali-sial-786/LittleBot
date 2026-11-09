"""The voice games' built-in content (assets/games/*.json): loaded once, checked, never written.

``load(name)`` returns the parsed file (``{}`` when it is missing or broken: the game then says it can't play
offline). The files are small (a few hundred short entries) and read on first use, not at import.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from littlebot.core.paths import ASSETS_DIR

log = logging.getLogger("littlebot.games.voice")

GAMES_DIR = ASSETS_DIR / "games"


@functools.lru_cache(maxsize=16)
def load(name: str, directory: str | None = None) -> dict[str, Any]:
    path = (Path(directory) if directory else GAMES_DIR) / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("voice games: could not read %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def items(name: str, key: str) -> list[dict[str, Any]]:
    raw = load(name).get(key)
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def key_of(*parts: Any) -> str:
    """A short stable id for an item (a question's words): what the "recently asked" table keeps."""
    from littlebot.games.voice.answers import normalize  # noqa: PLC0415

    text = "|".join(normalize(p) for p in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


__all__ = ["GAMES_DIR", "items", "key_of", "load"]
