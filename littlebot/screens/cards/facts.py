"""The "facts" card: the gist of everything else a home assistant answers, in one of six layouts.

``layout`` picks how it looks:

* ``number``  - one big number and what it means: "how tall is the Eiffel Tower?" -> value "330 m",
  label "tall", note "as tall as an 81-storey building", and up to four ``stats`` tiles under it ("Built:
  1889", "Steps: 1,665"); optional picture.
* ``list``    - a list of up to 12: "the planets in order", "top 5 baby names"; numbered unless ``ordered``
  is false; short items in two columns when there is no picture (eight planets on one page); paged when
  long; an optional ``note`` under it.
* ``compare`` - two to four things side by side: ``columns`` (each a name and its facts), with optional
  ``rows`` labels ("Weight", "Lives in") so the facts line up as a table (up to 8 rows, paged); an
  optional ``note`` under it ("Both are big cats").
* ``profile`` - a person, place or animal: picture plus up to five short facts, a subtitle ("Animal") and up
  to four ``stats`` tiles ("Height: 5.5 m").
* ``score``   - a sports score: two teams, their scores and optionally who scored (each team's ``detail``),
  the status ("Full time") and a note.
* ``item``    - one item: "today in history", a quote, a single fact: a kicker line ("24 October 1945"),
  the text as big as fits and an optional picture and note.

Text is at least BODY_PX (labels in the stats tiles SMALL_PX), at most MAX_LINES lines a page; "next" /
"prev" (or a tap) turn pages.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens import base
from littlebot.screens.base import (
    BODY_PX,
    MAX_LINES,
    SMALL_PX,
    TITLE_PX,
    Card,
    CardType,
    register,
)
from littlebot.screens.cards import _kit as kit

log = logging.getLogger("littlebot.screens")

LAYOUTS = ("number", "list", "compare", "profile", "score", "item")
VALUE_SIZES = (168, 148, 132, 116, 100, 84, 68, 56, TITLE_PX)
LABEL_PX = 44  # number: what the number is
LIST_PX = 40
LIST_INDENT = 64  # the text right of a list item's badge
LIST_GAP = 12
LIST_GUTTER = 40  # between two columns of a list
TWO_COLUMNS_FROM = 5  # a list of this many short items goes in two columns (when there is no picture)
CELL_PX = BODY_PX
HEAD_PX = 38  # a compare column's name
HEAD_H = 60
ROW_GAP = 16
COL_GAP = 12
LABEL_W = 200  # compare: the row labels' column
LABEL_W_NARROW = 170  # ...beside four columns
FACT_PX = 40
FACT_INDENT = 44
FACT_GAP = 14
SUBTITLE_PX = 34
TEAM_PX = 44
SCORE_SIZES = (180, 156, 132, 112, 96)
ITEM_SIZES = (64, 58, 52, 48, 44, 40, BODY_PX)
STAT_PX = 40  # a stats tile's value (its label is SMALL_PX)
STAT_H = 86
STAT_GAP = 14
STAT_MIN_W = 190
STAT_MAX_W = 300
DOTS_H = 30  # room kept for the page dots
PIC_W = kit.PIC_W
MAX_ITEMS = 12
MAX_COLUMNS = 4
MAX_ROWS = 8
MAX_FACTS = 5
MAX_STATS = 4


LABEL_KEYS = ("label", "row", "aspect", "category", "feature", "trait", "what")
VS = re.compile(r"\s+(?:vs\.?|versus|or|and|compared (?:to|with))\s+", re.I)


def _column(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, str):
        raw = {"name": raw}
    if isinstance(raw, (list, tuple)):  # ["Lion", "190 kg", "Africa"]: its name, then its facts
        cells = [kit.clean(c, 50) for c in raw]
        raw = {"name": cells[0], "facts": cells[1:]} if cells else {}
    if not isinstance(raw, dict):
        return None
    name = kit.clean(raw.get("name") or raw.get("title"), 24)
    if not name:
        return None
    return {"name": name, "facts": kit.str_list(raw.get("facts") or raw.get("values"), max_items=MAX_ROWS, limit=50)}


def _table(columns: list[dict[str, Any]], raw_rows: Any, title: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Columns and row labels from a comparison written row by row, as models often do: ``rows`` as records
    ({"label": "Weight", "Lion": "190 kg", "Tiger": "220 kg"}, or a label and its "values" in the columns'
    order) or as lists (a header row, then a label and a value per column). ([], []) when it isn't one."""
    if not isinstance(raw_rows, (list, tuple)):
        return [], []
    records = [r for r in raw_rows if isinstance(r, (dict, list, tuple))]
    if not records:
        return _lines(columns, [r for r in raw_rows if isinstance(r, str)], title)
    if all(isinstance(r, dict) and ("facts" in r or "values" in r) and not any(str(k).lower() in LABEL_KEYS
                                                                              for k in r) for r in records):
        named = [c for c in (_column(r) for r in records) if c and c["facts"]]  # columns, under the wrong name
        return (named[:MAX_COLUMNS], []) if len(named) >= 2 else ([], [])
    names = [c["name"] for c in columns]
    if not names and isinstance(records[0], (list, tuple)):  # a header row: ["", "Lion", "Tiger"]
        header = [kit.clean(c, 24) for c in records[0]]
        width = max((len(r) for r in records[1:] if isinstance(r, (list, tuple))), default=len(header))
        # its empty corner cell may have been dropped on the way: then every cell of it is a name
        names = [n for n in (header if len(header) < width else header[1:]) if n]
        records = records[1:]
    labels: list[str] = []
    cells: list[dict[int, str]] = []
    for r in records:
        row: dict[int, str] = {}
        if isinstance(r, dict):
            key = next((k for k in r if str(k).lower() in LABEL_KEYS), None)
            label = kit.clean(r.get(key), 24) if key is not None else ""
            values = r.get("values") or r.get("facts")
            if isinstance(values, (list, tuple)):  # in the columns' order
                row = {i: kit.clean(v, 50) for i, v in enumerate(values)}
            else:
                for k, v in r.items():
                    name = kit.clean(k, 24)
                    if k == key or not name or isinstance(v, (dict, list, tuple)):
                        continue
                    if name not in names:
                        names.append(name)
                    row[names.index(name)] = kit.clean(v, 50)
        else:
            label = kit.clean(r[0], 24) if r else ""
            row = {i: kit.clean(v, 50) for i, v in enumerate(list(r)[1:])}
        labels.append(label)
        cells.append(row)
    names = (names or _title_names(title))[:MAX_COLUMNS]
    out = [{"name": n, "facts": [row.get(i) or "-" for row in cells]} for i, n in enumerate(names)]
    if len(out) < 2 or not any(row for row in cells):
        return [], []
    return out, labels if any(labels) else []


def _title_names(title: str) -> list[str]:
    """The things a title compares: "Genghis Khan vs Kublai Khan"."""
    return [n[:1].upper() + n[1:] for n in (kit.clean(p, 24) for p in VS.split(title.rstrip("?!. "))) if n]


# How one line of text holds a value per column ("Conqueror vs Ruler", "Conqueror | Ruler"), tried in this order
SPLITS = tuple(re.compile(p, re.I) for p in (r"\s+(?:vs\.?|versus)\s+", r"\s*\|\s*", r"\s+/\s+", r"\s*;\s*",
                                             r"\s+[-–—]+>?\s+|\s*→\s*", r",?\s+(?:while|whereas|but)\s+",
                                             r",\s+"))


def _owner(label: str, names: list[str]) -> int | None:
    """The column a line's label names ("Genghis" for "Genghis Khan"), when it names exactly one."""
    low = label.strip().lower()
    if not low:
        return None
    hits = [i for i, n in enumerate(names)
            if low == n.lower() or (len(low) >= 4 and (low in n.lower() or n.lower() in low))]
    return hits[0] if len(hits) == 1 else None


def _lines(columns: list[dict[str, Any]], lines: list[str], title: str) -> tuple[list[dict[str, Any]], list[str]]:
    """A comparison whose ``rows`` are lines of text, as Haiku wrote one on the robot (2026-10-29): a label and
    a value per column ("Role: Conqueror vs Ruler"), or a fact of one column ("Genghis Khan: united the
    tribes"). The names come from ``columns`` or the title. ([], []) when it isn't one."""
    names = ([c["name"] for c in columns] or _title_names(title))[:MAX_COLUMNS]
    if len(names) < 2:
        return [], []
    own: dict[int, list[str]] = {i: [] for i in range(len(names))}
    labels: list[str] = []
    cells: list[list[str]] = []
    for line in lines:
        text = " ".join(line.split())
        label, colon, rest = text.partition(":")
        if not colon or not rest.strip() or len(label) > 30:
            label, rest = "", text
        i = _owner(label, names)
        if i is not None:
            own[i].append(kit.clean(rest, 50))
            continue
        parts = next((p for p in ([s.strip() for s in sep.split(rest.strip()) if s.strip()] for sep in SPLITS)
                      if len(p) == len(names)), None)
        if parts is not None:
            labels.append(kit.clean(label, 24))
            cells.append([kit.clean(p, 50) for p in parts])
    if cells:
        out = [{"name": n, "facts": [row[i] for row in cells][:MAX_ROWS]} for i, n in enumerate(names)]
        return out, labels if any(labels) else []
    if all(own.values()):  # each line a fact of one of them: a list of facts per column
        return [{"name": n, "facts": own[i][:MAX_ROWS]} for i, n in enumerate(names)], []
    return [], []


def _shape(value: Any) -> str:
    """What a field looked like (types and keys, never the words), for the log."""
    if isinstance(value, dict):
        return "{" + ",".join(sorted(str(k)[:20] for k in value))[:80] + "}"
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} x {_shape(value[0])}]" if value else "[]"
    return type(value).__name__


def _team(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    name = kit.clean(raw.get("name"), 24)
    score = kit.clean(raw.get("score"), 8)
    if not name:
        return None
    team = {"name": name, "score": score or "0"}
    detail = kit.clean(raw.get("detail") or raw.get("scorers"), 60)
    if detail:
        team["detail"] = detail
    return team


def _stat(raw: Any) -> tuple[str, str] | None:
    """One key number: "Height: 5.5 m" or {"label": "Height", "value": "5.5 m"} -> ("Height", "5.5 m")."""
    if isinstance(raw, dict):
        label = kit.clean(raw.get("label") or raw.get("name") or raw.get("what"), 22)
        value = kit.clean(raw.get("value") if raw.get("value") not in (None, "") else raw.get("number"), 22)
    elif isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
        label, colon, value = str(raw).partition(":")
        if not colon or not value.strip():
            label, value = "", str(raw)
        label, value = kit.clean(label, 22), kit.clean(value, 22)
    else:
        return None
    return (label, value) if value else None


def stat_tile(label: str, value: str, w: int, color: art.Color) -> pygame.Surface:
    """A key number in a soft candy tile: the value big, what it is small above it (cached)."""
    key = ("facts_stat", label, value, w, color)

    def make() -> pygame.Surface:
        h = STAT_H
        out = pygame.Surface((w, h + 4), pygame.SRCALPHA)
        out.blit(art.rounded_rect(w, h, 20, art.mix(color, art.WHITE, 0.8), shadow=3,
                                  border=art.mix(color, art.INK, 0.25), border_w=2), (0, 0))
        size = next((s for s in (STAT_PX, BODY_PX) if kit.text_w(value, s) <= w - 24), BODY_PX)
        val = art.text(kit.ellipsize(value, size, w - 24), size, kit.TEXT)
        if label:
            lab = art.text(kit.ellipsize(label, SMALL_PX, w - 24), SMALL_PX, art.mix(kit.TEXT_SOFT, color, 0.15))
            gap = 4
            top = (h - lab.get_height() - gap - val.get_height()) // 2
            out.blit(lab, lab.get_rect(midtop=(w // 2, top)))
            out.blit(val, val.get_rect(midtop=(w // 2, top + lab.get_height() + gap)))
        else:
            out.blit(val, val.get_rect(center=(w // 2, h // 2)))
        return out

    return art.cached(key, make)


class FactsCard(Card):
    kind = "facts"

    def __init__(self, spec: dict[str, Any], host: Any = None) -> None:
        super().__init__(spec, host)
        layout = str(spec.get("layout") or "").strip().lower()
        self.layout = layout if layout in LAYOUTS else "item"
        self.title = kit.clean(spec.get("title"), 60)
        self.value = kit.clean(spec.get("value"), 24)
        self.label = kit.clean(spec.get("label"), 40)
        self.note = kit.clean(spec.get("note"), 100)
        self.items = kit.str_list(spec.get("items"), max_items=MAX_ITEMS, limit=60)
        self.ordered = spec.get("ordered") is not False
        self.columns = [c for c in (_column(c) for c in (spec.get("columns") or [])[:MAX_COLUMNS]) if c]
        self.rows = kit.str_list(spec.get("rows"), max_items=MAX_ROWS, limit=24)
        if self.layout == "compare" and not any(c["facts"] for c in self.columns):
            columns, labels = _table(self.columns, spec.get("rows"), self.title)
            if columns:
                self.columns, self.rows = columns, labels[:MAX_ROWS]
        self.subtitle = kit.clean(spec.get("subtitle"), 40)
        self.facts = kit.str_list(spec.get("facts"), max_items=MAX_FACTS, limit=80)
        raw_stats = spec.get("stats")
        raw_stats = raw_stats if isinstance(raw_stats, (list, tuple)) else [raw_stats] if raw_stats else []
        self.stats = [s for s in (_stat(r) for r in raw_stats[:MAX_STATS * 2]) if s][:MAX_STATS]
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
        self.list_columns = 1  # the list layout: 1 or 2 columns of items (set by pages())

    # ------------------------------------------------------------ geometry
    @property
    def inner(self) -> pygame.Rect:
        """The panel inside its padding."""
        return kit.PANEL.inflate(-2 * kit.PAD, -2 * kit.PAD)

    def _text_right(self, pic: bool) -> int:
        return kit.PANEL.right - kit.PAD - (PIC_W + kit.GUTTER if pic else 0)

    def _note_h(self, width: int) -> int:
        if not self.note:
            return 0
        n = min(2, len(kit.wrap(self.note, BODY_PX, width)))
        return kit.line_h(BODY_PX) * (n - 1) + art.font(BODY_PX).get_height() + 14

    def _stat_rows(self, width: int) -> list[list[int]]:
        """The stat tiles in rows that fit ``width`` (at least STAT_MIN_W each)."""
        if not self.stats:
            return []
        per_row = max(1, min(len(self.stats), (width + STAT_GAP) // (STAT_MIN_W + STAT_GAP)))
        rows = -(-len(self.stats) // per_row)
        per_row = -(-len(self.stats) // rows)  # 3 tiles in 2 rows: 2 + 1; 4: 2 + 2
        return [list(range(i, min(i + per_row, len(self.stats)))) for i in range(0, len(self.stats), per_row)]

    def _stats_h(self, width: int) -> int:
        rows = self._stat_rows(width)
        return len(rows) * (STAT_H + STAT_GAP) if rows else 0

    def _draw_stats(self, surf: pygame.Surface, left: int, width: int, bottom: int, fade: float) -> int:
        """The stat tiles, their rows ending at ``bottom``; returns where they start."""
        rows = self._stat_rows(width)
        y = bottom - len(rows) * (STAT_H + STAT_GAP) + STAT_GAP
        top = y
        for row in rows:
            w = min(STAT_MAX_W, (width - STAT_GAP * (len(row) - 1)) // len(row))
            x = left + (width - (w * len(row) + STAT_GAP * (len(row) - 1))) // 2
            for i in row:
                label, value = self.stats[i]
                tile = stat_tile(label, value, w, kit.ACCENTS[(i + 1) % len(kit.ACCENTS)])
                kit.blit_alpha(surf, tile, (x, y + int((1 - fade) * 16)), fade)
                x += w + STAT_GAP
            y += STAT_H + STAT_GAP
        return top

    # ------------------------------------------------------------ pages
    def pages(self) -> list[Any]:
        """What each page shows (layout-specific); one page for the layouts that never page."""
        pic = self.slot.wanted
        got = self._pages.get(pic)
        if got is None:
            got = self._paginate(pic)
            self._pages[pic] = got
        return got

    def _paginate(self, pic: bool) -> list[Any]:
        inner = self.inner
        if self.layout == "list":
            return self._paginate_list(pic)
        if self.layout == "compare":
            n = max((len(c["facts"]) for c in self.columns), default=0)
            n = max(n, len(self.rows))
            counts = [self._row_lines(r) for r in range(n)]
            heights = [kit.line_h(CELL_PX) * k + ROW_GAP for k in counts]
            room = inner.h - self._head_h() - 6 - self._note_h(inner.w)
            pages = kit.pages_by_height(heights, counts, gap=0, room=room, max_lines=MAX_LINES - 1)
            if len(pages) > 1:  # the header row counts as a line; the dots need room
                pages = kit.pages_by_height(heights, counts, gap=0, room=room - DOTS_H, max_lines=MAX_LINES - 1)
            return pages or [[]]
        if self.layout == "profile":
            left, width = self._profile_text(pic)
            lines = [kit.wrap(f, FACT_PX, width - FACT_INDENT)[:3] for f in self.facts]
            heights = [kit.line_h(FACT_PX) * (len(ls) - 1) + art.font(FACT_PX).get_height() for ls in lines]
            room = inner.h - self._subtitle_h() - (self._stats_h(inner.w) + 8 if self.stats else 0)
            budget = MAX_LINES - len(self._stat_rows(inner.w))
            pages = kit.pages_by_height(heights, [len(ls) for ls in lines], gap=FACT_GAP, room=room,
                                            max_lines=budget)
            if len(pages) > 1:
                pages = kit.pages_by_height(heights, [len(ls) for ls in lines], gap=FACT_GAP, room=room - DOTS_H,
                                                max_lines=budget)
            return [[(i, lines[i]) for i in page] for page in pages] or [[]]
        return [None]

    def _paginate_list(self, pic: bool) -> list[Any]:
        """Items a page; two columns of short items when there is no picture (8 planets on one page)."""
        inner = self.inner
        room = inner.h - self._note_h(self._text_right(pic) - inner.x)
        half = (inner.w - LIST_GUTTER) // 2 - LIST_INDENT
        two = (not pic and len(self.items) >= TWO_COLUMNS_FROM
               and all(len(kit.wrap(i, LIST_PX, half)) == 1 for i in self.items))
        self.list_columns = 2 if two else 1
        if two:
            per_page = min(MAX_LINES, 2 * ((room + LIST_GAP) // (kit.line_h(LIST_PX) + LIST_GAP)))
            n_pages = -(-len(self.items) // per_page)
            per_page = -(-len(self.items) // n_pages)  # evened out: 12 items are 6 + 6
            return [[(i, (self.items[i],)) for i in range(s, min(s + per_page, len(self.items)))]
                    for s in range(0, len(self.items), per_page)]
        width = self._text_right(pic) - (inner.x + LIST_INDENT)
        lines = [kit.wrap(i, LIST_PX, width)[:2] for i in self.items]
        heights = [kit.line_h(LIST_PX) * (len(ls) - 1) + art.font(LIST_PX).get_height() for ls in lines]
        counts = [len(ls) for ls in lines]
        pages = kit.pages_by_height(heights, counts, gap=LIST_GAP, room=room)
        if len(pages) > 1:
            pages = kit.pages_by_height(heights, counts, gap=LIST_GAP, room=room - DOTS_H)
        return [[(i, lines[i]) for i in page] for page in pages]

    def _col_geometry(self) -> tuple[int, int, int]:
        """(x of the first value column, column width, label column width) for the compare table."""
        inner = self.inner
        n = max(1, len(self.columns))
        label_w = (LABEL_W if n <= 3 else LABEL_W_NARROW) if self.rows else 0
        col_w = (inner.w - label_w - COL_GAP * (n - 1)) // n
        return inner.x + label_w, col_w, label_w

    def _head_lines(self) -> int:
        """Lines the column names need (two when a name doesn't fit its column on one)."""
        col_w = self._col_geometry()[1]
        return max((min(2, len(kit.wrap(c["name"], HEAD_PX, col_w - 20))) for c in self.columns), default=1)

    def _head_h(self) -> int:
        return HEAD_H if self._head_lines() == 1 else HEAD_H + kit.line_h(HEAD_PX) - 4

    def _cell_lines(self, col: int, row: int) -> tuple[str, ...]:
        facts = self.columns[col]["facts"]
        text = facts[row] if row < len(facts) else "-"
        return kit.wrap(text, CELL_PX, self._col_geometry()[1] - 16)[:2]

    def _row_lines(self, row: int) -> int:
        label = kit.wrap(self.rows[row], BODY_PX, self._col_geometry()[2] - 14)[:2] if row < len(self.rows) else ()
        return max([len(label)] + [len(self._cell_lines(c, row)) for c in range(len(self.columns))])

    def _profile_text(self, pic: bool) -> tuple[int, int]:
        """(left, width) of the profile's text column (right of the picture when there is one)."""
        inner = self.inner
        left = inner.x + (PIC_W + kit.GUTTER if pic else 0)
        return left, inner.right - left

    def _subtitle_h(self) -> int:
        return kit.chip(self.subtitle, art.GRAPE, size=SUBTITLE_PX).get_height() + 16 if self.subtitle else 0

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

    def _said_stats(self) -> str:
        if not self.stats:
            return ""
        return " (" + "; ".join(f"{label}: {value}" if label else value for label, value in self.stats) + ")"

    def describe(self) -> str:
        title = f"{self.title}: " if self.title else ""
        pages = self.pages()
        paging = f" (page {self.page + 1} of {len(pages)})" if len(pages) > 1 else ""
        if self.layout == "number":
            return f"{title}{self.value} {self.label}".strip() + self._said_stats()
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
            return f"{self.title}{sub}: {facts}{paging}" + self._said_stats()
        if self.layout == "score":
            if len(self.teams) == 2:
                a, b = (f"{t['name']} {t['score']}" + (f" ({t['detail']})" if t.get("detail") else "")
                        for t in self.teams)
                status = f", {self.status_text}" if self.status_text else ""
                return f"{a}, {b}{status}"
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
        kit.page_dots(surf, len(pages), self.page, (kit.PANEL.centerx, kit.PANEL.bottom - 20))

    def _pic_rect(self, top: int | None = None, *, left: bool = False) -> pygame.Rect:
        inner = self.inner
        top = inner.y if top is None else top
        x = inner.x if left else inner.right - PIC_W
        return pygame.Rect(x, top, PIC_W, inner.bottom - top)

    def _enter(self, delay: float = 0.1) -> float:
        return art.ease_out_cubic(kit.appear(self.t, max(self.page_t, 0.0) + delay))

    def _draw_note(self, surf: pygame.Surface, cx: int, y: int, width: int, fade: float,
                   align: str = "center") -> int:
        """The note (soft, wrapped, at most two lines) from ``y``; returns its bottom."""
        if not self.note:
            return y
        lines = kit.wrap(self.note, BODY_PX, width)[:2]
        img = kit.text_block(lines, BODY_PX, kit.TEXT_SOFT, align=align)
        r = img.get_rect(midtop=(cx, y)) if align == "center" else img.get_rect(topleft=(cx, y))
        kit.blit_alpha(surf, img, r.topleft, fade)
        return y + img.get_height()

    def _draw_bottom_note(self, surf: pygame.Surface, left: int, width: int, fade: float) -> None:
        """The note at the bottom of the panel (list, compare, item), above the page dots."""
        if not self.note:
            return
        lines = kit.wrap(self.note, BODY_PX, width)[:2]
        img = kit.text_block(lines, BODY_PX, kit.TEXT_SOFT)
        dots = DOTS_H if len(self.pages()) > 1 else 0
        kit.blit_alpha(surf, img, (left, self.inner.bottom - dots - img.get_height()), fade)

    def _draw_number(self, surf: pygame.Surface) -> None:
        pic = self.slot.wanted
        inner = self.inner
        left, right = inner.x, self._text_right(pic)
        width = right - left
        cx = (left + right) // 2
        label_lines = kit.wrap(self.label, LABEL_PX, width)[:2] if self.label else []
        label = kit.text_block(label_lines, LABEL_PX, kit.TEXT, align="center") if label_lines else None
        note_h = self._note_h(width)
        stats_h = self._stats_h(width) + 8 if self.stats else 0
        rest = (label.get_height() + 8 if label else 0) + note_h
        sizes = [s for s in VALUE_SIZES
                 if art.font(s).get_height() + 8 + rest + stats_h <= inner.h] or [VALUE_SIZES[-1]]
        big = kit.outlined_fit(self.value, sizes, width, art.inkier(art.TANGERINE, 0.04), art.INK)
        total = big.get_height() + rest
        room = inner.h - stats_h
        top = inner.y + max(0, (room - total) // 2 - 6)
        k = art.ease_out_back(kit.appear(self.t, 0.1, 0.55))
        art.blit_center(surf, art.scaled(big, max(0.05, k)), (cx, top + big.get_height() // 2))
        y = top + big.get_height() + 4
        fade = self._enter(0.35)
        if label:
            kit.blit_alpha(surf, label, label.get_rect(midtop=(cx, y)).topleft, fade)
            y += label.get_height() + 12
        self._draw_note(surf, cx, y, width, fade)
        if self.stats:
            self._draw_stats(surf, left, width, inner.bottom, self._enter(0.6))
        if pic:
            self.slot.draw(surf, self._pic_rect(), self.t)

    def _draw_list(self, surf: pygame.Surface) -> None:
        pic = self.slot.wanted
        inner = self.inner
        page = self.pages()[self.page]
        pitch = kit.line_h(LIST_PX)
        first = art.font(LIST_PX).get_height()
        dots = DOTS_H if len(self.pages()) > 1 else 0
        room = inner.h - dots - self._note_h(self._text_right(pic) - inner.x)
        if self.list_columns == 2:
            rows = -(-len(page) // 2)
            columns = [page[:rows], page[rows:]]
            col_w = (inner.w - LIST_GUTTER) // 2
            total = rows * pitch - (pitch - first) + LIST_GAP * (rows - 1)
            top = inner.y + max(0, (room - total) // 2)
            for c, col in enumerate(columns):
                for n, (i, lines) in enumerate(col):
                    self._draw_list_item(surf, i, lines, inner.x + c * (col_w + LIST_GUTTER),
                                         top + n * (pitch + LIST_GAP), c * rows + n)
        else:
            heights = [pitch * (len(ls) - 1) + first for _, ls in page]
            total = sum(heights) + LIST_GAP * max(0, len(page) - 1)
            y = inner.y + max(0, (room - total) // 2)
            for n, ((i, lines), h) in enumerate(zip(page, heights)):
                self._draw_list_item(surf, i, lines, inner.x, y, n)
                y += h + LIST_GAP
        self._draw_bottom_note(surf, inner.x, self._text_right(pic) - inner.x, self._enter(0.3))
        if pic:
            self.slot.draw(surf, self._pic_rect(), self.t)

    def _draw_list_item(self, surf: pygame.Surface, i: int, lines: tuple[str, ...], x: int, y: int, n: int) -> None:
        fade = self._enter(0.06 * n)
        dx = int((1 - fade) * 30)
        color = kit.ACCENTS[i % len(kit.ACCENTS)]
        first = art.font(LIST_PX).get_height() // 2
        if self.ordered:
            badge = kit.number_badge(i + 1, color, 23)
            kit.blit_alpha(surf, badge, badge.get_rect(center=(x + 24, y + first + 1)).topleft, fade)
        else:
            kit.blit_alpha(surf, art.circle(11, color), (x + 13, y + first - 11), fade)
        kit.blit_alpha(surf, kit.text_block(lines, LIST_PX, pitch=kit.line_h(LIST_PX)), (x + LIST_INDENT + dx, y),
                       fade)

    def _draw_compare(self, surf: pygame.Surface) -> None:
        """Candy column cards (name on top) holding this page's rows; row labels on the left."""
        inner = self.inner
        x0, col_w, label_w = self._col_geometry()
        rows = self.pages()[self.page]
        pitch = kit.line_h(CELL_PX)
        heights = [pitch * self._row_lines(r) for r in rows]
        head_h = self._head_h()
        body_h = sum(heights) + ROW_GAP * len(heights) + 6
        total = head_h + body_h
        note_h = self._note_h(inner.w)
        avail = inner.h - (DOTS_H if len(self.pages()) > 1 else 0) - note_h
        top = inner.y + max(0, (avail - total) // 2)
        line_c = art.mix(art.INK_SOFT, art.WHITE, 0.55)
        for c, col in enumerate(self.columns):
            color = kit.ACCENTS[c]
            x = x0 + c * (col_w + COL_GAP)
            surf.blit(art.rounded_rect(col_w, total, 20, art.mix(color, art.WHITE, 0.84)), (x, top))
            surf.blit(art.rounded_rect(col_w, head_h, 20, art.inkier(color, 0.22), shine=True), (x, top))
            names = kit.wrap(col["name"], HEAD_PX, col_w - 20)
            if len(names) > 2:
                names = (names[0], kit.ellipsize(" ".join(names[1:]), HEAD_PX, col_w - 20))
            name = kit.text_block(list(names), HEAD_PX, art.WHITE, align="center", pitch=kit.line_h(HEAD_PX) - 4)
            surf.blit(name, name.get_rect(center=(x + col_w // 2, top + head_h // 2 + 1)))
        y = top + head_h + ROW_GAP
        fade = self._enter()
        for n, (r, h) in enumerate(zip(rows, heights)):
            if r < len(self.rows):
                label = kit.text_block(kit.wrap(self.rows[r], BODY_PX, label_w - 14)[:2], BODY_PX, kit.TEXT_SOFT)
                kit.blit_alpha(surf, label, (inner.x, y + 2), fade)
            for c in range(len(self.columns)):
                x = x0 + c * (col_w + COL_GAP)
                cell = kit.text_block(self._cell_lines(c, r), CELL_PX, kit.TEXT, align="center")
                kit.blit_alpha(surf, cell, cell.get_rect(midtop=(x + col_w // 2, y)).topleft, fade)
                if n < len(rows) - 1:
                    yy = y + h + ROW_GAP // 2 - 2
                    pygame.draw.line(surf, line_c, (x + 14, yy), (x + col_w - 14, yy), 2)
            y += h + ROW_GAP
        self._draw_bottom_note(surf, inner.x, inner.w, self._enter(0.3))

    def _draw_profile(self, surf: pygame.Surface) -> None:
        """The picture on the left, the subtitle and facts beside it, the stats in a row under both."""
        pic = self.slot.wanted
        inner = self.inner
        left, width = self._profile_text(pic)
        stats_top = inner.bottom
        if self.stats:
            stats_top = self._draw_stats(surf, inner.x, inner.w, inner.bottom - (DOTS_H if len(self.pages()) > 1
                                                                                  else 0), self._enter(0.4)) - 8
        if pic:
            rect = self._pic_rect(left=True)
            rect.h = stats_top - rect.y
            self.slot.draw(surf, rect, self.t)
        y = inner.y
        if self.subtitle:
            c = kit.chip(self.subtitle, art.GRAPE, size=SUBTITLE_PX)
            surf.blit(c, (left, y))
            y += c.get_height() + 16
        page = self.pages()[self.page]
        pitch = kit.line_h(FACT_PX)
        first = art.font(FACT_PX).get_height()
        heights = [pitch * (len(ls) - 1) + first for _, ls in page]
        total = sum(heights) + FACT_GAP * max(0, len(page) - 1)
        bottom = stats_top if self.stats else inner.bottom - (DOTS_H if len(self.pages()) > 1 else 0)
        y += max(0, min(40, (bottom - y - total) // 2))
        for n, ((i, lines), h) in enumerate(zip(page, heights)):
            fade = self._enter(0.1 * n)
            color = kit.ACCENTS[(i + 2) % len(kit.ACCENTS)]
            kit.blit_alpha(surf, art.star(15, color), (left, y + first // 2 - 15), fade)
            kit.blit_alpha(surf, kit.text_block(lines, FACT_PX, pitch=pitch), (left + FACT_INDENT, y), fade)
            y += h + FACT_GAP

    def _draw_score(self, surf: pygame.Surface) -> None:
        if len(self.teams) != 2:
            return
        inner = self.inner
        cx = kit.PANEL.centerx
        half = inner.w // 2
        k = art.ease_out_back(kit.appear(self.t, 0.15, 0.55))
        details = any(t.get("detail") for t in self.teams)
        name_h = max(len(kit.wrap(t["name"], TEAM_PX, half - 40)[:2]) for t in self.teams) * kit.line_h(TEAM_PX)
        score_h = art.font(SCORE_SIZES[0]).get_height()
        detail_h = 2 * kit.line_h(BODY_PX) if details else 0
        status_h = (kit.chip(self.status_text, art.MINT, size=BODY_PX).get_height() + 12) if self.status_text else 0
        total = name_h + score_h + detail_h + status_h + self._note_h(inner.w)
        top = inner.y + max(0, (inner.h - total) // 2)
        y_score = top + name_h + score_h // 2
        fade = self._enter(0.3)
        for side, team in enumerate(self.teams):
            color = kit.ACCENTS[side]
            tx = cx - half // 2 if side == 0 else cx + half // 2
            name = kit.text_block(kit.wrap(team["name"], TEAM_PX, half - 40)[:2], TEAM_PX, kit.TEXT, align="center")
            surf.blit(name, name.get_rect(midtop=(tx, top)))
            score = kit.outlined_fit(team["score"], SCORE_SIZES, half - 80, art.inkier(color, 0.05), art.INK)
            art.blit_center(surf, art.scaled(score, max(0.05, k)), (tx, y_score))
            if team.get("detail"):
                lines = kit.wrap(team["detail"], BODY_PX, half - 40)[:2]
                img = kit.text_block(lines, BODY_PX, kit.TEXT_SOFT, align="center")
                kit.blit_alpha(surf, img, img.get_rect(midtop=(tx, y_score + score_h // 2 + 4)).topleft, fade)
        art.blit_center(surf, art.rounded_rect(56, 18, 9, art.mix(art.INK_SOFT, art.WHITE, 0.25), border=art.INK,
                                               border_w=3), (cx, y_score))
        y = y_score + score_h // 2 + detail_h + 12
        if self.status_text:
            pill = kit.chip(self.status_text, art.MINT, size=BODY_PX)
            surf.blit(pill, pill.get_rect(midtop=(cx, y)))
            y += pill.get_height() + 12
        self._draw_note(surf, cx, y, inner.w, fade)

    def _draw_item(self, surf: pygame.Surface) -> None:
        pic = self.slot.wanted
        inner = self.inner
        left, right = inner.x, self._text_right(pic)
        y = inner.y + 4
        if self.kicker:
            c = kit.chip(self.kicker, art.STRAWBERRY, size=BODY_PX)
            if c.get_width() > right - left:
                c = kit.chip(kit.ellipsize(self.kicker, BODY_PX, right - left - 40), art.STRAWBERRY, size=BODY_PX)
            surf.blit(c, (left, y))
            y += c.get_height() + 12
        bottom = inner.bottom - (self._note_h(right - left) + 8 if self.note else 0)
        size, lines = item_fit(self.text, right - left - 8, bottom - y)
        img = kit.text_block(lines, size)
        ty = y + max(0, (bottom - y - img.get_height()) // 2)
        fade = self._enter(0.2)
        kit.blit_alpha(surf, img, (left + 4 + int((1 - fade) * 24), ty), fade)
        self._draw_bottom_note(surf, left + 4, right - left - 4, fade)
        if pic:
            self.slot.draw(surf, self._pic_rect(), self.t)


def item_fit(text: str, width: int, height: int) -> tuple[int, tuple[str, ...]]:
    """The item's text as big as fits ``width`` x ``height`` (ITEM_SIZES, never under BODY_PX, at most
    MAX_LINES lines), with its lines (cut short when even BODY_PX doesn't fit)."""
    lines: tuple[str, ...] = ()
    for size in ITEM_SIZES:
        pitch = kit.line_h(size)
        max_lines = max(1, min(MAX_LINES, (height - art.font(size).get_height()) // pitch + 1))
        lines = kit.wrap(text, size, width)
        if len(lines) <= max_lines:
            return size, lines
    size = ITEM_SIZES[-1]
    max_lines = max(1, min(MAX_LINES, (height - art.font(size).get_height()) // kit.line_h(size) + 1))
    if len(lines) > max_lines:
        lines = (*lines[:max_lines - 1], kit.ellipsize(" ".join(lines[max_lines - 1:]), size, width))
    return size, lines


def _fits(card: FactsCard, pic: bool) -> bool:
    """The card has what its layout shows (``pic``: a picture was asked for)."""
    return {
        "number": bool(card.value),
        "list": bool(card.items),
        "compare": len(card.columns) >= 2 and any(c["facts"] for c in card.columns),
        "profile": bool(card.title and (card.facts or pic)),
        "score": len(card.teams) == 2,
        "item": bool(card.text),
    }[card.layout]


FALLBACK_LAYOUTS = ("profile", "list", "item", "number")


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    """The card, or None when it has nothing to show. A layout without its content but with another's (a
    "profile" whose facts came as "items", an "item" with only a note) is shown in the layout that fits,
    rather than not at all: the model often mixes them up, and then no card at all came (and it tried again)."""
    pic = bool(kit.clean(spec.get("picture_query"), 80))
    card = FactsCard(spec, None)  # (no host yet: nothing is fetched while it is checked)
    if _fits(card, pic):
        return FactsCard(spec, host)
    given = dict(spec)
    # a comparison whose rows are sentences this card couldn't split into its columns: shown as a list of them
    lines = [kit.clean(r, 60) for r in (spec.get("rows") or []) if isinstance(r, str) and len(r.split()) >= 3]
    given["items"] = given.get("items") or list(card.facts) or (lines if card.layout == "compare" else [])
    given.setdefault("facts", list(card.items[:3]))
    if not card.text:
        given["text"] = card.note or (card.facts[0] if card.facts else card.items[0] if card.items else "")
    for layout in dict.fromkeys((card.layout, *FALLBACK_LAYOUTS)):  # its own layout first, with what was given
        if _fits(FactsCard({**given, "layout": layout}, None), pic):
            log.info("facts card: the %s layout was missing its part; shown as %s (rows: %s)", card.layout, layout,
                     _row_marks(spec.get("rows")))
            return FactsCard({**given, "layout": layout}, host)
    fields = ", ".join(f"{k}={_shape(v)}" for k, v in sorted(spec.items()) if v not in (None, "", [], {})) or "none"
    # Whatever shape it came in, the card shows what it was given (the parent's ask, 2026-10-29): every bit of
    # its text as a list, rather than no card
    texts = list(dict.fromkeys(t for t in (kit.clean(s, 60) for s in _strings(
        {k: v for k, v in spec.items() if k not in NOT_TEXT})) if t))[:MAX_ITEMS]
    if texts:
        log.info("facts card: the %s layout couldn't be read (fields given: %s; rows: %s); shown as a list of its "
                 "text", card.layout, fields[:400], _row_marks(spec.get("rows")))
        layout = "list" if len(texts) > 1 else "item"
        return FactsCard({"layout": layout, "title": spec.get("title"), "items": texts, "text": texts[0],
                          "ordered": False}, host)
    log.info("facts card not made: the %s layout had nothing to show (fields given: %s)", card.layout, fields[:400])
    return None


NOT_TEXT = ("card", "layout", "title", "picture_query", "ordered", "status", "kicker")  # a caption alone isn't a card
NAME_KEYS = ("name", "title", *LABEL_KEYS)


def _strings(value: Any) -> list[str]:
    """Every bit of text in ``value``, in order: a record's name first, then "key: value" for its other text."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        out = [s for k in NAME_KEYS if isinstance(value.get(k), str) for s in [value[k]]]
        for k, v in value.items():
            if k in NAME_KEYS:
                continue
            scalar = isinstance(v, (str, int, float)) and not isinstance(v, bool)
            out += [f"{k}: {v}"] if scalar and k not in ("facts", "values", "items") else _strings(v)
        return out
    return []


_JOINS = re.compile(r"\b(vs|versus|while|whereas|but|and|or|compared)\b", re.I)


def _row_marks(rows: Any) -> str:
    """How each text row is put together (its word count, punctuation and joining words, never its words), so
    the log shows a format this card can't read yet."""
    if not isinstance(rows, (list, tuple)):
        return "none"
    out = []
    for r in rows[:4]:
        if not isinstance(r, str):
            out.append(type(r).__name__)
            continue
        marks = "".join(sorted({c for c in r if not c.isalnum() and not c.isspace()}))
        joins = "/".join(dict.fromkeys(j.lower() for j in _JOINS.findall(r)))
        out.append(f"{len(r.split())} words, marks {marks or '-'}" + (f", joined by {joins}" if joins else ""))
    return "; ".join(out) or "none"


register(CardType(
    kind="facts",
    description=(
        "A quick answer on screen, for everything without its own card. Pick a layout: "
        "\"number\" one big number (\"how tall is the Eiffel Tower\": value \"330 m\", label \"tall\"), with up "
        "to 4 key numbers as stats; "
        "\"list\" a list of up to 12 (\"planets in order\", \"top 5 ...\"); "
        "\"compare\" two to four things side by side (columns, optional row labels, up to 8 rows); "
        "\"profile\" a person, place or animal (picture_query, subtitle, 3-5 facts, up to 4 stats); "
        "\"score\" a sports score (two teams, who scored, status); "
        "\"item\" one item (\"today in history\", a quote, a single fact: kicker + text). "
        "The 7-inch screen has room for the useful details (stats, a note, more items), but keep every text "
        "short: the screen shows the gist, your voice says the rest."
    ),
    fields={
        "layout": {"type": "string", "enum": list(LAYOUTS), "description": "number | list | compare | profile | "
                                                                           "score | item"},
        "title": {"type": "string", "maxLength": 60,
                  "description": "What it is about, 1-5 words (\"Eiffel Tower\", \"Planets\")"},
        "value": {"type": "string", "maxLength": 24,
                  "description": "number: the number with its unit, short (\"330 m\", \"1889\", \"8 billion\")"},
        "label": {"type": "string", "maxLength": 40,
                  "description": "number: what the number is, 1-4 words (\"tall\", \"built in\")"},
        "note": {"type": "string", "maxLength": 100,
                 "description": "Optional one short extra line, at most 12 words, for number, list, compare, score "
                                "or item (\"as tall as an 81-storey building\", \"Both are big cats\")"},
        "stats": {"type": "array", "maxItems": MAX_STATS,
                  "items": {"type": ["string", "object"], "maxLength": 44,
                            "properties": {"label": {"type": "string", "maxLength": 22},
                                           "value": {"type": "string", "maxLength": 22}}},
                  "description": "Optional for number and profile: up to 4 key numbers, each \"Label: value\" "
                                 "with 1-2 words a side (\"Height: 5.5 m\", \"Lives: 25 years\", \"Built: 1889\")"},
        "items": {"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": MAX_ITEMS,
                  "description": "list: up to 12 items, each 1-6 words"},
        "ordered": {"type": "boolean", "description": "list: number the items (default true)"},
        # (lists and plain names are kept too: the card also reads a comparison written as a table)
        "columns": {"type": "array", "maxItems": MAX_COLUMNS, "description": "compare: 2-4 columns, each "
                    "an object {\"name\": \"Lion\", \"facts\": [\"190 kg\", \"Africa\"]} (the facts are needed: "
                    "names alone show nothing); the n-th facts line up as a row, up to 8 rows",
                    "items": {"type": ["object", "array", "string"], "items": {"type": "string"},
                              "properties": {"name": {"type": "string"}, "title": {"type": "string"},
                                             "facts": {"type": "array", "items": {"type": "string"}},
                                             "values": {"type": "array", "items": {"type": "string"}}}}},
        "rows": {"type": "array", "items": {"type": ["string", "object", "array"], "items": {"type": "string"},
                                            "additionalProperties": {"type": ["string", "array"],
                                                                     "items": {"type": "string"}}},
                 "description": "compare, optional: a label for each row of facts, 1-3 words (\"Weight\", "
                                "\"Lives in\"); the facts themselves go in columns, not here"},
        "subtitle": {"type": "string", "maxLength": 40,
                     "description": "profile: what it is, 1-4 words (\"Animal\", \"City in Japan\")"},
        "facts": {"type": "array", "items": {"type": "string", "maxLength": 80}, "maxItems": MAX_FACTS,
                  "description": "profile: 3-5 short facts, each at most 8 words"},
        "teams": {"type": "array", "maxItems": 2, "description": "score: [{\"name\": \"Arsenal\", \"score\": \"2\", "
                  "\"detail\": \"Saka 23', Havertz 70'\"}, {\"name\": \"Chelsea\", \"score\": \"1\"}] (detail, "
                  "optional: who scored, a few words)",
                  "items": {"type": "object", "properties": {"name": {"type": "string"}, "score": {"type": "string"},
                                                             "detail": {"type": "string", "maxLength": 60}}}},
        "status": {"type": "string", "description": "score: \"Full time\", \"Half time\", \"Live 63'\""},
        "kicker": {"type": "string", "description": "item: a short line above the text (\"Today in history\", "
                                                    "\"24 October 1945\")"},
        "text": {"type": "string", "description": "item: the item itself, at most 30 words"},
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
     "note": "About as tall as an 81-storey building", "picture_query": "Eiffel Tower",
     "stats": ["Built: 1889", "Steps: 1,665", "Visitors: 7 million a year"]},
    {"card": "facts", "layout": "number", "title": "People on Earth", "value": "8.2 billion",
     "label": "people", "note": "and growing by about 70 million a year",
     "stats": ["Most: India", "Babies a day: 365,000", "In cities: 57%", "Under 15: 25%"]},
    {"card": "facts", "layout": "list", "title": "Planets in order",
     "items": ["Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"],
     "note": "Jupiter is the biggest; Mercury is the smallest"},
    {"card": "facts", "layout": "list", "title": "Good first finger foods", "ordered": False,
     "picture_query": "banana", "items": ["Soft banana pieces", "Steamed carrot sticks", "Avocado slices",
                                          "Toast strips", "Ripe pear slices", "Soft scrambled egg"]},
    {"card": "facts", "layout": "compare", "title": "Lion or tiger?", "rows": ["Weight", "Lives in", "Lives"],
     "columns": [{"name": "Lion", "facts": ["190 kg", "Africa", "In groups"]},
                 {"name": "Tiger", "facts": ["220 kg", "Asia", "Alone"]}]},
    {"card": "facts", "layout": "compare", "title": "Milk for babies",
     "columns": [{"name": "Breast milk", "facts": ["Changes as baby grows", "Always ready"]},
                 {"name": "Formula", "facts": ["Same every feed", "Others can help feed"]},
                 {"name": "Cow's milk", "facts": ["Main drink from 12 months", "Fine in food before"]}]},
    {"card": "facts", "layout": "compare", "title": "The big cats", "note": "Tigers are the biggest of them all",
     "rows": ["Weight", "Lives in", "Spots?", "Roars?", "Lives"],
     "columns": [{"name": "Lion", "facts": ["190 kg", "Africa", "No", "Yes", "In prides"]},
                 {"name": "Tiger", "facts": ["220 kg", "Asia", "Stripes", "Yes", "Alone"]},
                 {"name": "Leopard", "facts": ["60 kg", "Africa, Asia", "Yes", "Yes", "Alone"]},
                 {"name": "Cheetah", "facts": ["50 kg", "Africa", "Yes", "No", "Alone or pairs"]}]},
    {"card": "facts", "layout": "profile", "title": "Giraffe", "subtitle": "Animal", "picture_query": "giraffe",
     "facts": ["Tallest animal on land", "Sleeps only about 30 minutes a day", "Its tongue is about 50 cm long",
               "Lives in African savannas"],
     "stats": ["Height: 5.5 m", "Weight: 1,200 kg", "Lives: 25 years", "Top speed: 60 km/h"]},
    {"card": "facts", "layout": "score",
     "teams": [{"name": "Arsenal", "score": "2", "detail": "Saka 23', Havertz 70'"},
               {"name": "Chelsea", "score": "1", "detail": "Palmer 81'"}],
     "status": "Full time", "note": "Premier League"},
    {"card": "facts", "layout": "item", "title": "Today in history", "kicker": "24 October 1945",
     "text": "The United Nations was founded, to help countries work together for peace.",
     "picture_query": "United Nations", "note": "51 countries signed; today there are 193"},
    {"card": "facts", "layout": "item", "title": "Fun fact", "text": "Octopuses have three hearts and blue blood.",
     "note": "Two hearts pump blood to the gills"},
    {"card": "facts", "layout": "list", "title": "Baby names this year",  # the longest list: two pages
     "items": ["Olivia", "Noah", "Amelia", "Muhammad", "Isla", "Oliver", "Lily", "George", "Freya", "Leo",
               "Ava", "Arthur"]},
]

__all__ = ["LAYOUTS", "SAMPLES", "FactsCard"]
