"""The assistant's brain: one streamed call, sentences out early, tools, cards as final tools, think_harder."""

from __future__ import annotations

from typing import Any

from littlebot.assistant.agent import DEEP_TOOL, FINAL_TOOLS, Brain
from littlebot.brain.providers.base import ProviderError, ProviderReply
from littlebot.chatgpt.tools import RobotToolbox
from littlebot.testing import make_test_context


class ScriptedProvider:
    """Streams ``pieces``; between them, ``("tool", name, args)`` entries run a tool and ``("search",)`` starts a web
    search (Anthropic's own: no ``run_tool``)."""

    name = "claude"
    model = "claude-haiku-5-5"

    def __init__(self, script: list[Any], *, error: str | None = None) -> None:
        self.script = script
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.log: list[str] | None = None  # "words:<piece>" as each piece is streamed

    def configured(self) -> bool:
        return True

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: Any = None, run_tool: Any = None, max_tool_rounds: int = 6, effort: Any = None,
                    on_delta: Any = None, final_tools: Any = (), images: Any = None,
                    on_search: Any = None) -> ProviderReply:
        self.calls.append({"messages": messages, "system": system, "tools": [t.get("name") for t in tools or []],
                           "effort": effort, "final_tools": set(final_tools)})
        if self.error:
            raise ProviderError("nope", kind=self.error)
        text = ""
        for step in self.script:
            if step == ("search",):
                if on_search is not None:
                    on_search()
            elif isinstance(step, tuple):
                self.calls[-1].setdefault("results", []).append(await run_tool(step[1], step[2]))
            else:
                text += step
                if self.log is not None:
                    self.log.append(f"words:{step}")
                if on_delta is not None:
                    on_delta(step)
        return ProviderReply(text=text, model=self.model)


async def _turn(provider: ScriptedProvider, text: str = "hi", **kw: Any) -> tuple[Any, list[Any], list[str]]:
    ctx = make_test_context()
    brain = Brain(ctx, provider=provider)
    said: list[Any] = []
    tools: list[str] = []
    box = RobotToolbox(ctx, mode="chat")
    res = await brain.respond(text, history=kw.get("history", []), toolbox=box, on_sentence=said.append,
                              on_tool=tools.append)
    return res, said, tools


async def test_sentences_stream_with_expressions_and_context() -> None:
    p = ScriptedProvider(["[happy] It's 12 degrees", " and sunny. [curious] Going", " out?"])
    res, said, _ = await _turn(p, "what's the weather", history=[{"role": "user", "content": "hello"},
                                                                {"role": "assistant", "content": "Hi!"}])
    assert [(s.text, s.expression) for s in said] == [("It's 12 degrees and sunny.", "happy"),
                                                      ("Going out?", "curious")]
    assert res.text == "It's 12 degrees and sunny. Going out?" and res.error is None
    call = p.calls[0]
    assert call["effort"] == "minimal" and FINAL_TOOLS <= call["final_tools"]
    last = call["messages"][-1]["content"]
    assert last.startswith("[Context for this turn") and last.endswith("what's the weather")
    assert call["messages"][0] == {"role": "user", "content": "hello"}
    assert "LittleBot" in call["system"] and "[happy]" in call["system"]
    assert "never answer it from memory" in call["system"]  # recent facts are looked up
    assert "get_weather" in call["tools"] and len(res.prefix) == 6


async def test_words_before_a_tool_are_said_before_it_runs() -> None:
    order: list[str] = []
    p = ScriptedProvider(["Let me check", ("tool", "get_volume", "{}"), "It's at half."])

    ctx = make_test_context()
    brain = Brain(ctx, provider=p)
    box = RobotToolbox(ctx, mode="chat")
    res = await brain.respond("how loud", history=[], toolbox=box,
                              on_sentence=lambda s: order.append(f"say:{s.text}"),
                              on_tool=lambda n: order.append(f"tool:{n}"))
    assert order == ["say:Let me check", "tool:get_volume", "say:It's at half."]
    assert res.tools == ["get_volume"] and p.calls[0]["results"][0].startswith("{")
    assert res.done == ["get_volume: " + p.calls[0]["results"][0][:120]]  # kept for the next turns


async def test_words_before_a_web_search_are_said_as_it_starts() -> None:
    # Anthropic runs the search itself (no run_tool): "Let me check that for you." waited for the next words, 2.7 s
    # after the question (the robot, 2026-10-31).
    order: list[str] = []
    p = ScriptedProvider(["Let me check that for you.", ("search",), " Spain won."])
    p.log = order
    ctx = make_test_context()
    res = await Brain(ctx, provider=p).respond("who won", history=[], toolbox=RobotToolbox(ctx, mode="chat"),
                                               on_sentence=lambda s: order.append(f"say:{s.text}"))
    assert order == ["words:Let me check that for you.", "say:Let me check that for you.", "words: Spain won.",
                     "say:Spain won."]
    assert res.text == "Let me check that for you. Spain won."


async def test_silent_says_nothing() -> None:
    res, said, _ = await _turn(ScriptedProvider(["[sil", "ent]"]))
    assert res.silent and said == [] and res.error is None


async def test_failure_is_a_kind_not_an_exception() -> None:
    res, said, _ = await _turn(ScriptedProvider([], error="timeout"))
    assert res.error == "timeout" and said == []


async def test_unsafe_sentence_replaced_and_rest_dropped() -> None:
    p = ScriptedProvider(["Give the baby 5 ml of ibuprofen. ", "Then more stuff."])
    _, said, _ = await _turn(p, "how much medicine")
    assert len(said) == 1 and "5 ml" not in said[0].text


async def test_think_harder_uses_the_deep_model() -> None:
    deep = ScriptedProvider(["The answer is 42 because of careful reasoning."])
    p = ScriptedProvider(["Let me think.", ("tool", DEEP_TOOL, '{"question": "life?"}'), "It's 42."])
    ctx = make_test_context()
    brain = Brain(ctx, provider=p, deep=deep)
    said: list[Any] = []
    res = await brain.respond("meaning of life", history=[], toolbox=RobotToolbox(ctx), on_sentence=said.append)
    assert DEEP_TOOL in p.calls[0]["tools"]
    assert p.calls[0]["results"][0].startswith("ok: The answer is 42")
    assert [s.text for s in said] == ["Let me think.", "It's 42."] and res.tools == [DEEP_TOOL]


async def test_first_conversation_of_the_day_offers_a_greeting_once() -> None:
    ctx = make_test_context()
    p = ScriptedProvider(["Good morning!"])
    brain = Brain(ctx, provider=p)
    assert brain.first_today()
    await brain.respond("hi", history=[], toolbox=RobotToolbox(ctx), on_sentence=lambda s: None)
    assert "first conversation today" in p.calls[0]["messages"][-1]["content"]
    assert not brain.first_today()
    await brain.respond("hi again", history=[], toolbox=RobotToolbox(ctx), on_sentence=lambda s: None)
    assert "first conversation today" not in p.calls[1]["messages"][-1]["content"]
