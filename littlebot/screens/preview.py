"""Draw a card to a PNG without a window (tests, docs, checking a layout): ``render(card, path)``.

The presenter's eyes are drawn where the card leaves room for them, by littlebot.screens.presenter
when it is there (else an outline of the box), so a preview looks like the robot's screen.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from littlebot.screens.base import H, W, Card


def _pygame() -> Any:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame  # noqa: PLC0415

    if not pygame.font.get_init():
        pygame.font.init()
    return pygame


def surface(card: Card, *, seconds: float = 1.0, mouth: float = 0.0) -> Any:
    """The card as it looks ``seconds`` after it appeared (a pygame.Surface, W x H)."""
    pg = _pygame()
    surf = pg.Surface((W, H))
    step = 1 / 30
    t = 0.0
    while t < seconds:
        card.update(step)
        t += step
    card.draw(surf)
    try:
        from littlebot.screens import presenter  # noqa: PLC0415

        presenter.draw(surf, card.presenter_box, mood=card.mood, mouth=mouth,
                       color=getattr(card.host, "eye_color", (90, 210, 255)), t=card.t)
    except ImportError:
        pg.draw.rect(surf, (98, 50, 88), pg.Rect(*card.presenter_box), 2, border_radius=16)
    return surf


def render(card: Card, path: str | Path, **kw: Any) -> Path:
    pg = _pygame()
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pg.image.save(surface(card, **kw), str(out))
    return out
