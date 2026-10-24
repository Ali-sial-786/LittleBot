"""Parsing the battery monitor's lines: good, garbage, partial, other devices, odd values."""

from __future__ import annotations

import pytest

from littlebot.power.protocol import LineSplitter, parse_line

GOOD = b'{"dev":"littlebot-battery","fw":1,"v":7.84,"cell1":3.92,"t":24.6,"chg":false,"full":false,"up":123}'


def test_a_good_line():
    r = parse_line(GOOD)
    assert r is not None
    assert (r.voltage, r.cell1, r.temperature_c, r.charging, r.charged, r.uptime_s, r.fw) == \
        (7.84, 3.92, 24.6, False, False, 123, 1)
    assert r.cell2 == pytest.approx(3.92) and r.cells == (3.92, pytest.approx(3.92))
    assert parse_line(GOOD.decode() + "\r\n") == r  # str, and line endings, are fine too
    spaced = b'{"dev": "littlebot-battery", "fw": 1, "v": 7.0, "cell1": null, "t": null, "chg": true, "full": false, "up": 5}'
    r2 = parse_line(spaced)
    assert r2 is not None and r2.voltage == 7.0 and r2.cell1 is None and r2.cells == () and r2.charging is True


@pytest.mark.parametrize("line", [
    b"", b"   ", b"garbage", b"\xff\xfe\x00junk", b"MicroPython v1.24.1 on 2024-11-29; Raspberry Pi Pico with RP2040",
    b">>> ", b'{"dev":"littlebot-battery","fw":1,"v":7.84',  # cut off
    b'"cell1":3.92,"t":24.6,"chg":false,"full":false,"up":123}',  # the tail of a line (opened mid-line)
    b'{"dev":"something-else","v":7.8}', b'[1,2,3]', b'{"v":7.8}', b'{"dev":"littlebot-battery",}',
    b'{"dev":"littlebot-battery","v":' + b"9" * 600 + b"}",  # far too long
])
def test_garbage_and_partial_lines_are_ignored(line):
    assert parse_line(line) is None


def test_wrong_or_impossible_values_become_unknown():
    r = parse_line(b'{"dev":"littlebot-battery","fw":1,"v":"7.8","cell1":true,"t":999,"chg":"yes","full":1,"up":-4}')
    assert r is not None
    assert (r.voltage, r.cell1, r.temperature_c, r.charging, r.charged, r.uptime_s) == (None,) * 6
    r = parse_line(b'{"dev":"littlebot-battery","fw":1,"v":7.8,"cell1":4.2,"t":-12.5,"chg":null,"full":true,"up":0}')
    assert r is not None and r.cell1 == 4.2 and r.temperature_c == -12.5 and r.charged is True and r.charging is None
    # NaN / Infinity are not numbers; a cell that does not fit the pack (cell 2 = 3.0 - 4.9 < 0) is dropped
    r = parse_line(b'{"dev":"littlebot-battery","fw":1,"v":7.8,"cell1":4.9,"t":NaN,"chg":false,"full":false,"up":1}')
    assert r is not None and r.cell1 == 4.9 and r.temperature_c is None
    r = parse_line(b'{"dev":"littlebot-battery","fw":1,"v":3.0,"cell1":4.9,"t":Infinity,"chg":false,"full":false,"up":1}')
    assert r is not None and r.voltage == 3.0 and r.cell1 is None and r.temperature_c is None


def test_line_splitter_drops_partial_and_overlong_lines():
    s = LineSplitter(max_line=200)
    assert s.feed(b'tail of an old line}\r\n' + GOOD[:20]) == [b"tail of an old line}"]
    assert s.feed(GOOD[20:]) == []
    assert s.feed(b"\n") == [GOOD]
    assert s.feed(b"x" * 300) == []  # too long: dropped, and its tail too
    assert s.feed(b"yyy\n" + GOOD + b"\n") == [GOOD]
    # one parse per line, whatever the chunking
    s2 = LineSplitter()
    data = (GOOD + b"\n") * 3
    lines = [line for i in range(0, len(data), 7) for line in s2.feed(data[i:i + 7])]
    assert lines == [GOOD] * 3
