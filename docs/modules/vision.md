# Module: vision (camera, face detection, tracking, presence)

`littlebot/vision/` lets LittleBot notice that someone is there, look at them, and (experimentally)
notice a wave. It does **face detection only**: where faces are in the current frame. There is no
recognition, identification, age/emotion estimation or any other biometric profiling, and frames
are never stored or sent anywhere.

```
littlebot/vision/
  __init__.py     create_service(ctx) -> VisionService
  service.py      VisionService (VisionAPI + Service): capture thread, merge sim input, publish events
  camera.py       OpenCVCamera (V4L2, reader thread) / MockCamera / FixtureCamera, discovery, probe
  detector.py     Detection, HaarFaceDetector, YuNetFaceDetector, MockDetector, NullDetector, create_detector
  tracker.py      FaceTracker + GazeMapper: primary face, smoothing, mirroring, presence hysteresis (pure logic)
  gestures.py     GestureDetector: EXPERIMENTAL frame-differencing motion / wave heuristics
  imaging.py      optional-cv2 helpers, resize/grey, RateMeter, numpy-only synthetic drawing
scripts/check_camera.py      list devices, open one, negotiated format, capture fps, detector timings
scripts/download_models.py   YuNet ONNX / Haar XML into models/ (git-ignored), SHA-256 pinned
tests/vision/                82 tests (mocks, fake cv2 driver, fixtures, threads)
```

OpenCV is an optional extra (`pip install -e ".[vision]"`). Without it the service still runs:
mock camera in simulation/offline, `unavailable` on the Pi. It never crashes the robot.

## Public API (`core.interfaces.VisionAPI`)

| Method | Notes |
|---|---|
| `simulate_face(x, y, size=0.25)` | Face **centred** at (x, y) in normalised **image** coords; `size` = face width / frame width. Goes through the same tracker/event path as a camera face. Overrides camera detections while active; expires after `vision.simulated_face_seconds` (3 s) unless called again. |
| `simulate_no_face()` | Drops the simulated face now (camera faces, if any, take over; otherwise `person_left` follows after `left_after_seconds`). |
| `status()` | `{camera: ok\|disconnected\|disabled\|unavailable, detail, backend: opencv\|mock\|fixture\|none, detector: yunet\|haar\|mock\|none, detector_detail, device, resolution: "640x480"\|None, fourcc, fps (measured capture), detect_fps (measured), detect_ms (last), faces (count), person_present, gaze: {x, y}\|None, gestures_enabled, simulated}`. Cheap, thread-safe, JSON-serialisable. |

Lifecycle: `await start()` spawns the `littlebot-vision` daemon thread; `await stop()` joins it
(4 s timeout, off the event loop) and releases the camera. Both are idempotent and the service is
restartable. `health()` reports `running` (real camera), `degraded` (mock/fixture camera, camera
unavailable or disconnected) or `disabled` (privacy off / `camera.backend: none`).

## Events (all `source="vision"`)

| Topic | Payload | When |
|---|---|---|
| `vision.faces` | `{faces: [{x, y, w, h, gaze_x, gaze_y, score, primary}], frame_w, frame_h, simulated}` | every detection while faces are visible (<= `detect_fps`, hard cap 15/s), plus **one** `faces: []` when they disappear. Nothing while nobody is there. |
| `vision.person_present` | `{gaze_x, gaze_y}` | once, after faces were seen for `present_after_seconds` |
| `vision.person_left` | `{}` | once, after no face for `left_after_seconds` |
| `vision.gesture` | `{gesture: "wave"\|"motion", confidence}` | experimental, only if `vision.gestures_enabled` |
| `vision.camera_status` | `{status, detail, backend}` | on change only |

`x, y, w, h` are pixels in a `frame_w x frame_h` frame (the capture size). `faces[0]` is the
**primary** face (`primary: true`) and carries the **smoothed** gaze; other faces carry their raw
mapped gaze. `gaze_x/gaze_y` are screen coordinates for `FaceAPI.look_at` (0..1, 0.5 = centre).
`score`, `primary` and `simulated` are additions to the documented payload (extra keys only).

## Pipeline (one thread, latest frame only)

```
capture thread, every cycle (sleeps on an Event between cycles; woken by privacy / simulate / stop):
  manage camera  privacy off or backend none -> release now, status "disabled"
                 not open and retry due     -> open (auto: real -> mock fallback in simulation/offline)
  frame due?     (1/detect_fps, or 1/gesture_fps when gestures are on)
    read newest frame (OpenCVCamera.read(timeout)) -> driver error / no frame for stall_seconds -> "disconnected"
    downscale to camera.process_width (320 px, INTER_AREA) - the full frame is dropped right here
    detection due? -> detector.detect(small)                    (<= detect_fps)
    gestures on?   -> GestureDetector.update(small, primary face)
  merge          active simulated face overrides camera faces
  tracker tick   (on new detection / sim change / every 1/detect_fps) -> publish events
```

`OpenCVCamera` owns the device in its own small reader thread: it `grab()`s every frame the
camera sends (cheap, no decode; keeps the driver queue empty so frames are never stale) and only
`retrieve()`s (decodes) when the vision thread asks. So at 15 fps capture and 6 detections/s, only
6 MJPG frames/s are decoded. `read(timeout)` never blocks longer than its timeout; a driver
that stops answering is detected after `camera.stall_seconds` (3 s). Only the newest frame
is held, and only until it is handed over.

## Camera backends and device selection

| `camera.backend` | Behaviour |
|---|---|
| `auto` (default) | real camera; if it cannot be opened at start: **mock camera** in `simulation`/`offline` mode, **`unavailable`** on the Pi (retried every `reconnect_seconds`). Once a real camera worked, failures mean `disconnected` + retry, never a silent switch to mock. |
| `real` | real camera only: `unavailable` / `disconnected` + retry |
| `mock` | `MockCamera` (synthetic frames; `camera.mock_face: true` draws a wandering cartoon face) |
| `none` | no camera; status `disabled`. Simulated faces still work. |
| `camera.fixture: PATH` | image / directory / video file instead of a camera (offline tuning) |

With the mock camera the service uses a `MockDetector` fed by the mock camera's ground truth
(running a real detector on synthetic frames would only burn CPU). With a real or fixture camera
it uses the configured detector.

`camera.device: auto` picks the first USB capture node from sysfs (UVC cameras create two nodes; the
metadata node and the Pi 5's `rpi-hevc-dec` / `pispbe` nodes are skipped), falling back to index 0.
An index, `/dev/videoN` or a stable `/dev/v4l/by-id/...-video-index0` path also work. On Linux the
V4L2 backend is used and the node's existence is checked before OpenCV touches it, so a missing
camera costs nothing and prints no OpenCV warnings. Format: `fourcc` (MJPG) is set first, then
size, then rate; the negotiated values are read back and reported (`status()`, `check_camera.py`).

## Detectors

`create_detector(kind)` never raises. Order: requested -> the other real detector -> `none`.
The reason for every fallback is in `status()["detector_detail"]` and the log.

| Detector | Needs | Notes |
|---|---|---|
| `yunet` (default in `config/default.yaml`) | `cv2.FaceDetectorYN` + `models/face_detection_yunet_2023mar.onnx` (`scripts/download_models.py`, 230 KB, MIT) | small CNN; finds tilted / partly turned faces; 2023mar model needs OpenCV >= 4.8 |
| `haar` | `cv2.CascadeClassifier` (**OpenCV 4.x only**) + `haarcascade_frontalface_default.xml` (searched in `cv2.data`, `/usr/share/opencv4/haarcascades` (apt `opencv-data`), `models/`) | frontal faces only; `haar_scale_factor` 1.2 (1.1 = slightly more faces, ~2x CPU) |
| `none` | - | presence/gaze only from `simulate_face` |

Both real detectors run a self-test on a blank frame when created, so an incompatible
model/OpenCV pair (e.g. the 2023mar model on OpenCV 4.6) fails at start-up and falls back,
instead of failing on every frame. Faces narrower than `vision.min_face_fraction` (8 % of the
frame width, i.e. roughly people within ~1.3 m with a ~70 deg lens) are ignored on purpose: the
robot attends to the child in front of it, not to people across the room.

**OpenCV 5 caveat.** The `opencv-python-headless` 5.x wheels (5.0.0 is in the dev venv) no longer
contain `CascadeClassifier` or the Haar XML files. With them, Haar is unavailable and YuNet is the
only detector, so it needs `scripts/download_models.py`. With apt `python3-opencv` (4.x on
Bookworm/Trixie) Haar works out of the box.

## CPU budget (Raspberry Pi 5, 2 GB)

Design choices: 640x480 MJPG capture (USB 2.0 friendly, cheap to decode), detection on a 320 px
copy at 6 Hz (5 Hz in `pi.yaml`), decode only the frames we process, `vision.opencv_threads: 2` so
detection never takes every core from the face renderer and audio, and no work at all while the
camera is disabled.

Measured in the dev container (x86 Xeon 2.8 GHz, 4 vCPU; **not the Pi**), with
`check_camera.py --fixture` and a scratch benchmark of the full service (frames already decoded):

| | per detection (320 px) | full service at 6 Hz, process CPU |
|---|---|---|
| YuNet, OpenCV 5.0.0 | 6.5-8.6 ms (2-4 threads), 10-14 ms (1 thread) | **8.6 %** of one core |
| YuNet, OpenCV 4.14 | ~6.4 ms | 10.4 % |
| Haar, OpenCV 4.14, scale 1.2 | 17-34 ms (2 threads), 29-56 ms (1 thread) | 22.5 % |
| Haar, scale 1.1 (OpenCV default) | 32-70 ms (2 threads), 53-123 ms (1 thread) | ~2x the above |
| no detector (loop + resize) | - | 0.6 % |

A Pi 5 core is roughly 1.5-2.5x slower than this machine for this kind of code, so expect about
15-25 % of one core for YuNet and 35-55 % for Haar at 6 Hz, plus ~2-4 % for MJPG decoding.
**These Pi figures are estimates until measured on the hardware** (`check_camera.py` prints them).
That puts YuNet well within the `<= 40 % of one core` target in `docs/PERFORMANCE.md`; Haar is
borderline, which is why YuNet is the default. To cut further: `camera.detect_fps: 3`, or
`camera.process_width: 256` (YuNet still finds faces down to ~20 px).

## Gaze mapping and mirroring

The camera and the screen both face the child. The camera image is the robot's point of view:
something on the robot's own right appears on the image's **right**. The screen is seen from
the front, so the robot's right is the viewer's **left** (screen x near 0). For the pupils to
point at a child sitting to the robot's right (`cx` near 1), they must move toward screen-left:

```
gaze_x = 1 - cx          (camera.mirror: true, default)
gaze_x = cx              (mirror: false - only if the driver already delivers a mirrored image)
gaze_y = face_top + 0.4 * face_height + vision.gaze_vertical_offset
```

Vertical needs no flip. The 0.4 eye line makes the robot look at the child's eyes, not the box
centre (nose/mouth). `gaze_vertical_offset` can compensate for the camera being mounted above or
below the screen. **To verify on the robot**: sit to the robot's right; its pupils must move
toward its right-hand side of the screen. If they move away, set `camera.mirror: false`.

Smoothing: `gaze = s * previous + (1 - s) * new` per detection (`vision.gaze_smoothing` s = 0.35,
0 = raw). A newly appearing face snaps the gaze to it instead of sweeping across the room.
Primary face: the largest (closest), but the current one is kept unless another face is
>= 1.3x larger. That avoids flip-flopping between two children.

Presence hysteresis: `person_present` needs faces for `present_after_seconds` (0.6 s, gaps up
to 0.5 s tolerated); a single false detection never "arrives". `person_left` needs no face for
`left_after_seconds` (3 s); a turned head or a missed frame never "leaves". Timing is evaluated
on tracker ticks (every 1/detect_fps), so events can be up to one tick later than the threshold.

## Simulation

`simulate_face(x, y, size)` creates a detection that flows through the tracker exactly like a
camera face (smoothing, hysteresis, mirroring, events), so the eyes follow in the simulator and
`behaviours.py` reacts. It overrides the real camera while active, which keeps the simulator
predictable on a desktop with a webcam. It expires after `vision.simulated_face_seconds` (3 s),
so the simulator must refresh it, for example on mouse move. It works with any camera backend,
including `none` and privacy off, because it is not camera data.

## Privacy guarantees

* `ctx.privacy.camera_enabled` is read at start and on every `privacy.changed`: when it becomes
  false the vision thread is woken, any pending read is cancelled and the device is **released
  within one cycle** (tested: < 0.3 s), all detections and the gesture reference frame are
  dropped, and nothing is captured or processed until it is re-enabled (then the camera is
  reopened at once).
* Frames exist only as local variables for one cycle; the full frame is dropped right after
  downscaling. No queue, no ring buffer, no disk writes, no network. The only image ever kept
  is the gesture analyser's previous 80x60 grey frame (for differencing), cleared on release.
* Detection only. YuNet's facial landmarks are discarded immediately; no embeddings, no
  identity, no age/emotion/attribute estimation, no face recognition models.
* `check_camera.py` writes an image only with `--save PATH`, exactly one frame, and says so.
* Status and events contain only box coordinates, counts and rates.

## Disconnects

A driver error, the camera's reader thread dying, or no frame for `camera.stall_seconds` (3 s)
-> release the device, publish `camera_status: disconnected`, retry every `camera.reconnect_seconds`
(3 s). A read timeout on its own is *not* a disconnect: UVC cameras drop to a few fps under
auto-exposure in a dim room, and that must not make the robot "lose" its camera.
A camera that was never opened is `unavailable` (same retry). Exceptions in the detector or the
gesture code are logged (rate-limited) and contained; the thread keeps running.

## Experimental gestures (`vision.gestures_enabled`, off by default)

Frame differencing on an 80 px grey copy (~0.3 ms/frame) at `vision.gesture_fps` (10 Hz).
`motion` = more than 3 % of pixels changed in two consecutive frames (5 s cooldown). `wave` = the
horizontal centroid of motion beside/above the detected face (the face box itself is excluded)
reverses direction >= 3 times with an amplitude >= 0.4 face widths within 2 s (3 s cooldown).

**Honest status: untested with real children, rooms or the real camera.** On synthetic blob
sequences it detects waves of 0.7-2 Hz with amplitude >= ~0.35 face widths and ignores static
scenes, sensor noise, one-off sweeps, slow drifts and motion inside the face box. Faster waves
(>= 2.5 Hz) alias at 10 fps and are missed. Expect false positives from people walking past,
swinging toys and light flicker. Expect misses from babies' typical "waves" (opening/closing
a hand, two-arm flapping). It needs a detected face. Use it only for playful, low-stakes
reactions.

## Configuration (all keys)

`camera`: `backend`, `device`, `capture_width/height/fps`, `fourcc`, `process_width`, `detect_fps`,
`mirror`, `reconnect_seconds`, `stall_seconds`, `fixture`, `mock_face`.
`vision`: `detector`, `min_face_fraction`, `present_after_seconds`, `left_after_seconds`,
`gaze_smoothing`, `gestures_enabled`, `yunet_model`, `yunet_score_threshold`, `haar_cascade`,
`haar_min_neighbors`, `haar_scale_factor`, `opencv_threads`, `gaze_vertical_offset`,
`simulated_face_seconds`, `gesture_fps`. Parent settings: `vision.gestures_enabled` (live toggle),
`vision.follow_faces` (used by `app/behaviours.py`).

## Tools

```
.venv/bin/python scripts/check_camera.py --list           # devices (never opens one); exit 2 = no camera
.venv/bin/python scripts/check_camera.py                  # configured device: format, fps, detector timings
.venv/bin/python scripts/check_camera.py --fixture x.jpg  # detector benchmark without a camera
.venv/bin/python scripts/check_camera.py --save /tmp/f.jpg  # ONE frame with boxes (delete afterwards)
.venv/bin/python scripts/download_models.py               # YuNet into models/ (sha256 verified)
```

## What is verified vs not

Verified here (no camera in the container): all logic with `MockCamera`, `FixtureCamera`,
`MockDetector` and a **fake cv2 driver** that exercises `OpenCVCamera`'s reader thread
(negotiation readback, decode-on-demand, read timeouts, stalls, driver errors, release).
Covered: service lifecycle and threads, throttling, disconnect/reconnect, privacy release,
simulation expiry, the events -> `behaviours.py` -> `face.look_at` path, and the scripts.
The suite passes on OpenCV 5.0.0 (dev venv; Haar tests skip) and on OpenCV 4.14 (scratch venv;
Haar + YuNet run). Real detectors on public OpenCV sample photos (not committed): Haar and YuNet
both find the face in `lena.jpg` (YuNet score 0.87); neither fires on `fruits`, `baboon`, `home`,
`basketball1`. The small face in `messi5.jpg` (about 18 px at 320 px) is below
`min_face_fraction`; YuNet finds it (0.85) with the filter lowered to 0.04.

**Unverified (needs the Pi + InnoMaker U20CAM-1080P):** device auto-selection on the real
Pi 5 node layout (modelled from known sysfs names), MJPG negotiation and the real frame rate, V4L2
read-timeout behaviour on unplug, Pi CPU figures, mirroring direction on the assembled robot,
detection quality on a real baby (small, round, often tilted or partly turned faces; YuNet is
expected to cope better than Haar), lighting in the nursery, gestures. Bring-up checklist:
`check_camera.py --list`, then `check_camera.py --seconds 10` (expect MJPG 640x480, ~15-30 fps,
detect times as above), then the mirroring check above.
