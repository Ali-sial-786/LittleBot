# Movement: head, arms, body language and (later) wheels

No motors are fitted yet. Everything below runs on the simulated servos today and is ready for the
hardware: the arms and a pan/tilt head first (servos on the PCA9685), wheels in a later phase.
**Physical motion stays off** until you go through [MOTION_SAFETY.md](MOTION_SAFETY.md): the
config switches, a calibration file for every joint, and a grown-up arming the motors in the
dashboard after every start. Nothing here loosens those rules.

## What is ready

| Part | Where | Today, without motors |
|---|---|---|
| Pan/tilt head + two arms | `littlebot/motion/` (controller, joints, gestures) | simulated servos; the dashboard's Motion page and the desktop simulator panel draw them |
| `look_at(x, y)`: the head follows a person | `motion/look.py` + `MotionController.look_at` | the simulated head turns toward a face the camera sees |
| Conversation gestures | `motion/gestures.py` | nod, head shake, curious tilt, thinking look, shrug, perk up, point at the screen, hello / goodbye waves, happy wiggle, sad droop, tiny idle shift (+ the old wave, look left/right, raise arms, celebrate, dance, shake) |
| Body language | `motion/body.py` (started by `app/behaviours.py`) | **the face part works now**: eyes look at you while listening, glance up while thinking, nod at the end of sentences |
| Wheels | `motion/drive.py` | a simulated base with a pose (x, y, heading); off (`motion.drive.enabled: false`) and not used by anything yet |

## The joints

| Joint | What it does | + direction (normalised +1) | Default channel / range |
|---|---|---|---|
| `head` | head **pan**: turn left / right (the name stays `head` so old configs and activities keep working) | the robot's LEFT | 0 / 50..130°, neutral 90 |
| `head_tilt` | head **tilt**: look up / down | UP | 3 / 70..110°, neutral 90 |
| `left_arm` | left shoulder | raised | 1 / 10..150°, neutral 20 |
| `right_arm` | right shoulder (mounted mirrored) | raised | 2 / 30..170°, neutral 160 |

Every joint has its own speed and acceleration limit (`max_speed_dps`, `max_accel_dps2`), moves on a
minimum-jerk curve (starts and stops gently, never a jerk), is hard-clamped to `min_angle..max_angle`
and has **soft limits** `soft_margin_deg` (2°) inside those that look_at and body language never
cross. Gestures are written in normalised positions (0 = neutral, ±1 = the limits) and stay within 85 %
of each range at reduced speed, so they follow whatever calibration finds.

A robot can have any subset of these. Without a tilt servo, list the other three under
`motion.servos` (it replaces the whole list): gestures then simply leave the tilt out, `nod` is refused
(the face still nods), and look_at uses the pan only.

## Body language

`motion.body_language: true` (default). It listens to the conversation and moves the eyes and, once
there are servos, the head and arms:

| When | Face (works now) | Movement |
|---|---|---|
| listening / the person talks | eyes look at the person (the face the camera tracks, else the centre) | `perk_up` (head lifts a little, arms twitch), then the head follows the person |
| the person finished | a small face nod ("got it") | |
| thinking | eyes glance up and to the side (again every 3 s) | `look_up_think` |
| speaking | a face nod at the end of each sentence and of the answer | `nod_small`; a sentence's expression: celebrating -> `celebrate`, happy / excited -> `arm_wiggle`, curious -> `tilt_curious`, sad -> `droop`, confused -> `head_shake`, surprised -> `perk_up` |
| a card appears on the screen | | `point_screen` (an arm points at it) |
| you talk over LittleBot | eyes look at you | stops, smoothly back to neutral |
| the chat ends | eyes go back to their own wandering | neutral pose |
| nothing going on | (the eyes follow faces as before) | the head follows a face it sees; a tiny `idle_shift` about every `idle_motion_seconds` |

Rules it keeps: at most one gesture (and one face nod) per `body_language_gap_seconds` (1.5 s); its
movements run at the lowest priority (`Priority.AMBIENT`), so an activity, a game, a video, the
parent's dashboard or anyone holding the motors wins at once; nothing moves during an emergency stop;
it is quiet while LittleBot sleeps, plays a video / an activity or runs a game. Without motion it costs
nothing beyond the face calls.

It understands both voice pipelines: the new assistant's `assistant.*` events and the robot state
(`LISTENING` / `THINKING` / `SPEAKING`) that the live voice and the older chat set.

## look_at: eyes lead, the head follows

`ctx.motion.look_at(x, y)` takes the same coordinates as the face and vision (0..1, 0.5/0.5 = the
centre, x toward the screen's right = the robot's left, y down). The eyes take every small shift at
once; the head turns only when the target stays more than `follow_offset` (0.15 of the view) away for
`follow_after_seconds` (0.4 s), and then turns `gain` (80 %) of the way, so it settles in two or three
steps without overshooting. The camera sits in the head's top pod (`camera_on_head: true`), so the
person drifts back to the middle of the picture as the head turns. On the desktop simulator
(`config/simulation.yaml`) the camera does not turn with the drawn head, so there
`camera_on_head: false`: x/y are then directions from the body and the head turns to them. Look moves
never interrupt a gesture and any other command on the head replaces them.

## When the servos arrive

1. **Read [MOTION_SAFETY.md](MOTION_SAFETY.md)** and wire as it says: servos on their **own** 5-6 V
   supply (common ground with the Pi), the PCA9685 on I2C (`/dev/i2c-1`, address 0x40). Default
   channels: head pan 0, left arm 1, right arm 2, head tilt 3. Use other channels by listing all joints
   under `motion.servos` in `config/local.yaml` (example in `config/default.yaml`).
2. Stage 1, still simulated: `motion.physical_enabled: true`, `backend: mock`. Calibrate **every**
   joint, one at a time: `.venv/bin/python scripts/calibrate_servos.py --hardware --i-understand-the-risks`
   (`--joint head_tilt` for one; practise with `--simulate` first). It asks which way is "up" for the
   tilt, "left" for the pan and "raised" for the arms, and writes `config/calibration.yaml`. Physical
   motion stays off until the file covers every configured joint.
3. `.venv/bin/python scripts/servo_test_pattern.py --hardware --i-understand-the-risks --joint head_tilt`
   sweeps a joint slowly within its limits.
4. Stage 2: `motion.backend: pca9685`, restart, and arm the motors on the dashboard's Motion page
   (type ARM MOTORS; never remembered across restarts). The emergency stop (dashboard, `E` on the
   keyboard, the simulator panel) cuts every output at once and disarms.
5. Try the gestures from the Motion page, then a chat. If the head feels too lively, lower
   `motion.look.speed`, raise `follow_after_seconds`, or set `motion.body_language: false`.

If the tilt servo goes on later than the others, leave it out of `motion.servos` until then.

## Wheels (later phase)

`littlebot/motion/drive.py` has the drive base, owned by the motion controller (`ctx.motion.drive`) so
the emergency stop, the parent's STOP, arming and shutdown already cover it:

* `await drive(linear_mps, angular_dps, duration_s=None)`, `await turn(degrees)` (+ = left),
  `await forward(meters)` (negative = back), `stop()`, `status()` (pose in metres / degrees from the
  start: x forward, y left, heading + = turned left).
* Safety: refused unless `motion.drive.enabled`; real wheels also need `motion.physical_enabled` and a
  parent's arming, exactly like the servos (the simulated base needs no arming: nothing can move). Speeds
  are clamped to `max_linear_mps` / `max_angular_dps` (hard ceilings 0.5 m/s, 180°/s) and ramp within
  the acceleration limits. An open-ended `drive()` stops by itself unless it is sent again within
  `watchdog_seconds` (1 s); no command runs longer than `max_duration_seconds`; a stalled control loop
  stops the wheels. The e-stop cuts them at once and latches.
* It publishes `motion.drive` events and the Motion page shows the pose once it is enabled.

To fit real wheels: write `MotorHatWheels` in `drive.py` (its docstring lists what to implement:
open the HAT, `set_velocity` -> wheel speeds -> PWM, a keep-alive with `CommandWatchdog`, `stop`,
`close`), then set `motion.drive.backend: motor_hat` and `enabled: true`. Until it is written, that
backend falls back to the simulation.

To let the voice drive later: add a skill tool (`littlebot/skills/`) that calls
`ctx.motion.drive.forward(...)` / `turn(...)` with small fixed steps (e.g. "come closer" = 0.2 m), and
check `ctx.motion.drive.status()["reason"]` to tell the person why it can't move. Keep the steps short
and never drive while nobody is watching. Nothing is wired yet on purpose.

## Config keys (`motion:`)

| Key | Default | Meaning |
|---|---|---|
| `servos.<joint>` | see the table above | `channel, neutral, min_angle, max_angle, max_speed_dps, max_accel_dps2, inverted, min_pulse_us, max_pulse_us, positive_toward, soft_margin_deg` |
| `body_language` | `true` | the body-language module on / off |
| `body_language_gap_seconds` | `1.5` | at most one gesture (and one face nod) this often |
| `idle_motion_seconds` | `90` | a tiny idle shift about this often (0 = never) |
| `look.camera_on_head` | `true` | camera in the head (x/y relative to where it points) or fixed to the body |
| `look.fov_h_deg` / `fov_v_deg` | `70` / `45` | the camera's field of view |
| `look.follow_offset` / `follow_after_seconds` | `0.15` / `0.4` | when the head follows |
| `look.gain` / `settle_seconds` / `speed` | `0.8` / `0.6` / `0.5` | how far, how often, how fast |
| `drive.enabled` | `false` | wheels off |
| `drive.backend` | `mock` | `mock` or `motor_hat` (placeholder) |
| `drive.max_linear_mps` / `max_angular_dps` | `0.15` / `60` | speed limits |
| `drive.max_linear_accel_mps2` / `max_angular_accel_dps2` | `0.3` / `180` | ramps |
| `drive.cruise_linear_mps` / `cruise_angular_dps` | `0.10` / `45` | speeds for `forward` / `turn` |
| `drive.watchdog_seconds` / `max_duration_seconds` | `1.0` / `10` | auto-stop |
| `drive.update_hz` / `publish_hz` / `wheel_base_m` | `20` / `5` / `0.12` | control loop, events, geometry |

The arming and calibration rules (`backend`, `physical_enabled`, `calibration_file`,
`motion.physical_armed`) are unchanged: see MOTION_SAFETY.md and docs/modules/motion.md.

## Trying it on the simulator

* `python -m littlebot` (desktop): the side panel draws the head (pan and tilt) and arms. On the
  dashboard (http://127.0.0.1:8765), **Motion** has a button per gesture, a slider per joint
  (head_tilt included) and the emergency stop; **Simulator** lets you place a pretend face (the head
  follows it) and pretend to speak (listening, thinking and speaking drive body language).
* `scripts/calibrate_servos.py --simulate` and `scripts/servo_test_pattern.py` work without hardware.
* Tests (no hardware, no sleeping):
  `.venv/bin/python -m pytest -q tests/motion` - `test_motion_head_tilt.py` (limits, easing, subsets),
  `test_motion_look.py` (mapping and the eyes-lead timing), `test_motion_new_gestures.py`,
  `test_motion_body.py` (bus events -> face / gestures, rate limits, e-stop),
  `test_motion_drive.py` (pose, limits, watchdog, gate).
