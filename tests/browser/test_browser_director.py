"""The screen director's browser rules (littlebot/screens/director.py ``browser_rule`` and ``_browser_turn``):
explicit "google it" requests open the browser view and never a card; while it shows, steering words go to it."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from littlebot.brain.providers.base import ProviderReply
from littlebot.browser.service import CLOSED_NOTE, OPEN_NOTE
from littlebot.screens.director import (
    BROWSER_BLOCKED_NOTE,
    ScreenDirector,
    browser_rule,
)


class FakeBrowser:
    """Stands in for BrowserView (ctx.browser)."""

    def __init__(self) -> None:
        self.active = False
        self.on = True
        self.opened: list[tuple[str | None, str]] = []
        self.controls: list[tuple[str, dict[str, Any], str]] = []
        self.closed: list[str] = []
        self.reads = 0
        self.page_text = "Giraffes are the tallest land animals."
        self.open_result: dict[str, Any] | None = None
        self.moved = 0  # moved_on() calls: turns about something else while it shows

    def offered(self) -> bool:
        return self.on

    async def open(self, query: str | None = None, url: str | None = None, *, source: str = "voice") -> dict:
        self.opened.append((query, source))
        if self.open_result is not None:
            return self.open_result
        self.active = True
        return {"ok": True, "showing": f"Google results for “{query}”" if query else "Google", "already": False}

    async def control(self, action: str, args: dict[str, Any] | None = None, *, source: str = "voice") -> dict:
        self.controls.append((action, dict(args or {}), source))
        if action == "open_result" and (args or {}).get("n") == 9:
            return {"ok": False, "error": "no_result"}
        return {"ok": True, "showing": "Google", "opened": "Giraffe - Wikipedia", "moved": True}

    async def read(self) -> dict:
        self.reads += 1
        return {"ok": True, "title": "Giraffe", "host": "en.wikipedia.org", "text": self.page_text}

    def moved_on(self) -> None:
        self.moved += 1

    def close(self, reason: str) -> bool:
        self.closed.append(reason)
        was, self.active = self.active, False
        return was


class FakeScreens:
    def __init__(self) -> None:
        self.shown: list[dict[str, Any]] = []
        self.active = False
        self.current = None

    def describe(self) -> None:
        return None

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        self.shown.append(spec)
        return {"ok": True, "showing": "a card"}

    def command(self, action: str, args: dict[str, Any]) -> bool:
        return False

    def close(self, reason: str) -> bool:
        return False


class JsonProvider:
    name = "chatgpt"
    model = "fake"

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers) or [{"do": "none"}]
        self.calls: list[dict[str, Any]] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    effort: str | None = None) -> ProviderReply:
        self.calls.append({"messages": messages, "system": system})
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return ProviderReply(text=answer if isinstance(answer, str) else json.dumps(answer), model="fake")


class Setup:
    def __init__(self, ctx: Any, provider: JsonProvider) -> None:
        self.ctx, self.provider = ctx, provider
        self.screens = ctx.screens = FakeScreens()
        self.browser = ctx.browser = FakeBrowser()
        self.notes: list[str] = []
        self.director = ScreenDirector(ctx, lambda: self.provider, note=self.notes.append)

    async def heard(self, text: str) -> None:
        self.director.heard(text)
        while self.director._tasks:
            await asyncio.gather(*list(self.director._tasks), return_exceptions=True)


@pytest.fixture
def make(ctx: Any) -> Any:
    ctx.privacy.set(cloud_enabled=True)

    def factory(*answers: Any) -> Setup:
        return Setup(ctx, JsonProvider(*answers))

    return factory


# ---------------------------------------------------------------- the rule
@pytest.mark.parametrize(("text", "query"), [
    ("Google it", None),
    ("Can you google that for me, please?", None),
    ("google how tall giraffes are", "how tall giraffes are"),
    ("LittleBot, google the zoo opening hours", "the zoo opening hours"),
    ("Look it up online", None),
    ("look up the zoo opening hours online", "the zoo opening hours"),
    ("Search that in the browser", None),
    ("search for dinosaur facts on the internet", "dinosaur facts"),
    ("check the web for the weather in Paris", "the weather in paris"),
    ("open the browser", None),
    ("Open the browser and search for red pandas", "red pandas"),
    ("show me pictures of red pandas on google", "pictures of red pandas"),
    ("do a google search for volcanoes", "volcanoes"),
    ("can you check in the browser", None),
])
def test_explicit_browser_requests(text: str, query: str | None) -> None:
    rule = browser_rule(text)
    assert rule is not None and (rule.do, rule.action) == ("browse", "open")
    assert rule.spec == {"query": query}


@pytest.mark.parametrize("text", [
    "what is google", "how tall are giraffes", "what's the weather tomorrow", "show me a recipe for pancakes",
    "play a video", "I like the internet", "who made google", "close it", "scroll down", "go back",
])
def test_ordinary_turns_are_not_for_the_browser(text: str) -> None:
    assert browser_rule(text) is None


@pytest.mark.parametrize(("text", "action", "args"), [
    ("scroll down", "scroll", {"direction": "down", "amount": "page"}),
    ("scroll up a little bit", "scroll", {"direction": "up", "amount": "little"}),
    ("go to the top", "scroll", {"direction": "top"}),
    ("keep scrolling", "scroll", {"direction": "down", "amount": "page"}),
    ("open the first one", "open_result", {"n": 1}),
    ("click on the third result", "open_result", {"n": 3}),
    ("result number two", "open_result", {"n": 2}),
    ("open the last one", "open_result", {"n": "last"}),
    ("go back", "back", {}),
    ("back to the results", "back", {}),
    ("go forward", "forward", {}),
    ("refresh the page", "reload", {}),
    ("make it bigger", "zoom", {"level": "in"}),
    ("zoom out", "zoom", {"level": "out"}),
    ("what does it say?", "read", {}),
    ("read me the page", "read", {}),
    ("close it", "close", {}),
    ("close the browser", "close", {}),
    ("we're done with google", "close", {}),
    ("show me your face", "close", {}),
    ("Let me see your face.", "close", {}),
    ("can I see your face again please", "close", {}),
])
def test_steering_while_it_shows(text: str, action: str, args: dict[str, Any]) -> None:
    rule = browser_rule(text, up=True)
    assert rule is not None and (rule.do, rule.action) == ("browse", action)
    assert dict(rule.args) == args


# ---------------------------------------------------------------- the director
async def test_google_it_opens_the_browser_and_never_a_card(make: Any) -> None:
    s = make({"do": "none", "query": "giraffe height"})
    await s.heard("How tall are giraffes?")
    s.provider.calls.clear()
    await s.heard("Can you google it?")
    assert s.browser.opened == [("giraffe height", "voice")]
    assert s.screens.shown == []  # no card
    assert len(s.provider.calls) == 1 and "google" in s.provider.calls[0]["system"].lower()
    assert "How tall are giraffes?" in s.provider.calls[0]["messages"][0]["content"]
    assert s.notes[-1] == OPEN_NOTE.format(what="Google results for “giraffe height”")


async def test_their_own_words_need_no_model(make: Any) -> None:
    s = make()
    await s.heard("google how tall giraffes are")
    assert s.browser.opened == [("how tall giraffes are", "voice")]
    assert s.provider.calls == [] and s.screens.shown == []


async def test_no_model_answer_uses_their_last_question(make: Any) -> None:
    s = make("not json at all")
    await s.heard("how deep is the ocean")
    await s.heard("look it up online")
    assert s.browser.opened[-1] == ("how deep is the ocean", "voice")


async def test_nothing_to_search_opens_google(make: Any) -> None:
    s = make({"query": ""})
    await s.heard("open the browser")
    assert s.browser.opened == [(None, "voice")]
    assert s.notes[-1] == OPEN_NOTE.format(what="Google")


async def test_steering_the_browser(make: Any) -> None:
    s = make()
    await s.heard("google red pandas")
    await s.heard("scroll down")
    await s.heard("open the first one")
    await s.heard("go back")
    assert [(a, args) for a, args, _src in s.browser.controls] == [
        ("scroll", {"direction": "down", "amount": "page"}), ("open_result", {"n": 1}), ("back", {})]
    assert "Giraffe - Wikipedia" in s.notes[-2]
    assert s.screens.shown == []


async def test_what_does_it_say_reads_the_page_into_a_note(make: Any) -> None:
    s = make()
    await s.heard("google giraffes")
    await s.heard("What does it say?")
    assert s.browser.reads == 1
    assert "Giraffes are the tallest land animals." in s.notes[-1] and "don't read it out" in s.notes[-1]


async def test_close_it_closes_the_browser(make: Any) -> None:
    s = make()
    await s.heard("google giraffes")
    await s.heard("close it")
    assert s.browser.closed == ["asked in the voice chat"] and s.notes[-1] == CLOSED_NOTE


async def test_talking_about_something_else_tells_the_browser(make: Any) -> None:
    s = make({"do": "none"})
    await s.heard("what's the weather like?")
    assert s.browser.moved == 0  # nothing shows
    await s.heard("google giraffes")
    await s.heard("scroll down")
    assert s.browser.moved == 0  # about the browser
    await s.heard("what's the weather like?")
    assert s.browser.moved == 1 and s.browser.closed == []  # it closes itself a little later (browser.moved_on_seconds)


async def test_no_such_result(make: Any) -> None:
    s = make()
    await s.heard("google giraffes")
    await s.heard("open result number nine")
    assert "no such result" in s.notes[-1]


async def test_the_child_asking_gets_neither_browser_nor_card(make: Any, ctx: Any) -> None:
    s = make({"do": "show", "card": "facts"})
    ctx.bus.publish("voice.speaker", {"is_child": True, "name": "Amara"})
    await s.heard("google dinosaurs")
    assert s.browser.opened == [] and s.screens.shown == [] and s.provider.calls == []


async def test_a_refused_search(make: Any) -> None:
    s = make()
    s.browser.open_result = {"ok": False, "error": "blocked", "detail": "LittleBot won't search for that."}
    await s.heard("google something rude")
    assert s.notes[-1] == BROWSER_BLOCKED_NOTE
    s.browser.open_result = {"ok": False, "error": "cloud_off", "detail": "Cloud communication is off."}
    await s.heard("google giraffes")
    assert "Cloud communication is off." in s.notes[-1]


async def test_switched_off_falls_through_to_the_cards(make: Any) -> None:
    s = make({"do": "none"})
    s.browser.on = False
    await s.heard("google how tall giraffes are")
    assert s.browser.opened == []


async def test_steering_words_without_the_browser_are_ordinary(make: Any) -> None:
    s = make({"do": "none"})
    await s.heard("scroll down")
    assert s.browser.controls == []


async def test_while_the_browser_shows_no_cards_are_decided(make: Any) -> None:
    s = make({"do": "show", "card": "facts", "spec": {"title": "Giraffes"}})
    await s.heard("google giraffes")
    s.provider.calls.clear()
    await s.heard("what do giraffes eat?")
    assert s.screens.shown == [] and s.provider.calls == []
