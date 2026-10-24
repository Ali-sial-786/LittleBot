"""scripts/check_camera.py and scripts/download_models.py (no camera, no network needed)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from littlebot.core.paths import PROJECT_ROOT
from littlebot.vision.camera import list_video_devices
from littlebot.vision.imaging import blank_frame, draw_cartoon_face, get_cv2

CHECK = PROJECT_ROOT / "scripts" / "check_camera.py"
DOWNLOAD = PROJECT_ROOT / "scripts" / "download_models.py"


def run_check(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True, timeout=timeout,
                          cwd=PROJECT_ROOT)


def test_check_camera_help_mentions_list():
    res = run_check("--help")
    assert res.returncode == 0 and "--list" in res.stdout and "--save" in res.stdout


def test_check_camera_list_reports_cleanly():
    res = run_check("--list")
    assert "Traceback" not in res.stderr
    assert "Video devices" in res.stdout
    if not any(d["candidate"] for d in list_video_devices()):
        assert res.returncode == 2 and "No camera found" in res.stdout
    else:
        assert res.returncode == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="device-path check is Linux specific")
def test_check_camera_missing_device_is_no_camera_found(tmp_path):
    res = run_check("--device", "/dev/video-littlebot-missing", "--json")
    assert res.returncode == 2 and "Traceback" not in res.stderr
    report = json.loads(res.stdout)
    assert report["camera"]["ok"] is False and report["exit_code"] == 2
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def synthetic_image(tmp_path) -> Path:
    cv2 = get_cv2()
    if cv2 is None:
        pytest.skip("OpenCV needed to write the synthetic fixture")
    img = blank_frame(640, 480)
    draw_cartoon_face(img, 320, 240, 150)
    path = tmp_path / "synthetic.png"
    cv2.imwrite(str(path), img)
    return path


def test_check_camera_fixture_benchmark_stores_nothing(synthetic_image):
    before = set(synthetic_image.parent.iterdir())
    res = run_check("--fixture", str(synthetic_image), "--seconds", "0.3", "--detector", "none", "--json")
    assert res.returncode == 0, res.stdout + res.stderr
    report = json.loads(res.stdout)
    assert report["capture"]["frames"] > 0 and report["detection"]["runs"] > 0
    assert report["detector"]["name"] == "none" and "saved" not in report
    assert set(synthetic_image.parent.iterdir()) == before  # privacy: nothing written


def test_check_camera_save_writes_exactly_one_frame(synthetic_image, tmp_path):
    out = tmp_path / "out" / "frame.jpg"
    res = run_check("--fixture", str(synthetic_image), "--seconds", "0.3", "--detector", "none", "--save", str(out))
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Saved ONE frame" in res.stdout
    assert list(out.parent.iterdir()) == [out]
    assert get_cv2().imread(str(out)).shape == (480, 640, 3)


# ---------------------------------------------------------- download_models
@pytest.fixture
def dl():
    name = "littlebot_download_models"
    spec = importlib.util.spec_from_file_location(name, DOWNLOAD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations through sys.modules
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(name, None)


def test_download_models_verifies_hash_and_is_atomic(dl, tmp_path):
    payload = b"pretend onnx model " * 100
    src = tmp_path / "src.bin"
    src.write_bytes(payload)
    good = dl.Model(filename="m.onnx", sha256=hashlib.sha256(payload).hexdigest(), size=len(payload),
                    urls=[src.as_uri()])
    dest = tmp_path / "models"
    assert dl.download(good, dest, quiet=True)
    assert (dest / "m.onnx").read_bytes() == payload and dl.is_verified(good, dest)
    assert (dest / "m.onnx").stat().st_mode & 0o044  # readable by the robot's service user
    assert dl.download(good, dest, quiet=True)  # already present: no download needed

    bad = dl.Model(filename="bad.onnx", sha256="0" * 64, size=len(payload),
                   urls=[(tmp_path / "missing.bin").as_uri(), src.as_uri()])
    assert not dl.download(bad, dest, quiet=True)
    assert sorted(p.name for p in dest.iterdir()) == ["m.onnx"]  # nothing half-written left behind


def test_download_models_list_and_unknown(dl, tmp_path, capsys):
    assert dl.main(["--list", "--dest", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "yunet" in out and "not downloaded" in out
    with pytest.raises(SystemExit):
        dl.main(["nope", "--dest", str(tmp_path)])
    assert {"yunet", "haar", "hand_palm", "hand_pose"} <= set(dl.MODELS)
    assert all(len(m.sha256) == 64 and m.urls for m in dl.MODELS.values())


def test_download_models_hands_fetches_both_hand_models(dl, tmp_path, monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(dl, "download", lambda m, dest, **kw: fetched.append(m.filename) or True)
    assert dl.main(["hands", "--dest", str(tmp_path)]) == 0
    assert fetched == ["palm_detection_mediapipe_2023feb.onnx", "handpose_estimation_mediapipe_2023feb.onnx"]
    from littlebot.vision import hands

    palm, hand = hands.model_paths()
    assert (palm.name, hand.name) == tuple(fetched)  # where the counter looks for them


def test_download_models_faces_fetches_both_face_models(dl, tmp_path, monkeypatch, ctx):
    fetched: list[str] = []
    monkeypatch.setattr(dl, "download", lambda m, dest, **kw: fetched.append(m.filename) or True)
    assert dl.main(["faces", "--dest", str(tmp_path)]) == 0
    assert fetched == ["face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"]
    assert dl.MODELS["sface"].size > 30_000_000 and "Apache" in dl.MODELS["sface"].license
    from littlebot.vision import faces

    det, rec = faces.FaceRecognition(ctx).model_paths()
    assert (det.name, rec.name) == tuple(fetched)  # where face recognition looks for them
    assert Path(faces.SFACE_MODEL).name == fetched[1] == Path(ctx.config.vision.sface_model).name


def test_synthetic_fixture_helpers_are_deterministic():
    a = blank_frame(64, 48)
    draw_cartoon_face(a, 32, 24, 20)
    b = blank_frame(64, 48)
    draw_cartoon_face(b, 32, 24, 20)
    assert np.array_equal(a, b)
