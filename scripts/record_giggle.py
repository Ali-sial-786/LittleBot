#!/usr/bin/env python3
"""Record LittleBot's giggle in its GPT-Live voice (uses the robot's ChatGPT sign-in).

    .venv/bin/python scripts/record_giggle.py               # 3 takes -> data/sounds/voice/giggle_N.wav
    .venv/bin/python scripts/record_giggle.py --takes 5     # more takes
    .venv/bin/python scripts/record_giggle.py --dry-run     # record, but only save to data/giggle_takes/
    .venv/bin/python scripts/record_giggle.py --like ~/giggle.mp4 --ask   # copy that giggle; hear each take

Each take is a short live call: the voice is asked to give a real, happy giggle and then say
"That tickles!", and only the voice's own audio is recorded. The microphone is never used (the call
gets silence; if the voice doesn't react to the written request it hears a spoken request made with
espeak-ng instead).

--like FILE plays the giggle in FILE (any video or audio file; --start/--end pick a part of it, in
seconds) to the voice instead, and the voice copies it as closely as it can in its own voice
(nothing else, unless --say "That tickles!" is given). --ask plays each take on the speaker and
asks whether to keep it.

The takes are used instead of the shipped giggle clips (scripts/set_giggle.py --reset goes back);
restart LittleBot afterwards: systemctl --user restart littlebot
"""
from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import set_giggle  # noqa: E402

RATE = 48000
OUT_RATE = 22050
INSTRUCTIONS = (
    "You are LittleBot, a cute, friendly toy robot, and you are being recorded for a sound effect. "
    "When you are asked, perform exactly this out loud: a real, warm, happy giggle like someone being "
    "tickled - actual laughter sounds, never the spoken words 'he he' or 'hee hee' - and then the words "
    "'That tickles!'. Say nothing else before or after, and don't hand anything to the client."
)
REQUEST = "Do it now: giggle like you're being tickled, a real happy laugh, then say 'That tickles!'. Nothing else."
SPOKEN_REQUEST = "Please giggle like you are being tickled, then say: that tickles!"

# --like: the voice hears a giggle and copies it.
LIKE_INSTRUCTIONS = (
    "You are LittleBot, a cute, friendly toy robot, and you are being recorded for a sound effect. "
    "You will hear a short giggle. As soon as it ends, copy it out loud as exactly as you can in your own "
    "voice: the same happy laughs, the same number of them, the same rhythm, speed, pitch and length. "
    "Real laughter sounds only, never the spoken words 'he he' or 'hee hee'{say}. Say nothing else before "
    "or after, and don't hand anything to the client."
)
LIKE_REQUEST = "Listen to the giggle I play you now, then copy it exactly{say}. Nothing else."
SPOKEN_LIKE_REQUEST = "Now copy that giggle exactly."
QUIET_END = 1.5  # seconds of quiet after the voice that end a take


def _say_clause(say: str | None, lead: str) -> str:
    return f"{lead}'{say}'" if say else ""


def _espeak(text: str) -> np.ndarray | None:
    """The spoken fallback request at 48 kHz (int16), or None without espeak-ng."""
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if exe is None:
        return None
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "req.wav"
        subprocess.run([exe, "-s", "150", "-w", str(wav), text], check=True, capture_output=True)
        from littlebot.audio import dsp

        x, sr = dsp.read_wav(wav)
    return to_call_pcm(x, sr)


def to_call_pcm(x: np.ndarray, sr: int) -> np.ndarray:
    """Float audio at ``sr`` as the call's microphone sound (int16 at RATE)."""
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    n = int(len(x) * RATE / sr)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return (np.clip(y, -1, 1) * 20000).astype(np.int16)


def load_like(path: Path, start: float | None = None, end: float | None = None) -> np.ndarray:
    """The giggle to copy, from any video or audio file, trimmed and levelled like set_giggle does."""
    return to_call_pcm(set_giggle.tidy(set_giggle.decode(path), start, end, peak=1.0), set_giggle.RATE)


def _mic_track() -> Any:
    """Silence, except for the sounds handed to ``play()`` (one after another)."""
    import av
    from aiortc.mediastreams import AudioStreamTrack

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


async def record_take(auth: Any, cfg: Any, *, transport: Any = None, call_url: str | None = None,
                      max_seconds: float = 12.0, nudge_after: float = 4.0, like: np.ndarray | None = None,
                      say: str | None = None) -> tuple[np.ndarray, str, str]:
    """One live call: returns (voice audio at OUT_RATE as float32, what the voice said, voice name).

    ``like``: a giggle (int16 at RATE) the voice hears and copies; only what it says after hearing
    the whole giggle is kept. ``say``: words to add after the giggle in that case.
    """
    import av
    import httpx
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamError

    from littlebot.chatgpt import wire
    from littlebot.chatgpt.live import CALL_URL

    if like is None:
        instructions, request, spoken = INSTRUCTIONS, REQUEST, SPOKEN_REQUEST
    else:
        instructions = LIKE_INSTRUCTIONS.format(say=_say_clause(say, ", and then say the words "))
        request = LIKE_REQUEST.format(say=_say_clause(say, " and then say "))
        spoken = SPOKEN_LIKE_REQUEST
    mic = _mic_track()
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTrack(mic)
    channel = pc.createDataChannel("oai-events")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=OUT_RATE)
    chunks: list[np.ndarray] = []
    said: list[str] = []
    done = asyncio.Event()
    heard_voice = asyncio.Event()
    opened = asyncio.Event()
    listening = asyncio.Event()  # keep the voice's sound from now on (after the giggle, with --like)
    if like is None:
        listening.set()
    loop = asyncio.get_running_loop()
    last_loud = [0.0]

    def on_message(msg: Any) -> None:
        parsed = wire.parse(msg)
        if parsed is None:
            return
        _t, ev = parsed
        if isinstance(ev, wire.AssistantText) and ev.final and listening.is_set():
            said.append(ev.text)
            loop.call_later(0.8, done.set)  # keep the tail of the last word
        elif isinstance(ev, wire.Closed):
            done.set()

    def on_open() -> None:
        for f in wire.note_frames(request):
            channel.send(f)
        opened.set()

    channel.on("message", on_message)
    channel.on("open", on_open)

    async def pump(track: Any) -> None:
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            if not listening.is_set():
                continue
            for f in resampler.resample(frame):
                pcm = f.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
                if len(pcm) and float(np.sqrt(np.mean(pcm * pcm))) > 0.01:
                    heard_voice.set()
                    last_loud[0] = loop.time()
                elif heard_voice.is_set() and loop.time() - last_loud[0] > QUIET_END:
                    done.set()  # the voice has finished (no caption came)
                chunks.append(pcm)

    tasks: list[asyncio.Task[Any]] = []
    pc.on("track", lambda track: tasks.append(asyncio.ensure_future(pump(track))) if track.kind == "audio" else None)
    await pc.setLocalDescription(await pc.createOffer())
    bearer = await auth.bearer()
    voice = ""
    try:
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            for voice in list(dict.fromkeys([cfg.live_voice, *cfg.live_voice_fallbacks, "sol"])):
                body = {"sdp": pc.localDescription.sdp, "session": {
                    "model": cfg.live_model, "instructions": instructions,
                    "audio": {"output": {"voice": voice}}, "delegation": {"type": "client"}}}
                headers = {**auth.headers(bearer), "OpenAI-Alpha": "quicksilver=v2", "session-id": str(uuid.uuid4()),
                           "thread-id": str(uuid.uuid4()), "x-session-id": str(uuid.uuid4())}
                resp = await client.post(call_url or CALL_URL, json=body, headers=headers)
                if resp.status_code in (400, 422):
                    continue  # that voice isn't available: try the next one
                if resp.status_code >= 400:
                    raise SystemExit(f"ChatGPT refused the call (HTTP {resp.status_code}).")
                break
            else:
                raise SystemExit("ChatGPT accepted none of the voices.")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=resp.text, type="answer"))
        if like is not None:
            await asyncio.wait_for(opened.wait(), 15.0)
            await asyncio.sleep(0.5)  # let the request land before the giggle starts
            await asyncio.wait_for(mic.play(like).wait(), len(like) / RATE + 10.0)
            listening.set()
        try:
            await asyncio.wait_for(heard_voice.wait(), nudge_after)
        except asyncio.TimeoutError:
            prompt = _espeak(spoken)  # it didn't react to the written request: ask out loud
            if prompt is not None:
                mic.play(prompt)
        try:
            await asyncio.wait_for(done.wait(), max_seconds)
        except asyncio.TimeoutError:
            pass
    finally:
        await pc.close()
        for t in tasks:
            t.cancel()
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return audio, " ".join(said).strip(), voice


def tidy(audio: np.ndarray, peak: float = 0.55) -> np.ndarray:
    """Trim the silence around the voice and normalise its loudness."""
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


async def main_async(args: argparse.Namespace) -> int:
    from littlebot.audio import dsp
    from littlebot.chatgpt.auth import ChatGPTAuth
    from littlebot.core.config import load_config

    cfg = load_config(mode="pi")
    token_file = Path(cfg.chatgpt.token_file).expanduser()
    if not token_file.is_absolute():
        token_file = cfg.data_path / token_file
    auth = ChatGPTAuth(token_file, originator=cfg.chatgpt.originator, user_agent="LittleBot giggle recorder")
    if not auth.signed_in:
        print("Not signed in with ChatGPT on this robot. Sign in on the dashboard's AI page first.")
        return 1
    # data/ is outside the code: updates never touch the recordings, and they win over the shipped clips.
    like = None
    if args.like is not None:
        if not args.like.expanduser().is_file():
            print(f"No such file: {args.like}")
            return 1
        like = load_like(args.like.expanduser(), args.start, args.end)
        print(f"The voice will copy {len(like) / RATE:.1f} s of giggle from {args.like}.")
    out_dir = (cfg.data_path / "giggle_takes") if args.dry_run else cfg.data_path / "sounds" / "voice"
    out_dir.mkdir(parents=True, exist_ok=True)
    takes: list[np.ndarray] = []
    for i in range(1, args.takes + 1):
        print(f"Take {i}: calling the GPT-Live voice...", flush=True)
        audio, said, voice = await record_take(auth, cfg.chatgpt, like=like, say=args.say)
        clip = tidy(audio)
        secs = len(clip) / OUT_RATE
        print(f"   voice {voice!r} said: {said or '(no words heard)'}  ->  {secs:.1f} s of audio")
        if secs < 0.4:
            print("   too short, skipped")
            continue
        if secs > 6:
            print("   longer than 6 s (it may have said more than asked); kept, but check it")
        if args.ask and not ask_keep(clip):
            print("   skipped")
            continue
        takes.append(clip)
    if not takes:
        print("No usable takes. Nothing was changed.")
        return 1
    if not args.dry_run:
        for old in out_dir.glob("giggle_*.wav"):
            old.unlink()
    for i, clip in enumerate(takes, 1):
        path = out_dir / f"giggle_{i}.wav"
        dsp.write_wav(path, clip, OUT_RATE)
        print(f"Saved {path}")
    if not args.dry_run:
        print("Done. Restart LittleBot to use them:  systemctl --user restart littlebot")
    print(json.dumps({"takes": len(takes)}))
    return 0


def play_clip(clip: np.ndarray) -> bool:
    """Play a take on the speaker (PipeWire / PulseAudio / ALSA); False if no player is installed."""
    from littlebot.audio import dsp

    for cmd in (["pw-play"], ["paplay"], ["aplay", "-q"]):
        exe = shutil.which(cmd[0])
        if exe is None:
            continue
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "take.wav"
            dsp.write_wav(wav, clip, OUT_RATE)
            subprocess.run([exe, *cmd[1:], str(wav)], check=False)
        return True
    return False


def ask_keep(clip: np.ndarray) -> bool:
    while True:
        if not play_clip(clip):
            print("   (no sound player found to play it: pw-play, paplay or aplay)")
        answer = input("   Keep this take? [y]es / [n]o / [r]eplay: ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--takes", type=int, default=3, help="how many takes to record (default 3)")
    ap.add_argument("--dry-run", action="store_true", help="save to data/giggle_takes/ instead of replacing the clips")
    ap.add_argument("--like", type=Path, default=None, help="video or audio file with a giggle for the voice to copy")
    ap.add_argument("--start", type=float, default=None, help="with --like: start of the giggle in the file (seconds)")
    ap.add_argument("--end", type=float, default=None, help="with --like: end of the giggle in the file (seconds)")
    ap.add_argument("--say", default=None, help='with --like: words to say after the giggle, e.g. "That tickles!"')
    ap.add_argument("--ask", action="store_true", help="play each take on the speaker and ask whether to keep it")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
