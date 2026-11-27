"""One INFO line per text-model call: which provider and model, what for, how soon the first word came and how long
the whole answer took (tool rounds included), so ChatGPT and Claude can be compared from the robot's own log:

    text model: provider=claude model=claude-haiku-5-5 purpose=handoff result=ok first_token_ms=612 total_ms=1480
    tool_rounds=1 tokens_in=2210 tokens_out=74 cache_read=1830

Only labels and numbers: never the prompt, the answer or a key. ``first_token_ms`` is the first piece of the
answer's words or of a tool call (whichever came first), measured from the call's start.
"""

from __future__ import annotations

import logging
import re
import time

log = logging.getLogger("littlebot.text_model")

_LABEL = re.compile(r"[^A-Za-z0-9_.:\-]+")


def _label(value: object) -> str:
    return _LABEL.sub("_", str(value or "-"))[:60] or "-"


class CallTimer:
    """Times one call: ``first()`` when the first piece of the answer arrives, ``done(...)`` logs the line."""

    def __init__(self, provider: str, purpose: str = "", clock=time.monotonic) -> None:
        self.provider = provider
        self.purpose = purpose or "text"
        self._clock = clock
        self.started = clock()
        self.first_at: float | None = None
        self.rounds = 0  # tool rounds so far

    def first(self) -> None:
        if self.first_at is None:
            self.first_at = self._clock()

    @property
    def first_ms(self) -> int | None:
        return None if self.first_at is None else int((self.first_at - self.started) * 1000)

    @property
    def total_ms(self) -> int:
        return int((self._clock() - self.started) * 1000)

    def done(self, model: str, *, result: str = "ok", rounds: int | None = None, tokens_in: int = 0,
             tokens_out: int = 0, cache_read: int = 0, note: str = "") -> None:
        first = self.first_ms
        rounds = self.rounds if rounds is None else rounds
        log.info("text model: provider=%s model=%s purpose=%s result=%s first_token_ms=%s total_ms=%d tool_rounds=%d "
                 "tokens_in=%d tokens_out=%d cache_read=%d%s", _label(self.provider), _label(model),
                 _label(self.purpose), _label(result), "-" if first is None else first, self.total_ms, rounds,
                 tokens_in, tokens_out, cache_read, f" note={_label(note)}" if note else "")


__all__ = ["CallTimer"]
