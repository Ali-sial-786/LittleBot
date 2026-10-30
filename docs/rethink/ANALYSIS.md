# Overnight rethink: analysis (2026-10-30)

Read-only analysis of main 01d9259 by four readers, condensed. File:line pointers are as of that commit.
The user's goal and answers are in PLAN.md (and the top of PROGRESS.md).

## 1. Core architecture

- Entry `littlebot/__main__.py` → `app/main.py:main()` (load_config, logging, `LittleBot.create(cfg)` in
  `app/robot.py:71`). Exit code 75 = restart.
- Composition root `app/robot.py`: `SUBSYSTEMS` (lines 32-47) = face, audio, vision, motion, media, brain,
  activities, games, screens, voices, browser, live, wake, power; each package exposes `create_service(ctx)`.
  Only face is required; others degrade. Glue in `app/behaviours.py`, then the dashboard.
- `core/service.py:20` Service (async start/stop, `service_status`, `health()`); `core/runtime.py:21`
  AsyncRuntime = one asyncio loop in a daemon thread; the MAIN thread runs pygame (`face/display.py:100`).
- Event bus `core/events.py` (Topics line 31; publish 254; subscribe sync/queue/async; fnmatch patterns).
- `core/state.py` RobotState (IDLE, LISTENING, THINKING, SPEAKING, PLAYING_ACTIVITY, PLAYING_MEDIA, SLEEPING,
  ERROR) with a transition table; `core/arbiter.py` leases on FACE, AUDIO_OUT, MICROPHONE, SCREEN, MOTION with
  priorities AMBIENT 0 … CONVERSATION 40 … MEDIA 60, PARENT 80, SYSTEM 100.
- ctx = `core/context.py:31` RobotContext (config, bus, state, arbiter, db, settings, privacy, clock, monitor,
  and one field per subsystem; `extras` dict).
- Adding a feature (docs/DEV_CONVENTIONS.md): package + `create_service`, Protocol in `core/interfaces.py`,
  ctx field, SUBSYSTEMS entry, `_Section` in `core/config.py` (pydantic, `extra="forbid"`) + field on
  `LittleBotConfig` (line 635), defaults in config/default.yaml, settings keys in `core/settings.py:29`,
  fakes in `littlebot/testing.py`, tests in `tests/<name>/`.
- Config layering: pydantic defaults → config/default.yaml → config/<mode>.yaml (simulation/pi/offline) →
  config/local.yaml (git-ignored, the robot's own) → --config → env `LITTLEBOT__SECTION__KEY` → CLI.
  Secrets (ANTHROPIC_API_KEY, OPENAI_API_KEY, …) only from env/.env/600 key files, never YAML.
- Runs on Windows in simulation (`--mode offline` needs no devices or keys, `--headless`).
- Tests: ~2,580 tests in 164 files, ~3.5 min on 4 vCPUs (Linux). Fakes in `littlebot/testing.py`
  (make_test_config, make_test_context, FakeFace, FakeAudio, FakeMotion, FakeDecisionModel, FakeAnthropic).
- Deploy: `scripts/install_pi.sh` (pip install -e .[vision,audio,pi] + live + claude), systemd user unit;
  deps in pyproject.toml (pygame-ce, numpy, pydantic, fastapi, uvicorn, httpx, yt-dlp; extras aiortc,
  anthropic, vosk, sherpa-onnx, opencv).
- DEV_CONVENTIONS rules: subsystems talk only via ctx protocols and the bus; never block the loop; leases
  before face/speaker/screen/motors; check ctx.privacy before camera/mic/network; deterministic tests with no
  hardware/network/keys; type hints, `logging.getLogger("littlebot.<module>")`, heavy imports inside functions.

## 2. Screens, face, games, media

- Face: main-thread DisplayLoop at 30 fps (15 idle, 10 dozing, 5 under video); native 480x320 drawn
  supersampled, scaled to 720x480. 14 expressions (`core/interfaces.py:24`), 10 one-shot animations, states
  idle/listening/thinking/speaking/sleeping/error/hidden. Lip-sync from audio.level visemes (5 mouths) with a
  synthetic fallback. Takeover order in display.py:509: game, else card, else face; long press = games menu.
- Cards (`littlebot/screens`): one card at a time over the face. Kinds: weather, clock, timer, maths/count,
  definition, explain, recipe, facts (layouts number/list/compare/profile/score …), games menu. Lifetime: up
  while the chat is on, then lingers 30 s. Chosen by `screens/director.py`: child check → regex rules (~1500
  lines of word lists) → prefilter → decider (decision model) → text model JSON, waiting for the voice's
  answer (up to 25 s). The voice model can also call `show_on_screen` / `change_card` (chatgpt/tools.py:164).
  Two model calls per card, late cards, phrase lists that drift from speech.
- Games (`littlebot/games/catalog.py:37-72`): Bubble Pop, Peekaboo Friends, Colour Burst, Follow the Star
  (camera), Memory Lights, Tic-Tac-Toe (minimax), Reaction Race, Rock Paper Scissors (camera/tap/voice).
  Touch-only except RPS; scores in memory only. All toddler-oriented.
- Activities: 43 scripted YAML activities (songs, baby play, learning, movement, thinking, feelings, pretend,
  stories), engine `activities/engine.py:166`.
- Media: YouTube IFrame API in a Chromium kiosk page (`media/controller.py`), search via yt-dlp ytsearch5;
  hold/resume around voice chats. No music player. Browser: Google in Chromium over DevTools (scroll, open,
  back, zoom, read).
- Dashboard: web/phone UI with Home, Activities, Videos, Face, Camera, Games, Child profile, Privacy, Motion,
  Audio, Simulator, Settings.
- Motion: `motion/` (PCA9685 driver + mock, joints head/left_arm/right_arm, gestures wave, look_left/right,
  raise_arms, celebrate, dance, shake, neutral; arming + calibration gate). Nothing physical attached.
- Vision: USB camera via OpenCV (none attached now), Haar/YuNet detection, SFace family recognition (opt-in),
  hand pose for RPS.
- Info: Open-Meteo weather (location by IP), clock. Power: UPS HAT (E) reader + battery badge.
- Biggest weaknesses: screen ownership is a stack of if-checks (no shared stage where face and content
  coexist); slow layered card selection; no real gaze/attention; coarse lip-sync; fixed expressions not driven
  by the reply's content; minimal touch; toddler-only touch games without persistence; video never verified on
  the Pi; dashboard is parent-admin, not a "now playing" companion.

## 3. Research: voice stack and features (sources in the research notes below)

- No published measured time-to-first-audio for GPT-Live / gpt-realtime / Gemini Live. Prices: OpenAI
  gpt-realtime-2.1 audio $32 in / $64 out per 1M tokens (~$0.02/min in, ~$0.08/min out), mini $10/$20; Gemini
  3.1 Flash Live / 3.8 Live audio $3 in / $12 out per 1M (~$0.005/min in, ~$0.018/min out).
- Cascade parts (medians): Deepgram Flux end-of-turn ~260 ms ($0.0077/min); Claude Haiku TTFT ~0.45 s;
  Cartesia Sonic 3.5 TTFA ~128 ms incl. network (Vapi index); ElevenLabs Flash v2.5 ~197 ms. Sum ≈ 0.85-1.0 s.
- Turn detection: Deepgram Flux (end-of-turn built in, eager mode), LiveKit turn detector (~10-25 ms CPU,
  <500 MB). Barge-in: stop TTS on confirmed user onset, not on the transcript.
- Local TTS on the Pi: Piper RTF ~0.19 (fallback); Kokoro unmeasured; Chatterbox needs a GPU (hosted only).
- Features to borrow (Alexa+, Gemini for Home, Astro, Loona/EMO, Reachy Mini): persistent memory; voice-made
  routines; proactive suggestions; multi-step agent tasks; reminders tied to events; selectable personality
  styles; one memory across robot/phone/web; smart-home control; camera events; follow you / patrol (wheels
  phase); video calls; reacts to touch; apps from its dashboard; privacy indicator; gaze following the
  speaker; thinking pose; wake/sleep animations; morning/evening briefings; "what is this" vision; bedside
  timer; offline fallback for timers/smart home.
- Faces/motion: Vector/Cozmo eyes are parameter-based and tweened (corner points, lids), personality first;
  saccade speed carries meaning; gaze aversion timed before utterances; eyes for small shifts, head for large;
  listening = eyes toward the speaker + blinks + drift; thinking = brief look up/away; speaking = small nods on
  phrase boundaries; stop = halt and look at the user.
- Sources: developers.openai.com/api/docs/pricing; ai.google.dev/gemini-api/docs/pricing;
  deepgram.com/pricing; cartesia.ai/pricing; humannessindex.vapi.ai (Cartesia Sonic 3.5, ElevenLabs Flash
  v2.5); pypi livekit-plugins-turn-detector; Anki Vector character design guide (randym32.github.io);
  frontiersin.org frobt.2023.1127626; arxiv 2210.02866.

## 4. Conversation pipeline

(Still being mapped when this was written: see PROGRESS.md.)
