"""Microphone and speaker for a live call: 20 ms frames in, the model's voice out.

The Pi has no echo canceller, so by default this is half-duplex: while LittleBot's speaker is
playing the model's voice (and for ``echo_tail`` after), the microphone sends silence, or the
model would hear itself and answer itself. "Speaking" is measured on the audio actually handed
to the speaker, so the gate follows what is audible, not what arrived over the network.

The face's mouth follows the voice too: every block handed to the speaker gets a level and a
mouth shape (:class:`~littlebot.audio.dsp.VisemeEstimator`), stamped with the moment PortAudio
says it will be heard, and :attr:`LiveAudio.mouth` reads the one being heard now.

Nothing is recorded: samples only pass through small in-memory buffers.
"""

from __future__ import annotations

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
        return out if self.mic_open else np.zeros(FRAME, dtype=np.int16)

    # ------------------------------------------------------------ speaker
    def play_frame(self, frame: Any) -> None:
        """Queue one decoded frame of the model's voice (an av.AudioFrame from the remote track)."""
        if self._muted:
            return
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
