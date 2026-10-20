"""MotionController: LittleBot's ``MotionAPI`` implementation.

Responsibilities
----------------
* choose the output stage (``MockPCA9685`` simulation unless backend == "pca9685" AND
  physical_enabled AND a valid calibration file - then ``PCA9685Driver``);
* keep outputs de-energised until a parent arms motion (``motion.physical_armed``,
  never persisted) - the second key of the two-key design (docs/MOTION_SAFETY.md);
* plan smooth, limit-respecting movements (minimum-jerk, speed + acceleration limits,
  hard clamps, per-tick rate limiter) and run them on an asyncio scheduler at
  ``update_hz`` that sleeps completely when nothing moves;
* gestures (keyframes), arbitration (``Resource.MOTION`` leases), cancel / STOP_ALL /
  emergency stop, and ``motion.*`` events for the simulator panel and dashboard.

Threading: commands are coroutines on the runtime loop. ``cancel``, ``emergency_stop``,
``reset_emergency_stop`` and ``status`` are safe from any thread. ``emergency_stop``
switches the outputs off synchronously in the caller's thread before anything else.
Bus events are published after internal state is consistent (outbox), never while the
internal lock is held.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.clock import Clock
from littlebot.core.config import MotionConfig
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.runtime import cancel_and_wait
from littlebot.core.service import Service, ServiceStatus
from littlebot.motion import gestures as G
from littlebot.motion.drivers import DriverError, MockPCA9685, PCA9685Driver, ServoDriver
from littlebot.motion.interpolation import (
    BrakeSegment, MinJerkSegment, Segment, limit_step, make_brake, plan_duration,
)
from littlebot.motion.joints import (
    Calibration, CalibrationError, JointSpec, MotionConfigError, build_joint_specs, load_calibration,
    resolve_path,
)

LEASE_OWNER = "motion"
ARMED_KEY = "motion.physical_armed"
HOMING_PRIORITY = -1  # anything supersedes the STOP_ALL return-to-neutral
PULSE_EPS_US = 0.5  # don't rewrite a channel for sub-microsecond changes
STALL_FACTOR = 3.0  # a tick later than 3 periods = the loop stalled: pause motion instead of jumping
RATE_LIMIT_MARGIN = 1.05
NEUTRAL_SPEED = 0.6
_EPS_DEG = 1e-3

DriverFactory = Callable[[MotionConfig], ServoDriver]


def default_driver_factory(cfg: MotionConfig) -> ServoDriver:
    return PCA9685Driver(bus_number=cfg.i2c_bus, address=cfg.i2c_address, frequency_hz=cfg.pwm_frequency)


# --------------------------------------------------------------------------- backend gate
@dataclass
class BackendDecision:
    use_physical: bool
    reason: str
    calibration: Calibration | None = None
    calibration_error: str | None = None


def decide_backend(cfg: MotionConfig) -> BackendDecision:
    """Gate 1 of the two-key design. Pure: never touches hardware or imports smbus2."""
    path = resolve_path(cfg.calibration_file)
    calibration: Calibration | None = None
    cal_error: str | None = None
    if path.is_file():
        try:
            calibration = load_calibration(path, known_joints=cfg.servos.keys())
        except CalibrationError as e:
            cal_error = str(e)
    if cfg.backend != "pca9685":
        return BackendDecision(False, "simulation only: no servos connected yet (motion.backend is 'mock')", calibration, cal_error)
    if not cfg.physical_enabled:
        return BackendDecision(False, "simulation only: physical motion is switched off (motion.physical_enabled is false; see docs/MOTION_SAFETY.md)",
                               calibration, cal_error)
    if not path.is_file():
        return BackendDecision(False, f"simulation only: calibration file {cfg.calibration_file} not found "
                                      "- run scripts/calibrate_servos.py", None, None)
    if calibration is None:
        return BackendDecision(False, f"simulation only: calibration invalid: {cal_error}", None, cal_error)
    missing = sorted(set(cfg.servos) - set(calibration.servos))
    if missing:
        err = f"calibration missing joints: {', '.join(missing)}"
        return BackendDecision(False, f"simulation only: {err}", calibration, err)
    return BackendDecision(True, "physical motion enabled", calibration, None)


# --------------------------------------------------------------------------- internals
@dataclass
class _Step:
    targets: dict[str, float]
    min_duration: float = 0.0
    hold: float = 0.0
    speed: float = 1.0  # fraction of each joint's max speed
    speed_cap: float | None = None  # absolute deg/s cap


@dataclass(eq=False)
class _Job:
    id: int
    kind: str  # gesture | move | neutral | homing
    name: str | None
    joints: frozenset[str]
    steps: list[_Step]
    priority: int
    lease_mode: str  # own | borrowed | none
    borrowed: Lease | None = None
    future: "asyncio.Future[bool] | None" = None
    state: str = "pending"  # pending | running | done
    index: int = -1
    phase: str = "idle"  # settle | move | hold
    phase_end: float = 0.0
    events: bool = False  # gesture started published

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.name}" if self.name else self.kind


@dataclass(eq=False)
class _Joint:
    spec: JointSpec
    angle: float
    velocity: float = 0.0
    segment: Segment | None = None
    job_id: int | None = None
    known: bool = False  # position belief trustworthy (False at start / after e-stop)
    energised: bool = False  # receiving pulses
    last_pulse: float | None = None

    @property
    def target(self) -> float:
        return self.segment.target if self.segment is not None else self.angle


@dataclass
class _Outbox:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


# --------------------------------------------------------------------------- controller
class MotionController(Service):
    name = "motion"

    def __init__(self, ctx: Any, *, clock: Clock | None = None, driver_factory: DriverFactory | None = None) -> None:
        super().__init__()
        # core.service.Service keeps its lifecycle state in the *instance attribute*
        # ``status``, which would shadow MotionAPI.status(). Keep it as ``service_status``.
        self.__dict__.pop("status", None)
        self.service_status = ServiceStatus.STOPPED
        self.ctx = ctx
        self.cfg: MotionConfig = ctx.config.motion
        self._clock = clock or Clock(1.0)  # motion ALWAYS runs in real time (speed limits are physical)
        self._driver_factory = driver_factory or default_driver_factory
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._latch_lock = threading.Lock()
        self._outbox = _Outbox()
        self._ids = itertools.count(1)

        # Gate 1: which backend, which limits.
        self._decision = decide_backend(self.cfg)
        self._calibration = self._decision.calibration
        self._calibration_error = self._decision.calibration_error
        self._config_error: str | None = None
        self._sim_reason = self._decision.reason
        try:
            self._specs = build_joint_specs(self.cfg, self._calibration)
        except (MotionConfigError, CalibrationError) as e:
            if self._calibration is not None:
                self._calibration_error = f"calibration rejected: {e}"
                self._calibration = None
                if self._decision.use_physical:
                    self._decision = BackendDecision(False, f"simulation only: {self._calibration_error}")
                    self._sim_reason = self._decision.reason
                try:
                    self._specs = build_joint_specs(self.cfg, None)
                except MotionConfigError as e2:
                    self._config_error = str(e2)
            else:
                self._config_error = str(e)
            if self._config_error:
                self._specs = {n: JointSpec.from_config(n, sc) for n, sc in self.cfg.servos.items()}
                self.log.error("motion disabled - invalid configuration: %s", self._config_error)
        if self._calibration_error:
            self.log.warning("calibration not used: %s", self._calibration_error)
        gesture_errors = [] if self._config_error else G.validate_gestures(self._specs)
        for err in gesture_errors:
            self.log.warning("gesture check: %s (will be clamped)", err)

        self._joints: dict[str, _Joint] = {n: _Joint(spec=s, angle=s.neutral) for n, s in self._specs.items()}
        self._driver: ServoDriver = MockPCA9685(frequency_hz=self.cfg.pwm_frequency, address=self.cfg.i2c_address)
        self._driver_closed = False
        self._physical = False
        self._backend_error: str | None = None

        self._armed = bool(ctx.settings.get(ARMED_KEY, False))
        self._estop = False
        self._estop_source: str | None = None
        self._error: str | None = None

        self._jobs: list[_Job] = []
        self._gesture_job: _Job | None = None
        self._lease: Lease | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._release_handle: asyncio.TimerHandle | None = None
        self._subs: list[Subscription] = []
        self._started = False
        self._stopped = False
        self._closing = False

        self._last_tick: float | None = None
        self._last_positions = -math.inf
        self._was_moving = False
        self.ticks = 0
        self.idle = True

        if self._config_error:
            self.set_status(ServiceStatus.FAILED, "invalid motion configuration")

    # ================================================================ lifecycle
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopped = False
        self._closing = False
        if self._driver_closed:  # restarted after stop(): start again from a fresh, de-energised simulation driver
            with self._io_lock:
                self._driver = MockPCA9685(frequency_hz=self.cfg.pwm_frequency, address=self.cfg.i2c_address)
                self._driver_closed = False
                self._physical = False
        self._bind_loop()
        if self._decision.use_physical and not self._config_error:
            try:
                driver = await asyncio.to_thread(self._driver_factory, self.cfg)
            except Exception as e:  # noqa: BLE001 - missing smbus2, no chip, wrong chip...
                self._backend_error = f"PCA9685 unavailable: {e}"
                self._sim_reason = f"simulation only: {self._backend_error}"
                self.log.warning("%s - using the simulated servo driver", self._backend_error)
            else:
                old = self._driver
                with self._io_lock:
                    self._driver = driver
                    self._physical = True
                try:
                    old.close()
                except Exception:  # noqa: BLE001
                    pass
                self.log.warning("PHYSICAL servo output active (%s); outputs stay off until a parent arms motion",
                                 driver.describe())
        bus = self.ctx.bus
        self._subs = [
            bus.subscribe(Topics.STOP_ALL, self._on_stop_all),
            bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed),
        ]
        self._armed = bool(self.ctx.settings.get(ARMED_KEY, False))
        self._update_health()
        with self._lock:
            self._queue_positions(False)
        self._flush()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._closing = True
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        self._all_off_now()
        with self._lock:
            self._halt_locked("motion service stopping")
        self._flush()
        self._cancel_release_timer()
        task, self._task = self._task, None
        if task is not None:
            await cancel_and_wait(task)
        driver = self._driver
        with self._io_lock:
            self._driver_closed = True  # no further writes from the controller; close() switches outputs off
        try:
            if getattr(driver, "blocking_io", False):
                await asyncio.to_thread(driver.close)
            else:
                driver.close()
        except Exception:  # noqa: BLE001
            self.log.exception("closing servo driver failed")
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._started = False
        self.set_status(ServiceStatus.STOPPED)

    # ================================================================ MotionAPI
    def gestures(self) -> list[str]:
        return G.gesture_names()

    def gesture_info(self) -> list[dict[str, Any]]:
        return [{"name": n, "description": G.GESTURES[n].description} for n in G.gesture_names()]

    def joint_names(self) -> list[str]:
        return list(self._specs)

    async def run_gesture(self, name: str, *, priority: int | None = None, owner: str | None = None) -> bool:
        """Run a named gesture. False if unknown, refused (estop / not armed / lease) or cancelled.

        ``priority`` (default Priority.ACTIVITY) and ``owner`` (the caller's lease owner, if it
        holds Resource.MOTION) are optional extensions of the MotionAPI contract.
        """
        gesture = G.GESTURES.get(str(name))
        if gesture is None:
            self.log.warning("unknown gesture %r", name)
            return False
        if self._config_error:
            return self._refused(f"gesture {name}", "invalid motion configuration")
        joints = G.gesture_joints(gesture, self._specs)
        steps = [_Step(targets={j: a for j, a in G.resolve_pose(kf, self._specs).items() if j in joints},
                       min_duration=kf.duration, hold=kf.hold, speed=kf.speed) for kf in gesture.keyframes]
        if not joints:
            self.log.warning("gesture %s uses no configured joint", name)
            return False
        return await self._submit("gesture", gesture.name, joints, steps, priority, owner)

    async def move_joint(self, joint: str, angle: float, *, speed_dps: float | None = None,
                         priority: int | None = None, owner: str | None = None) -> bool:
        spec = self._specs.get(str(joint))
        if spec is None:
            self.log.warning("unknown joint %r", joint)
            return False
        try:
            requested = float(angle)
            clamped = spec.clamp(requested)
        except (TypeError, ValueError):
            self.log.warning("move_joint(%s): invalid angle %r", joint, angle)
            return False
        if abs(clamped - requested) > 1e-9:
            self.log.info("move_joint(%s, %.1f) clamped to %.1f (limits %.1f..%.1f)", joint, requested, clamped,
                          spec.min_angle, spec.max_angle)
        cap: float | None = None
        if speed_dps is not None:
            try:
                cap = float(speed_dps)
            except (TypeError, ValueError):
                return False
            if not math.isfinite(cap) or cap <= 0:
                self.log.warning("move_joint(%s): invalid speed %r", joint, speed_dps)
                return False
        step = _Step(targets={spec.name: clamped}, speed=1.0, speed_cap=cap)
        return await self._submit("move", spec.name, frozenset([spec.name]), [step], priority, owner)

    async def neutral(self, *, priority: int | None = None, owner: str | None = None) -> bool:
        step = _Step(targets={n: s.neutral for n, s in self._specs.items()}, speed=NEUTRAL_SPEED)
        return await self._submit("neutral", None, frozenset(self._specs), [step], priority, owner)

    def cancel(self) -> None:
        """Stop commanded motion smoothly where it is (decelerate within limits).

        The slow return-to-neutral started by STOP_ALL is part of stopping and is not
        aborted by cancel(); use emergency_stop() to halt and de-energise instantly.
        """
        self._dispatch(self._cancel_jobs, False, "cancelled")

    def _disarm(self, why: str) -> None:
        """Any e-stop also withdraws the parent's arming: after a reset, physical motion
        needs a fresh, deliberate re-arm (whatever triggered the stop: keyboard, panel,
        dashboard or a driver fault)."""
        try:
            if self.ctx.settings.get(ARMED_KEY):
                self.ctx.settings.set(ARMED_KEY, False, source="motion")
                self.log.info("physical motion disarmed: %s", why)
        except Exception:  # noqa: BLE001 - never let bookkeeping block an emergency stop
            self.log.exception("could not clear %s", ARMED_KEY)

    def emergency_stop(self, source: str = "unknown") -> None:
        """Immediately de-energise every output (in the caller's thread), cancel everything
        and latch: all motion commands are refused until reset_emergency_stop()."""
        with self._latch_lock:
            first = not self._estop
            self._estop = True  # writers check this inside the I/O lock -> no pulse after all_off
            if first:
                self._estop_source = str(source)
        self._all_off_now()
        if first:
            if source == "shutdown":  # routine: the app makes the servos safe on every exit
                self.log.info("servos de-energised for shutdown")
            else:
                self.log.warning("EMERGENCY STOP (source=%s): outputs off, motion latched", source)
                self._disarm(f"emergency stop ({source})")
            self.ctx.bus.publish(Topics.MOTION_ESTOP, {"active": True, "source": str(source)}, source="motion")
        self._update_health()
        self._dispatch(self._halt, "emergency stop")

    def reset_emergency_stop(self) -> bool:
        """Clear the latch. Requires motion to be idle; joints become 'unknown' and the next
        command first energises them at neutral, settles, then eases to its target."""
        with self._lock:
            if not self._estop:
                return True
            if self._closing:
                return False
            if self._jobs or any(j.segment is not None for j in self._joints.values()):
                self.log.info("emergency stop reset refused: motion not idle yet")
                return False
        if self._error or self._physical:
            probe = getattr(self._driver, "probe", None)
            if callable(probe) and not self._driver_closed:
                try:
                    with self._io_lock:
                        probe()
                        self._driver.all_off()
                except Exception as e:  # noqa: BLE001
                    self._error = f"servo driver still failing: {e}"
                    self.log.warning("emergency stop reset refused: %s", self._error)
                    return False
        with self._lock:
            source = self._estop_source
            self._estop = False
            self._estop_source = None
            self._error = None
            for j in self._joints.values():
                j.known = False
                j.energised = False
                j.last_pulse = None
                j.velocity = 0.0
        self.log.warning("emergency stop reset (was: %s)", source)
        self.ctx.bus.publish(Topics.MOTION_ESTOP, {"active": False, "source": "reset"}, source="motion")
        self._update_health()
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            joints = {
                n: {
                    "angle": round(j.angle, 2), "target": round(j.target, 2),
                    "min": j.spec.min_angle, "max": j.spec.max_angle, "neutral": j.spec.neutral,
                    "channel": j.spec.channel, "known": j.known, "energised": j.energised,
                    "position": round(j.spec.normalise(j.angle), 3), "positive_toward": j.spec.positive_toward,
                    "pulse_us": round(j.last_pulse, 1) if (j.energised and j.last_pulse is not None) else None,
                }
                for n, j in self._joints.items()
            }
            moving = self._moving_locked()
            gesture = self._gesture_job.name if self._gesture_job is not None else None
            energised = any(j.energised for j in self._joints.values()) and self._outputs_allowed()
        try:
            driver = self._driver.describe()
        except Exception as e:  # noqa: BLE001
            driver = {"error": str(e)}
        return {
            "backend": "pca9685" if self._physical else "mock",
            "configured_backend": self.cfg.backend,
            "physical_enabled": bool(self.cfg.physical_enabled),
            "armed": bool(self._armed),
            "estop": self._estop,
            "estop_source": self._estop_source,
            "moving": moving,
            "gesture": gesture,
            "joints": joints,
            "simulation_only": not self._physical,
            "reason": self._reason(),
            "error": self._error or self._config_error or self._backend_error,
            "outputs_energised": energised,
            "gestures": self.gestures(),
            "calibration": {"file": self.cfg.calibration_file, "loaded": self._calibration is not None,
                            "error": self._calibration_error},
            "driver": driver,
            "update_hz": self.cfg.update_hz,
        }

    # ---------------------------------------------------------------- Service health
    def set_status(self, status: ServiceStatus, detail: str = "") -> None:  # see note in __init__
        if status != self.service_status or detail != self.status_detail:
            self.log.info("status %s%s", status.value, f" ({detail})" if detail else "")
        self.service_status, self.status_detail = status, detail

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.service_status.value, "detail": self.status_detail}

    # ---------------------------------------------------------------- extras
    @property
    def driver(self) -> ServoDriver:
        return self._driver

    @property
    def specs(self) -> dict[str, JointSpec]:
        return dict(self._specs)

    @property
    def physical(self) -> bool:
        return self._physical

    # ================================================================ admission
    async def _submit(self, kind: str, name: str | None, joints: frozenset[str], steps: list[_Step],
                      priority: int | None, owner: str | None) -> bool:
        self._bind_loop()
        prio = int(Priority.ACTIVITY if priority is None else priority)
        label = f"{kind} {name}" if name else kind
        assert self._loop is not None
        with self._lock:
            reason = self._refusal()
            if reason:
                return self._refused(label, reason)
            for other in self._jobs:
                if other.joints & joints and other.priority > prio:
                    return self._refused(label, f"{other.label} has higher priority ({other.priority} > {prio})")
            admission = self._admit(prio, owner)
            if admission is None:
                holder = self.ctx.arbiter.holder(Resource.MOTION)
                return self._refused(label, f"motion reserved by {holder.owner if holder else '?'}")
            mode, borrowed = admission
            if kind == "gesture" and steps:
                # A gesture interrupting another gesture also brings home the joints the old one
                # was using but the new one doesn't (no arm left hanging mid-wave).
                leftovers = frozenset().union(*(o.joints - joints for o in self._jobs
                                                if o.kind == "gesture" and o.joints & joints)) or frozenset()
                if leftovers:
                    home = {j: self._specs[j].neutral for j in leftovers if j in self._specs}
                    first = steps[0]
                    steps = [_Step(targets={**home, **first.targets}, min_duration=first.min_duration,
                                   hold=first.hold, speed=first.speed, speed_cap=first.speed_cap), *steps[1:]]
                    joints = joints | frozenset(home)
            job = _Job(id=next(self._ids), kind=kind, name=name, joints=joints, steps=steps, priority=prio,
                       lease_mode=mode, borrowed=borrowed)
            job.future = self._loop.create_future()
            self._jobs.append(job)
            for other in list(self._jobs):
                if other is not job and other.joints & joints:
                    self._finish_job(other, False, brake=True, reason=f"superseded by {label}")
            self._cancel_release_timer()
            self._wake_scheduler()
            fut = job.future
        self._flush()
        try:
            return await fut
        except asyncio.CancelledError:
            # The caller was cancelled: stop its motion smoothly, then propagate.
            self._cancel_job(job.id, "caller cancelled")
            raise

    def _refused(self, label: str, reason: str) -> bool:
        self.log.info("%s refused: %s", label, reason)
        return False

    def _refusal(self) -> str | None:
        if self._closing:
            return "motion service stopped"
        if self._config_error:
            return "invalid motion configuration"
        if self._estop:
            return f"emergency stop latched ({self._estop_source})"
        if self._physical and not self._armed:
            return "physical motion is not armed"
        return None

    def _admit(self, prio: int, owner: str | None) -> tuple[str, Lease | None] | None:
        """Lease policy: motion never preempts another behaviour's lease.

        * MOTION free (or ours): take/upgrade our own lease ``motion`` at ``prio``.
        * held by ``owner`` (the caller): run under the caller's lease.
        * held by someone else with higher priority: refuse.
        * held by someone else with equal/lower priority: run on its behalf (typically the
          activity that reserved the motors is the caller); stops when that lease ends.
        """
        arbiter = self.ctx.arbiter
        holder = arbiter.holder(Resource.MOTION)
        if holder is None or holder.owner == LEASE_OWNER:
            lease = self._lease
            if lease is not None and lease.active and holder is lease and lease.priority >= prio:
                return ("own", None)
            new = arbiter.acquire(LEASE_OWNER, [Resource.MOTION], prio, on_preempt=self._on_preempted)
            if new is None:
                return None
            self._lease = new
            return ("own", None)
        if owner is not None and holder.owner == owner:
            return ("borrowed", holder)
        if holder.priority > prio:
            return None
        return ("borrowed", holder)

    def _on_preempted(self, lease: Lease, by: str) -> None:
        self._dispatch(self._lease_lost, lease, by)

    def _lease_lost(self, lease: Lease, by: str) -> None:
        with self._lock:
            if lease is not self._lease:
                return
            self._lease = None
            for job in list(self._jobs):
                if job.lease_mode == "own":
                    self._finish_job(job, False, brake=True, reason=f"preempted by {by}")
        self._flush()

    def _release_lease_if_unused(self) -> None:
        if self._lease is None:
            return
        if any(j.lease_mode == "own" for j in self._jobs):
            return
        lease, self._lease = self._lease, None
        lease.release()

    # ================================================================ job control
    def _cancel_job(self, job_id: int, reason: str) -> None:
        def _do() -> None:
            with self._lock:
                for job in list(self._jobs):
                    if job.id == job_id:
                        self._finish_job(job, False, brake=True, reason=reason)
            self._flush()
        self._dispatch(_do)

    def _cancel_jobs(self, include_homing: bool, reason: str) -> None:
        with self._lock:
            for job in list(self._jobs):
                if job.kind == "homing" and not include_homing:
                    continue
                self._finish_job(job, False, brake=True, reason=reason)
        self._flush()

    def _finish_job(self, job: _Job, success: bool, *, brake: bool, reason: str = "") -> None:
        """Remove a job; resolve its awaiter; brake (smooth) or drop its joints' motion."""
        if job.state == "done":
            return
        job.state = "done"
        for name in job.joints:
            joint = self._joints[name]
            if joint.job_id != job.id:
                continue
            joint.job_id = None
            if brake:
                self._brake(joint)
            else:
                joint.segment = None
                joint.velocity = 0.0
        if job in self._jobs:
            self._jobs.remove(job)
        if job.future is not None and not job.future.done():
            try:
                job.future.set_result(success)
            except RuntimeError:  # loop already closed
                pass
        if not success and reason:
            self.log.info("%s stopped: %s", job.label, reason)
        if job.events:
            self._queue(Topics.MOTION_GESTURE_FINISHED, {"gesture": job.name, "cancelled": not success})
        if self._gesture_job is job:
            self._gesture_job = None
        self._release_lease_if_unused()

    def _brake(self, joint: _Joint) -> None:
        seg = joint.segment
        if seg is None or isinstance(seg, BrakeSegment):
            return
        v = joint.velocity
        if abs(v) < _EPS_DEG:
            joint.segment = None
            joint.velocity = 0.0
            return
        t0 = self._last_tick if self._last_tick is not None else self._clock.now()
        brake = make_brake(joint.angle, v, t0, joint.spec.accel_limit)
        if isinstance(seg, MinJerkSegment) and ((v > 0 and brake.end >= seg.end) or (v < 0 and brake.end <= seg.end)):
            return  # already decelerating harder than a brake would: let the segment finish
        joint.segment = brake

    def _halt(self, reason: str) -> None:
        with self._lock:
            self._halt_locked(reason)
        self._flush()

    def _halt_locked(self, reason: str) -> None:
        """Hard stop (outputs are already off): drop all jobs and segments, forget positions."""
        for job in list(self._jobs):
            self._finish_job(job, False, brake=False, reason=reason)
        for j in self._joints.values():
            j.segment = None
            j.velocity = 0.0
            j.job_id = None
            j.energised = False
            j.known = False
            j.last_pulse = None
        if self._lease is not None:
            lease, self._lease = self._lease, None
            lease.release()
        self._cancel_release_timer()
        self._queue_positions(False)
        self._was_moving = False

    # ================================================================ events in
    def _on_stop_all(self, event: Event) -> None:
        self._dispatch(self._stop_all, str(event.get("source") or "unknown"))

    def _stop_all(self, source: str) -> None:
        """Parent STOP (not latched): cancel everything smoothly, then drift back to neutral slowly."""
        with self._lock:
            self._cancel_jobs_locked(f"stop all ({source})")
            if (self.cfg.stop_all_returns_to_neutral and self._refusal() is None and self._task is not None
                    and self._loop is not None):
                targets = {n: j.spec.neutral for n, j in self._joints.items()
                           if j.energised and (j.segment is not None or abs(j.angle - j.spec.neutral) > 0.5)}
                if targets:
                    job = _Job(id=next(self._ids), kind="homing", name=None, joints=frozenset(targets),
                               steps=[_Step(targets=targets, speed=self.cfg.homing_speed_factor)],
                               priority=HOMING_PRIORITY, lease_mode="none")
                    self._jobs.append(job)
                    self._cancel_release_timer()
                    self._wake_scheduler()
        self._flush()

    def _cancel_jobs_locked(self, reason: str) -> None:
        for job in list(self._jobs):
            self._finish_job(job, False, brake=True, reason=reason)

    def _on_settings_changed(self, event: Event) -> None:
        if event.get("key") != ARMED_KEY:
            return
        armed = bool(event.get("value"))
        if armed == self._armed:
            return
        self._armed = armed
        if armed:
            self.log.warning("motion ARMED by a parent%s", "" if self._physical else " (simulation only)")
        else:
            self.log.warning("motion disarmed%s", ": outputs off" if self._physical else "")
            if self._physical:
                self._all_off_now()
                self._dispatch(self._halt, "disarmed")
        self._update_health()

    # ================================================================ scheduler
    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._loop_thread = threading.get_ident()
            self._wake = asyncio.Event()
            self._task = None
        if not self._closing and (self._task is None or self._task.done()):
            self._task = loop.create_task(self._scheduler(), name="motion-scheduler")

    def _wake_scheduler(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _has_work(self) -> bool:
        return bool(self._jobs) or any(j.segment is not None for j in self._joints.values())

    def _moving_locked(self) -> bool:
        return self._has_work()

    async def _scheduler(self) -> None:
        period = 1.0 / float(self.cfg.update_hz)
        assert self._wake is not None
        while not self._closing:
            with self._lock:
                busy = self._has_work()
            if not busy:
                self._on_idle()
                self._wake.clear()
                with self._lock:
                    busy = self._has_work()
                if not busy:
                    self.idle = True
                    await self._wake.wait()  # no ticks at all while nothing moves
                self.idle = False
                self._last_tick = None
                continue
            await self._clock.sleep(period)
            if self._closing:
                break
            try:
                writes = self._tick(self._clock.now(), period)
            except Exception as e:  # noqa: BLE001 - never let the scheduler die with outputs on
                self.log.exception("motion scheduler error")
                self._fault(f"motion scheduler error: {e}", "internal_error")
                continue
            finally:
                self._flush()
            if writes:
                await self._write(writes)

    def _tick(self, now: float, period: float) -> list[tuple[int, float]]:
        with self._lock:
            self.ticks += 1
            dt = period if self._last_tick is None else now - self._last_tick
            if dt > STALL_FACTOR * period:
                shift = dt - period  # the loop stalled: pause the motion rather than jump
                for j in self._joints.values():
                    if j.segment is not None:
                        j.segment.shift(shift)
                for job in self._jobs:
                    job.phase_end += shift
                self.log.debug("motion tick late by %.3fs; trajectory paused", shift)
                dt = period
            dt = max(dt, 1e-6)
            self._last_tick = now

            # Leases that ended take their jobs with them.
            for job in list(self._jobs):
                if job.lease_mode == "own" and (self._lease is None or not self._lease.active):
                    self._finish_job(job, False, brake=True, reason="motion lease lost")
                elif job.lease_mode == "borrowed" and job.borrowed is not None and not job.borrowed.active:
                    self._finish_job(job, False, brake=True, reason=f"lease of {job.borrowed.owner} ended")

            # 1. integrate joints along their segments (clamped + rate limited)
            for j in self._joints.values():
                seg = j.segment
                if seg is None:
                    j.velocity = 0.0
                    continue
                if seg.done(now):
                    desired, vel = seg.end, 0.0
                    j.segment = None
                else:
                    desired, vel = seg.position(now), seg.velocity(now)
                desired = j.spec.clamp(desired)
                max_step = j.spec.speed_limit * dt * RATE_LIMIT_MARGIN + 1e-6
                new = limit_step(j.angle, desired, max_step)
                if new != desired:
                    self.log.debug("rate limiter engaged on %s", j.spec.name)
                    vel = (new - j.angle) / dt
                    if j.segment is None:  # finished but not reached: catch up gently
                        dur = plan_duration(desired - new, j.spec.speed_limit * 0.5, j.spec.accel_limit, 0.1)
                        j.segment = MinJerkSegment(new, desired, now, dur)
                j.angle = new
                j.velocity = vel

            # 2. advance running jobs, 3. start pending ones whose joints are free
            for job in list(self._jobs):
                if job.state == "running":
                    self._advance(job, now)
            for job in list(self._jobs):
                if job.state == "pending" and self._joints_free(job.joints):
                    self._start(job, now)

            # 4. outputs for energised joints whose pulse changed
            writes: list[tuple[int, float]] = []
            for j in self._joints.values():
                if not j.energised:
                    continue
                pulse = j.spec.angle_to_pulse(j.angle)
                at_rest = j.segment is None
                if j.last_pulse is None or abs(pulse - j.last_pulse) >= PULSE_EPS_US or (at_rest and pulse != j.last_pulse):
                    writes.append((j.spec.channel, pulse))
                    j.last_pulse = pulse

            moving = self._has_work()
            if moving and now - self._last_positions >= 1.0 / float(self.cfg.positions_hz):
                self._queue_positions(True)
                self._last_positions = now
            elif not moving and self._was_moving:
                self._queue_positions(False)
            self._was_moving = moving
            return writes

    def _joints_free(self, joints: frozenset[str]) -> bool:
        return all(self._joints[n].segment is None and self._joints[n].job_id is None for n in joints)

    def _start(self, job: _Job, now: float) -> None:
        job.state = "running"
        settle = False
        for name in job.joints:
            j = self._joints[name]
            j.job_id = job.id
            if not j.energised:
                if not j.known:
                    j.angle = j.spec.neutral  # unknown -> assume neutral and energise there
                j.velocity = 0.0
                j.energised = True
                j.known = True
                j.last_pulse = None
                settle = True
        if job.kind == "gesture":
            self._gesture_job = job
            job.events = True
            self._queue(Topics.MOTION_GESTURE_STARTED, {"gesture": job.name})
        if settle and self.cfg.settle_seconds > 0:
            job.phase, job.phase_end = "settle", now + float(self.cfg.settle_seconds)
        else:
            self._begin_step(job, 0, now)

    def _advance(self, job: _Job, now: float) -> None:
        for _ in range(len(job.steps) + 2):  # bounded: zero-length steps advance in the same tick
            if job.state != "running":
                return
            if job.phase in ("settle", "hold"):
                if now < job.phase_end:
                    return
                self._begin_step(job, job.index + 1, now)
            elif job.phase == "move":
                step = job.steps[job.index]
                if any(self._joints[n].segment is not None for n in step.targets if n in self._joints):
                    return
                if step.hold > 0:
                    job.phase, job.phase_end = "hold", now + step.hold
                else:
                    self._begin_step(job, job.index + 1, now)
            else:
                return

    def _begin_step(self, job: _Job, index: int, now: float) -> None:
        job.index = index
        if index >= len(job.steps):
            self._finish_job(job, True, brake=False)
            return
        step = job.steps[index]
        duration = max(0.0, float(step.min_duration))
        moves: list[tuple[_Joint, float, float]] = []
        for name, target in step.targets.items():
            j = self._joints.get(name)
            if j is None or j.job_id != job.id:
                continue
            target = j.spec.clamp(target)
            distance = target - j.angle
            if abs(distance) <= _EPS_DEG:
                continue
            vmax = j.spec.speed_limit * max(1e-3, min(1.0, step.speed)) * float(self.cfg.speed_scale)
            if step.speed_cap is not None:
                vmax = min(vmax, step.speed_cap)
            duration = max(duration, plan_duration(distance, vmax, j.spec.accel_limit))
            moves.append((j, j.angle, target))
        if moves:
            duration = max(duration, float(self.cfg.min_move_seconds))
            for j, start, target in moves:
                j.segment = MinJerkSegment(start=start, end=target, t0=now, duration=duration)
        job.phase, job.phase_end = "move", now + duration

    def _on_idle(self) -> None:
        secs = float(self.cfg.release_when_idle_seconds)
        if secs > 0 and self._loop is not None and self._release_handle is None:
            if any(j.energised for j in self._joints.values()):
                self._release_handle = self._loop.call_later(secs, self._idle_release)

    def _idle_release(self) -> None:
        self._release_handle = None
        with self._lock:
            if self._has_work():
                return
            for j in self._joints.values():
                j.energised = False  # still `known`: next move re-energises at the same angle
                j.last_pulse = None
        self._all_off_now()
        self.log.info("servos released after %.1fs idle", self.cfg.release_when_idle_seconds)

    def _cancel_release_timer(self) -> None:
        if self._release_handle is not None:
            self._release_handle.cancel()
            self._release_handle = None

    # ================================================================ outputs
    def _outputs_allowed(self) -> bool:
        return not self._estop and not self._closing and (not self._physical or self._armed)

    async def _write(self, writes: list[tuple[int, float]]) -> None:
        try:
            if getattr(self._driver, "blocking_io", False):
                assert self._loop is not None
                if self._executor is None:
                    self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                                           thread_name_prefix="motion-io")
                await self._loop.run_in_executor(self._executor, self._write_pulses, writes)
            else:
                self._write_pulses(writes)
        except Exception as e:  # noqa: BLE001 - I2C failure must latch, never crash
            self._fault(f"servo driver error: {e}", "driver_error")

    def _write_pulses(self, writes: list[tuple[int, float]]) -> None:
        with self._io_lock:
            if not self._outputs_allowed() or self._driver_closed:
                return
            for channel, pulse in writes:
                self._driver.set_pulse_us(channel, pulse)

    def _all_off_now(self) -> None:
        if self._driver_closed:
            return
        try:
            with self._io_lock:
                self._driver.all_off()
        except Exception as e:  # noqa: BLE001
            msg = f"could not switch servo outputs off: {e}"
            if self._error and msg not in self._error:
                self._error = f"{self._error}; {msg}"
            elif not self._error:
                self._error = msg
            self.log.error("%s - use the hardware power switch / OE pin", msg)

    def _fault(self, message: str, source: str) -> None:
        self.log.error("%s -> emergency stop", message)
        self._error = message
        self.ctx.bus.publish(Topics.SYSTEM_ERROR, {"module": "motion", "error": message, "recoverable": True},
                             source="motion")
        self.emergency_stop(source)
        self._error = self._error or message

    # ================================================================ helpers
    def _dispatch(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run fn on the runtime loop (inline when already there or when no loop is running)."""
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running() or threading.get_ident() == self._loop_thread:
            fn(*args)
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            fn(*args)

    def _queue(self, topic: str, data: dict[str, Any]) -> None:
        self._outbox.events.append((topic, data))

    def _queue_positions(self, moving: bool) -> None:
        self._queue(Topics.MOTION_POSITIONS,
                    {"joints": {n: round(j.angle, 2) for n, j in self._joints.items()}, "moving": moving})

    def _flush(self) -> None:
        with self._lock:
            events, self._outbox.events = self._outbox.events, []
        for topic, data in events:
            self.ctx.bus.publish(topic, data, source="motion")

    def _reason(self) -> str:
        if self._config_error:
            return f"motion disabled: invalid configuration: {self._config_error}"
        if self._physical:
            base = "physical motion ARMED" if self._armed else \
                "physical motion enabled but NOT armed - a parent must arm motion in the dashboard"
        else:
            base = self._sim_reason
        if self._estop:
            base += f"; emergency stop latched ({self._estop_source}) - reset required"
        return base

    def _update_health(self) -> None:
        if self._closing:
            return
        if self._config_error:
            self.set_status(ServiceStatus.FAILED, "invalid motion configuration")
        elif self._estop and self._error:
            self.set_status(ServiceStatus.DEGRADED, self._error)
        elif self._backend_error:
            self.set_status(ServiceStatus.DEGRADED, self._sim_reason)
        elif self._estop:
            self.set_status(ServiceStatus.RUNNING, f"emergency stop latched ({self._estop_source})")
        else:
            self.set_status(ServiceStatus.RUNNING, "physical" if self._physical else "simulation only")


__all__ = [
    "MotionController", "BackendDecision", "decide_backend", "default_driver_factory", "LEASE_OWNER", "ARMED_KEY",
    "DriverError",
]
