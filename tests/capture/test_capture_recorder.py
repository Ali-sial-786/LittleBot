"""The video recorder (littlebot/capture/recorder.py) with a fake ffmpeg: the command, frames written at the set rate,
the end (stdin closed, then Ctrl-C with sound), recording on without sound when the sound can't be opened, and the
failures (no ffmpeg, no frames, ffmpeg stopping by itself)."""

from __future__ import annotations

import io
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from littlebot.capture import recorder as rec_mod
from littlebot.capture.recorder import VideoRecorder


class _Stdin:
    def __init__(self, proc: FakeFFmpeg) -> None:
        self.proc = proc
        self.bytes = 0
        self.writes = 0
        self.closed = False

    def write(self, data: bytes) -> int:
        dies = self.proc.dies_after
        if dies is not None and self.writes >= dies:
            self.proc._exit(1, write=False)  # noqa: SLF001
        if self.closed or self.proc.returncode is not None:
            raise BrokenPipeError
        self.bytes += len(data)
        self.writes += 1
        return len(data)

    def close(self) -> None:
        self.closed = True
        self.proc.stdin_closed()


class FakeFFmpeg:
    """Writes the output file and exits like ffmpeg: 0 when its picture input ends (no sound), 255 on SIGINT
    (with sound, which never ends by itself). ``bad_sound``: exits at once when asked for the pulse input."""

    def __init__(self, argv: list[str], *, bad_sound: bool = False, dies_after: int | None = None) -> None:
        self.argv = argv
        self.out = Path(argv[-1])
        self.sound = "pulse" in argv
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.stdin = _Stdin(self)
        self.dies_after = dies_after
        self._done = threading.Event()
        err = b"Unknown input format: 'pulse'\n" if bad_sound and self.sound else b""
        self.stderr = io.BytesIO(err)
        if bad_sound and self.sound:
            self._exit(1, write=False)

    def _exit(self, code: int, *, write: bool = True) -> None:
        if self.returncode is None:
            if write and self.stdin.bytes:
                self.out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
            self.returncode = code
            self._done.set()

    def stdin_closed(self) -> None:
        if not self.sound:
            self._exit(0)

    def poll(self) -> int | None:
        if self.dies_after is not None and self.stdin.writes >= self.dies_after:
            self._exit(1, write=False)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(self.argv, timeout)
        assert self.returncode is not None
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGINT:
            self._exit(255)

    def terminate(self) -> None:
        self._exit(-15, write=False)

    def kill(self) -> None:
        self._exit(-9, write=False)


class Popen:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.procs: list[FakeFFmpeg] = []
        self.missing = False

    def __call__(self, argv: list[str], **_kw: Any) -> FakeFFmpeg:
        if self.missing:
            raise FileNotFoundError("ffmpeg")
        p = FakeFFmpeg(list(argv), **self.kw)
        self.procs.append(p)
        return p


def _frame(w: int = 1920, h: int = 1080, v: int = 80) -> np.ndarray:
    return np.full((h, w, 3), v, dtype=np.uint8)


def _feed(rec: VideoRecorder, stop: threading.Event, fps: float = 30.0) -> threading.Thread:
    def run() -> None:
        n = 0
        while not stop.is_set():
            rec.offer(_frame(128, 72, n % 255), time.monotonic())
            n += 1
            time.sleep(1.0 / fps)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _wait(cond: Any, timeout: float = 3.0) -> None:
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timed out"
        time.sleep(0.01)


def test_the_command_lines_up_sound_and_picture(tmp_path: Path) -> None:
    rec = VideoRecorder(tmp_path / "a.mp4.part", cv2=None, audio_source="default")
    argv = rec.command(1280, 720, audio=True)
    assert argv[argv.index("-f") + 1] == "pulse" and argv[argv.index("-i") + 1] == "default"
    assert "-use_wallclock_as_timestamps" in argv and "-copyts" in argv
    assert "-shortest" not in argv  # it held the picture back behind the sound (the robot, 2026-11-06)
    assert argv[argv.index("-map") + 1] == "1:v" and "0:a" in argv
    assert argv[argv.index("-threads") + 1] == str(rec_mod.ENCODER_THREADS)
    assert argv[-3:] == ["-f", "mp4", str(tmp_path / "a.mp4.part")] and "+faststart" in argv
    quiet = rec.command(1280, 720, audio=False)
    assert "pulse" not in quiet and "0:a" not in quiet and "-c:a" not in quiet
    assert quiet[quiet.index("-map") + 1] == "0:v"


def test_records_without_sound_and_finishes_when_stdin_closes(tmp_path: Path) -> None:
    popen = Popen()
    rec = VideoRecorder(tmp_path / "v.mp4.part", width=64, fps=20, cv2=None, audio_source=None, popen=popen)
    stop = threading.Event()
    feeder = _feed(rec, stop)
    rec.start()
    _wait(lambda: rec.state == "recording")
    time.sleep(0.5)
    stop.set()
    feeder.join()
    res = rec.stop()
    assert res.ok and res.error is None and not res.audio
    assert (res.width, res.height) == (64, 36)  # 128x72 scaled to the width, even sizes
    assert res.path.is_file() and res.seconds > 0.3
    proc = popen.procs[-1]
    assert proc.stdin.closed and proc.signals == []  # no sound: closing the picture ends it
    assert 6 <= res.frames <= 16  # ~20 a second, however fast the camera sent them
    assert proc.stdin.bytes == res.frames * 64 * 36 * 3  # bgr24 without OpenCV


def test_with_sound_it_stops_with_ctrl_c(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rec_mod, "AUDIO_PROBE_S", 0.2)
    monkeypatch.setattr(rec_mod, "SOUND_TAIL_S", 0.05)
    popen = Popen()
    rec = VideoRecorder(tmp_path / "s.mp4.part", width=64, fps=20, cv2=None, audio_source="default", popen=popen)
    stop = threading.Event()
    feeder = _feed(rec, stop)
    rec.start()
    _wait(lambda: rec.state == "recording")
    time.sleep(0.4)
    stop.set()
    feeder.join()
    res = rec.stop()
    assert res.ok and res.audio and res.audio_error is None
    proc = popen.procs[-1]
    assert proc.sound and proc.signals == [signal.SIGINT] and proc.returncode == 255
    assert len(popen.procs) == 1


def test_sound_that_cant_be_opened_records_without_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rec_mod, "AUDIO_PROBE_S", 0.2)
    popen = Popen(bad_sound=True)
    rec = VideoRecorder(tmp_path / "q.mp4.part", width=64, fps=20, cv2=None, audio_source="default", popen=popen)
    stop = threading.Event()
    feeder = _feed(rec, stop)
    rec.start()
    _wait(lambda: len(popen.procs) == 2 and rec.state == "recording")
    time.sleep(0.2)
    stop.set()
    feeder.join()
    res = rec.stop()
    assert res.ok and not res.audio and "pulse" in (res.audio_error or "")
    assert popen.procs[0].sound and not popen.procs[1].sound


def test_no_ffmpeg(tmp_path: Path) -> None:
    popen = Popen()
    popen.missing = True
    rec = VideoRecorder(tmp_path / "n.mp4.part", cv2=None, audio_source=None, popen=popen)
    rec.offer(_frame(64, 36))
    rec.start()
    _wait(lambda: rec.state == "failed")
    res = rec.stop()
    assert not res.ok and res.error == "no_encoder"


def test_no_frames_from_the_camera(tmp_path: Path) -> None:
    rec = VideoRecorder(tmp_path / "f.mp4.part", cv2=None, audio_source=None, popen=Popen(),
                        first_frame_timeout=0.2)
    rec.start()
    _wait(lambda: rec.state == "failed")
    res = rec.stop()
    assert not res.ok and res.error == "no_frames" and not res.path.exists()


def test_ffmpeg_stopping_by_itself_is_reported(tmp_path: Path) -> None:
    popen = Popen(dies_after=5)
    rec = VideoRecorder(tmp_path / "d.mp4.part", width=64, fps=30, cv2=None, audio_source=None, popen=popen)
    told: list[str] = []
    rec.on_failed = told.append
    stop = threading.Event()
    feeder = _feed(rec, stop)
    rec.start()
    _wait(lambda: rec.state == "failed")
    stop.set()
    feeder.join()
    res = rec.stop()
    assert not res.ok and res.error == "encoder_stopped" and told == ["encoder_stopped"]


def test_offer_only_keeps_the_newest_frame(tmp_path: Path) -> None:
    rec = VideoRecorder(tmp_path / "o.mp4.part", cv2=None, audio_source=None, popen=Popen())
    a, b = _frame(8, 8, 1), _frame(8, 8, 2)
    rec.offer(a)
    rec.offer(b)
    assert rec._latest is b and rec.frames_in == 2  # noqa: SLF001
