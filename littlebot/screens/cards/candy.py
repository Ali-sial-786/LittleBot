"""Small drawing helpers shared by the weather, clock, timer and maths cards (the games' candy look).

Everything here draws through littlebot/games/art.py, so it is cached the same way: a card's frame is
mostly blits of sprites made once.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, PRESENTER_BOX, TITLE_PX, W

HEADER_X = PRESENTER_BOX[0] + PRESENTER_BOX[2] + 16  # the title starts right of the presenter's eyes
HEADER_Y = PRESENTER_BOX[1] + PRESENTER_BOX[3] // 2 + 4
PANEL = pygame.Rect(16, 108, W - 32, 360)  # the card's content box under the title
PANEL_FILL: art.Color = (255, 248, 251)
PANEL_EDGE: art.Color = (246, 196, 220)

# Countries where clocks are usually read in 12 hours (am / pm).
TWELVE_HOUR_COUNTRIES = frozenset({"US", "CA", "AU", "NZ", "IN", "PH", "PK", "BD", "EG", "SA", "MY", "IE", "CO",
                                   "HN", "SV", "NI", "PR", "JO"})


@functools.lru_cache(maxsize=512)
def _fitted(s: str, size: int, max_w: int, min_size: int) -> tuple[str, int]:
    """The largest size from ``size`` down to ``min_size`` that fits ``max_w``; else ``s`` cut short with "..."."""
    while size > min_size and art.font(size).size(s)[0] > max_w:
        size -= 2
    if art.font(size).size(s)[0] <= max_w:
        return s, size
    while len(s) > 1 and art.font(size).size(s + "...")[0] > max_w:
        s = s[:-1]
    return s.rstrip() + "...", size


def fit(s: str, size: int, max_w: int, color: Sequence[int] = art.INK, *, min_size: int = BODY_PX) -> pygame.Surface:
    """``s`` as big as ``size`` while it fits in ``max_w`` pixels (never under ``min_size``)."""
    text, px = _fitted(str(s), int(size), int(max_w), int(min_size))
    return art.text(text, px, color)


def header(surf: pygame.Surface, title: str, *, dy: float = 0.0, color: Sequence[int] = art.INK) -> None:
    """The card's title, right of the presenter's eyes."""
    if not title:
        return
    img = fit(title, TITLE_PX, W - HEADER_X - 20, color)
    surf.blit(img, img.get_rect(midleft=(HEADER_X, int(HEADER_Y + dy))))


def panel(surf: pygame.Surface, rect: pygame.Rect = PANEL, *, dy: float = 0.0, fill: art.Color = PANEL_FILL,
          edge: art.Color = PANEL_EDGE) -> pygame.Rect:
    """The soft rounded box a card's content sits in (like the games' boards). Returns where it is."""
    box = art.rounded_rect(rect.w, rect.h, 30, fill, shadow=6, border=edge, border_w=4)
    r = rect.move(0, int(dy))
    surf.blit(box, r.topleft)
    return r


def chip(s: str, size: int = BODY_PX, fg: Sequence[int] = art.INK, bg: Sequence[int] = art.WHITE,
         border: Sequence[int] | None = None) -> pygame.Surface:
    """A rounded label ("High 13°")."""
    return art.pill(s, size, fg, bg, pad=(16, 7), border=border)


def degrees(value: Any, size: int, color: Sequence[int] = art.INK) -> pygame.Surface:
    """ "12°" with a drawn degree ring (the font's own ° sits too far from the number)."""
    s = "-" if value is None else str(value)
    key = ("deg", s, int(size), tuple(color))

    def make() -> pygame.Surface:
        num = art.font(size).render(s, True, color)
        r = max(3, round(size * 0.1))
        lw = max(2, round(size * 0.036))
        gap = max(1, round(size * 0.035))
        out = pygame.Surface((num.get_width() + gap + 2 * r + 1, num.get_height()), pygame.SRCALPHA)
        out.blit(num, (0, 0))
        out.blit(art.ring(r, lw, color), (num.get_width() + gap, round(size * 0.07)))
        return out

    return art.cached(key, make)


def blit_mid(surf: pygame.Surface, img: pygame.Surface, center: tuple[float, float]) -> pygame.Rect:
    r = img.get_rect(center=(int(center[0]), int(center[1])))
    surf.blit(img, r)
    return r


def slide_in(t: float, seconds: float = 0.35, distance: float = 26.0) -> float:
    """How far below its place a card's content still is ``t`` seconds after it appeared."""
    return (1.0 - art.ease_out_cubic(t / seconds)) * distance if t < seconds else 0.0


def uses_12h(ctx: Any) -> bool:
    """Whether this home reads the clock in 12 hours: the country of home (dashboard), else the locale."""
    country = ""
    try:
        home = ctx.settings.get("info.location") or {}
        country = str(home.get("country_code") or "") if isinstance(home, dict) else ""
    except Exception:  # noqa: BLE001 - no settings (previews): fall back to the locale
        country = ""
    if not country:
        from littlebot.info.weather import locale_country

        country = locale_country() or ""
    return not country or country.upper() in TWELVE_HOUR_COUNTRIES


def hour_label(hhmm: str, twelve: bool) -> str:
    """ "15:00" -> "3 pm" (12 hours) or "15:00"; "09:30" -> "9:30 am"."""
    try:
        h, m = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        return str(hhmm)
    if not twelve:
        return f"{h:02d}:{m:02d}"
    h12 = h % 12 or 12
    ampm = "am" if h < 12 else "pm"
    return f"{h12} {ampm}" if m == 0 else f"{h12}:{m:02d} {ampm}"


__all__ = ["HEADER_X", "PANEL", "blit_mid", "chip", "degrees", "fit", "header", "hour_label", "panel", "slide_in", "uses_12h"]
