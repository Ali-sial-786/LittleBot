"""The assistant's voices (littlebot/assistant/tts): each cloud voice's request and streamed PCM, the chain's order,
fallbacks and 48 kHz resampling, the local voice, and that no key ever shows. No network: httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from littlebot.assistant.speech import SpeechError, StreamingTTS
from littlebot.assistant.tts import OUTPUT_RATE, TTSChain, build_tts_chain
from littlebot.assistant.tts.cartesia import API_VERSION, CartesiaTTS
from littlebot.assistant.tts.local import LocalTTS
from littlebot.audio.errors import TTSError
from littlebot.audio.tts import MockTTS, TTSProvider
from littlebot.testing import make_test_context

KEYS = {"cartesia_api_key": "sk_car_TESTKEY_cartesia_0001", "elevenlabs_api_key": "xi_TESTKEY_eleven_0002",
        "openai_api_key": "sk-TESTKEY-openai-0003"}


def _ctx(cloud: bool = True, keys: dict[str, str] | None = None, **over: Any) -> Any:
    ctx = make_test_context(**over)
    for name in ("cartesia_api_key", "elevenlabs_api_key", "openai_api_key", "deepgram_api_key"):
        setattr(ctx.config, name, None)
    for name, value in (KEYS if keys is None else keys).items():
        setattr(ctx.config, name, SecretStr(value))
    if cloud:
        ctx.privacy.set(cloud_enabled=True)
    return ctx


def _sine(rate: int, seconds: float, hz: float = 440.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _pcm(x: np.ndarray) -> bytes:
    return np.round(np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def _pieces(data: bytes, sizes: tuple[int, ...] = (777, 1001, 4093, 333)) -> list[bytes]:
    """``data`` cut into odd-sized pieces (some split a sample in two), as a network delivers it."""
    out, i, k = [], 0, 0
    while i < len(data):
        n = sizes[k % len(sizes)]
        out.append(data[i:i + n])
        i, k = i + n, k + 1
    return out


async def _body(pieces: list[bytes]) -> AsyncIterator[bytes]:
    for p in pieces:
        yield p


class Recorder:
    """A MockTransport handler: answers every POST with streamed PCM (per host), records the requests."""

    def __init__(self, audio: dict[str, bytes] | None = None, status: dict[str, int] | None = None) -> None:
        self.audio = audio or {}
        self.status = status or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if request.method == "HEAD":
            return httpx.Response(405)
        code = self.status.get(host, 200)
        if code != 200:
            return httpx.Response(code, json={"error": "nope"})
        return httpx.Response(200, headers={"content-type": "audio/pcm"},
                              content=_body(_pieces(self.audio.get(host, _pcm(_sine(24000, 0.3))))))

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests if r.method == "POST"]


async def _collect(stream: AsyncIterator[np.ndarray]) -> np.ndarray:
    chunks = [c async for c in stream]
    assert all(c.dtype == np.float32 and c.ndim == 1 for c in chunks)
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def _peak_hz(x: np.ndarray, rate: int) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.argmax(spec) * rate / len(x))


# ---------------------------------------------------------------- each cloud voice
async def test_cartesia_request_and_48k_stream_without_resampling():
    rec = Recorder({"api.cartesia.ai": _pcm(_sine(48000, 0.5))})
    ctx = _ctx(assistant={"tts": ["cartesia"], "cartesia_speed": "fast"})
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(rec))
    out = await _collect(chain.stream("Hello there, friend!"))
    assert chain.last_provider == "cartesia"
    (req,) = rec.requests
    assert str(req.url) == "https://api.cartesia.ai/tts/bytes"
    assert req.headers["Cartesia-Version"] == API_VERSION
    assert req.headers["Authorization"] == "Bearer " + KEYS["cartesia_api_key"]
    body = json.loads(req.content)
    assert body["model_id"] == ctx.config.assistant.cartesia_model
    assert body["transcript"] == "Hello there, friend!"
    assert body["voice"] == {"id": ctx.config.assistant.cartesia_voice}
    assert body["output_format"] == {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 48000}
    assert body["language"] == "en"
    assert body["generation_config"] == {"speed": 1.15, "emotion": "calm"}  # gentle by default
    assert len(out) == 24000  # 0.5 s at 48 kHz, every sample (pieces split samples in two)
    assert np.max(np.abs(out - _sine(48000, 0.5))) < 1e-3
    await chain.aclose()


async def test_cartesia_mood_becomes_a_gentle_emotion_and_speed_may_be_a_number():
    from littlebot.assistant.speech import MOOD

    rec = Recorder()
    p = CartesiaTTS(key=lambda: "k", cloud_ok=lambda: True, speed=0.92, emotion="calm",
                    transport=httpx.MockTransport(rec))
    MOOD.set("happy")
    await _collect(p.stream("That's lovely."))
    MOOD.set("excited")
    await _collect(p.stream("You won."))
    MOOD.set(None)
    await _collect(p.stream("Okay."))
    configs = [json.loads(r.content)["generation_config"] for r in rec.requests]
    assert configs == [{"speed": 0.92, "emotion": "content"}, {"speed": 0.92, "emotion": "happy"},
                       {"speed": 0.92, "emotion": "calm"}]
    await p.aclose()


async def test_cartesia_normal_speed_sends_no_generation_config():
    rec = Recorder()
    p = CartesiaTTS(key=lambda: "k", cloud_ok=lambda: True, transport=httpx.MockTransport(rec))
    await _collect(p.stream("Hi."))
    assert "generation_config" not in json.loads(rec.requests[0].content)
    await p.aclose()


async def test_elevenlabs_request_and_24k_resampled_to_48k():
    rec = Recorder({"api.elevenlabs.io": _pcm(_sine(24000, 0.5, hz=300))})
    ctx = _ctx(assistant={"tts": ["elevenlabs"]})
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(rec))
    out = await _collect(chain.stream("Good morning."))
    (req,) = rec.requests
    voice = ctx.config.assistant.elevenlabs_voice
    assert req.url.path == f"/v1/text-to-speech/{voice}/stream"
    assert req.url.params["output_format"] == "pcm_24000"
    assert req.headers["xi-api-key"] == KEYS["elevenlabs_api_key"]
    assert "authorization" not in req.headers
    body = json.loads(req.content)
    assert body == {"text": "Good morning.", "model_id": "eleven_flash_v2_5", "language_code": "en"}
    assert chain.sample_rate == OUTPUT_RATE == 48000
    assert abs(len(out) - 24000) <= 2  # 0.5 s, now at 48 kHz
    assert abs(_peak_hz(out, 48000) - 300) < 5
    await chain.aclose()


async def test_openai_request_pcm_and_instructions():
    rec = Recorder({"api.openai.com": _pcm(_sine(24000, 0.2))})
    ctx = _ctx(assistant={"tts": ["openai"]})
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(rec))
    out = await _collect(chain.stream("Sure, let me check."))
    (req,) = rec.requests
    assert str(req.url) == "https://api.openai.com/v1/audio/speech"
    assert req.headers["Authorization"] == "Bearer " + KEYS["openai_api_key"]
    body = json.loads(req.content)
    assert body["model"] == "gpt-4o-mini-tts" and body["voice"] == "coral" and body["response_format"] == "pcm"
    assert body["input"] == "Sure, let me check."
    assert "warm" in body["instructions"] and "calm" in body["instructions"]
    assert abs(len(out) - 9600) <= 2
    await chain.aclose()


async def test_resampling_keeps_phase_across_chunks():
    """A 24 kHz sine cut into odd pieces comes out as one smooth 48 kHz sine (no click at a chunk boundary)."""
    src = _sine(24000, 0.4, hz=440)
    rec = Recorder({"api.openai.com": _pcm(src)})
    chain = build_tts_chain(_ctx(assistant={"tts": ["openai"]}), transport=httpx.MockTransport(rec))
    out = await _collect(chain.stream("Testing."))
    ideal = _sine(48000, 0.4, hz=440)[: len(out)]
    assert np.max(np.abs(out - ideal)) < 0.005
    assert np.max(np.abs(np.diff(out))) < 0.04  # 440 Hz at 0.5: at most ~0.029 per sample


async def test_json_answer_is_never_played():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": True})

    p = CartesiaTTS(key=lambda: "k", cloud_ok=lambda: True, transport=httpx.MockTransport(handler))
    with pytest.raises(SpeechError) as e:
        await _collect(p.stream("Hi."))
    assert e.value.kind == "bad_audio"


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (402, "auth"), (429, "rate_limit"), (500, "server_error"),
                                              (422, "bad_request")])
async def test_http_errors_become_kinds(status, kind):
    rec = Recorder(status={"api.cartesia.ai": status})
    p = CartesiaTTS(key=lambda: "k", cloud_ok=lambda: True, transport=httpx.MockTransport(rec))
    with pytest.raises(SpeechError) as e:
        await _collect(p.stream("Hi."))
    assert e.value.kind == kind


async def test_network_failure_and_timeout_kinds():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    for handler, kind in ((refuse, "network"), (slow, "timeout")):
        p = CartesiaTTS(key=lambda: "k", cloud_ok=lambda: True, transport=httpx.MockTransport(handler))
        with pytest.raises(SpeechError) as e:
            await _collect(p.stream("Hi."))
        assert e.value.kind == kind


# ---------------------------------------------------------------- the chain
async def test_missing_key_skips_to_the_next_voice():
    rec = Recorder()
    ctx = _ctx(keys={"elevenlabs_api_key": KEYS["elevenlabs_api_key"]})
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(rec))
    assert chain.describe()["first"] == "elevenlabs"
    await _collect(chain.stream("Hello."))
    assert rec.hosts() == ["api.elevenlabs.io"]
    assert chain.last_provider == "elevenlabs"
    rows = {r["name"]: r["missing"] for r in chain.describe()["providers"]}
    assert rows == {"cartesia": "no_key", "elevenlabs": None, "openai": "no_key", "local": None}


async def test_failing_voice_hands_the_sentence_on_and_rests():
    rec = Recorder(status={"api.cartesia.ai": 503})
    now = [1000.0]
    ctx = _ctx()
    built = build_tts_chain(ctx, transport=httpx.MockTransport(rec))
    chain = TTSChain(built.providers, cloud_ok=lambda: True, first_audio_timeout=2.5, clock=lambda: now[0])
    await _collect(chain.stream("First sentence."))
    assert rec.hosts() == ["api.cartesia.ai", "api.elevenlabs.io"]
    assert json.loads(rec.requests[1].content)["text"] == "First sentence."  # the same sentence
    assert chain.last_provider == "elevenlabs" and chain.last_error == "cartesia: server_error"
    await _collect(chain.stream("Second sentence."))  # cartesia rests: not asked again
    assert rec.hosts() == ["api.cartesia.ai", "api.elevenlabs.io", "api.elevenlabs.io"]
    assert chain.describe()["providers"][0]["missing"] == "resting after server_error"
    now[0] += 30  # rested enough (and the service works again)
    rec.status.clear()
    await _collect(chain.stream("Third sentence."))
    assert rec.hosts()[-1] == "api.cartesia.ai"


class FakeVoice:
    """A scripted StreamingTTS."""

    is_cloud = True

    def __init__(self, name: str, *, chunks: int = 3, wait: asyncio.Event | None = None, fail_before: str = "",
                 fail_after: int | None = None, rate: int = 24000) -> None:
        self.name = name
        self.sample_rate = rate
        self.chunks = chunks
        self.wait = wait
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.texts: list[str] = []
        self.closed = False
        self.streams_done = 0  # streams that ended or were closed (their finally ran)

    def missing(self) -> str | None:
        return None

    async def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        self.texts.append(text)
        try:
            if self.wait is not None:
                await self.wait.wait()
            if self.fail_before:
                raise SpeechError(self.fail_before)
            for i in range(self.chunks):
                if self.fail_after is not None and i == self.fail_after:
                    raise SpeechError("network", "dropped")
                yield np.full(240, 0.1, dtype=np.float32)
        finally:
            self.streams_done += 1

    async def warm(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


async def test_no_first_audio_in_time_tries_the_next():
    stuck = FakeVoice("slow", wait=asyncio.Event())
    good = FakeVoice("good")
    chain = TTSChain([stuck, good], cloud_ok=lambda: True, first_audio_timeout=0.05)
    out = await _collect(chain.stream("Hello."))
    assert len(out) > 0 and chain.last_provider == "good"
    assert stuck.texts == good.texts == ["Hello."]
    assert chain.last_error == "slow: timeout"
    assert stuck.streams_done == 1  # its request was given up (closed), not left hanging


async def test_empty_answer_tries_the_next():
    silent = FakeVoice("silent", chunks=0)
    good = FakeVoice("good")
    chain = TTSChain([silent, good], cloud_ok=lambda: True)
    await _collect(chain.stream("Hello."))
    assert chain.last_provider == "good" and chain.last_error == "silent: bad_audio"


async def test_failure_after_a_chunk_is_not_retried():
    flaky = FakeVoice("flaky", chunks=4, fail_after=2)
    other = FakeVoice("other")
    chain = TTSChain([flaky, other], cloud_ok=lambda: True)
    got = []
    with pytest.raises(SpeechError) as e:
        async for chunk in chain.stream("A long sentence."):
            got.append(chunk)
    assert e.value.kind == "network"
    assert len(got) >= 1
    assert other.texts == []  # never asked: the words would be said twice


async def test_raise_before_audio_uses_the_next():
    bad = FakeVoice("bad", fail_before="auth")
    good = FakeVoice("good")
    chain = TTSChain([bad, good], cloud_ok=lambda: True)
    await _collect(chain.stream("Hi."))
    assert chain.last_provider == "good"
    assert chain.describe()["providers"][0]["missing"] == "resting after auth"


async def test_stopping_early_closes_the_voice_stream():
    voice = FakeVoice("v", chunks=50)
    chain = TTSChain([voice], cloud_ok=lambda: True)
    gen = chain.stream("Talk talk talk.")
    first = await gen.__anext__()
    assert len(first)
    assert voice.streams_done == 0
    await gen.aclose()  # barge-in: the rest is not wanted
    assert voice.streams_done == 1


class LimitedVoice(FakeVoice):
    """A FakeVoice with a limit on requests at once, like Cartesia's plan: more than that is refused (429)."""

    def __init__(self, name: str, *, limit: int = 1, busy: int = 0, **kw: Any) -> None:
        super().__init__(name, **kw)
        self.max_concurrent = limit
        self.busy = busy  # this many requests are refused as busy first
        self.open = self.most_open = 0

    async def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        self.texts.append(text)
        if self.busy:
            self.busy -= 1
            raise SpeechError("rate_limit", "HTTP 429")
        self.open += 1
        self.most_open = max(self.most_open, self.open)
        try:
            for _ in range(self.chunks):
                await asyncio.sleep(0.01)  # made over a little while, as a real voice does
                yield np.full(240, 0.1, dtype=np.float32)
        finally:
            self.open -= 1


async def test_a_voice_is_never_asked_for_more_at_once_than_it_allows():
    voice = LimitedVoice("cartesia", limit=1, chunks=5)
    other = FakeVoice("openai")
    chain = TTSChain([voice, other], cloud_ok=lambda: True, first_audio_timeout=0.03)
    outs = await asyncio.gather(*(_collect(chain.stream(f"Sentence {i}.")) for i in range(3)))
    assert all(len(o) for o in outs)
    assert voice.most_open == 1
    assert other.texts == []  # waiting for its turn is not "too slow": the same voice says every sentence
    assert sorted(voice.texts) == ["Sentence 0.", "Sentence 1.", "Sentence 2."]


async def test_busy_voice_is_asked_again_before_another_voice_speaks(caplog):
    voice = LimitedVoice("cartesia", busy=1)
    other = FakeVoice("openai")
    chain = TTSChain([voice, other], cloud_ok=lambda: True)
    with caplog.at_level(logging.INFO, logger="littlebot.assistant.tts"):
        out = await _collect(chain.stream("Hello there."))
    assert len(out) and chain.last_provider == "cartesia"
    assert voice.texts == ["Hello there.", "Hello there."] and other.texts == []
    assert "cartesia is busy; asking again" in caplog.text


async def test_still_busy_hands_the_sentence_on_and_rests_briefly():
    now = [1000.0]
    voice = LimitedVoice("cartesia", busy=2)
    other = FakeVoice("openai")
    chain = TTSChain([voice, other], cloud_ok=lambda: True, clock=lambda: now[0])
    await _collect(chain.stream("Hello there."))
    assert chain.last_provider == "openai" and chain.last_error == "cartesia: rate_limit"
    assert chain.describe()["providers"][0]["missing"] == "resting after rate_limit"
    now[0] += 16  # a short rest: busy is over in moments, not minutes
    await _collect(chain.stream("And again."))
    assert chain.last_provider == "cartesia"


async def test_stopping_early_frees_the_voice_for_the_next_sentence():
    voice = LimitedVoice("cartesia", limit=1, chunks=50)
    chain = TTSChain([voice], cloud_ok=lambda: True, first_audio_timeout=0.5)
    gen = chain.stream("A long story.")
    await gen.__anext__()
    assert voice.open == 1
    await gen.aclose()  # barge-in
    assert voice.open == 0
    assert len(await _collect(chain.stream("Okay.")))  # its turn at once, not after SLOT_WAIT_S


def test_cartesia_keeps_to_one_request_at_a_time_by_default():
    chain = build_tts_chain(_ctx())
    cartesia = next(p for p in chain.providers if p.name == "cartesia")
    assert cartesia.max_concurrent == 1


async def test_cloud_off_uses_only_the_local_voice():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    ctx = _ctx(cloud=False)
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(handler))
    out = await _collect(chain.stream("Hello there."))
    assert len(out) > 0 and chain.last_provider == "local"
    assert calls == []
    assert {r["name"]: r["missing"] for r in chain.describe()["providers"]}["cartesia"] == "cloud_off"
    await chain.warm()
    assert calls == []


async def test_offline_mode_uses_only_the_local_voice():
    calls = []
    ctx = _ctx(cloud=False, mode="offline")
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)))
    await _collect(chain.stream("Offline words."))
    assert chain.last_provider == "local" and calls == []


async def test_cloud_switched_off_later_is_respected():
    rec = Recorder()
    ctx = _ctx()
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(rec))
    await _collect(chain.stream("One."))
    ctx.privacy.set(cloud_enabled=False)
    await _collect(chain.stream("Two."))
    assert rec.hosts() == ["api.cartesia.ai"] and chain.last_provider == "local"


def test_local_is_always_last():
    chain = build_tts_chain(_ctx(assistant={"tts": ["cartesia"]}))
    assert [p.name for p in chain.providers] == ["cartesia", "local"]
    chain = build_tts_chain(_ctx(assistant={"tts": ["local", "openai"]}))
    assert [p.name for p in chain.providers] == ["local", "openai"]
    assert all(isinstance(p, StreamingTTS) for p in chain.providers)


async def test_warm_opens_the_first_voice_without_the_key():
    rec = Recorder()
    chain = build_tts_chain(_ctx(), transport=httpx.MockTransport(rec))
    await chain.warm()
    (req,) = rec.requests
    assert req.method == "HEAD" and req.url.host == "api.cartesia.ai"
    assert "authorization" not in req.headers and "xi-api-key" not in req.headers


async def test_empty_text_says_nothing():
    voice = FakeVoice("v")
    chain = TTSChain([voice], cloud_ok=lambda: True)
    assert len(await _collect(chain.stream("  \n "))) == 0
    assert voice.texts == []


# ---------------------------------------------------------------- the local voice
async def test_local_voice_chunks_of_100ms_resampled_to_48k():
    local = LocalTTS([MockTTS(sample_rate=22050)])
    raw = [c async for c in local.stream("one two three four five")]
    assert local.sample_rate == 22050
    assert all(len(c) == 2205 for c in raw[:-1])
    chain = TTSChain([local], cloud_ok=lambda: False)
    out = await _collect(chain.stream("one two three four five"))
    total = sum(len(c) for c in raw)
    assert abs(len(out) - total * 48000 / 22050) < 3


class BrokenVoice(TTSProvider):
    name = "broken"

    def synthesize(self, text: str, rate: float = 1.0) -> tuple[np.ndarray, int]:
        raise TTSError("the voice crashed")


async def test_local_voice_falls_back_to_the_next_local_voice():
    local = LocalTTS([BrokenVoice(), MockTTS()])
    out = await _collect(TTSChain([local], cloud_ok=lambda: False).stream("Hello."))
    assert len(out) > 0 and local.voice == "mock"


def test_local_voice_from_config():
    ctx = _ctx(cloud=False)
    assert [v.name for v in LocalTTS.from_config(ctx.config.audio.tts).voices] == ["mock"]


# ---------------------------------------------------------------- keys never show
async def test_no_key_in_logs_errors_or_descriptions(caplog):
    caplog.set_level(logging.DEBUG)
    rec = Recorder(status={"api.cartesia.ai": 401, "api.elevenlabs.io": 429, "api.openai.com": 500})
    ctx = _ctx()
    chain = build_tts_chain(ctx, transport=httpx.MockTransport(rec))
    await _collect(chain.stream("Hello."))  # every cloud voice fails: the local one speaks
    assert chain.last_provider == "local"
    errors = []
    for p in chain.providers[:3]:
        try:
            await _collect(p.stream("Hello."))
        except SpeechError as e:
            errors.append(e)
    assert [e.kind for e in errors] == ["auth", "rate_limit", "server_error"]
    seen = caplog.text + json.dumps(chain.describe()) + "".join(f"{e!s}{e!r}" for e in errors) + "".join(
        repr(p) for p in chain.providers) + repr(rec.requests)
    for key in KEYS.values():
        assert key not in seen
