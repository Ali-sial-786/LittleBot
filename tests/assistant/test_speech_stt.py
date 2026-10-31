"""The assistant's speech-to-text (littlebot/assistant/stt): Deepgram Flux over a fake websocket (scripted TurnInfo
messages), OpenAI transcription over httpx.MockTransport, Vosk with a fake recogniser, the chain's choice and
fallback, and that no key ever shows. No network (one test talks to a websocket server on 127.0.0.1)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from littlebot.assistant.speech import SpeechError, STTEvent, STTProvider
from littlebot.assistant.stt import STTChain, build_stt
from littlebot.assistant.stt.deepgram import PACKET_BYTES, DeepgramFlux, websockets_installed
from littlebot.assistant.stt.endpoint import EndpointedSTT, Endpointer
from littlebot.assistant.stt.openai import OpenAITranscriber
from littlebot.assistant.stt.vosk import VoskSTT, model_path_for
from littlebot.testing import make_test_context

RATE = 16000
NO_HTTP = httpx.MockTransport(lambda r: httpx.Response(404))  # never the real network
DG_KEY = "dg_TESTKEY_deepgram_0004"
OA_KEY = "sk-TESTKEY-openai-0005"


def _ctx(cloud: bool = True, deepgram: bool = True, openai: bool = True, **over: Any) -> Any:
    ctx = make_test_context(**over)
    for name in ("cartesia_api_key", "elevenlabs_api_key", "openai_api_key", "deepgram_api_key"):
        setattr(ctx.config, name, None)
    if deepgram:
        ctx.config.deepgram_api_key = SecretStr(DG_KEY)
    if openai:
        ctx.config.openai_api_key = SecretStr(OA_KEY)
    if cloud:
        ctx.privacy.set(cloud_enabled=True)
    return ctx


def tone(ms: float, amp: float = 0.1) -> np.ndarray:
    t = np.arange(int(RATE * ms / 1000)) / RATE
    return (amp * np.sqrt(2) * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def quiet(ms: float) -> np.ndarray:
    return np.zeros(int(RATE * ms / 1000), dtype=np.float32)


class Events(list):
    def __call__(self, event: STTEvent) -> None:
        self.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self]


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class Closed(Exception):
    pass


class FakeWS:
    """A Flux websocket: records what is sent; ``push`` scripts what the server says."""

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    def push(self, **message: Any) -> None:
        self.inbox.put_nowait(json.dumps(message))

    def turn(self, event: str, index: int, transcript: str = "", words: list[tuple[str, float]] = ()) -> None:
        self.push(type="TurnInfo", request_id="r1", sequence_id=1, event=event, turn_index=index,
                  audio_window_start=0.0, audio_window_end=1.0, transcript=transcript,
                  words=[{"word": w, "confidence": c} for w, c in words], end_of_turn_confidence=0.9)

    def drop(self) -> None:
        self.inbox.put_nowait(None)

    async def send(self, data: bytes | str) -> None:
        if self.closed:
            raise Closed("closed")
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("type") == "CloseStream":
            self.inbox.put_nowait(None)  # Flux closes the connection after its last words

    async def recv(self) -> str:
        msg = await self.inbox.get()
        if msg is None:
            raise Closed("connection lost")
        return msg

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)

    def audio(self) -> list[bytes]:
        return [m for m in self.sent if isinstance(m, bytes)]

    def control(self) -> list[dict[str, Any]]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]


class Connector:
    def __init__(self, ws: FakeWS | None = None, error: BaseException | None = None) -> None:
        self.ws = ws or FakeWS()
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeWS:
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.ws


def _flux(connector: Connector, cloud: list[bool] | None = None, **kw: Any) -> DeepgramFlux:
    flag = cloud if cloud is not None else [True]
    return DeepgramFlux(key=lambda: DG_KEY, cloud_ok=lambda: flag[0], ws_connect=connector, **kw)


# ---------------------------------------------------------------- Deepgram Flux
async def test_flux_url_header_and_80ms_packets():
    conn = Connector()
    stream = await _flux(conn, eot_threshold=0.75, eot_timeout_ms=2500).open(Events())
    (url, headers), = conn.calls
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "wss://api.deepgram.com/v2/listen"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {"model": "flux-general-en", "encoding": "linear16", "sample_rate": "16000", "eot_threshold": "0.75",
                 "eot_timeout_ms": "2500", "mip_opt_out": "true"}
    assert headers == {"Authorization": "Token " + DG_KEY}
    audio = tone(500)
    for i in range(0, len(audio), 320):  # 20 ms at a time, as the session sends it
        await stream.feed(audio[i:i + 320])
    packets = conn.ws.audio()
    assert len(packets) == 6 and all(len(p) == PACKET_BYTES == 2560 for p in packets)  # 6 x 80 ms; 20 ms waits
    pcm = np.frombuffer(b"".join(packets), dtype="<i2")
    assert np.allclose(pcm / 32767.0, audio[: len(pcm)], atol=1e-4)
    await stream.aclose()
    assert conn.ws.control()[-1] == {"type": "CloseStream"} and conn.ws.closed


async def test_flux_turn_events():
    conn, events = Connector(), Events()
    stream = await _flux(conn).open(events)
    ws = conn.ws
    ws.push(type="Connected", request_id="r1", sequence_id=0)
    ws.turn("StartOfTurn", 0)
    ws.turn("Update", 0, "what's the")
    ws.turn("Update", 0, "what's the")  # nothing new: no event
    ws.turn("EagerEndOfTurn", 0, "what's the weather")
    ws.turn("TurnResumed", 0, "what's the weather")
    ws.turn("EndOfTurn", 0, "what's the weather today", [("what's", 0.9), ("the", 0.8), ("weather", 1.0),
                                                         ("today", 0.9)])
    await settle()
    assert events == [STTEvent("start_of_turn"), STTEvent("partial", text="what's the"),
                      STTEvent("partial", text="what's the weather"),
                      STTEvent("eager_end_of_turn", text="what's the weather"),
                      STTEvent("turn_resumed", text="what's the weather"),
                      STTEvent("end_of_turn", text="what's the weather today", confidence=0.9)]
    ws.turn("Update", 0, "late words of turn 0")  # an ended turn's stragglers: ignored
    ws.turn("StartOfTurn", 1)
    ws.turn("EndOfTurn", 1, "")  # noise: no words, an empty end (the session stops waiting)
    ws.turn("Update", 2, "and another")  # a turn seen first in an Update still starts
    await settle()
    assert events.kinds()[6:] == ["start_of_turn", "end_of_turn", "start_of_turn", "partial"]
    assert events[7].text == ""
    await stream.aclose()


async def test_flux_says_how_far_behind_it_ended_a_turn(caplog):
    # It works through what it is sent at about real time: the log says how long after their last word it ended the
    # turn and how far behind the audio sent it was (the robot, 2026-10-31: the first turn ended ~2 s late).
    caplog.set_level(logging.INFO, logger="littlebot.assistant")
    conn, events = Connector(), Events()
    stream = await _flux(conn).open(events)
    await stream.feed(np.zeros(1280 * 37 + 500, dtype=np.float32))  # 37 packets (2.96 s) sent, the rest waits
    conn.ws.push(type="TurnInfo", event="EndOfTurn", turn_index=0, transcript="hi there", audio_window_start=0.0,
                 audio_window_end=1.9, words=[{"word": "hi", "start": 0.5, "end": 0.9},
                                              {"word": "there", "start": 0.9, "end": 1.5}])
    await settle()
    assert "deepgram: end of turn 0.40 s after their last word, 1.06 s behind the audio sent" in caplog.text
    await stream.aclose()


async def test_flux_reset_forgets_the_turn_in_progress():
    conn, events = Connector(), Events()
    stream = await _flux(conn).open(events)
    ws = conn.ws
    ws.turn("StartOfTurn", 3)
    ws.turn("Update", 3, "tell me")
    await settle()
    await stream.reset()
    assert ws.control() == [{"type": "ForceEndTurn"}]
    ws.turn("Update", 3, "tell me a")
    ws.turn("EndOfTurn", 3, "tell me a joke")  # the forced end of the forgotten turn
    await settle()
    assert events.kinds() == ["start_of_turn", "partial"]
    ws.turn("StartOfTurn", 4)
    ws.turn("EndOfTurn", 4, "what time is it")
    await settle()
    assert events[-1] == STTEvent("end_of_turn", text="what time is it")
    await stream.reset()  # between turns: nothing to force
    assert ws.control() == [{"type": "ForceEndTurn"}]
    await stream.aclose()


async def test_flux_finish_sends_the_rest_and_ends_the_turn():
    conn = Connector()
    stream = await _flux(conn).open(Events())
    conn.ws.turn("StartOfTurn", 0)
    await settle()
    await stream.feed(tone(100))  # one packet + 20 ms
    await stream.finish()
    assert [len(p) for p in conn.ws.audio()] == [2560, 640]
    assert conn.ws.control() == [{"type": "ForceEndTurn"}]
    await stream.aclose()


async def test_flux_error_message_and_lost_connection():
    conn, events = Connector(), Events()
    stream = await _flux(conn).open(events)
    conn.ws.push(type="Error", sequence_id=3, code="INVALID_AUDIO", description="bad")
    await settle()
    assert events == [STTEvent("error", detail="server_error")]
    await stream.feed(tone(200))
    assert conn.ws.audio() == []  # failed: nothing more is sent
    await stream.aclose()

    conn2, events2 = Connector(), Events()
    stream2 = await _flux(conn2).open(events2)
    conn2.ws.drop()
    await settle()
    assert events2 == [STTEvent("error", detail="network")]
    await stream2.aclose()


async def test_flux_cloud_switched_off_mid_session_stops_sending():
    cloud = [True]
    conn, events = Connector(), Events()
    stream = await _flux(conn, cloud=cloud).open(events)
    await stream.feed(tone(80))
    cloud[0] = False
    await stream.feed(tone(160))
    assert len(conn.ws.audio()) == 1
    assert events == [STTEvent("error", detail="cloud_off")]
    await stream.aclose()


class Rejected(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


@pytest.mark.parametrize(("error", "kind"), [(Rejected(401), "auth"), (Rejected(429), "rate_limit"),
                                             (Rejected(503), "server_error"), (OSError("no route"), "network"),
                                             (TimeoutError(), "timeout")])
async def test_flux_connect_failures(error, kind):
    with pytest.raises(SpeechError) as e:
        await _flux(Connector(error=error)).open(Events())
    assert e.value.kind == kind
    assert DG_KEY not in str(e.value) and DG_KEY not in repr(e.value)


def test_flux_missing():
    conn = Connector()
    assert DeepgramFlux(key=lambda: "", cloud_ok=lambda: True, ws_connect=conn).missing() == "no_key"
    assert DeepgramFlux(key=lambda: "k", cloud_ok=lambda: False, ws_connect=conn).missing() == "cloud_off"
    assert DeepgramFlux(key=lambda: "k", cloud_ok=lambda: True, ws_connect=conn).missing() is None
    assert isinstance(DeepgramFlux(key=lambda: "k", cloud_ok=lambda: True), STTProvider)


@pytest.mark.skipif(not websockets_installed(), reason="websockets is not installed")
async def test_flux_real_websocket_on_localhost_never_logs_the_key(caplog, monkeypatch):
    """The real connector (websockets) against a local server: the key goes in the header, never in a log."""
    from websockets.asyncio.server import serve  # noqa: PLC0415

    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    caplog.set_level(logging.DEBUG)
    seen: dict[str, Any] = {}

    async def handler(ws: Any) -> None:
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["path"] = ws.request.path
        await ws.send(json.dumps({"type": "TurnInfo", "event": "StartOfTurn", "turn_index": 0, "transcript": ""}))
        await ws.send(json.dumps({"type": "TurnInfo", "event": "EndOfTurn", "turn_index": 0, "transcript": "hi there"}))
        async for msg in ws:
            seen.setdefault("got", []).append(msg)
            if isinstance(msg, str) and json.loads(msg).get("type") == "CloseStream":
                break

    server_log = logging.getLogger("test.flux.server")
    server_log.setLevel(logging.INFO)  # the test server's own handshake log would show the header it got
    async with serve(handler, "127.0.0.1", 0, logger=server_log) as server:
        port = server.sockets[0].getsockname()[1]
        events = Events()
        flux = DeepgramFlux(key=lambda: DG_KEY, cloud_ok=lambda: True, url=f"ws://127.0.0.1:{port}/v2/listen")
        stream = await flux.open(events)
        await stream.feed(tone(80))
        for _ in range(100):
            if "end_of_turn" in events.kinds():
                break
            await asyncio.sleep(0.01)
        await stream.aclose()
    assert seen["auth"] == "Token " + DG_KEY
    assert seen["path"].startswith("/v2/listen?model=flux-general-en")
    assert events.kinds() == ["start_of_turn", "end_of_turn"] and events[-1].text == "hi there"
    assert isinstance(seen["got"][0], bytes) and len(seen["got"][0]) == 2560
    assert DG_KEY not in caplog.text


# ---------------------------------------------------------------- OpenAI transcription
async def test_openai_transcription_request():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": " What's the weather? "})

    stt = OpenAITranscriber(key=lambda: OA_KEY, cloud_ok=lambda: True, model="gpt-4o-mini-transcribe",
                            language="en", transport=httpx.MockTransport(handler))
    assert await stt.transcribe(tone(800)) == "What's the weather?"
    (req,) = requests
    assert str(req.url) == "https://api.openai.com/v1/audio/transcriptions"
    assert req.headers["Authorization"] == "Bearer " + OA_KEY
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    for field, value in ((b'name="model"', b"gpt-4o-mini-transcribe"), (b'name="language"', b"en"),
                         (b'name="response_format"', b"json")):
        assert field in body and value in body
    assert b'filename="turn.wav"' in body and b"RIFF" in body and b"WAVE" in body
    assert await stt.transcribe(tone(100)) == ""  # too short: not sent
    assert len(requests) == 1
    await stt.aclose()


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (429, "rate_limit"), (500, "server_error"),
                                              (400, "bad_request")])
async def test_openai_transcription_errors(status, kind, caplog):
    caplog.set_level(logging.DEBUG)
    stt = OpenAITranscriber(key=lambda: OA_KEY, cloud_ok=lambda: True,
                            transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "x"})))
    with pytest.raises(SpeechError) as e:
        await stt.transcribe(tone(800))
    assert e.value.kind == kind
    assert OA_KEY not in str(e.value) + repr(e.value) + caplog.text + repr(stt)


async def test_openai_transcription_needs_cloud_and_key():
    assert OpenAITranscriber(key=lambda: "", cloud_ok=lambda: True).missing() == "no_key"
    stt = OpenAITranscriber(key=lambda: "k", cloud_ok=lambda: False)
    assert stt.missing() == "cloud_off"
    with pytest.raises(SpeechError) as e:
        await stt.transcribe(tone(800))
    assert e.value.kind == "cloud_off"


async def test_openai_through_the_endpointer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "turn on the lights"})

    events = Events()
    provider = EndpointedSTT(OpenAITranscriber(key=lambda: OA_KEY, cloud_ok=lambda: True,
                                               transport=httpx.MockTransport(handler)), end_of_turn_ms=500)
    stream = await provider.open(events)
    assert isinstance(stream, Endpointer)
    for chunk in (quiet(200), tone(700), quiet(600)):
        for i in range(0, len(chunk), 320):
            await stream.feed(chunk[i:i + 320])
    await stream.idle()
    assert events.kinds() == ["start_of_turn", "end_of_turn"] and events[-1].text == "turn on the lights"
    await stream.aclose()


# ---------------------------------------------------------------- Vosk
class FakeRecognizer:
    """KaldiRecognizer's surface: words appear as audio arrives; a long pause makes Vosk close a sentence."""

    def __init__(self, model: Any, rate: float) -> None:
        assert model == "MODEL" and rate == 16000.0
        self.bytes = 0
        self.closed_once = False

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802 - vosk's name
        self.bytes += len(data)
        if self.bytes >= 32000 and not self.closed_once:  # after 1 s: Vosk decided "turn on" was a sentence
            self.closed_once = True
            return True
        return False

    def Result(self) -> str:  # noqa: N802
        return json.dumps({"text": "turn on"})

    def PartialResult(self) -> str:  # noqa: N802
        return json.dumps({"partial": "the" if self.closed_once else "turn"})

    def FinalResult(self) -> str:  # noqa: N802
        return json.dumps({"text": "the lights" if self.closed_once else "turn"})


def _model_dir(tmp_path: Path) -> Path:
    d = tmp_path / "vosk-model"
    (d / "conf").mkdir(parents=True, exist_ok=True)
    (d / "conf" / "model.conf").write_text("--x=1\n")
    return d


async def test_vosk_follows_the_words_through_the_endpointer(tmp_path):
    loads = []
    vosk = VoskSTT(_model_dir(tmp_path), load=lambda p: loads.append(p) or "MODEL", recognizer=FakeRecognizer,
                   installed=lambda: True)
    assert vosk.missing() is None
    events = Events()
    stream = await EndpointedSTT(vosk, end_of_turn_ms=650).open(events)
    assert len(loads) == 1
    for chunk in (tone(1300), quiet(1300)):
        for i in range(0, len(chunk), 320):
            await stream.feed(chunk[i:i + 320])
    await stream.idle()
    partials = [e.text for e in events if e.kind == "partial"]
    assert partials[0] == "turn" and partials[-1] == "turn on the"
    assert events[-1] == STTEvent("end_of_turn", text="turn on the lights")
    assert await vosk.transcribe(tone(500)) == "turn"


def test_vosk_missing_and_model_path(tmp_path):
    assert VoskSTT(None, installed=lambda: True).missing() == "no_model"
    assert VoskSTT(tmp_path, installed=lambda: True).missing() == "no_model"  # not a model
    assert VoskSTT(_model_dir(tmp_path), installed=lambda: False).missing() == "not_installed"
    model = _model_dir(tmp_path)
    ctx = _ctx(audio={"stt": {"vosk_model_path": str(model)}})
    assert model_path_for(ctx.config) == model
    ctx = _ctx(audio={"stt": {"vosk_model_path": str(tmp_path / "nothing")}}, wake={"model_path": str(model)})
    assert model_path_for(ctx.config) == model  # the wake word's model, when the other isn't there


async def test_vosk_model_that_fails_to_load(tmp_path):
    def broken(path: Path) -> Any:
        raise Exception("Failed to create a model")  # noqa: TRY002 - what vosk raises

    vosk = VoskSTT(_model_dir(tmp_path), load=broken, recognizer=FakeRecognizer, installed=lambda: True)
    with pytest.raises(SpeechError) as e:
        await EndpointedSTT(vosk).open(Events())
    assert e.value.kind == "no_model"


# ---------------------------------------------------------------- choosing and falling back
async def test_build_stt_order_and_choice(tmp_path):
    conn = Connector()
    chain = build_stt(_ctx(), ws_connect=conn, transport=NO_HTTP)
    assert isinstance(chain, STTChain) and isinstance(chain, STTProvider)
    assert chain.name == "deepgram" and chain.streaming is True
    assert [p.name for p in chain.providers] == ["deepgram", "openai", "vosk"]
    assert all(p.streaming is False and isinstance(p, EndpointedSTT) for p in chain.providers[1:])
    chain = build_stt(_ctx(deepgram=False), ws_connect=conn, transport=NO_HTTP)
    assert chain is not None and chain.name == "openai" and chain.streaming is False
    nothing = _ctx(cloud=False, audio={"stt": {"vosk_model_path": str(tmp_path / "none")}},
                   wake={"model_path": str(tmp_path / "none")})
    assert build_stt(nothing, ws_connect=conn, transport=NO_HTTP) is None
    with_model = _ctx(cloud=False, audio={"stt": {"vosk_model_path": str(_model_dir(tmp_path))}})
    chain = build_stt(with_model, ws_connect=conn, transport=NO_HTTP)
    if chain is not None:  # vosk is installed here
        assert chain.name == "vosk" and chain.describe()["first"] == "vosk"
    described = build_stt(_ctx(deepgram=False), ws_connect=conn, transport=NO_HTTP).describe()
    rows = {r["name"]: r["missing"] for r in described["providers"]}
    assert rows["deepgram"] == "no_key" and rows["openai"] is None
    assert conn.calls == []  # building never connects


async def test_chain_falls_back_when_deepgram_cannot_connect():
    now = [0.0]
    conn = Connector(error=OSError("unreachable"))
    built = build_stt(_ctx(), ws_connect=conn, transport=NO_HTTP)
    chain = STTChain(built.providers, cloud_ok=lambda: True, clock=lambda: now[0])
    stream = await chain.open(Events())
    assert isinstance(stream, Endpointer) and chain.last_provider == "openai"
    assert chain.describe()["providers"][0]["missing"] == "resting after network"
    assert chain.name == "openai"
    await chain.open(Events())
    assert len(conn.calls) == 1  # resting: not tried again yet
    now[0] = 25.0
    conn.error = None
    stream = await chain.open(Events())
    assert chain.last_provider == "deepgram" and len(conn.calls) == 2
    await stream.aclose()


async def test_an_error_event_makes_the_next_open_use_the_next_provider():
    conn = Connector()
    built = build_stt(_ctx(), ws_connect=conn, transport=NO_HTTP)
    chain = STTChain(built.providers, cloud_ok=lambda: True)
    events = Events()
    stream = await chain.open(events)
    conn.ws.drop()
    await settle()
    assert events == [STTEvent("error", detail="network")]
    await stream.aclose()
    await chain.open(Events())
    assert chain.last_provider == "openai"


async def test_cloud_off_skips_the_cloud_providers(tmp_path):
    ctx = _ctx(audio={"stt": {"vosk_model_path": str(_model_dir(tmp_path))}})
    chain = build_stt(ctx, ws_connect=Connector(), transport=NO_HTTP)
    assert chain is not None and chain.name == "deepgram"
    ctx.privacy.set(cloud_enabled=False)
    assert chain.name == "vosk"
    rows = {r["name"]: r["missing"] for r in chain.describe()["providers"]}
    assert rows["deepgram"] == rows["openai"] == "cloud_off"


async def test_no_key_in_stt_logs_or_descriptions(caplog):
    caplog.set_level(logging.DEBUG)
    conn = Connector(error=Rejected(401))
    ctx = _ctx()
    chain = build_stt(ctx, ws_connect=conn, transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    stream = await chain.open(Events())  # deepgram refused: openai opens
    events = Events()
    stream._on_event = events  # noqa: SLF001
    for chunk in (tone(700), quiet(900)):
        await stream.feed(chunk)
    await stream.idle()
    assert events.kinds() == ["start_of_turn", "error"] and events[-1].detail == "auth"
    text = caplog.text + json.dumps(chain.describe()) + "".join(repr(p) for p in chain.providers)
    assert DG_KEY not in text and OA_KEY not in text
