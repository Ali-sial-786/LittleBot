"""The "facts" card: the gist of everything else a home assistant answers, in one of six layouts.

``layout`` picks how it looks:

* ``number``  - one big number and what it means: "how tall is the Eiffel Tower?" -> value "330 m",
  label "tall", note "as tall as an 81-storey building"; optional picture.
* ``list``    - a short list: "the planets in order", "top 3 baby names"; numbered unless ``ordered``
  is false; paged when long.
* ``compare`` - two or three things side by side: ``columns`` (each a name and its facts), with optional
  ``rows`` labels ("Weight", "Lives in") so the facts line up as a table; paged when long.
* ``profile`` - a person, place or animal: picture plus 2-3 short facts and a subtitle ("Animal").
* ``score``   - a sports score: two teams and their scores, the status ("Full time") and a note.
* ``item``    - one short item: "today in history", a quote, a single fact: a kicker line ("24 October
  1945"), 1-3 short lines of text and an optional picture.

Text is at least BODY_PX, at most MAX_LINES lines a page; "next" / "prev" (or a tap) turn pages.
"""

from __future__ import annotations

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

LAYOUTS = ("number", "list", "compare", "profile", "score", "item")
VALUE_SIZES = (132, 116, 100, 84, 68, 56, TITLE_PX)
LIST_PX = 38
CELL_PX = 34
FACT_PX = 36
ITEM_SIZES = (56, 50, 46, 42, 38, 34, BODY_PX)
PIC_W = 262
MAX_ITEMS = 12


def _column(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return None
    name = kit.clean(raw.get("name") or raw.get("title"), 24)
    if not name:
        return None
    return {"name": name, "facts": kit.str_list(raw.get("facts") or raw.get("values"), max_items=8, limit=50)}


def _team(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    name = kit.clean(raw.get("name"), 24)
    score = kit.clean(raw.get("score"), 8)
    return {"name": name, "score": score or "0"} if name else None


class FactsCard(Card):
    kind = "facts"

    def __init__(self, spec: dict[str, Any], host: Any = None) -> None:
        super().__init__(spec, host)
        layout = str(spec.get("layout") or "").strip().lower()
        self.layout = layout if layout in LAYOUTS else "item"
        self.title = kit.clean(spec.get("title"), 60)
        self.value = kit.clean(spec.get("value"), 24)
        self.label = kit.clean(spec.get("label"), 40)
        self.note = kit.clean(spec.get("note"), 80)
        self.items = kit.str_list(spec.get("items"), max_items=MAX_ITEMS, limit=60)
        self.ordered = spec.get("ordered") is not False
        self.columns = [c for c in (_column(c) for c in (spec.get("columns") or [])[:3]) if c]
        self.rows = kit.str_list(spec.get("rows"), max_items=8, limit=24)
        self.subtitle = kit.clean(spec.get("subtitle"), 40)
        self.facts = kit.str_list(spec.get("facts"), max_items=4, limit=80)
        self.teams = [t for t in (_team(t) for t in (spec.get("teams") or [])[:2]) if t]
        self.status_text = kit.clean(spec.get("status"), 24)
        self.kicker = kit.clean(spec.get("kicker"), 40)
        self.text = kit.clean(spec.get("text"), 200)
        self.mood = {"number": "surprised", "score": "happy"}.get(self.layout, "curious")
        wants_pic = self.layout in ("number", "list", "profile", "item")
        self.slot = kit.PictureSlot(self.host, kit.clean(spec.get("picture_query"), 80) if wants_pic else "")
        self.page = 0
        self.page_t = 0.0
        self._pages: dict[bool, list[Any]] = {}

    # ------------------------------------------------------------ pages
    def pages(self) -> list[Any]:
        """What each page shows (layout-specific); one page for the layouts that never page."""
        pic = self.slot.wanted
        got = self._pages.get(pic)
        if got is None:
            got = self._paginate(pic)
            self._pages[pic] = got
        return got

    def _text_right(self, pic: bool) -> int:
        return kit.PANEL.right - kit.PAD - (PIC_W + 18 if pic else 0)

    def _paginate(self, pic: bool) -> list[Any]:
        if self.layout == "list":
            width = self._text_right(pic) - (kit.PANEL.x + kit.PAD + 58)
            lines = [kit.wrap(i, LIST_PX, width)[:2] for i in self.items]
            return [[(i, lines[i]) for i in page] for page in kit.paged([len(ls) for ls in lines])]
        if self.layout == "compare":
            n = max((len(c["facts"]) for c in self.columns), default=0)
            n = max(n, len(self.rows))
            heights = [self._row_lines(r) for r in range(n)]
            return kit.paged(heights, MAX_LINES - 1) or [[]]  # the header row counts as a line
        if self.layout == "profile":
            width = self._text_right(False) - (kit.PANEL.x + kit.PAD + 40) - (PIC_W + 18 if pic else 0)
            lines = [kit.wrap(f, FACT_PX, width)[:3] for f in self.facts]
            return [[(i, lines[i]) for i in page] for page in kit.paged([len(ls) for ls in lines])] or [[]]
        return [None]

    def _col_geometry(self) -> tuple[int, int, int]:
        """(x of the first value column, column width, label column width) for the compare table."""
        left, right = kit.PANEL.x + kit.PAD, kit.PANEL.right - kit.PAD
        label_w = 150 if self.rows else 0
        n = max(1, len(self.columns))
        col_w = (right - left - label_w - 12 * (n - 1)) // n
        return left + label_w, col_w, label_w

    def _cell_lines(self, col: int, row: int) -> tuple[str, ...]:
        facts = self.columns[col]["facts"]
        text = facts[row] if row < len(facts) else "-"
        return kit.wrap(text, CELL_PX, self._col_geometry()[1] - 16)[:2]

    def _row_lines(self, row: int) -> int:
        label = kit.wrap(self.rows[row], BODY_PX, self._col_geometry()[2] - 12)[:2] if row < len(self.rows) else ()
        return max([len(label)] + [len(self._cell_lines(c, row)) for c in range(len(self.columns))])

    # ------------------------------------------------------------ logic
    def update(self, dt: float) -> None:
        super().update(dt)
        self.slot.update(self.t)
        self.page = min(self.page, len(self.pages()) - 1)

    def _turn(self, page: int) -> bool:
        page = max(0, min(len(self.pages()) - 1, page))
        if page != self.page:
            self.page, self.page_t = page, self.t
        return True

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "next":
            return self._turn(self.page + 1)
        if action == "prev":
            return self._turn(self.page - 1)
        if action == "page":
            n = kit.as_int(args.get("n"), 0)
            return 1 <= n <= len(self.pages()) and self._turn(n - 1)
        return False

    def tap(self, x: float, y: float) -> bool:
        if len(self.pages()) <= 1:
            return False
        return self.command("prev" if x < art.W * 0.33 else "next", {})

    def key(self, name: str) -> bool:
        if name in ("right", "space", "return", "down"):
            return self.command("next", {})
        if name in ("left", "backspace", "up"):
            return self.command("prev", {})
        return False

    def describe(self) -> str:
        title = f"{self.title}: " if self.title else ""
        pages = self.pages()
        paging = f" (page {self.page + 1} of {len(pages)})" if len(pages) > 1 else ""
        if self.layout == "number":
            return f"{title}{self.value} {self.label}".strip()
        if self.layout == "list":
            items = ", ".join(self.items[i] for i, _ in pages[self.page])
            return f"{title}{items}{paging}"
        if self.layout == "compare":
            names = " vs ".join(c["name"] for c in self.columns)
            shown = []
            for r in pages[self.page]:
                label = f"{self.rows[r]}: " if r < len(self.rows) else ""
                shown.append(label + ", ".join(c["facts"][r] if r < len(c["facts"]) else "-" for c in self.columns))
            return f"{title}{names}{paging}: {'; '.join(shown)}".rstrip(": ")
        if self.layout == "profile":
            facts = "; ".join(self.facts[i] for i, _ in pages[self.page])
            sub = f" ({self.subtitle})" if self.subtitle else ""
            return f"{self.title}{sub}: {facts}{paging}"
        if self.layout == "score":
            if len(self.teams) == 2:
                a, b = self.teams
                status = f", {self.status_text}" if self.status_text else ""
                return f"{a['name']} {a['score']}, {b['name']} {b['score']}{status}"
            return self.title or "a score"
        kicker = f"{self.kicker}: " if self.kicker else ""
        return f"{title}{kicker}{self.text}"

    def status(self) -> dict[str, Any]:
        return {**super().status(), "layout": self.layout, "page": self.page + 1, "pages": len(self.pages())}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=53)
        header = self.title
        if self.layout == "score" and not header and len(self.teams) == 2:
            header = f"{self.teams[0]['name']} v {self.teams[1]['name']}"
        if header:
            kit.header_title(surf, header)
        kit.panel(surf)
        getattr(self, f"_draw_{self.layout}")(surf)
        pages = self.pages()
        kit.page_dots(surf, len(pages), self.page, (kit.PANEL.centerx, kit.PANEL.bottom - 16))

    def _pic_rect(self, top: int | None = None) -> pygame.Rect:
        top = kit.PANEL.y + kit.PAD if top is None else top
        return pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, top, PIC_W, kit.PANEL.bottom - kit.PAD - top)

    def _enter(self, delay: float = 0.1) -> float:
        return art.ease_out_cubic(kit.appear(self.t, max(self.page_t, 0.0) + delay))

    def _draw_note(self, surf: pygame.Surface, cx: int, y: int, width: int, fade: float) -> int:
        """The note under a number or score (soft, wrapped, at most two lines); returns its bottom."""
        if not self.note:
            return y
        lines = kit.wrap(self.note, BODY_PX, width)[:2]
        img = kit.text_block(lines, BODY_PX, kit.TEXT_SOFT, align="center")
        kit.blit_alpha(surf, img, img.get_rect(midtop=(cx, y)).topleft, fade)
        return y + img.get_height()

    def _draw_number(self, surf: pygame.Surface) -> None:
        pic = self.slot.wanted
        left, right = kit.PANEL.x + kit.PAD, self._text_right(pic)
        cx = (left + right) // 2
        big = kit.outlined_fit(self.value, VALUE_SIZES, right - left, art.inkier(art.TANGERINE, 0.04), art.INK)
        label = kit.text_block(kit.wrap(self.label, 40, right - left)[:2], 40, kit.TEXT, align="center") \
            if self.label else None
        note_h = (kit.line_h(BODY_PX) * min(2, len(kit.wrap(self.note, BODY_PX, right - left))) + 10) if self.note else 0
        total = big.get_height() + (label.get_height() + 6 if label else 0) + note_h
        top = kit.PANEL.y + max(kit.PAD, (kit.PANEL.h - total) // 2 - 8)
        k = art.ease_out_back(kit.appear(self.t, 0.1, 0.55))
        art.blit_center(surf, art.scaled(big, max(0.05, k)), (cx, top + big.get_height() // 2))
        y = top + big.get_height() + 6
        fade = self._enter(0.35)
        if label:
            kit.blit_alpha(surf, label, label.get_rect(midtop=(cx, y)).topleft, fade)
            y += label.get_height() + 10
        self._draw_note(surf, cx, y, right - left, fade)
        if pic:
            self.slot.draw(surf, self._pic_rect(), self.t)

    def _draw_list(self, surf: pygame.Surface) -> None:
        pic = self.slot.wanted
        page = self.pages()[self.page]
        x = kit.PANEL.x + kit.PAD
        pitch = kit.line_h(LIST_PX)
        gap = 14
        total = sum(pitch * len(ls) for _, ls in page) + gap * max(0, len(page) - 1)
        y = kit.PANEL.y + max(kit.PAD, (kit.PANEL.h - 24 - total) // 2)
        for n, (i, lines) in enumerate(page):
            fade = self._enter(0.08 * n)
            dx = int((1 - fade) * 30)
            color = kit.ACCENTS[i % len(kit.ACCENTS)]
            first = art.font(LIST_PX).get_height() // 2
            if self.ordered:
                badge = kit.number_badge(i + 1, color, 20)
                kit.blit_alpha(surf, badge, badge.get_rect(center=(x + 22, y + first + 1)).topleft, fade)
            else:
                kit.blit_alpha(surf, art.circle(10, color), (x + 12, y + first - 10), fade)
            kit.blit_alpha(surf, kit.text_block(lines, LIST_PX, pitch=pitch), (x + 58 + dx, y), fade)
            y += pitch * len(lines) + gap
        if pic:
            self.slot.draw(surf, self._pic_rect(), self.t)

    def _draw_compare(self, surf: pygame.Surface) -> None:
        """Candy column cards (name on top) holding this page's rows; row labels on the left."""
        x0, col_w, label_w = self._col_geometry()
        rows = self.pages()[self.page]
        pitch = kit.line_h(CELL_PX)
        heights = [pitch * self._row_lines(r) for r in rows]
        head_h, row_gap = 50, 18
        body_h = sum(heights) + row_gap * len(heights) + 6
        total = head_h + body_h
        avail = kit.PANEL.h - 2 * kit.PAD - (24 if len(self.pages()) > 1 else 0)
        top = kit.PANEL.y + kit.PAD + max(0, (avail - total) // 2)
        line_c = art.mix(art.INK_SOFT, art.WHITE, 0.55)
        for c, col in enumerate(self.columns):
            color = kit.ACCENTS[c]
            x = x0 + c * (col_w + 12)
            surf.blit(art.rounded_rect(col_w, total, 18, art.mix(color, art.WHITE, 0.84)), (x, top))
            surf.blit(art.rounded_rect(col_w, head_h, 18, art.inkier(color, 0.22), shine=True), (x, top))
            name = kit.text_block([kit.ellipsize(col["name"], BODY_PX + 2, col_w - 16)], BODY_PX + 2, art.WHITE)
            surf.blit(name, name.get_rect(center=(x + col_w // 2, top + head_h // 2 + 1)))
        y = top + head_h + row_gap
        fade = self._enter()
        for n, (r, h) in enumerate(zip(rows, heights)):
            if r < len(self.rows):
                label = kit.text_block(kit.wrap(self.rows[r], BODY_PX, label_w - 12)[:2], BODY_PX, kit.TEXT_SOFT)
                kit.blit_alpha(surf, label, (kit.PANEL.x + kit.PAD, y + 2), fade)
            for c in range(len(self.columns)):
                x = x0 + c * (col_w + 12)
                cell = kit.text_block(self._cell_lines(c, r), CELL_PX, kit.TEXT, align="center")
                kit.blit_alpha(surf, cell, cell.get_rect(midtop=(x + col_w // 2, y)).topleft, fade)
                if n < len(rows) - 1:
                    yy = y + h + row_gap // 2 - 2
                    pygame.draw.line(surf, line_c, (x + 14, yy), (x + col_w - 14, yy), 2)
            y += h + row_gap

    def _draw_profile(self, surf: pygame.Surface) -> None:
        pic = self.slot.wanted
        left = kit.PANEL.x + kit.PAD + ((PIC_W + 18) if pic else 0)
        if pic:
            rect = pygame.Rect(kit.PANEL.x + kit.PAD, kit.PANEL.y + kit.PAD, PIC_W, kit.PANEL.h - 2 * kit.PAD)
            self.slot.draw(surf, rect, self.t)
        y = kit.PANEL.y + kit.PAD + 4
        if self.subtitle:
            c = kit.chip(self.subtitle, art.GRAPE, size=BODY_PX - 2)
            surf.blit(c, (left, y))
            y += c.get_height() + 14
        page = self.pages()[self.page]
        pitch = kit.line_h(FACT_PX)
        total = sum(pitch * len(ls) for _, ls in page) + 16 * max(0, len(page) - 1)
        y += max(0, min(36, (kit.PANEL.bottom - kit.PAD - 20 - y - total) // 2))
        for n, (i, lines) in enumerate(page):
            fade = self._enter(0.1 * n)
            color = kit.ACCENTS[(i + 2) % len(kit.ACCENTS)]
            first = art.font(FACT_PX).get_height() // 2
            kit.blit_alpha(surf, art.star(13, color), (left, y + first - 13), fade)
            kit.blit_alpha(surf, kit.text_block(lines, FACT_PX, pitch=pitch), (left + 38, y), fade)
            y += pitch * len(lines) + 16

    def _draw_score(self, surf: pygame.Surface) -> None:
        if len(self.teams) != 2:
            return
        top = kit.PANEL.y + kit.PAD
        cx = kit.PANEL.centerx
        half = (kit.PANEL.w - 2 * kit.PAD) // 2
        k = art.ease_out_back(kit.appear(self.t, 0.15, 0.55))
        y_score = top + 120
        for side, team in enumerate(self.teams):
            color = kit.ACCENTS[side]
            tx = cx - half // 2 if side == 0 else cx + half // 2
            name = kit.text_block(kit.wrap(team["name"], 38, half - 40)[:2], 38, kit.TEXT, align="center")
            surf.blit(name, name.get_rect(midtop=(tx, top + 6)))
            score = kit.outlined_fit(team["score"], (140, 120, 100, 80), half - 60, art.inkier(color, 0.05), art.INK)
            art.blit_center(surf, art.scaled(score, max(0.05, k)), (tx, y_score + 40))
        art.blit_center(surf, art.rounded_rect(44, 14, 7, art.mix(art.INK_SOFT, art.WHITE, 0.25), border=art.INK,
                                               border_w=3), (cx, y_score + 40))
        y = y_score + 120
        if self.status_text:
            pill = kit.chip(self.status_text, art.MINT, size=BODY_PX)
            surf.blit(pill, pill.get_rect(midtop=(cx, y)))
            y += pill.get_height() + 10
        self._draw_note(surf, cx, y, kit.PANEL.w - 2 * kit.PAD, self._enter(0.3))

    def _draw_item(self, surf: pygame.Surface) -> None:
        pic = self.slot.wanted
        left, right = kit.PANEL.x + kit.PAD, self._text_right(pic)
        y = kit.PANEL.y + kit.PAD + 4
        if self.kicker:
            c = kit.chip(self.kicker, art.STRAWBERRY, size=BODY_PX)
            if c.get_width() > right - left:
                c = kit.chip(kit.ellipsize(self.kicker, BODY_PX, right - left - 40), art.STRAWBERRY, size=BODY_PX)
            surf.blit(c, (left, y))
            y += c.get_height() + 10
        size, lines = kit.fit(self.text, ITEM_SIZES, right - left - 8)
        lines = lines[:MAX_LINES]
        img = kit.text_block(lines, size)
        bottom = kit.PANEL.bottom - kit.PAD - (kit.line_h(BODY_PX) * 2 + 8 if self.note else 0)
        ty = y + max(0, (bottom - y - img.get_height()) // 2)
        fade = self._enter(0.2)
        kit.blit_alpha(surf, img, (left + 4 + int((1 - fade) * 24), ty), fade)
        if self.note:
            note = kit.text_block(kit.wrap(self.note, BODY_PX, right - left)[:2], BODY_PX, kit.TEXT_SOFT)
            kit.blit_alpha(surf, note, (left + 4, kit.PANEL.bottom - kit.PAD - note.get_height()), fade)
        if pic:
            self.slot.draw(surf, self._pic_rect(), self.t)


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    card = FactsCard(spec, host)
    ok = {
        "number": bool(card.value),
        "list": bool(card.items),
        "compare": len(card.columns) >= 2 and any(c["facts"] for c in card.columns),
        "profile": bool(card.title and (card.facts or card.slot.picture is not None)),
        "score": len(card.teams) == 2,
        "item": bool(card.text),
    }[card.layout]
    return card if ok else None


register(CardType(
    kind="facts",
    description=(
        "A quick answer on screen, for everything without its own card. Pick a layout: "
        "\"number\" one big number (\"how tall is the Eiffel Tower\": value \"330 m\", label \"tall\"); "
        "\"list\" a short list (\"planets in order\", \"top 3 ...\"); "
        "\"compare\" two or three things side by side (columns, optional row labels); "
        "\"profile\" a person, place or animal (picture_query, subtitle, 2-3 facts); "
        "\"score\" a sports score (two teams, status); "
        "\"item\" one short item (\"today in history\", a quote, a single fact: kicker + text). "
        "Keep every text short: the screen shows the gist, your voice says the rest."
    ),
    fields={
        "layout": {"type": "string", "enum": list(LAYOUTS), "description": "number | list | compare | profile | "
                                                                           "score | item"},
        "title": {"type": "string", "description": "What it is about, 1-5 words (\"Eiffel Tower\", \"Planets\")"},
        "value": {"type": "string", "description": "number: the number with its unit, short (\"330 m\", \"1889\", "
                                                   "\"8 billion\")"},
        "label": {"type": "string", "description": "number: what the number is, 1-4 words (\"tall\", \"built in\")"},
        "note": {"type": "string", "description": "Optional one short extra line for number, score or item "
                                                  "(\"as tall as an 81-storey building\")"},
        "items": {"type": "array", "items": {"type": "string"}, "description": "list: the items, each 1-6 words"},
        "ordered": {"type": "boolean", "description": "list: number the items (default true)"},
        "columns": {"type": "array", "maxItems": 3, "description": "compare: 2-3 columns, each "
                    "{\"name\": \"Lion\", \"facts\": [\"190 kg\", \"Africa\"]}; the n-th facts line up as a row",
                    "items": {"type": "object", "properties": {"name": {"type": "string"},
                                                               "facts": {"type": "array", "items": {"type": "string"}}}}},
        "rows": {"type": "array", "items": {"type": "string"},
                 "description": "compare, optional: a label for each row of facts (\"Weight\", \"Lives in\")"},
        "subtitle": {"type": "string", "description": "profile: what it is, 1-4 words (\"Animal\", \"City in Japan\")"},
        "facts": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                  "description": "profile: 2-3 short facts, each at most 8 words"},
        "teams": {"type": "array", "maxItems": 2, "description": "score: [{\"name\": \"Arsenal\", \"score\": \"2\"}, "
                  "{\"name\": \"Chelsea\", \"score\": \"1\"}]",
                  "items": {"type": "object", "properties": {"name": {"type": "string"}, "score": {"type": "string"}}}},
        "status": {"type": "string", "description": "score: \"Full time\", \"Half time\", \"Live 63'\""},
        "kicker": {"type": "string", "description": "item: a short line above the text (\"Today in history\", "
                                                    "\"24 October 1945\")"},
        "text": {"type": "string", "description": "item: the item itself, at most 25 words"},
        "picture_query": {"type": "string", "description": "Optional for number, list, profile, item: a "
                                                           "Wikipedia-style subject for a picture (\"Eiffel Tower\")"},
    },
    required=("layout",),
    actions={base.PICTURE_ACTION: base.PICTURE_ACTION_HELP,
             "next": "the next page (list, compare, profile)", "prev": "the page before",
             "page": "go to page n (1-based): {n}"},
    build=_build,
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "facts", "layout": "number", "title": "Eiffel Tower", "value": "330 m", "label": "tall",
     "note": "About as tall as an 81-storey building", "picture_query": "Eiffel Tower"},
    {"card": "facts", "layout": "number", "title": "People on Earth", "value": "8.2 billion",
     "label": "people", "note": "and growing by about 70 million a year"},
    {"card": "facts", "layout": "list", "title": "Planets in order",
     "items": ["Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"]},
    {"card": "facts", "layout": "list", "title": "Good first finger foods", "ordered": False,
     "picture_query": "banana", "items": ["Soft banana pieces", "Steamed carrot sticks", "Avocado slices",
                                          "Toast strips"]},
    {"card": "facts", "layout": "compare", "title": "Lion or tiger?", "rows": ["Weight", "Lives in", "Lives"],
     "columns": [{"name": "Lion", "facts": ["190 kg", "Africa", "In groups"]},
                 {"name": "Tiger", "facts": ["220 kg", "Asia", "Alone"]}]},
    {"card": "facts", "layout": "compare", "title": "Milk for babies",
     "columns": [{"name": "Breast milk", "facts": ["Changes as baby grows", "Always ready"]},
                 {"name": "Formula", "facts": ["Same every feed", "Others can help feed"]},
                 {"name": "Cow's milk", "facts": ["Main drink from 12 months", "Fine in food before"]}]},
    {"card": "facts", "layout": "profile", "title": "Giraffe", "subtitle": "Animal", "picture_query": "giraffe",
     "facts": ["Tallest animal on land", "Sleeps only about 30 minutes a day", "Its tongue is about 50 cm long"]},
    {"card": "facts", "layout": "score", "teams": [{"name": "Arsenal", "score": "2"}, {"name": "Chelsea", "score": "1"}],
     "status": "Full time", "note": "Premier League"},
    {"card": "facts", "layout": "item", "title": "Today in history", "kicker": "24 October 1945",
     "text": "The United Nations was founded, to help countries work together for peace.",
     "picture_query": "United Nations"},
    {"card": "facts", "layout": "item", "title": "Fun fact", "text": "Octopuses have three hearts and blue blood.",
     "note": "Two hearts pump blood to the gills"},
]

__all__ = ["LAYOUTS", "SAMPLES", "FactsCard"]
