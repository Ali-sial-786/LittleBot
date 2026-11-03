"""LittleBot's last sign of life: a power cut told from a clean stop (littlebot/power/last_seen.py)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from littlebot.power.last_seen import LAST_EVERY_S, LastSeen, boot_time, describe
from littlebot.power.protocol import Reading

FULL = Reading(voltage=16.42, cell1=None, temperature_c=None, charging=False, charged=False, uptime_s=None,
               cell_volts=(4.11, 4.10, 4.11, 4.10), percent=100.0)


def _seen(path: Path, now: list[float], booted: float | None) -> LastSeen:
    return LastSeen(path, now=lambda: now[0], booted=lambda: booted)


def test_a_cut_is_logged_with_the_last_reading(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path, now = tmp_path / "power_last.json", [5000.0]
    run = _seen(path, now, booted=1000.0)
    run.note(FULL, 100)
    now[0] += 1
    run.note(None, None)  # too soon: the first reading stays
    now[0] += 100  # ... and the power goes (no clean())
    with caplog.at_level(logging.INFO, logger="littlebot.power"):
        last = _seen(path, now, booted=now[0] - 30).check()
    assert last is not None and last["percent"] == 100
    assert "the power was cut" in caplog.text and "on the battery, 100%, pack 16.42 V" in caplog.text


def test_a_clean_stop_or_a_crash_is_no_cut(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path, now = tmp_path / "power_last.json", [5000.0]
    run = _seen(path, now, booted=1000.0)
    run.note(FULL, 100)
    run.clean()
    assert _seen(path, now, booted=now[0] - 30).check() is None
    run.note(FULL, 100, force=True)  # running again, then the app alone stops (the Pi stays up)
    with caplog.at_level(logging.INFO, logger="littlebot.power"):
        assert _seen(path, now, booted=1000.0).check() is None
    assert "the computer kept running" in caplog.text
    assert LastSeen(None).check() is None and _seen(tmp_path / "none.json", now, 1.0).check() is None


def test_readings_are_written_every_few_seconds(tmp_path: Path) -> None:
    path, now = tmp_path / "power_last.json", [5000.0]
    run = _seen(path, now, booted=1000.0)
    run.note(FULL, 100)
    first = path.read_text(encoding="utf-8")
    now[0] += LAST_EVERY_S + 0.1
    run.note(FULL, 99)
    assert path.read_text(encoding="utf-8") != first


def test_boot_time_and_describe(tmp_path: Path) -> None:
    up = tmp_path / "uptime"
    up.write_text("120.50 300.00\n", encoding="ascii")
    assert boot_time(up, now=lambda: 1000.0) == pytest.approx(879.5)
    assert boot_time(tmp_path / "missing") is None
    assert describe({"at": 1.0}) == "no battery reading"
    assert describe({"percent": 40, "charging": True}) == "charger plugged in, 40%"
