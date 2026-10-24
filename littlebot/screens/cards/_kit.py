"""Shared drawing and layout helpers for the word, explanation, recipe and facts cards.

The look is the games' (littlebot/games/art.py): the soft pink background, cream rounded panels with
a plum outline and a soft shadow, candy-coloured chips and dark plum ink. Readability rules
(littlebot/screens/base.py): text at least BODY_PX, titles at TITLE_PX, at most MAX_LINES lines at a
time (more is paged), the presenter's box left free.

Everything that is text is rendered once and cached (``art.cached`` / ``art.text``); a frame is blits.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Sequence
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import (
    BODY_PX,
    MAX_LINES,
    PRESENTER_BOX,
    SMALL_PX,
    TITLE_PX,
    H,
    W,
)

# The header sits to the right of the presenter's eyes; the content panel below both.
_PX, _PY, _PW, _PH = PRESENTER_BOX
HEADER = pygame.Rect(_PX + _PW + 16, 8, W - (_PX + _PW + 16) - 14, _PY + _PH - 6)
PANEL = pygame.Rect(12, _PY + _PH + 12, W - 24, H - (_PY + _PH + 12) - 12)
PAD = 22  # inside the panel
PANEL_COLOR: art.Color = art.CREAM
PANEL_BORDER: art.Color = art.mix(art.INK, art.BG_BOTTOM, 0.55)
TEXT: art.Color = art.INK  # body text on cream: contrast ~10:1
TEXT_SOFT: art.Color = art.mix(art.INK, art.INK_SOFT, 0.45)  # secondary text, still ~6:1 on cream
FADE_S = 0.6  # a picture fades in over this long
ACCENTS: tuple[art.Color, ...] = (art.STRAWBERRY, art.SKY, art.MINT, art.TANGERINE, art.GRAPE, art.PERIWINKLE,
                                  art.BUBBLEGUM, art.LEMON)


# ---------------------------------------------------------------- spec cleaning
_SPACE = re.compile(r"\s+")
_MARKUP = re.compile(r"[*_`#>]+")


def clean(value: Any, limit: int = 120) -> str:
    """One tidy line from whatever the AI sent (no markdown, no newlines, at most ``limit`` chars)."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    s = _SPACE.sub(" ", _MARKUP.sub("", str(value))).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-") + "..."
    return s


def str_list(value: Any, *, max_items: int, limit: int = 120) -> list[str]:
    """A list of tidy strings (a single string is one item; empty ones dropped)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = [clean(v, limit) for v in value]
    return [s for s in out if s][:max_items]


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- text layout
def text_w(s: str, size: int) -> int:
    return art.font(size).size(s)[0]


def line_h(size: int) -> int:
    """Line pitch for ``size`` (the font's own line size plus a little air)."""
    return art.font(size).get_linesize() + max(4, size // 7)


@functools.lru_cache(maxsize=512)
def wrap(text: str, size: int, width: int) -> tuple[str, ...]:
    """``text`` word-wrapped to ``width`` pixels at ``size``; a word longer than a line is split."""
    f = art.font(size)
    out: list[str] = []
    line = ""
    for word in text.split():
        while f.size(word)[0] > width:  # a very long word: split it with a hyphen
            cut = len(word) - 1
            while cut > 1 and f.size(word[:cut] + "-")[0] > width:
                cut -= 1
            head, word = word[:cut] + "-", word[cut:]
            if line:
                out.append(line)
                line = ""
            out.append(head)
        trial = f"{line} {word}" if line else word
        if f.size(trial)[0] <= width:
            line = trial
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return tuple(out)


def ellipsize(text: str, size: int, width: int) -> str:
    if text_w(text, size) <= width:
        return text
    while text and text_w(text + "...", size) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def fit(text: str, sizes: Sequence[int], width: int, max_lines: int = MAX_LINES) -> tuple[int, tuple[str, ...]]:
    """The biggest of ``sizes`` (big to small) at which ``text`` fits in ``max_lines`` lines, with its
    lines; when none fits, the smallest size and all its lines (the caller pages them)."""
    lines: tuple[str, ...] = ()
    for size in sizes:
        lines = wrap(text, size, width)
        if len(lines) <= max_lines:
            return size, lines
    return sizes[-1], lines


def paged(blocks: Sequence[int], max_lines: int = MAX_LINES) -> list[list[int]]:
    """Split items (each ``blocks[i]`` lines tall) into as few pages of at most ``max_lines`` lines as
    possible, as evenly filled as possible (8 items: 4 + 4, not 5 + 3); an item taller than a page gets
    a page of its own."""
    pages = _greedy(blocks, max_lines)
    if len(pages) > 1:
        target = -(-sum(blocks) // len(pages))
        for limit in range(min(max_lines, max(target, max(blocks))), max_lines + 1):
            even = _greedy(blocks, limit)
            if len(even) == len(pages):
                return even
    return pages


def _greedy(blocks: Sequence[int], max_lines: int) -> list[list[int]]:
    pages: list[list[int]] = []
    cur: list[int] = []
    used = 0
    for i, n in enumerate(blocks):
        if cur and used + n > max_lines:
            pages.append(cur)
            cur, used = [], 0
        cur.append(i)
        used += n
    if cur:
        pages.append(cur)
    return pages


def text_block(lines: Sequence[str], size: int, color: Sequence[int] = TEXT, *, align: str = "left",
               pitch: int | None = None) -> pygame.Surface:
    """Lines of text as one cached sprite."""
    pitch = pitch or line_h(size)
    key = ("kit_block", tuple(lines), size, tuple(color), align, pitch)

    def make() -> pygame.Surface:
        imgs = [art.font(size).render(s, True, color) for s in lines]
        w = max((i.get_width() for i in imgs), default=1)
        h = pitch * (len(imgs) - 1) + (imgs[-1].get_height() if imgs else 1)
        out = pygame.Surface((max(1, w), max(1, h)), pygame.SRCALPHA)
        for n, img in enumerate(imgs):
            x = 0 if align == "left" else (w - img.get_width()) // (2 if align == "center" else 1)
            out.blit(img, (x, n * pitch))
        return out

    return art.cached(key, make)


# ---------------------------------------------------------------- chrome
def background(surf: pygame.Surface, seed: int = 11) -> None:
    surf.blit(art.background((W, H), seed=seed), (0, 0))


def panel(surf: pygame.Surface, rect: pygame.Rect = PANEL, color: Sequence[int] = PANEL_COLOR, *,
          radius: int = 26, border: Sequence[int] | None = PANEL_BORDER) -> None:
    surf.blit(art.rounded_rect(rect.w, rect.h, radius, color, shadow=5, border=border, border_w=3), rect.topleft)


def header_title(surf: pygame.Surface, title: str, *, size: int = TITLE_PX, rect: pygame.Rect = HEADER,
                 color: Sequence[int] = art.INK) -> pygame.Rect:
    """The title beside the presenter: up to two lines at ``size`` (never smaller), else ellipsized."""
    lines = list(wrap(title, size, rect.w))
    if len(lines) > 2:
        lines = [lines[0], ellipsize(" ".join(lines[1:]), size, rect.w)]
    pitch = art.font(size).get_linesize()
    img = text_block(lines, size, color, pitch=pitch)
    r = img.get_rect(midleft=(rect.x, rect.centery))
    surf.blit(img, r)
    return r


def outlined_fit(word: str, sizes: Sequence[int], width: int, color: Sequence[int] = art.WHITE,
                 outline: Sequence[int] = art.INK) -> pygame.Surface:
    """The biggest outlined word (title style) that fits ``width``; the smallest size otherwise."""
    for size in sizes:
        if text_w(word, size) + 8 <= width:
            return art.text_outlined(word, size, color, outline, 3)
    return art.text_outlined(ellipsize(word, sizes[-1], width - 8), sizes[-1], color, outline, 3)


def chip(label: str, color: Sequence[int], *, size: int = SMALL_PX, fg: Sequence[int] = art.WHITE) -> pygame.Surface:
    """A candy label (part of speech, "Step 3 of 8"). White text on a darkened candy colour for contrast."""
    return art.pill(label, size, fg, art.inkier(color, 0.22) if fg == art.WHITE else color, pad=(16, 7))


def number_badge(n: int | str, color: Sequence[int], r: int = 22) -> pygame.Surface:
    key = ("kit_badge", str(n), tuple(color), r)

    def make() -> pygame.Surface:
        out = pygame.Surface((2 * r, 2 * r + 3), pygame.SRCALPHA)
        out.blit(art.circle(r, art.inkier(color, 0.45)), (0, 3))
        out.blit(art.circle(r, color), (0, 0))
        out.blit(art.circle(r - 3, art.lighten(color, 0.08)), (3, 3))
        t = art.text_outlined(str(n), int(r * 1.7), art.WHITE, art.inkier(color, 0.5), 2)
        out.blit(t, t.get_rect(center=(r, r + 1)))
        return out

    return art.cached(key, make)


def page_dots(surf: pygame.Surface, n: int, i: int, center: tuple[float, float]) -> None:
    """``n`` dots, the ``i``-th one big and coloured (only when there is more than one page)."""
    if n <= 1:
        return
    gap = 26
    x0 = center[0] - gap * (n - 1) / 2
    for k in range(n):
        on = k == i
        art.blit_center(surf, art.circle(8 if on else 6, art.STRAWBERRY if on else art.mix(art.INK_SOFT, art.WHITE, .4)),
                        (x0 + k * gap, center[1]))


def arrow(direction: int, color: Sequence[int] = art.WHITE, r: int = 24) -> pygame.Surface:
    """A round next/back button (tap targets on paged cards)."""
    key = ("kit_arrow", direction, tuple(color), r)

    def make() -> pygame.Surface:
        out = pygame.Surface((2 * r, 2 * r + 3), pygame.SRCALPHA)
        out.blit(art.circle(r, art.mix(art.INK, art.BG_BOTTOM, 0.5)), (0, 3))
        out.blit(art.circle(r, color), (0, 0))

        def draw(s: pygame.Surface, k: float) -> None:
            c = r * k
            d = r * 0.42 * k
            pts = [(c - d * 0.5 * direction, c - d), (c + d * 0.8 * direction, c), (c - d * 0.5 * direction, c + d)]
            pygame.draw.polygon(s, art.INK, pts)

        out.blit(art.supersampled((2 * r, 2 * r), draw), (0, 0))
        return out

    return art.cached(key, make)


def appear(t: float, start: float, dur: float = 0.45) -> float:
    """0..1 progress of an entrance that starts at ``start`` seconds."""
    return art.clamp((t - start) / dur, 0.0, 1.0)


def blit_alpha(surf: pygame.Surface, img: pygame.Surface, pos: tuple[int, int], alpha: float) -> None:
    a = int(art.clamp(alpha, 0, 1) * 255)
    if a <= 0:
        return
    if a >= 255:
        surf.blit(img, pos)
        return
    img.set_alpha(a)
    surf.blit(img, pos)
    img.set_alpha(255)


# ---------------------------------------------------------------- pictures
def _rounded(img: pygame.Surface, radius: int) -> pygame.Surface:
    w, h = img.get_size()
    out = pygame.Surface((w, h), pygame.SRCALPHA)
    out.blit(img, (0, 0))
    mask = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(), border_radius=radius)
    out.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    return out


class PictureSlot:
    """A picture a card asked its host for: a soft placeholder while it loads, then a fade-in.

    ``wanted``: a picture was asked for and hasn't failed (a layout keeps room for it while True);
    ``shown``: it arrived. The scaled, framed sprite is made once per box size.
    """

    def __init__(self, host: Any, query: str = "", url: str = "") -> None:
        self.query, self.url = query, url
        self.picture: Any = None
        if query or url:
            try:
                self.picture = host.picture(query=query, url=url)
            except Exception:  # noqa: BLE001 - a host that can't fetch: no picture
                self.picture = None
        self.ready_at: float | None = None
        self._sprite: tuple[tuple[int, int], pygame.Surface] | None = None

    @property
    def loading(self) -> bool:
        return self.picture is not None and not self.picture.ready

    @property
    def shown(self) -> bool:
        return self.picture is not None and self.picture.ready and self.picture.surface is not None

    @property
    def wanted(self) -> bool:
        return self.loading or self.shown

    def update(self, t: float) -> None:
        if self.ready_at is None and self.shown:
            self.ready_at = t

    def credit(self) -> str:
        return str(getattr(self.picture, "credit", "") or "") if self.shown else ""

    def sprite(self, box: tuple[int, int]) -> pygame.Surface | None:
        """The picture scaled to fit ``box`` (up to 1.5x, never stretched), with rounded corners and a
        white frame."""
        if not self.shown:
            return None
        if self._sprite is not None and self._sprite[0] == box:
            return self._sprite[1]
        src = self.picture.surface
        frame = 6
        bw, bh = box[0] - 2 * frame, box[1] - 2 * frame
        w, h = src.get_size()
        k = min(bw / max(1, w), bh / max(1, h), 1.5)
        size = (max(1, int(w * k)), max(1, int(h * k)))
        try:
            img = pygame.transform.smoothscale(src, size)
        except (ValueError, pygame.error):
            img = pygame.transform.scale(src, size)
        img = _rounded(img, 14)
        out = pygame.Surface((size[0] + 2 * frame, size[1] + 2 * frame + 4), pygame.SRCALPHA)
        out.blit(art.rounded_rect(size[0] + 2 * frame, size[1] + 2 * frame, 18, art.WHITE, shadow=4,
                                  border=PANEL_BORDER, border_w=2), (0, 0))
        out.blit(img, (frame, frame))
        self._sprite = (box, out)
        return out

    def draw(self, surf: pygame.Surface, rect: pygame.Rect, t: float, *, credit: bool = True) -> None:
        """The picture centred in ``rect`` (its credit underneath, small), or the placeholder."""
        credit_h = line_h(SMALL_PX) if credit else 0
        box = pygame.Rect(rect.x, rect.y, rect.w, rect.h - credit_h)
        if self.loading or (self.shown and self.ready_at is not None and t - self.ready_at < FADE_S):
            placeholder(surf, box, t)
        spr = self.sprite(box.size)
        if spr is None:
            return
        fade = 1.0 if self.ready_at is None else art.ease_in_out(art.clamp((t - self.ready_at) / FADE_S, 0, 1))
        r = spr.get_rect(center=box.center)
        blit_alpha(surf, spr, r.topleft, fade)
        if credit and self.credit():
            label = text_block([ellipsize(self.credit(), SMALL_PX, rect.w)], SMALL_PX, TEXT_SOFT)
            blit_alpha(surf, label, label.get_rect(midtop=(rect.centerx, r.bottom + 2)).topleft, fade)


def placeholder(surf: pygame.Surface, rect: pygame.Rect, t: float) -> None:
    """A soft rounded tile with three gently bouncing dots: a picture is on its way."""
    surf.blit(art.rounded_rect(rect.w, rect.h, 22, art.mix(art.BG_TOP, art.WHITE, 0.45), border=art.mix(
        art.BG_BOTTOM, art.INK_SOFT, 0.25), border_w=2), rect.topleft)
    for k in range(3):
        phase = t * 3.2 - k * 0.55
        lift = max(0.0, math.sin(phase)) * 10
        art.blit_center(surf, art.circle(8, art.mix(ACCENTS[k], art.WHITE, 0.25)),
                        (rect.centerx + (k - 1) * 28, rect.centery - lift))


__all__ = ["ACCENTS", "BODY_PX", "HEADER", "MAX_LINES", "PAD", "PANEL", "SMALL_PX", "TEXT", "TEXT_SOFT", "TITLE_PX",
           "PictureSlot", "appear", "arrow", "as_int", "background", "blit_alpha", "chip", "clean", "ellipsize",
           "fit", "header_title", "line_h", "number_badge", "outlined_fit", "page_dots", "paged", "panel",
           "placeholder", "str_list", "text_block", "text_w", "wrap"]
