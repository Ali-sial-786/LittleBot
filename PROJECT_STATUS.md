# LittleBot – project status

_Last updated: 2026-10-20 (pre-hardware software build)._

**Summary:** the whole software platform is implemented as one integrated application and runs in
desktop simulation and offline-demo modes. **2,562 automated tests pass (2 skipped, 0 failed)** (see "Test suite" below).
Nothing has run on the Raspberry Pi or on real camera/microphone/speaker/servo hardware yet; all
hardware-dependent behaviour is marked below as mock-verified or awaiting verification.

Legend:
- ✅ **Verified working** – exercised end-to-end in this environment.
- 🧪 **Verified using mocks** – real code paths exercised against mocks/fakes/fixtures (no real device).
- 🛠️ **Implemented, not tested** – code exists but could not be exercised here.
- ⛔ **Blocked** – needs hardware, credentials or a normal network to verify.
- ❌ **Not implemented**.

The development environment was a cloud Linux container (Ubuntu 24.04, Python 3.13, x86-64) with **no
display, camera, microphone or speaker**. The face window ran on SDL's dummy video driver and was
checked through rendered screenshots.

## Acceptance criteria

| # | Criterion | Status | Evidence / notes |
|---|---|---|---|
| 1 | Launch LittleBot on a computer | ✅ | `python -m littlebot` builds every subsystem, opens the window (dummy display here) + dashboard, shuts down cleanly (`tests/test_integration.py::test_cli_offline_headless_smoke`, manual runs). Run it on your desktop to see the real window. |
| 2 | Eyes blink and display expressions | ✅ | 14 expressions, blinking/double blinks, gaze, breathing, 10 one-shot animations; 158 face tests; screenshots reviewed (`scripts/face_preview.py --screenshot-dir`). |
| 3 | Control expressions with keyboard shortcuts | ✅ | All shortcuts tested with synthetic key events under SDL dummy (press **H** in the window for the list). |
| 4 | Launch peekaboo and other activities | ✅ | 43 activities; every one runs to completion on the real robot composition (`test_every_activity_runs_on_the_real_robot`), from keyboard (P), dashboard and API. |
| 5 | Activities trigger the right animations and sounds | ✅ / 🧪 | Expressions, eye closing, animations, gestures verified on the real face/motion; sounds/speech verified as audio events + simulated speaker (nothing audible here). |
| 6 | Open the parent dashboard | ✅ | Real HTTP server, first-run setup code, login; 74 Playwright screenshots (phone + desktop, light/dark). |
| 7 | Dashboard controls LittleBot | ✅ | `tests/test_integration_dashboard.py` drives face, activities, STOP, media, simulator camera/speech, motion e-stop, privacy over real HTTP against the real robot. |
| 8 | Manage approved YouTube videos | ✅ | Add/validate/rename/enable/reorder/remove; malformed and non-YouTube URLs rejected (211 media tests). |
| 9 | Play an approved video on the desktop | ⛔ | Official IFrame player page + bridge verified with a stubbed YT API in headless Chromium; with real YouTube the API loaded and the player was created, but YouTube refused playback from this data-centre sandbox (error 150 on every video). Needs a check on a home network. |
| 10 | Test microphone and speaker | 🧪 / ⛔ | `scripts/check_audio.py`, dashboard Audio page tests; verified against a fake PortAudio and espeak-ng. No audio devices in this environment. |
| 11 | Camera detects faces (webcam) | 🧪 / ⛔ | YuNet and Haar detect the face in OpenCV's sample photo; capture/reconnect paths tested with a fake camera driver. No webcam here. |
| 12 | Eyes follow a detected face | ✅ (simulated faces) | Simulated faces flow through the real vision tracker → behaviours → face gaze (with correct mirroring). Real camera ⛔. |
| 13 | Mock AI conversation without keys | ✅ | Mock provider, push-to-talk with simulated mic, state transitions LISTENING→THINKING→SPEAKING→IDLE. |
| 14 | Real AI providers configurable | 🧪 / ⛔ | Anthropic (Messages API, `claude-haiku-5-5`) and OpenAI adapters tested with `httpx.MockTransport` (success, 401, 429, 5xx, timeouts). No API keys here. |
| 15 | Simulated head and arm movements | ✅ | Register-level PCA9685 mock, gestures, simulator side panel + dashboard SVG. |
| 16 | Emergency stop in simulation | ✅ | Keyboard E, dashboard, API; latched until reset; integration-tested. |
| 17 | Missing devices don't crash the app | ✅ | Real backends with no devices degrade to "unavailable"/simulated; robot keeps working (`test_missing_real_devices_degrade_without_crashing`). |
| 18 | Automated tests pass | ✅ | 2,562 passed, 2 skipped, 0 failed (see below). |
| 19 | Raspberry Pi deployment instructions ready | 🛠️ | `docs/PI_SETUP.md`, `scripts/install_pi.sh`, systemd/autostart units, diagnostics. Scripts lint-clean and dry-run tested; never run on a Pi. |
| 20 | Hardware-dependent functionality identified as unverified | ✅ | This document, `HARDWARE_ARRIVAL_CHECKLIST.md`, and "UNVERIFIED" markers in the docs. |

## Subsystems

| Subsystem | Status | What's verified | Awaiting hardware / credentials |
|---|---|---|---|
| Core (config, events, state, arbiter, settings, storage) | ✅ | Config layering + env overrides + secrets, bus delivery modes, transition rules, leases/preemption, SQLite persistence | – |
| Face (`littlebot/face`) | ✅ | All expressions/states/animations render; deterministic animator; thread-safe API; display loop; simulator panel; on-screen setup code | 480×320 HDMI panel, fullscreen on Pi, touch input, Pi FPS/CPU |
| Activities (`littlebot/activities`) | ✅ | 43 YAML activities (6 m – 5 y), pause/resume/stop/interrupt, caregiver prompts, branching, songs, profile placeholders, opt-in history | Real-world use with a caregiver and child |
| Dashboard (`littlebot/dashboard`) | ✅ | 80+ endpoints, setup-code first run, Host allowlist, body caps, global login lockout, scrypt passwords, server-side sessions, CSRF + Origin checks, rate limits, CSP, no secrets in responses, phone-first UI | Phone access over home Wi-Fi (`--lan`) |
| Audio (`littlebot/audio`) | 🧪 | 20 synthesised gentle sound effects, melody synth, espeak-ng voice (real), half-duplex, volume ceiling, privacy, device fallback, OpenAI TTS/STT (mock transport) | USB speaker + mic on Pi (PipeWire/ALSA), VAD threshold, real STT (Vosk/OpenAI) |
| Vision (`littlebot/vision`) | 🧪 | Detector on sample photos, tracker + presence hysteresis, mirroring, reconnect, privacy release, pause during video, reduced rate while asleep | USB UVC camera, Pi CPU, mirror direction on the assembled robot, gestures (experimental) |
| Media (`littlebot/media`) | 🧪 / ⛔ | Allowlist parsing, age gate, session/daily limits, official IFrame player page with stubbed YT, kiosk/desktop backends, Chromium cleanup | Real playback (blocked from sandbox), Pi 5 2 GB memory/CPU with Chromium |
| Brain (`littlebot/brain`, `safety/content.py`) | ✅ (mock) / 🧪 (cloud) | Mock conversations, code-enforced reply validation, PII scrubbing, rate/budget limits, provider gating by age/privacy/offline | Real Anthropic/OpenAI calls (needs a paid API key) |
| Motion (`littlebot/motion`) | 🧪 | Datasheet-exact PCA9685 register emulation, limits, eased interpolation, gestures, latched e-stop, two-key arming | PCA9685 + MG90S servos, calibration, mechanical safety (**physical motion disabled**) |
| Pi deployment (`scripts/`, `docs/`) | 🛠️ | Shell scripts `bash -n` + shellcheck clean, systemd units verified with `systemd-analyze`, installer dry run, diagnose/perf tools run here | Everything on the actual Pi 5 |

## Test suite

`.venv/bin/python -m pytest -q` → **2,562 passed, 2 skipped** (about 3½ minutes on 4 vCPUs).

| Area | Tests |
|---|---|
| brain + AI safety (incl. adversarial safety phrases) | 983 |
| activities | 526 |
| media | 271 |
| audio | 177 |
| face | 159 |
| motion | 140 |
| dashboard (incl. security) | 130 |
| vision | 87 |
| deploy scripts | 36 |
| core + app glue | 31 |
| cross-module integration (real subsystems, real HTTP, real browser) | 24 |

Skipped: shellcheck not installed here; the YuNet detector test needs the downloaded model
(`scripts/download_models.py yunet`). The YouTube player-page browser tests need Playwright (`pip install -e ".[browser-tests]"`).

## Security and child-safety review

An independent read-only review (2026-10-20) probed the running app and found 13 issues; **all were
fixed with regression tests and re-verified against a live instance**:

| Severity | Finding | Fix |
|---|---|---|
| High | Emergencies ("I ate a battery", "daddy hit me", "a man said come with me", "the baby is in the pool") got cheerful replies and no parent alert | Concern categories (swallowed, water, stranger, safety, emergency, hurt, help…) always give a calm "get a grown-up now" reply + a dashboard alert (category label only) |
| Medium | Reply validator missed secrecy/dependency/personal-info/unsafe/scary phrasings | Rules + per-sentence matching; adversarial suite (unicode, leetspeak, spacing) |
| Medium | DNS rebinding (no Host check) | Host allowlist before routing |
| Medium | Login rate limit bypass via X-Forwarded-For | Proxy headers off + global login lockout |
| Medium | Unbounded request bodies (300 MB → +700 MB RSS) | 64 KB / 16 KB body caps, enforced while streaming |
| Medium-low | Player accepted unapproved videos inside playlists / unverified ids | Fail-closed checks in the page and the controller; single claimed player page |
| Low | Password reset while running kept old sessions | DB-checked sessions; fresh setup code without restart |
| Low | Cloud speech-to-text / text-to-speech skipped the age gate and opt-ins | Same gates as cloud AI; nickname replaced unless shared |
| Low | PII scrubber gaps | Parent names, places, addresses, "I'm <Name>" |
| Low | Only the dashboard e-stop disarmed motion | Every e-stop disarms; reset never re-arms |
| Low | Channel "uploads" playlists approvable | UU/FL/UL lists refused |
| Low | LAN neighbour could keep burning the setup code | Per-client caps on failures |
| Low | DB/logs world-readable | `umask 077`, DB files 0600 |

A QA pass through the real app (phone + desktop browsers) found and fixed 6 interaction bugs
(prompt off-screen on phones, "null" text, camera label, video-off banner, mirrored click in the
face window, swapped arms in the simulator panel) and added "Suggested for <child>" activities.

Deliberate choices to review: a child saying "come with me"/"get in the car" alone is not flagged
(everyday play phrases) — it is when a stranger said it; `localhost` is accepted on any port (SSH
tunnels; rebinding pages never send "localhost"); LAN mode is plain HTTP (trusted home Wi-Fi only).

## Known limitations / honest caveats

- **Nothing has run on a Raspberry Pi.** Performance numbers are desktop measurements (`docs/PERFORMANCE.md`).
- **YouTube playback is unverified** (sandbox network blocked by YouTube). Chromium on a 2 GB Pi 5 is
  expected to be tight; fallbacks are documented in `docs/YOUTUBE_ON_PI.md`.
- **Speech recognition**: with default settings and no Vosk model or cloud key, real microphone speech
  is detected but not transcribed (push-to-talk then says it didn't hear). Simulated speech always works.
- **Gesture (wave) detection** is experimental and off by default.
- **Display**: the MPI3508 may not accept 480×320 over HDMI; 720×480 fallback documented. Touch on Pi 5 unverified.
- **Power**: the power bank is unverified; a 5 V/3 A source limits USB peripherals to 600 mA total.
- **Servos**: physical motion is disabled in configuration and requires calibration plus per-session parent arming.
- **Physical toy safety is not established by software tests.** The enclosure must be evaluated for small
  parts, exposed electronics, pinch points, heat, cables and stability, and LittleBot must always be used
  with a caregiver present.
