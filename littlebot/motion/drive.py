"""Drive base for the wheels phase: a protocol, a simulated base and a placeholder motor-HAT driver.

Nothing calls this yet (no tool, no activity): it is ready for when wheels are fitted. See
docs/MOVEMENT.md ("Wheels") for how to wire it to a tool later.

Layers (like the servos: planning + safety on top of a dumb output stage)
-------------------------------------------------------------------------
* ``DriveBase``    - what callers use: ``drive(linear_mps, angular_dps, duration_s)``,
                     ``turn(degrees)``, ``forward(meters)``, ``stop()``, ``status()``.
* ``Drive``        - the implementation: safety gate, speed / acceleration limits, ramps,
                     watchdog, e-stop latch, a dead-reckoned pose (x, y, heading) and
                     ``motion.drive`` events. One asyncio control task runs only while moving.
* ``WheelDriver``  - the output stage: ``set_velocity(linear_mps, angular_dps)``, ``stop()``,
                     ``close()``, ``describe()``, ``physical``. ``SimulatedWheels`` does no I/O;
                     ``MotorHatWheels`` is the placeholder for the real motor HAT (TODO list in it).
* ``MockDrive``    - ``Drive`` on ``SimulatedWheels``: the pose is simulated.

Safety rules
------------
* Every command is refused unless ``motion.drive.enabled`` is true, no e-stop is latched and the
  caller's ``gate`` (the motion controller: e-stop, service stopped) has no objection. Physical
  wheels also need ``motion.physical_enabled`` and a parent's arming (``motion.physical_armed``),
  exactly like the servos; the simulated base needs no arming (nothing can move).
* Speeds are clamped to ``max_linear_mps`` / ``max_angular_dps`` (and hard ceilings in code);
  speed changes ramp within ``max_*_accel`` (no jerks, no tipping).
* Watchdog: an open-ended ``drive()`` (``duration_s=None``) stops by itself unless it is re-sent
  within ``watchdog_seconds`` (1 s). No command runs longer than ``max_duration_seconds``. A control
  loop that stalls longer than the watchdog stops the wheels instead of carrying on.
* ``emergency_stop()`` cuts the wheels at once in the caller's thread and latches until
  ``reset_emergency_stop()``; the motion controller's e-stop calls it. ``stop()`` brakes within
  the acceleration limits (parent STOP).

Return values: the coroutines return True when the command ran to its end (duration elapsed,
distance / angle reached, or an open-ended command was replaced by a newer one) and False when it
was refused, stopped, stopped by the watchdog or the e-stop.

Pose convention: metres and degrees from where the base started (or ``reset_pose()``); heading 0 =
the initial forward direction, positive angles turn LEFT (counter-clockwise seen from above); x is
forward, y to the left.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from littlebot.core.clock import Clock
from littlebot.core.config import DriveConfig

log = logging.getLogger("littlebot.motion.drive")

DRIVE_TOPIC = "motion.drive"  # {x, y, heading, linear, angular, moving, command, enabled, estop, reason}
HARD_MAX_LINEAR_MPS = 0.5  # no configuration can make the base faster than this
HARD_MAX_ANGULAR_DPS = 180.0
HARD_MAX_LINEAR_ACCEL = 1.0
HARD_MAX_ANGULAR_ACCEL = 720.0
MIN_WATCHDOG_S, MAX_WATCHDOG_S = 0.1, 5.0
DISTANCE_EPS_M = 1e-4
ANGLE_EPS_DEG = 0.05
SPEED_EPS = 1e-6

Gate = Callable[[], "str | None"]


class DriveError(RuntimeError):
    """The wheel driver could not be opened or written."""


# --------------------------------------------------------------------------- protocols
@runtime_checkable
class DriveBase(Protocol):
    async def drive(self, linear_mps: float, angular_dps: float, duration_s: float | None = None) -> bool:
        """Move at this body velocity (m/s forward, deg/s turning left) for ``duration_s`` seconds, or until
        replaced / stopped / the watchdog fires when ``duration_s`` is None."""

    async def turn(self, degrees: float) -> bool:
        """Turn on the spot (+ = left) by this many degrees."""

    async def forward(self, meters: float) -> bool:
        """Drive straight (negative = backwards) this far."""

    def stop(self) -> None:
        """Brake to a halt within the acceleration limits (safe from any thread)."""

    def status(self) -> dict[str, Any]:
        """{enabled, backend, physical, moving, estop, pose: {x, y, heading}, linear, angular, command, reason,
        limits: {...}, error}"""


@runtime_checkable
class WheelDriver(Protocol):
    """Output stage: turns a body velocity into wheel commands. ``physical`` is True for real motors."""

    physical: bool

    def set_velocity(self, linear_mps: float, angular_dps: float) -> None: ...

    def stop(self) -> None:
        """Wheels stopped NOW (brake or coast); must never raise for a closed driver."""

    def close(self) -> None: ...

    def describe(self) -> dict[str, Any]: ...


def wheel_speeds(linear_mps: float, angular_dps: float, wheel_base_m: float) -> tuple[float, float]:
    """Differential-drive kinematics: body velocity -> (left, right) wheel surface speeds in m/s."""
    w = math.radians(float(angular_dps))
    half = float(wheel_base_m) / 2.0
    return float(linear_mps) - w * half, float(linear_mps) + w * half


class SimulatedWheels:
    """No I/O: remembers the last command (the simulator / tests)."""

    physical = False

    def __init__(self, wheel_base_m: float = 0.12) -> None:
        self.wheel_base_m = float(wheel_base_m)
        self.linear = 0.0
        self.angular = 0.0
        self.writes = 0
        self.stops = 0
        self.closed = False

    def set_velocity(self, linear_mps: float, angular_dps: float) -> None:
        if self.closed:
            return
        self.linear, self.angular = float(linear_mps), float(angular_dps)
        self.writes += 1

    def stop(self) -> None:
        self.linear = self.angular = 0.0
        self.stops += 1

    def close(self) -> None:
        self.stop()
        self.closed = True

    def describe(self) -> dict[str, Any]:
        left, right = wheel_speeds(self.linear, self.angular, self.wheel_base_m)
        return {"driver": "simulated wheels", "left_mps": round(left, 3), "right_mps": round(right, 3)}


class MotorHatWheels:
    """PLACEHOLDER for the future motor HAT (two DC gear motors, differential drive). Not written yet.

    TODO when the HAT is chosen (keep this interface, ``Drive`` needs nothing else):

    * ``__init__(cfg)``: open the HAT (I2C address / GPIO pins from new ``motion.drive`` keys), verify it
      answers (raise ``DriveError`` otherwise: ``Drive`` then falls back to the simulation), motors stopped.
      Import its library lazily, here, never at module import.
    * ``set_velocity(linear_mps, angular_dps)``: ``wheel_speeds(...)`` -> per-wheel speed -> PWM duty and
      direction (calibrated: duty for a known speed, minimum duty that moves, per-wheel trim, inverted motors).
      Clamp duty to 0..max. Called every control tick (``update_hz``) while moving.
    * keep-alive: stop the motors by itself if ``set_velocity`` is not called for ``watchdog_seconds``
      (the HAT's own watchdog if it has one, else ``CommandWatchdog`` below on a tiny thread), so a hung
      process can never leave the wheels turning.
    * ``stop()``: brake (both inputs high) or coast at once; idempotent; safe from any thread; never raises.
    * ``close()``: ``stop()`` then release the bus / pins.
    * ``describe()``: {"driver": ..., "address": ..., "left_duty": ..., "right_duty": ...}.
    * encoders (later): report measured wheel speeds so ``Drive`` can integrate odometry instead of commands.
    """

    physical = True

    def __init__(self, cfg: DriveConfig) -> None:
        self.cfg = cfg
        raise DriveError("the motor HAT driver is not written yet (littlebot/motion/drive.py MotorHatWheels)")

    def set_velocity(self, linear_mps: float, angular_dps: float) -> None:  # pragma: no cover - placeholder
        raise DriveError("motor HAT driver not implemented")

    def stop(self) -> None:  # pragma: no cover - placeholder
        pass

    def close(self) -> None:  # pragma: no cover - placeholder
        pass

    def describe(self) -> dict[str, Any]:  # pragma: no cover - placeholder
        return {"driver": "motor HAT (not implemented)"}


class CommandWatchdog:
    """Keep-alive for a real wheel driver: calls ``on_expire`` when ``kick()`` stops coming.

    ``check(now)`` is pure (tests); ``start()`` runs it on a tiny daemon thread every ``period`` seconds.
    """

    def __init__(self, timeout_s: float, on_expire: Callable[[], None], *, clock: Callable[[], float] | None = None,
                 period: float | None = None) -> None:
        import time

        self.timeout_s = float(timeout_s)
        self.on_expire = on_expire
        self._now = clock or time.monotonic
        self.period = float(period) if period is not None else max(0.02, self.timeout_s / 5.0)
        self._last: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.expired = 0

    def kick(self) -> None:
        self._last = self._now()

    def disarm(self) -> None:
        self._last = None

    def check(self, now: float | None = None) -> bool:
        """True (and ``on_expire`` called once) when armed and not kicked for ``timeout_s``."""
        t = self._now() if now is None else now
        if self._last is not None and t - self._last > self.timeout_s:
            self._last = None
            self.expired += 1
            try:
                self.on_expire()
            except Exception:  # noqa: BLE001
                log.exception("wheel watchdog stop failed")
            return True
        return False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def _run() -> None:
            while not self._stop.wait(self.period):
                self.check()

        self._thread = threading.Thread(target=_run, name="wheel-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=1.0)


# --------------------------------------------------------------------------- limits
@dataclass(frozen=True)
class DriveLimits:
    max_linear_mps: float
    max_angular_dps: float
    max_linear_accel: float
    max_angular_accel: float
    cruise_linear_mps: float
    cruise_angular_dps: float
    watchdog_s: float
    max_duration_s: float
    update_hz: float
    publish_hz: float
    wheel_base_m: float

    def to_dict(self) -> dict[str, float]:
        return {k: round(float(v), 4) for k, v in self.__dict__.items()}


def validate_drive_config(cfg: DriveConfig) -> list[str]:
    errs: list[str] = []
    checks = {
        "max_linear_mps": (cfg.max_linear_mps, 0.0, HARD_MAX_LINEAR_MPS),
        "max_angular_dps": (cfg.max_angular_dps, 0.0, HARD_MAX_ANGULAR_DPS),
        "max_linear_accel_mps2": (cfg.max_linear_accel_mps2, 0.0, HARD_MAX_LINEAR_ACCEL),
        "max_angular_accel_dps2": (cfg.max_angular_accel_dps2, 0.0, HARD_MAX_ANGULAR_ACCEL),
        "cruise_linear_mps": (cfg.cruise_linear_mps, 0.0, HARD_MAX_LINEAR_MPS),
        "cruise_angular_dps": (cfg.cruise_angular_dps, 0.0, HARD_MAX_ANGULAR_DPS),
        "watchdog_seconds": (cfg.watchdog_seconds, MIN_WATCHDOG_S - 1e-9, MAX_WATCHDOG_S),
        "max_duration_seconds": (cfg.max_duration_seconds, 0.0, 120.0),
        "update_hz": (cfg.update_hz, 4.99, 100.0),
        "publish_hz": (cfg.publish_hz, 0.0, 30.0),
        "wheel_base_m": (cfg.wheel_base_m, 0.0, 1.0),
    }
    for key, (v, lo, hi) in checks.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            errs.append(f"motion.drive.{key} must be a number")
            continue
        if not math.isfinite(f) or not lo < f <= hi:
            errs.append(f"motion.drive.{key}={v} outside ({max(lo, 0):g}, {hi:g}]")
    return errs


def limits_from_config(cfg: DriveConfig) -> DriveLimits:
    return DriveLimits(
        max_linear_mps=float(cfg.max_linear_mps), max_angular_dps=float(cfg.max_angular_dps),
        max_linear_accel=float(cfg.max_linear_accel_mps2), max_angular_accel=float(cfg.max_angular_accel_dps2),
        cruise_linear_mps=min(float(cfg.cruise_linear_mps), float(cfg.max_linear_mps)),
        cruise_angular_dps=min(float(cfg.cruise_angular_dps), float(cfg.max_angular_dps)),
        watchdog_s=float(cfg.watchdog_seconds), max_duration_s=float(cfg.max_duration_seconds),
        update_hz=float(cfg.update_hz), publish_hz=float(cfg.publish_hz), wheel_base_m=float(cfg.wheel_base_m),
    )


def _approach(current: float, target: float, max_step: float) -> float:
    if target > current + max_step:
        return current + max_step
    if target < current - max_step:
        return current - max_step
    return target


# --------------------------------------------------------------------------- the drive
@dataclass
class Pose:
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0  # degrees, + = turned left

    def to_dict(self) -> dict[str, float]:
        return {"x": round(self.x, 4), "y": round(self.y, 4), "heading": round(self.heading, 2)}


@dataclass(eq=False)
class _Command:
    kind: str  # velocity | forward | turn
    linear: float  # target m/s (velocity) or signed cruise speed (forward)
    angular: float  # target deg/s (velocity) or signed cruise speed (turn)
    remaining: float  # forward: metres left; turn: degrees left (>= 0)
    deadline: float
    open_ended: bool
    future: "asyncio.Future[bool]"

    @property
    def label(self) -> str:
        return self.kind


class Drive:
    """``DriveBase`` implementation on any ``WheelDriver``. Commands run on the asyncio loop; ``stop``,
    ``emergency_stop`` and ``status`` are safe from any thread."""

    def __init__(self, cfg: DriveConfig | None = None, *, wheels: WheelDriver | None = None, bus: Any = None,
                 clock: Clock | None = None, gate: Gate | None = None, armed: Callable[[], bool] | None = None,
                 physical_enabled: bool = False, note: str = "") -> None:
        self.cfg = cfg or DriveConfig()
        self.bus = bus
        self._clock = clock or Clock(1.0)  # the wheels always run in real time (speeds are physical)
        self._wheels: WheelDriver = wheels or SimulatedWheels(self.cfg.wheel_base_m)
        self._gate = gate
        self._armed = armed or (lambda: False)
        self._physical_enabled = bool(physical_enabled)
        self._note = note
        self._errors = validate_drive_config(self.cfg)
        self.limits = limits_from_config(self.cfg)
        self._lock = threading.RLock()
        self._pose = Pose()
        self._v = 0.0
        self._w = 0.0
        self._cmd: _Command | None = None
        self._estop = False
        self._estop_source: str | None = None
        self._last_command = -math.inf
        self._last_tick: float | None = None
        self._last_publish = -math.inf
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_reason = ""
        self.ticks = 0
        if self._errors:
            log.error("wheels disabled - invalid motion.drive configuration: %s", "; ".join(self._errors))

    # ------------------------------------------------------------------ DriveBase
    async def drive(self, linear_mps: float, angular_dps: float, duration_s: float | None = None) -> bool:
        try:
            v, w = float(linear_mps), float(angular_dps)
            dur = None if duration_s is None else float(duration_s)
        except (TypeError, ValueError):
            return self._refused("drive", "speeds must be numbers")
        if not (math.isfinite(v) and math.isfinite(w)) or (dur is not None and (not math.isfinite(dur) or dur < 0)):
            return self._refused("drive", "speeds / duration must be finite (duration >= 0)")
        v = max(-self.limits.max_linear_mps, min(self.limits.max_linear_mps, v))
        w = max(-self.limits.max_angular_dps, min(self.limits.max_angular_dps, w))
        span = self.limits.max_duration_s if dur is None else min(dur, self.limits.max_duration_s)
        return await self._submit("velocity", v, w, 0.0, span, open_ended=dur is None)

    async def forward(self, meters: float) -> bool:
        try:
            d = float(meters)
        except (TypeError, ValueError):
            return self._refused("forward", "distance must be a number")
        if not math.isfinite(d):
            return self._refused("forward", "distance must be finite")
        speed = math.copysign(self.limits.cruise_linear_mps, d)
        span = self._span(abs(d), self.limits.cruise_linear_mps, self.limits.max_linear_accel)
        return await self._submit("forward", speed, 0.0, abs(d), span, open_ended=False)

    async def turn(self, degrees: float) -> bool:
        try:
            a = float(degrees)
        except (TypeError, ValueError):
            return self._refused("turn", "angle must be a number")
        if not math.isfinite(a):
            return self._refused("turn", "angle must be finite")
        speed = math.copysign(self.limits.cruise_angular_dps, a)
        span = self._span(abs(a), self.limits.cruise_angular_dps, self.limits.max_angular_accel)
        return await self._submit("turn", 0.0, speed, abs(a), span, open_ended=False)

    def stop(self) -> None:
        """Brake smoothly to a halt (the current command ends with False)."""
        self._dispatch(self._stop_now, "stopped")

    def status(self) -> dict[str, Any]:
        with self._lock:
            cmd = self._cmd
            out = {
                "enabled": bool(self.cfg.enabled) and not self._errors,
                "configured_backend": self.cfg.backend,
                "physical": bool(self._wheels.physical),
                "moving": self._moving_locked(),
                "estop": self._estop,
                "estop_source": self._estop_source,
                "pose": self._pose.to_dict(),
                "linear": round(self._v, 4),
                "angular": round(self._w, 3),
                "command": cmd.label if cmd is not None else None,
                "reason": self._reason_locked(),
                "last": self._last_reason,
                "error": "; ".join(self._errors) or None,
                "limits": self.limits.to_dict(),
            }
        try:
            out["driver"] = self._wheels.describe()
        except Exception as e:  # noqa: BLE001
            out["driver"] = {"error": str(e)}
        return out

    # ------------------------------------------------------------------ extras
    @property
    def pose(self) -> Pose:
        with self._lock:
            return Pose(self._pose.x, self._pose.y, self._pose.heading)

    @property
    def wheels(self) -> WheelDriver:
        return self._wheels

    def reset_pose(self) -> None:
        with self._lock:
            self._pose = Pose()

    def refusal(self) -> str | None:
        """Why a command would be refused right now (None = it would run)."""
        with self._lock:
            return self._refusal_locked()

    def emergency_stop(self, source: str = "unknown") -> None:
        """Wheels off NOW (caller's thread), command dropped, latched until reset_emergency_stop()."""
        with self._lock:
            first = not self._estop
            self._estop = True
            self._estop_source = self._estop_source or str(source)
        self._wheels_stop()
        if first and source != "shutdown":
            log.warning("wheels: EMERGENCY STOP (%s)", source)
        self._dispatch(self._halt, f"emergency stop ({source})")

    def reset_emergency_stop(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._cmd is not None or abs(self._v) > SPEED_EPS or abs(self._w) > SPEED_EPS:
                return False
            self._estop = False
            self._estop_source = None
        return True

    async def close(self) -> None:
        with self._lock:
            self._closed = True
        self._wheels_stop()
        self._halt("drive closed")
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            self._wheels.close()
        except Exception:  # noqa: BLE001
            log.exception("closing the wheel driver failed")

    # ------------------------------------------------------------------ admission
    def _span(self, distance: float, cruise: float, accel: float) -> float:
        """Time cap for a distance / angle command: twice the trapezoid time + 1 s, never above the max."""
        if distance <= 0:
            return 0.0
        t = distance / max(cruise, SPEED_EPS) + cruise / max(accel, SPEED_EPS)
        return min(self.limits.max_duration_s, 2.0 * t + 1.0)

    def _refusal_locked(self) -> str | None:
        if self._closed:
            return "wheels stopped (service closed)"
        if self._errors:
            return "invalid motion.drive configuration"
        if not self.cfg.enabled:
            return "wheels are disabled (motion.drive.enabled is false)"
        if self._estop:
            return f"emergency stop latched ({self._estop_source})"
        if self._gate is not None:
            why = self._gate()
            if why:
                return why
        if self._wheels.physical:
            if not self._physical_enabled:
                return "physical motion is switched off (motion.physical_enabled is false)"
            if not self._armed():
                return "physical motion is not armed"
        return None

    def _refused(self, label: str, reason: str) -> bool:
        self._last_reason = f"{label} refused: {reason}"
        log.info("wheels: %s refused: %s", label, reason)
        return False

    async def _submit(self, kind: str, linear: float, angular: float, remaining: float, span: float, *,
                      open_ended: bool) -> bool:
        loop = asyncio.get_running_loop()
        with self._lock:
            why = self._refusal_locked()
            if why:
                return self._refused(kind, why)
            now = self._clock.now()
            if remaining <= 0 and kind != "velocity" or span <= 0:
                return True  # nothing to do
            fut: asyncio.Future[bool] = loop.create_future()
            old = self._cmd
            self._cmd = _Command(kind, linear, angular, remaining, now + span, open_ended, fut)
            self._last_command = now
            if old is not None:
                self._resolve(old, old.open_ended, f"replaced by {kind}")
        self._ensure_task(loop)
        try:
            return await fut
        except asyncio.CancelledError:
            with self._lock:
                if self._cmd is not None and self._cmd.future is fut:
                    self._cmd = None  # the caller gave up: the loop brakes to a halt
            raise

    def _resolve(self, cmd: _Command, ok: bool, reason: str) -> None:
        if not ok:
            self._last_reason = f"{cmd.kind} ended: {reason}"
        if not cmd.future.done():
            loop = cmd.future.get_loop()
            if loop.is_closed():
                return
            try:
                if self._loop_is_current(loop):
                    cmd.future.set_result(ok)
                else:
                    loop.call_soon_threadsafe(lambda: cmd.future.done() or cmd.future.set_result(ok))
            except RuntimeError:
                pass

    @staticmethod
    def _loop_is_current(loop: asyncio.AbstractEventLoop) -> bool:
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return not loop.is_running()

    # ------------------------------------------------------------------ control loop
    def _ensure_task(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not loop:
            self._loop = loop
            self._task = None
        if self._task is None or self._task.done():
            self._last_tick = None
            self._task = loop.create_task(self._run(), name="drive-control")

    def _moving_locked(self) -> bool:
        return self._cmd is not None or abs(self._v) > SPEED_EPS or abs(self._w) > SPEED_EPS

    async def _run(self) -> None:
        period = 1.0 / self.limits.update_hz
        try:
            while not self._closed:
                with self._lock:
                    busy = self._moving_locked()
                if not busy:
                    break  # idle: no ticks at all (a new command starts a new task)
                await self._clock.sleep(period)
                self._tick(self._clock.now(), period)
        finally:
            with self._lock:
                self._last_tick = None

    def _tick(self, now: float, period: float) -> None:
        events: list[dict[str, Any]] = []
        with self._lock:
            self.ticks += 1
            dt = period if self._last_tick is None else now - self._last_tick
            self._last_tick = now
            lim = self.limits
            cmd = self._cmd
            if dt > lim.watchdog_s:  # the loop stalled: never carry on blindly
                self._v = self._w = 0.0
                if cmd is not None:
                    self._cmd = None
                    self._resolve(cmd, False, f"control loop stalled for {dt:.1f}s")
                self._wheels_stop()
                events.append(self._event_locked())
                cmd = None
                dt = 0.0
            dt = max(dt, 0.0)
            if cmd is not None and cmd.open_ended and now - self._last_command > lim.watchdog_s:
                self._cmd = None
                self._resolve(cmd, False, f"watchdog: no command for {lim.watchdog_s:g}s")
                log.info("wheels: watchdog stop (no command for %.1fs)", lim.watchdog_s)
                cmd = None
            elif cmd is not None and now >= cmd.deadline - 1e-9:
                self._cmd = None
                done_ok = cmd.kind == "velocity"  # a distance that didn't finish in time is a failure
                self._resolve(cmd, done_ok, "time limit reached")
                cmd = None

            # desired velocity
            v_des = w_des = 0.0
            if cmd is not None:
                if cmd.kind == "velocity":
                    v_des, w_des = cmd.linear, cmd.angular
                elif cmd.kind == "forward":
                    v_des = math.copysign(min(abs(cmd.linear), math.sqrt(2 * lim.max_linear_accel * cmd.remaining)),
                                          cmd.linear)
                else:
                    w_des = math.copysign(min(abs(cmd.angular), math.sqrt(2 * lim.max_angular_accel * cmd.remaining)),
                                          cmd.angular)
            v_prev, w_prev = self._v, self._w
            self._v = _approach(self._v, v_des, lim.max_linear_accel * dt)
            self._w = _approach(self._w, w_des, lim.max_angular_accel * dt)
            # integrate (trapezoid on the velocity), distance commands stop exactly at their target
            ds = 0.5 * (v_prev + self._v) * dt
            da = 0.5 * (w_prev + self._w) * dt
            if cmd is not None and cmd.kind == "forward":
                ds = math.copysign(min(abs(ds), cmd.remaining), cmd.linear) if ds * cmd.linear > 0 else 0.0
                cmd.remaining -= abs(ds)
            elif cmd is not None and cmd.kind == "turn":
                da = math.copysign(min(abs(da), cmd.remaining), cmd.angular) if da * cmd.angular > 0 else 0.0
                cmd.remaining -= abs(da)
            heading_mid = math.radians(self._pose.heading + da / 2.0)
            self._pose.x += ds * math.cos(heading_mid)
            self._pose.y += ds * math.sin(heading_mid)
            self._pose.heading = (self._pose.heading + da + 180.0) % 360.0 - 180.0
            if cmd is not None and cmd.kind in ("forward", "turn"):
                eps = DISTANCE_EPS_M if cmd.kind == "forward" else ANGLE_EPS_DEG
                if cmd.remaining <= eps:
                    self._cmd = None
                    self._v = self._w = 0.0  # arrived on the deceleration profile: at (almost) zero speed already
                    self._resolve(cmd, True, "")
            moving = self._moving_locked()
            if moving:
                self._wheels_set(self._v, self._w)
            else:
                self._wheels_stop()
            if not moving or now - self._last_publish >= 1.0 / max(lim.publish_hz, 1e-3):
                self._last_publish = now
                events.append(self._event_locked())
        self._publish(events)

    def _stop_now(self, reason: str) -> None:
        with self._lock:
            cmd, self._cmd = self._cmd, None
            if cmd is not None:
                self._resolve(cmd, False, reason)
        # the control loop (running while the wheels turn) brakes to zero, then idles

    def _halt(self, reason: str) -> None:
        """Hard stop (wheels already off): no ramp, command dropped."""
        events: list[dict[str, Any]] = []
        with self._lock:
            cmd, self._cmd = self._cmd, None
            if cmd is not None:
                self._resolve(cmd, False, reason)
            was_moving = abs(self._v) > SPEED_EPS or abs(self._w) > SPEED_EPS or cmd is not None
            self._v = self._w = 0.0
            if was_moving:
                events.append(self._event_locked())
        self._publish(events)

    # ------------------------------------------------------------------ outputs
    def _wheels_set(self, v: float, w: float) -> None:
        try:
            self._wheels.set_velocity(v, w)
        except Exception as e:  # noqa: BLE001 - a driver fault latches the e-stop, never crashes
            log.error("wheel driver error: %s -> emergency stop", e)
            self._errors_from_driver(str(e))

    def _errors_from_driver(self, message: str) -> None:
        self._estop = True
        self._estop_source = self._estop_source or "driver_error"
        self._last_reason = f"wheel driver error: {message}"
        self._wheels_stop()
        if self._cmd is not None:
            cmd, self._cmd = self._cmd, None
            self._resolve(cmd, False, "wheel driver error")
        self._v = self._w = 0.0

    def _wheels_stop(self) -> None:
        try:
            self._wheels.stop()
        except Exception:  # noqa: BLE001
            log.exception("wheel stop failed - switch the motor supply off")

    # ------------------------------------------------------------------ helpers
    def _reason_locked(self) -> str:
        why = self._refusal_locked()
        if why:
            return why
        base = "physical wheels ARMED" if self._wheels.physical else "simulated wheels (no motors fitted)"
        return f"{base}{'; ' + self._note if self._note else ''}"

    def _event_locked(self) -> dict[str, Any]:
        cmd = self._cmd
        return {**self._pose.to_dict(), "linear": round(self._v, 4), "angular": round(self._w, 3),
                "moving": self._moving_locked(), "command": cmd.label if cmd is not None else None,
                "estop": self._estop, "simulated": not self._wheels.physical}

    def _publish(self, events: list[dict[str, Any]]) -> None:
        if self.bus is None:
            return
        for data in events:
            try:
                self.bus.publish(DRIVE_TOPIC, data, source="motion")
            except Exception:  # noqa: BLE001
                log.exception("publishing %s failed", DRIVE_TOPIC)

    def _dispatch(self, fn: Callable[..., Any], *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running() or self._loop_is_current(loop):
            fn(*args)
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            fn(*args)


class MockDrive(Drive):
    """``Drive`` on simulated wheels: integrates a simulated pose and publishes ``motion.drive`` events."""

    def __init__(self, cfg: DriveConfig | None = None, **kwargs: Any) -> None:
        kwargs.pop("wheels", None)
        cfg = cfg or DriveConfig()
        super().__init__(cfg, wheels=SimulatedWheels(cfg.wheel_base_m), **kwargs)


def create_drive(cfg: DriveConfig, *, physical_enabled: bool = False, **kwargs: Any) -> Drive:
    """The drive for this configuration: the motor HAT only with backend=motor_hat AND physical_enabled (and
    then still only after a parent arms motion); otherwise, or when the HAT can't be opened, the simulation."""
    if cfg.backend == "motor_hat":
        if not physical_enabled:
            return MockDrive(cfg, physical_enabled=physical_enabled,
                             note="motor_hat configured but motion.physical_enabled is false", **kwargs)
        try:
            wheels = MotorHatWheels(cfg)
        except Exception as e:  # noqa: BLE001 - not written yet / no HAT: simulate
            log.warning("wheels: %s - using simulated wheels", e)
            return MockDrive(cfg, physical_enabled=physical_enabled, note=f"motor HAT unavailable: {e}", **kwargs)
        return Drive(cfg, wheels=wheels, physical_enabled=physical_enabled, **kwargs)
    return MockDrive(cfg, physical_enabled=physical_enabled, **kwargs)


__all__ = [
    "CommandWatchdog", "DRIVE_TOPIC", "Drive", "DriveBase", "DriveError", "DriveLimits", "MockDrive",
    "MotorHatWheels", "Pose", "SimulatedWheels", "WheelDriver", "create_drive", "validate_drive_config",
    "wheel_speeds",
]
