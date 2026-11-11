"""LittleBot's own settings, on its screen: for a grown-up standing at the robot.

Opened by holding a finger still on the screen for 5 seconds, anywhere (littlebot/face/display.py; a running game
stops for it): ``screens.show({"card": "settings"}, source="touch")``. Never offered to the AI
(``CardType.offered=False``).

* A rail of three big tabs on the left, under LittleBot's eyes: **Settings**, **Games**, **About** (the open one
  filled in its colour), and a red **Close** button top right. A tap on the eyes closes it too (the screen
  service), and so does Esc in the desktop simulator.
* **Settings**, a row each (drag to scroll, like the games menu): the speaker **Volume** and the screen
  **Brightness** ( - / a bar with the percent / + in steps of 10 %, or a tap on the bar), **Dark mode**, the
  **Upside-down screen** and **Info cards in chats** (switches: a tap anywhere on the row), **Go to sleep**,
  **Always recording** (a tap goes round Auto, On, Off: the setting ``capture.always_recording``,
  littlebot/capture/buffer.py) and **Save the last 3 minutes** (what that buffer holds becomes a video in the
  gallery: ``ctx.capture.save_buffer``, and LittleBot says so) and **WhatsApp** (Open: WhatsApp Web for a video
  call takes the screen and this card goes, ``ctx.calls.open(source="screen")``; Close while it is open).
  The volume goes through ``ctx.audio.set_volume`` (it clamps to the safe maximum and keeps the setting), like the
  voice's tools; the others are settings the display loop and the screen service follow (``display.brightness``,
  ``display.theme``, ``display.flipped``, ``screens.enabled``); sleep is what the voice's ``go_to_sleep`` tool does.
  The row changes at once; the change itself runs on the robot's loop, never on the display thread (it writes
  SQLite), the newest value per setting only. A change made elsewhere meanwhile (the voice, the dashboard, an edge
  swipe) shows up within half a second.
* **Games**: every on-screen game as a big tile with the games menu's drawn icon; a tap starts it
  (``ctx.games.start(id, source="screen")``): the game takes the screen and this card goes.
* **About**: the parent dashboard's address(es), the battery (when one is fitted) and how to open these settings.
* Keys (the simulator): the arrows move a highlight between the tabs, Close and the controls (to the nearest one
  that way), Enter / space presses it; page up / down scroll.
* Outside a chat it goes a minute after the last touch (``linger_seconds``), like the games menu.

Spec: ``{"card": "settings", "tab": "settings" | "games" | "about"}`` (default the settings).
"""

from __future__ import annotations

import asyncio
import logging
import math
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pygame

from littlebot.games import art
from littlebot.games.catalog import GAMES, GameInfo
from littlebot.screens.base import (
    BODY_PX,
    PRESENTER_BOX,
    SMALL_PX,
    TITLE_PX,
    Card,
    CardType,
    H,
    W,
    register,
)
from littlebot.screens.cards import _kit
from littlebot.screens.cards.games import COLOURS as GAME_COLOURS
from littlebot.screens.cards.games import _starter, game_icon, pick

log = logging.getLogger("littlebot.screens.settings")

KIND = "settings"
SOURCE = "screen"  # the settings' change source (bus events, logs)
TABS = ("settings", "games", "about")
TAB_NAMES = {"settings": "Settings", "games": "Games", "about": "About"}
TAB_COLOURS = {"settings": art.SKY, "games": art.TANGERINE, "about": art.GRAPE}
TOUCH_LINGER = 60.0  # s without a touch before it goes (outside a chat), like the games menu
POLL_SECONDS = 0.5  # how often a change made elsewhere is picked up (in-memory reads)
NOTICE_SECONDS = 3.5
FLASH_SECONDS = 0.18  # a tapped control lights up this long
FRICTION = 4.5  # fling slow-down, 1/s
STEP = 0.1  # the volume and the brightness move in steps of 10 %
BRIGHTNESS_MIN = 0.15  # never darker (the display's floor, littlebot/face/edge_controls.py)
DEFAULT_MAX_VOLUME = 0.8
LOW_BATTERY = 20  # percent: the battery shows red below this (littlebot/face/battery_badge.py LOW_PERCENT)

# The settings this card shows (and their defaults when there are no settings: a preview).
VOLUME, BRIGHTNESS, THEME, FLIPPED, CARDS = ("audio.volume", "display.brightness", "display.theme",
                                             "display.flipped", "screens.enabled")
ALWAYS = "capture.always_recording"  # auto | on | off (littlebot/capture/buffer.py)
ALWAYS_MODES = ("auto", "on", "off")
ALWAYS_NAMES = {"auto": "Auto", "on": "On", "off": "Off"}
ALWAYS_WORDS = {"auto": "While plugged in", "on": "On the battery too", "off": "Nothing is kept"}
DEFAULTS: dict[str, Any] = {VOLUME: 0.5, BRIGHTNESS: 1.0, THEME: "light", FLIPPED: False, CARDS: True,
                            ALWAYS: "auto"}

# ---------------------------------------------------------------- layout (the 1024x600 canvas)
_PX, _PY, _PW, _PH = PRESENTER_BOX
TOP = _PY + _PH + 14  # 132: the panel starts under the eyes and the title
EYES_MARGIN = 14  # the screen service's tap margin round the eyes (a tap there closes): nothing of ours in it
RAIL = pygame.Rect(_PX, TOP + 6, _PW + 12, H - 14 - (TOP + 6))  # the tabs: 196 x 448 under the eyes
TAB_GAP = 20
CLOSE = pygame.Rect(W - 16 - 196, 14, 196, 96)  # top right
PANEL = pygame.Rect(RAIL.right + 16, TOP, W - 16 - (RAIL.right + 16), H - 14 - TOP)  # 780 x 454
VIEW = PANEL.inflate(-8, -8)  # what scrolls is clipped to this (the panel's inside)
TITLE_X = _PX + _PW + 20
LANE = 26  # the scrollbar's lane, right in the panel
BUTTON = 90  # every button is at least this tall (and the - / + ones this wide): ~14 mm on the glass

# the settings' rows
ROW_H = 104
ROW_X = PANEL.x + 20
ROW_W = PANEL.right - LANE - 8 - ROW_X
ROW_PAD = 6  # above the first row and below the last
BAR_W = 236
SWITCH_W, SWITCH_H = 128, 64
SLEEP_W = 236
ICON_PX = 52

# the games' tiles
G_COLS = 2
G_GAP = 16
G_X = PANEL.x + 18
G_W = (PANEL.right - LANE - 8 - G_X - G_GAP) // G_COLS
G_H = 128
G_PAD = 16
G_ICON = 100


@dataclass(frozen=True)
class Control:
    """Something to tap: its id ("vol+", "dark", "game:bubbles", "tab:about", "close"), where it is now
    (scrolled), whether the arrow keys stop on it, and whether it is in the scrolling panel."""

    id: str
    rect: pygame.Rect
    focusable: bool = True
    scrolls: bool = True


# ---------------------------------------------------------------- the robot side
class RobotLink:
    """What the settings card reads from the robot and asks of it (made from ``ctx`` when the card is built; with
    no ``ctx``, a preview's: defaults, and changes kept here).

    Reads are in-memory and cheap (the settings, the battery's last reading). Every change runs off the display
    thread on the robot's loop (``ctx.runtime``), like the display's edge swipes: only the newest value per setting,
    each landed value (the volume as the speaker clamped it) handed back through :meth:`landed`. With no running
    loop (tests) it is done at once.
    """

    def __init__(self, ctx: Any = None) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[Callable[[Any], Any], Any]] = {}
        self._running: set[str] = set()
        self._landed: dict[str, Any] = {}
        self._failed: set[str] = set()
        self._local = dict(DEFAULTS)  # no ctx: the "settings"
        self.start_game = _starter(ctx) if ctx is not None else None

    # ------------------------------------------------------------ reads
    def setting(self, key: str) -> Any:
        settings = getattr(self.ctx, "settings", None)
        if settings is None:
            return self._local.get(key, DEFAULTS.get(key))
        try:
            return settings.get(key, DEFAULTS.get(key))
        except Exception:  # noqa: BLE001
            return DEFAULTS.get(key)

    def snapshot(self) -> dict[str, Any]:
        """The settings now (the volume as kept in ``audio.volume``: the speaker keeps it there)."""
        return {k: self.setting(k) for k in DEFAULTS}

    def speaker(self) -> tuple[float, float]:
        """(volume, the safe maximum), 0..1, read like the voice's tools: the speaker's own, else the setting."""
        cfg = getattr(getattr(getattr(self.ctx, "config", None), "audio", None), "speaker", None)
        top = _num(getattr(cfg, "max_volume", None), DEFAULT_MAX_VOLUME)
        audio = getattr(self.ctx, "audio", None)
        st: Any = None
        if audio is not None and hasattr(audio, "status"):
            try:
                st = audio.status()
            except Exception:  # noqa: BLE001
                st = None
        st = st if isinstance(st, dict) else {}
        v = st.get("volume")
        if not isinstance(v, (int, float)):
            v = self.setting(VOLUME)
        top = _num(st.get("max_volume"), top)
        return art.clamp(_num(v, DEFAULTS[VOLUME]), 0.0, 1.0), art.clamp(top, 0.0, 1.0)

    def battery(self) -> dict[str, Any] | None:
        """The battery's last reading (``power.battery``'s payload) when one is fitted, else None."""
        power = getattr(self.ctx, "power", None)
        if power is None:
            return None
        try:
            st = power.state()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(st, dict) or not st.get("fitted") or not isinstance(st.get("percent"), (int, float)):
            return None
        return {"percent": max(0, min(100, round(float(st["percent"])))), "charging": bool(st.get("charging")),
                "full": bool(st.get("charged")), "plugged_in": bool(st.get("plugged_in"))}

    def dashboard(self) -> tuple[list[str], bool]:
        """The parent dashboard's addresses as they would be typed on a phone (no "http://"), the ones another
        device can reach first; and True when only the robot itself can open it (LAN access off). Read from the
        running dashboard (``robot.dashboard.dash.urls``), else ``<hostname>.local`` and the configured port."""
        ctx = self.ctx
        robot = (getattr(ctx, "extras", None) or {}).get("robot")
        dash = getattr(getattr(robot, "dashboard", None), "dash", None)
        urls = [str(u) for u in (getattr(dash, "urls", None) or []) if u]
        if not urls:
            cfg = getattr(getattr(ctx, "config", None), "dashboard", None)
            port = int(_num(getattr(dash, "port", None) or getattr(cfg, "port", None), 8765))
            urls = [f"http://{_hostname()}.local:{port}/"]
        outside = [u for u in urls if (urlsplit(u).hostname or "") not in ("127.0.0.1", "localhost", "::1")]
        return [_short(u) for u in (outside or urls)][:3], not outside

    # ------------------------------------------------------------ changes
    def set_setting(self, key: str, value: Any) -> None:
        settings = getattr(self.ctx, "settings", None)
        if settings is None:
            self._local[key] = value
            return
        self._offload(key, lambda v: settings.set(key, v, source=SOURCE), value)

    def set_volume(self, value: float) -> None:
        """The speaker's volume: ``ctx.audio.set_volume`` (clamps to the safe maximum and keeps the setting), else
        the setting the live voice reads (like the voice's tools and the display's edge swipe)."""
        audio = getattr(self.ctx, "audio", None)
        if audio is not None and hasattr(audio, "set_volume"):
            self._offload(VOLUME, audio.set_volume, value)
        else:
            self.set_setting(VOLUME, value)

    def sleep(self, done: Callable[[bool], None]) -> None:
        """What the voice's ``go_to_sleep`` tool does: ``robot.sleep``, else the state machine. ``done(ok)``."""
        ctx = self.ctx
        if ctx is None:
            done(False)
            return

        def go(_: Any) -> bool:
            from littlebot.core.state import RobotState  # noqa: PLC0415

            if ctx.state.state == RobotState.SLEEPING:
                return True
            robot = (getattr(ctx, "extras", None) or {}).get("robot")
            if robot is not None and callable(getattr(robot, "sleep", None)):
                return bool(robot.sleep(SOURCE))
            return bool(ctx.state.transition(RobotState.SLEEPING, f"sleep ({SOURCE})"))

        self._offload("sleep", go, None, done=done)

    def clip_minutes(self) -> float:
        """How many minutes the always-recording buffer keeps (``capture.buffer_minutes``)."""
        cfg = getattr(getattr(self.ctx, "config", None), "capture", None)
        return _num(getattr(cfg, "buffer_minutes", None), 3.0)

    def save_clip(self, done: Callable[[bool, str], None]) -> None:
        """"Save that" from the screen: the always-recording buffer into the gallery, on the robot's loop
        (``ctx.capture.save_buffer``; LittleBot says what it saved). ``done(ok, words)``."""
        capture = getattr(self.ctx, "capture", None)
        if capture is None or not hasattr(capture, "save_buffer"):
            done(False, "Always recording isn't available.")
            return

        async def go() -> None:
            try:
                res = await capture.save_buffer(source=SOURCE, announce=True)
            except Exception:  # noqa: BLE001
                log.exception("saving the last minutes from the settings failed")
                res = {"ok": False}
            done(bool(res.get("ok")), str(res.get("words") if res.get("ok") else res.get("detail") or ""))

        runtime = getattr(self.ctx, "runtime", None)
        coro = go()
        try:
            if runtime is not None and getattr(runtime, "running", False):
                runtime.submit(coro)
            else:
                asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            done(False, "LittleBot can't save it right now.")

    def whatsapp_on(self) -> bool:
        """WhatsApp (littlebot/calls) is on the screen right now."""
        calls = getattr(self.ctx, "calls", None)
        return calls is not None and getattr(calls, "active", False) is True

    def whatsapp(self, done: Callable[[bool, str], None]) -> None:
        """The WhatsApp row: open WhatsApp for a video call (``ctx.calls.open``), or close it when it is open, on
        the robot's loop. ``done(ok, words)``: the words say why it didn't open."""
        calls = getattr(self.ctx, "calls", None)
        if calls is None or not hasattr(calls, "open"):
            done(False, "WhatsApp calls aren't available.")
            return

        async def go() -> None:
            try:
                if getattr(calls, "active", False) is True:
                    await calls.hang_up("closed in the settings on the screen")
                    res: dict[str, Any] = {"ok": True}
                else:
                    res = await calls.open(source=SOURCE)
            except Exception:  # noqa: BLE001
                log.exception("opening WhatsApp from the settings failed")
                res = {"ok": False}
            done(bool(res.get("ok")), "" if res.get("ok") else str(res.get("detail") or "WhatsApp didn't open."))

        runtime = getattr(self.ctx, "runtime", None)
        coro = go()
        try:
            if runtime is not None and getattr(runtime, "running", False):
                runtime.submit(coro)
            else:
                asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            done(False, "LittleBot can't open WhatsApp right now.")

    def busy(self, key: str) -> bool:
        """A change of ``key`` is on its way (its row keeps what was tapped meanwhile)."""
        with self._lock:
            return key in self._running or key in self._pending

    def landed(self) -> tuple[dict[str, Any], set[str]]:
        """The changes that landed since the last call ({key: value as kept}) and the keys that failed."""
        with self._lock:
            landed, failed = self._landed, self._failed
            self._landed, self._failed = {}, set()
        return landed, failed

    def _offload(self, key: str, fn: Callable[[Any], Any], value: Any,
                 done: Callable[[bool], None] | None = None) -> None:
        runtime = getattr(self.ctx, "runtime", None)
        with self._lock:
            self._pending[key] = (fn, value)
            if key in self._running:
                return  # the running worker takes the newest value
            self._running.add(key)

        def run_one() -> bool:
            with self._lock:
                item = self._pending.pop(key, None)
                if item is None:
                    self._running.discard(key)
                    return False
            try:
                out = item[0](item[1])
            except Exception:  # noqa: BLE001
                log.warning("settings card: could not change %s", key, exc_info=True)
                with self._lock:
                    self._failed.add(key)
                ok = False
            else:
                with self._lock:
                    self._landed[key] = out
                ok = out is not False
            if done is not None:
                done(ok)
            return True

        async def worker() -> None:
            while True:
                with self._lock:
                    if key not in self._pending:
                        self._running.discard(key)
                        return
                await asyncio.to_thread(run_one)

        if runtime is not None and getattr(runtime, "running", False):
            coro = worker()
            try:
                runtime.submit(coro)
                return
            except Exception:  # noqa: BLE001 - shutting down: do it here
                coro.close()
        while run_one():  # no loop (tests, a preview): at once
            pass


def _num(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    return f if math.isfinite(f) else float(default)


def _hostname() -> str:
    try:
        name = socket.gethostname().strip()
    except OSError:
        name = ""
    return name if name and "." not in name else "littlebot"


def _short(url: str) -> str:
    """ "http://192.168.1.20:8765/" -> "192.168.1.20:8765" (what to type on a phone)."""
    s = str(url).strip()
    for scheme in ("http://", "https://"):
        s = s.removeprefix(scheme)
    return s.rstrip("/")


def step_level(value: float, sign: int, lo: float, hi: float) -> float:
    """One 10 % step up or down from ``value``, on the tens (0.15 -> 0.2 -> 0.3 ...), kept in lo..hi."""
    tens = value * 10
    nxt = (math.floor(tens + 1e-6) + 1) / 10 if sign > 0 else (math.ceil(tens - 1e-6) - 1) / 10
    return round(art.clamp(nxt, lo, hi), 3)


# ---------------------------------------------------------------- the card
class SettingsCard(Card):
    kind = KIND

    def __init__(self, spec: dict[str, Any], host: Any = None, *, link: RobotLink | None = None,
                 games: list[GameInfo] | None = None) -> None:
        super().__init__(spec, host)
        self.title = "LittleBot's settings"
        self.linger_seconds = TOUCH_LINGER  # the service: how long it stays after a chat / since the last touch
        self.link = link if link is not None else RobotLink()
        self.games = list(games if games is not None else GAMES)
        tab = str(spec.get("tab") or "settings").strip().lower()
        self.tab = tab if tab in TABS else "settings"
        self.offsets = {t: 0.0 for t in TABS}  # px scrolled, per tab
        self.velocity = 0.0
        self.dragging = False
        self.focus = f"tab:{self.tab}"  # the arrow keys' highlight
        self.keyboard = False  # the highlight shows once a key was pressed (not for a finger)
        self.values = self.link.snapshot()
        self._seen = dict(self.values)  # the settings as last read (a change elsewhere is a difference)
        volume, self.max_volume = self.link.speaker()
        self.values[VOLUME] = volume
        self.battery = self.link.battery()
        self.urls, self.local_only = self.link.dashboard()
        self.notice = ""
        self.notice_until = 0.0
        self.flash: tuple[str, float] | None = None  # (control id, when) a tapped control lights up
        self.starting: str | None = None  # a game being started
        self.sleeping = False  # asked to sleep (the service closes the card when LittleBot falls asleep)
        self.saving = False  # "Save the last 3 minutes" is on its way
        self.calling = False  # the WhatsApp row was tapped: opening (or closing) is on its way
        self.whatsapp_on = self.link.whatsapp_on()
        self.clip_minutes = self.link.clip_minutes()
        self._poll = 0.0
        self._done_lock = threading.Lock()
        self._results: list[tuple[str, bool, str]] = []  # (what, ok, why) from the robot's loop

    # ------------------------------------------------------------ the values
    @property
    def volume(self) -> float:
        return float(self.values.get(VOLUME) or 0.0)

    @property
    def brightness(self) -> float:
        return art.clamp(_num(self.values.get(BRIGHTNESS), 1.0), BRIGHTNESS_MIN, 1.0)

    @property
    def dark(self) -> bool:
        return str(self.values.get(THEME) or "light").lower() == "dark"

    @property
    def flipped(self) -> bool:
        return bool(self.values.get(FLIPPED))

    @property
    def cards_on(self) -> bool:
        return bool(self.values.get(CARDS))

    @property
    def always(self) -> str:
        v = str(self.values.get(ALWAYS) or "auto").lower()
        return v if v in ALWAYS_MODES else "auto"

    # ------------------------------------------------------------ layout
    def tab_rect(self, name: str) -> pygame.Rect:
        i = TABS.index(name)
        h = (RAIL.h - TAB_GAP * (len(TABS) - 1)) // len(TABS)
        return pygame.Rect(RAIL.x, RAIL.y + i * (h + TAB_GAP), RAIL.w, h)

    def content_height(self, tab: str | None = None) -> int:
        tab = tab or self.tab
        if tab == "settings":
            return 2 * ROW_PAD + len(_ROWS) * ROW_H
        if tab == "games":
            rows = -(-len(self.games) // G_COLS)
            return 2 * G_PAD + rows * G_H + max(0, rows - 1) * G_GAP
        return VIEW.h

    @property
    def max_offset(self) -> float:
        return max(0.0, float(self.content_height() - VIEW.h))

    @property
    def offset(self) -> float:
        return self.offsets[self.tab]

    @offset.setter
    def offset(self, value: float) -> None:
        self.offsets[self.tab] = float(value)

    def row_rect(self, i: int) -> pygame.Rect:
        """The settings tab's row ``i`` (volume, brightness, dark, flip, cards, sleep, always, clip), scrolled."""
        return pygame.Rect(ROW_X, int(VIEW.y + ROW_PAD + i * ROW_H - self.offsets["settings"]), ROW_W, ROW_H)

    def tile_rect(self, i: int) -> pygame.Rect:
        """The games tab's tile ``i`` (reading order), scrolled."""
        row, col = divmod(i, G_COLS)
        return pygame.Rect(G_X + col * (G_W + G_GAP), int(VIEW.y + G_PAD + row * (G_H + G_GAP) - self.offsets["games"]),
                           G_W, G_H)

    def controls(self) -> list[Control]:
        """Everything tappable now: the tabs, Close, and the open tab's controls (scrolled; some may be out of
        the panel)."""
        out = [Control(f"tab:{t}", self.tab_rect(t), scrolls=False) for t in TABS]
        out.append(Control("close", CLOSE.copy(), scrolls=False))
        if self.tab == "settings":
            for i, (rid, kind) in enumerate(_ROWS):
                r = self.row_rect(i)
                cy = r.centery
                if kind == "level":
                    plus = pygame.Rect(r.right - BUTTON, cy - BUTTON // 2, BUTTON, BUTTON)
                    bar = pygame.Rect(plus.x - 12 - BAR_W, cy - BUTTON // 2, BAR_W, BUTTON)
                    minus = pygame.Rect(bar.x - 12 - BUTTON, cy - BUTTON // 2, BUTTON, BUTTON)
                    out += [Control(f"{rid}-", minus), Control(f"{rid}:bar", bar, focusable=False),
                            Control(f"{rid}+", plus)]
                elif kind == "switch":
                    out.append(Control(rid, r.copy()))  # the whole row: a big target
                else:  # the sleep button
                    out.append(Control(rid, pygame.Rect(r.right - SLEEP_W, cy - BUTTON // 2, SLEEP_W, BUTTON)))
        elif self.tab == "games":
            out += [Control(f"game:{g.id}", self.tile_rect(i)) for i, g in enumerate(self.games)]
        return out

    def control(self, cid: str) -> Control | None:
        return next((c for c in self.controls() if c.id == cid), None)

    def control_at(self, x: float, y: float) -> Control | None:
        for c in self.controls():
            if c.scrolls and not VIEW.collidepoint(x, y):
                continue  # scrolled out of the panel: not there
            if c.rect.collidepoint(x, y):
                return c
        return None

    @property
    def animating(self) -> bool:
        return (self.dragging or abs(self.velocity) > 1.0 or self.starting is not None
                or (self.flash is not None and self.t - self.flash[1] < FLASH_SECONDS))

    # ------------------------------------------------------------ logic
    def update(self, dt: float) -> None:
        super().update(dt)
        if not self.dragging and abs(self.velocity) > 1.0:
            self.offset += self.velocity * dt
            self.velocity *= math.exp(-FRICTION * dt)
        if not self.dragging:  # spring back inside the list
            lo, hi = 0.0, self.max_offset
            if self.offset < lo or self.offset > hi:
                target = lo if self.offset < lo else hi
                self.offset += (target - self.offset) * min(1.0, 12.0 * dt)
                self.velocity = 0.0
                if abs(self.offset - target) < 0.5:
                    self.offset = target
        if self.notice and self.t >= self.notice_until:
            self.notice = ""
        self._take_results()
        self._poll -= dt
        if self._poll <= 0:
            self._poll = POLL_SECONDS
            self.sync()

    def sync(self) -> None:
        """Pick up what landed, and changes made elsewhere (the voice, the dashboard, an edge swipe)."""
        landed, failed = self.link.landed()
        for key, value in landed.items():
            if key in DEFAULTS and value is not None and not self.link.busy(key):
                self.values[key] = value
        now = self.link.snapshot()
        for key, value in now.items():
            if self.link.busy(key):
                continue
            if key in failed or value != self._seen.get(key):
                self.values[key] = value
            self._seen[key] = value
        if self.tab == "about":
            self.battery = self.link.battery()

    def _take_results(self) -> None:
        with self._done_lock:
            results, self._results = self._results, []
        for what, ok, why in results:
            if what == "sleep":
                self.sleeping = ok
                if not ok:
                    self.say_notice("LittleBot can't go to sleep right now.")
                    self.host.sound("uhoh")
            elif what == "whatsapp":
                self.calling = False
                self.whatsapp_on = self.link.whatsapp_on()
                if not ok:
                    self.say_notice(why or "WhatsApp didn't open.")
                    self.host.sound("uhoh")
            elif what == "clip":
                self.saving = False
                self.say_notice(why or ("Saved." if ok else "That couldn't be saved."))
                if not ok:
                    self.host.sound("uhoh")
            elif what.startswith("game:"):
                name = next((g.name for g in self.games if g.id == what[5:]), "That game")
                self.starting = None
                if not ok:
                    self.say_notice(why or f"{name} can't start right now.")
                    self.host.sound("uhoh")

    def _result(self, what: str) -> Callable[..., None]:
        """A callback for the robot's loop: noted, and taken in on the display thread (``update``)."""

        def done(ok: bool, why: str = "") -> None:
            with self._done_lock:
                self._results.append((what, bool(ok), str(why or "")))

        return done

    # ------------------------------------------------------------ touch
    def scroll_by(self, dy: float) -> None:
        self.offset = art.clamp(self.offset + dy, -60.0, self.max_offset + 60.0)

    def drag(self, dx: float, dy: float) -> None:
        """The finger moved by (dx, dy) canvas px (the screen service calls this while dragging)."""
        if self.max_offset <= 0:
            return
        self.dragging = True
        self.scroll_by(-dy)
        self.velocity = art.lerp(self.velocity, -dy * 30.0, 0.5)  # ~30 fps of motion events

    def drag_end(self) -> None:
        self.dragging = False
        self.velocity = art.clamp(self.velocity, -2400.0, 2400.0)

    def tap(self, x: float, y: float) -> bool:
        self.velocity = 0.0
        self.keyboard = False
        c = self.control_at(x, y)
        if c is None:
            return False
        if c.focusable:
            self.focus = c.id
        self.press(c.id, x)
        return True

    def key(self, name: str) -> bool:
        name = str(name or "").lower()
        if name in ("up", "down", "left", "right"):
            self.keyboard = True
            nxt = self._nearest(name)
            if nxt is not None:
                self.focus = nxt.id
                self.show_control(nxt)
            return True
        if name in ("return", "enter", "space"):
            self.keyboard = True
            c = self.control(self.focus)
            if c is None:
                self.focus = f"tab:{self.tab}"
                return True
            self.press(c.id)
            return True
        if name in ("page down", "page up"):
            self.scroll_by((1 if name == "page down" else -1) * VIEW.h * 0.8)
            return True
        if name == "tab":
            self.open_tab(TABS[(TABS.index(self.tab) + 1) % len(TABS)])
            return True
        return False

    def _nearest(self, direction: str) -> Control | None:
        """The control the highlight goes to with an arrow key: the nearest one that way."""
        controls = [c for c in self.controls() if c.focusable]
        cur = next((c for c in controls if c.id == self.focus), None)
        if cur is None:
            return next((c for c in controls if c.id == f"tab:{self.tab}"), None)
        a = cur.rect
        best: tuple[float, Control] | None = None
        slack = 24
        for c in controls:
            if c.id == cur.id:
                continue
            b = c.rect
            if direction in ("left", "right"):
                beyond = b.right <= a.left + slack if direction == "left" else b.left >= a.right - slack
                along = (a.left - b.right) if direction == "left" else (b.left - a.right)
                across = max(0, max(a.top, b.top) - min(a.bottom, b.bottom))
                off = abs(b.centery - a.centery)
            else:
                beyond = b.bottom <= a.top + slack if direction == "up" else b.top >= a.bottom - slack
                along = (a.top - b.bottom) if direction == "up" else (b.top - a.bottom)
                across = max(0, max(a.left, b.left) - min(a.right, b.right))
                off = abs(b.centerx - a.centerx)
            if not beyond:
                continue
            score = max(0, along) + 3 * across + 0.05 * off
            if best is None or score < best[0]:
                best = (score, c)
        return best[1] if best else None

    def show_control(self, c: Control) -> None:
        """Scroll so ``c`` is wholly in the panel."""
        if not c.scrolls:
            return
        r = c.rect
        if r.top < VIEW.top + 4:
            self.offset -= VIEW.top + 4 - r.top
        elif r.bottom > VIEW.bottom - 4:
            self.offset += r.bottom - (VIEW.bottom - 4)
        self.offset = art.clamp(self.offset, 0.0, self.max_offset)
        self.velocity = 0.0

    # ------------------------------------------------------------ what a press does
    def press(self, cid: str, x: float | None = None) -> None:
        self.flash = (cid, self.t)
        if cid == "close":
            self.host.sound("pop")
            self.host.close()
        elif cid.startswith("tab:"):
            self.open_tab(cid[4:])
        elif cid.startswith("game:"):
            self.play(cid[5:])
        elif cid in ("vol-", "vol+"):
            self.set_volume(step_level(self.volume, 1 if cid.endswith("+") else -1, 0.0, self.max_volume))
        elif cid in ("bright-", "bright+"):
            self.set_brightness(step_level(self.brightness, 1 if cid.endswith("+") else -1, BRIGHTNESS_MIN, 1.0))
        elif cid in ("vol:bar", "bright:bar") and x is not None:
            c = self.control(cid)
            if c is not None:
                k = art.clamp((x - c.rect.x) / max(1, c.rect.w), 0.0, 1.0)
                level = round(k * 10) / 10
                if cid == "vol:bar":
                    self.set_volume(min(level, self.max_volume))
                else:
                    self.set_brightness(max(BRIGHTNESS_MIN, level))
        elif cid == "dark":
            self.set_dark(not self.dark)
        elif cid == "flip":
            self.set_flipped(not self.flipped)
        elif cid == "cards":
            self.toggle_cards()
        elif cid == "sleep":
            self.go_to_sleep()
        elif cid == "always":
            self.set_always(ALWAYS_MODES[(ALWAYS_MODES.index(self.always) + 1) % len(ALWAYS_MODES)])
        elif cid == "clip":
            self.save_clip()
        elif cid == "whatsapp":
            self.open_whatsapp()

    def open_tab(self, name: str) -> None:
        if name not in TABS:
            return
        if name != self.tab:
            self.host.sound("pop")
            self.tab = name
            self.velocity = 0.0
            self.dragging = False
            if name == "about":
                self.battery = self.link.battery()
        self.focus = f"tab:{name}"

    def set_volume(self, v: float) -> None:
        v = round(art.clamp(v, 0.0, self.max_volume), 3)
        if abs(v - self.volume) < 1e-6:
            self.host.sound("boop")  # already at the end
            return
        self.values[VOLUME] = v
        self.link.set_volume(v)
        self.host.sound("click")  # at the new volume

    def set_brightness(self, v: float) -> None:
        v = round(art.clamp(v, BRIGHTNESS_MIN, 1.0), 3)
        if abs(v - self.brightness) < 1e-6:
            self.host.sound("boop")
            return
        self.values[BRIGHTNESS] = v
        self.link.set_setting(BRIGHTNESS, v)  # the display loop applies it
        self.host.sound("click")

    def set_dark(self, on: bool) -> None:
        self.values[THEME] = "dark" if on else "light"
        self.link.set_setting(THEME, self.values[THEME])  # the display loop draws the next frame in it
        self.host.sound("click")

    def set_flipped(self, on: bool) -> None:
        self.values[FLIPPED] = bool(on)
        self.link.set_setting(FLIPPED, bool(on))
        self.host.sound("click")

    def toggle_cards(self) -> None:
        """Info cards during chats, on or off. (The screen service keeps this panel up, and lets it open, with
        them off: ``service.ALWAYS_SHOWN``.)"""
        on = not self.cards_on
        self.values[CARDS] = on
        self.host.sound("click")
        self.link.set_setting(CARDS, on)

    def set_always(self, mode: str) -> None:
        """Always recording: auto (while plugged in), on (on the battery too, until it is low) or off. The capture
        service follows the setting (and puts "on" back to "auto" by itself)."""
        if mode not in ALWAYS_MODES:
            return
        self.values[ALWAYS] = mode
        self.link.set_setting(ALWAYS, mode)
        self.host.sound("click")

    def save_clip(self) -> None:
        """What the always-recording buffer holds becomes a video in the gallery."""
        if self.saving:
            return
        self.host.sound("click")
        self.saving = True
        self.link.save_clip(self._result("clip"))
        self._take_results()

    def open_whatsapp(self) -> None:
        """WhatsApp for a video call takes the screen (this card goes); tapped while it is open, it closes."""
        if self.calling:
            return
        self.host.sound("click")
        self.calling = True
        self.link.whatsapp(self._result("whatsapp"))
        self._take_results()

    def go_to_sleep(self) -> None:
        if self.sleeping:
            return
        self.host.sound("click")
        self.sleeping = True
        self.link.sleep(self._result("sleep"))
        self._take_results()

    def play(self, game_id: str) -> None:
        if self.starting:
            return
        info = next((g for g in self.games if g.id == game_id), None)
        if info is None:
            return
        self.host.sound("pop")
        start = self.link.start_game
        if start is None:
            self.say_notice(f"{info.name} can't start here.")
            return
        self.starting = game_id
        start(game_id, self._result(f"game:{game_id}"))
        self._take_results()

    def say_notice(self, text: str) -> None:
        self.notice = str(text)[:60]
        self.notice_until = self.t + NOTICE_SECONDS

    # ------------------------------------------------------------ the voice
    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "tab":
            name = str(args.get("name") or args.get("tab") or "").strip().lower()
            if name in TABS:
                self.open_tab(name)
                return True
            return False
        if action == "scroll":
            self.scroll_by((-1 if str(args.get("direction", "down")).lower() == "up" else 1) * VIEW.h * 0.8)
            return True
        return False

    def summary(self) -> str:
        parts = [f"volume {round(self.volume * 100)}%", f"brightness {round(self.brightness * 100)}%",
                 f"dark mode {'on' if self.dark else 'off'}",
                 "the screen upside down" if self.flipped else "the screen the right way up",
                 f"info cards during chats {'on' if self.cards_on else 'off'}"]
        return ", ".join(parts)

    def describe(self) -> str:
        line = f"LittleBot's settings: {self.summary()}."
        if self.tab == "games":
            names = ", ".join(g.name for g in self.games)
            return f"{line} Showing the games: {names}."
        if self.tab == "about":
            where = " or ".join(self.urls) or "the dashboard"
            bat = ""
            if self.battery is not None:
                charging = ", charging" if self.battery["charging"] else ""
                bat = f" The battery is at {self.battery['percent']}%{charging}."
            return f"{line} Showing about: the parent dashboard is at {where}.{bat}"
        return line

    def status(self) -> dict[str, Any]:
        return {**super().status(), "tab": self.tab, "volume": round(self.volume, 3),
                "max_volume": round(self.max_volume, 3), "brightness": round(self.brightness, 3), "dark": self.dark,
                "flipped": self.flipped, "cards": self.cards_on, "always_recording": self.always,
                "focus": self.focus, "offset": round(self.offset),
                "starting": self.starting, "dashboard": list(self.urls), "battery": self.battery}

    # ------------------------------------------------------------ drawing
    def draw(self, surf: Any) -> None:
        surf.blit(art.background(), (0, 0))
        self._draw_header(surf)
        for t in TABS:
            self._draw_tab(surf, t)
        self._draw_close(surf)
        _kit.panel(surf, PANEL, radius=28)
        clip = surf.get_clip()
        surf.set_clip(VIEW.clip(clip))
        if self.tab == "settings":
            self._draw_settings(surf)
        elif self.tab == "games":
            self._draw_games(surf)
        else:
            self._draw_about(surf)
        surf.set_clip(clip)
        self._draw_scrollbar(surf)
        if self.keyboard:
            c = self.control(self.focus)
            if c is not None and (not c.scrolls or c.rect.colliderect(VIEW)):
                r = c.rect.inflate(10, 10)
                if c.scrolls:
                    surf.set_clip(PANEL.clip(clip))
                surf.blit(_focus_ring(r.w, r.h), r.topleft)
                surf.set_clip(clip)
        if self.notice:
            pill = art.pill(self.notice, BODY_PX, art.WHITE, art.inkier(art.STRAWBERRY, 0.15), pad=(22, 10))
            art.blit_center(surf, pill, (PANEL.centerx, PANEL.bottom - 44))

    def _flashing(self, cid: str) -> bool:
        return self.flash is not None and self.flash[0] == cid and self.t - self.flash[1] < FLASH_SECONDS

    def _draw_header(self, surf: Any) -> None:
        room = CLOSE.x - 16 - TITLE_X
        title = art.text(_kit.ellipsize(self.title, TITLE_PX, room), TITLE_PX, art.INK)
        surf.blit(title, title.get_rect(midleft=(TITLE_X, _PY + 38)))
        hint = {"settings": "Changes work straight away" if self.max_offset <= 0 else
                "Changes work straight away. Drag for more",
                "games": "Tap a game to play it", "about": "For grown-ups"}[self.tab]
        sub = art.text(_kit.ellipsize(hint, SMALL_PX, room), SMALL_PX, art.INK_SOFT)
        surf.blit(sub, sub.get_rect(midleft=(TITLE_X + 2, _PY + 86)))

    def _draw_tab(self, surf: Any, name: str) -> None:
        r = self.tab_rect(name)
        on = name == self.tab
        col = TAB_COLOURS[name]
        fill = art.inkier(col, 0.18) if on else art.WHITE
        if self._flashing(f"tab:{name}"):
            fill = art.inkier(col, 0.3) if on else art.mix(art.WHITE, col, 0.25)
        border = art.inkier(col, 0.4) if on else _kit.PANEL_BORDER
        surf.blit(art.rounded_rect(r.w, r.h, 28, fill, shadow=5, border=border, border_w=3), r.topleft)
        icon = _icon(name, ICON_PX + 8, art.WHITE if on else col)
        art.blit_center(surf, icon, (r.centerx, r.y + r.h * 0.38))
        label = art.text(TAB_NAMES[name], BODY_PX, art.WHITE if on else art.INK, on=fill)
        surf.blit(label, label.get_rect(center=(r.centerx, r.y + r.h * 0.76)))

    def _draw_close(self, surf: Any) -> None:
        r = CLOSE
        fill = art.inkier(art.STRAWBERRY, 0.3 if self._flashing("close") else 0.12)
        surf.blit(art.rounded_rect(r.w, r.h, 30, fill, shadow=5, border=art.inkier(art.STRAWBERRY, 0.4), border_w=3),
                  r.topleft)
        icon = _icon("close", 40, art.WHITE)
        label = art.text("Close", BODY_PX, art.WHITE, on=fill)
        w = icon.get_width() + 12 + label.get_width()
        x = r.centerx - w // 2
        surf.blit(icon, icon.get_rect(midleft=(x, r.centery)))
        surf.blit(label, label.get_rect(midleft=(x + icon.get_width() + 12, r.centery + 1)))

    # the settings tab
    def _draw_settings(self, surf: Any) -> None:
        controls = {c.id: c.rect for c in self.controls()}
        for i, (rid, kind) in enumerate(_ROWS):
            r = self.row_rect(i)
            if r.bottom < VIEW.top or r.top > VIEW.bottom:
                continue
            if i:
                surf.blit(_divider(r.w), (r.x, r.y - 1))
            col = _ROW_COLOURS[rid]
            label, sub = self._row_words(rid)
            if kind == "switch" and self._flashing(rid):
                surf.blit(art.rounded_rect(r.w + 12, r.h - 8, 20, art.mix(art.WHITE, col, 0.22)), (r.x - 6, r.y + 4))
            _badge(surf, rid, col, (r.x + 32, r.centery))
            tx = r.x + 74
            right = (controls.get(f"{rid}-") or controls.get(rid) or r).x if kind != "switch" else r.right - SWITCH_W
            room = right - 14 - tx
            name = art.text(_kit.ellipsize(label, BODY_PX, room), BODY_PX, art.INK)
            if sub:
                sub_img = art.text(_kit.ellipsize(sub, SMALL_PX, room), SMALL_PX, _kit.TEXT_SOFT)
                gap = 6
                top = r.centery - (name.get_height() + gap + sub_img.get_height()) // 2
                surf.blit(name, (tx, top))
                surf.blit(sub_img, (tx + 1, top + name.get_height() + gap))
            else:
                surf.blit(name, name.get_rect(midleft=(tx, r.centery + 1)))
            if kind == "level":
                self._draw_level(surf, rid, controls)
            elif kind == "switch":
                on = {"dark": self.dark, "flip": self.flipped, "cards": self.cards_on}[rid]
                sw = pygame.Rect(r.right - SWITCH_W, r.centery - SWITCH_H // 2, SWITCH_W, SWITCH_H)
                surf.blit(_switch(on, col), sw.topleft)
            elif rid == "always":
                self._draw_button(surf, controls[rid], ALWAYS_NAMES[self.always], col, "", self._flashing(rid),
                                  dim=self.always == "off")
            elif rid == "clip":
                self._draw_button(surf, controls[rid], "Saving" if self.saving else "Save", col, "",
                                  self._flashing(rid), dim=self.saving)
            elif rid == "whatsapp":
                label = "Wait" if self.calling else "Close" if self.whatsapp_on else "Open"
                self._draw_button(surf, controls[rid], label, col, "", self._flashing(rid), dim=self.calling)
            else:
                self._draw_button(surf, controls[rid], "Sleep now", art.PERIWINKLE, "sleep", self._flashing(rid),
                                  dim=self.sleeping)

    def _row_words(self, rid: str) -> tuple[str, str]:
        if rid == "vol":
            top = round(self.max_volume * 100)
            return "Volume", ("The most allowed" if self.volume >= self.max_volume - 1e-6 else f"Up to {top}%")
        if rid == "bright":
            return "Brightness", "The screen"
        if rid == "dark":
            return "Dark mode", "Night colours, glowing eyes"
        if rid == "flip":
            return "Upside down", "Turn the screen round"
        if rid == "cards":
            return "Info cards", "Pictures and facts in chats"
        if rid == "always":
            return "Always recording", ALWAYS_WORDS[self.always]
        if rid == "clip":
            return f"Save the last {self.clip_minutes:g} minutes", "As a video in the gallery"
        if rid == "whatsapp":
            return "WhatsApp", ("On the screen now" if self.whatsapp_on else "Video calls: call or answer")
        return "Go to sleep", "Sleepy face, quiet"

    def _draw_level(self, surf: Any, rid: str, controls: dict[str, pygame.Rect]) -> None:
        col = _ROW_COLOURS[rid]
        if rid == "vol":
            v, lo, hi, cap = self.volume, 0.0, self.max_volume, self.max_volume
        else:
            v, lo, hi, cap = self.brightness, BRIGHTNESS_MIN, 1.0, 1.0
        minus, bar, plus = controls[f"{rid}-"], controls[f"{rid}:bar"], controls[f"{rid}+"]
        self._draw_button(surf, minus, "", col, "minus", self._flashing(f"{rid}-"), dim=v <= lo + 1e-6)
        self._draw_button(surf, plus, "", col, "plus", self._flashing(f"{rid}+"), dim=v >= hi - 1e-6)
        surf.blit(_bar(bar.w, 56, v, cap, col), (bar.x, bar.centery - 28))
        pct = art.text_outlined(f"{round(v * 100)}%", BODY_PX, art.WHITE, art.inkier(col, 0.55), 3)
        art.blit_center(surf, pct, (bar.centerx, bar.centery))

    def _draw_button(self, surf: Any, r: pygame.Rect, label: str, col: art.Color, icon: str, lit: bool,
                     dim: bool = False) -> None:
        fill = art.inkier(col, 0.32 if lit else 0.14)
        if dim:
            fill = art.mix(col, art.WHITE, 0.55)
        surf.blit(art.rounded_rect(r.w, r.h, 26, fill, shadow=4, border=art.inkier(col, 0.42), border_w=3), r.topleft)
        fg = art.mix(art.inkier(col, 0.3), art.WHITE, 0.25) if dim else art.WHITE
        text = art.text(label, BODY_PX, fg, on=fill) if label else None
        if text is not None and not icon:  # words only
            surf.blit(text, text.get_rect(center=(r.centerx, r.centery + 1)))
            return
        glyph = _icon(icon, 44 if label else 40, fg)
        if text is None:
            art.blit_center(surf, glyph, r.center)
            return
        w = glyph.get_width() + 10 + text.get_width()
        x = r.centerx - w // 2
        surf.blit(glyph, glyph.get_rect(midleft=(x, r.centery)))
        surf.blit(text, text.get_rect(midleft=(x + glyph.get_width() + 10, r.centery + 1)))

    # the games tab
    def _draw_games(self, surf: Any) -> None:
        for i, g in enumerate(self.games):
            r = self.tile_rect(i)
            if r.bottom < VIEW.top or r.top > VIEW.bottom:
                continue
            col = GAME_COLOURS.get(g.id, art.CANDY[i % len(art.CANDY)])
            busy = self.starting == g.id
            lit = busy or self._flashing(f"game:{g.id}")
            fill = art.mix(art.WHITE, col, 0.18) if lit else art.WHITE
            surf.blit(art.rounded_rect(r.w, r.h, 28, fill, shadow=5, border=art.inkier(col, 0.25) if lit else
                                       art.mix(col, art.WHITE, 0.15), border_w=3), r.topleft)
            ic = (r.x + 14 + G_ICON // 2, r.centery)
            art.blit_center(surf, art.circle(G_ICON / 2, art.surface(art.lighten(col, 0.55))), ic)
            art.blit_center(surf, game_icon(g.id, G_ICON - 20, col), ic)
            tx = r.x + 26 + G_ICON
            room = r.right - 14 - tx
            lines = list(_kit.wrap(g.name, BODY_PX, room))
            if len(lines) > 2:
                lines = [lines[0], _kit.ellipsize(" ".join(lines[1:]), BODY_PX, room)]
            name = _kit.text_block(lines, BODY_PX, art.INK, pitch=art.font(BODY_PX).get_linesize())
            age = art.pill(g.ages, SMALL_PX, art.INK, art.lighten(col, 0.6), pad=(12, 4))
            gap = 8
            top = r.centery - (name.get_height() + gap + age.get_height()) // 2
            surf.blit(name, (tx, top))
            surf.blit(age, (tx - 2, top + name.get_height() + gap))
            if busy:  # starting: three little dots
                for k in range(3):
                    on = int(self.t * 4) % 3 == k
                    art.blit_center(surf, art.circle(6 if on else 5, art.inkier(col, 0.2)),
                                    (r.right - 46 + k * 15, r.y + 22))

    # the about tab
    def _draw_about(self, surf: Any) -> None:
        x = VIEW.x + 22
        y = VIEW.y + 14
        room = VIEW.right - 22 - x
        # the parent dashboard: where to open it
        where = ("Only on LittleBot itself: LAN access is off" if self.local_only else
                 "On a phone or computer on the same Wi-Fi")
        y = self._about_row(surf, "dashboard", art.SKY, x, y, "The parent dashboard", sub=where)
        cx, line = x + 70, 0
        for u in self.urls:  # side by side while they fit
            chip = art.pill(_kit.ellipsize(u, BODY_PX, room - 110), BODY_PX, art.INK, art.mix(art.WHITE, art.SKY, 0.2),
                            pad=(18, 8), border=art.mix(art.SKY, art.WHITE, 0.3))
            if cx > x + 70 and cx + chip.get_width() > x + room:
                cx, y = x + 70, y + line + 10
            surf.blit(chip, (cx, y))
            cx += chip.get_width() + 14
            line = chip.get_height()
        y += line + 20
        # the battery, when one is fitted
        if self.battery is not None:
            b = self.battery
            words = f"{b['percent']}%"
            words += ", full" if b["full"] else ", charging" if b["charging"] else (", plugged in" if b["plugged_in"]
                                                                                   else "")
            col = art.STRAWBERRY if b["percent"] < LOW_BATTERY and not b["charging"] else art.MINT
            y = self._about_row(surf, "battery", col, x, y, "Battery", value=words, level=b["percent"] / 100)
            y += 8
        # how to open these settings
        surf.blit(_divider(room), (x, y))
        y += 14
        lines = list(_kit.wrap("Hold a finger still for 5 seconds anywhere to open these settings", BODY_PX,
                               room - 74))
        block = _kit.text_block(lines, BODY_PX, art.INK)
        h = max(64, block.get_height())
        _badge(surf, "hold", art.GRAPE, (x + 32, y + h // 2))
        surf.blit(block, (x + 74, y + (h - block.get_height()) // 2))

    def _about_row(self, surf: Any, icon: str, col: art.Color, x: int, y: int, label: str, *, sub: str = "",
                   value: str = "", level: float | None = None) -> int:
        """A heading with its badge (and a small line under it, or a value beside it); the y under it."""
        h = 68
        c = (x + 32, y + h // 2)
        if level is not None:
            art.blit_center(surf, art.circle(ICON_PX // 2 + 6, art.surface(art.mix(art.WHITE, col, 0.28))), c)
            art.blit_center(surf, _battery_icon(ICON_PX - 6, level), c)
        else:
            _badge(surf, icon, col, c)
        img = art.text(label, BODY_PX, art.INK)
        if sub:
            note = art.text(_kit.ellipsize(sub, SMALL_PX, VIEW.right - 22 - (x + 74)), SMALL_PX, _kit.TEXT_SOFT)
            top = c[1] - (img.get_height() + 6 + note.get_height()) // 2
            surf.blit(img, (x + 74, top))
            surf.blit(note, (x + 75, top + img.get_height() + 6))
        else:
            surf.blit(img, img.get_rect(midleft=(x + 74, c[1] + 1)))
        if value:
            v = art.text(value, BODY_PX, art.inkier(col, 0.45))
            surf.blit(v, v.get_rect(midleft=(x + 74 + img.get_width() + 18, c[1] + 1)))
        return y + h + 6

    def _draw_scrollbar(self, surf: Any) -> None:
        total = self.content_height()
        if total <= VIEW.h:
            return
        track_h = VIEW.h - 28
        thumb_h = max(44, int(track_h * VIEW.h / total))
        p = art.clamp(self.offset / max(1.0, self.max_offset), 0.0, 1.0)
        x = PANEL.right - LANE + 4
        y = VIEW.y + 14 + int((track_h - thumb_h) * p)
        surf.blit(art.rounded_rect(10, track_h, 5, art.mix(art.BG_BOTTOM, art.INK_SOFT, 0.15)), (x, VIEW.y + 14))
        surf.blit(art.rounded_rect(10, thumb_h, 5, art.INK_SOFT), (x, y))


# the settings' rows, in order: (id, kind)
_ROWS: tuple[tuple[str, str], ...] = (("vol", "level"), ("bright", "level"), ("dark", "switch"), ("flip", "switch"),
                                      ("cards", "switch"), ("sleep", "button"), ("always", "button"),
                                      ("clip", "button"), ("whatsapp", "button"))
_ROW_COLOURS: dict[str, art.Color] = {"vol": art.SKY, "bright": art.TANGERINE, "dark": art.GRAPE,
                                      "flip": art.PERIWINKLE, "cards": art.MINT, "sleep": art.PERIWINKLE,
                                      "always": art.STRAWBERRY, "clip": art.STRAWBERRY, "whatsapp": art.MINT}


# ---------------------------------------------------------------- sprites (cached per theme by art.cached)
def _badge(surf: pygame.Surface, icon: str, col: art.Color, center: tuple[float, float]) -> None:
    """A row's icon on a soft disc of its colour: a deeper shade of it on the light disc, the candy colour itself
    on the dark one (dark mode)."""
    art.blit_center(surf, art.circle(ICON_PX // 2 + 6, art.surface(art.mix(art.WHITE, col, 0.28))), center)
    art.blit_center(surf, _icon(icon, ICON_PX - 6, col if art.dark() else art.inkier(col, 0.25)), center)


def _divider(w: int) -> pygame.Surface:
    return art.rounded_rect(w, 2, 1, art.mix(art.INK_SOFT, art.CREAM, 0.6))


def _focus_ring(w: int, h: int) -> pygame.Surface:
    """The arrow keys' highlight: a thick lemon ring with a plum edge (reads on any colour, either theme)."""

    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            rect = s.get_rect()
            pygame.draw.rect(s, art.inkier(art.LEMON, 0.55), rect, int(8 * k), border_radius=int(30 * k))
            pygame.draw.rect(s, art.LEMON, rect.inflate(-2 * k, -2 * k), int(5 * k), border_radius=int(29 * k))

        return art.supersampled((w, h), draw, ss=2)

    return art.cached(("settings-focus", w, h), make)


def _switch(on: bool, col: art.Color) -> pygame.Surface:
    """A big on / off switch: the knob right and the colour on, the knob left and grey off, with the word."""
    w, h = SWITCH_W, SWITCH_H

    def make() -> pygame.Surface:
        fill = art.inkier(col, 0.12) if on else art.surface(art.mix(art.INK_SOFT, art.WHITE, 0.62))
        edge = art.inkier(col, 0.4) if on else art.ink(art.mix(art.INK_SOFT, art.INK, 0.2))
        out = pygame.Surface((w, h + 3), pygame.SRCALPHA)

        def draw(s: pygame.Surface, k: float) -> None:
            rect = pygame.Rect(0, 0, w * k, h * k)
            pygame.draw.rect(s, edge, rect, border_radius=int(h * k / 2))
            pygame.draw.rect(s, fill, rect.inflate(-6 * k, -6 * k), border_radius=int((h - 6) * k / 2))
            r = (h / 2 - 7) * k
            cx = (w - h / 2) * k if on else h / 2 * k
            pygame.draw.circle(s, (0, 0, 0, 50), (cx, h / 2 * k + 3 * k), r)
            pygame.draw.circle(s, art.WHITE, (cx, h / 2 * k), r)
            pygame.draw.circle(s, edge, (cx, h / 2 * k), r, max(1, int(2 * k)))

        out.blit(art.supersampled((w, h + 3), draw), (0, 0))
        word = art.text("On" if on else "Off", SMALL_PX, art.WHITE if on else art.INK, on=fill)
        cx = (w - h / 2 - 2) / 2 + 4 if on else (h + (w - h / 2)) / 2 + 2
        out.blit(word, word.get_rect(center=(int(cx), h // 2 + 1)))
        return out

    return art.cached(("settings-switch", on, tuple(col)), make)


def _bar(w: int, h: int, v: float, cap: float, col: art.Color) -> pygame.Surface:
    """A level bar: filled to ``v`` (0..1) in ``col``; above ``cap`` (the safe maximum) a hatched part."""
    v = round(art.clamp(v, 0.0, 1.0), 3)
    cap = round(art.clamp(cap, 0.0, 1.0), 3)

    def make() -> pygame.Surface:
        track = art.surface(art.mix(art.BG_BOTTOM, art.WHITE, 0.35))
        edge = art.ink(art.mix(art.INK_SOFT, art.INK, 0.15))
        hatch = art.ink(art.mix(art.INK_SOFT, art.WHITE, 0.25))

        def draw(s: pygame.Surface, k: float) -> None:
            rect = pygame.Rect(0, 0, w * k, h * k)
            rad = int(h * k / 2)
            pygame.draw.rect(s, edge, rect, border_radius=rad)
            inner = rect.inflate(-6 * k, -6 * k)
            pygame.draw.rect(s, track, inner, border_radius=int(rad - 3 * k))
            if cap < 1.0:  # out of bounds: stripes
                x0 = inner.x + inner.w * cap
                lane = pygame.Surface(inner.size, pygame.SRCALPHA)
                pygame.draw.rect(lane, (255, 255, 255, 255), lane.get_rect(), border_radius=int(rad - 3 * k))
                stripes = pygame.Surface(inner.size, pygame.SRCALPHA)
                step = int(14 * k)
                for sx in range(-inner.h, inner.w + inner.h, step):
                    pygame.draw.line(stripes, (*hatch, 160), (sx, inner.h), (sx + inner.h, 0), max(1, int(3 * k)))
                stripes.blit(lane, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
                s.blit(stripes, (x0, inner.y), area=pygame.Rect(x0 - inner.x, 0, inner.right - x0, inner.h))
            if v > 0:
                fw = max(inner.h, inner.w * v)
                pygame.draw.rect(s, col, pygame.Rect(inner.x, inner.y, fw, inner.h), border_radius=int(rad - 3 * k))
                shine = pygame.Rect(inner.x + inner.h * 0.3, inner.y + inner.h * 0.16, max(0, fw - inner.h * 0.6),
                                    inner.h * 0.2)
                pygame.draw.rect(s, art.lighten(col, 0.35), shine, border_radius=int(inner.h * 0.1))

        return art.supersampled((w, h), draw)

    return art.cached(("settings-bar", w, h, v, cap, tuple(col)), make)


def _battery_icon(size: int, level: float) -> pygame.Surface:
    level = round(art.clamp(level, 0.0, 1.0), 2)
    col = art.STRAWBERRY if level * 100 < LOW_BATTERY else art.MINT

    def draw(s: pygame.Surface, k: float) -> None:
        z = size * k
        ink = art.ink(art.INK)
        body = pygame.Rect(z * 0.06, z * 0.26, z * 0.78, z * 0.48)
        pygame.draw.rect(s, ink, body, border_radius=int(z * 0.1))
        pygame.draw.rect(s, ink, pygame.Rect(z * 0.84, z * 0.4, z * 0.1, z * 0.2), border_radius=int(z * 0.04))
        inner = body.inflate(-z * 0.12, -z * 0.12)
        pygame.draw.rect(s, art.surface(art.WHITE), inner, border_radius=int(z * 0.05))
        pygame.draw.rect(s, col, pygame.Rect(inner.x, inner.y, max(z * 0.05, inner.w * level), inner.h),
                         border_radius=int(z * 0.05))

    return art.cached(("settings-battery", size, level), lambda: art.supersampled((size, size), draw))


def _icon(name: str, size: int, color: art.Color) -> pygame.Surface:
    """A small drawn icon in ``color`` (cached): the tabs', the rows', the buttons'."""
    size = int(size)
    fn = _ICONS.get(name, _icon_dot)
    return art.cached(("settings-icon", name, size, tuple(color)),
                      lambda: art.supersampled((size, size), lambda s, k: fn(s, size * k, color)))


def _icon_dot(s: pygame.Surface, z: float, c: art.Color) -> None:
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.3)


def _icon_gear(s: pygame.Surface, z: float, c: art.Color) -> None:
    cx = cy = z / 2
    for i in range(8):
        a = i * math.tau / 8
        ca, sa = math.cos(a), math.sin(a)
        w, r0, r1 = z * 0.075, z * 0.28, z * 0.47
        pts = [(cx + ca * r0 - sa * w, cy + sa * r0 + ca * w), (cx + ca * r1 - sa * w, cy + sa * r1 + ca * w),
               (cx + ca * r1 + sa * w, cy + sa * r1 - ca * w), (cx + ca * r0 + sa * w, cy + sa * r0 - ca * w)]
        pygame.draw.polygon(s, c, pts)
    pygame.draw.circle(s, c, (cx, cy), z * 0.34)
    pygame.draw.circle(s, (0, 0, 0, 0), (cx, cy), z * 0.14)


def _icon_pad(s: pygame.Surface, z: float, c: art.Color) -> None:
    body = pygame.Rect(z * 0.02, z * 0.18, z * 0.96, z * 0.64)
    pygame.draw.rect(s, c, body, border_radius=int(z * 0.3))
    hole = (0, 0, 0, 0)
    art.capsule(s, hole, (z * 0.17, z * 0.5), (z * 0.39, z * 0.5), z * 0.1)
    art.capsule(s, hole, (z * 0.28, z * 0.39), (z * 0.28, z * 0.61), z * 0.1)
    pygame.draw.circle(s, hole, (z * 0.66, z * 0.42), z * 0.07)
    pygame.draw.circle(s, hole, (z * 0.8, z * 0.58), z * 0.07)


def _icon_info(s: pygame.Surface, z: float, c: art.Color) -> None:
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.46)
    hole = (0, 0, 0, 0)
    pygame.draw.circle(s, hole, (z / 2, z * 0.27), z * 0.07)
    art.capsule(s, hole, (z / 2, z * 0.44), (z / 2, z * 0.74), z * 0.13)


def _icon_close(s: pygame.Surface, z: float, c: art.Color) -> None:
    w = z * 0.16
    art.capsule(s, c, (z * 0.2, z * 0.2), (z * 0.8, z * 0.8), w)
    art.capsule(s, c, (z * 0.2, z * 0.8), (z * 0.8, z * 0.2), w)


def _icon_minus(s: pygame.Surface, z: float, c: art.Color) -> None:
    art.capsule(s, c, (z * 0.16, z / 2), (z * 0.84, z / 2), z * 0.17)


def _icon_plus(s: pygame.Surface, z: float, c: art.Color) -> None:
    art.capsule(s, c, (z * 0.16, z / 2), (z * 0.84, z / 2), z * 0.17)
    art.capsule(s, c, (z / 2, z * 0.16), (z / 2, z * 0.84), z * 0.17)


def _icon_speaker(s: pygame.Surface, z: float, c: art.Color) -> None:
    pygame.draw.polygon(s, c, [(z * 0.06, z * 0.36), (z * 0.24, z * 0.36), (z * 0.5, z * 0.14), (z * 0.5, z * 0.86),
                               (z * 0.24, z * 0.64), (z * 0.06, z * 0.64)])
    for r in (0.2, 0.36):
        box = pygame.Rect(z * (0.5 - r), z * (0.5 - r), z * 2 * r, z * 2 * r)
        pygame.draw.arc(s, c, box.move(z * 0.08, 0), -math.pi / 3.2, math.pi / 3.2, max(2, int(z * 0.08)))


def _icon_sun(s: pygame.Surface, z: float, c: art.Color) -> None:
    for i in range(8):
        a = i * math.tau / 8
        art.capsule(s, c, (z / 2 + math.cos(a) * z * 0.32, z / 2 + math.sin(a) * z * 0.32),
                    (z / 2 + math.cos(a) * z * 0.44, z / 2 + math.sin(a) * z * 0.44), z * 0.08)
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.22)


def _icon_moon(s: pygame.Surface, z: float, c: art.Color) -> None:
    pygame.draw.circle(s, c, (z * 0.46, z * 0.54), z * 0.38)
    pygame.draw.circle(s, (0, 0, 0, 0), (z * 0.68, z * 0.36), z * 0.32)


def _icon_flip(s: pygame.Surface, z: float, c: art.Color) -> None:
    lw = max(2, int(z * 0.1))
    box = pygame.Rect(z * 0.14, z * 0.14, z * 0.72, z * 0.72)
    pygame.draw.arc(s, c, box, math.radians(20), math.radians(160), lw)
    pygame.draw.arc(s, c, box, math.radians(200), math.radians(340), lw)
    for ang, turn in ((20, -1), (200, -1)):
        a = math.radians(ang)
        px, py = z / 2 + math.cos(a) * z * 0.36, z / 2 - math.sin(a) * z * 0.36
        tx, ty = -math.sin(a) * turn, -math.cos(a) * turn  # along the arc, clockwise
        nx, ny = math.cos(a), -math.sin(a)
        d = z * 0.15
        pygame.draw.polygon(s, c, [(px + tx * d, py + ty * d), (px + nx * d * 0.9, py + ny * d * 0.9),
                                   (px - nx * d * 0.9, py - ny * d * 0.9)])


def _icon_card(s: pygame.Surface, z: float, c: art.Color) -> None:
    body = pygame.Rect(z * 0.06, z * 0.16, z * 0.88, z * 0.68)
    pygame.draw.rect(s, c, body, border_radius=int(z * 0.12))
    hole = (0, 0, 0, 0)
    pygame.draw.rect(s, hole, pygame.Rect(z * 0.16, z * 0.28, z * 0.3, z * 0.3), border_radius=int(z * 0.06))
    for k, y in enumerate((0.32, 0.48, 0.66)):
        art.capsule(s, hole, (z * (0.56 if k < 2 else 0.18), z * y), (z * 0.8, z * y), z * 0.07)


def _icon_sleep(s: pygame.Surface, z: float, c: art.Color) -> None:
    _icon_moon(s, z * 0.8, c)
    lw = max(2, z * 0.07)
    for k, (x, y, d) in enumerate(((0.62, 0.18, 0.16), (0.8, 0.04, 0.12))):
        x0, y0, d0 = z * x, z * y, z * d
        art.capsule(s, c, (x0, y0), (x0 + d0, y0), lw)
        art.capsule(s, c, (x0 + d0, y0), (x0, y0 + d0), lw)
        art.capsule(s, c, (x0, y0 + d0), (x0 + d0, y0 + d0), lw)


def _icon_record(s: pygame.Surface, z: float, c: art.Color) -> None:
    """Always recording: a ring with a dot in it."""
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.42, max(2, int(z * 0.08)))
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.2)


def _icon_save(s: pygame.Surface, z: float, c: art.Color) -> None:
    """Save: an arrow down into a tray."""
    lw = max(2, z * 0.09)
    art.capsule(s, c, (z * 0.5, z * 0.12), (z * 0.5, z * 0.6), lw)
    art.capsule(s, c, (z * 0.3, z * 0.42), (z * 0.5, z * 0.62), lw)
    art.capsule(s, c, (z * 0.7, z * 0.42), (z * 0.5, z * 0.62), lw)
    art.capsule(s, c, (z * 0.16, z * 0.84), (z * 0.84, z * 0.84), lw)


def _icon_call(s: pygame.Surface, z: float, c: art.Color) -> None:
    """A video call: a camera body with its lens wedge."""
    body = pygame.Rect(z * 0.12, z * 0.28, z * 0.5, z * 0.44)
    pygame.draw.rect(s, c, body, border_radius=max(2, int(z * 0.1)))
    pygame.draw.polygon(s, c, [(z * 0.66, z * 0.5), (z * 0.88, z * 0.3), (z * 0.88, z * 0.7)])


def _icon_globe(s: pygame.Surface, z: float, c: art.Color) -> None:
    lw = max(2, int(z * 0.07))
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.42, lw)
    pygame.draw.ellipse(s, c, pygame.Rect(z * 0.32, z * 0.08, z * 0.36, z * 0.84), lw)
    pygame.draw.line(s, c, (z * 0.1, z / 2), (z * 0.9, z / 2), lw)
    for y in (0.3, 0.7):
        pygame.draw.line(s, c, (z * 0.18, z * y), (z * 0.82, z * y), max(1, lw - 1))


def _icon_hold(s: pygame.Surface, z: float, c: art.Color) -> None:
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.16)
    pygame.draw.circle(s, c, (z / 2, z / 2), z * 0.3, max(2, int(z * 0.06)))
    pygame.draw.arc(s, c, pygame.Rect(z * 0.04, z * 0.04, z * 0.92, z * 0.92), math.pi * 0.1, math.pi * 1.6,
                    max(2, int(z * 0.06)))


_ICONS: dict[str, Callable[[pygame.Surface, float, art.Color], None]] = {
    "settings": _icon_gear, "games": _icon_pad, "about": _icon_info, "close": _icon_close, "minus": _icon_minus,
    "plus": _icon_plus, "vol": _icon_speaker, "bright": _icon_sun, "dark": _icon_moon, "flip": _icon_flip,
    "cards": _icon_card, "sleep": _icon_sleep, "dashboard": _icon_globe, "hold": _icon_hold,
    "always": _icon_record, "clip": _icon_save, "whatsapp": _icon_call,
}


# ---------------------------------------------------------------- building
async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    games = getattr(ctx, "games", None)
    catalog = list(GAMES)
    if games is not None and getattr(games, "catalog", None):
        catalog = list(games.catalog.values())
    return SettingsCard(spec, host, link=RobotLink(ctx), games=pick(catalog, "all", None))


register(CardType(
    kind=KIND,
    description="LittleBot's own settings panel for a grown-up at the robot (opened by holding a finger on the "
                "screen for 5 seconds): volume, brightness, dark mode, the games, the dashboard's address.",
    fields={"tab": {"type": "string", "enum": list(TABS), "description": "Which tab opens (default settings)"}},
    build=_build,
    actions={"tab": "open a tab: {name: \"settings\" | \"games\" | \"about\"}",
             "scroll": "scroll the open tab: {direction: \"down\" | \"up\"}"},
    offered=False,
))

SAMPLES: list[dict[str, Any]] = [{"card": KIND}, {"card": KIND, "tab": "games"}, {"card": KIND, "tab": "about"}]

__all__ = ["KIND", "SAMPLES", "Control", "RobotLink", "SettingsCard", "step_level"]
