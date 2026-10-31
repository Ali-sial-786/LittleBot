"""Instant answers: the few things said so often, and so plainly, that waiting for a model would only be slower.

"Stop", "never mind", "louder", "volume 40", "pause", "carry on", "skip", "what time is it?", "thanks", "bye":
matched on the whole utterance (never a word inside a longer question), answered in a few milliseconds with
LittleBot's own tools (``chatgpt.tools.RobotToolbox``), and said straight away. Anything else goes to the brain.
A short, fixed list on purpose: the brain understands everything; this only saves it the trouble.
"""

from __future__ import annotations

import dataclasses
import random
import re
from typing import Any

_FILLER = r"(?:hey|please|now|little ?bot|thanks|okay|ok)"
_POLITE = rf"(?:(?:{_FILLER})[\s,.!]*)*"  # speech-to-text writes "Thanks. Bye." as often as "Thanks, bye!"


def _whole(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{_POLITE}(?:{pattern})[\s,.!]*{_POLITE}[.!?]*\s*$", re.IGNORECASE)


_STOP_CORE = (r"stop(?: it| that| talking| now)?|cancel(?: that)?|never ?mind|forget it|be quiet|quiet|shush|shh+|"
              r"hush|shut up|that'?s enough|enough|no(?:thing)? never ?mind|"
              # telling it to stop ends the call, without a word (asked for on the robot, 2026-10-31)
              r"(?:end|stop|close|finish) (?:the |this )?(?:call|chat|conversation|session)|end it|hang up|"
              r"stop listening|you can (?:go|stop)(?: now)?|no more questions|nothing else|that will be all|"
              r"that'?ll be all|that'?s (?:all|it)(?: for now)?|i'?m done|we'?re done|go away|leave me alone|"
              # the end of "stop talking", its "stop" said under the voice's last words (the robot, 2026-10-31:
              # "Talking." was answered with "Looks like you're still there")
              r"(?:no )?talking")
_STOP_LEAD = r"hey|no|oh|ok(?:ay)?|alright|all right|just|i said|(?:can|could|would|will) you(?: please)?"
STOP = _whole(rf"(?:(?:{_STOP_LEAD})[\s,]+)?(?:{_STOP_CORE})(?:[\s,.!]+(?:{_STOP_CORE}))*|nothing")
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
LOUDER = _whole(rf"{_ASK}(?:turn (?:it |that |yourself |{_THE_VOLUME} )?up{_BIT_AFTER}|"
                rf"turn up {_THE_VOLUME}{_BIT_AFTER}|(?:raise|increase) {_THE_VOLUME}{_BIT_AFTER}|volume up|"
                rf"speak up|(?:make it |be |speak |talk |go )?{_BIT}louder)(?: please)?")
QUIETER = _whole(rf"{_ASK}(?:turn (?:it |that |yourself |{_THE_VOLUME} )?down{_BIT_AFTER}|"
                 rf"turn down {_THE_VOLUME}{_BIT_AFTER}|(?:lower|decrease|reduce) {_THE_VOLUME}{_BIT_AFTER}|"
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
    name: str  # stop | thanks | bye | volume | pause | resume | skip | stop_media | time
    say: str = ""  # what to say (may be "")
    end: bool = False  # the session ends after saying it
    args: dict[str, Any] = dataclasses.field(default_factory=dict)


def match(text: str, *, media_on: bool = False) -> Intent | None:
    """The instant intent ``text`` is, or None (the brain answers). ``media_on``: a video or music is playing or
    paused for this chat, so "pause", "play", "skip" are about it."""
    t = " ".join(str(text or "").replace("’", "'").split())
    if not t or len(t) > 60:
        return None
    if STOP_MEDIA.match(t) and media_on:
        return Intent("stop_media", random.choice(ACK_OK))
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
        if capped and now is not None and intent.args.get("percent", 0) > now:
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
    return intent.say


__all__ = ["Intent", "match", "run"]
