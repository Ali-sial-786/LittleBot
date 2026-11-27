"""The reminders card: what's coming up, and the big ringing view with Snooze and Stop when one goes off.

Two views:

* **coming up** (``{"card": "reminders"}``): the next reminders and alarms grouped by day under a heading band
  ("Today", "Tomorrow", "Monday", "Thu 12 Nov"), each with its time, a bell for an alarm, the words (two lines when
  long) and how it repeats ("every weekday", or "snoozed"); as many as fit, at most MAX_LINES lines a page (about
  eight), paged with the arrows, a tap or "next". The one just set glows for a moment (``spec["highlight"]``: its
  id). Live from the Reminders skill (``ctx.skills.get("reminders")``). The layout is the calendar card's agenda
  (littlebot/screens/cards/calendar.py).
* **ringing** (``{"card": "reminders", "ringing": id}``, shown by the skill when one is due): the label big, the
  time now huge, a wobbling bell in a pulsing glow, how it repeats, and two big buttons, Snooze and Stop, that call
  the skill (``snooze`` / ``stop_ringing``). Sticky while it rings; once it stops (a button, the voice, or it rang
  long enough) the card says "Stopped" or "Snoozed until 7:09 am" for a moment and goes. The sound is the skill's,
  so an alarm rings even while a video has the screen.

A ringing card asked for again while it is already on the screen is that same card (the skill shows it, and
an announcement may carry it too).
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import re
import time
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, TITLE_PX, Card, CardHost, CardType, H, W, register
from littlebot.screens.cards import _kit as kit
from littlebot.screens.cards import calendar as agenda
from littlebot.screens.cards import candy
from littlebot.skills.when import clock_words, repeat_words

log = logging.getLogger("littlebot.screens.reminders")

ENDED_S = 2.5  # "Stopped" / "Snoozed until ..." stays this long, then the card goes
HIGHLIGHT_S = agenda.HIGHLIGHT_S
REFRESH_S = 20.0  # "Tomorrow" becomes "today": the rows are read again this often
MAX_ROWS = 40
# The ringing view: the bell left, the label and the time now right of it, two big buttons along the bottom.
BUTTON_H = 124
BUTTON_GAP = 32
_BUTTON_W = (W - 2 * 32 - BUTTON_GAP) // 2
SNOOZE_BUTTON = pygame.Rect(32, H - 22 - BUTTON_H, _BUTTON_W, BUTTON_H)
STOP_BUTTON = pygame.Rect(W - 32 - _BUTTON_W, H - 22 - BUTTON_H, _BUTTON_W, BUTTON_H)
BUTTON_PX = 64
RING_TOP = kit.PANEL.y  # the middle band, between the title and the buttons
RING_BOTTOM = SNOOZE_BUTTON.y - 16
BELL_PX = 210
BELL_CENTER = (196, (RING_TOP + RING_BOTTOM) // 2)
WORDS_X = 372  # the label and the time
LABEL_SIZES = (72, 64, 58, TITLE_PX, 46)
CLOCK_PX = 150
REPEAT_PX = BODY_PX
RING_TITLE_PX = 64
SNOOZE_COLOR: art.Color = art.SKY
STOP_COLOR: art.Color = art.STRAWBERRY
RING_COLOR: art.Color = art.LEMON
_WEEKDAYS = {d[:3]: d for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}
_WHEN = re.compile(r"^(?P<day>.*?)\s*(?P<clock>\d{1,2}(?::\d{2})?(?:\s*[ap]m)?)$", re.I)


def _linger(spec: dict[str, Any]) -> float | None:
    try:
        v = float(spec.get("linger"))
    except (TypeError, ValueError):
        return None
    return v if 0 < v <= 600 else None


def time_label(at: _dt.datetime, now: _dt.datetime, twelve: bool = True) -> str:
    """ "6 pm" (today), "Tomorrow 7 am", "Mon 9 am" (this week), "2 Nov 9 am"."""
    clock = clock_words(at, twelve)
    delta = (at.date() - now.date()).days
    if delta <= 0:
        return clock
    if delta == 1:
        return f"Tomorrow {clock}"
    if delta < 7:
        return f"{at:%a} {clock}"
    return f"{at.day} {at:%b} {clock}"


def day_label(at: _dt.datetime, now: _dt.datetime) -> str:
    """The heading a reminder goes under: "Today", "Tomorrow", "Monday" (this week), "Thu 12 Nov"."""
    delta = (at.date() - now.date()).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta < 7:
        return f"{at:%A}"
    return f"{at:%a} {at.day} {at:%b}"


def split_when(when: str) -> tuple[str, str]:
    """A time label as (day heading, clock): "Tomorrow 7 am" -> ("Tomorrow", "7 am"), "6 pm" -> ("Today", "6 pm"),
    "Mon 8 pm" -> ("Monday", "8 pm"); ("", when) when it has no clock at the end."""
    m = _WHEN.match(when.strip())
    if m is None:
        return "", when
    day = m["day"].strip().rstrip(",")
    return _WEEKDAYS.get(day, day) or "Today", m["clock"]


def bell(size: int, color: art.Color = RING_COLOR) -> pygame.Surface:
    """A friendly bell: a dome, a flared rim, a clapper and a little knob on top."""
    key = ("reminders_bell", size, color)

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            cx = n / 2
            edge = art.inkier(color, 0.4)
            for col, grow in ((edge, n * 0.03), (color, 0.0)):
                pygame.draw.circle(s, col, (cx, n * 0.14), n * 0.06 + grow)
                pygame.draw.circle(s, col, (cx, n * 0.44), n * 0.27 + grow)
                body = pygame.Rect(0, 0, n * 0.54 + 2 * grow, n * 0.32)
                body.midtop = (int(cx), int(n * 0.44))
                pygame.draw.rect(s, col, body)
                rim = pygame.Rect(0, 0, n * 0.80 + 2 * grow, n * 0.12 + 2 * grow)
                rim.midtop = (int(cx), int(n * 0.70 - grow))
                pygame.draw.rect(s, col, rim, border_radius=int(n * 0.06))
            pygame.draw.circle(s, edge, (cx, n * 0.86), n * 0.075)
            pygame.draw.circle(s, art.lighten(color, 0.55), (cx - n * 0.1, n * 0.36), n * 0.06)

        return art.supersampled((size, size), draw)

    return art.cached(key, make)


def _button(rect: pygame.Rect, label: str, color: art.Color, pressed: bool = False) -> pygame.Surface:
    key = ("reminders_button", rect.w, rect.h, label, color, pressed)

    def make() -> pygame.Surface:
        fill = art.inkier(color, 0.12) if pressed else color
        out = art.rounded_rect(rect.w, rect.h, rect.h // 2, fill, shadow=6, border=art.inkier(color, 0.35),
                               border_w=4, shine=not pressed).copy()
        txt = art.text_outlined(label, BUTTON_PX, art.WHITE, art.inkier(color, 0.55), 3)
        out.blit(txt, txt.get_rect(center=(rect.w // 2, rect.h // 2)))
        return out

    return art.cached(key, make)


class RemindersCard(Card):
    """The reminders coming up, or one ringing (Snooze / Stop)."""

    kind = "reminders"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, skill: Any = None,
                 ringing: int | None = None, twelve: bool = True, wall: Any = None) -> None:
        super().__init__(spec, host)
        self.skill = skill
        self.twelve = twelve
        self.wall = wall or (skill.now if skill is not None else time.time)
        self.ring_id = ringing
        self.ended: str | None = None  # "Stopped", "Snoozed until 7:09 am"
        self.ended_t = 0.0
        self.pressed: str | None = None
        self.highlight = kit.as_int(spec.get("highlight"), 0) or None
        self.linger_seconds = _linger(spec)
        self.page = 0
        self.page_t = 0.0
        self.rows: list[dict[str, Any]] = []  # the reminders, soonest first
        self._pages: list[list[dict[str, Any]]] = [[]]  # their agenda rows (day headings too), a page each
        self._version = -1
        self._read_t = -1e9
        self.ring_kind = str(spec.get("kind") or "alarm")
        self.ring_label = kit.clean(spec.get("label"), 60)
        self.ring_repeat = kit.clean(spec.get("repeat"), 60)
        self._load()
        if not self.title:
            self.title = self._noun().capitalize() if self.ringing_view else "Coming up"

    # ------------------------------------------------------------ what it shows
    @property
    def ringing_view(self) -> bool:
        return self.ring_id is not None

    @property
    def sticky(self) -> bool:  # type: ignore[override]
        return self.ringing_view and self.ended is None and not self.done

    @property
    def ringing(self) -> bool:
        """Still ringing: covered by another card (or a video), the screen service keeps it behind and brings it
        back as soon as it can (littlebot/screens/service.py), like a timer that has just finished."""
        return self.sticky

    @property
    def runs_behind(self) -> bool:
        return self.sticky

    def _local(self, ts: float) -> _dt.datetime:
        if self.skill is not None:
            try:
                return self.skill.local(ts)
            except Exception:  # noqa: BLE001
                pass
        return _dt.datetime.fromtimestamp(ts).astimezone()

    def _load(self) -> None:
        self._read_t = self.t
        if self.skill is None:
            self._load_sample()
        else:
            try:
                self._version = int(getattr(self.skill, "version", 0))
                if self.ring_id is not None:
                    item = self.skill.item(self.ring_id)
                    if item is not None:
                        self.ring_kind, self.ring_label = item.kind, item.label
                        self.ring_repeat = repeat_words(item.days)
                now = self._local(self.wall())
                rows = []
                for it in self.skill.upcoming():
                    at = self._local(it.fire_at)
                    rows.append({"id": it.id, "kind": it.kind,
                                 "text": it.text or ("Alarm" if it.kind == "alarm" else "Reminder"),
                                 "when": time_label(at, now, self.twelve), "day": day_label(at, now),
                                 "clock": clock_words(at, self.twelve), "repeat": repeat_words(it.days),
                                 "snoozed": it.snooze_until is not None})
                self.rows = rows[:MAX_ROWS]
            except Exception:  # noqa: BLE001 - keep what it had
                log.debug("reminders card: couldn't read the reminders", exc_info=True)
        self._pages = agenda.layout(self._agenda)[1]
        self.page = max(0, min(self.page, self.pages - 1))

    def _load_sample(self) -> None:
        rows = []
        raw = self.spec.get("items")
        for i, it in enumerate(raw if isinstance(raw, list) else []):
            if not isinstance(it, dict):
                continue
            when = kit.clean(it.get("when"), 24)
            day, clock = split_when(when)
            rows.append({"id": kit.as_int(it.get("id"), i + 1), "kind": "alarm" if it.get("kind") == "alarm" else
                         "reminder", "text": kit.clean(it.get("text"), 80) or "Reminder", "when": when,
                         "day": kit.clean(it.get("day"), 30) or day, "clock": clock,
                         "repeat": kit.clean(it.get("repeat"), 60), "snoozed": False})
        self.rows = rows[:MAX_ROWS]

    def _agenda(self, tier: agenda.Tier) -> list[dict[str, Any]]:
        """The reminders as agenda rows, each day under its heading."""
        out: list[dict[str, Any]] = []
        day = ""
        for r in self.rows:
            if r["day"] and r["day"] != day:
                day = r["day"]
                out.append(agenda.day_row(day, tier))
            alarm = r["kind"] == "alarm"
            text = r["text"][:1].upper() + r["text"][1:]
            out.append(agenda.event_row(
                r, tier=tier, clock=r["clock"], text=text, side=r["repeat"] or ("snoozed" if r.get("snoozed") else ""),
                side_kind="repeat" if r["repeat"] else "snooze", color=art.STRAWBERRY if alarm else art.SKY,
                icon=(lambda px: bell(px, RING_COLOR)) if alarm else None))
        return out

    @property
    def pages(self) -> int:
        return len(self._pages)

    def page_rows(self) -> list[dict[str, Any]]:
        """The reminders on this page."""
        return [r for r in self._pages[self.page] if r["type"] == "event"]

    def update(self, dt: float) -> None:
        super().update(dt)
        if self.skill is not None:
            if self.ringing_view and self.ended is None and not self.skill.is_ringing(self.ring_id):
                self._end(self._why_ended())
            if int(getattr(self.skill, "version", 0)) != self._version or self.t - self._read_t >= REFRESH_S:
                self._load()
        if self.ended is not None and self.t - self.ended_t >= ENDED_S:
            self.done = True
        self.mood = "surprised" if self.sticky else "happy" if self.ended else "neutral"

    def _why_ended(self) -> str:
        item = self.skill.item(self.ring_id) if self.skill is not None else None
        if item is not None and item.snooze_until is not None:
            return f"Snoozed until {clock_words(self._local(item.snooze_until), self.twelve)}"
        return "Stopped"

    def _end(self, text: str) -> None:
        self.ended, self.ended_t = text, self.t

    # ------------------------------------------------------------ touch and voice
    def _snooze(self, minutes: Any = None) -> bool:
        if not self.ringing_view or self.ended is not None:
            return False
        if self.skill is not None:
            done = self.skill.snooze(kit.as_int(minutes, 0) or None, rid=self.ring_id)
            if not done:
                return False
            self._end(self._why_ended())
        else:
            self._end("Snoozed")
        return True

    def _stop(self) -> bool:
        if not self.ringing_view or self.ended is not None:
            return False
        if self.skill is not None:
            self.skill.stop_ringing(self.ring_id)
        self._end("Stopped")
        return True

    def _turn(self, page: int) -> bool:
        page = max(0, min(self.pages - 1, page))
        if page != self.page:
            self.page, self.page_t = page, self.t
        return True

    def tap(self, x: float, y: float) -> bool:
        if self.ringing_view:
            what = self._noun()
            if SNOOZE_BUTTON.inflate(16, 16).collidepoint(x, y) and self._snooze():
                self.pressed = "snooze"
                self.host.sound("click")
                self.host.tell(f"The {what} was snoozed on the screen ({self.ended}).")
                return True
            if STOP_BUTTON.inflate(16, 16).collidepoint(x, y) and self._stop():
                self.pressed = "stop"
                self.host.sound("click")
                self.host.tell(f"The {what} was stopped on the screen.")
                return True
            return False
        if self.pages > 1:
            if agenda.on_back_arrow(x, y):
                return self.command("prev", {})
            return self.command("next" if self.page < self.pages - 1 else "page", {"n": 1})
        return False

    def key(self, name: str) -> bool:
        if self.ringing_view:
            if name in ("s", "z"):
                return self._snooze()
            if name in ("return", "space"):
                return self._stop()
            return False
        if name in ("right", "space", "down", "return"):
            return self.command("next", {})
        if name in ("left", "up", "backspace"):
            return self.command("prev", {})
        return False

    def command(self, action: str, args: dict[str, Any]) -> bool:
        args = args if isinstance(args, dict) else {}
        if action == "stop":
            return self._stop()
        if action == "snooze":
            return self._snooze(args.get("minutes"))
        if action == "next":
            return self.pages > 1 and self._turn(self.page + 1)
        if action == "prev":
            return self.pages > 1 and self._turn(self.page - 1)
        if action == "page":
            n = kit.as_int(args.get("n"), 0)
            return 1 <= n <= self.pages and self._turn(n - 1)
        return False

    def _noun(self) -> str:
        return "alarm" if self.ring_kind == "alarm" else "reminder"

    def describe(self) -> str:
        if self.ringing_view:
            label = self.ring_label or self._noun()
            if self.ended is not None:
                return f"The {self._noun()} ({label}): {self.ended}"
            now = clock_words(self._local(self.wall()), self.twelve)
            return (f"{self._noun().capitalize()} ringing: {label} (it's {now}); Snooze and Stop buttons on the "
                    "screen")
        if not self.rows:
            return "No reminders or alarms coming up"
        n = len(self.rows)
        shown = "; ".join(f"{r['when']}: {r['text']}{' (alarm)' if r['kind'] == 'alarm' else ''}"
                          for r in self.page_rows())
        paging = f" (page {self.page + 1} of {self.pages})" if self.pages > 1 else ""
        return f"Coming up, {n}{paging}: {shown}"

    def status(self) -> dict[str, Any]:
        return {**super().status(), "ringing": self.ring_id, "ended": self.ended, "rows": len(self.rows),
                "page": self.page + 1, "pages": self.pages}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: pygame.Surface) -> None:
        if self.ringing_view:
            self._draw_ringing(surf)
        else:
            self._draw_list(surf)

    def _draw_list(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=37)
        kit.header_title(surf, self.title)
        kit.panel(surf)
        if not self.rows:
            agenda.draw_empty(surf, bell(agenda.EMPTY_ICON, art.lighten(RING_COLOR, 0.2)), "Nothing coming up",
                              "Say “remind me at 6 to call mom”")
            return
        agenda.draw_page(surf, self._pages[self.page], paged=self.pages > 1,
                         enter=kit.appear(self.t - self.page_t, 0.0, 0.3), t=self.t,
                         highlight=self.highlight)
        agenda.draw_paging(surf, self.pages, self.page)

    def _draw_ringing(self, surf: pygame.Surface) -> None:
        kit.background(surf, seed=43)  # the whole 1024 x 600 canvas
        noun = self._noun().capitalize()
        kit.header_title(surf, f"{noun}!" if self.ended is None else noun, size=RING_TITLE_PX)
        pulse = 0.5 + 0.5 * math.sin(self.t * 5.0)
        if self.ended is None:
            art.blit_center(surf, art.glow(BELL_PX * 0.95, RING_COLOR, 1.0), BELL_CENTER, int(110 + 140 * pulse))
            wobble = math.sin(self.t * 14.0) * 16 * (0.6 + 0.4 * pulse)
            art.blit_center(surf, art.rotated(bell(BELL_PX), wobble, key=("reminders_bell", BELL_PX), step=2.0),
                            BELL_CENTER)
        else:
            art.blit_center(surf, bell(BELL_PX, art.mix(RING_COLOR, art.WHITE, 0.45)), BELL_CENTER)
        col_w = W - WORDS_X - 32
        label = self.ring_label or self._noun()
        label = label[:1].upper() + label[1:]
        size, lines = kit.fit(label, LABEL_SIZES, col_w, max_lines=2)
        if len(lines) > 2:
            lines = (lines[0], kit.ellipsize(" ".join(lines[1:]), size, col_w))
        block = kit.text_block(list(lines), size, art.INK)
        clock = candy.fit(clock_words(self._local(self.wall()), self.twelve), CLOCK_PX, col_w,
                          art.inkier(art.GRAPE, 0.2), min_size=96)
        repeat = self._repeat_sprite(col_w)
        gap = 12
        total = block.get_height() + gap + clock.get_height() + (repeat.get_height() + 4 if repeat else 0)
        top = (RING_TOP + RING_BOTTOM - total) // 2
        surf.blit(block, (WORDS_X, top))
        top += block.get_height() + gap
        surf.blit(clock, (WORDS_X - 4, top))
        if repeat is not None:
            surf.blit(repeat, (WORDS_X, top + clock.get_height() + 4))
        if self.ended is None:
            for rect, text, color, name in ((SNOOZE_BUTTON, "Snooze", SNOOZE_COLOR, "snooze"),
                                            (STOP_BUTTON, "Stop", STOP_COLOR, "stop")):
                surf.blit(_button(rect, text, color, self.pressed == name), rect.topleft)
        else:
            msg = candy.chip(self.ended, TITLE_PX, art.INK, art.lighten(art.MINT, 0.6))
            surf.blit(msg, msg.get_rect(center=(W // 2, SNOOZE_BUTTON.centery)))


    def _repeat_sprite(self, width: int) -> pygame.Surface | None:
        """ "every weekday" with the repeat icon, under the time (cached)."""
        if not self.ring_repeat:
            return None
        key = ("reminders_repeat", self.ring_repeat, width)

        def make() -> pygame.Surface:
            icon = agenda.side_icon("repeat", REPEAT_PX - 4)
            words = art.text(kit.ellipsize(self.ring_repeat, REPEAT_PX, width - icon.get_width() - 12), REPEAT_PX,
                             kit.TEXT_SOFT)
            out = pygame.Surface((icon.get_width() + 12 + words.get_width(), max(icon.get_height(),
                                                                                 words.get_height())), pygame.SRCALPHA)
            out.blit(icon, icon.get_rect(midleft=(0, out.get_height() // 2)))
            out.blit(words, words.get_rect(midleft=(icon.get_width() + 12, out.get_height() // 2 + 1)))
            return out

        return art.cached(key, make)

def _skill(ctx: Any) -> Any:
    skills = getattr(ctx, "skills", None) if ctx is not None else None
    try:
        return skills.get("reminders") if skills is not None and hasattr(skills, "get") else None
    except Exception:  # noqa: BLE001
        return None


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> RemindersCard | None:
    """The reminders coming up, or (``spec["ringing"]``: an id) the one ringing; the ringing card already on the
    screen for that id when there is one."""
    skill = _skill(ctx)
    twelve = candy.uses_12h(ctx) if ctx is not None else bool(spec.get("twelve_hour", True))
    rid = kit.as_int(spec.get("ringing"), 0) or None
    if skill is None:  # previews: the rows (or the ringing label) are in the spec
        return RemindersCard(spec, host, ringing=rid, twelve=twelve)
    if rid is not None and not skill.is_ringing(rid):
        rid = None  # it stopped before the card went up: what's coming up instead
    if rid is not None:
        current = getattr(getattr(ctx, "screens", None), "current", None)
        if isinstance(current, RemindersCard) and current.ring_id == rid and current.sticky:
            return current
    return RemindersCard(spec, host, skill=skill, ringing=rid, twelve=twelve)


register(CardType(
    kind="reminders",
    description="The reminders and alarms coming up, with their times ('what reminders do I have?', 'show my "
                "alarms'). They come from the robot's own reminders.",
    fields={},
    build=build,
    required=(),
    actions={"next": "the next page", "prev": "the page before", "stop": "stop the one ringing",
             "snooze": "snooze the one ringing: {\"minutes\": 9}"},
))

# Example specs for scripts/screen_preview.py (no skill there: the rows are given).
SAMPLES: list[dict[str, Any]] = [
    {"card": "reminders", "title": "Coming up", "items": [
        {"text": "Call mom", "when": "6 pm"}, {"text": "Wake up", "when": "Tomorrow 7 am", "kind": "alarm",
                                               "repeat": "every weekday"},
        {"text": "Take the bins out", "when": "Mon 8 pm", "repeat": "every Monday"},
        {"text": "Amara's check-up", "when": "12 Nov 10:30 am"}]},
    {"card": "reminders", "ringing": 1, "kind": "alarm", "label": "Wake up", "repeat": "every weekday"},
    {"card": "reminders", "items": []},
]

__all__ = ["SAMPLES", "RemindersCard", "bell", "day_label", "split_when", "time_label"]
