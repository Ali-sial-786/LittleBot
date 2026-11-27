"""AudioService: speech, sound effects, melodies and listening (half-duplex).

Implements ``core.interfaces.AudioAPI``.

Output
    One output thread plays a bounded queue of items (speech / sound / melody)
    one at a time. ``stop_output()`` drops the queue and interrupts the current
    item within one audio chunk (~50 ms). Speech publishes
    ``audio.speech_started`` -> ``audio.level`` (``{level, shape}``: loudness and
    mouth shape of the 20 ms being heard, sent in step with the speaker; drives the
    face's mouth/eyes) -> ``audio.speech_finished``.
    A line recorded in the ChatGPT voice (``audio.voice_lines``: data/voice_lines, made by
    scripts/record_voice_lines.py) is played instead of text-to-speech when one exists for
    exactly that (normalised) line; everything else about it (volume, mouth, stop) is the same.
    Digital gain = volume (clamped to ``audio.speaker.max_volume``) and is
    re-read every chunk, so volume changes apply immediately.

Input (half-duplex echo prevention)
    The microphone stream is only open while ``listen()`` / ``test_microphone()``
    run. ``listen()`` first waits (bounded) for output to finish, then for
    ``audio.post_speech_mute_seconds``; any audio captured while LittleBot is
    (or just was) making sound is discarded. Recorded audio lives in memory
    only and is dropped after transcription (``audio.store_recordings`` must be
    true to keep WAVs). ``inject_utterance()`` satisfies a pending or the next
    ``listen()`` (simulator / tests).

Sharing the microphone with a background listener (the wake word)
    The USB microphone is usually an exclusive ALSA device: it can't be opened twice.
    A background listener (``attach_background_listener``) reads this service's own input
    backend (``background_input()``) while nothing else needs it, so the stream is then also
    open between listens. Before ``listen()``, ``test_microphone()``, ``suspend()`` (live
    voice) and ``stop()`` the service takes the microphone back: ``listener.release_mic()``
    returns once the listener has closed the stream; ``listener.mic_free()`` hands it back.

Privacy
    Microphone disabled -> ``listen()`` returns None immediately and the input
    stream is closed (also mid-listen). Cloud TTS/STT only while the same gates as
    cloud AI conversation pass: ``ctx.privacy.cloud_enabled`` (not offline),
    ``age_policy().cloud_ai_allowed()`` (any age unless ``ai.min_age_months_for_cloud``
    sets a minimum) and the parent's ``ai.enabled`` opt-in; otherwise / on failure the
    local provider is used. Text for a cloud voice has the child's nickname replaced by
    "friend" unless ``ai.share_child_name`` is on.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextlib
import dataclasses
import datetime as _dt
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from littlebot.audio import devices as dev
from littlebot.audio import dsp
from littlebot.audio import ec_route
from littlebot.audio.devices import (
    InputBackend, MockOutput, NullInput, NullOutput, OutputBackend, PlaybackControl, SoundDeviceInput,
    SoundDeviceOutput,
)
from littlebot.audio.errors import AudioDeviceError
from littlebot.audio.sfx import SAMPLE_RATE as SFX_RATE
from littlebot.audio.sfx import SoundBank, render_melody
from littlebot.audio.stt import STTProvider, STTResult, build_stt
from littlebot.audio.tts import MockTTS, TTSProvider, build_tts, clamp_rate, clean_text, local_tts, scrub_names
from littlebot.audio.vad import EnergyVAD, VADState
from littlebot.audio.voice_lines import VoiceLineStore, default_folder, get_store, line_hash
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus

log = logging.getLogger("littlebot.audio")

LEVEL_HZ = 50.0  # mouth frames per second while speaking (audio.level {level, shape}, sent as the speaker plays)
VISEME_LEAD_S = 0.04  # send each mouth frame this early: the face needs about a frame to show it
VISEME_REPEAT_S = 0.15  # an unchanged mouth frame is re-sent this often (the face treats silence as no data)
MAX_QUEUE = 16  # pending output items
OUTPUT_WAIT_LIMIT = 30.0  # listen() waits at most this long for output to finish
IDLE_RELEASE_SECONDS = 3.0  # close the speaker stream after this much silence
INJECT_TTL_SECONDS = 120.0  # injected utterances expire
MIN_INPUT_GAIN, MAX_INPUT_GAIN = 0.1, 8.0
HANDBACK_WAIT_S = 3.0  # speaker_back(): how long to keep trying to open the speaker after a hand-back
HANDBACK_RETRY_S = 0.1
HANDBACK_CHECK_S = 0.2  # ...by playing this much silence (a sound's first blocks are what fail)

SPEAKER_HINT = ("Plug in a USB speaker (the Raspberry Pi 5 has no 3.5 mm audio jack), check `aplay -l`, "
                "then run `python scripts/check_audio.py --play`.")
MIC_HINT = ("Plug in a USB microphone, check `arecord -l`, then run `python scripts/check_audio.py --record 3`.")

_CAPTURE_FAILED = object()


def _voice_line_store(ctx: Any) -> VoiceLineStore:
    try:
        return get_store(ctx)
    except Exception:  # noqa: BLE001 - a context without extras (some tests): a private store
        return VoiceLineStore(default_folder(ctx.config.data_path))


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _resolve(fut: "concurrent.futures.Future[bool]", value: bool) -> None:
    if not fut.done():
        with contextlib.suppress(concurrent.futures.InvalidStateError):
            fut.set_result(value)


@dataclasses.dataclass(eq=False)
class _Item:
    kind: str  # "speech" | "sound" | "melody" | "check" (speaker_back: silent, no events)
    label: str  # text (speech) or sound name
    samples: np.ndarray
    sample_rate: int
    volume: float = 1.0  # relative to the master volume (never louder)
    wait: float = 0.0  # "check" items: how long to keep trying to open the speaker
    stop: threading.Event = dataclasses.field(default_factory=threading.Event)
    future: "concurrent.futures.Future[bool]" = dataclasses.field(default_factory=concurrent.futures.Future)

    @property
    def seconds(self) -> float:
        return len(self.samples) / float(self.sample_rate)


class _ListenSession:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.wake = asyncio.Event()
        self.cancel = threading.Event()
        self.reason = ""

    def poke(self) -> None:
        with contextlib.suppress(RuntimeError):  # loop closed
            self.loop.call_soon_threadsafe(self.wake.set)

    def cancel_now(self, reason: str) -> None:
        self.reason = reason
        self.cancel.set()
        self.poke()


class AudioService(Service):
    name = "audio"

    def __init__(self, ctx: Any, *, output: OutputBackend | None = None, input: InputBackend | None = None,  # noqa: A002
                 tts: TTSProvider | None = None, stt: STTProvider | None = None,
                 sound_bank: SoundBank | None = None, voice_lines: VoiceLineStore | None = None) -> None:
        super().__init__()  # lifecycle state lives in ``service_status`` / ``health()``
        self.ctx = ctx
        self.cfg = ctx.config.audio
        self._bus = ctx.bus
        self._clock = ctx.clock
        self.reconnect_seconds = 5.0
        dev.set_echo_cancel(getattr(self.cfg, "echo_cancel", "auto"))  # PipeWire's echo-cancelled devices: auto | on | off

        # --- volume / rate / gain (parent settings, clamped) ---
        self._max_volume = max(0.0, min(1.0, float(self.cfg.speaker.max_volume)))
        self._volume = self._clamp_volume(ctx.settings.get("audio.volume", self.cfg.speaker.volume), strict=False)
        self._tts_rate = clamp_rate(ctx.settings.get("audio.tts_rate", self.cfg.tts.rate), self.cfg.tts.rate)
        self._input_gain = self._clamp_gain(ctx.settings.get("audio.input_gain", self.cfg.microphone.input_gain))
        for key, fn in (("audio.volume", self._validate_volume), ("audio.tts_rate", self._validate_rate),
                        ("audio.input_gain", self._validate_gain)):
            with contextlib.suppress(Exception):
                ctx.settings.register_validator(key, fn)

        # --- providers (primary may be cloud; local is the offline fallback) ---
        # The service re-checks this gate before every cloud call (also for injected providers).
        cloud_allowed = self._cloud_speech_allowed
        if tts is None:
            self._tts, self._tts_local = build_tts(self.cfg.tts, cloud_allowed=cloud_allowed, api_key=self._openai_key)
        else:
            self._tts, self._tts_local = tts, (local_tts(self.cfg.tts) if tts.is_cloud else tts)
        if stt is None:
            self._stt, self._stt_local = build_stt(self.cfg.stt, cloud_allowed=cloud_allowed, api_key=self._openai_key,
                                                   sample_rate=self.cfg.microphone.sample_rate)
        else:
            from littlebot.audio.stt import local_stt  # noqa: PLC0415

            self._stt, self._stt_local = stt, (local_stt(self.cfg.stt) if stt.is_cloud else stt)
        self._mock_tts = MockTTS()
        self.last_tts_error = ""
        self.last_stt_error = ""
        self._bank = sound_bank or SoundBank(user_dir=Path(ctx.config.data_path) / "sounds")
        # Lines recorded in the ChatGPT voice (data/voice_lines); each line's voice is logged once.
        self._voice_lines = voice_lines if voice_lines is not None else _voice_line_store(ctx)
        self._voice_noted: set[str] = set()

        # --- devices (resolved lazily / in start(); probing PortAudio can be slow) ---
        self._out: OutputBackend | None = output
        self._inp: InputBackend | None = input
        self._out_injected = output is not None
        self._inp_injected = input is not None
        # stand-in used while a real speaker is unavailable (keeps timing + events, nothing audible)
        self._sim_out = MockOutput(time_scale=self._clock.scale, max_records=20)
        self._devices_ready = False
        self._dev_lock = threading.RLock()
        self._next_out_retry = 0.0
        self._next_in_retry = 0.0
        self._published_status: dict[str, tuple[str, str]] = {}

        # --- output worker ---
        self._cv = threading.Condition()
        self._pending: collections.deque[_Item] = collections.deque()
        self._current: _Item | None = None
        self._worker: threading.Thread | None = None
        self._shutdown = False
        self._stop_counter = 0
        self._last_output_end = 0.0
        self._released = True
        self._speaking = False

        # --- input / listening ---
        self._inject_lock = threading.Lock()
        self._injected: collections.deque[tuple[float, str]] = collections.deque(maxlen=5)
        self._listen_session: _ListenSession | None = None
        self._listening = False
        self.mic_level = 0.0
        # Who has borrowed the speaker + microphone (live voice); None = this service owns them.
        self._suspended: str | None = None
        # Background listener (wake word) sharing the microphone, and why it can't have it now
        # (reason -> count). Listener calls happen under _claim_lock, so they arrive in order.
        self._bg_listener: Any = None
        self._mic_claims: collections.Counter[str] = collections.Counter()
        self._claim_lock = threading.Lock()

        self._subs: list[Subscription] = []
        self._subscribe()

    def _subscribe(self) -> None:
        if not self._subs:
            self._subs = [
                self._bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
                self._bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
                self._bus.subscribe(Topics.STOP_ALL, self._on_stop_all),
            ]

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._shutdown = False
        self._unclaim_mic("stopping")  # restarted after a stop()
        self._subscribe()
        self.set_status(ServiceStatus.STARTING)
        await asyncio.to_thread(self._ensure_devices)
        self._ensure_worker()
        self._update_health()

    async def stop(self) -> None:
        self.stop_output()
        self._cancel_listen("stopping")
        await asyncio.to_thread(self._claim_mic, "stopping")  # never released: the service is going away
        with self._cv:
            self._shutdown = True
            self._cv.notify_all()
        worker = self._worker
        if worker is not None and worker.is_alive():
            await asyncio.to_thread(worker.join, 3.0)
        self._worker = None
        for backend in (self._out, self._inp):
            if backend is not None:
                with contextlib.suppress(Exception):
                    backend.close()
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        self.set_status(ServiceStatus.STOPPED)

    def _ensure_devices(self) -> None:
        with self._dev_lock:
            if self._devices_ready:
                return
            spk, mic = self.cfg.speaker, self.cfg.microphone
            if self._out is None:
                res = dev.resolve_output(spk.backend, spk.device, spk.sample_rate, time_scale=self._clock.scale)
                self._out = res.backend
                if res.note:
                    log.warning("speaker: %s", res.note)
            else:
                try:
                    self._out.open()
                except AudioDeviceError as e:
                    log.warning("speaker unavailable: %s", e)
            if self._inp is None:
                res = dev.resolve_input(mic.backend, mic.device, mic.sample_rate, mic.channels)
                self._inp = res.backend
                if res.note:
                    log.warning("microphone: %s", res.note)
            else:
                try:
                    self._inp.probe()
                except AudioDeviceError as e:
                    log.warning("microphone unavailable: %s", e)
            self._devices_ready = True
        log.info("audio: speaker=%s (%s), microphone=%s (%s), tts=%s, stt=%s", self._out.name, self._out.status,
                 self._inp.name, self._inp.status, self._tts.name, self._stt.name)
        self._log_echo_cancel()
        self._publish_device("speaker")
        self._publish_device("microphone")

    def _log_echo_cancel(self) -> None:
        """Which way the sound goes: PipeWire's echo-cancelled devices (shared, non-exclusive) or the USB ones."""
        spk = getattr(self._out, "device_name", None)
        mic = getattr(self._inp, "device_name", None)
        if dev.is_echo_cancel_device(spk, "output") and dev.is_echo_cancel_device(mic, "input"):
            log.info("audio: echo cancelling on (PipeWire: %s / %s)", mic, spk)
            ec_route.ensure_soon()  # and it plays on the USB speaker (WirePlumber can link it elsewhere at boot)
        elif isinstance(self._out, SoundDeviceOutput) or isinstance(self._inp, SoundDeviceInput):
            log.info("audio: echo cancelling off (%s)", dev.echo_cancel_note())

    @property
    def echo_cancelling(self) -> bool:
        """Both devices are PipeWire's echo-cancelled ones."""
        return dev.is_echo_cancel_device(getattr(self._out, "device_name", None), "output") and \
            dev.is_echo_cancel_device(getattr(self._inp, "device_name", None), "input")

    def _ensure_ready(self) -> None:
        if not self._devices_ready:
            self._ensure_devices()
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self._cv:
            if self._shutdown or (self._worker is not None and self._worker.is_alive()):
                return
            self._worker = threading.Thread(target=self._worker_main, name="littlebot-audio-out", daemon=True)
            self._worker.start()

    def _update_health(self) -> None:
        out, inp = self._out, self._inp
        problems = []
        if out is None or out.status != dev.STATUS_OK:
            problems.append(f"speaker {out.status if out else 'missing'}")
        if inp is None or inp.status not in (dev.STATUS_OK, dev.STATUS_DISABLED):
            problems.append(f"microphone {inp.status if inp else 'missing'}")
        if problems:
            self.set_status(ServiceStatus.DEGRADED, ", ".join(problems))
        else:
            self.set_status(ServiceStatus.RUNNING, "")

    # =================================================================== events
    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self._bus.publish(topic, data, source="audio")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _device_info(self, which: str) -> dict[str, Any]:
        backend = self._out if which == "speaker" else self._inp
        if backend is None:
            return {"backend": None, "device": None, "status": "starting", "detail": "not initialised yet"}
        info = backend.info()
        if which == "microphone" and not self._mic_allowed():
            info["status"] = dev.STATUS_DISABLED
            info["detail"] = "microphone turned off in the privacy settings"
        return info

    def _publish_device(self, which: str, *, force: bool = False) -> None:
        info = self._device_info(which)
        key = (str(info["status"]), str(info.get("backend")))
        if not force and self._published_status.get(which) == key:
            return
        self._published_status[which] = key
        self._publish(Topics.AUDIO_DEVICE_STATUS, {"device": which, "status": info["status"], "detail": info["detail"],
                                                  "backend": info["backend"], "name": info["device"]})
        if self._devices_ready:
            self._update_health()

    def _on_setting(self, event: Event) -> None:
        key, value = event.get("key"), event.get("value")
        try:
            if key == "audio.volume":
                self._volume = self._clamp_volume(value, strict=False)
            elif key == "audio.tts_rate":
                self._tts_rate = clamp_rate(value, self._tts_rate)
            elif key == "audio.input_gain":
                self._input_gain = self._clamp_gain(value)
        except Exception:  # noqa: BLE001
            log.debug("ignoring bad audio setting %s=%r", key, value)

    def _on_privacy(self, event: Event) -> None:
        if event.get("microphone_enabled", True) is False:
            self._cancel_listen("microphone disabled")
            self._close_input()
        self._publish_device("microphone")

    def _on_stop_all(self, _event: Event) -> None:
        self._cancel_listen("stop all")

    # =================================================================== volume
    def _clamp_volume(self, value: Any, *, strict: bool = True) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"volume must be a number 0..1, got {value!r}") from None
            v = float(self.cfg.speaker.volume)
        if not math.isfinite(v):
            if strict:
                raise ValueError("volume must be a finite number")
            v = float(self.cfg.speaker.volume)
        return round(max(0.0, min(self._max_volume, v)), 4)

    def _validate_volume(self, value: Any) -> float:
        return self._clamp_volume(value, strict=True)

    @staticmethod
    def _validate_rate(value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("speech rate must be a number") from None
        if not math.isfinite(v):
            raise ValueError("speech rate must be a finite number")
        return clamp_rate(v)

    @staticmethod
    def _clamp_gain(value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return 1.0
        return max(MIN_INPUT_GAIN, min(MAX_INPUT_GAIN, v)) if math.isfinite(v) else 1.0

    def _validate_gain(self, value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("input gain must be a number") from None
        if not math.isfinite(v):
            raise ValueError("input gain must be a finite number")
        return self._clamp_gain(v)

    def set_volume(self, volume: float) -> float:
        v = self._clamp_volume(volume, strict=True)
        self._volume = v
        try:
            self.ctx.settings.set("audio.volume", v, source="audio")
        except Exception:  # noqa: BLE001 - persisting must never break the call
            log.warning("could not persist audio.volume", exc_info=True)
        return v

    def _gain_for(self, item: _Item) -> float:
        return max(0.0, min(self._max_volume, self._volume * item.volume))

    # =================================================================== output
    def _output_busy(self) -> bool:
        with self._cv:
            return self._current is not None or bool(self._pending)

    def _mic_muted(self) -> bool:
        """Half-duplex: ignore the microphone while (and shortly after) making sound."""
        if not self.cfg.half_duplex:
            return False
        if self._output_busy():
            return True
        mute = self._clock.scaled(self.cfg.post_speech_mute_seconds)
        return self._last_output_end > 0 and time.monotonic() - self._last_output_end < mute

    def _post_mute_remaining(self) -> float:
        if self._last_output_end <= 0:
            return 0.0
        return self.cfg.post_speech_mute_seconds - (time.monotonic() - self._last_output_end)

    async def _enqueue(self, item: _Item, wait: bool) -> bool:
        self._ensure_worker()
        with self._cv:
            if self._shutdown or self._suspended:
                return False
            if len(self._pending) >= MAX_QUEUE:
                log.warning("audio output queue full; dropping %s %r", item.kind, item.label[:40])
                return False
            self._pending.append(item)
            self._cv.notify()
        if not wait:
            return True
        try:
            return bool(await asyncio.wrap_future(item.future))
        except asyncio.CancelledError:
            item.stop.set()  # the caller went away: do not keep talking
            raise

    # ================================================================ hand-over
    @property
    def suspended(self) -> str | None:
        return self._suspended

    async def suspend(self, owner: str, wait: float = 1.5) -> None:
        """Lend the speaker and microphone to ``owner`` (live voice) until ``resume(owner)``.

        Stops playback, cancels a listen and closes both device streams, so another stream can
        open the same (often exclusive, hw:) ALSA device. Meanwhile sounds and speech are dropped
        (they return False) and listen() returns None. A background listener (wake word) gives
        the microphone back first.
        """
        t0 = time.monotonic()
        was_lent = self._suspended is not None
        self._suspended = owner
        self.stop_output()
        self._cancel_listen(f"suspended for {owner}")
        if not was_lent:
            await self._claim_mic_async("lent")
        claimed = time.monotonic()
        steps = await asyncio.to_thread(self._release_devices, wait)
        log.info("audio: speaker and microphone lent to %s (%d ms: wake word %d, playback %d, speaker %d, mic %d)",
                 owner, _ms(time.monotonic() - t0), _ms(claimed - t0), *steps)

    def _release_devices(self, wait: float) -> tuple[int, int, int]:
        """Close the devices once the sound playing has stopped: (ms waited for it, closing the speaker, the mic)."""
        t0 = time.monotonic()
        deadline = t0 + max(0.0, wait)
        while time.monotonic() < deadline:
            with self._cv:
                if self._current is None:
                    break
            time.sleep(0.02)
        t1 = time.monotonic()
        out = self._out
        if out is not None:
            with contextlib.suppress(Exception):
                out.release()
            self._released = True
        t2 = time.monotonic()
        self._close_input()
        return _ms(t1 - t0), _ms(t2 - t1), _ms(time.monotonic() - t2)

    def resume(self, owner: str) -> None:
        if self._suspended == owner:
            self._suspended = None
            log.info("audio: speaker and microphone back from %s", owner)
            self._unclaim_mic("lent")

    # ======================================================= background listener
    def attach_background_listener(self, listener: Any) -> None:
        """Share the microphone with ``listener`` (the wake word) while nothing else needs it.

        ``listener.release_mic(reason)`` must stop reading and close the stream before it returns
        (it is called from a worker thread; bounded wait); ``listener.mic_free()`` says the
        microphone may be read again (``background_input()``)."""
        with self._claim_lock:
            self._bg_listener = listener
            if self._mic_claims:
                listener.release_mic(", ".join(sorted(self._mic_claims)))

    def detach_background_listener(self, listener: Any) -> None:
        with self._claim_lock:
            if self._bg_listener is listener:
                self._bg_listener = None

    def background_input(self) -> InputBackend | None:
        """The input backend for the background listener, or None while the microphone is needed
        elsewhere (listening, a test, lent to live voice, stopping) or turned off. Lock-free: the
        listener calls it under its own lock."""
        if self._mic_claims or self._suspended or self._listening or self._shutdown:
            return None
        inp = self._inp
        if not self._devices_ready or inp is None or isinstance(inp, NullInput) or not self._mic_allowed():
            return None
        return inp

    def mic_muted(self) -> bool:
        """True while (and just after) LittleBot makes sound: the microphone hears itself (half-duplex)."""
        return self._mic_muted()

    def output_note(self) -> str:
        """What keeps the microphone muted, for the log: what is playing or queued."""
        with self._cv:
            cur, queued = self._current, len(self._pending)
        if cur is not None:
            return f"playing {cur.kind} {cur.label[:40]!r}" + (f", {queued} more queued" if queued else "")
        return f"{queued} sound(s) queued" if queued else "just finished playing"

    @property
    def input_gain(self) -> float:
        return self._input_gain

    @property
    def mic_claims(self) -> list[str]:
        """Why the microphone isn't free for the background listener right now ([] = it is)."""
        return sorted(self._mic_claims)

    def _claim_mic(self, reason: str) -> None:
        """Take the microphone from the background listener until ``_unclaim_mic(reason)``.

        Blocks until the listener has closed its stream: call it from a worker thread."""
        with self._claim_lock:
            self._mic_claims[reason] += 1
            listener = self._bg_listener
            if listener is not None:
                try:
                    listener.release_mic(reason)
                except Exception:  # noqa: BLE001 - never block listening on the wake word
                    log.exception("background listener failed to release the microphone")

    async def _claim_mic_async(self, reason: str) -> None:
        """``_claim_mic`` in a worker thread. If the caller is cancelled meanwhile, the claim still
        completes in its thread and is then undone (the caller never saw it succeed)."""
        fut = asyncio.ensure_future(asyncio.to_thread(self._claim_mic, reason))
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            fut.add_done_callback(lambda _f: self._unclaim_mic(reason))
            raise

    def _unclaim_mic(self, reason: str) -> None:
        with self._claim_lock:
            if self._mic_claims.get(reason, 0) <= 0:
                return
            self._mic_claims[reason] -= 1
            if self._mic_claims[reason] <= 0:
                del self._mic_claims[reason]
            listener = self._bg_listener
            if listener is not None and not self._mic_claims:
                try:
                    listener.mic_free()
                except Exception:  # noqa: BLE001
                    log.exception("background listener failed to resume")

    def stop_output(self) -> None:
        with self._cv:
            self._stop_counter += 1
            dropped = list(self._pending)
            self._pending.clear()
            current = self._current
        for it in dropped:
            it.stop.set()
            _resolve(it.future, False)
        if current is not None:
            current.stop.set()

    def _worker_main(self) -> None:
        idle_since = time.monotonic()
        while True:
            with self._cv:
                if not self._pending and not self._shutdown:
                    self._cv.wait(timeout=1.0)
                if self._shutdown:
                    leftovers = list(self._pending)
                    self._pending.clear()
                    break
                item = self._pending.popleft() if self._pending else None
                self._current = item
            if item is None:
                self._idle_tick(idle_since)
                continue
            ok = False
            try:
                ok = self._play_item(item)
            except Exception:  # noqa: BLE001 - the output thread must survive anything
                log.exception("audio playback failed")
            finally:
                with self._cv:
                    self._current = None
                    self._last_output_end = time.monotonic()
                idle_since = time.monotonic()
                self._released = False
                _resolve(item.future, ok)
        for it in leftovers:
            _resolve(it.future, False)

    def _idle_tick(self, idle_since: float) -> None:
        out = self._out
        if out is None:
            return
        now = time.monotonic()
        if not self._released and now - idle_since > IDLE_RELEASE_SECONDS:
            self._released = True
            with contextlib.suppress(Exception):
                out.release()
        if out.status in (dev.STATUS_DISCONNECTED, dev.STATUS_UNAVAILABLE) and now >= self._next_out_retry:
            self._try_reconnect_output(out)

    def _try_reconnect_output(self, out: OutputBackend) -> bool:
        if self._suspended:
            # Lent (live voice has it open): probing the same USB device under its stream can stop that
            # stream. Tried again once it is given back.
            return False
        self._next_out_retry = time.monotonic() + self.reconnect_seconds
        if isinstance(out, SoundDeviceOutput):
            dev.refresh_portaudio()
        try:
            out.open()
        except AudioDeviceError as e:
            log.debug("speaker still unavailable: %s", e)
            return False
        except Exception:  # noqa: BLE001
            log.debug("speaker reconnect failed", exc_info=True)
            return False
        log.info("speaker available again: %s", out.detail)
        self._publish_device("speaker")
        return True

    def _output_ready(self, out: OutputBackend) -> bool:
        if out.status in (dev.STATUS_DISCONNECTED, dev.STATUS_UNAVAILABLE):
            if time.monotonic() < self._next_out_retry or not self._try_reconnect_output(out):
                return False
        return True

    def _play_item(self, item: _Item) -> bool:
        if item.stop.is_set() or self._suspended:
            return False
        out = self._out
        if out is None or isinstance(out, NullOutput):
            return False
        if item.kind == "check":
            return self._check_output(out, item)
        audible = self._output_ready(out)
        if not audible:
            # Speaker missing / unplugged: keep the timing and events (face animation, activity
            # pacing) on a simulated speaker, but report False because nothing was heard.
            out = self._sim_out
        speech = item.kind == "speech"
        # mouth level + shape for every 20 ms of the speech, sent in step with what the speaker plays
        levels, shapes = dsp.viseme_track(item.samples, item.sample_rate, LEVEL_HZ) if speech else ([], ())
        last_idx = -1
        last_mouth: tuple[float, str] | None = None
        last_sent = 0.0

        def progress(seconds: float) -> None:
            nonlocal last_idx, last_mouth, last_sent
            if not len(levels):
                return
            idx = min(len(levels) - 1, int((seconds + VISEME_LEAD_S) * LEVEL_HZ + 1e-6))
            if idx == last_idx:
                return
            last_idx = idx
            mouth = (round(float(levels[idx]), 3), shapes[idx])
            if mouth != last_mouth or seconds - last_sent >= VISEME_REPEAT_S:
                last_mouth, last_sent = mouth, seconds
                self._publish(Topics.AUDIO_VISEME, {"level": mouth[0], "shape": mouth[1]})

        ctrl = PlaybackControl(item.stop, gain=lambda: self._gain_for(item), progress=progress,
                               label=f"{item.kind}:{item.label}")
        if speech:
            self._speaking = True
            self._publish(Topics.AUDIO_SPEECH_STARTED, {"text": item.label, "duration_estimate": round(item.seconds, 2)})
        else:
            self._publish(Topics.AUDIO_SOUND_STARTED, {"name": item.label})
        completed = False
        try:
            completed = bool(out.play(item.samples, item.sample_rate, ctrl))
        except AudioDeviceError as e:
            self._on_output_failure(out, e)
        except Exception:  # noqa: BLE001
            log.exception("speaker backend %s crashed", out.name)
        finally:
            if speech:
                self._publish(Topics.AUDIO_VISEME, {"level": 0.0, "shape": "closed"})
                self._speaking = False
                self._publish(Topics.AUDIO_SPEECH_FINISHED, {"text": item.label, "interrupted": not completed})
            else:
                self._publish(Topics.AUDIO_SOUND_FINISHED, {"name": item.label, "interrupted": not completed})
        return completed and audible

    def _check_output(self, out: OutputBackend, item: _Item) -> bool:
        """speaker_back(): open the speaker and play a moment of silence until that works (output thread).

        No events, nothing audible. Afterwards the device is let go again: what was handed over (a
        video in the browser, say) may need it, and the next sound opens it again at once."""
        if not out.real:
            return True  # a simulated speaker has nothing to wait for
        deadline = time.monotonic() + max(0.0, item.wait)
        tries, error = 0, ""
        while not item.stop.is_set() and not self._suspended:
            tries += 1
            try:
                if out.status != dev.STATUS_OK:
                    out.open()
                if out.play(item.samples, item.sample_rate, PlaybackControl(item.stop, gain=lambda: 0.0,
                                                                            label="speaker check")):
                    if tries > 1:
                        log.info("speaker back after %d tries (it was still busy: %s)", tries, error)
                        self._publish_device("speaker")
                    with contextlib.suppress(Exception):
                        out.release()
                    return True
            except Exception as e:  # noqa: BLE001 - AudioDeviceError, PortAudio errors: try again shortly
                error = str(e)[:120]
            if time.monotonic() + HANDBACK_RETRY_S > deadline:
                break
            item.stop.wait(HANDBACK_RETRY_S)
        if error:
            log.warning("speaker still not back after %.1f s (%s)", item.wait, error)
            self._on_output_failure(out, AudioDeviceError(error))
        return False

    async def speaker_back(self, timeout: float = HANDBACK_WAIT_S) -> bool:
        """After a hand-back (``resume``): return once the speaker really plays again.

        The borrower (live voice) has only just closed its own stream on the same, often exclusive
        (ALSA ``hw:``), USB device. A sound started at that moment can fail part-way: it is cut off,
        and the speaker is then taken for unplugged, so everything for ``reconnect_seconds`` plays on
        the simulated speaker (silence). So whoever takes over after a hand-back (an activity's first
        line or sound) waits for this: it opens the speaker and plays a moment of silence, retrying
        until that works or ``timeout`` passes, and lets the device go again. True when the speaker
        plays (or there is no real speaker to wait for); False while it is still lent or never came back.
        """
        if self._suspended:
            return False
        self._ensure_ready()
        if isinstance(self._out, NullOutput):
            return True
        silence = np.zeros(int(HANDBACK_CHECK_S * SFX_RATE), dtype=np.float32)
        return await self._enqueue(_Item("check", "speaker check", silence, SFX_RATE, volume=0.0, wait=timeout),
                                   wait=True)

    def _on_output_failure(self, out: OutputBackend, e: Exception) -> None:
        log.warning("speaker failed during playback (%s); will retry in %.0fs", e, self.reconnect_seconds)
        if out.status not in (dev.STATUS_DISCONNECTED, dev.STATUS_UNAVAILABLE):
            out._set_status(dev.STATUS_DISCONNECTED, str(e))  # noqa: SLF001
        self._next_out_retry = time.monotonic() + self.reconnect_seconds
        self._publish_device("speaker")

    # ------------------------------------------------------------------ speech
    def _openai_key(self) -> str | None:
        key = getattr(self.ctx.config, "openai_api_key", None)
        try:
            return key.get_secret_value() if key is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _cloud_block_reason(self) -> str:
        """Why cloud speech (TTS/STT) may not be used right now; "" when it may.

        Same gates as the cloud AI conversation: the privacy cloud switch (never offline), the
        child's age if ``ai.min_age_months_for_cloud`` sets a minimum (by default any age: the
        parent decides) and the parent's ``ai.enabled`` opt-in."""
        ctx = self.ctx
        try:
            if getattr(ctx.config, "is_offline", False):
                return "offline mode"
            if not bool(ctx.privacy.cloud_enabled):
                return "cloud features are turned off (privacy)"
            policy = ctx.age_policy()
            if not policy.cloud_ai_allowed():
                return ("the child's age is not set" if policy.age_months is None
                        else f"child under {policy.min_age_months_for_cloud_ai} months")
            if not bool(ctx.settings.get("ai.enabled", False)):
                return "cloud AI is turned off (ai.enabled)"
        except Exception:  # noqa: BLE001 - fail closed
            return "cloud check failed"
        return ""

    def _cloud_speech_allowed(self) -> bool:
        return self._cloud_block_reason() == ""

    def _cloud_text(self, text: str) -> str:
        """Text as sent to a cloud voice: the child's nickname only with ``ai.share_child_name``."""
        try:
            if bool(self.ctx.settings.get("ai.share_child_name", self.ctx.config.ai.share_child_name)):
                return text
            profile = self.ctx.settings.get("profile") or {}
            nickname = str(profile.get("nickname") or "") if isinstance(profile, dict) else ""
        except Exception:  # noqa: BLE001
            return text
        return scrub_names(text, [nickname]) if nickname.strip() else text

    def _active_tts(self) -> TTSProvider:
        if self._tts.is_cloud and not (self._cloud_speech_allowed() and self._tts.available()):
            return self._tts_local
        return self._tts

    def _synthesize(self, text: str, rate: float) -> tuple[np.ndarray, int, str]:
        """(samples, sample rate, name of the voice that spoke) from the first text-to-speech that works."""
        chain: list[TTSProvider] = []
        for p in (self._active_tts(), self._tts_local, self._mock_tts):
            if p not in chain:
                chain.append(p)
        last: Exception | None = None
        for provider in chain:
            if provider.is_cloud and not self._cloud_speech_allowed():
                continue  # never a cloud voice without every gate (age, ai.enabled, privacy)
            try:
                spoken = self._cloud_text(text) if provider.is_cloud else text
                samples, sr = provider.synthesize(spoken, rate)
                if len(samples) == 0:
                    raise ValueError("no audio produced")
                return samples, sr, provider.name
            except Exception as e:  # noqa: BLE001 - TTSError, CloudNotAllowed, crashes: try the next voice
                last = e
                self.last_tts_error = f"{provider.name}: {e}"
                log.warning("%s text-to-speech failed (%s); trying a fallback voice", provider.name, e)
        raise RuntimeError(f"all text-to-speech providers failed: {last}")

    async def say(self, text: str, *, rate: float | None = None) -> bool:
        clean = clean_text(text)
        if not clean:
            return True
        self._ensure_ready()
        if isinstance(self._out, NullOutput):
            return False
        marker = self._stop_counter
        r = clamp_rate(rate if rate is not None else self._tts_rate, self._tts_rate)
        voice = await asyncio.to_thread(self._recorded_line, clean)  # the ChatGPT voice, if this line was recorded
        if voice is None:
            try:
                voice = await asyncio.to_thread(self._synthesize, clean, r)
            except Exception as e:  # noqa: BLE001
                log.warning("could not speak %r: %s", clean[:40], e)
                return False
        samples, sr, used = voice
        self._note_voice(clean, used)
        if self._stop_counter != marker:
            return False  # stop_output() was pressed while the voice was being prepared
        return await self._enqueue(_Item("speech", clean, samples, sr), wait=True)

    async def play_speech(self, samples: np.ndarray, sample_rate: int, text: str = "", *, wait: bool = True) -> bool:
        """Speak audio that is already made (the assistant's cloud voice, littlebot/assistant): as ``say``, with the
        same events, mouth, volume and stop, but no text-to-speech here."""
        x = dsp.as_mono_float(np.asarray(samples, dtype=np.float32))
        if not len(x):
            return True
        self._ensure_ready()
        if isinstance(self._out, NullOutput):
            return False
        return await self._enqueue(_Item("speech", clean_text(text) or "speech", x, int(sample_rate)), wait=wait)

    def sound_samples(self, name: str) -> tuple[np.ndarray, int] | None:
        """A sound effect's samples (``SOUND_NAMES``), for a stream that isn't this service's (the assistant's call
        audio plays its game sounds itself). None: unknown."""
        try:
            return self._bank.cached(name) or self._bank.get(name)
        except Exception:  # noqa: BLE001
            return None

    def _recorded_line(self, text: str) -> tuple[np.ndarray, int, str] | None:
        """The recording of ``text`` in the ChatGPT voice (data/voice_lines), padded like spoken text."""
        hit = self._voice_lines.get(text) if self._voice_lines is not None else None
        if hit is None:
            return None
        samples, sr, entry = hit
        voice = str(entry.get("voice") or "ChatGPT")
        return dsp.pad(dsp.as_mono_float(samples), sr, before=0.02, after=0.08), sr, f"recorded:{voice}"

    def _note_voice(self, text: str, used: str) -> None:
        """Log once per line which voice says it: the recorded ChatGPT voice or text-to-speech."""
        key = line_hash(text)
        if key in self._voice_noted:
            return
        if len(self._voice_noted) >= 4096:
            self._voice_noted.clear()
        self._voice_noted.add(key)
        if used.startswith("recorded:"):
            how = f"the recorded ChatGPT voice ({used.split(':', 1)[1]})"
        else:
            how = f"text-to-speech ({used}; not recorded)"
        log.info("speech in %s: %r [%s]", how, self._loggable(text), key)

    def _loggable(self, text: str) -> str:
        """A line as logged: the child's nickname replaced, at most 60 characters."""
        try:
            profile = self.ctx.settings.get("profile") or {}
            nickname = str(profile.get("nickname") or "") if isinstance(profile, dict) else ""
        except Exception:  # noqa: BLE001
            nickname = ""
        out = scrub_names(text, [nickname], "<child>") if nickname.strip() else text
        return out if len(out) <= 60 else out[:57] + "..."

    async def play_sound(self, name: str, *, wait: bool = True, volume: float | None = None) -> bool:
        if not isinstance(name, str) or not name.strip():
            return False
        self._ensure_ready()
        if isinstance(self._out, NullOutput):
            return False
        hit = self._bank.cached(name) or await asyncio.to_thread(self._bank.get, name)
        if hit is None:
            log.warning("unknown sound %r", name)
            return False
        rel = 1.0
        if volume is not None:
            try:
                rel = float(volume)
            except (TypeError, ValueError):
                rel = 1.0
            rel = max(0.0, min(1.0, rel)) if math.isfinite(rel) else 1.0
        samples, sr = hit
        return await self._enqueue(_Item("sound", name, samples, sr, volume=rel), wait=wait)

    async def play_melody(self, notes: list[tuple[str, float]], *, tempo_bpm: float = 100, wait: bool = True) -> bool:
        self._ensure_ready()
        if isinstance(self._out, NullOutput):
            return False
        try:
            samples = await asyncio.to_thread(render_melody, list(notes or []), tempo_bpm)
        except (ValueError, TypeError) as e:
            log.warning("invalid melody: %s", e)
            return False
        return await self._enqueue(_Item("melody", "melody", samples, SFX_RATE), wait=wait)

    # =================================================================== input
    def _mic_allowed(self) -> bool:
        try:
            return bool(self.ctx.privacy.microphone_enabled)
        except Exception:  # noqa: BLE001
            return False

    def _close_input(self) -> None:
        inp = self._inp
        if inp is not None:
            with contextlib.suppress(Exception):
                inp.close()

    def _cancel_listen(self, reason: str) -> None:
        with self._inject_lock:
            session = self._listen_session
        if session is not None:
            session.cancel_now(reason)

    def inject_utterance(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        if not self._mic_allowed():
            log.info("ignoring simulated utterance: microphone is disabled")
            return
        with self._inject_lock:
            self._injected.append((time.monotonic(), text[:500]))
            session = self._listen_session
        if session is not None:
            session.poke()

    def _pop_injected(self) -> str | None:
        now = time.monotonic()
        with self._inject_lock:
            while self._injected:
                ts, text = self._injected.popleft()
                if now - ts <= INJECT_TTL_SECONDS:
                    return text
        return None

    async def _wait_output_idle(self, session: _ListenSession | None, limit: float = OUTPUT_WAIT_LIMIT) -> bool:
        deadline = time.monotonic() + limit
        while self._output_busy():
            if session is not None and session.cancel.is_set():
                return False
            if time.monotonic() > deadline:
                log.warning("output still busy after %.0fs; not listening", limit)
                return False
            await asyncio.sleep(0.02)
        return True

    async def listen(self, *, timeout: float | None = None) -> str | None:
        if not self._mic_allowed():
            self._close_input()
            return None
        if self._suspended:
            return None
        loop = asyncio.get_running_loop()
        with self._inject_lock:
            if self._listen_session is not None:
                log.warning("listen() already in progress; ignoring a concurrent call")
                return None
            session = _ListenSession(loop)
            self._listen_session = session
        started = claimed = False
        result: tuple[str, float, str] | None = None
        try:
            self._ensure_ready()
            wait_s = self.cfg.microphone.max_record_seconds if timeout is None else max(0.0, float(timeout))
            if self.cfg.half_duplex:
                if not await self._wait_output_idle(session):
                    return None
                remaining = self._post_mute_remaining()
                if remaining > 0:
                    await self._clock.sleep(remaining)
            if session.cancel.is_set() or not self._mic_allowed():
                return None
            self._listening = True
            started = True
            await self._claim_mic_async("listening")  # the wake word closes its stream first
            claimed = True
            self._publish(Topics.AUDIO_LISTENING_STARTED, {})
            result = await self._listen_once(session, wait_s)
        finally:
            session.cancel.set()
            with self._inject_lock:
                if self._listen_session is session:
                    self._listen_session = None
            if started:
                self._listening = False
                self._publish(Topics.AUDIO_LISTENING_STOPPED, {})
            if claimed:
                self._unclaim_mic("listening")
        if result is None:
            return None
        text, confidence, source = result
        self._publish(Topics.AUDIO_UTTERANCE, {"text": text, "confidence": round(float(confidence), 3), "source": source})
        return text

    async def _listen_once(self, session: _ListenSession, timeout: float) -> tuple[str, float, str] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        cap_task: asyncio.Future[Any] | None = None
        if self._inp is not None and not isinstance(self._inp, NullInput):
            cap_task = asyncio.ensure_future(asyncio.to_thread(self._capture_utterance, session, timeout))
        wake_task: asyncio.Future[Any] | None = None
        try:
            while True:
                session.wake.clear()
                if session.cancel.is_set() or not self._mic_allowed():
                    return None
                text = self._pop_injected()
                if text:
                    return text, 1.0, "simulated"
                if cap_task is not None and cap_task.done():
                    try:
                        audio = cap_task.result()
                    except Exception:  # noqa: BLE001
                        log.exception("microphone capture crashed")
                        audio = _CAPTURE_FAILED
                    cap_task = None
                    if audio is _CAPTURE_FAILED:
                        continue  # mic broken: keep waiting for simulated input until the deadline
                    if audio is None or session.cancel.is_set():
                        return None
                    res = await asyncio.to_thread(self._transcribe, audio, self._inp.sample_rate)
                    del audio  # never kept
                    if res is None or not res.text.strip() or session.cancel.is_set():
                        return None
                    return res.text.strip(), res.confidence, "stt"
                if wake_task is None or wake_task.done():
                    wake_task = asyncio.ensure_future(session.wake.wait())
                waiters: set[asyncio.Future[Any]] = {wake_task}
                if cap_task is not None:
                    waiters.add(cap_task)
                    wait_for: float | None = None
                else:
                    wait_for = deadline - loop.time()
                    if wait_for <= 0:
                        return None
                await asyncio.wait(waiters, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)
        finally:
            session.cancel.set()
            if wake_task is not None:
                wake_task.cancel()
            if cap_task is not None and not cap_task.done():
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(cap_task), 2.0)

    def _capture_utterance(self, session: _ListenSession, timeout: float) -> Any:
        """Runs in a worker thread. Returns audio (np.ndarray), None (nothing heard) or _CAPTURE_FAILED."""
        inp = self._inp
        if inp is None:
            return _CAPTURE_FAILED
        mic = self.cfg.microphone
        if inp.status in (dev.STATUS_DISCONNECTED, dev.STATUS_UNAVAILABLE) and isinstance(inp, SoundDeviceInput):
            if time.monotonic() < self._next_in_retry:
                return _CAPTURE_FAILED
            self._next_in_retry = time.monotonic() + self.reconnect_seconds
            dev.refresh_portaudio()
        try:
            inp.open()
        except AudioDeviceError as e:
            log.warning("microphone unavailable: %s", e)
            self._publish_device("microphone")
            return _CAPTURE_FAILED
        self._publish_device("microphone")
        vad = EnergyVAD(inp.sample_rate, threshold=mic.vad_threshold, silence_seconds=mic.silence_seconds,
                        max_record_seconds=mic.max_record_seconds)
        start_deadline = time.monotonic() + timeout
        hard_deadline = start_deadline + mic.max_record_seconds + 2.0
        try:
            while not session.cancel.is_set():
                if not self._mic_allowed():
                    return None
                now = time.monotonic()
                if not vad.started and now > start_deadline:
                    return None
                if now > hard_deadline:
                    break
                try:
                    chunk = inp.read(0.1)
                except AudioDeviceError as e:
                    log.warning("microphone failed while listening: %s", e)
                    self._publish_device("microphone")
                    return _CAPTURE_FAILED
                if chunk is None or not len(chunk):
                    continue
                if self._mic_muted():
                    vad.reset()  # half-duplex: never treat our own voice / sounds as input
                    continue
                if self._input_gain != 1.0:
                    chunk = np.clip(chunk * self._input_gain, -1.0, 1.0)
                self.mic_level = dsp.rms(chunk)
                if vad.process(chunk) == VADState.DONE:
                    audio = vad.audio()
                    vad.discard()
                    self._maybe_store(audio, inp.sample_rate)
                    return audio
            if vad.started and not session.cancel.is_set():
                audio = vad.audio()
                vad.discard()
                return audio if len(audio) > 0.3 * inp.sample_rate else None
            return None
        finally:
            vad.discard()
            self.mic_level = 0.0
            with contextlib.suppress(Exception):
                inp.close()  # the microphone is only open while listening

    def _maybe_store(self, audio: np.ndarray, sample_rate: int) -> None:
        if not self.cfg.store_recordings:
            return  # default: recordings never touch the disk
        try:
            folder = self.ctx.config.data_path / "recordings"
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            dsp.write_wav(folder / f"utterance-{stamp}.wav", audio, sample_rate)
            log.info("stored recording (audio.store_recordings=true) in %s", folder)
        except Exception:  # noqa: BLE001
            log.warning("could not store recording", exc_info=True)

    def _active_stt(self) -> STTProvider:
        if self._stt.is_cloud and not (self._cloud_speech_allowed() and self._stt.available()):
            return self._stt_local
        return self._stt

    def _transcribe(self, audio: np.ndarray, sample_rate: int) -> STTResult | None:
        provider = self._active_stt()
        if provider.is_cloud and not self._cloud_speech_allowed():  # re-checked right before uploading
            provider = self._stt_local
        try:
            return provider.transcribe(audio, sample_rate)
        except Exception as e:  # noqa: BLE001
            self.last_stt_error = f"{provider.name}: {e}"
            if provider is self._stt_local:
                log.warning("speech recognition failed: %s", e)
                return None
            log.warning("%s speech recognition failed (%s); using %s", provider.name, e, self._stt_local.name)
        try:
            return self._stt_local.transcribe(audio, sample_rate)
        except Exception as e:  # noqa: BLE001
            self.last_stt_error = f"{self._stt_local.name}: {e}"
            log.warning("local speech recognition failed: %s", e)
            return None

    # =============================================================== diagnostics
    def list_devices(self) -> dict[str, Any]:
        try:
            out = dev.list_devices()
        except Exception as e:  # noqa: BLE001
            out = {"inputs": [], "outputs": [], "portaudio": False, "detail": f"device query failed: {e}"}
        out["selected"] = {"speaker": self._device_info("speaker"), "microphone": self._device_info("microphone")}
        return out

    def _rescan_output(self) -> None:
        """Look for a real speaker again (e.g. plugged in after start). Only while idle."""
        if self._output_busy():
            return
        old = self._out
        if self._out_injected:
            if old is not None and old.status != dev.STATUS_OK:
                with contextlib.suppress(Exception):
                    old.open()
            return
        dev.refresh_portaudio()
        spk = self.cfg.speaker
        res = dev.resolve_output(spk.backend, spk.device, spk.sample_rate, time_scale=self._clock.scale)
        new = res.backend
        if new.real and new.status == dev.STATUS_OK and not self._output_busy():
            self._out = new
            if old is not None and old is not new:
                with contextlib.suppress(Exception):
                    old.close()
            self._publish_device("speaker")
        elif new is not old:
            with contextlib.suppress(Exception):
                new.close()

    def _rescan_input(self) -> None:
        if self._listening or self._inp_injected:
            return
        dev.refresh_portaudio()
        mic = self.cfg.microphone
        res = dev.resolve_input(mic.backend, mic.device, mic.sample_rate, mic.channels)
        if res.backend.real and res.backend.status == dev.STATUS_OK:
            self._inp = res.backend
            self._publish_device("microphone")

    async def test_speaker(self) -> dict[str, Any]:
        try:
            await asyncio.to_thread(self._ensure_devices)
            self._ensure_worker()
            out = self._out
            if out is None or not out.real or out.status != dev.STATUS_OK:
                await asyncio.to_thread(self._rescan_output)
                out = self._out
            assert out is not None
            base = {"backend": out.name, "device": out.device_name, "volume": round(self._volume, 3)}
            if isinstance(out, NullOutput):
                return {**base, "ok": False, "status": out.status,
                        "detail": "Speaker disabled in config (audio.speaker.backend: none)."}
            if not out.real:
                await self.play_sound("chime")
                return {**base, "ok": False, "status": out.status,
                        "detail": "No real speaker found; the chime only played on the simulated speaker. " + SPEAKER_HINT}
            ok = await self.play_sound("chime")
            if ok:
                detail = f"Played a chime on {out.device_name} at volume {self._volume:.0%}."
                if self._volume <= 0.01:
                    detail += " Volume is 0, so nothing was audible: raise it in the dashboard."
            else:
                detail = f"Playback failed ({out.detail}). {SPEAKER_HINT}"
            return {**base, "ok": ok, "status": out.status, "detail": detail}
        except Exception as e:  # noqa: BLE001 - diagnostics never raise
            log.exception("speaker test failed")
            return {"ok": False, "backend": getattr(self._out, "name", None), "device": None,
                    "detail": f"Speaker test failed: {e}"}

    async def test_microphone(self, seconds: float = 2.0) -> dict[str, Any]:
        try:
            secs = max(0.2, min(10.0, float(seconds)))
        except (TypeError, ValueError):
            secs = 2.0
        await self._claim_mic_async("microphone test")  # the wake word closes its stream first
        try:
            return await self._test_microphone(secs)
        finally:
            self._unclaim_mic("microphone test")

    async def _test_microphone(self, secs: float) -> dict[str, Any]:
        try:
            await asyncio.to_thread(self._ensure_devices)
            if self._inp is None or not self._inp.real or self._inp.status != dev.STATUS_OK:
                await asyncio.to_thread(self._rescan_input)
            inp = self._inp
            assert inp is not None
            base: dict[str, Any] = {"ok": False, "backend": inp.name, "device": inp.device_name, "peak": 0.0, "rms": 0.0}
            if not self._mic_allowed():
                return {**base, "detail": "The microphone is turned off in the privacy settings."}
            if isinstance(inp, NullInput):
                return {**base, "detail": "Microphone disabled in config (audio.microphone.backend: none)."}
            if not inp.real:
                return {**base, "detail": "No real microphone found (simulated microphone in use). " + MIC_HINT}
            if self._listening:
                return {**base, "detail": "LittleBot is listening right now; try again in a moment."}
            if self._suspended:
                return {**base, "detail": f"The microphone is in use by {self._suspended}; try again after it ends."}
            if self.cfg.half_duplex:
                await self._wait_output_idle(None, limit=10.0)
            stats = await asyncio.to_thread(self._measure_input, secs)
            return {**base, **stats}
        except Exception as e:  # noqa: BLE001
            log.exception("microphone test failed")
            return {"ok": False, "backend": getattr(self._inp, "name", None), "device": None, "peak": 0.0, "rms": 0.0,
                    "detail": f"Microphone test failed: {e}"}

    def _measure_input(self, seconds: float) -> dict[str, Any]:
        """Record briefly, keep only running peak/RMS (the audio itself is never stored)."""
        inp = self._inp
        assert inp is not None
        try:
            inp.open()
        except AudioDeviceError as e:
            self._publish_device("microphone")
            return {"ok": False, "detail": f"Could not open the microphone: {e}. {MIC_HINT}"}
        self._publish_device("microphone")
        need = int(seconds * inp.sample_rate)
        got = 0
        pk = 0.0
        sumsq = 0.0
        deadline = time.monotonic() + seconds * 2 + 1.0
        error = ""
        try:
            while got < need and time.monotonic() < deadline and self._mic_allowed():
                try:
                    chunk = inp.read(0.2)
                except AudioDeviceError as e:
                    error = str(e)
                    break
                if chunk is None or not len(chunk):
                    continue
                chunk = np.clip(chunk * self._input_gain, -1.0, 1.0)
                pk = max(pk, dsp.peak(chunk))
                sumsq += float(np.sum(chunk.astype(np.float64) ** 2))
                got += len(chunk)
        finally:
            with contextlib.suppress(Exception):
                inp.close()
        level = math.sqrt(sumsq / got) if got else 0.0
        res = {"peak": round(pk, 4), "rms": round(level, 5), "seconds": round(got / float(inp.sample_rate), 2)}
        if error:
            return {**res, "ok": False, "detail": f"Microphone stopped during the test: {error}. {MIC_HINT}"}
        if got < need * 0.5:
            return {**res, "ok": False, "detail": "The microphone delivered too little audio. " + MIC_HINT}
        if pk < 1e-4:
            return {**res, "ok": False, "detail": "Only digital silence was recorded: the mic may be muted "
                                                  "(check `alsamixer -c <card>` capture level) or the wrong device."}
        detail = f"Recorded {res['seconds']}s: peak {dsp.dbfs(pk):.0f} dBFS, level {dsp.dbfs(level):.0f} dBFS."
        if level < self.cfg.microphone.vad_threshold / 3:
            detail += " That is very quiet: speak closer, or raise the capture level / audio.input_gain."
        return {**res, "ok": True, "detail": detail}

    # ============================================================ a recording for a voiceprint
    async def record_clip(self, seconds: float = 20.0) -> dict[str, Any]:
        """Record ``seconds`` from the microphone into memory, for the dashboard's "Record voice"
        (littlebot/voices/): ``{ok, samples, rate, detail}``. Never stored: the caller turns it into a
        voiceprint and drops it. The same checks as the microphone test (privacy, a real microphone, not
        while listening or lent to live voice)."""
        try:
            secs = max(1.0, min(30.0, float(seconds)))
        except (TypeError, ValueError):
            secs = 20.0
        await self._claim_mic_async("voice recording")  # the wake word closes its stream first
        try:
            return await self._record_clip(secs)
        finally:
            self._unclaim_mic("voice recording")

    async def _record_clip(self, secs: float) -> dict[str, Any]:
        fail: dict[str, Any] = {"ok": False, "samples": None, "rate": 0}
        try:
            await asyncio.to_thread(self._ensure_devices)
            if self._inp is None or not self._inp.real or self._inp.status != dev.STATUS_OK:
                await asyncio.to_thread(self._rescan_input)
            inp = self._inp
            if not self._mic_allowed():
                return {**fail, "detail": "The microphone is turned off in Privacy."}
            if inp is None or isinstance(inp, NullInput):
                return {**fail, "detail": "Microphone disabled in config (audio.microphone.backend: none)."}
            if not inp.real:
                return {**fail, "detail": "No real microphone found (simulated microphone in use). " + MIC_HINT}
            if self._listening:
                return {**fail, "detail": "LittleBot is listening right now; try again in a moment."}
            if self._suspended:
                return {**fail, "detail": f"The microphone is in use by {self._suspended}; try again after it ends."}
            if self.cfg.half_duplex:
                await self._wait_output_idle(None, limit=10.0)
            return await asyncio.to_thread(self._capture_clip, secs)
        except Exception as e:  # noqa: BLE001
            log.exception("voice recording failed")
            return {**fail, "detail": f"Recording failed: {e}"}

    def _capture_clip(self, seconds: float) -> dict[str, Any]:
        inp = self._inp
        assert inp is not None
        fail: dict[str, Any] = {"ok": False, "samples": None, "rate": int(inp.sample_rate)}
        try:
            inp.open()
        except AudioDeviceError as e:
            self._publish_device("microphone")
            return {**fail, "detail": f"Could not open the microphone: {e}. {MIC_HINT}"}
        need = int(seconds * inp.sample_rate)
        chunks: list[np.ndarray] = []
        got = 0
        deadline = time.monotonic() + seconds * 2 + 1.0
        error = ""
        try:
            while got < need and time.monotonic() < deadline and self._mic_allowed():
                try:
                    chunk = inp.read(0.2)
                except AudioDeviceError as e:
                    error = str(e)
                    break
                if chunk is None or not len(chunk):
                    continue
                if self._mic_muted():  # LittleBot's own sound is never part of someone's voice
                    continue
                chunks.append(np.clip(np.asarray(chunk, dtype=np.float32) * self._input_gain, -1.0, 1.0))
                got += len(chunk)
        finally:
            with contextlib.suppress(Exception):
                inp.close()
        if error:
            return {**fail, "detail": f"The microphone stopped during the recording: {error}. {MIC_HINT}"}
        if not self._mic_allowed():
            return {**fail, "detail": "The microphone was turned off in Privacy."}
        if got < need * 0.5:
            return {**fail, "detail": "The microphone delivered too little sound. " + MIC_HINT}
        samples = np.concatenate(chunks)
        del chunks
        return {"ok": True, "samples": samples, "rate": int(inp.sample_rate),
                "detail": f"Recorded {got / float(inp.sample_rate):.1f} s."}

    # =================================================================== status
    def _provider_label(self, active: Any, primary: Any) -> str:
        if active is primary:
            return active.name
        reason = self._cloud_block_reason() if getattr(primary, "is_cloud", False) else ""
        if reason and "privacy" not in reason:
            return f"{active.name} ({primary.name} not allowed: {reason})"
        return f"{active.name} ({primary.name} needs cloud + key)"

    def status(self) -> dict[str, Any]:
        with self._cv:
            busy = self._current is not None or bool(self._pending)
            queued = len(self._pending)
        return {
            "speaker": self._device_info("speaker"),
            "microphone": self._device_info("microphone"),
            "volume": round(self._volume, 3),
            "max_volume": self._max_volume,
            "speaking": self._speaking,
            "playing": busy,
            "queued": queued,
            "listening": self._listening,
            "mic_muted": self._mic_muted(),
            "half_duplex": bool(self.cfg.half_duplex),
            "lent_to": self._suspended,
            "tts_provider": self._provider_label(self._active_tts(), self._tts),
            "stt_provider": self._provider_label(self._active_stt(), self._stt),
            "tts_rate": self._tts_rate,
            "recorded_lines": self._recorded_count(),
            "last_error": self.last_tts_error or self.last_stt_error or "",
        }

    def _recorded_count(self) -> int:
        """How many lines are recorded in the ChatGPT voice (data/voice_lines)."""
        try:
            return len(self._voice_lines) if self._voice_lines is not None else 0
        except Exception:  # noqa: BLE001 - status never raises
            return 0
