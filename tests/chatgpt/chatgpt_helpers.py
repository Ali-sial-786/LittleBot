"""Shared fakes for the ChatGPT sign-in / provider / live voice tests (no network, no devices)."""

from __future__ import annotations

import asyncio
import base64
import fractions
import json
import time
from typing import Any

import numpy as np

from littlebot.brain.providers.base import BaseProvider, ProviderReply
from littlebot.chatgpt.auth import AUTH_CLAIM, ChatGPTAuth, TokenSet


def jwt(payload: dict[str, Any]) -> str:
    def b64(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def access_token(account: str = "acct-123", exp: float | None = None, plan: str = "plus") -> str:
    return jwt({"exp": int(exp if exp is not None else time.time() + 3600),
                AUTH_CLAIM: {"chatgpt_account_id": account, "chatgpt_plan_type": plan}})


def signed_in_auth(tmp_path: Any, **kw: Any) -> ChatGPTAuth:
    auth = ChatGPTAuth(tmp_path / "chatgpt_auth.json", **kw)
    auth.store.write(TokenSet(access_token=access_token(), refresh_token="refresh-1", expires_at=time.time() + 3600,
                              account_id="acct-123", plan_type="plus", email_masked="p***@example.com"))
    return auth


class FakeProvider(BaseProvider):
    name = "chatgpt"
    is_cloud = True
    model = "fake"

    def __init__(self, text: str = "A cow says moo.") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float) -> ProviderReply:
        self.calls.append({"messages": messages, "system": system})
        return ProviderReply(text=self.text, model="fake")


class FakeLiveAudio:
    """Stands in for LiveAudio: silent 20 ms mic frames, counts the voice frames it is given."""

    def __init__(self) -> None:
        self.opened = self.closed = self.muted = False
        self.speaking = False
        self.level = 0.0
        self.mic_open = True
        self.frames = 0
        self.peak = 0

    def open(self, loop: Any) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    async def read_frame(self) -> np.ndarray:
        await asyncio.sleep(0.02)
        return np.zeros(960, dtype=np.int16)

    def play_frame(self, frame: Any) -> None:
        self.frames += 1
        self.peak = max(self.peak, int(np.abs(frame.to_ndarray()).max()))

    def mute(self) -> None:
        self.muted = True


def tone_track(silent: Any = None) -> Any:
    """A 440 Hz tone, the 'voice' of the fake OpenAI peer (silence while ``silent()`` is true)."""
    from aiortc import MediaStreamTrack
    import av

    class Tone(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.pts = 0
            self.t0: float | None = None

        async def recv(self) -> Any:
            if self.t0 is None:
                self.t0 = time.monotonic()
            wait = self.t0 + self.pts / 48000 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            t = (np.arange(960) + self.pts) / 48000
            pcm = (np.sin(2 * np.pi * 440 * t) * (0 if silent is not None and silent() else 8000)).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate, frame.pts, frame.time_base = 48000, self.pts, fractions.Fraction(1, 48000)
            self.pts += 960
            return frame

    return Tone()


class FakeOpenAI:
    """httpx handler that answers the call like OpenAI would: a real local WebRTC peer + data channel."""

    def __init__(self, status: int = 201, reject_voices: tuple[str, ...] = ()) -> None:
        self.status = status
        self.reject_voices = reject_voices
        self.requests: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.pc: Any = None
        self.channel: Any = None
        self.channel_ready = asyncio.Event()
        self.heard: list[np.ndarray] = []  # the client's microphone sound, int16 frames
        self.voice_quiet = False  # the "voice" pauses (silence) while this is true
        self._tasks: list[Any] = []

    async def __call__(self, request: Any) -> Any:
        import httpx
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"code": "nope"}})
        if body["session"]["audio"]["output"]["voice"] in self.reject_voices:
            return httpx.Response(400, json={"error": {"code": "invalid_value", "param": "session.audio.output.voice"}})
        pc = self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))

        @pc.on("datachannel")
        def _dc(ch: Any) -> None:
            self.channel = ch
            ch.on("message", lambda m: self.received.append(json.loads(m)))
            self.channel_ready.set()

        async def listen(track: Any) -> None:
            from aiortc.mediastreams import MediaStreamError

            while True:
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    return
                self.heard.append(frame.to_ndarray().reshape(-1))

        pc.on("track", lambda t: self._tasks.append(asyncio.ensure_future(listen(t))) if t.kind == "audio" else None)
        pc.addTrack(tone_track(lambda: self.voice_quiet))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        return httpx.Response(201, text=pc.localDescription.sdp, headers={"content-type": "application/sdp"})

    def send(self, event: dict[str, Any]) -> None:
        self.channel.send(json.dumps(event))

    async def close(self) -> None:
        if self.pc is not None:
            await self.pc.close()
        for t in self._tasks:
            t.cancel()
