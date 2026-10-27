# Performance on the Raspberry Pi 5 (2 GB)

The 2 GB Pi 5 is fast enough for LittleBot's face, vision and dashboard; **memory** is
the scarce resource, and Chromium playing YouTube is by far the heaviest component.
This page says how to measure, and holds the budget. Every number in a "Measured"
column is **TO MEASURE** on the real robot.

## Tools

| Tool | What it measures |
|---|---|
| `scripts/perf_monitor.py` | CPU %, RSS, threads of the running app (+ children / Chromium), system RAM, swap, temperature, CPU clock, throttling; live line + CSV + avg/max summary |
| `scripts/benchmark_face.py` (face module) | update + render cost per frame for every expression/animation, headless; its `cpu@30fps` column is the share of ONE core the face needs at 30 fps (`--frames 300 --json`, `--scale 2` adds the up-scale cost) |
| `scripts/diagnose.py --section power --section memory` | snapshot of temperature, throttling, RAM/swap |
| `vcgencmd measure_temp`, `vcgencmd measure_clock arm`, `vcgencmd get_throttled` | quick checks |
| `top -H -p "$(pgrep -of '^[^ ]*python[^ ]* -m littlebot')"` | per-thread CPU inside the app (`perf_monitor.py` prints the PID too) |

### perf_monitor.py

```bash
.venv/bin/python scripts/perf_monitor.py                         # find the app, 5 min every 2 s
.venv/bin/python scripts/perf_monitor.py --duration 15 --interval 5 --children
.venv/bin/python scripts/perf_monitor.py --wait 60 --duration 10    # start it, then start the app
```

CSV goes to `data/perf/perf-<timestamp>.csv`, the summary next to it
(`.summary.json`). `proc_cpu_pct` is per core (100 % = one core; the Pi 5 has four),
`sys_cpu_pct` is the whole system. `--children` adds processes the app starts (e.g. the
Chromium kiosk); `chromium_rss_mb` counts *all* Chromium processes either way.
`throttled_any` in the summary is every throttling flag seen during the run.

## Benchmark procedure (do it in this order)

Run each scenario for 10 minutes after a fresh boot, on the official PSU, enclosure as
it will be used, room temperature noted. Record the summary lines in the table.

1. **Baseline**: desktop only, LittleBot not running: `free -m`, `vcgencmd measure_temp`.
2. **Idle face**: `python -m littlebot --mode pi`, nobody in front of the camera.
3. **Face + vision**: someone in front of the camera (tracking active).
4. **Conversation**: speak to it (mic + TTS active, mock AI).
5. **YouTube**: start an allowed video from the dashboard (Chromium kiosk) - the worst case.
6. **Face benchmark**: `.venv/bin/python scripts/benchmark_face.py --frames 300 --json > data/perf/face-benchmark.json`
   (face module; headless, so it can run over SSH) to see the rendering headroom.
7. **Thermal soak**: scenario 5 for 30 minutes in the closed enclosure; watch `temp_c` and `throttled_any`.

## Desktop baseline (measured 2026-10-20, NOT the Pi)

Measured in the development container (x86-64, 4 vCPU, Python 3.13, OpenCV 4.14) with
`scripts/perf_monitor.py --pid <app> --interval 2 --samples 22` while running
`python -m littlebot --mode simulation` (2x window + simulator panel on SDL's dummy display,
mock camera frames through face detection at 6 Hz, simulated audio, dashboard listening):

| Metric | Value |
|---|---|
| Process RSS | 146 MB average (136–149 MB) |
| Process CPU | 13.6 % of one core average (startup spike 76 %, steady 9.5–12 %) |
| Threads | 14 |
| Face update + render | 1.2 ms/frame mean at scale 1 (`scripts/benchmark_face.py`) |
| Face detection | YuNet 6.5–8.6 ms/frame at 320 px |

The Pi 5 is roughly 2–3x slower per core than this machine, but runs at scale 1 without the
simulator panel (the 2x scaling alone costs ~2 ms/frame here). Expect the app alone to stay
well under 400 MB RSS on the Pi; Chromium during YouTube is the big unknown.

## Memory budget (2 GB)

| Component | Budget *(estimate)* | Measured |
|---|---|---|
| Kernel, firmware reservations, system services | ~150 MB | TO MEASURE |
| Desktop session (labwc, panel, PipeWire) | 150-250 MB | TO MEASURE |
| **LittleBot app** (Python, pygame, numpy, OpenCV, FastAPI) | **target <= 250 MB RSS, alarm at 350 MB** | TO MEASURE |
| Chromium kiosk playing YouTube | 400-700 MB (largest item) | TO MEASURE |
| Headroom (page cache, spikes, dashboard browser on the Pi) | >= 300 MB free | TO MEASURE |
| **Total** | < ~1.6 GB with ~0.3 GB available | TO MEASURE |

If the total is too tight: lower Chromium's footprint (one tab, kiosk mode only, 480p
video), keep swap/zram enabled (`diagnose.py --section memory`), avoid running the Pi's
own desktop browser at the same time, and consider `MemoryHigh=` in the user service
(see the comment in `scripts/systemd/littlebot.service`) once real numbers are known.

## CPU / FPS budget

| Item | Target | Measured |
|---|---|---|
| Face animating | 30 FPS (`display.fps`) using <= 25 % of one core | TO MEASURE on Pi (desktop x86: ~4 % of one core during the demo; 1.2 ms mean update+render per frame at scale 1) |
| Face idle | 15 FPS (`display.idle_fps`) using <= 10 % of one core | TO MEASURE on Pi (desktop x86: ~3 % of one core) |
| Vision (640x480 MJPG capture, 320 px detection at 15 Hz, `config/pi.yaml`) | <= 40 % of one core | **Pi 5, measured 2026-10-22** (`check_camera.py`, U20CAM, 5 s): YuNet 7.9 ms mean / 9.5 ms p95 per frame, face found in 100 % of frames, ~4 % of one core at 5 Hz; Haar 13.6 ms, face in 1 % of frames. Camera delivers 15 fps MJPG 640x480, 0 empty reads. Raised to 15 Hz (every frame) for smoother eye tracking: ~12 % of one core estimated from those numbers; **TO MEASURE** on the Pi with the app running (`perf_monitor.py --children`, a voice chat, and with the browser open). Lower it in `config/local.yaml` if the voice or temperature suffer |
| Dashboard idle | ~0 % | TO MEASURE |
| Whole app, idle face, nobody present | <= 20 % of one core | TO MEASURE |
| YouTube in Chromium (system total) | will dominate; keep `sys_cpu_pct` < 80 % | TO MEASURE |
| CPU temperature, 30 min soak in enclosure | < 75 C, `throttled_any` = 0x0 | TO MEASURE |

## Knobs (in `config/local.yaml`)

| Setting | Effect |
|---|---|
| `display.fps`, `display.idle_fps` | face frame rates (biggest steady CPU cost of the face) |
| `face.glow: false` | cheaper rendering if the face benchmark shows it is expensive |
| `camera.capture_fps`, `camera.capture_width/height` | capture cost; 640x480 MJPG is plenty |
| `camera.detect_fps`, `camera.process_width` | detection cost |
| `vision.opencv_threads` | OpenCV worker threads (leave cores for the face and audio) |
| `media.player: none` | disables YouTube entirely (saves the most memory) |

## Results log

| Date | OS / kernel | Scenario | App CPU avg/max | App RSS avg/max | Chromium RSS | Avail RAM min | Temp max | throttled_any | Notes |
|---|---|---|---|---|---|---|---|---|---|
| TO MEASURE | | | | | | | | | |
