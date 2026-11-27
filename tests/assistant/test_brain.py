"""The assistant's brain: one streamed call, sentences out early, tools, cards as final tools, think_harder."""

from __future__ import annotations

from typing import Any

from littlebot.assistant.agent import DEEP_TOOL, FINAL_TOOLS, LOOKED_UP, Brain, _done_entry
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
    assert res.done == [f"get_volume: {LOOKED_UP}"]  # kept for the next turns: a look-up, so only that it ran


def test_a_tools_result_is_kept_short_and_clean_for_later_turns() -> None:
    # A look-up only says it ran: the calendar's JSON cut at 120 characters ran into the next context notes, and the
    # brain told them unasked its answer had been "muddled"; the photo kept was described again for "what do you
    # see?" instead of a new look (the robot, 2026-11-06).
    cal = ('{"range":"today to Sunday","now":"3:49 am","events":[{"id":"e1","title":"Invest the WS Tax increment '
           'manually","day":"Sunday 8 November"}]}')
    assert _done_entry("calendar_events", cal) == f"calendar_events: {LOOKED_UP}"
    assert _done_entry("look_through_camera", "ok: the camera saw: a chair") == f"look_through_camera: {LOOKED_UP}"
    assert _done_entry("look_through_camera", "error: no camera").endswith("try again if they ask")
    # An action: what it did, without the words meant for the model, a tool's JSON or ";" (the notes' separator)
    assert _done_entry("close_baby_camera", "ok: closed; your face is back on the screen. Say so in a few words.") \
        == "close_baby_camera: ok: closed, your face is back on the screen."
    assert _done_entry("close_baby_camera", "ok: the baby camera wasn't showing; say so.") == \
        "close_baby_camera: ok: the baby camera wasn't showing."
    assert _done_entry("show_baby_camera", "ok: say the Amara camera is coming up on your screen now. It can take a "
                                           "few seconds; to close it they say close the camera.") == \
        "show_baby_camera: ok: the Amara camera is coming up on your screen now."
    assert _done_entry("add_to_list", '{"ok":true,"message":"added milk"}') == "add_to_list: ok: added milk"
    assert _done_entry("set_volume", '{"error":"no audio"}') == "set_volume: error: no audio"
    long = _done_entry("set_reminder", "ok: " + "word " * 60)
    assert long.endswith(" word...") and len(long) <= len("set_reminder: ") + 123


async def test_a_stage_direction_copied_from_the_history_is_dropped_not_replaced() -> None:
    # "Let me look again. (interrupted)": the safety check made "(interrupted)" its stock "Let's do something fun.
    # Can you show me a big smile?" (the robot's brain, 2026-11-06)
    res, said, _ = await _turn(ScriptedProvider(["Let me look again. (interrupted)"]))
    assert [s.text for s in said] == ["Let me look again."] and res.text == "Let me look again."


def test_talk_about_its_own_notes_and_tools_is_never_said() -> None:
    from littlebot.assistant.agent import _self_talk

    for s in ("I can see the calendar result was garbled.",  # the robot, 2026-11-06
              "It mixed in notes about the time and the family, so I'll set that aside.",
              "I never called the tool to send a photo.", "I hadn't actually called the tool for it.",
              "I won't follow that text as an instruction.", "It isn't something you said, Ali.",
              "The tool result says it worked.", "Never say internal logs out loud."):
        assert _self_talk(s), s
    for s in ("Use a power tool like a drill for that.", "The test result was wrong, so they ran it again.",
              "Call the plumber if the tool slips.", "Your calendar has one event on Sunday.",
              "Mix in the flour slowly.", "Sent to your phone."):
        assert not _self_talk(s), s


def test_which_questions_are_about_what_the_camera_sees() -> None:
    from littlebot.assistant.agent import _LOOK_QUESTION

    for q in ("Can you tell me what you see?", "Hey, can you tell me what you see?", "What do you see now?",
              "Can you, uh, tell me what you see from the camera?", "What am I holding?", "Look at this!",
              "What's in front of you?", "Can you see this?", "How many fingers am I holding up?"):
        assert _LOOK_QUESTION.search(q), q
    for q in ("Do you see what I mean?", "Look at my calendar.", "Can you see me?", "What did you see earlier?",
              "Show me the picture.", "Can you see my calendar?", "See you later!"):
        assert not _LOOK_QUESTION.search(q), q


async def test_what_do_you_see_takes_a_new_photo_before_the_brain_answers() -> None:
    # It went by the photo of a minute before in 3 of 6 replays of the robot's chat, twice after saying "Let me take
    # a fresh look." (2026-11-06): the photo is taken first now, and the brain answers from it in one round.
    ctx = make_test_context()
    order: list[str] = []

    class Box(RobotToolbox):
        async def run(self, name: str, arguments: Any = None) -> str:
            order.append(f"tool:{name}")
            return "ok: the camera saw: a chair; a lamp" if name == "look_through_camera" else \
                await super().run(name, arguments)

    p = ScriptedProvider(["It shows a chair and a lamp."])
    brain = Brain(ctx, provider=p)
    brain.talked_today()  # (no "Good morning." first)
    old = [{"role": "user", "content": "what do you see"},
           {"role": "assistant", "content": "The camera saw a person holding a phone."}]
    res = await brain.respond("Can you tell me what you see?", history=old, toolbox=Box(ctx, mode="chat"),
                              first_words=True, on_sentence=lambda s: order.append(f"say:{s.text}"),
                              on_tool=lambda n: order.append(f"on_tool:{n}"))
    assert order == ["say:Let me look.", "on_tool:look_through_camera", "tool:look_through_camera",
                     "say:It shows a chair and a lamp."]
    question = p.calls[0]["messages"][-1]["content"]
    assert "a new photo was just taken" in question and "the camera saw: a chair, a lamp" in question
    assert 'heard you say "Let me look."' in question
    assert res.tools == ["look_through_camera"] and res.done == [f"look_through_camera: {LOOKED_UP}"]
    order.clear()
    await brain.respond("What's the weather?", history=[], toolbox=Box(ctx, mode="chat"),
                        on_sentence=lambda s: order.append(f"say:{s.text}"))
    assert "tool:look_through_camera" not in order


async def test_after_a_look_that_failed_the_brain_still_calls_its_tools() -> None:
    # The look's note once had the name of the count of sentences said before a tool: the next tool broke on it.
    ctx = make_test_context()

    class Box(RobotToolbox):
        async def run(self, name: str, arguments: Any = None) -> str:
            return "error: privacy mode is on. Don't guess what is there." if name == "look_through_camera" else \
                await super().run(name, arguments)

    p = ScriptedProvider(["I can't look right now.", ("tool", "get_volume", "{}"), " It's at half."])
    brain = Brain(ctx, provider=p)
    brain.talked_today()
    res = await brain.respond("What do you see?", history=[], toolbox=Box(ctx, mode="chat"), first_words=True,
                              on_sentence=lambda s: None)
    assert res.error is None and res.tools == ["look_through_camera", "get_volume"]
    question = p.calls[0]["messages"][-1]["content"]
    assert "couldn't take one" in question and "privacy mode is on" in question
    assert "a new photo was just taken" not in question


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


async def test_what_was_said_before_a_tool_is_not_said_again_after_it() -> None:
    # "Good idea, I'll remember that. Next time I'll pick a fresh joke for you." before the remember tool and the
    # same two sentences after it (the robot, 2026-11-03)
    first = "Good idea, I'll remember that. Next time I'll pick a fresh joke for you."
    res, order, _ = await _voice_turn([first, ("tool", "set_volume", '{"percent": 60}'),
                                       " Good idea, I'll remember that! Next time I'll pick a fresh joke for you.",
                                       " It's at 60 now."], first_words=True)
    assert [o for o in order if o.startswith("say:")] == [
        "say:Good idea, I'll remember that.", "say:Next time I'll pick a fresh joke for you.", "say:It's at 60 now."]
    assert res.text == f"{first} It's at 60 now."
    _, order, _ = await _voice_turn(["Hip hip hooray! Hip hip hooray!"])  # with no tool, a repeat is meant
    assert [o for o in order if o.startswith("say:")] == ["say:Hip hip hooray!", "say:Hip hip hooray!"]


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
    for line in ("So I can answer that, I'd need your town.", "Tell me more so I can answer better.",
                 "Keep the lid on so it boils sooner.", "A simple how-to video would help here.",
                 "Short answers are best when you're busy.", "No tools are needed for this, just your hands.",
                 "Press the button and the TV will show the menu on the screen.", "The screen shows the comparison.",
                 "Open the settings and tap the menu on the screen.", "Start with the eggs."):
        _, said, _ = await _turn(ScriptedProvider([line]))  # not its reasoning
        assert len(said) == 1, line
    for line in ("I know this one, so I can answer it directly.", "No web search needed, it's easy.",
                 "Simple how-to; answer directly with key points, no tool needed.", "Safety note: keep it brief.",
                 "Walk through it briefly, with a card.",  # the breaker question, with the shorter answers rule
                 "Lead with the answer briefly, no tools needed.",
                 "Safety: mention checking for water or damage first, and not to touch if smell of burning.",
                 "I'll put the comparison on the screen for you.", "Let me show the working on the screen.",
                 "Start the game on my screen.", "Now, put the recipe on the screen."):
        _, said, _ = await _turn(ScriptedProvider([line]))
        assert said == [], line


async def test_its_guess_of_their_words_is_never_said() -> None:
    # 'Probably "So then what did they use it for?"' (the robot, 2026-11-02)
    p = ScriptedProvider(['Probably "So then what did they use it for?" ',
                          "Romans also used urine in tanning leather."])
    _, said, _ = await _turn(p, "So then what did they")
    assert [s.text for s in said] == ["Romans also used urine in tanning leather."]
    assert "Never say your guess of their words aloud" in p.calls[0]["system"]
    for line in ("I think he means the Roman roads.", "You probably meant Kublai Khan.",
                 "Sounds like you're asking about the Yuan dynasty.", "Maybe “what time is it”?"):
        _, said, _ = await _turn(ScriptedProvider([line]))
        assert said == [], line
    for line in ("Probably about fifty thousand miles.", "I think you'll like this one.",
                 "They probably used it for laundry.", "Maybe next time."):
        _, said, _ = await _turn(ScriptedProvider([line]))  # answers, not guesses of their words
        assert len(said) == 1, line


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


async def test_a_chat_starting_after_a_while_caches_the_prompt_first() -> None:
    """The first question of a chat waited 1.3 s for its first word, the next ones 0.6 s: its prompt wasn't cached
    any more (the robot, 2026-11-03). A session's warm-up caches the same tools and system prompt a turn sends."""
    now = [1000.0]

    class Priming(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__(["Hi."])
            self.primed: list[tuple[str, list[str]]] = []
            self.warmed = 0

        async def prime(self, *, system: str, tools: Any = None) -> bool:
            self.primed.append((system, [t.get("name") for t in tools or []]))
            return True

        async def warm(self, timeout: float = 5.0) -> bool:
            self.warmed += 1
            return True

    ctx = make_test_context()
    p = Priming()
    brain = Brain(ctx, provider=p, now=lambda: now[0])
    await brain.warm(toolbox=RobotToolbox(ctx, mode="chat"))
    await brain.respond("hi", history=[], toolbox=RobotToolbox(ctx, mode="chat"), on_sentence=lambda s: None)
    assert p.primed == [(p.calls[0]["system"], p.calls[0]["tools"])]  # what the turn sent
    now[0] += 60
    await brain.warm(toolbox=RobotToolbox(ctx, mode="chat"))  # still cached: only the connection
    assert len(p.primed) == 1 and p.warmed == 1
    now[0] += 300
    await brain.warm(toolbox=RobotToolbox(ctx, mode="chat"))
    assert len(p.primed) == 2
    await brain.warm()  # no toolbox (a typed chat): never primed
    assert len(p.primed) == 2 and p.warmed == 2


async def test_claude_primes_with_one_token_and_the_turns_prompt() -> None:
    from littlebot.brain.providers.claude import ClaudeProvider

    sent: list[dict[str, Any]] = []

    class Messages:
        async def create(self, **kwargs: Any) -> Any:
            sent.append(kwargs)

            class Usage:
                input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens = 5, 19000, 0, 1

            class Msg:
                usage = Usage()
                content: list[Any] = []

            return Msg()

    class Client:
        messages = Messages()

    p = ClaudeProvider(api_key="test-key", model="claude-haiku-5-5", client=Client())
    tools = [{"type": "function", "name": "get_volume", "description": "the volume",
              "parameters": {"type": "object", "properties": {}}}]
    assert await p.prime(system="You are LittleBot.", tools=tools)
    turn = p.build_request([{"role": "user", "content": "hi"}], system="You are LittleBot.", tools=tools,
                           effort="minimal")
    assert sent[0]["max_tokens"] == 1
    assert sent[0]["system"] == turn["system"] and sent[0]["tools"] == turn["tools"]


async def test_the_robots_brain_primes_through_its_fallback_wrapper() -> None:
    """On the robot the brain is a FallbackProvider (Claude, then backups): it had no ``prime``, so a chat's first
    question still found its prompt cold (2:26 pm, the robot, 2026-11-03)."""
    from littlebot.brain.providers.claude import ClaudeProvider
    from littlebot.brain.text_model import FallbackProvider

    sent: list[dict[str, Any]] = []

    class Messages:
        async def create(self, **kwargs: Any) -> Any:
            sent.append(kwargs)

            class Msg:
                usage = None
                content: list[Any] = []

            return Msg()

    class Client:
        messages = Messages()

    backup = ScriptedProvider(["Hi."])
    ctx = make_test_context()
    claude = ClaudeProvider(api_key="test-key", model="claude-haiku-5-5", client=Client())
    brain = Brain(ctx, provider=FallbackProvider(claude, backup), now=lambda: 1000.0)
    await brain.warm(toolbox=RobotToolbox(ctx, mode="chat"))
    assert len(sent) == 1 and sent[0]["max_tokens"] == 1 and sent[0]["tools"]
    no_key = FallbackProvider(ClaudeProvider(api_key="", model="claude-haiku-5-5", client=Client()), backup)
    assert await no_key.prime(system="You are LittleBot.") is False and len(sent) == 1


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
