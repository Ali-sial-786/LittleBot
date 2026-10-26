"""Face actions the AI may ask for: a menu for prompts, request / tag parsers and a player.

* Typed chat: the model may end a reply with a hidden tag such as ``[face: happy]``.
  ``extract_tags`` removes every tag before the reply is validated, spoken, remembered or stored.
* Live voice: a hand-off such as "make a happy face" or "wink at her" is recognised by
  ``parse_request`` (questions and look-ups such as "how many fingers am I holding?" are not).
* ``perform`` plays an action on ``ctx.face``. It never raises and returns quickly: expressions,
  winks and animations are fire-and-forget; peekaboo keeps the eyes closed for about 1.5 s.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS

log = logging.getLogger("littlebot.brain.face")

# Action name -> short plain description. Expressions first, then animations / combos.
# ("listening", "speaking" and "zzz" are driven by the robot itself and are not offered.)
FACE_MENU: dict[str, str] = {
    "happy": "a big happy smile",
    "surprised": "wide-eyed surprise",
    "sleepy": "droopy, sleepy eyes",
    "curious": "a curious, wondering look",
    "excited": "an excited, wide-eyed look",
    "sad": "a gentle sad face",
    "playful": "a cheeky, playful look",
    "celebrating": "a party, hooray look",
    "affectionate": "a warm, loving look",
    "confused": "a puzzled look",
    "thinking": "a thinking look",
    "neutral": "a calm, ordinary face",
    "wink": "a quick wink",
    "spin_eyes": "eyes spinning round and round",
    "peekaboo": "close the eyes, then pop them open: peekaboo!",
    "nod": "a little nod (yes)",
    "shake": "a little head shake (no)",
    "bounce": "a happy bounce",
    "wiggle": "a silly wiggle",
    "sparkle": "sparkles around the face",
    "hearts": "little floating hearts",
    "confetti": "a burst of confetti",
}

# Timings (seconds). Peekaboo must stay well under 3 s in total.
EXPRESSION_HOLD = 4.0
WINK_HOLD = 1.2
PEEKABOO_CLOSED = 1.5
PEEK_HOLD = 2.5

_COMBOS = ("wink", "peekaboo")
assert all(n in EXPRESSIONS or n in FACE_ANIMATIONS or n in _COMBOS for n in FACE_MENU)


@dataclass(frozen=True)
class FaceAction:
    name: str  # a FACE_MENU key


def menu_text() -> str:
    """All action names as one comma-separated line, for prompts."""
    return ", ".join(FACE_MENU)


# --------------------------------------------------------------------------- names
_ALIASES: dict[str, str] = {
    "spin": "spin_eyes", "spin eye": "spin_eyes", "spinning eyes": "spin_eyes", "spinny eyes": "spin_eyes",
    "dizzy": "spin_eyes", "peek a boo": "peekaboo", "peek": "peekaboo", "heart": "hearts", "love hearts": "hearts",
    "celebrate": "celebrating", "celebration": "celebrating", "party": "celebrating", "surprise": "surprised",
    "smile": "happy", "smiling": "happy", "sleep": "sleepy", "tired": "sleepy", "silly": "playful",
    "think": "thinking", "puzzled": "confused", "loving": "affectionate", "love": "affectionate",
    "sparkles": "sparkle", "sparkly": "sparkle", "winking": "wink", "nodding": "nod", "shaking": "shake",
    "bouncing": "bounce", "wiggling": "wiggle", "normal": "neutral", "calm": "neutral",
}


def _resolve(name: str) -> str | None:
    """Menu name for a tag / bare name ("Happy", "spin eyes", "spin-eyes", "happy face"), else None."""
    key = re.sub(r"[\s_-]+", " ", str(name).lower()).strip(" .,!?;:'\"")
    for k in (key, re.sub(r" (?:face|eyes|animation)$", "", key)):
        if k.replace(" ", "_") in FACE_MENU:
            return k.replace(" ", "_")
        if k in _ALIASES:
            return _ALIASES[k]
    return None


# ---------------------------------------------------------------------------- tags
_TAG = re.compile(r"[\[({<]\s*face\s*[:=]\s*([^\[\](){}<>]*?)\s*[\])}>]", re.IGNORECASE)
_CUT_TAG = re.compile(r"[\[({<]\s*face\b[^\[\](){}<>]*$", re.IGNORECASE)  # cut off by the token limit


def extract_tags(text: str) -> tuple[str, list[FaceAction]]:
    """Remove every ``[face: NAME]`` tag. Returns (clean text, known actions in order)."""
    text = str(text or "")
    actions: list[FaceAction] = []

    def _take(m: re.Match[str]) -> str:
        name = _resolve(m.group(1))
        if name is not None:
            actions.append(FaceAction(name))
        return " "

    clean, found = _TAG.subn(_take, text)
    cut = _CUT_TAG.search(clean.rstrip())
    if cut is not None:
        clean, found = clean.rstrip()[: cut.start()], found + 1
    if not found:
        return text, actions
    clean = re.sub(r"[ \t]+([.,!?;:])", r"\1", clean)  # "Moo [face: happy]!" -> "Moo !" -> "Moo!"
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = "\n".join(line.strip() for line in clean.splitlines()).strip()
    return clean, actions


# ------------------------------------------------------------------ live requests
# Keyword -> action, searched within one clause; the earliest match wins (ties: table order).
_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("peekaboo", r"peek ?a ?boo|peekaboo|(?:close|shut|cover|hide)\w* (?:\w+ )?eyes|(?:hide|cover)\w* (?:\w+ )?face"
                 r"|peek(?:s|ing)?"),
    ("wink", r"wink\w*"),
    ("spin_eyes", r"spin\w*|roll\w* (?:\w+ )?eyes|eyes? (?:spin|roll)\w*|dizzy|swirl\w*"),
    ("hearts", r"love hearts|heart eyes|hearts?"),
    ("confetti", r"confetti"),
    ("sparkle", r"sparkl\w*|twinkl\w*|glitter\w*|stars?|shin(?:e|y|ing)"),
    ("nod", r"nod(?:s|ding)?"),
    ("shake", r"shak\w*|head shake"),
    ("bounce", r"bounc\w*|jump\w*|hop(?:s|ping)?"),
    ("wiggle", r"wiggl\w*|wobbl\w*|jiggl\w*|shimmy|danc\w*"),
    ("celebrating", r"celebrat\w*|party|hooray|hurray|yay|cheer(?:s|ing)?"),
    ("excited", r"excit\w*|thrilled"),
    ("sad", r"sad|unhappy|frown\w*|cry|crying|upset|pout\w*|boo ?hoo|grumpy"),
    ("surprised", r"surpris\w*|shock\w*|amaz\w*|astonish\w*|wow|gasp\w*"),
    ("sleepy", r"sleep\w*|tired|yawn\w*|drowsy|snooz\w*|nap"),
    ("curious", r"curious|wonder(?:ing)?|interested|inquisitive"),
    ("playful", r"playful|silly|funny|cheeky|goofy|mischievous|tongue"),
    ("affectionate", r"affectionate|loving|love|kiss\w*|cuddl\w*|hugs?|adoring|sweet"),
    ("confused", r"confus\w*|puzzled|muddled|baffled"),
    ("thinking", r"think\w*|thoughtful|ponder\w*"),
    ("neutral", r"neutral|normal|calm|plain|regular|default|resting"),
    ("happy", r"happy|smil\w*|grin\w*|glad|cheerful|joyful"),
)
_KEYWORD_RES = tuple((name, re.compile(rf"\b(?:{pattern})\b")) for name, pattern in _KEYWORDS)

# Words that start an instruction to make a face (after polite prefixes are removed).
_VERBS = frozenset("""
    make making do doing show showing give giving put pull wear look looking act be become pretend feel get go
    turn switch change try play playing express use send throw blow rain sprinkle flash display react
    smile grin frown pout cry yawn wink spin roll close shut cover hide peek nod shake bounce jump hop wiggle
    wobble jiggle shimmy dance sparkle twinkle shine celebrate cheer
""".split())

_PREFIXES = re.compile(
    r"^(?:(?:hey|hi|ok|okay|now|so|and|then|also|please|littlebot|little bot|lil bot|robot|bot)\b ?"
    r"|(?:tell|ask|get|have) (?:littlebot|little bot|lil bot|the robot|the toy|it|yourself) (?:to )?"
    r"|(?:can|could|would|will) you (?:please )?|you should |you can |i want you to |i'd like you to "
    r"|try to |go ahead and |time to |let's |lets |let us |quickly |just )"
)
# A clause that asks about the world (a look-up / question), not for a face.
_NOT_A_REQUEST = re.compile(
    r"\b(?:if|whether|what|which|who|whom|whose|where|why|when|how many|how much|how old|holding|camera"
    r"|photo|picture|see|seeing|describe|count|read|weather|song|songs|music|video|videos|story|stories|tune"
    r"|melody|rhyme|sound|sounds|noise)\b"
    r"|^(?:(?:do|does|did) (?:you|she|he|they|i|we|it|the|her|his|my|your|this|that)|is|are|was|were|am|has"
    r"|have|had|don't|dont|do not|never|no|not|stop|quit)\b"
)
_LOOK_ELSEWHERE = re.compile(
    r"^look(?:ing)? (?:at|around|for|over|through|out|up|down|in|into|outside|left|right|here|there|closely"
    r"|carefully|after)\b")
_SUBJECT = re.compile(r"\b(?:she|he|they|it|i|we|baby|child|kid|is|are|am|was|were|does|did)\b")
_FACE_TAG_TEXT = re.compile(r"\bface ?[:=] ?([a-z][a-z_ -]*)")


def _strip_prefixes(clause: str) -> str:
    prev = None
    while prev != clause:
        prev, clause = clause, _PREFIXES.sub("", clause, count=1).strip(" ,")
    return clause


def _keyword(clause: str) -> str | None:
    best: tuple[int, int, str] | None = None
    for order, (name, rx) in enumerate(_KEYWORD_RES):
        m = rx.search(clause)
        if m and (best is None or (m.start(), order) < best[:2]):
            best = (m.start(), order, name)
    return best[2] if best else None


def _clause_action(clause: str) -> str | None:
    clause = _strip_prefixes(clause)
    if not clause or _NOT_A_REQUEST.search(clause) or _LOOK_ELSEWHERE.match(clause):
        return None
    words = clause.split()
    if words[0] in _VERBS:
        return _keyword(clause)
    # A bare name: "happy face", "hearts", "peekaboo", "a big smile please".
    if len(words) <= 4 and not _SUBJECT.search(clause):
        return _keyword(clause)
    return None


def parse_request(text: str) -> FaceAction | None:
    """A face action asked for in plain words ("make a happy face", "wink"), else None."""
    raw = str(text or "")
    _, tagged = extract_tags(raw)
    if tagged:
        return tagged[0]
    t = raw.lower().replace("’", "'").replace("_", " ").replace("-", " ")
    m = _FACE_TAG_TEXT.search(t)
    if m:
        words = m.group(1).split()
        for n in (len(words), 2, 1):
            name = _resolve(" ".join(words[:n]))
            if name is not None:
                return FaceAction(name)
    t = re.sub(r"[.!?;:\n]+", ",", t)
    t = re.sub(r"[^a-z0-9' ,]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    clauses = re.split(r" ?(?:,|\band then\b|\bthen\b|\band\b|\bplus\b) ?", t)
    for clause in clauses[:6]:
        name = _clause_action(clause.strip())
        if name is not None:
            return FaceAction(name)
    return None


# --------------------------------------------------------------------- perform
async def perform(ctx: Any, action: FaceAction) -> bool:
    """Play ``action`` on ``ctx.face``. False if there is no face or the name is unknown; never raises."""
    face = getattr(ctx, "face", None)
    name = getattr(action, "name", None)
    if face is None or not isinstance(name, str) or name not in FACE_MENU:
        return False
    if name == "peekaboo":
        return await _peekaboo(face)
    try:
        if name in EXPRESSIONS:
            face.set_expression(name, hold=EXPRESSION_HOLD)
        elif name == "wink":
            face.set_expression("playful", hold=WINK_HOLD)  # the playful face shuts one eye
        else:
            face.play_animation(name)
    except Exception:  # noqa: BLE001 - a face glitch must never break a conversation
        log.debug("face action %s failed", name, exc_info=True)
        return False
    log.debug("face action: %s", name)
    return True


async def _peekaboo(face: Any) -> bool:
    """Close the eyes, wait, then pop them open with the peek animation and a happy face."""
    opened = False
    try:
        face.close_eyes()
        await asyncio.sleep(PEEKABOO_CLOSED)
        face.open_eyes()
        opened = True
        face.play_animation("peek")
        face.set_expression("happy", hold=PEEK_HOLD)
    except Exception:  # noqa: BLE001
        log.debug("peekaboo failed", exc_info=True)
        return False
    finally:
        if not opened:  # failed or cancelled (STOP ALL) while hiding: never leave the eyes shut
            try:
                face.open_eyes()
            except Exception:  # noqa: BLE001
                log.debug("open_eyes failed", exc_info=True)
    log.debug("face action: peekaboo")
    return True
