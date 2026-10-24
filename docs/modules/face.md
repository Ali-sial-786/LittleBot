# Module: face (animated eyes, display loop, simulator window)

`littlebot/face/` is LittleBot's visual identity: two big, round cartoon eyes on a soft
light-pink screen (iris-coloured eyes with a deep "ink" rim and a soft rosy shadow, big dark
pupils that look around inside them and two white catchlights) and a little mouth that is
always there: a resting smile / frown / "o" per expression that lip-syncs to the voice while
LittleBot talks. The face blinks, looks around, follows faces, changes expression and reacts
to speech. It also owns the main-thread pygame window and the desktop simulator panel.

```
littlebot/face/
  __init__.py     create_service(ctx) -> FaceController   (no pygame import)
  expressions.py  expression parameter sets (pure data), state -> default expression
  effects.py      one-shot animation curves + particle overlays as pure functions of time
  animator.py     FaceAnimator: all time-based logic, deterministic with a seeded RNG (no pygame)
  frame.py        FaceFrame / EyeFrame / MouthFrame / Sprite: animator -> renderer contract, layout,
                  eye + mouth geometry (pure functions)
  mouth.py        mouth poses: viseme shapes, level-only fallback, talking pose in the expression's mood
  renderer.py     FaceRenderer: pygame drawing with LRU caches (imports pygame)
  controller.py   FaceController(Service): FaceAPI + track_gaze, command queue, headless ticker
  display.py      run_display(robot, demo=False): window, input, overlays, fps policy
  sim_panel.py    desktop side panel: drawn robot (head/arms from motion.status()), E-STOP, status
  demo.py         DemoSequence: scripted tour of every expression / state / animation / gaze
scripts/face_preview.py   standalone preview + --screenshot-dir PNG export
scripts/benchmark_face.py update + render cost per frame (table or --json)
tests/face/               253 tests (pure logic, eye geometry, mouth, controller, renderer, display with SDL dummy driver)
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
| `status()` | `{expression, state, gaze: {x, y}, eyes_closed, fps, visible}` plus `talking, listening, animations, display, dozing`. `fps` is the measured render rate (0.0 when headless). `eyes_closed` is true for `close_eyes()`, while sleeping and once dozing has closed the eyes. |

Controller extras for the display / tools: `update(dt)`, `frame()`, `render(surface, rect=None)`,
`attach_display()` / `detach_display()`, `target_fps()`, `wants_high_fps()`, `start_ticker()` /
`stop_ticker()`, `health()`.

Bus: publishes `face.expression {expression}` when the expression name changes (including
hold reverts), `face.visible {visible}` when entering/leaving `hidden`, `face.blink {double}`
on every blink (source `"face"`). Subscribes to `audio.speech_started`,
`audio.speech_finished`, `audio.level {level, shape}` (see *Mouth and lip-sync*; a bare
`{level}` still works), `audio.listening_started/stopped`.

Lifecycle: `start()` runs a ~10 Hz daemon ticker that calls `update()` so blinks, holds and
status work headless (tests, `--headless`, dashboard-only). `run_display` calls
`attach_display()` and drives `update()` itself; the ticker then idles. `stop()` stops the
ticker (idempotent). Note: `core.service.Service` stores lifecycle state in `self.status`,
which would shadow `status()`; like audio/media the controller keeps it in `service_status`.

## Expressions, states, animations

| Expression | Look (eyes; mouth) |
|---|---|
| neutral | big round eyes, dark pupils, two white catchlights; small gentle smile |
| happy | "^ ^" crescents (lower edge curved up): closed smiling eyes, so the pupils shrink away; open "D" smile with a tongue |
| excited | bigger eyes and pupils with a slight crescent, bouncy hop, twinkling sparkles; big open smile |
| curious | left eye bigger + raised, right eye smaller with a tilted lid, gentle sway, glance to the side; small "o" |
| surprised | wide round eyes with small pupils, eyes slightly apart; round "O" |
| sleepy | heavy arched lids (pupils peek out below), dimmer, looking down, slow heavy blinks, zzz; small soft line |
| sad | outer-drooping arched lids, big soft "puppy" pupils, bluer tint, looking down; frown |
| thinking | eyes up and to the side, one lid lowered; lopsided "hmm" line with "..." dots trailing beside it |
| listening | slightly bigger attentive eyes (bigger pupils) with a head-tilt, soft pulsing frame around the screen; small attentive smile |
| speaking | friendly eyes; smile, lip-synced while the voice plays |
| confused | asymmetric eyes, sway, floating "?"; small tilted frown |
| playful | wink-ish (one crescent, one big eye), bounce and sway; smile with a cheeky tongue poking out |
| affectionate | soft pink crescents, rosy cheeks, floating hearts; sweet smile |
| celebrating | big golden-tinted crescents, cheeks, sparkles + confetti rain, bouncing; biggest open smile |

Face states (default expression): idle (neutral), listening (listening), thinking (thinking),
speaking (speaking), sleeping (sleepy, eyes closed as gentle dark "‿" arcs, slow deep breathing,
zzz, a tiny breathing "o" mouth), error (confused with a soft lavender tint: never red or scary),
hidden (fades to a calm, dim screen with a small "play" glyph in the corner while a video owns
the screen; the only time the mouth is not drawn).

Dozing (`littlebot/face/doze.py`, a look on the idle face, not the SLEEPING state: the wake word,
camera, touch and dashboard keep working). When nothing has happened for `face.doze_after_seconds`
(30) the eyes go sleepy, droop, fight it once, then close into the sleeping arcs; a "zzz" floats
up, breathing slows, the mouth becomes the little breathing "o", and every 15-25 s the eyes
flutter half open and drift shut again. The frame rate drops to 10 fps. Awake reasons (bus
topics): a voice chat (`live.call_started` .. `live.call_ended`), the wake word
(`audio.wake_word`), any robot state but IDLE, speech / sounds / listening (`audio.*`), an
activity, game, video or info card on the screen (`activity.*`, `game.started/finished`,
`media.*`, `screen.card_shown/closed/keep_awake`), something said or answered, STOP ALL, and the
face's own commands (an expression, animation, look or blink someone asked for keeps it awake at
least 5 s or its hold; `neutral` / `sleepy` don't wake it). Someone walking in
(`vision.person_present`, or one more face than in the last 10 s) or moving (the tracked face
moving 15 % of the view, `vision.gesture`) wakes it for `face.awake_on_arrival_seconds` (20): a
person who stays and keeps still lets it doze again. Waking is gentle: the lids lift over 0.8 s,
a small stretch and yawn, a soft double blink. Off with `face.doze_enabled` (dashboard: Face page,
"Dozing and sleep", which also sets `system.sleep_after_idle_minutes`, the real sleep).

One-shot animations: bounce (squash, hop, stretch, land; "wheee" mouth), wiggle, nod, shake,
spin_eyes (dizzy roll; a little "ooh"), sparkle, hearts, zzz, confetti, peek (duck down with
eyes shut, rise with a bounce and pop open wide with an open-mouthed "boo!"). The mouth moves
and turns with the face in all of them and is kept on screen (peek ducks 70 px).

Always-on life: randomised blinks (`face.blink_interval`, per-expression rate),
double blinks (`face.double_blink_chance`), blinks with some big saccades, idle gaze
wandering with micro-saccades (`face.idle_gaze`), critically-damped gaze springs (snappy
saccades, smooth tracking; the eyes move and the pupils look inside them, while the
catchlights stay put like a real reflection), perspective (the eye nearer the gaze
direction grows a little), breathing (`face.breathing`), a speech pulse on the eyes and the
lip-synced mouth (next section). A missing `speech_finished` is covered by a safety timeout
(max(8 s, 1.5 x estimate + 4 s), extended by level events).

## Mouth and lip-sync

The mouth is part of every frame (`FaceFrame.mouth`, `None` only once the face is fully
hidden). `MouthFrame` describes it as a few numbers that morph smoothly: `open` (0 = a curved
line), `width`, `smile` (-1 frown .. 1 big smile), `round` ("o"), `teeth` (a row of upper
teeth), `tongue` (a cheeky tongue poking out), `angle`, plus the spoken `shape` for
debugging. `frame.mouth_edges()` turns them into the upper / lower lip curves (pure geometry);
the renderer draws an ink lip line along them, a raspberry inside, teeth or a little tongue,
cached by the quantised shape.

* **At rest** each expression has its own mouth (`ExpressionParams.mouth`, a `MouthParams`
  that interpolates with the rest of the expression); sleeping turns it into a tiny breathing
  "o", and one-shot animations add to it (`Motion.mouth_open/smile/round`).
* **While talking** a spoken pose takes over (`talk_mix`, 0.12 s fade). Its shape comes from
  what is being heard: `audio.level` events carry `{"level": 0..1, "shape": viseme}` with the
  visemes `closed` (silence, m/b/p), `small` (most consonants, "uh"), `ah` (open vowels),
  `ee` (front vowels and s/z/sh/f: wide, teeth showing) and `oo` (rounded vowels and w). The
  pose (`face/mouth.py`: open / width / round / teeth per viseme, opening scaled by the level,
  the smile taken from the expression so a sad LittleBot talks a little sadly) is followed with
  a ~30 ms time constant: on the next frame the mouth is visibly moving, within ~100 ms it is
  there, without flicker. A bare `{level}` event (older senders) or a shape older than 0.25 s
  falls back to a shape guessed from loudness; no events at all -> a synthesised syllable rhythm.
* **Where the shapes come from** (`littlebot/audio/dsp.py`): each 20 ms block of the sound is
  classified from its loudness, its first three formants (LPC at 8 kHz; tilt-independent, so
  espeak-ng, neural TTS and the live ChatGPT voice behave alike), its energy above 3.8 kHz /
  zero-crossing rate (hissing) and a few band ratios (nasal hums). `dsp.viseme_track()`
  precomputes a whole TTS utterance (~3 ms per second of audio); `dsp.VisemeEstimator`
  does one block at a time (~0.1-0.3 ms) for live audio. Tested on synthetic adult / child
  vowels (several pitches, bright and natural spectra, 8-48 kHz) and on espeak-ng words.
* **In sync with the sound**: the audio service sends the frame for the position the speaker
  is playing (`PlaybackControl.progress`, already net of the output latency) plus
  `VISEME_LEAD_S` (40 ms, the face's own drawing delay), on change and at least every 0.15 s.
  The live voice (`chatgpt/live_audio.py`) estimates the block handed to PortAudio, stamps it
  with when PortAudio says it will be heard (DAC time, else the stream latency) and
  `LiveAudio.mouth` returns the one being heard now (60 ms early, to cover its 20 Hz polling and
  drawing); `chatgpt/live.py` publishes it.

## Display loop (`run_display(robot, demo=False)`)

* Runs on the main thread until the window is closed / Q / Esc (calls
  `robot.request_shutdown`) or `robot.shutdown_event` is set; then returns (`main.py`
  calls `robot.shutdown()`). Only `pygame.display` is quit, never the mixer.
* Renders natively at `display.width x display.height` (480x320) and scales smoothly to
  `display.scale` (windowed) or to the screen (fullscreen, aspect preserved, black borders).
* Frame rate: `display.fps` while anything animates, `display.idle_fps` when calm, 10 fps
  while dozing, 5 fps while hidden (`controller.target_fps()`); pygame's `Clock.tick` sleeps in between.
* If the display cannot open (no X/Wayland/KMS, bad `SDL_VIDEODRIVER`) or frames keep
  failing (30 in a row) it logs a clear error and falls back to waiting on
  `robot.shutdown_event`; the robot keeps running headless.
* `display.hide_cursor`, `display.show_debug` (start with the overlay),
  `display.show_sim_panel` (desktop simulator panel; also means "simulator mode" for clicks).
* Touch: when not in simulator mode (the robot's own screen), tapping the face makes
  LittleBot look at the finger, smile, bounce and giggle (`audio.play_sound("giggle")`),
  with a 1.5 s cooldown. Disable with `TAP_REACTION_ENABLED = False` in `face/display.py`.
* Camera sign: while a parent watches the camera live on the dashboard (`vision.live_view`
  with `active: true`; read from `vision.status()` when the window opens), a small rounded
  camera with a dot sits in the face's top-right corner (30x22 px at 480x320, scaled with the
  window), over the face, a game or the video veil, so watching is never hidden. It uses the
  face's palette: plum ink at ~60 % with a raspberry dot on the pastel screen, the eye colour on a
  dark one. Drawn 4x and smooth-scaled once per size (cached), so it costs one blit a frame;
  still, never animated, so it doesn't pull a child's attention.

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

* Cartoon eye anatomy: the eye body is iris-coloured with a darker rim ("ink" outline,
  also along lid and crescent edges) and a vertical gradient over its open part (deeper
  under the lid, glowing lighter at the bottom). On top: a big near-black pupil and two
  white catchlights. Where they go is pure geometry in `frame.py`
  (`pupil_geometry`, `catchlights`, `open_span_at`, unit-tested without pygame): the pupil
  sits in the middle of the open part of the eye, follows `EyeFrame.look_x/look_y`
  (gaze) and always stays inside the rim (lids may cover it); it squashes with a blink and
  is gone before the eye is a thin line, and shrinks away on happy crescents. Catchlights
  stay put (screen-anchored, they do not spin with `spin_eyes`), are kept inside the open
  eye and shrink as the eye closes. Per-expression `EyeParams.pupil` sets the pupil size
  (small = wide-eyed surprise, big = soft sad / attentive eyes).
* The body is drawn 3x super-sampled with plain pygame primitives (rounded rect built from
  fills + circles, lid polygons with a gentle arch, a circle cut for the happy crescent)
  and smooth-scaled down: clean anti-aliasing for any lid shape. The colour gradient is
  multiplied in after the down-scale from a cached gradient column. Glow = the body
  downscaled 4x, gaussian-blurred, upscaled.
* Caches: eye bodies keyed by the quantised shape (0.5 px / 0.01 / 0.5 deg), glows by a
  coarser key (blur hides 3 px differences), sprites by (kind, size, colour, angle bucket),
  plus background vignette (with a 1-bit dither against banding) and the listening frame.
  Pupil and catchlight sprites are cached too and composited onto the cached body every
  frame in a reused scratch surface (the pupil clipped with `BLEND_RGBA_MIN` against the
  body), so gaze movement never re-renders an eye. Surfaces are converted to the display
  format when a display exists.
* Colours: `face.eye_color` / `face.background_color` (default soft light pink
  `[255, 214, 228]`, also set in `config/default.yaml`); expressions mix in a tint (sad blue,
  affectionate pink, celebrating gold), the error state a soft lavender. The renderer picks
  its look from the background's brightness: on a light screen the eye rims are deep ink,
  blinks and closed eyes are dark lines, the eyes cast a soft rosy shadow instead of a glow,
  Z's / "?" / thinking dots are a deep shade of the eye colour, sparkles are amber with an
  outline, confetti and hearts are more saturated, the listening frame is darker and the hidden
  screen is a dim mauve; a dark background (e.g. the old `[6, 8, 14]`) gives the glowing look.
  `face.glow: false` disables the glow / shadow (cheaper). The dashboard's mini face
  (`miniFace` in `dashboard/static/js/ui.js`) and the simulator panel's robot drawing use the
  same pink screen, inked eyes and per-expression mouths.
* Everything is deterministic for a seed: particles are pure functions of
  `(elapsed, seed)`; the animator owns one `random.Random`.

## Performance (Xeon 2.8 GHz container, Python 3.13, pygame-ce 2.5.8)

`scripts/benchmark_face.py --frames 150` (headless, 480x320, update + render per frame),
measured 2026-10-23 with the cartoon eyes:

| Scenario | mean ms | p95 ms | cpu @ 30 fps |
|---|---|---|---|
| calm neutral (cache warm, with blinks + wandering) | 0.63 | 1.6 | 1.9 % |
| typical expressions (happy, sad, sleepy, thinking, listening ...) | 0.6 - 1.0 | 1.3 - 2.8 | 2 - 3 % |
| busiest expressions (curious, confused, playful, celebrating with confetti) | 1.2 - 1.9 | 2.3 - 3.3 | 4 - 5.6 % |
| one-shot animations (bounce ... spin_eyes) | 0.6 - 2.3 | 1.0 - 3.5 | 2 - 7 % |
| sleeping / hidden | 0.22 / 0.11 | 0.3 / 0.15 | < 1 % |
| expression changed every 0.4 s (worst case) | 2.2 | 3.8 | 6.5 % |
| overall mean of all 31 scenarios | 0.96 | | |

For the same eye shapes the cartoon renderer costs about the same as the previous glossy
one: pupil + catchlights add ~0.02-0.04 ms per eye per frame, and an eye-body cache miss
is ~0.4-0.9 ms, about the same as or less than before. The eyes themselves are a little
bigger now.

Pink screen + always-on mouth (2026-10-23, same container, busier at the time, so compared
with the previous version run interleaved): overall mean 1.6-1.9 ms/frame vs 1.56 ms before,
worst scenario (expression changed every 0.4 s) 4.2-4.5 ms vs 3.5-3.6 ms. The light and dark
looks cost the same; the resting mouth is one cached blit, a mouth cache miss costs
0.13-0.38 ms, and lip-synced talking renders at ~1.2 ms/frame.

`--scale 2` (desktop window 960x640) adds ~2 ms/frame of smooth-scaling (desktop only;
the Pi runs at scale 1 / fullscreen 480x320).

Real-time run of the display loop (dummy video driver, scale 1, no panel, 6 s; measured
2026-10-20, before the cartoon eyes):
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
