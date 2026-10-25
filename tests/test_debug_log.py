"""LittleBot's own debug log read back for the AI (littlebot/core/debug_log.py, the read_debug_log tool)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

from littlebot.chatgpt.tools import RobotToolbox
from littlebot.core import debug_log

NOW = dt.datetime(2026, 10, 24, 21, 10, 0)


def _line(ts: str, msg: str, level: str = "INFO", logger: str = "littlebot.chatgpt.live", **extra) -> bytes:
    return (json.dumps({"ts": f"2026-10-24T{ts}.000", "level": level, "logger": logger, "msg": msg, **extra})
            + "\n").encode()


def _config(folder: Path) -> SimpleNamespace:
    return SimpleNamespace(data_path=folder, logging=SimpleNamespace(file="logs/littlebot.log"))


def _write(folder: Path) -> None:
    logs = folder / "logs"
    logs.mkdir(parents=True)
    (logs / "littlebot.log.1").write_bytes(_line("20:58:00", "wake word heard (phrase=hey littlebot)",
                                                 logger="littlebot.audio.wakeword"))
    (logs / "littlebot.log").write_bytes(b"".join([
        _line("20:30:00", "too old to show"),
        _line("21:00:00", "wake word heard (phrase=hey littlebot)", logger="littlebot.audio.wakeword"),
        _line("21:00:01", "live voice: parent started a conversation (limit 20 min)"),
        _line("21:00:03", "state LISTENING -> SPEAKING (live voice)", logger="littlebot.core.state"),
        _line("21:00:05", "state SPEAKING -> LISTENING (live voice)", logger="littlebot.core.state"),
        b"\x00" * 30 + b"{broken\n",
        _line("21:00:06", 'HTTP Request: POST https://x "HTTP/1.1 200 OK"', logger="httpx"),
        _line("21:00:07", "screen director: no card (unknown card 'movie')", logger="littlebot.screens.director"),
        _line("21:03:10", "live voice: call failed (HTTP 429)", level="WARNING"),
        _line("21:03:11", "live voice failed", level="ERROR", exc="Traceback\n  File x\nValueError: boom"),
        _line("21:03:12", "live voice: ending (connection lost)"),
    ]))


def test_the_last_minutes_as_a_timeline_with_what_lines_mean(tmp_path: Path) -> None:
    _write(tmp_path)
    result = debug_log.read(_config(tmp_path), minutes=15, now=NOW)
    assert result["ok"] and result["summary"] == {"voice_chats": 1, "chat_endings": {"connection lost": 1},
                                                  "wake_words": 2, "warnings": 1, "errors": 1}
    text = debug_log.as_text(result)
    assert "too old to show" not in text and "HTTP Request" not in text and "{broken" not in text
    assert "20:58:00" in text  # from the rotated copy
    assert "(the voice took 2 turns of talking and listening)" in text
    assert "nothing logged for 183 s" in text
    assert "21:03:10 WARN  live voice: call failed (HTTP 429)" in text and "| ValueError: boom" in text
    assert "usage limit" in text and "kind of info card it doesn't have" in text  # the hints


def test_about_keeps_matching_lines_and_every_warning(tmp_path: Path) -> None:
    _write(tmp_path)
    lines = debug_log.read(_config(tmp_path), minutes=15, about="wake word", now=NOW)["lines"]
    assert any("wake word heard" in ln for ln in lines) and any("HTTP 429" in ln for ln in lines)
    assert not any("parent started" in ln for ln in lines)


def test_a_decisions_context_never_reaches_the_text_model(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "littlebot.log").write_bytes(_line(
        "21:05:00", "decide: answered (shadow): LittleBot's way answer, openai stalling, different | they_asked: "
        "where is my secret diary | camera_sees: Ali (dad)", logger="littlebot.decide"))
    text = debug_log.as_text(debug_log.read(_config(tmp_path), minutes=15, now=NOW))
    assert "decide: answered (shadow): LittleBot's way answer, openai stalling, different" in text
    assert "diary" not in text and "Ali" not in text


def test_no_log_file(tmp_path: Path) -> None:
    result = debug_log.read(_config(tmp_path), now=NOW)
    assert result["ok"] is False and debug_log.as_text(result).startswith("error:")


def test_long_logs_keep_the_newest_lines(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "littlebot.log").write_bytes(b"".join(
        _line(f"21:0{i // 60}:{i % 60:02d}", f"event number {i}") for i in range(400)))
    result = debug_log.read(_config(tmp_path), minutes=15, now=NOW)
    assert result["truncated"] and len(result["lines"]) <= debug_log.MAX_LINES
    assert "event number 399" in result["lines"][-1]
    assert len(debug_log.as_text(result)) < debug_log.MAX_CHARS + 1000


async def test_the_tool_reads_it(ctx, tmp_path: Path) -> None:
    ctx.config.data_dir = str(tmp_path)
    ctx.config.logging.file = "logs/littlebot.log"
    logs = tmp_path / "logs"
    logs.mkdir()
    stamp = dt.datetime.now().isoformat(timespec="milliseconds")
    (logs / "littlebot.log").write_text(json.dumps({"ts": stamp, "level": "WARNING", "logger": "x",
                                                    "msg": "camera lost: unplugged; retrying every 2s"}) + "\n")
    box = RobotToolbox(ctx, mode="live")
    assert "read_debug_log" in {s["name"] for s in box.specs()}
    text = await box.run("read_debug_log", json.dumps({"minutes": 5, "about": "camera"}))
    assert "camera lost" in text and "loose USB cable" in text
