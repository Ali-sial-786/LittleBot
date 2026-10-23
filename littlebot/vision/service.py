"""VisionService: camera -> face detection -> tracking -> bus events.

Everything runs in ONE daemon thread ("littlebot-vision"); the asyncio loop is
never blocked. Per cycle the thread:

1. manages the camera: open / reconnect every ``camera.reconnect_seconds`` /
   release immediately when privacy turns the camera off;
2. grabs the newest frame at most at ``detect_fps`` (or ``gesture_fps`` when
   the experimental gestures are on), downscales it to ``process_width`` and
   runs the detector at most at ``detect_fps`` (CPU budget for the Pi);
3. merges simulated faces (``simulate_face``) - they take precedence over the
   camera while active and expire after ``vision.simulated_face_seconds``;
4. updates the tracker (primary face, smoothing, presence hysteresis) and
   publishes ``vision.*`` events.

Frames live only in local variables for one cycle: nothing is buffered,
stored, sent anywhere, or used for identification. The one exception is
``capture_photo``: on request (one at a time, e.g. a parent asks ChatGPT "how many
fingers?") the thread JPEG-encodes a downscaled copy of the NEXT frame and hands
the bytes to that one caller; nothing is kept here or written anywhere.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from typing import Any, Callable

from littlebot.core.config import Mode
from littlebot.core.context import RobotContext
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.vision.camera import CameraDevice, MockCamera, create_camera
from littlebot.vision.detector import (
    Detection, FaceDetector, MockDetector, NullDetector, configure_opencv_threads, detector_from_config,
)
from littlebot.vision.gestures import GestureDetector
from littlebot.vision.imaging import RateMeter, brighten_if_dark, get_cv2, resize_to_width
from littlebot.vision.tracker import FaceTracker, TrackedFace, TrackerUpdate

CameraFactory = Callable[[str], CameraDevice]

MAX_FACES_EVENT_HZ = 15.0  # hard cap for vision.faces events
IDLE_WAIT = 1.0  # longest sleep when nothing is going on (events wake the thread earlier)
PHOTO_RETRY = 0.05  # retry interval while a photo request waits for a frame


class PhotoUnavailable(Exception):
    """``capture_photo`` could not take a photo. ``reason``: camera_off | no_camera | timeout | unavailable."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


PhotoRequest = tuple["concurrent.futures.Future[bytes]", int, int]  # (future, max_width, jpeg quality)


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


class VisionService(Service):
    name = "vision"

    def __init__(self, ctx: RobotContext, *, camera_factory: CameraFactory | None = None,
                 detector: FaceDetector | None = None, gestures: GestureDetector | None = None,
                 clock: Callable[[], float] | None = None, join_timeout: float = 4.0) -> None:
        super().__init__()
        self.ctx = ctx
        self.cam_cfg = ctx.config.camera
        self.vis_cfg = ctx.config.vision
        self._clock = clock or time.monotonic
        self._camera_factory: CameraFactory = camera_factory or (
            lambda kind: create_camera(self.cam_cfg, kind, clock=self._clock))
        self._injected_detector = detector
        self._real_detector: FaceDetector | None = None
        self._detector: FaceDetector = detector or NullDetector("not loaded yet")
        self._backend = self.cam_cfg.backend
        self._kind = self._initial_kind()
        self._allow_mock_fallback = ctx.config.mode in (Mode.SIMULATION, Mode.OFFLINE)
        self._join_timeout = join_timeout
        self._tracker = FaceTracker(
            smoothing=min(0.99, max(0.0, self.vis_cfg.gaze_smoothing)),
            present_after=self.vis_cfg.present_after_seconds,
            left_after=self.vis_cfg.left_after_seconds,
            mirror=self.cam_cfg.mirror,
            vertical_offset=self.vis_cfg.gaze_vertical_offset,
            gap_tolerance=self._gap_tolerance(float(self.cam_cfg.detect_fps)),
        )
        self._gestures = gestures or GestureDetector()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._subs: list[Subscription] = []
        # camera
        self._camera: CameraDevice | None = None
        self._camera_allowed = True
        self._ever_opened = False
        self._next_open_at = 0.0
        self._read_fail_since: float | None = None
        self._cam_status: tuple[str, str] = ("starting", "")
        self._cam_info: dict[str, Any] = {}
        self._frame_size = (int(self.cam_cfg.capture_width), int(self.cam_cfg.capture_height))
        self._read_timeout = min(1.0, max(0.25, 2.5 / max(1.0, float(self.cam_cfg.capture_fps))))
        # timing / detections
        self._next_frame_at = 0.0
        self._next_detect_at = 0.0
        self._next_track_at = 0.0
        self._detect_rate = RateMeter(window=3.0)
        self._camera_dets: list[Detection] = []
        self._camera_dets_at: float | None = None
        self._detect_errors = 0
        self._cycle_errors = 0
        # simulation
        self._sim_face: Detection | None = None
        self._sim_until = 0.0
        self._sim_dirty = False
        # gestures (experimental)
        self._gestures_on = bool(self.vis_cfg.gestures_enabled)  # parent setting
        self._gestures_reset = False
        # robot-state dependent work level: active | reduced (SLEEPING) | paused (PLAYING_MEDIA)
        self._robot_state = ctx.state.state.value
        self._mode = self._mode_for(self._robot_state)
        # publishing
        self._last_faces_pub = -1e9
        self._faces_visible = False
        self._last_faces_count = 0
        self._snapshot: dict[str, Any] = {}
        # one-shot photo requests, fulfilled by the vision thread from the next frame (guarded by _lock)
        self._photo_requests: list[PhotoRequest] = []
        self._refresh_snapshot(self._clock())

    # ------------------------------------------------------------------ setup
    def _initial_kind(self) -> str:
        if self._backend == "none":
            return "none"
        if self._backend == "mock":
            return "mock"
        if self.cam_cfg.fixture:
            return "fixture"
        return "real"

    def _backend_label(self) -> str:
        return {"real": "opencv", "mock": "mock", "fixture": "fixture", "none": "none"}[self._kind]

    def _mode_for(self, robot_state: str | None) -> str:
        if robot_state == "PLAYING_MEDIA" and self.vis_cfg.pause_during_media:
            return "paused"  # the video player needs the CPU; camera stays open, nothing is decoded
        if robot_state == "SLEEPING":
            return "reduced"  # still notice a person arriving (wakes the robot), at a low rate
        return "active"

    @staticmethod
    def _gap_tolerance(detect_fps: float) -> float:
        # Missed-detection tolerance must exceed the detection interval, or slow rates never "arrive".
        return max(0.5, 1.5 / max(0.1, detect_fps))

    @property
    def effective_detect_fps(self) -> float:
        rate = max(0.1, float(self.cam_cfg.detect_fps))
        if self._mode == "reduced":
            rate = min(rate, max(0.1, float(self.vis_cfg.sleep_detect_fps)))
        return rate

    @property
    def gestures_active(self) -> bool:
        return self._gestures_on and self._mode == "active"

    @property
    def frame_interval(self) -> float:
        rate = self.effective_detect_fps
        if self.gestures_active:
            rate = max(rate, float(self.vis_cfg.gesture_fps))
        return 1.0 / rate

    @property
    def detect_interval(self) -> float:
        return 1.0 / self.effective_detect_fps

    # -------------------------------------------------------------- lifecycle
    async def start(self, *, run_thread: bool = True) -> None:
        """Start the capture thread. ``run_thread=False`` only binds events (tests drive ``process_once``)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self.set_status(ServiceStatus.STARTING)
        self._stop_evt.clear()
        self._camera_allowed = bool(self.ctx.privacy.camera_enabled)
        self._gestures_on = bool(self.ctx.settings.get("vision.gestures_enabled", self.vis_cfg.gestures_enabled))
        self._robot_state = self.ctx.state.state.value
        if not self._subs:
            bus = self.ctx.bus
            self._subs = [bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
                          bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
                          bus.subscribe(Topics.STATE_CHANGED, self._on_state)]
        if run_thread:
            self._thread = threading.Thread(target=self._run, name="littlebot-vision", daemon=True)
            self._thread.start()

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs = []
        self._stop_evt.set()
        self._wake.set()
        cam = self._camera
        if cam is not None:
            cam.cancel_read()
        t = self._thread
        if t is not None and t.is_alive():
            await asyncio.to_thread(t.join, self._join_timeout)
            if t.is_alive():
                self.log.warning("vision thread did not stop within %.1fs", self._join_timeout)
                return
        self._thread = None
        self._close_camera()  # no-op if the thread already released it
        self._fail_photos("no_camera", "vision stopped")
        self.set_status(ServiceStatus.STOPPED)

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        self.log.debug("vision thread started")
        try:
            while not self._stop_evt.is_set():
                self._wake.clear()
                now = self._clock()
                try:
                    delay = self._cycle(now)
                    self._cycle_errors = 0
                except Exception:  # noqa: BLE001 - keep the robot running
                    self._cycle_errors += 1
                    if self._cycle_errors in (1, 10) or self._cycle_errors % 100 == 0:
                        self.log.exception("vision cycle failed (%d in a row)", self._cycle_errors)
                    self._camera_lost(now, "internal error")
                    delay = IDLE_WAIT
                if self._stop_evt.is_set():
                    break
                if delay > 0:
                    self._wake.wait(delay)
        finally:
            self._close_camera()
            self.log.debug("vision thread stopped")

    def process_once(self, now: float | None = None) -> float:
        """Run one capture/track cycle synchronously (tests). Returns seconds until the next is due."""
        return self._cycle(self._clock() if now is None else now)

    # ------------------------------------------------------------- bus input
    def _on_privacy(self, event: Event) -> None:
        allowed = bool(event.get("camera_enabled", True))
        if allowed == self._camera_allowed:
            return
        self._camera_allowed = allowed
        self.log.info("camera %s by privacy settings", "enabled" if allowed else "disabled")
        cam = self._camera
        if not allowed and cam is not None:
            cam.cancel_read()  # the vision thread releases the device right away
        self._wake.set()

    def _on_state(self, event: Event) -> None:
        new = event.get("new")
        if new and new != self._robot_state:
            self._robot_state = str(new)
            self._wake.set()  # the vision thread applies the new work level on its next cycle

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "vision.gestures_enabled":
            self._gestures_on = bool(event.get("value"))
            self._gestures_reset = True
            self._wake.set()

    # -------------------------------------------------------------- VisionAPI
    def simulate_face(self, x: float, y: float, size: float = 0.25) -> None:
        """Simulated face centred at (x, y) in normalised IMAGE coords, ``size`` = face width / frame width.
        Overrides camera detections until it expires (``vision.simulated_face_seconds``) unless refreshed."""
        fw, fh = self._frame_size
        w = min(1.0, max(0.02, float(size)))
        det = Detection.centred(_clamp01(float(x)), _clamp01(float(y)), w, w * fw / max(1, fh))
        with self._lock:
            self._sim_face = det
            self._sim_until = self._clock() + max(0.0, float(self.vis_cfg.simulated_face_seconds))
            self._sim_dirty = True
        self._wake.set()

    def simulate_no_face(self) -> None:
        with self._lock:
            self._sim_face = None
            self._sim_dirty = True
        self._wake.set()

    async def capture_photo(self, *, max_width: int = 768, quality: int = 80, timeout: float = 3.0) -> bytes:
        """JPEG bytes of the NEXT camera frame, downscaled to ``max_width`` (never upscaled).

        Taken by the vision thread only because of this call; the bytes go to the caller alone and
        are never kept, logged or written to disk here. Raises ``PhotoUnavailable`` when the camera
        privacy switch is off (camera_off), there is no open camera (no_camera), no frame arrives
        within ``timeout`` (timeout) or OpenCV cannot encode (unavailable)."""
        if not self.ctx.privacy.camera_enabled or not self._camera_allowed:
            raise PhotoUnavailable("camera_off", "camera turned off in privacy settings")
        if self._kind == "none":
            raise PhotoUnavailable("no_camera", "disabled in config (camera.backend: none)")
        status, detail = self._cam_status
        if self._camera is None and status in ("unavailable", "disconnected"):
            raise PhotoUnavailable("no_camera", detail)
        if get_cv2() is None:
            raise PhotoUnavailable("unavailable", "OpenCV is required to take photos")
        fut: concurrent.futures.Future[bytes] = concurrent.futures.Future()
        req: PhotoRequest = (fut, max(64, int(max_width)), min(95, max(20, int(quality))))
        with self._lock:
            self._photo_requests.append(req)
        self._wake.set()
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.TimeoutError:
            raise PhotoUnavailable("timeout", f"no camera frame within {timeout:g}s") from None
        finally:
            with self._lock:
                if req in self._photo_requests:
                    self._photo_requests.remove(req)
            fut.cancel()  # no-op when done; a late frame is then simply dropped

    def _photo_pending(self) -> bool:
        return bool(self._photo_requests)

    def _serve_photos(self, frame: Any) -> None:
        """Vision thread: encode ``frame`` for every waiting request, then forget it."""
        with self._lock:
            reqs, self._photo_requests = self._photo_requests, []
        if not reqs:
            return
        cv2 = get_cv2()
        for fut, width, quality in reqs:
            if fut.done():
                continue
            try:
                if cv2 is None:
                    raise RuntimeError("OpenCV unavailable")
                img = brighten_if_dark(resize_to_width(frame, width))  # dim rooms: lift it so the AI can see
                ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                del img
                if not ok:
                    raise RuntimeError("imencode failed")
                _resolve(fut, buf.tobytes())
                del buf
            except Exception as e:  # noqa: BLE001 - a photo must never break vision
                _reject(fut, PhotoUnavailable("unavailable", f"could not encode the photo ({type(e).__name__})"))

    def _fail_photos(self, reason: str, detail: str = "") -> None:
        if not self._photo_requests:
            return
        with self._lock:
            reqs, self._photo_requests = self._photo_requests, []
        for fut, _w, _q in reqs:
            _reject(fut, PhotoUnavailable(reason, detail))

    def _photo_while_paused(self, cam: CameraDevice) -> None:
        try:
            frame = cam.read(timeout=self._read_timeout)
        except Exception as e:  # noqa: BLE001
            self.log.warning("camera read failed: %s", e)
            return
        if frame is not None:
            self._serve_photos(frame)
            del frame

    def status(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self._snapshot)
        out["gaze"] = dict(out["gaze"]) if out.get("gaze") else None
        return out

    # ------------------------------------------------------------------ cycle
    def _cycle(self, now: float) -> float:
        if self._gestures_reset:
            self._gestures_reset = False
            self._gestures.reset()
        self._apply_mode(now)
        self._manage_camera(now)
        if self._mode == "paused":
            # No reads (so no MJPG decoding), no detection, no events; presence is frozen so a video
            # does not cause a fake "left" + "arrived" pair. Still notice an unplugged camera.
            cam = self._camera
            if cam is not None and (cam.failed or not cam.is_open):
                self._camera_lost(now, "camera stopped delivering frames")
            elif cam is not None and self._photo_pending():
                self._photo_while_paused(cam)  # one frame for a requested photo, nothing else
            self._refresh_snapshot(now)
            if self._camera is not None and self._photo_pending():
                return PHOTO_RETRY
            if self._camera is None and self._kind != "none" and self._camera_allowed:
                return min(IDLE_WAIT, max(0.0, self._next_open_at - now))
            return IDLE_WAIT
        detected = False
        cam = self._camera
        if cam is not None and (now >= self._next_frame_at or self._photo_pending()):
            if now >= self._next_frame_at:  # a requested photo reads one extra frame right away
                self._next_frame_at = self._advance(self._next_frame_at, self.frame_interval, now)
            detected = self._capture(cam, now)

        sim = self._active_sim(now)
        if detected or self._sim_dirty or now >= self._next_track_at:
            self._next_track_at = now + self.detect_interval
            self._sim_dirty = False
            if sim is not None:
                faces = [sim]
            elif self._camera is not None and self._camera_dets_at is not None \
                    and now - self._camera_dets_at <= 2.5 * self.detect_interval:  # fresh only
                faces = self._camera_dets
            else:
                faces = []
            upd = self._tracker.update(faces, now)
            self._publish(upd, now, simulated=sim is not None)
        self._refresh_snapshot(now)
        return self._next_delay(now)

    def _apply_mode(self, now: float) -> None:
        mode = self._mode_for(self._robot_state)
        if mode == self._mode:
            return
        old, self._mode = self._mode, mode
        self.log.info("vision %s -> %s (robot %s, detection %.1f/s)", old, mode, self._robot_state,
                      0.0 if mode == "paused" else self.effective_detect_fps)
        self._tracker.gap_tolerance = self._gap_tolerance(self.effective_detect_fps)
        self._gestures.reset()
        if old == "paused":
            self._camera_dets, self._camera_dets_at = [], None  # stale: from before the video
            self._next_frame_at = self._next_detect_at = self._next_track_at = now
        else:  # apply the new rate from now on
            self._next_frame_at = min(self._next_frame_at, now + self.frame_interval)
            self._next_detect_at = min(self._next_detect_at, now + self.detect_interval)

    @staticmethod
    def _advance(target: float, interval: float, now: float) -> float:
        nxt = target + interval
        return nxt if nxt > now else now + interval  # keep the average rate, never burst to catch up

    def _capture(self, cam: CameraDevice, now: float) -> bool:
        try:
            frame = cam.read(timeout=self._read_timeout)
        except Exception as e:  # noqa: BLE001
            self.log.warning("camera read failed: %s", e)
            frame = None
        if frame is None:
            # A read timeout alone is not a disconnect: UVC cameras slow down to a few fps in dim light.
            # Disconnected = the device reports failure (driver error / no frame for stall_seconds)
            # or reads have kept failing for stall_seconds.
            if self._read_fail_since is None:
                self._read_fail_since = now
            if cam.failed or not cam.is_open or now - self._read_fail_since >= float(self.cam_cfg.stall_seconds):
                self._camera_lost(now, "camera stopped delivering frames")
            return False
        self._read_fail_since = None
        self._frame_size = (int(frame.shape[1]), int(frame.shape[0]))
        if self._photo_pending():
            self._serve_photos(frame)
        small = resize_to_width(frame, int(self.cam_cfg.process_width))
        del frame  # never keep full frames around
        detected = False
        if now >= self._next_detect_at:
            self._next_detect_at = self._advance(self._next_detect_at, self.detect_interval, now)
            try:
                dets = self._detector.detect(small)
            except Exception:  # noqa: BLE001
                self._detect_errors += 1
                if self._detect_errors in (1, 10) or self._detect_errors % 100 == 0:
                    self.log.exception("face detector failed (%d times)", self._detect_errors)
                dets = []
            self._camera_dets, self._camera_dets_at = dets, now
            self._detect_rate.tick(now)
            detected = True
        if self.gestures_active:
            face = self._camera_dets[0] if self._camera_dets else None
            try:
                for ev in self._gestures.update(small, now, face):
                    self.ctx.bus.publish(Topics.VISION_GESTURE, {"gesture": ev.gesture, "confidence": ev.confidence},
                                         source="vision")
            except Exception:  # noqa: BLE001 - experimental code must never break vision
                self.log.exception("gesture analysis failed; disabling gestures until re-enabled")
                self._gestures_on = False
        return detected

    def _active_sim(self, now: float) -> Detection | None:
        with self._lock:
            if self._sim_face is not None and now >= self._sim_until:
                self._sim_face = None
                self._sim_dirty = True
            return self._sim_face

    def _next_delay(self, now: float) -> float:
        cands: list[float] = []
        if self._camera is not None:
            cands.append(self._next_frame_at)
            if self._photo_pending():
                cands.append(now + PHOTO_RETRY)
        elif self._kind != "none" and self._camera_allowed:
            cands.append(self._next_open_at)
        if self._sim_face is not None:
            cands += [self._next_track_at, self._sim_until]
        if self._tracker.busy or self._faces_visible:
            cands.append(self._next_track_at)
        if not cands:
            return IDLE_WAIT
        return min(IDLE_WAIT, max(0.0, min(cands) - now))

    # ----------------------------------------------------------------- camera
    def _manage_camera(self, now: float) -> None:
        if self._kind == "none" or not self._camera_allowed:
            if self._camera is not None:
                self._close_camera()
            detail = ("disabled in config (camera.backend: none)" if self._kind == "none"
                      else "camera turned off in privacy settings")
            self._set_cam_status("disabled", detail)
            self._next_open_at = now  # reopen right away when re-enabled
            self._fail_photos("no_camera" if self._kind == "none" else "camera_off", detail)
            return
        if self._camera is None and now >= self._next_open_at and not self._stop_evt.is_set():
            self._open_camera(now)

    def _open_camera(self, now: float) -> None:
        kind = self._kind
        try:
            cam = self._camera_factory(kind)
            cam.open()
        except Exception as e:  # noqa: BLE001 - CameraError or a broken driver: never crash
            if kind == "real" and self._backend == "auto" and not self._ever_opened and self._allow_mock_fallback:
                self.log.warning("no usable camera (%s); using the mock camera (simulation)", e)
                self._kind = "mock"
                self._open_camera(now)
                return
            status = "disconnected" if self._ever_opened else "unavailable"
            reconnect = max(0.1, float(self.cam_cfg.reconnect_seconds))
            self._set_cam_status(status, f"{e} (retrying every {reconnect:g}s)")
            self._next_open_at = now + reconnect
            self._fail_photos("no_camera", status)
            return
        if not self._camera_allowed or self._stop_evt.is_set():  # privacy flipped while opening
            cam.release()
            return
        self._camera = cam
        self._ever_opened = True
        self._read_fail_since = None
        self._next_frame_at = now
        self._next_detect_at = now
        self._detector = self._detector_for(cam)
        info = cam.info()
        self._cam_info = info
        if info.get("width") and info.get("height"):
            self._frame_size = (int(info["width"]), int(info["height"]))
        self._set_cam_status("ok", self._describe_camera(info))

    def _detector_for(self, cam: CameraDevice) -> FaceDetector:
        if self._injected_detector is not None:
            return self._injected_detector
        if isinstance(cam, MockCamera):
            # Synthetic frames: use their ground truth instead of burning CPU on a real detector.
            return MockDetector(source=cam)
        if self._real_detector is None:
            configure_opencv_threads(int(self.vis_cfg.opencv_threads))
            self._real_detector = detector_from_config(self.ctx.config)
            self.log.info("face detector: %s (%s)", self._real_detector.name, self._real_detector.detail)
        return self._real_detector

    @staticmethod
    def _describe_camera(info: dict[str, Any]) -> str:
        if info.get("backend") == "mock":
            return "mock camera (no real camera in use)"
        parts = [str(info.get("device") or "")]
        if info.get("width"):
            parts.append(f"{info['width']}x{info['height']}")
        if info.get("fps"):
            parts.append(f"@{info['fps']:g}fps")
        if info.get("fourcc"):
            parts.append(str(info["fourcc"]))
        return " ".join(p for p in parts if p)

    def _camera_lost(self, now: float, reason: str) -> None:
        if self._camera is None:
            return
        self._close_camera()
        self._fail_photos("no_camera", reason)
        reconnect = max(0.1, float(self.cam_cfg.reconnect_seconds))
        self.log.warning("camera lost: %s; retrying every %gs", reason, reconnect)
        self._set_cam_status("disconnected", f"{reason} (retrying every {reconnect:g}s)")
        self._next_open_at = now + reconnect

    def _close_camera(self) -> None:
        cam, self._camera = self._camera, None
        if cam is not None:
            try:
                cam.release()
            except Exception:  # noqa: BLE001
                self.log.warning("camera release failed", exc_info=True)
        self._camera_dets, self._camera_dets_at = [], None
        self._read_fail_since = None
        self._gestures.reset()  # drop the previous (tiny) frame used for differencing

    def _set_cam_status(self, status: str, detail: str) -> None:
        if (status, detail) == self._cam_status:
            return
        self._cam_status = (status, detail)
        self.ctx.bus.publish(Topics.VISION_CAMERA_STATUS,
                             {"status": status, "detail": detail, "backend": self._backend_label()},
                             source="vision")
        if status == "ok":
            self.set_status(ServiceStatus.RUNNING if self._kind == "real" else ServiceStatus.DEGRADED,
                            "" if self._kind == "real" else f"{self._backend_label()} camera")
        elif status == "disabled":
            self.set_status(ServiceStatus.DISABLED, detail)
        else:
            self.set_status(ServiceStatus.DEGRADED, f"camera {status}")

    # ------------------------------------------------------------- publishing
    def _face_payload(self, tf: TrackedFace, fw: int, fh: int) -> dict[str, Any]:
        d = tf.det.clamped()
        return {"x": int(round(d.x * fw)), "y": int(round(d.y * fh)), "w": int(round(d.w * fw)),
                "h": int(round(d.h * fh)), "gaze_x": round(tf.gaze_x, 4), "gaze_y": round(tf.gaze_y, 4),
                "score": round(float(d.score), 3), "primary": tf.primary}

    def _publish(self, upd: TrackerUpdate, now: float, *, simulated: bool) -> None:
        bus = self.ctx.bus
        fw, fh = self._frame_size
        self._last_faces_count = len(upd.faces)
        if upd.faces:
            if now - self._last_faces_pub >= 1.0 / MAX_FACES_EVENT_HZ:
                self._last_faces_pub = now
                self._faces_visible = True
                bus.publish(Topics.VISION_FACES, {"faces": [self._face_payload(f, fw, fh) for f in upd.faces],
                                                  "frame_w": fw, "frame_h": fh, "simulated": simulated},
                            source="vision")
        elif self._faces_visible:
            self._faces_visible = False
            bus.publish(Topics.VISION_FACES, {"faces": [], "frame_w": fw, "frame_h": fh, "simulated": simulated},
                        source="vision")
        if upd.arrived and upd.gaze is not None:
            bus.publish(Topics.VISION_PERSON_PRESENT,
                        {"gaze_x": round(upd.gaze[0], 4), "gaze_y": round(upd.gaze[1], 4)}, source="vision")
        if upd.left:
            bus.publish(Topics.VISION_PERSON_LEFT, {}, source="vision")

    def _refresh_snapshot(self, now: float) -> None:
        cam = self._camera
        gaze = self._tracker.gaze
        fw, fh = self._frame_size
        det = self._detector
        snap = {
            "camera": self._cam_status[0],
            "detail": self._cam_status[1],
            "backend": self._backend_label(),
            "detector": det.name,
            "detector_detail": det.detail,
            "device": self._cam_info.get("device") if cam is not None else (
                None if self._kind == "none" else str(self.cam_cfg.fixture or self.cam_cfg.device)),
            "resolution": f"{fw}x{fh}" if cam is not None else None,
            "fourcc": self._cam_info.get("fourcc") if cam is not None else None,
            "fps": cam.measured_fps() if cam is not None else 0.0,
            "detect_fps": round(self._detect_rate.rate(now), 2) if cam is not None else 0.0,
            "detect_ms": round(det.last_ms, 1) if det.last_ms is not None else None,
            "faces": self._last_faces_count,
            "person_present": self._tracker.present,
            "gaze": {"x": round(gaze[0], 4), "y": round(gaze[1], 4)} if gaze is not None else None,
            "gestures_enabled": self._gestures_on,
            "simulated": self._sim_face is not None,
            "detect_mode": self._mode,  # active | reduced (robot sleeping) | paused (video playing)
            "robot_state": self._robot_state,
        }
        with self._lock:
            self._snapshot = snap


def _resolve(fut: "concurrent.futures.Future[bytes]", data: bytes) -> None:
    try:
        fut.set_result(data)
    except concurrent.futures.InvalidStateError:  # the caller gave up (timeout / cancelled)
        pass


def _reject(fut: "concurrent.futures.Future[bytes]", exc: BaseException) -> None:
    try:
        fut.set_exception(exc)
    except concurrent.futures.InvalidStateError:
        pass
