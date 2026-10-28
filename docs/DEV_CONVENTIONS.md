# LittleBot developer conventions

Read this before changing code. It is short on purpose.

## Layout

```
littlebot/
  app/          main.py (CLI), robot.py (composition root), behaviours.py (cross-module glue)
  core/         config, events (EventBus + Topics), state (RobotState), arbiter (leases),
                runtime (asyncio thread), settings (SQLite-backed parent settings),
                storage (SQLite), clock, service (lifecycle base), interfaces (Protocols), context (DI)
  face/         animated eyes: model/animation logic, pygame renderer, display loop
  audio/        speaker/mic devices, sound effects, TTS + STT providers, half-duplex AudioService
  vision/       camera devices, face detection, tracking, presence events
  activities/   activity model + engine; scripts live in assets/activities/*.yaml
  media/        YouTube allowlist + embedded IFrame player control
  brain/        conversation engine + AI providers (mock / Anthropic / OpenAI)
  motion/       servo abstraction, mock + PCA9685 drivers, gestures, e-stop
  dashboard/    FastAPI parent dashboard + static vanilla-JS frontend
  safety/       age policy, privacy manager, AI content validation
  hardware/     platform detection, system monitor (+ device helpers per subsystem)
  testing.py    fakes for every protocol + make_test_context()
assets/  sounds/ images/ activities/
config/  default.yaml + simulation.yaml / pi.yaml / offline.yaml (+ git-ignored local.yaml)
scripts/ diagnostics, Pi setup, benchmarks
tests/   pytest suite (one file or folder per subsystem)
docs/    guides
```

## Wiring

* Every subsystem package exposes `create_service(ctx: RobotContext)` in its `__init__.py`
  (lazy-import heavy deps inside). It returns an object implementing the subsystem's
  protocol from `core/interfaces.py`; if it has a lifecycle it subclasses `core.service.Service`
  (`async start()`, `async stop()`, `health()`).
* Lifecycle state lives in `Service.service_status` (not `status`, which every protocol uses
  for its `status()` dict). Activities and media have protocol methods `start(id)` / `stop(reason)`
  that share a name with `Service.start()` / `Service.stop()`: called with no arguments they act as
  the lifecycle methods (the composition root always calls them that way).
* `app/robot.py` builds subsystems in order: face, audio, vision, motion, media, brain,
  activities, then dashboard. Each factory may use the ones built before it via `ctx`.
* Talk to other subsystems only through `ctx.<subsystem>` protocol methods and bus events
  (`core.events.Topics`). Never import another subsystem's internals.
* Add new event topics to `Topics` only if really needed (document payload keys).

## Threads

* Main thread: pygame display loop (`face/display.py`).
* One asyncio loop in a background thread (`core/runtime.AsyncRuntime`): dashboard server,
  activities, conversation, media, motion scheduler. Never block it: run blocking I/O with
  `asyncio.to_thread` or a dedicated thread.
* Camera capture and audio playback/capture use their own threads.
* `FaceAPI` methods are thread-safe and non-blocking (they enqueue commands).
* Bus callbacks registered with `bus.subscribe` run in the publisher's thread: keep them tiny.

## State and leases

* Top-level state lives in `ctx.state` (`RobotState`). Behaviours set it on start and use
  `ctx.state.release(expected_state)` when done (so they never clobber a newer state).
* A behaviour acquires a lease (`ctx.arbiter.acquire(owner, resources, priority, on_preempt)`)
  before using the face/speaker/screen/motors. `on_preempt` must stop the behaviour promptly.
  Priorities: `core.arbiter.Priority`.

## Settings, privacy, safety

* Static config: `ctx.config` (pydantic, read-only at runtime).
* Parent-editable values: `ctx.settings.get/set("dotted.key")` - keys are registered in
  `core/settings.build_defaults`. Subscribe to `Topics.SETTINGS_CHANGED` / `PRIVACY_CHANGED`.
* `ctx.privacy.camera_enabled / microphone_enabled / cloud_enabled` must be checked before
  using the camera, microphone or any network service. Offline mode forces cloud off.
* `ctx.age_policy()` gives age-aware rules (video gate, cloud AI gate, reply length).
* Never store audio, images or transcripts by default. Never send camera data anywhere.
* The `cloud_enabled` switch covers services that receive data about the child (cloud AI,
  cloud speech-to-text / text-to-speech). YouTube playback is gated separately by `media.enabled`,
  the allowlist, the age gate and offline mode: it only streams parent-approved videos and sends
  nothing about the child.
* API keys: only `ctx.config.anthropic_api_key` / `openai_api_key` (SecretStr). Never log them,
  never return them to the dashboard frontend.

## Devices

* Every device has real + mock implementations selected by config (`backend: auto|real|mock|none`).
  `auto` = try real, fall back to mock/none with a logged warning; missing devices must never crash
  the app. Report status via `status()` and `*_STATUS` events.

## Tests

* `pytest` from the repo root (`.venv/bin/python -m pytest -q`). asyncio tests are plain
  `async def test_...` (asyncio_mode=auto).
* Use `littlebot.testing.make_test_context()` (in-memory DB, fakes, `time_scale=0`).
* Tests must be deterministic and must not need real hardware, network or API keys.
  Tests that do need them are marked `hardware` / `network` and skip cleanly.
* For pygame, set `SDL_VIDEODRIVER=dummy` and `SDL_AUDIODRIVER=dummy` (tests/conftest.py does).

## Style

* Python >= 3.11, type hints, `from __future__ import annotations`, module docstrings.
* Standard `logging` (`logging.getLogger("littlebot.<module>")`), no prints in library code.
* Keep dependencies minimal; heavy/optional imports go inside functions.
* Target: Raspberry Pi 5 with 4 GB RAM, and still fine on 2 GB (`littlebot/core/memory.py` `roomy`: what is
  kept loaded only where there is room). Bounded queues, timeouts, low idle CPU.
