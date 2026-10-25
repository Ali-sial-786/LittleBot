"""OpenAI's Decisions API (``POST /v1/decisions``, gpt-6-luna): the same questions as Jev, in OpenAI's shape.

Request: ``{"model", "input": [{"role": "user", "content": [{"type": "input_json", "data": <state>}]}],
"questions": [{"type": "predicate" | "choice" | "score", "name", "instructions", "choices": [{"value",
"description"}] | "levels": [{"label", "description"}]}]}``. Answers: ``{"answers": [{"name", "type",
"probability" | "choice" ... | "score" ..., "probabilities", "confidence"} | {"type": "refusal"}]}``. See
https://developers.openai.com/api/docs/guides/decisions.
"""

from __future__ import annotations

from typing import Any

from littlebot.decide.base import Answer, Choice, DecideError, Question, Score, YesNo, choice_confidence, clip
from littlebot.decide.http import post_json


def request_body(model: str, state: Any, questions: dict[str, Question]) -> dict[str, Any]:
    content: list[dict[str, Any]]
    if isinstance(state, str):
        content = [{"type": "input_text", "text": state}]
    else:
        content = [{"type": "input_json", "data": state}]
    qs: list[dict[str, Any]] = []
    for name, q in questions.items():
        if isinstance(q, Choice):
            qs.append({"type": "choice", "name": name, "instructions": q.instructions,
                       "choices": [{"value": k, "description": v or k} for k, v in q.options.items()]})
        elif isinstance(q, YesNo):
            text = q.instructions
            if q.yes or q.no:
                text += f" (Yes: {q.yes or 'yes'}. No: {q.no or 'no'}.)"
            qs.append({"type": "predicate", "name": name, "instructions": text})
        elif isinstance(q, Score):
            qs.append({"type": "score", "name": name, "instructions": q.instructions,
                       "levels": [{"label": f"level {i}", "description": d} for i, d in enumerate(q.levels)]})
    return {"model": model, "input": [{"role": "user", "content": content}], "questions": qs}


def parse_answers(data: Any, questions: dict[str, Question]) -> dict[str, Answer]:
    raw = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise DecideError("bad_answer", "no answers")
    out: dict[str, Answer] = {}
    for a in raw:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        q = questions.get(name)
        kind = a.get("type")
        if q is None or kind == "refusal":
            continue
        if isinstance(q, Choice) and kind == "choice":
            choice = str(a.get("choice") or "")
            if choice not in q.options:
                continue
            probs = {str(p.get("value")): clip(p.get("probability")) for p in a.get("probabilities") or []
                     if isinstance(p, dict) and str(p.get("value")) in q.options}
            conf = a.get("confidence")
            out[name] = Answer("choice", choice, clip(conf) if conf is not None else choice_confidence(probs), probs)
        elif isinstance(q, YesNo) and kind == "predicate" and a.get("probability") is not None:
            p = clip(a.get("probability"))
            out[name] = Answer("yesno", p, abs(2 * p - 1), {"yes": p, "no": 1 - p})
        elif isinstance(q, Score) and kind == "score" and a.get("score") is not None:
            probs = {str(p.get("value")): clip(p.get("probability")) for p in a.get("probabilities") or []
                     if isinstance(p, dict)}
            out[name] = Answer("score", float(a.get("score") or 0.0), clip(a.get("confidence")), probs)
    return out


class OpenAIDecisionsClient:
    name = "openai"

    def __init__(self, api_key: str, *, model: str = "gpt-6-luna", base_url: str = "https://api.openai.com/v1",
                 transport: Any = None) -> None:
        self._key = api_key
        self.model = model
        self._url = base_url.rstrip("/") + "/decisions"
        self._transport = transport

    async def ask(self, state: Any, questions: dict[str, Question], *, timeout: float) -> dict[str, Answer]:
        if not self._key:
            raise DecideError("no_key", "OPENAI_API_KEY isn't set")
        data = await post_json(self._url, request_body(self.model, state, questions), key=self._key,
                               timeout=timeout, transport=self._transport)
        return parse_answers(data, questions)


__all__ = ["OpenAIDecisionsClient", "parse_answers", "request_body"]
