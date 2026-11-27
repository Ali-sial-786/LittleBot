"""The "recipe" card: cooking one step at a time, in big letters, readable from across the kitchen.

First the ingredients in two columns (14 short ones fit on one page; more are paged), with the serves /
time / oven chips and, when they all fit beside it on one page, the dish's picture. Then one step per screen
in big letters ("Step 3 of 8", a progress row), with its tip in a lemon band (``tips``) and the next step
previewed underneath ("Next: Whisk in the eggs"); or two short steps per screen with ``steps_at_once: 2`` (a
pair that wouldn't fit big is shown one step at a time). A time in a step ("simmer for 10 minutes") is shown
as a highlighted pill in the text and as a timer chip above it; the director may start a timer card for it
(``status()["timer_seconds"]``), but this card doesn't depend on that. The "Enjoy!" screen shows the dish's
picture too.

The voice or a tap moves on: ``next`` / ``prev`` / ``step {n}`` / ``ingredients`` / ``close``; a tap on
the left third goes back, anywhere else goes on. When moved by a tap, the card tells the voice what it
shows now. The card is sticky (the parent walks away mid-recipe and it must not vanish when the chat
ends): it is done after "close", a while after the last step (:data:`DONE_AFTER_LAST_S`) or after a
long time without being touched (:data:`IDLE_DONE_S`).
"""

from __future__ import annotations

import math
import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import (
    ADD_PICTURE_ACTION,
    BODY_PX,
    MAX_LINES,
    STEP_PX,
    Card,
    CardType,
    register,
)
from littlebot.screens.cards import _kit as kit

MAX_STEPS = 40
MAX_INGREDIENTS = 30
STEP_SIZES = (76, 68, 60, 54, STEP_PX)  # one step a screen: as big as it fits (with its tip and the "Next:" line)
PAIR_SIZES = (64, 56, 52, STEP_PX)  # two steps a screen
INGREDIENT_SIZES = (44, 40, BODY_PX)  # the biggest that needs the fewest pages
INGREDIENT_PX = INGREDIENT_SIZES[0]
NEXT_PX = BODY_PX  # "Next: Whisk in the eggs" under the step
TIP_PX = BODY_PX
TIP_LINES = 2
TIP_CHARS = 80
ING_PIC_W = 300  # the dish's picture beside the ingredients
# The panel (kit.PANEL, 992 x 456 on the 1024x600 screen): a row of chips at the top, the content, then the
# back / next buttons with the progress row (or page dots) at the bottom.
ROW_Y = kit.PANEL.y + kit.PAD + 22  # the chips' centre line
CONTENT_TOP = ROW_Y + 36
NAV_Y = kit.PANEL.bottom - 38  # the buttons' centre line
CONTENT_H = NAV_Y - 36 - CONTENT_TOP  # 296 px: 14 one-line ingredients at 40 px, in two columns
COL_GAP = 28  # between the two ingredient columns
BULLET_W = 36  # an ingredient's coloured dot and the space after it
ITEM_GAP = 8
NEXT_H = 50  # the "Next:" band
EXTRA_GAP = 12
TIP_PAD = 9
TIP_INSET = 66  # the tip's text, right of its bulb
BADGE_INSET = 64  # a step's text right of its number badge (two steps a screen)
PAIR_GAP = 28
OVEN_WORDS = re.compile(r"\b(?:oven|bake[sd]?|baking|roast(?:ed|ing)?|preheat|grill)\b", re.IGNORECASE)
DONE_AFTER_LAST_S = 300.0  # on the last step (or "enjoy") this long without a touch: done
FINISHED_DONE_S = 45.0  # the "enjoy!" screen goes after this long
IDLE_DONE_S = 3600.0  # anywhere: an hour without a touch and the card is done

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
              "twenty-five": 25, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
_UNIT_S = {"s": 1, "m": 60, "h": 3600}  # by the unit's first letter
_NUM = r"(?:\d+(?:\.\d+)?|" + "|".join(sorted((re.escape(w) for w in _NUM_WORDS), key=len, reverse=True)) + r")"
_UNIT = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"
TIMER_RE = re.compile(
    rf"\b(?:(?:about|around|another|for)\s+)?(?:half an hour|an? (?:hour|minute)(?: and a half)?"
    rf"|{_NUM}(?:\s*(?:-|–|to|or)\s*{_NUM})?\s*(?:more\s+)?{_UNIT}(?:\s+(?:and\s+)?{_NUM}\s*{_UNIT})?)\b",
    re.IGNORECASE)


def find_timer(text: str) -> tuple[int, int, int] | None:
    """(start, end, seconds) of the first cooking time in ``text`` ("10 minutes", "1 hour 20 mins",
    "half an hour", "5-7 minutes": the shorter time, check early), or None."""
    m = TIMER_RE.search(text)
    if m is None:
        return None
    phrase = m.group(0).lower()
    start = m.start() + (len(m.group(0)) - len(re.sub(r"^(?:about|around|another|for)\s+", "", m.group(0), flags=re.IGNORECASE)))
    if "half an hour" in phrase:
        return start, m.end(), 1800
    if re.search(r"\ban? hour and a half\b", phrase):
        return start, m.end(), 5400
    total = 0.0
    for num, unit in re.findall(rf"({_NUM})(?:\s*(?:-|–|to|or)\s*{_NUM})?\s*(?:more\s+)?({_UNIT})", phrase):
        n = float(num) if num[0].isdigit() else _NUM_WORDS.get(num, 0)
        total += n * _UNIT_S[unit[0].lower()]
    if total == 0 and re.search(r"\ban? (hour|minute)\b", phrase):
        total = 3600 if "hour" in phrase else 60
    return (start, m.end(), int(total)) if total > 0 else None


def short_time(seconds: int) -> str:
    """600 -> "10 min", 5400 -> "1 h 30 min"."""
    if seconds < 60:
        return f"{seconds} s"
    h, m = divmod(round(seconds / 60), 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


# ---------------------------------------------------------------- rich step text (a timer as a pill)
Token = tuple[str, bool, bool]  # (text, is a timer, glued to the token before: punctuation after a timer)


def tokens(text: str) -> list[Token]:
    found = find_timer(text)
    if found is None:
        return [(w, False, False) for w in text.split()]
    a, b, _ = found
    after = text[b:]
    glue = re.match(r"[^\s\w]+", after)  # "10 minutes," keeps its comma right after the pill
    out = [(w, False, False) for w in text[:a].split()] + [(text[a:b].strip(), True, False)]
    if glue:
        out.append((glue.group(0), False, True))
        after = after[glue.end():]
    return out + [(w, False, False) for w in after.split()]


def _tok_w(tok: Token, size: int) -> int:
    return kit.text_w(tok[0], size) + (24 if tok[1] else 0)


def rich_wrap(toks: list[Token], size: int, width: int) -> list[list[Token]]:
    space = kit.text_w(" ", size)
    lines: list[list[Token]] = []
    cur: list[Token] = []
    used = 0
    for tok in toks:
        if not tok[1] and kit.text_w(tok[0], size) > width:  # a huge word: let the plain wrapper split it
            for part in kit.wrap(tok[0], size, width):
                if cur:
                    lines.append(cur)
                cur, used = [(part, False, False)], kit.text_w(part, size)
            continue
        w = _tok_w(tok, size)
        gap = 0 if (tok[2] or not cur) else space
        if cur and not tok[2] and used + gap + w > width:
            lines.append(cur)
            cur, used, gap = [], 0, 0
        used += gap + w
        cur.append(tok)
    if cur:
        lines.append(cur)
    return lines


def _segments(line: list[Token]) -> list[tuple[str, bool, bool]]:
    """A line as runs to render: (text, is a timer pill, follows the previous run without a space)."""
    out: list[tuple[str, bool, bool]] = []
    for text, timer, glued in line:
        if out and not timer and not out[-1][1]:  # plain after plain: one run
            out[-1] = (out[-1][0] + ("" if glued else " ") + text, False, out[-1][2])
        else:
            out.append((text, timer, glued))
    return out


def rich_sprite(lines: list[list[Token]], size: int) -> pygame.Surface:
    """Wrapped step text as one cached sprite, the timer phrase on a lemon pill."""
    key = ("recipe_rich", tuple(tuple(ln) for ln in lines), size)

    def make() -> pygame.Surface:
        f = art.font(size)
        pitch = kit.line_h(size)
        space = f.size(" ")[0]
        width = max((sum(_tok_w(t, size) for t in ln) + space * (len(ln) - 1) for ln in lines), default=1)
        out = pygame.Surface((max(1, width + 4), pitch * (len(lines) - 1) + f.get_height() + 8), pygame.SRCALPHA)
        pill_h = f.get_height() + 6
        for n, ln in enumerate(lines):
            x, y = 0, n * pitch + 3
            for k, (text, timer, glued) in enumerate(_segments(ln)):
                if k and not glued:
                    x += space
                img = f.render(text, True, art.ink(kit.TEXT if not timer else art.INK))
                if timer:
                    pill = art.rounded_rect(img.get_width() + 24, pill_h, pill_h // 2, art.mix(art.LEMON, art.WHITE, 0.25),
                                            border=art.inkier(art.LEMON, 0.35), border_w=2)
                    out.blit(pill, (x, y - 3))
                    out.blit(img, (x + 12, y + 1))
                    x += pill.get_width()
                else:
                    out.blit(img, (x, y))
                    x += img.get_width()
        return out

    return art.cached(key, make)


def clock_icon(r: int = 13, color: art.Color = art.INK) -> pygame.Surface:
    key = ("recipe_clock", r, color)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = r * k
            pygame.draw.circle(s, color, (c, c), c)
            pygame.draw.circle(s, art.WHITE, (c, c), c - 3 * k)
            pygame.draw.line(s, color, (c, c), (c, c - 0.62 * c), int(3 * k))
            pygame.draw.line(s, color, (c, c), (c + 0.5 * c, c), int(3 * k))

        return art.supersampled((2 * r, 2 * r), draw)

    return art.cached(key, make)


# ---------------------------------------------------------------- the card
def flame_icon(r: int = 13) -> pygame.Surface:
    """A little flame (the oven chip)."""
    key = ("recipe_flame", r)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            w, h = 2 * r * k, 2 * r * k
            outer = [(w * 0.5, h * 0.02), (w * 0.82, h * 0.42), (w * 0.9, h * 0.66), (w * 0.78, h * 0.88),
                     (w * 0.5, h * 0.98), (w * 0.22, h * 0.88), (w * 0.1, h * 0.66), (w * 0.2, h * 0.4),
                     (w * 0.34, h * 0.52)]
            pygame.draw.polygon(s, art.inkier(art.TANGERINE, 0.35), outer)
            inner = [(w * 0.5, h * 0.42), (w * 0.68, h * 0.66), (w * 0.64, h * 0.84), (w * 0.5, h * 0.9),
                     (w * 0.36, h * 0.84), (w * 0.32, h * 0.68)]
            pygame.draw.polygon(s, art.LEMON, inner)

        return art.supersampled((2 * r, 2 * r), draw)

    return art.cached(key, make)


def bulb_icon(r: int = 15) -> pygame.Surface:
    """A small light bulb (a step's tip)."""
    key = ("recipe_bulb", r)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            c = r * k
            pygame.draw.circle(s, art.inkier(art.LEMON, 0.45), (c, c * 0.8), c * 0.72)
            pygame.draw.circle(s, art.LEMON, (c, c * 0.8), c * 0.58)
            pygame.draw.circle(s, art.WHITE, (c * 0.78, c * 0.6), c * 0.16)
            base = pygame.Rect(c * 0.64, c * 1.38, c * 0.72, c * 0.5)
            pygame.draw.rect(s, art.INK_SOFT, base, border_radius=int(c * 0.14))

        return art.supersampled((2 * r, 2 * r), draw)

    return art.cached(key, make)


def icon_chip(label: str, icon: pygame.Surface, fill: art.Color, edge: art.Color) -> pygame.Surface:
    """A rounded label with a little drawn icon in front ("20 min" with a clock, "Oven 180°C" with a flame)."""
    key = ("recipe_icon_chip", label, id(icon), fill, edge)

    def make() -> pygame.Surface:
        t = art.text(label, BODY_PX, art.INK, on=fill)
        w, h = t.get_width() + icon.get_width() + 46, t.get_height() + 18
        out = art.rounded_rect(w, h, h // 2, fill, shadow=3, border=edge, border_w=2).copy()
        out.blit(icon, (17, (h - icon.get_height()) // 2))
        out.blit(t, (17 + icon.get_width() + 10, (h - t.get_height()) // 2 + 1))
        return out

    return art.cached(key, make)


def _tips(value: Any, n_steps: int) -> dict[int, str]:
    """Tips by step index: [{"step": 3, "tip": "..."}] or ["3: ..."] (1-based step numbers)."""
    out: dict[int, str] = {}
    if not isinstance(value, (list, tuple)):
        return out
    for item in value[:MAX_STEPS]:
        if isinstance(item, dict):
            n, text = kit.as_int(item.get("step"), 0), kit.clean(item.get("tip") or item.get("text"), TIP_CHARS)
        else:
            m = re.match(r"\s*(?:step\s*)?(\d+)\s*[:.)-]\s*(.+)", str(item or ""), re.IGNORECASE)
            n, text = (int(m.group(1)), kit.clean(m.group(2), TIP_CHARS)) if m else (0, "")
        if 1 <= n <= n_steps and text and n - 1 not in out:
            out[n - 1] = text
    return out


def _wrap_max(text: str, size: int, width: int, max_lines: int) -> tuple[tuple[str, ...], bool]:
    """``text`` wrapped to at most ``max_lines`` lines (the last one ellipsized), and whether it all fitted."""
    lines = kit.wrap(text, size, width)
    if len(lines) <= max_lines:
        return lines, True
    head = lines[:max_lines - 1]
    return (*head, kit.ellipsize(" ".join(lines[max_lines - 1:]), size, width)), False


class RecipeCard(Card):
    kind = "recipe"
    sticky = True

    def __init__(self, spec: dict[str, Any], host: Any = None) -> None:
        super().__init__(spec, host)
        self.title = kit.clean(spec.get("title"), 50) or "Recipe"
        self.ingredients = kit.str_list(spec.get("ingredients"), max_items=MAX_INGREDIENTS, limit=60)
        self.steps = kit.str_list(spec.get("steps"), max_items=MAX_STEPS, limit=200)
        self.at_once = 2 if kit.as_int(spec.get("steps_at_once"), 1) >= 2 else 1
        self.serves = kit.clean(spec.get("serves"), 16)
        self.time = kit.clean(spec.get("time"), 16)
        self.oven = kit.clean(spec.get("oven"), 16)
        self.tips = _tips(spec.get("tips"), len(self.steps))
        self.mood = "happy"
        self.slot = kit.PictureSlot(self.host, kit.clean(spec.get("picture_query"), 80))
        self.timers = [find_timer(s) for s in self.steps]
        self.screen = 0  # index into screens()
        self.screen_t = 0.0
        self.touched_t = 0.0
        self.last_t: float | None = None  # when the last step (or "enjoy") was reached
        self._had_picture = self.slot.wanted
        self._screens: dict[bool, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------ screens
    def screens(self) -> list[dict[str, Any]]:
        """Every screen in order: ingredient pages, step screens, then "enjoy"."""
        pic = self.slot.wanted
        got = self._screens.get(pic)
        if got is None:
            got = self._ingredient_screens(pic) + self._step_screens() + [{"view": "finished"}]
            self._screens[pic] = got
        return got

    # ---- ingredients: two columns (the dish's picture beside them when they all fit on one page with it)
    def _columns(self, text_w: int, size: int, ncols: int, strict: bool) -> list[list[list[tuple[int, tuple[str, ...]]]]] | None:
        """Pages of ``ncols`` columns of (ingredient, lines) at ``size``, columns ``text_w`` wide in all; None
        when ``strict`` and an ingredient would need more than two lines there."""
        col_w = (text_w - COL_GAP * (ncols - 1)) // ncols - BULLET_W
        items = []
        for i, ing in enumerate(self.ingredients):
            lines, whole = _wrap_max(ing, size, col_w, 2)
            if strict and not whole:
                return None
            items.append((i, lines))
        pitch = kit.line_h(size)
        heights = [len(ls) * pitch + ITEM_GAP for _, ls in items]

        def fits(chunk: list[int]) -> bool:  # one column
            return (sum(heights[k] for k in chunk) - ITEM_GAP <= CONTENT_H
                    and sum(len(items[k][1]) for k in chunk) <= MAX_LINES)

        def split(chunk: list[int]) -> list[list[int]] | None:
            """``chunk`` (one page) into ncols columns, as even as possible, or None."""
            if ncols == 1:
                return [chunk] if fits(chunk) else None
            best: list[list[int]] | None = None
            for cut in range(len(chunk) + 1):
                a, b = chunk[:cut], chunk[cut:]
                if fits(a) and fits(b):
                    tall = max(sum(heights[k] for k in a), sum(heights[k] for k in b))
                    if best is None or tall < max(sum(heights[k] for k in c) for c in best):
                        best = [a, b]
            return best

        greedy: list[list[int]] = []
        page: list[int] = []
        for k in range(len(items)):
            if split(page + [k]) is not None:
                page.append(k)
            else:
                greedy.append(page)
                page = [k]
        if page:
            greedy.append(page)
        n = len(greedy)
        if n > 1:  # the same number of pages, as evenly filled as they go (9 items: 5 + 4, not 8 + 1)
            even = [list(range(round(p * len(items) / n), round((p + 1) * len(items) / n))) for p in range(n)]
            if all(split(c) is not None for c in even):
                greedy = even
        return [[[items[k] for k in col] for col in split(c) or [c]] for c in greedy]

    def _ingredient_screens(self, pic: bool) -> list[dict[str, Any]]:
        if not self.ingredients:
            return []
        full = kit.PANEL.w - 2 * kit.PAD
        choice: tuple[int, list[Any], bool] | None = None
        if pic:  # beside the picture: only when they all fit on one page there (one wide column if they can)
            for ncols in (1, 2):
                for size in INGREDIENT_SIZES:
                    pages = self._columns(full - ING_PIC_W - kit.GUTTER, size, ncols, strict=True)
                    if pages is not None and len(pages) == 1:
                        choice = (size, pages, True)
                        break
                if choice:
                    break
        if choice is None:
            options = [(size, self._columns(full, size, 2, strict=False) or []) for size in INGREDIENT_SIZES]
            fewest = min(len(p) for _, p in options)
            size, pages = next((s, p) for s, p in options if len(p) == fewest)  # the biggest size with the fewest pages
            choice = (size, pages, False)
        size, pages, with_pic = choice
        return [{"view": "ingredients", "page": n, "pages": len(pages), "size": size, "pic": with_pic,
                 "cols": cols, "items": [it for col in cols for it in col]} for n, cols in enumerate(pages)]

    # ---- steps
    def _step_width(self) -> int:
        return kit.PANEL.w - 2 * kit.PAD - 8

    def _next_text(self, last: int) -> str:
        """What the "Next:" line under step ``last`` (0-based) says."""
        if last + 1 < len(self.steps):
            return f"Next: {self.steps[last + 1]}"
        return "Last step: nearly there!"

    def _tip_lines(self, i: int) -> tuple[str, ...]:
        tip = self.tips.get(i)
        if not tip:
            return ()
        return _wrap_max(tip, TIP_PX, self._step_width() - TIP_INSET - 20, TIP_LINES)[0]

    def _extras_h(self, tip: tuple[str, ...]) -> int:
        """Height under the step text for its tip band and the "Next:" line (gaps included)."""
        h = NEXT_H + EXTRA_GAP
        if tip:
            h += len(tip) * kit.line_h(TIP_PX) + 2 * TIP_PAD + EXTRA_GAP
        return h

    def _step_screens(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        width = self._step_width()
        i = 0
        while i < len(self.steps):
            if self.at_once == 2 and i + 1 < len(self.steps):
                room = CONTENT_H - self._extras_h(())
                for size in PAIR_SIZES:
                    a = rich_wrap(tokens(self.steps[i]), size, width - BADGE_INSET)
                    b = rich_wrap(tokens(self.steps[i + 1]), size, width - BADGE_INSET)
                    tall = (len(a) + len(b)) * kit.line_h(size) + PAIR_GAP
                    if tall <= room and len(a) + len(b) + 1 <= MAX_LINES:
                        out.append({"view": "steps", "steps": [(i, a), (i + 1, b)], "size": size})
                        break
                else:
                    size = 0
                if size:
                    i += 2
                    continue
            tip = self._tip_lines(i)
            room = CONTENT_H - self._extras_h(tip)
            budget = MAX_LINES - 1 - len(tip)  # the "Next:" line and the tip count as lines too
            toks = tokens(self.steps[i])
            size, lines = STEP_SIZES[-1], rich_wrap(toks, STEP_SIZES[-1], width)
            for s in STEP_SIZES:
                lines = rich_wrap(toks, s, width)
                if len(lines) <= min(budget, room // kit.line_h(s)):
                    size = s
                    break
            per = max(1, min(budget, room // kit.line_h(size)))
            parts = [lines[k:k + per] for k in range(0, len(lines), per)] or [[]]
            for n, part in enumerate(parts):
                out.append({"view": "steps", "steps": [(i, part)], "size": size, "part": n, "parts": len(parts),
                            "tip": tip})
            i += 1
        return out

    @property
    def view(self) -> str:
        return str(self.current()["view"])

    def current(self) -> dict[str, Any]:
        scr = self.screens()
        self.screen = max(0, min(len(scr) - 1, self.screen))
        return scr[self.screen]

    def steps_showing(self) -> list[int]:
        """0-based indexes of the steps on the screen now (empty for ingredients / "enjoy")."""
        return [i for i, _ in self.current().get("steps", [])]

    # ------------------------------------------------------------ logic
    def update(self, dt: float) -> None:
        super().update(dt)
        self.slot.update(self.t)
        if self._had_picture != self.slot.wanted:  # the picture failed: the pages reflow, keep the place
            self._reflow(self._had_picture)
            self._had_picture = self.slot.wanted
        cur = self.current()
        showing = cur.get("steps", [])
        if self.last_t is None and (cur["view"] == "finished" or (showing and showing[-1][0] == len(self.steps) - 1)):
            self.last_t = self.t
        quiet = self.t - self.touched_t
        finished = cur["view"] == "finished" and self.t - self.screen_t >= FINISHED_DONE_S
        after_last = self.last_t is not None and quiet >= DONE_AFTER_LAST_S and self.t - self.last_t >= DONE_AFTER_LAST_S
        if finished or after_last or quiet >= IDLE_DONE_S:
            self.done = True

    def _reflow(self, had_picture: bool) -> None:
        old = self._screens.get(had_picture) or []
        if 0 <= self.screen < len(old) and old[self.screen]["view"] == "ingredients":
            first = old[self.screen]["items"][0][0] if old[self.screen]["items"] else 0
            for n, scr in enumerate(self.screens()):
                if scr["view"] == "ingredients" and any(i == first for i, _ in scr["items"]):
                    self.screen = n
                    return
        elif 0 <= self.screen < len(old):
            n_old = sum(1 for s in old if s["view"] == "ingredients")
            n_new = sum(1 for s in self.screens() if s["view"] == "ingredients")
            self.screen += n_new - n_old

    def _go(self, n: int) -> None:
        n = max(0, min(len(self.screens()) - 1, n))
        self.touched_t = self.t
        if n == self.screen:
            return
        self.screen, self.screen_t = n, self.t
        cur = self.current()
        if cur["view"] == "finished":
            self.host.sound("tada")
            self.mood = "happy"
        else:
            self.host.sound("pop")
        if cur["view"] != "finished" and self.last_t is not None:
            self.last_t = None  # went back: the "after the last step" clock starts again when it's reached

    def command(self, action: str, args: dict[str, Any]) -> bool:
        scr = self.screens()
        if action == "next":
            self._go(self.screen + 1)
            return True
        if action == "prev":
            self._go(self.screen - 1)
            return True
        if action in ("step", "start"):
            n = kit.as_int(args.get("n"), 1) if action == "step" else 1
            if not 1 <= n <= len(self.steps):
                return False
            for k, s in enumerate(scr):
                if any(i == n - 1 for i, _ in s.get("steps", [])):
                    self._go(k)
                    return True
            return False
        if action == "ingredients":
            if not self.ingredients:
                return False
            self._go(0)
            return True
        if action == "close":
            self.done = True
            return True
        if action == ADD_PICTURE_ACTION:
            return self.add_picture(str(args.get("query") or ""))
        return False

    def add_picture(self, query: str = "") -> bool:
        """The dish's picture beside the ingredients, asked for after the card was made without one ("show me a
        picture of butter chicken" changed nothing, the robot, 2026-11-03). The ingredients come back if a step was
        showing, as only they have the picture. False: no picture can be fetched."""
        if not self.slot.wanted:
            self.slot = kit.PictureSlot(self.host, kit.clean(query, 80) or self.title)
            if not self.slot.wanted:
                return False
        if self.view != "ingredients" and self.ingredients:
            self._go(0)
        return True

    def tap(self, x: float, y: float) -> bool:
        ok = self.command("prev" if x < art.W * 0.33 else "next", {})
        if ok:
            self.host.tell(f"The screen shows {self.describe()}")
        return ok

    def key(self, name: str) -> bool:
        if name in ("right", "space", "return", "down"):
            ok = self.command("next", {})
        elif name in ("left", "backspace", "up"):
            ok = self.command("prev", {})
        else:
            return False
        self.host.tell(f"The screen shows {self.describe()}")
        return ok

    def describe(self) -> str:
        cur = self.current()
        n = len(self.steps)
        if cur["view"] == "ingredients":
            page = f" (page {cur['page'] + 1} of {cur['pages']})" if cur["pages"] > 1 else ""
            items = ", ".join(self.ingredients[i] for i, _ in cur["items"])
            return f"Ingredients for {self.title}{page}: {items}"
        if cur["view"] == "finished":
            return f"{self.title}: all {n} steps done"
        idx = [i for i, _ in cur["steps"]]
        if len(idx) == 2:
            a, b = idx
            return f"Steps {a + 1} and {b + 1} of {n}: {self.steps[a]} Then: {self.steps[b]}"
        more = " (continued)" if cur.get("part") else ""
        tip = f" Tip: {self.tips[idx[0]]}" if idx[0] in self.tips else ""
        return f"Step {idx[0] + 1} of {n}{more}: {self.steps[idx[0]]}{tip}"

    def timer_seconds(self) -> int | None:
        for i in self.steps_showing():
            if self.timers[i] is not None:
                return self.timers[i][2]
        return None

    def status(self) -> dict[str, Any]:
        showing = self.steps_showing()
        return {**super().status(), "view": self.view, "step": showing[0] + 1 if showing else None,
                "steps": len(self.steps), "timer_seconds": self.timer_seconds()}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=41)
        kit.header_title(surf, self.title)
        kit.panel(surf)
        cur = self.current()
        enter = art.ease_out_cubic(kit.appear(self.t, self.screen_t, 0.35))
        if cur["view"] == "ingredients":
            self._draw_ingredients(surf, cur, enter)
        elif cur["view"] == "finished":
            self._draw_finished(surf, enter)
        else:
            self._draw_steps(surf, cur, enter)
        self._draw_nav(surf, cur)

    def _chips_row(self, surf: pygame.Surface, chips: list[pygame.Surface], right: list[pygame.Surface]) -> None:
        """Chips from the left of the top row, ``right`` ones from its right; the left ones that don't fit go."""
        x0, x1 = kit.PANEL.x + kit.PAD, kit.PANEL.right - kit.PAD
        for c in right:
            x1 -= c.get_width()
            surf.blit(c, c.get_rect(midleft=(x1, ROW_Y)))
            x1 -= 12
        x = x0
        for k, c in enumerate(chips):
            if k and x + c.get_width() > x1:
                break
            surf.blit(c, c.get_rect(midleft=(x, ROW_Y)))
            x += c.get_width() + 12

    def _time_chip(self, label: str) -> pygame.Surface:
        return icon_chip(label, clock_icon(14), art.mix(art.LEMON, art.WHITE, 0.2), art.inkier(art.LEMON, 0.35))

    def _oven_chip(self) -> pygame.Surface:
        label = self.oven if self.oven.lower().startswith(("oven", "gas")) else f"Oven {self.oven}"
        return icon_chip(label, flame_icon(14), art.mix(art.TANGERINE, art.WHITE, 0.62), art.inkier(art.TANGERINE, 0.3))

    def _draw_ingredients(self, surf: pygame.Surface, cur: dict[str, Any], enter: float) -> None:
        label = "Ingredients" + (f" {cur['page'] + 1}/{cur['pages']}" if cur["pages"] > 1 else "")
        chips = [kit.chip(label, art.MINT, size=BODY_PX)]
        if self.serves:  # "4" -> "Serves 4"; "Makes 24" stays as it is
            chips.append(kit.chip(f"Serves {self.serves}" if self.serves.isdigit() else self.serves, art.SKY,
                                  size=BODY_PX))
        if self.time:
            chips.append(self._time_chip(self.time))
        if self.oven:
            chips.append(self._oven_chip())
        self._chips_row(surf, chips, [])
        size = cur["size"]
        pitch = kit.line_h(size)
        full = kit.PANEL.w - 2 * kit.PAD
        text_w = full - (ING_PIC_W + kit.GUTTER if cur["pic"] else 0)
        cols = cur["cols"]
        col_w = (text_w - COL_GAP * (len(cols) - 1)) // max(1, len(cols))
        dx = int((1 - enter) * 30)
        dot_dy = art.font(size).get_height() // 2
        for c, col in enumerate(cols):
            x = kit.PANEL.x + kit.PAD + c * (col_w + COL_GAP)
            y = CONTENT_TOP
            for i, lines in col:
                color = kit.ACCENTS[i % len(kit.ACCENTS)]
                kit.blit_alpha(surf, art.circle(11, color), (x + 4 + dx, y + dot_dy - 11), enter)
                kit.blit_alpha(surf, kit.text_block(list(lines), size, pitch=pitch), (x + BULLET_W + dx, y), enter)
                y += pitch * len(lines) + ITEM_GAP
        if len(cols) == 2 and all(cols):  # a soft line between the two columns
            xx = kit.PANEL.x + kit.PAD + col_w + COL_GAP // 2
            bottom = CONTENT_TOP + max(sum(pitch * len(ls) + ITEM_GAP for _, ls in col) for col in cols) - ITEM_GAP
            for yy in range(CONTENT_TOP + 4, bottom, 16):
                pygame.draw.line(surf, art.mix(art.INK_SOFT, art.WHITE, 0.55), (xx, yy), (xx, min(bottom, yy + 8)), 2)
        if cur["pic"]:
            rect = pygame.Rect(kit.PANEL.right - kit.PAD - ING_PIC_W, CONTENT_TOP - 8, ING_PIC_W, CONTENT_H + 8)
            self.slot.draw(surf, rect, self.t)

    def _draw_steps(self, surf: pygame.Surface, cur: dict[str, Any], enter: float) -> None:
        n = len(self.steps)
        idx = [i for i, _ in cur["steps"]]
        label = f"Step {idx[0] + 1} of {n}" if len(idx) == 1 else f"Steps {idx[0] + 1}-{idx[1] + 1} of {n}"
        chips = [kit.chip(label, art.STRAWBERRY, size=BODY_PX)]
        if cur.get("parts", 1) > 1:
            chips.append(kit.chip(f"part {cur['part'] + 1} of {cur['parts']}", art.GRAPE, size=BODY_PX))
        right = []
        secs = self.timer_seconds()
        if secs:
            right.append(self._time_chip(short_time(secs)))
        if self.oven and any(OVEN_WORDS.search(self.steps[i]) for i in idx):
            right.append(self._oven_chip())
        self._chips_row(surf, chips, right)
        size = cur["size"]
        left = kit.PANEL.x + kit.PAD
        width = self._step_width()
        dx = int((1 - enter) * 40)
        last_part = cur.get("part", 0) == cur.get("parts", 1) - 1
        tip = cur.get("tip", ()) if last_part else ()
        bottom = CONTENT_TOP + CONTENT_H
        # the "Next:" line at the bottom, the tip band above it
        nxt = self._next_text(idx[-1]) if last_part else "...and the rest of this step next"
        line = kit.text_block([kit.ellipsize(nxt, NEXT_PX, width - 20)], NEXT_PX, kit.TEXT_SOFT)
        band = pygame.Rect(left - 6, bottom - NEXT_H, width + 12, NEXT_H)
        surf.blit(art.rounded_rect(band.w, band.h, band.h // 2, art.mix(art.BG_TOP, art.WHITE, 0.5)), band.topleft)
        kit.blit_alpha(surf, line, line.get_rect(midleft=(band.x + 18, band.centery + 1)).topleft, enter)
        bottom = band.y - EXTRA_GAP
        if tip:
            th = len(tip) * kit.line_h(TIP_PX) + 2 * TIP_PAD
            tb = pygame.Rect(left - 6, bottom - th, width + 12, th)
            surf.blit(art.rounded_rect(tb.w, tb.h, 22, art.mix(art.LEMON, art.WHITE, 0.62),
                                       border=art.mix(art.LEMON, art.INK_SOFT, 0.3), border_w=2), tb.topleft)
            icon = bulb_icon(17)
            surf.blit(icon, (tb.x + 18, tb.y + TIP_PAD + (art.font(TIP_PX).get_height() - icon.get_height()) // 2))
            img = kit.text_block(list(tip), TIP_PX, art.INK)
            kit.blit_alpha(surf, img, (tb.x + TIP_INSET, tb.y + TIP_PAD + 1), enter)
            bottom = tb.y - EXTRA_GAP
        if len(idx) == 1:
            img = rich_sprite(cur["steps"][0][1], size)
            y = CONTENT_TOP + max(0, (bottom - CONTENT_TOP - img.get_height()) // 2)
            kit.blit_alpha(surf, img, (left + 4 + dx, y), enter)
            return
        imgs = [rich_sprite(lines, size) for _, lines in cur["steps"]]
        total = sum(i.get_height() for i in imgs) + PAIR_GAP
        y = CONTENT_TOP + max(0, (bottom - CONTENT_TOP - total) // 2)
        for k, ((i, _), img) in enumerate(zip(cur["steps"], imgs)):
            badge = kit.number_badge(i + 1, kit.ACCENTS[i % len(kit.ACCENTS)])
            first = art.font(size).get_height() // 2 + 3
            kit.blit_alpha(surf, badge, badge.get_rect(center=(left + 25, y + first)).topleft, enter)
            kit.blit_alpha(surf, img, (left + BADGE_INSET + dx, y), enter)
            if k == 0:
                yy = y + img.get_height() + PAIR_GAP // 2
                for xx in range(left + BADGE_INSET, kit.PANEL.right - kit.PAD, 18):
                    pygame.draw.line(surf, art.mix(art.INK_SOFT, art.WHITE, 0.5), (xx, yy), (xx + 9, yy), 2)
            y += img.get_height() + PAIR_GAP

    def _draw_finished(self, surf: pygame.Surface, enter: float) -> None:
        pic = self.slot.shown
        if pic:  # the dish beside the cheer
            rect = pygame.Rect(kit.PANEL.x + kit.PAD, kit.PANEL.y + kit.PAD, kit.PIC_W + 40,
                               kit.PANEL.h - 2 * kit.PAD - 56)
            self.slot.draw(surf, rect, self.t)
            cx = (rect.right + kit.PANEL.right) // 2
        else:
            cx = kit.PANEL.centerx
        cy = kit.PANEL.y + kit.PANEL.h // 2 - 40
        k = art.ease_out_back(kit.appear(self.t, self.screen_t, 0.6))
        rx = 210 if pic else 330
        for j in range(6):  # stars circling around the words, never across them
            a = j / 6 * math.tau + self.t * 0.4
            art.blit_center(surf, art.star(17, kit.ACCENTS[j], rot_deg=self.t * 40 % 360),
                            (cx + math.cos(a) * rx, cy + 60 + math.sin(a) * 150), int(255 * enter))
        art.blit_center(surf, art.scaled(art.heart(84, art.STRAWBERRY), max(0.05, k)), (cx, cy - 16))
        word = art.text_outlined("Enjoy!", 100, art.inkier(art.TANGERINE, 0.05), art.INK, 4)
        art.blit_center(surf, art.scaled(word, max(0.05, k)), (cx, cy + 86))
        done = kit.text_block([f"All {len(self.steps)} steps done"], BODY_PX, kit.TEXT_SOFT)
        kit.blit_alpha(surf, done, done.get_rect(center=(cx, cy + 158)).topleft, enter)

    def _draw_nav(self, surf: pygame.Surface, cur: dict[str, Any]) -> None:
        """Back / next buttons and the progress row (steps) or page dots (ingredients)."""
        y = NAV_Y
        if self.screen > 0:
            art.blit_center(surf, kit.arrow(-1), (kit.PANEL.x + kit.PAD + 30, y))
        if cur["view"] == "ingredients" and cur["page"] == cur["pages"] - 1:
            go = art.pill("Let's cook!", BODY_PX, art.WHITE, art.inkier(art.MINT, 0.22), pad=(24, 9))
            surf.blit(go, go.get_rect(midright=(kit.PANEL.right - kit.PAD, y)))
        elif cur["view"] != "finished":
            art.blit_center(surf, kit.arrow(1), (kit.PANEL.right - kit.PAD - 30, y))
        if cur["view"] == "ingredients":
            kit.page_dots(surf, cur["pages"], cur["page"], (kit.PANEL.centerx, y))
        elif cur["view"] == "steps":
            self._progress(surf, y)

    def _progress(self, surf: pygame.Surface, y: int) -> None:
        n = len(self.steps)
        showing = self.steps_showing()
        if n <= 1:
            return
        left, right = kit.PANEL.x + kit.PAD + 90, kit.PANEL.right - kit.PAD - 90  # between the back / next buttons
        if n <= 20:
            gap = min(44, (right - left) / (n - 1))
            x0 = (left + right) / 2 - gap * (n - 1) / 2
            for i in range(n):
                x = x0 + i * gap
                if i in showing:
                    art.blit_center(surf, art.circle(14, art.STRAWBERRY), (x, y))
                    art.blit_center(surf, art.circle(6, art.WHITE), (x, y))
                elif i < showing[0]:
                    art.blit_center(surf, art.circle(10, art.MINT), (x, y))
                else:
                    art.blit_center(surf, art.circle(10, art.mix(art.INK_SOFT, art.WHITE, 0.55)), (x, y))
            return
        w = right - left
        surf.blit(art.rounded_rect(w, 18, 9, art.mix(art.INK_SOFT, art.WHITE, 0.55)), (left, y - 9))
        fill = max(18, int(w * (showing[-1] + 1) / n))
        surf.blit(art.rounded_rect(fill, 18, 9, art.MINT), (left, y - 9))
        art.blit_center(surf, art.circle(14, art.STRAWBERRY), (left + fill - 9, y))


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    card = RecipeCard(spec, host)
    return card if card.steps else None


register(CardType(
    kind="recipe",
    description=(
        "Cooking a recipe together, one step at a time in big letters (the parent may be across the kitchen). "
        "The ingredients come first (up to 14 fit on one screen, in two columns), then each step with the next "
        "one previewed under it; say \"next\" (the action) when the parent is ready for the next step. Write each "
        "step short (at most 15 words) and say a time in it plainly (\"simmer for 10 minutes\"): it is "
        "highlighted. Give the oven temperature in oven, and a step's handy tip in tips. steps_at_once 2 shows "
        "two short steps per screen. The card stays on screen while they cook."
    ),
    fields={
        "title": {"type": "string", "maxLength": 50, "description": "The dish (\"Banana pancakes\")"},
        "ingredients": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 40},
                        "description": "Ingredients with amounts, each short (\"2 ripe bananas\", \"1 cup oats\")"},
        "steps": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 120},
                  "description": "The steps in order, each one action of at most 15 words; give times as "
                                 "numbers (\"Bake for 20 minutes\")"},
        "steps_at_once": {"type": "integer", "enum": [1, 2],
                          "description": "1 (default): one step per screen; 2: two steps per screen"},
        "serves": {"type": "string", "maxLength": 16, "description": "Optional: how many it serves (\"4\"), or \"Makes 24\""},
        "time": {"type": "string", "maxLength": 16, "description": "Optional: total time (\"25 min\")"},
        "oven": {"type": "string", "maxLength": 16,
                 "description": "Optional: the oven temperature (\"180°C\", \"350°F\", \"Gas 4\"), shown with the "
                                "ingredients and on the steps that use the oven"},
        "tips": {"type": "array", "maxItems": 8,
                 "items": {"type": "object", "properties": {
                     "step": {"type": "integer", "minimum": 1, "maximum": 16, "description": "the step number"},
                     "tip": {"type": "string", "maxLength": TIP_CHARS, "description": "a few words"}}},
                 "description": "Optional: a short tip for a step, shown under it ([{\"step\": 4, \"tip\": "
                                "\"Small bubbles mean it's time to flip\"}])"},
        "picture_query": {"type": "string", "maxLength": 60,
                          "description": "Optional: the dish, for a picture beside the ingredients (\"pancakes\")"},
    },
    required=("title", "steps"),
    actions={"next": "the next step (from the ingredients: step 1)", "prev": "the step before",
             "step": "go to step n (1-based): {n}", "ingredients": "show the ingredients again",
             "close": "the recipe is finished or abandoned: take it off the screen"},
    build=_build,
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "recipe", "title": "Banana pancakes", "serves": "2", "time": "20 min", "picture_query": "pancakes",
     "ingredients": ["2 ripe bananas", "2 eggs", "1/2 cup rolled oats", "1 tsp baking powder", "Pinch of cinnamon",
                     "Butter for the pan"],
     "steps": ["Mash the bananas in a bowl", "Whisk in the eggs", "Stir in the oats, baking powder and cinnamon",
               "Let the batter rest for 5 minutes", "Melt a little butter in a pan over medium heat",
               "Cook small pancakes for 2 minutes on each side", "Serve warm with berries"],
     "tips": [{"step": 6, "tip": "Small bubbles on top mean it's time to flip"}]},
    {"card": "recipe", "title": "Tomato soup", "steps_at_once": 2,
     "ingredients": ["1 onion", "2 cloves garlic", "800 g tinned tomatoes", "500 ml vegetable stock",
                     "2 tbsp olive oil", "Salt and pepper", "Splash of cream"],
     "steps": ["Chop the onion and garlic", "Warm the oil in a big pot", "Soften the onion for 5 minutes",
               "Add the garlic and stir", "Pour in the tomatoes and stock",
               "Simmer for 20 minutes", "Blend until smooth", "Season and stir in the cream"]},
    {"card": "recipe", "title": "Baby oatmeal",
     "steps": ["Bring 1/2 cup of water or milk to a gentle boil in a small pan",
               "Stir in 2 tablespoons of baby oats and turn the heat down low",
               "Cook for 3 to 5 minutes, stirring often, until soft and creamy",
               "Let it cool until just warm, then test it on your wrist"]},
    {"card": "recipe", "title": "Chocolate chip cookies", "serves": "Makes 24", "time": "35 min", "oven": "180°C",
     "ingredients": ["225 g soft butter", "150 g brown sugar", "100 g caster sugar", "2 large eggs",
                     "1 tsp vanilla extract", "300 g plain flour", "1 tsp baking soda", "1/2 tsp salt",
                     "200 g chocolate chips", "50 g chopped walnuts", "Baking paper", "2 baking trays"],
     "steps": ["Heat the oven to 180°C and line two trays", "Beat the butter and both sugars until fluffy",
               "Beat in the eggs one at a time, then the vanilla", "Stir the flour, baking soda and salt together",
               "Mix the flour into the butter until just combined", "Fold in the chocolate chips and walnuts",
               "Roll spoonfuls into balls and space them well apart",
               "Bake for 10 to 12 minutes until the edges are golden",
               "Cool on the tray for 5 minutes, then on a rack"],
     "tips": [{"step": 5, "tip": "Stop as soon as no flour shows: overmixing makes cookies tough"},
              {"step": 8, "tip": "They look soft in the middle when done; they firm up as they cool"}]},
]

__all__ = ["SAMPLES", "RecipeCard", "find_timer", "short_time"]
