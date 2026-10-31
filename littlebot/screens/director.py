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

**The card follows the answer** (``wait_for_answer``, on in live calls): the model request waits until LittleBot's
answer is known, so the card agrees with what the voice says and never shows while it is still "thinking": the
answer to the voice's hand-off (``handoff_started`` / ``handoff_answer`` / ``handoff_done``, from live.py; it is
in the request as what LittleBot is saying), else the voice's first caption after their words that isn't a
filler ("hold on, let me check"), ``SAID_GRACE_S`` old (a hand-off may still follow it). At most
``ANSWER_WAIT_S``; no answer by then: no card. When the hand-off's own model put a card up for it (the tools'
``show_on_screen``), nothing more is decided.

Latency: the decision runs in the call's own tasks and never blocks it; it gets ``DECIDE_TIMEOUT_S`` and
the card ``SHOW_TIMEOUT_S``. The target is a card within ~2 s of the answer, while the voice is still
saying it. A decision for a turn that is no longer the newest (they said something else meanwhile) is dropped.

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

from littlebot.screens.base import PICTURE_ACTION

log = logging.getLogger("littlebot.screens.director")

SOURCE = "voice"  # ScreenService.show(source=...) for the director's own cards; the tools use "tool"
DECIDE_TIMEOUT_S = 4.0  # the model's answer; later than this the voice has said it all anyway
RETRY_TIMEOUT_S = 3.0  # one more try when the answer had no usable JSON
RETRY_NOTE = "Your answer was not a JSON object. Answer again with exactly one JSON object and nothing else."
ANSWER_CUE = "Answer with exactly one JSON object and nothing else."
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
ANSWER_WAIT_S = 25.0  # wait_for_answer: at most this long for LittleBot's answer (live.py HANDOFF_TIMEOUT_S)
SAID_GRACE_S = 0.6  # a caption counts as the answer once this old with no hand-off opened after it
# The voice's words as it says them (``saying``): this many are enough to go on, so the card comes while it
# talks. GPT-Live's final caption only comes when its turn ends, 20 s later for a long answer (2026-10-29).
# Only enough to tell an answer from "hold on, let me check": the words come as fast as they are spoken, and
# 25 kept the card waiting ~8 s ("there shouldn't be any wait for the card", the parent, 2026-10-29). The card
# then shows what it is plainly answering (PROMPT).
PARTIAL_WORDS = 6
# GPT-Live starts its answer before the person's final caption reaches LittleBot (on the robot ~1 s earlier,
# 2026-10-29): the words it is saying count for a turn heard up to this long after they began.
SAYING_LEAD_S = 5.0
ANSWER_CHARS = 600
VERDICT_WAIT_S = 1.5  # the decider's "has it answered?" (feature answered, on): a caption waits this long for it
# The decider's "no card" skips the text model only when it is this sure. On the robot an unsure "none" (0.46,
# 0.47, 0.51) kept back the card for a new question (Alexander the Great after the Khans, 2026-10-29); asking
# Haiku costs a call, a missing card is what the parent sees, and Haiku can still answer "none" itself.
NONE_SURE_AT = 0.8
FILLER = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|sure|alright|all right|hmm+|um+|oh|great|good question|let'?s see)\W+)*"
    r"(?:(?:hold|hang) on|one (?:moment|sec(?:ond)?)|just a (?:moment|sec(?:ond)?)|give me a (?:moment|sec(?:ond)?)|"
    r"let me (?:check|see|look|find|think|get|ask)|i'?ll (?:check|look|find out|ask)|i'?m (?:checking|looking|thinking)|"
    r"(?:still )?(?:thinking|checking|working on it)|(?:wait|bear with me))\b"
    # a line that is only "Sure." / "Okay, looking." (the answer comes in its next line)
    r"|^\W*(?:ok(?:ay)?|sure(?: thing)?|alright|all right|of course|mm+|hmm+|um+|oh|looking|on it)"
    r"(?:\W+(?:ok(?:ay)?|sure(?: thing)?|alright|all right|of course|mm+|hmm+|um+|oh|looking|on it))*\W*$",
    re.IGNORECASE)
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
When a card is on screen and they now ask about something else that is worth seeing, answer "show" with the new \
card (it replaces the old one); if the new thing isn't worth a card, answer "close". Never leave a card about \
something else up.
{{"do": "none"}} for everything else: chit-chat, songs, games, talking to the baby, or nothing worth showing.
Never answer the question yourself: the voice does that. Your whole answer is the one JSON object.
The card must agree with what {robot} says: take its numbers, names and facts from {robot}'s answer below, never from what you know yourself, and leave out anything it didn't say. When {robot} hasn't actually given the answer, answer {{"do": "none"}}.
When only the first words of its answer are known (it is still saying the rest), it is answering: make the card for what was asked, from those words and plain well-known facts that fit the question, never against what it said. Leave out live information (weather, times, news, scores) it hasn't said.
Use only the fields listed for the card, with short text (a few words per line, numbers as digits). Never put \
personal details on the screen.

The cards:
{catalogue}"""


def _label(decision: Decision) -> str:
    """A decision as the decider's options name it (littlebot/decide/uses.py ``card_options``)."""
    if decision.do == "show" and decision.spec is not None:
        return f"show_{decision.spec.get('card')}"
    return {"command": "steer", "close": "close"}.get(decision.do, "none")


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
         r"please|wait|sorry|oops|no|hey|littlebot|little bot|lil bot|robot|can you|could you|you can) )*")
_TAIL = r"(?: (?:please|now|thanks|thank you|littlebot|little bot|lil bot|robot))*"
_CARD_NOUN = r"(?:card|screen|picture|page|recipe|timer|weather|forecast|clock|list|sum|word|fact|facts)"
CLOSE_WORDS = re.compile(
    rf"^{_LEAD}(?:"
    rf"(?:close|hide|clear|remove|dismiss)(?: (?:it|that|this|the {_CARD_NOUN}))?(?: (?:off|away|down))?|"
    r"(?:take|put|get) (?:it|that|this) (?:away|off(?: the screen)?|down)|get rid of (?:it|that|this)|"
    r"(?:show me |show |bring back |back to |go back to )your face(?: again| back)?|"
    r"(?:(?:let me|can i|could i|i want to|i wanna|i'?d like to) )?see your (?:face|eyes)(?: again)?|"
    rf"(?:i'?m|we'?re) (?:done|finished) with (?:it|that|this|the {_CARD_NOUN})"
    rf"){_TAIL}$")
# The card's picture full screen / zoomed (cards with a picture: base.PICTURE_ACTION). "Can you zoom into that",
# "just make the image take over the screen" (from a real call).
_PIC = r"(?:it|that|this|the (?:picture|image|photo|drawing|pic))"
_PICTURE_VIEWS = (
    ("in", re.compile(rf"^{_LEAD}(?:just )?(?:zoom (?:in(?:to {_PIC})?|into {_PIC}|on {_PIC})|(?:get |go )?closer)"
                      rf"(?: (?:more|a bit|a little|a lot|further))*{_TAIL}$")),
    ("out", re.compile(rf"^{_LEAD}(?:just )?(?:zoom (?:out|back)|make {_PIC} smaller)(?: (?:more|a bit|a little|again))*{_TAIL}$")),
    ("fill", re.compile(rf"^{_LEAD}(?:just )?(?:(?:make|put|show) {_PIC} (?:bigger|big|full ?screen|on the whole "
                        r"screen|fill the (?:whole )?screen|take (?:over|up) the (?:whole )?screen)|"
                        rf"(?:make|show) {_PIC} (?:as big as|the size of) the screen|full ?screen(?: {_PIC})?|"
                        rf"(?:show me |let me see )?(?:a )?bigger (?:picture|image|one))" + rf"{_TAIL}$")),
    ("back", re.compile(rf"^{_LEAD}(?:just )?(?:back to the card|show (?:me )?the (?:whole )?card(?: again)?|"
                        rf"show (?:me )?the words(?: again)?){_TAIL}$")),
)
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
# With no card up, a turn of fewer than NO_QUESTION_WORDS words is worth asking the model about only when it is a
# question or a request for information (prefilter).
NO_QUESTION_WORDS = 3
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
    # Only a word or two with nothing of a question in them ("she's tired"): longer turns go to the model, which
    # answers "none" for what isn't worth a card. On the robot (2026-10-29) this word list kept a 10-word question
    # about the Khans and "and Alexander the Great?" from ever being asked about: no card.
    if not card_up and not INFO_CUE.search(plain) and len(words) < NO_QUESTION_WORDS:
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
    if PICTURE_ACTION in actions:
        view = next((v for v, words in _PICTURE_VIEWS if words.match(plain)), None)
        if view is None:
            from littlebot.screens.picture_words import picture_view  # noqa: PLC0415

            view = picture_view(plain)  # "show the picture in full screen", "can I see it bigger?"
        if view is not None:
            return Decision("command", action=PICTURE_ACTION, args={"view": view})
    if kind == "weather" and "view" in actions:
        steer = _weather_steer(plain)
        if steer is not None:
            return steer
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


_WEATHER_ASK = re.compile(r"\b(?:show|switch|go|change|put|see|what about|how about|and|weather|forecast|"
                          r"looks?|look like|will it|is it|going to|gonna)\b")
WEATHER_STEER_WORDS = 5  # this short, the view words alone steer it ("the hourly", "and tomorrow?")


def _weather_steer(plain: str) -> Decision | None:
    """The weather card is up: "show me the hourly", "what about Saturday?", "the week" switch its view at once."""
    from littlebot.screens.weather_words import weather_view  # noqa: PLC0415

    view, day = weather_view(plain)
    if view is None or not (_WEATHER_ASK.search(plain) or len(plain.split()) <= WEATHER_STEER_WORDS):
        return None
    return Decision("command", action="view", args={"view": view, **({"day": day} if day else {})})


# ---------------------------------------------------------------- the model's answer
_DROP = object()
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_BARE = re.compile(r"^\W*(none|close)\W*$", re.I)


def parse_decision(text: str) -> dict[str, Any] | None:
    """The JSON object in the model's answer, or None. Forgiving: code fences and words around it, a second
    object after it, trailing commas, or a bare "none" / "close" all still count."""
    t = str(text or "").strip()
    bare = _BARE.match(t)
    if bare:
        return {"do": bare.group(1).lower()}
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    for candidate in (t[start:end + 1], _TRAILING_COMMA.sub(r"\1", t[start:end + 1])):
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    decoder = json.JSONDecoder()
    first: dict[str, Any] | None = None
    i = start
    while 0 <= i <= end:
        try:
            obj, _ = decoder.raw_decode(t, i)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            if "do" in obj:
                return obj
            first = first if first is not None else obj
        i = t.find("{", i + 1)
    return first


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
    if schema.get("type"):  # the right thing in another shape (steps as one string, text as a list): reshaped
        for k in kinds:
            reshaped = _reshaped(value, str(k), schema)
            if reshaped is not _DROP:
                out = _as(reshaped, str(k), schema, depth)
                if out is not _DROP:
                    return out
    return _DROP


_NUMBERED = re.compile(r"(?:^|\s+)\d+[.)]\s+")  # "1. Mix the flour 2. Fry them"
_LINES = re.compile(r"\s*(?:\n+|;\s+)\s*")  # lines, or "a; b"


def _reshaped(value: Any, kind: str, schema: dict[str, Any]) -> Any:
    """``value`` in the shape ``kind`` asks for, when the model sent the same thing in another one (a list as
    one string, a text as a list or a record, "true" as a word); ``_DROP`` when there is no such shape."""
    if kind == "array" and isinstance(value, str):
        text = value.strip()
        pieces = _NUMBERED.split(text) if re.match(r"1[.)]\s", text) else _LINES.split(text)
        return [p.strip() for p in pieces if p.strip()] or _DROP
    if kind == "array" and isinstance(value, dict):
        items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        if items.get("type") == "object":
            return [value]
        return [t for t in (f"{k}: {v}" if isinstance(v, (str, int, float)) else _any_text(v)
                            for k, v in value.items()) if t] or _DROP
    if kind == "string" and isinstance(value, (list, tuple, dict)):
        return _any_text(value) or _DROP
    if kind == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "yes", "false", "no"):
        return value.strip().lower() in ("true", "yes")
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
        extra = schema.get("additionalProperties")  # free-form keys of this shape (a table row: a key per column)
        cleaned: dict[str, Any] = {}
        for key, v in list(value.items())[:MAX_KEYS * 2]:
            name = str(key)[:40]
            if isinstance(props, dict) and name in props:
                c = _clean(v, props[name] if isinstance(props[name], dict) else {}, depth + 1)
            elif isinstance(props, dict) and not isinstance(extra, dict):
                continue
            else:
                c = _clean(v, extra if isinstance(extra, dict) else {}, depth + 1)
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


# A card kind the model made up from a facts layout's name ({"card": "compare"} on the robot, 2026-10-29)
FACTS_KINDS = {"comparison": "compare", "table": "compare", "versus": "compare", "vs": "compare",
               "difference": "compare", "fact": "item", "info": "item", "text": "item", "answer": "item",
               "summary": "list", "points": "list", "key_points": "list", "stat": "number"}


def _as_facts(kind: str, spec: dict[str, Any], types: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A facts card for a spec of an unknown kind (named after a layout, or anything with text in it: however
    the model sends it, it should still be shown, the parent's ask), and a layout for one that has none."""
    fields = getattr(types.get("facts"), "fields", None) or {}
    layouts = list((fields.get("layout") or {}).get("enum") or ())
    layout = str(spec.get("layout") or "").strip().lower()
    layout = FACTS_KINDS.get(layout.replace(" ", "_").replace("-", "_"), layout)
    named = kind if kind in layouts else FACTS_KINDS.get(kind.replace(" ", "_").replace("-", "_"))
    if kind != "facts" and named is None and not _texts({k: v for k, v in spec.items()
                                                          if k not in ("card", "kind", "title")}):
        return kind, spec  # nothing in it to show but a title: stays unknown
    if layout not in layouts:
        layout = named or ("compare" if spec.get("columns") else "list" if spec.get("items") else
                           "number" if spec.get("value") else "score" if spec.get("teams") else "item")
    spec = {**spec, "card": "facts", "layout": layout}
    items = spec.get("items")
    if layout == "compare" and isinstance(items, list) and items and not spec.get("columns") and not spec.get("rows"):
        # a comparison sent as items, as the brain often does (the robot, 2026-10-31): labelled rows naming each
        # of them ({"label": "Snout", "value": "Alligator: broad. Crocodile: narrow."}), or a line for each
        # ("Tiger: largest cat, up to 300 kg"); shown as a profile of cut-off lines before
        table = _labelled_rows(items) if any(isinstance(i, dict) for i in items) else None
        columns = table[0] if table else _subject_columns(items, str(spec.get("title") or ""))
        if columns:
            spec["columns"], spec["rows"] = (table if table else (columns, []))
            spec.pop("items")
            items = None
    if isinstance(items, list) and any(isinstance(i, dict) for i in items):
        # items as records: the strings-only items dropped them all and no card came (2026-10-31)
        spec["items"] = [t for t in (_record_text(i) for i in items) if t]
    return "facts", spec


_ROW_LABELS = ("label", "row", "aspect", "feature", "category", "name", "title", "key")
_ROW_VALUES = ("value", "text", "detail", "details", "fact", "facts", "note", "notes", "description", "difference",
               "summary")
# "Alligator: broad and U-shaped. Crocodile: narrow and V-shaped." -> the name and what follows it, each
_NAMED = re.compile(r"([A-Z][\w'’ -]{0,28}?)\s*:\s*(.+?)(?=[.;,]?\s+[A-Z][\w'’ -]{0,28}?\s*:\s|[.;]?\s*$)", re.S)


def _record_text(record: Any) -> str:
    """One labelled record as a line: "Snout: Alligator: broad ...", a plain string as it is."""
    if isinstance(record, str):
        return record
    if not isinstance(record, dict):
        return ""
    label = next((str(record[k]) for k in _ROW_LABELS if isinstance(record.get(k), str)), "")
    value = next((str(record[k]) for k in _ROW_VALUES if isinstance(record.get(k), (str, int, float))), "")
    return f"{label}: {value}" if label and value else label or value


def _any_text(value: Any) -> str:
    """The text in ``value`` as one line: a record labelled ("Snout: broad") or else its texts joined, a list's
    entries joined; "" when there is none."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        return _record_text(value) or " ".join(t for t in (_any_text(v) for v in value.values()) if t)
    if isinstance(value, (list, tuple)):
        return " ".join(t for t in (_any_text(v) for v in value) if t)
    return ""


def _subject_columns(items: list[Any], title: str) -> list[dict[str, Any]] | None:
    """Items that each describe one of the two or three things the title compares ("Tiger: largest cat, up to
    300 kg", {"label": "Frog", "note": "smooth skin, long legs"}) as compare columns, their facts in short points;
    None when they don't."""
    low = title.lower()
    columns: list[dict[str, Any]] = []
    for item in items:
        name, colon, rest = _record_text(item).partition(":")
        name = " ".join(name.split())
        if not colon or not rest.strip() or not name or len(name) > 24 or name.lower().rstrip("s") not in low:
            return None
        columns.append({"name": name, "facts": _points(rest)})
    if not 2 <= len(columns) <= 3 or len({c["name"].lower() for c in columns}) < len(columns):
        return None
    return columns


def _points(text: str, most: int = 4) -> list[str]:
    """One line of facts as short points: split at sentences, semicolons and commas, a short piece kept with the
    next ("Smooth, moist skin")."""
    out: list[str] = []
    for piece in (p.strip(" .") for p in re.split(r";\s*|,\s+|\.\s+", text.strip())):
        if not piece:
            continue
        if out and len(out[-1]) < 8:
            out[-1] = f"{out[-1]}, {piece}"
        else:
            out.append(piece)
    return out[:most]


def _labelled_rows(items: list[Any]) -> tuple[list[dict[str, Any]], list[str]] | None:
    """Labelled rows that each name the same two or three things ("Alligator: ... Crocodile: ...") as compare
    columns [{"name", "facts"}] and row labels; None when they don't."""
    names: list[str] = []
    rows: list[tuple[str, dict[str, str]]] = []
    for record in items:
        if not isinstance(record, dict):
            return None
        label = next((str(record[k]) for k in _ROW_LABELS if isinstance(record.get(k), str)), "")
        value = next((str(record[k]) for k in _ROW_VALUES if isinstance(record.get(k), str)), "")
        pairs = {" ".join(n.split()).lower(): (" ".join(n.split()), v.strip(" .;")) for n, v in _NAMED.findall(value)}
        if not label or not 2 <= len(pairs) <= 3:
            return None
        if not names:
            names = [shown for shown, _ in pairs.values()]
        if set(pairs) != {n.lower() for n in names}:
            return None
        rows.append((label, {k: v for k, (_, v) in pairs.items()}))
    if len(rows) < 1:
        return None
    columns = [{"name": n, "facts": [facts[n.lower()] for _, facts in rows]} for n in names]
    return columns, [label for label, _ in rows]


def clean_spec(spec: Any, types: dict[str, Any], relaxed: Any = ()) -> tuple[dict[str, Any] | None, str]:
    """A card spec fit for ``ScreenService.show``: a known kind, only its fields (and a title), each of the
    right type and size, the required ones there, every word within the safety rules. Else (None, why).

    However the model sends it, a card with text in it is shown (the parent's ask, 2026-10-31): a string or a
    list, a kind missing what it needs, or an unknown kind becomes a facts card of its text (text_card), and one
    too long is shortened. Only the safety rules, or nothing at all to show, keep it off the screen."""
    out, why = _cleaned(spec, types)
    if out is None and "facts" in types:
        fallback = text_card(spec)
        if fallback is not None:
            log.info("screen: a card that couldn't be used as sent (%s) is shown as a facts card of its text", why)
            out, _ = _cleaned(fallback, types)
    if out is None:
        return None, why
    broken = unsafe(_texts(out), relaxed)
    if broken:
        return None, f"blocked by the safety rules ({broken[0]})"
    return out, ""


def _cleaned(spec: Any, types: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """clean_spec without the fallback and the safety check."""
    if not isinstance(spec, dict):
        return None, "not an object"
    kind = str(spec.get("card") or spec.get("kind") or "").strip().lower()[:40]
    if kind == "facts" or (kind not in types and "facts" in types):
        kind, spec = _as_facts(kind, spec, types)
    ct = types.get(kind)
    if ct is None:
        return None, f"unknown card {re.sub(r'[^a-z0-9_ -]', '', kind)[:40]!r}"
    fields = ct.fields if isinstance(ct.fields, dict) else {}
    allowed = {"title": {"type": "string", "maxLength": 80}, **fields}
    for name in [n for n in spec if n not in allowed]:  # a field under a near name ("meaning" for "meanings")
        near = next((f for f in (f"{name}s", name[:-1] if name.endswith("s") else "")
                     if f in allowed and f not in spec), None)
        if near:
            spec = {**spec, near: spec[name]}
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
    if kind == "facts" and not set(out) - {"card", "layout", "title", "ordered"}:  # its text under other names
        return None, "nothing to show"
    while len(json.dumps(out, ensure_ascii=False)) > MAX_SPEC_CHARS:
        if not _shorten(out):
            return None, "too long"
    return out, ""


def _shorten(value: Any) -> bool:
    """One step shorter: the last entry off the biggest list, else the longest text cut by a third. False when
    there is nothing left to shorten."""
    lists: list[list[Any]] = []
    texts: list[tuple[Any, Any]] = []  # (its container, its key or index)

    def walk(v: Any) -> None:
        pairs = v.items() if isinstance(v, dict) else enumerate(v) if isinstance(v, list) else ()
        if isinstance(v, list) and len(v) > 1:
            lists.append(v)
        for k, child in pairs:
            if isinstance(child, str) and len(child) > 40:
                texts.append((v, k))
            walk(child)
    walk(value)
    if lists:
        max(lists, key=lambda v: len(json.dumps(v, ensure_ascii=False))).pop()
        return True
    if texts:
        box, key = max(texts, key=lambda p: len(p[0][p[1]]))
        text = box[key]
        box[key] = text[:len(text) * 2 // 3].rstrip(" ,;:-") + "…"
        return True
    return False


# Not text to show on a text card: what kind of card, how it looks, where its picture comes from
_NOT_SHOWN = {"card", "kind", "layout", "title", "picture_query", "ordered", "view", "style", "color", "colour",
              "icon", "emoji", "id"}


def text_card(spec: Any) -> dict[str, Any] | None:
    """A facts card of the text in ``spec`` (a list of it, or one item when there is one), for a card that couldn't
    be used as sent; None when there is no text in it to show. Short words alone ("Hamilton", "week") are
    settings, not text to show: a text needs two words, or to be in a list."""
    title = ""
    pieces: list[str] = []
    if isinstance(spec, str):
        pieces = [spec]
    elif isinstance(spec, (list, tuple)):
        pieces = [_any_text(v) for v in spec]
    elif isinstance(spec, dict):
        title = spec["title"] if isinstance(spec.get("title"), str) else ""
        for key, value in spec.items():
            if str(key).lower() in _NOT_SHOWN:
                continue
            if isinstance(value, str) and len(value.split()) >= 2:
                pieces.append(value)
            elif isinstance(value, str) and not title and value.strip():
                title = value  # a word on its own ("ephemeral"): what the card is about
            elif isinstance(value, (list, tuple)):
                pieces += [_any_text(v) for v in value]
            elif isinstance(value, dict):
                pieces.append(_any_text(value))
    texts = list(dict.fromkeys(" ".join(_CONTROL_CHARS.sub(" ", p).split()) for p in pieces))
    texts = [t for t in texts if t][:MAX_ITEMS]
    if not texts:
        return None
    card: dict[str, Any] = {"card": "facts", "title": title}
    if len(texts) == 1:
        card.update(layout="item", text=texts[0])
    else:
        card.update(layout="list", items=texts, ordered=False)
    return card


def text_instead(spec: dict[str, Any], types: dict[str, Any], relaxed: Any = (),
                 sent: Any = None) -> dict[str, Any] | None:
    """The facts card of a checked ``spec``'s text, for a card the screen couldn't make from it ("no_card"); None
    when there is no text, or it is that card already. ``sent``: the spec as the model sent it (its text may be
    under names the card doesn't take: {"card": "answer", "answer": {...}})."""
    fallback = (text_card(sent) if sent is not None else None) or text_card(spec)
    clean = clean_spec(fallback, types, relaxed)[0] if fallback is not None else None
    if clean is None or clean == spec:
        return None
    log.info("screen: a %s card the screen couldn't make is shown as a facts card of its text", spec.get("card"))
    return clean


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
                 now: Callable[[], float] = time.monotonic, wait_for_answer: bool = False,
                 answer_wait_s: float = ANSWER_WAIT_S) -> None:
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
        self._last_tool_card = -1e9  # when a card asked for elsewhere in the call (the tools) was last shown
        self.wait_for_answer = wait_for_answer
        self.answer_wait_s = answer_wait_s
        self._said: collections.deque[tuple[float, str]] = collections.deque(maxlen=LINES)  # (when, caption)
        self._saying: tuple[float, str] | None = None  # (when it began, words so far) of the voice's turn now
        self._handoffs = 0  # the voice's hand-offs open now
        self._handoff_said: tuple[float, str] | None = None  # (when, answer) of the latest hand-off answer
        self._changed = asyncio.Event()
        self._verdicts: dict[tuple[float, str], float | None] = {}  # caption -> P(an answer); None: asking
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
        browser_turn = not handled and self._browser_turn(text)
        if not browser_turn:
            self._browser_moved_on(text)
        if handled or browser_turn or not self.enabled():
            if not browser_turn:
                log.info("screen: nothing to decide (%s)", "another part of the call took it" if handled
                         else "cards are off")
            return
        t0 = self._now()
        child = self._speaker(t0)
        if child:  # (info, as below: a card that didn't come must say why in the log, 2026-10-29)
            log.info("screen: nothing to decide (voice recognition says the child spoke)")
            return
        kind = self._current_kind()
        rule = local_rule(text, kind, self._actions(kind), kinds=self._types())  # clear grown-up phrases
        if rule is not None and rule.do == "none":
            log.info("screen: nothing to do (%s)", rule.why)
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
            log.info("screen: nothing to decide (%s, %d words)", why, len(text.split()))
            return
        self._spawn(self._decide(self._turn, t0, needs_grownup=not grownup))

    # ------------------------------------------------------------ LittleBot's answer (wait_for_answer)
    def handoff_started(self) -> None:
        """The voice handed something to LittleBot (it will say "hold on" until the answer comes)."""
        self._handoffs += 1
        self._ping()

    def handoff_answer(self, text: str) -> None:
        """What the voice was given to say for its hand-off (only real answers, never "say you're thinking")."""
        text = " ".join(str(text or "").split())
        if text and not self._closed:
            self._handoff_said = (self._now(), text[:ANSWER_CHARS])
            self._ping()

    def handoff_done(self) -> None:
        self._handoffs = max(0, self._handoffs - 1)
        self._ping()

    def _ping(self) -> None:
        try:
            self._changed.set()
        except RuntimeError:  # no loop yet (a test without one): nothing waits
            pass

    def _answer_since(self, t0: float) -> tuple[str, str] | None:
        """(how, text) of LittleBot's answer to the turn heard at ``t0``, or None while it isn't known yet."""
        said = self._handoff_said
        if said is not None and said[0] >= t0:
            return "hand-off", said[1]
        if self._handoffs > 0:
            return None  # the voice is saying "hold on": its words aren't the answer
        now = self._now()
        candidates = [(when, text) for when, text in self._said if when >= t0 and not FILLER.match(text)]
        if self._verdicts and any(self._verdicts.get(c, 0.0) is None and now - c[0] < VERDICT_WAIT_S
                                  for c in candidates):
            return None  # the decider is still saying whether this is the answer or "hold on"
        words = [c[1] for c in candidates if not self._stalling(c)]
        newest = max((when for when, text in self._said if when >= t0), default=None)
        if words and newest is not None and now - newest >= SAID_GRACE_S:
            return "voice", " ".join(words)[:ANSWER_CHARS]
        saying = self._saying  # no final caption yet: the words it is saying now, once there are enough
        if (saying is not None and saying[0] >= t0 - SAYING_LEAD_S and now - saying[0] >= SAID_GRACE_S
                and len(saying[1].split()) >= PARTIAL_WORDS and not FILLER.match(saying[1])):
            return "saying", saying[1][:ANSWER_CHARS]
        return None

    def _stalling(self, caption: tuple[float, str]) -> bool:
        """The decider (feature answered, on) is sure this caption only says LittleBot is thinking."""
        p = self._verdicts.get(caption)
        if p is None:
            return False
        from littlebot.decide import get_decider  # noqa: PLC0415

        return p <= 1 - get_decider(self.ctx).yes_at()

    async def _check_answered(self, caption: tuple[float, str], asked: str, mode: str) -> None:
        from littlebot.decide import get_decider, uses  # noqa: PLC0415

        decider = get_decider(self.ctx)
        try:
            answer = await uses.answered(self.ctx, asked=asked, said=caption[1],
                                         robot=getattr(self.ctx.config, "robot_name", "LittleBot"))
        finally:
            if mode == "on":
                self._verdicts[caption] = answer.p if answer is not None else 1.0  # no verdict: the old way
            else:
                self._verdicts.pop(caption, None)
            self._ping()
        if answer is None:
            return
        yes = decider.yes_at()
        theirs = "answer" if answer.p >= yes else "stalling" if answer.p <= 1 - yes else "unsure"
        decider.compare("answered", mine="answer", theirs=theirs, used=mode == "on" and theirs == "stalling",
                        answer=answer)

    async def _await_answer(self, turn: int, t0: float) -> tuple[str, str] | None:
        deadline = t0 + self.answer_wait_s
        while not self._closed and turn == self._turn:
            found = self._answer_since(t0)
            if found is not None:
                return found
            left = deadline - self._now()
            if left <= 0:
                return None
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), min(left, SAID_GRACE_S / 2))
            except TimeoutError:
                pass
        return None

    async def warm(self) -> None:
        """Open the text model's connection ahead of the first decision (best effort)."""
        try:
            warm = getattr(self._provider_factory(), "warm", None)
            if warm is not None and self._cloud_ok():
                await warm()
        except Exception:  # noqa: BLE001
            log.debug("screen: warming the connection failed", exc_info=True)

    def saying(self, text: str) -> None:
        """The voice's words so far in the turn it is saying now (each new piece; the final caption follows)."""
        text = " ".join(str(text or "").split())
        if not text or self._closed:
            return
        begun = self._saying[0] if self._saying is not None and text.startswith(self._saying[1][:40]) else self._now()
        enough = len(text.split()) >= PARTIAL_WORDS
        was = self._saying is not None and len(self._saying[1].split()) >= PARTIAL_WORDS
        self._saying = (begun, text[:ANSWER_CHARS])
        if enough and not was:
            self._ping()

    def said(self, text: str) -> None:
        """A final caption of what the voice said."""
        text = " ".join(str(text or "").split())
        self._saying = None
        if text and not self._closed:
            self._lines.append(("robot", text[:LINE_CHARS]))
            caption = (self._now(), text[:LINE_CHARS])
            self._said.append(caption)
            self._ask_answered(caption)
            self._ping()

    def _ask_answered(self, caption: tuple[float, str]) -> None:
        """Feature answered: is this caption the answer, or a "hold on" the word list doesn't know?"""
        if not self.wait_for_answer or FILLER.match(caption[1]) or not self.enabled():
            return
        from littlebot.decide import get_decider  # noqa: PLC0415

        mode = get_decider(self.ctx).mode("answered")
        if mode == "off":
            return
        asked = next((t for who, t in reversed(self._lines) if who == "heard"), "")
        if not asked:
            return
        live = set(self._said)
        for c in [c for c in self._verdicts if c not in live]:
            self._verdicts.pop(c, None)  # forgotten captions
        if mode == "on":
            self._verdicts[caption] = None
        try:
            self._spawn(self._check_answered(caption, asked, mode))
        except RuntimeError:  # no loop
            self._verdicts.pop(caption, None)

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
        result = await self._show(clean, source, sent=spec)
        if result.get("ok"):
            self._last_tool_card = self._now()  # the director doesn't put up a second card for the same answer
        return result

    # ------------------------------------------------------------ deciding
    async def _decide(self, turn: int, t0: float, *, needs_grownup: bool = False) -> None:
        """``needs_grownup``: a short turn that is a grown-up's only if voice recognition says so by the time the
        answer comes (it may identify them a moment after their words)."""
        answer, partial = None, False
        if self.wait_for_answer:
            found = await self._await_answer(turn, t0)
            if turn != self._turn or self._closed:
                self._log("dropped", t0, "model", "they said something else meanwhile")
                return
            if found is None:
                self._log("none", t0, "model", "LittleBot's answer didn't come in time")
                return
            if self._last_tool_card >= t0:
                self._log("none", t0, "model", "LittleBot put a card up for it already")
                return
            how, answer = found
            partial = how == "saying"  # its first words: the rest is still being said
            log.debug("screen: deciding from LittleBot's answer (%s)", how)
        from littlebot.decide import get_decider, uses  # noqa: PLC0415

        decider = get_decider(self.ctx)
        mode = decider.mode("cards")
        shadow = asyncio.ensure_future(self._card_pick(answer, partial)) if mode == "shadow" else None
        pick, hint = None, None
        try:
            if mode == "on":
                pick = await self._card_pick(answer, partial)
                if turn != self._turn or self._closed:
                    self._log("dropped", t0, "decider", "they said something else meanwhile")
                    return
                if pick is not None and pick.confidence >= decider.min_confidence():
                    if pick.value == uses.CARD_NONE and pick.confidence >= NONE_SURE_AT:
                        decider.used("cards", pick.value, pick)
                        self._log("none", t0, "decider", f"{decider.provider()}, confidence {pick.confidence:.2f}")
                        return
                    # Its "close" is only a hint: on the robot (2026-10-29) it closed the Khans card for "and
                    # Alexander the Great?", which wanted a new card. "Close it" / "hide that" are the close words.
                    hint = pick.value[len("show_"):] if pick.value.startswith("show_") else \
                        "steer" if pick.value == uses.CARD_STEER else \
                        "close" if pick.value == uses.CARD_CLOSE and self._current_kind() is not None else None
            label = await self._decide_model(turn, t0, answer, needs_grownup=needs_grownup, hint=hint,
                                             partial=partial)
        except BaseException:
            if shadow is not None:
                shadow.cancel()
            raise
        if shadow is not None:
            self._spawn(self._compare_cards(shadow, label))
        elif pick is not None and label not in ("dropped", "failed"):
            decider.compare("cards", mine=label, theirs=pick.value, answer=pick)

    async def _card_pick(self, answer: str | None, partial: bool = False) -> Any:
        from littlebot.decide import uses  # noqa: PLC0415

        kind = self._current_kind()
        kinds = {k: getattr(ct, "description", "") for k, ct in sorted(self._types().items())}
        return await uses.card_pick(self.ctx, kinds=kinds, card_up=kind, actions=list(self._actions(kind)),
                                    lines=list(self._lines), answer=answer, partial=partial,
                                    robot=getattr(self.ctx.config, "robot_name", "LittleBot"))

    async def _compare_cards(self, shadow: Any, label: str) -> None:
        """Shadow mode: the decider's pick next to what the text model decided (logged, nothing changes)."""
        try:
            pick = await asyncio.wait_for(shadow, 5.0)
        except asyncio.CancelledError:
            shadow.cancel()
            raise
        except Exception:  # noqa: BLE001 - no pick: nothing to compare
            return
        if pick is not None and label not in ("dropped", "failed"):
            from littlebot.decide import get_decider  # noqa: PLC0415

            get_decider(self.ctx).compare("cards", mine=label, theirs=pick.value, answer=pick)

    async def _decide_model(self, turn: int, t0: float, answer: str | None, *, needs_grownup: bool,
                            hint: str | None = None, partial: bool = False) -> str:
        """The text model's decision, acted on. Returns what it decided ("none", "close", "steer", "show_<kind>"),
        or "dropped" / "failed"."""
        try:
            reply = await asyncio.wait_for(self._ask(answer=answer, hint=hint, partial=partial), self.timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._log("none", t0, "model", "no answer in time")
            return "failed"
        except Exception as e:  # noqa: BLE001 - a failed decision is simply no card
            self._log("none", t0, "model", f"the request failed ({getattr(e, 'kind', None) or type(e).__name__})")
            return "failed"
        if turn != self._turn or self._closed:
            self._log("dropped", t0, "model", "they said something else meanwhile")
            return "dropped"
        text = getattr(reply, "text", "") or ""
        obj = parse_decision(text)
        if obj is None:  # a fast model sometimes answers the question itself: ask once more, firmly
            log.info("screen: the answer had no JSON (%d characters%s); asking once more", len(text),
                     ", starts with text" if text.strip()[:1] not in ("{", "`", "") else "")
            try:
                retry = await asyncio.wait_for(self._ask(retry=True, answer=answer, hint=hint, partial=partial),
                                               RETRY_TIMEOUT_S)
                obj = parse_decision(getattr(retry, "text", "") or "")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - still no card
                obj = None
            if turn != self._turn or self._closed:
                self._log("dropped", t0, "model", "they said something else meanwhile")
                return "dropped"
        decision = self._validate(obj)
        if hint and hint not in ("steer", "close") and decision.do == "command":
            # On the robot (2026-10-29) "and Alexander the Great?" after a Khans card only paged that card ("next"),
            # though the quick check said a new facts card: ask once more, plainly
            log.info("screen: the model steered the card on screen, but the quick check says a new %s card; "
                     "asking once more", hint)
            try:
                again = await asyncio.wait_for(self._ask(answer=answer, hint=hint, partial=partial, insist=True),
                                               RETRY_TIMEOUT_S)
                second = self._validate(parse_decision(getattr(again, "text", "") or ""))
                if second.do == "show":
                    decision = second
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the first decision
                log.debug("screen: asking once more failed", exc_info=True)
            if turn != self._turn or self._closed:
                self._log("dropped", t0, "model", "they said something else meanwhile")
                return "dropped"
        label = _label(decision)
        child = self._speaker(t0)
        if decision.do != "none" and (child or (needs_grownup and child is not False)):
            self._log("none", t0, "model", "the child spoke" if child else "not known to be a grown-up")
            return label
        if decision.do in ("command", "close"):
            self._steer(decision, t0, "model")
        elif decision.do == "show" and decision.spec is not None:
            await self._apply_show(decision.spec, t0, "model")
        else:
            self._log("none", t0, "model", decision.why)
        return label

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

    async def _ask(self, *, retry: bool = False, answer: str | None = None, hint: str | None = None,
                   partial: bool = False, insist: bool = False) -> Any:
        provider = self._provider_factory()
        opts = _reply_options(provider.reply, effort=self.effort)
        content = self._context(answer, partial)
        if hint == "steer":
            content += "\n(A quick check says they are following up on the card on screen.)"
        elif hint == "close":
            content += ("\n(A quick check says the card on screen no longer fits the talk. If what they just asked is "
                        "worth a card, answer \"show\" with a new card for it; otherwise answer \"close\".)")
        elif hint and insist:
            content += (f"\n(Not a command: they asked about something new, not the card on screen. Answer \"show\" "
                        f"with a new {hint} card for what they just asked.)")
        elif hint and self._current_kind() is not None:
            content += (f"\n(A quick check says they asked about something new: show a new {hint} card for it "
                        "rather than steering the card on screen, unless it really is a follow-up on it.)")
        elif hint:
            content += f"\n(A quick check says a {hint} card fits: use it, unless it really doesn't.)"
        content = f"{content}\n\n{RETRY_NOTE if retry else ANSWER_CUE}"
        return await provider.reply([{"role": "user", "content": content}], system=self._prompt(),
                                    max_tokens=400, timeout=RETRY_TIMEOUT_S if retry else self.timeout, **opts)

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

    def _context(self, answer: str | None = None, partial: bool = False) -> str:
        """The card on screen and the conversation; ``answer``: what LittleBot is saying for their last words
        (``partial``: only its first words so far)."""
        from littlebot.safety.content import sanitize_user_text  # noqa: PLC0415

        kind = self._current_kind()
        if kind is None:
            head = "Card on screen: none."
        else:
            actions = ", ".join(self._actions(kind)) or "none"
            head = f"Card on screen: {kind}: \"{(self._describe() or kind)[:NOTE_CHARS]}\" (actions: {actions})."
        lines = list(self._lines)
        out = [head, "The conversation so far, latest last:"]
        latest = max((i for i, (who, _t) in enumerate(lines) if who == "heard"), default=-1)
        for i, (who, text) in enumerate(lines):
            if who == "heard":
                label = "They just said" if i == latest else "Heard"
                out.append(f"{label}: {sanitize_user_text(text, max_chars=LINE_CHARS)}")
            else:
                out.append(f"{getattr(self.ctx.config, 'robot_name', 'LittleBot')}: {text}")
        robot = getattr(self.ctx.config, "robot_name", "LittleBot")
        if answer and partial:
            out.append(f"{robot}'s answer, being said now (only its first words so far, cut off where it has got "
                       f"to: it is answering, and the rest follows): {answer} ...")
        elif answer and not any(who == "robot" and text in answer for who, text in lines[-2:]):
            out.append(f"{robot}'s answer, being said now: {answer}")
        elif answer is None and self.wait_for_answer:
            out.append(f"({robot} hasn't answered yet.)")
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

    async def _show(self, spec: dict[str, Any], source: str, sent: Any = None) -> dict[str, Any]:
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
        if result.get("error") == "no_card":
            instead = text_instead(spec, self._types(), self._relaxed(), sent)
            if instead is not None:
                return await self._show(instead, source)
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

    def _browser_moved_on(self, text: str = "") -> None:
        """A turn that isn't about the browser while it shows: it closes a little later unless used again. With
        the decider's ``browser`` feature on, a turn it is sure is still about the page doesn't count."""
        browser = self.browser
        try:
            if browser is None or not browser.active:
                return
        except Exception:  # noqa: BLE001
            return
        from littlebot.decide import get_decider  # noqa: PLC0415

        mode = get_decider(self.ctx).mode("browser") if text else "off"
        if mode != "on":
            self._tell_moved_on()
        if mode != "off":
            try:
                self._spawn(self._check_page_turn(text, mode))
            except RuntimeError:  # no loop (a test calling from outside one)
                if mode == "on":
                    self._tell_moved_on()

    def _tell_moved_on(self) -> None:
        try:
            self.browser.moved_on()
        except Exception:  # noqa: BLE001 - a browser that can't be told stays until its other timers
            log.debug("screen: telling the browser they moved on failed", exc_info=True)

    async def _check_page_turn(self, text: str, mode: str) -> None:
        from littlebot.decide import get_decider, uses  # noqa: PLC0415

        decider = get_decider(self.ctx)
        try:
            page = str((self.browser.status() or {}).get("showing") or "")
        except Exception:  # noqa: BLE001
            page = ""
        answer = await uses.about_the_page(self.ctx, page=page, said=text)
        about = answer is not None and answer.p >= decider.yes_at()
        if mode == "on" and not about:
            self._tell_moved_on()
        if answer is None:
            return
        theirs = "about_the_page" if about else "moved_on" if answer.p <= 1 - decider.yes_at() else "unsure"
        decider.compare("browser", mine="moved_on", theirs=theirs, used=mode == "on" and about, answer=answer)

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

        return card_types(offered_only=True)

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
