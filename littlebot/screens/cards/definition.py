"""The "definition" card: a word, how to say it, and what it means; also spelling and translations.

Three modes (the ``mode`` field):

* ``meaning`` (default), "what does encyclopedia mean?": the word big, how to say it as a simple
  respelling with the stressed part in capitals ("en-SY-kluh-PEE-dee-uh", never IPA; the capitals are
  shown in colour), the part of speech, one to three short meanings, one or two example sentences, words
  that mean the same (``synonyms``) and the opposite (``opposites``) on candy chips, where the word comes
  from (``origin``), and for a concrete noun optionally a picture (``picture_query``).
* ``spell``, "how do you spell necessary?": the letters big on candy tiles, popping in one at a time
  at about the pace of spelling them out loud; then the whole word, how to say it and a spelling ``tip``
  ("One collar, two sleeves: 1 c, 2 s") under them, and how many letters it has. "again" replays it.
* ``translate``, "how do you say apple in Spanish?": the translation big, how to say it, the language
  and the word it translates, and optionally an example in that language with what it means
  (``example_meaning``), other ways to say it (``synonyms``), a ``note`` ("It's feminine: la manzana") and
  a picture.

Body text is at most MAX_LINES lines a page and never more than the panel holds; anything longer is
paged ("next", or a tap, or on its own after a while).
"""

from __future__ import annotations

import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens import base
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
WORD_SIZES = (88, 80, 72, 64, 56, TITLE_PX)
BIG_SIZES = (132, 116, 100, 88, 76, 64, TITLE_PX)  # the translated word (it has the panel to itself)
SAY_PX = 44
PART_PX = 32  # the part-of-speech chip (a label)
MEANING_PX = 40
EXAMPLE_PX = BODY_PX
SPELL_WORD_PX = 60  # the whole word under the letter tiles
PIC_W = kit.PIC_W
INDENT = 46  # body text right of a meaning's bullet / number
TAG_GAP = 14  # between a block's label chip ("Same as", "Origin", "Tip") and its text
CHIP_H = 44  # a row of word chips
CHIP_GAP = 10
BLOCK_GAP = 20
BAND_PAD = 22  # inside an example's band
BAND_Y = 10
DOTS_H = 30  # room kept for the page dots
PAGE_S = 9.0  # a paged definition turns its page on its own after this long
LETTER_S = 0.5  # spelling: one letter every half second (about the pace of saying them)
LETTER_FIRST = 0.6
MAX_LETTERS = 36
MAX_MEANINGS = 3
MAX_EXAMPLES = 2
MAX_WORDS = 4  # synonyms (meaning) / other ways to say it (translate)
MAX_OPPOSITES = 3
TILE_SIZES = (88, 78, 68, 60, 52, 46)  # letter tiles: as big as the word allows
TILE_GAP = 12
TILE_ROW_GAP = 16
TILE = TILE_SIZES[0]
WORD_COLORS = {"Same as": art.MINT, "Also": art.SKY, "Opposite": art.STRAWBERRY}


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
            img = f.render(token, True, art.ink(color))
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
    for size in (SAY_PX, 40, BODY_PX):
        img = say_it_sprite(say_it, size, accent)
        if img.get_width() <= max_w:
            return img
    return say_it_sprite(say_it, BODY_PX, accent, max_w)


def speaker_icon(color: art.Color, r: int = 21) -> pygame.Surface:
    """A little round "say it" speaker badge."""
    key = ("def_speaker", color, r)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = r * k
            pygame.draw.circle(s, color, (c, c), c)
            z = r / 18 * k  # drawn for r = 18
            body = [(c - 9 * z, c - 4 * z), (c - 4 * z, c - 4 * z), (c + 3 * z, c - 10 * z), (c + 3 * z, c + 10 * z),
                    (c - 4 * z, c + 4 * z), (c - 9 * z, c + 4 * z)]
            pygame.draw.polygon(s, art.WHITE, body)
            pygame.draw.arc(s, art.WHITE, (c - 1 * z, c - 8 * z, 14 * z, 16 * z), -1.0, 1.0, int(3 * z))

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
        self.meanings = kit.str_list(spec.get("meanings") or spec.get("meaning"), max_items=MAX_MEANINGS, limit=110)
        examples = kit.str_list(spec.get("example"), max_items=1, limit=120)
        examples += kit.str_list(spec.get("examples"), max_items=MAX_EXAMPLES, limit=120)
        self.examples = list(dict.fromkeys(examples))[:MAX_EXAMPLES]
        self.example = self.examples[0] if self.examples else ""
        self.example_meaning = kit.clean(spec.get("example_meaning"), 120)
        self.synonyms = kit.str_list(spec.get("synonyms"), max_items=MAX_WORDS, limit=24)
        self.opposites = kit.str_list(spec.get("opposites") or spec.get("antonyms"), max_items=MAX_OPPOSITES, limit=24)
        self.origin = kit.clean(spec.get("origin"), 100)
        self.note = kit.clean(spec.get("note") or spec.get("tip"), 100)
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
        self._layouts: dict[bool, list[list[Block]]] = {}
        self._popped = 0

    # ------------------------------------------------------------ layout
    def _text_width(self, pic: bool) -> int:
        return kit.PANEL.w - 2 * kit.PAD - INDENT - (PIC_W + kit.GUTTER if pic else 0)

    def _body_room(self, pic: bool) -> int:
        """The height the body blocks may use: the panel under the say-it row (meaning) or under the big word
        and its respelling (translate)."""
        inner = kit.PANEL.h - 2 * kit.PAD
        if self.mode == "translate":
            return inner - self._translate_head_h(pic) - 16
        return inner - (self._say_row_h() + 18 if self.say_it or self.part else 0)

    def _say_row_h(self) -> int:
        return max(art.font(SAY_PX).get_height(), kit.chip(self.part or "x", art.GRAPE, size=PART_PX).get_height()) + 4

    def _translate_head_h(self, pic: bool) -> int:
        width = kit.PANEL.w - 2 * kit.PAD - (PIC_W + kit.GUTTER if pic else 0)
        big = kit.outlined_fit(self.translation or self.word, BIG_SIZES, width)
        return big.get_height() + (art.font(SAY_PX).get_height() + 14 if self.say_it else 0)

    def pages(self) -> list[list[Block]]:
        """Pages of body blocks, each (kind, lines, height): "meaning", "example" (its English meaning as a second
        part, translate), "words" (rows of synonym / opposite chips), "origin" and "note"; at most MAX_LINES lines
        and the body's height a page."""
        pic = self.slot.wanted
        got = self._layouts.get(pic)
        if got is None:

            blocks = self._blocks(self._text_width(pic))
            budget = MAX_LINES - (1 if self.mode == "translate" else 0)
            pages = kit.pages_by_height([b[2] for b in blocks], [_lines_of(b) for b in blocks], gap=BLOCK_GAP,
                                            room=self._body_room(pic), max_lines=budget)
            if len(pages) > 1:
                pages = kit.pages_by_height([b[2] for b in blocks], [_lines_of(b) for b in blocks], gap=BLOCK_GAP,
                                                room=self._body_room(pic) - DOTS_H, max_lines=budget)
            got = [[blocks[i] for i in page] for page in pages] or [[]]
            self._layouts[pic] = got
        return got

    def _blocks(self, width: int) -> list[Block]:
        if self.mode == "spell":
            return []
        out: list[Block] = []
        if self.mode == "meaning":
            for m in self.meanings:
                lines = kit.wrap(m, MEANING_PX, width)[:MAX_LINES]
                out.append(("meaning", lines, _text_h(lines, MEANING_PX)))
        for n, ex in enumerate(self.examples if self.mode == "meaning" else self.examples[:1]):
            lines = kit.wrap(f"“{ex}”", EXAMPLE_PX, width - 2 * BAND_PAD)[:MAX_LINES]
            extra: tuple[str, ...] = ()
            if self.mode == "translate" and self.example_meaning and n == 0:
                extra = kit.wrap(self.example_meaning, BODY_PX, width - 2 * BAND_PAD)[:2]
            h = _text_h(lines, EXAMPLE_PX) + 2 * BAND_Y + (6 + _text_h(extra, BODY_PX) if extra else 0)
            out.append(("example", (lines, extra), h))
        rows = self.word_rows(width + INDENT)
        if rows:
            out.append(("words", tuple(rows), len(rows) * (CHIP_H + CHIP_GAP) - CHIP_GAP))
        for kind, text in (("origin", self.origin if self.mode == "meaning" else ""), ("note", self.note)):
            if text:
                lines = kit.wrap(text, BODY_PX, width + INDENT - _tag_w(self._label(kind)))[:2]
                out.append((kind, lines, _text_h(lines, BODY_PX)))
        return out

    def _label(self, kind: str) -> str:
        return "Origin" if kind == "origin" else ("Tip" if self.mode != "meaning" else "Note")

    def _groups(self) -> list[tuple[str, list[str]]]:
        groups = [("Same as" if self.mode == "meaning" else "Also", self.synonyms),
                  ("Opposite", self.opposites if self.mode == "meaning" else [])]
        return [(label, words) for label, words in groups if words]

    def words_x(self) -> int:
        """Where the chips of every word row start (after the widest label: the rows line up)."""
        return max((_tag_w(label) for label, _ in self._groups()), default=0)

    def word_rows(self, width: int) -> list[tuple[str, tuple[str, ...]]]:
        """Rows of chips: ("Same as", ("huge", "big")), ("Opposite", ("tiny",)); a group too wide for one row goes
        on, without its label, on the next."""
        rows: list[tuple[str, tuple[str, ...]]] = []
        for label, words in self._groups():
            room = width - self.words_x()
            cur: list[str] = []
            used = 0
            for w in words:
                cw = _chip(w, art.MINT).get_width() + CHIP_GAP
                if cur and used + cw > room:
                    rows.append((label if not any(r[0] == label for r in rows) else "", tuple(cur)))
                    cur, used = [], 0
                cur.append(kit.ellipsize(w, BODY_PX, room - 40))
                used += cw
            if cur:
                rows.append((label if not any(r[0] == label for r in rows) else "", tuple(cur)))
        return rows

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
            tip = f" (tip: {self.note})" if self.note else ""
            return f"Spelling {self.word}: {shown}{tip}"
        say = f" (say {self.say_it})" if self.say_it else ""
        if self.mode == "translate":
            also = f"; also {', '.join(self.synonyms)}" if self.synonyms else ""
            return f"{self.word} in {self.language or 'another language'}: {self.translation}{say}{also}"
        blocks = self.pages()[self.page]
        what = "; ".join(" ".join(lines) for kind, lines, _ in blocks if kind == "meaning") or (
            "the example" if blocks else "")
        if any(kind == "words" for kind, _, _ in blocks):
            words = [f"same as {', '.join(self.synonyms)}" if self.synonyms else "",
                     f"opposite {', '.join(self.opposites)}" if self.opposites else ""]
            what += "".join(f"; {w}" for w in words if w)
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

    def _draw_header_word(self, surf: pygame.Surface, word: str) -> None:
        img = kit.outlined_fit(word, WORD_SIZES, kit.HEADER.w)
        k = art.ease_out_back(kit.appear(self.t, 0.0, 0.5))
        r = img.get_rect(midleft=(kit.HEADER.x, kit.HEADER.centery + 2))
        art.blit_center(surf, art.scaled(img, max(0.05, k)), r.center)

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

    def _draw_body(self, surf: pygame.Surface, x: int, top: int, bottom: int, width: int) -> None:
        """This page's blocks from ``top``: the meanings (numbered when more than one), the examples (in a soft
        band), the synonym / opposite chips, the origin and the note; moved down a little when there is room."""
        pages = self.pages()
        blocks = pages[self.page]
        total = sum(h for _, _, h in blocks) + BLOCK_GAP * max(0, len(blocks) - 1)
        y = top + min(30, max(0, (bottom - top - total) // 2))
        fade = art.ease_out_cubic(kit.appear(self.t, self.page_t + 0.15))
        number = sum(1 for page in pages[:self.page] for kind, _, _ in page if kind == "meaning")
        first_h = art.font(MEANING_PX).get_height()
        for kind, payload, h in blocks:
            if kind == "meaning":
                number += 1
                if len(self.meanings) > 1:
                    badge = kit.number_badge(number, kit.ACCENTS[number - 1], 20)
                    kit.blit_alpha(surf, badge, badge.get_rect(center=(x + 20, y + first_h // 2 + 1)).topleft, fade)
                else:
                    art.blit_center(surf, art.circle(9, art.STRAWBERRY), (x + 20, y + first_h // 2))
                kit.blit_alpha(surf, kit.text_block(payload, MEANING_PX), (x + INDENT, y), fade)
            elif kind == "example":
                lines, extra = payload
                band = pygame.Rect(x, y, width + INDENT, h)
                kit.blit_alpha(surf, art.rounded_rect(band.w, band.h, 18, art.mix(art.LEMON, art.WHITE, 0.72)),
                               band.topleft, fade)
                text = kit.text_block(lines, EXAMPLE_PX)
                kit.blit_alpha(surf, text, (x + BAND_PAD, y + BAND_Y), fade)
                if extra:
                    meaning = kit.text_block(extra, BODY_PX, kit.TEXT_SOFT)
                    kit.blit_alpha(surf, meaning, (x + BAND_PAD, y + BAND_Y + text.get_height() + 6), fade)
            elif kind == "words":
                self._draw_word_rows(surf, x, y, payload, fade)
            else:
                label = self._label(kind)
                tag = kit.chip(label, art.PERIWINKLE if kind == "origin" else art.TANGERINE)
                kit.blit_alpha(surf, tag, tag.get_rect(midleft=(x, y + art.font(BODY_PX).get_height() // 2)).topleft,
                               fade)
                kit.blit_alpha(surf, kit.text_block(payload, BODY_PX, kit.TEXT_SOFT), (x + _tag_w(label), y), fade)
            y += h + BLOCK_GAP
        kit.page_dots(surf, len(pages), self.page, (kit.PANEL.centerx, kit.PANEL.bottom - 20))

    def _draw_word_rows(self, surf: pygame.Surface, x: int, y: int, rows: tuple[Any, ...], fade: float) -> None:
        group = ""
        for label, words in rows:
            group = label or group  # a row without a label goes on with the group above it
            if label:
                tag = kit.chip(label, art.GRAPE)
                kit.blit_alpha(surf, tag, tag.get_rect(midleft=(x, y + CHIP_H // 2)).topleft, fade)
            cx = x + self.words_x()
            for w in words:
                chip = _chip(w, WORD_COLORS.get(group, art.MINT))
                kit.blit_alpha(surf, chip, (cx, y + (CHIP_H - chip.get_height()) // 2), fade)
                cx += chip.get_width() + CHIP_GAP
            y += CHIP_H + CHIP_GAP

    def _draw_meaning(self, surf: pygame.Surface) -> None:
        self._draw_header_word(surf, self.word)
        kit.panel(surf)
        pic = self.slot.wanted
        x = kit.PANEL.x + kit.PAD
        top = kit.PANEL.y + kit.PAD
        if self.say_it or self.part:  # the say-it row spans the panel; the picture goes under it
            width = kit.PANEL.w - 2 * kit.PAD
            right, mid = x, top + self._say_row_h() // 2
            if self.say_it:
                r = self._draw_say_row(surf, x, top + 2, self.say_it, art.STRAWBERRY, width)
                right = r.right + 20
                mid = r.y + art.font(SAY_PX).get_height() // 2
            if self.part:
                c = kit.chip(self.part, art.GRAPE, size=PART_PX)
                if right + c.get_width() <= kit.PANEL.right - kit.PAD:
                    surf.blit(c, c.get_rect(midleft=(right, mid)))
            top += self._say_row_h() + 18
        self._draw_body(surf, x, top, kit.PANEL.bottom - kit.PAD, self._text_width(pic))
        if pic:
            rect = pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, top, PIC_W, kit.PANEL.bottom - kit.PAD - top)
            self.slot.draw(surf, rect, self.t)

    def _draw_translate(self, surf: pygame.Surface) -> None:
        kit.header_title(surf, f"{self.word} in {self.language}" if self.language else self.word)
        kit.panel(surf)
        pic = self.slot.wanted
        left = kit.PANEL.x + kit.PAD
        right = kit.PANEL.right - kit.PAD - (PIC_W + kit.GUTTER if pic else 0)
        blocks = self.pages()[self.page]
        cx = (left + right) // 2
        fill = art.inkier(art.SKY, 0.1)
        big = kit.outlined_fit(self.translation or self.word, BIG_SIZES, right - left, fill, art.INK)
        k = art.ease_out_back(kit.appear(self.t, 0.15, 0.55))
        say_h = art.font(SAY_PX).get_height() + 14 if self.say_it else 0
        body_h = (sum(h for _, _, h in blocks) + BLOCK_GAP * (len(blocks) - 1) + 16) if blocks else 0
        block_h = big.get_height() + say_h + body_h
        room = kit.PANEL.h - 2 * kit.PAD - (DOTS_H if len(self.pages()) > 1 else 0)
        big_top = kit.PANEL.y + kit.PAD + max(0, (room - block_h) // 2)
        art.blit_center(surf, art.scaled(big, max(0.05, k)), (cx, big_top + big.get_height() // 2))
        y = big_top + big.get_height() + 4
        if self.say_it:
            icon_w = speaker_icon(art.SKY).get_width() + 12
            w = fit_say_it(self.say_it, right - left - icon_w, art.SKY).get_width() + icon_w
            r = self._draw_say_row(surf, int(cx - w / 2), y, self.say_it, art.SKY, right - left)
            y = r.bottom + 12
        if blocks:
            self._draw_body(surf, left, y + 4, y + 4 + body_h, self._text_width(pic))
        if pic:
            rect = pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, kit.PANEL.y + kit.PAD, PIC_W,
                               kit.PANEL.h - 2 * kit.PAD)
            self.slot.draw(surf, rect, self.t)

    def _tip_lines(self) -> tuple[str, ...]:
        return kit.wrap(self.note, BODY_PX, kit.PANEL.w - 2 * kit.PAD - _tag_w("Tip") - 2 * BAND_PAD)[:2] \
            if self.note else ()

    def _under_tiles_h(self) -> int:
        """Room the word, its respelling and the tip need under the tiles."""
        h = art.font(SPELL_WORD_PX).get_height() + 16
        if self.say_it:
            h += art.font(SAY_PX).get_height() + 8
        tip = self._tip_lines()
        if tip:
            h += _text_h(tip, BODY_PX) + 2 * BAND_Y + 14
        return h

    def tile_layout(self) -> tuple[int, list[str]]:
        """(tile size, rows of letters): the biggest tiles whose rows leave room for the word under them."""
        words = self.word.split()
        rows: list[str] = []
        room = kit.PANEL.h - 2 * kit.PAD - self._under_tiles_h()
        for tile in TILE_SIZES:
            rows = self._tile_rows(words, tile)
            if len(rows) * (tile + TILE_ROW_GAP) <= room:
                return tile, rows
        return TILE_SIZES[-1], rows

    def _draw_spell(self, surf: pygame.Surface) -> None:
        count = kit.chip(f"{len(self.letters)} letters", art.GRAPE)
        title = f"How to spell {self.word}" if len(self.word) <= 20 else "How to spell it"
        room = pygame.Rect(kit.HEADER.x, kit.HEADER.y, kit.HEADER.w - count.get_width() - 16, kit.HEADER.h)
        r = kit.header_title(surf, title, rect=room)
        surf.blit(count, count.get_rect(midleft=(r.right + 16, kit.HEADER.centery + 2)))  # how many letters
        kit.panel(surf)
        n = self.letters_shown()
        tile, rows = self.tile_layout()
        row_h = tile + TILE_ROW_GAP
        total_h = row_h * len(rows) + self._under_tiles_h()
        top = kit.PANEL.y + max(kit.PAD + 10, (kit.PANEL.h - total_h) // 2 + 6)
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
                    surf.blit(art.rounded_rect(tile, tile, 18, art.mix(art.BG_TOP, art.WHITE, 0.3),
                                               border=art.mix(art.BG_BOTTOM, art.INK_SOFT, 0.2), border_w=2), (x, y))
                x += tile + TILE_GAP
                i += 1
        if n >= len(self.letters):
            k = art.ease_out_cubic(kit.appear(self.t, self.spell_t0 + LETTER_FIRST + len(self.letters) * LETTER_S))
            y = top + row_h * len(rows) + 8
            room = kit.PANEL.w - 2 * kit.PAD
            size, lines = kit.fit(self.word, (SPELL_WORD_PX, 52, 44, BODY_PX), room, 1)
            word = kit.text_block([kit.ellipsize(lines[0], size, room)] if lines else [], size, art.INK)
            kit.blit_alpha(surf, word, word.get_rect(midtop=(kit.PANEL.centerx, y)).topleft, k)
            y += art.font(SPELL_WORD_PX).get_height() + 8
            if self.say_it:
                say = fit_say_it(self.say_it, room)
                kit.blit_alpha(surf, say, say.get_rect(midtop=(kit.PANEL.centerx, y)).topleft, k)
                y += art.font(SAY_PX).get_height() + 8
            tip = self._tip_lines()
            if tip:
                text = kit.text_block(tip, BODY_PX, kit.TEXT)
                tag = kit.chip("Tip", art.TANGERINE)
                w = _tag_w("Tip") + text.get_width() + 2 * BAND_PAD
                band = pygame.Rect(kit.PANEL.centerx - w // 2, y + 6, w, text.get_height() + 2 * BAND_Y)
                kit.blit_alpha(surf, art.rounded_rect(band.w, band.h, 18, art.mix(art.LEMON, art.WHITE, 0.72)),
                               band.topleft, k)
                kit.blit_alpha(surf, tag, tag.get_rect(midleft=(band.x + BAND_PAD, band.y + BAND_Y + art.font(
                    BODY_PX).get_height() // 2)).topleft, k)
                kit.blit_alpha(surf, text, (band.x + BAND_PAD + _tag_w("Tip"), band.y + BAND_Y), k)

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
            out = pygame.Surface((tile, tile + 6), pygame.SRCALPHA)
            out.blit(art.rounded_rect(tile, tile, 18, art.inkier(color, 0.4)), (0, 6))
            out.blit(art.rounded_rect(tile, tile, 18, color, shine=True), (0, 0))
            t = art.text_outlined(ch, int(tile * 0.93), art.WHITE, art.inkier(color, 0.55), 3)
            out.blit(t, t.get_rect(center=(tile // 2, tile // 2 + 2)))
            return out

        return art.cached(key, make)


Block = tuple[str, Any, int]  # (kind, its lines or rows, its height in px)


def _text_h(lines: Any, size: int) -> int:
    return kit.line_h(size) * (len(lines) - 1) + art.font(size).get_height() if lines else 0


def _lines_of(block: Block) -> int:
    """How many lines of text a block counts as (MAX_LINES a page)."""
    kind, payload, _ = block
    if kind == "example":
        return len(payload[0]) + len(payload[1])
    return len(payload)


def _tag_w(label: str) -> int:
    """The width a block's label chip takes, with the gap after it."""
    return kit.chip(label, art.GRAPE).get_width() + TAG_GAP


def _chip(word: str, color: art.Color) -> pygame.Surface:
    """A synonym / opposite: the word on a soft candy chip, at body size."""
    return art.pill(word, BODY_PX, art.INK, art.mix(color, art.WHITE, 0.72), pad=(16, 6),
                    border=art.mix(color, art.INK, 0.2))



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
        "part of speech, 1-3 short meanings, 1-2 example sentences, words that mean the same and the opposite, "
        "where the word comes from, plus a picture for a concrete thing. "
        "mode \"spell\" for \"how do you spell X\": the letters pop up one by one while you spell it out loud, "
        "with a spelling tip if there is a good one. "
        "mode \"translate\" for \"how do you say X in Spanish\": the translation big with how to say it, an "
        "example in that language and what it means, other ways to say it and a short note."
    ),
    fields={
        "mode": {"type": "string", "enum": list(MODES), "description": "meaning (default) | spell | translate"},
        "word": {"type": "string", "maxLength": 40,
                 "description": "The word asked about, as written (\"encyclopedia\"); for translate, the word in "
                                "English (\"apple\")"},
        "say_it": {"type": "string", "maxLength": 48,
                   "description": "How to say it as a simple respelling with hyphens between syllables and the "
                                  "stressed syllable in CAPITALS (\"en-SY-kluh-PEE-dee-uh\", \"mahn-SAH-nah\"). "
                                  "Never IPA symbols. For translate: how to say the translation"},
        "part": {"type": "string", "maxLength": 20,
                 "description": "Part of speech: noun, verb, adjective, adverb, ... (meaning)"},
        "meanings": {"type": "array", "items": {"type": "string", "maxLength": 110}, "maxItems": MAX_MEANINGS,
                     "description": "1-3 short, plain meanings, at most 12 words each (meaning)"},
        "example": {"type": "string", "maxLength": 120,
                    "description": "One short example sentence using the word, at most 12 words (for translate: in "
                                   "that language)"},
        "examples": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": MAX_EXAMPLES,
                     "description": "Optional, meaning: a second example sentence can go here (at most 2 in all)"},
        "synonyms": {"type": "array", "items": {"type": "string", "maxLength": 24}, "maxItems": MAX_WORDS,
                     "description": "Optional: up to 4 simple words that mean the same (meaning: \"huge\", \"giant\"); "
                                    "for translate: other ways to say it in that language"},
        "opposites": {"type": "array", "items": {"type": "string", "maxLength": 24}, "maxItems": MAX_OPPOSITES,
                      "description": "Optional, meaning: up to 3 simple words that mean the opposite (\"tiny\")"},
        "origin": {"type": "string", "maxLength": 100,
                   "description": "Optional, meaning: where the word comes from, at most 10 words (\"From Greek: "
                                  "all-round learning\")"},
        "example_meaning": {"type": "string", "maxLength": 120,
                            "description": "translate: what the example means in English (\"I like the red apple\")"},
        "note": {"type": "string", "maxLength": 100,
                 "description": "Optional, at most 12 words. translate: a short tip (\"It's feminine: la "
                                "manzana\"); spell: a spelling tip (\"One collar, two sleeves: 1 c, 2 s\")"},
        "translation": {"type": "string", "maxLength": 40, "description": "The word in the other language (translate)"},
        "language": {"type": "string", "maxLength": 24,
                     "description": "The other language's name, \"Spanish\" (translate)"},
        "picture_query": {"type": "string", "maxLength": 80,
                          "description": "Optional, for a concrete thing only: a Wikipedia-style subject for a "
                                         "picture (\"giraffe\", \"accordion\"). Leave out for abstract words"},
    },
    required=("word",),
    actions={base.PICTURE_ACTION: base.PICTURE_ACTION_HELP,
             "next": "the next page of meanings (spell: show all letters now)", "prev": "the page before",
             "again": "spell: pop the letters in again from the start"},
    build=_build,
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "definition", "word": "encyclopedia", "say_it": "en-SY-kluh-PEE-dee-uh", "part": "noun",
     "meanings": ["A book or set of books full of facts about many things"],
     "example": "We looked up volcanoes in the encyclopedia.", "picture_query": "encyclopedia",
     "synonyms": ["reference book"], "origin": "From Greek: all-round learning"},
    {"card": "definition", "word": "resilient", "say_it": "rih-ZIL-yuhnt", "part": "adjective",
     "meanings": ["Able to recover quickly after something hard", "Springing back into shape after bending"],
     "example": "Babies are resilient when they learn to walk.",
     "synonyms": ["tough", "strong", "bouncy"], "opposites": ["fragile", "delicate"],
     "origin": "From Latin: to jump back"},
    {"card": "definition", "word": "giraffe", "say_it": "juh-RAF", "part": "noun",
     "meanings": ["A very tall African animal with a long neck and spots"],
     "example": "The giraffe ate leaves from the top of the tree.", "picture_query": "giraffe"},
    {"card": "definition", "mode": "spell", "word": "necessary", "say_it": "NESS-uh-sair-ee",
     "note": "One collar, two sleeves: 1 c, 2 s"},
    {"card": "definition", "mode": "translate", "word": "apple", "translation": "manzana", "language": "Spanish",
     "say_it": "mahn-SAH-nah", "example": "Me gusta la manzana roja.", "example_meaning": "I like the red apple.",
     "note": "It's feminine: la manzana", "picture_query": "apple"},
    {"card": "definition", "mode": "translate", "word": "thank you", "translation": "merci", "language": "French",
     "say_it": "mair-SEE", "synonyms": ["merci beaucoup"], "example": "Merci, maman !",
     "example_meaning": "Thank you, mum!"},
    {"card": "definition", "word": "light", "say_it": "LYTE", "part": "adjective",  # the longest: two pages
     "meanings": ["Not heavy: easy to lift or carry", "Pale in colour, with lots of white in it",
                  "Bright, with the sun or a lamp shining"],
     "examples": ["The feather is as light as air.", "She painted the room light blue."],
     "synonyms": ["weightless", "pale", "bright", "airy"], "opposites": ["heavy", "dark"],
     "origin": "From Old English leoht"},
]

__all__ = ["MODES", "SAMPLES", "DefinitionCard"]
