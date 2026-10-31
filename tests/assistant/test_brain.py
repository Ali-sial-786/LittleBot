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


async def _voice_turn(script: list[Any], text: str = "hi", *, first_today: bool = False,
                      **kw: Any) -> tuple[Any, list[str], ScriptedProvider]:
    order: list[str] = []
    p = ScriptedProvider(script)
    p.log = order
    ctx = make_test_context()
    brain = Brain(ctx, provider=p)
    if not first_today:
        brain.talked_today()
    res = await brain.respond(text, history=[], toolbox=RobotToolbox(ctx, mode="chat"),
                              on_sentence=lambda s: order.append(f"say:{s.text}"),
                              on_tool=lambda n: order.append(f"tool:{n}"), **kw)
    return res, order, p


async def test_a_tool_before_any_word_gets_a_word_of_its_own_first() -> None:
    # "What's in the news today?": the brain fetched the briefing first and nothing was heard for 3.7 s (the robot,
    # 2026-10-31). Now a word is said as the tool starts, and the model is told so (no "One sec." twice).
    res, order, p = await _voice_turn([("tool", "get_time", "{}"), "Rain tomorrow."], "will it rain",
                                      first_words=True)
    assert order == ["say:One sec.", "tool:get_time", "words:Rain tomorrow.", "say:Rain tomorrow."]
    assert p.calls[0]["results"][0].endswith('(they have already heard you say "One sec.": go straight on, never '
                                             'say it again)')
    assert res.text == "Rain tomorrow." and res.done[0].startswith("get_time: ") and "heard" not in res.done[0]
    _, order, _ = await _voice_turn([("tool", "set_volume", '{"percent": 60}'), "Done, it's at 60."], first_words=True)
    assert order[:2] == ["say:Sure.", "tool:set_volume"]  # an action: "Sure."
    _, order, _ = await _voice_turn([("search",), "Spain won."], first_words=True)
    assert order[0] == "say:One sec."  # Anthropic's own web search too


async def test_an_action_told_before_its_tool_is_not_told_again() -> None:
    # "Got it, I'll remember that your favorite color is green. Done, I've saved that green is your favorite
    # color." (the robot, 2026-10-31): the tool's result says what they already heard
    res, order, p = await _voice_turn(["Got it, I'll turn it up.", ("tool", "set_volume", '{"percent": 60}'),
                                       " Done, I've turned it up. It's at 60 now."], first_words=True)
    assert p.calls[0]["results"][0].endswith(
        '(they have already heard you say "Got it, I\'ll turn it up.": if it worked, say nothing more about it (no '
        '"Done, I\'ve ..."), only anything new; if it failed, say so)')
    # and a "done" said anyway is not said ("I'll forget that for you. Done, I've forgotten that.")
    assert [o for o in order if o.startswith("say:")] == ["say:Got it, I'll turn it up.", "say:It's at 60 now."]
    assert res.text == "Got it, I'll turn it up. It's at 60 now."
    _, order, _ = await _voice_turn([("tool", "set_volume", '{"percent": 60}'), "Done, I've turned it up."],
                                    first_words=True)
    assert order[-1] == "say:Done, I've turned it up."  # after only "Sure.", it is the confirmation
    # a look-up still gets its answer after "Let me check."; a second tool, with nothing said since, no note
    _, _, p = await _voice_turn(["Let me check.", ("tool", "get_volume", "{}"), ("tool", "set_volume",
                                                                                  '{"percent": 60}')])
    assert all("already heard" not in r for r in p.calls[0]["results"])


async def test_on_the_first_chat_of_the_day_that_word_is_the_greeting(monkeypatch) -> None:
    # "One sec. Good morning! No, it looks dry tomorrow." (the robot's brain, 2026-10-31)
    import littlebot.assistant.agent as agent

    monkeypatch.setattr(agent, "_greeting", lambda ctx: "Good morning.")
    _, order, p = await _voice_turn([("tool", "get_time", "{}"), "Dry tomorrow."], first_words=True,
                                    first_today=True)
    assert order[:2] == ["say:Good morning.", "tool:get_time"]
    assert 'heard you say "Good morning."' in p.calls[0]["results"][0]
    monkeypatch.setattr(agent, "_greeting", lambda ctx: "")  # at night: the usual word
    _, order, _ = await _voice_turn([("tool", "get_time", "{}"), "Dry."], first_words=True, first_today=True)
    assert order[0] == "say:One sec."
    assert agent._greeting(make_test_context()) in ("Good morning.", "Good afternoon.", "Good evening.", "")


async def test_no_word_of_its_own_after_its_words_for_stops_and_cards_or_typed_chats() -> None:
    _, order, p = await _voice_turn(["Let me check.", ("tool", "get_time", "{}"), "Dry."], first_words=True)
    assert order.count("say:One sec.") == 0 and "heard you say" not in p.calls[0]["results"][0]
    for tool in ("stop_video", "show_on_screen"):
        _, order, _ = await _voice_turn([("tool", tool, "{}"), "Okay."], first_words=True)
        assert order[0] == f"tool:{tool}"
    _, order, _ = await _voice_turn([("tool", "get_time", "{}"), "Dry."])  # not a voice: no first words
    assert order[0] == "tool:get_time"


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


async def test_its_own_reasoning_is_never_said() -> None:
    # "Resetting a tripped breaker is general how-to knowledge, so I can answer directly." (the robot, 2026-10-31)
    p = ScriptedProvider(["Resetting a tripped breaker is general how-to knowledge, so I can answer directly. ",
                          "Push the switch to off, then back on."])
    res, said, _ = await _turn(p, "how do I reset a tripped breaker")
    assert [s.text for s in said] == ["Push the switch to off, then back on."]
    assert res.text == "Push the switch to off, then back on."
    assert "never your reasoning" in p.calls[0]["system"] and "Which card:" in p.calls[0]["system"]
    assert "{cards}" not in p.calls[0]["system"]
    for line in ("So I can answer that, I'd need your town.", "Tell me more so I can answer better."):
        _, said, _ = await _turn(ScriptedProvider([line]))  # not its reasoning
        assert len(said) == 1, line
    for line in ("I know this one, so I can answer it directly.", "No web search needed, it's easy."):
        _, said, _ = await _turn(ScriptedProvider([line]))
        assert said == [], line


async def test_a_sum_said_in_words_comes_with_its_exact_result(caplog) -> None:
    # "The square root of seventeen sixty four is about forty-one point nine." (the robot, 2026-10-31)
    p = ScriptedProvider(["It's 42."])
    await _turn(p, "What is the square root of seventeen sixty four?")
    assert "the sum they asked, worked out exactly: square root of 1764 = 42" in p.calls[0]["messages"][-1]["content"]
    p = ScriptedProvider(["Paris."])
    await _turn(p, "What is the capital of France?")
    assert "worked out exactly" not in p.calls[0]["messages"][-1]["content"]
    caplog.set_level("WARNING", logger="littlebot.assistant.agent")
    await _turn(ScriptedProvider(["Done, I've forgotten your favorite color."]), "forget my favorite color")
    assert "says something was done, but no tool ran" in caplog.text


class CardBox:
    """A toolbox with show_on_screen (and another tool) that records the cards put up."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, Any]] = []

    def specs(self, stable: bool = False) -> list[dict[str, Any]]:
        return [{"type": "function", "name": "get_time", "parameters": {"type": "object"}},
                {"type": "function", "name": "show_on_screen", "parameters": {"type": "object"}}]

    async def run(self, name: str, arguments: Any = None) -> str:
        self.ran.append((name, arguments))
        return "ok: on the screen now: Resetting a breaker: about to show 3 points."


BREAKER = ("First find the panel and the switch sitting between on and off. Push it firmly to off, then back on. "
           "If it trips again, unplug what's on that circuit and call an electrician.")
BREAKER_CARD = '{"card": "explain", "fields": {"title": "Resetting a breaker", "points": ["Find the panel"]}}'


async def test_a_card_the_brain_skipped_is_made_from_the_answer() -> None:
    # "How do I reset a tripped breaker?" never got its card (0 of 4 on the robot, 2026-10-31)
    p = ScriptedProvider([("tool", "show_on_screen", BREAKER_CARD), "Done."])
    box = CardBox()
    out = await Brain(make_test_context(), provider=p).card_after("How do I reset a tripped breaker?", BREAKER, box)
    assert out is not None and out.startswith("ok: on the screen now") and box.ran == [("show_on_screen",
                                                                                         BREAKER_CARD)]
    call = p.calls[0]
    assert call["tools"] == ["show_on_screen"] and call["effort"] == "minimal" and "explain" in call["system"]
    assert call["messages"] == [{"role": "user", "content": f"They asked: How do I reset a tripped breaker?\n"
                                                            f"It said: {BREAKER}"}]


async def test_no_backup_card_when_none_is_needed_or_it_is_too_late() -> None:
    ctx = make_test_context()
    box = CardBox()
    assert await Brain(ctx, provider=ScriptedProvider(["none"])).card_after("hi", BREAKER, box) is None
    late = ScriptedProvider([("tool", "show_on_screen", BREAKER_CARD)])
    assert await Brain(ctx, provider=late).card_after("q", BREAKER, box, still=lambda: False) is None
    shows = ScriptedProvider(["none"])
    await Brain(ctx, provider=shows).card_after("q", BREAKER, box, showing="a facts card: Japan")
    assert shows.calls[0]["messages"][0]["content"].endswith("\nThe screen shows now: a facts card: Japan")
    short = ScriptedProvider([("tool", "show_on_screen", BREAKER_CARD)])
    assert await Brain(ctx, provider=short).card_after("thanks", "You're welcome!", box) is None
    assert short.calls == [] and box.ran == []
    failed = ScriptedProvider([], error="timeout")
    assert await Brain(ctx, provider=failed).card_after("q", BREAKER, box) is None


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
