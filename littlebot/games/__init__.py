"""On-screen games: little cause-and-effect games for babies and quick games for kids and grown-ups.

Modules
-------
* ``catalog``  - game metadata (id, name, ages ...), cheap to import (no pygame)
* ``service``  - ``GamesService``: list / start / stop / status, leases, time limits, display hooks
* ``base``     - ``Game`` base class (update / draw / tap separated) and the ``GameHost`` interface
* ``art``      - shared palette, cached sprites, LittleBot's eyes, particles (imports pygame)
* one module per game: ``bubbles``, ``peekaboo``, ``colour_burst``, ``follow_star``, ``memory``,
  ``tictactoe``, ``reaction``, ``rps``

While a game runs the display loop (``littlebot.face.display``) draws it instead of the face and
sends it taps and keys. See docs/GAMES.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from littlebot.games.catalog import BY_ID, GAMES, GameInfo

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.games.service import GamesService

__all__ = ["BY_ID", "GAMES", "GameInfo", "create_service"]


def create_service(ctx: Any) -> "GamesService":
    """Factory used by ``app/robot.py``."""
    from littlebot.games.service import GamesService

    return GamesService(ctx)
