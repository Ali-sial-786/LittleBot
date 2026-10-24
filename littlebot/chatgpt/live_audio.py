"""Microphone and speaker for a live call: 20 ms frames in, the model's voice out.

The Pi has no echo canceller, so by default this is half-duplex: while LittleBot's speaker is
playing the model's voice (and for ``echo_tail`` after), the microphone sends silence, or the
model would hear itself and answer itself. "Speaking" is measured on the audio actually handed
to the speaker, so the gate follows what is audible, not what arrived over the network.

The face's mouth follows the voice too: every block handed to the speaker gets a level and a
mouth shape (:class:`~littlebot.audio.dsp.VisemeEstimator`), stamped with the moment PortAudio
says it will be heard, and :attr:`LiveAudio.mouth` reads the one being heard now.

A call started by the wake word sends what was said with it first (:class:`CarryFeed`).

Nothing is recorded: samples only pass through small in-memory buffers.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from typing import Any, Callable

import numpy as np

from littlebot.audio import devices as dev
from littlebot.audio import dsp
from littlebot.audio.errors import AudioDeviceError

log = logging.getLogger("littlebot.chatgpt.live")

RATE = 48000  # WebRTC / Opus
FRAME = RATE // 50  # 20 ms
MAX_QUEUED_S = 1.0  # more than this waiting for the speaker = we fell behind: drop the oldest
MOUTH_LEAD_S = 0.06  # report the mouth this early: polling (~20 Hz) and drawing the face take about that
MAX_OUTPUT_DELAY_S = 0.5  # a reported speaker delay above this is nonsense: ignore it
# CarryFeed: while frames wait to be sent, each pause is cut to KEEP_PAUSE_S (enough to keep words apart);
# the call is then back in real time at their first pause.
KEEP_PAUSE_S = 0.3
BEHIND_FRAMES = 2  # this many frames waiting = behind real time (fewer is ordinary jitter)
MAX_WAITING_S = 10.0  # never more than this waiting to be sent: the oldest goes
PACE_SLACK_S = 0.06  # a frame this late (the microphone or the encoder was slow) restarts the beat: no burst


class LiveAudio:
    """Real devices through sounddevice (PortAudio). ``sd`` is injectable for tests."""

    def __init__(self, *, mic: int | str | None, speaker: int | str | None, volume: Callable[[], float],
                 mic_gain: Callable[[], float] = lambda: 1.0, half_duplex: bool = True, echo_tail_s: float = 0.45,
                 voice_level: float = 0.012, sd: Any = None, now: Callable[[], float] = time.monotonic) -> None:
        self.mic_spec, self.speaker_spec = mic, speaker
        self._volume, self._mic_gain = volume, mic_gain
        self.half_duplex = half_duplex
        self.echo_tail_s = echo_tail_s
        self.voice_level = voice_level
        self._sd = sd
        self._now = now
        self._lock = threading.Lock()
        self._out: collections.deque[np.ndarray] = collections.deque()
        self._out_queued = 0
        self._in: collections.deque[np.ndarray] = collections.deque(maxlen=50)
        self._in_event: Any = None
        self._loop: Any = None
        self._in_stream: Any = None
        self._out_stream: Any = None
        self.mic_rate = RATE
        self.out_rate = RATE
        self._out_channels = 1
        self._in_resampler: Any = None
        self._pcm = np.zeros(0, dtype=np.int16)  # microphone audio at 48 kHz not yet sent
        self._out_resampler: Any = None
        self._muted = False
        self._vol, self._vol_at = 0.0, float("-inf")
        self._last_loud = float("-inf")
        self.level = 0.0  # last speaker block RMS (0..1)
        self._visemes: dsp.VisemeEstimator | None = None
        self._mouths: collections.deque[tuple[float, float, str]] = collections.deque(maxlen=64)  # (heard at, level, shape)
        self._mouth: tuple[float, str] = (0.0, "closed")
        self.mic_level = 0.0
        self.device_names = ("", "")
        # For the call's "still on" log line (littlebot/chatgpt/live.py, LiveCall._pulse): counts only, never sound.
        self.frames_in = self.frames_gated = self.frames_out = 0  # mic frames sent (of them silenced), voice frames
        self.peak_in = 0.0  # the loudest mic block since the last line

    # ------------------------------------------------------------ lifecycle
    def open(self, loop: Any) -> None:
        import asyncio  # noqa: PLC0415

        import av  # noqa: PLC0415  (comes with aiortc)

        sd = self._sd or dev.get_sounddevice()
        if sd is None:
            raise AudioDeviceError(f"PortAudio unavailable ({dev.sounddevice_error()})")
        self._loop = loop
        self._in_event = asyncio.Event()
        mic = dev.find_device(self.mic_spec, "input")
        spk = dev.find_device(self.speaker_spec, "output")
        self.mic_rate = self._pick_rate(sd.check_input_settings, mic, 1)
        self.out_rate, self._out_channels = self._pick_output(sd, spk)
        self._in_resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        self._out_resampler = av.AudioResampler(format="s16", layout="mono", rate=self.out_rate)
        with dev._PA_LOCK:  # noqa: SLF001 - same lock the audio service uses around PortAudio
            self._out_stream = sd.OutputStream(device=spk["index"], samplerate=self.out_rate, channels=self._out_channels,
                                               dtype="float32", blocksize=self.out_rate // 50, latency="high",
                                               callback=self._play_cb)
            dev._stream_opened()  # noqa: SLF001
            try:
                self._in_stream = sd.InputStream(device=mic["index"], samplerate=self.mic_rate, channels=1,
                                                 dtype="int16", blocksize=self.mic_rate // 50, latency="low",
                                                 callback=self._record_cb)
                dev._stream_opened()  # noqa: SLF001
            except Exception:
                self._out_stream.close()
                dev._stream_closed()  # noqa: SLF001
                self._out_stream = None
                raise
        self._out_stream.start()
        self._in_stream.start()
        self.device_names = (mic["name"], spk["name"])
        log.info("live audio: mic %s @ %d Hz, speaker %s @ %d Hz", mic["name"], self.mic_rate, spk["name"], self.out_rate)

    @staticmethod
    def _pick_rate(check: Callable[..., Any], d: dict[str, Any], channels: int) -> int:
        for rate in dev._rate_candidates(RATE, d.get("default_samplerate") or 0):  # noqa: SLF001
            try:
                check(device=d["index"], channels=channels, samplerate=rate, dtype="int16")
                return rate
            except Exception:  # noqa: BLE001
                continue
        raise AudioDeviceError(f"{d['name']}: no usable sample rate")

    def _pick_output(self, sd: Any, d: dict[str, Any]) -> tuple[int, int]:
        for channels in (1, 2):
            if channels > max(1, int(d.get("max_output_channels") or 1)):
                continue
            for rate in dev._rate_candidates(RATE, d.get("default_samplerate") or 0):  # noqa: SLF001
                try:
                    sd.check_output_settings(device=d["index"], channels=channels, samplerate=rate, dtype="float32")
                    return rate, channels
                except Exception:  # noqa: BLE001
                    continue
        raise AudioDeviceError(f"{d['name']}: no usable sample rate / channel layout")

    def close(self) -> None:
        for name in ("_in_stream", "_out_stream"):
            stream = getattr(self, name)
            setattr(self, name, None)
            if stream is None:
                continue
            with dev._PA_LOCK:  # noqa: SLF001
                try:
                    stream.stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    log.debug("closing live audio stream failed", exc_info=True)
                dev._stream_closed()  # noqa: SLF001
        with self._lock:
            self._out.clear()
            self._out_queued = 0
            self._in.clear()
        self._pcm = np.zeros(0, dtype=np.int16)
        self.level = self.mic_level = 0.0
        self._mouths.clear()
        self._mouth = (0.0, "closed")

    # ------------------------------------------------------------ state
    @property
    def speaking(self) -> bool:
        """The model's voice is audible now (or was within the echo tail)."""
        return self._now() - self._last_loud < self.echo_tail_s

    @property
    def mic_open(self) -> bool:
        return not (self.half_duplex and self.speaking)

    @property
    def mouth(self) -> tuple[float, str]:
        """``(level 0..1, viseme)`` of the voice heard right now, for the face's lip-sync.

        Blocks are stamped with when the speaker will play them, so this follows the sound, not
        the network; it runs :data:`MOUTH_LEAD_S` early to make up for the face's own delay.
        """
        now = self._now() + MOUTH_LEAD_S
        q = self._mouths
        while q and q[0][0] <= now:  # only this reader pops; the audio thread only appends
            _, level, shape = q.popleft()
            self._mouth = (level, shape)
        return self._mouth

    def mute(self) -> None:
        """Silence the speaker at once and drop whatever is queued (safety cut, hang-up)."""
        self._muted = True
        with self._lock:
            self._out.clear()
            self._out_queued = 0
        self._mouths.clear()
        self._mouth = (0.0, "closed")

    # ------------------------------------------------------------ microphone
    def _record_cb(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:  # PortAudio thread
        block = np.array(indata[:, 0], dtype=np.int16, copy=True)
        with self._lock:
            self._in.append(block)
        loop, ev = self._loop, self._in_event
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # loop closed
                pass

    async def _next_block(self) -> np.ndarray:
        while True:
            with self._lock:
                block = self._in.popleft() if self._in else None
                if not self._in and self._in_event is not None:
                    self._in_event.clear()
            if block is not None:
                return block
            await self._in_event.wait()

    async def read_frame(self) -> np.ndarray:
        """The next 20 ms of microphone audio at 48 kHz (int16, mono); silence while the gate is shut."""
        import av  # noqa: PLC0415

        while len(self._pcm) < FRAME:
            block = await self._next_block()
            gain = max(0.0, min(4.0, float(self._mic_gain())))
            if gain != 1.0:
                block = np.clip(block.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
            self.mic_level = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2))) if len(block) else 0.0
            if self.mic_rate != RATE:
                frame = av.AudioFrame.from_ndarray(block.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = self.mic_rate
                parts = [f.to_ndarray().reshape(-1) for f in self._in_resampler.resample(frame)]
                block = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
            self._pcm = np.concatenate([self._pcm, block.astype(np.int16)])
        out, self._pcm = self._pcm[:FRAME], self._pcm[FRAME:]
        if len(self._pcm) > RATE // 5:  # never let the microphone lag behind
            self._pcm = self._pcm[-FRAME:]
        self.frames_in += 1
        self.peak_in = max(self.peak_in, self.mic_level)
        if not self.mic_open:
            self.frames_gated += 1
            return np.zeros(FRAME, dtype=np.int16)
        return out

    # ------------------------------------------------------------ speaker
    def play_frame(self, frame: Any) -> None:
        """Queue one decoded frame of the model's voice (an av.AudioFrame from the remote track)."""
        if self._muted:
            return
        self.frames_out += 1
        for f in self._out_resampler.resample(frame):
            pcm = f.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
            with self._lock:
                self._out.append(pcm)
                self._out_queued += len(pcm)
                while self._out_queued > MAX_QUEUED_S * self.out_rate and len(self._out) > 1:
                    self._out_queued -= len(self._out.popleft())

    def _take(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < n and self._out:
                head = self._out[0]
                k = min(n - filled, len(head))
                out[filled:filled + k] = head[:k]
                filled += k
                if k == len(head):
                    self._out.popleft()
                else:
                    self._out[0] = head[k:]
                self._out_queued -= k
        return out

    def _output_delay(self, time_info: Any) -> float:
        """Seconds until the block being filled is heard: PortAudio's DAC time, else the stream latency."""
        try:
            delay = float(time_info.outputBufferDacTime) - float(time_info.currentTime)
            if 0.0 < delay <= MAX_OUTPUT_DELAY_S:
                return delay
        except Exception:  # noqa: BLE001 - no timing info (some hosts / fakes): use the latency
            pass
        try:
            latency = float(getattr(self._out_stream, "latency", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return latency if 0.0 <= latency <= MAX_OUTPUT_DELAY_S else 0.0

    def _play_cb(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:  # PortAudio thread
        y = self._take(frames) if not self._muted else np.zeros(frames, dtype=np.float32)
        rms = float(np.sqrt(np.mean(y * y))) if frames else 0.0
        self.level = rms
        now = self._now()
        if rms >= self.voice_level:
            self._last_loud = now
        try:  # lip-sync for the face: must never break the voice (an exception here stops the stream)
            est = self._visemes
            if est is None or est.sample_rate != self.out_rate:
                est = self._visemes = dsp.VisemeEstimator(self.out_rate)
            level, shape = est.process(y) if frames else (0.0, "closed")
            self._mouths.append((now + self._output_delay(time_info), level, shape))
        except Exception:  # noqa: BLE001
            log.debug("lip-sync estimate failed", exc_info=True)
        if now - self._vol_at > 0.25:  # re-read the volume a few times a second, not every block
            self._vol_at, self._vol = now, max(0.0, min(1.0, float(self._volume())))
        y = y * self._vol
        outdata[:] = np.repeat(y[:, None], self._out_channels, axis=1) if self._out_channels > 1 else y[:, None]


class CarryFeed:
    """The microphone of a call the wake word started: what was said with the wake word first, then ``audio``.

    ``start()`` (once the devices are open) reads ``audio.read_frame()`` all the time from then on, so what is
    said while the call connects waits here too (LiveAudio itself keeps only the last second) and the
    half-duplex gate is applied as it is heard, not when it is sent. ``read_frame()`` (the call's MicTrack)
    gives one 20 ms frame per 20 ms, never faster than real time: a burst could be dropped or mistimed by the
    far end. While frames are waiting, each pause is cut to KEEP_PAUSE_S, so the call is back in real time
    at their first pause. Timestamps stay continuous: MicTrack numbers the frames it is given.

    ``carry``: a littlebot.audio.carry.SpeechCarry (taken once; its audio is never kept anywhere else).
    """

    def __init__(self, audio: Any, carry: Any) -> None:
        self.audio = audio
        self._carry = carry
        self.quiet_level = float(getattr(carry, "quiet_level", 0.0105))
        self._waiting: collections.deque[np.ndarray] = collections.deque()
        self._arrived = asyncio.Event()
        self._puller: Any = None
        self._due: float | None = None
        self._pause = 0  # quiet frames in a row
        self._keep_pause = int(round(KEEP_PAUSE_S * RATE / FRAME))
        self._carried = 0  # frames of the carried speech still waiting
        self._on_at: float | None = None  # the first frame asked for: the call's sound flows
        self._first_heard: float | None = None  # the first frame from the call's own microphone
        self.sent = self.skipped = self.lost = 0
        self.caught_up = False
        samples = carry.take() if carry is not None else np.zeros(0, dtype=np.float32)
        rate = int(getattr(carry, "sample_rate", RATE) or RATE)
        if len(samples):
            pcm = dsp.to_int16(dsp.resample(dsp.fade(samples, rate, 0.01, 0.01), rate, RATE))  # no click at the cut
            pcm = np.concatenate([pcm, np.zeros(-len(pcm) % FRAME, dtype=np.int16)])
            self._waiting.extend(row.copy() for row in pcm.reshape(-1, FRAME))
            del samples, pcm
        self._carried = len(self._waiting)
        self.carried_s = self._carried * FRAME / RATE
        self.carried_speech_s = sum(1 for f in self._waiting if self._loud(f)) * FRAME / RATE

    def start(self, spawn: Callable[[Any], Any] = asyncio.ensure_future) -> None:
        """Start reading the microphone (``spawn`` makes the task; the call's own, so it ends with the call)."""
        if self._puller is None:
            self._puller = spawn(self._pull())

    def close(self) -> None:
        if self._puller is not None and not self._puller.done():
            self._puller.cancel()
        self._waiting.clear()
        self._arrived.set()
        self._carry = None

    @property
    def waiting_s(self) -> float:
        return len(self._waiting) * FRAME / RATE

    async def _pull(self) -> None:
        try:
            while True:
                frame = await self.audio.read_frame()
                if self._first_heard is None:
                    self._first_heard = time.monotonic()
                self._waiting.append(frame)
                while len(self._waiting) > MAX_WAITING_S * RATE / FRAME:  # far behind (a very slow connect)
                    self._waiting.popleft()
                    self._carried = max(0, self._carried - 1)
                    self.lost += 1
                self._arrived.set()
        finally:
            self._arrived.set()  # a waiting reader then reads the microphone itself

    async def read_frame(self) -> np.ndarray:
        """The next 20 ms to send (int16, 48 kHz)."""
        if self._on_at is None:
            self._on_at = time.monotonic()
            self._log_start()
        await self._beat()
        while True:
            frame, waited = await self._next()
            if waited:  # nothing was waiting: the microphone sets the pace from here
                self._due = time.monotonic() + FRAME / RATE
            quiet = not self._loud(frame)
            self._pause = self._pause + 1 if quiet else 0
            if quiet and self._pause > self._keep_pause and len(self._waiting) >= BEHIND_FRAMES:
                self.skipped += 1  # a long pause while behind: catch up
                continue
            self.sent += 1
            if not self.caught_up and not self._carried and len(self._waiting) < BEHIND_FRAMES:
                self.caught_up = True
                log.info("live voice: caught up with real time %d ms after the call's microphone came on "
                         "(%.1f s of pauses left out)", int((time.monotonic() - self._on_at) * 1000),
                         self.skipped * FRAME / RATE)
            return frame

    async def _beat(self) -> None:
        """Wait for this frame's 20 ms slot (a slot missed by more than PACE_SLACK_S restarts the beat)."""
        now = time.monotonic()
        if self._due is None or now - self._due > PACE_SLACK_S:
            self._due = now
        elif self._due > now:
            await asyncio.sleep(self._due - now)
        self._due += FRAME / RATE

    async def _next(self) -> tuple[np.ndarray, bool]:
        waited = False
        while not self._waiting:
            if self._puller is None or self._puller.done():
                return await self.audio.read_frame(), True
            waited = True
            self._arrived.clear()
            await self._arrived.wait()
        if self._carried:
            self._carried -= 1
        return self._waiting.popleft(), waited

    def _loud(self, frame: np.ndarray) -> bool:
        x = frame.astype(np.float32)
        return float(np.sqrt(np.mean(x * x))) / 32768.0 >= self.quiet_level if len(x) else False

    def _log_start(self) -> None:
        carry = self._carry
        now = time.monotonic()
        heard_at = getattr(carry, "heard_at", None)
        handed_at = getattr(carry, "sealed_at", None)
        gap = ""
        if handed_at is not None and self._first_heard is not None:
            gap = f"; microphone hand-over gap about {max(0, int((self._first_heard - handed_at) * 1000) - 20)} ms"
        log.info("live voice: microphone on%s; sending first the %.1f s carried over from the wake word (%.1f s of "
                 "speech) and the %.1f s heard while connecting%s",
                 f" {int((now - heard_at) * 1000)} ms after the wake phrase" if heard_at is not None else "",
                 self.carried_s, self.carried_speech_s, max(0, len(self._waiting) - self._carried) * FRAME / RATE, gap)
