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

import collections
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

log = logging.getLogger("littlebot.face.display")

# Cause-and-effect for a baby: tapping the face (on the robot's touch screen,
# i.e. when not in simulator mode) makes LittleBot giggle and bounce.
TAP_REACTION_ENABLED = True
TAP_REACTION_SOUND = "giggle"
TAP_COOLDOWN_SECONDS = 1.5

PANEL_WIDTH = 300
PANEL_MIN_HEIGHT = 460
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

    # ------------------------------------------------------------------ setup
    def open(self) -> None:
        import pygame

        self._pg = pygame
        pygame.display.init()
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
        self._opened = True
        log.info("face display open: %dx%d%s (driver %s)", *self.window.get_size(),
                 " fullscreen" if self.fullscreen else "", pygame.display.get_driver())

    def close(self) -> None:
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
        self.face.render(win, self.face_rect)
        if self.show_panel and self.panel is not None and self.panel_rect is not None:
            self._poll(now)
            caption = f"demo: {self.demo.caption}" if self.demo is not None and self.demo.caption else ""
            self.panel.draw(win, self.panel_rect, self._status, self._motion, fps=self.measured_fps, caption=caption)
        elif self.demo is not None and self.show_debug:
            self._text(win, f"demo: {self.demo.caption}", (self.face_rect.x + 8, self.face_rect.bottom - 24), 20)
        if self.show_debug:
            self._draw_debug(win)
        if self.show_help:
            self._draw_help(win)

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
                self.handle_click(ev.pos, ev.button, getattr(ev, "mod", None))
        return True

    def handle_key(self, key: int, mod: int = 0) -> bool:
        """Apply one key press. Returns False when the user asked to quit."""
        pg = self._pg
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
        if self.sim_mode and not shift:
            if vision is not None:
                vision.simulate_face(nx, ny)
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
