"""Composition root: builds every subsystem, wires them together, runs the lifecycle.

Subsystem packages expose ``create_service(ctx)``; this module creates them in
dependency order and stores them on the shared ``RobotContext``. A subsystem
that fails to build is logged and left as ``None`` (the robot keeps running
in a degraded mode) - except the face, which is required.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any, Callable

from littlebot import __version__
from littlebot.core import memory
from littlebot.core.config import LittleBotConfig
from littlebot.core.context import RobotContext, build_core_context
from littlebot.core.events import Topics
from littlebot.core.runtime import AsyncRuntime
from littlebot.core.service import Service
from littlebot.core.state import RobotState

log = logging.getLogger("littlebot.robot")

# Exit code asking the service manager to start LittleBot again (EX_TEMPFAIL).
RESTART_EXIT_CODE = 75

# (context attribute, module path) in start order. Later ones may use earlier ones.
SUBSYSTEMS: tuple[tuple[str, str], ...] = (
    ("face", "littlebot.face"),
    ("audio", "littlebot.audio"),
    ("vision", "littlebot.vision"),
    ("motion", "littlebot.motion"),
    ("media", "littlebot.media"),
    ("brain", "littlebot.brain"),
    ("activities", "littlebot.activities"),
    ("games", "littlebot.games"),  # on-screen games: draws over the face, uses audio + activities
    ("screens", "littlebot.screens"),  # info cards over the face while the voice talks (a game wins)
    ("voices", "littlebot.voices"),  # the family's voices (opt-in): before live voice, which uses them
    ("browser", "littlebot.browser"),  # Google on the screen when a grown-up asks (one Chromium with the videos)
    ("skills", "littlebot.skills"),  # tools for both voices (memory, reminders, lists, games ...): before them
    ("live", "littlebot.chatgpt"),
    ("assistant", "littlebot.assistant"),  # the fast voice pipeline (GPT-Live stays the fallback)
    ("wake", "littlebot.audio.wakeword"),  # after audio + live: it shares the mic and starts live voice
    ("power", "littlebot.power"),  # the battery monitor (quiet, "not fitted", on mains power); uses the rest when empty
)


class LittleBot:
    """The whole robot. Thread-safe facade used by the dashboard and the display loop."""

    def __init__(self, ctx: RobotContext, *, runtime: AsyncRuntime | None = None) -> None:
        self.ctx = ctx
        self.runtime = runtime or ctx.runtime or AsyncRuntime()
        ctx.runtime = self.runtime
        ctx.extras["robot"] = self
        self.services: list[tuple[str, Any]] = []
        self.dashboard: Any = None
        self.behaviours: Any = None
        self.shutdown_event = threading.Event()
        self.restart_requested = False
        self.poweroff_requested = False  # the battery is empty / too hot: switch the Pi off after the clean exit
        self._started = False
        self._stopped = False
        self.started_at = time.time()
        self.stall_watch: Any = None

    # ------------------------------------------------------------------ build
    @classmethod
    def create(cls, config: LittleBotConfig, *, db_path: str | None = None) -> "LittleBot":
        ctx = build_core_context(config, db_path=db_path)
        bot = cls(ctx)
        bot.build_subsystems()
        return bot

    def build_subsystems(self, only: set[str] | None = None) -> None:
        for attr, module_path in SUBSYSTEMS:
            if only is not None and attr not in only:
                continue
            if getattr(self.ctx, attr) is not None:  # pre-injected (tests)
                self.services.append((attr, getattr(self.ctx, attr)))
                continue
            factory = _load_factory(module_path)
            if factory is None:
                if attr == "face":
                    raise RuntimeError("face subsystem is required but could not be loaded")
                continue
            try:
                svc = factory(self.ctx)
            except Exception:  # noqa: BLE001 - degrade instead of crashing
                if attr == "face":
                    raise
                log.exception("failed to build %s subsystem; continuing without it", attr)
                self.ctx.bus.publish(Topics.SYSTEM_ERROR, {"module": attr, "error": "build failed", "recoverable": True})
                continue
            setattr(self.ctx, attr, svc)
            self.services.append((attr, svc))
        # Glue behaviours (state -> face, idle -> sleep, vision -> gaze ...)
        try:
            from littlebot.app.behaviours import Behaviours

            self.behaviours = Behaviours(self.ctx)
        except Exception:  # noqa: BLE001
            log.exception("behaviour glue unavailable")
        if self.ctx.config.dashboard.enabled and (only is None or "dashboard" in only):
            factory = _load_factory("littlebot.dashboard")
            if factory is not None:
                try:
                    self.dashboard = factory(self.ctx)
                except Exception:  # noqa: BLE001
                    log.exception("failed to build dashboard; continuing without it")

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._started:
            return
        self.runtime.start()
        try:
            from littlebot.core.stall_watch import StallWatch

            self.stall_watch = StallWatch(self.runtime)  # logs when the robot stops responding, and why
            self.stall_watch.start()
        except Exception:  # noqa: BLE001 - only a diagnostic
            log.exception("stall watch unavailable")
        self.runtime.run(self._start_async(), timeout=60)
        self._started = True
        self.ctx.bus.publish(Topics.SYSTEM_STARTED, {"mode": self.ctx.config.mode.value, "version": __version__})
        log.info("LittleBot %s started in %s mode", __version__, self.ctx.config.mode.value)
        ram = memory.total_mb()
        log.info("memory: %s RAM, %s (system.memory: %s)", f"{ram / 1024:.1f} GB" if ram else "unknown",
                 "keeping more loaded" if memory.roomy_for(self.ctx) else "saving memory",
                 self.ctx.config.system.memory)

    async def _start_async(self) -> None:
        targets = [(n, s) for n, s in self.services] + ([("dashboard", self.dashboard)] if self.dashboard else [])
        if self.behaviours is not None:
            targets.append(("behaviours", self.behaviours))
        for name, svc in targets:
            start = getattr(svc, "start", None)
            if start is None:
                continue
            try:
                await start()
            except Exception:  # noqa: BLE001
                log.exception("%s failed to start; continuing degraded", name)
                if isinstance(svc, Service):
                    from littlebot.core.service import ServiceStatus

                    svc.set_status(ServiceStatus.FAILED, "start failed")

    def shutdown(self, reason: str = "shutdown") -> None:
        if self._stopped:
            return
        self._stopped = True
        log.info("shutting down (%s)", reason)
        self.ctx.bus.publish(Topics.SYSTEM_SHUTDOWN, {"reason": reason})
        self.shutdown_event.set()
        if self.stall_watch is not None:
            self.stall_watch.stop()
        if self.runtime.running:
            try:
                self.runtime.run(self._stop_async(), timeout=15)
            except Exception:  # noqa: BLE001
                log.exception("error while stopping services")
            self.runtime.stop()
        self.ctx.db.close()
        log.info("shutdown complete")

    async def _stop_async(self) -> None:
        # Make the robot physically safe first.
        if self.ctx.motion is not None:
            try:
                self.ctx.motion.emergency_stop("shutdown")
            except Exception:  # noqa: BLE001
                log.exception("motion stop failed")
        targets: list[tuple[str, Any]] = []
        if self.behaviours is not None:
            targets.append(("behaviours", self.behaviours))
        if self.dashboard is not None:
            targets.append(("dashboard", self.dashboard))
        targets += list(reversed(self.services))
        for name, svc in targets:
            stop = getattr(svc, "stop", None)
            if stop is None:
                continue
            try:
                import asyncio

                await asyncio.wait_for(stop(), timeout=5)
            except Exception:  # noqa: BLE001
                log.exception("%s failed to stop cleanly", name)

    def request_shutdown(self, reason: str = "requested", *, restart: bool = False) -> None:
        """Ask the main loop to exit (safe from any thread, including the dashboard).

        ``restart=True`` makes the process exit with RESTART_EXIT_CODE so a service manager
        (systemd ``Restart=on-failure``) starts it again; a plain shutdown exits 0 and stays down.
        """
        log.info("%s requested: %s", "restart" if restart else "shutdown", reason)
        self.restart_requested = self.restart_requested or restart
        self.shutdown_event.set()

    def request_poweroff(self, reason: str) -> None:
        """Exit cleanly, then switch the Raspberry Pi off (``littlebot.app.main`` runs the power-off once every
        subsystem has stopped and the database is closed). Used by the battery (littlebot/power) only when
        ``power.allow_poweroff`` allows it; safe from any thread."""
        log.critical("power-off requested: %s", reason)
        self.poweroff_requested = True
        self.restart_requested = False
        self.request_shutdown(reason)

    # ------------------------------------------------------------ parent ops
    async def stop_all(self, source: str = "parent") -> dict[str, Any]:
        """The big STOP button: halt activity, media, speech, listening and motion."""
        ctx = self.ctx
        ctx.bus.publish(Topics.STOP_ALL, {"source": source})
        if ctx.motion is not None:
            ctx.motion.cancel()
        if ctx.audio is not None:
            ctx.audio.stop_output()
        if ctx.activities is not None:
            await _safe(ctx.activities.stop("parent stop"))
        if ctx.media is not None:
            await _safe(ctx.media.stop("stopped"))
        ctx.arbiter.preempt_all(by=source)
        ctx.state.transition(RobotState.IDLE, f"stop all ({source})", force=True)
        if ctx.face is not None:
            ctx.face.open_eyes()
            ctx.face.set_expression("neutral")
        return self.status()

    def sleep(self, source: str = "parent") -> bool:
        ok = self.ctx.state.transition(RobotState.SLEEPING, f"sleep ({source})")
        return ok

    def wake(self, source: str = "parent") -> bool:
        if self.ctx.state.state in (RobotState.SLEEPING, RobotState.ERROR):
            return self.ctx.state.transition(RobotState.IDLE, f"wake ({source})")
        return True

    def status(self) -> dict[str, Any]:
        ctx = self.ctx
        out: dict[str, Any] = {
            "name": ctx.config.robot_name,
            "version": __version__,
            "mode": ctx.config.mode.value,
            "state": ctx.state.snapshot(),
            "leases": ctx.arbiter.snapshot(),
            "privacy": ctx.privacy.snapshot(),
            "system": _safe_status(ctx.monitor),
            "age_months": ctx.settings.child_age_months(),
            "age_group": ctx.age_policy().group,
        }
        for attr, _ in SUBSYSTEMS:
            svc = getattr(ctx, attr)
            out[attr] = _safe_status(svc) if svc is not None else {"status": "unavailable"}
        out["services"] = {name: (svc.health() if isinstance(svc, Service) else {"name": name, "status": "running"})
                           for name, svc in self.services}
        return out


def _load_factory(module_path: str) -> Callable[[RobotContext], Any] | None:
    try:
        module = importlib.import_module(module_path)
    except Exception:  # noqa: BLE001
        log.exception("could not import %s", module_path)
        return None
    factory = getattr(module, "create_service", None)
    if factory is None:
        log.warning("%s has no create_service(); subsystem not available yet", module_path)
    return factory


def _safe_status(svc: Any) -> dict[str, Any]:
    try:
        return svc.status()
    except Exception as e:  # noqa: BLE001
        log.debug("status() failed for %r", svc, exc_info=True)
        return {"status": "error", "detail": str(e)}


async def _safe(coro: Any) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        log.exception("stop step failed")
