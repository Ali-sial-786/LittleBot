"""Detectors: Detection helpers, mock/null detectors, fallback chain, Haar/YuNet smoke tests.

Real face-detection *accuracy* cannot be asserted with synthetic images, so the
real detectors are only checked to run, return well-formed lists and not crash
(they are skipped when this OpenCV build / model file does not support them).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from littlebot.testing import make_test_config
from littlebot.vision import detector as det_mod
from littlebot.vision.detector import (
    Detection, DetectorUnavailable, HaarFaceDetector, MockDetector, NullDetector, YuNetFaceDetector,
    create_detector, detector_from_config, detector_support, find_haar_cascade, resolve_model_path,
)
from littlebot.vision.imaging import blank_frame, draw_cartoon_face, get_cv2


def _haar_ok() -> bool:
    cv2 = get_cv2()
    return cv2 is not None and hasattr(cv2, "CascadeClassifier") and find_haar_cascade() is not None


def _yunet_model() -> Path | None:
    for cand in (os.environ.get("LITTLEBOT_YUNET_MODEL"), "models/face_detection_yunet_2023mar.onnx"):
        if cand and resolve_model_path(cand).is_file():
            return resolve_model_path(cand)
    return None


def synthetic_frames() -> list[np.ndarray]:
    rng = np.random.default_rng(0)
    face = blank_frame(640, 480)
    draw_cartoon_face(face, 320, 220, 160)
    return [
        np.zeros((480, 640, 3), np.uint8),
        np.full((240, 320, 3), 255, np.uint8),
        rng.integers(0, 255, (480, 640, 3), dtype=np.uint8),
        face,
        np.zeros((240, 320), np.uint8),  # already grey
        np.zeros((10, 12, 3), np.uint8),  # smaller than any face
    ]


# ------------------------------------------------------------- Detection
def test_detection_geometry_helpers():
    d = Detection.centred(0.5, 0.5, 0.2, 0.4)
    assert (d.x, d.y, d.cx, d.cy) == pytest.approx((0.4, 0.3, 0.5, 0.5))
    assert d.area == pytest.approx(0.08)
    assert d.iou(d) == pytest.approx(1.0)
    assert d.iou(Detection(0.0, 0.0, 0.1, 0.1)) == 0.0
    c = Detection(-0.1, 0.9, 0.3, 0.3).clamped()
    assert (c.x, c.y, c.w, c.h) == pytest.approx((0.0, 0.9, 0.2, 0.1))


# ------------------------------------------------------- mock / null
def test_mock_detector_script_list_repeats_last():
    a, b = Detection(0.1, 0.1, 0.2, 0.2), Detection(0.5, 0.5, 0.3, 0.3)
    m = MockDetector([[a], [a, b], []])
    frame = np.zeros((10, 10, 3), np.uint8)
    assert m.detect(frame) == [a]
    assert m.detect(frame) == [b, a]  # sorted largest first
    assert m.detect(frame) == []
    assert m.detect(frame) == []
    assert m.calls == 4 and m.last_ms is not None


def test_mock_detector_callable_fixed_and_source():
    m = MockDetector(lambda i: [Detection(0.1 * i, 0.0, 0.1, 0.1)])
    f = np.zeros((4, 4, 3), np.uint8)
    assert m.detect(f)[0].x == 0.0 and m.detect(f)[0].x == pytest.approx(0.1)
    m.set_faces([Detection(0.9, 0.9, 0.5, 0.5)])
    out = m.detect(f)
    assert out[0].x == 0.9 and out[0].w == pytest.approx(0.1)  # clamped to the frame

    class Src:
        def scripted_faces(self):
            return [Detection(0.2, 0.2, 0.1, 0.1)]

    assert MockDetector(source=Src()).detect(f) == [Detection(0.2, 0.2, 0.1, 0.1)]


def test_null_detector_returns_empty():
    n = NullDetector()
    assert n.detect(np.zeros((5, 5, 3), np.uint8)) == []
    assert n.describe()["name"] == "none"


# ------------------------------------------------------ fallback chain
def test_create_detector_none():
    d = create_detector("none")
    assert isinstance(d, NullDetector) and "disabled" in d.detail


def test_create_detector_never_raises_when_nothing_is_available(monkeypatch, tmp_path):
    monkeypatch.setattr(det_mod, "find_haar_cascade", lambda explicit=None: None)
    for kind in ("haar", "yunet"):
        d = create_detector(kind, yunet_model=tmp_path / "missing.onnx")
        assert isinstance(d, NullDetector)
        assert "haar" in d.detail and "yunet" in d.detail
        assert d.detect(np.zeros((240, 320, 3), np.uint8)) == []


def test_yunet_missing_model_is_unavailable_and_falls_back(tmp_path):
    if get_cv2() is None:
        pytest.skip("OpenCV not installed")
    with pytest.raises(DetectorUnavailable, match="not found"):
        YuNetFaceDetector(tmp_path / "nope.onnx")
    d = create_detector("yunet", yunet_model=tmp_path / "nope.onnx")
    if _haar_ok():
        assert d.name == "haar" and "fallback" in d.detail
    else:
        assert d.name == "none"


def test_yunet_rejects_a_corrupt_model(tmp_path):
    cv2 = get_cv2()
    if cv2 is None or not hasattr(cv2, "FaceDetectorYN"):
        pytest.skip("FaceDetectorYN not available")
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not an onnx model")
    with pytest.raises(DetectorUnavailable):
        YuNetFaceDetector(bad)


def test_haar_unavailable_reason_is_explained(monkeypatch):
    cv2 = get_cv2()
    if cv2 is None:
        pytest.skip("OpenCV not installed")
    if not hasattr(cv2, "CascadeClassifier"):
        with pytest.raises(DetectorUnavailable, match="CascadeClassifier"):
            HaarFaceDetector()
    else:
        monkeypatch.setattr(det_mod, "find_haar_cascade", lambda explicit=None: None)
        with pytest.raises(DetectorUnavailable, match="not found"):
            HaarFaceDetector()


def test_detector_from_config_and_support_report(tmp_path):
    cfg = make_test_config(vision={"detector": "none"})
    assert detector_from_config(cfg).name == "none"
    cfg = make_test_config(vision={"detector": "yunet", "yunet_model": str(tmp_path / "x.onnx")})
    assert detector_from_config(cfg).name in ("haar", "none")
    sup = detector_support()
    assert set(sup) == {"opencv", "haar_classifier", "haar_cascade_file", "yunet"}


# ------------------------------------------------------ real detectors
@pytest.mark.skipif(not _haar_ok(), reason="Haar cascade not available in this OpenCV build")
def test_haar_runs_on_synthetic_frames_without_crashing():
    d = HaarFaceDetector(max_width=320, min_face_fraction=0.08)
    for frame in synthetic_frames():
        faces = d.detect(frame)
        assert isinstance(faces, list)
        for f in faces:
            assert 0.0 <= f.x <= 1.0 and 0.0 <= f.y <= 1.0 and 0.0 < f.w <= 1.0 and 0.0 < f.h <= 1.0
        assert d.last_ms is not None and d.last_ms >= 0
    # blank frames certainly contain no faces
    assert d.detect(np.zeros((480, 640, 3), np.uint8)) == []


@pytest.mark.skipif(_yunet_model() is None, reason="YuNet model not downloaded (scripts/download_models.py)")
def test_yunet_runs_on_synthetic_frames_without_crashing():
    d = YuNetFaceDetector(_yunet_model(), max_width=320)
    for frame in synthetic_frames():
        faces = d.detect(frame)
        assert isinstance(faces, list)
        for f in faces:
            assert 0.0 <= f.score <= 1.0
    assert d.detect(np.zeros((480, 640, 3), np.uint8)) == []
