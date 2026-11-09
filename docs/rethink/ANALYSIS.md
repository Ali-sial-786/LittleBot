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

## 4. Conversation pipeline (timings are code constants, not measurements)

### The path
- Wake word: Vosk grammar spotting on 0.1 s blocks (`audio/wakeword.py` `_listen` 621-720, `_match` 730,
  `_short_ok` 772, `_carry_from` 841 keeps the words said after the phrase) → `_detected` 937 / `_handle` 958
  (pause video, earcon, then `live.start_session`, `chatgpt/live.py:728`).
- `LiveCall.run` 899 → `_connect` 929: suspend audio, open the mic (`live_audio.LiveAudio`), aiortc offer, POST
  the SDP to CALL_URL (`_post_offer` 1110), data channel "oai-events". `_watch` 2636 polls every 50 ms.
- Wire (`chatgpt/wire.py`): input/output transcript deltas, `turn.done`, `delegation.created`, `error`,
  `session.closed`; answers go back as `delegation.context.append`, notes as `session.context.append`
  (frames ≤500 bytes, results ≤1800 chars).
- Hand-off: `_on_message` 1181 → `_answer` 1667 re-classifies with regex lists → `_quick_answer`
  (`chatgpt/quick.py`, no model: weather, time, volume, timer, picture) / `_route` 1731 (decider "routing") /
  `_ask_robot` 1992 (tools) or `_look_up` 2547 (no tools) / `_look_for_voice` 2183 (camera) → `_deliver` 1789.
- Text model: `brain/text_model.py` `text_provider` 54 = FallbackProvider: Claude `claude.model` →
  `claude.fallback_models` → ChatGPT (`chatgpt/responses.py:171`); falls through only if no tool ran and no
  words streamed.
- Output safety: `_check_reply` 1634 (320-char window while speaking), `_speakable` 520 →
  `safety/content.py` `validate_reply` 826 (1307 lines of regex rules); `classify_child_input` live.py:1239.
- Decider: `decide/service.py` `Decider.ask` 122 (never raises); features in `decide/uses.py` (route 85,
  addressed 166, concern 182, cards 50). Default provider off in default.yaml; on the robot it is OpenAI.
- Voices: `voices/service.py` (MATCH_THRESHOLD 0.5), `voices/call.py`; opt-in privacy switch.
- Barge-in: `live_audio.py` BargeInDetector 187, `_barge_frame` 714, `_stop_word` 743, `_interrupt` 859;
  `chatgpt/stop_words.py` (a second Vosk spotter).

### Waits between "they stop talking" and "they hear the answer"
- Wake → connected: video pause wait 1.0 s, speaker free ≤2.0 s, earcon ≤8.0 s (skipped if they keep
  talking), audio suspend 1.5 s, mic open retries 3.0 s, face check ≤1.0 s and voice check ≤1.5 s, SDP POST
  timeout 20 s. On the robot the microphone came on ~2.4-2.8 s after the wake phrase; connect took 0.9-1.7 s.
- In the call: mute 0.4 s after speech, echo tail 450 ms, barge-in speech 300 ms, false alarm 2.5 s, hold ≤4 s.
- Each hand-off, in series: routing ≤1.5 s (mode on), quick answers ≤3.0 s, Claude first word ≤4.0 s per
  model (two Claude models: ≤8 s before ChatGPT), reply timeout 15 s, ≤6 tool rounds, weather 3.0 s, "still
  working" note at 3.5 s, thinking sound from 1.5 s, hard cap 25 s. The first sentence goes out as it streams.
- Cards: the director waits ≤25 s for the voice's answer, decider 2.0 s, director model 4.0 s.
- Push-to-talk path: end of speech after 1.2 s of silence, max 8 s, text model 12 s.

### Patchwork: the worst 10
1. The hand-off kind is re-decided by regex after the voice model already chose to delegate (live.py
   1667-1729, word lists 82-340), and the routing decider can overrule both.
2. One utterance is matched many times in `_on_message` (1217-1257): concern, video, game, face, look-if-asked
   (early and final), director, each with its own regex.
3. Stop/quiet logic in three overlapping places: STOP_CALL/END_CALL/HUSH_CALL (live.py 179-191),
   `live_audio._stop_word`, and `stop_words.py`.
4. Three "is it a child" signals that disagree: `live_barge_in_max_pitch_hz` (260 default, 220 on the robot),
   STOP_MAX_PITCH_HZ 400 (hard-coded), voice recognition veto.
5. "Said to me?" decided three ways: `_for_robot` word rules, the decider `addressed`, the voice's own
   instructions.
6. The screen director patched per incident: ~40 regexes (director.py 166-310), a decider call with its own
   none/close/steer options, dated fixes.
7. ~17 dated one-off patches in comments ("on the robot 2026-10-29").
8. Safety runs repeatedly (validate_reply in two live.py places and conversation.py; classify_child_input twice).
9. Duplicated text-model paths (`_ask_robot`, `_look_up`, conversation.py:664, director.py:1073 each build
   prompt + history); the preamble regex copied three times; the tool-round limit 6 in three places.
10. Two Anthropic config blocks (`ai.anthropic_*` and `claude.*`); seven unrelated RMS thresholds
   (1e-4, 0.0105, 0.01, 0.012, 0.015, 0.004, 0.008). quick.py volume/timers duplicate the volume tools.
- Also: pi.yaml says tts auto uses piper "if a model is configured", but the code only picks espeak or the
  mock unless `tts.provider=piper`.

### The non-GPT-Live path (complete and tested, but the wake word doesn't use it)
- Triggers: space bar push-to-talk (`face/display.py:1136`), dashboard (`dashboard/routes/ai_routes.py:91`),
  typed `handle_utterance` (`brain/conversation.py:271`).
- `brain/conversation.py` ConversationEngine 104 (`converse_once` 278, `_converse_turn` 509): AudioService
  listen with EnergyVAD → STT (`audio/stt/__init__.py:27`: Vosk if present, OpenAI Whisper if configured) →
  text provider (`select_provider` 235; ai.provider default mock) → TTS (`audio/service.py:855` `_synthesize`:
  recorded lines, espeak-ng, piper when configured, OpenAI TTS when cloud is allowed).

### Config switches
chatgpt.live_barge_in (auto|on|off), chatgpt.live_stop_words, chatgpt.live_thinking_sound,
chatgpt.tools_enabled, chatgpt.web_search, text.provider / text.fallback, decide.provider and
decide.default_mode / decide.modes, screens.enabled, browser.enabled. Runtime settings (SQLite, dashboard,
`core/settings.py:29`) override YAML: wake.enabled, ai.enabled, privacy.cloud_enabled,
privacy.voice_recognition_enabled, privacy.ai_camera_enabled.

### Tools the models get (`chatgpt/tools.py` TOOLS 91-196)
get_robot_status, battery, describe_yourself, read_debug_log; list/start/stop_activity; list/start/stop_game;
get/set_volume, volume_up/down, set_face; go_to_sleep, wake_up, stop_everything; search_youtube,
play_youtube, list_approved_videos, stop/pause/resume_video; look_through_camera (chat mode); get_weather,
get_time; show_on_screen, change_card; search_in_browser, browser; OpenAI hosted web_search. Take-overs
(video, activity, sleep) are deferred until the voice finishes (tools.py:29-36).
