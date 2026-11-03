"""AssemblyAI Universal-3.6 Pro speech-to-text (littlebot/assistant/stt/assemblyai.py), with a fake websocket."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np

from littlebot.assistant.speech import SpeechError, STTEvent
from littlebot.assistant.stt.assemblyai import PACKET_BYTES, AssemblyAIUniversal, error_kind

KEY = "test-assemblyai-key-never-shown"


class Closed(Exception):
    pass


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    def turn(self, order: int, transcript: str = "", end: bool = False,
             words: list[tuple[str, float]] = ()) -> None:
        self.inbox.put_nowait(json.dumps({
            "type": "Turn", "turn_order": order, "turn_is_formatted": end, "end_of_turn": end,
            "transcript": transcript, "end_of_turn_confidence": 0.9,
            "words": [{"text": w, "confidence": c, "start": 0, "end": 1, "word_is_final": end} for w, c in words]}))

    async def send(self, data: bytes | str) -> None:
        if self.closed:
            raise Closed("closed")
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("type") == "Terminate":
            self.inbox.put_nowait(json.dumps({"type": "Termination", "audio_duration_seconds": 1}))
            self.inbox.put_nowait(None)

    async def recv(self) -> str:
        msg = await self.inbox.get()
        if msg is None:
            raise Closed("connection lost")
        return msg

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)

    def control(self) -> list[dict[str, Any]]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]


class Connector:
    def __init__(self, error: BaseException | None = None) -> None:
        self.ws = FakeWS()
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeWS:
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.ws


class Events(list):
    def __call__(self, event: STTEvent) -> None:
        self.append(event)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _aai(conn: Connector, cloud: list[bool] | None = None, **kw: Any) -> AssemblyAIUniversal:
    flag = cloud if cloud is not None else [True]
    return AssemblyAIUniversal(key=lambda: KEY, cloud_ok=lambda: flag[0], ws_connect=conn, **kw)


async def test_url_header_keyterms_and_100ms_packets():
    conn = Connector()
    long_name = "A playlist name that is far too long for AssemblyAI"  # over its 50 characters... and fit's 40
    stream = await _aai(conn, max_turn_silence_ms=3000,
                        keyterms=lambda: ["LittleBot", "Amara", "amara", long_name]).open(Events())
    (url, headers), = conn.calls
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "wss://streaming.assemblyai.com/v3/ws"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {"speech_model": "universal-3-6-pro", "encoding": "pcm_s16le", "sample_rate": "16000",
                 "max_turn_silence": "3000", "keyterms_prompt": '["LittleBot", "Amara"]'}
    assert headers == {"Authorization": KEY}  # no "Bearer"
    assert KEY not in repr(_aai(conn)) and KEY not in url
    audio = np.full(4000, 0.25, dtype=np.float32)  # 250 ms
    for i in range(0, len(audio), 320):
        await stream.feed(audio[i:i + 320])
    packets = [m for m in conn.ws.sent if isinstance(m, bytes)]
    assert len(packets) == 2 and all(len(p) == PACKET_BYTES == 3200 for p in packets)
    await stream.set_keyterms(["LittleBot", "Amara"])  # the same: nothing sent
    await stream.set_keyterms(["LittleBot", "Great Wall"])
    assert conn.ws.control() == [{"type": "UpdateConfiguration", "keyterms_prompt": ["LittleBot", "Great Wall"]}]
    await stream.aclose()
    assert conn.ws.control()[-1] == {"type": "Terminate"} and conn.ws.closed
    assert "keyterms_prompt" not in _aai(conn).stream_url([])  # none: not sent at all


def test_its_own_turn_wait_early_words_and_words_while_talking():
    url = _aai(Connector(), max_turn_silence_ms=1280, interruption_delay_ms=0, continuous_partials=True).stream_url([])
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["max_turn_silence"] == "1280" and q["interruption_delay"] == "0" and q["continuous_partials"] == "true"
    q = {k: v[0] for k, v in parse_qs(urlsplit(_aai(Connector()).stream_url([])).query).items()}
    assert not {"max_turn_silence", "interruption_delay", "continuous_partials"} & set(q)  # AssemblyAI's defaults


def test_the_config_gives_assemblyai_its_own_settings():
    from littlebot.assistant.stt import build_stt
    from littlebot.testing import make_test_context

    ctx = make_test_context(assistant={"stt": ["assemblyai", "vosk"], "deepgram_eot_timeout_ms": 3000})
    try:
        ctx.privacy.set(cloud_enabled=True)
        from pydantic import SecretStr

        ctx.config.assemblyai_api_key = SecretStr(KEY)
        aai = build_stt(ctx, ws_connect=Connector()).providers[0]
        assert (aai.max_turn_silence_ms, aai.interruption_delay_ms, aai.continuous_partials) == (750, 0, True)
    finally:
        ctx.db.close()


async def test_speech_started_starts_the_turn_once():
    conn, events = Connector(), Events()
    stream = await _aai(conn).open(events)
    ws = conn.ws
    ws.inbox.put_nowait(json.dumps({"type": "SpeechStarted", "timestamp": 120, "confidence": 0.9}))
    ws.turn(0, "tell")
    ws.turn(0, "Tell me.", end=True)
    ws.turn(1, "and")  # no SpeechStarted this time: the first Turn starts it
    await settle()
    assert [e.kind for e in events] == ["start_of_turn", "partial", "end_of_turn", "start_of_turn", "partial"]
    ws.inbox.put_nowait(json.dumps({"type": "SpeechStarted", "timestamp": 900}))  # mid-turn: nothing new
    await settle()
    assert len(events) == 5
    await stream.aclose()


async def test_turn_messages_as_flux_gives_them():
    conn, events = Connector(), Events()
    stream = await _aai(conn).open(events)
    ws = conn.ws
    ws.inbox.put_nowait(json.dumps({"type": "Begin", "id": "s1", "expires_at": 0,
                                    "configuration": {"model": "universal-3-6-pro"}}))
    ws.inbox.put_nowait(json.dumps({"type": "SpeechStarted", "timestamp": 120, "confidence": 0.9}))
    ws.turn(0, "play eighty")
    ws.turn(0, "play eighty")  # nothing new: no event
    ws.turn(0, "Play eighty music.", end=True, words=[("Play", 0.95), ("eighty", 0.41), ("music.", 0.93)])
    await settle()
    assert events == [STTEvent("start_of_turn"), STTEvent("partial", text="play eighty"),
                      STTEvent("end_of_turn", text="Play eighty music.", confidence=0.763, unsure=("eighty",))]
    ws.turn(0, "Play eighty music.", end=True)  # a repeat of an ended turn: ignored
    ws.turn(1, "", end=True)  # a cough: an empty end, so the session stops waiting
    await settle()
    assert [e.kind for e in events][3:] == ["start_of_turn", "end_of_turn"] and events[-1].text == ""
    await stream.aclose()


async def test_words_ended_mid_sentence_are_marked_cut_off():
    # "So then what did they" (max_turn_silence ended it: no full stop; the robot, 2026-11-02)
    conn, events = Connector(), Events()
    stream = await _aai(conn).open(events)
    for order, text in enumerate(["So then what did they", "What the fuck? I asked you to", "Use it for?",
                                  'He said "go."', "Okay."]):
        conn.ws.turn(order, text, end=True)
    await settle()
    ends = [e for e in events if e.kind == "end_of_turn"]
    assert [e.cut_off for e in ends] == [True, True, False, False, False]
    await stream.aclose()


async def test_finish_forces_the_end_and_reset_forgets_the_turn():
    conn, events = Connector(), Events()
    stream = await _aai(conn).open(events)
    ws = conn.ws
    await stream.finish()  # no turn yet: nothing to end
    assert ws.control() == []
    ws.turn(0, "tell me a very long")
    await settle()
    await stream.feed(np.full(1000, 0.1, dtype=np.float32))  # 62.5 ms left over: goes before the end
    await stream.finish()  # the session's max_turn_seconds
    assert isinstance(ws.sent[-2], bytes) and len(ws.sent[-2]) == 2000
    assert ws.control() == [{"type": "ForceEndpoint"}]
    ws.turn(0, "Tell me a very long story.", end=True)
    ws.turn(1, "um")
    await settle()
    assert [e.kind for e in events] == ["start_of_turn", "partial", "end_of_turn", "start_of_turn", "partial"]
    await stream.reset()  # LittleBot answered: the rest of that turn is forgotten
    assert ws.control()[-1] == {"type": "ForceEndpoint"}
    ws.turn(1, "Um, okay.", end=True)
    ws.turn(2, "next")
    await settle()
    assert [e.kind for e in events][5:] == ["start_of_turn", "partial"] and events[-1].text == "next"
    await stream.aclose()


def test_error_kinds():
    assert error_kind(1008, "Unauthorized Connection: Invalid API key") == "auth"
    assert error_kind(1008, "Unauthorized Connection: Insufficient funds") == "quota"
    assert error_kind(1008, "Unauthorized Connection: Too many concurrent sessions") == "rate_limit"
    assert error_kind(3007, "Input duration violation: 20 ms. Expected between 50 and 1000 ms") == "bad_request"
    assert error_kind(3008, "Session Expired: Maximum session duration exceeded") == "closed"
    assert error_kind(3005, "Session Cancelled: An error occurred") == "server_error"
    assert error_kind(None, "") == "server_error"


async def test_errors_and_gates():
    conn, events = Connector(), Events()
    stream = await _aai(conn).open(events)
    conn.ws.inbox.put_nowait(json.dumps({"type": "Error", "error_code": 1008,
                                         "error": "Unauthorized Connection: Invalid API key"}))
    await settle()
    assert events == [STTEvent("error", detail="auth")]
    await stream.feed(np.full(3200, 0.1, dtype=np.float32))  # failed: nothing more goes out
    assert not [m for m in conn.ws.sent if isinstance(m, bytes)]
    await stream.aclose()
    cloud = [True]
    aai = _aai(Connector(), cloud)
    assert aai.missing() is None
    cloud[0] = False
    assert aai.missing() == "cloud_off"
    assert AssemblyAIUniversal(key=lambda: "", cloud_ok=lambda: True, ws_connect=Connector()).missing() == "no_key"
    try:
        await _aai(Connector(error=TimeoutError())).open(Events())
    except SpeechError as e:
        assert e.kind == "timeout"
    else:
        raise AssertionError("expected SpeechError")


async def test_cloud_switched_off_mid_session_stops_sending():
    conn, events, cloud = Connector(), Events(), [True]
    stream = await _aai(conn, cloud).open(events)
    cloud[0] = False
    await stream.feed(np.full(3200, 0.1, dtype=np.float32))
    assert events == [STTEvent("error", detail="cloud_off")] and conn.ws.sent == []
    await stream.aclose()
