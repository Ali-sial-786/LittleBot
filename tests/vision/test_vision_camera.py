"""Camera devices: mock, fixture, discovery, and OpenCVCamera driven by a fake cv2 driver."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from littlebot.testing import make_test_config
from littlebot.vision import camera as cam_mod
from littlebot.vision.camera import (
    CameraError, FixtureCamera, MockCamera, OpenCVCamera, create_camera, fourcc_to_str, list_video_devices,
    probe_device, resolve_device,
)
from littlebot.vision.imaging import get_cv2


# ------------------------------------------------------------------ mock
def test_mock_camera_frames_and_ground_truth():
    cam = MockCamera(160, 120, face=(0.25, 0.5, 0.3))
    assert cam.read() is None  # not opened yet
    cam.open()
    frame = cam.read()
    assert frame.shape == (120, 160, 3) and frame.dtype == np.uint8
    faces = cam.scripted_faces()
    assert len(faces) == 1 and faces[0].cx == pytest.approx(0.25) and faces[0].cy == pytest.approx(0.5)
    empty = MockCamera(160, 120)
    empty.open()
    diff = np.abs(frame.astype(int) - empty.read().astype(int)).sum(axis=2)
    ys, xs = np.nonzero(diff)
    assert 0.1 < xs.mean() / 160 < 0.4  # the face pattern is drawn where the ground truth says
    assert empty.scripted_faces() == []
    cam.release()
    assert not cam.is_open and cam.release_count == 1


def test_mock_camera_moving_face_follows_clock():
    t = [0.0]
    cam = MockCamera(160, 120, face=True, clock=lambda: t[0])
    cam.open()
    x0 = cam.scripted_faces()[0].cx
    t[0] = 2.0
    assert cam.scripted_faces()[0].cx != pytest.approx(x0)


def test_mock_camera_failure_controls():
    cam = MockCamera(fail_after=2)
    cam.open()
    assert cam.read() is not None and cam.read() is not None
    assert cam.read() is None and cam.failed
    cam.release()
    cam.open()  # still "unplugged"
    assert cam.read() is None
    cam.recover()
    assert cam.read() is not None and not cam.failed
    cam.fail()
    assert cam.read() is None and cam.failed
    with pytest.raises(CameraError):
        MockCamera(available=False).open()


def test_mock_camera_paced_read_times_out_and_cancels():
    cam = MockCamera(fps=2.0)
    cam.open()
    assert cam.read(timeout=1.0) is not None
    t0 = time.monotonic()
    assert cam.read(timeout=0.05) is None  # next frame is 0.5 s away
    threading.Timer(0.05, cam.cancel_read).start()
    assert cam.read(timeout=1.0) is None
    assert time.monotonic() - t0 < 0.4


# --------------------------------------------------------------- fixture
def test_fixture_camera_in_memory_loop_and_end():
    frames = [np.full((20, 30, 3), i, np.uint8) for i in range(3)]
    cam = FixtureCamera(frames, loop=True)
    cam.open()
    assert [int(cam.read()[0, 0, 0]) for _ in range(5)] == [0, 1, 2, 0, 1]
    assert cam.info()["width"] == 30
    once = FixtureCamera(frames, loop=False)
    once.open()
    assert [int(once.read()[0, 0, 0]) for _ in range(3)] == [0, 1, 2]
    assert once.read() is None and once.failed  # end of stream behaves like a disconnect
    gray = FixtureCamera([np.zeros((4, 4), np.uint8)])
    gray.open()
    assert gray.read().shape == (4, 4, 3)


def test_fixture_camera_files_and_directory(tmp_path: Path):
    a = np.full((24, 32, 3), 10, np.uint8)
    b = np.full((24, 32, 3), 200, np.uint8)
    cv2 = get_cv2()
    if cv2 is not None:
        cv2.imwrite(str(tmp_path / "a.png"), a)
        cv2.imwrite(str(tmp_path / "b.png"), b)
    else:
        np.save(tmp_path / "a.npy", a)
        np.save(tmp_path / "b.npy", b)
    cam = FixtureCamera(tmp_path, loop=False)
    cam.open()
    assert int(cam.read()[0, 0, 0]) == 10 and int(cam.read()[0, 0, 0]) == 200
    assert cam.read() is None
    cam.release()
    single = FixtureCamera(sorted(tmp_path.iterdir())[0])
    single.open()
    assert single.read().shape == (24, 32, 3)
    with pytest.raises(CameraError):
        FixtureCamera(tmp_path / "missing.png").open()
    (tmp_path / "empty").mkdir()
    with pytest.raises(CameraError):
        FixtureCamera(tmp_path / "empty").open()


@pytest.mark.skipif(get_cv2() is None, reason="OpenCV not installed")
def test_fixture_camera_video_file(tmp_path: Path):
    cv2 = get_cv2()
    path = tmp_path / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    if not writer.isOpened():
        pytest.skip("no video writer backend")
    for i in range(5):
        writer.write(np.full((48, 64, 3), i * 40, np.uint8))
    writer.release()
    cam = FixtureCamera(path, loop=False)
    cam.open()
    n = 0
    while cam.read() is not None:
        n += 1
    assert n == 5 and cam.failed
    cam.release()


# ------------------------------------------------------------- discovery
def _fake_pi5_sysfs(tmp_path: Path) -> tuple[Path, Path]:
    sys_cls = tmp_path / "sys/class/video4linux"
    sys_cls.mkdir(parents=True)
    dev = tmp_path / "dev"
    dev.mkdir()
    usb = tmp_path / "sys/devices/platform/axi/1000120000.pcie/1f00200000.usb/xhci-hcd.0/usb1/1-1/1-1:1.0"
    nodes = {
        0: (usb, "U20CAM-1080P: U20CAM-1080P", "0"),
        1: (usb, "U20CAM-1080P: U20CAM-1080P", "1"),  # UVC metadata node
        19: (tmp_path / "sys/devices/platform/axi/1000800000.codec", "rpi-hevc-dec", "0"),
        20: (tmp_path / "sys/devices/platform/axi/1000880000.pisp_be", "pispbe-input", "0"),
    }
    for n, (parent, name, idx) in nodes.items():
        d = parent / "video4linux" / f"video{n}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "index").write_text(idx + "\n")
        (sys_cls / f"video{n}").symlink_to(d)
        (dev / f"video{n}").touch()
    return sys_cls, dev


def test_list_video_devices_pi5_layout(tmp_path: Path, monkeypatch):
    sys_cls, dev = _fake_pi5_sysfs(tmp_path)
    devs = list_video_devices(sys_cls, dev)
    assert [d["index"] for d in devs] == [0, 1, 19, 20]
    assert [d["path"] for d in devs if d["candidate"]] == [str(dev / "video0")]
    by = {d["index"]: d for d in devs}
    assert by[0]["usb"] and by[0]["name"].startswith("U20CAM")
    assert by[19]["platform_node"] and by[20]["platform_node"]
    monkeypatch.setattr(cam_mod, "list_video_devices", lambda: list_video_devices(sys_cls, dev))
    assert resolve_device("auto") == str(dev / "video0")


def test_list_video_devices_empty_and_resolve(tmp_path: Path, monkeypatch):
    assert list_video_devices(tmp_path / "nosys", tmp_path / "nodev") == []
    monkeypatch.setattr(cam_mod, "list_video_devices", lambda: [])
    assert resolve_device("auto") == 0 and resolve_device(None) == 0
    assert resolve_device(2) == 2 and resolve_device("1") == 1
    assert resolve_device("/dev/video4") == "/dev/video4"


def test_fourcc_to_str():
    v = ord("M") | ord("J") << 8 | ord("P") << 16 | ord("G") << 24
    assert fourcc_to_str(float(v)) == "MJPG"
    assert fourcc_to_str(0) == "" and fourcc_to_str(-1) == ""


@pytest.mark.skipif(get_cv2() is None, reason="OpenCV not installed")
def test_real_camera_missing_device_fails_fast():
    t0 = time.monotonic()
    with pytest.raises(CameraError):
        OpenCVCamera("/dev/video-littlebot-missing").open()
    res = probe_device("/dev/video-littlebot-missing", timeout=3.0)
    assert res["ok"] is False and "no camera" in res["detail"]
    assert time.monotonic() - t0 < 3.0


def test_create_camera_kinds(tmp_path: Path):
    cfg = make_test_config(camera={"capture_width": 640, "capture_height": 480, "process_width": 320}).camera
    mock = create_camera(cfg, "mock")
    assert isinstance(mock, MockCamera) and (mock.width, mock.height) == (320, 240)
    assert isinstance(create_camera(cfg, "real"), OpenCVCamera)
    with pytest.raises(CameraError):
        create_camera(cfg, "fixture")
    cfg2 = make_test_config(camera={"fixture": str(tmp_path)}).camera
    assert isinstance(create_camera(cfg2, "fixture"), FixtureCamera)


# --------------------------------------------- OpenCVCamera, fake driver
class FakeCap:
    """Imitates cv2.VideoCapture for a UVC camera that only does 30 fps."""

    instances: list["FakeCap"] = []

    def __init__(self, dev, api, *, fps=100.0, opened=True, fail_after=None, hang: threading.Event | None = None):
        self.dev, self.api, self.fps = dev, api, fps
        self.opened, self.fail_after, self.hang = opened, fail_after, hang
        self.props: dict[int, float] = {}
        self.grabs = 0
        self.retrieves = 0
        self.released = False
        FakeCap.instances.append(self)

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def get(self, prop):
        if prop == FAKE_CV2.CAP_PROP_FPS:
            return 30.0  # negotiated value differs from the request
        return self.props.get(prop, 0.0)

    def grab(self):
        if self.hang is not None:
            self.hang.wait(5.0)
            return False
        time.sleep(1.0 / self.fps)
        self.grabs += 1
        return self.fail_after is None or self.grabs <= self.fail_after

    def retrieve(self):
        self.retrieves += 1
        return True, np.full((48, 64, 3), self.grabs % 255, np.uint8)

    def release(self):
        self.released = True


FAKE_CV2 = SimpleNamespace(
    CAP_V4L2=200, CAP_ANY=0, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5, CAP_PROP_FOURCC=6,
    CAP_PROP_BUFFERSIZE=38, CAP_PROP_READ_TIMEOUT_MSEC=54,
    VideoWriter_fourcc=lambda *c: sum(ord(ch) << (8 * i) for i, ch in enumerate(c)),
)


@pytest.fixture
def fake_driver(monkeypatch, tmp_path):
    """Patch cv2 with FAKE_CV2; returns (device_path, configure(**FakeCap kwargs))."""
    FakeCap.instances.clear()
    opts: dict = {}
    FAKE_CV2.VideoCapture = lambda dev, api: FakeCap(dev, api, **opts)
    monkeypatch.setattr(cam_mod, "get_cv2", lambda: FAKE_CV2)
    node = tmp_path / "video0"
    node.touch()
    return str(node), opts.update


def test_opencv_camera_negotiation_read_and_release(fake_driver):
    dev, _ = fake_driver
    cam = OpenCVCamera(dev, 640, 480, 15, "MJPG")
    cam.open()
    cap = FakeCap.instances[-1]
    assert cap.api == FAKE_CV2.CAP_V4L2 or not cam_mod._is_linux()
    assert cap.props[FAKE_CV2.CAP_PROP_FOURCC] == FAKE_CV2.VideoWriter_fourcc(*"MJPG")
    assert cap.props[FAKE_CV2.CAP_PROP_BUFFERSIZE] == 1
    info = cam.info()
    assert (info["width"], info["height"], info["fps"], info["fourcc"]) == (640, 480, 30.0, "MJPG")
    assert info["requested"]["fps"] == 15.0
    frames = [cam.read(timeout=1.0) for _ in range(5)]
    assert all(f is not None and f.shape == (48, 64, 3) for f in frames)
    time.sleep(0.1)
    assert cam.measured_fps() > 10  # grab rate of the reader thread
    assert cap.retrieves < cap.grabs  # frames nobody asked for are never decoded
    assert not cam.failed
    reader = cam._reader
    cam.release()
    assert cap.released and not reader.is_alive() and cam.release_count == 1
    assert cam.read() is None
    cam.release()  # idempotent
    assert cam.release_count == 1


def test_opencv_camera_driver_errors_mark_failed(fake_driver):
    dev, configure = fake_driver
    configure(fail_after=3)
    cam = OpenCVCamera(dev)
    cam.open()
    deadline = time.monotonic() + 2.0
    while not cam.failed and time.monotonic() < deadline:
        cam.read(timeout=0.1)
    assert cam.failed
    assert cam.read(timeout=1.0) is None
    cam.release()
    assert FakeCap.instances[-1].released


def test_opencv_camera_open_errors(fake_driver):
    dev, configure = fake_driver
    configure(opened=False)
    with pytest.raises(CameraError, match="could not open"):
        OpenCVCamera(dev).open()
    configure(opened=True, fail_after=0)
    with pytest.raises(CameraError, match="no frames|stopped"):
        OpenCVCamera(dev, open_timeout=2.0).open()
    assert FakeCap.instances[-1].released


def test_opencv_camera_stall_times_out_and_cancel_read(fake_driver):
    dev, configure = fake_driver
    cam = OpenCVCamera(dev, stall_seconds=0.3)
    cam.open()
    hang = threading.Event()
    cam._cap.hang = hang  # driver stops answering mid-stream
    try:
        time.sleep(0.05)
        t0 = time.monotonic()
        assert cam.read(timeout=0.2) is None or cam.read(timeout=0.2) is None
        assert time.monotonic() - t0 < 0.8
        time.sleep(0.35)
        assert cam.failed  # no grab for > stall_seconds
        threading.Timer(0.05, cam.cancel_read).start()
        t0 = time.monotonic()
        assert cam.read(timeout=3.0) is None
        assert time.monotonic() - t0 < 1.0
    finally:
        hang.set()
        cam.release()
