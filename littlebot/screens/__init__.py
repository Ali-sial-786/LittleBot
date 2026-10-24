"""LittleBot's screen as part of the conversation: info cards it presents while it talks.

* ``base``      - the Card interface, the host cards use, the card-type registry
* ``cards``     - the built-in cards (weather, words, explanations, recipes, timers, clocks, maths, ...)
* ``service``   - ScreenService: one card at a time over the face, the presenter's eyes, when it goes away
* ``presenter`` - LittleBot's eyes, small, lip-synced, presenting the card
* ``images``    - pictures for cards, fetched when needed and never kept
* ``director``  - decides during a voice chat what to show for what was just asked

See docs/SCREENS.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.screens.service import ScreenService

__all__ = ["create_service"]


def create_service(ctx: Any) -> ScreenService:
    """Factory used by ``app/robot.py``."""
    from littlebot.screens.service import ScreenService  # noqa: PLC0415

    return ScreenService(ctx)
