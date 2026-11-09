"""Main-thread display loop and desktop simulator window.

``run_display(robot)`` opens the face window (``display.width`` x ``display.height``: the 7" screen's
1024x600, times ``display.scale`` on a desktop, or fullscreen), drives the face animation (drawn at
``frame.face_size`` and scaled to the window; cards and games draw at the screen's own size),
handles keyboard / mouse / touch input and (on the desktop) draws the
simulator side panel. It blocks until the window is closed or
``robot.shutdown_event`` is set, then returns; ``main.py`` calls
``robot.shutdown()``.

If no display can be opened (no X / Wayland / KMS, missing SDL video driver)
the error is logged clearly and the function falls back to waiting on
``robot.shutdown_event`` - the robot keeps running headless.

After a long quiet spell the face says the screen may go dark (``face.screen_off``, littlebot/face/doze.py):
on the robot's own screen the loop switches its HDMI signal off with ``wlopm`` (about 0.4 W less, measured
on the robot 2026-11-03) and draws nothing until it lights again; the first touch on the dark screen only
wakes it. The screen is always switched back on when the window closes or opens.

``robot`` is ``app.robot.LittleBot`` (or any object with ``ctx``, ``runtime``,
``shutdown_event``, ``status()``, ``stop_all(source)`` and
``request_shutdown(reason)``). Every ``ctx.<subsystem>`` may be ``None``.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
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
# Holding a finger still anywhere on the screen this long opens LittleBot's settings (littlebot/screens/cards/
# settings.py: volume, brightness, dark mode, the games...), over the face, a card or a game. A plain tap still
# giggles: babies tap the face all the time; five seconds of a still finger is a grown-up's. After HOLD_RING_FROM
# seconds a ring fills around the finger, so whoever holds knows it is coming.
SETTINGS_HOLD_SECONDS = 5.0
HOLD_RING_FROM = 1.0
HOLD_RING_R = 64  # px on the 1024x600 screen (scaled with the face)
DOUBLE_TAP_SECONDS = 0.45  # two taps on the idle face this close together: today at a glance
LONG_PRESS_SLOP = 0.04  # of the face width: a finger that wanders further is not holding still
# A resistive touch screen (the old 3.5" one) often flickers as a finger lands: down, up, down again within a few
# ms. A lift is held back this long; a press again near it (TOUCH_BOUNCE_PX) means the finger never left. The 7"
# screen's capacitive touch doesn't flicker; the short hold-back is harmless there (a tap ends 0.1 s later).
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
# Overlays on the face (the camera sign, the setup code) are sized for a face this tall and scaled with it:
# 1.5x on the 7" screen's 600 px (the camera sign is then 45 px, about 7 mm on the glass).
OVERLAY_REF_H = 400
LIVE_BADGE_WIDTH = 30  # px on a face OVERLAY_REF_H tall
LIVE_BADGE_MARGIN = 12
LIVE_BADGE_INK = (66, 32, 62, 150)  # deep plum, ~60 % opaque
LIVE_BADGE_DOT = (146, 40, 82, 210)  # raspberry "on" dot
LIVE_BADGE_DOT_DARK = (255, 120, 146, 220)
# While LittleBot records a video (littlebot/capture): a red dot and "REC 0:42" at the top centre, over cards
# too, so everyone in the room can see they are being filmed.
REC_TOPIC = "capture.recording"  # littlebot.capture.service.RECORDING
REC_RED = (232, 44, 52)
REC_TEXT = 24  # px on a face OVERLAY_REF_H tall (36 px on the 7" screen)
STATUS_POLL_SECONDS = 0.5
MOTION_POLL_SECONDS = 1 / 15
SCREEN_RETRY_SECONDS = 5.0  # a wlopm call that failed is tried again after this long
SCREEN_TOUCH = "someone touched the screen"
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
    ("hold click", "hold 5 s anywhere: LittleBot's settings (and the games)"),
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


def find_output(name: str | None, *, run: Callable[..., Any] = subprocess.run,
                which: Callable[[str], str | None] = shutil.which, env: Any = None) -> str:
    """The screen's output name. ``name`` ("auto", or e.g. "HDMI-A-1") is used when that output is on;
    otherwise (auto, or that port has no screen) the first enabled HDMI output (the Pi 5's two micro-HDMI ports
    are HDMI-A-1 and HDMI-A-2), else the first enabled one. Without Wayland / wlr-randr: ``name``, or HDMI-A-1."""
    env = os.environ if env is None else env
    wanted = (name or "").strip()
    fallback = wanted if wanted and wanted.lower() != "auto" else "HDMI-A-1"
    if not env.get("WAYLAND_DISPLAY") or which("wlr-randr") is None:
        return fallback
    try:
        res = run(["wlr-randr"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        log.debug("could not list the screens", exc_info=True)
        return fallback
    if res.returncode != 0:
        return fallback
    outputs: list[tuple[str, bool]] = []  # (name, enabled), in the compositor's order
    for line in (res.stdout or "").splitlines():
        if line and not line[0].isspace():
            outputs.append((line.split()[0], True))
        elif outputs and re.match(r"\s*Enabled:\s*no\b", line):
            outputs[-1] = (outputs[-1][0], False)
    names = [n for n, _ in outputs]
    enabled = [n for n, on in outputs if on]
    if wanted and wanted.lower() != "auto" and wanted in enabled:
        return wanted
    pick = next((n for n in enabled if n.startswith("HDMI")), None) or (enabled or names or [fallback])[0]
    if wanted and wanted.lower() != "auto":
        log.warning("screen %s not found (screens: %s): using %s", wanted, ", ".join(names) or "none", pick)
    else:
        log.info("screen found on %s", pick)
    return pick


def apply_output_mode(output: str, mode: str, *, run: Callable[..., Any] = subprocess.run,
                      which: Callable[[str], str | None] = shutil.which,
                      env: Any = None, settle_s: float = 0.5) -> bool:
    """Ask the Wayland compositor to drive ``output`` at ``mode`` (e.g. ``"1024x600@60"``).

    The 7" GeeekPi panel is 1024x600; a desktop that picked another mode from its EDID (some of these
    boards also offer 1280x720 or 1920x1080, which their scaler squeezes) is put back on the panel's own
    pixels. (The old 3.5" MPI3508 needed ``"720x480@60"`` the same way.) Uses ``wlr-randr`` and only acts in a
    Wayland session; never fatal. Returns True when the mode was changed.
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


def set_output_power(output: str, on: bool, *, run: Callable[..., Any] = subprocess.run, env: Any = None) -> bool:
    """Switch the screen's signal on or off with ``wlopm`` (the compositor's output power, DPMS). True when it
    worked. The MPI3508 keeps its backlight lit (only the button on its back turns that off), but the HDMI
    output and the panel's scaler still draw about 0.4 W less (the robot, 2026-11-03). Never fatal."""
    env = os.environ if env is None else env
    try:
        res = run(["wlopm", "--on" if on else "--off", output], capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not switch screen %s %s: %s", output, "on" if on else "off", e)
        return False
    if res.returncode != 0:
        log.warning("could not switch screen %s %s: %s", output, "on" if on else "off",
                    (res.stderr or res.stdout or "").strip() or f"wlopm exit {res.returncode}")
        return False
    return True


class ScreenPower:
    """The robot screen's signal, switched off the display thread so a slow compositor never stalls the face.

    ``want(on, now)`` asks for a state; one worker thread makes the ``wlopm`` call, always for the newest wish.
    ``dark`` is True only once the screen really went off (the loop stops drawing then). A failed call is
    retried after SCREEN_RETRY_SECONDS."""

    def __init__(self, output: str, *, switch: Callable[[str, bool], bool] = set_output_power,
                 start: Callable[[Callable[[], None]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.output = output
        self.dark = False
        self._switch = switch
        self._clock = clock  # the same clock as want()'s ``now``
        self._start = start or (lambda fn: threading.Thread(target=fn, name="screen-power", daemon=True).start())
        self._lock = threading.Lock()
        self._target_on = True
        self._busy = False
        self._failed_at = float("-inf")

    def want(self, on: bool, now: float) -> None:
        with self._lock:
            self._target_on = on
            if self._busy or on != self.dark:  # running (it picks the wish up), or already as wanted
                return
            if now - self._failed_at < SCREEN_RETRY_SECONDS:
                return
            self._busy = True
        self._start(self._work)

    def _work(self) -> None:
        while True:
            with self._lock:
                on = self._target_on
            ok = self._switch(self.output, on)
            with self._lock:
                if ok:
                    self.dark = not on
                else:
                    self._failed_at = self._clock()
                if not ok or self._target_on == (not self.dark):
                    self._busy = False
                    return

    def restore(self) -> None:
        """Back on, now, on this thread (the window is closing or opening: never leave the robot dark)."""
        if self._switch(self.output, True):
            with self._lock:
                self.dark, self._target_on = False, True


def make_screen_power(output: str, *, fullscreen: bool, sim_mode: bool, env: Any = None,
                      which: Callable[[str], str | None] = shutil.which) -> ScreenPower | None:
    """A ScreenPower for the robot's own screen only (fullscreen, not the desktop simulator), in a Wayland session
    with ``wlopm``; else None (logged once: the face still dozes, the screen just stays on)."""
    env = os.environ if env is None else env
    if not fullscreen or sim_mode:
        return None
    if not env.get("WAYLAND_DISPLAY") or which("wlopm") is None:
        log.info("screen off after a quiet spell: not available (needs a Wayland session with wlopm)")
        return None
    log.info("screen off after a quiet spell: wlopm on %s", output)
    return ScreenPower(output)


def apply_output_transform(output: str, flipped: bool, *, run: Callable[..., Any] = subprocess.run,
                           which: Callable[[str], str | None] = shutil.which, env: Any = None,
                           settle_s: float = 0.3) -> bool:
    """Turn the picture on ``output`` the right way up ("normal"), or round ("180") when ``flipped`` (a screen
    mounted upside down; setting ``display.flipped``). LittleBot sets it at every start, so a turn left in the
    desktop's settings (e.g. kanshi's, from the old screen) doesn't keep the screen upside down. Uses
    ``wlr-randr`` in a Wayland session; never fatal. True when it was set."""
    env = os.environ if env is None else env
    transform = "180" if flipped else "normal"
    if not env.get("WAYLAND_DISPLAY") or which("wlr-randr") is None:
        log.info("screen turn (%s) for %s skipped: needs a Wayland session with wlr-randr", transform, output)
        return False
    try:
        res = run(["wlr-randr", "--output", output, "--transform", transform], capture_output=True, text=True,
                  timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not turn screen %s to %s: %s", output, transform, e)
        return False
    if res.returncode != 0:
        log.warning("could not turn screen %s to %s: %s", output, transform, (res.stderr or res.stdout or "").strip())
        return False
    log.info("screen %s: %s", output, "turned round (display.flipped)" if flipped else "the right way up")
    if settle_s > 0:
        time.sleep(settle_s)
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
        # Recording a video since (monotonic), else None: the REC sign shows.
        self.recording_since: float | None = None
        self._rec_sub: Any = None
        self._rec_label: tuple[str, int, Any] | None = None
        self._game_t: float | None = None  # last frame time while an on-screen game runs
        self._card_t: float | None = None  # last frame time while an info card shows
        self._press: tuple[float, float, float] | None = None  # (nx, ny, when) a finger held on the idle face
        self._hold: tuple[float, float, float] | None = None  # (nx, ny, when) a still finger anywhere: the settings
        self.dark = bool(self._setting("display.theme", "light") == "dark")  # dark mode (display.theme)
        self.flipped = bool(self._setting("display.flipped", False))  # the picture turned round (display.flipped)
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
        # The screen's signal off after a quiet spell (the robot's own screen only; made in open()).
        self.screen_power: ScreenPower | None = None
        self.output_name = str(self.cfg.output_name)  # the screen's port, found in open() ("auto")
        self._waking_touch = False  # the finger that woke the dark screen: its press does nothing else
        self._woke_lift = float("-inf")  # when that finger lifted (a flicker right after is still it)

    # ------------------------------------------------------------------ setup
    def open(self) -> None:
        import pygame

        self._pg = pygame
        self.output_name = find_output(self.cfg.output_name) if self.fullscreen else self.cfg.output_name
        if self.fullscreen and self.cfg.output_mode:
            apply_output_mode(self.output_name, self.cfg.output_mode)
        if self.fullscreen:  # the right way up (display.flipped; a turn left over from the old screen goes)
            apply_output_transform(self.output_name, self._setting("display.flipped", False))
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
        self._watch_recording()
        self._watch_levels()
        self._watch_power()
        self.screen_power = make_screen_power(self.output_name, fullscreen=self.fullscreen,
                                              sim_mode=self.sim_mode)
        if self.screen_power is not None:
            self.screen_power.restore()  # a crash may have left it dark
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
        if self._rec_sub is not None:
            self._rec_sub.cancel()
            self._rec_sub = None
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
        if self.screen_power is not None:
            self.screen_power.restore()  # never leave the robot with a dark screen
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

    def _watch_recording(self) -> None:
        capture = getattr(self.ctx, "capture", None)
        try:  # a recording may be going already when the window opens
            rec = capture.recording if capture is not None else None
            self.recording_since = time.monotonic() - float(rec.get("seconds") or 0.0) if rec else None
        except Exception:  # noqa: BLE001
            self.recording_since = None
        self._rec_sub = self.ctx.bus.subscribe(REC_TOPIC, self._on_recording)

    def _on_recording(self, event: Any) -> None:
        if not event.get("active"):
            self.recording_since = None
            return
        try:
            ago = max(0.0, time.time() - float(event.get("started_at") or time.time()))
        except (TypeError, ValueError):
            ago = 0.0
        self.recording_since = time.monotonic() - ago

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
                self._sync_screen(now)
                if not self.screen_dark:  # dark: nothing to draw (and no flip a sleeping output might hold up)
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

    @property
    def screen_dark(self) -> bool:
        """The robot's screen is really off (its signal switched off after a quiet spell)."""
        return self.screen_power is not None and self.screen_power.dark

    def _sync_screen(self, now: float) -> None:
        """Per frame: the screen's signal follows the face's screen-off decision (switched off this thread)."""
        if self.screen_power is not None:
            self.screen_power.want(not getattr(self.face, "screen_off", False), now)

    def target_fps(self) -> int:
        fn = getattr(self.face, "target_fps", None)
        if self.screen_dark:  # nothing is drawn: only the events (a touch) and the face's timers run
            return max(1, int(fn()) if callable(fn) else 1)
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
        from littlebot.games import art

        art.set_dark(self.dark)  # cards, games and the bars draw in the theme (here: the display thread)
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
        if self.recording_since is not None:
            self._draw_rec_badge(win)
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
        self._draw_hold_ring(win, now)

    def face_hidden(self) -> bool:
        try:
            return not self.face.status().get("visible", True)
        except Exception:  # noqa: BLE001
            return False

    def _draw_setup_banner(self, win: Any) -> None:
        """Small strip at the bottom of the face: the one-time dashboard setup code."""
        pg = self._pg
        r = self.face_rect
        s = r.h / OVERLAY_REF_H
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

    def _draw_rec_badge(self, win: Any) -> None:
        """While recording: a dark pill with a blinking red dot and "REC m:ss", top centre."""
        since = self.recording_since
        if since is None:
            return
        pg = self._pg
        r = self.face_rect
        s = r.h / OVERLAY_REF_H
        secs = max(0, int(time.monotonic() - since))
        text = f"REC {secs // 60}:{secs % 60:02d}"
        size = max(14, int(round(REC_TEXT * s)))
        cached = self._rec_label
        if cached is None or cached[0] != text or cached[1] != size:
            label = self._font(size).render(text, True, (255, 255, 255))
            cached = self._rec_label = (text, size, label)
        label = cached[2]
        pad = max(4, int(round(6 * s)))
        dot_r = max(3, int(round(label.get_height() * 0.28)))
        h = label.get_height() + 2 * pad
        w = 2 * pad + 2 * dot_r + pad + label.get_width() + pad
        x, y = r.centerx - w // 2, r.y + max(4, int(round(8 * s)))
        box = pg.Surface((w, h), pg.SRCALPHA)
        pg.draw.rect(box, (0, 0, 0, 165), box.get_rect(), border_radius=h // 2)
        win.blit(box, (x, y))
        if (time.monotonic() - since) % 1.0 < 0.65:  # blinks once a second
            pg.draw.circle(win, REC_RED, (x + 2 * pad + dot_r - pad // 2, y + h // 2), dot_r)
        win.blit(label, (x + 2 * pad + 2 * dot_r + pad // 2, y + (h - label.get_height()) // 2))

    def rec_badge_rect(self) -> Any:
        """Where the REC sign is drawn (tests / screenshots), or None when not recording."""
        if self.recording_since is None:
            return None
        cached = self._rec_label
        s = self.face_rect.h / OVERLAY_REF_H
        pad = max(4, int(round(6 * s)))
        lw, lh = (cached[2].get_width(), cached[2].get_height()) if cached else (60, 16)
        dot_r = max(3, int(round(lh * 0.28)))
        w, h = 2 * pad + 2 * dot_r + pad + lw + pad, lh + 2 * pad
        return self._pg.Rect(self.face_rect.centerx - w // 2, self.face_rect.y + max(4, int(round(8 * s))), w, h)

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
        s = r.h / OVERLAY_REF_H  # scaled with the face
        width = max(12, int(round(LIVE_BADGE_WIDTH * s)))
        badge = self._badges.get((width, self.dark))
        if badge is None:
            badge = self._badges[(width, self.dark)] = self._make_live_badge(width)
        m = int(round(LIVE_BADGE_MARGIN * s))
        return badge, (r.right - badge.get_width() - m, r.y + m)

    def _make_live_badge(self, width: int) -> Any:
        """A rounded little camera with a dot beside it, drawn 4x and smooth-scaled (soft edges like the eyes)."""
        from littlebot.face.frame import is_light_background

        pg = self._pg
        face_cfg = getattr(self.ctx.config, "face", None)
        bg = tuple(getattr(face_cfg, "dark_background_color" if self.dark else "background_color",
                           (255, 214, 228)))[:3]  # (dark mode: the night-blue face)
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
        """A finger held still SETTINGS_HOLD_SECONDS anywhere on the screen opens LittleBot's settings (a running
        game stops for it). True when it opened them."""
        hold = self._hold
        if hold is None or self._lift is not None or now - hold[2] < SETTINGS_HOLD_SECONDS:
            return False  # (a lift held back: the finger is up)
        self._hold = self._press = None
        self._edge_tap = None
        self.edges.cancel()
        screens = getattr(self.ctx, "screens", None)
        if screens is None or self.face_hidden():
            return False
        log.info("5 s hold on the screen: settings")
        games = self._games()

        async def open_settings() -> Any:
            if games is not None:
                try:
                    games.stop("settings")
                except Exception:  # noqa: BLE001
                    log.debug("stopping the game for the settings failed", exc_info=True)
            return await screens.show({"card": "settings"}, source="touch")

        self._submit(open_settings, "settings")
        return True

    def hold_progress(self, now: float) -> float | None:
        """How far a still finger is towards the settings (0..1) once the ring shows, else None."""
        hold = self._hold
        if hold is None or self._lift is not None or now - hold[2] < HOLD_RING_FROM:
            return None
        return min(1.0, (now - hold[2] - HOLD_RING_FROM) / (SETTINGS_HOLD_SECONDS - HOLD_RING_FROM))

    def _draw_hold_ring(self, win: Any, now: float) -> None:
        """A ring filling around a held finger (over everything): the settings are coming."""
        p = self.hold_progress(now)
        if p is None or self._hold is None:
            return
        from littlebot.games import art

        pg = self._pg
        r = self.face_rect
        s = r.h / 600
        cx, cy = r.x + self._hold[0] * r.w, r.y + self._hold[1] * r.h
        rad = HOLD_RING_R * s
        art.blit_center(win, art.ring(rad, 10 * s, art.WHITE, 150), (cx, cy))
        if p > 0.0:
            box = pg.Rect(0, 0, int(2 * rad), int(2 * rad))
            box.center = (int(cx), int(cy))
            pg.draw.arc(win, art.SKY, box, math.pi / 2 - 2 * math.pi * p, math.pi / 2, max(3, int(10 * s)))
        label = art.pill("Settings", max(18, int(28 * s)), art.INK, art.WHITE, pad=(14, 6))
        art.blit_center(win, label, (cx, max(r.y + label.get_height(), cy - rad - 26 * s)))

    def _pointer_moved(self, pos: tuple[int, int], buttons: tuple[int, ...]) -> None:
        if self.edges.tracking and self._edge_moved(pos):
            return
        if buttons and buttons[0]:
            self._card_pointer("drag", pos)
        r = self.face_rect
        nx, ny = (pos[0] - r.x) / max(1, r.w), (pos[1] - r.y) / max(1, r.h)
        for name in ("_press", "_hold"):  # a finger that wanders is neither a tap nor a hold
            held = getattr(self, name)
            if held is not None and (abs(nx - held[0]) > LONG_PRESS_SLOP or abs(ny - held[1]) > LONG_PRESS_SLOP * 1.5):
                setattr(self, name, None)

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
        elif key == "display.theme":  # dark mode: the next frame draws in it (art.set_dark, on the display thread)
            self.dark = event.get("value") == "dark"
        elif key == "display.flipped":
            self.flipped = bool(event.get("value"))
            if self.fullscreen and self._opened:
                output, flipped = self.output_name, self.flipped
                threading.Thread(target=lambda: apply_output_transform(output, flipped, settle_s=0),
                                 name="screen-flip", daemon=True).start()

    def _setting(self, key: str, default: Any) -> Any:
        settings = getattr(self.ctx, "settings", None)
        try:
            return settings.get(key, default) if settings is not None else default
        except Exception:  # noqa: BLE001
            return default

    # ------------------------------------------------------------------ input
    def _handle_events(self) -> bool:
        pg = self._pg
        for ev in pg.event.get():
            if ev.type == pg.QUIT:
                self.robot.request_shutdown("window closed")
                return False
            if ev.type in (pg.MOUSEBUTTONDOWN, pg.MOUSEMOTION, pg.MOUSEBUTTONUP) and self._wake_touch(ev, pg):
                continue
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

    def _wake_touch(self, ev: Any, pg: Any) -> bool:
        """A touch on the dark screen only lights it (no giggle, game or swipe from a finger that can't see);
        the rest of that touch is used up too. True = the event is used up."""
        if ev.type == pg.MOUSEBUTTONDOWN and not self._waking_touch:
            if time.monotonic() - self._woke_lift < self.touch_bounce_s:
                self._waking_touch = True  # the touch screen flickered: still the finger that woke it
                return True
            if not self.screen_dark:
                return False
            self._waking_touch = True
            self._press, self._lift = None, None
            log.info("touch on the dark screen: lighting it")
            activity = getattr(self.face, "activity", None)
            if callable(activity):
                activity(SCREEN_TOUCH)
            return True
        if not self._waking_touch:
            return False
        if ev.type == pg.MOUSEBUTTONUP:
            self._waking_touch, self._woke_lift = False, time.monotonic()
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
        if self._press is not None:  # a still tap on the face (not a swipe or a hold)
            self._tap_lifted(lift[1])
        self._press = self._hold = None
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
        if button == 1:  # anywhere (face, card or game): held still 5 s, the settings
            self._hold = (nx, ny, time.monotonic())
        if button == 1 and (self._game_tap(nx, ny) or self._card_tap(nx, ny)):
            return
        if button == 1:
            self._press = (nx, ny, time.monotonic())  # a still tap on the face (a giggle; two: today at a glance)
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

    def _tap_lifted(self, at: float) -> None:
        """A tap on the face ended (the finger lifted where it pressed). Two in a row: today at a glance."""
        last, self._last_tap_up = getattr(self, "_last_tap_up", float("-inf")), at
        if at - last <= DOUBLE_TAP_SECONDS and not self.sim_mode and self.show_glance():
            self._last_tap_up = float("-inf")

    def show_glance(self) -> bool:
        """A double tap on the idle face: the day at a glance (littlebot/screens/cards/glance.py)."""
        screens = getattr(self.ctx, "screens", None)
        if screens is None or self._games() is not None or self._screens() is not None or self.face_hidden():
            return False
        log.info("double tap on the face: today at a glance")
        self._submit(lambda: screens.show({"card": "glance"}, source="touch"), "glance card")
        return True

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
