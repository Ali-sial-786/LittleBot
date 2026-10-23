"""GPT-Live data-channel events (quicksilver), same shapes as WatchGPT's QuicksilverWireTest."""

from __future__ import annotations

import json

from littlebot.chatgpt import wire


def test_parse_transcripts_and_turns():
    assert wire.parse('{"type":"input_transcript.added","item":{"text":"hi"}}') == (
        "input_transcript.added", wire.UserText("hi", False))
    assert wire.parse('{"type":"output_transcript.added","item":{"text":"Moo"}}')[1] == wire.AssistantText("Moo", False)
    assert wire.parse('{"type":"turn.done","turn":{"role":"user","transcript":"hi there"}}')[1] == wire.UserText("hi there", True)
    assert wire.parse('{"type":"turn.done","turn":{"role":"assistant","transcript":"Moo!"}}')[1] == wire.AssistantText("Moo!", True)


def test_parse_delegation_only_for_the_client():
    ev = {"type": "delegation.created", "item": {"id": "d1", "type": "delegation", "target": "client",
                                                 "content": [{"type": "input_text", "text": "What "}, {"type": "input_text", "text": "time?"}]}}
    assert wire.parse(json.dumps(ev))[1] == wire.Delegation("d1", "What time?")
    ev["item"]["target"] = "server"
    assert wire.parse(json.dumps(ev))[1] == wire.Other()


def test_parse_errors_and_close_and_junk():
    assert wire.parse('{"type":"error","error":{"code":"x"}}')[1] == wire.Failure("x")
    assert wire.parse('{"type":"session.closed","reason":"close_requested"}')[1] == wire.Closed("close_requested")
    assert wire.parse("not json") is None
    assert wire.parse('{"no":"type"}') is None
    assert wire.parse('{"type":"something.new"}') == ("something.new", wire.Other())


def test_result_frames_are_bounded_and_chunked():
    frames = [json.loads(f) for f in wire.result_frames("d1", "é" * 900)]
    assert all(f["type"] == "delegation.context.append" and f["delegation_item_id"] == "d1" and f["channel"] == "speakable"
               for f in frames)
    assert all(len(f["content"][0]["text"].encode()) <= wire.MAX_FRAME_BYTES for f in frames)
    assert "".join(f["content"][0]["text"] for f in frames) == "é" * 900
    long = "".join(json.loads(f)["content"][0]["text"] for f in wire.result_frames("d1", "a" * 5000))
    assert len(long) <= wire.MAX_RESULT_CHARS and long.endswith("[truncated]")
    note = json.loads(wire.note_frames("bye")[0])
    assert note == {"type": "session.context.append", "channel": "speakable", "content": [{"type": "input_text", "text": "bye"}]}
