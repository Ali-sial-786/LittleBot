"""TypeSafe's Jev (``POST /v1/systemone``): one state, named typed questions, typed answers.

Request: ``{"model", "state", "questions": {name: {"type": "choice" | "noul" | "score", "instructions",
"criteria"}}}`` with the key as ``Authorization: Bearer``. Answers: ``{"answers": {name: {"type": "choice",
"choice", "confidence", "probabilities"} | {"type": "noul", "noul"} | {"type": "score", "score", "confidence",
"probabilities": {"0": ...}}}}``. See https://docs.typesafe.ai/api/system-one.md.
"""

from __future__ import annotations

from typing import Any

from littlebot.decide.base import Answer, Choice, DecideError, Question, Score, YesNo, choice_confidence, clip
from littlebot.decide.http import post_json


def request_body(model: str, state: Any, questions: dict[str, Question]) -> dict[str, Any]:
    qs: dict[str, Any] = {}
    for name, q in questions.items():
        if isinstance(q, Choice):
            qs[name] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.options)}
        elif isinstance(q, YesNo):
            item: dict[str, Any] = {"type": "noul", "instructions": q.instructions}
            if q.yes or q.no:
                item["criteria"] = {"true": q.yes or "Yes", "false": q.no or "No"}
            qs[name] = item
        elif isinstance(q, Score):
            qs[name] = {"type": "score", "instructions": q.instructions, "criteria": list(q.levels)}
    return {"model": model, "state": state, "questions": qs}


def parse_answers(data: Any, questions: dict[str, Question]) -> dict[str, Answer]:
    raw = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise DecideError("bad_answer", "no answers")
    out: dict[str, Answer] = {}
    for name, q in questions.items():
        a = raw.get(name)
        if not isinstance(a, dict):
            continue
        if isinstance(q, Choice):
            probs = {str(k): clip(v) for k, v in (a.get("probabilities") or {}).items() if str(k) in q.options}
            choice = str(a.get("choice") or "")
            if choice not in q.options:
                continue
            conf = a.get("confidence")
            out[name] = Answer("choice", choice, clip(conf) if conf is not None else choice_confidence(probs), probs)
        elif isinstance(q, YesNo):
            if a.get("noul") is None:
                continue
            p = clip(a.get("noul"))
            out[name] = Answer("yesno", p, abs(2 * p - 1), {"yes": p, "no": 1 - p})
        elif isinstance(q, Score):
            if a.get("score") is None:
                continue
            probs = {str(k): clip(v) for k, v in (a.get("probabilities") or {}).items()}
            out[name] = Answer("score", float(a.get("score") or 0.0), clip(a.get("confidence")), probs)
    return out


class JevClient:
    name = "jev"

    def __init__(self, api_key: str, *, model: str = "jev-latest", base_url: str = "https://api.typesafe.ai",
                 transport: Any = None) -> None:
        self._key = api_key
        self.model = model
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._transport = transport

    async def ask(self, state: Any, questions: dict[str, Question], *, timeout: float) -> dict[str, Answer]:
        if not self._key:
            raise DecideError("no_key", "TYPESAFE_API_KEY isn't set")
        data = await post_json(self._url, request_body(self.model, state, questions), key=self._key,
                               timeout=timeout, transport=self._transport)
        return parse_answers(data, questions)


__all__ = ["JevClient", "parse_answers", "request_body"]
