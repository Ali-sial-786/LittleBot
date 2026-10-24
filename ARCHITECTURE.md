# LittleBot architecture

LittleBot is one Python process with a small number of threads, an in-process
event bus, a single explicit state machine and dependency-injected subsystems.
Everything runs the same way on a desktop (simulation), on the Raspberry Pi,
and in the offline demo; only configuration decides which device backends are
real and which are simulated.

```
                       ┌──────────────────────── main thread ────────────────────────┐
                       │  face/display.py  pygame loop @30 fps (idle 15–20 fps)       │
                       │  renders FaceController → 480×320 surface → window/HDMI      │
                       │  keyboard / touch / simulator panel                          │
                       └───────────────▲──────────────────────────────┬───────────────┘
                                       │ thread-safe FaceAPI commands  │ runtime.submit()
┌────────── camera thread ─────────┐   │                              ▼
│ vision/service.py                │   │   ┌──────────── asyncio runtime thread ─────────────┐
│ capture → downscale → detect     │───┼──▶│ dashboard (FastAPI/uvicorn)   activities engine  │
│ → tracker → events               │   │   │ conversation engine (brain)   media controller   │
└──────────────────────────────────┘   │   │ motion scheduler              behaviours (glue)  │
┌────────── audio threads ─────────┐   │   └──────────────────────────────────────────────────┘
│ audio/service.py output worker   │───┘                 ▲            │
│ mic stream + VAD (half-duplex)   │◀────────────────────┘            │
└──────────────────────────────────┘          EventBus (pub/sub) ◀─────┘
```

## Core (littlebot/core)

| Module | Responsibility |
|---|---|
| `config.py` | Typed configuration (pydantic). Layering: defaults → `config/default.yaml` → `config/<mode>.yaml` → `config/local.yaml` → `--config` → `LITTLEBOT__SECTION__KEY` env. Secrets only from env/`.env` as `SecretStr`. Offline mode forces cloud off in code. |
| `events.py` | Thread-safe `EventBus` (sync callbacks, bounded queues, asyncio delivery, `wait_for`). `Topics` documents every topic and payload. |
| `state.py` | `StateManager` with the eight `RobotState`s and a validated transition table; publishes `state.changed` with the matching face state. |
| `arbiter.py` | `ResourceArbiter`: priority leases on face / speaker / microphone / screen / motors so behaviours never fight. Higher priority preempts (callback), lower is refused. |
| `runtime.py` | `AsyncRuntime`: the one asyncio loop, in a daemon thread (pygame owns the main thread). |
| `settings.py` | Parent-editable settings in SQLite with typed coercion and `settings.changed` events. Safety-critical `motion.physical_armed` is never persisted. |
| `storage.py` | Tiny thread-safe SQLite wrapper + JSON key/value store. |
| `interfaces.py` | Protocols for every subsystem + shared vocabularies (expressions, sounds, age groups, categories). |
| `context.py` | `RobotContext` – the dependency-injection container handed to every `create_service(ctx)` factory. |
| `clock.py` | Time source; `time_scale` makes scripted waits instant in tests. |

## Subsystems

Each package exposes `create_service(ctx)` and implements a protocol from
`core/interfaces.py`. `app/robot.py` builds them in order (face → audio →
vision → motion → media → brain → activities → dashboard); later ones receive
earlier ones through `ctx`. A subsystem that fails to build or start is logged
and reported as degraded — the robot keeps running (only the face is mandatory).

| Package | Implements | Notes |
|---|---|---|
| `face/` | `FaceAPI` | Pure animation model (testable without a window) + pygame renderer + display loop/simulator panel. Thread-safe command queue. |
| `audio/` | `AudioAPI` | Output worker thread, procedural sound effects, TTS (mock/espeak-ng/piper/OpenAI) and STT (mock/vosk/OpenAI) providers, energy VAD, **half-duplex** echo prevention. |
| `vision/` | `VisionAPI` | Camera thread, Haar (or YuNet) face detection at reduced resolution and rate, smoothing + presence hysteresis, mirrored gaze mapping, experimental gestures. |
| `activities/` | `ActivityAPI` | YAML-scripted, age-tagged activities run by an async engine (prompts for caregivers, waits, branches, songs). |
| `media/` | `MediaAPI` | Parent allowlist of YouTube items, official IFrame Player API page driven through a token-protected bridge, kiosk/desktop browser backends, age gate and time limits. |
| `brain/` | `BrainAPI` | Conversation turns (listen → think → speak), providers (mock / Anthropic / OpenAI via httpx), code-enforced safety validation, rate + budget limits. |
| `motion/` | `MotionAPI` | Servo joints with hard limits, eased interpolation, gestures, latched emergency stop, register-level PCA9685 mock; real driver gated behind config + calibration + parent arming. |
| `power/` | — | `ctx.power`: the battery, read from an RP2040-Zero monitor's JSON lines on a USB serial port (stdlib termios, no pyserial; `firmware/battery_monitor/`). Percent, charging, temperature, time left; low / empty / hot alarms, the gentle low-battery line, the goodbye and clean power-off. Quiet and "not fitted" without a monitor. See docs/POWER.md section 8. |
| `dashboard/` | — | FastAPI + vanilla JS parent dashboard; setup-code first run, scrypt passwords, server-side sessions, CSRF tokens, localhost-only by default. |
| `safety/` | — | `AgePolicy` (video/cloud gates, reply length), `PrivacyManager`, AI content validation. |
| `app/behaviours.py` | — | Reactive glue in one place: state → face, faces → gaze, person arrives → greeting / wake, idle → sleepy → sleep. |

## How a typical interaction flows

**Parent starts peekaboo from a phone**
1. `POST /api/activities/peekaboo/start` (session cookie + CSRF header) → `ctx.activities.start("peekaboo")`.
2. Engine acquires a lease on face + speaker (priority ACTIVITY), sets `PLAYING_ACTIVITY`.
3. Steps call `face.close_eyes()`, `audio.play_sound("whoosh")`, wait, `face.open_eyes()`,
   `face.set_expression("surprised")`, `audio.say("Peekaboo!")`...
4. `audio.say` publishes `audio.speech_started` / `audio.level` / `audio.speech_finished`; the face animates speech.
5. On completion the engine publishes `activity.finished`, releases the lease and returns the state to IDLE.

**Child appears in front of the camera**
camera thread → detector → tracker → `vision.person_present` + `vision.faces` →
`behaviours` → `face.track_gaze(x, y)` (soft gaze, explicit `look_at` still wins) and a happy greeting if idle (or wake from sleep).

**Push-to-talk conversation (older child)**
`brain.converse_once()` → lease (face, speaker, mic) → `LISTENING` → `audio.listen()`
(mic only open while not speaking) → `THINKING` → provider (mock unless parent-enabled cloud + age ≥ 24 months + key + budget) →
`validate_reply()` → `SPEAKING` → `audio.say()` → `IDLE`.

**Video**
`media.play(id)` → age gate + limits → lease (screen, speaker, face; priority MEDIA) → `PLAYING_MEDIA` →
face hidden → kiosk browser opens the local `/player?token=…` page which embeds the official IFrame player →
the page reports `ended` → controller stops, browser closes, `face.visible` → the eyes come back.

**First run**
the dashboard generates a one-time setup code, logs it and publishes `dashboard.setup`; the face
display shows it at the bottom of the screen until a parent claims the dashboard (physical presence
= permission). Parent password → scrypt hash in SQLite; sessions are server-side; mutations need a
CSRF token.

**Restart from the dashboard**
`robot.request_shutdown(restart=True)` → the process exits with code 75 → systemd
(`Restart=on-failure`) starts it again. A plain shutdown exits 0 and stays down.

**STOP**
Dashboard STOP / keyboard X → `robot.stop_all()` → `system.stop_all` event, motion cancel, audio cut,
activity + media stopped, all leases revoked, state IDLE, neutral face.

## Concurrency rules

* Face API: non-blocking, thread-safe; render thread applies commands.
* Async subsystems never block the loop (blocking I/O in threads / `asyncio.to_thread`).
* Device threads communicate only via bus events and thread-safe APIs.
* Bounded queues everywhere; camera keeps only the latest frame.
* Vision pauses detection during video (CPU for Chromium) and drops to ~1.5 Hz while asleep.
* The face drops from 30 to 15–20 fps when nothing is animating.

## Modes

| | simulation | pi | offline |
|---|---|---|---|
| Window | scaled 2×, simulator side panel | fullscreen 480×320 | scaled |
| Camera | webcam if present, else mock | USB UVC | mock |
| Mic / speaker | computer devices if present | USB devices | mock mic, speaker if present |
| TTS / STT | espeak-ng if installed / mock | espeak-ng (piper optional) / mock or vosk | local only |
| Cloud AI | parent opt-in | parent opt-in | forced off |
| Servos | mock | mock until calibrated + enabled + armed | mock |

## Testing strategy

`littlebot.testing` provides fakes for every protocol and `make_test_context()`
(in-memory DB, mock monitor, `time_scale=0`). Each subsystem has unit tests;
`tests/test_integration*.py` builds the real robot (headless, mock devices) and
drives it through the dashboard API. See PROJECT_STATUS.md for what is verified
with mocks vs. on real hardware.
