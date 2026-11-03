"""The jokes LittleBot told lately (littlebot/assistant/jokes.py)."""

from __future__ import annotations

from pathlib import Path

from littlebot.assistant.jokes import KEEP, KEEP_DAYS, JokesTold


def test_only_joke_turns_are_kept_and_named(tmp_path: Path) -> None:
    now = [1_000_000.0]
    jokes = JokesTold(tmp_path / "jokes_told.json", now=lambda: now[0])
    jokes.told("what's the capital of France", "Paris.")
    assert jokes.note("tell me a joke") == ""  # nothing told yet
    jokes.told("tell me a joke", "Why did the robot go on a diet? It had too many bytes!")
    assert "too many bytes" in jokes.note("can you tell me another joke?")
    assert jokes.note("what's the weather") == ""
    again = JokesTold(tmp_path / "jokes_told.json", now=lambda: now[0])  # kept across a restart
    assert "too many bytes" in again.note("knock knock")
    now[0] += KEEP_DAYS * 86400 + 1
    assert again.note("a funny one please") == ""  # too old


def test_the_list_stays_short_and_a_bad_file_starts_a_new_one(tmp_path: Path) -> None:
    path = tmp_path / "jokes_told.json"
    jokes = JokesTold(path)
    for i in range(KEEP + 5):
        jokes.told("a joke please", f"joke {i}")
    assert len(JokesTold(path)._items) == KEEP
    path.write_text("{not json", encoding="utf-8")
    assert JokesTold(path).note("joke") == ""
    assert JokesTold(None).note("joke") == ""  # nowhere to keep them
