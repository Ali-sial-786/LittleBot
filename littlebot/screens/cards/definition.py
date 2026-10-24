"""The "definition" card: a word, how to say it, and what it means; also spelling and translations.

Three modes (the ``mode`` field):

* ``meaning`` (default), "what does encyclopedia mean?": the word big, how to say it as a simple
  respelling with the stressed part in capitals ("en-SY-kluh-PEE-dee-uh", never IPA; the capitals are
  shown in colour), the part of speech, one or two short meanings, one example sentence, and for a
  concrete noun optionally a picture (``picture_query``).
* ``spell``, "how do you spell necessary?": the letters big on candy tiles, popping in one at a time
  at about the pace of spelling them out loud; then the whole word under them. "again" replays it.
* ``translate``, "how do you say apple in Spanish?": the translation big, how to say it, the language
  and the word it translates, and optionally an example in that language and a picture.

Body text (meanings and example) is at most MAX_LINES lines per page; anything longer is paged
("next", or a tap, or on its own after a while).
"""

from __future__ import annotations

import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import (
    BODY_PX,
    MAX_LINES,
    TITLE_PX,
    Card,
    CardType,
    register,
)
from littlebot.screens.cards import _kit as kit

MODES = ("meaning", "spell", "translate")
WORD_SIZES = (76, 68, 60, 52, TITLE_PX)
BIG_SIZES = (104, 92, 80, 68, 56, TITLE_PX)  # the translated word (it has the panel to itself)
SAY_PX = 40
MEANING_PX = 36
EXAMPLE_PX = BODY_PX
PIC_W = 250
PAGE_S = 9.0  # a paged definition turns its page on its own after this long
LETTER_S = 0.5  # spelling: one letter every half second (about the pace of saying them)
LETTER_FIRST = 0.6
MAX_LETTERS = 36
TILE_SIZES = (60, 52, 44)  # letter tiles: as big as the word allows
TILE_GAP = 10
TILE = TILE_SIZES[0]


def _say_parts(say_it: str) -> list[tuple[str, bool]]:
    """ "en-SY-kluh-PEE-dee-uh" -> [("en", False), ("-", None)...]: syllables, stressed when in capitals."""
    parts: list[tuple[str, bool]] = []
    for token in re.split(r"(\s+|-)", say_it):
        if not token:
            continue
        if token == "-" or token.isspace():
            parts.append((" " if token.isspace() else "-", False))
            continue
        letters = [c for c in token if c.isalpha()]
        stressed = len(letters) >= 1 and all(c.isupper() for c in letters) and len(say_it) > len(token)
        parts.append((token, stressed))
    return parts


def say_it_sprite(say_it: str, size: int = SAY_PX, accent: art.Color = art.STRAWBERRY,
                  max_w: int = 10_000) -> pygame.Surface:
    """How to say a word, the stressed syllables in a strong colour, hyphens soft; wrapped after a hyphen
    when wider than ``max_w``."""
    key = ("def_say", say_it, size, accent, max_w)

    def make() -> pygame.Surface:
        f = art.font(size)
        strong = art.inkier(accent, 0.42)
        lines: list[list[pygame.Surface]] = [[]]
        used = 0
        for token, stressed in _say_parts(say_it):
            color = strong if stressed else (kit.TEXT_SOFT if token == "-" else kit.TEXT)
            img = f.render(token, True, color)
            if used and used + img.get_width() > max_w and token not in ("-", " "):
                lines.append([])
                used = 0
            if not used and token == " ":
                continue
            lines[-1].append(img)
            used += img.get_width() + 2
        pitch = f.get_linesize() + 4
        w = max(sum(i.get_width() + 2 for i in ln) for ln in lines)
        out = pygame.Surface((max(1, w), pitch * (len(lines) - 1) + f.get_height()), pygame.SRCALPHA)
        for n, ln in enumerate(lines):
            x = 0
            for img in ln:
                out.blit(img, (x, n * pitch))
                x += img.get_width() + 2
        return out

    return art.cached(key, make)


def fit_say_it(say_it: str, max_w: int, accent: art.Color = art.STRAWBERRY) -> pygame.Surface:
    """The biggest respelling (SAY_PX down to BODY_PX) on one line, else wrapped at BODY_PX."""
    for size in (SAY_PX, 36, BODY_PX):
        img = say_it_sprite(say_it, size, accent)
        if img.get_width() <= max_w:
            return img
    return say_it_sprite(say_it, BODY_PX, accent, max_w)


def speaker_icon(color: art.Color, r: int = 18) -> pygame.Surface:
    """A little round "say it" speaker badge."""
    key = ("def_speaker", color, r)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = r * k
            pygame.draw.circle(s, color, (c, c), c)
            body = [(c - 9 * k, c - 4 * k), (c - 4 * k, c - 4 * k), (c + 3 * k, c - 10 * k), (c + 3 * k, c + 10 * k),
                    (c - 4 * k, c + 4 * k), (c - 9 * k, c + 4 * k)]
            pygame.draw.polygon(s, art.WHITE, body)
            pygame.draw.arc(s, art.WHITE, (c - 1 * k, c - 8 * k, 14 * k, 16 * k), -1.0, 1.0, int(3 * k))

        return art.supersampled((2 * r, 2 * r), draw)

    return art.cached(key, make)


class DefinitionCard(Card):
    kind = "definition"

    def __init__(self, spec: dict[str, Any], host: Any = None) -> None:
        super().__init__(spec, host)
        mode = str(spec.get("mode") or "meaning").strip().lower()
        self.mode = mode if mode in MODES else "meaning"
        self.word = kit.clean(spec.get("word"), 40)
        self.say_it = kit.clean(spec.get("say_it"), 48)
        self.part = kit.clean(spec.get("part") or spec.get("part_of_speech"), 20).lower()
        self.meanings = kit.str_list(spec.get("meanings") or spec.get("meaning"), max_items=2, limit=110)
        self.example = kit.clean(spec.get("example"), 120)
        self.translation = kit.clean(spec.get("translation"), 40)
        self.language = kit.clean(spec.get("language"), 24)
        self.title = self.word
        self.mood = "happy" if self.mode == "spell" else "curious"
        self.letters = [c for c in self.word if not c.isspace()][:MAX_LETTERS]
        self.spell_t0 = 0.0
        query = kit.clean(spec.get("picture_query"), 80) if self.mode != "spell" else ""
        self.slot = kit.PictureSlot(self.host, query)
        self.page = 0
        self.page_t = 0.0
        self.manual_t = -99.0
        self._layouts: dict[bool, list[list[tuple[str, tuple[str, ...]]]]] = {}
        self._popped = 0

    # ------------------------------------------------------------ layout
    def _text_width(self, pic: bool) -> int:
        return kit.PANEL.w - 2 * kit.PAD - 44 - (PIC_W + 18 if pic else 0)

    def pages(self) -> list[list[tuple[str, tuple[str, ...]]]]:
        """Pages of body blocks: ("meaning", lines) / ("example", lines), at most MAX_LINES lines a page."""
        pic = self.slot.wanted
        got = self._layouts.get(pic)
        if got is None:
            width = self._text_width(pic)
            budget = MAX_LINES - (1 if self.mode == "translate" else 0)
            blocks: list[tuple[str, tuple[str, ...]]] = []
            for m in self.meanings if self.mode == "meaning" else []:
                blocks.append(("meaning", kit.wrap(m, MEANING_PX, width)[:budget]))
            if self.example and self.mode != "spell":
                blocks.append(("example", kit.wrap(f"“{self.example}”", EXAMPLE_PX, width - 20)[:budget]))
            got = [[blocks[i] for i in page] for page in kit.paged([len(b[1]) for b in blocks], budget)] or [[]]
            self._layouts[pic] = got
        return got

    # ------------------------------------------------------------ logic
    def update(self, dt: float) -> None:
        super().update(dt)
        self.slot.update(self.t)
        pages = self.pages()
        self.page = min(self.page, len(pages) - 1)
        if (len(pages) > 1 and self.page < len(pages) - 1 and self.t - self.page_t >= PAGE_S
                and self.t - self.manual_t >= PAGE_S):
            self._turn(self.page + 1)
        if self.mode == "spell":
            n = self.letters_shown()
            if n > self._popped:
                self._popped = n
                if n == len(self.letters):
                    self.host.sound("sparkle")

    def letters_shown(self) -> int:
        if self.mode != "spell":
            return 0
        return max(0, min(len(self.letters), int((self.t - self.spell_t0 - LETTER_FIRST) / LETTER_S) + 1))

    def _turn(self, page: int) -> None:
        page = max(0, min(len(self.pages()) - 1, page))
        if page != self.page:
            self.page, self.page_t = page, self.t

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action in ("next", "prev"):
            self.manual_t = self.t
            if self.mode == "spell" and action == "next" and self.letters_shown() < len(self.letters):
                self.spell_t0 = self.t - LETTER_FIRST - LETTER_S * len(self.letters)  # show them all
                return True
            self._turn(self.page + (1 if action == "next" else -1))
            return True
        if action == "again":
            if self.mode == "spell":
                self.spell_t0, self._popped = self.t, 0
            else:
                self._turn(0)
                self.manual_t = self.t
            return True
        return False

    def tap(self, x: float, y: float) -> bool:
        if self.mode == "spell":
            return self.command("again", {})
        return self.command("prev" if x < art.W * 0.33 else "next", {})

    def key(self, name: str) -> bool:
        if name in ("right", "space", "return"):
            return self.command("next", {})
        if name in ("left", "backspace"):
            return self.command("prev", {})
        return False

    def describe(self) -> str:
        if self.mode == "spell":
            n = self.letters_shown()
            shown = "-".join(c.upper() for c in self.letters[:n])
            if n < len(self.letters):
                return f"Spelling {self.word}: {shown or 'starting'} so far"
            return f"Spelling {self.word}: {shown}"
        say = f" (say {self.say_it})" if self.say_it else ""
        if self.mode == "translate":
            return f"{self.word} in {self.language or 'another language'}: {self.translation}{say}"
        blocks = self.pages()[self.page]
        what = "; ".join(" ".join(lines) for kind, lines in blocks if kind == "meaning") or (
            "the example" if blocks else "")
        part = f", {self.part}" if self.part else ""
        return f"{self.word}{part}{say}: {what}".rstrip(": ")

    def status(self) -> dict[str, Any]:
        return {**super().status(), "mode": self.mode, "page": self.page + 1, "pages": len(self.pages())}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=31)
        if self.mode == "spell":
            self._draw_spell(surf)
        elif self.mode == "translate":
            self._draw_translate(surf)
        else:
            self._draw_meaning(surf)

    def _draw_header_word(self, surf: pygame.Surface, word: str, *, chip: str = "") -> None:
        room = kit.HEADER.w - (kit.chip(chip, art.GRAPE).get_width() + 14 if chip else 0)
        img = kit.outlined_fit(word, WORD_SIZES, room)
        k = art.ease_out_back(kit.appear(self.t, 0.0, 0.5))
        r = img.get_rect(midleft=(kit.HEADER.x, kit.HEADER.centery + 2))
        art.blit_center(surf, art.scaled(img, max(0.05, k)), r.center)
        if chip:
            c = kit.chip(chip, art.GRAPE)
            surf.blit(c, c.get_rect(midleft=(r.right + 14, r.centery + 4)))

    def _draw_say_row(self, surf: pygame.Surface, x: int, top: int, say_it: str, accent: art.Color,
                      max_w: int) -> pygame.Rect:
        """The speaker badge and the respelling from ``top``; returns the respelling's rect."""
        icon = speaker_icon(art.inkier(accent, 0.15))
        say = fit_say_it(say_it, max_w - icon.get_width() - 12, accent)
        first = min(say.get_height(), art.font(SAY_PX).get_height())
        surf.blit(icon, icon.get_rect(center=(x + icon.get_width() // 2, top + first // 2)))
        r = say.get_rect(topleft=(x + icon.get_width() + 12, top + 2))
        surf.blit(say, r)
        return r

    def _body_heights(self) -> list[int]:
        out = []
        for kind, lines in self.pages()[self.page]:
            size = MEANING_PX if kind == "meaning" else EXAMPLE_PX
            out.append(kit.line_h(size) * (len(lines) - 1) + art.font(size).get_height() + (18 if kind == "example"
                                                                                            else 0))
        return out

    def _draw_body(self, surf: pygame.Surface, x: int, top: int, bottom: int, width: int) -> None:
        """The meanings (numbered when two) and the example (in a soft band) of this page, from ``top``,
        moved down a little when there is room to spare."""
        pages = self.pages()
        blocks = pages[self.page]
        heights = self._body_heights()
        gap = 24
        total = sum(heights) + gap * max(0, len(heights) - 1)
        y = top + min(30, max(0, (bottom - top - total) // 2))
        fade = art.ease_out_cubic(kit.appear(self.t, self.page_t + 0.15))
        number = sum(1 for page in pages[:self.page] for kind, _ in page if kind == "meaning")
        first_h = art.font(MEANING_PX).get_height()
        for (kind, lines), h in zip(blocks, heights):
            if kind == "meaning":
                number += 1
                if len(self.meanings) > 1:
                    badge = kit.number_badge(number, kit.ACCENTS[number - 1], 17)
                    kit.blit_alpha(surf, badge, badge.get_rect(center=(x + 17, y + first_h // 2 + 1)).topleft, fade)
                else:
                    art.blit_center(surf, art.circle(8, art.STRAWBERRY), (x + 17, y + first_h // 2))
                kit.blit_alpha(surf, kit.text_block(lines, MEANING_PX), (x + 46, y), fade)
            else:
                band = pygame.Rect(x, y, width + 46, h)
                kit.blit_alpha(surf, art.rounded_rect(band.w, band.h, 16, art.mix(art.LEMON, art.WHITE, 0.72)),
                               band.topleft, fade)
                kit.blit_alpha(surf, kit.text_block(lines, EXAMPLE_PX), (x + 22, y + 10), fade)
            y += h + gap
        kit.page_dots(surf, len(pages), self.page, (kit.PANEL.centerx, kit.PANEL.bottom - 16))

    def _draw_meaning(self, surf: pygame.Surface) -> None:
        self._draw_header_word(surf, self.word)
        kit.panel(surf)
        pic = self.slot.wanted
        x = kit.PANEL.x + kit.PAD
        top = kit.PANEL.y + kit.PAD
        if self.say_it or self.part:  # the say-it row spans the panel; the picture goes under it
            width = kit.PANEL.w - 2 * kit.PAD
            bottom, right, mid = top + 44, x, top + 22
            if self.say_it:
                r = self._draw_say_row(surf, x, top + 4, self.say_it, art.STRAWBERRY, width)
                bottom, right = r.bottom, r.right + 20
                mid = r.bottom - art.font(SAY_PX if r.h < 50 else BODY_PX).get_height() // 2 - 2
            if self.part:
                c = kit.chip(self.part, art.GRAPE, size=BODY_PX - 4)
                if right + c.get_width() > kit.PANEL.right - kit.PAD:  # no room on the row: under it
                    right, mid = x, bottom + 8 + c.get_height() // 2
                    bottom = bottom + 8 + c.get_height()
                surf.blit(c, c.get_rect(midleft=(right, mid)))
            top = bottom + 18
        self._draw_body(surf, x, top, kit.PANEL.bottom - kit.PAD - 14, self._text_width(pic))
        if pic:
            rect = pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, top, PIC_W, kit.PANEL.bottom - kit.PAD - top)
            self.slot.draw(surf, rect, self.t)

    def _draw_translate(self, surf: pygame.Surface) -> None:
        kit.header_title(surf, f"{self.word} in {self.language}" if self.language else self.word)
        kit.panel(surf)
        pic = self.slot.wanted
        left = kit.PANEL.x + kit.PAD
        right = kit.PANEL.right - kit.PAD - (PIC_W + 18 if pic else 0)
        has_body = bool(self.pages()[0])
        cx = (left + right) // 2
        fill = art.inkier(art.SKY, 0.1)
        big = kit.outlined_fit(self.translation or self.word, BIG_SIZES, right - left, fill, art.INK)
        k = art.ease_out_back(kit.appear(self.t, 0.15, 0.55))
        say_h = 50 if self.say_it else 0
        body_h = (sum(self._body_heights()) + 24) if has_body else 0
        block_h = big.get_height() + say_h + body_h
        big_top = kit.PANEL.y + max(kit.PAD, (kit.PANEL.h - block_h) // 2 - 6)
        art.blit_center(surf, art.scaled(big, max(0.05, k)), (cx, big_top + big.get_height() // 2))
        y = big_top + big.get_height() + 22
        if self.say_it:
            icon_w = speaker_icon(art.SKY).get_width() + 12
            w = fit_say_it(self.say_it, right - left - icon_w, art.SKY).get_width() + icon_w
            r = self._draw_say_row(surf, int(cx - w / 2), y - 18, self.say_it, art.SKY, right - left)
            y = r.bottom + 10
        if has_body:
            self._draw_body(surf, left, y, y + body_h, self._text_width(pic))
        if pic:
            rect = pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, kit.PANEL.y + kit.PAD, PIC_W,
                               kit.PANEL.h - 2 * kit.PAD)
            self.slot.draw(surf, rect, self.t)

    def tile_layout(self) -> tuple[int, list[str]]:
        """(tile size, rows of letters): the biggest tiles whose rows leave room for the word under them."""
        words = self.word.split()
        rows: list[str] = []
        for tile in TILE_SIZES:
            rows = self._tile_rows(words, tile)
            if len(rows) * (tile + 18) + 100 <= kit.PANEL.h - 2 * kit.PAD:
                return tile, rows
        return TILE_SIZES[-1], rows

    def _draw_spell(self, surf: pygame.Surface) -> None:
        kit.header_title(surf, f"How to spell {self.word}" if len(self.word) <= 14 else "How to spell it")
        kit.panel(surf)
        n = self.letters_shown()
        tile, rows = self.tile_layout()
        row_h = tile + 16
        total_h = row_h * len(rows)
        top = kit.PANEL.y + max(kit.PAD, (kit.PANEL.h - total_h - 100) // 2)
        i = 0
        for r, row in enumerate(rows):
            width = sum(tile + TILE_GAP if ch != " " else tile // 2 for ch in row) - TILE_GAP
            x = kit.PANEL.centerx - width // 2
            y = top + r * row_h
            for ch in row:
                if ch == " ":
                    x += tile // 2
                    continue
                if i < n:
                    start = self.spell_t0 + LETTER_FIRST + i * LETTER_S
                    k = art.ease_out_back(kit.appear(self.t, start, 0.35))
                    spr = self._tile(ch.upper(), kit.ACCENTS[i % len(kit.ACCENTS)], tile)
                    art.blit_center(surf, art.scaled(spr, max(0.05, k)), (x + tile // 2, y + tile // 2))
                else:
                    surf.blit(art.rounded_rect(tile, tile, 16, art.mix(art.BG_TOP, art.WHITE, 0.3),
                                               border=art.mix(art.BG_BOTTOM, art.INK_SOFT, 0.2), border_w=2), (x, y))
                x += tile + TILE_GAP
                i += 1
        if n >= len(self.letters):
            k = art.ease_out_cubic(kit.appear(self.t, self.spell_t0 + LETTER_FIRST + len(self.letters) * LETTER_S))
            y = top + total_h + 30
            room = kit.PANEL.w - 2 * kit.PAD
            size, lines = kit.fit(self.word, (56, 48, 40, BODY_PX), room, 1)
            word = kit.text_block([kit.ellipsize(lines[0], size, room)] if lines else [], size, art.INK)
            kit.blit_alpha(surf, word, word.get_rect(center=(kit.PANEL.centerx, y)).topleft, k)
            if self.say_it:
                say = fit_say_it(self.say_it, room)
                kit.blit_alpha(surf, say, say.get_rect(midtop=(kit.PANEL.centerx, y + 26)).topleft, k)

    def _tile_rows(self, words: list[str], tile: int = TILE) -> list[str]:
        per_row = (kit.PANEL.w - 2 * kit.PAD + TILE_GAP) // (tile + TILE_GAP)
        rows: list[str] = []
        cur = ""
        budget = MAX_LETTERS
        for w in words:
            w = w[:budget]
            budget -= len(w)
            while len(w) > per_row:
                if cur:
                    rows.append(cur)
                    cur = ""
                rows.append(w[:per_row])
                w = w[per_row:]
            trial = f"{cur} {w}" if cur else w
            if len(trial.replace(" ", "")) + 0.5 * trial.count(" ") <= per_row:
                cur = trial
            elif w:
                rows.append(cur)
                cur = w
            if budget <= 0:
                break
        if cur:
            rows.append(cur)
        return rows or [""]

    @staticmethod
    def _tile(ch: str, color: art.Color, tile: int = TILE) -> pygame.Surface:
        key = ("def_tile", ch, color, tile)

        def make() -> pygame.Surface:
            out = pygame.Surface((tile, tile + 5), pygame.SRCALPHA)
            out.blit(art.rounded_rect(tile, tile, 16, art.inkier(color, 0.4)), (0, 5))
            out.blit(art.rounded_rect(tile, tile, 16, color, shine=True), (0, 0))
            t = art.text_outlined(ch, int(tile * 0.93), art.WHITE, art.inkier(color, 0.55), 3)
            out.blit(t, t.get_rect(center=(tile // 2, tile // 2 + 2)))
            return out

        return art.cached(key, make)


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    card = DefinitionCard(spec, host)
    if not card.word:
        return None
    if card.mode == "translate" and not card.translation:
        return None
    if card.mode == "meaning" and not (card.meanings or card.say_it):
        return None
    return card


register(CardType(
    kind="definition",
    description=(
        "A word on screen. mode \"meaning\" (default) for \"what does X mean\": the word big, how to say it, the "
        "part of speech, 1-2 short meanings and one example sentence, plus a picture for a concrete thing. "
        "mode \"spell\" for \"how do you spell X\": the letters pop up one by one while you spell it out loud. "
        "mode \"translate\" for \"how do you say X in Spanish\": the translation big with how to say it."
    ),
    fields={
        "mode": {"type": "string", "enum": list(MODES), "description": "meaning (default) | spell | translate"},
        "word": {"type": "string", "description": "The word asked about, as written (\"encyclopedia\"); for "
                                                  "translate, the word in English (\"apple\")"},
        "say_it": {"type": "string",
                   "description": "How to say it as a simple respelling with hyphens between syllables and the "
                                  "stressed syllable in CAPITALS (\"en-SY-kluh-PEE-dee-uh\", \"mahn-SAH-nah\"). "
                                  "Never IPA symbols. For translate: how to say the translation"},
        "part": {"type": "string", "description": "Part of speech: noun, verb, adjective, adverb, ... (meaning)"},
        "meanings": {"type": "array", "items": {"type": "string"}, "maxItems": 2,
                     "description": "1-2 short, plain meanings, at most 12 words each (meaning)"},
        "example": {"type": "string", "description": "One short example sentence using the word, at most 12 words "
                                                     "(for translate: in that language)"},
        "translation": {"type": "string", "description": "The word in the other language (translate)"},
        "language": {"type": "string", "description": "The other language's name, \"Spanish\" (translate)"},
        "picture_query": {"type": "string",
                          "description": "Optional, for a concrete thing only: a Wikipedia-style subject for a "
                                         "picture (\"giraffe\", \"accordion\"). Leave out for abstract words"},
    },
    required=("word",),
    actions={"next": "the next page of meanings (spell: show all letters now)", "prev": "the page before",
             "again": "spell: pop the letters in again from the start"},
    build=_build,
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "definition", "word": "encyclopedia", "say_it": "en-SY-kluh-PEE-dee-uh", "part": "noun",
     "meanings": ["A book or set of books full of facts about many things"],
     "example": "We looked up volcanoes in the encyclopedia.", "picture_query": "encyclopedia"},
    {"card": "definition", "word": "resilient", "say_it": "rih-ZIL-yuhnt", "part": "adjective",
     "meanings": ["Able to recover quickly after something hard", "Springing back into shape after bending"],
     "example": "Babies are resilient when they learn to walk."},
    {"card": "definition", "word": "giraffe", "say_it": "juh-RAF", "part": "noun",
     "meanings": ["A very tall African animal with a long neck and spots"],
     "example": "The giraffe ate leaves from the top of the tree.", "picture_query": "giraffe"},
    {"card": "definition", "mode": "spell", "word": "necessary", "say_it": "NESS-uh-sair-ee"},
    {"card": "definition", "mode": "translate", "word": "apple", "translation": "manzana", "language": "Spanish",
     "say_it": "mahn-SAH-nah", "example": "Me gusta la manzana roja.", "picture_query": "apple"},
    {"card": "definition", "mode": "translate", "word": "thank you", "translation": "merci", "language": "French",
     "say_it": "mair-SEE"},
]

__all__ = ["MODES", "SAMPLES", "DefinitionCard"]
