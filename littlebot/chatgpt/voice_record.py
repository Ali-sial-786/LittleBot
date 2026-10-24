"""Record fixed lines in the GPT-Live voice: many lines per call, each checked against its transcript.

GPT-Live speaks only inside a live call (audio-only WebRTC; there is no text-to-speech endpoint), so
LittleBot records its fixed lines once (``littlebot.app.spoken_lines``) and plays the recordings
(``littlebot.audio.voice_lines``, ``AudioService.say``). One call records many lines, because a call
takes a few seconds to connect: for each line the voice gets the note "Say exactly, word for word,
nothing else: <line>", its sound is kept until its turn ends (``turn.done``) and goes quiet, and its
transcript is compared with the line (:func:`similarity`). A mismatch is asked again (``retries``);
lines that never match are reported and keep the text-to-speech voice.

The call machinery is shared with ``scripts/record_giggle.py``: :func:`open_call` (offer, the voice
and its fall-backs, the answer), :func:`mic_track` (the call's "microphone": silence, or a sound
handed to it) and :func:`tidy` (trim the silence, level the loudness). The robot's own microphone and
speaker are never used, so LittleBot can keep playing meanwhile. Uses the robot's ChatGPT sign-in.

:class:`VoiceLineJob` runs a whole recording (the command line and the dashboard's button), saving
each matched take in the voice line store as it goes.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from littlebot.audio.voice_lines import normalise

log = logging.getLogger("littlebot.chatgpt.voice_record")

RATE = 48000  # the call's audio (WebRTC / Opus)
OUT_RATE = 22050  # recordings
MATCH = 0.8  # similarity() a take's transcript needs to count as the line
MIN_SECONDS = 0.25  # a shorter take (after trimming) is no take
LINE_TIMEOUT_S = 25.0  # the voice's turn hasn't ended this long after the line was asked: no take
NUDGE_S = 6.0  # no sound from the voice this long after asking: ask out loud too (espeak, if installed)
TAIL_QUIET_S = 1.0  # after its turn ends, the voice's sound counts as finished after this much quiet
TAIL_MAX_S = 12.0  # ...but never wait longer than this for the end of the sound
LOUD = 0.01  # RMS of a 20 ms block that counts as the voice making sound
MAX_FAILED_CALLS = 3  # calls in a row that fail to connect or drop: give up

INSTRUCTIONS = (
    "You are LittleBot, a small, friendly toy robot, and you are being recorded saying your lines for games "
    "with a baby. Each time you are given a line, say exactly that line out loud, word for word, and nothing "
    "else: no greeting, no 'okay' or 'sure', no comment before or after, and never an earlier line again. "
    "Say it warmly and happily, a little slowly and clearly, like talking to a little one; animal noises and "
    "sound words (moo, boing, peekaboo) playfully. Say numbers as words. Don't hand anything to the client."
)
ASK = "Say exactly, word for word, nothing else: {line}"
SPOKEN_ASK = "Please say exactly this: {line}"
DELEGATION_ANSWER = "Nothing to look up: just say the line out loud now, word for word, and nothing else."
_NUMBERS = dict(enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                          "fifteen sixteen seventeen eighteen nineteen twenty".split()))


class VoiceCallError(Exception):
    """The call could not be made (refused, no voice accepted, not signed in)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CallLost(Exception):
    """The call dropped while recording."""


# ------------------------------------------------------------ the call's audio
def mic_track() -> Any:
    """The call's microphone: silence, except for the sounds handed to ``play()`` (one after another)."""
    import av  # noqa: PLC0415
    from aiortc.mediastreams import AudioStreamTrack  # noqa: PLC0415

    class Mic(AudioStreamTrack):
        def __init__(self) -> None:
            super().__init__()
            self._queue: list[tuple[np.ndarray, asyncio.Event]] = []
            self._pos = 0
            self._pts = 0

        def play(self, pcm: np.ndarray) -> asyncio.Event:
            """Queue ``pcm`` (int16 at RATE); the returned event is set once it has all been sent."""
            ended = asyncio.Event()
            self._queue.append((pcm, ended))
            return ended

        async def recv(self) -> Any:
            await super().recv()  # only for the 20 ms pacing: its silence is 8 kHz, every frame here is RATE
            n = RATE // 50
            pcm = np.zeros(n, dtype=np.int16)
            if self._queue:
                sound, ended = self._queue[0]
                chunk = sound[self._pos:self._pos + n]
                pcm[:len(chunk)] = chunk
                self._pos += n
                if self._pos >= len(sound):
                    self._queue.pop(0)
                    self._pos = 0
                    ended.set()
            out = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
            out.sample_rate, out.pts, out.time_base = RATE, self._pts, fractions.Fraction(1, RATE)
            self._pts += n
            return out

    return Mic()


def to_call_pcm(x: np.ndarray, sr: int) -> np.ndarray:
    """Float audio at ``sr`` as the call's microphone sound (int16 at RATE)."""
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    n = int(len(x) * RATE / sr)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return (np.clip(y, -1, 1) * 20000).astype(np.int16)


def espeak_pcm(text: str) -> np.ndarray | None:
    """``text`` spoken by espeak-ng as the call's microphone sound, or None without espeak-ng."""
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe is None:
        return None
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "req.wav"
        # The text goes through stdin, never the command line (no option injection).
        subprocess.run([exe, "-s", "150", "-w", str(wav), "--stdin"], input=text.encode("utf-8"), check=True,
                       capture_output=True)
        from littlebot.audio import dsp  # noqa: PLC0415

        x, sr = dsp.read_wav(wav)
    return to_call_pcm(x, sr)


def tidy(audio: np.ndarray, peak: float = 0.55) -> np.ndarray:
    """Trim the silence around the voice (at OUT_RATE) and normalise its loudness."""
    if not len(audio):
        return audio
    win = max(1, OUT_RATE // 50)
    env = np.sqrt(np.convolve(audio * audio, np.ones(win) / win, mode="same"))
    loud = np.nonzero(env > max(0.01, 0.06 * float(env.max())))[0]
    if not len(loud):
        return np.zeros(0, dtype=np.float32)
    pad = int(0.06 * OUT_RATE)
    y = audio[max(0, loud[0] - pad): loud[-1] + pad].astype(np.float64)
    fade = min(len(y) // 4, int(0.02 * OUT_RATE))
    if fade:
        y[:fade] *= np.linspace(0, 1, fade)
        y[-fade:] *= np.linspace(1, 0, fade)
    return (y / max(1e-6, float(np.abs(y).max())) * peak).astype(np.float32)


# ------------------------------------------------------------ the call
def voices(cfg: Any) -> list[str]:
    """The voice to ask for first, then its fall-backs (``chatgpt.live_voice`` / ``live_voice_fallbacks``)."""
    return list(dict.fromkeys(v.strip().lower() for v in [cfg.live_voice, *cfg.live_voice_fallbacks, "sol"]
                              if v and v.strip()))


async def open_call(auth: Any, cfg: Any, instructions: str, *, mic: Any, on_message: Callable[[Any], None],
                    on_open: Callable[[Any], None] | None = None, on_track: Callable[[Any], None] | None = None,
                    transport: Any = None, call_url: str | None = None) -> tuple[Any, Any, str]:
    """Start a GPT-Live call that only sends ``mic``: returns (peer connection, data channel, voice).

    ``cfg``: ``config.chatgpt``. Tries the configured voice, then its fall-backs (a voice the account
    doesn't have is refused with HTTP 400/422). The caller closes the peer connection."""
    import httpx  # noqa: PLC0415
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: PLC0415

    from littlebot.chatgpt.live import CALL_URL  # noqa: PLC0415

    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    try:
        pc.addTrack(mic)
        channel = pc.createDataChannel("oai-events")
        channel.on("message", on_message)
        if on_open is not None:
            channel.on("open", lambda: on_open(channel))
        if on_track is not None:
            pc.on("track", on_track)
        await pc.setLocalDescription(await pc.createOffer())
        bearer = await auth.bearer()
        refreshed = False
        answer, voice = None, ""
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            choices = voices(cfg)
            while choices:
                voice = choices[0]
                body = {"sdp": pc.localDescription.sdp, "session": {
                    "model": cfg.live_model, "instructions": instructions,
                    "audio": {"output": {"voice": voice}}, "delegation": {"type": "client"}}}
                headers = {**auth.headers(bearer), "OpenAI-Alpha": "quicksilver=v2", "session-id": str(uuid.uuid4()),
                           "thread-id": str(uuid.uuid4()), "x-session-id": str(uuid.uuid4())}
                resp = await client.post(call_url or CALL_URL, json=body, headers=headers)
                if resp.status_code == 401 and not refreshed:
                    refreshed = True
                    bearer = await auth.on_unauthorized(bearer)
                    continue
                if resp.status_code in (400, 422):
                    choices.pop(0)  # that voice isn't available: try the next one
                    continue
                if resp.status_code >= 400:
                    raise VoiceCallError(f"ChatGPT refused the call (HTTP {resp.status_code}).", resp.status_code)
                answer = resp.text
                break
        if answer is None:
            raise VoiceCallError("ChatGPT accepted none of the voices.")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
    except BaseException:
        await pc.close()
        raise
    return pc, channel, voice


# ------------------------------------------------------------ checking a take
def _squash(text: str) -> str:
    """Letters and digits only, numbers as words, repeated letters once ("Moooo!" == "Moo", "1" == "one")."""
    words = [_NUMBERS.get(int(w), w) if w.isdigit() else w for w in normalise(text).split()]
    return re.sub(r"(.)\1+", r"\1", "".join(words))


def similarity(said: str, line: str) -> float:
    """0..1: how close the voice's transcript is to the line (case, punctuation, spaces don't count)."""
    a, b = _squash(said), _squash(line)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


@dataclass
class Take:
    line: str
    audio: np.ndarray  # trimmed and levelled, OUT_RATE, float32 (empty: nothing usable)
    said: str  # the voice's transcript
    voice: str
    score: float
    tries: int

    @property
    def ok(self) -> bool:
        return self.score >= MATCH and len(self.audio) / OUT_RATE >= MIN_SECONDS


class _Call:
    """One live call that says line after line."""

    def __init__(self, auth: Any, cfg: Any, *, transport: Any, call_url: str | None, instructions: str) -> None:
        self.auth, self.cfg, self.transport, self.call_url = auth, cfg, transport, call_url
        self.instructions = instructions
        self.pc: Any = None
        self.channel: Any = None
        self.voice = ""
        self.mic: Any = None
        self.recording = False
        self._chunks: list[np.ndarray] = []
        self._said: list[str] = []
        self._last_loud: float | None = None
        self._turn_done = asyncio.Event()
        self._opened = asyncio.Event()
        self._closed = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._loop = asyncio.get_running_loop()

    async def connect(self, timeout: float = 20.0) -> None:
        import av  # noqa: PLC0415

        from littlebot.chatgpt import wire  # noqa: PLC0415

        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=OUT_RATE)
        self.mic = mic_track()

        def on_message(msg: Any) -> None:
            parsed = wire.parse(msg)
            if parsed is None:
                return
            _t, ev = parsed
            if isinstance(ev, wire.AssistantText) and ev.final and self.recording:
                self._said.append(ev.text)
                self._turn_done.set()
            elif isinstance(ev, wire.Delegation) and self.channel is not None:
                for f in wire.result_frames(ev.id, DELEGATION_ANSWER):
                    self.channel.send(f)
            elif isinstance(ev, wire.Closed):
                self._closed.set()

        def on_track(track: Any) -> None:
            if track.kind == "audio":
                self._tasks.append(asyncio.ensure_future(self._pump(track)))

        self.pc, self.channel, self.voice = await asyncio.wait_for(open_call(
            self.auth, self.cfg, self.instructions, mic=self.mic, on_message=on_message,
            on_open=lambda _ch: self._opened.set(), on_track=on_track, transport=self.transport,
            call_url=self.call_url), timeout)
        if self.channel.readyState != "open":
            await asyncio.wait_for(self._opened.wait(), timeout)

    async def _pump(self, track: Any) -> None:
        from aiortc.mediastreams import MediaStreamError  # noqa: PLC0415

        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            if not self.recording:
                continue
            for f in self._resampler.resample(frame):
                pcm = f.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
                if len(pcm) and float(np.sqrt(np.mean(pcm * pcm))) > LOUD:
                    self._last_loud = self._loop.time()
                self._chunks.append(pcm)

    def _lost(self) -> bool:
        return self._closed.is_set() or getattr(self.pc, "connectionState", "new") in ("failed", "closed")

    async def say(self, line: str, *, timeout: float = LINE_TIMEOUT_S, nudge_after: float = NUDGE_S
                  ) -> tuple[np.ndarray, str]:
        """Ask the voice to say ``line``: (its sound at OUT_RATE, its transcript)."""
        from littlebot.chatgpt import wire  # noqa: PLC0415

        loop = self._loop
        self._chunks, self._said, self._last_loud = [], [], None
        self._turn_done.clear()
        self.recording = True
        try:
            for f in wire.note_frames(ASK.format(line=line)):
                self.channel.send(f)
            t0, nudged = loop.time(), False
            while not self._turn_done.is_set():  # 1) until its turn ends (its final caption)
                if self._lost():
                    raise CallLost("the call ended")
                now = loop.time()
                if now - t0 > timeout:
                    break
                if not nudged and self._last_loud is None and now - t0 > nudge_after:
                    nudged = True  # it didn't react to the written line: ask out loud as well
                    pcm = await asyncio.to_thread(_quiet(espeak_pcm), SPOKEN_ASK.format(line=line))
                    if pcm is not None:
                        self.mic.play(pcm)
                try:
                    await asyncio.wait_for(self._turn_done.wait(), 0.05)
                except asyncio.TimeoutError:
                    pass
            if self._turn_done.is_set():  # 2) the caption can come before the end of the sound
                end = loop.time() + TAIL_MAX_S
                while loop.time() < end and not self._lost():
                    last = self._last_loud
                    if last is not None and loop.time() - last >= TAIL_QUIET_S:
                        break
                    await asyncio.sleep(0.05)
        finally:
            self.recording = False
        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
        return audio, " ".join(t for t in self._said if t).strip()

    async def close(self) -> None:
        if self.pc is not None:
            try:
                await asyncio.wait_for(self.pc.close(), 5.0)
            except Exception:  # noqa: BLE001
                log.debug("closing the recording call failed", exc_info=True)
        for t in self._tasks:
            t.cancel()


def _is_auth_error(e: BaseException) -> bool:
    from littlebot.chatgpt.auth import ChatGPTAuthError  # noqa: PLC0415

    return isinstance(e, ChatGPTAuthError)


def _quiet(fn: Callable[[str], Any]) -> Callable[[str], Any]:
    def run(text: str) -> Any:
        try:
            return fn(text)
        except Exception:  # noqa: BLE001 - the spoken nudge is only a fallback
            return None

    return run


TakeCallback = Callable[[int, int, Take], "Awaitable[None] | None"]


async def record_lines(auth: Any, cfg: Any, lines: list[str], *, on_take: TakeCallback | None = None,
                       on_status: Callable[[str], None] | None = None, stop: asyncio.Event | None = None,
                       transport: Any = None, call_url: str | None = None, retries: int = 2,
                       lines_per_call: int = 40, line_timeout: float = LINE_TIMEOUT_S, nudge_after: float = NUDGE_S,
                       instructions: str = INSTRUCTIONS) -> list[Take]:
    """Record ``lines`` in the GPT-Live voice, ``lines_per_call`` lines per call; returns the best take of each.

    A take whose transcript doesn't match its line (:data:`MATCH`) is asked again up to ``retries``
    times. ``on_take(i, total, take)`` gets each line's final take as it is done. ``stop`` set:
    returns after the line being recorded. Raises :class:`VoiceCallError` when no call can be made."""
    takes: list[Take] = []
    call: _Call | None = None
    said_in_call = 0
    failed_calls = 0

    def status(text: str) -> None:
        if on_status is not None:
            on_status(text)

    try:
        i = 0
        while i < len(lines):
            if stop is not None and stop.is_set():
                break
            line = lines[i]
            if call is not None and said_in_call >= lines_per_call:
                await call.close()
                call = None
            if call is None:
                status("connecting")
                call = _Call(auth, cfg, transport=transport, call_url=call_url, instructions=instructions)
                try:
                    await call.connect()
                except (VoiceCallError, asyncio.CancelledError):
                    await call.close()
                    raise
                except Exception as e:  # noqa: BLE001 - network trouble, a timeout: try again a few times
                    await call.close()
                    call = None
                    if _is_auth_error(e):
                        raise VoiceCallError(str(e) or "The ChatGPT sign-in doesn't work. Sign in again.") from e
                    failed_calls += 1
                    log.warning("recording call didn't connect (%s)", type(e).__name__)
                    if failed_calls >= MAX_FAILED_CALLS:
                        raise VoiceCallError("Couldn't connect to the ChatGPT voice. Check the robot's internet.") from e
                    await asyncio.sleep(2.0)
                    continue
                said_in_call = 0
                status("recording")
            best: Take | None = None
            try:
                for attempt in range(1 + max(0, retries)):
                    audio, said = await call.say(line, timeout=line_timeout, nudge_after=nudge_after)
                    clip = tidy(audio, peak=0.6)
                    take = Take(line, clip, said, call.voice, similarity(said, line), attempt + 1)
                    if best is None or (take.score, len(take.audio)) > (best.score, len(best.audio)):
                        best = take
                    if take.ok or (stop is not None and stop.is_set()):
                        break
                    log.info("take %d of a line didn't match (%.2f)", attempt + 1, take.score)
            except CallLost:
                await call.close()
                call = None
                failed_calls += 1
                log.warning("the recording call dropped; calling again")
                if failed_calls >= MAX_FAILED_CALLS:
                    raise VoiceCallError("The ChatGPT voice call kept dropping. Check the robot's internet.") from None
                continue  # the same line again, in a new call
            failed_calls = 0
            said_in_call += 1
            assert best is not None
            takes.append(best)
            if on_take is not None:
                result = on_take(i, len(lines), best)
                if asyncio.iscoroutine(result):
                    await result
            i += 1
    finally:
        if call is not None:
            await call.close()
    return takes


# ------------------------------------------------------------ a whole recording
@dataclass
class VoiceLineJob:
    """Record ``lines`` (``spoken_lines.SpokenLine``) and save each matched take in ``store``.

    ``status()`` is what the dashboard shows; ``stop()`` ends it after the current line."""

    store: Any  # littlebot.audio.voice_lines.VoiceLineStore
    auth: Any
    cfg: Any  # config.chatgpt
    lines: list[Any]
    transport: Any = None
    call_url: str | None = None
    retries: int = 2
    lines_per_call: int = 40
    on_line: Callable[[int, int, Any, Take, bool], None] | None = None  # (i, total, line, take, saved): progress
    on_phase: Callable[[str], None] | None = None
    phase: str = "waiting"  # waiting | connecting | recording | done | stopped | failed
    done: int = 0
    saved: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)  # lines that never matched: {text, said}
    current: str = ""
    error: str = ""
    voice: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    _stop: asyncio.Event | None = None

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    @property
    def running(self) -> bool:
        return self.phase in ("waiting", "connecting", "recording")

    async def run(self) -> dict[str, Any]:
        self._stop = asyncio.Event()
        self.started_at = time.time()
        texts = [x.text for x in self.lines]
        if texts:
            self.current = texts[0]

        def on_status(phase: str) -> None:
            self.phase = phase
            if self.on_phase is not None:
                self.on_phase(phase)

        async def on_take(i: int, total: int, take: Take) -> None:
            line = self.lines[i]
            self.voice = take.voice
            if take.ok:
                await asyncio.to_thread(self.store.save, line.text, take.audio, OUT_RATE, voice=take.voice,
                                        source=line.source, extra={"tries": take.tries, "match": round(take.score, 2)})
                self.saved += 1
            else:
                self.failed.append({"text": line.text, "said": take.said[:200]})
            self.done = i + 1  # after the save, so done == saved + never matched in the status
            self.current = texts[i + 1] if i + 1 < total else ""
            if self.on_line is not None:
                self.on_line(i, total, line, take, take.ok)

        try:
            await record_lines(self.auth, self.cfg, texts, on_take=on_take, on_status=on_status, stop=self._stop,
                               transport=self.transport, call_url=self.call_url, retries=self.retries,
                               lines_per_call=self.lines_per_call)
            self.phase = "stopped" if self._stop.is_set() and self.done < len(texts) else "done"
        except VoiceCallError as e:
            self.phase, self.error = "failed", str(e)
        except asyncio.CancelledError:
            self.phase = "stopped"
            raise
        except Exception as e:  # noqa: BLE001 - report it, never take the robot down
            log.exception("recording the voice lines failed")
            self.phase, self.error = "failed", f"Recording failed ({type(e).__name__})."
        finally:
            self.finished_at = time.time()
            self.current = ""
            log.info("voice lines: %s (%d of %d done, %d saved, %d never matched)", self.phase, self.done,
                     len(texts), self.saved, len(self.failed))
        return self.status()

    def status(self) -> dict[str, Any]:
        return {"phase": self.phase, "running": self.running, "total": len(self.lines), "done": self.done,
                "saved": self.saved, "failed": list(self.failed[-50:]), "failed_count": len(self.failed),
                "current": self.current, "error": self.error, "voice": self.voice, "started_at": self.started_at,
                "finished_at": self.finished_at}
