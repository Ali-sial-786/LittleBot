"""The screen director: during a voice chat, decides what LittleBot's screen shows for what was just asked.

One ``ScreenDirector`` per live call (littlebot/chatgpt/live.py). After each final thing the person says
(``heard``), and knowing the voice's last captions (``said``), it picks one of: show a card, steer the card
that is up, close it, or nothing (most turns: chit-chat, songs, talking to the baby). In order:

0. **Only for a grown-up.** The baby mostly sees the face: a turn voice recognition (``ctx.voices``, bus topic
   ``VOICE_SPEAKER``) says was the child's gets no card; one it says was a grown-up's does, even when short
   ("tomorrow?"). Without it, a turn needs ``GROWNUP_MIN_WORDS`` real (not babbled) words, or one of the local
   rules' clear phrases.
1. **Local rules** (instant, no request): "show me the games" / "what games do you have?" show the games menu
   (a "games" card: the voice is then told to say the games' names); with a card up, "next step" / "go back" /
   "stop the timer" become that card's command, "close it" / "hide that" / "show me your face" close it
   (``local_rule``).
   Explicit browser requests come first and never get a card (``browser_rule``, with ``ctx.browser``,
   littlebot/browser): "google it", "google <x>", "search that in the browser", "look it up online", "open the
   browser", "check the web" open Google (SafeSearch on) for their words after the trigger, else for the subject
   of the conversation (one small model request, ``QUERY_PROMPT``; the last thing they said if that fails). While
   the browser shows, "scroll down", "go back", "open the first one", "zoom in", "close it" steer it and "what does
   it say?" reads the page into a note for the voice to sum up.
2. **The pre-filter** (instant): turns plainly not worth a card cost nothing (``prefilter``): a stop word,
   a look or camera question (live.py's own path, ``asks_to_see``), the robot's own controls (videos,
   volume, games, faces), songs, chit-chat and babble, and, with no card up, anything without a question
   or a request for information.
3. **One small text-model request** (the camera looks' fast model and reasoning effort): the last few
   lines of the conversation, the card on screen (``describe()`` and kind) and the catalogue of card types
   (``base.card_types()``: kind, description, fields, required, actions), answered with strict JSON
   (``PROMPT``). The answer is validated hard (``clean_spec`` / ``clean_args``: unknown kinds, unknown
   fields, wrong types, oversized text and lists are dropped or trimmed) and every word of it goes through
   LittleBot's safety rules (``validate_reply``) before anything reaches the screen.

Latency: the decision runs in the call's own tasks and never blocks it; it gets ``DECIDE_TIMEOUT_S`` and
the card ``SHOW_TIMEOUT_S``. The target is a card within ~2 s of the question, while the voice is still
talking. A decision for a turn that is no longer the newest (they said something else meanwhile) is dropped.

The voice is told once per call what a card is ("On your screen now: ... don't read it out"), then briefly
on each new card or change. It must never read the card out word for word.

Respected: ``screens.enabled`` off, no ``ctx.screens``, a game owning the screen (``show`` answers "busy":
nothing is said), the cloud switch (no request with cloud off or offline; local rules still work).

Logs: one INFO line per decision (what, milliseconds, which path), never the conversation's words.
"""

from __future__ import annotations

import asyncio
import collections
import inspect
import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("littlebot.screens.director")

SOURCE = "voice"  # ScreenService.show(source=...) for the director's own cards; the tools use "tool"
DECIDE_TIMEOUT_S = 4.0  # the model's answer; later than this the voice has said it all anyway
SHOW_TIMEOUT_S = 6.0  # building and showing the card (a forecast is usually cached by then)
FAST_EFFORT = "none"  # reasoning effort for the decision, as for camera looks (look.LOOK_EFFORT)
LINES = 6  # conversation lines the model sees
LINE_CHARS = 300
MAX_TEXT = 200  # one text field on a card
MAX_ITEMS = 12  # one list on a card
MAX_KEYS = 12  # one object on a card
MAX_ARGS = 4  # a command's arguments
MAX_SPEC_CHARS = 3000
CATALOGUE_LINE_CHARS = 1200
NOTE_CHARS = 300
RECENT_S = 10.0  # the same card shown this recently (by the director or a tool) isn't shown again
MAX_HISTORY = 20

FIRST_NOTE = ("On your screen now: {what}. LittleBot puts cards like this on its screen by itself while you talk. "
              "Don't read the screen out or describe it; you may point to it (\"look!\"). The grown-up can say {hint}.")
SHOWN_NOTE = "On your screen now: {what}."
CHANGED_NOTE = "The screen now shows: {what}."
CLOSED_NOTE = "The card is off the screen now; your face is back."

PROMPT = """\
You are the screen director of {robot}, a small home robot with a screen, talking with a family (a grown-up and \
a very young child). While the robot's voice answers out loud, its screen may show an info card that adds to what \
is said: the gist, short and big, never the whole answer. After each thing someone says, answer with exactly one \
JSON object and nothing else:
{{"do": "show", "spec": {{"card": "<kind>", <that card's fields>}}}} when what was just asked has something worth \
seeing: a forecast, a word's meaning, the key points of an explanation, a recipe, a timer, the time somewhere, a sum \
or conversion, a quick fact, a list or a comparison.
{{"do": "command", "action": "<one of the card's actions>", "args": {{...}}}} to steer the card on screen when \
they follow up on it ("and tomorrow?", "next step"). Prefer this to a new card of the same kind.
{{"do": "close"}} when they want the card gone, or the talk has moved on to something it doesn't fit.
{{"do": "none"}} for everything else: chit-chat, songs, games, talking to the baby, or nothing worth showing. \
That is most turns.
Use only the fields listed for the card, with short text (a few words per line, numbers as digits). Never put \
personal details on the screen.

The cards:
{catalogue}"""


@dataclass
class Decision:
    """What to do with the screen for one turn."""

    do: str  # show | command | close | none
    spec: dict[str, Any] | None = None
    action: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""  # for the log: why "none" (never the conversation's words)


# ---------------------------------------------------------------- words
_LEAD = (r"(?:(?:ok|okay|right|alright|all right|and|so|then|now|great|good|done|thanks|thank you|cool|yes|yeah|"
         r"please|wait|sorry|oops|no|hey|littlebot|little bot|robot|can you|could you|you can) )*")
_TAIL = r"(?: (?:please|now|thanks|thank you|littlebot|little bot|robot))*"
_CARD_NOUN = r"(?:card|screen|picture|page|recipe|timer|weather|forecast|clock|list|sum|word|fact|facts)"
CLOSE_WORDS = re.compile(
    rf"^{_LEAD}(?:"
    rf"(?:close|hide|clear|remove|dismiss)(?: (?:it|that|this|the {_CARD_NOUN}))?(?: (?:off|away|down))?|"
    r"(?:take|put|get) (?:it|that|this) (?:away|off(?: the screen)?|down)|get rid of (?:it|that|this)|"
    r"(?:show me |show |bring back |back to |go back to )your face(?: again| back)?|"
    rf"(?:i'?m|we'?re) (?:done|finished) with (?:it|that|this|the {_CARD_NOUN})"
    rf"){_TAIL}$")
NEXT_WORDS = re.compile(
    rf"^{_LEAD}(?:next(?: (?:step|one|page|bit|card|fact))?|what'?s next|what is next|(?:and )?then what|what now|"
    r"show (?:me )?the next (?:step|one|page|fact)|(?:go )?(?:to|on to) the next (?:step|one|page|fact))"
    rf"{_TAIL}$")
BACK_WORDS = re.compile(
    rf"^{_LEAD}(?:(?:go )?back(?: (?:one|a step|one step))?|previous(?: (?:step|one|page))?|"
    r"the (?:previous|last) (?:step|one|page)|(?:one )?step back|what was the (?:last|previous) step|"
    r"(?:say|show) (?:me )?the (?:last|previous) step(?: again)?)"
    rf"{_TAIL}$")
TIMER_STOP_WORDS = re.compile(
    rf"^{_LEAD}(?:stop|cancel|end|turn off|switch off|silence)(?: the| that| my)? (?:timer|alarm|countdown)(?: off)?"
    rf"{_TAIL}$")

# Things with their own path in the call (live.py): the robot's controls, videos, games, faces, notes.
OWN_PATHS = re.compile(
    r"\b(?:videos?|youtube|cartoons?|volume|louder|quieter|go(?:ing)? to (?:sleep|bed)|wake up|activit(?:y|ies)|"
    r"play(?:ing)? (?:a |the |some |another )?games?|let'?s play|start (?:a |the )?game|"
    r"stop (?:the )?(?:game|video|activity)|make (?:a |your )?(?:\w+ )?face|your face|wink|peek ?a ?boo|"
    r"spin your eyes|remember|forget)\b")
WHO_ASKS = re.compile(r"\b(?:who am i|do you know (?:who i am|me)|who is (?:this|that|here)|who'?s (?:this|that|here))\b")
SONGS = re.compile(r"^(?:(?:can|could|will|would) you |let'?s |please |now )*(?:sing|dance|clap|count to)\b")
CHAT_QUESTIONS = re.compile(
    r"^(?:(?:and|so|hey|hi|oh|well) )*(?:how are you|how'?s it going|how do you do|what'?s up|what is up|who are you|"
    r"what are you|what'?s your name|what is your name|how old are you|where are you|are you there|can you hear me|"
    r"do you hear me|what are you doing|are you (?:ok|okay|happy|sad|tired|sleepy|awake|real|a robot|listening)|"
    r"do you (?:like|love|want)|what do you (?:like|want|think of me)|what'?s your favou?rite|"
    r"what is your favou?rite)\b")
TO_THE_BABY = re.compile(r"^(?:(?:can|could|will) you )?(?:say (?:hi|hello|hey|bye|goodbye|good night|night night)\b|"
                         r"say \w+ to\b|say [a-z']+$|tell (?:her|him|the baby) )")
# With no card up, only a question or a request for information is worth asking the model about.
INFO_CUE = re.compile(
    r"\b(?:what|how|when|where|which|who|why|whose|tell me|show me|explain|define|definition|meaning|mean|means|"
    r"spell|recipe|recipes|cook|cooking|bake|baking|ingredients|timer|remind me|count ?down|minutes?|hours?|seconds?|"
    r"convert|plus|minus|times|divided|percent|equals|calculate|weather|forecast|temperature|rain|raining|snow|"
    r"sunny|umbrella|time|date|today|tomorrow|weekend|compare|difference|versus|vs|list|steps|fact|facts|history|"
    r"capital|population|translate|synonym|news|is it|are there|does|did|can i|should i|could i|do i|"
    r"is there|i need|i want to know|look up|find out)\b")
CHAT_WORDS = frozenset((
    "hi", "hello", "hey", "hiya", "yes", "yeah", "yep", "yup", "no", "nope", "nah", "ok", "okay", "alright", "right",
    "sure", "thanks", "thank", "you", "cool", "nice", "great", "wow", "oh", "ooh", "ah", "aah", "aha", "haha", "hehe",
    "lol", "um", "umm", "uh", "hmm", "mm", "mhm", "bye", "goodbye", "night", "morning", "good", "please", "sorry",
    "awesome", "amazing", "yay", "littlebot", "little", "bot", "robot", "love", "it", "that", "is", "so", "me", "too",
    "very", "much", "really", "oops", "well", "fine", "lovely"))
BABBLE = re.compile(r"((?:[bdgmnptwl]?[aeiou]{1,2}h?))\1{0,3}")
# "Show me the games", "what games do you have?": the menu of on-screen games (a "games" card), at once.
GAMES_MENU_WORDS = re.compile(
    rf"^{_LEAD}(?:(?:show|tell|give) (?:me |us )?(?:the |your |all (?:the |your )?)?games(?: (?:list|menu))?|"
    r"(?:what|which) games (?:do you have|have you got|can (?:we|i|you) play|are there|is there|do you know)|"
    r"(?:what|which) games|(?:list|show) (?:all )?(?:of )?your games|what can we play)"
    rf"{_TAIL}$")
# Cards are for the grown-ups: the baby mostly sees the face. A turn counts as a grown-up's when voice recognition
# says so (``VOICE_SPEAKER``), when it is one of the local rules' clear phrases, or when it has at least this many
# real (not babbled) words (as live.py's NAMING_MIN_WORDS, when it has one). Recognised as the child: no card.
GROWNUP_MIN_WORDS = 3
VOICE_SPEAKER = "voice.speaker"  # {person_id, name, role, is_child, score}: who spoke this turn (ctx.voices)
SPEAKER_BEFORE_S = 3.0  # an identification this long before their final words still belongs to this turn
GAMES_NOTE = ("On your screen now: the menu of your games: {what}. Say the games' names out loud now, briefly, and ask "
              "which one they'd like to play.")

# ---------------------------------------------------------------- the browser view (littlebot/browser)
# Only explicit requests: the words "google", "online", "the browser", "the web", "the internet". Matched on _plain.
_BLEAD = rf"{_LEAD}(?:let'?s |lets |i want (?:you )?to |i'?d like (?:you )?to |just |quickly |go |go and )?"
_WEB = r"(?:online|on the internet|on the web|on google|in (?:the |your |a )?(?:web )?browser|on (?:the |your )?browser)"
_THAT = r"(?:it|that|this|them|those|that one|this one)"
BROWSER_SEARCH = re.compile(rf"^{_BLEAD}(?:"
                            r"google(?: search)?(?: for)? (?P<q1>.+)|"
                            r"(?:do|run) a google search(?: for (?P<q2>.+))?|"
                            rf"(?:look|search|check|find) {_THAT} (?:up )?{_WEB}|"
                            rf"(?:look up|look for|search for|search|check|find|look) (?P<q3>.+?) (?:up )?{_WEB}|"
                            rf"(?:search|look|check|have a look) {_WEB}(?: for (?P<q4>.+))?|"
                            r"(?:search|check|look on|look at|try|use) (?:the )?(?:web|internet|google)(?: for (?P<q5>.+))?|"
                            r"(?:open|show|start|bring up|pull up|get|use|launch) (?:me |us )?(?:the |your |a )?"
                            r"(?:web )?(?:browser|google)(?: (?:and |to )?(?:search|look up|find|google|look)(?: for)?"
                            r" (?P<q6>.+))?|"
                            r"(?:show|find|get) (?:me |us )?(?P<q7>.+?) on google"
                            rf"){_TAIL}$")
_PAGE = r"(?:it|that|this|the page|the web ?page|the website|the site|the article|that page|this page|the screen)"
BROWSER_READ = re.compile(rf"^{_BLEAD}(?:what (?:does|did|do) {_PAGE} say|what does it say (?:on|in) (?:the )?(?:page|screen)|"
                          rf"what'?s on {_PAGE}|what is on {_PAGE}|what'?s it say|"
                          rf"read (?:me )?{_PAGE}(?: to me| to us)?(?: out(?: loud)?)?|"
                          rf"(?:summari[sz]e|sum up) {_PAGE}|tell me what {_PAGE} says|what does the (?:page|article) say)"
                          rf"{_TAIL}$")
BROWSER_SCROLL = re.compile(rf"^{_BLEAD}(?:"
                            r"(?:scroll|go|move|page|swipe)(?: it| the page)? (?P<dir>down|up)"
                            r"(?: (?P<amt>a little bit|a little|a bit|a lot|more|some|a page|one page|half a page|again))?|"
                            r"keep scrolling|scroll(?: more| again| on)?|show me more|more please|next page|"
                            r"(?:scroll|go|jump|take me)(?: back)? (?:up |down )?to the (?P<edge>top|bottom|end)(?: of the page)?"
                            rf"){_TAIL}$")
BROWSER_BACK = re.compile(rf"^{_BLEAD}(?:go back(?: a page| one page)?|back|previous page|the previous page|"
                          r"go to the previous page|(?:go )?back to (?:the )?(?:results|google|search(?: results)?|"
                          rf"last page|list)){_TAIL}$")
BROWSER_FORWARD = re.compile(rf"^{_BLEAD}(?:go forward(?: a page| one page)?|forward){_TAIL}$")
BROWSER_RELOAD = re.compile(rf"^{_BLEAD}(?:reload|refresh)(?: (?:it|that|this|the page))?{_TAIL}$")
BROWSER_ZOOM = re.compile(rf"^{_BLEAD}(?:zoom (?P<z1>in|out)|make (?:it|that|the (?:text|writing|words|page)) "
                          r"(?P<z2>bigger|larger|smaller)|(?P<z3>bigger|larger|smaller)(?: (?:text|writing|words))?|"
                          rf"(?P<z4>reset|normal) (?:the )?(?:zoom|size)){_TAIL}$")
_ORD = r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|1st|2nd|3rd|4th|5th|top|last"
BROWSER_RESULT = re.compile(rf"^{_BLEAD}(?:"
                            r"(?:open|click|tap|press|choose|pick|select|go to|show me|try|look at|go into|go on)"
                            rf"(?: on| into)? (?:the )?(?P<ord>{_ORD})(?: (?:one|result|link|site|website|page|article|"
                            r"answer|hit|search result))?|"
                            rf"(?:the )?(?P<ord2>{_ORD}) (?:one|result|link)|"
                            r"(?:open |click |go to |show me )?(?:(?:the )?(?:result|link)(?: number)?|number) "
                            r"(?P<num>one|two|three|four|five|"
                            r"six|seven|eight|nine|ten|\d{1,2})"
                            rf"){_TAIL}$")
BROWSER_CLOSE = re.compile(rf"^{_BLEAD}(?:close|hide|exit|quit|shut|leave|get out of|turn off|switch off|get rid of|"
                           r"stop|put away)(?: the| that| this| your)? (?:web )?(?:browser|google|web ?page|website|"
                           r"site|page|search|internet|results)(?: (?:off|away|down|now))?|"
                           rf"(?:i'?m|we'?re) (?:done|finished) with (?:the |that |this )?(?:browser|google|page|"
                           rf"website|search|internet){_TAIL}$")
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "first": 1, "1st": 1, "top": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4,
            "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_TRIM_QUERY = re.compile(r"(?: (?:for|to) (?:me|us)| please| now| littlebot| little bot| robot| for a second)+$")
QUERY_PROMPT = """You help {robot}, a small home robot with a screen, search Google for a grown-up who just asked it to google something or look it up online without saying what. From the conversation, work out what they want looked up and answer with exactly one JSON object and nothing else: {{"query": "<a short Google search, a few words>"}}, or {{"query": ""}} when nothing in the conversation is worth searching for. Never put personal details in the query."""
QUERY_CHARS = 120
BROWSER_DONE_NOTE = "Done: {what}. Say okay in a few words, or nothing."
BROWSER_BLOCKED_NOTE = "LittleBot won't search for that. Say kindly, in a few words, that you can't look that up."


def _plain(text: str) -> str:
    """Lower case, letters, digits and apostrophes, single spaces: "Close it, please!" -> "close it please"."""
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", str(text or "").lower().replace("’", "'")).split())


def prefilter(text: str, *, card_up: bool = False) -> str | None:
    """Why this turn needs no screen decision (no request is made), or None when the model should decide."""
    plain = _plain(text)
    words = plain.split()
    if not words:
        return "empty"
    from littlebot.chatgpt import live  # noqa: PLC0415 - the call's own paths

    if live.STOP_CALL.match(plain):
        return "stop"
    if live.asks_to_see(text) or WHO_ASKS.search(plain):
        return "look"
    if OWN_PATHS.search(plain):
        return "control"
    if SONGS.match(plain):
        return "song"
    if all(w in CHAT_WORDS or BABBLE.fullmatch(w) for w in words) or CHAT_QUESTIONS.match(plain):
        return "chit-chat"
    if TO_THE_BABY.match(plain):
        return "talking to the baby"
    if not card_up and not INFO_CUE.search(plain):
        return "no question"
    return None


def browser_rule(text: str, *, up: bool = False) -> Decision | None:
    """An explicit browser request (``do == "browse"``: ``action`` "open" with ``spec["query"]``: their words, or
    None when they named none), or with the browser ``up`` a request to steer it ("scroll", "open_result",
    "back", "forward", "reload", "zoom", "read", "close"); else None (an ordinary turn)."""
    plain = _plain(text)
    if not plain:
        return None
    m = BROWSER_SEARCH.match(plain)
    if m:
        query = next((g for g in m.groups() if g), None)
        query = _TRIM_QUERY.sub("", query or "").strip()
        if not query or re.fullmatch(_THAT, query):
            query = ""
        return Decision("browse", action="open", spec={"query": query or None}, why="browser words")
    if not up:
        return None
    if CLOSE_WORDS.match(plain) or BROWSER_CLOSE.match(plain):
        return Decision("browse", action="close", why="close words")
    if BROWSER_READ.match(plain):
        return Decision("browse", action="read", why="read words")
    m = BROWSER_SCROLL.match(plain)
    if m:
        edge = m.group("edge")
        if edge:
            return Decision("browse", action="scroll", args={"direction": "bottom" if edge == "end" else edge})
        amt = m.group("amt") or ""
        amount = ("little" if "bit" in amt or "little" in amt else "half" if "half" in amt
                  else "lot" if "lot" in amt else "page")
        return Decision("browse", action="scroll", args={"direction": m.group("dir") or "down", "amount": amount})
    m = BROWSER_RESULT.match(plain)
    if m:
        word = m.group("ord") or m.group("ord2") or m.group("num") or "1"
        n: Any = _NUMBERS.get(word, word)  # "last" stays a word (the browser counts the results)
        return Decision("browse", action="open_result", args={"n": int(n) if str(n).isdigit() else n})
    if BROWSER_BACK.match(plain):
        return Decision("browse", action="back")
    if BROWSER_FORWARD.match(plain):
        return Decision("browse", action="forward")
    if BROWSER_RELOAD.match(plain):
        return Decision("browse", action="reload")
    m = BROWSER_ZOOM.match(plain)
    if m:
        word = next(g for g in m.groups() if g)
        level = "in" if word in ("in", "bigger", "larger") else "out" if word in ("out", "smaller") else "reset"
        return Decision("browse", action="zoom", args={"level": level})
    return None


def _first(actions: dict[str, Any], names: tuple[str, ...]) -> str | None:
    return next((n for n in names if n in actions), None)


def real_words(text: str) -> int:
    """How many words of ``text`` aren't babble ("ba ba ba" has none, "what's the weather" three)."""
    return sum(1 for w in _plain(text).split() if not BABBLE.fullmatch(w))


def local_rule(text: str, kind: str | None, actions: dict[str, Any] | None = None,
               kinds: Any = ()) -> Decision | None:
    """A very common, clearly safe request, decided without the model, else None: the games menu (when there is
    a "games" card in ``kinds``), or one about the card on screen (``kind``, with its ``actions``)."""
    plain = _plain(text)
    if "games" in kinds and GAMES_MENU_WORDS.match(plain):
        if kind == "games":
            return Decision("none", why="the games menu is up already")
        return Decision("show", spec={"card": "games"}, why="games words")
    if not kind:
        return None
    actions = actions or {}
    if CLOSE_WORDS.match(plain):
        return Decision("close", why="close words")
    if NEXT_WORDS.match(plain):
        act = _first(actions, ("next",))
        return Decision("command", action=act) if act else None
    if BACK_WORDS.match(plain):
        act = _first(actions, ("back", "previous", "prev"))
        return Decision("command", action=act) if act else None
    if TIMER_STOP_WORDS.match(plain):
        act = _first(actions, ("stop", "cancel"))
        if act:
            return Decision("command", action=act)
        return Decision("close", why="timer stop words") if kind == "timer" else None
    return None


# ---------------------------------------------------------------- the model's answer
_DROP = object()
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def parse_decision(text: str) -> dict[str, Any] | None:
    """The JSON object in the model's answer (code fences and stray words around it are ignored), or None."""
    t = str(text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _text(value: str, limit: int) -> str:
    s = " ".join(_CONTROL_CHARS.sub(" ", value).split())
    if len(s) > limit:
        s = s[:max(1, limit - 1)].rstrip(" ,;:-") + "…"
    return s


def _clean(value: Any, schema: dict[str, Any], depth: int = 0) -> Any:
    """``value`` made to fit its JSON-schema ``schema`` (type, enum, bounds, sizes), or ``_DROP``."""
    kind = schema.get("type")
    if not kind:  # untyped: a scalar as it is
        kind = "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string"
    kinds = kind if isinstance(kind, list) else [kind]
    for k in kinds:
        out = _as(value, str(k), schema, depth)
        if out is not _DROP:
            return out
    return _DROP


def _as(value: Any, kind: str, schema: dict[str, Any], depth: int) -> Any:  # noqa: PLR0911, PLR0912
    if kind == "string":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return _DROP
        try:
            limit = max(1, min(MAX_TEXT, int(schema.get("maxLength") or MAX_TEXT)))
        except (TypeError, ValueError):
            limit = MAX_TEXT
        s = _text(str(value), limit)
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            match = next((e for e in enum if isinstance(e, str) and e.lower() == s.lower()), None)
            return match if match is not None else _DROP
        return s or _DROP
    if kind in ("integer", "number"):
        if isinstance(value, bool):
            return _DROP
        if isinstance(value, str):
            try:
                value = float(value.strip())
            except ValueError:
                return _DROP
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return _DROP
        if kind == "integer":
            if float(value) != int(value):
                return _DROP
            value = int(value)
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if isinstance(lo, (int, float)) and value < lo:
            value = type(value)(lo)
        if isinstance(hi, (int, float)) and value > hi:
            value = type(value)(hi)
        enum = schema.get("enum")
        return value if not isinstance(enum, list) or value in enum else _DROP
    if kind == "boolean":
        return value if isinstance(value, bool) else _DROP
    if kind == "array":
        if not isinstance(value, (list, tuple)) or depth >= 3:
            return _DROP
        try:
            limit = max(1, min(MAX_ITEMS, int(schema.get("maxItems") or MAX_ITEMS)))
        except (TypeError, ValueError):
            limit = MAX_ITEMS
        items = schema.get("items") if isinstance(schema.get("items"), dict) else {"type": "string"}
        out = [c for c in (_clean(v, items, depth + 1) for v in list(value)[:limit * 2]) if c is not _DROP][:limit]
        return out or _DROP
    if kind == "object":
        if not isinstance(value, dict) or depth >= 3:
            return _DROP
        props = schema.get("properties")
        cleaned: dict[str, Any] = {}
        for key, v in list(value.items())[:MAX_KEYS * 2]:
            name = str(key)[:40]
            if isinstance(props, dict):
                if name not in props:
                    continue
                c = _clean(v, props[name] if isinstance(props[name], dict) else {}, depth + 1)
            else:
                c = _clean(v, {}, depth + 1)
            if c is not _DROP:
                cleaned[name] = c
            if len(cleaned) >= MAX_KEYS:
                break
        return cleaned or _DROP
    return _DROP


def _texts(value: Any) -> list[str]:
    """Every piece of text in a cleaned spec (keys of free-form objects too), for the safety check."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in (str(k), *_texts(v))]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def unsafe(texts: list[str], relaxed: Any = ()) -> list[str]:
    """The safety rules ``texts`` break (the same ``validate_reply`` rules as the voice's words; [] when fine).

    Checked as one text, a line each (each line is a sentence to the rules)."""
    joined = "\n".join(t for t in texts if t.strip())
    if not joined:
        return []
    from littlebot.safety.content import BLOCK_RULES, validate_reply  # noqa: PLC0415

    result = validate_reply(joined, max_words=100_000, allow=relaxed or ())
    if result.ok:
        return []
    return [r for r in result.reasons if r in BLOCK_RULES or r == "grownup_redirect"]


def clean_spec(spec: Any, types: dict[str, Any], relaxed: Any = ()) -> tuple[dict[str, Any] | None, str]:
    """A card spec fit for ``ScreenService.show``: a known kind, only its fields (and a title), each of the
    right type and size, the required ones there, every word within the safety rules. Else (None, why)."""
    if not isinstance(spec, dict):
        return None, "not an object"
    kind = str(spec.get("card") or spec.get("kind") or "").strip().lower()[:40]
    ct = types.get(kind)
    if ct is None:
        return None, "unknown card"
    fields = ct.fields if isinstance(ct.fields, dict) else {}
    allowed = {"title": {"type": "string", "maxLength": 80}, **fields}
    out: dict[str, Any] = {"card": kind}
    for name, schema in allowed.items():
        if name == "card" or name not in spec:
            continue
        value = _clean(spec[name], schema if isinstance(schema, dict) else {})
        if value is not _DROP:
            out[name] = value
    missing = [f for f in ct.required if out.get(f) in (None, "", [], {})]
    if missing:
        return None, f"missing {', '.join(missing)[:80]}"
    if len(json.dumps(out, ensure_ascii=False)) > MAX_SPEC_CHARS:
        return None, "too long"
    broken = unsafe(_texts(out), relaxed)
    if broken:
        return None, f"blocked by the safety rules ({broken[0]})"
    return out, ""


def clean_args(args: Any, relaxed: Any = ()) -> dict[str, Any] | None:
    """A command's arguments: at most MAX_ARGS short scalars, within the safety rules; None when unusable."""
    if args is None:
        return {}
    if not isinstance(args, dict):
        return None
    out: dict[str, Any] = {}
    for key, value in list(args.items())[:MAX_ARGS]:
        v = _clean(value, {"maxLength": 80})
        if v is not _DROP:
            out[str(key)[:30]] = v
    return None if unsafe(_texts(out), relaxed) else out


def _place(spec: dict[str, Any]) -> str:
    for key in ("place", "city", "location", "where", "zone", "timezone"):
        v = spec.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


def _reply_options(reply: Any, **wanted: Any) -> dict[str, Any]:
    """The per-call options this provider's ``reply`` takes (a plain provider gets none of them)."""
    try:
        params = inspect.signature(reply).parameters
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in wanted.items() if v is not None and k in params}


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# ---------------------------------------------------------------- the director
class ScreenDirector:
    """Decides, during one voice chat, what the screen shows for what was just asked (see the module docstring).

    ``provider_factory()``: the text model (a ChatGPTProvider; the camera looks' models first). ``note(text)``:
    a quiet note to the voice. ``spawn(coro)``: runs a task in the call (cancelled when it ends). ``types()``:
    the card catalogue (``base.card_types``). ``effort``: the decision's reasoning effort."""

    def __init__(self, ctx: Any, provider_factory: Callable[[], Any], *, note: Callable[[str], Any],
                 spawn: Callable[[Any], Any] | None = None, types: Callable[[], dict[str, Any]] | None = None,
                 effort: str | None = FAST_EFFORT, timeout: float = DECIDE_TIMEOUT_S,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.ctx = ctx
        self._provider_factory = provider_factory
        self._note = note
        self._spawn = spawn or self._own_spawn
        self._types_fn = types
        self.effort = effort
        self.timeout = timeout
        self._now = now
        self._lines: collections.deque[tuple[str, str]] = collections.deque(maxlen=LINES)
        self._turn = 0  # final things the person said
        self._last_shown: tuple[str, str, float] | None = None  # (kind, place, when) of the latest card shown
        self._told_once = False  # the voice had the full note about cards
        self._closed = False
        self._system: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()  # only when no spawn was given
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=MAX_HISTORY)  # tests, status
        self._spoke: tuple[bool, float] | None = None  # (is_child, when) from voice recognition's latest turn
        try:
            self._sub: Any = ctx.bus.subscribe(VOICE_SPEAKER, self._on_speaker)
        except Exception:  # noqa: BLE001 - no voice recognition: words decide
            self._sub = None

    # ------------------------------------------------------------ the call's side
    def heard(self, text: str, *, handled: bool = False) -> None:
        """A final thing the person said. ``handled``: another path of the call took it (a game, a video, a
        face): it counts as the newest turn but gets no screen decision."""
        text = " ".join(str(text or "").split())
        if not text or self._closed:
            return
        self._turn += 1
        self._lines.append(("heard", text[:LINE_CHARS]))
        if handled or self._browser_turn(text) or not self.enabled():
            return
        t0 = self._now()
        child = self._speaker(t0)
        if child:
            log.debug("screen: nothing to decide (the child spoke)")
            return
        kind = self._current_kind()
        rule = local_rule(text, kind, self._actions(kind), kinds=self._types())  # clear grown-up phrases
        if rule is not None and rule.do == "none":
            log.debug("screen: nothing to do (%s)", rule.why)
            return
        if rule is not None and rule.do == "show" and rule.spec is not None:
            self._spawn(self._apply_show(rule.spec, t0, "rule"))
            return
        if rule is not None:
            self._steer(rule, t0, "rule")
            return
        why = prefilter(text, card_up=kind is not None)
        grownup = child is False or real_words(text) >= self._min_words()
        if why is None and not grownup and getattr(self.ctx, "voices", None) is None:
            why = "too few words for a grown-up"  # (with voice recognition, it may still say who it was)
        if why is None and not self._cloud_ok():
            why = "cloud off"
        if why is None and self._screen_taken():
            why = "a game or the browser is on the screen"
        if why is not None:
            log.debug("screen: nothing to decide (%s)", why)
            return
        self._spawn(self._decide(self._turn, t0, needs_grownup=not grownup))

    async def warm(self) -> None:
        """Open the text model's connection ahead of the first decision (best effort)."""
        try:
            warm = getattr(self._provider_factory(), "warm", None)
            if warm is not None and self._cloud_ok():
                await warm()
        except Exception:  # noqa: BLE001
            log.debug("screen: warming the connection failed", exc_info=True)

    def said(self, text: str) -> None:
        """A final caption of what the voice said."""
        text = " ".join(str(text or "").split())
        if text and not self._closed:
            self._lines.append(("robot", text[:LINE_CHARS]))

    def close(self) -> None:
        """The call ended: forget the conversation; nothing more is decided."""
        self._closed = True
        self._lines.clear()
        if self._sub is not None:
            self._sub.cancel()
            self._sub = None
        for t in list(self._tasks):
            t.cancel()

    def _on_speaker(self, event: Any) -> None:
        """Voice recognition identified who spoke (bus thread: kept tiny)."""
        is_child = event.get("is_child")
        if isinstance(is_child, bool):
            self._spoke = (is_child, self._now())

    def _speaker(self, t0: float) -> bool | None:
        """True: voice recognition says the child spoke this turn; False: a grown-up; None: not known."""
        spoke = self._spoke
        return spoke[0] if spoke is not None and spoke[1] >= t0 - SPEAKER_BEFORE_S else None

    @staticmethod
    def _min_words() -> int:
        from littlebot.chatgpt import live  # noqa: PLC0415

        return int(getattr(live, "NAMING_MIN_WORDS", GROWNUP_MIN_WORDS))

    def enabled(self) -> bool:
        screens = self.screens
        if screens is None or self._closed:
            return False
        try:
            return bool(self.ctx.settings.get("screens.enabled", True))
        except Exception:  # noqa: BLE001
            return False

    @property
    def screens(self) -> Any:
        return getattr(self.ctx, "screens", None)

    def status(self) -> dict[str, Any]:
        return {"turns": self._turn, "decisions": list(self.history)}

    async def show_spec(self, spec: dict[str, Any], *, source: str = "tool") -> dict[str, Any]:
        """Show a card asked for elsewhere in the call (LittleBot's tools, littlebot/chatgpt/tools.py): checked
        like the director's own, not shown again when it is up already, and the voice is told."""
        if not self.enabled():
            return {"ok": False, "error": "disabled"}
        clean, why = clean_spec(spec, self._types(), self._relaxed())
        if clean is None:
            return {"ok": False, "error": "invalid", "detail": why}
        if self._up_already(clean):
            return {"ok": True, "showing": self._describe(), "already": True}
        return await self._show(clean, source)

    # ------------------------------------------------------------ deciding
    async def _decide(self, turn: int, t0: float, *, needs_grownup: bool = False) -> None:
        """``needs_grownup``: a short turn that is a grown-up's only if voice recognition says so by the time the
        answer comes (it may identify them a moment after their words)."""
        try:
            reply = await asyncio.wait_for(self._ask(), self.timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._log("none", t0, "model", "no answer in time")
            return
        except Exception as e:  # noqa: BLE001 - a failed decision is simply no card
            self._log("none", t0, "model", f"the request failed ({getattr(e, 'kind', None) or type(e).__name__})")
            return
        if turn != self._turn or self._closed:
            self._log("dropped", t0, "model", "they said something else meanwhile")
            return
        decision = self._validate(parse_decision(getattr(reply, "text", "") or ""))
        child = self._speaker(t0)
        if decision.do != "none" and (child or (needs_grownup and child is not False)):
            self._log("none", t0, "model", "the child spoke" if child else "not known to be a grown-up")
            return
        if decision.do in ("command", "close"):
            self._steer(decision, t0, "model")
        elif decision.do == "show" and decision.spec is not None:
            await self._apply_show(decision.spec, t0, "model")
        else:
            self._log("none", t0, "model", decision.why)

    async def _apply_show(self, spec: dict[str, Any], t0: float, path: str) -> None:
        clean, why = clean_spec(spec, self._types(), self._relaxed())
        if clean is None:
            self._log("none", t0, path, f"card not usable: {why}")
            return
        if self._up_already(clean):
            self._log("none", t0, path, f"a {clean['card']} card is up already")
            return
        result = await self._show(clean, SOURCE)
        self._log(f"show {clean['card']}", t0, path,
                  "" if result.get("ok") else f"not shown ({result.get('error') or 'no card'})")

    async def _ask(self) -> Any:
        provider = self._provider_factory()
        opts = _reply_options(provider.reply, effort=self.effort)
        return await provider.reply([{"role": "user", "content": self._context()}], system=self._prompt(),
                                    max_tokens=400, timeout=self.timeout, **opts)

    def _prompt(self) -> str:
        if self._system is None:
            lines = []
            for kind, ct in sorted(self._types().items()):
                line = (f"- {kind}: {ct.description} Fields: "
                        f"{json.dumps(ct.fields, ensure_ascii=False, separators=(',', ':'))}.")
                if ct.required:
                    line += f" Required: {', '.join(ct.required)}."
                if ct.actions:
                    line += f" Actions: {json.dumps(ct.actions, ensure_ascii=False, separators=(',', ':'))}."
                lines.append(line[:CATALOGUE_LINE_CHARS])
            robot = getattr(self.ctx.config, "robot_name", "LittleBot")
            self._system = PROMPT.format(robot=robot, catalogue="\n".join(lines) or "(none)")
        return self._system

    def _context(self) -> str:
        from littlebot.safety.content import sanitize_user_text  # noqa: PLC0415

        kind = self._current_kind()
        if kind is None:
            head = "Card on screen: none."
        else:
            actions = ", ".join(self._actions(kind)) or "none"
            head = f"Card on screen: {kind}: \"{(self._describe() or kind)[:NOTE_CHARS]}\" (actions: {actions})."
        lines = list(self._lines)
        out = [head, "The conversation so far, latest last:"]
        for i, (who, text) in enumerate(lines):
            if who == "heard":
                label = "They just said" if i == len(lines) - 1 else "Heard"
                out.append(f"{label}: {sanitize_user_text(text, max_chars=LINE_CHARS)}")
            else:
                out.append(f"{getattr(self.ctx.config, 'robot_name', 'LittleBot')}: {text}")
        return "\n".join(out)

    def _validate(self, obj: dict[str, Any] | None) -> Decision:
        """The model's answer, checked hard: anything unexpected becomes "none" (with why, for the log)."""
        if obj is None:
            return Decision("none", why="no JSON in the answer")
        do = str(obj.get("do") or "").strip().lower()
        kind = self._current_kind()
        if do == "show":
            spec, why = clean_spec(obj.get("spec"), self._types(), self._relaxed())
            return Decision("show", spec=spec) if spec is not None else Decision("none", why=f"card not usable: {why}")
        if do == "command":
            action = str(obj.get("action") or "").strip()[:40]
            if kind is None:
                return Decision("none", why="a command with no card up")
            if action == "close":
                return Decision("close")
            if action not in self._actions(kind):
                return Decision("none", why=f"the {kind} card has no such action")
            args = clean_args(obj.get("args"), self._relaxed())
            return Decision("command", action=action, args=args) if args is not None \
                else Decision("none", why="unusable command arguments")
        if do == "close":
            return Decision("close") if kind is not None else Decision("none", why="nothing to close")
        return Decision("none", why="" if do == "none" else "unknown answer")

    # ------------------------------------------------------------ doing it
    def _steer(self, decision: Decision, t0: float, path: str) -> None:
        """A command for the card that is up, or closing it (both instant)."""
        screens = self.screens
        before = self._describe()
        try:
            if decision.do == "close":
                ok = bool(screens.close("asked in the voice chat"))
            else:
                ok = bool(screens.command(decision.action, dict(decision.args)))
        except Exception:  # noqa: BLE001 - a card that can't be steered is left as it is
            log.warning("screen: steering the card failed", exc_info=True)
            ok = False
        what = "close" if decision.do == "close" else f"command {decision.action}"
        self._log(what, t0, path, "" if ok else "the card didn't take it")
        if not ok:
            return
        if decision.do == "close":
            self._tell(CLOSED_NOTE)
            return
        after = self._describe()
        if after and after != before:
            self._tell(CHANGED_NOTE.format(what=after[:NOTE_CHARS]))

    async def _show(self, spec: dict[str, Any], source: str) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(_maybe_await(self.screens.show(spec, source=source)), SHOW_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return {"ok": False, "error": "timeout"}
        except Exception:  # noqa: BLE001 - a card that can't be shown is simply not shown
            log.warning("screen: showing a %s card failed", spec.get("card"), exc_info=True)
            return {"ok": False, "error": "failed"}
        result = result if isinstance(result, dict) else {"ok": bool(result)}
        if result.get("ok"):
            kind = str(spec.get("card"))
            self._last_shown = (kind, _place(spec), self._now())
            what = str(result.get("showing") or self._describe() or spec.get("title") or kind)[:NOTE_CHARS]
            if kind == "games":  # the menu is for choosing: the voice says the names
                self._tell(GAMES_NOTE.format(what=what))
            elif self._told_once:
                self._tell(SHOWN_NOTE.format(what=what))
            else:
                self._told_once = True
                hint = "\"next\" or \"close it\"" if "next" in self._actions(kind) else "\"close it\""
                self._tell(FIRST_NOTE.format(what=what, hint=hint))
        return result  # busy (a game owns the screen), disabled, no_card: nothing is said

    def _tell(self, text: str) -> None:
        if self._closed:
            return
        try:
            self._note(text)
        except Exception:  # noqa: BLE001
            log.debug("screen: the note to the voice failed", exc_info=True)

    def _log(self, what: str, t0: float, path: str, detail: str = "") -> None:
        ms = int((self._now() - t0) * 1000)
        self.history.append({"what": what, "path": path, "ms": ms, "detail": detail})
        log.info("screen: %s (%s, %d ms)%s", what, path, ms, f": {detail}" if detail else "")

    # ------------------------------------------------------------ the browser view (littlebot/browser)
    @property
    def browser(self) -> Any:
        return getattr(self.ctx, "browser", None)

    def _browser_turn(self, text: str) -> bool:
        """An explicit browser request, or steering the browser while it shows: done here, never a card. False:
        an ordinary turn (also when there is no browser view, or the parent switched it off)."""
        browser = self.browser
        if browser is None or self._closed:
            return False
        try:
            up = bool(browser.active)
            if not up and not browser.offered():
                return False
        except Exception:  # noqa: BLE001
            return False
        from littlebot.chatgpt import live  # noqa: PLC0415 - a look at the camera is the call's own

        if live.asks_to_see(text):
            return False
        rule = browser_rule(text, up=up)
        if rule is None:
            return False
        t0 = self._now()
        if self._speaker(t0):  # voice recognition says the child spoke: no browser, no card
            log.debug("screen: no browser (the child spoke)")
            return True
        self._spawn(self._browse(rule, t0, self._turn))
        return True

    async def _browse(self, rule: Decision, t0: float, turn: int) -> None:
        browser = self.browser
        try:
            if rule.action == "open":
                await self._browse_open((rule.spec or {}).get("query"), t0, turn)
            elif rule.action == "read":
                result = await browser.read()
                text = str(result.get("text") or "").strip() if result.get("ok") else ""
                self._log("browser read", t0, "rule", "" if result.get("ok") else str(result.get("error")))
                if text:
                    from littlebot.browser.service import READ_NOTE  # noqa: PLC0415

                    self._tell(READ_NOTE.format(text=text))
                elif result.get("ok"):
                    self._tell("The page on your screen shows no text to read. Say so briefly.")
            elif rule.action == "close":
                ok = bool(browser.close("asked in the voice chat"))
                self._log("browser close", t0, "rule", "" if ok else "it wasn't open")
                if ok:
                    from littlebot.browser.service import CLOSED_NOTE  # noqa: PLC0415

                    self._tell(CLOSED_NOTE)
            else:
                result = await browser.control(rule.action, dict(rule.args), source=SOURCE)
                self._log(f"browser {rule.action}", t0, "rule", "" if result.get("ok") else str(result.get("error")))
                self._browse_note(rule, result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a browser that can't be steered is left as it is
            log.warning("screen: the browser request failed", exc_info=True)

    async def _browse_open(self, query: str | None, t0: float, turn: int) -> None:
        from littlebot.browser.service import ERRORS, OPEN_NOTE, REFUSED_NOTE  # noqa: PLC0415

        path = "rule"
        if not query:
            query, path = await self._browse_query(turn), "rule+model"
        result = await self.browser.open(query or None, source=SOURCE)
        self._log("browser open", t0, path, "" if result.get("ok") else str(result.get("error")))
        if result.get("ok"):
            if not result.get("already"):
                self._tell(OPEN_NOTE.format(what=str(result.get("showing") or "Google")[:NOTE_CHARS]))
        elif result.get("error") == "blocked":
            self._tell(BROWSER_BLOCKED_NOTE)
        else:
            why = str(result.get("detail") or ERRORS.get(str(result.get("error")), "it didn't work this time."))
            self._tell(REFUSED_NOTE.format(why=why[:NOTE_CHARS]))

    async def _browse_query(self, turn: int) -> str:
        """What "google it" means here: the model's few words from the conversation, else the last thing they
        asked before (when it was more than a word or two), else "" (Google's start page)."""
        earlier = [t for who, t in list(self._lines)[:-1] if who == "heard"]
        fallback = ""
        if earlier and real_words(earlier[-1]) >= 2 and browser_rule(earlier[-1], up=True) is None:
            fallback = earlier[-1][:QUERY_CHARS]
        if not self._cloud_ok() or len(self._lines) < 2:
            return fallback
        try:
            provider = self._provider_factory()
            opts = _reply_options(provider.reply, effort=self.effort)
            robot = getattr(self.ctx.config, "robot_name", "LittleBot")
            reply = await asyncio.wait_for(provider.reply(
                [{"role": "user", "content": self._context()}], system=QUERY_PROMPT.format(robot=robot),
                max_tokens=100, timeout=self.timeout, **opts), self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - no model: their last question
            return fallback
        obj = parse_decision(getattr(reply, "text", "") or "")
        query = obj.get("query") if isinstance(obj, dict) else None
        if not isinstance(query, str):
            return fallback
        return _text(query, QUERY_CHARS) or fallback

    def _browse_note(self, rule: Decision, result: dict[str, Any]) -> None:
        """A short note to the voice about what the browser did (so it can say okay, or why not)."""
        if result.get("already"):
            return
        showing = str(result.get("showing") or "the page")[:NOTE_CHARS]
        if not result.get("ok"):
            error = result.get("error")
            if error == "no_result":
                self._tell("There is no such result on the page. Say so briefly.")
            elif error not in (None, "not_open"):
                self._tell(f"The browser couldn't do that: {result.get('detail') or error} Say so briefly.")
            return
        if rule.action == "open_result":
            what = f"the browser opened \u201c{result.get('opened') or 'the result'}\u201d"
        elif rule.action in ("back", "forward"):
            what = f"the browser went {rule.action}: it shows {showing}" if result.get("moved", True) \
                else f"there is no page to go {rule.action} to"
        elif rule.action == "scroll":
            what = "the browser scrolled" + (" (the bottom of the page)" if result.get("at_bottom") else "")
        elif rule.action == "zoom":
            what = "the browser's pages are " + ("bigger" if rule.args.get("level") == "in" else "smaller"
                                                 if rule.args.get("level") == "out" else "back to normal size")
        else:
            what = "the browser reloaded the page"
        self._tell(BROWSER_DONE_NOTE.format(what=what))

    # ------------------------------------------------------------ helpers
    def _types(self) -> dict[str, Any]:
        if self._types_fn is not None:
            return self._types_fn()
        from littlebot.screens.base import card_types  # noqa: PLC0415

        return card_types()

    def _actions(self, kind: str | None) -> dict[str, Any]:
        ct = self._types().get(kind) if kind else None
        actions = getattr(ct, "actions", None)
        return actions if isinstance(actions, dict) else {}

    def _current_kind(self) -> str | None:
        screens = self.screens
        if screens is None:
            return None
        try:
            if not screens.active:
                return None
            card = screens.current
        except Exception:  # noqa: BLE001
            return None
        kind = card.get("kind") if isinstance(card, dict) else getattr(card, "kind", None)
        return str(kind) if kind else None

    def _describe(self) -> str:
        try:
            return str(self.screens.describe() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _up_already(self, spec: dict[str, Any]) -> bool:
        """The same card (kind and place) was shown a moment ago and is still up: don't show it twice (the
        director's card and a tool's for the same question)."""
        last = self._last_shown
        if last is None or last[0] != spec.get("card") or self._now() - last[2] >= RECENT_S:
            return False
        place = _place(spec)
        if place and last[1] and place != last[1]:
            return False
        return self._current_kind() == last[0]

    def _relaxed(self) -> Any:
        try:
            return self.ctx.config.ai.relaxed_rules
        except AttributeError:
            return ()

    def _cloud_ok(self) -> bool:
        try:
            return bool(self.ctx.privacy.cloud_enabled) and not self.ctx.config.is_offline
        except AttributeError:
            return False

    def _screen_taken(self) -> bool:
        games = getattr(self.ctx, "games", None)
        browser = getattr(self.ctx, "browser", None)
        try:
            return (games is not None and bool(games.active)) or (browser is not None and bool(browser.active))
        except Exception:  # noqa: BLE001
            return False

    def _own_spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task


__all__ = ["Decision", "ScreenDirector", "browser_rule", "clean_args", "clean_spec", "local_rule", "parse_decision",
           "prefilter", "unsafe"]
