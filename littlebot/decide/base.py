"""The questions a decision model answers, its answers, and the clients' common error.

Three kinds of question, the same for both providers (littlebot/decide/jev.py, littlebot/decide/openai.py):

* ``Choice``: one option from a fixed set (``options``: name -> when it applies). Answer: the option, a
  probability per option and a confidence (0: an even split, 1: certain).
* ``YesNo``: the probability that a statement is true. Answer: ``value`` is that probability; its confidence is
  ``|2p - 1|``.
* ``Score``: one level of an ordered rubric (``levels``, lowest first). Answer: ``value`` is the
  probability-weighted level (0 = the first), with a probability per level and a confidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

FEATURES = ("cards", "routing", "answered", "browser", "naming", "videos", "names", "concern",
            "addressed")
MODES = ("off", "shadow", "on")
PROVIDERS = ("off", "jev", "openai", "claude")


@dataclass(frozen=True)
class Choice:
    instructions: str
    options: dict[str, str]


@dataclass(frozen=True)
class YesNo:
    instructions: str
    yes: str = ""  # what counts as yes (optional)
    no: str = ""


@dataclass(frozen=True)
class Score:
    instructions: str
    levels: tuple[str, ...]


Question = Choice | YesNo | Score


@dataclass
class Answer:
    kind: str  # choice | yesno | score
    value: Any  # the option (choice), the probability of yes (yesno), the weighted level (score)
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    state: Any = field(default=None, repr=False, compare=False)  # what the decider was shown (for the log)

    @property
    def p(self) -> float:
        """YesNo: the probability of yes."""
        return float(self.value) if self.kind == "yesno" else 0.0


class DecideError(Exception):
    """A decision request that failed: ``kind`` (no_key, http_401, http_429, timeout, network, bad_answer...)."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


def choice_confidence(probs: dict[str, float]) -> float:
    """TypeSafe's choice confidence: how far the top probability sits above an even split (0..1)."""
    n = len(probs)
    if n < 2:
        return 1.0 if probs else 0.0
    top = max(probs.values())
    return max(0.0, min(1.0, (top - 1 / n) / (1 - 1 / n)))


def clip(p: Any) -> float:
    try:
        v = float(p)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) else max(0.0, min(1.0, v))


__all__ = ["FEATURES", "MODES", "PROVIDERS", "Answer", "Choice", "DecideError", "Question", "Score", "YesNo",
           "choice_confidence", "clip"]
