"""Claude as a decision model (``decide.provider: claude``): the same questions as Jev and OpenAI's Decisions API,
answered as structured JSON (``output_config.format``) by the text model's provider (littlebot/brain/providers/
claude.py, ANTHROPIC_API_KEY, ``decide.claude_model`` or ``claude.model``).

Claude gives no probabilities of its own: each answer carries the model's stated confidence (0..1). A choice's
``probabilities`` therefore hold only the chosen option; a yes/no's is the stated probability of yes; a score's
value is the level it chose. So its confidences are judgements, not measured probabilities: ``decide.min_confidence``
may need a different value for it than for Jev (compare them in the Decisions panel before switching a feature on).
"""

from __future__ import annotations

import json
from typing import Any

from littlebot.brain.providers.base import ProviderError
from littlebot.decide.base import Answer, Choice, DecideError, Question, Score, YesNo, clip

SYSTEM = ("You make quick decisions for LittleBot, a small talking robot for a family with a young child. You are "
          "shown what is going on and some questions. Answer every question from what you are shown, and say how "
          "sure you are from 0 (a guess) to 1 (certain). Be calibrated: when it is unclear, say so with a low "
          "number.")


def question_list(questions: dict[str, Question]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, q in questions.items():
        if isinstance(q, Choice):
            out[name] = {"type": "choice", "instructions": q.instructions, "options": dict(q.options)}
        elif isinstance(q, YesNo):
            item: dict[str, Any] = {"type": "yes_no", "instructions": q.instructions}
            if q.yes or q.no:
                item["yes_means"], item["no_means"] = q.yes or "yes", q.no or "no"
            out[name] = item
        elif isinstance(q, Score):
            out[name] = {"type": "score", "instructions": q.instructions,
                         "levels": {str(i): d for i, d in enumerate(q.levels)}}
    return out


def schema(questions: dict[str, Question]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for name, q in questions.items():
        if isinstance(q, Choice):
            props[name] = {"type": "object", "properties": {"choice": {"type": "string", "enum": list(q.options)},
                                                            "confidence": {"type": "number"}},
                           "required": ["choice", "confidence"], "additionalProperties": False}
        elif isinstance(q, YesNo):
            props[name] = {"type": "object", "properties": {"probability_yes": {"type": "number"}},
                           "required": ["probability_yes"], "additionalProperties": False}
        elif isinstance(q, Score):
            props[name] = {"type": "object", "properties": {"level": {"type": "integer"},
                                                            "confidence": {"type": "number"}},
                           "required": ["level", "confidence"], "additionalProperties": False}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def parse_answers(data: Any, questions: dict[str, Question]) -> dict[str, Answer]:
    if not isinstance(data, dict):
        raise DecideError("bad_answer", "not an object")
    out: dict[str, Answer] = {}
    for name, q in questions.items():
        a = data.get(name)
        if not isinstance(a, dict):
            continue
        if isinstance(q, Choice):
            choice = str(a.get("choice") or "")
            if choice in q.options:
                conf = clip(a.get("confidence"))
                out[name] = Answer("choice", choice, conf, {choice: conf})
        elif isinstance(q, YesNo) and a.get("probability_yes") is not None:
            p = clip(a.get("probability_yes"))
            out[name] = Answer("yesno", p, abs(2 * p - 1), {"yes": p, "no": 1 - p})
        elif isinstance(q, Score) and isinstance(a.get("level"), (int, float)):
            level = max(0, min(len(q.levels) - 1, int(a["level"])))
            out[name] = Answer("score", float(level), clip(a.get("confidence")), {str(level): clip(a.get("confidence"))})
    return out


class ClaudeDecisionsClient:
    name = "claude"

    def __init__(self, provider: Any) -> None:
        self.provider = provider  # a ClaudeProvider (purpose "decide")
        self.model = getattr(provider, "model", "")

    async def ask(self, state: Any, questions: dict[str, Question], *, timeout: float) -> dict[str, Answer]:
        missing = self.provider.missing()
        if missing:
            raise DecideError("no_key" if missing == "no_key" else missing)
        user = json.dumps({"what_is_going_on": state, "questions": question_list(questions)}, ensure_ascii=False)
        try:
            data = await self.provider.ask_json(system=SYSTEM, user=user, schema=schema(questions), timeout=timeout)
        except ProviderError as e:
            status = getattr(e, "status_code", None)
            raise DecideError(f"http_{status}" if e.kind == "auth_error" and status else e.kind) from None
        return parse_answers(data, questions)


__all__ = ["ClaudeDecisionsClient", "parse_answers", "question_list", "schema"]
