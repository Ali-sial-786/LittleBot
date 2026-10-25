"""littlebot/decide: the Jev and OpenAI Decisions clients (requests, answers, errors) and the decider's gates."""

from __future__ import annotations

import json
from typing import Any

import httpx

from littlebot.core.config import load_config
from littlebot.decide import Choice, DecideError, Decider, Score, YesNo, get_decider
from littlebot.decide.jev import JevClient
from littlebot.decide.openai import OpenAIDecisionsClient
from littlebot.testing import decision_answers as choose

QUESTIONS = {
    "route": Choice("Which department?", {"billing": "Payments", "tech": "Problems"}),
    "spam": YesNo("Is this spam?", yes="Advertising"),
    "urgency": Score("How urgent?", ("Can wait", "Today")),
}


def _transport(seen: list[dict[str, Any]], body: Any, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                     "json": json.loads(request.content)})
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


async def test_jev_request_and_answers() -> None:
    seen: list[dict[str, Any]] = []
    body = {"model": "jev-1.14.0", "usage": {"input_tokens": 9, "output_tokens": 3}, "answers": {
        "route": {"type": "choice", "choice": "billing", "confidence": 0.8, "probabilities": {"billing": 0.9,
                                                                                              "tech": 0.1}},
        "spam": {"type": "noul", "noul": 0.2},
        "urgency": {"type": "score", "score": 0.7, "confidence": 0.4, "probabilities": {"0": 0.3, "1": 0.7}}}}
    client = JevClient("sk-test", transport=_transport(seen, body))
    answers = await client.ask({"message": "charged twice"}, QUESTIONS, timeout=2)
    req = seen[0]
    assert req["url"] == "https://api.typesafe.ai/v1/systemone" and req["auth"] == "Bearer sk-test"
    assert req["json"]["model"] == "jev-latest" and req["json"]["state"] == {"message": "charged twice"}
    assert req["json"]["questions"]["route"] == {"type": "choice", "instructions": "Which department?",
                                                 "criteria": {"billing": "Payments", "tech": "Problems"}}
    assert req["json"]["questions"]["spam"] == {"type": "noul", "instructions": "Is this spam?",
                                                "criteria": {"true": "Advertising", "false": "No"}}
    assert req["json"]["questions"]["urgency"]["criteria"] == ["Can wait", "Today"]
    assert answers["route"].value == "billing" and answers["route"].confidence == 0.8
    assert answers["spam"].p == 0.2 and round(answers["spam"].confidence, 2) == 0.6
    assert answers["urgency"].value == 0.7


async def test_openai_decisions_request_and_answers() -> None:
    seen: list[dict[str, Any]] = []
    body = {"answers": [
        {"type": "choice", "name": "route", "choice": "tech", "confidence": 0.93,
         "probabilities": [{"value": "billing", "probability": 0.03}, {"value": "tech", "probability": 0.97}]},
        {"type": "predicate", "name": "spam", "probability": 0.9},
        {"type": "refusal", "name": "urgency"}]}
    client = OpenAIDecisionsClient("sk-oa", transport=_transport(seen, body))
    answers = await client.ask({"message": "it crashes"}, QUESTIONS, timeout=2)
    req = seen[0]["json"]
    assert seen[0]["url"] == "https://api.openai.com/v1/decisions" and req["model"] == "gpt-6-luna"
    assert req["input"] == [{"role": "user", "content": [{"type": "input_json", "data": {"message": "it crashes"}}]}]
    by_name = {q["name"]: q for q in req["questions"]}
    assert by_name["route"]["choices"] == [{"value": "billing", "description": "Payments"},
                                           {"value": "tech", "description": "Problems"}]
    assert by_name["spam"]["type"] == "predicate" and by_name["urgency"]["levels"][1]["description"] == "Today"
    assert answers["route"].value == "tech" and answers["spam"].p == 0.9 and "urgency" not in answers  # refused


async def test_an_option_that_was_never_offered_is_dropped() -> None:
    body = {"answers": {"route": {"type": "choice", "choice": "made_up", "confidence": 1.0}}}
    answers = await JevClient("k", transport=_transport([], body)).ask("x", QUESTIONS, timeout=2)
    assert answers == {}


async def test_errors() -> None:
    for status, kind in ((401, "http_401"), (429, "http_429"), (529, "http_529")):
        try:
            await JevClient("k", transport=_transport([], {}, status)).ask("x", QUESTIONS, timeout=2)
        except DecideError as e:
            assert e.kind == kind
        else:
            raise AssertionError("no error")
    try:
        await JevClient("").ask("x", QUESTIONS, timeout=2)
    except DecideError as e:
        assert e.kind == "no_key"


def test_the_key_comes_from_the_environment_only() -> None:
    cfg = load_config(environ={"TYPESAFE_API_KEY": "sk-ts"}, use_dotenv=False, include_local=False)
    assert cfg.typesafe_api_key.get_secret_value() == "sk-ts"
    assert "sk-ts" not in json.dumps(cfg.public_dict()) and cfg.public_dict()["has_typesafe_key"] is True
    assert cfg.decide.provider == "off"  # nothing is sent until a parent picks a provider


def test_the_gates(ctx) -> None:
    d = get_decider(ctx)
    assert d.mode("cards") == "off" and d.why_off() == "the provider is off"
    ctx.settings.set("decide.provider", "jev")
    assert d.why_off() == "TYPESAFE_API_KEY isn't set"
    ctx.config.typesafe_api_key = "sk"  # (a plain string is fine for the test)
    ctx.privacy.set(cloud_enabled=False)
    assert d.why_off() == "cloud communication is off (Privacy)"
    ctx.privacy.set(cloud_enabled=True)
    assert d.why_off() == "" and d.mode("cards") == "shadow"  # the default: shadow
    ctx.settings.set("decide.modes", {"cards": "on", "videos": "off"})
    assert (d.mode("cards"), d.mode("videos"), d.mode("routing")) == ("on", "off", "shadow")


class Failing:
    def __init__(self, kind: str) -> None:
        self.kind, self.calls = kind, 0

    async def ask(self, state: Any, questions: Any, *, timeout: float) -> Any:
        self.calls += 1
        raise DecideError(self.kind)


async def test_failures_pause_it_and_a_wrong_key_at_once(ctx) -> None:
    now = [1000.0]
    d = Decider(ctx, now=lambda: now[0])
    ctx.privacy.set(cloud_enabled=True)
    ctx.settings.set("decide.provider", "jev")
    d.client = Failing("timeout")
    for _ in range(ctx.config.decide.failures_to_pause):
        assert await d.ask("cards", "x", QUESTIONS) is None
    assert d.mode("cards") == "off" and "paused" in d.why_off()
    assert await d.ask("cards", "x", QUESTIONS) is None and d.client.calls == ctx.config.decide.failures_to_pause
    now[0] += ctx.config.decide.pause_seconds + 1
    assert d.mode("cards") == "shadow"
    d.client = Failing("http_401")
    await d.ask("cards", "x", QUESTIONS)
    assert d.mode("cards") == "off" and "http_401" in d.why_off()
    assert d.status()["features"]["cards"]["failed"] == ctx.config.decide.failures_to_pause + 1


async def test_the_hourly_limit(ctx, use_model) -> None:
    use_model(choose("billing"))
    ctx.config.decide.max_requests_per_hour = 2
    d = get_decider(ctx)
    assert await d.ask("cards", "x", QUESTIONS) is not None
    assert await d.ask("cards", "x", QUESTIONS) is not None
    assert await d.ask("cards", "x", QUESTIONS) is None and "used up" in d.why_off()


def test_compare_counts_agreement(ctx, use_model, caplog) -> None:
    caplog.set_level("INFO", logger="littlebot.decide")
    use_model(choose("x"))
    d = get_decider(ctx)
    d.compare("cards", mine="none", theirs="none")
    d.compare("cards", mine="none", theirs="show_weather", used=True)
    st = d.status()["features"]["cards"]
    assert (st["agreed"], st["differed"], st["used"], st["agreement"]) == (1, 1, 1, 0.5)
    assert "LittleBot's way none, jev show_weather, different" in caplog.text
