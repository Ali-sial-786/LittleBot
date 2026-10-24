"""``ctx.power``: the battery, as the robot sees it (see littlebot/power/__init__.py and docs/POWER.md).

Readings come from a source (``monitor.SerialMonitor`` thread, or ``monitor.SimulatedMonitor``) into an
inbox; a 1 s task on the runtime loop takes them in order (``process``): the gauge (percent, smoothing), the
time-left estimate, the charger's transitions and the policy's alarms, then what the robot does about them:

* **low**: ``power.low``; LittleBot says once, gently, that it is getting sleepy and needs charging (in a live
  voice chat the voice is given a quiet note instead; while asleep or busy it waits until it is idle again);
  the dashboard shows a banner.
* **critical** (never while plugged in): ``power.critical``; a live voice chat says goodbye and ends, everything
  stops (STOP ALL), LittleBot says goodbye, then the clean shutdown: ``LittleBot.request_poweroff`` (the app
  exits cleanly, then ``systemctl poweroff``) when ``power.allow_poweroff`` allows it; otherwise it goes to
  sleep and logs it loudly. Plugging the charger in before the end cancels it. The power-off is requested
  at most once.
* **hot** (50 °C): ``power.hot`` + a dashboard alert; **too hot** (60 °C): the same clean shutdown (even while
  charging: the pack must cool down). **cold**: charging below 0 °C: ``power.cold`` + a dashboard alert.
* **charging / charged / unplugged**: events, and the face shows its battery sign for a moment
  (``power.show``); it also shows on waking from a doze below ``show_on_wake_below_percent``.

With no monitor (mains power) nothing is published, said or shown: ``status()`` says "not fitted".
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import time
from typing import Any, Callable

from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.power.battery import BatteryGauge, TimeLeft
from littlebot.power.monitor import SerialMonitor, SimulatedMonitor
from littlebot.power.policy import PowerPolicy, Snapshot, Thresholds
from littlebot.power.protocol import Reading
from littlebot.power.shutdown import poweroff_allowed

log = logging.getLogger("littlebot.power")

TICK_S = 1.0
HEARTBEAT_S = 60.0  # power.battery at least this often while fitted
IMBALANCE_V = 0.15  # cells further apart than this: a health flag
SAY_TIMEOUT_S = 15.0
SOURCE = "power"

LOW_LINE = "I'm getting a little sleepy. Can a grown-up charge my battery, please?"
EMPTY_LINE = "My battery is empty, so I'm going to sleep now. Night night!"
HOT_LINE = "I'm a bit too warm, so I'm going to have a rest now. Bye-bye!"
NEARLY_EMPTY_LINE = "My battery is nearly empty. Please charge me soon!"
LOW_NOTE = ("LittleBot's battery is low ({percent}%). Mention it gently, in a few words, like a sleepy robot: you "
            "are getting sleepy and a grown-up should charge you soon. Then carry on.")
EMPTY_NOTE = ("LittleBot's battery is empty and LittleBot has to switch off in a moment. Say a very short, gentle "
              "goodbye now: you are going to sleep.")
NEARLY_EMPTY_NOTE = ("LittleBot's battery is nearly empty. Tell them gently, in a few words, that a grown-up should "
                     "charge you now. Then carry on.")
HOT_NOTE = ("LittleBot's battery is too warm and LittleBot has to switch off in a moment to cool down. Say a very "
            "short, gentle goodbye now: you are going to have a rest.")
VOICE_INSTRUCTIONS = """
You run on a battery. If someone asks how much battery you have, whether you need charging or how long you can keep \
going, delegate it to the client ("how much battery?") and answer with what comes back, in a few words."""


def _percent_validator(key: str, lo: int, hi: int) -> Callable[[Any], int]:
    def check(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{key}: expected a percentage")
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key}: expected a percentage") from e
        if not math.isfinite(v):
            raise ValueError(f"{key}: expected a percentage")
        return max(lo, min(hi, round(v)))
    return check


class PowerService(Service):
    """The battery: state, events and what LittleBot does when it runs low (see the module docstring)."""

    name = "power"

    def __init__(self, ctx: Any, *, source: Any = None, now: Callable[[], float] = time.monotonic,
                 poweroff: Callable[[str], Any] | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.power
        self._now = now
        self.source = source if source is not None else self._make_source()
        self.gauge = BatteryGauge()
        self.time_left = TimeLeft()
        self.policy = PowerPolicy(self._thresholds())
        self._poweroff_fn = poweroff
        self.poweroff_calls = 0  # how many times the power-off was requested (at most once)
        self._inbox: collections.deque[Any] = collections.deque(maxlen=600)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._jobs: set[asyncio.Task[Any]] = set()
        self._subs: list[Subscription] = []
        self.latest: Reading | None = None
        self.latest_at: float | None = None
        self.fitted = False  # a monitor was found since start
        self.connected = False
        self.port: str | None = None
        self._plugged: bool | None = None  # None until the first reading
        self._charging: bool | None = None
        self._charged: bool | None = None
        self._low_pending = False
        self._shutdown: asyncio.Task[Any] | None = None
        self.shutdown_reason: str | None = None
        self._published: tuple[Any, ...] | None = None
        self._published_at = -math.inf
        self._register_settings()

    # ------------------------------------------------------------------ setup
    def _make_source(self) -> Any:
        cfg = self.cfg
        if cfg.monitor == "none":
            return None
        if cfg.monitor == "simulated":
            return SimulatedMonitor(percent=cfg.simulated_percent, drain_per_hour=cfg.simulated_drain_per_hour)
        return SerialMonitor(port=cfg.port, globs=list(cfg.port_globs), match_any=cfg.probe_any_port,
                             scan_s=cfg.scan_seconds, probe_s=cfg.probe_seconds, stale_s=cfg.stale_seconds)

    def _register_settings(self) -> None:
        settings = getattr(self.ctx, "settings", None)
        if settings is None:
            return
        try:
            settings.register_validator("power.low_percent", _percent_validator("power.low_percent", 5, 50))
            settings.register_validator("power.critical_percent", _percent_validator("power.critical_percent", 2, 20))
        except Exception:  # noqa: BLE001
            log.debug("power settings validators not registered", exc_info=True)

    def _setting(self, key: str, default: Any) -> Any:
        try:
            value = self.ctx.settings.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _thresholds(self) -> Thresholds:
        c = self.cfg
        low = float(self._setting("power.low_percent", c.low_percent))
        critical = min(float(self._setting("power.critical_percent", c.critical_percent)), low - 1)
        return Thresholds(low_percent=low, critical_percent=critical, critical_cell_v=c.critical_cell_volts,
                          low_sustain_s=c.low_sustain_seconds, critical_sustain_s=c.critical_sustain_seconds,
                          hot_c=c.hot_celsius, shutdown_c=max(c.shutdown_celsius, c.hot_celsius),
                          hot_sustain_s=c.hot_sustain_seconds, cold_charge_c=c.cold_charge_celsius)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        bus = self.ctx.bus
        # harmless without a monitor: waking only shows the sign once a reading has arrived
        self._subs = [
            bus.subscribe(Topics.FACE_DOZE, self._on_doze),
            bus.subscribe(Topics.STATE_CHANGED, self._on_state),
        ]
        if self.source is None:
            self.set_status(ServiceStatus.DISABLED, "no battery (power.monitor: none)")
            return
        self.source.start(self._on_reading, self._on_source_status)
        self._task = self._loop.create_task(self._tick_loop(), name="power")
        self.set_status(ServiceStatus.RUNNING, "looking for the battery monitor" if self.source.kind == "serial"
                        else "simulated battery")

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        if self.source is not None:
            await asyncio.to_thread(self.source.stop)
        pending = [t for t in [self._task, *self._jobs] if t is not None and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._task = None
        self.set_status(ServiceStatus.STOPPED)

    # ------------------------------------------------------------------ source callbacks (any thread)
    def _on_reading(self, reading: Reading) -> None:
        self._inbox.append(("reading", reading))

    def _on_source_status(self, connected: bool, port: str | None) -> None:
        self._inbox.append(("status", (connected, port)))

    async def _tick_loop(self) -> None:
        while True:
            try:
                self.drain()
            except Exception:  # noqa: BLE001 - the battery watch must keep going
                log.exception("battery update failed")
            await asyncio.sleep(TICK_S)

    def drain(self) -> int:
        """Handle what the source sent since the last tick (on the loop). Returns how many readings."""
        n = 0
        while self._inbox:
            kind, item = self._inbox.popleft()
            if kind == "status":
                self.set_connected(*item)
            else:
                self.process(item)
                n += 1
        if self.fitted:
            self._try_announce_low()
            self._maybe_publish(self._now())
        return n

    # ------------------------------------------------------------------ state
    def set_connected(self, connected: bool, port: str | None = None) -> None:
        if connected == self.connected and (not connected or port == self.port):
            return
        self.connected = bool(connected)
        if connected:
            first = not self.fitted
            self.fitted, self.port = True, port
            if self.source is not None and getattr(self.source, "kind", "") == "serial":
                self.set_status(ServiceStatus.RUNNING, "battery monitor connected")
            if first:
                log.info("battery monitor found: running on the battery")
        else:
            self.set_status(ServiceStatus.DEGRADED, "battery monitor lost: looking for it again")
        self.ctx.bus.publish(Topics.POWER_MONITOR, {"connected": self.connected,
                                                     "kind": getattr(self.source, "kind", "none")}, source=SOURCE)
        self._maybe_publish(self._now(), force=True)

    def process(self, reading: Reading, now: float | None = None) -> list[str]:
        """Take one reading (on the loop): gauge, transitions, alarms and actions. Returns the alarms fired."""
        now = self._now() if now is None else now
        if not self.connected:
            self.set_connected(True, self.port or getattr(self.source, "kind", None))
        self.latest, self.latest_at = reading, now
        self.policy.limits = self._thresholds()
        pct = self.gauge.update(reading, now)
        self.time_left.add(now, pct, self.gauge.plugged_in)
        self._transitions(reading)
        weakest = min(reading.cells) if reading.cells else (
            reading.voltage / self.gauge.cells if reading.voltage is not None else None)
        snap = Snapshot(percent=pct, min_cell=weakest, temperature_c=reading.temperature_c,
                        charging=self.gauge.charging, plugged_in=self.gauge.plugged_in)
        fired = self.policy.update(snap, now)
        for alarm in fired:
            self._alarm(alarm)
        self._maybe_publish(now, force=bool(fired))
        return fired

    @property
    def percent(self) -> int | None:
        p = self.gauge.percent
        return None if p is None else int(round(p))

    @property
    def plugged_in(self) -> bool:
        return self.gauge.plugged_in

    def _transitions(self, reading: Reading) -> None:
        charging, charged = reading.charging is True, reading.charged is True
        plugged = charging or charged
        first = self._plugged is None
        before = (self._plugged, self._charging, self._charged)
        self._plugged, self._charging, self._charged = plugged, charging, charged
        if first:
            return
        was_plugged, was_charging, was_charged = before
        data = {"percent": self.percent}
        if charging and not was_charging:
            log.info("battery: charging (%s%%)", self.percent)
            self.ctx.bus.publish(Topics.POWER_CHARGING, data, source=SOURCE)
            self._low_pending = False
            if not was_plugged:
                self.show()
        if charged and not was_charged:
            log.info("battery: charged")
            self.ctx.bus.publish(Topics.POWER_CHARGED, data, source=SOURCE)
            if not was_plugged:
                self.show()
        if was_plugged and not plugged:
            log.info("battery: charger unplugged (%s%%)", self.percent)
            self.ctx.bus.publish(Topics.POWER_UNPLUGGED, data, source=SOURCE)
        if plugged and self._shutdown is not None and not self._shutdown.done() \
                and self.shutdown_reason == "battery empty":
            log.warning("battery: charger plugged in, the shutdown is cancelled")

    # ------------------------------------------------------------------ alarms
    def _alarm(self, alarm: str) -> None:
        bus = self.ctx.bus
        t = self.latest.temperature_c if self.latest is not None else None
        if alarm == "low":
            log.warning("battery low: %s%%", self.percent)
            bus.publish(Topics.POWER_LOW, {"percent": self.percent}, source=SOURCE)
            self._low_pending = True
            self._try_announce_low()
        elif alarm == "critical":
            reason = self.policy.critical_reason
            log.critical("BATTERY EMPTY (%s): saying goodbye and shutting down", reason)
            bus.publish(Topics.POWER_CRITICAL, {"percent": self.percent, "reason": reason}, source=SOURCE)
            if self._setting("power.shutdown_when_empty", True) is False:
                log.critical("battery empty, but shutting down is switched off (power.shutdown_when_empty)")
                if not (getattr(self.ctx, "state", None) is not None and self.ctx.state.is_(RobotState.SLEEPING)):
                    self._spawn(self._say(NEARLY_EMPTY_LINE, NEARLY_EMPTY_NOTE, goodbye=False))
                return
            self.begin_shutdown("battery empty")
        elif alarm == "hot":
            log.warning("battery hot: %.1f C", t if t is not None else float("nan"))
            bus.publish(Topics.POWER_HOT, {"temperature_c": t, "level": "warm"}, source=SOURCE)
        elif alarm == "too_hot":
            log.critical("BATTERY TOO HOT (%.1f C): shutting down to cool down", t if t is not None else float("nan"))
            bus.publish(Topics.POWER_HOT, {"temperature_c": t, "level": "shutdown"}, source=SOURCE)
            self.begin_shutdown("battery too hot")
        elif alarm == "cold":
            log.warning("battery charging below %.0f C (%.1f C)", self.cfg.cold_charge_celsius,
                        t if t is not None else float("nan"))
            bus.publish(Topics.POWER_COLD, {"temperature_c": t}, source=SOURCE)

    def _try_announce_low(self) -> None:
        """Say "I'm getting sleepy" once, when it fits: in a chat as a note, when idle out loud, never asleep."""
        if not self._low_pending:
            return
        if self.plugged_in or not self.policy.low or (self._shutdown is not None and not self._shutdown.done()):
            self._low_pending = False
            return
        if self._live_active():
            self._low_pending = False
            self._note(LOW_NOTE.format(percent=self.percent), goodbye=False)
            return
        if self._setting("power.announce_low", True) is False:
            self._low_pending = False
            return
        state = getattr(self.ctx, "state", None)
        if state is None or not state.is_(RobotState.IDLE):
            return  # asleep (it would wake the baby) or busy: later
        self._low_pending = False
        self.show()
        self._spawn(self._say(LOW_LINE, None, goodbye=False, face="sleepy"))

    # ------------------------------------------------------------------ the clean shutdown
    def begin_shutdown(self, reason: str) -> bool:
        """Start the goodbye + shutdown sequence (once at a time). False when one is already running."""
        if self._shutdown is not None and not self._shutdown.done():
            return False
        if self.poweroff_calls:
            return False
        self.shutdown_reason = reason
        task = self._spawn(self._shutdown_sequence(reason))
        self._shutdown = task
        return task is not None

    def _cancelled_by_charger(self, reason: str) -> bool:
        if reason == "battery empty" and self.plugged_in:
            log.warning("battery: the charger is plugged in, not shutting down")
            self.ctx.bus.publish(Topics.POWER_SHUTDOWN, {"reason": reason, "poweroff": False, "cancelled": True},
                                 source=SOURCE)
            return True
        return False

    async def _shutdown_sequence(self, reason: str) -> None:
        ctx = self.ctx
        hot = reason == "battery too hot"
        allowed = poweroff_allowed(ctx.config)
        ctx.bus.publish(Topics.POWER_SHUTDOWN, {"reason": reason, "poweroff": allowed}, source=SOURCE)
        # 1. a live voice chat says goodbye and ends
        if self._live_active():
            self._note(HOT_NOTE if hot else EMPTY_NOTE, goodbye=True)
            wait = float(self.cfg.goodbye_wait_seconds)
            if wait > 0:
                await ctx.bus.wait_for("live.call_ended", timeout=wait)
            live = getattr(ctx, "live", None)
            if live is not None and self._live_active():
                try:
                    await asyncio.wait_for(live.stop_session(reason), 10.0)
                except Exception:  # noqa: BLE001
                    log.exception("could not end the live voice chat")
        if self._cancelled_by_charger(reason):
            return
        # 2. stop videos, activities, games, speech and motion
        asleep = bool(getattr(ctx, "state", None) is not None and ctx.state.is_(RobotState.SLEEPING))
        await self._stop_everything(reason)
        # 3. goodbye (not out loud at night: it would wake the baby)
        face = getattr(ctx, "face", None)
        if face is not None:
            try:
                face.set_expression("sleepy", hold=8.0)
            except Exception:  # noqa: BLE001
                log.debug("face unavailable", exc_info=True)
        if not asleep:
            await self._say_now(HOT_LINE if hot else EMPTY_LINE)
        if self._cancelled_by_charger(reason):
            return
        # 4. switch off (or, where that isn't allowed, sleep and say so loudly in the log)
        if not allowed:
            log.critical("%s: LittleBot would switch the Raspberry Pi off now, but power.allow_poweroff doesn't allow "
                         "it here (the desktop simulation); going to sleep instead", reason)
            if getattr(ctx, "state", None) is not None:
                ctx.state.transition(RobotState.SLEEPING, reason, force=True)
            return
        self._poweroff(reason)

    def _poweroff(self, reason: str) -> None:
        if self.poweroff_calls:
            return
        self.poweroff_calls += 1
        log.critical("%s: clean shutdown and power-off", reason)
        fn = self._poweroff_fn
        if fn is not None:
            fn(reason)
            return
        robot = self.ctx.extras.get("robot") if isinstance(getattr(self.ctx, "extras", None), dict) else None
        request = getattr(robot, "request_poweroff", None)
        if callable(request):
            request(reason)  # the app exits cleanly, then littlebot.app.main switches the Pi off
            return
        # Never switch the machine off from here: only after the app has stopped cleanly (littlebot.app.main).
        log.critical("%s: no robot to shut down (a bare context); not powering off", reason)

    async def _stop_everything(self, reason: str) -> None:
        ctx = self.ctx
        robot = ctx.extras.get("robot") if isinstance(getattr(ctx, "extras", None), dict) else None
        try:
            if robot is not None and callable(getattr(robot, "stop_all", None)):
                await asyncio.wait_for(robot.stop_all(SOURCE), 15.0)
                return
            ctx.bus.publish(Topics.STOP_ALL, {"source": SOURCE}, source=SOURCE)
            for name, call in (("media", "stop"), ("activities", "stop")):
                svc = getattr(ctx, name, None)
                if svc is not None:
                    await asyncio.wait_for(getattr(svc, call)(reason), 10.0)
        except Exception:  # noqa: BLE001 - shutting down must go on
            log.exception("stopping everything before the shutdown failed")

    # ------------------------------------------------------------------ speech / notes / the face
    def _live_active(self) -> bool:
        live = getattr(self.ctx, "live", None)
        if live is None:
            return False
        try:
            return bool(live.status().get("active"))
        except Exception:  # noqa: BLE001
            return False

    def _note(self, text: str, *, goodbye: bool) -> None:
        """A quiet note for the live voice chat (``chatgpt.live`` turns it into a note, like ``screen.news``)."""
        self.ctx.bus.publish(Topics.POWER_NOTE, {"text": text, "goodbye": goodbye}, source=SOURCE)

    async def _say(self, line: str, note: str | None, *, goodbye: bool, face: str | None = None) -> None:
        if note is not None and self._live_active():
            self._note(note, goodbye=goodbye)
            return
        if face is not None and getattr(self.ctx, "face", None) is not None:
            try:
                self.ctx.face.set_expression(face, hold=4.0)
            except Exception:  # noqa: BLE001
                log.debug("face unavailable", exc_info=True)
        await self._say_now(line)

    async def _say_now(self, line: str) -> None:
        audio = getattr(self.ctx, "audio", None)
        if audio is None:
            return
        try:
            await asyncio.wait_for(audio.say(line), SAY_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            log.warning("battery: couldn't say it", exc_info=True)

    def show(self, seconds: float | None = None) -> bool:
        """The face shows its battery sign (level, and a bolt while charging) for a moment. False when not fitted."""
        if not self.fitted:
            return False
        self.ctx.bus.publish(Topics.POWER_SHOW, {"percent": self.percent, "charging": self.gauge.charging,
                                                  "charged": self.gauge.charged, "plugged_in": self.plugged_in,
                                                  "seconds": float(seconds or self.cfg.show_seconds)}, source=SOURCE)
        return True

    def _on_doze(self, event: Event) -> None:
        if event.get("dozing") is False:
            self._woke()

    def _on_state(self, event: Event) -> None:
        if event.get("old") == RobotState.SLEEPING.value and event.get("new") == RobotState.IDLE.value:
            self._woke()

    def _woke(self) -> None:
        pct = self.percent
        if self.fitted and self.connected and pct is not None and not self.plugged_in \
                and pct < self.cfg.show_on_wake_below_percent:
            self.show()

    # ------------------------------------------------------------------ publishing / views
    def _maybe_publish(self, now: float, *, force: bool = False) -> None:
        if not self.fitted:
            return
        st = self.state()
        temp = st["temperature_c"]
        key = (st["connected"], st["charging"], st["charged"], tuple(st["health"]))
        detail = (st["percent"], None if temp is None else round(temp))
        since = now - self._published_at
        changed = self._published is None or key != self._published[0]
        if not (force or changed or (detail != self._published[1] and since >= self.cfg.publish_seconds)
                or since >= HEARTBEAT_S):
            return
        self._published, self._published_at = (key, detail), now
        self.ctx.bus.publish(Topics.POWER_BATTERY, st, source=SOURCE)

    def health(self) -> dict[str, Any]:  # type: ignore[override]
        h = super().health()
        h["fitted"] = self.fitted
        return h

    def state(self) -> dict[str, Any]:
        """The battery now (the ``power.battery`` payload)."""
        r = self.latest
        g = self.gauge
        if not self.fitted or r is None:
            return {"fitted": self.fitted, "connected": self.connected, "percent": None, "voltage": None,
                    "cells": None, "temperature_c": None, "charging": False, "charged": False, "plugged_in": False,
                    "time_left_minutes": None, "time_to_full_minutes": None, "health": self._health_flags()}
        cells = [round(c, 2) for c in r.cells] if r.cells else None
        left = self.time_left.minutes(g.percent)
        return {
            "fitted": True,
            "connected": self.connected,
            "percent": self.percent,
            "voltage": None if g.voltage is None else round(g.voltage, 2),
            "cells": cells,
            "temperature_c": None if r.temperature_c is None else round(r.temperature_c, 1),
            "charging": g.charging,
            "charged": g.charged,
            "plugged_in": g.plugged_in,
            "time_left_minutes": None if g.plugged_in else left,
            "time_to_full_minutes": left if g.charging else None,
            "health": self._health_flags(),
        }

    def _health_flags(self) -> list[str]:
        flags = list(self.policy.flags())
        r = self.latest
        if self.fitted and not self.connected:
            flags.append("monitor_lost")
        if r is not None and self.connected:
            if r.voltage is None:
                flags.append("voltage_unknown")
            if r.temperature_c is None:
                flags.append("temperature_unknown")
            if len(r.cells) == 2 and abs(r.cells[0] - r.cells[1]) > IMBALANCE_V:
                flags.append("cell_imbalance")
        if self._shutdown is not None and not self._shutdown.done():
            flags.append("shutting_down")
        return flags

    def status(self) -> dict[str, Any]:
        """For the dashboard and ``robot.status()``: the state plus the monitor and the parent's settings."""
        out = self.state()
        out["status"] = self.service_status.value
        out["detail"] = "not fitted" if not self.fitted else ("monitor lost" if not self.connected else "ok")
        out["kind"] = getattr(self.source, "kind", "none")
        out["monitor"] = _safe(self.source.status) if self.source is not None else {"kind": "none"}
        out["age_s"] = None if self.latest_at is None else round(max(0.0, self._now() - self.latest_at), 1)
        out["settings"] = {
            "low_percent": int(self._setting("power.low_percent", self.cfg.low_percent)),
            "critical_percent": int(self._setting("power.critical_percent", self.cfg.critical_percent)),
            "announce_low": bool(self._setting("power.announce_low", True)),
            "shutdown_when_empty": bool(self._setting("power.shutdown_when_empty", True)),
            "poweroff_allowed": poweroff_allowed(self.ctx.config),
        }
        out["limits"] = {"hot_c": self.cfg.hot_celsius, "shutdown_c": self.cfg.shutdown_celsius,
                         "critical_cell_v": self.cfg.critical_cell_volts}
        return out

    def describe(self) -> dict[str, Any] | None:
        """A short view for the voice's tools (None when no battery is fitted)."""
        if not self.fitted:
            return None
        st = self.state()
        out: dict[str, Any] = {"percent": st["percent"], "charging": st["charging"], "full": st["charged"],
                               "plugged_in": st["plugged_in"]}
        if not self.connected:
            out["note"] = "the battery monitor isn't answering right now; this is the last reading"
        if st["time_left_minutes"] is not None:
            out["about_minutes_left"] = st["time_left_minutes"]
        if st["time_to_full_minutes"] is not None:
            out["about_minutes_to_full"] = st["time_to_full_minutes"]
        if st["temperature_c"] is not None:
            out["battery_temperature_c"] = st["temperature_c"]
        warnings = [f for f in st["health"] if f in ("low", "critical", "hot", "too_hot", "cold", "cell_imbalance")]
        if warnings:
            out["warnings"] = warnings
        return out

    def simulate(self, *, percent: float | None = None, charging: bool | None = None,
                 temperature_c: Any = "keep") -> bool:
        """Set the simulated pack (dashboard simulator); False when the battery isn't simulated."""
        src = self.source
        if not isinstance(src, SimulatedMonitor):
            return False
        src.set(percent=percent, charging=charging, temperature_c=temperature_c)
        if percent is not None:
            # a jump the parent asked for, not a reading to smooth: drop readings of the old level still waiting
            self._inbox = collections.deque((i for i in self._inbox if i[0] != "reading"), maxlen=600)
            self.gauge.reset()
            self.time_left.clear()
        src.emit()
        self.drain()
        return True

    # ------------------------------------------------------------------ helpers
    def _spawn(self, coro: Any) -> asyncio.Task[Any] | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
            if loop is None or loop.is_closed():
                coro.close()
                return None
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut  # type: ignore[return-value]
        task = loop.create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._job_done)
        return task

    def _job_done(self, task: asyncio.Task[Any]) -> None:
        self._jobs.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("battery job failed", exc_info=task.exception())

    async def idle(self) -> None:
        """Wait for the jobs started so far (tests)."""
        while self._jobs:
            await asyncio.gather(*list(self._jobs), return_exceptions=True)


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None
