"""Camera devices and discovery.

* ``OpenCVCamera``  - real USB/UVC camera through ``cv2.VideoCapture`` (V4L2 on Linux).
  A small reader thread owns the device: it ``grab()``s every frame the camera
  delivers (cheap, keeps the driver queue empty so frames are never stale) and
  only decodes (``retrieve()``) when the consumer asked for a frame. ``read()``
  waits with a timeout, so a stuck driver can never block the vision loop.
  Only the newest frame is kept, and only until it is handed over.
* ``MockCamera``    - synthetic frames (numpy), optionally with a scripted moving
  face; can be told to fail / recover (tests). Very cheap.
* ``FixtureCamera`` - frames from image files, a directory, a video file or
  in-memory arrays (tests, offline tuning).
* ``list_video_devices`` / ``resolve_device`` / ``probe_device`` - discovery.

None of these ever write frames to disk.
"""

from __future__ import annotations

import abc
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from littlebot.vision.detector import Detection
from littlebot.vision.imaging import RateMeter, blank_frame, draw_cartoon_face, get_cv2

log = logging.getLogger("littlebot.vision.camera")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".ppm", ".pgm", ".npy"}
VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".mjpg", ".mjpeg"}
# Raspberry Pi 5 / other SoC video nodes that are not cameras (codecs, ISP back-ends).
_PLATFORM_NODE_HINTS = ("pispbe", "hevc", "bcm2835", "rp1-cfe", "unicam", "codec", "-isp", " isp", "m2m")


class CameraError(RuntimeError):
    """Opening / configuring a camera failed."""


class CameraDevice(abc.ABC):
    """Common interface. Not thread-safe: one consumer thread calls open/read/release."""

    backend: str = "base"

    def __init__(self) -> None:
        self._open = False
        self._rate = RateMeter(window=2.0)
        self.frames_read = 0
        self.open_count = 0
        self.release_count = 0

    @abc.abstractmethod
    def open(self) -> None:
        """Open + configure; raise ``CameraError`` on failure."""

    @abc.abstractmethod
    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        """Newest BGR frame, or None (timeout / failure)."""

    @abc.abstractmethod
    def release(self) -> None:
        """Free the device. Idempotent."""

    def cancel_read(self) -> None:
        """Make a blocked ``read()`` return early (any thread)."""

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def failed(self) -> bool:
        """True when the device reported a fatal error and must be reopened."""
        return False

    @property
    def device_label(self) -> str:
        return self.backend

    def measured_fps(self) -> float:
        return round(self._rate.rate(), 2)

    def info(self) -> dict[str, Any]:
        return {"backend": self.backend, "device": self.device_label, "width": None, "height": None,
                "fps": None, "fourcc": None}

    def __enter__(self) -> "CameraDevice":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Mock camera
# ---------------------------------------------------------------------------

FacePath = Callable[[float], "tuple[float, float, float] | None"]


def default_face_path(t: float) -> tuple[float, float, float]:
    """Slow Lissajous wander: (cx, cy, width) in normalised image coords."""
    return (0.5 + 0.28 * math.sin(2 * math.pi * t / 9.0), 0.45 + 0.08 * math.sin(2 * math.pi * t / 5.0), 0.22)


class MockCamera(CameraDevice):
    """Synthetic frames. ``face`` = None (empty scene), True (default wandering face),
    a fixed ``(cx, cy, width)`` tuple, or a callable ``t -> (cx, cy, width) | None``."""

    backend = "mock"

    def __init__(self, width: int = 320, height: int = 240, *, face: Any = None, fps: float | None = None,
                 fail_after: int | None = None, available: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.width, self.height = int(width), int(height)
        if face is True:
            face = default_face_path
        self._face = face
        self.fps = fps
        self.fail_after = fail_after
        self.available = available
        self.failing = False
        self._clock = clock
        self._t0 = clock()
        self._bg = blank_frame(self.width, self.height)
        self._next_frame_at = 0.0
        self._cancel = threading.Event()

    # test controls -------------------------------------------------------
    def fail(self) -> None:
        """Simulate an unplugged camera: reads return None until ``recover()``."""
        self.failing = True

    def recover(self) -> None:
        self.failing = False
        self.fail_after = None

    def set_face(self, face: Any) -> None:
        self._face = default_face_path if face is True else face

    # ground truth ----------------------------------------------------------
    def _face_now(self) -> tuple[float, float, float] | None:
        face = self._face
        if face is None or face is False:
            return None
        if callable(face):
            return face(self._clock() - self._t0)
        return tuple(face)  # type: ignore[return-value]

    def scripted_faces(self) -> list[Detection]:
        f = self._face_now()
        if f is None:
            return []
        cx, cy, w = f
        h = w * 1.24 * self.width / self.height  # oval face: taller than wide (in pixels)
        return [Detection.centred(cx, cy, w, h).clamped()]

    # device ------------------------------------------------------------------
    def open(self) -> None:
        if not self.available:
            raise CameraError("mock camera unavailable")
        self._open = True  # a failing camera stays failing until recover() (like an unplugged cable)
        self.frames_read = 0
        self.open_count += 1
        self._cancel.clear()

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        if not self._open or self.failing:
            return None
        if self.fail_after is not None and self.frames_read >= self.fail_after:
            self.failing = True
            return None
        if self.fps:
            delay = self._next_frame_at - self._clock()
            if delay > 0:
                if delay > timeout or self._cancel.wait(delay):
                    return None
            self._next_frame_at = max(self._next_frame_at, self._clock()) + 1.0 / self.fps
        frame = self._bg.copy()
        f = self._face_now()
        if f is not None:
            cx, cy, w = f
            draw_cartoon_face(frame, cx * self.width, cy * self.height, w * self.width)
        self.frames_read += 1
        self._rate.tick()
        return frame

    def cancel_read(self) -> None:
        self._cancel.set()

    def release(self) -> None:
        if self._open:
            self.release_count += 1
        self._open = False

    @property
    def failed(self) -> bool:
        return self._open and self.failing

    @property
    def device_label(self) -> str:
        return "mock"

    def info(self) -> dict[str, Any]:
        return {"backend": self.backend, "device": "mock", "width": self.width, "height": self.height,
                "fps": self.fps, "fourcc": None}


# ---------------------------------------------------------------------------
# Fixture camera
# ---------------------------------------------------------------------------

class FixtureCamera(CameraDevice):
    """Frames from files or arrays. ``source``: image file, directory of images,
    video file, list of paths, or list of numpy frames. End of a non-looping
    source counts as a failure (like an unplugged camera)."""

    backend = "fixture"

    def __init__(self, source: str | Path | Sequence[Any], *, loop: bool = True, fps: float | None = None) -> None:
        super().__init__()
        self.source = source
        self.loop = loop
        self.fps = fps
        self._frames: list[np.ndarray] = []
        self._video: Any = None
        self._idx = 0
        self._ended = False
        self._next_frame_at = 0.0
        self._size: tuple[int, int] | None = None

    @property
    def device_label(self) -> str:
        if isinstance(self.source, (str, Path)):
            return str(self.source)
        return f"{len(self._frames) or len(self.source)} in-memory frames"

    def _load_image(self, path: Path) -> np.ndarray:
        if path.suffix.lower() == ".npy":
            arr = np.load(path, allow_pickle=False)
        else:
            cv2 = get_cv2()
            if cv2 is None:
                raise CameraError("OpenCV is required to read image fixtures")
            arr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if arr is None:
            raise CameraError(f"could not read image {path}")
        return np.ascontiguousarray(arr.astype(np.uint8, copy=False))

    def open(self) -> None:
        self._frames, self._idx, self._ended = [], 0, False
        src = self.source
        if isinstance(src, (str, Path)):
            p = Path(src).expanduser()
            if p.is_dir():
                files = sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_EXTS)
                if not files:
                    raise CameraError(f"no images in {p}")
                self._frames = [self._load_image(f) for f in files]
            elif p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                cv2 = get_cv2()
                if cv2 is None:
                    raise CameraError("OpenCV is required to read video fixtures")
                cap = cv2.VideoCapture(str(p))
                if not cap.isOpened():
                    raise CameraError(f"could not open video {p}")
                self._video = cap
            elif p.is_file():
                self._frames = [self._load_image(p)]
            else:
                raise CameraError(f"fixture not found: {p}")
        else:
            items = list(src)
            if not items:
                raise CameraError("empty fixture list")
            self._frames = [np.asarray(f, dtype=np.uint8) if isinstance(f, np.ndarray) else self._load_image(Path(f))
                            for f in items]
        if self._frames:
            h, w = self._frames[0].shape[:2]
            self._size = (w, h)
        self._open = True
        self.open_count += 1

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        if not self._open or self._ended:
            return None
        if self.fps:
            delay = self._next_frame_at - time.monotonic()
            if delay > 0:
                if delay > timeout:
                    return None
                time.sleep(delay)
            self._next_frame_at = max(self._next_frame_at, time.monotonic()) + 1.0 / self.fps
        frame: np.ndarray | None
        if self._video is not None:
            ok, frame = self._video.read()
            if not ok and self.loop:
                self._video.set(get_cv2().CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._video.read()
            if not ok:
                self._ended = True
                return None
        else:
            if self._idx >= len(self._frames):
                if not self.loop:
                    self._ended = True
                    return None
                self._idx = 0
            frame = self._frames[self._idx].copy()
            self._idx += 1
        if frame is not None and frame.ndim == 2:
            frame = np.repeat(frame[:, :, None], 3, axis=2)
        if frame is not None:
            self._size = (frame.shape[1], frame.shape[0])
        self.frames_read += 1
        self._rate.tick()
        return frame

    @property
    def failed(self) -> bool:
        return self._open and self._ended

    def release(self) -> None:
        if self._video is not None:
            try:
                self._video.release()
            finally:
                self._video = None
        if self._open:
            self.release_count += 1
        self._open = False
        self._frames = []

    def info(self) -> dict[str, Any]:
        w, h = self._size or (None, None)
        return {"backend": self.backend, "device": self.device_label, "width": w, "height": h,
                "fps": self.fps, "fourcc": None}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def list_video_devices(sys_root: Path | str = "/sys/class/video4linux",
                       dev_root: Path | str = "/dev") -> list[dict[str, Any]]:
    """Video nodes on Linux (from sysfs, falling back to /dev/video*). Never opens a device.

    Each entry: {path, index, name, usb, node_index, platform_node, exists, candidate}.
    ``candidate`` marks likely real camera capture nodes (USB, primary node, not a codec/ISP).
    On a Pi 5 the SoC adds non-camera nodes (rpi-hevc-dec, pispbe ...); a UVC camera adds
    two nodes (capture + metadata) - only the capture one (sysfs index 0) is a candidate.
    """
    out: dict[int, dict[str, Any]] = {}
    sys_root, dev_root = Path(sys_root), Path(dev_root)
    if sys_root.is_dir():
        for entry in sys_root.glob("video*"):
            try:
                n = int(entry.name[5:])
            except ValueError:
                continue
            name = _read_text(entry / "name") or ""
            idx_txt = _read_text(entry / "index")
            node_index = int(idx_txt) if idx_txt and idx_txt.isdigit() else None
            real = os.path.realpath(entry)
            usb = "/usb" in real
            platform = any(h in name.lower() for h in _PLATFORM_NODE_HINTS)
            node = dev_root / f"video{n}"
            out[n] = {"path": str(node), "index": n, "name": name, "usb": usb, "node_index": node_index,
                      "platform_node": platform, "exists": node.exists()}
    for dev in dev_root.glob("video*") if dev_root.is_dir() else []:
        try:
            n = int(dev.name[5:])
        except ValueError:
            continue
        out.setdefault(n, {"path": str(dev), "index": n, "name": "", "usb": False, "node_index": None,
                           "platform_node": False, "exists": True})
    # Stable names (survive re-plugging): /dev/v4l/by-id/usb-<vendor>_<model>-video-index0 -> ../../videoN
    by_id: dict[int, str] = {}
    by_id_dir = dev_root / "v4l" / "by-id"
    if by_id_dir.is_dir():
        for link in sorted(by_id_dir.iterdir()):
            try:
                target = Path(os.path.realpath(link)).name
                by_id[int(target[5:])] = str(link)
            except (ValueError, OSError):
                continue
    devices = [out[k] for k in sorted(out)]
    for d in devices:
        d["by_id"] = by_id.get(d["index"])
        d["candidate"] = bool(d["exists"] and not d["platform_node"] and d["node_index"] in (0, None)
                              and (d["usb"] or not d["name"]))
    return devices


def resolve_device(spec: int | str | None) -> int | str:
    """``"auto"`` -> first likely USB camera node (else 0); digits -> int; paths unchanged."""
    if isinstance(spec, int):
        return spec
    s = "" if spec is None else str(spec).strip()
    if s.lower() in ("", "auto"):
        for d in list_video_devices():
            if d["candidate"]:
                return d["path"]
        return 0
    if s.isdigit():
        return int(s)
    return s


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def fourcc_to_str(value: float | int) -> str:
    v = int(value)
    if v <= 0:
        return ""
    s = "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))
    return s if s.isprintable() else ""


# ---------------------------------------------------------------------------
# Real camera
# ---------------------------------------------------------------------------

class OpenCVCamera(CameraDevice):
    backend = "opencv"

    def __init__(self, device: int | str = "auto", width: int = 640, height: int = 480, fps: float = 15,
                 fourcc: str = "MJPG", *, open_timeout: float = 5.0, stall_seconds: float = 3.0,
                 buffer_size: int = 1) -> None:
        super().__init__()
        self.requested = {"device": device, "width": int(width), "height": int(height), "fps": float(fps),
                          "fourcc": (fourcc or "").upper()}
        self.open_timeout = float(open_timeout)
        self.stall_seconds = float(stall_seconds)
        self.buffer_size = int(buffer_size)
        self._device: int | str = device
        self._cap: Any = None
        self._negotiated: dict[str, Any] = {}
        self._cond = threading.Condition()
        self._reader: threading.Thread | None = None
        self._stop = False
        self._want = False
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._taken_seq = 0
        self._alive = False
        self._fatal: str | None = None
        self._last_grab = 0.0
        self._cancelled = False
        self.frames_grabbed = 0

    @property
    def device_label(self) -> str:
        d = self._device
        return f"/dev/video{d}" if isinstance(d, int) and _is_linux() else str(d)

    def open(self) -> None:
        if self._open:
            return
        cv2 = get_cv2()
        if cv2 is None:
            raise CameraError("OpenCV is not installed (pip install opencv-python-headless)")
        dev = resolve_device(self.requested["device"])
        self._device = dev
        if _is_linux():
            path = dev if isinstance(dev, str) else f"/dev/video{dev}"
            if not Path(path).exists():
                raise CameraError(f"no camera at {path}")
            api = cv2.CAP_V4L2
        else:
            api = cv2.CAP_ANY
        cap = cv2.VideoCapture(dev, api)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"could not open {self.device_label} (busy or not a capture device?)")
        req = self.requested
        # Order matters for V4L2: pixel format first, then size, then rate.
        if req["fourcc"]:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*req["fourcc"][:4].ljust(4)))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, req["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, req["height"])
        if req["fps"] > 0:
            cap.set(cv2.CAP_PROP_FPS, req["fps"])
        if self.buffer_size > 0:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, self.buffer_size)
        if hasattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC"):
            try:  # honoured by some backends only; the reader thread + read(timeout) cover the rest
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(max(1.0, self.stall_seconds) * 1000))
            except Exception:  # noqa: BLE001
                pass
        self._negotiated = {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or None,
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or None,
            "fps": round(float(cap.get(cv2.CAP_PROP_FPS) or 0.0), 2) or None,
            "fourcc": fourcc_to_str(cap.get(cv2.CAP_PROP_FOURCC)) or None,
        }
        self._cap = cap
        self._stop, self._fatal, self._cancelled = False, None, False
        self._frame, self._seq, self._taken_seq = None, 0, 0
        self._want = True  # decode the very first frame to verify the stream works
        self._alive = True
        self._last_grab = time.monotonic()
        self._reader = threading.Thread(target=self._reader_loop, name="littlebot-camera", daemon=True)
        self._reader.start()
        with self._cond:
            self._cond.wait_for(lambda: self._seq > 0 or not self._alive, timeout=self.open_timeout)
            got = self._seq > 0
        if not got:
            reason = self._fatal or f"no frames within {self.open_timeout:.0f}s"
            self._shutdown_reader(join_timeout=1.0)
            raise CameraError(f"{self.device_label} opened but {reason}")
        self._open = True
        self.open_count += 1
        n = self._negotiated
        log.info("camera %s open: %sx%s @ %s fps %s (requested %sx%s @ %s %s)", self.device_label, n["width"],
                 n["height"], n["fps"], n["fourcc"], req["width"], req["height"], req["fps"], req["fourcc"])

    def _reader_loop(self) -> None:
        cap = self._cap
        fails = 0
        try:
            while not self._stop:
                ok = cap.grab()
                if self._stop:
                    break
                if not ok:
                    fails += 1
                    if fails >= 5:
                        self._fatal = "camera stopped delivering frames"
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                now = time.monotonic()
                self._last_grab = now
                self.frames_grabbed += 1
                self._rate.tick(now)
                if self._want:
                    ok, frame = cap.retrieve()
                    if ok and frame is not None:
                        with self._cond:
                            self._frame = frame
                            self._seq += 1
                            self._want = False
                            self._cond.notify_all()
        except Exception as e:  # noqa: BLE001 - driver errors must not kill the app
            self._fatal = f"camera error: {e}"
            log.warning("camera reader stopped: %s", e)
        finally:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                log.debug("cap.release failed", exc_info=True)
            with self._cond:
                self._alive = False
                self._frame = None
                self._cond.notify_all()

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        if not self._open:
            return None
        with self._cond:
            if self._seq > self._taken_seq and self._frame is not None:
                pass  # a requested frame already arrived
            else:
                self._want = True
                self._cancelled = False
                self._cond.wait_for(lambda: (self._seq > self._taken_seq) or not self._alive or self._cancelled,
                                    timeout=max(0.0, timeout))
            if self._seq > self._taken_seq and self._frame is not None:
                frame, self._frame = self._frame, None  # hand over; keep no reference
                self._taken_seq = self._seq
                self.frames_read += 1
                return frame
            return None

    def cancel_read(self) -> None:
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()

    @property
    def failed(self) -> bool:
        if not self._open:
            return False
        if not self._alive:
            return True
        return time.monotonic() - self._last_grab > self.stall_seconds

    def _shutdown_reader(self, join_timeout: float) -> None:
        self._stop = True
        self.cancel_read()
        t = self._reader
        if t is not None and t is not threading.current_thread():
            t.join(join_timeout)
            if t.is_alive():
                log.warning("camera reader did not stop within %.1fs (driver stuck?); it will release the "
                            "device when the pending grab returns", join_timeout)
        elif t is None and self._cap is not None:
            self._cap.release()
        self._reader = None
        self._cap = None
        self._frame = None

    def release(self) -> None:
        was_open = self._open
        self._open = False
        if self._reader is not None or self._cap is not None:
            self._shutdown_reader(join_timeout=2.0)
        if was_open:
            self.release_count += 1
            log.info("camera %s released", self.device_label)

    def info(self) -> dict[str, Any]:
        n = self._negotiated
        return {"backend": self.backend, "device": self.device_label, "width": n.get("width"),
                "height": n.get("height"), "fps": n.get("fps"), "fourcc": n.get("fourcc"),
                "requested": dict(self.requested)}


def probe_device(device: int | str, *, width: int = 640, height: int = 480, fps: float = 15, fourcc: str = "MJPG",
                 timeout: float = 6.0) -> dict[str, Any]:
    """Open a device, read one frame, release. Runs in a helper thread so a hung
    driver cannot block the caller longer than ``timeout``. The frame is discarded."""
    result: dict[str, Any] = {"device": str(device), "ok": False, "detail": "timed out"}

    def _probe() -> None:
        cam = OpenCVCamera(device, width, height, fps, fourcc, open_timeout=max(1.0, timeout - 1.0))
        try:
            cam.open()
            frame = cam.read(timeout=2.0)
            result.update(cam.info())
            result["ok"] = frame is not None
            result["detail"] = "ok" if frame is not None else "opened but no frame"
        except CameraError as e:
            result["detail"] = str(e)
        except Exception as e:  # noqa: BLE001
            result["detail"] = f"error: {e}"
        finally:
            cam.release()

    t = threading.Thread(target=_probe, name="littlebot-camera-probe", daemon=True)
    t.start()
    t.join(timeout)
    return result


def create_camera(cfg: Any, kind: str, *, clock: Callable[[], float] = time.monotonic) -> CameraDevice:
    """Camera for ``kind`` ("real" | "mock" | "fixture") from a ``CameraConfig``."""
    if kind == "fixture":
        if not cfg.fixture:
            raise CameraError("camera.fixture is not set")
        return FixtureCamera(cfg.fixture, loop=True, fps=float(cfg.capture_fps) or None)
    if kind == "mock":
        w = max(64, min(int(cfg.process_width), int(cfg.capture_width)))
        h = max(48, int(round(w * cfg.capture_height / max(1, cfg.capture_width))))
        return MockCamera(w, h, face=True if cfg.mock_face else None, clock=clock)
    return OpenCVCamera(cfg.device, cfg.capture_width, cfg.capture_height, cfg.capture_fps, cfg.fourcc,
                        stall_seconds=cfg.stall_seconds)
