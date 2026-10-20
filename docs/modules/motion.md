# Motion module (`littlebot/motion/`)

Servo control for head turn and both arms: three MG90S servos on a PCA9685 over I2C.
**Simulated by default.** Physical output needs two independent keys (config + parent
arming); see [MOTION_SAFETY.md](../MOTION_SAFETY.md), which also covers wiring, power,
calibration and the verification checklist.

## Files

| File | Contents |
|------|----------|
| `__init__.py` | `create_service(ctx)` -> `MotionController` |
| `controller.py` | `MotionController` (`MotionAPI` + `Service`), backend gate `decide_backend()`, scheduler, jobs, leases, stop logic |
| `drivers.py` | `ServoDriver` protocol; `PCA9685Chip` (register-level emulation); `MockSMBus` (smbus2 look-alike with fault injection); `_PCA9685Base` (register programming); `MockPCA9685` (simulation driver); `PCA9685Driver` (real, lazy `smbus2`) |
| `joints.py` | `JointSpec` (limits, angle <-> pulse, normalised positions), validation, calibration file load/save |
| `interpolation.py` | minimum-jerk segments, duration planning under speed + acceleration limits, brake segments, rate limiter |
| `gestures.py` | gesture library (keyframes in normalised positions) + validation |
| `scripts/calibrate_servos.py` | interactive one-servo-at-a-time calibration (`--simulate` works anywhere) |
| `scripts/servo_test_pattern.py` | slow sweep within limits (simulated by default, `--fast` for no waiting) |
| `config/calibration.example.yaml` | calibration file format (the real file `config/calibration.yaml` is git-ignored) |

## Backend selection (key 1) and arming (key 2)

```
decide_backend(cfg.motion)                       pure, no I/O, never imports smbus2
  backend != "pca9685"            -> simulation  "motion.backend is 'mock'"
  physical_enabled == false       -> simulation  "motion.physical_enabled is false"
  calibration file missing        -> simulation  "... not found - run scripts/calibrate_servos.py"
  calibration invalid / partial   -> simulation  "calibration invalid: ..." / "missing joints"
  else                            -> try PCA9685Driver in start() (to_thread)
                                     failure -> simulation "PCA9685 unavailable: ..." (DEGRADED)

physical driver active:
  motion.physical_armed == false  -> every command refused, outputs off ("NOT armed")
  armed                           -> commands run; disarm = immediate all_off + halt
```

In simulation the arming setting does not gate anything. `status()` reports
`simulation_only` and `reason`. A valid calibration file is applied in simulation too, so the
simulator shows calibrated limits. An invalid one is ignored and reported in
`status().calibration.error`.

## Angle conventions

* **Joint angle** (config, API, status, events): degrees 0..180.
* **Servo angle** = joint angle, or `180 - joint angle` if `inverted`.
* **Pulse** = `min_pulse_us + servo_angle / 180 * (max_pulse_us - min_pulse_us)`.
* Hard clamps everywhere: commands -> `[min_angle, max_angle]`; pulses ->
  `[min_pulse_us, max_pulse_us]` and the absolute 500..2500 us (also enforced in the driver).
* **Normalised position** (gestures, `status().joints[*].position`): 0 = neutral, +1 = the
  limit named by `positive_toward`, -1 = the other limit. For the arms +1 = fully raised; for
  the head +1 = looking to the robot's LEFT. Default config: `right_arm.positive_toward: min`
  (its angle decreases as it rises). UIs should draw from `position`, not raw angles.

Default joints (`core/config.py::_default_servos`):

| joint | ch | neutral | min..max | max speed | inverted | positive_toward |
|-------|----|---------|----------|-----------|----------|-----------------|
| head | 0 | 90 | 50..130 | 60 deg/s | no | max |
| left_arm | 1 | 20 | 10..150 | 90 deg/s | no | max |
| right_arm | 2 | 160 | 30..170 | 90 deg/s | yes | min |

All joints: `max_accel_dps2: 200`, pulses 500..2500 us. Hard ceilings: 180 deg/s, 720 deg/s^2.

## Trajectories

* Every move is a **minimum-jerk** segment (`10t^3 - 15t^4 + 6t^5`), which starts and ends at
  zero velocity and zero acceleration. Duration = the smallest that keeps the peak velocity
  (`1.875 D/T`) <= max speed x keyframe speed x `speed_scale` (x optional `speed_dps` cap)
  and the peak acceleration (`5.77 D/T^2`) <= max acceleration, and never below
  `min_move_seconds`. All joints in a keyframe arrive together.
* **First move / unknown positions**: at start and after an e-stop the joints are *unknown*
  and assumed to be at neutral. The first command energises each joint it uses at that
  assumed angle, holds `settle_seconds` (0.5 s), then eases to the target. Joints a command
  does not use stay de-energised.
* **Safety nets** in every tick: clamp to limits, then a rate limiter (`max_speed x dt x 1.05`).
  If the loop stalls for more than 3 periods, the trajectory is paused (time-shifted)
  instead of jumping. Tests check that the planner alone never triggers the limiter.
* **Smooth stop** (`cancel`, supersede, lease loss): a `BrakeSegment` decays the current
  velocity to zero along a smoothstep with deceleration <= max acceleration. If the running
  segment would stop sooner, it is left to finish.

## Scheduler and jobs

* One asyncio task on the runtime loop. While there is work it ticks at `update_hz`
  (default 50 Hz). With nothing to do it waits on an `asyncio.Event`: **zero ticks when idle**.
* A command becomes a *job* (gesture | move | neutral | homing) that owns a set of joints. A new
  job **supersedes** running or pending jobs on overlapping joints (latest wins). Those jobs
  brake smoothly and their awaiters get `False`. The new job starts once its joints are at
  rest. Jobs on disjoint joints run in parallel (`move_joint("head")` alongside
  `move_joint("left_arm")`). A job is refused if an overlapping job has a higher priority.
* Pulses are written only for energised joints whose pulse changed (>= 0.5 us, plus the exact
  final value at rest), so a resting robot causes no I2C traffic. The real driver's writes run
  on a single-thread executor (`blocking_io = True`) so the loop never blocks on I2C.
* Optional `release_when_idle_seconds > 0` de-energises servos after idling (positions stay
  *known*; the next move re-energises at the same angle).

## Arbitration (`Resource.MOTION`)

Motion never preempts another behaviour's lease:

| MOTION holder | `run_gesture` / `move_joint` / `neutral` (default priority `Priority.ACTIVITY`) |
|---------------|---------------------------------------------------------------------------------|
| nobody / motion itself | take (or upgrade) motion's own lease `owner="motion"`; release when idle; if preempted (e.g. an activity or `preempt_all`) the motion brakes and returns `False` |
| the caller (`owner=` matches) | run under the caller's lease |
| another owner with **higher** priority | **refused** (`False`) |
| another owner with equal/lower priority | run on its behalf (typically the activity that reserved the motors is the caller); stops when that lease ends |

The STOP_ALL return to neutral takes no lease (it is part of stopping), and any new command
supersedes it.

## Stop semantics

| | motion | outputs | latched |
|-|--------|---------|---------|
| `cancel()` | brake where it is (STOP_ALL homing is not aborted) | energised | no |
| `Topics.STOP_ALL` | cancel all, then return energised joints to neutral at `homing_speed_factor` (0.4) if `stop_all_returns_to_neutral` (default true) | energised | no |
| `emergency_stop(source)` | immediate halt; positions become unknown | `all_off()` synchronously in the caller's thread, before anything else | yes |
| `reset_emergency_stop()` | only when idle (else `False`); re-probes the chip after driver errors (else `False`) | stay off until the next command | cleared |
| disarm (`motion.physical_armed` -> false) | immediate halt (physical only) | all off | until re-armed |
| driver/I2C exception | -> `emergency_stop("driver_error")`, `system.error` published, health DEGRADED | all off attempted | yes |

The writer checks the e-stop flag inside the same I/O lock that `all_off()` uses, so no pulse can
be written after an e-stop's `all_off()`.

## API

`MotionController` implements `core.interfaces.MotionAPI`:

| method | notes |
|--------|-------|
| `gestures() -> list[str]` | `wave, look_left, look_right, raise_arms, celebrate, dance, shake, neutral` |
| `async run_gesture(name, *, priority=None, owner=None) -> bool` | `False` if unknown, refused (estop / not armed / lease / config) or cancelled |
| `async move_joint(joint, angle, *, speed_dps=None, priority=None, owner=None) -> bool` | angle clamped to limits; unknown joint / non-finite angle / speed <= 0 -> `False` |
| `async neutral(*, priority=None, owner=None) -> bool` | all joints to neutral at 60 % speed |
| `cancel()` | thread-safe |
| `emergency_stop(source="unknown")` | thread-safe, synchronous all-off, latched |
| `reset_emergency_stop() -> bool` | thread-safe |
| `status() -> dict` | thread-safe, see below |

`priority` and `owner` are optional keyword extensions to the protocol. Extras:
`gesture_info()` (`[{name, description}]`), `joint_names()`, `specs`, `driver`, `physical`,
`health()`.

`status()`:
```
{backend: "mock"|"pca9685", configured_backend, physical_enabled, armed, estop, estop_source,
 moving, gesture, simulation_only, reason, error, outputs_energised, gestures, update_hz,
 calibration: {file, loaded, error}, driver: {...describe()},
 joints: {name: {angle, target, min, max, neutral, channel, known, energised, pulse_us,
                 position, positive_toward}}}
```

## Events

| topic | payload | when |
|-------|---------|------|
| `motion.positions` | `{joints: {name: angle}, moving}` | ~`positions_hz` (10 Hz) while moving (including holds within a gesture), once when it stops, at start and on e-stop/disarm |
| `motion.gesture_started` | `{gesture}` | a gesture job begins (after any queued braking) |
| `motion.gesture_finished` | `{gesture, cancelled}` | completed, superseded, cancelled, preempted or e-stopped |
| `motion.estop` | `{active, source}` | latch set (`active: true`) / reset (`active: false, source: "reset"`) |
| `system.error` | `{module: "motion", error, recoverable: true}` | driver/I2C failure |

Subscribed: `system.stop_all`, `settings.changed` (`motion.physical_armed`).

## Configuration (`motion:`)

`backend`, `physical_enabled`, `i2c_bus`, `i2c_address`, `pwm_frequency` (40-60 Hz),
`update_hz` (5-200), `servos` (per joint: `channel, neutral, min_angle, max_angle,
max_speed_dps, max_accel_dps2, inverted, min_pulse_us, max_pulse_us, positive_toward`),
`calibration_file`, `positions_hz`, `speed_scale` (0-1], `min_move_seconds`,
`settle_seconds`, `stop_all_returns_to_neutral`, `homing_speed_factor`,
`release_when_idle_seconds`. Invalid values do not crash the app: motion is disabled,
health is FAILED, and `status().reason` explains why.

## Testing

* `tests/motion/` (no hardware): register-level checks of the emulated chip against the
  datasheet formulas (prescale 121 @ 50 Hz, counts 102/205/307/410/512 for 500-2500 us);
  the real `PCA9685Driver` code runs against `MockSMBus` (init order, verification, close,
  probe, fault injection); planner limits; gestures; controller end-to-end with
  `core.clock.ManualClock` (simulated time, so a 10 s gesture runs in milliseconds).
* Inject a clock and a driver factory: `MotionController(ctx, clock=ManualClock(),
  driver_factory=lambda cfg: PCA9685Driver(bus=MockSMBus({0x40: PCA9685Chip()})))` exercises
  the full physical path (arming, executor I/O, I2C faults) without hardware.
* `smbus2` is never imported unless every key-1 gate is open (tested with an import blocker).

## Verified here vs needs hardware

Verified in software: register programming and timing maths (emulated chip), gating, arming,
limits, easing, stop semantics, fault latching, idle behaviour. **Not verified**: the
real chip/bus, actual servo pulse range and direction (calibration), mechanical limits,
current draw, heat, how a servo behaves on the first energise, and everything in
MOTION_SAFETY.md section 8.
