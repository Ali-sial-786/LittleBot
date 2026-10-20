"""Face *detection* only: where are faces in this frame?

There is deliberately NO recognition, identity, age/emotion estimation or any
other biometric profiling here, and frames are never written anywhere. Detectors
return plain boxes in normalised frame coordinates; landmarks that some models
produce are discarded immediately.

Detectors
---------
* ``HaarFaceDetector``  - classic Viola-Jones cascade. Needs ``cv2.CascadeClassifier``
  (OpenCV 4.x; it was moved out of the main package in OpenCV 5) and the
  ``haarcascade_frontalface_default.xml`` file (bundled with opencv-python 4.x,
  /usr/share/opencv4 on Raspberry Pi OS, or models/ via scripts/download_models.py).
* ``YuNetFaceDetector`` - small CNN (``cv2.FaceDetectorYN``) from the OpenCV Model Zoo.
  More robust (tilted / partly turned faces, dim light); needs the ONNX model in
  models/ (scripts/download_models.py). The 2023mar model needs OpenCV >= 4.8.
* ``MockDetector``      - scripted results for tests and the simulator.
* ``NullDetector``      - detection disabled / nothing available.

``create_detector`` tries the requested detector, then the other real one, then
falls back to ``NullDetector`` - it never raises.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import numpy as np

from littlebot.core.paths import PROJECT_ROOT
from littlebot.vision.imaging import ensure_bgr, get_cv2, quiet_opencv, resize_to_width, to_gray

log = logging.getLogger("littlebot.vision.detector")

HAAR_FILENAME = "haarcascade_frontalface_default.xml"
_HAAR_SYSTEM_DIRS = (
    "/usr/share/opencv4/haarcascades",
    "/usr/share/opencv/haarcascades",
    "/usr/local/share/opencv4/haarcascades",
    "/usr/local/share/opencv/haarcascades",
)


@dataclass(frozen=True)
class Detection:
    """A face box in normalised frame coords (0..1, origin top-left). No identity attached."""

    x: float
    y: float
    w: float
    h: float
    score: float = 1.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def clamped(self) -> "Detection":
        if self.x >= 0.0 and self.y >= 0.0 and self.x + self.w <= 1.0 and self.y + self.h <= 1.0:
            return self
        x0, y0 = min(max(self.x, 0.0), 1.0), min(max(self.y, 0.0), 1.0)
        x1, y1 = min(max(self.x + self.w, 0.0), 1.0), min(max(self.y + self.h, 0.0), 1.0)
        return Detection(x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0), self.score)

    def iou(self, other: "Detection") -> float:
        ix0, iy0 = max(self.x, other.x), max(self.y, other.y)
        ix1, iy1 = min(self.x + self.w, other.x + other.w), min(self.y + self.h, other.y + other.h)
        inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    @classmethod
    def centred(cls, cx: float, cy: float, w: float, h: float | None = None, score: float = 1.0) -> "Detection":
        h = w if h is None else h
        return cls(cx - w / 2.0, cy - h / 2.0, w, h, score)


class DetectorUnavailable(RuntimeError):
    """The detector cannot run here (missing OpenCV feature or model file)."""


class FaceDetector(abc.ABC):
    name: str = "base"

    def __init__(self) -> None:
        self.detail = ""
        self.last_ms: float | None = None  # wall time of the last detect() call
        self.calls = 0

    @abc.abstractmethod
    def _detect(self, frame: np.ndarray) -> list[Detection]: ...

    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Faces in ``frame`` (BGR or gray uint8), largest first."""
        t0 = time.perf_counter()
        try:
            faces = self._detect(frame)
        finally:
            self.calls += 1
            self.last_ms = (time.perf_counter() - t0) * 1000.0
        faces = [f.clamped() for f in faces if f.w > 0 and f.h > 0]
        faces.sort(key=lambda f: f.area, reverse=True)
        return faces

    def close(self) -> None:  # pragma: no cover - default no-op
        pass

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "detail": self.detail,
                "last_ms": None if self.last_ms is None else round(self.last_ms, 2)}


class NullDetector(FaceDetector):
    name = "none"

    def __init__(self, detail: str = "face detection disabled") -> None:
        super().__init__()
        self.detail = detail

    def _detect(self, frame: np.ndarray) -> list[Detection]:
        return []


class _FaceSource(Protocol):
    def scripted_faces(self) -> list[Detection]: ...


class MockDetector(FaceDetector):
    """Scripted detector.

    ``script`` may be: None (use ``set_faces``), a list of per-call results (the
    last one repeats), or a callable ``f(call_index) -> faces``. ``source`` (e.g.
    a ``MockCamera``) supplies ground-truth faces of synthetic frames instead.
    """

    name = "mock"

    def __init__(self, script: Sequence[Sequence[Detection]] | Callable[[int], Sequence[Detection]] | None = None,
                 *, source: _FaceSource | None = None) -> None:
        super().__init__()
        self._script = script
        self._source = source
        self._fixed: list[Detection] = []
        self.detail = "scripted faces" if source is None else "mock camera ground truth"

    def set_faces(self, faces: Sequence[Detection]) -> None:
        self._fixed = list(faces)
        self._script = None

    def _detect(self, frame: np.ndarray) -> list[Detection]:
        i = self.calls
        if self._source is not None:
            return list(self._source.scripted_faces())
        if callable(self._script):
            return list(self._script(i))
        if self._script:
            seq = list(self._script)
            return list(seq[min(i, len(seq) - 1)])
        return list(self._fixed)


def find_haar_cascade(explicit: str | Path | None = None) -> Path | None:
    """Locate the frontal-face cascade XML (explicit path, OpenCV data dir, system dirs, models/)."""
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit).expanduser()
        candidates.append(p if p.is_absolute() else PROJECT_ROOT / p)
    cv2 = get_cv2()
    data_dir = getattr(getattr(cv2, "data", None), "haarcascades", None) if cv2 is not None else None
    if data_dir:
        candidates.append(Path(data_dir) / HAAR_FILENAME)
    candidates += [Path(d) / HAAR_FILENAME for d in _HAAR_SYSTEM_DIRS]
    candidates.append(PROJECT_ROOT / "models" / HAAR_FILENAME)
    for c in candidates:
        if c.is_file():
            return c
    return None


class HaarFaceDetector(FaceDetector):
    name = "haar"

    def __init__(self, *, max_width: int = 320, min_face_fraction: float = 0.08,
                 cascade_path: str | Path | None = None, scale_factor: float = 1.2,
                 min_neighbors: int = 5, equalize: bool = True) -> None:
        super().__init__()
        cv2 = get_cv2()
        if cv2 is None:
            raise DetectorUnavailable("OpenCV is not installed (pip install opencv-python-headless)")
        if not hasattr(cv2, "CascadeClassifier"):
            raise DetectorUnavailable(
                f"OpenCV {cv2.__version__} has no CascadeClassifier (Haar moved out of the main package in "
                "OpenCV 5); use detector 'yunet' or install opencv-python-headless<5")
        path = find_haar_cascade(cascade_path)
        if path is None:
            raise DetectorUnavailable(f"{HAAR_FILENAME} not found (run scripts/download_models.py haar)")
        clf = cv2.CascadeClassifier(str(path))
        if clf.empty():
            raise DetectorUnavailable(f"could not load Haar cascade {path}")
        self._cv2 = cv2
        self._clf = clf
        self.cascade_path = path
        self.max_width = int(max_width)
        self.min_face_fraction = float(min_face_fraction)
        self.scale_factor = float(scale_factor)
        self.min_neighbors = int(min_neighbors)
        self.equalize = equalize
        self.detail = f"Haar cascade {path.name}"
        _self_test(self)

    def _detect(self, frame: np.ndarray) -> list[Detection]:
        cv2 = self._cv2
        gray = to_gray(resize_to_width(frame, self.max_width))
        if self.equalize:
            gray = cv2.equalizeHist(gray)  # helps in dim / uneven nursery lighting
        H, W = gray.shape[:2]
        min_px = max(24, int(round(self.min_face_fraction * W)))  # 24 px = cascade window size
        if min_px > min(W, H):
            return []
        boxes = self._clf.detectMultiScale(gray, scaleFactor=self.scale_factor, minNeighbors=self.min_neighbors,
                                           minSize=(min_px, min_px))
        return [Detection(float(x) / W, float(y) / H, float(w) / W, float(h) / H, 1.0) for (x, y, w, h) in boxes]


def resolve_model_path(path: str | Path) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


class YuNetFaceDetector(FaceDetector):
    name = "yunet"

    def __init__(self, model_path: str | Path, *, max_width: int = 320, min_face_fraction: float = 0.08,
                 score_threshold: float = 0.7, nms_threshold: float = 0.3, top_k: int = 20) -> None:
        super().__init__()
        cv2 = get_cv2()
        if cv2 is None:
            raise DetectorUnavailable("OpenCV is not installed (pip install opencv-python-headless)")
        if not hasattr(cv2, "FaceDetectorYN"):
            raise DetectorUnavailable(f"OpenCV {cv2.__version__} has no FaceDetectorYN (needs >= 4.5.4)")
        model = resolve_model_path(model_path)
        if not model.is_file():
            raise DetectorUnavailable(f"YuNet model not found at {model} (run scripts/download_models.py)")
        try:
            with quiet_opencv():  # OpenCV 5 warns that DNN targets are unsupported by its new engine
                self._det = cv2.FaceDetectorYN.create(str(model), "", (320, 240), float(score_threshold),
                                                      float(nms_threshold), int(top_k))
        except Exception as e:  # noqa: BLE001 - cv2.error: incompatible model / OpenCV version
            raise DetectorUnavailable(f"could not load YuNet model {model.name}: {e}") from e
        self._size: tuple[int, int] = (320, 240)
        self.model_path = model
        self.max_width = int(max_width)
        self.min_face_fraction = float(min_face_fraction)
        self.detail = f"YuNet {model.name}"
        _self_test(self)  # e.g. the 2023mar model loads on OpenCV < 4.8 but cannot run

    def _detect(self, frame: np.ndarray) -> list[Detection]:
        img = ensure_bgr(resize_to_width(frame, self.max_width))
        H, W = img.shape[:2]
        if (W, H) != self._size:
            self._det.setInputSize((W, H))
            self._size = (W, H)
        _, faces = self._det.detect(img)
        if faces is None:
            return []
        out = []
        for row in np.asarray(faces):
            x, y, w, h, score = (float(v) for v in (row[0], row[1], row[2], row[3], row[-1]))
            # row[4:14] are eye/nose/mouth landmarks: intentionally discarded.
            if w / W < self.min_face_fraction:
                continue
            out.append(Detection(x / W, y / H, w / W, h / H, score))
        return out


def _self_test(det: FaceDetector) -> None:
    """Run once on a blank frame so incompatible builds/models fail here (-> fallback), not later."""
    try:
        det._detect(np.zeros((240, 320, 3), np.uint8))
    except Exception as e:  # noqa: BLE001 - cv2.error and friends
        raise DetectorUnavailable(f"{det.name} detector failed its self-test: {e}") from e


def configure_opencv_threads(n: int) -> None:
    """Limit OpenCV's worker threads (process-wide) so detection leaves cores for the UI/audio."""
    cv2 = get_cv2()
    if cv2 is not None and n and n > 0:
        try:
            cv2.setNumThreads(int(n))
        except Exception:  # noqa: BLE001
            log.debug("cv2.setNumThreads failed", exc_info=True)


def create_detector(kind: str = "haar", *, max_width: int = 320, min_face_fraction: float = 0.08,
                    yunet_model: str | Path = "models/face_detection_yunet_2023mar.onnx",
                    yunet_score_threshold: float = 0.7, haar_cascade: str | Path | None = None,
                    haar_min_neighbors: int = 5, haar_scale_factor: float = 1.2) -> FaceDetector:
    """Best available detector for ``kind`` with automatic fallback. Never raises."""
    if kind == "none":
        return NullDetector("disabled in config (vision.detector: none)")
    order = ["yunet", "haar"] if kind == "yunet" else ["haar", "yunet"]
    reasons: list[str] = []
    for name in order:
        try:
            if name == "haar":
                det: FaceDetector = HaarFaceDetector(max_width=max_width, min_face_fraction=min_face_fraction,
                                                     cascade_path=haar_cascade, min_neighbors=haar_min_neighbors,
                                                     scale_factor=haar_scale_factor)
            else:
                det = YuNetFaceDetector(yunet_model, max_width=max_width, min_face_fraction=min_face_fraction,
                                        score_threshold=yunet_score_threshold)
        except DetectorUnavailable as e:
            reasons.append(f"{name}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 - never crash the robot over a detector
            log.exception("unexpected error creating %s detector", name)
            reasons.append(f"{name}: {e}")
            continue
        if name != kind:
            det.detail += f" (fallback; {'; '.join(reasons)})"
            log.warning("face detector %r unavailable, using %r: %s", kind, name, "; ".join(reasons))
        return det
    detail = "no face detector available: " + "; ".join(reasons)
    log.warning(detail)
    return NullDetector(detail)


def detector_from_config(cfg: Any) -> FaceDetector:
    """Build from a ``LittleBotConfig`` (camera.process_width + vision.*)."""
    v = cfg.vision
    return create_detector(v.detector, max_width=cfg.camera.process_width, min_face_fraction=v.min_face_fraction,
                           yunet_model=v.yunet_model, yunet_score_threshold=v.yunet_score_threshold,
                           haar_cascade=v.haar_cascade, haar_min_neighbors=v.haar_min_neighbors,
                           haar_scale_factor=v.haar_scale_factor)


def detector_support() -> dict[str, Any]:
    """What this OpenCV build can do (for diagnostics / status)."""
    cv2 = get_cv2()
    cascade = find_haar_cascade()
    return {
        "opencv": getattr(cv2, "__version__", None) if cv2 is not None else None,
        "haar_classifier": bool(cv2 is not None and hasattr(cv2, "CascadeClassifier")),
        "haar_cascade_file": str(cascade) if cascade else None,
        "yunet": bool(cv2 is not None and hasattr(cv2, "FaceDetectorYN")),
    }
