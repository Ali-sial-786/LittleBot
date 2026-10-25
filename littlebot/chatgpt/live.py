"""Live voice: GPT-Live speech-to-speech on the parent's ChatGPT plan (ported from WatchGPT).

A parent starts each conversation from the dashboard (and confirms they are with the child).
LittleBot then opens a WebRTC call to OpenAI's GPT-Live voice model with the ChatGPT sign-in:

    POST https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas
         {"sdp": <offer>, "session": {model, instructions, audio.output.voice, delegation: client}}
    <- the SDP answer; audio then flows directly between the robot and OpenAI (DTLS-SRTP),
       and a data channel ("oai-events") carries captions and delegations (see wire.py).

Safety, enforced here in code (docs/AI_SAFETY.md, "Live voice"):

* only a signed-in parent can start it, from the dashboard, confirming they are present;
  privacy (cloud + microphone) must allow it; STOP ALL, a privacy change or a higher-priority
  task ends it at once; each call has a hard time limit and hangs up when nobody talks;
* every caption of what the voice says goes through the same ``validate_reply`` rules as typed
  replies *while it speaks*. They only stop what could harm a small child; a hit on one of the
  serious ones silences the speaker immediately, ends the call and alerts the parent (category
  only). Some words may already have been heard by then. Asking the child for personal details
  or giving a child's medicine dose doesn't end the call: the voice is told not to (``RULE_NOTES``);
* what the person says is checked with ``classify_child_input``; a worrying category alerts the
  parent (category only, never the words);
* look-ups the voice hands over (delegations: the weather, news, facts, ...) are answered by the
  ChatGPT text model with LittleBot's prompt and validated before the voice is given them; with
  ``chatgpt.tools_enabled`` that model may also act on the robot (volume, videos, activities, games,
  sleep, its own status: littlebot/chatgpt/tools.py). A video, an activity or sleep starts only after
  the voice has said its line; the call then ends ("handed over"), as they need the speaker and screen;
* every hand-off is answered within ``HANDOFF_INTERIM_S`` (5 s): if the answer isn't ready, the voice
  is told to say it's thinking, and the answer follows as a note when it comes (at most
  ``HANDOFF_TIMEOUT_S`` in all, else a short "that didn't work" note);
* a photo is taken only for a question that needs it (the voice's hand-off, or the person's own
  words when the voice doesn't hand it over), only if the parent allows it in Privacy, and at
  most one per question; when looking isn't allowed the voice is told why instead of guessing;
* nothing is recorded; captions live in memory during the call only and are cleared at the end.

With a screen service (``ctx.screens``), a ``ScreenDirector`` (littlebot/screens/director.py) puts info cards on
the screen for what was asked while the voice talks; cards' news (``SCREEN_NEWS``) reaches the voice as notes.
``CALL_STARTED`` / ``CALL_ENDED`` tell the rest of the robot when a call is on.

UNDOCUMENTED endpoint, as in WatchGPT ("a spike to see whether it works, not a contract").
Tested here against a local WebRTC peer standing in for OpenAI, not against OpenAI itself.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import datetime as _dt
import fractions
import logging
import time
import re
import uuid
from typing import Any, Callable

import httpx

from littlebot.brain import identity
from littlebot.brain.prompts import build_system_prompt
from littlebot.brain.providers.base import BaseProvider, ProviderError
from littlebot.chatgpt import get_auth, wire
from littlebot.chatgpt.auth import ChatGPTAuth, ChatGPTAuthError
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.safety.content import BLOCK_RULES, classify_child_input, sanitize_user_text, validate_reply

log = logging.getLogger("littlebot.chatgpt.live")

OWNER = "live_voice"
CALL_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
VOICES = ("cove", "arbor", "breeze", "ember", "juniper", "maple", "sol", "spruce", "vale")  # known; others are tried too
# A look-up the voice hands over that needs the camera ("how many fingers am I holding up?");
# "look up the weather" is a look-up, not a look.
# "Show" only counts as showing LittleBot something ("what am I showing you?"): "show me the hourly" is
# about the screen.
WANTS_LOOK = re.compile(r"\b(?:look(?:ing)?\b(?!\s+(?:it\s+)?up\b)|(?:camera|see|seeing|sees|fingers?|holding|"
                        r"hold(?:s|ing)? up|show(?:s|ing)? you|(?:am|are|is|i'm|we're|he's|she's|they're) "
                        r"(?:\w+ )?showing|colou?rs?|wearing|what(?:'s| is) (?:this|that)|"
                        r"what (?:this|that|these|those) (?:is|are)|in (?:[\w']+ ){0,2}hands?)\b)", re.I)
# Hand-offs clearly about the camera: when looking isn't allowed they get the reason, not the text model.
LOOK_WORDS = re.compile(r"\b(?:look(?:ing)?\b(?!\s+(?:it\s+)?up\b)|camera\b|fingers?\b)", re.I)
# While a card is on the screen, only these (or the person's own words asking to look) take a photo.
CAMERA_WORDS = re.compile(r"\b(?:camera|photo|fingers?|holding|wearing|in (?:[\w']+ ){0,2}hands?|"
                          r"look(?:ing)? at (?:me|us|this|that|him|her|them|it)\b)", re.I)
# A hand-off that is about the camera even though it names a game, a video or an activity ("look: what game
# is the child holding?"): it is looked at, not handed to the tools, unless it also asks for something done.
SEE_FIRST = re.compile(r"\b(?:camera|fingers?|look(?:ing)?\b(?!\s+(?:it\s+)?(?:up|for|around|after)\b)|"
                       r"(?:can|could|do|did) you see|what (?:do|can|did) you see|holding|wearing)\b", re.I)
CONTROL_ACTS = re.compile(r"\b(?:play|put on|start|open|turn|stop|search|find|volume|louder|quieter|sleep|bed|"
                          r"wake)\b", re.I)
# A hand-off with words like these, while a look the person's own words asked for is under way or just
# done, is about that look ("what is that thing?"): it gets that answer instead of the text model's guess.
SEEN_HINT = re.compile(r"\b(?:this|that|these|those|here|hands?|holding|showing|wearing|colou?rs?|shapes?|"
                       r"front of you|picture|photo|see|seeing|looks?)\b", re.I)
_WHO = (r"(?:i|we|he|she|they|it|baby|the baby|daddy|mummy|mommy|mama|papa|dad|mum|mom|"
        r"the (?:child|kid|person|boy|girl|grown-up|parent))")
_POSS = r"(?:my|our|his|her|their|the (?:baby|child|kid|person|boy|girl|grown-up|parent)'s)"
# The person's own words asking LittleBot to look (matched on a final transcript normalised by
# asks_to_see). The voice may say "let me look" without handing the question over; LittleBot then
# looks anyway. Kept narrow: "see you later" or "how many fingers does a cat have" take no photo.
USER_WANTS_LOOK = re.compile(r"\b(?:" + "|".join((
    r"how many fingers\b(?! (?:does|do|did|has|have) (?!i\b|we\b|you see\b))",
    rf"how many(?: \w+){{0,3}} (?:am|are|is|do|have) {_WHO} (?:holding|showing|got)\b",
    r"how many(?: \w+){0,3} (?:can|do|did|could) you see\b",
    rf"what (?:am|are|is) {_WHO} (?:holding|wearing|showing|pointing at)\b",
    rf"what {_WHO} (?:am|are|is) (?:holding|wearing|showing|pointing at)\b",
    rf"what (?:is|are) (?:this|that|these|those) {_WHO} (?:am|are|is) (?:holding|wearing|showing)\b",
    rf"what colou?r(?: \w+){{0,2}} (?:am|are|is) {_WHO} (?:holding|wearing|showing)\b",
    rf"what is in {_POSS} hands?\b",
    rf"what (?:do|does|did) {_WHO} (?:have|hold) in {_POSS} hands?\b",
    rf"what (?:have|has) {_WHO} got (?:here|in {_POSS} hands?)\b",
    rf"(?:can|could|do|did) you see (?:me|us|this|that|these|those|it|him|her|them|{_POSS}|anything|something)\b",
    rf"(?:can|could|do|did) you see what {_WHO} (?:am|are|is) (?:holding|doing|showing|wearing|making|drawing|"
    r"pointing)\b",
    r"(?:(?:do|can|could|did) you (?:know|tell(?: me)?|guess|say|see)|tell me|guess) what (?:this|that|these|those) "
    r"(?:is|are)\b",
    r"are you seeing (?:this|that|me|us)\b",
    r"what (?:do|can|did|could) you see\b",
    r"what are you looking at\b",
    rf"(?:what|which) colou?rs? (?:is|are|am) (?:this|that|these|those|it|i|we|he|she|they|{_POSS})\b",
    rf"what (?:shapes?|animals?|letters?|numbers?|kind of \w+|sort of \w+) (?:is|are) (?:this|that|these|those|"
    rf"{_POSS})\b",
    r"what does (?:this|that) (?:say|spell)\b",
    r"can you read (?:this|that|these|those)\b",
    rf"look(?:ing)? at (?:me|us|this|that|these|those|it|him|her|what|{_POSS})\b",
    rf"look what {_WHO} (?:made|drew|did|built|found|got|have|has)\b",
    r"(?:take|have) a look\b",
    rf"count (?:{_POSS}|these|those) \w+",
)) + r")")
# Short questions that ask to look only when they are the whole sentence ("What's this?", but not "what is
# this song?"; a bare "what's that?" is as often about a noise): matched per sentence, after greetings such
# as "hey LittleBot". FOLLOW_UPS count only right after a look ("and now?" in a finger game).
SEE_SENTENCES = re.compile(r"what (?:is|are) (?:(?:this|these)(?: (?:thing|things|one|ones|here|now|called))?|"
                           r"(?:that|those) (?:thing|things|one|ones))")
FOLLOW_UPS = re.compile(r"(?:and|what about|how about) (?:this|that|these|those|now)(?: one| ones| time)?|"
                        r"how many (?:now|this time)|(?:what about|how about|and) (?:this|that) (?:one )?now")
_FILLERS = re.compile(r"^(?:(?:hey|hi|oh|ooh|wow|look|so|ok|okay|um|uh|littlebot|little bot|robot) )+|"
                      r"(?: (?:littlebot|little bot|robot|please))+$")
# "Do you know who I am?", "who's this?": answered by face recognition (littlebot/vision/faces.py), when it's on.
# Whole sentences (after _sentences), so "who is this song by" or "who is the president" never count.
WHO_SENTENCES = re.compile(
    r"(?:(?:and|but|now|well|then|so|tell me|guess) )*(?:"
    r"who am i|(?:do |can )?you (?:know|remember|recogni[sz]e|tell) (?:who i am|who this is|who is (?:here|talking|"
    r"speaking|with you|in front of you)|me|us|my face|my name)|"
    r"who (?:is|are) (?:this|that|these|those|here|there|talking|speaking|with you|in front of (?:you|the camera|"
    r"your camera))(?: (?:person|man|lady|woman|guy|grown up|people))?|"
    r"who (?:are you|do you think you are) (?:talking|speaking) to|who (?:can|do) you see|"
    r"can you see (?:me|who i am|who this is|who is here)|what is my name|who do you think i am"
    r")(?: (?:now|then|again|yet|today|right now|already))?")
FOLLOW_UP_S = 30.0  # "and now?" this soon after a look asks to look again
LOOK_REUSE_S = 8.0  # a look this recent answers the same question again: one photo per question
# The voice usually hands a seeing question over a moment after the person's last words, and then gets the
# answer at once. A look started from their own words therefore tells the voice itself (a note) only when the
# voice starts talking without handing it over, or NOTE_GRACE_S after their last words with no hand-off
# (never while they are still talking, unless NOTE_TURN_WAIT_S passed). A hand-off after such a note is told
# it already had the answer (ALREADY_TOLD), so the voice says it once.
NOTE_GRACE_S = 0.5
NOTE_TURN_WAIT_S = 4.0
ALREADY_TOLD = "(You were told this a moment ago too: if you already said it, don't say it again.)"
# While LittleBot looks, the voice can only fill the wait ("One second...", "Let me look!"), whatever its
# instructions say. So its sound is held back from the moment a look starts until it has the answer, and let
# through again at the next pause in what it says (HUSH_PAUSE_S of quiet: never mid-word), so the person hears
# the answer only. Never longer than HUSH_MAX_S per look.
HUSH_MAX_S = 6.0
HUSH_PAUSE_S = 0.16
HUSH_QUIET_LEVEL = 0.01  # RMS (full scale 1.0) below which a 20 ms frame is quiet
# Said on their own, these stop it. The whole utterance must be one of them, so "can you stop the video?" or
# "stop that, Emma" don't. END_CALL ("bye", "hang up", "stop listening") ends the call at once; HUSH_CALL
# ("stop", "be quiet", "shh") makes the voice go quiet and wait for them (QUIET_END_S: then the call ends),
# except as their first words in a call (a wake word heard by mistake): then it ends too. The wake word
# listens again after a call.
_STOP_LEAD = (r"(?:ok|okay|oh|um+|uh+|no|hey|please|just|alright|all right|littlebot|little bot|bot|robot|"
              r"(?:can|could|would|will) you(?: please)?|you can|i want you to|i need you to)")
_STOP_TAIL = r"(?: (?:please|now|thanks|thank you|littlebot|little bot|bot|robot))*$"
_HUSH_WHAT = (r"(?:stop(?: (?:talking|it|now|that|for now))*|stop talking to me|be quiet|quiet|shh+|hush|shush|"
              r"shut up|that'?s enough|enough)")
_END_WHAT = (r"(?:stop(?: (?:talking|it|now|that|for now))* and (?:stop )?listening|"
             r"stop listening(?: (?:to me|now|for now))*(?: and (?:stop )?talking)?|"
             r"never ?mind|cancel(?: that)?|go away|hang up|end (?:the )?call|that'?s (?:all|it)|"
             r"(?:good ?)?bye(?: bye)?|i'?m done|we'?re done|false alarm|"
             r"(?:i was )?not talking to you|i wasn'?t talking to you|wrong (?:person|robot))")
HUSH_CALL = re.compile(rf"^(?:{_STOP_LEAD} )*{_HUSH_WHAT}{_STOP_TAIL}")
END_CALL = re.compile(rf"^(?:{_STOP_LEAD} )*{_END_WHAT}{_STOP_TAIL}")
STOP_CALL = re.compile(rf"^(?:{_STOP_LEAD} )*(?:{_END_WHAT}|{_HUSH_WHAT}){_STOP_TAIL}")  # either
QUIET_END_S = 30.0  # quiet after "stop" and they said nothing more for this long: the call ends
QUIET_NOTE = "They asked you to be quiet. Say nothing at all until they talk to you again."
STOP_SETTLE_S = 0.8  # their words so far say stop and nothing more came: stop (the final words may come late)
# The person's own words asking for one of LittleBot's on-screen games: LittleBot starts it itself (the voice
# can't see the screen and would often just play it out loud), keeps the voice quiet until it has been told,
# passes their moves ("rock!") to the game and lets the game say what happened (who won).
GAME_ALIASES: tuple[tuple[str, str], ...] = (
    ("rps", r"rock paper (?:and )?scissors?"),
    ("tictactoe", r"tic ?tac ?toe|noughts and crosses|x'?s and o'?s"),
    ("bubbles", r"bubble pop|bubbles? game|pop(?:ping)? (?:the )?bubbles"),
    ("peekaboo", r"peekaboo friends|peek ?a ?boo (?:game|friends)"),
    ("colours", r"colou?r burst|colou?rs? game|painting game"),
    ("star", r"follow the star|star game"),
    ("memory", r"memory lights|memory game"),
    ("reaction", r"reaction (?:race|game)"),
)
_GAME_VERB = r"(?:play(?:ing)?|start|open|put on|show me|try)"
STOP_GAME = re.compile(r"\b(?:stop (?:the )?(?:game|playing)|(?:i'?m|we'?re) (?:done|finished) playing|"
                       r"no more (?:games?|playing)|(?:end|quit|close|exit) (?:the )?game|"
                       r"(?:show|go back to|back to|bring back) (?:me )?your face|your face back)\b")
GAME_NOTES = {
    "rps": "They play by saying (or showing the camera) rock, paper or scissors. LittleBot picks on the screen "
           "and tells you who won: never make your own pick or play it out loud. ",
}
PARTIAL_MAX_CHARS = 300  # what the person is still saying, kept to start a look before they finish
CANT_SEE = "You couldn't look. Say you can't see right now, and suggest the grown-up shows you again later; " \
           "don't guess what is there."
# Why LittleBot can't look (CameraLook.blocked_reason), with the words for the voice to use. The wording
# avoids "turn on the camera": the safety rules treat that as asking for pictures and would end the call.
CANT_LOOK = {
    "ai_camera_off": "You can't see yet, so don't guess or pretend. Tell them, in these words: \"I can't see you yet. "
                     "A grown-up can let me look by switching on Let the AI look through the camera, on the Privacy "
                     "page of my dashboard.\"",
    "camera_off": "You can't see right now, so don't guess or pretend. Tell them, in these words: \"I can't see right "
                  "now, because my camera is switched off on the Privacy page of my dashboard. A grown-up can switch "
                  "it back on there.\"",
    "no_camera": "You can't see right now, so don't guess or pretend. Tell them, in these words: \"I can't see right "
                 "now, my camera isn't working. A grown-up can check it.\"",
}
_NO_CHATGPT = "you couldn't reach ChatGPT to look"
# What went wrong while looking (CameraLook.look error codes), in words the voice can say.
LOOK_FAILED = {
    "photo_failed": "your camera couldn't take a picture",
    "timeout": "looking took too long",
    "busy": "you were already looking at something else",
    "refused": "ChatGPT wouldn't say what it saw",
    **{code: _NO_CHATGPT for code in ("network", "rate_limited", "usage_limit", "expired", "auth",
                                      "plan_not_included", "provider_error")},
}
# Dashboard caption wording where look.ERRORS isn't specific enough.
LOOK_NOTES = {"ai_camera_off": "\"Let the AI look through the camera when asked\" is off in Privacy."}
FACE_REUSE_S = 5.0  # the same face asked for again this soon (words, then a hand-off) plays once
# Faces the voice announces in its own words ("Peekaboo!", "here's my happy face"), played even when it
# doesn't hand them over. Conservative: only the voice talking about its own face, at most once per turn.
_VOICE_EXPRESSIONS = {"happy": "happy", "surprised": "surprised", "surprise": "surprised", "sad": "sad",
                      "sleepy": "sleepy", "silly": "playful", "playful": "playful", "excited": "excited",
                      "curious": "curious", "confused": "confused", "puzzled": "confused", "thinking": "thinking"}
VOICE_FACE_CUES: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(r"\bpeek[\s-]?a[\s-]?boo\b", re.I), "peekaboo"),
    (re.compile(r"\bspin(?:s|ning)? my eyes\b|\bmy eyes (?:go |are )?spin(?:s|ning)?\b", re.I), "spin_eyes"),
    (re.compile(r"\bwink(?:s|ing)?\b", re.I), "wink"),
    (re.compile(r"\bmy (" + "|".join(_VOICE_EXPRESSIONS) + r") face\b", re.I), None),  # that expression
)
MAX_SEEN_TYPES = 40
POLL_S = 0.05
GOODBYE_QUIET_S = 0.8
GOODBYE_MAX_S = 10.0
FIRST_WORDS_MIN_S = 30.0
MAX_CAPTIONS = 8
HANDOFF_INTERIM_S = 5.0  # no answer yet: the voice is told to say it's thinking (the answer follows as a note)
HANDOFF_TIMEOUT_S = 25.0  # a hand-off the voice waits on always gets an answer (or a "didn't work" note) within this
HANDOVER_MAX_S = 15.0  # a video / activity / sleep was promised: the call ends this long after, at the latest
HANDOVER_REASON = "handed over to what was asked for"
STILL_WORKING = ("LittleBot is still working on that. Say, in a few words, that you're thinking about it and will "
                 "tell them in a moment, then wait: the answer comes next.")
# "remember: Ali is Amara's dad" / "forget: ..." from the voice (VOICE_INSTRUCTIONS); not a question about the past.
REMEMBER_ASK = re.compile(r"^\s*(?:please\s+)?(remember|forget)\b\s*(?::|that\b)?\s*([^?]+?)[\s.!]*$", re.I)
PULSE_EVERY_S = 20.0  # a "still on" line of counts in the log during a call (_pulse)
AUDIO_REOPENS = 2  # a microphone or speaker that stopped is opened again at most this often in one call
SENT_STALL_S = 8.0  # the microphone gives sound but none of it is read (sending stopped): the call ends
FACES_WAIT_S = 1.0  # from the call's start: how long connecting waits for the first face check (see _instructions)
VOICES_WAIT_S = 1.5  # ...and for knowing, by voice, who said "Hey LittleBot" (littlebot/voices/call.py)
TALKS_FROM_MONTHS = 24  # younger: whoever talks to LittleBot in sentences is a grown-up, not the child
NAMING_MIN_WORDS = 3  # their turn had this many words: a grown-up talking (babbling is shorter)
NAMING_WINDOW_S = 20.0  # the voice's reply this soon after it counts as talking to that grown-up
NAME_FIXES = 3  # at most this many "you called the grown-up by the child's name" notes a call
LATE_ANSWER = "Here is the answer to what they asked a moment ago: {answer} Tell them now, in your own words."
LATE_FAILED = "What they asked a moment ago didn't work out this time. Say so briefly; they can ask again."
# Hand-offs that ask LittleBot to do something with the robot or the app, not to make a face or to look
# ("start the peekaboo activity", "show me a video of a dog", "go to sleep"): LittleBot's tools answer them.
CONTROL_WORDS = re.compile(r"\b(?:activit(?:y|ies)|games?|videos?|youtube|cartoons?|volume|louder|quieter|"
                           r"(?:go(?:es|ing)?|went|put \w+) to (?:sleep|bed)|wake (?:up|\w+ up)|temperature|"
                           r"how hot|cpu|memory|wi-?fi|stop everything|debug(?:ging)?|(?:debug|your|the) logs?|"
                           r"(?:went|go(?:es|ne)?|going) wrong|(?:didn'?t|did not|doesn'?t|wasn'?t|isn'?t|won'?t) "
                           r"work(?:ing)?|not working)\b", re.I)
# Hand-offs asking for live information: the text model answers them with its tools (get_weather, get_time,
# web search), never the camera or a face ("what's the weather looking like?", "show me the weather").
INFO_WORDS = re.compile(r"\b(?:weather|forecasts?|hourly|rain(?:ing|y)?|snow(?:ing|y)?|sunny|umbrella|windy|"
                        r"humid(?:ity)?|degrees|(?:hot|cold|warm|chilly) (?:outside|out there)|"
                        r"what time|what(?:'s| is) the time|time is it|what day|what date|(?:the|today'?s) date|"
                        r"news|headlines?|scores?)\b", re.I)
CHECK_WINDOW = 320  # characters of a reply in progress checked at a time
NOT_SURE = "LittleBot has no answer for that. Say briefly, in your own words, that you're not sure."
LOOKUP_MAX_WORDS = 60  # a hand-off's answer: room for two to four sentences for a grown-up
# Rule hits that don't end the call (the words were already said; ending it wouldn't take them back):
# the voice is told not to do it again instead. Every other rule hit ends the call.
RULE_NOTES = {
    "personal_info": "LittleBot's safety rules: don't ask the child for personal details such as their name, "
                     "address or school. Carry on with something else.",
    "medicine_dose": "LittleBot's safety rules: don't give medicine doses for a child. Say to follow the label "
                     "or ask a pharmacist or doctor.",
}

ERRORS = {
    "parent_not_present": "Live voice only runs while a grown-up is with your child. Tick the box to confirm.",
    "offline": "LittleBot is in offline mode.",
    "cloud_off": "Cloud communication is turned off in Privacy.",
    "mic_off": "The microphone is turned off in Privacy.",
    "not_signed_in": "Sign in with ChatGPT first.",
    "unavailable": "Live voice isn't installed on this robot yet: re-run scripts/install_pi.sh, or run "
                   "cd ~/LittleBot && .venv/bin/python -m pip install -e '.[live]' and restart LittleBot.",
    "no_audio": "LittleBot's audio system is not running.",
    "already_running": "A live conversation is already running.",
    "busy": "LittleBot is busy with something else. Stop it first.",
}

CALL_ERRORS = {
    401: "The ChatGPT sign-in expired. Sign in again.",
    403: "Live voice is not available for this ChatGPT account or region. It uses an undocumented "
         "ChatGPT endpoint; a subscription doesn't guarantee access.",
    429: "ChatGPT voice usage limit reached. Try again after your plan's allowance resets; nothing else was used.",
    404: "The undocumented ChatGPT voice endpoint is unavailable or changed.",
}

VOICE_INSTRUCTIONS = """\
You are {name}, a small, friendly robot with a screen face, talking out loud with a family: \
{child} and the child's grown-ups, who are right there. You are the little one's playful companion and the \
grown-ups' helpful home assistant.

How you talk:
- With the little one: very short, simple, warm sentences, slow and cheerful; one or two sentences, then stop \
and listen. They may babble, coo or make sounds rather than words: answer with gentle, playful words, copy \
their sounds, animal noises, counting, colours, peekaboo, or a few lines of a well-known nursery rhyme.
- With a grown-up: answer their question properly and plainly, like a good home assistant, usually in two to \
four sentences, and help them play with the child.
- No lists, no reading out links (say the website's name), no emoji names, no stage directions.
- You are a robot, not a person or a parent. If asked, say you are a robot.

Answering:
- Answer what you are asked: facts, the weather, news, how-to, shows, videos, apps and products are all fine.
- Parents may ask about their child's health, feeding, sleep or development: give helpful, practical, general \
information, and say when a doctor should check. Don't diagnose, and don't give an amount for a child's \
medicine dose: say to follow the label or ask a pharmacist or doctor.

Keeping the child safe:
- Never ask the child for personal information: full name, address, school, phone numbers, passwords.
- Never suggest keeping secrets from grown-ups or meeting anyone, and never invite the child to do anything \
dangerous: climbing furniture, water, heat, sharp things, small things in the mouth, medicine, going off alone, \
strangers.
- Nothing sexual, gory or hateful, and nothing about self-harm.
- If a child might be hurt or in danger, say: "Let's get a grown-up right now." If a grown-up asks about an \
emergency, give clear first-aid steps and tell them to call emergency services if it could be serious.

For anything live or current (the weather and forecast, news, sports, the date, opening hours, facts you are \
not sure of), delegate it to the client and wait for the result: LittleBot can look things up. Then say it \
simply in your own words. Never say you can't check the weather or the news. Whenever you delegate anything, \
do it at once and silently: never say "let me check", "let me look", "let me see", "one moment", "hold on" or \
"hmm" first.

You start every chat fresh, but LittleBot keeps a few notes for you. When a grown-up asks you to remember \
something (who they are, a name, something the family likes), delegate it to the client as "remember: <the fact, \
with names, not I or me>" (for example "remember: Sam is the baby's dad"), wait, then say you'll remember it. To \
forget one, delegate "forget: <the fact>". Never say you'll remember something without doing this.

The client knows live information: the weather (now, today, tomorrow, anywhere), the time and the date, and \
things that change, like news, sports scores or opening hours. For these, always delegate the question to the \
client in plain words (for example "what's the weather today" or "what time is it"), never guess or say you can't know.

You have an animated face on a screen (big cartoon eyes). Faces you can make: {faces}. When someone asks you \
to make a face, wink, spin your eyes or play peekaboo, delegate it to the client as "face: <name>" (for example \
"face: happy" or "face: peekaboo"), wait, then carry on talking. You may also do this yourself at big moments \
(a celebration, a surprise), but not on every sentence. Never say the word "face:" out loud.

You have a camera, but you only see when the client looks for you. When someone asks what you see, shows you \
something, or asks how many fingers they are holding up, what colour something is, or similar, delegate it to the \
client straight away with the word "look" and their question, without saying anything first. The answer comes \
back in a second or two: then start with the answer itself (for example "Three fingers!"). Never pretend to see \
without that. If the client tells you what your camera just saw, or why you can't see, tell them that.

The client can also control you and your app. When someone asks you to change the volume, put on a video or a \
song video, start one of your activities (songs, stories, peekaboo and more), play a game on your screen, go to \
sleep or wake up, stop everything, or asks about yourself (how hot you are, your memory, your Wi-Fi, what is \
connected, what you can do), delegate it to the client in plain words, for example "turn the volume up", "play a \
baby shark song video", "start the peekaboo activity", "play a game", "go to sleep", "how hot is the robot", \
"what can the robot do". Wait, then say what the client tells you, briefly. A video, an activity or going to sleep \
starts after you finish speaking, and then this voice chat ends: say a short goodbye. If the client says it is \
still working on something, say you are thinking and wait; the answer comes soon. A game stays on the screen \
while you keep talking, and LittleBot runs it: never play it out loud or make your own move (in Rock Paper \
Scissors LittleBot picks on the screen); the client tells you what happened, then you say it."""

# Added to VOICE_INSTRUCTIONS when the call has a screen director (littlebot/screens/director.py).
SCREEN_INSTRUCTIONS = """
Your screen also shows info cards, and you can show things on it: the weather and forecast (now, by the hour, \
the next days), a word's meaning, the key points of an explanation with a picture, a recipe one or two big steps \
at a time, timers, the time, sums and maths, quick facts, and the menu of your games. When something you are \
asked is worth seeing, LittleBot usually puts a card up by itself while you talk, and a note tells you what is on \
it. Never read a card out or describe it word for word: say the answer in your own words, and you may point to it \
("look!"). When someone asks you to show something, put it on the screen, or asks why it isn't on the screen, \
delegate it to the client as "show on the screen: <what, with the details>" (for example "show on the screen: 2 + \
2 = 4" or "show on the screen: why New York is called the Big Apple"), wait, then carry on. Never say you can't \
show things on your screen. If they ask to change or close what is on the screen ("next step", "tomorrow", "by \
the hour", "close it"), just say okay in a few words: LittleBot does it."""
SCREEN_NEWS = "screen.news"  # {text}: a card wants the voice to say something ("the egg timer is done")
POWER_NOTE = Topics.POWER_NOTE  # {text, goodbye}: the battery is low (a note) or empty / too hot (goodbye, then end)
# Published by live voice for whoever needs to know a call is on (the screen service: a card's presenter talks).
CALL_STARTED = "live.call_started"  # {} the call is connected: the voice can talk now
CALL_ENDED = "live.call_ended"  # {reason} the call is over (also when it never connected)
# Added to the instructions when a grown-up can talk over the voice (barge-in: echo cancelling is on).
BARGE_IN_INSTRUCTION = ("If someone talks while you are speaking, stop and listen to them. If all they say is "
                        "something like \"oops, go ahead\" or \"sorry, carry on\", carry on from where you stopped, "
                        "without starting over.")
BARGE_IN_GO_AHEAD = "They didn't mean to interrupt: carry on from where you stopped, without starting over."

LOOKUP_NOTE = f"""
You are answering a question that LittleBot's voice will say out loud to a family with a very young child; \
usually a grown-up asked it. Answer it properly in plain spoken sentences: two to four for a grown-up, one or \
two simple ones for the child, at most {LOOKUP_MAX_WORDS} words. Give your best answer rather than saying you're \
not sure, but never make up live facts (today's weather, the news) that you couldn't check."""

LIVE_TOOLS_NOTE = """
Use your tools for live information (the weather and forecast, the date and time, news, sports, opening hours, \
anything current): look it up and answer with what you found; never say you can't check it. The hand-off may also \
ask LittleBot to do something (the volume, a video, an activity, a game, sleep) or ask about LittleBot itself (how \
hot it is, its memory, Wi-Fi, what it can do): use your tools for that too, then answer with one short sentence \
for the voice to say. When it asks to show something on the screen, call show_on_screen with the card that fits \
best (an explain card with a picture for an explanation, a maths card for a sum), then answer briefly."""
HANDOVER_WHAT = {"video": "the video starts", "activity": "the activity starts", "sleep": "LittleBot goes to sleep"}
# A video paused for this chat ("Hey LittleBot" while it played: littlebot/audio/wakeword.py, MediaController.hold).
# The person's own words, or the voice's hand-off, carry on with it (once the voice said goodbye, as a hand-over)
# or stop it (the chat goes on). Matched on plain words (_plain_words), only while a video is paused for a chat.
_VID = r"(?:videos?|cartoons?|shows?|movies?|films?|programmes?|programs?|episodes?)"
VIDEO_RESUME = re.compile(r"\b(?:" + "|".join((
    r"(?:keep|carry on|continue|go on|start) watching",
    rf"(?:resume|unpause|continue|carry on(?: with)?|restart|finish) (?:the |my |that |this |our )?{_VID}(?: again)?",
    rf"(?:play|put on|start) (?:the |my |that |this |our ){_VID}(?: again| back)?"
    r"(?! (?:of|about|with|where|for|from|called)\b)",  # "play the video of the cat" asks for a new one
    r"(?:resume|unpause|continue|carry on)(?: it| that)?(?: please)?$",
    r"(?:play|put|turn|start) (?:it|that) (?:back on|again|on again)",
    rf"(?:go |get |going )?back to (?:the |my |our |that )?{_VID}",
    rf"(?:i|we) want (?:my |the |our )?{_VID}(?: back| again)?$",
    rf"(?:can|could|may) (?:i|we) (?:have|watch|see) (?:my |the |our |that )?{_VID} (?:back|again)",
    rf"watch (?:it|that|the {_VID}|my {_VID}) again",
    r"(?:i|we) want to (?:keep )?watch(?:ing)?(?: it| that| my \w+| the \w+)?(?: again)?$",
)) + r")\b")
VIDEO_STOP = re.compile(r"\b(?:" + "|".join((
    rf"(?<!don't )(?<!do not )(?:stop|end|close|finish|cancel|pause) (?:the |that |this |my )?{_VID}",
    rf"(?<!don't )(?<!do not )(?:turn|switch) (?:it|that|the {_VID}|the tv) off",
    rf"(?<!don't )(?<!do not )(?:turn|switch) off (?:the |that |this )?(?:{_VID}|tv)",
    rf"no more (?:{_VID}|tv|watching)",
    r"(?:stop|done|finished|enough) watching",
    rf"enough (?:{_VID}|tv)",
    rf"(?:i|we) don'?t want (?:to watch )?(?:the |that |this |my )?{_VID}(?: any ?more)?",
)) + r")\b")
VIDEO_REPEAT_S = 20.0  # a hand-off asking again for what their own words just did gets "done" at once
VIDEO_NOTE = ("While you talk, the video \u201c{title}\u201d is paused on LittleBot's screen and your face is "
              "showing. If they want to keep watching (\"keep watching\", \"play it again\", \"go back to the "
              "video\"), delegate \"resume the video\" to the client: the video then carries on where it stopped and "
              "this voice chat ends. If they want it off, delegate \"stop the video\" and keep talking. If they "
              "want something else, help them as usual.")
VIDEO_RESUMING = ("LittleBot heard them: the video \u201c{title}\u201d carries on where it stopped as soon as you have "
                  "said this, and this voice chat ends then. Say a very short goodbye now.")
VIDEO_STOPPED = ("LittleBot stopped the video \u201c{title}\u201d, as they asked; your face stays on the screen. "
                 "Say so in a few words, then carry on talking.")


def _speakable(text: str, relaxed: Any = ()) -> str | None:
    """A hand-off's answer as the voice may be given it (``validate_reply``), or None. A child's medicine
    dose is swapped for "check the label or ask a pharmacist" (DOSE_FALLBACK), which the voice may say."""
    if not text.strip():
        return None
    checked = validate_reply(text, max_words=LOOKUP_MAX_WORDS, allow=relaxed)
    return checked.text if checked.ok or checked.reasons[:1] == ["medicine_dose"] else None


def aiortc_error() -> str | None:
    try:
        import aiortc  # noqa: F401, PLC0415
        import av  # noqa: F401, PLC0415
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None


class CallFailed(Exception):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"call failed {status}")
        self.status = status
        self.detail = detail


class LiveVoiceService(Service):
    """``ctx.live``: one parent-started live conversation at a time."""

    name = "live"

    def __init__(self, ctx: Any, *, auth: ChatGPTAuth | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 audio_factory: Callable[[], Any] | None = None, provider: BaseProvider | None = None,
                 call_url: str = CALL_URL, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.chatgpt
        self.auth = auth or get_auth(ctx)
        self._transport = transport
        self._audio_factory = audio_factory or self._default_audio
        self._provider = provider
        self._provider_given = provider is not None
        self._screen_provider: BaseProvider | None = None
        self.call_url = call_url
        self._now = now
        self._call: LiveCall | None = None
        self._task: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[Subscription] = []
        self.last_call: dict[str, Any] | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        bus = self.ctx.bus
        self._subs = [
            bus.subscribe(Topics.STOP_ALL, lambda e: self._end_threadsafe("stopped (STOP ALL)")),
            bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
            bus.subscribe("game.news", self._on_game_news),  # littlebot.games.service.GAME_NEWS
            bus.subscribe(SCREEN_NEWS, self._on_screen_news),
            bus.subscribe(POWER_NOTE, self._on_power_note),
        ]
        err = aiortc_error()
        if err:
            self.set_status(ServiceStatus.DEGRADED, "live voice not installed (.venv/bin/python -m pip install -e '.[live]')")
        else:
            self.set_status(ServiceStatus.RUNNING, "")

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        await self.stop_session("service stop")
        if self._provider is not None:
            await self._provider.aclose()
        if self._screen_provider is not None and self._screen_provider is not self._provider:
            await self._screen_provider.aclose()
        self.set_status(ServiceStatus.STOPPED)

    def _on_privacy(self, event: Event) -> None:
        p = self.ctx.privacy
        if not (p.cloud_enabled and p.microphone_enabled):
            self._end_threadsafe("stopped (privacy settings changed)")

    def _on_game_news(self, event: Event) -> None:
        """A game says what happened ("they picked rock, you picked paper: you win"): the voice says it."""
        self._to_call("game_news", str(event.get("text") or ""))

    def _on_screen_news(self, event: Event) -> None:
        """A card on the screen has news ("the egg timer is done"): the voice says it."""
        self._to_call("screen_news", str(event.get("text") or ""))

    def _on_power_note(self, event: Event) -> None:
        """The battery (littlebot/power) is low, or empty / too hot: a quiet note (then goodbye and the end)."""
        self._to_call("power_goodbye" if event.get("goodbye") else "power_news", str(event.get("text") or ""))

    def _to_call(self, method: str, text: str) -> None:
        call, loop = self._call, self._loop
        if call is None or loop is None or loop.is_closed() or not text:
            return
        try:
            loop.call_soon_threadsafe(getattr(call, method), text)
        except RuntimeError:
            pass

    def _end_threadsafe(self, reason: str) -> None:
        call, loop = self._call, self._loop
        if call is None or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(call.end, reason)
        except RuntimeError:
            pass

    # ------------------------------------------------------------ helpers
    def _default_audio(self) -> Any:
        from littlebot.chatgpt.live_audio import BargeInSettings, LiveAudio  # noqa: PLC0415

        a = self.ctx.config.audio
        max_vol = float(a.speaker.max_volume)

        def volume() -> float:  # the parent's volume setting, never above the configured maximum
            v = self.cfg.live_volume
            if v is None:
                v = self.ctx.settings.get("audio.volume", a.speaker.volume)
            try:
                return max(0.0, min(max_vol, float(v)))
            except (TypeError, ValueError):
                return min(max_vol, float(a.speaker.volume))

        def gain() -> float:
            try:
                return float(self.ctx.settings.get("audio.input_gain", a.microphone.input_gain) or 1.0)
            except (TypeError, ValueError):
                return 1.0

        return LiveAudio(mic=a.microphone.device, speaker=a.speaker.device, volume=volume, mic_gain=gain,
                         half_duplex=self.cfg.live_half_duplex, echo_tail_s=self.cfg.live_echo_tail_ms / 1000,
                         voice_level=self.cfg.live_voice_level, barge_in=BargeInSettings.from_config(self.cfg))

    def provider(self) -> BaseProvider:
        if self._provider is None:
            from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

            self._provider = ChatGPTProvider(self.auth, models=list(self.cfg.models), effort=self.cfg.reasoning_effort,
                                             transport=self._transport)
        return self._provider

    def screen_provider(self) -> BaseProvider:
        """The text model for the screen director: the camera looks' fast models first, on its own connection
        (a slow hand-off answer never holds a card up). A provider given to this service is used as it is."""
        if self._provider_given:
            return self.provider()
        if self._screen_provider is None:
            from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

            models = list(dict.fromkeys([*getattr(self.cfg, "look_models", []), *self.cfg.models]))
            self._screen_provider = ChatGPTProvider(self.auth, models=models, effort=self.cfg.reasoning_effort,
                                                    transport=self._transport)
        return self._screen_provider

    def blocked_reason(self, parent_present: bool = True) -> str | None:
        ctx = self.ctx
        if self._call is not None:
            return "already_running"
        if not parent_present:
            return "parent_not_present"
        if ctx.config.is_offline:
            return "offline"
        if not ctx.privacy.cloud_enabled:
            return "cloud_off"
        if not ctx.privacy.microphone_enabled:
            return "mic_off"
        if not self.auth.signed_in:
            return "not_signed_in"
        if self._audio_factory == self._default_audio and aiortc_error():
            return "unavailable"
        return None

    def status(self) -> dict[str, Any]:
        call = self._call
        reason = self.blocked_reason() if call is None else None
        out: dict[str, Any] = {
            "available": aiortc_error() is None,
            "signed_in": self.auth.signed_in,
            "can_start": call is None and reason is None,
            "blocked": reason,
            "blocked_detail": ERRORS.get(reason or "", None),
            "active": call is not None,
            "voice": self.cfg.live_voice,
            "session_minutes": self.cfg.live_session_minutes,
            "last_call": self.last_call,
        }
        if call is not None:
            out.update(call.snapshot())
        return out

    # ------------------------------------------------------------ control
    async def start_session(self, *, parent_present: bool, carry: Any = None) -> dict[str, Any]:
        """``carry``: what was said with the wake word (littlebot/audio/carry.py), sent before the microphone.
        The caller drops it when this doesn't start; once started, the call drops it when it ends."""
        reason = self.blocked_reason(parent_present)
        if reason:
            return {"ok": False, "error": reason, "detail": ERRORS[reason]}
        if self.ctx.audio is None:
            return {"ok": False, "error": "no_audio", "detail": ERRORS["no_audio"]}
        self._loop = asyncio.get_running_loop()
        call = LiveCall(self)
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT,
                                         on_preempt=lambda _lease, why: self._end_threadsafe(f"stopped ({why})"))
        if lease is None:
            return {"ok": False, "error": "busy", "detail": ERRORS["busy"]}
        call.lease = lease
        call.carry = carry
        self._call = call
        self._task = self._loop.create_task(self._run(call), name="live-voice")
        log.info("live voice: parent started a conversation (limit %.0f min)", self.cfg.live_session_minutes)
        return {"ok": True, **self.status()}

    async def _run(self, call: "LiveCall") -> None:
        try:
            await call.run()
        finally:
            if self._call is call:
                self._call = None
            self.last_call = call.summary()
        done = await call.run_handover()  # the speaker, microphone, lease and state are given back by now
        if done and self.last_call is not None:
            self.last_call["handed_over"] = done

    async def stop_session(self, reason: str = "stopped by parent") -> dict[str, Any]:
        call, task = self._call, self._task
        if call is None or task is None:
            return {"ok": True, **self.status()}
        call.end(reason)
        try:
            await asyncio.wait_for(asyncio.shield(task), 8.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        return {"ok": True, **self.status()}


class LiveCall:
    """One conversation: connect, run, and always clean up (speaker, mic, lease, state)."""

    def __init__(self, svc: LiveVoiceService) -> None:
        self.svc = svc
        self.ctx = svc.ctx
        self.cfg = svc.cfg
        self.lease: Lease | None = None
        self.audio: Any = None
        self.carry: Any = None  # what was said with the wake word (littlebot/audio/carry.py), sent first
        self._feed: Any = None  # live_audio.CarryFeed for it
        self.pc: Any = None
        self.channel: Any = None
        self.phase = "connecting"
        self.detail = "Connecting to ChatGPT..."
        self.error: str | None = None
        self.end_reason: str | None = None
        self._ended = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._captions: collections.deque[dict[str, str]] = collections.deque(maxlen=MAX_CAPTIONS)
        self._assistant_turn = ""
        self._last_check = 0.0
        self._delegations = 0
        self._started = svc._now()
        self._connected_at: float | None = None
        self._last_activity = self._started
        self._heard_user = False
        self._goodbye_at: float | None = None
        self._goodbye_reason: str | None = None  # why the voice is saying goodbye (None: the time limit)
        self._quiet_since: float | None = None
        self._our_state: RobotState | None = None
        self._suspended_audio = False
        self.connect_ms: int | None = None
        self.safety_stop: str | None = None
        self.voice: str | None = None
        self.photos = 0
        self._look_job: _LookJob | None = None
        self._stop_words = ""  # what they are saying, while it is only a stop ("stop talking")
        self._quiet_at: float | None = None  # they said "stop": the voice stays quiet until they talk again
        self._game_hold: _Hold | None = None  # the voice waits for the game (a move made, a game starting)
        self._game_started: tuple[str, float] | None = None  # (id, when) of a game started from their words
        self._video_done: tuple[str, float] | None = None  # ("resume" | "stop", when) for a video paused for the chat
        self._hush_job: _LookJob | None = None  # the voice's sound is held back until this look's answer
        self._hush_release = False  # the voice has that answer: let its sound through at the next pause
        self._hush_quiet_since: float | None = None
        self._hush_held = 0  # frames held back for the current look (for the log)
        self._last_frame_at: float | None = None
        self._heard_partial = ""  # what the person is saying right now (before the final transcript)
        self._user_turns = 0  # final things the person said; a look is shared only within one of them
        self._words_began_at: float | None = None  # their first partial words this turn (None: none came yet)
        self._turn_ended_at = self._started  # their latest final words
        self._voice_spoke_at: float | None = None  # the voice's latest words (a caption in progress)
        self._news: set[asyncio.Future[None]] = set()  # notes waiting for a hand-off (_wait_for_news)
        self._told_blocked: set[str] = set()
        self._rule_noted: set[str] = set()  # RULE_NOTES the voice was given this turn
        self._recent_face: tuple[str, float] | None = None  # (name, when) the latest face played
        self._sent: collections.deque[tuple[float, str, int]] = collections.deque(maxlen=4)  # (when, what, chars)
        self._grown_up_spoke_at: float | None = None  # their latest turn in words (a grown-up, while the child can't talk)
        self._name_fixes = 0  # times the voice was told it called a grown-up by the child's name
        self._pulse_counts: tuple[int, int, int, int] | None = None  # for _pulse
        self._reopens = 0  # times a stopped microphone / speaker was opened again (_check_audio)
        self._reopen_job: asyncio.Future[Any] | None = None
        self._read_check: tuple[int, float] | None = None  # (mic frames read, since when): is sending going on?
        self._voice_face_this_turn = False
        self._seen_types: set[str] = set()
        # A video / activity / sleep the tools queued (RobotToolbox): runs once the voice said its line.
        self._handover: Any = None
        self._handover_at: float | None = None  # when the voice was given the answer that promised it
        self._handover_spoke = False
        self._handover_was_quiet = False
        self._handover_quiet_since: float | None = None
        self._faces: Any = None  # who is in front of the camera (littlebot/vision/faces.py), while warm
        self._director: Any = None  # what the screen shows for what was asked (littlebot/screens/director.py)
        self._voices: Any = None  # who is talking, by voice (littlebot/voices/call.py), when it's on

    # ------------------------------------------------------------ views
    def snapshot(self) -> dict[str, Any]:
        now = self.svc._now()
        limit = self.cfg.live_session_minutes * 60
        base = self._connected_at or self._started
        return {"phase": self.phase, "detail": self.detail, "error": self.error,
                "elapsed_s": int(now - base), "remaining_s": max(0, int(limit - (now - base))),
                "captions": list(self._captions), "connect_ms": self.connect_ms, "voice_in_use": self.voice,
                "photos": self.photos, "heard_user": self._heard_user}

    def summary(self) -> dict[str, Any]:
        base = self._connected_at or self._started
        return {"ended_reason": self.end_reason or "ended", "error": self.error, "safety_stop": self.safety_stop,
                "duration_s": int(self.svc._now() - base), "connected": self._connected_at is not None,
                "ended_at": time.time(), "voice": self.voice, "photos": self.photos, "heard_user": self._heard_user,
                "video": self._video_done[0] if self._video_done else None}  # a paused video: resume | stop

    def end(self, reason: str) -> None:
        if self.end_reason is None:
            self.end_reason = reason
            log.info("live voice: ending (%s)", reason)
        if self.audio is not None:
            self.audio.mute()
        self._ended.set()
        self._poke()

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="live")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _set_state(self, state: RobotState) -> None:
        sm = self.ctx.state
        if sm.state == state:
            self._our_state = state
            return
        if sm.transition(state, "live voice"):
            self._our_state = state

    # ------------------------------------------------------------ run
    async def run(self) -> None:
        try:
            self._warm_camera(True)  # while connecting: the camera keeps a photo ready, ChatGPT's line opens
            self._director = self._make_director()
            self._watch_voices(True)  # who woke LittleBot, by voice, while it connects
            await self._connect()
            if not self._ended.is_set():
                await self._watch()
        except asyncio.CancelledError:
            self.end_reason = self.end_reason or "cancelled"
            raise
        except CallFailed as e:
            self.error = CALL_ERRORS.get(e.status if e.status in CALL_ERRORS else (404 if e.status in (405, 410) else 0),
                                         "Could not connect to ChatGPT live voice. Check the robot's internet and "
                                         "your ChatGPT account, then try again.")
            log.warning("live voice: call failed (HTTP %s)", e.status)
        except ChatGPTAuthError as e:
            self.error = str(e)
        except TimeoutError:
            self.error = "ChatGPT's voice service did not answer in time. Check the robot's internet, then try again."
            log.warning("live voice: connecting timed out")
        except Exception as e:  # noqa: BLE001 - always clean up and report
            name = type(e).__name__
            self.error = ("The speaker or microphone could not be opened." if name == "AudioDeviceError"
                          else f"Live voice failed ({name}).")
            log.exception("live voice failed")
        finally:
            await self._cleanup()

    async def _connect(self) -> None:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: PLC0415

        t0 = self.svc._now()
        audio_svc = self.ctx.audio
        if hasattr(audio_svc, "suspend"):
            await audio_svc.suspend("live voice")
            self._suspended_audio = True
        self._set_state(RobotState.LISTENING)
        self.audio = self.svc._audio_factory()
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.audio.open, loop)
        self._barge_in_setup()
        mic = self.audio
        if self.carry is not None:  # started by the wake word: what they said with it goes first
            from littlebot.chatgpt.live_audio import CarryFeed  # noqa: PLC0415

            mic = self._feed = CarryFeed(self.audio, self.carry)
            self._feed.start(self._spawn)  # from now on: what they say while it connects waits there too
        bearer = await self.svc.auth.bearer()
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pc.addTrack(MicTrack(self._voice_tap(mic)))
        self.channel = self.pc.createDataChannel("oai-events")
        self.channel.on("message", self._on_message)
        self.channel.on("open", self._tell_paused_video)
        self.pc.on("track", self._on_track)
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)  # gathers candidates (no trickle ICE with this endpoint)
        sdp = self.pc.localDescription.sdp
        await self._first_face_check(t0)
        await self._first_voice_check(t0)
        timeout = self.cfg.live_connect_timeout_seconds
        voices = list(dict.fromkeys(v.strip().lower() for v in [self.cfg.live_voice, *self.cfg.live_voice_fallbacks]
                                    if v and v.strip())) or ["sol"]
        refreshed = False
        while True:
            self.voice = voices[0]
            try:
                answer = await asyncio.wait_for(self._post_offer(sdp, bearer, self.voice), timeout)
                break
            except CallFailed as e:
                if e.status == 401 and not refreshed:
                    refreshed = True
                    bearer = await self.svc.auth.on_unauthorized(bearer)
                elif e.status in (400, 422) and len(voices) > 1:
                    log.info("live voice: voice %r not accepted (HTTP %s); trying %r", voices[0], e.status, voices[1])
                    voices.pop(0)
                else:
                    raise
        if self._ended.is_set():
            return
        if not answer.lstrip().startswith("v=0"):
            raise CallFailed(200, "the answer was not an SDP")
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        self._connected_at = self.svc._now()
        self._last_activity = self._connected_at
        self.connect_ms = int((self._connected_at - t0) * 1000)
        self.phase, self.detail = "listening", "Connected. Just talk."
        log.info("live voice: connected in %d ms (voice %s)", self.connect_ms, self.voice)
        self._publish(CALL_STARTED, {})
        if self._director is not None:
            self._spawn(self._director.warm())  # the screen's first decision doesn't wait for a connection

    def _make_director(self) -> Any:
        """The screen director for this call, when there is a screen service and its cards are switched on."""
        screens = getattr(self.ctx, "screens", None)
        try:
            if screens is None or not self.ctx.settings.get("screens.enabled", True):
                return None
            from littlebot.screens import director  # noqa: PLC0415

            return director.ScreenDirector(self.ctx, self.svc.screen_provider, note=self._note, spawn=self._spawn,
                                           effort=getattr(self.cfg, "look_effort", None) or director.FAST_EFFORT,
                                           now=self.svc._now, wait_for_answer=True)
        except Exception:  # noqa: BLE001 - no cards is fine; the call goes on
            log.warning("live voice: the screen director couldn't start", exc_info=True)
            return None

    def _instructions(self) -> str:
        from littlebot.brain.prompts import age_description  # noqa: PLC0415

        age = self.ctx.settings.child_age_months()
        text = VOICE_INSTRUCTIONS.format(name=self.ctx.config.robot_name, child=age_description(age),
                                         faces=_face_menu())
        text += "\n\n" + identity.about(self.ctx.config, debug="voice" if self._tools_on() else "")
        nickname = self._child_name()
        who = nickname or "the little one"
        if age is not None and age < TALKS_FROM_MONTHS:
            if nickname:
                text += (f"\nThe little one's name is {nickname}. Use it when you talk about {nickname}, or answer "
                         f"{nickname}'s babbling (not in every sentence).")
            text += (f"\nWHO IS TALKING: {who[0].upper() + who[1:]} is too young to talk in words. Whoever talks to "
                     f"you in words, including whoever said \"Hey LittleBot\", is one of {who}'s grown-ups, never "
                     f"{who}: never call them {who}, never greet them as {who}, and never talk to them as if they "
                     f"were a baby. Babbling, cooing and baby sounds are {who}. If a grown-up tells you who they are, "
                     f"use their name for the rest of the chat; if you don't know which grown-up is talking, use no "
                     f"name for them.")
        else:
            if nickname:
                text += (f"\nThe little one's name is {nickname}. Say it warmly now and then when you talk to or "
                         f"about {nickname} (not in every sentence).")
            text += (f"\nGrown-ups talk to you too: only call someone {who} when it's clearly the child. If a "
                     f"grown-up tells you who they are, use their name.")
        text += self._barge_in_instructions()
        notes = self._notes(nickname)
        if notes:
            text += "\nWhat the family asked you to remember: " + "; ".join(notes) + "."
        seen = self._faces.intro() if self._faces is not None else ""  # "In front of your camera now: Ali (dad)."
        if seen:
            text += "\n" + seen
        if self._director is not None:
            text += SCREEN_INSTRUCTIONS
        heard = self._voices.intro() if self._voices is not None else ""  # "The grown-up who woke you sounds like Ali"
        if heard:
            text += "\n" + heard
        text += self._browser_instructions()
        text += self._power_instructions()
        now = _dt.datetime.now()
        return f"{text}\nAt the start of this call it is {now:%A}, {now:%H:%M} local time."

    def _browser_instructions(self) -> str:
        """The browser view (littlebot/browser): when to hand "google it" to LittleBot ("" without one)."""
        from littlebot.browser import voice_instructions  # noqa: PLC0415

        return voice_instructions(self.ctx)

    def _power_instructions(self) -> str:
        """The battery (littlebot/power): "how much battery?" is handed to LittleBot ("" on mains power)."""
        from littlebot.power import voice_instructions  # noqa: PLC0415

        return voice_instructions(self.ctx)

    def _notes(self, nickname: str | None) -> list[str]:
        """The family's notes (littlebot/brain/remembered.py); the child's name left out unless it may be shared."""
        from littlebot.brain import remembered  # noqa: PLC0415
        from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

        try:
            kept = remembered.notes(self.ctx.settings)
            nick = clean_child_name((self.ctx.settings.get("profile") or {}).get("nickname")) if not nickname else None
        except Exception:  # noqa: BLE001 - no notes is fine
            return []
        if nick:
            kept = [re.sub(rf"\b{re.escape(nick)}\b", "the little one", n, flags=re.I) for n in kept]
        return kept

    async def _first_face_check(self, t0: float) -> None:
        """Give the first face check a moment (FACES_WAIT_S from the start) so the instructions can say who is here."""
        faces = self._faces
        if faces is None or faces.checked.is_set():
            return
        wait = FACES_WAIT_S - max(0.0, self.svc._now() - t0)
        if wait > 0:
            try:
                await asyncio.wait_for(asyncio.shield(faces.checked.wait()), wait)
            except asyncio.TimeoutError:
                log.info("live voice: the first face check wasn't ready in %.1f s; connecting anyway", FACES_WAIT_S)

    def _nickname(self) -> str | None:
        try:
            from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

            return clean_child_name((self.ctx.settings.get("profile") or {}).get("nickname"))
        except Exception:  # noqa: BLE001
            return None

    def _child_name(self) -> str | None:
        """The child's nickname from the profile, unless the parent switched sharing it off (``ai.share_child_name``)."""
        from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

        try:
            settings = self.ctx.settings
            if not bool(settings.get("ai.share_child_name", self.ctx.config.ai.share_child_name)):
                return None
            profile = settings.get("profile") or {}
            return clean_child_name(profile.get("nickname")) if isinstance(profile, dict) else None
        except Exception:  # noqa: BLE001 - no name is fine
            return None

    async def _post_offer(self, sdp: str, bearer: Any, voice: str) -> str:
        body = {"sdp": sdp, "session": {
            "model": self.cfg.live_model, "instructions": self._instructions(),
            "audio": {"output": {"voice": voice}},
            "delegation": {"type": "client"}}}
        headers = {**self.svc.auth.headers(bearer), "OpenAI-Alpha": "quicksilver=v2",
                   "session-id": str(uuid.uuid4()), "thread-id": str(uuid.uuid4()), "x-session-id": str(uuid.uuid4())}
        try:
            async with httpx.AsyncClient(transport=self.svc._transport, follow_redirects=False,
                                         timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                resp = await client.post(self.svc.call_url, json=body, headers=headers)
        except httpx.HTTPError:
            raise CallFailed(0, "network") from None
        if resp.status_code >= 400:
            raise CallFailed(resp.status_code)
        return resp.text

    # ------------------------------------------------------------ media
    def _on_track(self, track: Any) -> None:
        if track.kind == "audio":
            self._spawn(self._pump(track))

    async def _pump(self, track: Any) -> None:
        from aiortc.mediastreams import MediaStreamError  # noqa: PLC0415

        while not self._ended.is_set():
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            if self._quiet_at is None and not self._hushed(frame):
                self.audio.play_frame(frame)

    # ------------------------------------------------------------ keeping the voice quiet while it looks
    def _hush(self, job: "_LookJob") -> None:
        self._hush_job, self._hush_release, self._hush_quiet_since, self._hush_held = job, False, None, 0

    def _unhush(self, job: "_LookJob") -> None:
        """The voice has ``job``'s answer (or why it couldn't look): its sound may come through again."""
        if self._hush_job is job:
            self._hush_release = True

    def _hushed(self, frame: Any) -> bool:
        """Whether to hold back this frame of the voice's sound (see HUSH_MAX_S)."""
        now = self.svc._now()
        gap = self._last_frame_at is not None and now - self._last_frame_at >= HUSH_PAUSE_S
        self._last_frame_at = now
        job = self._hush_job
        if job is None:
            return False
        try:
            pcm = frame.to_ndarray().astype("float32") / 32768.0
            quiet = float((pcm * pcm).mean()) ** 0.5 < HUSH_QUIET_LEVEL
        except Exception:  # noqa: BLE001 - an odd frame: treat it as sound
            quiet = False
        if quiet:
            self._hush_quiet_since = self._hush_quiet_since if self._hush_quiet_since is not None else now
        else:
            self._hush_quiet_since = None
        paused = gap or (self._hush_quiet_since is not None and now - self._hush_quiet_since >= HUSH_PAUSE_S)
        late = now - job.started >= HUSH_MAX_S
        if (self._hush_release and paused) or late or self._ended.is_set():
            self._hush_job = None
            log.info("live voice: the voice is heard again %d ms after the look began (%d frames held back%s)",
                     int((now - job.started) * 1000), self._hush_held, "; too long" if late else "")
            return False
        if not quiet:
            self._hush_held += 1
        return True

    # ------------------------------------------------------------ events
    def _on_message(self, message: Any) -> None:
        parsed = wire.parse(message)
        if parsed is None:
            return
        etype, event = parsed
        if etype not in self._seen_types and len(self._seen_types) < MAX_SEEN_TYPES:
            self._seen_types.add(etype)  # shows in the journal whether hand-offs arrive at all
            log.info("live voice: first %r event", etype[:60])
        now = self.svc._now()
        self._barge_in_words(event)
        if isinstance(event, wire.AssistantText):
            self._last_activity = now
            if not event.final:  # words it is saying now (a turn's final caption may come a little late)
                self._voice_spoke_at = now
                self._poke()  # a look's answer held for a hand-off may go now: the voice is talking instead
            if event.final:
                text, self._assistant_turn = event.text, ""
                self._caption("robot", text)
                self._check_names(text)
                if self._director is not None:
                    self._director.said(text)
            else:
                self._assistant_turn = (self._assistant_turn + event.text)[-2000:]
                text = self._assistant_turn
            self._check_reply(text, event.final)
            if not self._voice_face_this_turn and not self._ended.is_set():
                action = _voice_face(text)
                if action is not None:
                    self._voice_face_this_turn = True
                    self._play_face(action, "voice")
            if event.final:
                self._voice_face_this_turn = False
        elif isinstance(event, wire.UserText):
            self._last_activity = now
            self._heard_user = True
            if not event.final:
                self._look_if_asked_early(event.text)  # words still coming: look as soon as they clearly ask
            else:
                self._heard_partial = ""
                self._caption("heard", event.text)
                if event.text.strip():  # in words: a grown-up; babbling: the little one (the voice may use her name)
                    self._grown_up_spoke_at = now if len(_plain_words(event.text).split()) >= NAMING_MIN_WORDS \
                        else None
                if event.text.strip():
                    self._user_turns += 1
                    self._turn_ended_at = now
                    self._poke()  # their words are finished: a held look answer may go soon
                    self._voice_turn(event.text)
                if STOP_CALL.match(_plain_words(event.text)):
                    self._asked_to_stop(event.text, first=self._user_turns <= 1)
                    return
                if event.text.strip():
                    self._stop_words = ""  # the turn ended with more than a stop: carry on
                    self._talking_again()
                concern = classify_child_input(event.text)
                if concern is not None:
                    log.warning("live voice: worrying words heard (category=%s)", concern.category)
                    self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "attention",
                                                        "detail": concern.category})
                self._ask_concern(event.text, concern is not None)
                # The person's own words: only when clearly about the face, so everyday talk
                # ("that makes me sad", "give me a hug") doesn't change it.
                handled = self._video_words(event.text) or self._game_words(event.text)
                if not handled:
                    action = _face_request(event.text) if USER_FACE_CUE.search(event.text) \
                        and not self._is_control(event.text) else None  # "start the peekaboo activity": tools
                    handled = action is not None
                    if action is None:
                        self._look_if_asked(event.text)
                    elif self._play_face(action, "user"):
                        self._note(f"LittleBot is showing {action.name.replace('_', ' ')} on its face now.")
                if self._director is not None:  # worrying words get no card: the voice sends them to a grown-up
                    self._director.heard(event.text, handled=handled or concern is not None)
                self._words_began_at = None  # the next words are a new turn
        elif isinstance(event, wire.Delegation):
            self._spawn(self._answer(event))
        elif isinstance(event, wire.Failure):
            # What ChatGPT didn't accept, and what LittleBot had sent just before (never the text itself).
            now = time.monotonic()
            sent = ", ".join(f"{what} ({chars} chars) {now - at:.1f} s before" for at, what, chars in reversed(self._sent))
            log.warning("live voice: error event %s: %s%s; LittleBot last sent: %s", event.code,
                        (event.message or "no message")[:200], f" (param {event.param})" if event.param else "",
                        sent or "nothing yet")
        elif isinstance(event, wire.Closed):
            if event.reason != "close_requested":
                self.error = "ChatGPT ended the call."
            self.end(f"closed by ChatGPT ({event.reason or 'no reason'})")

    # ------------------------------------------------------------ barge-in (live_audio.LiveAudio, BargeInDetector)
    def _barge_in_setup(self) -> None:
        """Hand the call's hooks to the audio: voice recognition (ctx.voices, if there is one) and the call's
        reaction when someone cuts in. Without echo cancelling the audio stays half-duplex and never calls back."""
        setup = getattr(self.audio, "setup_barge_in", None)
        if callable(setup):
            voices = getattr(self.ctx, "voices", None)
            setup(identify=getattr(voices, "identify", None), on_change=self._barge_in_changed)

    def _barge_in_instructions(self) -> str:
        return "\n" + BARGE_IN_INSTRUCTION if getattr(self.audio, "barge_in_on", False) else ""

    def _barge_in_changed(self, what: str, info: dict[str, Any]) -> None:
        """The audio stopped the voice for someone talking over it (or let it carry on after a false alarm)."""
        if what == "interrupted" and not self._ended.is_set():
            self._last_activity = self.svc._now()
            self.phase = "listening"  # the face listens at once, not at the next poll
            self._set_state(RobotState.LISTENING)
            self._publish(Topics.AUDIO_VISEME, {"level": 0.0})

    def _barge_in_words(self, event: Any) -> None:
        """Their words after cutting in: tell the audio (it drops the voice it held), and when all they said was
        "oops, go ahead", tell the voice to carry on where it stopped."""
        heard = getattr(self.audio, "barge_in_heard", None)
        if not isinstance(event, wire.UserText) or not callable(heard):
            return
        if heard(event.text, event.final) == "go ahead":
            log.info("live voice: they didn't mean to interrupt (\"go ahead\"); the voice carries on")
            self._note(BARGE_IN_GO_AHEAD)

    # ------------------------------------------------------------ a video paused for this chat
    def _held_video(self) -> dict[str, Any] | None:
        """The media status while a video is paused for a chat (``MediaController.hold``), else None."""
        media = getattr(self.ctx, "media", None)
        try:
            st = media.status() if media is not None else None
        except Exception:  # noqa: BLE001
            return None
        return st if isinstance(st, dict) and st.get("held") else None

    def _video_title(self, st: dict[str, Any] | None) -> str:
        from littlebot.chatgpt.tools import speakable_title  # noqa: PLC0415

        return speakable_title(((st or {}).get("item") or {}).get("title"))

    def _tell_paused_video(self) -> None:
        """The data channel is open: tell the voice about a video paused for this chat (and what to do)."""
        st = self._held_video()
        if st is not None:
            log.info("live voice: a video is paused for this chat; the voice was told")
            self._note(VIDEO_NOTE.format(title=self._video_title(st)))

    def _video_words(self, text: str) -> bool:
        """Their own words about the video paused for this chat: "keep watching" carries on with it once the
        voice said goodbye; "stop the video" stops it and the chat goes on."""
        want = _video_request(text)
        if want is None or self._held_video() is None:
            return False
        self._spawn(self._video_from_words(want))
        return True

    async def _video_from_words(self, want: str) -> None:
        text = await self._video_action(want, "their words")
        self._note(text)
        if want == "resume":
            self._arm_handover()

    async def _video_hand_off(self, delegation_id: str, question: str) -> bool:
        """A hand-off to carry on with or stop the paused video: done here, without the text model."""
        want = _video_request(question)
        if want is None:
            return False
        done = self._video_done
        if done is not None and done[0] == want and self.svc._now() - done[1] < VIDEO_REPEAT_S:
            self._send_result(delegation_id, "Done already: LittleBot did that a moment ago."
                              + (" Say a very short goodbye." if want == "resume" else ""))
            return True
        if self._held_video() is None:
            return False  # nothing paused for the chat: the tools answer (a new video, a paused one ...)
        log.info("live voice: hand-off received (kind=video)")
        self._send_result(delegation_id, await self._video_action(want, "a hand-off"))
        if want == "resume":
            self._arm_handover()
        return True

    async def _video_action(self, want: str, via: str) -> str:
        """Carry on with (after the call, as a hand-over) or stop the paused video; what to tell the voice."""
        from littlebot.chatgpt.tools import RobotToolbox  # noqa: PLC0415

        title = self._video_title(self._held_video())
        box = RobotToolbox(self.ctx, mode="live", defer=True)
        result = await box.run("resume_video" if want == "resume" else "stop_video", {})
        ok = result.startswith("ok") and (want != "resume" or bool(box.pending))
        log.info("live voice: %s the paused video (from %s)%s", "carrying on with" if want == "resume" else
                 "stopping", via, "" if ok else f" didn't work: {result[:80]}")
        if not ok:
            return f"That didn't work this time ({result[:120]}). Say so briefly."
        self._video_done = (want, self.svc._now())
        if want == "stop":
            return VIDEO_STOPPED.format(title=title)
        self._handover = box  # LiveVoiceService._run carries on with the video once this call ended
        return VIDEO_RESUMING.format(title=title)

    # ------------------------------------------------------------ games on the screen
    def _game_words(self, text: str) -> bool:
        """Their words about LittleBot's on-screen games: start one, stop it, or a move in it ("rock!")."""
        games = getattr(self.ctx, "games", None)
        if games is None:
            return False
        plain = _plain_words(text)
        try:
            active = bool(games.active)
            running = (games.status() or {}).get("id") if active else None
        except Exception:  # noqa: BLE001
            return False
        if active and STOP_GAME.search(plain):
            self._spawn(self._stop_game(games))
            return True
        wanted = _game_wanted(plain)
        if wanted is not None and wanted != running:
            self._hold_voice_for_game()
            self._spawn(self._start_game(games, wanted))
            return True
        if active and games.hear(text):
            self._hold_voice_for_game()  # until the game says what happened
            return True
        return False

    def _hold_voice_for_game(self) -> None:
        hold = self._game_hold = _Hold(self.svc._now())
        self._hush(hold)  # type: ignore[arg-type]  # only .started is used

    def game_news(self, text: str) -> None:
        """What the game on the screen wants said (``littlebot.games.service.GAME_NEWS``)."""
        if self._ended.is_set():
            return
        self._note(f"{text} Say it now, in a few words.")
        self._release_game_hold()

    def screen_news(self, text: str) -> None:
        """What a card on the screen wants said (``SCREEN_NEWS``: "the egg timer is done")."""
        if not self._ended.is_set():
            self._note(f"{' '.join(text.split())[:300]} Tell them now, in a few words.")

    def power_news(self, text: str) -> None:
        """The battery is low (``POWER_NOTE``): the voice mentions it gently, then carries on."""
        if not self._ended.is_set():
            self._note(" ".join(text.split())[:300])

    def power_goodbye(self, text: str) -> None:
        """The battery is empty or too hot: the voice says goodbye, then the call ends (like the time limit)."""
        if self._ended.is_set():
            return
        self._note(" ".join(text.split())[:300])
        if self._goodbye_at is None:
            self._goodbye_at = self.svc._now()
            self._goodbye_reason = "battery"
            self.detail = "The battery needs a rest: saying goodbye."

    def _release_game_hold(self) -> None:
        hold, self._game_hold = self._game_hold, None
        if hold is not None:
            self._unhush(hold)  # type: ignore[arg-type]

    async def _start_game(self, games: Any, game_id: str) -> None:
        info = next((g for g in (games.list() or []) if g.get("id") == game_id), {})
        name = info.get("name") or game_id
        try:
            ok = bool(await games.start(game_id, source="live voice"))
        except Exception:  # noqa: BLE001
            log.warning("live voice: starting the game failed", exc_info=True)
            ok = False
        log.info("live voice: game %s %s from their words", game_id, "started" if ok else "didn't start")
        if ok:
            self._game_started = (game_id, self.svc._now())
            self._note(f"{name} is on your screen now: LittleBot started it. {GAME_NOTES.get(game_id, '')}"
                       "Tell them it's on, in a few words.")
        else:
            why = str((games.status() or {}).get("last_error") or "it couldn't start")[:160]
            self._note(f"They asked for {name}, but it couldn't start: {why} Say so briefly.")
        self._release_game_hold()

    async def _stop_game(self, games: Any) -> None:
        try:
            stopped = games.stop("asked in the voice chat")
            if hasattr(stopped, "__await__"):
                await stopped
        except Exception:  # noqa: BLE001
            log.warning("live voice: stopping the game failed", exc_info=True)
        log.info("live voice: game stopped from their words")
        self._note("The game is stopped and your face is back on the screen. Say so in a few words.")

    def _caption(self, who: str, text: str) -> None:
        text = " ".join(str(text).split())[:300]
        if text:
            self._captions.append({"who": who, "text": text})

    def _check_names(self, text: str) -> None:
        """The voice called the grown-up who just spoke by the child's name ("Hi Amara!"): put it right, quietly.

        Only while the child is too young to talk (then whoever talks in words is a grown-up), only right after
        such words, at most NAME_FIXES times a call."""
        spoke = self._grown_up_spoke_at
        if spoke is None or self._name_fixes >= NAME_FIXES or self.svc._now() - spoke > NAMING_WINDOW_S:
            return
        age = self.ctx.settings.child_age_months()
        if age is None or age >= TALKS_FROM_MONTHS:
            return
        names = [n for n in (self._nickname(), "little one") if n]
        hit = any(_addresses(text, n) for n in names)
        self._ask_naming(text, hit)
        if hit:
            self._fix_name()

    def _ask_naming(self, text: str, hit: bool) -> None:
        """Feature naming (littlebot/decide): the decision model also reads the reply, for the ways of talking to
        the grown-up as the baby the word list misses ("Good job, sweetie!", "Amara, would you like..."). On: its
        sure yes puts it right too; shadow: only compared."""
        from littlebot.decide import get_decider  # noqa: PLC0415

        nick = self._child_name()
        mode = get_decider(self.ctx).mode("naming")
        if mode == "off" or not nick:
            return
        heard = [c["text"] for c in self._captions if c["who"] == "heard"]
        if heard:
            self._spawn(self._check_naming(text, heard[-1], nick, hit, mode))

    async def _check_naming(self, text: str, said: str, nick: str, hit: bool, mode: str) -> None:
        from littlebot.decide import get_decider, uses  # noqa: PLC0415

        decider = get_decider(self.ctx)
        answer = await uses.talked_as_if_baby(self.ctx, grown_up_said=said, reply=text, nick=nick,
                                              robot=self.ctx.config.robot_name)
        if answer is None:
            return
        yes = answer.p >= decider.yes_at()
        theirs = "as_baby" if yes else "as_grown_up" if answer.p <= 1 - decider.yes_at() else "unsure"
        use = mode == "on" and yes and not hit and self._grown_up_spoke_at is not None \
            and self._name_fixes < NAME_FIXES
        decider.compare("naming", mine="as_baby" if hit else "as_grown_up", theirs=theirs, used=use)
        if use:
            self._fix_name()

    def _ask_concern(self, text: str, flagged: bool) -> None:
        """Feature concern (littlebot/decide): the decision model also asks whether someone may be in danger. It
        only ever adds the dashboard's alert (when sure, and the word lists found nothing); it never takes one
        away, and the voice's own safety instructions are unchanged."""
        from littlebot.decide import get_decider  # noqa: PLC0415

        mode = get_decider(self.ctx).mode("concern")
        if mode != "off" and len(_plain_words(text).split()) >= 2:
            self._spawn(self._check_concern(text, flagged, mode))

    async def _check_concern(self, text: str, flagged: bool, mode: str) -> None:
        from littlebot.decide import get_decider, uses  # noqa: PLC0415

        decider = get_decider(self.ctx)
        answer = await uses.concern(self.ctx, said=text)
        if answer is None:
            return
        theirs = "danger" if answer.p >= uses.CONCERN_AT else "ok" if answer.p <= 1 - decider.yes_at() else "unsure"
        use = mode == "on" and theirs == "danger" and not flagged
        decider.compare("concern", mine="danger" if flagged else "ok", theirs=theirs, used=use)
        if use:
            log.warning("live voice: the decision model thinks someone may be in danger (%.2f)", answer.p)
            self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "attention", "detail": "decider"})

    def _fix_name(self) -> None:
        self._name_fixes += 1
        self._grown_up_spoke_at = None  # once for what they said
        nick = self._child_name() or "the little one"
        known = self._faces.grown_up() if self._faces is not None else None
        them = f"a grown-up ({known}, LittleBot's face recognition says)" if known else "a grown-up"
        log.info("live voice: the voice called the grown-up by the child's name; it was told")
        self._note(f"Careful: you just talked to the person speaking as if they were {nick}. They are {them}, not "
                   f"{nick}, who is too young to talk. Don't call them {nick}; carry on naturally, no need to "
                   "apologise at length.")

    def _check_reply(self, text: str, final: bool) -> None:
        """What the voice is saying, checked while it says it."""
        now = self.svc._now()
        if not final and now - self._last_check < 0.1 and not text.rstrip().endswith((".", "!", "?", ",")):
            return
        self._last_check = now
        if not final and len(text) > CHECK_WINDOW:
            # While it speaks: the latest few sentences (the rules match per sentence too); the whole
            # answer is checked once when the turn is done. Keeps each check to a few ms on a Pi.
            cut = text.rfind(". ", 0, len(text) - CHECK_WINDOW)
            text = text[cut + 2:] if cut >= 0 else text[-CHECK_WINDOW:]
        if not text.strip(" .!?,"):
            return
        result = validate_reply(text, max_words=10_000, allow=self.ctx.config.ai.relaxed_rules)
        rules = [r for r in result.reasons if r in BLOCK_RULES or r == "grownup_redirect"]
        if result.blocked and rules and all(r in RULE_NOTES for r in rules):
            for rule in rules:  # once per turn: the same words are checked again as more arrive
                if rule not in self._rule_noted:
                    self._rule_noted.add(rule)
                    log.info("live voice: reply broke a safety rule (%s); the voice was told", rule)
                    self._note(RULE_NOTES[rule])
        elif result.blocked:
            category = rules[0] if rules else "safety"
            self.safety_stop = category
            self.error = "LittleBot stopped the voice because it said something against its safety rules."
            log.warning("live voice: reply broke a safety rule (%s); call ended", category)
            self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "live_safety_stop", "detail": category})
            self.end(f"safety rule ({category})")
        elif result.concern and final:
            self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "attention", "detail": result.concern})
        if final:
            self._rule_noted.clear()

    async def _answer(self, d: wire.Delegation) -> None:
        self._delegations += 1
        if self._director is not None:
            self._director.handoff_started()  # its card waits for this answer, not for "hold on"
        try:
            question = d.prompt.strip()
            if not question:
                heard = [c["text"] for c in self._captions if c["who"] == "heard"]
                question = heard[-1] if heard else ""
            keep = REMEMBER_ASK.match(d.prompt)  # the voice's own words only: "remember I'm her dad" has no names
            if keep:
                log.info("live voice: hand-off received (kind=%s)", keep.group(1).lower())
                self._send_result(d.id, self._remember(keep.group(1).lower(), keep.group(2)))
                return
            info = bool(INFO_WORDS.search(question))  # "what's the weather looking like?": tools, not the camera
            seeing = bool(question) and not info and self._needs_camera(question)
            # "start the peekaboo activity", "show me a video": tools. But a look stays a look whatever it
            # names ("look: what game is the child holding?"): the tools would only ask the camera themselves,
            # two text-model requests later.
            started = self._game_started
            if started is not None and self.svc._now() - started[1] < 20 and \
                    _game_wanted(_plain_words(question)) == started[0]:
                self._send_result(d.id, "Done: that game is on the screen already (LittleBot started it).")
                return
            if await self._video_hand_off(d.id, question):
                return
            who = self._who_asked(question)  # "who is here": face recognition answers, on the robot
            control = not who and self._is_control(question) and not (
                seeing and SEE_FIRST.search(question) and not CONTROL_ACTS.search(question))
            action = None if who or control or info else _face_request(question)
            hinted = not (who or seeing or control or info or action is not None) and self._about_the_look(question)
            kind = ("who" if who else "face" if action is not None else "empty" if not question else "control"
                    if control else "info" if info else "look" if seeing or hinted else "lookup")
            if kind != "empty":
                kind, action = await self._route(question, kind, action)
            log.info("live voice: hand-off received (kind=%s%s)", kind, ", from the person's words" if hinted else "")
            if kind == "who":
                await self._deliver(d.id, self._spawn(self._look_for_voice(question, kind="who")), "who")
                return
            if action is not None:
                # Local and instant: answer first so the voice isn't kept waiting, then play it
                # (not again if the person's or the voice's own words just played it).
                self._send_result(d.id, f"Done: LittleBot is showing {action.name.replace('_', ' ')} on its face now.")
                self._play_face(action, "delegation")
                return
            if kind == "empty":
                self._send_result(d.id, wire.NO_INPUT_RESULT)
                return
            if kind == "look":
                work = self._look_for_voice(question)
            else:
                work = self._ask_robot(sanitize_user_text(question, max_chars=600))
            await self._deliver(d.id, self._spawn(work), kind)
        finally:
            self._delegations -= 1
            self._last_activity = self.svc._now()
            if self._director is not None:
                self._director.handoff_done()

    async def _route(self, question: str, kind: str, action: Any) -> tuple[str, Any]:
        """Feature routing (littlebot/decide): the decision model's pick of what handles a hand-off (a face, a
        camera look, who is here, or the text model with its tools) next to the word lists' ``kind``. On: its pick
        is used when it is sure and the robot can do it; shadow: only compared. Returns (kind, action)."""
        from littlebot.decide import get_decider, uses  # noqa: PLC0415

        decider = get_decider(self.ctx)
        mode = decider.mode("routing")
        if mode == "off":
            return kind, action
        mine = kind if kind in ("face", "look", "who") else "robot"
        heard = [c["text"] for c in self._captions if c["who"] == "heard"]
        last = heard[-1] if heard else ""
        if mode == "shadow":
            self._spawn(self._compare_route(question, last, mine))
            return kind, action
        answer = await uses.route(self.ctx, question, last)
        if answer is None:
            return kind, action
        theirs = str(answer.value)
        new: tuple[str, Any] | None = None
        if theirs != mine and answer.confidence >= decider.min_confidence():
            if theirs == "face":
                face = _face_request(question)
                new = ("face", face) if face is not None else None  # no face named in it: the word lists' way
            elif theirs == "look":
                new = ("look", None)
            elif theirs == "who":
                new = ("who", None) if self._who_asked("who is here") else None  # needs face/voice recognition
            else:
                new = ("lookup", None)
        decider.compare("routing", mine=mine, theirs=theirs, used=new is not None)
        return new if new is not None else (kind, action)

    async def _compare_route(self, question: str, last: str, mine: str) -> None:
        from littlebot.decide import get_decider, uses  # noqa: PLC0415

        answer = await uses.route(self.ctx, question, last)
        if answer is not None:
            get_decider(self.ctx).compare("routing", mine=mine, theirs=str(answer.value))

    def _remember(self, what: str, fact: str) -> str:
        from littlebot.brain import remembered  # noqa: PLC0415

        try:
            if what == "forget":
                gone = remembered.forget(self.ctx.settings, fact)
                return (f"Done: LittleBot forgot {'; '.join(gone)}." if gone
                        else "LittleBot had nothing like that written down.")
            note = remembered.remember(self.ctx.settings, fact)
        except Exception:  # noqa: BLE001
            log.warning("live voice: couldn't keep a note", exc_info=True)
            return wire.FAILED_RESULT
        if note is None:
            return "LittleBot couldn't write that down. Say so briefly."
        log.info("live voice: remembering a note (%d kept)", len(remembered.notes(self.ctx.settings)))
        return f"Saved: LittleBot will remember this in every chat from now on: {note}."

    async def _deliver(self, delegation_id: str, task: "asyncio.Task[str]", kind: str) -> None:
        """Answer a hand-off. The voice waits silently until a hand-off is answered, so: the answer if
        it's ready within HANDOFF_INTERIM_S; else "say you're thinking" right away, and the answer
        follows as a note when it comes (HANDOFF_TIMEOUT_S in all, else a short "didn't work" note)."""
        first = min(HANDOFF_INTERIM_S, HANDOFF_TIMEOUT_S)
        try:
            text = await asyncio.wait_for(asyncio.shield(task), first)
        except asyncio.TimeoutError:
            if task.done() or first >= HANDOFF_TIMEOUT_S:
                task.cancel()
                log.warning("live voice: hand-off took too long (kind=%s); answered anyway", kind)
                self._send_result(delegation_id, wire.FAILED_RESULT)
                return
            log.info("live voice: hand-off not ready after %.0f s (kind=%s); the voice was told to wait", first, kind)
            self._send_result(delegation_id, STILL_WORKING)
            try:
                text = await asyncio.wait_for(task, HANDOFF_TIMEOUT_S - first)
            except asyncio.TimeoutError:
                log.warning("live voice: hand-off took too long (kind=%s); the voice was told", kind)
                self._note(LATE_FAILED)
                return
            except Exception:  # noqa: BLE001
                log.warning("live voice: hand-off failed (kind=%s); the voice was told", kind, exc_info=True)
                self._note(LATE_FAILED)
                return
            self._answer_known(text)
            self._note(LATE_ANSWER.format(answer=text))
            self._arm_handover()
            return
        except Exception:  # noqa: BLE001
            log.warning("live voice: hand-off failed (kind=%s); answered anyway", kind, exc_info=True)
            text = wire.FAILED_RESULT
        self._answer_known(text)
        self._send_result(delegation_id, text)
        self._arm_handover()

    def _answer_known(self, text: str) -> None:
        """What the voice is about to say for a hand-off: the screen's card is made to agree with it."""
        if self._director is not None and text not in (wire.FAILED_RESULT, NOT_SURE, STILL_WORKING, LATE_FAILED):
            self._director.handoff_answer(text)

    def _send_result(self, delegation_id: str, text: str) -> None:
        if not self._ended.is_set() and self.channel is not None and self.channel.readyState == "open":
            for frame in wire.result_frames(delegation_id, text):
                self.channel.send(frame)
            self._sent.append((time.monotonic(), "a hand-off answer", len(text)))

    # ------------------------------------------------------------ tools (littlebot/chatgpt/tools.py)
    def _tools_on(self) -> bool:
        return bool(self.cfg.tools_enabled and getattr(self.svc.provider(), "supports_tools", False))

    def _is_control(self, text: str) -> bool:
        """A hand-off (or the person's words) asking LittleBot to do something with the robot / app."""
        return bool(text and CONTROL_WORDS.search(text)) and self._tools_on()

    async def _ask_robot(self, question: str) -> str:
        """A hand-off for the text model, which may also act on the robot with LittleBot's tools
        (volume, videos, activities, games, sleep, its own status). Without tools: ``_look_up``."""
        if not self._tools_on():
            return await self._look_up(question)
        from littlebot.brain.face_actions import extract_tags  # noqa: PLC0415
        from littlebot.chatgpt.tools import RobotToolbox  # noqa: PLC0415

        box = RobotToolbox(self.ctx, mode="live", defer=True,
                           screen=self._director.show_spec if self._director is not None else None)
        policy = self.ctx.age_policy()
        system = build_system_prompt(robot_name=self.ctx.config.robot_name, age_months=policy.age_months,
                                     max_words=LOOKUP_MAX_WORDS, tools=True,
                                     about=identity.about(self.ctx.config)) + LOOKUP_NOTE + LIVE_TOOLS_NOTE \
            + box.instructions()
        history = []
        for c in [c for c in self._captions if c["who"] != "note"][-6:]:
            history.append({"role": "user" if c["who"] == "heard" else "assistant", "content": c["text"]})
        while history and history[0]["role"] != "user":
            history.pop(0)
        answer, failed = None, False
        try:
            reply = await self.svc.provider().reply(
                history + [{"role": "user", "content": question}], system=system, max_tokens=200, timeout=15.0,
                tools=box.specs(), run_tool=box.run, max_tool_rounds=self.cfg.max_tool_rounds)
            text, _ = extract_tags(reply.text)  # a stray [face: ...] tag must never be read out
            answer = _speakable(text, self.ctx.config.ai.relaxed_rules)
        except (ProviderError, asyncio.TimeoutError):
            failed = True
        if box.calls:
            log.info("live voice: tools used: %s", ",".join(box.calls))
        if not answer and box.done:  # the words didn't pass, but something was done: say that much
            answer = f"Done: {'; '.join(box.done[:3])}. Tell them in a few words."
        if box.pending:
            self._handover = box  # runs once the voice has said this (the newest request wins)
            label = box.pending[-1].label.split(" ", 1)[0]
            answer = (answer or "Done.") + (f" After you say this, this voice chat ends and "
                                            f"{HANDOVER_WHAT.get(label, 'it starts')}, so finish with a short goodbye.")
        return answer or (wire.FAILED_RESULT if failed else NOT_SURE)

    def _arm_handover(self) -> None:
        """The voice was just given the answer that promised a video / activity / sleep: end the call
        once it has said it (``_handover_due``), then ``run_handover`` starts it."""
        box = self._handover
        if box is None or not box.pending or self._handover_at is not None or self._ended.is_set():
            return
        self._handover_at = self.svc._now()
        self._handover_spoke = False
        self._handover_was_quiet = not bool(getattr(self.audio, "speaking", False))
        self._handover_quiet_since = None
        self.detail = "Saying goodbye: what was asked for starts next."
        log.info("live voice: hand-over armed (%s)", box.pending[-1].label.split(" ", 1)[0])

    def _handover_due(self, now: float, speaking: bool) -> bool:
        """The voice has spoken (and gone quiet) since the hand-over was armed, or it has been long enough."""
        assert self._handover_at is not None
        if speaking:
            self._handover_spoke = self._handover_spoke or self._handover_was_quiet
            self._handover_quiet_since = None
        else:
            self._handover_was_quiet = True
            self._handover_quiet_since = self._handover_quiet_since or now
        if self._handover_spoke and self._handover_quiet_since is not None \
                and now - self._handover_quiet_since >= GOODBYE_QUIET_S:
            return True
        return now - self._handover_at >= HANDOVER_MAX_S

    async def run_handover(self) -> list[str]:
        """After the call: start what the voice promised, unless the call ended for another reason.

        Only once the speaker is LittleBot's again (``_speaker_back``), so its first line or sound is heard."""
        box, self._handover = self._handover, None
        promised = self._handover_at is not None and self.end_reason == "time limit reached"
        if box is None or not (self.end_reason == HANDOVER_REASON or promised):
            return []
        await self._speaker_back()
        return await box.run_pending()

    async def _speaker_back(self) -> None:
        """Wait until the speaker plays again before the video / activity / sleep starts.

        The cleanup gave it back (``resume``), but the call's own stream on the same USB device was
        closed only a moment ago: a sound started right then can be cut off after a few ms, and the
        speaker is then taken for unplugged (silence for a few seconds). ``AudioService.speaker_back``
        opens it and plays a little silence until that works."""
        audio = self.ctx.audio
        if self._suspended_audio and getattr(audio, "suspended", None) == "live voice":
            audio.resume("live voice")  # the cleanup didn't get that far
        back = getattr(audio, "speaker_back", None)
        if back is None:
            return
        t0 = self.svc._now()
        try:
            ok = await back()
        except Exception:  # noqa: BLE001 - never keep what was promised from starting
            log.debug("live voice: speaker check failed", exc_info=True)
            return
        log.info("live voice: speaker back for the hand-over in %d ms%s", int((self.svc._now() - t0) * 1000),
                 "" if ok else " (still not playing: starting anyway)")

    def _play_face(self, action: Any, source: str) -> bool:
        """Play a face now, unless the same one just played (words and a hand-off often both ask)."""
        name, now = action.name, self.svc._now()
        recent = self._recent_face
        if recent is not None and recent[0] == name and now - recent[1] < FACE_REUSE_S:
            log.info("live voice: face %s already playing (from=%s)", name, source)
            return False
        self._recent_face = (name, now)
        log.info("live voice: face %s played (from=%s)", name, source)
        self._spawn(self._perform_face(action))
        return True

    async def _perform_face(self, action: Any) -> None:
        try:
            from littlebot.brain.face_actions import perform  # noqa: PLC0415

            await perform(self.ctx, action)
        except Exception:  # noqa: BLE001 - a face that doesn't play must never end the call
            log.debug("face action %s failed", getattr(action, "name", action), exc_info=True)

    def _camera(self) -> Any:
        """The shared camera look-up (it says itself whether the parent allows looking), or None."""
        try:
            from littlebot.chatgpt.look import get_look  # noqa: PLC0415
        except ImportError:
            return None
        return get_look(self.ctx)

    def _who_asked(self, text: str, *, partial: bool = False) -> bool:
        """ "Do you know who I am?" while face recognition can answer it (on, a camera, models, someone added)."""
        faces = self._faces
        voices = self._voices is not None and self._voices.active  # voice recognition can answer it too
        if (faces is None or faces.closed) and not voices or not text:
            return False
        if text.strip().lower().startswith(("who is here", "who:")):  # the voice's own hand-off (FACES_INTRO)
            hit = True
        else:
            hit = any(WHO_SENTENCES.fullmatch(s) for s in _sentences(text, partial=partial))
        try:
            return hit and (voices or faces.faces.blocked_reason() is None)
        except Exception:  # noqa: BLE001
            return False

    def _needs_camera(self, question: str) -> bool:
        """A hand-off answered by looking, or, when looking isn't allowed, by saying why."""
        if not (WANTS_LOOK.search(question) or asks_to_see(question)):
            return False
        if self._card_on_screen() and not (CAMERA_WORDS.search(question) or asks_to_see(question)):
            return False  # "let me see tomorrow", "show it hour by hour": about the card, not the camera
        look = self._camera()
        if look is None:
            return False
        if look.blocked_reason() is None:
            return True
        return bool(LOOK_WORDS.search(question) or asks_to_see(question))  # else the text model answers

    def _card_on_screen(self) -> bool:
        """An info card (the weather, a recipe) is on the screen right now."""
        screens = getattr(self.ctx, "screens", None)
        try:
            return screens is not None and screens.current is not None
        except Exception:  # noqa: BLE001
            return False

    def _about_the_look(self, question: str) -> bool:
        """A hand-off about the look the person's own words just asked for ("what is that thing?")."""
        job = self._shared_look()
        return job is not None and job.by_user and bool(SEEN_HINT.search(question))

    async def _look_for_voice(self, question: str, kind: str = "look") -> str:
        """A seeing hand-off: use the look already taken for this question, else take one.

        Answers as soon as the camera answer's first sentence is ready; any rest follows as a note. If
        a note already gave the voice this answer, it is told so (it says the answer once)."""
        t0 = self.svc._now()
        job = self._shared_look()
        if job is not None:
            job.claimed = True
            self._poke()
            log.info("live voice: look shared (from=delegation; the %s look started %d ms earlier)", job.source,
                     int((t0 - job.started) * 1000))
            why = f"the hand-off came at +{int((t0 - job.started) * 1000)} ms"
        else:
            job = self._start_look(question, source="delegation", kind=kind)
            why = "the hand-off asked for it"
        await asyncio.shield(job.progress.wait())
        if job.blocked:
            self._told_blocked.add(job.blocked)
        text, more = job.for_voice()
        if more:
            self._spawn(self._tell_rest(job))
        if "note" in job.told:
            text, why = f"{text} {ALREADY_TOLD}", f"{why}, after the note"
        job.told.append("hand-off")
        self._log_told(job, "hand-off", f"{why}; {'first sentence' if more else job.outcome}")
        self._unhush(job)
        return text

    def _shared_look(self) -> "_LookJob | None":
        job = self._look_job
        if job is None:
            return None
        if job.ended is None:
            return job  # under way: one photo per question
        fresh = self.svc._now() - job.ended < LOOK_REUSE_S
        return job if job.by_user and not job.claimed and job.turn == self._user_turns and fresh else None

    def _look_if_asked(self, text: str, *, partial: bool = False) -> None:
        """The person asked LittleBot to look: look even if the voice doesn't hand the question over.

        ``partial``: they are still talking ("how many fingers am I ..."): the look starts right away,
        well before the voice hands the question over, and is shared with that hand-off."""
        job, began = self._look_job, self._words_began_at
        # The last look was for words of theirs that had ended, and it was over before these words began:
        # these are a new question. (A hand-off's look may be for these very words: their transcript can lag.)
        since = began - job.ended if job is not None and job.ended is not None and began is not None \
            and job.by_user and self._user_turns >= job.turn else None
        earlier = since is not None and since >= 0
        who = self._who_asked(text, partial=partial)
        if not who and not asks_to_see(text, partial=partial, follow_up=since is not None and 0 <= since < FOLLOW_UP_S):
            return
        if job is not None and not earlier and (job.ended is None or self.svc._now() - job.ended < LOOK_REUSE_S):
            return  # a look for this question is under way or just happened
        if not who and self._camera() is None:
            return
        self._spawn(self._tell_voice(self._start_look(text, source="partial" if partial else "user",
                                                      kind="who" if who else "look")))

    def _look_if_asked_early(self, text: str) -> None:
        """A piece of what the person is saying (``input_transcript.added``): look once the words ask to."""
        prev = self._heard_partial
        if not prev:
            self._words_began_at = self.svc._now()
        self._heard_partial = (text if prev and text.startswith(prev) else prev + text)[-PARTIAL_MAX_CHARS:]
        if not self._ended.is_set():
            if STOP_CALL.match(_plain_words(self._heard_partial)):
                self._stop_words = self._heard_partial
                self._spawn(self._stop_if_nothing_more(self._heard_partial))
            elif _plain_words(self._heard_partial):
                self._talking_again()
            self._look_if_asked(self._heard_partial, partial=True)

    async def _stop_if_nothing_more(self, words: str) -> None:
        """Their words so far are a stop ("stop talking"): stop unless more words follow (STOP_SETTLE_S)."""
        await asyncio.sleep(STOP_SETTLE_S)
        if self._stop_words == words and self._heard_partial in ("", words) and not self._ended.is_set():
            self._asked_to_stop(words, first=self._user_turns == 0, early=True)

    def _asked_to_stop(self, words: str, *, first: bool, early: bool = False) -> None:
        """They said only a stop: "bye" (or "stop" as their first words) ends the call; "stop" / "be quiet" makes
        the voice go quiet at once and wait for them (QUIET_END_S)."""
        when = " (before their words were final)" if early else ""
        if END_CALL.match(_plain_words(words)) or first:
            log.info("live voice: asked to stop%s", when)
            self.end("asked to stop")
            return
        if self._quiet_at is None:
            log.info("live voice: asked to be quiet%s; waiting for them (the call ends after %.0f s of quiet)",
                     when, QUIET_END_S)
            drop = getattr(self.audio, "drop_voice", None)
            if callable(drop):
                drop()
            self._note(QUIET_NOTE)
        self._quiet_at = self.svc._now()

    def _talking_again(self) -> None:
        """They said something more than a stop: the voice may answer again."""
        if self._quiet_at is not None:
            self._quiet_at = None
            log.info("live voice: they talk again; the voice may answer")

    def _start_look(self, question: str, *, source: str, kind: str = "look") -> "_LookJob":
        # a look started while the person is still talking belongs to the turn their words will end
        turn = self._user_turns + (1 if source == "partial" else 0)
        job = self._look_job = _LookJob(started=self.svc._now(), by_user=source != "delegation", turn=turn,
                                        source=source, kind=kind)
        self._hush(job)  # whatever the voice says until the answer is only "one second..."
        job.task = self._spawn(self._run_look(job, question))
        return job

    async def _run_look(self, job: "_LookJob", question: str) -> None:
        try:
            if job.kind == "who" and (self._faces is not None or self._voices is not None):
                job.text, job.ok, job.outcome = await self._who_answer(), True, "who"
            else:
                await self._look(job, question)
        except Exception:  # noqa: BLE001 - a broken look must never leave the voice waiting
            log.warning("live voice: looking failed", exc_info=True)
        finally:
            job.ended = self.svc._now()
            job.ready(job.ended)
            # Where the time went: the photo (warm or new, its age), the robot's count, ChatGPT (model,
            # detail, effort, first sentence, all); "look answer told" lines say when the voice got it.
            steps = ""
            if job.timing:
                from littlebot.chatgpt.look import timing_text  # noqa: PLC0415

                steps = f" | {timing_text(job.timing)}"
            log.info("live voice: look triggered (from=%s) -> %s in %d ms%s", job.source, job.outcome,
                     int((job.ended - job.started) * 1000), steps)

    async def _tell_voice(self, job: "_LookJob") -> None:
        """After a look the person asked for in their own words: tell the voice, unless a hand-off asks for it."""
        await asyncio.shield(job.progress.wait())  # the answer's first sentence, or the whole look
        why = await self._hold_for_hand_off(job)
        if job.claimed or self._ended.is_set():
            return  # the hand-off got the answer
        self._unhush(job)  # the note below gives it the answer (or nothing new to say)
        text, more = job.for_voice()
        if job.ok or more:
            self._note(f"You just looked through your camera. {text} Tell them right away, starting with the answer.")
            job.told.append("note")
            self._log_told(job, "note", why)
            if more:
                self._spawn(self._tell_rest(job))
        elif job.blocked is None or job.blocked not in self._told_blocked:
            if job.blocked:
                self._told_blocked.add(job.blocked)  # say why once per call, not on every "look at me!"
            self._note(f"They asked you to look. {job.text}")
            job.told.append("note")
            self._log_told(job, "note", why)
        self._last_activity = self.svc._now()

    async def _hold_for_hand_off(self, job: "_LookJob") -> str:
        """Wait while a hand-off may still come for ``job`` (see NOTE_GRACE_S); why the wait ended, for the log.

        The voice hands seeing questions over a moment after the person's last words. It doesn't when it
        starts talking without doing so: then the note goes at once. A note never goes while they are
        still talking (unless their words never seem to end)."""
        while not (job.claimed or self._ended.is_set()):
            now = self.svc._now()
            if self._voice_spoke_at is not None and self._voice_spoke_at > max(job.started, self._turn_ended_at):
                return "the voice talked without handing it over"  # so they had stopped talking too
            if self._user_turns >= job.turn:  # their words are finished
                left = self._turn_ended_at + NOTE_GRACE_S - now
                if left <= 0:
                    return f"no hand-off {NOTE_GRACE_S:g} s after their last words"
            else:
                left = job.started + NOTE_TURN_WAIT_S - now
                if left <= 0:
                    return f"their words had not ended after {NOTE_TURN_WAIT_S:g} s"
            await self._wait_for_news(left)
        return "a hand-off came"

    def _poke(self) -> None:
        """Something a held look answer waits for happened: their words ended, the voice spoke, a hand-off came."""
        for waiter in self._news:
            if not waiter.done():
                waiter.set_result(None)
        self._news.clear()

    async def _wait_for_news(self, timeout: float) -> None:
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._news.add(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._news.discard(waiter)

    def _log_told(self, job: "_LookJob", via: str, why: str) -> None:
        """When the voice got a look's answer, and how long it was held after it was ready (never content)."""
        now = self.svc._now()
        ready = job.ready_at if job.ready_at is not None else now
        log.info("live voice: look answer told via %s at +%d ms (ready at +%d ms, held %d ms: %s)", via,
                 int((now - job.started) * 1000), int((ready - job.started) * 1000),
                 int(max(0.0, now - ready) * 1000), why)

    async def _tell_rest(self, job: "_LookJob") -> None:
        """The rest of a camera answer whose first sentence the voice already has (once, if there is any)."""
        await asyncio.shield(job.task)
        if job.rest_told or not job.ok or not job.rest or self._ended.is_set():
            return
        job.rest_told = True
        self._note(f"More of what your camera saw: {job.rest}")

    def _warm_camera(self, on: bool) -> None:
        """During the call the camera keeps a photo ready and ChatGPT's connection stays open (if allowed)."""
        try:
            look = self._camera()
            keep = getattr(look, "keep_warm", None)
            if keep is not None:
                keep(on, owner=OWNER)
        except Exception:  # noqa: BLE001 - getting ready must never stop the call
            log.debug("live voice: keeping the camera warm failed", exc_info=True)
        self._watch_faces(on)

    def _watch_faces(self, on: bool) -> None:
        """Who is in front of the camera during the call (opt-in, recognised on the robot: littlebot/vision/faces.py)."""
        try:
            from littlebot.vision import faces  # noqa: PLC0415

            if on and self._faces is None:
                self._faces = faces.CallFaces(faces.get_faces(self.ctx), tell=self._tell_faces,
                                              hide=faces.hidden_child_name(self.ctx), child=self._nickname(),
                                              now=self.svc._now)
                self._spawn(self._faces.run())
            elif not on and self._faces is not None:
                self._faces.close()
        except Exception:  # noqa: BLE001 - knowing who is there must never stop the call
            log.debug("live voice: face recognition failed", exc_info=True)

    def _tell_faces(self, text: str) -> bool:
        """A note about who is in view, only at a quiet moment (nobody talking, no hand-off or look under way)."""
        if self.channel is None or self.channel.readyState != "open" or self._ended.is_set() or self._delegations \
                or getattr(self.audio, "speaking", False) or self._heard_partial or self._hush_job is not None:
            return False
        self._caption("note", text)
        self._note(text)
        return True

    # ------------------------------------------------------------ who is talking, by voice (littlebot/voices/)
    def _watch_voices(self, on: bool) -> None:
        """Who is talking, recognised by voice on the robot (opt-in): from the wake phrase, then each of their turns."""
        try:
            if on and self._voices is None:
                from littlebot.vision.faces import hidden_child_name  # noqa: PLC0415
                from littlebot.voices.call import CallVoices  # noqa: PLC0415

                voices = getattr(self.ctx, "voices", None)
                if voices is None or voices.blocked_reason() is not None:
                    return  # off (or not set up): nothing is listened to at all
                self._voices = CallVoices(voices, tell=self._tell_faces, spawn=self._spawn, now=self.svc._now,
                                          publish=lambda data: self._publish(Topics.VOICE_SPEAKER, data),
                                          hide=hidden_child_name(self.ctx))
                self._voices.start_from_wake(self.carry)
                self._spawn(self._voices.run())
            elif not on and self._voices is not None:
                self._voices.close()
        except Exception:  # noqa: BLE001 - knowing who is talking must never stop the call
            log.debug("live voice: voice recognition failed", exc_info=True)

    def _voice_tap(self, mic: Any) -> Any:
        """The call's microphone, with a copy for voice recognition (only while it is on)."""
        return self._voices.tap(mic) if self._voices is not None else mic

    async def _first_voice_check(self, t0: float) -> None:
        """Give recognising who said "Hey LittleBot" a moment (VOICES_WAIT_S from the start), for the instructions."""
        voices = self._voices
        if voices is None or voices.checked.is_set():
            return
        wait = VOICES_WAIT_S - max(0.0, self.svc._now() - t0)
        if wait > 0:
            try:
                await asyncio.wait_for(asyncio.shield(voices.checked.wait()), wait)
            except asyncio.TimeoutError:
                log.info("live voice: who woke it wasn't known by voice in %.1f s; connecting anyway", VOICES_WAIT_S)

    def _voice_turn(self, text: str) -> None:
        if self._voices is not None:
            self._voices.turn_ended(text)  # who said it, "I'm Ali"; the sound since their last turn

    async def _who_answer(self) -> str:
        """ "Do you know who I am?": face recognition and voice recognition together."""
        if self._voices is not None:
            return await self._voices.answer_now(self._faces)
        return await self._faces.answer_now()

    async def _look(self, job: "_LookJob", question: str) -> None:
        """One photo, answered by the ChatGPT text model (validated there); never kept.

        Fills in ``job``: the answer, or what to tell the voice about why it couldn't see.
        """
        look = self._camera()
        if look is None:
            job.outcome = "unavailable"
            return
        reason = look.blocked_reason()
        if reason is not None:
            self._caption("note", f"No photo taken: {_look_note(reason)}")
            job.text, job.blocked, job.outcome = CANT_LOOK.get(reason, CANT_SEE), reason, f"blocked:{reason}"
            return
        self._caption("note", "LittleBot is taking a photo to answer (not kept)")
        who = self._faces.for_look() if self._faces is not None else None  # names LittleBot recognised, if any
        result = await _ask_camera(look, question, job, self.svc._now, who=who)
        answer = str(result.get("answer") or "").strip()
        if result.get("ok") and answer:
            self.photos += 1
            job.ok, job.text, job.outcome = True, answer, "ok"
            job.rest = (str(result.get("rest") or "").strip() or None) if job.first is not None else None
            where = ""
            if result.get("source") == "robot":  # sent: the robot was slow, so ChatGPT was asked too
                where = " (counted on the robot)" if result.get("sent") else \
                    " (counted on the robot; the photo was not sent)"
            self._caption("note", f"Camera answer{where}: {answer}")  # the parent sees what the camera said
            return
        code = job.outcome = str(result.get("error") or "no_answer")[:40]
        self._caption("note", f"Looking didn't work: {result.get('detail') or _look_note(code)}")
        if code in CANT_LOOK:  # switched off or broke between the check and the photo
            job.text, job.blocked = CANT_LOOK[code], code
            return
        why = LOOK_FAILED.get(code, "something went wrong")
        job.text = (f"You tried to look, but {why}, so you didn't see anything this time. Say that briefly, "
                    "don't guess what is there, and suggest trying again in a moment.")

    async def _look_up(self, question: str) -> str:
        policy = self.ctx.age_policy()
        system = build_system_prompt(robot_name=self.ctx.config.robot_name, age_months=policy.age_months,
                                     max_words=LOOKUP_MAX_WORDS,
                                     about=identity.about(self.ctx.config, debug="")) + LOOKUP_NOTE
        history = []
        for c in [c for c in self._captions if c["who"] != "note"][-6:]:
            history.append({"role": "user" if c["who"] == "heard" else "assistant", "content": c["text"]})
        while history and history[0]["role"] != "user":
            history.pop(0)
        try:
            reply = await self.svc.provider().reply(history + [{"role": "user", "content": question}],
                                                    system=system, max_tokens=200, timeout=15.0)
        except (ProviderError, asyncio.TimeoutError):
            return wire.FAILED_RESULT
        from littlebot.brain.face_actions import extract_tags  # noqa: PLC0415

        text, _ = extract_tags(reply.text)  # a stray [face: ...] tag must never be read out
        return _speakable(text, self.ctx.config.ai.relaxed_rules) or NOT_SURE

    # ------------------------------------------------------------ watch
    def _pulse(self, now: float) -> None:
        """Every PULSE_EVERY_S of a call, one INFO line of counts: when the robot "stops responding" during a
        chat, it shows which way the sound stopped (nothing heard, all silenced, no voice coming back)."""
        a = self.audio
        last = self._pulse_counts
        counts = (int(getattr(a, "frames_in", 0)), int(getattr(a, "frames_gated", 0)),
                  int(getattr(a, "frames_out", 0)), self._user_turns)
        if last is not None:
            mic, gated, voice, turns = (c - p for c, p in zip(counts, last))
            held = " The voice's sound is held back (a look or game)." if self._hush_job is not None else ""
            log.info("live voice: still on (%s): mic %d frames sent (%d silenced while it spoke, loudest %.3f), "
                     "voice %d frames, %d new turn(s) of theirs, %d hand-off(s) open, %.0f s since anything "
                     "happened.%s", self.phase, mic, gated, float(getattr(a, "peak_in", 0.0)), voice, turns,
                     self._delegations, now - self._last_activity, held)
        self._pulse_counts = counts
        if hasattr(a, "peak_in"):
            a.peak_in = 0.0

    def _check_audio(self, now: float) -> None:
        """A microphone or speaker that stopped mid-call is opened again; when that doesn't help, the call ends.

        Otherwise a call nobody can hear stays on (and the wake word stays off) until its time limit: the
        voice keeps answering its own notes, so it never seems quiet enough to hang up."""
        if self._reopen_job is not None:
            return
        stalled = getattr(self.audio, "stalled", None)
        which = stalled() if callable(stalled) else None
        if which is not None:
            if self._reopens >= AUDIO_REOPENS:
                log.warning("live voice: the %s stopped again; ending the call", which)
                self.error = f"The {which} stopped working, so the chat ended."
                self.end(f"the {which} stopped")
                return
            self._reopens += 1
            log.warning("live voice: the %s stopped (nothing from it for %.0f s); opening the speaker and microphone "
                        "again", which, float(getattr(self.audio, "stall_s", 0.0)))
            self._reopen_job = asyncio.get_running_loop().run_in_executor(None, self.audio.reopen)
            self._reopen_job.add_done_callback(_retrieved)  # also when the call ends before it is done
            self._spawn(self._reopened(which, self._reopen_job))
            return
        frames = getattr(self.audio, "frames_in", None)
        if not isinstance(frames, int) or getattr(self.pc, "connectionState", None) != "connected":
            self._read_check = None
            return
        if self._read_check is None or frames != self._read_check[0]:
            self._read_check = (frames, now)
        elif now - self._read_check[1] >= SENT_STALL_S:
            log.warning("live voice: the microphone gives sound but none of it was sent for %.0f s; ending the call",
                        now - self._read_check[1])
            self.error = "The microphone's sound stopped reaching ChatGPT, so the chat ended."
            self.end("the microphone stopped")

    async def _reopened(self, which: str, job: asyncio.Future[Any]) -> None:
        try:
            await asyncio.shield(job)  # never cancelled: _cleanup waits for it before closing the devices
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("live voice: the speaker and microphone could not be opened again (%s); ending the call", e)
            self.error = f"The {which} stopped working, so the chat ended."
            self.end(f"the {which} stopped")
            return
        finally:
            if self._reopen_job is job and job.done():
                self._reopen_job = None
            self._read_check = None  # the time it took isn't "sound not sent"
        log.info("live voice: the speaker and microphone are open again")

    async def _watch(self) -> None:
        limit = self.cfg.live_session_minutes * 60
        idle = self.cfg.live_idle_hangup_seconds
        last_mouth: tuple[float, str] | None = None
        mouth_sent = 0.0
        next_pulse = self.svc._now()
        while not self._ended.is_set():
            now = self.svc._now()
            if now >= next_pulse:
                next_pulse = now + PULSE_EVERY_S
                self._pulse(now)
            self._check_audio(now)
            if self._ended.is_set():
                break
            speaking = bool(self.audio.speaking)
            if speaking:
                self._last_activity = now
            phase = "speaking" if speaking else ("thinking" if self._delegations else "listening")
            if phase != self.phase:
                self.phase = phase
                self._set_state({"speaking": RobotState.SPEAKING, "thinking": RobotState.THINKING}.get(
                    phase, RobotState.LISTENING))
            mouth = getattr(self.audio, "mouth", None) if speaking else (0.0, "closed")  # (level, shape) heard now
            if mouth is None:  # an audio object without lip-sync: loudness only
                mouth = (min(1.0, float(self.audio.level) * 8.0), "")
            mouth = (round(float(mouth[0]), 2), str(mouth[1]))
            if mouth != last_mouth or (speaking and now - mouth_sent >= 0.15):  # re-send a steady sound
                last_mouth, mouth_sent = mouth, now
                self._publish(Topics.AUDIO_VISEME, {"level": mouth[0], **({"shape": mouth[1]} if mouth[1] else {})})
            state = getattr(self.pc, "connectionState", "new")
            if state in ("failed", "closed"):
                self.error = "The connection to ChatGPT was lost."
                self.end("connection lost")
                break
            base = self._connected_at or self._started
            if self._goodbye_at is None and now - base >= limit:
                self._goodbye_at = now
                self.detail = "Time's up: saying goodbye."
                self._note("The grown-up's time limit for this chat is reached. Say a short, cheerful goodbye now.")
            if self._goodbye_at is not None:
                self._quiet_since = None if speaking else (self._quiet_since or now)
                said_it = now - self._goodbye_at > 1.5 and self._quiet_since is not None \
                    and now - self._quiet_since >= GOODBYE_QUIET_S
                if said_it or now - self._goodbye_at >= GOODBYE_MAX_S:
                    self.end(self._goodbye_reason or "time limit reached")
                    break
            if self._handover_at is not None and self._handover_due(now, speaking):
                self.end(HANDOVER_REASON)  # LiveVoiceService._run then starts the video / activity / sleep
                break
            if self._quiet_at is not None and now - self._quiet_at >= QUIET_END_S:
                self.end(f"quiet for {int(QUIET_END_S)} s after they said stop")
                break
            idle_limit = idle if self._heard_user else max(idle, FIRST_WORDS_MIN_S)
            if idle > 0 and now - self._last_activity >= idle_limit:
                self.end(f"hung up after {int(idle_limit)} s of quiet")
                break
            try:
                await asyncio.wait_for(self._ended.wait(), POLL_S)
            except asyncio.TimeoutError:
                pass

    def _note(self, text: str) -> None:
        if not self._ended.is_set() and self.channel is not None and self.channel.readyState == "open":
            for frame in wire.note_frames(text):
                self.channel.send(frame)
            self._sent.append((time.monotonic(), "a note", len(text)))

    # ------------------------------------------------------------ cleanup
    async def _cleanup(self) -> None:
        self._ended.set()
        if self.audio is not None:
            self.audio.mute()
        self._warm_camera(False)  # the warm photo is dropped now
        if self._director is not None:
            self._director.close()
        self._watch_voices(False)
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.pc is not None:
            try:
                await asyncio.wait_for(self.pc.close(), 5.0)
            except Exception:  # noqa: BLE001
                log.debug("closing the peer connection failed", exc_info=True)
        if self._reopen_job is not None and not self._reopen_job.done():
            await asyncio.wait([self._reopen_job], timeout=10.0)  # opening them again: done before they close
        if self.audio is not None:
            await asyncio.to_thread(self.audio.close)
        if self._suspended_audio:
            self.ctx.audio.resume("live voice")
        self._publish(Topics.AUDIO_VISEME, {"level": 0.0})
        if self.lease is not None:
            self.lease.release()
        current = self.ctx.state.state
        if self._our_state is not None and current in (RobotState.LISTENING, RobotState.SPEAKING, RobotState.THINKING):
            self.ctx.state.release(current, "live voice ended")  # the lease kept anyone else out of these
        self._captions.clear()  # nothing from the conversation is kept
        if self._feed is not None:
            self._feed.close()
        if self.carry is not None:
            self.carry.drop(self.end_reason or "call ended")
        self._assistant_turn = ""
        self.phase = "ended"
        self.end_reason = self.end_reason or ("failed" if self.error else "ended")
        self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "live_ended", "detail": self.end_reason[:60]})
        self._publish(CALL_ENDED, {"reason": self.end_reason[:60]})


_GREETING = r"(?:hi|hello|hey|oh|okay|ok|yes|yeah|well|wow|aw+|sure|thanks|thank you|good morning|good night|" \
            r"great job|good job|well done|of course|you got it|alright|all right)"


def _addresses(text: str, name: str) -> bool:
    """``text`` talks to ``name`` ("Hi Amara!", "Sure, Amara.", "..., Amara?"), not about her ("Amara is asleep")."""
    n = re.escape(name)
    return bool(re.search(rf"(?:^|[.!?]\s+){_GREETING}?[,\s]*(?:there,?\s+|my\s+)?{n}\s*[,!?.]", text, re.I)
                or re.search(rf",\s*(?:my\s+)?{n}\s*[!?.]", text, re.I))


def _face_menu() -> str:
    try:
        from littlebot.brain.face_actions import menu_text  # noqa: PLC0415

        return menu_text()
    except Exception:  # noqa: BLE001
        return "happy, surprised, sleepy, curious, excited, sad, playful, celebrating, wink, spin_eyes, peekaboo"


def _retrieved(job: "asyncio.Future[Any]") -> None:
    """Done callback: a failed job's error is taken (and so never reported as never retrieved)."""
    if not job.cancelled():
        job.exception()


def asks_to_see(text: str, *, partial: bool = False, follow_up: bool = False) -> bool:
    """True when the person's own words ask LittleBot to look ("how many fingers am I holding up?").

    ``partial``: words still being said. A plain "how many fingers" then waits for two more words, so
    "how many fingers does a cat have" never takes a photo, and a short whole-sentence question
    ("what's this?") waits for the end of its sentence; every other phrase is already a clear ask and
    later words can't undo it. ``follow_up``: a look was just answered, so "and now?" asks again."""
    t = _normalised(text)
    for m in USER_WANTS_LOOK.finditer(t):
        if partial and m.group(0) == "how many fingers" and len(t[m.end():].split()) < 2:
            continue  # its "not about a cat" check needs the next words
        return True
    return any(SEE_SENTENCES.fullmatch(s) or (follow_up and FOLLOW_UPS.fullmatch(s))
               for s in _sentences(text, partial=partial))


def _normalised(text: str) -> str:
    t = str(text or "").lower().replace("\u2019", "'")
    t = re.sub(r"\b(what|that|it|who)'s\b|\bwhats\b", lambda m: f"{m.group(1) or 'what'} is", t)
    t = re.sub(r"\bi'?m\b", "i am", t)
    t = re.sub(r"\b(you|we|they)'re\b", r"\1 are", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t)


def _sentences(text: str, *, partial: bool) -> list[str]:
    """The sentences of ``text``, normalised, without greetings ("hey LittleBot, what's this?" -> "what is
    this"). While the words are still coming, the last one only once it has ended (".", "?" or "!")."""
    parts = re.split(r"[.?!]+", str(text or ""))
    if partial and not re.search(r"[.?!][\"')\]]*\s*$", str(text or "")):
        parts = parts[:-1]  # it may still go on: "what is this ..." could become "what is this song"
    return [s for s in (_FILLERS.sub("", _normalised(p).strip()).strip() for p in parts) if s]


async def _ask_camera(look: Any, question: str, job: "_LookJob", now: Callable[[], float],
                      who: str | None = None) -> dict[str, Any]:
    """``look.look`` for the live voice, passing each answer's first sentence on to ``job`` as it arrives
    (and its ``timing`` to ``job.timing``, for the log). ``who``: the names LittleBot recognised."""
    import inspect  # noqa: PLC0415

    def first(text: str) -> None:
        if job.first is None and job.ended is None:
            job.first = text
            job.ready(now())  # a waiting hand-off or note can go ahead with this sentence

    try:
        params = inspect.signature(look.look).parameters
    except (TypeError, ValueError):
        params = {}
    extra = {"context": who} if who and "context" in params else {}
    if "on_first" in params:
        result = await look.look(question, asked_by="live", on_first=first, **extra)
    else:
        result = await look.look(question, asked_by="live", **extra)
    if isinstance(result.get("timing"), dict):
        job.timing = result["timing"]
    return result


def _look_note(code: str) -> str:
    """Why no photo / no answer, for the dashboard captions (never shown to the voice)."""
    if code in LOOK_NOTES:
        return LOOK_NOTES[code]
    try:
        from littlebot.chatgpt.look import ERRORS  # noqa: PLC0415
    except ImportError:
        ERRORS = {}  # noqa: N806
    return ERRORS.get(code) or f"ChatGPT could not look ({code})."


@dataclasses.dataclass(eq=False)
class _LookJob:
    """One look (at most one photo) for one question, shared by the voice's hand-off and the person's words."""

    started: float
    by_user: bool  # started from the person's own words, not a hand-off
    turn: int  # the LiveCall._user_turns its question belongs to
    source: str = "user"  # partial (words still coming) | user (their finished words) | delegation (hand-off)
    kind: str = "look"  # look (a photo for ChatGPT) | who (face recognition on the robot, nothing sent)
    task: asyncio.Task[None] | None = None
    ended: float | None = None
    claimed: bool = False  # a hand-off got its answer, so the voice isn't also sent a note
    ok: bool = False
    text: str = CANT_SEE
    blocked: str | None = None
    outcome: str = "failed"  # for the log: ok, an error code or blocked:<reason>
    first: str | None = None  # the answer's first sentence, passed on while the rest was still coming
    rest: str | None = None  # what the answer says after ``first`` (told as a note, once)
    rest_told: bool = False
    progress: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)  # first sentence ready, or ended
    ready_at: float | None = None  # when ``progress`` was set
    told: list[str] = dataclasses.field(default_factory=list)  # how the voice got the answer: note, hand-off
    timing: dict[str, Any] = dataclasses.field(default_factory=dict)  # CameraLook.look's, for the log

    def ready(self, now: float) -> None:
        if self.ready_at is None:
            self.ready_at = now
        self.progress.set()

    def for_voice(self) -> tuple[str, bool]:
        """What to tell the voice now, and whether more will follow (only a first sentence is ready yet)."""
        if self.ended is None and self.first is not None:
            return self.first, True
        return self.text, False


def _voice_face(text: str) -> Any:
    """A face the voice announces in its own words ("Peekaboo!", "here's my happy face"), else None."""
    for pattern, name in VOICE_FACE_CUES:
        m = pattern.search(text)
        if m is not None:
            try:
                from littlebot.brain.face_actions import FaceAction  # noqa: PLC0415
            except ImportError:
                return None
            return FaceAction(name or _VOICE_EXPRESSIONS[m.group(1).lower()])
    return None


@dataclasses.dataclass(eq=False)
class _Hold:
    """The voice is held back for a game (see LiveCall._hush, which only needs ``started``)."""

    started: float


def _video_request(text: str) -> str | None:
    """"stop" or "resume" when the words ask to stop / carry on with the video ("keep watching"), else None."""
    plain = _plain_words(text)
    if VIDEO_STOP.search(plain):
        return "stop"
    if VIDEO_RESUME.search(plain):
        return "resume"
    return None


def _game_wanted(plain: str) -> str | None:
    """The game id their (plain) words ask for ("let's play rock paper scissors"), else None."""
    for game_id, alias in GAME_ALIASES:
        if re.search(rf"\b{_GAME_VERB}\b(?:.*\b)?(?:{alias})\b", plain):
            return game_id
    return None


def _plain_words(text: str) -> str:
    """Lower case, letters, digits and apostrophes only, single spaces: "Stop, listening!" -> "stop listening"."""
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", str(text).lower().replace("’", "'")).split())


USER_FACE_CUE = re.compile(r"\b(?:face|faces|wink\w*|peek[- ]?a[- ]?boo|peekaboo|spin\w*|eyes|smile|blink\w*)\b", re.I)


def _face_request(text: str) -> Any:
    """A hand-off asking LittleBot to make a face ("face: happy", "make a happy face", "wink"), else None."""
    if not text.strip():
        return None
    try:
        from littlebot.brain.face_actions import parse_request  # noqa: PLC0415
    except ImportError:
        return None
    return parse_request(text)


class MicTrack:
    """aiortc audio track fed by LiveAudio (20 ms, 48 kHz, mono s16). Built lazily (aiortc is optional)."""

    def __new__(cls, audio: Any) -> Any:  # noqa: D102
        from aiortc import MediaStreamTrack  # noqa: PLC0415

        class _Mic(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self._pts = 0

            async def recv(self) -> Any:
                import av  # noqa: PLC0415

                pcm = await audio.read_frame()
                frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = 48000
                frame.pts = self._pts
                frame.time_base = fractions.Fraction(1, 48000)
                self._pts += pcm.shape[-1]
                return frame

        return _Mic()
