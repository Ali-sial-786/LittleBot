# LittleBot – next steps (prioritised)

Status of what exists today is in `PROJECT_STATUS.md`. This is the ordered to-do list.

## P0 – the day the hardware arrives

Follow `HARDWARE_ARRIVAL_CHECKLIST.md` top to bottom. In short:

1. **Power first.** Bring the Pi 5 up on the official 27 W USB-C PSU. Only then test the power bank
   under load while watching `scripts/diagnose.py` (under-voltage / throttling flags). See `docs/POWER.md`.
2. **Display.** HDMI video first (`docs/DISPLAY_SETUP.md`). If the panel rejects 480×320, use the
   documented 720×480 fallback (same 3:2 shape; the face letterboxes correctly). Do **not** run the
   vendor's LCD-show script. Touch is optional.
3. **Install.** `scripts/install_pi.sh` (dry-run first), then `scripts/diagnose.py`.
4. **Devices.** `scripts/check_camera.py`, `scripts/check_audio.py --play`, `--record 3`;
   put device names in `config/local.yaml` (template: `config/hardware.example.yaml`).
5. **First run.** `python -m littlebot --mode pi`, claim the dashboard with the setup code shown on
   the console, set the child's birth month.
6. **Measure.** `scripts/benchmark_face.py`, `scripts/perf_monitor.py` for 30 min with the face idle,
   with a person in view, and during a YouTube video. Fill in the TO MEASURE rows in `docs/PERFORMANCE.md`.
7. **Report back** the outputs listed at the end of the checklist so the next session can tune.

## P1 – verify what could only be mocked here

* Real USB speaker/mic on Pi OS (PipeWire vs ALSA device names), echo/half-duplex timing, VAD threshold
  in a real room (`audio.microphone.vad_threshold`), espeak-ng voice choice (try `en-us+f3`, `en-gb+f4`,
  pitch 55–75); consider installing Piper for a warmer voice and measure its latency.
* Camera: confirm `camera.mirror` direction on the assembled robot (one-minute check in
  `docs/modules/vision.md`), real MJPG fps, YuNet CPU on the Pi, detection of a baby at play distance.
* YouTube: real playback in Chromium kiosk on the Pi (could not play here: YouTube refuses embeds from
  data-centre IPs/headless browsers). Measure RAM/CPU; if too heavy, use the "open player on this
  device" fallback (`media.player: none`) or audio-only songs. `docs/YOUTUBE_ON_PI.md`.
* Cloud AI: add an API key to `.env` (separate paid API account), enable in the dashboard, press
  **Test**. Check latency and the daily budget numbers.
* Touchscreen (MPI3508 resistive, SPI): only after HDMI works; overlay parameters on Pi 5 are unverified.

## P2 – servos (only after P0/P1 and the safety doc)

1. Read `docs/MOTION_SAFETY.md`; build the separate servo supply with a common ground.
2. `scripts/calibrate_servos.py --simulate`, then on hardware with horns removed.
3. Set `motion.physical_enabled: true` (backend still `mock`), calibrate, then `backend: pca9685`.
4. A parent arms motion from the dashboard each session.
5. `python -m littlebot.motion off` switches every PCA9685 output off directly (used by
   `scripts/safe_shutdown.sh --force`); verify it on the real chip during calibration.

## P3 – software improvements

* Dashboard: "Suggested for her" section using `activities.recommended()`; drag-to-reorder approved videos.
* Gestures: the wave detector is experimental – collect real (non-recorded) tuning feedback before
  enabling `vision.gestures_enabled` by default.
* Optional local STT (Vosk small model) for older-child conversation without the cloud; measure RAM.
* More activities as she grows (the YAML format is documented in `docs/ACTIVITY_GUIDE.md`;
  parent-written scripts go in `data/activities/`, `python -m littlebot.activities` validates them).
* Melodies are simple arrangements; refine any that sound off on the real speaker.
* Enclosure: evaluate small parts, exposed electronics, pinch points, heat, cable routing, stability and
  battery safety before the child plays with it unsupervised **at any distance** (it must always be supervised).

## How to resume development in a new Claude Code session

```text
Read PROJECT_STATUS.md, NEXT_STEPS.md, ARCHITECTURE.md and docs/DEV_CONVENTIONS.md in the LittleBot
repo, run `.venv/bin/python -m pytest -q`, then continue with the highest-priority unfinished item in
NEXT_STEPS.md. If I pasted hardware outputs, start by tuning config/local.yaml from them.
```
