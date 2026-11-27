"""littlebot/core/memory.py: giving freed memory back to the system."""

from __future__ import annotations

import sys

import pytest

from littlebot.core import memory
from littlebot.core.memory import total_mb as REAL_TOTAL_MB  # (tests/conftest.py pretends 2 GB for each test)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="malloc_trim is glibc's")
def test_give_back_returns_freed_heap_to_the_system():
    def rss_mb() -> float:
        with open("/proc/self/status") as f:
            return next(int(line.split()[1]) for line in f if line.startswith("VmRSS")) / 1024

    blocks, keep = [], []
    for _ in range(1600):  # ~100 MB in heap-sized pieces, with small things kept in between (fragmented)
        blocks.append(bytearray(64 * 1024))
        keep.append(bytearray(200))
    big = rss_mb()
    del blocks
    memory.give_back()  # measured: ~40 MB back without malloc_trim, all 100 with it
    assert big - rss_mb() > 80 and len(keep) == 1600


def test_give_back_never_raises(monkeypatch):
    monkeypatch.setattr(memory, "_TRIM", lambda pad: 1 / 0)
    memory.give_back()


def test_a_4gb_board_is_roomy_and_a_2gb_one_small(monkeypatch):
    monkeypatch.setattr(memory, "total_mb", lambda: 3900)  # a 4 GB Pi 5
    assert memory.roomy("auto") and not memory.roomy("small")
    monkeypatch.setattr(memory, "total_mb", lambda: 1990)  # a 2 GB one
    assert not memory.roomy("auto") and memory.roomy("roomy")
    monkeypatch.setattr(memory, "total_mb", lambda: None)  # not Linux: not a 2 GB Pi
    assert memory.roomy("auto")


def test_the_robots_config_decides(ctx):
    assert not memory.roomy_for(ctx)  # auto, on the 2 GB board every test pretends to be (tests/conftest.py)
    ctx.config.system.memory = "roomy"
    assert memory.roomy_for(ctx)
    assert not memory.roomy_for(object())  # no config: auto


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/meminfo is Linux's")
def test_the_boards_ram_is_read_from_the_system():
    assert REAL_TOTAL_MB() > 100
