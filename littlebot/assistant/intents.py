"""Instant answers: the few things said so often, and so plainly, that waiting for a model would only be slower.

"Stop", "never mind", "louder", "volume 40", "pause", "carry on", "skip", "what time is it?", "thanks", "bye",
"take a picture", "record this", "stop recording", "show me my photos", "send it to my phone", "save that" (the
last 3 minutes of the always-recording buffer), "turn on always recording", "video call Dad", "open WhatsApp",
"answer on WhatsApp", "hang up" (WhatsApp calls, littlebot/skills/calls.py): matched on the whole
utterance (never a word inside a longer question), answered in a few milliseconds with LittleBot's own tools
(``chatgpt.tools.RobotToolbox``, the camera skill), and said straight away. Anything else goes to the brain.
A short, fixed list on purpose: the brain understands everything; this only saves it the trouble.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import re
from collections.abc import Callable
from typing import Any

log = logging.getLogger("littlebot.assistant.intents")

_FILLER = r"(?:hey|please|now|little ?bot|thanks|okay|ok)"
_POLITE = rf"(?:(?:{_FILLER})[\s,.!]*)*"  # speech-to-text writes "Thanks. Bye." as often as "Thanks, bye!"


def _whole(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{_POLITE}(?:{pattern})[\s,.!]*{_POLITE}[.!?]*\s*$", re.IGNORECASE)


# "Can you stop talking to him?" (to a child it was talking with) got "Okay, I'll stop talking now." and it went on
# listening; the owner: "stop talking, which means stop listening as well" (the robot, 2026-11-03)
_STOP_CORE = (r"stop(?: it| that| now| talking(?: to (?:him|her|them|us|me|everyone|everybody|"
              r"the (?:kids?|baby|children|boys?|girls?)))?)?|cancel(?: that)?|never ?mind|forget it|be quiet|quiet|"
              r"shush|shh+|"
              r"hush|shut up|that'?s enough|enough|no(?:thing)? never ?mind|"
              # telling it to stop ends the call, without a word (asked for on the robot, 2026-10-31)
              r"(?:end|stop|close|finish) (?:the |this )?(?:call|chat|conversation|session)|end it|hang up|"
              r"stop listening|you can (?:go|stop)(?: now)?|no more questions|nothing else|that will be all|"
              r"that'?ll be all|that'?s (?:all|it)(?: for now)?|i'?m done|we'?re done|go away|leave me alone|"
              # the end of "stop talking", its "stop" said under the voice's last words (the robot, 2026-10-31:
              # "Talking." was answered with "Looks like you're still there")
              r"(?:no )?talking")
# ("hello" / "hi": a child's word over the voice joined to the turn that followed it)
_STOP_LEAD = (r"hey|hello|hi|no|oh|ok(?:ay)?|alright|all right|just|wait|i said|i told you(?: to)?|"
              r"(?:can|could|would|will) you(?: please)?")
STOP = _whole(rf"(?:(?:{_STOP_LEAD})[\s,.!]+)*(?:{_STOP_CORE})(?:[\s,.!]+(?:and[\s,]+)?(?:{_STOP_CORE}))*|nothing")
# A bare "stop" / "cancel" while WhatsApp is on the screen ends the chat, not the call (as for a video or Android).
STOP_ONLY = _whole(r"stop(?: it| that| now)?|cancel(?: that)?|close(?: it)?|leave(?: it)?|finish|end")
THANKS = _whole(r"thanks?(?: you)?(?: so much| very much| a lot)?|cheers|perfect|great(?:,? thanks?)?|awesome|"
                r"got it|cool|nice|good job|well done|appreciate it")
BYE = _whole(r"(?:good)?bye(?: bye)?|see you(?: later)?|talk (?:to you )?later|good ?night|go to sleep|"
             r"bye little ?bot")
# "Turn the volume down a bit." and "A little louder, please." went to the brain, which looked the volume up
# before changing it, 2-2.3 s to the first word (the robot, 2026-10-31): "a bit", "a little" ... are taken here too.
_ASK = r"(?:(?:can|could|would|will) you (?:please )?|please )?"
_BIT = r"(?:a (?:little )?bit |a little |a touch |a tad |slightly |much |even |bit )?"  # before "louder"
_BIT_AFTER = r"(?: (?:a (?:little )?bit|a little|a touch|a tad|slightly|some|more))?"  # after "turn it up"
_THE_VOLUME = r"(?:the volume|the sound|your volume)"
_TURN = r"turn(?:ed)?"  # "turn the" is often written "Turned the volume down a bit." (the robot, 2026-10-31)
LOUDER = _whole(rf"{_ASK}(?:{_TURN} (?:it |that |yourself |{_THE_VOLUME} )?up{_BIT_AFTER}|"
                rf"{_TURN} up {_THE_VOLUME}{_BIT_AFTER}|(?:raise|increase) {_THE_VOLUME}{_BIT_AFTER}|volume up|"
                rf"speak up|(?:make it |be |speak |talk |go )?{_BIT}louder)(?: please)?")
QUIETER = _whole(rf"{_ASK}(?:{_TURN} (?:it |that |yourself |{_THE_VOLUME} )?down{_BIT_AFTER}|"
                 rf"{_TURN} down {_THE_VOLUME}{_BIT_AFTER}|(?:lower|decrease|reduce) {_THE_VOLUME}{_BIT_AFTER}|"
                 rf"volume down|(?:make it |be |speak |talk |go )?{_BIT}(?:quieter|softer)|not so loud|"
                 rf"(?:(?:it'?s|that'?s|you'?re) )?(?:a (?:little )?bit |a little |way )?too loud)(?: please)?")
# Speech-to-text writes "set the volume to one hundred percent" in words as often as in digits.
_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
          "seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_NUMBER = (rf"\d{{1,3}}|(?:one |a )?hundred|(?:{'|'.join(_TENS)})(?:[- ](?:{'|'.join(_UNITS[1:10])}))?|"
           rf"{'|'.join(reversed(_UNITS))}|max(?:imum)?|full|half")
VOLUME_TO = re.compile(rf"^\s*{_POLITE}{_ASK}(?:(?:set|turn|put|change|make) (?:the |your )?)?volume "
                       rf"(?:to |at |on )?(?P<n>{_NUMBER})(?: ?(?:percent|per cent|%))?[\s,.!]*{_POLITE}[.!?]*\s*$",
                       re.I)


def _percent(said: str) -> int | None:
    """``said`` (a VOLUME_TO number: "40", "seventy-five", "one hundred", "max") as a percent, or None."""
    w = said.lower().replace("-", " ").split()
    if not w:
        return None
    if w[0].isdigit():
        return min(100, int(w[0]))
    if w[-1] in ("hundred", "max", "maximum", "full"):
        return 100
    if w == ["half"]:
        return 50
    if w[0] in _TENS:
        return 20 + 10 * _TENS.index(w[0]) + (_UNITS.index(w[1]) if len(w) > 1 else 0)
    return _UNITS.index(w[0]) if w[0] in _UNITS else None
PAUSE = _whole(r"pause(?: (?:it|the video|the music|that))?|hold on|wait")
RESUME = _whole(r"(?:resume|unpause|continue|carry on|keep (?:playing|going|watching)|play)(?: (?:it|the video|"
                r"the music))?|go back to (?:the )?(?:video|music)")
SKIP = _whole(r"(?:skip|next)(?: (?:song|track|video|one|it))?|play the next (?:song|one|video)")
STOP_MEDIA = _whole(r"stop (?:the )?(?:video|music|song|playing)|turn (?:the )?(?:video|music) off")
TIME = _whole(r"what(?:'s| is) the time|what time is it(?: now)?|(?:tell me )?the time")

# Photos and videos (littlebot/skills/camera.py). Never a bare "cheese" or "smile": said to a child, not to LittleBot.
_SHOT = r"(?:photo|picture|pic|selfie|snapshot|snap|photograph)"
_OF = (r"(?: of (?:me|us|this|that|him|her|them|it|everyone|everybody|the (?:baby|kids?|children|family|dog|cat|"
       r"room)|my (?:baby|kids?|son|daughter|family)))?")
_NOW = r"(?: (?:now|please|right now))?"
PHOTO = _whole(rf"{_ASK}(?:take|snap|get|make|grab|shoot) (?:a |an |the |one |another |my |our )?(?:quick )?{_SHOT}"
               rf"{_OF}{_NOW}|(?:photo|picture|selfie) time")
RECORD = _whole(rf"{_ASK}(?:(?:start|begin) (?:recording|filming)|record|film)(?: (?:this|that|me|us|him|her|them|"
                rf"it|everyone|everybody|a video|video|the (?:baby|kids?|children|family)))?{_NOW}|"
                rf"{_ASK}(?:take|make|shoot|start|record|film) (?:a |an )?(?:video|movie|clip|recording){_OF}{_NOW}")
STOP_RECORD = _whole(r"(?:stop|end|finish) (?:the )?(?:recording|filming|video recording)(?: (?:now|please))?|"
                     r"(?:stop|end|finish) (?:recording|filming|videoing)(?: (?:this|that|it|now|please))?")
# Said while recording (and no video or music on): about the recording. ("never mind", "cancel" still end the chat.)
STOP_WHILE_RECORDING = _whole(r"stop(?: (?:it|that|now|the video|the camera))?|cut|that'?s a wrap|"
                              r"(?:i'?m|we'?re) done|that'?s (?:it|all|enough)|done|finish(?:ed)?")
_SEE = r"(?:show|let me see|i want to see|can i see|let'?s see|let'?s look at|open|see)"
_MINE = r"(?:my |our |the |your |all (?:the |my |our )?)?(?:last |latest |new |newest |recent )?"
_ALBUM = r"(?:photos?|pictures?|pics|videos?|recordings?|gallery|photo gallery|photo album)"
SHOW_PHOTOS = _whole(rf"{_ASK}{_SEE}(?: me| us)? {_MINE}{_ALBUM}(?: (?:you|we|i) took)?|(?:open|show) (?:the )?"
                     rf"(?:photo )?gallery")
# Whose phone ("my", "our", "my wife's", "Ali's", "everyone's"): littlebot/capture/recipients.py reads it.
_REL = r"(?:wife|husband|hubby|wifey|spouse|partner|other half|better half|mum|mom|mommy|mummy|dad|daddy)"
_WHOSE = rf"(?:my|our|(?:my |our )?{_REL}'?s|[^\W\d_][\w-]{{1,19}}'s|everyone'?s|everybody'?s|all (?:of )?our)"
_PHONE = rf"(?P<to1>{_WHOSE}) (?:phones?|iphones?|cell ?phones?|mobiles?|androids?)"
_THING = r"(?:it|that|this|them|(?:the|that|this|my) (?:last |latest )?(?:photo|picture|pic|video|selfie))"
_MEDIA = r"(?:the|that|this|my) (?:last |latest )?(?:photo|picture|pic|video|selfie)"
_TO = rf"(?P<to2>me|my {_REL}|[^\W\d_][\w-]{{1,19}}|everyone|everybody|the family)"
SEND_PHOTO = _whole(rf"{_ASK}(?:send|share|airdrop|put) (?:{_THING} )?(?:to|on|onto) {_PHONE}|"
                    rf"{_ASK}(?:send|share|airdrop) (?:me )?{_MEDIA}|"
                    rf"{_ASK}(?:send|share|airdrop) {_MEDIA} to {_TO}")

# "Always recording" (littlebot/capture/buffer.py): the last 3 minutes are kept in RAM; these save them, or part.
# Never a question ("did you save that?") and never "save that <something>" (a note, a song): those go to the brain.
_SPAN_UNIT = r"(?:seconds?|secs?|minutes?|mins?)"
_SPAN = rf"(?:the )?(?:last|past|previous) (?:(?P<n>\d{{1,3}}|half a|a|one|two|three|four|five|ten|fifteen|twenty|" \
        rf"thirty|forty[- ]five|sixty|ninety|few|couple(?: of)?) )?(?P<unit>{_SPAN_UNIT})"
_CLIP = r"(?:clip|video|recording|moment|bit)"
SAVE_CLIP = _whole(rf"{_ASK}(?:save|clip) (?:that|this|it)(?: {_CLIP})?{_NOW}|"
                   rf"{_ASK}(?:save|clip|keep) (?:a {_CLIP} of )?{_SPAN}(?: of (?:video|that|this))?{_NOW}|"
                   rf"{_ASK}(?:save|keep) (?:the|a|that|this) clip{_NOW}")
_ALWAYS = r"(?:the )?always[- ]recording|(?:the )?(?:rolling|video) buffer"
ALWAYS_ON = _whole(rf"{_ASK}(?:turn|switch) on (?:{_ALWAYS})|{_ASK}(?:turn|switch) (?:{_ALWAYS}) (?:back )?on|"
                   rf"{_ASK}(?:start|enable|resume) (?:{_ALWAYS})|always[- ]recording on")
ALWAYS_OFF = _whole(rf"{_ASK}(?:turn|switch) off (?:{_ALWAYS})|{_ASK}(?:turn|switch) (?:{_ALWAYS}) off|"
                    rf"{_ASK}(?:stop|disable|end) (?:{_ALWAYS})|always[- ]recording off")
_SPAN_WORDS = {"half a": 0.5, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15,
               "twenty": 20, "thirty": 30, "forty five": 45, "sixty": 60, "ninety": 90}


def _span_seconds(m: re.Match[str]) -> float | None:
    """"the last minute" -> 60, "the last 30 seconds" -> 30; None for "save that" and "the last few minutes"
    (everything the buffer holds)."""
    try:
        unit = m.group("unit")
    except IndexError:
        return None
    if not unit:
        return None
    said = (m.group("n") or "a").lower().replace("-", " ")
    n = float(said) if said.isdigit() else _SPAN_WORDS.get(said)
    if n is None:
        return None
    return max(5.0, n * (60.0 if unit.lower().startswith("m") else 1.0))


# WhatsApp calls (littlebot/skills/calls.py, littlebot/calls). "Call Dad" is a call only when Dad is on the parent's
# list of people to call (``call_names``); with WhatsApp named ("call Sam on WhatsApp") it always is, and someone not
# on the list hears who can be called. While WhatsApp is on the screen, "hang up" / "end the call" / "answer" are
# about it.
_WA = r"whats ?app"
_NOT_NAME = rf"(?!(?:on|with|using|via|through|please|now|a|an|the|it|me|us|this|that|{_WA})\b)"
_WORD = rf"{_NOT_NAME}[^\W\d_][\w'-]{{0,19}}"
_PERSON = rf"(?:(?:my|our) )?{_WORD}(?: {_WORD}){{0,2}}"
_ON_WA = rf"(?: (?:on|with|using|via|through) {_WA})"
_CALL_KIND = r"(?:(?P<kind>video|voice|audio|phone)[- ]?)?"
CALL = _whole(rf"{_ASK}{_CALL_KIND}(?:call|ring|phone|dial) (?P<who>{_PERSON})(?P<wa>{_ON_WA})?{_NOW}|"
              rf"{_ASK}(?P<wa2>{_WA}) (?:(?P<kind2>video|voice|audio) )?(?:call )?(?P<who2>{_PERSON}){_NOW}|"
              rf"{_ASK}(?:make|start|place) a (?:(?P<kind3>video|voice|audio|phone) )?call (?:to|with) "
              rf"(?P<who3>{_PERSON})(?P<wa3>{_ON_WA})?{_NOW}")
OPEN_WHATSAPP = _whole(rf"{_ASK}(?:open|start|show(?: me)?|launch|bring up|go to)(?: up)? {_WA}{_NOW}|{_WA}")
ANSWER_WHATSAPP = _whole(rf"{_ASK}(?:answer|pick up|take|accept|get)(?: (?:it|that|the call|the phone|the video call|"
                         rf"my call))?{_ON_WA}{_NOW}|{_ASK}(?:answer|pick up) {_WA}{_NOW}")
CLOSE_WHATSAPP = _whole(rf"{_ASK}(?:close|quit|exit|shut|leave|stop|turn off)(?: down)? {_WA}{_NOW}")
# ... only while WhatsApp is on the screen:
ANSWER_CALL = _whole(rf"{_ASK}(?:answer|pick up|accept|take)(?: (?:it|that|the call|the phone|the video call))?{_NOW}")
HANG_UP = _whole(rf"{_ASK}(?:hang up|end|stop|finish|leave|close|cancel)(?: (?:it|now|the call|the video call|"
                 rf"this call|that call|call|the phone))?{_NOW}")


def _call_intent(t: str, call_names: Callable[[str], bool]) -> Intent | None:
    """"Video call Dad", "call Mum on WhatsApp", "WhatsApp Dad": who and which kind; None when it isn't one."""
    m = CALL.match(t)
    if not m:
        return None
    who = m.group("who") or m.group("who2") or m.group("who3") or ""
    kind = (m.group("kind") or m.group("kind2") or m.group("kind3") or "video").lower()
    named = bool(m.group("wa") or m.group("wa2") or m.group("wa3"))
    try:
        known = bool(call_names(who))
    except Exception:  # noqa: BLE001
        known = False
    if not known and not named:
        return None  # "call me a taxi", "call the doctor": the brain answers
    return Intent("call", args={"name": who, "kind": "video" if kind == "video" else "voice"})


def _kind(t: str) -> str | None:
    """"photo" / "video" when the words say which."""
    low = t.lower()
    if re.search(r"\b(?:video|videos|recording|recordings|movie|clip)\b", low):
        return "video"
    if re.search(r"\b(?:photos?|pictures?|pics?|selfie|snapshot)\b", low):
        return "photo"
    return None

MIN_VOICE_VOLUME = 0.15  # by voice the volume never goes below this (the dashboard can still go lower)


def _speaker_volume(box: Any) -> float | None:
    try:
        return float(box._speaker_volume())  # noqa: SLF001 - the toolbox's own reading of the volume
    except Exception:  # noqa: BLE001
        return None


ACK_THANKS = ("You're welcome!", "Anytime!", "Happy to help!", "My pleasure!")
ACK_BYE = ("Bye for now!", "See you!", "Talk soon!")
ACK_OK = ("Okay.", "Sure.", "Done.")


@dataclasses.dataclass
class Intent:
    name: str  # stop | thanks | bye | volume | pause | resume | skip | stop_media | time | photo | record |
    #            stop_record | show_photos | send_photo | save_clip | always_recording | call | whatsapp | hang_up
    say: str = ""  # what to say (may be "")
    end: bool = False  # the session ends after saying it
    args: dict[str, Any] = dataclasses.field(default_factory=dict)


# Dark mode (setting display.theme): "dark mode", "switch to night mode", "turn off dark mode", "light mode please".
_SWITCH = r"(?:(?:switch|turn|go|change|put|set)(?: it| the screen| your screen| yourself)?(?: on| over| back)?(?: (?:to|into|in))? )"
_THEME_WORD = r"(?: mode| theme)"
DARK_MODE = _whole(rf"{_SWITCH}?(?:the )?(?:dark|night){_THEME_WORD}(?: on)?|(?:make|turn) (?:the |your )?screen dark|"
                   rf"(?:turn|switch) (?:off|out) (?:the )?(?:light|day){_THEME_WORD}")
LIGHT_MODE = _whole(rf"{_SWITCH}?(?:the )?(?:light|day|normal){_THEME_WORD}(?: on)?|"
                    rf"(?:turn|switch) (?:off|out) (?:the )?(?:dark|night){_THEME_WORD}|"
                    rf"(?:no|stop|exit|leave) (?:the )?(?:dark|night){_THEME_WORD}")


def match(text: str, *, media_on: bool = False, recording_on: bool = False, calls_on: bool = False,
          call_names: Callable[[str], bool] | None = None) -> Intent | None:
    """The instant intent ``text`` is, or None (the brain answers). ``media_on``: a video or music is playing or
    paused for this chat, so "pause", "play", "skip" are about it. ``recording_on``: LittleBot is recording a video,
    so "stop" / "cut" / "that's it" stop the recording. ``call_names(name)``: is that someone on the list of people
    to call (None: there are no WhatsApp calls on this robot, so none of their phrases match). ``calls_on``:
    WhatsApp is on the screen, so "hang up", "end the call", "answer" are about it."""
    t = " ".join(str(text or "").replace("’", "'").split())
    if not t or len(t) > 60:
        return None
    if call_names is not None:
        if CLOSE_WHATSAPP.match(t) or (calls_on and HANG_UP.match(t) and not STOP_ONLY.match(t)):
            return Intent("hang_up", end=True)
        if ANSWER_WHATSAPP.match(t) or (calls_on and ANSWER_CALL.match(t)):
            return Intent("whatsapp", args={"answer": True})
        if OPEN_WHATSAPP.match(t):
            return Intent("whatsapp", args={"answer": False})
        call = _call_intent(t, call_names)
        if call is not None:
            return call
    if recording_on and not media_on and STOP_WHILE_RECORDING.match(t):
        return Intent("stop_record", end=True)
    if STOP_RECORD.match(t):
        return Intent("stop_record", end=True)
    if STOP_MEDIA.match(t) and media_on:
        return Intent("stop_media", random.choice(ACK_OK))
    if PHOTO.match(t):
        return Intent("photo")  # the chat ends when the photo starts (a take-over), not here
    if RECORD.match(t):
        return Intent("record")
    if SHOW_PHOTOS.match(t):
        return Intent("show_photos", args={"kind": _kind(t)})
    m = SEND_PHOTO.match(t)
    if m:
        return Intent("send_photo", args={"kind": _kind(t), "to": m.group("to1") or m.group("to2") or None})
    m = SAVE_CLIP.match(t)
    if m:
        return Intent("save_clip", args={"seconds": _span_seconds(m)})
    if ALWAYS_ON.match(t):
        return Intent("always_recording", args={"on": True})
    if ALWAYS_OFF.match(t):
        return Intent("always_recording", args={"on": False})
    if STOP.match(t):
        return Intent("stop", "", end=True)
    if BYE.match(t):
        return Intent("bye", random.choice(ACK_BYE), end=True)
    if THANKS.match(t):
        return Intent("thanks", random.choice(ACK_THANKS), end=True)
    if LOUDER.match(t):
        return Intent("volume", "Louder.", args={"step": 1})
    if QUIETER.match(t):
        return Intent("volume", "Quieter.", args={"step": -1})
    m = VOLUME_TO.match(t)
    percent = _percent(m.group("n")) if m else None
    if percent is not None:
        return Intent("volume", f"Volume {percent}.", args={"percent": percent})
    if DARK_MODE.match(t):
        return Intent("theme", "Dark mode.", args={"theme": "dark"})
    if LIGHT_MODE.match(t):
        return Intent("theme", "Light mode.", args={"theme": "light"})
    if media_on:
        if PAUSE.match(t):
            return Intent("pause", "", end=True)
        if RESUME.match(t):
            return Intent("resume", "", end=True)
        if SKIP.match(t):
            return Intent("skip", "", end=True)
    if TIME.match(t):
        return Intent("time")
    return None


async def run(intent: Intent, box: Any, ctx: Any) -> str:
    """Do it with LittleBot's toolbox; returns what to say (``intent.say`` unless the action says otherwise)."""
    name = intent.name
    if name == "theme":
        result = await box.run("set_screen_theme", {"theme": intent.args.get("theme")})
        return intent.say if not str(result).startswith("error") else "I can't change the screen right now."
    if name == "volume":
        current = _speaker_volume(box)
        if intent.args.get("step", 0) < 0 and current is not None and current <= MIN_VOICE_VOLUME + 1e-6:
            return "That's as quiet as I go."  # "softer" said again and again must never make it silent
        if "percent" in intent.args and intent.args["percent"] < MIN_VOICE_VOLUME * 100:
            intent.args["percent"] = int(MIN_VOICE_VOLUME * 100)  # "volume 0" by voice: the quietest it still hears
            intent.say = f"Volume {intent.args['percent']}, as quiet as I go."
        if "percent" in intent.args:
            res = await box.run("set_volume", {"percent": intent.args["percent"]})
        else:
            res = await box.run("volume_up" if intent.args.get("step", 1) > 0 else "volume_down", {})
        if res.startswith("error"):
            return "I can't change the volume right now."
        # the toolbox says "speaker volume is now 70% (the most allowed)" at the ceiling (audio.speaker.max_volume)
        capped = "the most allowed" in res or "maximum" in res.lower()
        level = re.search(r"speaker volume is now (\d+)%", res)
        now = int(level.group(1)) if level else None
        if capped and intent.args.get("step", 0) > 0:
            was_top = current is not None and now is not None and round(current * 100) >= now
            return "That's as loud as I go." if was_top else "Louder. That's as loud as I go."
        if now is not None and intent.args.get("percent", 0) > now:
            return f"Volume {now}, as loud as I go."  # "volume 100": it never says a level it didn't set
        return intent.say
    if name == "time":
        from littlebot.info.clock import now_info  # noqa: PLC0415

        tz = getattr(getattr(ctx.config, "info", None), "timezone", "") or None
        try:
            info = now_info(tz)
        except ValueError:
            info = now_info(None)
        return f"It's {info['time_12h'].replace(' am', ' a.m.').replace(' pm', ' p.m.')}."
    if name == "pause":
        await box.run("pause_video", {})
        return intent.say
    if name == "resume":
        await box.run("resume_video", {})
        return intent.say
    if name == "stop_media":
        await box.run("stop_video", {})
        return intent.say
    if name == "skip":
        media = getattr(ctx, "media", None)
        if media is not None:
            try:
                await media.skip()
            except Exception:  # noqa: BLE001 - nothing to skip to
                return "There's nothing to skip to."
        return intent.say
    if name in CAMERA:
        return await _camera(intent, box, ctx)
    if name in CALLS:
        return await _calls_run(intent, box, ctx)
    return intent.say


CAMERA = {"photo": "photo", "record": "record", "stop_record": "stop_record", "show_photos": "show",
          "send_photo": "send", "save_clip": "save_clip", "always_recording": "buffer_on"}


CALLS = ("call", "whatsapp", "hang_up")


def calls_skill(ctx: Any) -> Any:
    """The WhatsApp calls skill while calls are offered (they exist and the parent's switch is on), else None."""
    hub = getattr(ctx, "skills", None)
    skill = hub.get("calls") if hub is not None and hasattr(hub, "get") else None
    try:
        return skill if skill is not None and skill.available() else None
    except Exception:  # noqa: BLE001
        return None


def calls_flags(ctx: Any) -> dict[str, Any]:
    """``match``'s ``calls_on`` and ``call_names`` for this robot right now."""
    skill = calls_skill(ctx)
    if skill is None:
        return {}
    return {"calls_on": bool(skill.active), "call_names": skill.knows}


async def _calls_run(intent: Intent, box: Any, ctx: Any) -> str:
    """A WhatsApp phrase through the calls skill."""
    skill = calls_skill(ctx)
    if skill is None:
        return "I can't make WhatsApp calls."
    try:
        if intent.name == "hang_up":
            words = await skill.instant("hang_up")
        elif intent.name == "whatsapp":
            words = await skill.instant("answer" if intent.args.get("answer") else "open")
        else:
            words = await skill.instant("call", name=str(intent.args.get("name") or ""),
                                        kind=str(intent.args.get("kind") or "video"))
    except Exception:  # noqa: BLE001 - the skill's bug: say sorry rather than break the chat
        log.warning("assistant: the WhatsApp phrase failed", exc_info=True)
        return "Sorry, WhatsApp didn't work."
    _claim(box, ctx)
    return words


def _cam(ctx: Any) -> Any:
    hub = getattr(ctx, "skills", None)
    return hub.get("camera") if hub is not None and hasattr(hub, "get") else None


def _claim(box: Any, ctx: Any) -> None:
    """What the camera queued to start after the words (the photo, the recording) goes to ``box``'s after-the-turn
    list, as when the brain calls the tool."""
    queue = getattr(box, "_queue", None)
    if callable(queue):
        from littlebot.skills import takeover  # noqa: PLC0415

        for p in takeover.claim(ctx):
            queue(p.label, p.run, takeover=bool(getattr(p, "takeover", True)))


async def _camera(intent: Intent, box: Any, ctx: Any) -> str:
    """A photo / video phrase through the camera skill."""
    cam = _cam(ctx)
    if cam is None:
        return "I can't take photos or videos."
    try:
        if intent.name == "send_photo":
            words = await cam.instant("send", kind=intent.args.get("kind"), to=intent.args.get("to"))
        elif intent.name == "save_clip":
            words = await cam.instant("save_clip", seconds=intent.args.get("seconds"))
        elif intent.name == "always_recording":
            words = await cam.instant("buffer_on" if intent.args.get("on") else "buffer_off")
        else:
            words = await cam.instant(CAMERA[intent.name], kind=intent.args.get("kind"))
    except Exception:  # noqa: BLE001 - the skill's bug: say sorry rather than break the chat
        log.warning("assistant: the camera phrase failed", exc_info=True)
        return "Sorry, the camera didn't work."
    _claim(box, ctx)
    return words


def camera_asking(ctx: Any) -> bool:
    """The camera just asked whose phone to send a photo to: the next words may be the answer."""
    cam = _cam(ctx)
    try:
        return cam is not None and bool(cam.asking())
    except Exception:  # noqa: BLE001
        return False


async def camera_reply(text: str, box: Any, ctx: Any) -> str | None:
    """Their answer to the camera's "whose phone should I send it to?" ("Ali's", "mine", "my wife's"): sent, and
    the words to say; None when ``text`` isn't such an answer (the brain answers)."""
    if not camera_asking(ctx):
        return None
    try:
        words = await _cam(ctx).reply(text)
    except Exception:  # noqa: BLE001
        log.warning("assistant: the answer to whose phone failed", exc_info=True)
        return None
    if words is not None:
        _claim(box, ctx)
    return words


__all__ = ["Intent", "calls_flags", "calls_skill", "camera_asking", "camera_reply", "match", "run"]
