"""The "recipe" card: cooking one step at a time, in big letters, readable from across the kitchen.

First the ingredients (paged, big, with the dish's picture when there is one), then one step per screen
("Step 3 of 8" and a progress row), or two short steps per screen with ``steps_at_once: 2`` (a pair that
wouldn't fit big is shown one step at a time). A time in a step ("simmer for 10 minutes") is shown as a
highlighted pill in the text and as a timer chip above it; the director may start a timer card for it
(``status()["timer_seconds"]``), but this card doesn't depend on that.

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
STEP_SIZES = (60, 54, 48, STEP_PX)  # one step a screen: as big as it fits in MAX_LINES lines
PAIR_SIZES = (48, STEP_PX)  # two steps a screen
INGREDIENT_PX = 40
PIC_W = 230
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
                img = f.render(text, True, kit.TEXT if not timer else art.INK)
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

    def _ingredient_screens(self, pic: bool) -> list[dict[str, Any]]:
        if not self.ingredients:
            return []
        width = kit.PANEL.w - 2 * kit.PAD - 40 - (PIC_W + 18 if pic else 0)
        lines = [kit.wrap(i, INGREDIENT_PX, width)[:2] for i in self.ingredients]
        pages = kit.paged([len(ls) for ls in lines])
        return [{"view": "ingredients", "page": n, "pages": len(pages), "items": [(i, lines[i]) for i in page]}
                for n, page in enumerate(pages)]

    def _step_width(self) -> int:
        return kit.PANEL.w - 2 * kit.PAD - 8

    def _fit_step(self, i: int, sizes: tuple[int, ...], width: int) -> tuple[int, list[list[Token]]]:
        toks = tokens(self.steps[i])
        lines: list[list[Token]] = []
        for size in sizes:
            lines = rich_wrap(toks, size, width)
            if len(lines) <= MAX_LINES:
                return size, lines
        return sizes[-1], lines

    def _step_screens(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        width = self._step_width()
        i = 0
        while i < len(self.steps):
            if self.at_once == 2 and i + 1 < len(self.steps):
                a = rich_wrap(tokens(self.steps[i]), PAIR_SIZES[0], width - 60)
                b = rich_wrap(tokens(self.steps[i + 1]), PAIR_SIZES[0], width - 60)
                size = PAIR_SIZES[0]
                if len(a) + len(b) > MAX_LINES:
                    size = PAIR_SIZES[-1]
                    a = rich_wrap(tokens(self.steps[i]), size, width - 60)
                    b = rich_wrap(tokens(self.steps[i + 1]), size, width - 60)
                if len(a) + len(b) <= MAX_LINES:
                    out.append({"view": "steps", "steps": [(i, a), (i + 1, b)], "size": size})
                    i += 2
                    continue
            size, lines = self._fit_step(i, STEP_SIZES, width)
            parts = [lines[k:k + MAX_LINES] for k in range(0, len(lines), MAX_LINES)] or [[]]
            for n, part in enumerate(parts):
                out.append({"view": "steps", "steps": [(i, part)], "size": size, "part": n, "parts": len(parts)})
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
        if cur["view"] == "finished" and self.t - self.screen_t >= FINISHED_DONE_S or self.last_t is not None and quiet >= DONE_AFTER_LAST_S and self.t - self.last_t >= DONE_AFTER_LAST_S or quiet >= IDLE_DONE_S:
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
        return False

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
        return f"Step {idx[0] + 1} of {n}{more}: {self.steps[idx[0]]}"

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

    def _top_row(self, surf: pygame.Surface, chips: list[tuple[str, art.Color]], y: int) -> int:
        x = kit.PANEL.x + kit.PAD
        for label, color in chips:
            c = kit.chip(label, color, size=BODY_PX)
            surf.blit(c, c.get_rect(midleft=(x, y)))
            x += c.get_width() + 12
        return x

    def _draw_ingredients(self, surf: pygame.Surface, cur: dict[str, Any], enter: float) -> None:
        row_y = kit.PANEL.y + kit.PAD + 20
        label = "Ingredients" + (f" {cur['page'] + 1}/{cur['pages']}" if cur["pages"] > 1 else "")
        chips: list[tuple[str, art.Color]] = [(label, art.MINT)]
        if self.serves:
            chips.append((f"Serves {self.serves}" if self.serves[:1].isdigit() else self.serves, art.SKY))
        if self.time:
            chips.append((self.time, art.TANGERINE))
        pic = self.slot.wanted
        room = kit.PANEL.w - 2 * kit.PAD
        while len(chips) > 1 and sum(kit.chip(c, k, size=BODY_PX).get_width() + 12 for c, k in chips) > room:
            chips.pop()
        self._top_row(surf, chips, row_y)
        x = kit.PANEL.x + kit.PAD
        y = row_y + 40
        dx = int((1 - enter) * 30)
        pitch = kit.line_h(INGREDIENT_PX)
        for n, (i, lines) in enumerate(cur["items"]):
            color = kit.ACCENTS[i % len(kit.ACCENTS)]
            dot_y = y + art.font(INGREDIENT_PX).get_height() // 2
            kit.blit_alpha(surf, art.circle(10, color), (x + 6 + dx, dot_y - 10), enter)
            kit.blit_alpha(surf, kit.text_block(lines, INGREDIENT_PX, pitch=pitch), (x + 36 + dx, y), enter)
            y += pitch * len(lines) + 8
        if pic:
            top = row_y + 34
            rect = pygame.Rect(kit.PANEL.right - kit.PAD - PIC_W, top, PIC_W, kit.PANEL.bottom - 62 - top)
            self.slot.draw(surf, rect, self.t)

    def _draw_steps(self, surf: pygame.Surface, cur: dict[str, Any], enter: float) -> None:
        n = len(self.steps)
        idx = [i for i, _ in cur["steps"]]
        row_y = kit.PANEL.y + kit.PAD + 20
        label = f"Step {idx[0] + 1} of {n}" if len(idx) == 1 else f"Steps {idx[0] + 1}-{idx[1] + 1} of {n}"
        x = self._top_row(surf, [(label, art.STRAWBERRY)], row_y)
        secs = self.timer_seconds()
        if secs:
            chip = self._timer_chip(short_time(secs))
            surf.blit(chip, chip.get_rect(midright=(kit.PANEL.right - kit.PAD, row_y)))
        if cur.get("parts", 1) > 1:
            c = kit.chip(f"part {cur['part'] + 1} of {cur['parts']}", art.GRAPE)
            surf.blit(c, c.get_rect(midleft=(x, row_y)))
        size = cur["size"]
        top, bottom = row_y + 30, kit.PANEL.bottom - 64
        dx = int((1 - enter) * 40)
        if len(idx) == 1:
            img = rich_sprite(cur["steps"][0][1], size)
            y = top + max(0, (bottom - top - img.get_height()) // 2)
            kit.blit_alpha(surf, img, (kit.PANEL.x + kit.PAD + 4 + dx, y), enter)
            return
        imgs = [rich_sprite(lines, size) for _, lines in cur["steps"]]
        gap = 26
        total = sum(i.get_height() for i in imgs) + gap
        y = top + max(0, (bottom - top - total) // 2)
        for k, ((i, _), img) in enumerate(zip(cur["steps"], imgs)):
            badge = kit.number_badge(i + 1, kit.ACCENTS[i % len(kit.ACCENTS)], 22)
            first = art.font(size).get_height() // 2 + 3
            kit.blit_alpha(surf, badge, badge.get_rect(center=(kit.PANEL.x + kit.PAD + 22, y + first)).topleft, enter)
            kit.blit_alpha(surf, img, (kit.PANEL.x + kit.PAD + 60 + dx, y), enter)
            if k == 0:
                yy = y + img.get_height() + gap // 2
                for xx in range(kit.PANEL.x + kit.PAD + 60, kit.PANEL.right - kit.PAD, 18):
                    pygame.draw.line(surf, art.mix(art.INK_SOFT, art.WHITE, 0.5), (xx, yy), (xx + 9, yy), 2)
            y += img.get_height() + gap

    def _timer_chip(self, label: str) -> pygame.Surface:
        key = ("recipe_timer_chip", label)

        def make() -> pygame.Surface:
            t = art.text(label, BODY_PX, art.INK)
            icon = clock_icon(13)
            w, h = t.get_width() + icon.get_width() + 44, t.get_height() + 16
            out = art.rounded_rect(w, h, h // 2, art.mix(art.LEMON, art.WHITE, 0.2), shadow=3,
                                   border=art.inkier(art.LEMON, 0.35), border_w=2).copy()
            out.blit(icon, (16, (h - icon.get_height()) // 2))
            out.blit(t, (16 + icon.get_width() + 10, (h - t.get_height()) // 2 + 1))
            return out

        return art.cached(key, make)

    def _draw_finished(self, surf: pygame.Surface, enter: float) -> None:
        cx, cy = kit.PANEL.centerx, kit.PANEL.y + kit.PANEL.h // 2 - 30
        k = art.ease_out_back(kit.appear(self.t, self.screen_t, 0.6))
        for j in range(6):  # stars circling around the words, never across them
            a = j / 6 * math.tau + self.t * 0.4
            art.blit_center(surf, art.star(14, kit.ACCENTS[j], rot_deg=self.t * 40 % 360),
                            (cx + math.cos(a) * 280, cy + 50 + math.sin(a) * 128), int(255 * enter))
        art.blit_center(surf, art.scaled(art.heart(70, art.STRAWBERRY), max(0.05, k)), (cx, cy - 10))
        word = art.text_outlined("Enjoy!", 84, art.inkier(art.TANGERINE, 0.05), art.INK, 3)
        art.blit_center(surf, art.scaled(word, max(0.05, k)), (cx, cy + 80))
        done = kit.text_block([f"All {len(self.steps)} steps done"], BODY_PX, kit.TEXT_SOFT)
        kit.blit_alpha(surf, done, done.get_rect(center=(cx, cy + 140)).topleft, enter)

    def _draw_nav(self, surf: pygame.Surface, cur: dict[str, Any]) -> None:
        """Back / next buttons and the progress row (steps) or page dots (ingredients)."""
        y = kit.PANEL.bottom - 34
        if self.screen > 0:
            art.blit_center(surf, kit.arrow(-1), (kit.PANEL.x + kit.PAD + 24, y))
        if cur["view"] == "ingredients" and cur["page"] == cur["pages"] - 1:
            go = art.pill("Let's cook!", BODY_PX, art.WHITE, art.inkier(art.MINT, 0.22), pad=(20, 8))
            surf.blit(go, go.get_rect(midright=(kit.PANEL.right - kit.PAD, y)))
        elif cur["view"] != "finished":
            art.blit_center(surf, kit.arrow(1), (kit.PANEL.right - kit.PAD - 24, y))
        if cur["view"] == "ingredients":
            kit.page_dots(surf, cur["pages"], cur["page"], (kit.PANEL.centerx, y))
        elif cur["view"] == "steps":
            self._progress(surf, y)

    def _progress(self, surf: pygame.Surface, y: int) -> None:
        n = len(self.steps)
        showing = self.steps_showing()
        if n <= 1:
            return
        left, right = kit.PANEL.x + kit.PAD + 70, kit.PANEL.right - kit.PAD - 70
        if n <= 16:
            gap = min(34, (right - left) / (n - 1))
            x0 = (left + right) / 2 - gap * (n - 1) / 2
            for i in range(n):
                x = x0 + i * gap
                if i in showing:
                    art.blit_center(surf, art.circle(12, art.STRAWBERRY), (x, y))
                    art.blit_center(surf, art.circle(5, art.WHITE), (x, y))
                elif i < showing[0]:
                    art.blit_center(surf, art.circle(9, art.MINT), (x, y))
                else:
                    art.blit_center(surf, art.circle(9, art.mix(art.INK_SOFT, art.WHITE, 0.55)), (x, y))
            return
        w = right - left
        surf.blit(art.rounded_rect(w, 16, 8, art.mix(art.INK_SOFT, art.WHITE, 0.55)), (left, y - 8))
        fill = max(16, int(w * (showing[-1] + 1) / n))
        surf.blit(art.rounded_rect(fill, 16, 8, art.MINT), (left, y - 8))
        art.blit_center(surf, art.circle(12, art.STRAWBERRY), (left + fill - 8, y))


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    card = RecipeCard(spec, host)
    return card if card.steps else None


register(CardType(
    kind="recipe",
    description=(
        "Cooking a recipe together, one step at a time in big letters (the parent may be across the kitchen). "
        "The ingredients come first, then each step; say \"next\" (the action) when the parent is ready for the "
        "next step. Write each step short (at most 15 words) and say a time in it plainly (\"simmer for 10 "
        "minutes\"): it is highlighted. steps_at_once 2 shows two short steps per screen. The card stays on "
        "screen while they cook."
    ),
    fields={
        "title": {"type": "string", "description": "The dish (\"Banana pancakes\")"},
        "ingredients": {"type": "array", "items": {"type": "string"},
                        "description": "Ingredients with amounts, each short (\"2 ripe bananas\", \"1 cup oats\")"},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "The steps in order, each one action of at most 15 words; give times as "
                                 "numbers (\"Bake for 20 minutes\")"},
        "steps_at_once": {"type": "integer", "enum": [1, 2],
                          "description": "1 (default): one step per screen; 2: two steps per screen"},
        "serves": {"type": "string", "description": "Optional: how many it serves (\"4\")"},
        "time": {"type": "string", "description": "Optional: total time (\"25 min\")"},
        "picture_query": {"type": "string",
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
               "Cook small pancakes for 2 minutes on each side", "Serve warm with berries"]},
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
]

__all__ = ["SAMPLES", "RecipeCard", "find_timer", "short_time"]
