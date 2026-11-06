"""One video being recorded: the camera's frames (from the vision thread) and the room's sound into an .mp4.

The camera stays with vision (littlebot/vision/service.py ``add_frame_sink``): LittleBot keeps tracking faces
while it films, and the privacy switch, a lost camera or a video playing stop the frames exactly as they stop
everything else. ``offer(frame, now)`` (the vision thread) only keeps a reference to the newest frame. A writer
thread of its own then, ``video_fps`` times a second, scales that frame to ``video_width`` (1280x720 for the 1080p
camera), turns it into YUV 4:2:0 and writes it to ffmpeg's stdin; a frame that didn't change is written again, so
the video keeps time when the camera slows down in a dim room.

ffmpeg (Debian's, with libx264 and PipeWire's Pulse input)::

    ffmpeg -f pulse -sample_rate 48000 -channels 1 -i default               (the sound, unless off)
           -f rawvideo -pix_fmt yuv420p -video_size 1280x720 -framerate 30
           -use_wallclock_as_timestamps 1 -i pipe:0                         (the frames)
           -copyts -avoid_negative_ts make_zero                             (one clock for both)
           -c:v libx264 -preset veryfast -crf 23 -threads 2 -c:a aac -b:a 96k
           -movflags +faststart -f mp4 <id>.mp4.part

Both inputs are stamped with the wall clock, and ``-copyts`` keeps those stamps, so sound and picture line up
however long ffmpeg took to open each one (zeroing each input on its own would let the sound lag the picture by
that difference; on the robot the picture starts ~0.1 s after the sound). Closing stdin ends the picture, then
SIGINT (ffmpeg's Ctrl-C) ends the sound and ffmpeg finishes the file (``+faststart``: a phone starts playing at
once). The sound can't be opened (no PipeWire, the microphone busy): the video is recorded without it, and
``audio_error`` says why.

Blocking work (ffmpeg, the pipe) runs on the writer thread; ``stop()`` blocks until the file is finished, so the
asyncio side calls it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("littlebot.capture")

FIRST_FRAME_TIMEOUT_S = 5.0  # no camera frame by then: the recording fails ("no_frames")
AUDIO_PROBE_S = 1.5  # ffmpeg exiting this soon with the sound on: the sound is the problem, try again without
FINISH_TIMEOUT_S = 60.0  # ffmpeg finishing the file (+faststart rewrites it once)
SOUND_TAIL_S = 0.3  # after the last frame: this much more sound, then ffmpeg is told to stop
# x264 on two of the Pi 5's four cores (the voice, the face and vision keep the others); a shorter look-ahead keeps
# ffmpeg's memory near 100 MB instead of ~240 MB.
ENCODER_THREADS = 2
THUMB_WIDTH = 640
THUMB_QUALITY = 80
STDERR_LINES = 30


@dataclass
class Result:
    ok: bool
    path: Path
    seconds: float = 0.0
    width: int = 0
    height: int = 0
    frames: int = 0
    thumb: bytes | None = None
    audio: bool = False
    audio_error: str | None = None
    error: str | None = None  # no_frames | no_encoder | encoder_stopped | failed


Popen = Callable[..., Any]


class VideoRecorder:
    def __init__(self, path: str | Path, *, width: int = 1280, fps: int = 30, crf: int = 23,
                 preset: str = "veryfast", ffmpeg: str = "ffmpeg", audio_source: str | None = "default",
                 popen: Popen = subprocess.Popen, clock: Callable[[], float] = time.monotonic,
                 first_frame_timeout: float = FIRST_FRAME_TIMEOUT_S, cv2: Any = "auto") -> None:
        self.path = Path(path)
        self.width = max(64, int(width))
        self.fps = max(1, int(fps))
        self.crf = int(crf)
        self.preset = str(preset)
        self.ffmpeg = str(ffmpeg)
        self.audio_source = audio_source or None
        self._popen = popen
        self._clock = clock
        self._first_timeout = float(first_frame_timeout)
        if cv2 == "auto":
            from littlebot.vision.imaging import get_cv2  # noqa: PLC0415

            cv2 = get_cv2()
        self._cv2 = cv2
        self._cond = threading.Condition()
        self._latest: Any = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: Any = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        self.state = "new"  # new | waiting | recording | finishing | done | failed
        self.size = (0, 0)
        self.frames = 0
        self.frames_in = 0
        self.started_at: float | None = None  # monotonic: the first frame went to ffmpeg
        self.ended_at: float | None = None
        self.audio = self.audio_source is not None
        self.audio_error: str | None = None
        self.error: str | None = None
        self.thumb: bytes | None = None
        self._done = threading.Event()
        self._probe_seq = -1  # what the sound check already wrote
        self._probe_buf = b""
        self.on_failed: Callable[[str], None] | None = None  # (writer thread) ffmpeg stopped by itself

    # ------------------------------------------------------------ the vision thread
    def offer(self, frame: Any, now: float = 0.0) -> None:
        """The newest camera frame (keeps a reference only; the vision thread never waits here)."""
        with self._cond:
            self._latest = frame
            self._seq += 1
            self.frames_in += 1
            self._cond.notify_all()

    # ------------------------------------------------------------ control
    def start(self) -> None:
        if self._thread is not None:
            return
        self.state = "waiting"
        self._thread = threading.Thread(target=self._run, name="littlebot-recorder", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self.state in ("waiting", "recording")

    def seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, (self.ended_at if self.ended_at is not None else self._clock()) - self.started_at)

    def stop(self, timeout: float = FINISH_TIMEOUT_S) -> Result:
        """End the recording and finish the file (blocks: call it from a thread)."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        t = self._thread
        if t is not None:
            t.join(timeout + 5.0)
        self._done.wait(0.0 if t is None else 1.0)
        ok = self.state == "done" and self.path.is_file() and self.path.stat().st_size > 0
        return Result(ok=ok, path=self.path, seconds=round(self.seconds(), 1), width=self.size[0], height=self.size[1],
                      frames=self.frames, thumb=self.thumb, audio=self.audio and ok, audio_error=self.audio_error,
                      error=None if ok else (self.error or "failed"))

    def kill(self) -> None:
        """Shutdown in a hurry: no file."""
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()

    # ------------------------------------------------------------ the writer thread
    def _run(self) -> None:
        try:
            frame = self._wait_first()
            if frame is None:
                self._fail("no_frames" if not self._stop.is_set() else "stopped")
                return
            h, w = frame.shape[:2]
            ow = min(self.width, int(w)) // 2 * 2
            oh = max(2, int(round(h * ow / max(1, w))) // 2 * 2)
            self.size = (ow, oh)
            self.thumb = self._make_thumb(frame)
            if not self._spawn():
                return
            self._write_loop(ow, oh)
            self._finish()
        except Exception:  # noqa: BLE001 - never take the robot down
            log.exception("capture: the recorder failed")
            self._fail("failed")
            self._kill_proc()
        finally:
            self._done.set()

    def _wait_first(self) -> Any:
        deadline = self._clock() + self._first_timeout
        with self._cond:
            while self._latest is None and not self._stop.is_set():
                left = deadline - self._clock()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 0.25))
            return self._latest

    def command(self, ow: int, oh: int, *, audio: bool) -> list[str]:
        pix = "yuv420p" if self._cv2 is not None else "bgr24"
        argv = [self.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning", "-y"]
        if audio and self.audio_source:
            argv += ["-f", "pulse", "-thread_queue_size", "1024", "-sample_rate", "48000", "-channels", "1",
                     "-i", str(self.audio_source)]
        argv += ["-f", "rawvideo", "-pix_fmt", pix, "-video_size", f"{ow}x{oh}", "-framerate", str(self.fps),
                 "-use_wallclock_as_timestamps", "1", "-thread_queue_size", "64", "-i", "pipe:0"]
        vin = "1" if audio and self.audio_source else "0"
        argv += ["-map", f"{vin}:v"] + (["-map", "0:a"] if vin == "1" else [])
        argv += ["-copyts", "-avoid_negative_ts", "make_zero", "-fps_mode", "passthrough",
                 "-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf), "-pix_fmt", "yuv420p",
                 "-profile:v", "high", "-g", str(self.fps * 2), "-threads", str(ENCODER_THREADS),
                 "-rc-lookahead", "15"]
        if vin == "1":
            argv += ["-c:a", "aac", "-b:a", "96k"]
        argv += ["-movflags", "+faststart", "-f", "mp4", str(self.path)]
        return argv

    def _spawn(self) -> bool:
        """Start ffmpeg; with the sound first, and again without it if the sound is what stops it."""
        ow, oh = self.size
        for audio in ([True, False] if self.audio else [False]):
            self.started_at, self.frames, self.state = None, 0, "waiting"  # (a try with the sound is not counted)
            argv = self.command(ow, oh, audio=audio)
            try:
                proc = self._popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   start_new_session=True)
            except FileNotFoundError:
                self._fail("no_encoder")
                return False
            except OSError as e:
                log.warning("capture: ffmpeg didn't start (%s)", str(e)[:120])
                self._fail("no_encoder")
                return False
            self._proc = proc
            self._stderr.clear()
            reader = threading.Thread(target=self._read_stderr, args=(proc,), name="littlebot-recorder-log",
                                      daemon=True)
            reader.start()
            if not audio:
                self.audio = False
                return True
            # The sound is opened before the picture: a missing Pulse input or an unknown source ends ffmpeg at once.
            ok = self._first_write_ok(proc)
            if ok:
                self.audio = True
                return True
            self._kill_proc()
            reader.join(1.0)  # ffmpeg's last words (why the sound failed) arrive as it exits
            self.audio_error = self._last_error() or "the sound couldn't be opened"
            log.warning("capture: recording without sound (%s)", self.audio_error[:160])
        self._fail("encoder_stopped")
        return False

    def _first_write_ok(self, proc: Any) -> bool:
        """Write the first frame, then wait ``AUDIO_PROBE_S`` (writing frames) to see that ffmpeg keeps running."""
        ow, oh = self.size
        t0 = self._clock()
        last_seq, buf = -1, b""
        interval = 1.0 / self.fps
        while self._clock() - t0 < AUDIO_PROBE_S:
            if proc.poll() is not None:
                return False
            with self._cond:
                frame, seq = self._latest, self._seq
            if seq != last_seq:
                buf, last_seq = self._convert(frame, ow, oh), seq
            try:
                proc.stdin.write(buf)
            except (BrokenPipeError, OSError, ValueError):
                return False
            if self.started_at is None:
                self.started_at = self._clock()
                self.state = "recording"
            self.frames += 1
            if self._stop.is_set():
                break
            self._stop.wait(interval)
        if proc.poll() is not None:
            return False
        self._probe_seq = last_seq
        self._probe_buf = buf
        return True

    def _write_loop(self, ow: int, oh: int) -> None:
        proc = self._proc
        interval = 1.0 / self.fps
        last_seq, buf = self._probe_seq, self._probe_buf
        self._probe_buf = b""
        next_t = self._clock()
        while not self._stop.is_set():
            with self._cond:
                frame, seq = self._latest, self._seq
            if seq != last_seq:
                buf, last_seq = self._convert(frame, ow, oh), seq
            frame = None
            try:
                proc.stdin.write(buf)
            except (BrokenPipeError, OSError, ValueError):
                if not self._stop.is_set():
                    log.warning("capture: ffmpeg stopped while recording (%s)", (self._last_error() or "?")[:160])
                    self._fail("encoder_stopped")
                    cb = self.on_failed
                    if cb is not None:
                        with contextlib.suppress(Exception):
                            cb("encoder_stopped")
                return
            if self.started_at is None:
                self.started_at = self._clock()
                self.state = "recording"
            self.frames += 1
            next_t += interval
            delay = next_t - self._clock()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -1.0:  # far behind (a busy moment): carry on from now, never burst
                next_t = self._clock()

    def _finish(self) -> None:
        self.state = "finishing"
        self.ended_at = self._clock()
        proc = self._proc
        with contextlib.suppress(Exception):
            proc.stdin.close()  # the picture ends
        if self.audio:
            # The sound never ends by itself: Ctrl-C is ffmpeg's own "stop recording" (it finishes the file and
            # exits 255). Not -shortest: on ffmpeg 7.1 it held the picture back behind the sound until the end
            # (a 10 s video took 5 s more to finish, or lost most of its frames; the robot, 2026-11-06).
            with contextlib.suppress(subprocess.TimeoutExpired, Exception):
                proc.wait(timeout=SOUND_TAIL_S)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.send_signal(signal.SIGINT)
        try:
            code = proc.wait(timeout=FINISH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.warning("capture: ffmpeg didn't finish the video in %.0f s; stopping it", FINISH_TIMEOUT_S)
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                code = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = -9
        ended_by_us = code == 0 or (self.audio and code in (255, -signal.SIGINT))
        if ended_by_us and self.path.is_file() and self.path.stat().st_size > 0:
            self.state = "done"
        else:
            log.warning("capture: ffmpeg ended with %s (%s)", code, (self._last_error() or "")[:200])
            self.error = self.error or "encoder_stopped"
            self.state = "failed"

    def _fail(self, why: str) -> None:
        self.error = self.error or why
        self.state = "failed"
        if self.ended_at is None:
            self.ended_at = self._clock()

    def _kill_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.stdin.close()
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=2.0)

    def _read_stderr(self, proc: Any) -> None:
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
                if line:
                    self._stderr.append(line[:300])
                    log.debug("capture ffmpeg: %s", line[:300])
        except (OSError, ValueError):
            pass

    def _last_error(self) -> str:
        return " | ".join(list(self._stderr)[-3:])

    # ------------------------------------------------------------ pictures
    def _convert(self, frame: Any, ow: int, oh: int) -> bytes:
        cv2 = self._cv2
        h, w = frame.shape[:2]
        if cv2 is None:  # (tests, a robot without OpenCV): nearest-neighbour, BGR
            import numpy as np  # noqa: PLC0415

            if (w, h) != (ow, oh):
                ys = (np.arange(oh) * h / oh).astype(np.intp)
                xs = (np.arange(ow) * w / ow).astype(np.intp)
                frame = frame[ys][:, xs]
            return bytes(memoryview(np.ascontiguousarray(frame)))
        img = frame if (w, h) == (ow, oh) else cv2.resize(frame, (ow, oh), interpolation=cv2.INTER_LINEAR)
        return cv2.cvtColor(img, cv2.COLOR_BGR2YUV_I420).tobytes()

    def _make_thumb(self, frame: Any) -> bytes | None:
        cv2 = self._cv2
        if cv2 is None:
            return None
        try:
            from littlebot.vision.imaging import resize_to_width  # noqa: PLC0415

            ok, buf = cv2.imencode(".jpg", resize_to_width(frame, THUMB_WIDTH),
                                   [int(cv2.IMWRITE_JPEG_QUALITY), THUMB_QUALITY])
            return buf.tobytes() if ok else None
        except Exception:  # noqa: BLE001
            return None


__all__ = ["Result", "VideoRecorder"]
