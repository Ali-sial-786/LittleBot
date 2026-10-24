"""Speaker and microphone backends, device enumeration and selection.

Output backends (all take mono float32 audio and play it blocking, in the
AudioService output thread, in small chunks so they can be interrupted and so
volume changes apply immediately):

* ``SoundDeviceOutput``  PortAudio via ``sounddevice`` (preferred, desktop + Pi)
* ``PygameMixerOutput``  SDL mixer fallback
* ``MockOutput``         records what was played; simulates playback time
                         (``time_scale`` 0 = instant, used by tests)
* ``NullOutput``         speaker disabled (``backend: none``)

Input backends deliver mono float32 chunks at the configured rate (16 kHz):

* ``SoundDeviceInput``   PortAudio callback stream (its own thread) -> bounded queue
* ``MockInput``          synthetic noise floor + injected (speech-like) audio
* ``NullInput``          microphone disabled

Devices are chosen by index, case-insensitive name substring, or the system
default (``None``). Sample rate / channel count fall back to what the device
supports and audio is resampled. A failure mid-stream raises
``AudioDeviceError``; the service marks the device disconnected and retries
later. Nothing here ever needs a real device to import or construct.

Echo cancelling: ``scripts/setup_echo_cancel.sh`` makes PipeWire's echo-cancelled
microphone and speaker (``littlebot_ec_mic`` / ``littlebot_ec_speaker``) ALSA devices
that PortAudio lists. With the default device choice (``None`` / "auto") and
``audio.echo_cancel: auto`` they are used whenever *both* exist, by everything that
opens a device here (the audio service, the wake word, live voice), else the USB
devices as before. Through PipeWire a device can be opened by several streams at
once; the ``hw:`` USB devices can't.
"""

from __future__ import annotations

import collections
import dataclasses
import logging
import re
import threading
import time
from typing import Any, Callable

import numpy as np

from littlebot.audio import dsp
from littlebot.audio.errors import AudioDeviceError

log = logging.getLogger("littlebot.audio.devices")

# Device status vocabulary (also used in status() and audio.device_status events)
STATUS_OK = "ok"
STATUS_SIMULATED = "simulated"
STATUS_DISCONNECTED = "disconnected"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DISABLED = "disabled"

# ------------------------------------------------------------------ PortAudio
_PA_LOCK = threading.RLock()
_sd_module: Any = None
_sd_error: str | None = None
_open_streams = 0


def get_sounddevice() -> Any:
    """The ``sounddevice`` module, or None when it (or the PortAudio library) is missing."""
    global _sd_module, _sd_error
    if _sd_module is not None:
        return _sd_module
    if _sd_error is not None:
        return None
    try:
        import sounddevice as sd  # noqa: PLC0415 - optional dependency
    except Exception as e:  # noqa: BLE001 - ImportError, or OSError when libportaudio2 is missing
        _sd_error = f"{type(e).__name__}: {e}"
        log.info("sounddevice unavailable (%s)", _sd_error)
        return None
    _sd_module = sd
    return sd


def sounddevice_error() -> str | None:
    get_sounddevice()
    return _sd_error


def _stream_opened() -> None:
    global _open_streams
    with _PA_LOCK:
        _open_streams += 1


def _stream_closed() -> None:
    global _open_streams
    with _PA_LOCK:
        _open_streams = max(0, _open_streams - 1)


def refresh_portaudio() -> bool:
    """Re-initialise PortAudio so newly plugged USB devices appear.

    PortAudio only enumerates devices at initialisation. This is only done when
    no LittleBot stream is open (it would invalidate them). Returns True if done.
    """
    sd = get_sounddevice()
    if sd is None:
        return False
    with _PA_LOCK:
        if _open_streams > 0:
            return False
        try:
            sd._terminate()  # noqa: SLF001 - documented workaround for hot-plug
            sd._initialize()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            log.debug("PortAudio re-initialisation failed", exc_info=True)
            return False
    return True


def query_devices() -> list[dict[str, Any]]:
    """Every PortAudio device as a plain dict ([] when PortAudio is unavailable)."""
    sd = get_sounddevice()
    if sd is None:
        return []
    with _PA_LOCK:
        try:
            raw = list(sd.query_devices())
        except Exception:  # noqa: BLE001
            log.debug("query_devices failed", exc_info=True)
            return []
        try:
            hostapis = [h.get("name", "") for h in sd.query_hostapis()]
        except Exception:  # noqa: BLE001
            hostapis = []
        default_in = _default_index(sd, "input")
        default_out = _default_index(sd, "output")
    out = []
    for i, d in enumerate(raw):
        idx = int(d.get("index", i))
        ha = int(d.get("hostapi", 0))
        out.append({
            "index": idx,
            "name": str(d.get("name", f"device {idx}")),
            "hostapi": hostapis[ha] if 0 <= ha < len(hostapis) else str(ha),
            "max_input_channels": int(d.get("max_input_channels", 0)),
            "max_output_channels": int(d.get("max_output_channels", 0)),
            "default_samplerate": float(d.get("default_samplerate", 0.0) or 0.0),
            "default_input": idx == default_in,
            "default_output": idx == default_out,
        })
    return out


def _default_index(sd: Any, kind: str) -> int:
    try:
        info = sd.query_devices(kind=kind)
        return int(info.get("index", -1))
    except Exception:  # noqa: BLE001 - no default device
        return -1


def list_devices() -> dict[str, Any]:
    """{inputs: [{index, name, default, channels, default_samplerate, hostapi}], outputs: [...], portaudio, detail}"""
    devs = query_devices()
    inputs = [{"index": d["index"], "name": d["name"], "default": d["default_input"],
               "channels": d["max_input_channels"], "default_samplerate": d["default_samplerate"],
               "hostapi": d["hostapi"]} for d in devs if d["max_input_channels"] > 0]
    outputs = [{"index": d["index"], "name": d["name"], "default": d["default_output"],
                "channels": d["max_output_channels"], "default_samplerate": d["default_samplerate"],
                "hostapi": d["hostapi"]} for d in devs if d["max_output_channels"] > 0]
    err = sounddevice_error()
    detail = f"PortAudio unavailable ({err})" if err else ("" if devs else "no audio devices found")
    return {"inputs": inputs, "outputs": outputs, "portaudio": err is None, "detail": detail}


_USB = re.compile(r"\busb\b|\buac", re.I)
_HDMI = re.compile(r"hdmi|vc4", re.I)


def is_usb_device(d: dict[str, Any]) -> bool:
    return bool(_USB.search(d.get("name", "")))


def is_hdmi_device(d: dict[str, Any]) -> bool:
    return bool(_HDMI.search(d.get("name", "")))


# ------------------------------------------------------------ echo cancelling (PipeWire)
# The ALSA PCM names scripts/setup_echo_cancel.sh writes to ~/.asoundrc (one per PipeWire node of the same
# name). PortAudio's ALSA host API lists every top-level ``pcm.<name>`` of the ALSA configuration by that name.
EC_MIC = "littlebot_ec_mic"
EC_SPEAKER = "littlebot_ec_speaker"
_EC_NAMES = {"input": EC_MIC, "output": EC_SPEAKER}
EC_MODES = ("auto", "on", "off")
_ec_mode = "auto"
_ec_missing_warned = False


def set_echo_cancel(mode: str) -> None:
    """``audio.echo_cancel``: auto (the echo-cancelled devices when they exist), on (the same, warning when they
    don't), off (never: the USB devices as before)."""
    global _ec_mode, _ec_missing_warned
    mode = str(mode or "auto").strip().lower()
    _ec_mode = mode if mode in EC_MODES else "auto"
    _ec_missing_warned = False


def echo_cancel_mode() -> str:
    return _ec_mode


def is_echo_cancel_device(d: dict[str, Any] | str | None, kind: str | None = None) -> bool:
    """Whether ``d`` (a device, or its name) is one of PipeWire's echo-cancelled devices (of ``kind``)."""
    name = (d.get("name", "") if isinstance(d, dict) else str(d or "")).strip().lower()
    names = [_EC_NAMES[kind]] if kind in _EC_NAMES else list(_EC_NAMES.values())
    return any(name == n or name.startswith((n + " ", n + ":")) for n in names)


def echo_cancel_devices(devs: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """PipeWire's echo-cancelled ``(microphone, speaker)``, or None unless *both* are listed.

    One without the other cancels nothing: the canceller only removes what was played through its own
    speaker, and a ``hw:`` speaker opened directly would also keep PipeWire off the USB device."""
    devs = query_devices() if devs is None else devs
    mic = next((d for d in devs if d["max_input_channels"] > 0 and is_echo_cancel_device(d, "input")), None)
    spk = next((d for d in devs if d["max_output_channels"] > 0 and is_echo_cancel_device(d, "output")), None)
    return (mic, spk) if mic is not None and spk is not None else None


def echo_cancel_note() -> str:
    """Why echo cancelling isn't in use, for the log."""
    if _ec_mode == "off":
        return "audio.echo_cancel: off"
    if echo_cancel_devices() is not None:
        return (f"PipeWire's {EC_MIC} / {EC_SPEAKER} exist, but another device was chosen: leave "
                "audio.microphone.device and audio.speaker.device empty (null) to use them")
    return "not set up on this robot: scripts/setup_echo_cancel.sh"


def _auto_echo_cancel(kind: str, devs: list[dict[str, Any]]) -> dict[str, Any] | None:
    global _ec_missing_warned
    if _ec_mode == "off":
        return None
    pair = echo_cancel_devices(devs)
    if pair is not None:
        return pair[0] if kind == "input" else pair[1]
    if _ec_mode == "on" and not _ec_missing_warned:
        _ec_missing_warned = True
        log.warning("audio.echo_cancel is on, but PipeWire's echo-cancelled devices (%s, %s) aren't there: using "
                    "the USB devices without echo cancelling. Set them up with scripts/setup_echo_cancel.sh "
                    "(check: scripts/setup_echo_cancel.sh --check)", EC_MIC, EC_SPEAKER)
    return None


def find_device(spec: int | str | None, kind: str) -> dict[str, Any]:
    """Pick a device: int = index, str = name substring, 'system' = the desktop's default.

    None / 'default' / 'auto': PipeWire's echo-cancelled microphone and speaker when both exist (see
    ``audio.echo_cancel``), else the robot's own USB speaker (or microphone) when one is plugged in,
    whatever the desktop's default is - on a Pi that default is easily the HDMI screen. Without a
    USB device: the system default, but never HDMI while anything else exists.
    """
    key = "max_output_channels" if kind == "output" else "max_input_channels"
    every = query_devices()
    devs = [d for d in every if d[key] > 0]
    if not devs:
        err = sounddevice_error()
        raise AudioDeviceError(f"no {kind} devices found" + (f" ({err})" if err else ""))
    system = isinstance(spec, str) and spec.strip().lower() == "system"
    if system or spec is None or (isinstance(spec, str) and spec.strip().lower() in ("", "default", "auto", "none")):
        if not system:
            ec = _auto_echo_cancel(kind, every)
            if ec is not None:
                return ec
            usb = [d for d in devs if is_usb_device(d)]
            if usb:
                return usb[0]
        for d in devs:
            if d[f"default_{kind}"] and (system or not is_hdmi_device(d)):
                return d
        for d in devs:  # PortAudio has no default: prefer the ALSA/PipeWire "default" routes
            if d["name"].lower() in ("default", "pipewire", "pulse"):
                return d
        return next((d for d in devs if not is_hdmi_device(d)), devs[0])
    if isinstance(spec, bool):
        raise AudioDeviceError(f"invalid {kind} device {spec!r}")
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().lstrip("-").isdigit()):
        idx = int(spec)
        for d in devs:
            if d["index"] == idx:
                return d
        raise AudioDeviceError(f"{kind} device #{idx} not found ({_names(devs)})")
    needle = str(spec).strip().lower()
    exact = [d for d in devs if d["name"].lower() == needle]
    if exact:
        return exact[0]
    partial = [d for d in devs if needle in d["name"].lower()]
    if partial:
        return partial[0]
    raise AudioDeviceError(f"no {kind} device matching {spec!r} ({_names(devs)})")


def _names(devs: list[dict[str, Any]]) -> str:
    names = ", ".join(f"#{d['index']} {d['name']}" for d in devs[:8])
    return f"available: {names}" if names else "none available"


def _rate_candidates(preferred: int, device_default: float) -> list[int]:
    out: list[int] = []
    for r in (preferred, int(device_default or 0), 48000, 44100, 32000, 16000):
        if r > 0 and r not in out:
            out.append(r)
    return out


def _ramp(chunk: np.ndarray, g0: float, g1: float) -> np.ndarray:
    """Apply gain, ramping smoothly from g0 to g1 across the chunk (no zipper clicks)."""
    if len(chunk) == 0:
        return chunk.astype(np.float32)
    if abs(g0 - g1) < 1e-6:
        return (chunk * g1).astype(np.float32)
    return (chunk * np.linspace(g0, g1, len(chunk), dtype=np.float32)).astype(np.float32)


# --------------------------------------------------------------- playback ctl
class PlaybackControl:
    """What a backend needs from the service while playing one item."""

    def __init__(self, stop_event: threading.Event | None = None, *, gain: Callable[[], float] | None = None,
                 progress: Callable[[float], None] | None = None, label: str = "") -> None:
        self.stop_event = stop_event or threading.Event()
        self._gain = gain or (lambda: 1.0)
        self._progress = progress
        self.label = label

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    def gain(self) -> float:
        try:
            return max(0.0, min(1.0, float(self._gain())))
        except Exception:  # noqa: BLE001
            return 0.0

    def progress(self, seconds: float) -> None:
        if self._progress is not None:
            try:
                self._progress(max(0.0, seconds))
            except Exception:  # noqa: BLE001 - never let UI callbacks break playback
                log.debug("progress callback failed", exc_info=True)


# ================================================================== outputs
class OutputBackend:
    name = "base"
    real = False

    def __init__(self, sample_rate: int = 22050) -> None:
        self.sample_rate = int(sample_rate)
        self.device_name: str | None = None
        self.status = STATUS_UNAVAILABLE
        self.detail = ""

    def _set_status(self, status: str, detail: str = "") -> None:
        self.status, self.detail = status, detail

    def open(self) -> None:
        """Find / check the device (cheap). Raises AudioDeviceError if unusable."""

    def play(self, samples: np.ndarray, sample_rate: int, ctrl: PlaybackControl) -> bool:
        """Blocking. True when played to the end, False when stopped. AudioDeviceError on failure."""
        raise NotImplementedError

    def release(self) -> None:
        """Free resources held between items (called after a quiet period)."""

    def close(self) -> None:
        self.release()

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "device": self.device_name, "status": self.status, "detail": self.detail}


class SoundDeviceOutput(OutputBackend):
    name = "sounddevice"
    real = True

    def __init__(self, device: int | str | None = None, sample_rate: int = 22050, *,
                 latency: str | float = "high", block_frames: int = 1024, lead_in_seconds: float = 0.03) -> None:
        super().__init__(sample_rate)
        self.device_spec = device
        self.preferred_rate = int(sample_rate)
        self.latency = latency
        self.block_frames = int(block_frames)
        self.lead_in_seconds = max(0.0, lead_in_seconds)
        self._index: int | None = None
        self._channels = 1
        self._stream: Any = None
        self._lock = threading.RLock()

    def open(self) -> None:
        sd = get_sounddevice()
        if sd is None:
            self._set_status(STATUS_UNAVAILABLE, f"PortAudio unavailable ({sounddevice_error()})")
            raise AudioDeviceError(self.detail)
        try:
            dev = find_device(self.device_spec, "output")
        except AudioDeviceError as e:
            self._set_status(STATUS_UNAVAILABLE, str(e))
            raise
        rate, channels = self._negotiate(sd, dev)
        with self._lock:
            self._close_stream()
            self._index, self._channels, self.sample_rate = dev["index"], channels, rate
            self.device_name = dev["name"]
        self._set_status(STATUS_OK, f"{dev['name']} ({rate} Hz, {channels} ch, {dev['hostapi']})")

    def _negotiate(self, sd: Any, dev: dict[str, Any]) -> tuple[int, int]:
        max_ch = max(1, dev["max_output_channels"])
        for channels in (1, 2):
            if channels > max_ch:
                continue
            for rate in _rate_candidates(self.preferred_rate, dev["default_samplerate"]):
                try:
                    sd.check_output_settings(device=dev["index"], channels=channels, samplerate=rate, dtype="float32")
                    return rate, channels
                except Exception:  # noqa: BLE001
                    continue
        msg = f"{dev['name']}: no supported sample rate / channel layout"
        self._set_status(STATUS_UNAVAILABLE, msg)
        raise AudioDeviceError(msg)

    def _ensure_stream(self) -> Any:
        sd = get_sounddevice()
        with self._lock:
            if self._stream is None:
                with _PA_LOCK:
                    self._stream = sd.OutputStream(device=self._index, samplerate=self.sample_rate,
                                                   channels=self._channels, dtype="float32", latency=self.latency)
                    _stream_opened()
            return self._stream

    def _frames(self, y: np.ndarray) -> np.ndarray:
        y = np.clip(y, -1.0, 1.0).astype(np.float32)
        if self._channels > 1:
            return np.ascontiguousarray(np.repeat(y[:, None], self._channels, axis=1))
        return np.ascontiguousarray(y[:, None])

    def play(self, samples: np.ndarray, sample_rate: int, ctrl: PlaybackControl) -> bool:
        if self._index is None:
            self.open()
        x = samples if sample_rate == self.sample_rate else dsp.resample(samples, sample_rate, self.sample_rate)
        rate = float(self.sample_rate)
        try:
            stream = self._ensure_stream()
            latency = float(getattr(stream, "latency", 0.0) or 0.0)
            if not stream.active:
                stream.start()
                # USB speakers often swallow the first few ms after waking up: lead in with silence
                lead = int(self.lead_in_seconds * self.sample_rate)
                if lead > 0:
                    stream.write(self._frames(np.zeros(lead, dtype=np.float32)))
            g_prev = ctrl.gain()
            written = 0
            for i in range(0, len(x), self.block_frames):
                if ctrl.stopped:
                    stream.abort()
                    return False
                chunk = x[i:i + self.block_frames]
                g = ctrl.gain()
                stream.write(self._frames(_ramp(chunk, g_prev, g)))
                g_prev = g
                written += len(chunk)
                ctrl.progress(written / rate - latency)
            # let the buffered tail play out while staying interruptible
            end = time.monotonic() + latency
            while time.monotonic() < end:
                if ctrl.stop_event.wait(0.02):
                    stream.abort()
                    return False
                ctrl.progress(written / rate - max(0.0, end - time.monotonic()))
            stream.stop()
            ctrl.progress(written / rate)
            return True
        except AudioDeviceError:
            raise
        except Exception as e:  # noqa: BLE001 - PortAudioError (unplugged, busy...) and friends
            self._fail(e)
            raise AudioDeviceError(f"speaker failed: {e}") from e

    def _fail(self, e: BaseException) -> None:
        with self._lock:
            self._close_stream()
            self._index = None  # indexes change after re-plugging: resolve again on reconnect
        self._set_status(STATUS_DISCONNECTED, f"{self.device_name or 'speaker'}: {e}")
        log.warning("speaker failure: %s", e)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:  # noqa: BLE001
            pass
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
        _stream_closed()

    def release(self) -> None:
        with self._lock:
            self._close_stream()


class PygameMixerOutput(OutputBackend):
    name = "pygame"
    real = True

    def __init__(self, device: int | str | None = None, sample_rate: int = 22050, *, allow_dummy: bool = False) -> None:
        super().__init__(sample_rate)
        self.device_spec = device
        self.allow_dummy = allow_dummy
        self._we_initialised = False
        self._channels = 2

    def _match_device(self) -> str | None:
        if not isinstance(self.device_spec, str) or not self.device_spec.strip():
            return None
        try:
            import pygame._sdl2.audio as sdl2_audio  # noqa: PLC0415

            names = list(sdl2_audio.get_audio_device_names(False))
        except Exception:  # noqa: BLE001
            return None
        needle = self.device_spec.strip().lower()
        return next((n for n in names if needle in n.lower()), None)

    def open(self) -> None:
        try:
            import pygame  # noqa: PLC0415
        except Exception as e:  # noqa: BLE001
            self._set_status(STATUS_UNAVAILABLE, f"pygame unavailable: {e}")
            raise AudioDeviceError(self.detail) from e
        devname = None
        try:
            if not pygame.mixer.get_init():
                kwargs: dict[str, Any] = {"frequency": self.sample_rate, "size": -16, "channels": 2, "buffer": 1024}
                devname = self._match_device()
                if devname:
                    kwargs["devicename"] = devname
                pygame.mixer.init(**kwargs)
                self._we_initialised = True
            driver = pygame.mixer.get_driver() if hasattr(pygame.mixer, "get_driver") else "unknown"
        except Exception as e:  # noqa: BLE001
            self._set_status(STATUS_UNAVAILABLE, f"SDL mixer could not open a speaker: {e}")
            raise AudioDeviceError(self.detail) from e
        if driver in ("dummy", "disk") and not self.allow_dummy:
            self.close()
            self._set_status(STATUS_UNAVAILABLE, f"SDL audio driver is '{driver}' (no real speaker)")
            raise AudioDeviceError(self.detail)
        freq, _size, channels = pygame.mixer.get_init()
        self.sample_rate, self._channels = int(freq), int(channels)
        self.device_name = devname or f"SDL default ({driver})"
        self._set_status(STATUS_OK, f"{self.device_name} ({freq} Hz)")

    def play(self, samples: np.ndarray, sample_rate: int, ctrl: PlaybackControl) -> bool:
        import pygame  # noqa: PLC0415

        try:
            if not pygame.mixer.get_init():
                self.open()
            x = samples if sample_rate == self.sample_rate else dsp.resample(samples, sample_rate, self.sample_rate)
            pcm = dsp.to_int16(x)
            if self._channels > 1:
                pcm = np.repeat(pcm[:, None], self._channels, axis=1)
            sound = pygame.mixer.Sound(buffer=np.ascontiguousarray(pcm).tobytes())
            gain = ctrl.gain()
            channel = sound.play()
            if channel is None:
                raise AudioDeviceError("no free SDL mixer channel")
            channel.set_volume(gain)
            dur = len(x) / float(self.sample_rate)
            t0 = time.monotonic()
            while channel.get_busy():
                if ctrl.stop_event.wait(0.03):
                    channel.stop()
                    return False
                g = ctrl.gain()
                if abs(g - gain) > 1e-3:
                    channel.set_volume(g)
                    gain = g
                elapsed = time.monotonic() - t0
                ctrl.progress(elapsed)
                if elapsed > dur + 2.0:
                    channel.stop()
                    break
            ctrl.progress(dur)
            return True
        except AudioDeviceError:
            raise
        except Exception as e:  # noqa: BLE001
            self._set_status(STATUS_DISCONNECTED, f"SDL mixer failed: {e}")
            raise AudioDeviceError(self.detail) from e

    def close(self) -> None:
        if self._we_initialised:
            try:
                import pygame  # noqa: PLC0415

                pygame.mixer.quit()
            except Exception:  # noqa: BLE001
                pass
            self._we_initialised = False


@dataclasses.dataclass
class PlayedSound:
    label: str
    duration: float  # seconds of audio submitted
    sample_rate: int
    peak: float  # after gain (what would have reached the speaker)
    completed: bool
    played_seconds: float


class MockOutput(OutputBackend):
    """Simulated speaker: nothing is heard, but timing and events are realistic.

    ``time_scale`` 1.0 = real time (desktop simulation: the face animates while
    "speaking"), 0 = instant (tests). ``fail_after_seconds`` simulates a device
    unplugged mid-playback; ``fail_on_open`` a missing device.
    """

    name = "mock"
    real = False

    def __init__(self, sample_rate: int = 22050, *, time_scale: float = 1.0, chunk_seconds: float = 0.04,
                 fail_after_seconds: float | None = None, fail_on_open: bool = False, max_records: int = 500) -> None:
        super().__init__(sample_rate)
        self.time_scale = max(0.0, float(time_scale))
        self.chunk_seconds = chunk_seconds
        self.fail_after_seconds = fail_after_seconds
        self.fail_on_open = fail_on_open
        self.max_records = max_records
        self.played: list[PlayedSound] = []
        self.opens = 0
        self.device_name = "simulated speaker"
        self._set_status(STATUS_SIMULATED, "simulated speaker (nothing is heard)")

    def open(self) -> None:
        self.opens += 1
        if self.fail_on_open:
            self._set_status(STATUS_UNAVAILABLE, "simulated: speaker missing")
            raise AudioDeviceError(self.detail)
        self._set_status(STATUS_SIMULATED, "simulated speaker (nothing is heard)")

    def play(self, samples: np.ndarray, sample_rate: int, ctrl: PlaybackControl) -> bool:
        if self.fail_on_open:
            self._set_status(STATUS_UNAVAILABLE, "simulated: speaker missing")
            raise AudioDeviceError(self.detail)
        n = len(samples)
        chunk = max(1, int(self.chunk_seconds * sample_rate))
        pos = 0
        peak_out = 0.0
        g_prev = ctrl.gain()
        completed = False
        try:
            while pos < n:
                if ctrl.stopped:
                    break
                if self.fail_after_seconds is not None and pos / sample_rate >= self.fail_after_seconds:
                    self._set_status(STATUS_DISCONNECTED, "simulated speaker failure")
                    raise AudioDeviceError("simulated speaker failure (unplugged)")
                c = samples[pos:pos + chunk]
                g = ctrl.gain()
                y = _ramp(c, g_prev, g)
                g_prev = g
                if len(y):
                    peak_out = max(peak_out, float(np.max(np.abs(y))))
                pos += len(c)
                ctrl.progress(pos / sample_rate)
                if self.time_scale > 0 and ctrl.stop_event.wait(len(c) / sample_rate * self.time_scale):
                    break
            completed = pos >= n and not ctrl.stopped
        finally:
            self.played.append(PlayedSound(ctrl.label, n / float(sample_rate), int(sample_rate), peak_out,
                                           completed, pos / float(sample_rate)))
            del self.played[: -self.max_records]
        return completed


class NullOutput(OutputBackend):
    name = "none"
    real = False

    def __init__(self, sample_rate: int = 22050) -> None:
        super().__init__(sample_rate)
        self._set_status(STATUS_DISABLED, "speaker disabled in config (backend: none)")

    def play(self, samples: np.ndarray, sample_rate: int, ctrl: PlaybackControl) -> bool:
        return False


# =================================================================== inputs
class InputBackend:
    name = "base"
    real = False

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = int(sample_rate)
        self.device_name: str | None = None
        self.status = STATUS_UNAVAILABLE
        self.detail = ""

    def _set_status(self, status: str, detail: str = "") -> None:
        self.status, self.detail = status, detail

    @property
    def is_open(self) -> bool:
        return False

    def probe(self) -> None:
        """Check the device exists without opening it. Raises AudioDeviceError."""

    def open(self) -> None:
        raise AudioDeviceError("no microphone")

    def read(self, timeout: float) -> np.ndarray | None:
        """Next chunk (mono float32) or None if nothing within ``timeout``. AudioDeviceError on failure."""
        return None

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "device": self.device_name, "status": self.status, "detail": self.detail}


class SoundDeviceInput(InputBackend):
    """PortAudio input stream; its callback thread fills a bounded chunk queue.

    The stream is only open while LittleBot is listening / testing (privacy
    and CPU). A stream that stops delivering audio for ``stall_seconds`` is
    treated as disconnected (USB mic unplugged).
    """

    name = "sounddevice"
    real = True

    def __init__(self, device: int | str | None = None, sample_rate: int = 16000, channels: int = 1, *,
                 chunk_seconds: float = 0.032, max_queue_chunks: int = 96, stall_seconds: float = 2.0) -> None:
        super().__init__(sample_rate)
        self.device_spec = device
        self.channels = max(1, int(channels))
        self.chunk_seconds = chunk_seconds
        self.stall_seconds = stall_seconds
        self._index: int | None = None
        self._dev_rate = sample_rate
        self._dev_channels = 1
        self._stream: Any = None
        self._lock = threading.RLock()
        self._cv = threading.Condition()
        self._chunks: collections.deque[np.ndarray] = collections.deque(maxlen=max_queue_chunks)
        self._resampler: dsp.StreamResampler | None = None
        self._last_chunk_at = 0.0
        self._failed: str | None = None
        self.dropped = 0
        self.overflows = 0

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def probe(self) -> None:
        sd = get_sounddevice()
        if sd is None:
            self._set_status(STATUS_UNAVAILABLE, f"PortAudio unavailable ({sounddevice_error()})")
            raise AudioDeviceError(self.detail)
        try:
            dev = find_device(self.device_spec, "input")
        except AudioDeviceError as e:
            self._set_status(STATUS_UNAVAILABLE, str(e))
            raise
        max_ch = max(1, dev["max_input_channels"])
        for channels in sorted({min(self.channels, max_ch), 1, min(2, max_ch)}):
            for rate in _rate_candidates(self.sample_rate, dev["default_samplerate"]):
                try:
                    sd.check_input_settings(device=dev["index"], channels=channels, samplerate=rate, dtype="float32")
                except Exception:  # noqa: BLE001
                    continue
                self._index, self._dev_rate, self._dev_channels = dev["index"], rate, channels
                self.device_name = dev["name"]
                self._set_status(STATUS_OK, f"{dev['name']} ({rate} Hz, {channels} ch, {dev['hostapi']})")
                return
        self._set_status(STATUS_UNAVAILABLE, f"{dev['name']}: no supported sample rate")
        raise AudioDeviceError(self.detail)

    def open(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            if self._index is None:
                self.probe()
            sd = get_sounddevice()
            self._resampler = dsp.StreamResampler(self._dev_rate, self.sample_rate)
            self._failed = None
            self.flush()
            blocksize = max(64, int(self._dev_rate * self.chunk_seconds))
            try:
                with _PA_LOCK:
                    stream = sd.InputStream(device=self._index, samplerate=self._dev_rate, channels=self._dev_channels,
                                            dtype="float32", blocksize=blocksize, latency="high",
                                            callback=self._callback)
                    _stream_opened()
                try:
                    stream.start()
                except Exception:
                    stream.close()
                    _stream_closed()
                    raise
            except Exception as e:  # noqa: BLE001
                self._index = None
                self._set_status(STATUS_DISCONNECTED, f"could not open microphone: {e}")
                raise AudioDeviceError(self.detail) from e
            self._stream = stream
            self._last_chunk_at = time.monotonic()
            self._set_status(STATUS_OK, f"{self.device_name} (listening)")

    def _callback(self, indata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
        if status:
            self.overflows += 1
        mono = indata.mean(axis=1) if indata.ndim == 2 and indata.shape[1] > 1 else indata.reshape(-1)
        chunk = np.array(mono, dtype=np.float32)  # copy: PortAudio reuses the buffer
        if self._resampler is not None:
            chunk = self._resampler.process(chunk)
        with self._cv:
            if len(self._chunks) == self._chunks.maxlen:
                self.dropped += 1
            self._chunks.append(chunk)
            self._last_chunk_at = time.monotonic()
            self._cv.notify()

    def read(self, timeout: float) -> np.ndarray | None:
        with self._cv:
            if not self._chunks and self._stream is not None:
                self._cv.wait(max(0.0, timeout))
            if self._chunks:
                return self._chunks.popleft()
        if self._stream is None:
            return None
        stream = self._stream
        stalled = time.monotonic() - self._last_chunk_at > self.stall_seconds
        inactive = False
        try:
            inactive = not stream.active
        except Exception:  # noqa: BLE001
            inactive = True
        if stalled or inactive:
            self._failed = "microphone stopped delivering audio (unplugged?)"
            self._set_status(STATUS_DISCONNECTED, self._failed)
            self.close(keep_status=True)
            with self._lock:
                self._index = None
            raise AudioDeviceError(self._failed)
        return None

    def flush(self) -> None:
        with self._cv:
            self._chunks.clear()

    def close(self, keep_status: bool = False) -> None:
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:  # noqa: BLE001
                pass
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
            _stream_closed()
            if not keep_status and self.status == STATUS_OK:
                self._set_status(STATUS_OK, f"{self.device_name} (idle)")
        self.flush()
        with self._cv:
            self._cv.notify_all()


class MockInput(InputBackend):
    """Simulated microphone: a quiet synthetic noise floor plus injected audio.

    Injected audio (``inject_audio`` / ``inject_speech``) is delivered as fast
    as it is read; the noise floor is paced in real time like a live mic.
    """

    name = "mock"
    real = False

    def __init__(self, sample_rate: int = 16000, *, chunk_seconds: float = 0.032, noise_level: float = 0.002,
                 fail_on_open: bool = False, seed: int = 1234) -> None:
        super().__init__(sample_rate)
        self.chunk_seconds = chunk_seconds
        self.noise_level = noise_level
        self.fail_on_open = fail_on_open
        self.device_name = "simulated microphone"
        self._rng = np.random.default_rng(seed)
        self._queue: collections.deque[np.ndarray] = collections.deque(maxlen=2000)
        self._open = False
        self._closed_event = threading.Event()
        self.opens = 0
        self._set_status(STATUS_SIMULATED, "simulated microphone (use inject_utterance)")

    @property
    def is_open(self) -> bool:
        return self._open

    def probe(self) -> None:
        if self.fail_on_open:
            self._set_status(STATUS_UNAVAILABLE, "simulated: microphone missing")
            raise AudioDeviceError(self.detail)

    def open(self) -> None:
        self.probe()
        self.opens += 1
        self._open = True
        self._closed_event.clear()

    def close(self) -> None:
        self._open = False
        self._closed_event.set()

    def flush(self) -> None:
        pass  # injected audio represents the future: keep it

    def set_level(self, noise_level: float) -> None:
        self.noise_level = max(0.0, float(noise_level))

    def inject_audio(self, samples: np.ndarray) -> None:
        x = np.asarray(samples, dtype=np.float32)
        step = max(1, int(self.chunk_seconds * self.sample_rate))
        for i in range(0, len(x), step):
            self._queue.append(x[i:i + step].copy())

    def inject_speech(self, seconds: float = 1.0, level: float = 0.12, *, leading_silence: float = 0.1,
                      trailing_silence: float = 1.6) -> None:
        """Speech-like test signal (syllable-modulated voiced tone + breath noise) with silences."""
        sr = self.sample_rate
        n = int(seconds * sr)
        t = np.arange(n) / sr
        f0 = 220 + 30 * np.sin(2 * np.pi * 1.5 * t)
        voiced = np.sin(2 * np.pi * np.cumsum(f0) / sr) + 0.4 * np.sin(4 * np.pi * np.cumsum(f0) / sr)
        syllables = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t) ** 2
        speech = voiced * syllables + 0.1 * self._rng.standard_normal(n)
        speech = dsp.fade(speech.astype(np.float32), sr, 0.02, 0.05)
        speech = speech / (dsp.rms(speech) + 1e-9) * level
        silence = lambda secs: (self._rng.standard_normal(int(secs * sr)) * self.noise_level).astype(np.float32)  # noqa: E731
        self.inject_audio(np.concatenate([silence(leading_silence), speech.astype(np.float32), silence(trailing_silence)]))

    def pending_chunks(self) -> int:
        return len(self._queue)

    def read(self, timeout: float) -> np.ndarray | None:
        if not self._open:
            return None
        if self._queue:
            return self._queue.popleft()
        if timeout < self.chunk_seconds:
            self._closed_event.wait(max(0.0, timeout))
            return None
        if self._closed_event.wait(self.chunk_seconds):
            return None
        n = int(self.chunk_seconds * self.sample_rate)
        return (self._rng.standard_normal(n) * self.noise_level).astype(np.float32)


class NullInput(InputBackend):
    name = "none"
    real = False

    def __init__(self, sample_rate: int = 16000) -> None:
        super().__init__(sample_rate)
        self._set_status(STATUS_DISABLED, "microphone disabled in config (backend: none)")

    def open(self) -> None:
        raise AudioDeviceError(self.detail)


# ================================================================ resolution
@dataclasses.dataclass
class Resolution:
    backend: Any
    degraded: bool = False  # True when we fell back to something less than requested
    note: str = ""


def resolve_output(backend: str, device: int | str | None = None, sample_rate: int = 22050, *,
                   time_scale: float = 1.0) -> Resolution:
    """auto: PortAudio -> SDL mixer -> simulated (logged). Explicit real backends stay
    'unavailable' (and are retried) instead of silently switching to a mock."""
    backend = (backend or "auto").lower()
    if backend == "none":
        return Resolution(NullOutput(sample_rate))
    if backend == "mock":
        return Resolution(MockOutput(sample_rate, time_scale=time_scale))
    order = {"auto": ("sounddevice", "pygame"), "real": ("sounddevice", "pygame"),
             "sounddevice": ("sounddevice",), "pygame": ("pygame",)}.get(backend, ("sounddevice", "pygame"))
    errors: list[str] = []
    for kind in order:
        candidate: OutputBackend = (SoundDeviceOutput(device, sample_rate) if kind == "sounddevice"
                                    else PygameMixerOutput(device, sample_rate))
        try:
            candidate.open()
            return Resolution(candidate)
        except AudioDeviceError as e:
            errors.append(f"{kind}: {e}")
        except Exception as e:  # noqa: BLE001 - never crash on exotic driver errors
            errors.append(f"{kind}: {type(e).__name__}: {e}")
    note = "no usable speaker (" + "; ".join(errors) + ")"
    if backend == "auto":
        mock = MockOutput(sample_rate, time_scale=time_scale)
        mock._set_status(STATUS_SIMULATED, note + " - using simulated output")
        return Resolution(mock, degraded=True, note=note)
    first: OutputBackend = (SoundDeviceOutput(device, sample_rate) if order[0] == "sounddevice"
                            else PygameMixerOutput(device, sample_rate))
    first._set_status(STATUS_UNAVAILABLE, note)
    return Resolution(first, degraded=True, note=note)


def resolve_input(backend: str, device: int | str | None = None, sample_rate: int = 16000,
                  channels: int = 1) -> Resolution:
    """auto: PortAudio mic if present, else a simulated mic (inject_utterance still works)."""
    backend = (backend or "auto").lower()
    if backend == "none":
        return Resolution(NullInput(sample_rate))
    if backend == "mock":
        return Resolution(MockInput(sample_rate))
    real = SoundDeviceInput(device, sample_rate, channels)
    try:
        real.probe()
        return Resolution(real)
    except AudioDeviceError as e:
        note = f"no usable microphone ({e})"
    except Exception as e:  # noqa: BLE001
        note = f"no usable microphone ({type(e).__name__}: {e})"
    if backend == "auto":
        mock = MockInput(sample_rate)
        mock._set_status(STATUS_SIMULATED, note + " - using simulated microphone")
        return Resolution(mock, degraded=True, note=note)
    real._set_status(STATUS_UNAVAILABLE, note)
    return Resolution(real, degraded=True, note=note)
