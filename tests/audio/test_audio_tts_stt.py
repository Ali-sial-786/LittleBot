"""TTS / STT providers: mock, espeak-ng, piper (fake binary), OpenAI (httpx.MockTransport), cloud gating."""

from __future__ import annotations

import json
import shutil
import stat

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from littlebot.audio import dsp
from littlebot.audio.devices import MockInput, MockOutput
from littlebot.audio.errors import CloudNotAllowed, STTError, TTSError
from littlebot.audio.stt import MockSTT, OpenAISTT, VoskSTT, build_stt, vosk_installed
from littlebot.audio.tts import EspeakTTS, MockTTS, OpenAITTS, PiperTTS, build_tts
from littlebot.core.config import STTConfig, TTSConfig

HAS_ESPEAK = bool(shutil.which("espeak-ng") or shutil.which("espeak"))


def _wav_bytes(secs: float = 0.5, sr: int = 24000) -> bytes:
    t = np.arange(int(secs * sr)) / sr
    return dsp.encode_wav((0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), sr)


class Recorder:
    """httpx MockTransport handler that records requests and replies as configured."""

    def __init__(self, reply):
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("simulated timeout", request=request)


# ------------------------------------------------------------------ mock TTS
def test_mock_tts_duration_and_quiet_babble():
    tts = MockTTS()
    x, sr = tts.synthesize("one two three four five", 1.0)
    assert len(x) / sr == pytest.approx(2.0, abs=0.01)  # words / 2.5 s
    assert 0.01 < dsp.peak(x) <= 0.05 + 1e-6  # very quiet blips, but enough envelope for the mouth
    slow, _ = tts.synthesize("one two three four five", 0.5)
    assert len(slow) / sr == pytest.approx(4.0, abs=0.01)
    assert np.array_equal(tts.synthesize("same text")[0], tts.synthesize("same text")[0])
    silent, _ = MockTTS(style="silence").synthesize("a b c", 1.0)
    assert len(silent) > 0 and not np.any(silent)
    with pytest.raises(ValueError):
        MockTTS(style="opera")


# ------------------------------------------------------------------ espeak
@pytest.mark.skipif(not HAS_ESPEAK, reason="espeak-ng not installed")
def test_espeak_produces_friendly_audio():
    tts = EspeakTTS(voice="en-us+f3", pitch=65)
    assert tts.available()
    x, sr = tts.synthesize("Hello little friend!", 1.0)
    assert sr in (16000, 22050, 24000) and x.dtype == np.float32
    assert 0.5 < len(x) / sr < 5.0
    assert dsp.peak(x) == pytest.approx(0.6, abs=0.02)  # normalised, never clipping
    fast, _ = tts.synthesize("Hello little friend, how are you today?", 1.5)
    slow, _ = tts.synthesize("Hello little friend, how are you today?", 0.6)
    assert len(slow) > 1.5 * len(fast)


@pytest.mark.skipif(not HAS_ESPEAK, reason="espeak-ng not installed")
def test_espeak_text_cannot_inject_options():
    x, sr = EspeakTTS().synthesize("--help -v xx --stdout", 1.0)
    assert len(x) / sr > 0.5  # spoken as text via stdin, not parsed as options


@pytest.mark.skipif(not HAS_ESPEAK, reason="espeak-ng not installed")
def test_espeak_unknown_voice_falls_back():
    tts = EspeakTTS(voice="en-us+f3")
    tts.voice = "en-us+notavariant"
    y, _sr = tts.synthesize("hello", 1.0)
    assert len(y) > 0
    with pytest.raises(TTSError):
        EspeakTTS(voice="zz-nonexistent").synthesize("hello", 1.0)


def test_espeak_missing_binary():
    tts = EspeakTTS(binary=None)
    tts.binary = None
    assert not tts.available() and "apt install espeak-ng" in tts.unavailable_reason()
    with pytest.raises(TTSError):
        tts.synthesize("hi")


# ------------------------------------------------------------------ piper (fake binary)
def test_piper_with_fake_binary(tmp_path):
    fake = tmp_path / "piper"
    fake.write_text("#!/bin/sh\ncat > /dev/null\nhead -c 8820 /dev/zero\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"onnx")
    (tmp_path / "voice.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 16000}}))
    tts = PiperTTS(str(model), binary=str(fake))
    assert tts.available()
    x, sr = tts.synthesize("hello", 1.0)
    assert sr == 16000 and len(x) >= 4410
    assert not PiperTTS(None, binary=str(fake)).available()
    assert "not configured" in PiperTTS(None, binary=str(fake)).unavailable_reason()
    assert "not found" in PiperTTS(str(tmp_path / "missing.onnx"), binary=str(fake)).unavailable_reason()


# ------------------------------------------------------------------ provider selection
def test_auto_tts_is_local_never_cloud():
    primary, local = build_tts(TTSConfig(provider="auto"), cloud_allowed=lambda: True, api_key=lambda: "sk-test")
    assert primary is local and not primary.is_cloud
    assert primary.name == ("espeak" if HAS_ESPEAK else "mock")
    p, loc = build_tts(TTSConfig(provider="openai"), cloud_allowed=lambda: False, api_key=lambda: None)
    assert p.is_cloud and not loc.is_cloud
    p, _ = build_tts(TTSConfig(provider="piper", piper_model=None), cloud_allowed=lambda: False, api_key=lambda: None)
    assert p.name in ("espeak", "mock")  # piper not configured -> local


def test_auto_stt_is_mock_without_vosk_model():
    primary, local = build_stt(STTConfig(provider="auto"), cloud_allowed=lambda: True, api_key=lambda: "k")
    assert isinstance(primary, MockSTT) and primary is local
    vosk = VoskSTT(None)
    assert not vosk.available() and vosk.unavailable_reason()
    if not vosk_installed():
        assert "pip install vosk" in vosk.unavailable_reason()


# ------------------------------------------------------------------ OpenAI TTS
def _openai_tts(handler, *, allowed=True, key="sk-test-123"):
    return OpenAITTS(api_key=lambda: key, cloud_allowed=lambda: allowed, transport=handler.transport, timeout=2.0)


def test_openai_tts_success_request_shape():
    rec = Recorder(lambda req: httpx.Response(200, content=_wav_bytes(), headers={"content-type": "audio/wav"}))
    tts = _openai_tts(rec)
    x, sr = tts.synthesize("Hello baby", 0.9)
    assert sr == 24000 and len(x) > 0.5 * sr and dsp.peak(x) == pytest.approx(0.6, abs=0.02)
    req = rec.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/audio/speech"
    assert req.headers["authorization"] == "Bearer sk-test-123"
    body = json.loads(req.content)
    assert body["model"] == "gpt-4o-mini-tts" and body["voice"] == "shimmer" and body["input"] == "Hello baby"
    assert body["response_format"] == "wav" and body["speed"] == 0.9
    assert "softly" in body["instructions"].lower()


def test_openai_tts_raw_pcm_response():
    pcm = (np.ones(2400, dtype="<i2") * 1000).tobytes()
    rec = Recorder(lambda req: httpx.Response(200, content=pcm, headers={"content-type": "audio/pcm"}))
    x, sr = _openai_tts(rec).synthesize("hi")
    assert sr == 24000 and len(x) >= 2400


@pytest.mark.parametrize("reply,match", [
    (lambda req: httpx.Response(401, json={"error": {"message": "Incorrect API key"}}), "401"),
    (lambda req: httpx.Response(429, json={}), "429"),
    (lambda req: httpx.Response(500, text="oops"), "HTTP 500"),
    (_timeout, "timed out"),
    (lambda req: httpx.Response(200, content=b"garbage"), "unexpected content"),
    (lambda req: httpx.Response(200, json={"error": "x"}), "unexpected content"),
    (lambda req: httpx.Response(200, content=b"RIFF\x00\x00\x00\x00WAVEjunk"), "unreadable"),
])
def test_openai_tts_errors_raise_tts_error(reply, match):
    rec = Recorder(reply)
    with pytest.raises(TTSError, match=match) as exc:
        _openai_tts(rec).synthesize("hi")
    assert "sk-test-123" not in str(exc.value)  # never leak the key


def test_openai_tts_refused_without_cloud_or_key():
    rec = Recorder(lambda req: httpx.Response(200, content=_wav_bytes()))
    with pytest.raises(CloudNotAllowed):
        _openai_tts(rec, allowed=False).synthesize("hi")
    with pytest.raises(CloudNotAllowed):
        _openai_tts(rec, key=None).synthesize("hi")
    assert rec.requests == []
    assert not _openai_tts(rec, allowed=False).available()


# ------------------------------------------------------------------ OpenAI STT
def _openai_stt(handler, *, allowed=True, key="sk-test-123"):
    return OpenAISTT(api_key=lambda: key, cloud_allowed=lambda: allowed, transport=handler.transport, timeout=2.0)


def _speech(secs: float = 1.0) -> np.ndarray:
    t = np.arange(int(secs * 16000)) / 16000
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_openai_stt_success_multipart_in_memory():
    rec = Recorder(lambda req: httpx.Response(200, json={"text": " more milk please "}))
    res = _openai_stt(rec).transcribe(_speech(), 16000)
    assert res is not None and res.text == "more milk please" and 0 < res.confidence <= 1
    req = rec.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/audio/transcriptions"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert b"whisper-1" in body and b'name="language"' in body and b"RIFF" in body and b"WAVE" in body


def test_openai_stt_empty_and_short_audio():
    rec = Recorder(lambda req: httpx.Response(200, json={"text": ""}))
    assert _openai_stt(rec).transcribe(_speech(), 16000) is None
    assert _openai_stt(rec).transcribe(_speech(0.1), 16000) is None  # too short: not uploaded
    assert len(rec.requests) == 1


@pytest.mark.parametrize("reply,match", [
    (lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}), "401"),
    (lambda req: httpx.Response(503, text="busy"), "HTTP 503"),
    (_timeout, "timed out"),
    (lambda req: httpx.Response(200, text="not json"), "invalid JSON"),
])
def test_openai_stt_errors(reply, match):
    with pytest.raises(STTError, match=match):
        _openai_stt(Recorder(reply)).transcribe(_speech(), 16000)


def test_openai_stt_refused_without_cloud():
    rec = Recorder(lambda req: httpx.Response(200, json={"text": "hi"}))
    with pytest.raises(CloudNotAllowed):
        _openai_stt(rec, allowed=False).transcribe(_speech(), 16000)
    with pytest.raises(CloudNotAllowed):
        _openai_stt(rec, key="").transcribe(_speech(), 16000)
    assert rec.requests == []


# ------------------------------------------------------------------ service-level gating / fallback
def _cloud_ctx(actx, *, cloud: bool, age: int | None = 30, ai_enabled: bool = True, nickname: str = ""):
    """Every cloud gate open (unless told otherwise): privacy cloud, key, a known age, ai.enabled."""
    actx.config.openai_api_key = SecretStr("sk-test-123")
    actx.privacy.set(cloud_enabled=cloud)
    profile = dict(actx.settings.get("profile") or {})
    profile.update({"age_months": age, "birth_month": "", "nickname": nickname})
    actx.settings.set("profile", profile)
    actx.settings.set("ai.enabled", ai_enabled)
    return actx


async def test_service_uses_cloud_tts_only_when_allowed(actx, make_service):
    rec = Recorder(lambda req: httpx.Response(200, content=_wav_bytes()))
    ctx = _cloud_ctx(actx, cloud=False)
    tts = OpenAITTS(api_key=lambda: "sk-test-123", cloud_allowed=lambda: bool(ctx.privacy.cloud_enabled),
                    transport=rec.transport)
    svc = make_service(ctx, tts=tts)
    assert await svc.say("offline voice please") is True
    assert rec.requests == []  # privacy: cloud off -> never contacted
    assert svc.status()["tts_provider"].startswith(svc._tts_local.name)
    ctx.privacy.set(cloud_enabled=True)
    assert await svc.say("now the cloud voice") is True
    assert len(rec.requests) == 1 and svc.status()["tts_provider"] == "openai"


@pytest.mark.parametrize("reply", [lambda req: httpx.Response(401, json={}), _timeout])
async def test_service_falls_back_to_local_tts_on_cloud_failure(actx, make_service, reply):
    rec = Recorder(reply)
    ctx = _cloud_ctx(actx, cloud=True)
    tts = OpenAITTS(api_key=lambda: "sk-test-123", cloud_allowed=lambda: True, transport=rec.transport)
    out = MockOutput(time_scale=0)
    svc = make_service(ctx, tts=tts, output=out)
    assert await svc.say("hello anyway") is True
    assert len(rec.requests) == 1 and "openai" in svc.last_tts_error
    assert out.played[-1].completed


async def test_service_cloud_stt_gating_and_fallback(actx, make_service):
    rec = Recorder(_timeout)
    ctx = _cloud_ctx(actx, cloud=True)
    stt = OpenAISTT(api_key=lambda: "sk-test-123", cloud_allowed=lambda: bool(ctx.privacy.cloud_enabled),
                    transport=rec.transport)
    mic = MockInput()
    svc = make_service(ctx, stt=stt, input=mic)
    assert isinstance(svc._stt_local, MockSTT)
    svc._stt_local.queue_text("local guess")
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "local guess"  # cloud timed out -> local provider
    assert len(rec.requests) == 1 and "timed out" in svc.last_stt_error
    ctx.privacy.set(cloud_enabled=False)
    svc._stt_local.queue_text("offline only")
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "offline only"
    assert len(rec.requests) == 1  # no new cloud request once cloud is off
    assert "needs cloud" in svc.status()["stt_provider"]


# ------------------------------------------------------------------ cloud speech: age gate + opt-ins
def _stt_ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"text": "cloud words"})


def _gated_stt_service(make_service, ctx, rec):
    stt = OpenAISTT(api_key=lambda: "sk-test-123", cloud_allowed=lambda: True, transport=rec.transport)
    mic = MockInput()
    svc = make_service(ctx, stt=stt, input=mic)
    return svc, mic


@pytest.mark.parametrize("age, ai_enabled, min_age, why", [
    (12, True, 24, "under 24 months"),  # with a minimum age set, a younger child's voice is never uploaded
    (23, True, 24, "under 24 months"),
    (None, True, 24, "age is not set"),
    (30, False, 0, "ai.enabled"),  # the parent hasn't opted in to cloud AI
    (10, False, 0, "ai.enabled"),
])
async def test_cloud_stt_needs_age_and_ai_opt_in(actx, make_service, age, ai_enabled, min_age, why):
    rec = Recorder(_stt_ok)
    actx.config.ai.min_age_months_for_cloud = min_age
    ctx = _cloud_ctx(actx, cloud=True, age=age, ai_enabled=ai_enabled)
    svc, mic = _gated_stt_service(make_service, ctx, rec)
    svc._stt_local.queue_text("local words")
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "local words"
    assert rec.requests == []  # nothing was uploaded
    assert why in svc.status()["stt_provider"] and svc.status()["stt_provider"].startswith("mock")


@pytest.mark.parametrize("age", [10, None])  # by default any age: the parent's opt-in decides
async def test_cloud_stt_at_any_age_once_the_parent_opts_in(actx, make_service, age):
    rec = Recorder(_stt_ok)
    ctx = _cloud_ctx(actx, cloud=True, age=age, ai_enabled=True)
    svc, mic = _gated_stt_service(make_service, ctx, rec)
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "cloud words" and len(rec.requests) == 1


async def test_cloud_stt_used_once_every_gate_is_open(actx, make_service):
    rec = Recorder(_stt_ok)
    ctx = _cloud_ctx(actx, cloud=True, age=30, ai_enabled=True)
    svc, mic = _gated_stt_service(make_service, ctx, rec)
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "cloud words"
    assert len(rec.requests) == 1 and svc.status()["stt_provider"] == "openai"
    ctx.settings.set("ai.enabled", False)  # switched off again: next listen stays local
    svc._stt_local.queue_text("local again")
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "local again"
    assert len(rec.requests) == 1


async def test_cloud_stt_built_from_config_is_gated(actx, make_service):
    # Provider created by the service itself (audio.stt.provider: openai), not injected.
    rec = Recorder(_stt_ok)
    actx.config.ai.min_age_months_for_cloud = 24
    ctx = _cloud_ctx(actx, cloud=True, age=10, ai_enabled=True)
    ctx.config.audio.stt.provider = "openai"
    mic = MockInput()
    svc = make_service(ctx, stt=None, input=mic)
    assert isinstance(svc._stt, OpenAISTT)
    svc._stt._transport = rec.transport
    assert not svc._stt.available()  # its own gate refuses too
    with pytest.raises(CloudNotAllowed):
        svc._stt.transcribe(_speech(), 16000)
    mic.inject_speech(0.6)
    await svc.listen(timeout=2)
    assert rec.requests == []
    profile = dict(ctx.settings.get("profile")); profile["age_months"] = 30
    ctx.settings.set("profile", profile)
    mic.inject_speech(0.6)
    assert await svc.listen(timeout=2) == "cloud words" and len(rec.requests) == 1


def _tts_service(make_service, ctx, rec):
    tts = OpenAITTS(api_key=lambda: "sk-test-123", cloud_allowed=lambda: True, transport=rec.transport)
    return make_service(ctx, tts=tts)


@pytest.mark.parametrize("age, ai_enabled, min_age", [(12, True, 24), (None, True, 24), (30, False, 0)])
async def test_cloud_tts_needs_age_and_ai_opt_in(actx, make_service, age, ai_enabled, min_age):
    rec = Recorder(lambda req: httpx.Response(200, content=_wav_bytes()))
    actx.config.ai.min_age_months_for_cloud = min_age
    ctx = _cloud_ctx(actx, cloud=True, age=age, ai_enabled=ai_enabled, nickname="Zara")
    svc = _tts_service(make_service, ctx, rec)
    assert await svc.say("Well done, Zara!") is True  # spoken by the local voice
    assert rec.requests == []
    assert "not allowed" in svc.status()["tts_provider"]


def _sent_text(req: httpx.Request) -> str:
    return json.loads(req.content)["input"]


async def test_cloud_tts_never_sends_the_nickname_when_sharing_is_off(actx, make_service):
    rec = Recorder(lambda req: httpx.Response(200, content=_wav_bytes()))
    ctx = _cloud_ctx(actx, cloud=True, nickname="Zara")
    ctx.settings.set("ai.share_child_name", False)
    svc = _tts_service(make_service, ctx, rec)
    assert await svc.say("Well done, Zara! ZARA's turn. zara, clap!") is True
    assert len(rec.requests) == 1
    body = rec.requests[0].content.decode("utf-8")
    assert "zara" not in body.lower()
    assert _sent_text(rec.requests[0]) == "Well done, friend! friend's turn. friend, clap!"
    assert await svc.say("Zarah and Lazarus are other names") is True  # whole words only
    assert _sent_text(rec.requests[1]) == "Zarah and Lazarus are other names"

    ctx.settings.set("ai.share_child_name", True)  # the parent opted in
    assert await svc.say("Well done, Zara!") is True
    assert _sent_text(rec.requests[2]) == "Well done, Zara!"


async def test_cloud_tts_multi_part_nickname_is_scrubbed(actx, make_service):
    rec = Recorder(lambda req: httpx.Response(200, content=_wav_bytes()))
    ctx = _cloud_ctx(actx, cloud=True, nickname="Mary-Jane Rose")
    ctx.settings.set("ai.share_child_name", False)
    svc = _tts_service(make_service, ctx, rec)
    assert await svc.say("Hi Mary-Jane! Rose, Mary-Jane Rose and mary, look!") is True
    sent = _sent_text(rec.requests[0])
    assert sent == "Hi friend! friend, friend and friend, look!"
    for part in ("mary", "jane", "rose"):
        assert part not in sent.lower()


async def test_cloud_tts_built_from_config_is_gated(actx, make_service):
    rec = Recorder(lambda req: httpx.Response(200, content=_wav_bytes()))
    actx.config.ai.min_age_months_for_cloud = 24
    ctx = _cloud_ctx(actx, cloud=True, age=12, nickname="Zara")
    ctx.settings.set("ai.share_child_name", False)
    ctx.config.audio.tts.provider = "openai"
    svc = make_service(ctx, tts=None)
    assert isinstance(svc._tts, OpenAITTS)
    svc._tts._transport = rec.transport
    assert await svc.say("Hello Zara") is True
    assert rec.requests == []  # infant: local voice
    profile = dict(ctx.settings.get("profile")); profile["age_months"] = 36
    ctx.settings.set("profile", profile)
    assert await svc.say("Hello Zara") is True
    assert len(rec.requests) == 1 and _sent_text(rec.requests[0]) == "Hello friend"
