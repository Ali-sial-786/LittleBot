"""scripts/setup_calendar.py and scripts/setup_phone.py with --person: the name is looked up on Who's who (read-only),
and the person's own files are used (data/google_calendar_p<id>.json, calendar_ics_url_p<id>, ntfy_topic_p<id>)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import setup_calendar, setup_phone


@pytest.fixture
def data(tmp_path: Path) -> Path:
    con = sqlite3.connect(tmp_path / "littlebot.db")
    con.execute("CREATE TABLE face_people (id INTEGER PRIMARY KEY, name TEXT, role TEXT, added_at REAL)")
    con.executemany("INSERT INTO face_people (id, name, role) VALUES (?, ?, ?)", [(1, "Ali", "dad"), (2, "Sara", "mom")])
    con.commit()
    con.close()
    return tmp_path


def test_a_name_on_whos_who(data, capsys) -> None:
    assert setup_calendar.person_by_name(data, " sara ") == (2, "Sara")
    with pytest.raises(SystemExit):
        setup_calendar.person_by_name(data, "Bob")
    assert "On it: Ali, Sara" in capsys.readouterr().out


def test_a_persons_own_calendar_files(data, monkeypatch, capsys) -> None:
    monkeypatch.setattr(setup_calendar, "data_dir", lambda: data)
    (data / "calendar_ics_url_p2").write_text("https://example.com/a.ics\n", encoding="utf-8")
    assert setup_calendar.main(["--person", "Sara", "--check"]) == 0
    out = capsys.readouterr().out
    assert "Sara's own calendar" in out and "not connected" in out and "iCal addresses: 1" in out
    assert setup_calendar.main(["--person", "Sara", "--remove-ics"]) == 0
    assert not (data / "calendar_ics_url_p2").exists()


def test_a_persons_work_and_personal_ical_addresses(data, monkeypatch, capsys) -> None:
    import httpx

    feed = ("BEGIN:VCALENDAR\nX-WR-CALNAME:ali@gmail.com\nBEGIN:VEVENT\nUID:a\nDTSTART:20991102T130000Z\n"
            "SUMMARY:Thing\nEND:VEVENT\nEND:VCALENDAR\n")
    monkeypatch.setattr(setup_calendar, "data_dir", lambda: data)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text=feed, request=httpx.Request("GET", a[0])))
    answers = iter(["https://example.com/work.ics", "Work", "webcal://example.com/home.ics", "<bad>", "Personal"])
    monkeypatch.setattr(setup_calendar, "ask", lambda *a, **k: next(answers))
    assert setup_calendar.main(["--person", "Ali", "--ics"]) == 0
    assert setup_calendar.main(["--person", "Ali", "--ics"]) == 0  # the second one is added, the first kept
    path = data / "calendar_ics_url_p1"
    assert path.read_text().strip() == "https://example.com/work.ics Work https://example.com/home.ics Personal"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    out = capsys.readouterr().out
    assert "can only have letters" in out and "2 addresses" in out
    assert setup_calendar.main(["--person", "Ali", "--check"]) == 0
    assert "iCal addresses: 2 (Work, Personal)" in capsys.readouterr().out


def test_a_persons_own_phone(data, monkeypatch, capsys) -> None:
    cfg = SimpleNamespace(data_path=data, skills=SimpleNamespace(ntfy_server=""))
    monkeypatch.setattr(setup_phone, "config", lambda: cfg)
    sent: list[str] = []
    monkeypatch.setattr(setup_phone, "send_test", lambda server, topic: sent.append(topic) or True)
    assert setup_phone.main(["--person", "ali", "--test"]) == 0
    topic = (data / "ntfy_topic_p1").read_text(encoding="utf-8").strip()
    assert topic.startswith("littlebot-") and sent == [topic] and not (data / "ntfy_topic").exists()
    assert setup_phone.main(["--person", "Ali", "--remove"]) == 0
    assert not (data / "ntfy_topic_p1").exists()
