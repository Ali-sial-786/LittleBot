"""scripts/log_timeline.py: a readable timeline from the robot's JSON log."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _script():
    spec = importlib.util.spec_from_file_location("log_timeline", ROOT / "scripts" / "log_timeline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _line(ts: str, msg: str, level: str = "INFO", logger: str = "littlebot.chatgpt.live", **extra) -> bytes:
    return (json.dumps({"ts": f"2026-10-24T{ts}.000", "level": level, "logger": logger, "msg": msg, **extra})
            + "\n").encode()


def test_the_timeline_marks_gaps_folds_voice_turns_and_skips_damaged_lines(tmp_path, capsys):
    log = tmp_path / "littlebot.log"
    (tmp_path / "littlebot.log.1").write_bytes(_line("20:00:00", "LittleBot 0.1 started in pi mode",
                                                     logger="littlebot.app.robot"))
    log.write_bytes(b"".join([
        _line("21:00:00", "wake word heard (phrase=hey littlebot)", logger="littlebot.audio.wakeword"),
        _line("21:00:01", "live voice: parent started a conversation (limit 10 min)"),
        _line("21:00:03", "state LISTENING -> SPEAKING (live voice)", logger="littlebot.core.state"),
        _line("21:00:05", "state SPEAKING -> LISTENING (live voice)", logger="littlebot.core.state"),
        b"\x00" * 40 + b"\n",
        _line("21:00:06", 'HTTP Request: POST https://x "HTTP/1.1 200 OK"', logger="httpx"),
        _line("21:03:10", "the main loop has been stuck for 2 s; it is busy in: faces.py:630 run",
              level="WARNING", logger="littlebot.core.stall_watch"),
        _line("21:03:11", "live voice failed", level="ERROR", exc="Traceback\n  File x\nValueError: boom"),
        _line("21:03:12", "live voice: ending (connection lost)"),
    ]))
    mod = _script()
    assert mod.main(["--log", str(log), "--since", "20:30"]) == 0
    out = capsys.readouterr().out
    assert "21:00:00       wake word heard (phrase=hey littlebot)" in out
    assert "(the voice took 2 turns of talking and listening)" in out
    assert "nothing logged for 3 min 4 s (DURING A VOICE CHAT)" in out
    assert "WARN  the main loop has been stuck" in out and "ERROR live voice failed" in out
    assert "| ValueError: boom" in out
    assert "HTTP Request" not in out and "started in pi mode" not in out  # noisy, and before --since
    assert "voice chats started: 1 (ended: 1 x connection lost)" in out and "main loop stuck: 1 time(s)" in out
    assert "== The Pi now ==" in out
    assert mod.main(["--log", str(log), "--minutes", "600", "--all"]) == 0
    assert "started in pi mode" in capsys.readouterr().out  # the rotated file is read too, oldest first
