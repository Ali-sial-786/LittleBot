# Module: face (animated eyes, display loop, simulator window)

`littlebot/face/` is LittleBot's visual identity: two large, rounded, softly glowing eyes on
a dark screen that blink, look around, follow faces, change expression and react to
speech. It also owns the main-thread pygame window and the desktop simulator panel.

```
littlebot/face/
  __init__.py     create_service(ctx) -> FaceController   (no pygame import)
  expressions.py  expression parameter sets (pure data), state -> default expression
  effects.py      one-shot animation curves + particle overlays as pure functions of time
  animator.py     FaceAnimator: all time-based logic, deterministic with a seeded RNG (no pygame)
  frame.py        FaceFrame / EyeFrame / MouthFrame / Sprite: animator -> renderer contract, layout
  renderer.py     FaceRenderer: pygame drawing with LRU caches (imports pygame)
  controller.py   FaceController(Service): FaceAPI + track_gaze, command queue, headless ticker
  display.py      run_display(robot, demo=False): window, input, overlays, fps policy
  sim_panel.py    desktop side panel: drawn robot (head/arms from motion.status()), E-STOP, status
  demo.py         DemoSequence: scripted tour of every expression / state / animation / gaze
scripts/face_preview.py   standalone preview + --screenshot-dir PNG export
scripts/benchmark_face.py update + render cost per frame (table or --json)
tests/face/               158 tests (pure logic, controller, renderer, display with SDL dummy driver)
```

Pipeline: **API call -> command queue -> FaceAnimator.update(dt) -> FaceFrame -> FaceRenderer**.
Everything that moves is computed by the animator from `dt` alone, so it is unit-testable
without a window; the renderer only turns plain data into pixels.

## Public API (`core.interfaces.FaceAPI` + extras)

All methods are synchronous, cheap, thread-safe and never block: they validate their
arguments and append a command to a bounded deque (512; oldest dropped with a warning).
Commands are applied under a short lock by whoever advances or inspects the face
(`update()`, `frame()`/`render()`, `status()`), so `status()` always reflects every command
issued before it. Unknown names / non-finite numbers are logged as warnings and ignored.

| Method | Notes |
|---|---|
| `set_expression(name, *, hold=None)` | one of `EXPRESSIONS`; smooth transition (`face.transition_seconds`). `hold` seconds -> reverts to the default expression of the current face state. A new expression or state cancels the hold. |
| `look_at(x, y, *, hold=None)` | explicit gaze, 0..1 screen coords (0.5/0.5 centre), clamped. Wins over tracking for `hold` (default `face.look_hold_seconds`). |
| `track_gaze(x, y)` | **extra**: soft camera-tracking target (used by `app/behaviours.py`); ignored while an explicit look holds; expires 1.2 s after the last update. |
| `clear_gaze()` | drop explicit + tracking targets -> idle wandering |
| `blink(double=False)` | immediate blink (not while eyes closed / asleep / hidden) |
| `close_eyes()` / `open_eyes()` | peekaboo: lids close and stay closed (curved "‿" line); reopening pops with a little boing |
| `set_state(state)` | one of `FACE_STATES`; switches to that state's default expression (same state = no-op, keeps the current expression) |
| `play_animation(name)` | one of `FACE_ANIMATIONS`, layered on the expression; replaying restarts it |
| `status()` | `{expression, state, gaze: {x, y}, eyes_closed, fps, visible}` plus `talking, listening, animations, display`. `fps` is the measured render rate (0.0 when headless). `eyes_closed` is true for `close_eyes()` and while sleeping. |

Controller extras for the display / tools: `update(dt)`, `frame()`, `render(surface, rect=None)`,
`attach_display()` / `detach_display()`, `target_fps()`, `wants_high_fps()`, `start_ticker()` /
`stop_ticker()`, `health()`.

Bus: publishes `face.expression {expression}` when the expression name changes (including
hold reverts), `face.visible {visible}` when entering/leaving `hidden`, `face.blink {double}`
on every blink (source `"face"`). Subscribes to `audio.speech_started`,
`audio.speech_finished`, `audio.level {level}`, `audio.listening_started/stopped`.

Lifecycle: `start()` runs a ~10 Hz daemon ticker that calls `update()` so blinks, holds and
status work headless (tests, `--headless`, dashboard-only). `run_display` calls
`attach_display()` and drives `update()` itself; the ticker then idles. `stop()` stops the
ticker (idempotent). Note: `core.service.Service` stores lifecycle state in `self.status`,
which would shadow `status()`; like audio/media the controller keeps it in `service_status`.

## Expressions, states, animations

| Expression | Look |
|---|---|
| neutral | tall rounded "pill" eyes, glossy gradient, double highlight |
| happy | "^ ^" crescents (lower edge curved up) |
| excited | bigger eyes with a slight crescent, bouncy hop, twinkling sparkles |
| curious | left eye bigger + raised, right eye smaller with a tilted lid, gentle sway, glance to the side |
| surprised | wide round eyes, brighter glow, eyes slightly apart |
| sleepy | heavy arched lids (half-moon eyes), dimmer, looking down, slow heavy blinks, zzz |
| sad | outer-drooping arched lids, bluer tint, looking down |
| thinking | eyes up and to the side, one lid lowered, "..." dots bouncing below |
| listening | slightly bigger attentive eyes with a head-tilt, soft pulsing frame around the screen |
| speaking | friendly eyes + small mouth (closed smile / "D" mouth with tongue) moving with speech |
| confused | asymmetric eyes, sway, floating "?" |
| playful | wink-ish (one crescent, one big eye), bounce and sway |
| affectionate | soft pink crescents, rosy cheeks, floating hearts |
| celebrating | big golden-tinted crescents, cheeks, sparkles + confetti rain, bouncing |

Face states (default expression): idle (neutral), listening (listening), thinking (thinking),
speaking (speaking), sleeping (sleepy, eyes closed as gentle "‿" arcs, slow deep breathing,
zzz), error (confused with a soft lavender tint: never red or scary), hidden (fades to a
calm dark screen with a small dim "play" glyph in the corner while a video owns the screen).

One-shot animations: bounce (squash, hop, stretch, land), wiggle, nod, shake, spin_eyes
(dizzy roll), sparkle, hearts, zzz, confetti, peek (duck down with eyes shut, rise with a
bounce and pop open wide: "peek-a-boo!").

Always-on life: randomised blinks (`face.blink_interval`, per-expression rate),
double blinks (`face.double_blink_chance`), blinks with some big saccades, idle gaze
wandering with micro-saccades (`face.idle_gaze`), critically-damped gaze springs (snappy
saccades, smooth tracking), perspective (the eye nearer the gaze direction grows a
little), breathing (`face.breathing`), a speech pulse on the eyes and mouth. While speaking
the mouth follows `audio.level` events; if none arrive for 0.25 s it synthesises a
plausible syllable rhythm. A missing `speech_finished` is covered by a safety timeout
(max(8 s, 1.5 x estimate + 4 s), extended by level events).

## Display loop (`run_display(robot, demo=False)`)

* Runs on the main thread until the window is closed / Q / Esc (calls
  `robot.request_shutdown`) or `robot.shutdown_event` is set; then returns (`main.py`
  calls `robot.shutdown()`). Only `pygame.display` is quit, never the mixer.
* Renders natively at `display.width x display.height` (480x320) and scales smoothly to
  `display.scale` (windowed) or to the screen (fullscreen, aspect preserved, black borders).
* Frame rate: `display.fps` while anything animates, `display.idle_fps` when calm, 5 fps
  while hidden (`controller.target_fps()`); pygame's `Clock.tick` sleeps in between.
* If the display cannot open (no X/Wayland/KMS, bad `SDL_VIDEODRIVER`) or frames keep
  failing (30 in a row) it logs a clear error and falls back to waiting on
  `robot.shutdown_event`; the robot keeps running headless.
* `display.hide_cursor`, `display.show_debug` (start with the overlay),
  `display.show_sim_panel` (desktop simulator panel; also means "simulator mode" for clicks).
* Touch: when not in simulator mode (the robot's own screen), tapping the face makes
  LittleBot look at the finger, smile, bounce and giggle (`audio.play_sound("giggle")`),
  with a 1.5 s cooldown. Disable with `TAP_REACTION_ENABLED = False` in `face/display.py`.

### Keyboard / mouse (press **H** in the window for this list)

| Key | Action |
|---|---|
| 1 2 3 4 5 6 7 8 9 0 | neutral, happy, excited, curious, surprised, sleepy, sad, thinking, listening, confused |
| J K L V | playful, speaking, affectionate (love), celebrating (victory) |
| Shift + 1..0 | bounce, wiggle, nod, shake, spin_eyes, sparkle, hearts, zzz, confetti, peek |
| F1..F7 | face state idle, listening, thinking, speaking, sleeping, error, hidden |
| B / N | blink / double blink |
| arrows / R | move gaze (explicit look, 4 s) / reset gaze |
| O | close / open eyes |
| T | test speech (`audio.say`, or synthetic speech events when audio is missing) |
| M | demo sequence on/off (`--demo` starts it looping) |
| P | start the "peekaboo" activity (`activities.start`) - a refusal shows a brief confused face |
| C | caregiver confirm (`activities.respond("done")`) |
| SPACE | push-to-talk (`brain.converse_once()`) |
| W | wave gesture (`motion.run_gesture("wave")`) |
| X | STOP everything (`robot.stop_all("keyboard")`) |
| E | motion emergency stop (also: click the E-STOP pill in the panel) |
| G | debug overlay: fps/target, frame + render ms, robot/face state, expression, gaze, leases, animations |
| F or F11 | toggle fullscreen |
| F12 | screenshot to `artifacts/face/screenshot-*.png` |
| Q / Esc | quit (Esc first closes the help) |
| left click (sim mode) | simulated camera face at that point (`vision.simulate_face(x, y)`, normalised; vision handles mirroring). Without vision: `look_at` |
| right click (sim mode) | simulated "no face" (`vision.simulate_no_face()`) |
| Shift + click | tap reaction (giggle + bounce) for testing |

Async actions go through `robot.runtime.submit(...)`; missing subsystems (`None`) are
logged and ignored, never fatal.

## Tools

```bash
.venv/bin/python scripts/face_preview.py                 # window + simulator panel, H for keys
.venv/bin/python scripts/face_preview.py --demo --scale 1.5
.venv/bin/python scripts/face_preview.py --no-panel --scale 1   # what the Pi shows
.venv/bin/python scripts/face_preview.py --screenshot-dir artifacts/face   # PNGs, then exit
.venv/bin/python scripts/benchmark_face.py [--frames 300] [--scale 2] [--json]
```

The preview builds `make_test_context(fakes=False)`, a real `FaceController`, a tiny stub
robot and a simulated motion stub (so W waves the panel's arm); no audio/camera/AI needed.
Screenshot mode writes `expressions/`, `states/`, `animations/`, `gaze/`, contact sheets
`_expressions.png`, `_states.png`, `_animations.png`, `_gaze_and_talking.png` and the
simulator window (`window.png`, `window_help.png`, `window_debug.png`).

## Design notes

* Eyes are drawn 3x super-sampled with plain pygame primitives (rounded rect, lid polygons
  with a gentle arch, a circle cut for the happy crescent) and smooth-scaled down: clean
  anti-aliasing for any lid shape. A 40-band vertical gradient (lighter top) and a two-part
  glossy highlight give depth; glow = the shape downscaled 4x, gaussian-blurred, upscaled.
* Caches: eye surfaces keyed by the quantised shape (0.5 px / 0.01 / 0.5 deg), glows by a
  coarser key (blur hides 3 px differences), sprites by (kind, size, colour, angle bucket),
  plus background vignette (with a 1-bit dither against banding) and the listening frame.
  Surfaces are converted to the display format when a display exists.
* Colours: `face.eye_color` / `face.background_color`; expressions mix in a tint
  (sad blue, affectionate pink, celebrating gold), the error state a soft lavender.
  `face.glow: false` disables the glow (cheaper).
* Everything is deterministic for a seed: particles are pure functions of
  `(elapsed, seed)`; the animator owns one `random.Random`.

## Performance (measured 2026-10-20, Xeon 2.8 GHz container, Python 3.13, pygame-ce 2.5.8)

`scripts/benchmark_face.py --frames 200` (headless, 480x320, update + render per frame):

| Scenario | mean ms | p95 ms | cpu @ 30 fps |
|---|---|---|---|
| calm neutral (cache warm, with blinks + wandering) | 0.72 | 1.9 | 2.2 % |
| typical expressions (happy, sad, sleepy, thinking, listening ...) | 0.6 - 0.9 | 1.5 - 2.5 | 2 - 3 % |
| busiest expressions (playful, celebrating with confetti) | 2.3 - 2.9 | 5 - 6.5 | 7 - 9 % |
| one-shot animations (bounce ... spin_eyes) | 0.7 - 2.8 | 1.9 - 4.5 | 2 - 8.5 % |
| sleeping / hidden | 0.36 / 0.25 | 0.5 / 0.3 | ~1 % |
| expression changed every 0.4 s (worst case) | 3.3 | 6.5 | 9.9 % |
| overall mean of all 31 scenarios | 1.23 | | |

`--scale 2` (desktop window 960x640) adds ~2 ms/frame of smooth-scaling (desktop only;
the Pi runs at scale 1 / fullscreen 480x320).

Real-time run of the display loop (dummy video driver, scale 1, no panel, 6 s):
idle face ~22 fps average (20 fps calm, 30 during blinks / saccades) at **3.2 % of one
core**; demo sequence ~25 fps at **4.2 %**. A Raspberry Pi 5 core is roughly 2-3x slower
than this machine, so expect ~5-10 % of one core for an idle face and well under 25 % while
animating (the PERFORMANCE.md budget); measure on the Pi with the benchmark script.

## Known limitations

* Hardware unverified: the 480x320 HDMI panel, KMS/Wayland fullscreen and touch input are
  untested on the real Pi (see DISPLAY_SETUP.md). With `SDL_VIDEODRIVER=kmsdrm` pygame owns
  the screen, so the Chromium video player cannot appear on top.
* The hidden state only draws a calm screen; it does not iconify the window. The media
  module's kiosk browser is expected to sit on top while a video plays.
* Status `fps` is the measured render rate, 0.0 when headless.
