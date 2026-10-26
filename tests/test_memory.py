"""littlebot/core/memory.py: giving freed memory back to the system."""

from __future__ import annotations

import sys

import pytest

from littlebot.core import memory


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
