"""The stall watch (littlebot/core/stall_watch.py): it notices a stuck main loop and says where."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from littlebot.core import stall_watch as sw
from littlebot.core.runtime import AsyncRuntime


@pytest.fixture
def runtime():
    rt = AsyncRuntime(name="test-loop")
    rt.start()
    yield rt
    rt.stop()


def _hog_the_loop_for_a_while() -> None:
    time.sleep(0.8)  # stands in for code that blocks the loop (a long computation, a blocking read)


def test_a_stuck_loop_is_logged_with_where_it_is_busy_and_for_how_long(runtime, caplog, monkeypatch):
    monkeypatch.setattr(sw, "STUCK_AFTER_S", 0.2)
    caplog.set_level(logging.INFO, logger="littlebot.core.stall_watch")
    watch = sw.StallWatch(runtime, sample=dict)
    assert watch.ping() is not None and watch.stalls == 0
    runtime.call_soon(_hog_the_loop_for_a_while)
    took = watch.ping()
    assert took is not None and took >= 0.5 and watch.stalls == 1
    assert "the main loop has been stuck" in caplog.text
    assert "test_stall_watch.py" in caplog.text and "_hog_the_loop_for_a_while" in caplog.text
    assert "the main loop answered again after" in caplog.text


def test_low_memory_swapping_and_heat_are_warned_once_and_health_is_logged(runtime, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.core.stall_watch")
    samples = iter([
        {"available_mb": 400, "swap_used_mb": 10, "temp_c": 60.0, "throttled": 0, "load": 0.5, "rss_mb": 300,
         "cpu_s": 10.0},
        {"available_mb": 90, "swap_used_mb": 200, "temp_c": 82.0, "throttled": 0x50005, "load": 3.0, "rss_mb": 600,
         "cpu_s": 20.0},
        {"available_mb": 80, "swap_used_mb": 300, "temp_c": 83.0, "throttled": 0x50005, "load": 3.0, "rss_mb": 620,
         "cpu_s": 30.0},
        {"available_mb": 500, "swap_used_mb": 300, "temp_c": 70.0, "throttled": 0x50000, "load": 0.8, "rss_mb": 400,
         "cpu_s": 31.0},
    ])
    watch = sw.StallWatch(runtime, sample=lambda: next(samples))
    for health in (False, False, False, True):
        watch._check_system(health=health)
    text = caplog.text
    assert text.count("memory is low: 90 MB free") == 1 and "memory is fine again: 500 MB free" in text
    assert text.count("the Pi is swapping") == 1
    assert text.count("the Pi is hot: 82") == 1 and "the Pi has cooled to 70" in text
    assert text.count("under-voltage now") == 1
    assert "health: 500 MB free, 300 MB swap in use, 70 °C, load 0.80, LittleBot 400 MB" in text


def test_the_real_system_reading_works_here():
    s = sw.StallWatch(None)._read_system()
    assert set(s) >= {"available_mb", "swap_used_mb", "temp_c", "throttled", "load", "rss_mb", "cpu_s"}


def test_where_names_code_only():
    assert sw.where(None) == "unknown"
    assert "test_where_names_code_only" in sw.where(threading.current_thread())
