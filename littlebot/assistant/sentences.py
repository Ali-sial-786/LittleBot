"""The brain's words, as they stream in, cut into sentences the voice can say one by one.

The first sentence goes to text-to-speech as soon as it is complete (a long first sentence is cut at a comma once
it has ``FIRST_CUT_CHARS``), so LittleBot starts talking while the rest is still being written. Each piece is
cleaned for speech (no markdown, links, emoji or stray symbols) and loses its face tags: ``[happy]`` at the start
of a sentence becomes that sentence's ``expression`` (one of core.interfaces.EXPRESSIONS), ``[hearts]`` its
``animation`` (FACE_ANIMATIONS), so the face changes as the sentence is heard. ``[silent]`` alone means the brain
chose not to answer (it heard people talking to each other).
"""

from __future__ import annotations

import dataclasses
import re

from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS

FIRST_CUT_CHARS = 70  # the first sentence may be cut at a comma once it is this long (sooner first audio)
MAX_CHARS = 240  # any piece longer than this is cut at a comma or space
SILENT = "silent"

_TAG = re.compile(r"\[\s*([a-zA-Z_ ]{2,24})\s*\]")
_ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "st", "vs", "etc", "jr", "sr", "prof", "inc", "ltd", "no",
                            "approx", "dept", "est", "fig", "mt", "ft", "e.g", "i.e", "a.m", "p.m", "u.s", "u.k"})
_END = re.compile(r"([.!?…]+)([\"')\]]*)(\s+)")
_SOFT = re.compile(r"([,;:—–])\s+")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)")
_BULLET = re.compile(r"(?m)^\s*(?:[-*•]+|\d+[.)])\s+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SYMBOLS = re.compile(r"[*_`#>|~^]+")
_SPACES = re.compile(r"\s+")
ANIMATION_TAGS = frozenset(FACE_ANIMATIONS)
EXPRESSION_TAGS = frozenset(EXPRESSIONS)


@dataclasses.dataclass(frozen=True)
class Sentence:
    text: str  # what to say (clean)
    expression: str | None = None
    animation: str | None = None
    index: int = 0


def clean(text: str) -> str:
    """Words for text-to-speech: markdown, links, emoji and symbols out, spaces tidied."""
    t = _MD_LINK.sub(r"\1", text)
    t = _URL.sub("", t)
    t = _BULLET.sub("", t)
    t = _EMOJI.sub("", t)
    t = _SYMBOLS.sub("", t)
    t = t.replace("&", " and ").replace(" / ", " or ")
    return _SPACES.sub(" ", t).strip()


def split_tags(text: str) -> tuple[str, str | None, str | None]:
    """``text`` without its face tags, and the first expression and animation tag in it."""
    expression = animation = None
    for m in _TAG.finditer(text):
        word = m.group(1).strip().lower().replace(" ", "_")
        if word in EXPRESSION_TAGS and expression is None:
            expression = word
        elif word in ANIMATION_TAGS and animation is None:
            animation = word
    return _TAG.sub(lambda m: "" if _is_tag(m.group(1)) else m.group(0), text), expression, animation


def _is_tag(word: str) -> bool:
    w = word.strip().lower().replace(" ", "_")
    return w in EXPRESSION_TAGS or w in ANIMATION_TAGS or w == SILENT or (len(w.split("_")) <= 2 and w.isalpha())


def is_silent(text: str) -> bool:
    """The whole answer is ``[silent]`` (the brain heard people talking to each other, not to LittleBot)."""
    return re.fullmatch(r"\s*\[\s*silent\s*\]\s*\.?\s*", text, re.IGNORECASE) is not None


class SentenceSplitter:
    """Feed the brain's deltas; get whole sentences back (``feed``), and the rest at the end (``flush``)."""

    def __init__(self, *, first_cut_chars: int = FIRST_CUT_CHARS, max_chars: int = MAX_CHARS) -> None:
        self.first_cut = int(first_cut_chars)
        self.max_chars = int(max_chars)
        self._buf = ""
        self._count = 0
        self._pending_expression: str | None = None  # a tag on its own: goes with the next sentence
        self._pending_animation: str | None = None
        self.said: list[str] = []  # every sentence handed out (what LittleBot is saying)

    @property
    def count(self) -> int:
        return self._count

    def feed(self, delta: str) -> list[Sentence]:
        self._buf += delta
        out: list[Sentence] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            piece, self._buf = self._buf[:cut], self._buf[cut:]
            s = self._make(piece)
            if s is not None:
                out.append(s)
        return out

    def flush(self) -> list[Sentence]:
        piece, self._buf = self._buf, ""
        s = self._make(piece)
        return [s] if s is not None else []

    def _find_cut(self) -> int | None:
        buf = self._buf
        for m in _END.finditer(buf):
            before = buf[: m.start()]
            word = re.search(r"([A-Za-z.]+)$", before)
            if m.group(1) == "." and word and word.group(1).lower().rstrip(".") in _ABBREVIATIONS:
                continue
            if m.group(1) == "." and re.search(r"\b[A-Z]$", before):  # an initial: "J. R. R."
                continue
            return m.end()
        nl = buf.find("\n")
        if nl >= 0 and buf[:nl].strip():
            return nl + 1
        limit = self.first_cut if self._count == 0 else self.max_chars
        if len(buf) >= limit:
            soft = [m.end() for m in _SOFT.finditer(buf) if m.end() >= 12]
            if soft:
                return soft[-1] if self._count else soft[0]
            if len(buf) >= self.max_chars:
                space = buf.rfind(" ", 0, self.max_chars)
                return space + 1 if space > 0 else self.max_chars
        return None

    def _make(self, piece: str) -> Sentence | None:
        text, expression, animation = split_tags(piece)
        expression = expression or self._pending_expression
        animation = animation or self._pending_animation
        words = clean(text)
        if not re.search(r"[A-Za-z0-9]", words):
            self._pending_expression, self._pending_animation = expression, animation
            return None
        self._pending_expression = self._pending_animation = None
        s = Sentence(words, expression, animation, self._count)
        self._count += 1
        self.said.append(words)
        return s


__all__ = ["SILENT", "Sentence", "SentenceSplitter", "clean", "is_silent", "split_tags"]
