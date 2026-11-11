""""Always recording": the last few minutes of video (with the room's sound), in RAM only, thrown away as it goes.

Nothing of it is kept unless someone asks ("save that", the dashboard's or the settings panel's button): then the
pieces are joined into one video in the gallery, like any other video. docs/PHOTOS.md, "Always recording".

How
---
One ffmpeg, the same command as an asked-for recording (littlebot/capture/recorder.py: vision's frames on stdin,
the sound from PipeWire, 1280x720 at 30 pictures a second, H.264 + AAC), writing ~10 s MPEG-TS pieces instead of
one .mp4 (``-f segment -segment_format mpegts -reset_timestamps 1``). MPEG-TS because pieces of it join with
``-c copy`` (the concat demuxer) whatever happened in between, and a piece cut short by a kill is still readable.
The pieces live in ``capture.buffer_dir`` (``/dev/shm/littlebot-buffer``: a RAM disk, never the SD card; a folder
that isn't in RAM is refused). A piece that ended more than ``capture.buffer_minutes`` ago is deleted when the next
tick sees it, and the oldest go first whenever the folder passes ``capture.buffer_max_mb``. The folder is emptied
at start, at stop and at shutdown.

When it runs (``RollingBuffer.blocked``)
----------------------------------------
The setting ``capture.always_recording``: "auto" (while plugged in; no battery monitor counts as plugged in), "on"
(on the battery too) or "off"; ``capture.buffer_enabled: false`` in the config wins over all three.

* "on" while on the battery lasts until the battery is low (``power.low_percent``): then it stops, LittleBot says
  so, and the setting goes back to "auto". Plugging back in also puts "on" back to "auto".
* The camera's privacy switch, ``capture.enabled`` off and "off" stop it and WIPE what was buffered, at once.
* Anything else that stops it (unplugged in "auto", a low battery, an asked-for recording, the camera paused for a
  YouTube video, no pictures from the camera) keeps what was buffered saveable for ``buffer_minutes`` more, then
  wipes it.
* One encoder at a time: while an asked-for recording runs ("record this"), the buffer pauses, and resumes after.
* The microphone's privacy switch (and ``capture.video_sound``) restart the encoder without sound: new pieces are
  silent. A saved clip whose pieces don't all have sound is saved without sound.
* Asleep: vision keeps handing a frame sink its 30 pictures a second, so the buffer carries on. Whenever fewer
  than ``MIN_FPS`` pictures a second arrive for ``STALL_TICKS`` seconds (the camera paused, lost, or too slow), it
  pauses, and resumes when they come again.

``tick()`` (once a second, and at once after anything that matters: a setting, privacy, the charger) compares what
should be with what is. Everything blocking (ffmpeg, files) runs off the loop.

Bus: ``capture.buffering`` {active, sound} when it starts and stops (the small dot on the face,
littlebot/face/display.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from littlebot.capture.recorder import VideoRecorder

log = logging.getLogger("littlebot.capture")

OWNER = "capture_buffer"  # vision's frame sink
BUFFERING = "capture.buffering"  # {active, sound}
SETTING = "capture.always_recording"
MODES = ("auto", "on", "off")
PREFIX = "lbbuf-"
EXT = ".ts"
NAME_RE = re.compile(r"^lbbuf-(\d{4,9})-(\d{6})\.ts$")
JOIN_LIST = f"{PREFIX}join.txt"
TICK_S = 1.0
MIN_FPS = 2.0  # fewer pictures a second than this: the camera isn't filming
STALL_TICKS = 3  # ... for this many ticks in a row: pause
RETRY_S = 30.0  # after the encoder stopped by itself
RECHECK_S = 60.0  # how often ffmpeg and the folder are looked for again
JOIN_TIMEOUT_S = 60.0
LOW_PERCENT = 20.0  # when there is no ``power.low_percent`` setting
RAM_FILESYSTEMS = ("tmpfs", "ramfs")
# What stops it AND wipes what was buffered at once (everything else keeps it saveable for a while).
WIPES = frozenset({"config", "unavailable", "off", "disabled", "camera_off"})
# ... and what leaves vision's frame sink in place, to see the pictures come back.
WAITS_FOR_FRAMES = frozenset({"camera_paused", "no_frames", "retrying"})

WORDS = {
    "config": "Always recording is switched off in LittleBot's config (capture.buffer_enabled).",
    "unavailable": "Photos and videos aren't available on this robot.",
    "off": "Always recording is off.",
    "disabled": "Photos and videos are switched off.",
    "camera_off": "The camera is switched off in Privacy.",
    "no_camera": "LittleBot's camera isn't working right now.",
    "no_encoder": "ffmpeg isn't installed on LittleBot (sudo apt install ffmpeg).",
    "no_ram": "The buffer's folder (capture.buffer_dir) isn't a RAM disk, so nothing is buffered.",
    "on_battery": "LittleBot is running on its battery (it records while plugged in).",
    "low_battery": "The battery is low.",
    "recording": "Paused while LittleBot records a video.",
    "camera_paused": "Paused while a video plays.",
    "no_frames": "Waiting for the camera's pictures.",
    "retrying": "The recorder stopped by itself; LittleBot tries again in a moment.",
    "stopped": "Not started yet.",
}
LOW_BATTERY_LINE = "My battery is low, so I've stopped always recording."


@dataclass(frozen=True)
class Segment:
    path: Path
    run: int  # which encoder wrote it
    index: int  # its place in that run
    bytes: int
    mtime: float  # wall clock: when its last byte was written (its end)


@dataclass(frozen=True)
class Run:
    """One encoder's stretch, once it ended."""

    audio: bool
    seconds: float
    width: int
    height: int


@dataclass(frozen=True)
class Power:
    fitted: bool
    plugged: bool
    low: bool


def in_ram(path: str | os.PathLike[str]) -> bool | None:
    """Whether ``path`` is (or would be) on a RAM disk; None when this system can't tell (no /proc)."""
    try:
        mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    target = os.path.realpath(path)
    best, kind = "", ""
    for line in mounts.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount = fields[4].replace("\\040", " ")
        if (target == mount or target.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best):
            best, kind = mount, right.split()[0]
    return kind in RAM_FILESYSTEMS if best else None


class SegmentRecorder(VideoRecorder):
    """The recorder, writing ~``segment_seconds`` MPEG-TS pieces ``lbbuf-<run>-<n>.ts`` into ``folder``."""

    def __init__(self, folder: Path, run: int, *, segment_seconds: int = 10, **kw: Any) -> None:
        super().__init__(Path(folder) / f"{PREFIX}{int(run):04d}-%06d{EXT}", **kw)
        self.folder = Path(folder)
        self.run = int(run)
        self.segment_seconds = max(1, int(segment_seconds))

    def output_args(self) -> list[str]:
        return ["-f", "segment", "-segment_time", str(self.segment_seconds), "-segment_format", "mpegts",
                "-reset_timestamps", "1", str(self.path)]

    def output_ok(self) -> bool:
        return any(self.folder.glob(f"{PREFIX}{self.run:04d}-*{EXT}"))

    def _finish(self) -> None:
        # A saved clip's still is its last picture; and the camera's picture isn't kept once the encoder stopped.
        with self._cond:
            frame, self._latest = self._latest, None
        if frame is not None:
            self.thumb = self._make_thumb(frame) or self.thumb
        super()._finish()


class SegmentStore:
    """The pieces in the RAM folder. Only files with our own names are ever listed or deleted."""

    def __init__(self, folder: str | os.PathLike[str]) -> None:
        self.folder = Path(folder)

    def ensure(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.folder, 0o700)  # the family's living room: only LittleBot's user

    def segments(self) -> list[Segment]:
        """Oldest first."""
        out: list[Segment] = []
        try:
            names = os.listdir(self.folder)
        except OSError:
            return out
        for name in names:
            m = NAME_RE.match(name)
            if not m:
                continue
            path = self.folder / name
            try:
                st = path.stat()
            except OSError:
                continue
            out.append(Segment(path, int(m.group(1)), int(m.group(2)), int(st.st_size), float(st.st_mtime)))
        out.sort(key=lambda s: (s.run, s.index))
        return out

    def delete(self, segs: list[Segment]) -> int:
        n = 0
        for seg in segs:
            with contextlib.suppress(OSError):
                seg.path.unlink()
                n += 1
        return n

    def wipe(self) -> int:
        """Every piece gone (and the join's list). Returns how many pieces."""
        n = self.delete(self.segments())
        with contextlib.suppress(OSError):
            (self.folder / JOIN_LIST).unlink()
        return n


def span_words(seconds: float) -> str:
    """"the last 3 minutes", "the last minute", "the last 40 seconds" (for the voice)."""
    s = int(round(max(0.0, float(seconds))))
    if s < 50:
        return f"the last {s} seconds"
    m = max(1, int(round(s / 60.0)))  # (pieces are ~10 s: "the last minute" is 60 to 70 s)
    return "the last minute" if m == 1 else f"the last {m} minutes"


class RollingBuffer:
    """The buffer's rules and its encoder, for :class:`~littlebot.capture.service.CaptureService` (``svc``)."""

    def __init__(self, svc: Any, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, run: Callable[..., Any] = subprocess.run,
                 ram_check: Callable[[Path], bool | None] = in_ram, loop: bool = True) -> None:
        self.svc = svc
        self.ctx = svc.ctx
        self.cfg = svc.cfg
        self.clock = clock
        self.wall = wall
        self._run_cmd = run
        self._ram_check = ram_check
        self._loop_wanted = bool(loop)
        self.store = SegmentStore(str(self.cfg.buffer_dir))
        self._lock = asyncio.Lock()
        self._rec: SegmentRecorder | None = None
        self._rec_sound = False
        self._rec_started = 0.0
        self._run_no = 0
        self._runs: dict[int, Run] = {}
        self._thumb: bytes | None = None
        self._sink = False
        self._frames_in = 0  # (the vision thread counts; the loop reads)
        self._measured: tuple[float, int] | None = None
        self._frames_ok = False
        self._bad_ticks = 0
        self._retry_at = 0.0
        self._idle_since: float | None = None  # the encoder stopped then, with pieces left
        self._on_battery = False  # "on" was seen running on the battery (plugging in puts it back to "auto")
        self._dir: tuple[float, bool] | None = None  # (checked at, usable)
        self._ffmpeg: tuple[float, str | None] | None = None
        self._published = False
        self._aio: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[Any] | None = None
        self.state = "stopped"  # "running", else why not (a WORDS code)
        self.saves = 0

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._aio = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        await asyncio.to_thread(self._wipe_files, "start")  # whatever a crash left in RAM
        if self.cfg.buffer_enabled and self._loop_wanted and self._task is None:
            self._task = self._aio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Shutdown (or the service stopping): the encoder is killed and the folder emptied."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        async with self._lock:
            await self._stop_encoder(kill=True)
            self._drop_sink()
            await asyncio.to_thread(self._wipe_files, "stop")
            self.state = "stopped"
            self._publish_state()

    async def make_way(self) -> None:
        """An asked-for recording is about to start: the buffer's encoder ends first (never two at once). The
        ticks keep it paused while that recording runs, and start it again after."""
        async with self._lock:
            self._drop_sink()
            await self._stop_encoder()
            self.state = "recording"
            self._publish_state()

    async def _run_loop(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never take the robot down
                log.exception("capture: the always-recording buffer's tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_S)
            self._wake.clear()

    def poke(self) -> None:
        """Something changed (a setting, privacy, the charger): tick now. Any thread."""
        loop, wake = self._aio, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(wake.set)

    # ================================================================ what should be
    @property
    def mode(self) -> str:
        try:
            v = str(self.ctx.settings.get(SETTING, "auto") or "auto").lower()
        except Exception:  # noqa: BLE001
            v = "auto"
        return v if v in MODES else "auto"

    @property
    def keep_seconds(self) -> float:
        return float(self.cfg.buffer_minutes) * 60.0

    @property
    def running(self) -> bool:
        rec = self._rec
        return rec is not None and rec.running

    def want_sound(self) -> bool:
        return bool(self.svc.video_sound) and bool(self.ctx.privacy.microphone_enabled)

    def power(self) -> Power:
        """The charger and the battery as the power service knows them. No monitor fitted: plugged in."""
        pw = getattr(self.ctx, "power", None)
        if pw is None or not getattr(pw, "fitted", False):
            return Power(False, True, False)
        try:
            st = dict(pw.state())
        except Exception:  # noqa: BLE001
            return Power(False, True, False)
        plugged = bool(st.get("plugged_in"))
        pct = st.get("percent")
        try:
            low_at = float(self.ctx.settings.get("power.low_percent", LOW_PERCENT) or LOW_PERCENT)
        except Exception:  # noqa: BLE001
            low_at = LOW_PERCENT
        low = not plugged and ("low" in (st.get("health") or ())
                               or (isinstance(pct, (int, float)) and float(pct) <= low_at))
        return Power(True, plugged, bool(low))

    def _encoder(self) -> str | None:
        now = self.clock()
        if self._ffmpeg is None or now - self._ffmpeg[0] >= RECHECK_S or now < self._ffmpeg[0]:
            self._ffmpeg = (now, self.svc._ffmpeg())  # noqa: SLF001 - the service's own ffmpeg lookup
        return self._ffmpeg[1]

    def _folder_ok(self) -> bool:
        now = self.clock()
        if self._dir is None or (not self._dir[1] and (now - self._dir[0] >= RECHECK_S or now < self._dir[0])):
            ok = False
            try:
                if self._ram_check(self.store.folder) is True:  # (unknown counts as "not RAM": never the SD card)
                    self.store.ensure()
                    ok = True
            except OSError as e:
                log.warning("capture: the buffer's folder can't be used (%s)", e)
            if not ok and self._dir is None:
                log.warning("capture: %s is not a RAM disk; always recording stays off", self.store.folder)
            self._dir = (now, ok)
        return self._dir[1]

    def _camera(self) -> str | None:
        """Why the camera gives no pictures now (camera_off | no_camera | camera_paused), else None."""
        vision = getattr(self.ctx, "vision", None)
        if vision is None:
            return "no_camera"
        try:
            st = vision.status()
        except Exception:  # noqa: BLE001
            return "no_camera"
        cam = str(st.get("camera") or "")
        if cam == "disabled":
            return "camera_off" if "privacy" in str(st.get("detail") or "") else "no_camera"
        if cam in ("unavailable", "disconnected"):
            return "no_camera"
        if st.get("detect_mode") == "paused":
            return "camera_paused"
        return None

    def blocked(self) -> str | None:
        """Why the buffer must not run now (a ``WORDS`` code), else None. Cheap: in-memory reads."""
        svc = self.svc
        if not self.cfg.buffer_enabled:
            return "config"
        if svc.unavailable_reason() is not None:
            return "unavailable"
        mode = self.mode
        if mode == "off":
            return "off"
        if not svc.switched_on:
            return "disabled"
        if not self.ctx.privacy.camera_enabled:
            return "camera_off"
        if self._encoder() is None:
            return "no_encoder"
        if not self._folder_ok():
            return "no_ram"
        power = self.power()
        if not power.plugged:
            if mode == "auto":
                return "on_battery"
            if power.low:
                return "low_battery"
        if svc.recording is not None:
            return "recording"
        return self._camera()

    # ================================================================ the tick
    async def tick(self) -> str:
        """Make what is match what should be; returns ``state`` ("running", else why not)."""
        async with self._lock:
            return await self._tick()

    async def _tick(self) -> str:
        now = self.clock()
        await self._follow_power()
        self._measure(now)
        why = self.blocked()
        if why is None and (not self._sink or not self._frames_ok):
            fresh = not self._sink  # (asked for again while no pictures come: vision drops a sink that failed)
            why = self._add_sink()
            if fresh:
                self._measured, self._frames_ok = (now, self._frames_in), False  # count from here
        if why is None:
            why = await self._keep_running(now)
        else:
            if why not in WAITS_FOR_FRAMES:
                self._drop_sink()
            if self._rec is not None:
                log.info("capture: always recording stops (%s)", why)
            await self._stop_encoder(kill=why in WIPES)
            if why in WIPES:
                await asyncio.to_thread(self._wipe_files, why)
        if self._rec is not None or self._idle_since is not None:  # (else the folder is empty: nothing to look at)
            await asyncio.to_thread(self._prune, now)
        self.state = why or "running"
        self._publish_state()
        return self.state

    async def _keep_running(self, now: float) -> str | None:
        rec = self._rec
        if rec is not None and not rec.running:  # ffmpeg stopped by itself
            log.warning("capture: the always-recording encoder stopped (%s); trying again in %.0f s",
                        rec.error or rec.state, RETRY_S)
            await self._stop_encoder()
            self._retry_at = now + RETRY_S
            return "retrying"
        if rec is not None and self._rec_sound != self.want_sound():
            log.info("capture: always recording restarts %s sound", "with" if self.want_sound() else "without")
            await self._stop_encoder()
            rec = None
        if rec is None:
            if now < self._retry_at:
                return "retrying"
            if not self._frames_ok:
                return "no_frames"
            self._start_encoder(now)
            return None
        if self._bad_ticks >= STALL_TICKS:
            log.info("capture: always recording pauses (no pictures from the camera)")
            await self._stop_encoder()
            return "no_frames"
        return None

    def _measure(self, now: float) -> None:
        """How many pictures a second vision handed over since the last look."""
        last = self._measured
        if last is None or now < last[0]:
            self._measured = (now, self._frames_in)
            return
        dt = now - last[0]
        if dt < 0.5:
            return
        frames = self._frames_in
        self._frames_ok = (frames - last[1]) / dt >= MIN_FPS
        self._bad_ticks = 0 if self._frames_ok else self._bad_ticks + 1
        self._measured = (now, frames)

    async def _follow_power(self) -> None:
        """"on" is for now: a low battery, or the charger coming back, puts the setting back to "auto"."""
        if not self.cfg.buffer_enabled or self.mode != "on":
            self._on_battery = False
            return
        power = self.power()
        if not power.fitted:
            return
        if not power.plugged:
            self._on_battery = True
            if power.low:
                log.info("capture: the battery is low; always recording goes back to auto")
                self._on_battery = False
                await self._set_mode("auto")
                self.svc._announce(LOW_BATTERY_LINE)  # noqa: SLF001 - the service's own voice
        elif self._on_battery:
            log.info("capture: plugged in again; always recording goes back to auto")
            self._on_battery = False
            await self._set_mode("auto")

    async def _set_mode(self, mode: str) -> None:
        try:
            await asyncio.to_thread(lambda: self.ctx.settings.set(SETTING, mode, source="capture"))
        except Exception:  # noqa: BLE001
            log.warning("capture: the always-recording setting couldn't be changed", exc_info=True)

    # ================================================================ vision's frames
    def _offer(self, frame: Any, now: float = 0.0) -> None:
        """The vision thread: count it, and hand it to the encoder (a reference only)."""
        self._frames_in += 1
        rec = self._rec
        if rec is not None:
            rec.offer(frame, now)

    def _add_sink(self) -> str | None:
        try:
            self.ctx.vision.add_frame_sink(OWNER, self._offer, float(self.cfg.buffer_fps))
        except Exception as e:  # noqa: BLE001 - PhotoUnavailable (camera_off | no_camera), or a broken vision
            reason = str(getattr(e, "reason", "") or "no_camera")
            return reason if reason in WORDS else "no_camera"
        self._sink = True
        return None

    def _drop_sink(self) -> None:
        if not self._sink:
            return
        self._sink = False
        with contextlib.suppress(Exception):
            self.ctx.vision.remove_frame_sink(OWNER)

    # ================================================================ the encoder
    def _start_encoder(self, now: float) -> None:
        cfg = self.cfg
        self._run_no += 1
        sound = self.want_sound()
        rec = SegmentRecorder(self.store.folder, self._run_no, segment_seconds=int(cfg.buffer_segment_seconds),
                              width=int(cfg.buffer_width), fps=int(cfg.buffer_fps), crf=int(cfg.video_crf),
                              preset=str(cfg.video_preset), ffmpeg=self._encoder() or "ffmpeg",
                              audio_source=str(cfg.audio_source) if sound else None,
                              popen=self.svc._popen)  # noqa: SLF001 - one way to start ffmpeg
        rec.on_failed = lambda _why: self.poke()
        self._rec, self._rec_sound, self._rec_started = rec, sound, now
        self._idle_since = None
        self._bad_ticks = 0
        rec.start()
        log.info("capture: always recording runs (%s, the last %g minutes, in %s)",
                 "with sound" if sound else "no sound", float(cfg.buffer_minutes), self.store.folder)

    async def _stop_encoder(self, *, kill: bool = False) -> None:
        """End the encoder: finishing its last piece, or (``kill``) at once."""
        rec, self._rec = self._rec, None
        if rec is None:
            return
        if kill:
            rec.kill()
        res = await asyncio.to_thread(rec.stop, 5.0 if kill else 20.0)
        now = self.clock()
        self._runs[rec.run] = Run(audio=bool(res.audio), seconds=max(0.0, now - self._rec_started),
                                  width=int(res.width), height=int(res.height))
        for old in sorted(self._runs)[:-50]:
            del self._runs[old]
        if res.thumb is not None and not kill:
            self._thumb = res.thumb
        self._idle_since = now

    # ================================================================ the pieces
    def _wipe_files(self, why: str) -> None:
        n = self.store.wipe()
        self._thumb = None
        self._idle_since = None
        if n:
            log.info("capture: the always-recording buffer was wiped (%s; %d pieces)", why, n)

    def wipe_now(self, why: str) -> None:
        """At once, from any thread (the camera's privacy switch): the encoder killed, every piece deleted. The
        next tick waits for ffmpeg to be gone and wipes again."""
        rec = self._rec
        if rec is not None:
            rec.kill()
        self._wipe_files(why)
        self._publish(False)
        self.poke()

    def _prune(self, now: float) -> None:
        """Pieces older than the buffer's length go (while it runs); so does everything once it has been stopped
        for that long; and the oldest whenever the folder is too big."""
        segs = self.store.segments()
        if not segs:
            self._idle_since = None
            return
        if self._rec is None:
            if self._idle_since is None:
                self._idle_since = now
            if now - self._idle_since >= self.keep_seconds or now < self._idle_since:
                self._wipe_files("not saved in time")
                return
        else:
            too_old = self.wall() - self.keep_seconds
            old = [s for s in segs[:-1] if s.mtime < too_old]  # (never the piece being written)
            if old:
                self.store.delete(old)
                segs = [s for s in segs if s not in old]
        cap = int(self.cfg.buffer_max_mb) * 1024 * 1024
        total = sum(s.bytes for s in segs)
        gone: list[Segment] = []
        while total > cap and len(segs) - len(gone) > 1:
            seg = segs[len(gone)]
            gone.append(seg)
            total -= seg.bytes
        if gone:
            log.warning("capture: the always-recording buffer passed %d MB; %d old pieces dropped",
                        int(self.cfg.buffer_max_mb), len(gone))
            self.store.delete(gone)

    def _length(self, seg: Segment, last_of_run: bool, now: float) -> float:
        """A piece's length in seconds: the cut length, but for a run's last piece what was left of the run."""
        cut = float(self.cfg.buffer_segment_seconds)
        if not last_of_run:
            return cut
        rec = self._rec
        if rec is not None and rec.run == seg.run:
            total = max(0.0, now - self._rec_started)
        elif seg.run in self._runs:
            total = self._runs[seg.run].seconds
        else:
            return cut
        return min(2.0 * cut, max(0.0, total - seg.index * cut))

    def _lengths(self, segs: list[Segment]) -> list[float]:
        now = self.clock()
        return [self._length(s, i + 1 == len(segs) or segs[i + 1].run != s.run, now) for i, s in enumerate(segs)]

    def _capped(self, seconds: float) -> float:
        return round(min(self.keep_seconds + float(self.cfg.buffer_segment_seconds), seconds), 1)

    def buffered_seconds(self) -> float:
        return self._capped(sum(self._lengths(self.store.segments())))

    def pick(self, seconds: float | None = None) -> tuple[list[Segment], float]:
        """The newest whole pieces that cover the last ``seconds`` (default: all that is kept), oldest first, and
        their length."""
        segs = self.store.segments()
        lengths = self._lengths(segs)
        want = self.keep_seconds if seconds is None else max(1.0, min(float(seconds), self.keep_seconds))
        picked: list[Segment] = []
        total = 0.0
        for seg, length in zip(reversed(segs), reversed(lengths)):
            if total >= want:
                break
            if seg.bytes <= 0:
                continue
            picked.append(seg)
            total += length
        picked.reverse()
        return picked, total

    # ================================================================ saving
    async def save(self, seconds: float | None = None, *, source: str = "voice",
                   who: str | None = None) -> dict[str, Any]:
        """Join the last ``seconds`` (default: everything buffered) into one video in the gallery."""
        svc = self.svc
        async with self._lock:
            if not self.cfg.buffer_enabled or svc.unavailable_reason() is not None:
                return svc._fail("unavailable", WORDS["config"])  # noqa: SLF001
            ffmpeg = self._encoder()
            if ffmpeg is None:
                return svc._fail("no_encoder")  # noqa: SLF001
            if svc.gallery.free_mb() < int(self.cfg.min_free_mb):
                return svc._fail("disk_full")  # noqa: SLF001
            await self._stop_encoder()  # finishes the piece being written: the last seconds are the ones wanted
            picked, total = await asyncio.to_thread(self.pick, seconds)
            if not picked:
                await self._tick()
                return svc._fail("nothing_buffered", self.why_nothing())  # noqa: SLF001
            audio = all(self._runs.get(s.run, Run(False, 0.0, 0, 0)).audio for s in picked)
            size = next((self._runs[s.run] for s in reversed(picked) if s.run in self._runs), Run(False, 0, 0, 0))
            taken = self.wall()
            item_id = svc.gallery.new_id(taken)
            await asyncio.to_thread(svc.gallery.ensure)
            part = svc.gallery.part(item_id)
            error = await asyncio.to_thread(self._join, ffmpeg, picked, part, audio)
            item = None
            if error is None:
                item = await asyncio.to_thread(svc.gallery.finish_video, item_id, taken_at=taken - total,
                                               seconds=total, width=size.width, height=size.height,
                                               thumb=self._thumb, source=source, who=who)
            if item is None:
                await asyncio.to_thread(svc.gallery.discard_part, item_id)
                log.warning("capture: the last %.0f s couldn't be saved (%s)", total, error or "nothing written")
            else:
                self.saves += 1
                log.info("capture: the last %.0f s saved as video %s (%s, %d pieces, %d MB%s)", total, item.id,
                         source, len(picked), item.bytes // (1024 * 1024), "" if audio else ", no sound")
            await self._tick()  # the encoder starts again
        if item is None:
            return svc._fail("failed", "The clip couldn't be saved.")  # noqa: SLF001
        return {"ok": True, "item": item.public(), "seconds": round(total, 1), "sound": audio,
                "words": f"Saved {span_words(total)}."}

    def join_command(self, ffmpeg: str, listing: Path, out: Path, *, audio: bool) -> list[str]:
        argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                "-i", str(listing), "-map", "0:v:0"]
        if audio:
            argv += ["-map", "0:a:0?"]
        return argv + ["-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(out)]

    def _join(self, ffmpeg: str, segs: list[Segment], out: Path, audio: bool) -> str | None:
        """ffmpeg's concat of the pieces into ``out`` (no re-encoding). Returns what went wrong, else None."""
        listing = self.store.folder / JOIN_LIST
        try:
            lines = ["ffconcat version 1.0"] + ["file '%s'" % str(s.path).replace("'", "'\\''") for s in segs]
            listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
            done = self._run_cmd(self.join_command(ffmpeg, listing, out, audio=audio), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=JOIN_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)[:200]
        finally:
            with contextlib.suppress(OSError):
                listing.unlink()
        if getattr(done, "returncode", 1) != 0:
            err = getattr(done, "stderr", b"") or b""
            return (err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)).strip()[-200:] or "failed"
        return None

    def why_nothing(self) -> str:
        why = self.state if self.state != "running" else self.blocked()
        if why is None or why == "running":
            return "Nothing is buffered yet: always recording only just started."
        return f"There's nothing to save. {WORDS.get(why, WORDS['off'])}"

    # ================================================================ views
    def status(self) -> dict[str, Any]:
        segs = self.store.segments()
        power = self.power()
        now = self.clock()
        left = None
        if self._rec is None and segs and self._idle_since is not None:
            left = round(max(0.0, self.keep_seconds - (now - self._idle_since)))
        state = self.state
        return {
            "enabled": bool(self.cfg.buffer_enabled), "setting": self.mode, "running": self.running,
            "state": state, "detail": None if state == "running" else WORDS.get(state, state),
            "seconds": self._capped(sum(self._lengths(segs))),
            "mb": round(sum(s.bytes for s in segs) / (1024 * 1024), 1), "pieces": len(segs),
            "minutes": float(self.cfg.buffer_minutes), "max_mb": int(self.cfg.buffer_max_mb),
            "sound": self._rec_sound if self._rec is not None else self.want_sound(),
            "battery": power.fitted, "plugged_in": power.plugged, "wipes_in": left, "saves": self.saves,
        }

    def _publish_state(self) -> None:
        active = self.running
        if active != self._published:
            self._publish(active)

    def _publish(self, active: bool) -> None:
        self._published = active
        with contextlib.suppress(Exception):
            self.ctx.bus.publish(BUFFERING, {"active": active, "sound": bool(active and self._rec_sound)},
                                 source="capture")


__all__ = ["BUFFERING", "MODES", "OWNER", "SETTING", "WORDS", "RollingBuffer", "Segment", "SegmentRecorder",
           "SegmentStore", "in_ram", "span_words"]
