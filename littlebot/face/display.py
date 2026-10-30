"""Main-thread display loop and desktop simulator window.

``run_display(robot)`` opens the face window (480x320 rendered natively, then
scaled smoothly to ``display.scale`` or fullscreen), drives the face animation,
handles keyboard / mouse / touch input and (on the desktop) draws the
simulator side panel. It blocks until the window is closed or
``robot.shutdown_event`` is set, then returns; ``main.py`` calls
``robot.shutdown()``.

If no display can be opened (no X / Wayland / KMS, missing SDL video driver)
the error is logged clearly and the function falls back to waiting on
``robot.shutdown_event`` - the robot keeps running headless.

``robot`` is ``app.robot.LittleBot`` (or any object with ``ctx``, ``runtime``,
``shutdown_event``, ``status()``, ``stop_all(source)`` and
``request_shutdown(reason)``). Every ``ctx.<subsystem>`` may be ``None``.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from littlebot.face import edge_controls as EC
from littlebot.face.battery_badge import BatteryBadge, draw_badge

log = logging.getLogger("littlebot.face.display")

# Cause-and-effect for a baby: tapping the face (on the robot's touch screen,
# i.e. when not in simulator mode) makes LittleBot giggle and bounce.
TAP_REACTION_ENABLED = True
TAP_REACTION_SOUND = "giggle"
TAP_COOLDOWN_SECONDS = 1.5
# Holding a finger on the idle face this long opens the games menu (littlebot/screens/cards/games.py).
# A plain tap still giggles: babies tap the face all the time, a deliberate hold is a grown-up's.
LONG_PRESS_SECONDS = 1.0
LONG_PRESS_SLOP = 0.04  # of the face width: a finger that wanders further is not holding still
# A resistive touch screen often flickers as a finger lands: down, up, down again within a few ms. A lift is
# held back this long; a press again near it (TOUCH_BOUNCE_PX) means the finger never left.
TOUCH_BOUNCE_S = 0.1
TOUCH_BOUNCE_PX = 40
# MX Player-style edge swipes on the idle face (littlebot/face/edge_controls.py): slide up / down on the right
# edge for the speaker volume, on the left edge for the screen brightness. A soft blip makes the volume audible.
VOLUME_BLIP = "blip"
VOLUME_BLIP_LEVEL = 0.8  # relative to the volume being set (play_sound never goes louder than it)

PANEL_WIDTH = 300
PANEL_MIN_HEIGHT = 460
# While a parent watches the camera live on the dashboard, a small camera sign sits in the face's top-right
# corner so viewing is never hidden. Drawn in the face's own palette (renderer.MOUTH_INK / MOUTH_INSIDE on
# the pastel screen, the eye colour on a dark one), soft and still so it doesn't pull a child's attention.
LIVE_BADGE_WIDTH = 30  # px at the native 480x320
LIVE_BADGE_MARGIN = 12
LIVE_BADGE_INK = (66, 32, 62, 150)  # deep plum, ~60 % opaque
LIVE_BADGE_DOT = (146, 40, 82, 210)  # raspberry "on" dot
LIVE_BADGE_DOT_DARK = (255, 120, 146, 220)
STATUS_POLL_SECONDS = 0.5
MOTION_POLL_SECONDS = 1 / 15
MAX_CONSECUTIVE_ERRORS = 30
TEST_PHRASE = "Hello! I'm LittleBot. Shall we play?"

HELP_LINES: tuple[tuple[str, str], ...] = (
    ("1-9, 0", "neutral happy excited curious surprised sleepy sad thinking listening confused"),
    ("J K L V", "playful / speaking / affectionate (love) / celebrating (victory)"),
    ("Shift+1-0", "bounce wiggle nod shake spin sparkle hearts zzz confetti peek"),
    ("F1-F7", "face state: idle listening thinking speaking sleeping error hidden"),
    ("B / N", "blink / double blink"),
    ("arrows, R", "move gaze / reset gaze"),
    ("O", "close / open eyes (peekaboo)"),
    ("T", "test speech (mouth animation)"),
    ("M", "demo sequence on / off"),
    ("P / C", "start peekaboo activity / caregiver confirm"),
    ("SPACE", "push-to-talk"),
    ("W", "wave gesture"),
    ("X / E", "STOP everything / motion emergency stop"),
    ("Esc (game)", "stop the on-screen game (taps, 1-9, space play it)"),
    ("Esc (card)", "close the info card (a tap on its little eyes too)"),
    ("hold click", "hold 1 s on the face: the games menu"),
    ("edge drag", "slide up / down on the face's right edge: volume; left edge: brightness"),
    ("- / =", "speaker volume down / up"),
    ("[ / ]", "screen brightness down / up"),
    ("click", "simulated camera face there (right-click: no face)"),
    ("Shift+click", "tap reaction (giggle)"),
    ("G / H", "debug overlay / this help"),
    ("F or F11", "toggle fullscreen"),
    ("F12", "save screenshot"),
    ("Q / Esc", "quit"),
)


def run_display(robot: Any, demo: bool = False) -> None:
    """Run the face window on the calling (main) thread until quit / shutdown."""
    face = getattr(robot.ctx, "face", None)
    if face is None or not callable(getattr(face, "render", None)) or not callable(getattr(face, "update", None)):
        log.error("face service cannot render (got %r); continuing without a window", type(face).__name__)
        _wait_for_shutdown(robot)
        return
    loop = DisplayLoop(robot, demo=demo)
    try:
        loop.open()
    except Exception as e:  # noqa: BLE001 - pygame.error, missing driver, ...
        log.error("could not open the face display (%s: %s). Running without a window; "
                  "use --headless to silence this, or check DISPLAY / SDL_VIDEODRIVER.", type(e).__name__, e)
        loop.close()
        _wait_for_shutdown(robot)
        return
    reason = "error"
    try:
        reason = loop.run()
    except Exception:  # noqa: BLE001
        log.exception("display loop crashed")
    finally:
        loop.close()
    if reason == "error":
        log.error("display stopped after repeated errors; robot keeps running without a window")
        _wait_for_shutdown(robot)


def apply_output_mode(output: str, mode: str, *, run: Callable[..., Any] = subprocess.run,
                      which: Callable[[str], str | None] = shutil.which,
                      env: Any = None, settle_s: float = 0.5) -> bool:
    """Ask the Wayland compositor to drive ``output`` at ``mode`` (e.g. ``"720x480@60"``).

    The MPI3508's EDID offers only 16:9 / 4:3 modes (preferred 1280x720), which its scaler
    squashes into the 3:2 panel; 720x480 has the panel's own shape. Uses ``wlr-randr`` and
    only acts in a Wayland session; never fatal. Returns True when the mode was changed.
    """
    env = os.environ if env is None else env
    if not env.get("WAYLAND_DISPLAY") or which("wlr-randr") is None:
        log.info("screen mode %s for %s skipped: needs a Wayland session with wlr-randr", mode, output)
        return False
    size = mode.split("@", 1)[0].strip()
    try:
        cur = run(["wlr-randr", "--output", output], capture_output=True, text=True, timeout=5)
        if cur.returncode == 0 and _current_size(cur.stdout, output) == size:
            log.info("screen %s already at %s", output, size)
            return False
    except (OSError, subprocess.SubprocessError):
        log.debug("could not read the current screen mode", exc_info=True)
    try:
        res = run(["wlr-randr", "--output", output, "--custom-mode", mode], capture_output=True, text=True,
                  timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not set screen %s to %s: %s", output, mode, e)
        return False
    if res.returncode != 0:
        log.warning("could not set screen %s to %s: %s", output, mode, (res.stderr or res.stdout or "").strip())
        return False
    log.info("screen %s set to %s", output, mode)
    if settle_s > 0:
        time.sleep(settle_s)  # let the compositor (and Xwayland) settle before SDL reads the screen size
    return True


def init_video(pg: Any, driver: str | None, env: Any = None) -> None:
    """``pygame.display.init()``, preferring SDL video ``driver`` (e.g. ``"wayland"``).

    On the robot, the native Wayland driver gives a true fullscreen window (no taskbar)
    and hides the mouse pointer; SDL2's default there is X11 via Xwayland, which does
    neither reliably. An ``SDL_VIDEODRIVER`` already in the environment always wins, and
    if the preferred driver cannot start, SDL's default is used instead.
    """
    env = os.environ if env is None else env
    if not driver or env.get("SDL_VIDEODRIVER"):
        pg.display.init()
        return
    env["SDL_VIDEODRIVER"] = driver
    try:
        pg.display.init()
    except Exception as e:  # noqa: BLE001 - pygame.error: driver not available / no compositor
        log.warning("SDL video driver %r unavailable (%s); using SDL's default", driver, e)
        env.pop("SDL_VIDEODRIVER", None)
        try:
            pg.display.quit()
        except Exception:  # noqa: BLE001
            pass
        pg.display.init()


def _current_size(wlr_randr_text: str, output: str) -> str | None:
    """The ``WxH`` marked ``current`` for ``output`` in plain ``wlr-randr`` output, if any."""
    block: list[str] = []
    inside = False
    for line in wlr_randr_text.splitlines():
        if line and not line[0].isspace():
            inside = line.split()[0] == output
            continue
        if inside:
            block.append(line)
    for line in block:
        m = re.match(r"\s*(\d+x\d+) px\b.*\bcurrent\b", line)
        if m:
            return m.group(1)
    return None


def _wait_for_shutdown(robot: Any) -> None:
    ev = robot.shutdown_event
    while not ev.wait(0.5):  # short waits keep signal handlers responsive on the main thread
        pass


class DisplayLoop:
    """The window, its input handling and overlays. Create, ``open()``, ``run()``, ``close()``."""

    def __init__(self, robot: Any, *, demo: bool = False, max_frames: int | None = None) -> None:
        from littlebot.face.demo import DemoSequence

        self.robot = robot
        self.ctx = robot.ctx
        self.face = self.ctx.face
        cfg = self.ctx.config.display
        self.cfg = cfg
        self.native_size = (int(cfg.width), int(cfg.height))
        self.scale = max(0.25, float(cfg.scale))
        self.fullscreen = bool(cfg.fullscreen)
        self.sim_mode = bool(cfg.show_sim_panel)
        self.show_panel = self.sim_mode and not self.fullscreen
        self.show_debug = bool(cfg.show_debug)
        self.show_help = False
        self.demo: DemoSequence | None = DemoSequence(self.face, loop=True) if demo else None
        self.max_frames = max_frames
        self.window: Any = None
        self.face_rect: Any = None
        self.panel_rect: Any = None
        self.panel: Any = None
        self._pg: Any = None
        self._opened = False
        self._gaze = [0.5, 0.5]
        self._timers: list[tuple[float, Callable[[], None]]] = []
        self._frame_stamps: collections.deque[float] = collections.deque(maxlen=60)
        self._work_ms: collections.deque[float] = collections.deque(maxlen=60)
        self._status: dict[str, Any] = {}
        self._status_t = 0.0
        self._motion: dict[str, Any] | None = None
        self._motion_t = 0.0
        self._last_tap = 0.0
        self._fonts: dict[int, Any] = {}
        self._errors = 0
        self.frames = 0
        # First-run dashboard setup code, shown on the robot's own screen until a parent claims it.
        self.setup_banner: dict[str, Any] | None = None
        self._setup_sub: Any = None
        # How many dashboard viewers are watching the camera live (vision.live_view): > 0 shows the camera sign.
        self.live_viewers = 0
        self._live_sub: Any = None
        self._badges: dict[int, Any] = {}
        self._game_t: float | None = None  # last frame time while an on-screen game runs
        self._card_t: float | None = None  # last frame time while an info card shows
        self._press: tuple[float, float, float] | None = None  # (nx, ny, when) a finger held on the idle face
        self.touch_bounce_s = TOUCH_BOUNCE_S
        self._pointer_down = False  # button 1 / the finger is down (some touch drivers' motion has no buttons)
        self._lift: tuple[tuple[int, int], float] | None = None  # a lift held back (TOUCH_BOUNCE_S)
        # Edge swipes: volume (right) and brightness (left). A press in an edge zone holds back its usual
        # reaction (giggle / simulated face) until the finger lifts or rests: it may become a slide instead.
        self.brightness = self._make_brightness()
        self.edges = self._make_edges()
        self._edge_tap: tuple[float, Callable[[], None]] | None = None
        self._level_news: collections.deque[tuple[str, Any]] = collections.deque(maxlen=16)
        self._levels_sub: Any = None
        self._off_lock = threading.Lock()
        self._off_pending: dict[str, tuple[Callable[[Any], Any], Any]] = {}
        self._off_running: set[str] = set()
        # The battery sign in the top-left corner (littlebot/power publishes power.show: plugged in, asked, low on waking).
        self.battery = BatteryBadge()
        self._power_sub: Any = None
        self._battery_sub: Any = None

    # ------------------------------------------------------------------ setup
    def open(self) -> None:
        import pygame

        self._pg = pygame
        if self.fullscreen and self.cfg.output_mode:
            apply_output_mode(self.cfg.output_name, self.cfg.output_mode)
        init_video(pygame, self.cfg.video_driver)
        pygame.font.init()
        pygame.display.set_caption(f"{self.ctx.config.robot_name}")
        self._set_mode()
        if self.cfg.hide_cursor:
            pygame.mouse.set_visible(False)
        pygame.key.set_repeat(0)
        if self.show_panel:
            from littlebot.face.sim_panel import SimPanel

            self.panel = SimPanel()
        attach = getattr(self.face, "attach_display", None)
        if callable(attach):
            attach()
        self._watch_setup_code()
        self._watch_live_view()
        self._watch_levels()
        self._watch_power()
        self._opened = True
        log.info("face display open: %dx%d%s (driver %s)", *self.window.get_size(),
                 " fullscreen" if self.fullscreen else "", pygame.display.get_driver())

    def close(self) -> None:
        if self._setup_sub is not None:
            self._setup_sub.cancel()
            self._setup_sub = None
        if self._live_sub is not None:
            self._live_sub.cancel()
            self._live_sub = None
        if self._levels_sub is not None:
            self._levels_sub.cancel()
            self._levels_sub = None
        for name in ("_power_sub", "_battery_sub"):
            sub = getattr(self, name)
            if sub is not None:
                sub.cancel()
                setattr(self, name, None)
        detach = getattr(self.face, "detach_display", None)
        if callable(detach):
            try:
                detach()
            except Exception:  # noqa: BLE001
                log.debug("detach_display failed", exc_info=True)
        pg = self._pg
        if pg is not None:
            try:
                pg.display.quit()  # not pygame.quit(): audio may still be using the mixer
            except Exception:  # noqa: BLE001
                log.debug("display quit failed", exc_info=True)
        self._opened = False

    def _watch_setup_code(self) -> None:
        from littlebot.core.events import Topics

        bus = self.ctx.bus
        recent = bus.recent(300, Topics.DASHBOARD_SETUP)
        if recent:
            self._on_setup_code(recent[-1])
        self._setup_sub = bus.subscribe(Topics.DASHBOARD_SETUP, self._on_setup_code)

    def _on_setup_code(self, event: Any) -> None:
        data = dict(event.data)
        self.setup_banner = data if data.get("active") and data.get("code") else None

    def _watch_live_view(self) -> None:
        from littlebot.core.events import Topics

        vision = self.ctx.vision
        try:  # someone may already be watching when the window opens
            live = (vision.status().get("live_view") or {}) if vision is not None else {}
            self.live_viewers = int(live.get("viewers") or 0)
        except Exception:  # noqa: BLE001
            self.live_viewers = 0
        self._live_sub = self.ctx.bus.subscribe(Topics.VISION_LIVE_VIEW, self._on_live_view)

    def _watch_power(self) -> None:
        from littlebot.core.events import Topics

        bus = self.ctx.bus
        self._power_sub = bus.subscribe(Topics.POWER_SHOW, self.battery.on_event)
        if self.ctx.config.power.corner:  # the small always-on copy follows power.battery
            recent = bus.recent(300, Topics.POWER_BATTERY)
            if recent:
                self.battery.on_state(recent[-1])
            self._battery_sub = bus.subscribe(Topics.POWER_BATTERY, self.battery.on_state)

    def _on_live_view(self, event: Any) -> None:
        try:
            self.live_viewers = int(event.get("viewers") or 0) if event.get("active") else 0
        except (TypeError, ValueError):
            self.live_viewers = 1 if event.get("active") else 0

    def _set_mode(self) -> None:
        pg = self._pg
        w, h = self.native_size
        if self.fullscreen:
            self.window = pg.display.set_mode((0, 0), pg.FULLSCREEN)
            self.show_panel = False
        else:
            self.show_panel = self.sim_mode
            fw, fh = int(round(w * self.scale)), int(round(h * self.scale))
            pw = PANEL_WIDTH if self.show_panel else 0
            wh = max(fh, PANEL_MIN_HEIGHT) if self.show_panel else fh
            self.window = pg.display.set_mode((fw + pw, wh))
        self._layout()

    def _layout(self) -> None:
        pg = self._pg
        ww, wh = self.window.get_size()
        avail = ww - (PANEL_WIDTH if self.show_panel else 0)
        w, h = self.native_size
        s = min(avail / w, wh / h)
        fw, fh = max(1, int(w * s)), max(1, int(h * s))
        self.face_rect = pg.Rect((avail - fw) // 2, (wh - fh) // 2, fw, fh)
        self.panel_rect = pg.Rect(avail, 0, PANEL_WIDTH, wh) if self.show_panel else None
        if self.show_panel and self.panel is None:
            from littlebot.face.sim_panel import SimPanel

            self.panel = SimPanel()

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        try:
            self._set_mode()
        except Exception as e:  # noqa: BLE001
            log.warning("could not switch fullscreen: %s", e)
            self.fullscreen = not self.fullscreen
            self._set_mode()

    # ------------------------------------------------------------------ loop
    def run(self) -> str:
        """Returns why it stopped: "quit", "shutdown", "error" or "max_frames"."""
        pg = self._pg
        clock = pg.time.Clock()
        last = time.monotonic()
        shutdown = self.robot.shutdown_event
        while True:
            if shutdown.is_set():
                return "shutdown"
            now = time.monotonic()
            dt, last = now - last, now
            try:
                if not self._handle_events():
                    return "quit"
                self.check_long_press(now)
                self.check_edges(now)
                t0 = time.perf_counter()
                self._run_timers(now)
                if self.demo is not None:
                    self.demo.update(dt)
                self.face.update(dt)
                self._draw(now)
                self._work_ms.append((time.perf_counter() - t0) * 1000.0)
                pg.display.flip()
                self._frame_stamps.append(time.monotonic())
                self._errors = 0
            except pg.error as e:
                if "not initialized" in str(e) or "video system" in str(e).lower():
                    log.error("display lost: %s", e)
                    return "error"
                if self._count_error():
                    return "error"
            except Exception:  # noqa: BLE001
                if self._count_error():
                    return "error"
            self.frames += 1
            if self.max_frames is not None and self.frames >= self.max_frames:
                return "max_frames"
            clock.tick(self.target_fps())

    def _count_error(self) -> bool:
        self._errors += 1
        if self._errors <= 3:
            log.exception("display frame failed")
        return self._errors >= MAX_CONSECUTIVE_ERRORS

    def target_fps(self) -> int:
        fn = getattr(self.face, "target_fps", None)
        fps = int(fn()) if callable(fn) else int(self.cfg.fps)
        if self.demo is not None or self.show_help:
            fps = max(fps, int(self.cfg.idle_fps))
        if self.show_panel and self._motion and self._motion.get("moving"):
            fps = max(fps, int(self.cfg.fps))
        if self._games() is not None or self.edges.active or self.edges.bar() is not None:
            fps = max(fps, int(self.cfg.fps))
        else:
            screens = self._screens()
            if screens is not None:
                try:
                    busy = bool(screens.animating)
                except Exception:  # noqa: BLE001
                    busy = True
                fps = max(fps, int(self.cfg.fps if busy else self.cfg.idle_fps))
        if self.battery.visible:  # the battery sign fades in and out
            fps = max(fps, int(self.cfg.idle_fps))
        return max(1, fps)

    @property
    def measured_fps(self) -> float:
        st = list(self._frame_stamps)
        if len(st) < 2 or st[-1] <= st[0]:
            return 0.0
        return (len(st) - 1) / (st[-1] - st[0])

    @property
    def frame_ms(self) -> float:
        ms = list(self._work_ms)
        return sum(ms) / len(ms) if ms else 0.0

    def _run_timers(self, now: float) -> None:
        if not self._timers:
            return
        due = [fn for t, fn in self._timers if t <= now]
        self._timers = [(t, fn) for t, fn in self._timers if t > now]
        for fn in due:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("display timer failed")

    def after(self, seconds: float, fn: Callable[[], None]) -> None:
        self._timers.append((time.monotonic() + seconds, fn))

    # ------------------------------------------------------------------ drawing
    def _draw(self, now: float) -> None:
        win = self.window
        if self.face_rect.size != win.get_size():
            win.fill((0, 0, 0))
        covered = self._draw_game(win, now) or self._draw_card(win, now)
        if not covered:
            self.face.render(win, self.face_rect)
        if self.show_panel and self.panel is not None and self.panel_rect is not None:
            self._poll(now)
            caption = f"demo: {self.demo.caption}" if self.demo is not None and self.demo.caption else ""
            self.panel.draw(win, self.panel_rect, self._status, self._motion, fps=self.measured_fps, caption=caption)
        elif self.demo is not None and self.show_debug:
            self._text(win, f"demo: {self.demo.caption}", (self.face_rect.x + 8, self.face_rect.bottom - 24), 20)
        if self.setup_banner is not None and not self.face_hidden():
            self._draw_setup_banner(win)
        if self.live_viewers > 0:
            self._draw_live_view_badge(win)
        if self.show_debug:
            self._draw_debug(win)
        if self.show_help:
            self._draw_help(win)
        self.brightness.apply(win, self.face_rect)  # software dimming (a no-op with a backlight or at 100 %)
        bar = self.edges.bar(now)
        if bar is not None and not self.face_hidden():  # over the dimming: readable, only half as dimmed
            shade = 1.0 if self.brightness.backlight is not None else 0.5 + 0.5 * self.brightness.level
            EC.draw_bar(win, self.face_rect, bar, shade)
        badge = self.battery.current(now)
        if badge is None and not covered:  # the small corner copy: on the face only (cards put eyes there)
            badge = self.battery.steady()
        if badge is not None and not self.face_hidden():
            draw_badge(win, self.face_rect, badge, 1.0 if self.brightness.backlight is not None
                       else 0.5 + 0.5 * self.brightness.level)

    def face_hidden(self) -> bool:
        try:
            return not self.face.status().get("visible", True)
        except Exception:  # noqa: BLE001
            return False

    def _draw_setup_banner(self, win: Any) -> None:
        """Small strip at the bottom of the face: the one-time dashboard setup code."""
        pg = self._pg
        r = self.face_rect
        s = r.h / self.native_size[1]
        size = max(14, int(17 * s))
        code = str(self.setup_banner.get("code", ""))
        label = self._font(size).render("Parent setup code", True, (150, 170, 190))
        value = self._font(int(size * 1.35)).render(code, True, (255, 214, 102))
        pad = int(6 * s)
        h = max(label.get_height(), value.get_height()) + 2 * pad
        w = label.get_width() + value.get_width() + 5 * pad
        box = pg.Surface((w, h), pg.SRCALPHA)
        box.fill((0, 0, 0, 170))
        x, y = r.centerx - w // 2, r.bottom - h - int(8 * s)
        win.blit(box, (x, y))
        win.blit(label, (x + 2 * pad, y + (h - label.get_height()) // 2))
        win.blit(value, (x + 3 * pad + label.get_width(), y + (h - value.get_height()) // 2))

    def _draw_live_view_badge(self, win: Any) -> None:
        """While a parent watches the camera live on the dashboard: a small camera sign, top right."""
        badge, pos = self._live_badge()
        win.blit(badge, pos)

    def live_badge_rect(self) -> Any:
        """Where the camera sign is drawn (tests / screenshots)."""
        badge, pos = self._live_badge()
        return self._pg.Rect(pos, badge.get_size())

    def _live_badge(self) -> tuple[Any, tuple[int, int]]:
        r = self.face_rect
        s = r.h / self.native_size[1]  # scaled with the face
        width = max(12, int(round(LIVE_BADGE_WIDTH * s)))
        badge = self._badges.get(width)
        if badge is None:
            badge = self._badges[width] = self._make_live_badge(width)
        m = int(round(LIVE_BADGE_MARGIN * s))
        return badge, (r.right - badge.get_width() - m, r.y + m)

    def _make_live_badge(self, width: int) -> Any:
        """A rounded little camera with a dot beside it, drawn 4x and smooth-scaled (soft edges like the eyes)."""
        from littlebot.face.frame import is_light_background

        pg = self._pg
        face_cfg = getattr(self.ctx.config, "face", None)
        bg = tuple(getattr(face_cfg, "background_color", (255, 214, 228)))[:3]
        if is_light_background(bg):  # inked, like the mouth and the closed eyes on the pastel screen
            ink, dot = LIVE_BADGE_INK, LIVE_BADGE_DOT
        else:  # glowing, like the eyes on a dark screen
            eye = tuple(getattr(face_cfg, "eye_color", (90, 210, 255)))[:3]
            ink, dot = (*eye, 140), LIVE_BADGE_DOT_DARK
        k = 4
        w = width * k
        h = int(w * 0.72)
        dot_r, gap = int(w * 0.1), int(w * 0.12)
        x0 = 2 * dot_r + gap
        clear = (*ink[:3], 0)  # transparent, but ink-coloured, so smoothscale leaves no dark fringe
        surf = pg.Surface((x0 + w, h), pg.SRCALPHA)
        surf.fill(clear)
        top = int(h * 0.2)
        body = pg.Rect(x0, top, w, h - top)
        bump = pg.Rect(x0 + int(w * 0.3), 0, int(w * 0.32), int(h * 0.36))  # the viewfinder on top
        pg.draw.rect(surf, ink, bump, border_radius=int(w * 0.08))
        pg.draw.rect(surf, ink, body, border_radius=int(w * 0.2))
        lens = (x0 + w // 2, body.centery)
        pg.draw.circle(surf, clear, lens, int(w * 0.25))  # a ring cut out of the body...
        pg.draw.circle(surf, ink, lens, int(w * 0.15))  # ...around the lens
        pg.draw.circle(surf, dot, (dot_r, body.centery), dot_r)
        return pg.transform.smoothscale(surf, (max(1, surf.get_width() // k), max(1, h // k)))

    def _poll(self, now: float) -> None:
        if now - self._status_t >= STATUS_POLL_SECONDS:
            self._status_t = now
            try:
                self._status = self.robot.status()
            except Exception as e:  # noqa: BLE001
                self._status = {"state": {"state": f"status error: {e}"}}
        motion = self.ctx.motion
        if motion is not None and now - self._motion_t >= MOTION_POLL_SECONDS:
            self._motion_t = now
            try:
                self._motion = motion.status()
            except Exception:  # noqa: BLE001
                self._motion = None
        elif motion is None:
            self._motion = None

    def _font(self, size: int) -> Any:
        f = self._fonts.get(size)
        if f is None:
            f = self._pg.font.Font(None, size)
            self._fonts[size] = f
        return f

    def _text(self, surf: Any, text: str, pos: tuple[int, int], size: int = 18,
              color: tuple[int, int, int] = (220, 230, 240)) -> None:
        img = self._font(size).render(text, True, color)
        bg = self._pg.Surface((img.get_width() + 8, img.get_height() + 4), self._pg.SRCALPHA)
        bg.fill((0, 0, 0, 150))
        surf.blit(bg, (pos[0] - 4, pos[1] - 2))
        surf.blit(img, pos)

    def _draw_debug(self, win: Any) -> None:
        fs = {}
        try:
            fs = self.face.status()
        except Exception:  # noqa: BLE001
            pass
        renderer = getattr(self.face, "renderer", None)
        rms = getattr(renderer, "last_ms", 0.0) if renderer is not None else 0.0
        try:
            leases = {k: v for k, v in self.ctx.arbiter.snapshot().items() if v}
        except Exception:  # noqa: BLE001
            leases = {}
        try:
            robot_state = self.ctx.state.state.value
        except Exception:  # noqa: BLE001
            robot_state = "?"
        gaze = fs.get("gaze", {})
        lines = [
            f"fps {self.measured_fps:5.1f}  target {self.target_fps()}",
            f"frame {self.frame_ms:5.2f} ms  render {rms:5.2f} ms",
            f"robot {robot_state}  face {fs.get('state', '?')}",
            f"expr {fs.get('expression', '?')}  closed {fs.get('eyes_closed')}",
            f"gaze {gaze.get('x', 0):.2f}, {gaze.get('y', 0):.2f}  talk {fs.get('talking')}",
            "leases " + (", ".join(f"{k}:{v}" for k, v in leases.items()) or "-"),
        ]
        anims = fs.get("animations") or []
        if anims:
            lines.append("anim " + ", ".join(anims))
        x, y = self.face_rect.x + 8, self.face_rect.y + 6
        for line in lines:
            self._text(win, line, (x, y), 18)
            y += 18

    def _draw_help(self, win: Any) -> None:
        pg = self._pg
        r = self.face_rect.inflate(-max(12, self.face_rect.w // 30), -max(12, self.face_rect.h // 30))
        box = pg.Surface(r.size, pg.SRCALPHA)
        pg.draw.rect(box, (8, 12, 22, 225), box.get_rect(), border_radius=14)
        pg.draw.rect(box, (90, 210, 255, 160), box.get_rect(), width=2, border_radius=14)
        win.blit(box, r.topleft)
        size = max(13, min(20, r.h // (len(HELP_LINES) + 3)))
        title = self._font(size + 4).render("LittleBot keys", True, (90, 210, 255))
        win.blit(title, (r.x + 14, r.y + 8))
        y = r.y + 14 + title.get_height()
        key_w = int(size * 5.2)
        for key, desc in HELP_LINES:
            win.blit(self._font(size).render(key, True, (255, 214, 102)), (r.x + 14, y))
            img = self._font(size).render(desc, True, (215, 225, 238))
            if img.get_width() > r.w - key_w - 28:
                img = pg.transform.smoothscale(img, (r.w - key_w - 28, img.get_height()))
            win.blit(img, (r.x + 14 + key_w, y))
            y += size + 1

    # ------------------------------------------------------------------ on-screen games
    # While ``ctx.games`` (littlebot.games) runs a game it is drawn instead of the face and gets
    # taps and keys first (Esc stops it). Everything else - overlays, panel, STOP keys - is unchanged.
    def _games(self) -> Any:
        games = getattr(self.ctx, "games", None)
        try:
            return games if games is not None and games.active else None
        except Exception:  # noqa: BLE001
            return None

    def _draw_game(self, win: Any, now: float) -> bool:
        games = self._games()
        if games is None:
            self._game_t = None
            return False
        dt = 0.0 if self._game_t is None else now - self._game_t
        self._game_t = now
        try:
            return bool(games.render(win, self.face_rect, dt))
        except Exception:  # noqa: BLE001 - fall back to the face
            log.exception("game frame failed")
            return False

    def _game_tap(self, nx: float, ny: float) -> bool:
        games = self._games()
        if games is None:
            return False
        try:
            return bool(games.handle_tap(nx, ny))
        except Exception:  # noqa: BLE001
            log.exception("game tap failed")
            return False

    def _game_key(self, key: int) -> bool:
        games = self._games()
        if games is None:
            return False
        try:
            return bool(games.handle_key(self._pg.key.name(key)))
        except Exception:  # noqa: BLE001
            log.exception("game key failed")
            return False

    # ------------------------------------------------------------------ info cards
    # While ``ctx.screens`` (littlebot.screens) shows a card - and no game runs: a game wins - the card is
    # drawn instead of the face (LittleBot's small eyes present it) and gets taps and keys (Esc closes it).
    def _screens(self) -> Any:
        screens = getattr(self.ctx, "screens", None)
        try:
            return screens if screens is not None and screens.active else None
        except Exception:  # noqa: BLE001
            return None

    def _draw_card(self, win: Any, now: float) -> bool:
        screens = self._screens()
        if screens is None:
            self._card_t = None
            return False
        dt = 0.0 if self._card_t is None else now - self._card_t
        self._card_t = now
        try:
            return bool(screens.render(win, self.face_rect, dt))
        except Exception:  # noqa: BLE001 - fall back to the face
            log.exception("card frame failed")
            return False

    def _card_tap(self, nx: float, ny: float) -> bool:
        """The finger went down on a card (a card that scrolls waits for the release to call it a tap)."""
        screens = self._screens()
        if screens is None or self._games() is not None:
            return False
        try:
            press = getattr(screens, "handle_press", None) or screens.handle_tap
            return bool(press(nx, ny))
        except Exception:  # noqa: BLE001
            log.exception("card tap failed")
            return False

    def _card_pointer(self, what: str, pos: tuple[int, int]) -> bool:
        """The finger moved ("drag") or came up ("release") on a card."""
        screens = self._screens()
        fn = getattr(screens, "handle_" + what, None) if screens is not None else None
        if fn is None or self._games() is not None:
            return False
        r = self.face_rect
        try:
            return bool(fn((pos[0] - r.x) / max(1, r.w), (pos[1] - r.y) / max(1, r.h)))
        except Exception:  # noqa: BLE001
            log.exception("card %s failed", what)
            return False

    # A long press on the idle face opens the games menu (a plain tap keeps its giggle).
    def check_long_press(self, now: float) -> bool:
        press = self._press
        if press is None or self._lift is not None or now - press[2] < LONG_PRESS_SECONDS:
            return False  # (a lift held back: the finger is up)
        self._press = None
        screens = getattr(self.ctx, "screens", None)
        self.edges.cancel()
        if screens is None or self._games() is not None or self._screens() is not None or self.face_hidden():
            return False
        log.info("long press on the face: games menu")
        self._submit(lambda: screens.show({"card": "games"}, source="touch"), "games menu")
        return True

    def _pointer_moved(self, pos: tuple[int, int], buttons: tuple[int, ...]) -> None:
        if self.edges.tracking and self._edge_moved(pos):
            return
        if buttons and buttons[0]:
            self._card_pointer("drag", pos)
        press = self._press
        if press is not None:
            r = self.face_rect
            nx, ny = (pos[0] - r.x) / max(1, r.w), (pos[1] - r.y) / max(1, r.h)
            if abs(nx - press[0]) > LONG_PRESS_SLOP or abs(ny - press[1]) > LONG_PRESS_SLOP * 1.5:
                self._press = None

    def _card_key(self, key: int) -> bool:
        screens = self._screens()
        if screens is None or self._games() is not None:
            return False
        try:
            return bool(screens.handle_key(self._pg.key.name(key)))
        except Exception:  # noqa: BLE001
            log.exception("card key failed")
            return False

    # ------------------------------------------------------------------ edge swipes
    # On the idle face only: slide on the right edge for the speaker volume, on the left for the brightness
    # (littlebot/face/edge_controls.py). A press there that doesn't slide is the usual tap / long press.
    def _make_brightness(self) -> EC.Brightness:
        settings = self.ctx.settings
        try:
            settings.register_validator("display.brightness", EC.Brightness.clamp)
            level = settings.get("display.brightness", 1.0)
        except Exception:  # noqa: BLE001
            level = 1.0
        # A real backlight only on the robot's own screen: a desktop simulator must not dim the laptop.
        sysfs = EC.BACKLIGHT_DIR if self.fullscreen and not self.sim_mode else None
        return EC.Brightness(level, sysfs=sysfs)

    def _make_edges(self) -> EC.EdgeControls:
        ctx = self.ctx
        speaker = ctx.config.audio.speaker

        def volume() -> float:
            return float(ctx.settings.get("audio.volume", speaker.volume))

        vol = EC.Level("volume", volume, self._set_volume, 0.0, max(0.0, min(1.0, float(speaker.max_volume))),
                       feedback=self._volume_blip)
        bright = EC.Level("brightness", lambda: self.brightness.level, self.brightness.set, EC.BRIGHTNESS_FLOOR, 1.0)
        return EC.EdgeControls(left=bright, right=vol)

    def edges_allowed(self) -> bool:
        """Only the default face: no game, info card, setup banner, hidden face or full-screen activity (video)."""
        if self._games() is not None or self._screens() is not None or self.setup_banner is not None:
            return False
        if self.face_hidden():
            return False
        try:
            return not self.ctx.arbiter.snapshot().get("screen")
        except Exception:  # noqa: BLE001
            return True

    def _edge_moved(self, pos: tuple[int, int]) -> bool:
        """Feed a move to the edge gesture. True while it slides a level (the move is used up)."""
        r = self.face_rect
        state = self.edges.move((pos[0] - r.x) / max(1, r.w), (pos[1] - r.y) / max(1, r.h))
        if state == "active":
            self._press = None  # no long press...
            self._edge_tap = None  # ...and no giggle while sliding
            return True
        if state == "rejected":
            self._fire_edge_tap()  # sideways: a plain touch after all
        return False

    def _edge_release(self, pos: tuple[int, int]) -> None:
        if not self.edges.tracking:
            self._fire_edge_tap()
            return
        r = self.face_rect
        swipe = self.edges.release((pos[0] - r.x) / max(1, r.w), (pos[1] - r.y) / max(1, r.h))
        if swipe is None:
            self._fire_edge_tap()  # it didn't slide: a tap
        else:
            self._level_done(swipe, "touch")

    def _fire_edge_tap(self) -> None:
        tap, self._edge_tap = self._edge_tap, None
        if tap is not None:
            tap[1]()

    def check_edges(self, now: float) -> None:
        """Per frame: rate-limited levels, a resting finger's held-back tap, levels changed elsewhere."""
        edges = self.edges
        if edges.tracking and (self._games() is not None or self._screens() is not None):
            swipe = edges.cancel()  # something else took the screen
            if swipe is not None:
                self._level_done(swipe, "touch")
        edges.tick()
        tap = self._edge_tap
        if tap is not None and now - tap[0] >= EC.STILL_SECONDS:
            self._fire_edge_tap()
        while self._level_news:
            key, value = self._level_news.popleft()
            if key == "audio.volume":
                edges.show("volume", value)
            elif key == "display.brightness":
                level = EC.Brightness.clamp(value)
                if not (edges.active and edges.side == "left") and abs(level - self.brightness.level) > 0.004:
                    self.brightness.set(level)
                edges.show("brightness", level)

    def nudge_level(self, name: str, sign: int) -> None:
        """Keyboard (desktop simulator): one step of the volume or the brightness, with the bar."""
        swipe = self.edges.nudge(name, sign * EC.KEY_STEP)
        if swipe is not None:
            self._level_done(swipe, "keys")

    def _level_done(self, swipe: Any, how: str) -> None:
        extra = ""
        if swipe.name == "brightness":
            extra = f" ({self.brightness.mode})"
            settings = self.ctx.settings
            self._offload("brightness", lambda v: settings.set("display.brightness", v, source="display"), swipe.end)
        log.info("%s: %s %d%% -> %d%%%s", how, swipe.name, round(swipe.start * 100), round(swipe.end * 100), extra)

    def _set_volume(self, value: float) -> None:
        audio = self.ctx.audio
        if audio is not None and hasattr(audio, "set_volume"):
            fn: Callable[[Any], Any] = audio.set_volume  # clamps to the safe maximum, persists audio.volume
        else:  # the live voice reads the setting too
            settings = self.ctx.settings

            def fn(v: Any) -> Any:
                return settings.set("audio.volume", v, source="display")
        self._offload("volume", fn, value)

    def _volume_blip(self, value: float, final: bool) -> None:
        """A soft blip at the new level, only while the speaker is free (it never queues behind speech)."""
        audio = self.ctx.audio
        if audio is None or value <= 0.0 or not self._speaker_idle(audio):
            return
        self._submit(lambda: audio.play_sound(VOLUME_BLIP, wait=False, volume=VOLUME_BLIP_LEVEL), "volume blip")

    @staticmethod
    def _speaker_idle(audio: Any) -> bool:
        try:
            st = audio.status()
        except Exception:  # noqa: BLE001
            return False
        return not any(st.get(k) for k in ("speaking", "playing", "queued", "listening", "lent_to"))

    def _offload(self, key: str, fn: Callable[[Any], Any], value: Any) -> None:
        """Run ``fn(value)`` off the display thread (it writes SQLite); only the latest value per ``key``."""
        runtime = getattr(self.robot, "runtime", None)
        with self._off_lock:
            self._off_pending[key] = (fn, value)
            if key in self._off_running:
                return  # the running worker picks the newest value up
            self._off_running.add(key)

        async def worker() -> None:
            while True:
                with self._off_lock:
                    item = self._off_pending.pop(key, None)
                    if item is None:
                        self._off_running.discard(key)
                        return
                try:
                    await asyncio.to_thread(item[0], item[1])
                except Exception:  # noqa: BLE001
                    log.warning("could not set %s", key, exc_info=True)

        submitted = False
        if runtime is not None:
            coro = worker()
            try:
                runtime.submit(coro)
                submitted = True
            except Exception:  # noqa: BLE001 - runtime not started / shutting down
                coro.close()
        if not submitted:  # no async runtime: apply here
            with self._off_lock:
                item = self._off_pending.pop(key, None)
                self._off_running.discard(key)
            if item is not None:
                try:
                    item[0](item[1])
                except Exception:  # noqa: BLE001
                    log.warning("could not set %s", key, exc_info=True)

    def _watch_levels(self) -> None:
        from littlebot.core.events import Topics

        if self.brightness.backlight is not None:  # restore the saved level on the real backlight
            self.brightness.set(self.brightness.level)
        self._levels_sub = self.ctx.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_level_setting)

    def _on_level_setting(self, event: Any) -> None:
        """Bus thread: just note it; the display loop shows the bar (dashboard, voice, another source)."""
        key = event.get("key")
        if key in ("audio.volume", "display.brightness"):
            self._level_news.append((key, event.get("value")))

    # ------------------------------------------------------------------ input
    def _handle_events(self) -> bool:
        pg = self._pg
        for ev in pg.event.get():
            if ev.type == pg.QUIT:
                self.robot.request_shutdown("window closed")
                return False
            if ev.type == pg.KEYDOWN:
                if not self.handle_key(ev.key, getattr(ev, "mod", 0)):
                    return False
            elif ev.type == pg.MOUSEBUTTONDOWN:
                if ev.button == 1 and self._bounced(ev.pos):
                    continue  # the finger never left: it goes on as a move
                self._flush_lift(force=True)
                if ev.button == 1:
                    self._pointer_down = True
                self.handle_click(ev.pos, ev.button, getattr(ev, "mod", None))
            elif ev.type == pg.MOUSEMOTION:
                buttons = tuple(getattr(ev, "buttons", ()) or ())
                if self._pointer_down and not any(buttons[:1]):
                    buttons = (1, *buttons[1:])
                self._pointer_moved(ev.pos, buttons)
            elif ev.type == pg.MOUSEBUTTONUP:
                if ev.button == 1:
                    self._pointer_down = False
                    self._flush_lift(force=True)
                    self._lift = (ev.pos, time.monotonic())
                else:
                    self._press = None
        self._flush_lift()
        return True

    def _bounced(self, pos: tuple[int, int]) -> bool:
        """A press right after a lift, near it: the touch screen flickered. True = the lift never happened."""
        lift = self._lift
        if lift is None or time.monotonic() - lift[1] >= self.touch_bounce_s \
                or abs(pos[0] - lift[0][0]) + abs(pos[1] - lift[0][1]) > TOUCH_BOUNCE_PX:
            return False
        self._lift, self._pointer_down = None, True
        self._pointer_moved(pos, (1,))
        return True

    def _flush_lift(self, *, force: bool = False) -> None:
        """The held-back lift, once TOUCH_BOUNCE_S has passed (or at once: another press or lift came)."""
        lift = self._lift
        if lift is None or (not force and time.monotonic() - lift[1] < self.touch_bounce_s):
            return
        self._lift = None
        self._press = None
        self._edge_release(lift[0])
        self._card_pointer("release", lift[0])

    def handle_key(self, key: int, mod: int = 0) -> bool:
        """Apply one key press. Returns False when the user asked to quit."""
        pg = self._pg
        if self._game_key(key) or self._card_key(key):
            return True
        face = self.face
        ctx = self.ctx
        shift = bool(mod & pg.KMOD_SHIFT)
        digits = {pg.K_1: 0, pg.K_2: 1, pg.K_3: 2, pg.K_4: 3, pg.K_5: 4, pg.K_6: 5, pg.K_7: 6, pg.K_8: 7,
                  pg.K_9: 8, pg.K_0: 9}
        if key in digits:
            idx = digits[key]
            if shift:
                face.play_animation(DIGIT_ANIMATIONS[idx])
            else:
                face.set_expression(DIGIT_EXPRESSIONS[idx])
            return True
        letter_expr = {pg.K_j: "playful", pg.K_k: "speaking", pg.K_l: "affectionate", pg.K_v: "celebrating"}
        if key in letter_expr:
            face.set_expression(letter_expr[key])
            return True
        states = {pg.K_F1: "idle", pg.K_F2: "listening", pg.K_F3: "thinking", pg.K_F4: "speaking",
                  pg.K_F5: "sleeping", pg.K_F6: "error", pg.K_F7: "hidden"}
        if key in states:
            face.set_state(states[key])
            return True
        nudges = {pg.K_MINUS: ("volume", -1), pg.K_EQUALS: ("volume", 1), pg.K_LEFTBRACKET: ("brightness", -1),
                  pg.K_RIGHTBRACKET: ("brightness", 1)}
        if key in nudges:
            self.nudge_level(*nudges[key])
            return True
        if key in (pg.K_q, pg.K_ESCAPE):
            if key == pg.K_ESCAPE and self.show_help:
                self.show_help = False
                return True
            self.robot.request_shutdown("keyboard quit")
            return False
        if key == pg.K_b:
            face.blink(False)
        elif key == pg.K_n:
            face.blink(True)
        elif key in (pg.K_LEFT, pg.K_RIGHT, pg.K_UP, pg.K_DOWN):
            dx = {pg.K_LEFT: -0.25, pg.K_RIGHT: 0.25}.get(key, 0.0)
            dy = {pg.K_UP: -0.25, pg.K_DOWN: 0.25}.get(key, 0.0)
            self._gaze = [min(1.0, max(0.0, self._gaze[0] + dx)), min(1.0, max(0.0, self._gaze[1] + dy))]
            face.look_at(self._gaze[0], self._gaze[1], hold=4.0)
        elif key == pg.K_r:
            self._gaze = [0.5, 0.5]
            face.clear_gaze()
        elif key == pg.K_o:
            closed = bool(_safe_status(face).get("eyes_closed"))
            face.open_eyes() if closed else face.close_eyes()
        elif key == pg.K_g:
            self.show_debug = not self.show_debug
        elif key == pg.K_h:
            self.show_help = not self.show_help
        elif key == pg.K_m:
            self.toggle_demo()
        elif key == pg.K_t:
            self.test_speech()
        elif key == pg.K_p:
            if ctx.activities is not None:
                self._submit(lambda: ctx.activities.start("peekaboo", source="keyboard"), "peekaboo activity")
            else:
                log.info("activities unavailable; P ignored")
        elif key == pg.K_c:
            if ctx.activities is not None:
                try:
                    ok = ctx.activities.respond("done")
                    log.info("caregiver confirm -> %s", ok)
                except Exception:  # noqa: BLE001
                    log.exception("caregiver respond failed")
        elif key == pg.K_x:
            self._submit(lambda: self.robot.stop_all("keyboard"), "stop all")
        elif key == pg.K_e:
            self.emergency_stop("keyboard")
        elif key == pg.K_SPACE:
            if ctx.brain is not None:
                self._submit(lambda: ctx.brain.converse_once(), "push-to-talk")
            else:
                log.info("brain unavailable; push-to-talk ignored")
        elif key == pg.K_w:
            if ctx.motion is not None:
                self._submit(lambda: ctx.motion.run_gesture("wave"), "wave gesture")
        elif key in (pg.K_f, pg.K_F11):
            self.toggle_fullscreen()
        elif key == pg.K_F12:
            self.save_screenshot()
        return True

    def handle_click(self, pos: tuple[int, int], button: int, mod: int | None = None) -> None:
        pg = self._pg
        if self.panel is not None and self.panel.estop_rect is not None and self.show_panel \
                and self.panel.estop_rect.collidepoint(pos) and button == 1:
            self.emergency_stop("sim panel")
            return
        if not self.face_rect.collidepoint(pos):
            return
        nx = (pos[0] - self.face_rect.x) / max(1, self.face_rect.w)
        ny = (pos[1] - self.face_rect.y) / max(1, self.face_rect.h)
        if button == 1 and (self._game_tap(nx, ny) or self._card_tap(nx, ny)):
            return
        if button == 1:
            self._press = (nx, ny, time.monotonic())  # held still for a second: the games menu
        if mod is None:
            mod = pg.key.get_mods()
        shift = bool(mod & pg.KMOD_SHIFT)
        vision = self.ctx.vision
        if button == 3:
            if vision is not None and self.sim_mode:
                vision.simulate_no_face()
            else:
                self.face.clear_gaze()
            return
        if button != 1:
            return
        if self.edges_allowed() and self.edges.press(nx, ny):
            self._edge_tap = (time.monotonic(), lambda: self._face_press(nx, ny, shift))
            return  # maybe a slide: the usual reaction waits for the release (or a resting finger)
        self.edges.cancel()
        self._face_press(nx, ny, shift)

    def _face_press(self, nx: float, ny: float, shift: bool) -> None:
        """A finger (or click) on the face: a simulated camera face in sim mode, else the giggle."""
        vision = self.ctx.vision
        if self.sim_mode and not shift:
            if vision is not None:
                # (nx, ny) is where the pretend person is on LittleBot's screen, i.e. where the eyes
                # should look; simulate_face() takes camera-image coordinates, which are mirrored.
                mirror = getattr(getattr(self.ctx.config, "camera", None), "mirror", True)
                vision.simulate_face(1.0 - nx if mirror else nx, ny)
            else:
                self.face.look_at(nx, ny)
            return
        self.tap_reaction(nx, ny)

    def tap_reaction(self, nx: float, ny: float) -> bool:
        """Gentle giggle + bounce when the child taps the face. Returns True if it reacted."""
        if not TAP_REACTION_ENABLED or not getattr(self.ctx.config.display, "tap_reaction", True):
            return False
        now = time.monotonic()
        if now - self._last_tap < TAP_COOLDOWN_SECONDS:
            return False
        self._last_tap = now
        face = self.face
        face.look_at(nx, ny, hold=1.5)
        face.set_expression("happy", hold=1.6)
        face.play_animation("bounce")
        audio = self.ctx.audio
        if audio is not None:
            self._submit(lambda: audio.play_sound(TAP_REACTION_SOUND, wait=False), "tap giggle")
        return True

    def emergency_stop(self, source: str) -> None:
        motion = self.ctx.motion
        if motion is None:
            log.info("motion unavailable; emergency stop has nothing to stop")
            return
        try:
            motion.emergency_stop(source)
            log.warning("motion emergency stop (%s)", source)
        except Exception:  # noqa: BLE001
            log.exception("emergency stop failed")

    def toggle_demo(self) -> None:
        from littlebot.face.demo import DemoSequence

        if self.demo is None:
            self.demo = DemoSequence(self.face, loop=True)
            log.info("face demo started")
        else:
            self.demo = None
            try:
                self.face.set_state("idle")
                self.face.open_eyes()
                self.face.set_expression("neutral")
                self.face.clear_gaze()
            except Exception:  # noqa: BLE001
                pass
            log.info("face demo stopped")

    def test_speech(self) -> None:
        audio = self.ctx.audio
        if audio is not None:
            self._submit(lambda: audio.say(TEST_PHRASE), "test speech")
            return
        from littlebot.core.events import Topics

        bus = self.ctx.bus
        bus.publish(Topics.AUDIO_SPEECH_STARTED, {"text": TEST_PHRASE, "duration_estimate": 2.5}, source="display")
        self.after(2.5, lambda: bus.publish(Topics.AUDIO_SPEECH_FINISHED, {"text": TEST_PHRASE, "interrupted": False},
                                            source="display"))

    def save_screenshot(self, directory: str | os.PathLike[str] | None = None) -> Path | None:
        from littlebot.core.paths import PROJECT_ROOT

        d = Path(directory) if directory else PROJECT_ROOT / "artifacts" / "face"
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / time.strftime("screenshot-%Y%m%d-%H%M%S.png")
            self._pg.image.save(self.window, str(path))
            log.info("screenshot saved to %s", path)
            return path
        except Exception as e:  # noqa: BLE001
            log.warning("screenshot failed: %s", e)
            return None

    def _submit(self, make: Callable[[], Coroutine[Any, Any, Any]], what: str) -> None:
        runtime = getattr(self.robot, "runtime", None)
        if runtime is None:
            log.info("%s ignored: no async runtime", what)
            return
        try:
            coro = make()
        except Exception:  # noqa: BLE001
            log.exception("%s failed", what)
            return
        try:
            fut = runtime.submit(coro)
        except Exception as e:  # noqa: BLE001 - runtime not started / shutting down
            coro.close()
            log.warning("%s not run: %s", what, e)
            return

        def _done(f: Any) -> None:
            try:
                f.result()
            except PermissionError as e:
                log.warning("%s refused: %s", what, e)
                try:
                    self.face.set_expression("confused", hold=1.5)
                except Exception:  # noqa: BLE001
                    pass
            except KeyError as e:
                log.warning("%s: unknown %s", what, e)
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed: %s", what, e)

        fut.add_done_callback(_done)


DIGIT_EXPRESSIONS: tuple[str, ...] = (
    "neutral", "happy", "excited", "curious", "surprised", "sleepy", "sad", "thinking", "listening", "confused",
)
DIGIT_ANIMATIONS: tuple[str, ...] = (
    "bounce", "wiggle", "nod", "shake", "spin_eyes", "sparkle", "hearts", "zzz", "confetti", "peek",
)


def _safe_status(face: Any) -> dict[str, Any]:
    try:
        return face.status()
    except Exception:  # noqa: BLE001
        return {}
