"""Recording LittleBot's lines in the GPT-Live voice (littlebot.chatgpt.voice_record) against a local
WebRTC peer standing in for OpenAI: many lines per call, transcripts checked, mismatches asked again."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
import numpy as np
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeOpenAI, signed_in_auth  # noqa: E402

import littlebot.chatgpt.voice_record as vr  # noqa: E402
from littlebot.app.spoken_lines import SpokenLine  # noqa: E402
from littlebot.audio.voice_lines import VoiceLineStore, line_hash  # noqa: E402
from littlebot.testing import make_test_config  # noqa: E402

pytestmark = pytest.mark.timeout(90)

LINES = ["Tap again, and I'll do my trick!", "Boing!", "Boing! Bye-bye for now!"]


class Server(FakeOpenAI):
    """FakeOpenAI that keeps every call it answered (the recorder makes a new one every N lines)."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.pcs: list[Any] = []
        self.voice_quiet = True  # the "voice" (a tone) only sounds while it says a line

    async def __call__(self, request: Any) -> Any:
        resp = await super().__call__(request)
        if self.pc is not None and self.pc not in self.pcs:
            self.pcs.append(self.pc)
        return resp

    async def close(self) -> None:
        for pc in self.pcs:
            await pc.close()
        await super().close()


def asks(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


async def voice(server: Server, says: Callable[[str, int], str], speak: float = 0.3) -> None:
    """The fake voice: for each line asked, a tone for ``speak`` s, then its turn ends with ``says(line, try)``."""
    await server.channel_ready.wait()
    handled = 0
    tries: dict[str, int] = {}
    while True:
        notes = asks(server)
        if len(notes) > handled:
            line = notes[handled].split("nothing else: ", 1)[1]
            handled += 1
            tries[line] = tries.get(line, 0) + 1
            server.voice_quiet = False
            await asyncio.sleep(speak)
            server.voice_quiet = True
            server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": says(line, tries[line])}})
        await asyncio.sleep(0.02)


@pytest.fixture(autouse=True)
def quick_tail(monkeypatch):
    monkeypatch.setattr(vr, "TAIL_QUIET_S", 0.25)


async def record(tmp_path: Any, server: Server, lines: list[str], says: Callable[[str, int], str], **kw: Any):
    helper = asyncio.ensure_future(voice(server, says))
    try:
        return await vr.record_lines(signed_in_auth(tmp_path), make_test_config().chatgpt, lines,
                                     transport=httpx.MockTransport(server), **kw)
    finally:
        helper.cancel()
        await server.close()


def test_similarity_ignores_case_punctuation_spelling_and_numbers():
    assert vr.similarity("Boing!", "boing") == 1.0
    assert vr.similarity("Mooooo, moo!", "Moo! Moo!") == 1.0  # a long "moo" is still a moo
    assert vr.similarity("One!", "1!") == 1.0
    assert vr.similarity("Peek-a-boo! I see you!", "Peekaboo! I see you!") == 1.0
    assert vr.similarity("Okay! Boing!", "Boing!") < vr.MATCH  # it said more than the line
    assert vr.similarity("Boing, boing!", "Boing!") < vr.MATCH
    assert vr.similarity("", "Boing!") == 0.0


async def test_many_lines_in_one_call_each_checked_and_a_mismatch_asked_again(tmp_path):
    server = Server(reject_voices=("bubbie",))
    said: list[str] = []

    def says(line: str, attempt: int) -> str:
        said.append(line)
        return "Okay! Boing boing!" if line == "Boing!" and attempt == 1 else line  # wrong the first time

    takes = await record(tmp_path, server, LINES, says)
    assert len(server.requests) == 2  # one call (bubbie refused, then sol)
    body = server.requests[-1]["body"]["session"]
    assert "say exactly that line out loud, word for word, and nothing else" in body["instructions"]
    assert asks(server) == [f"Say exactly, word for word, nothing else: {line}" for line in
                            [LINES[0], LINES[1], LINES[1], LINES[2]]]
    assert [t.line for t in takes] == LINES and all(t.ok for t in takes)
    assert [t.tries for t in takes] == [1, 2, 1] and {t.voice for t in takes} == {"sol"}
    for t in takes:
        seconds = len(t.audio) / vr.OUT_RATE
        assert 0.25 < seconds < 0.9, seconds  # only the voice's own sound for that line (0.3 s), trimmed
        assert 0.55 < float(np.abs(t.audio).max()) <= 0.61


async def test_a_line_that_never_matches_is_reported_and_not_saved(tmp_path):
    server = Server()
    store = VoiceLineStore(tmp_path / "voice_lines")
    lines = [SpokenLine(LINES[0], "activity:again_again"), SpokenLine("Moo! Moo!", "activity:animal_sounds")]
    job = vr.VoiceLineJob(store, signed_in_auth(tmp_path), make_test_config().chatgpt, lines,
                          transport=httpx.MockTransport(server), retries=1)
    progress: list[tuple[int, bool]] = []
    job.on_line = lambda i, total, line, take, saved: progress.append((i, saved))
    helper = asyncio.ensure_future(voice(server, lambda line, n: "Quack!" if line.startswith("Moo") else line))
    try:
        status = await job.run()
    finally:
        helper.cancel()
        await server.close()
    assert status["phase"] == "done" and status["done"] == 2 and status["saved"] == 1
    assert status["failed"] == [{"text": "Moo! Moo!", "said": "Quack!"}] and progress == [(0, True), (1, False)]
    assert len(asks(server)) == 3  # Moo asked twice (retries=1)
    assert store.recorded([LINES[0], "Moo! Moo!"]) == {line_hash(LINES[0])}
    entry = store.entry(LINES[0])
    assert entry["voice"] == "bubbie" and entry["source"] == "activity:again_again" and entry["text"] == LINES[0]


async def test_a_new_call_every_few_lines(tmp_path):
    server = Server()
    takes = await record(tmp_path, server, LINES, lambda line, n: line, lines_per_call=2)
    assert all(t.ok for t in takes) and len(server.requests) == 2  # 2 lines, then a new call for the third


async def test_stop_ends_after_the_current_line(tmp_path):
    server = Server()
    stop = asyncio.Event()

    def on_take(i: int, total: int, take: vr.Take) -> None:
        stop.set()

    takes = await record(tmp_path, server, LINES, lambda line, n: line, on_take=on_take, stop=stop)
    assert [t.line for t in takes] == LINES[:1]


async def test_a_refused_call_is_reported(tmp_path):
    server = Server(status=403)
    with pytest.raises(vr.VoiceCallError, match="HTTP 403"):
        await record(tmp_path, server, LINES, lambda line, n: line)
