"""A small QR code maker (byte mode, versions 1-10): the share link as a code a phone's camera opens.

No dependency: it follows the QR Code standard (ISO/IEC 18004) the way Project Nayuki's reference implementation
does (MIT licence), cut down to what a web address needs: 8-bit byte mode, error correction M (L when the text
needs it), the eight masks scored by the standard's four penalty rules. Version 10-M holds 213 bytes, far more than
a share link (``http://192.168.2.112:8765/share/<22 characters>`` is 54, version 4).

    matrix(text) -> list[list[bool]]   (True = dark; no quiet zone)
    svg(text)    -> str                (with a 4-module quiet zone, for the dashboard)

Tests decode the codes with OpenCV's QR reader where OpenCV is installed (the robot).
"""

from __future__ import annotations

from typing import Callable

MAX_VERSION = 10

# Indexed [level][version] (index 0 unused). Levels: 0 = L, 1 = M (the standard's tables, versions 1-10).
_ECC_PER_BLOCK = ((0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18),
                  (0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26))
_BLOCKS = ((0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4),
           (0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5))
_FORMAT_BITS = (1, 0)  # L, M as the format information encodes them
LOW, MEDIUM = 0, 1

_MASKS: tuple[Callable[[int, int], int], ...] = (
    lambda x, y: (x + y) % 2,
    lambda x, y: y % 2,
    lambda x, y: x % 3,
    lambda x, y: (x + y) % 3,
    lambda x, y: (x // 3 + y // 2) % 2,
    lambda x, y: x * y % 2 + x * y % 3,
    lambda x, y: (x * y % 2 + x * y % 3) % 2,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
)


class TooLong(ValueError):
    """The text doesn't fit in a version 10 code."""


def _raw_modules(ver: int) -> int:
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver: int, level: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[level][ver] * _BLOCKS[level][ver]


def _rs_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_multiply(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _rs_multiply(coef, factor)
    return result


def _codewords(data: bytes, ver: int, level: int) -> list[int]:
    """Mode, length, the bytes, terminator and padding; then error correction per block, interleaved."""
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)  # byte mode
    put(len(data), 8 if ver <= 9 else 16)
    for b in data:
        put(b, 8)
    capacity = _data_codewords(ver, level) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity // 8:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    nblocks = _BLOCKS[level][ver]
    ecc_len = _ECC_PER_BLOCK[level][ver]
    raw = _raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    short_len = raw // nblocks
    divisor = _rs_divisor(ecc_len)
    blocks: list[list[int]] = []
    k = 0
    for i in range(nblocks):
        dat = words[k:k + short_len - ecc_len + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < nshort:
            dat.append(0)
        blocks.append(dat + ecc)
    out: list[int] = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= nshort:  # (the padding byte of a short block is not sent)
                out.append(blk[i])
    return out


class _Code:
    def __init__(self, ver: int, level: int) -> None:
        self.ver, self.level = ver, level
        self.size = ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set_fixed(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    # ------------------------------------------------------------ function patterns
    def draw_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set_fixed(6, i, i % 2 == 0)
            self.set_fixed(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_fixed(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self.alignment_positions()
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # the finder patterns are there
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fixed(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        self.draw_version()

    def alignment_positions(self) -> list[int]:
        if self.ver == 1:
            return []
        num = self.ver // 7 + 2
        step = (self.ver * 8 + num * 3 + 5) // (num * 4 - 4) * 2
        out = [self.size - 7 - i * step for i in range(num - 1)] + [6]
        return list(reversed(out))

    def draw_format(self, mask: int) -> None:
        data = _FORMAT_BITS[self.level] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        n = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 != 0

        for i in range(6):
            self.set_fixed(8, i, bit(i))
        self.set_fixed(8, 7, bit(6))
        self.set_fixed(8, 8, bit(7))
        self.set_fixed(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fixed(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fixed(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fixed(8, n - 15 + i, bit(i))
        self.set_fixed(8, n - 8, True)  # always dark

    def draw_version(self) -> None:
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.ver << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self.set_fixed(a, b, dark)
            self.set_fixed(b, a, dark)

    # ------------------------------------------------------------ data
    def draw_data(self, words: list[int]) -> None:
        n = self.size
        i = 0
        total = len(words) * 8
        for right in range(n - 1, 0, -2):
            if right <= 6:
                right -= 1  # skip the vertical timing column
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1

    def apply_mask(self, mask: int) -> None:
        fn = _MASKS[mask]
        for y in range(self.size):
            row, fixed = self.dark[y], self.fixed[y]
            for x in range(self.size):
                if not fixed[x] and fn(x, y) == 0:
                    row[x] = not row[x]

    def penalty(self) -> int:
        n, m = self.size, self.dark
        score = 0
        lines = [m[y] for y in range(n)] + [[m[y][x] for y in range(n)] for x in range(n)]
        for line in lines:  # rule 1: runs of five or more; rule 3: finder-like 1:1:3:1:1 with 4 light beside
            run_color, run = line[0], 1
            for v in line[1:]:
                if v == run_color:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run_color, run = v, 1
            if run >= 5:
                score += 3 + run - 5
            padded = [False] * 4 + list(line) + [False] * 4
            for k in range(len(padded) - 10):
                w = padded[k:k + 11]
                if w == _FINDER_A or w == _FINDER_B:
                    score += 40
        for y in range(n - 1):  # rule 2: 2x2 blocks of one colour
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)  # rule 4: far from half dark
        total = n * n
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + k * 10


_FINDER_A = [True, False, True, True, True, False, True, False, False, False, False]
_FINDER_B = list(reversed(_FINDER_A))


def matrix(text: str | bytes, *, level: int = MEDIUM) -> list[list[bool]]:
    """The QR code for ``text`` (UTF-8), as rows of modules (True = dark), without the quiet zone."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    for lvl in ((level, LOW) if level != LOW else (LOW,)):
        for ver in range(1, MAX_VERSION + 1):
            header = 4 + (8 if ver <= 9 else 16)
            if header + 8 * len(data) <= _data_codewords(ver, lvl) * 8:
                return _make(data, ver, lvl)
    raise TooLong(f"{len(data)} bytes don't fit in a version {MAX_VERSION} QR code")


def _make(data: bytes, ver: int, level: int) -> list[list[bool]]:
    words = _codewords(data, ver, level)
    best: tuple[int, int] | None = None
    for mask in range(8):
        code = _Code(ver, level)
        code.draw_patterns()
        code.draw_data(words)
        code.apply_mask(mask)
        code.draw_format(mask)
        p = code.penalty()
        if best is None or p < best[0]:
            best = (p, mask)
    assert best is not None
    code = _Code(ver, level)
    code.draw_patterns()
    code.draw_data(words)
    code.apply_mask(best[1])
    code.draw_format(best[1])
    return code.dark


def svg(text: str, *, border: int = 4, dark: str = "#000", light: str = "#fff") -> str:
    """An SVG of the code, ``border`` light modules all round (scales to any size)."""
    m = matrix(text)
    n = len(m) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(m) for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="{light}"/><path d="{path}" fill="{dark}"/></svg>')


__all__ = ["TooLong", "matrix", "svg"]
