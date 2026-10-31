"""The maths cards: a sum or a conversion worked out big, and counting for the little one.

``maths``: "what's 12 times 7?" shows "12 × 7" and, popping in under it, "= 84" (up to three short
steps in between when the AI gives them). "3 cups in ml", "350 °F in °C" work the same way. The AI
gives the expression and the result; when both can be read here (plain arithmetic, or a number with a
known unit) the result is checked with a tiny parser (no eval) and corrected if it is wrong.

``count``: "count to 5" shows five big stars (hearts, apples, balls) appearing one by one with a pop,
each with its number, for the little one to follow along.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, Card, CardHost, CardType, W, register
from littlebot.screens.cards import candy

log = logging.getLogger("littlebot.screens.maths")

MAX_STEPS = 3
TEXT_W = candy.PANEL.w - 60


# ---------------------------------------------------------------- a tiny arithmetic parser (no eval)
_WORDS = (
    (r"\bsquare root of\b|\bsqrt\b|√", "√"),  # (before "of")
    (r"\bmultiplied by\b|\btimes\b|×|\*", "*"), (r"\bdivided by\b|\bover\b|÷", "/"), (r"\bplus\b", "+"),
    (r"\bminus\b|−|–", "-"), (r"\bsquared\b", "^2"), (r"\bcubed\b", "^3"), (r"\bto the power of\b|\*\*", "^"),
    (r"\bpercent\b", "%"), (r"\bof\b", "*"),
)
_TOKEN = re.compile(r"\s*(\d+(?:\.\d+)?|\.\d+|[-+*/^%()√])")


class _Parser:
    """expr := term (+|- term)*; term := power (*|/ power)*; power := unary (^ power)?;
    unary := - unary | √ unary | atom %*; atom := number | ( expr )."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens, self.i = tokens, 0

    def peek(self) -> str | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self) -> str:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expr(self) -> float:
        v = self.term()
        while self.peek() in ("+", "-"):
            v = v + self.term() if self.take() == "+" else v - self.term()
        return v

    def term(self) -> float:
        v = self.power()
        while self.peek() in ("*", "/"):
            if self.take() == "*":
                v *= self.power()
            else:
                d = self.power()
                if d == 0:
                    raise ValueError("division by zero")
                v /= d
        return v

    def power(self) -> float:
        v = self.unary()
        if self.peek() == "^":
            self.take()
            e = self.power()
            if abs(e) > 64 or abs(v) > 1e6:
                raise ValueError("too big")
            v = float(v) ** e
            if isinstance(v, complex):
                raise ValueError("complex")
        return v

    def unary(self) -> float:
        if self.peek() == "-":
            self.take()
            return -self.unary()
        if self.peek() == "+":
            self.take()
            return self.unary()
        if self.peek() == "√":
            self.take()
            v = self.unary()
            if v < 0:
                raise ValueError("square root of a negative number")
            return math.sqrt(v)
        v = self.atom()
        while self.peek() == "%":
            self.take()
            v /= 100
        return v

    def atom(self) -> float:
        tok = self.peek()
        if tok is None:
            raise ValueError("end")
        if tok == "(":
            self.take()
            v = self.expr()
            if self.peek() != ")":
                raise ValueError("no )")
            self.take()
            return v
        if tok[0].isdigit() or tok[0] == ".":
            self.take()
            return float(tok)
        raise ValueError(f"unexpected {tok}")


def _normal(text: str) -> str:
    s = str(text or "").lower().strip()
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)  # 1,000 -> 1000
    s = re.sub(r"(?<=[\d)\s])x(?=[\s\d(])", "*", s)  # 12 x 7
    for pattern, repl in _WORDS:
        s = re.sub(pattern, repl, s)
    return s.strip().rstrip("=?").strip()


def evaluate(text: str) -> float | None:
    """The value of plain arithmetic ("12 x 7", "(3 + 4) * 2", "15% of 80", "2^10"); None for anything else."""
    s = _normal(text)
    if not s or len(s) > 80:
        return None
    tokens, pos = [], 0
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m:
            if s[pos:].strip() == "":
                break
            return None
        tokens.append(m.group(1))
        pos = m.end()
    if not tokens:
        return None
    p = _Parser(tokens)
    try:
        v = p.expr()
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    if p.peek() is not None or not math.isfinite(v) or abs(v) > 1e15:
        return None
    return v


def format_number(v: float, decimals: int | None = None) -> str:
    """84.0 -> "84"; 1234567 -> "1,234,567"; 3.14159 -> "3.142" (or ``decimals`` places)."""
    if decimals is None:
        if abs(v - round(v)) < 1e-9:
            return f"{round(v):,}"
        decimals = max(0, 4 - len(str(int(abs(v)))))  # about 4 to 5 significant figures
    s = f"{v:,.{decimals}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _decimals(text: str) -> int:
    m = re.search(r"\.(\d+)", text)
    return len(m.group(1)) if m else 0


# ---------------------------------------------------------------- units (conversions)
_UNITS: dict[str, tuple[str, float]] = {}  # name -> (what it measures, size in the base unit)
for _names, _dim, _k in (
    (("mm", "millimetre", "millimeter"), "length", 0.001), (("cm", "centimetre", "centimeter"), "length", 0.01),
    (("m", "metre", "meter"), "length", 1.0), (("km", "kilometre", "kilometer"), "length", 1000.0),
    (("in", "inch", "inches", '"'), "length", 0.0254), (("ft", "foot", "feet", "'"), "length", 0.3048),
    (("yd", "yard"), "length", 0.9144), (("mi", "mile"), "length", 1609.344),
    (("mg", "milligram"), "mass", 0.001), (("g", "gram", "gramme"), "mass", 1.0),
    (("kg", "kilo", "kilogram", "kilogramme"), "mass", 1000.0), (("oz", "ounce"), "mass", 28.349523125),
    (("lb", "lbs", "pound"), "mass", 453.59237), (("st", "stone"), "mass", 6350.29318),
    (("ml", "millilitre", "milliliter"), "volume", 1.0), (("cl", "centilitre", "centiliter"), "volume", 10.0),
    (("dl", "decilitre", "deciliter"), "volume", 100.0), (("l", "litre", "liter"), "volume", 1000.0),
    (("tsp", "teaspoon"), "volume", 4.92892), (("tbsp", "tablespoon"), "volume", 14.7868),
    (("fl oz", "fluid ounce"), "volume", 29.5735), (("cup",), "volume", 236.588),
    (("pint", "pt"), "volume", 473.176), (("quart", "qt"), "volume", 946.353), (("gallon", "gal"), "volume", 3785.41),
    (("km/h", "kph", "kmh", "kilometres per hour", "kilometers per hour"), "speed", 1.0),
    (("mph", "miles per hour"), "speed", 1.609344), (("m/s",), "speed", 3.6),
):
    for _n in _names:
        _UNITS[_n] = (_dim, _k)
_TEMPS = {"c": "c", "celsius": "c", "centigrade": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}


def _unit(text: str) -> tuple[str, Any] | None:
    """("length", 0.3048) / ("temperature", "f") for a unit's name; None when unknown."""
    u = re.sub(r"[.]", "", str(text or "").lower()).replace("°", " ").strip()
    u = re.sub(r"^degrees?\s+", "", u).strip()
    if u in _TEMPS:
        return "temperature", _TEMPS[u]
    for cand in (u, u.removesuffix("s"), u.removesuffix("es")):
        if cand in _UNITS:
            return _UNITS[cand]
    return None


_QUANTITY = re.compile(r"^\s*(-?\d[\d,]*(?:\.\d+)?|-?\.\d+)\s*([^\d=]+?)\s*$")


def _quantity(text: str) -> tuple[float, str] | None:
    m = _QUANTITY.match(str(text or "").replace(",", "") if re.search(r"\d,\d{3}", str(text)) else str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "")), m.group(2).strip()
    except ValueError:
        return None


def _to_kelvin(v: float, u: str) -> float:
    return v + 273.15 if u == "c" else (v - 32) * 5 / 9 + 273.15 if u == "f" else v


def _from_kelvin(v: float, u: str) -> float:
    return v - 273.15 if u == "c" else (v - 273.15) * 9 / 5 + 32 if u == "f" else v


def convert(value: float, src: str, dst: str) -> float | None:
    """``value`` in unit ``src`` expressed in ``dst`` ("cups" -> "ml", "°F" -> "°C"); None if they don't match."""
    a, b = _unit(src), _unit(dst)
    if a is None or b is None or a[0] != b[0]:
        return None
    if a[0] == "temperature":
        return _from_kelvin(_to_kelvin(value, a[1]), b[1])
    return value * a[1] / b[1]


def check(expression: str, result: str) -> str:
    """``result``, or the right one when it can be worked out here and the AI's is wrong."""
    result = " ".join(str(result or "").split())
    shown = result.lstrip("=≈ ").strip()
    v = evaluate(expression.split("=")[0])
    if v is not None:
        got = evaluate(shown) if re.fullmatch(r"-?[\d,]*\.?\d+", shown.replace(" ", "")) else None
        if got is None and shown:
            return result  # words ("about 3"): trust it
        d = _decimals(shown)
        if got is None or abs(got - v) > 0.5 * 10 ** -d + 1e-9:
            right = format_number(v, d if got is not None and d else None)
            if shown:
                log.info("screens: maths result corrected")
            return right
        return result
    # a conversion: "3 cups" / "3 cups to ml" = "710 ml"
    left = re.split(r"\s+(?:in|to|into|as)\s+", expression.split("=")[0].strip(), maxsplit=1)
    q, r = _quantity(left[0]), _quantity(shown)
    if q is None or r is None:
        return result
    right = convert(q[0], q[1], r[1])
    if right is None:
        return result
    tol = max(0.5 * 10 ** -_decimals(shown), abs(right) * 0.015, 0.05 if _unit(r[1])[0] == "temperature" else 0)
    if abs(right - r[0]) <= tol:
        return result
    log.info("screens: conversion result corrected")
    places = 0 if abs(right) >= 100 else 1 if abs(right) >= 10 else 2
    return f"{format_number(right, places)} {r[1]}"


def pretty(expression: str) -> str:
    """ "12*7" -> "12 × 7" for plain arithmetic; conversions stay as they were given."""
    s = re.sub(r"(?<=\d)\s+°", "°", " ".join(str(expression or "").split()).rstrip("=? "))  # the font spaces ° itself
    if evaluate(s) is None:
        return s
    s = re.sub(r"(?<=[\d)\s])x(?=[\s\d(])", "×", s)
    s = re.sub(r"\s*\*\s*|\s*×\s*", " × ", s)
    s = re.sub(r"\s*/\s*|\s*÷\s*", " ÷ ", s)
    s = re.sub(r"(?<=[\d)])\s*([+])\s*", r" \1 ", s)
    s = re.sub(r"(?<=[\d)])\s*-\s*(?=[\d(])", " - ", s)
    return s


# ---------------------------------------------------------------- the maths card
class MathsCard(Card):
    """An expression, up to three steps, and the result popping in under them."""

    kind = "maths"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None) -> None:
        super().__init__(spec, host)
        self.show(spec)

    def show(self, spec: dict[str, Any]) -> None:
        self.expression = pretty(str(spec.get("expression") or ""))[:60]
        result = str(spec.get("result") or "")
        if not result:
            v = evaluate(self.expression)
            result = format_number(v) if v is not None else ""
        self.result = re.sub(r"(?<=\d)\s+°", "°", check(str(spec.get("expression") or ""), result))[:40]
        steps = spec.get("steps") or []
        self.steps = [" ".join(str(s).split())[:60] for s in (steps if isinstance(steps, list) else [steps])
                      if str(s).strip()][:MAX_STEPS]
        # "Converting" only for one ("√1764" was titled "Converting" on the robot, 2026-10-31)
        left = self.expression.split("=")[0]
        self.conversion = evaluate(self.expression) is None and (
            _quantity(left) is not None or re.search(r"\s(?:in|to|into|as)\s", left) is not None)
        self.title = str(spec.get("title") or "")[:40] or ("Converting" if self.conversion else "Let's work it out")
        self.shown_at = self.t
        self.mood = "happy"

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "show" and isinstance(args, dict) and args.get("expression"):
            self.show(args)
            return True
        return False

    def describe(self) -> str:
        return f"{self.expression} = {self.result.lstrip('= ')}" if self.result else self.expression

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(art.background((W, 480), seed=9), (0, 0))
        dy = candy.slide_in(self.t)
        candy.header(surf, self.title, dy=dy * 0.5)
        box = candy.panel(surf, dy=dy)
        age = self.t - self.shown_at
        if self.steps:
            y = box.top + 50
            candy.blit_mid(surf, candy.fit(self.expression, 60, TEXT_W, min_size=40), (box.centerx, y))
            for i, step in enumerate(self.steps):
                a = art.clamp((age - 0.25 - 0.3 * i) / 0.25, 0, 1)
                if a > 0:
                    art.blit_center(surf, candy.fit(step, 40, TEXT_W, min_size=BODY_PX),
                                    (box.centerx, y + 56 + 44 * i), int(255 * a))
            ry, rsize, delay = box.bottom - 66, 88, 0.35 + 0.3 * len(self.steps)
        else:
            candy.blit_mid(surf, candy.fit(self.expression, 96, TEXT_W, min_size=44), (box.centerx, box.top + 100))
            ry, rsize, delay = box.top + 250, 136, 0.45
        if not self.result or age < delay:
            return
        sprite = self._result_sprite(rsize)
        p = (age - delay) / 0.35
        k = art.ease_out_back(p, 2.0) if p < 1 else 1.0
        art.blit_center(surf, art.scaled(sprite, k) if k < 0.99 or k > 1.01 else sprite, (box.centerx, ry))

    def _result_sprite(self, size: int) -> pygame.Surface:
        text = f"= {self.result.lstrip('= ')}"
        key = ("maths_result", text, size)

        def make() -> pygame.Surface:
            img = candy.fit(text, size, TEXT_W - 60, min_size=48)
            w, h = img.get_width() + 64, img.get_height() + 20
            box = art.rounded_rect(w, h, h // 2, art.lighten(art.LEMON, 0.62), shadow=5,
                                   border=art.inkier(art.LEMON, 0.15), border_w=4).copy()
            box.blit(img, img.get_rect(center=(w // 2, h // 2 + 3)))
            return box

        return art.cached(key, make)


async def build_maths(spec: dict[str, Any], ctx: Any, host: CardHost) -> MathsCard | None:
    if not str(spec.get("expression") or "").strip():
        return None
    return MathsCard(spec, host)


register(CardType(
    kind="maths",
    description="A sum or a conversion worked out big: '12 × 7 = 84', '3 cups = 710 ml', '350 °F = 177 °C', "
                "'15% of 80 = 12'. Give the expression and the result; steps only if they help (at most 3, short).",
    fields={
        "expression": {"type": "string", "description": "What was asked, short, with numbers and symbols: "
                                                        "'12 × 7', '3 cups to ml', '350 °F to °C'."},
        "result": {"type": "string", "description": "The answer with its unit: '84', '710 ml', '177 °C'."},
        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_STEPS,
                  "description": "Optional working, at most 3 short lines: '10 × 7 = 70', '2 × 7 = 14'."},
    },
    build=build_maths,
    required=("expression", "result"),
    actions={"show": "another sum on the same card: {\"expression\": \"12 × 8\", \"result\": \"96\", "
                     "\"steps\": []}"},
))


# ---------------------------------------------------------------- counting
THINGS = ("star", "heart", "apple", "ball")
MAX_COUNT = 10
PACE = 1.1  # seconds between things appearing (about the voice's counting)
FIRST = 0.6


def _apple(size: int) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            red = art.STRAWBERRY
            for color, g in ((art.inkier(red, 0.35), n * 0.035), (red, 0.0)):
                pygame.draw.circle(s, color, (n * 0.38, n * 0.58), n * 0.29 + g)
                pygame.draw.circle(s, color, (n * 0.62, n * 0.58), n * 0.29 + g)
                pygame.draw.circle(s, color, (n * 0.5, n * 0.68), n * 0.26 + g)
            art.capsule(s, (140, 92, 60), (n * 0.5, n * 0.34), (n * 0.54, n * 0.16), n * 0.06)
            leaf = [(n * 0.55, n * 0.24), (n * 0.68, n * 0.12), (n * 0.84, n * 0.14), (n * 0.72, n * 0.26)]
            pygame.draw.polygon(s, art.inkier(art.MINT, 0.3), leaf)
            pygame.draw.ellipse(s, art.lighten(red, 0.55), pygame.Rect(n * 0.22, n * 0.42, n * 0.14, n * 0.1))

        return art.supersampled((size, size), draw)

    return art.cached(("count_apple", size), make)


def _ball(size: int, color: art.Color) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            pygame.draw.circle(s, art.inkier(color, 0.35), (n / 2, n / 2), n * 0.46)
            pygame.draw.circle(s, color, (n / 2, n / 2), n * 0.42)
            pygame.draw.ellipse(s, art.lighten(color, 0.55), pygame.Rect(n * 0.26, n * 0.2, n * 0.18, n * 0.11))
            art.cute_face(s, n / 2, n * 0.54, n * 0.8)

        return art.supersampled((size, size), draw)

    return art.cached(("count_ball", size, color), make)


def thing_sprite(thing: str, size: int, i: int) -> pygame.Surface:
    color = art.CANDY[(i * 3) % len(art.CANDY)]
    if thing == "heart":
        return art.heart(size, art.BUBBLEGUM if i % 2 else art.STRAWBERRY)
    if thing == "apple":
        return _apple(size)
    if thing == "ball":
        return _ball(size, color)
    return art.star(size * 0.5, color, face=True)


class CountCard(Card):
    """Things appearing one by one, each with its number."""

    kind = "count"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None) -> None:
        super().__init__(spec, host)
        self.particles = art.Particles()
        self.restart(spec)

    def restart(self, spec: dict[str, Any]) -> None:
        try:
            n = int(spec.get("to") or spec.get("count") or 5)
        except (TypeError, ValueError):
            n = 5
        self.n = max(1, min(MAX_COUNT, n))
        thing = str(spec.get("thing") or "star").lower().rstrip("s")
        self.thing = thing if thing in THINGS else "star"
        self.title = f"Let's count to {self.n}!"
        self.start = self.t
        self.shown = 0
        self.cheered = False
        self.mood = "happy"

    def update(self, dt: float) -> None:
        super().update(dt)
        self.particles.update(dt)
        due = min(self.n, int((self.t - self.start - FIRST) // PACE) + 1) if self.t - self.start >= FIRST else 0
        while self.shown < due:
            self.shown += 1
            self.host.sound("pop")
        if self.shown == self.n and not self.cheered and self.t - self.start >= FIRST + (self.n - 1) * PACE + 0.7:
            self.cheered = True
            self.host.sound("sparkle")
            self.particles.confetti(W, 40)
            self.mood = "surprised"

    def command(self, action: str, args: dict[str, Any]) -> bool:
        args = args if isinstance(args, dict) else {}
        if action == "again":
            self.restart({"to": self.n, "thing": self.thing})
            return True
        if action == "count":
            self.restart({"to": args.get("to") or args.get("n") or self.n, "thing": args.get("thing") or self.thing})
            return True
        return False

    def describe(self) -> str:
        def things(k: int) -> str:
            return f"{k} {self.thing}{'' if k == 1 else 's'}"

        if self.shown < self.n:
            return f"Counting to {self.n}: {things(self.shown)} so far"
        return f"{things(self.n)}, all counted"

    def _places(self) -> list[tuple[int, int, int]]:
        """(x, y, size) of each thing."""
        box = candy.PANEL
        rows = [self.n] if self.n <= 5 else [math.ceil(self.n / 2), self.n // 2]
        size = 116 if len(rows) == 1 else 86
        ys = [box.top + 140] if len(rows) == 1 else [box.top + 92, box.top + 250]
        out = []
        for row, y in zip(rows, ys):
            step = min(132, (box.w - 40) // max(1, row))
            x0 = box.centerx - step * (row - 1) / 2
            out += [(int(x0 + step * i), y, size) for i in range(row)]
        return out

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(art.background((W, 480), seed=13), (0, 0))
        dy = candy.slide_in(self.t)
        candy.header(surf, self.title, dy=dy * 0.5)
        candy.panel(surf, dy=dy)
        big = self.n <= 5
        for i, (x, y, size) in enumerate(self._places()[: self.shown]):
            age = self.t - self.start - FIRST - i * PACE
            k = art.ease_out_back(age / 0.35, 2.2) if age < 0.35 else 1.0
            bob = math.sin(self.t * 2.4 + i) * 3
            sprite = thing_sprite(self.thing, size, i)
            art.blit_center(surf, art.scaled(sprite, k) if k < 0.99 else sprite, (x, y + dy + bob))
            num = art.text_outlined(str(i + 1), 72 if big else 50, art.WHITE, art.INK, 3)
            art.blit_center(surf, num, (x, y + dy + size * 0.5 + (46 if big else 30)))
        self.particles.draw(surf)


async def build_count(spec: dict[str, Any], ctx: Any, host: CardHost) -> CountCard | None:
    return CountCard(spec, host)


register(CardType(
    kind="count",
    description="Counting for the little one: 'count to 5' shows big stars (or hearts, apples, balls) appearing "
                "one by one with their numbers while you count out loud, about one a second. Up to 10.",
    fields={
        "to": {"type": "integer", "minimum": 1, "maximum": MAX_COUNT, "description": "How many to count, 1 to 10."},
        "thing": {"type": "string", "enum": list(THINGS), "description": "What to count (default star)."},
    },
    build=build_count,
    required=("to",),
    actions={"again": "count again from 1: {}", "count": "count to another number: {\"to\": 3, \"thing\": "
                                                          "\"apple\"}"},
))

# Example specs for scripts/screen_preview.py.
SAMPLES: list[dict[str, Any]] = [
    {"card": "maths", "expression": "12 x 7", "result": "84"},
    {"card": "maths", "expression": "3 cups to ml", "result": "710 ml"},
    {"card": "maths", "expression": "350 °F to °C", "result": "177 °C"},
    {"card": "maths", "expression": "23 × 14", "result": "322", "steps": ["20 × 14 = 280", "3 × 14 = 42",
                                                                          "280 + 42 = 322"]},
    {"card": "count", "to": 5, "thing": "star"},
    {"card": "count", "to": 8, "thing": "apple"},
]

__all__ = ["SAMPLES", "CountCard", "MathsCard", "check", "convert", "evaluate", "format_number", "pretty"]
