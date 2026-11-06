"""The QR code maker (littlebot/capture/qr.py): the right size, the fixed patterns where a reader looks for them, valid
format and version information, and (where OpenCV is installed, as on the robot) codes a real reader decodes."""

from __future__ import annotations

import re

import pytest

from littlebot.capture import qr
from littlebot.vision.imaging import get_cv2

LINK = "http://192.168.2.112:8765/share/AbCdEfGhIjKlMnOpQrStUv"


def _finder_ok(m: list[list[bool]], x0: int, y0: int) -> bool:
    for dy in range(7):
        for dx in range(7):
            ring = max(abs(dx - 3), abs(dy - 3))
            if m[y0 + dy][x0 + dx] != (ring != 2):
                return False
    return True


def _format_word(m: list[list[bool]]) -> int:
    """The 15 format bits from the copy around the top-left finder."""
    bits = 0
    spots = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    for i, (x, y) in enumerate(spots):
        bits |= int(m[y][x]) << i
    return bits


def _bch_format(data: int) -> int:
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


@pytest.mark.parametrize("text,version", [("hi", 1), ("x" * 14, 1), ("x" * 15, 2), (LINK, 4), ("z" * 115, 7),
                                          ("y" * 150, 8), ("v" * 213, 10)])
def test_the_smallest_version_that_fits(text: str, version: int) -> None:
    m = qr.matrix(text)
    assert len(m) == 17 + 4 * version and all(len(row) == len(m) for row in m)


def test_fixed_patterns_and_format_information() -> None:
    m = qr.matrix(LINK)
    n = len(m)
    assert _finder_ok(m, 0, 0) and _finder_ok(m, n - 7, 0) and _finder_ok(m, 0, n - 7)
    for i in range(8, n - 8):  # the timing patterns
        assert m[6][i] == (i % 2 == 0) and m[i][6] == (i % 2 == 0)
    assert m[n - 8][8] is True  # the dark module
    word = _format_word(m)
    valid = {_bch_format(level << 3 | mask): (level, mask) for level in (0, 1, 2, 3) for mask in range(8)}
    assert word in valid and valid[word][0] == 0  # level M (format bits 00)
    second = sum(int(m[8][n - 1 - i]) << i for i in range(8)) | sum(int(m[n - 15 + i][8]) << i for i in range(8, 15))
    assert second == word  # both copies agree


def test_version_information_from_version_7() -> None:
    m = qr.matrix("z" * 115)  # version 7 at level M
    n = len(m)
    assert n == 45
    bits = sum(int(m[i // 3][n - 11 + i % 3]) << i for i in range(18))
    rem = 7
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    assert bits == (7 << 12 | rem)
    assert bits == sum(int(m[n - 11 + i % 3][i // 3]) << i for i in range(18))  # and its mirror


def test_too_long_drops_to_level_l_then_refuses() -> None:
    assert len(qr.matrix("w" * 230)) == 53  # more than 10-M holds (213 bytes): 9-L
    with pytest.raises(qr.TooLong):
        qr.matrix("w" * 400)


def test_svg_is_one_path_with_a_quiet_zone() -> None:
    out = qr.svg(LINK)
    n = 33 + 8
    assert out.startswith("<svg") and f'viewBox="0 0 {n} {n}"' in out and out.count("<path") == 1
    assert "M4,4h1v1h-1z" in out  # the top-left finder's corner, 4 modules in
    assert not re.search(r"<script|on\w+=", out)


def test_the_same_text_makes_the_same_code() -> None:
    assert qr.matrix(LINK) == qr.matrix(LINK)


@pytest.mark.skipif(get_cv2() is None, reason="OpenCV's QR reader (installed on the robot)")
@pytest.mark.parametrize("text", ["hi", LINK, "http://littlebot.local:8765/share/" + "Q" * 22, "é ü 漢字", "z" * 115,
                                  "y" * 150, "w" * 230])
def test_a_real_reader_decodes_it(text: str) -> None:
    import numpy as np

    cv2 = get_cv2()
    m = qr.matrix(text)
    scale, border = 8, 4
    n = len(m) + 2 * border
    img = np.full((n * scale, n * scale), 255, dtype=np.uint8)
    for y, row in enumerate(m):
        for x, dark in enumerate(row):
            if dark:
                y0, x0 = (y + border) * scale, (x + border) * scale
                img[y0:y0 + scale, x0:x0 + scale] = 0
    found, _points, _ = cv2.QRCodeDetector().detectAndDecode(img)
    assert found == text
