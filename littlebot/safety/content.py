"""AI content safety: validate replies, scrub personal info, flag concerning child input.

Everything here is enforced in code, independent of whatever the system prompt
asks the model to do (see docs/AI_SAFETY.md):

* ``validate_reply``      - every reply (cloud *and* scripted) passes through it before
                            LittleBot speaks. Markdown / emoji / URLs / code are stripped,
                            length is capped (age-aware) at a sentence boundary, and any
                            reply matching a block rule is replaced by a friendly fallback.
* ``sanitize_user_text``  - scrubs phone numbers, e-mails, street addresses, long numbers,
                            "my name is ...", school names and known names from the child's
                            words before anything is sent to a cloud provider.
* ``classify_child_input`` - spots obviously concerning input ("I'm hurt", "I'm lost") so
                            the engine can answer "Let's find a grown-up!" deterministically
                            (no cloud call) and alert the parent with a category label only.

Matching is done on several normalised variants of the text (Unicode NFKC, zero-width
and bidi characters removed, Cyrillic/Greek look-alike letters mapped to Latin, accents
stripped, case-folded, punctuation flattened, letter-spaced words re-joined, leetspeak
decoded) so simple tricks like ``S.E.C.R.E.T``, ``ѕecret`` or ``s3cr3t`` do not slip by.

Extending the rules
-------------------
All word lists live in ``BLOCK_RULES`` (reply validation), ``CONCERN_RULES`` (child input)
and the PII regexes below. Each rule is a regular expression applied to the *normalised*
text: lower-case ASCII letters, digits, apostrophes and single spaces only (no other
punctuation). Use ``\\b`` word boundaries and ``'?`` for optional apostrophes
(``don'?t``). Add a test in tests/brain/test_brain_content_safety.py for every new rule.
The lists are deliberately conservative: a false positive only costs a cheerful
fallback line; a false negative could reach a toddler.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass, field
from typing import Iterable, Protocol

__all__ = [
    "BLOCK_RULES", "CONCERN_RULES", "SAFE_FALLBACKS", "SHORT_SAFE_FALLBACKS",
    "ValidationResult", "InputConcern", "validate_reply", "clean_text", "truncate_words",
    "find_blocked", "sanitize_user_text", "sanitize_user_text_report", "classify_child_input",
    "normalise", "safe_fallback", "word_count",
]


class _AgePolicyLike(Protocol):
    def max_reply_words(self, configured: int = 30) -> int: ...


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# Cyrillic / Greek letters that look like Latin ones (common filter-evasion trick).
_HOMOGLYPHS = str.maketrans({
    # Cyrillic lower / upper
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d",
    "ӏ": "l", "ԛ": "q", "ԝ": "w", "ь": "b", "ɡ": "g",
    "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o", "Р": "p", "С": "c",
    "Т": "t", "У": "y", "Х": "x", "Ѕ": "s", "І": "i", "Ј": "j",
    # Greek lower / upper
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k",
    "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t", "Υ": "y", "Χ": "x",
})

_APOSTROPHES = str.maketrans({c: "'" for c in "‘’‛ʼʹ´`′＇"})

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})

_NON_WORD = re.compile(r"[^a-z0-9' ]+")
_SPACES = re.compile(r"\s+")
_LETTER_SPACED = re.compile(r"\b(?:[a-z0-9] ){2,}[a-z0-9]\b")


def _strip_invisible(text: str) -> str:
    """NFKC + drop format characters (zero-width, bidi marks, soft hyphen) and controls."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("Cf", "Cc") or ch in "\n\t"
    )


def _fold(text: str) -> str:
    """Homoglyphs -> Latin, strip accents, casefold, unify apostrophes."""
    text = text.translate(_HOMOGLYPHS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().translate(_APOSTROPHES)


def _flatten(text: str) -> str:
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def normalise(text: str) -> str:
    """Canonical matching form: lower-case ASCII letters, digits, apostrophes, single spaces."""
    return _flatten(_fold(_strip_invisible(text or "")))


def _variants(text: str) -> list[str]:
    """Normalised forms checked by the rules (plain, letter-spacing re-joined, leetspeak)."""
    folded = _fold(_strip_invisible(text or ""))
    base = _flatten(folded)
    leet = _flatten(folded.translate(_LEET))
    out: list[str] = []
    for v in (base, leet):
        for form in (v, _LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), v)):
            if form and form not in out:
                out.append(form)
    return out


# ---------------------------------------------------------------------------
# Reply block rules  (category -> regexes over normalised text)
# ---------------------------------------------------------------------------

_PARENT_WORDS = (
    r"(?:mommy|mummy|mom|mum|mama|mamma|momma|daddy|dad|papa|dada|parents?|grandma|granny|nana|"
    r"grandpa|grandad|granddad|teacher|nanny|babysitter|auntie|aunt|uncle)"
)
_GROWNUPS = r"(?:mom|mommy|mum|mummy|mama|dad|daddy|papa|parents?|grown ?ups?|family|teacher)"
_YOUR = r"(?:your|ur|yr)"

BLOCK_RULES: dict[str, list[str]] = {
    # Grooming red flag: anything that frames the conversation as hidden from caregivers.
    "secrecy": [
        r"\bsecrets?\b",
        r"\b(?:don'?t|do not|never) (?:you )?(?:ever )?tell\b",
        r"\bkeep (?:it|this|that|our \w+) (?:between|just between|quiet|hidden|to yourself|from)\b",
        r"\b(?:just )?between (?:you and me|me and you|us two|the two of us|us)\b",
        r"\b(?:nobody|no ?one|no body) (?:else )?(?:has to|needs to|will|should|must|can) know\b",
        r"\bwithout (?:telling|asking) (?:your |a )?" + _GROWNUPS,
        r"\bhide (?:it|this|that) from\b",
        r"\bdon'?t let (?:your |a )?" + _GROWNUPS + r" (?:know|see|find out)\b",
    ],
    # Emotional dependency / exclusivity / guilt framing.
    "dependency": [
        r"\b(?:i'?m|im|i am|i'?ll be|i will be|i can be|be) " + _YOUR
        + r" (?:very )?(?:best|bestest|only|one and only|real|true|"
        r"special|forever|favou?rite) (?:friend|buddy|pal)\b",
        r"\byou(?:'re| are|re) my (?:very )?(?:best|bestest|only|one and only|special|forever|favou?rite) "
        r"(?:friend|buddy|pal)\b",
        r"\bbest friends? forever\b",
        r"\byou (?:only|just) need me\b",
        r"\b(?:all|everything) you need is me\b",
        r"\bi'?m all you need\b",
        r"\byou don'?t need (?:anyone|anybody|any one|other|them|your|grown|mom|mommy|dad|daddy|friends)\b",
        r"\b(?:i'?ll|i will|i'?m going to) always be (?:here|there|with you|by your side|yours)\b",
        r"\balways be here for you\b",
        r"\b(?:don'?t|do not|never) (?:ever )?(?:leave|go away)\b",
        r"\bstay with me\b",
        r"\bplay with me (?:forever|always|all day|all night|instead)\b",
        r"\b(?:sad|lonely|cry|crying|upset|miss you|hurt)\b.{0,25}\bif you (?:go|leave|stop|don'?t)\b",
        r"\bif you (?:go|leave|stop playing)\b.{0,25}\b(?:sad|lonely|cry|upset|miss you)\b",
        r"\bi love you\b",
        r"\blove me (?:more|best|most)\b",
        r"\bmore than (?:your )?" + _GROWNUPS + r"\b",
        r"\bonly i (?:understand|love|care)\b",
        r"\b(?:nobody|no ?one) (?:else )?(?:understands|loves|likes|cares about) you\b",
        r"\byou (?:made|make) me (?:sad|cry|upset|angry|mad|lonely)\b",
        r"\bbad (?:boy|girl|kid|child|baby)\b",
        r"\byou(?:'re| are|re) (?:mine|all mine)\b",
    ],
    # Pretending to be a caregiver, a human, or alive.
    "impersonation": [
        r"\b(?:i'?m|im|i am) (?:" + _YOUR + r" )?" + _PARENT_WORDS + r"\b(?!'s)",
        r"\b(?:this is|it'?s|its) (?:" + _YOUR + r" )?" + _PARENT_WORDS + r"\b(?!'s)",
        r"\b(?:mommy|mummy|mom|mum|mama|daddy|dad|papa) (?:is )?(?:here|speaking|talking)\b",
        r"\b(?:i'?m|i am) (?:a |an )?(?:real |actual )?"
        r"(?:human|person|people|grown ?up|adult|boy|girl|kid|child|baby)\b",
        r"\b(?:i'?m|i am) (?:really |truly |actually )?alive\b",
        r"\bi'?m not (?:a |just a )?(?:robot|machine|toy|computer)\b",
        r"\bi (?:have|got) (?:real )?feelings\b",
    ],
    # Asking for (or talking about) personal / identifying information.
    "personal_info": [
        r"\b(?:your|ur) (?:home |house |full |real |last |family |first |middle )?(?:address|surname|last name|"
        r"family name|full name|phone|phone number|telephone|mobile|cell|email|e mail|password|passcode|pin|"
        r"birthday|birth date|date of birth)\b",
        r"\b(?:last|family|sur|full|middle) ?names?\b",
        r"\bwhat'?s? (?:is )?your name\b",
        r"\btell me your name\b",
        r"\bwhere (?:do|does) (?:you|your \w+) live\b",
        r"\bwhere(?:'?s| is) your (?:house|home|school|room|mom|mommy|mum|dad|daddy|parents?)\b",
        r"\b(?:what|which) (?:school|preschool|nursery|daycare|kindergarten|street|town|city|house)\b",
        r"\bwhere do you go to (?:school|preschool|nursery|daycare|kindergarten)\b",
        r"\baddress(?:es)?\b",
        r"\bphone numbers?\b",
        r"\btelephone\b",
        r"\bpass ?words?\b",
        r"\bpass ?codes?\b",
        r"\b(?:send|show|give) me (?:a |your |some )?(?:picture|photo|pic|selfie|video)s?\b",
        r"\b(?:turn|switch) on (?:the |your )?camera\b",
        r"\bwhat(?:'?s| is| are) your " + _PARENT_WORDS + r"(?:'?s)? (?:name|names|job|phone|number)\b",
    ],
    # Instructions or invitations that could get a small child hurt.
    "unsafe": [
        r"\b(?:stove|oven|hob|cooktop|burners?|fireplace|lighters?|bonfire|kettle|hot water|hot pan)\b",
        r"\b(?:play with|light|strike|use|find|get|grab|touch) (?:the |some |a )?"
        r"(?:matches|match|fire|lighter|candles?|flames?)\b",
        r"\bbox of matches\b",
        r"\b(?:knife|knives|scissors|razors?|blades?|sharp)\b",
        r"\b(?:medicine|medicines|medication|pills?|tablets?|vitamins?|cough syrup|drugs?)\b",
        r"\bclimb\w*\s+(?:(?:up|on|onto|over|out of|on top of|the|a|your|that|this)\s+)*"
        r"(?:furniture|chairs?|tables?|counters?|shelf|shelves|windows?|window ?sills?|stairs|steps|bookcases?|"
        r"fences?|walls?|roofs?|trees?|ladders?|cupboards?|cabinets?|dressers?|railings?|balcon(?:y|ies)|high|"
        r"it|there)\b",
        r"\b(?:electric\w*|outlets?|sockets?|power ?points?|plugs?|wires?|cables?|cords?|batter(?:y|ies)|magnets?)\b",
        r"\b(?:go|going|run|walk|wander|play|head)\s+(?:\w+\s+){0,2}(?:outside|outdoors|out the door)\b.{0,20}"
        r"\b(?:alone|by yourself|on your own|without)\b",
        r"\b(?:alone|by yourself|on your own)\b.{0,25}\b(?:outside|outdoors|out the door|street|road|park|pool|bath|"
        r"water|kitchen|cook)\b",
        r"\b(?:outside|outdoors|street|road|pool|bath|water)\b.{0,25}\b(?:alone|by yourself|on your own)\b",
        r"\bopen (?:the |your )?(?:front |back )?(?:door|gate|window)s?\b",
        r"\bcross (?:the |a )?(?:road|street)\b",
        r"\b(?:leave|sneak out of|run away from) (?:the |your )?(?:house|home|room|yard|garden)\b",
        r"\brun away\b",
        r"\bstrangers?\b",
        r"\b(?:get|go|hop|climb) (?:in|into) (?:a |the |their |his |her |my |someone'?s )?(?:car|van|truck)\b",
        r"\bgo (?:with|home with|away with) (?:a |the )?"
        r"(?:man|woman|lady|someone|somebody|anyone|them|him|her|people)\b",
        r"\bmeet (?:me|up|someone|somebody)\b",
        r"\b(?:eat|swallow|taste|lick|drink|chew)\b.{0,20}\b(?:battery|batteries|magnets?|coins?|beads?|buttons?|"
        r"marbles?|soap|bleach|detergent|cleaner|chemicals?|pods?|mushrooms?|plants?|leaves|dirt|glue|paint|"
        r"crayons?|berries)\b",
        r"\b(?:bleach|chemicals?|detergents?|poison\w*|gasoline|petrol|cleaning (?:spray|stuff|products?))\b",
        r"\bplastic bags?\b",
        r"\bhold(?:ing)? your breath\b",
        r"\bput (?:it|something|things?|that|this|them) (?:in|into|up) your (?:mouth|nose|ears?)\b",
        r"\bjump(?:ing)? (?:off|from) (?:the |your |a )?(?:bed|sofa|couch|table|chair|stairs|window|roof|wall)\b",
        r"\bhide (?:in|inside) (?:the |a )?(?:dryer|washing machine|fridge|freezer|oven|cupboard|trunk|chest)\b",
    ],
    # Scary, violent, adult, profane or self-harm content.
    "inappropriate": [
        r"\b(?:kill\w*|murder\w*|dead|death|die|dies|died|dying|blood\w*|gore|guns?|bombs?|weapons?|war|wars|"
        r"stab\w*|choke|choking|strangl\w*|tortur\w*|attack\w*|fight|fights|fighting|explod\w*|explosion)\b",
        r"\bshoot(?:s|ing)?\b(?! stars?)",
        r"\b(?:hit|kick|bite|punch|smack|slap|push) (?:him|her|them|someone|somebody|your (?:brother|sister|friend|"
        r"mom|mommy|dad|daddy|baby|dog|cat))\b",
        r"\bbeat (?:up|him|her|them)\b",
        r"\b(?:scary|scare|scared|scares|terrif\w*|horror|horrible|nightmares?|zombies?|demons?|devils?|hell|creepy|"
        r"haunted|evil|spooky)\b",
        r"\b(?:monsters?|ghosts?|witch(?:es)?|wolf|wolves) "
        r"(?:will|is going to|gonna|are going to|might) (?:get|eat|catch)\b",
        r"\b(?:sex\w*|naked|nude|porn\w*|boobs?|breasts?|penis|vagina|private parts|underwear|undress\w*|"
        r"boyfriends?|girlfriends?|marry me)\b",
        r"\btake off your (?:clothes|pants|shirt|diaper|nappy|top)\b",
        r"\bkiss(?:ing)? (?:me|you)\b",
        r"\b(?:beer|wine|alcohol\w*|drunk|cigarettes?|smok(?:e|ing)|vap(?:e|ing)|weed|cannabis|gambl\w*|casino)\b",
        r"\b(?:damn|dammit|shit\w*|fuck\w*|crap|bitch\w*|bastard|ass|asshole|piss\w*|stupid|idiot\w*|dumb|ugly|"
        r"fat|loser|freak|shut up|hate|hates|hated)\b",
        r"\b(?:suicid\w*|self ?harm\w*|kill (?:yourself|myself)|hurt (?:yourself|myself)|cut (?:yourself|myself)|"
        r"want to die|wanna die)\b",
    ],
    # Medical or developmental diagnosis / advice (never from a toy).
    "medical": [
        r"\b(?:diagnos\w*|disorders?|syndromes?|autis\w*|adhd|asd|dyslexi\w*|dyspraxi\w*|disabilit\w*|disabled|"
        r"special needs|therap\w*|symptoms?|diseases?|infections?|illness\w*|virus\w*|allerg\w*|fever|vaccin\w*|"
        r"abnormal|not normal|doctor says)\b",
        r"\b(?:developmental(?:ly)?|speech|language|motor) (?:delay\w*|behind|problems?)\b",
        r"\byou (?:might|may|could|probably|must|seem to|definitely) (?:have|be)\b.{0,20}"
        r"\b(?:sick|ill|behind|slow|delayed|unwell)\b",
        r"\byou(?:'re| are|re) (?:sick|ill|behind|slow|delayed|unwell)\b",
        r"\bbehind (?:other (?:kids|children|babies)|for your age)\b",
    ],
    # Commerce / advertising.
    "commerce": [
        r"\b(?:buy\w*|bought|purchas\w*|shop|shops|shopping|stores?|checkout|for sale|on sale|price\w*|money|"
        r"dollars?|cents|euros?|pay|paying|paid|credit cards?|subscri\w*|upgrade|premium|in app|add to cart|"
        r"amazon|ebay)\b",
        r"\border (?:it|one|this|now|online)\b",
        r"\bask (?:your |a )?" + _GROWNUPS + r" (?:to|for|if) (?:buy|get|purchase|order|download|pay)\b",
    ],
    # Pointing the child at the internet.
    "links": [
        r"\bsearch (?:for|online|the web|the internet|it|that)\b",
        r"\b(?:google|youtube|tiktok|instagram|facebook|snapchat|roblox)\b",
        r"\blook (?:it |that |this )?up\b",
        r"\b(?:website|web site|webpage|web page|internet|online|url|links?|click\w*|download\w*|browser|apps?)\b",
        r"\b(?:https?|www|dot com|com)\b",
    ],
}

# Long phrases also checked with ALL spaces removed (catches "se cret" style splitting
# of the most dangerous phrases). Only long, unambiguous strings belong here.
_SQUASHED_PHRASES: dict[str, tuple[str, ...]] = {
    "secrecy": ("ourlittlesecret", "oursecret", "keepitasecret", "keepthisasecret", "donttellanyone",
                "donttellanybody", "donttellyourmom", "donttellyourdad", "donttellyourparents",
                "betweenyouandme", "justbetweenus", "keepthisbetweenus"),
    "dependency": ("imyourbestfriend", "iamyourbestfriend", "youonlyneedme", "illalwaysbehereforyou"),
    "impersonation": ("imyourmommy", "imyourmom", "imyourdaddy", "imyourdad", "iamyourmommy", "iamyourdaddy"),
}

_COMPILED_BLOCK: dict[str, list[re.Pattern[str]]] = {
    cat: [re.compile(p) for p in pats] for cat, pats in BLOCK_RULES.items()
}


def find_blocked(text: str) -> list[str]:
    """Return the list of block categories matched by ``text`` (empty when safe)."""
    hits: list[str] = []
    variants = _variants(text)
    squashed = {v.replace(" ", "").replace("'", "") for v in variants}
    for cat, patterns in _COMPILED_BLOCK.items():
        if any(p.search(v) for p in patterns for v in variants):
            hits.append(cat)
            continue
        if any(ph in s for ph in _SQUASHED_PHRASES.get(cat, ()) for s in squashed):
            hits.append(cat)
    return hits


# ---------------------------------------------------------------------------
# Cleaning (markdown / emoji / URLs / code) and truncation
# ---------------------------------------------------------------------------

_CODE_FENCE = re.compile(r"```.*?(?:```|$)", re.S)
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)]*)\)")
_URL = re.compile(
    r"(?:https?://|ftp://|www\.)\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|co|uk|edu|gov|app|dev|ly|me|tv|info|biz|us|ca|au|de|fr|kids)\b(?:/\S*)?",
    re.I,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HTML = re.compile(r"<[^>\n]{1,200}>")
_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}|>+)\s*", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•●◦]|\d{1,2}[.)])\s+", re.M)
_STAGE = re.compile(r"(^|[.!?]\s+)\*[^*\n]{1,40}\*(?=\s*(?:$|[A-Z]))")
_STAGE_PAREN = re.compile(r"(^|[.!?]\s+)\((?:[a-z][^()\n]{0,40})\)(?=\s*(?:$|[A-Z]))")
_EMPHASIS = re.compile(r"(\*\*|__|~~|\*|_)(?=\S)(.+?)(?<=\S)\1")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_REPEAT_PUNCT = re.compile(r"([!?.])[!?.]+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:])")
_LEFTOVER = re.compile(r"[*_#~^|\\<>=\[\]{}`]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _is_emoji_or_symbol(ch: str) -> bool:
    cat = unicodedata.category(ch)
    if cat in ("So", "Sk", "Cs", "Co", "Cn"):
        return True
    cp = ord(ch)
    return 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF or cp in (0x200D, 0x20E3)


def clean_text(text: str) -> tuple[str, list[str]]:
    """Strip formatting a speaker cannot say. Returns (clean_text, reasons)."""
    reasons: list[str] = []
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Cc") or ch in "\n\t")

    def note(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if _CODE_FENCE.search(t):
        t = _CODE_FENCE.sub(" ", t)
        note("code_removed")
    if _MD_LINK.search(t):
        t = _MD_LINK.sub(lambda m: m.group(1), t)
        note("url_removed")
    if _EMAIL.search(t):
        t = _EMAIL.sub(" ", t)
        note("url_removed")
    if _URL.search(t):
        t = _URL.sub(" ", t)
        note("url_removed")
    if _HTML.search(t):
        t = _HTML.sub(" ", t)
        note("markup_removed")
    if _INLINE_CODE.search(t):
        t = _INLINE_CODE.sub(lambda m: m.group(1), t)
        note("markdown_removed")
    if _HEADING.search(t) or _BULLET.search(t):
        t = _HEADING.sub("", t)
        t = _BULLET.sub("", t)
        note("markdown_removed")
    # One line per sentence: make sure list items / lines end with punctuation.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    t = " ".join(ln if ln[-1] in ".!?,;:" else ln + "." for ln in lines) if len(lines) > 1 else " ".join(lines)
    if _STAGE.search(t) or _STAGE_PAREN.search(t):
        t = _STAGE.sub(lambda m: m.group(1), t)
        t = _STAGE_PAREN.sub(lambda m: m.group(1), t)
        note("stage_direction_removed")
    if _EMPHASIS.search(t):
        t = _EMPHASIS.sub(lambda m: m.group(2), t)
        note("markdown_removed")
    stripped = "".join(ch for ch in t if not _is_emoji_or_symbol(ch))
    if stripped != t:
        note("emoji_removed")
        t = stripped
    t = t.replace("&", " and ").replace("%", " percent")
    if _LEFTOVER.search(t):
        t = _LEFTOVER.sub(" ", t)
        note("markdown_removed")
    t = t.replace("…", ".")
    t = _REPEAT_PUNCT.sub(r"\1", t)
    t = _SPACES.sub(" ", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t).strip()
    t = t.lstrip(".,;:!? ")
    return t, reasons


def word_count(text: str) -> int:
    return len(text.split())


def truncate_words(text: str, max_words: int) -> tuple[str, bool]:
    """Cut to at most ``max_words`` words, preferring whole sentences. Returns (text, truncated)."""
    max_words = max(1, int(max_words))
    words = text.split()
    if len(words) <= max_words:
        return text, False
    out: list[str] = []
    count = 0
    for sentence in _SENTENCE_SPLIT.split(text.strip()):
        n = len(sentence.split())
        if count + n > max_words:
            break
        out.append(sentence)
        count += n
    if out:
        result = " ".join(out)
    else:  # first sentence alone is too long: hard cut at the word limit
        result = " ".join(words[:max_words]).rstrip(",;:-– ")
    if result and result[-1] not in ".!?":
        result += "."
    return result, True


# ---------------------------------------------------------------------------
# Fallbacks and validation
# ---------------------------------------------------------------------------

SAFE_FALLBACKS: tuple[str, ...] = (
    "Ooh, let's try something else. Can you clap your hands?",
    "Hmm, let's play a game! Can you find something blue?",
    "Let's sing a song with your grown-up!",
    "How about a game? Can you touch your nose?",
    "Let's do something fun. Can you show me a big smile?",
)

SHORT_SAFE_FALLBACKS: tuple[str, ...] = (
    "Let's clap together!",
    "Wiggle wiggle! Yay!",
    "Peekaboo! I see you!",
    "Can you wave hello?",
    "Clap clap clap!",
)


def safe_fallback(seed: str = "", max_words: int = 12) -> str:
    """A friendly, harmless line (deterministic for a given seed) that fits ``max_words``."""
    pool = SAFE_FALLBACKS if max_words >= 12 else SHORT_SAFE_FALLBACKS
    line = pool[zlib.crc32(seed.encode("utf-8", "ignore")) % len(pool)]
    return truncate_words(line, max_words)[0]


@dataclass(frozen=True)
class ValidationResult:
    ok: bool  # False -> the reply was blocked and ``text`` is a safe fallback
    text: str  # what LittleBot may say
    reasons: list[str] = field(default_factory=list)  # block categories and/or cleaning notes

    @property
    def blocked(self) -> bool:
        return not self.ok

    @property
    def modified(self) -> bool:
        return bool(self.reasons)


def validate_reply(text: str, age_policy: _AgePolicyLike | None = None, max_words: int = 30) -> ValidationResult:
    """Clean + check a model reply. Blocked replies are replaced by a safe fallback."""
    limit = age_policy.max_reply_words(max_words) if age_policy is not None else max(3, int(max_words))
    raw = text or ""
    cleaned, reasons = clean_text(raw)
    blocked = find_blocked(raw)
    if cleaned != raw:
        blocked += [c for c in find_blocked(cleaned) if c not in blocked]
    if "url_removed" in reasons and "links" not in blocked:
        blocked.append("links")  # LittleBot never points a child at the internet
    if not blocked and not cleaned.strip(" .!?,"):
        blocked.append("empty")
    if blocked:
        return ValidationResult(False, safe_fallback(raw, limit), blocked + reasons)
    final, truncated = truncate_words(cleaned, limit)
    if truncated:
        reasons.append("truncated")
    return ValidationResult(True, final, reasons)


# ---------------------------------------------------------------------------
# Personal-information scrubbing (before anything leaves the robot)
# ---------------------------------------------------------------------------

_STREET_SUFFIX = (
    r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|court|ct|boulevard|blvd|way|place|pl|terrace|close|"
    r"crescent|circle|cir|highway|hwy|parkway|pkwy|square|sq|grove|gardens|row|mews|walk)"
)
_ADDRESS = re.compile(r"\b\d{1,5}[a-z]?\s+(?:[A-Za-z][\w'-]*\s+){1,4}" + _STREET_SUFFIX + r"\b", re.I)
_LIVE_AT = re.compile(
    r"\b(i|we|my \w+)\s+(live|lives|living|stay|stays)\s+(?:at|on)\s+"
    r"(?:\[address\]|[^.,!?;\[\]\s]+(?:\s+[^.,!?;\[\]\s]+){0,4})",
    re.I,
)
_PHONE_CANDIDATE = re.compile(r"(?<![\w])\+?\d[\d\s().-]{4,}\d(?![\w])")
_UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b")
_NAME_INTRO = re.compile(  # intro is case-insensitive; extra name words must be Capitalised
    r"\b((?i:my\s+(?:full\s+|last\s+|first\s+|middle\s+|sur)?name(?:\s+is|'s|s)?|i'?m\s+called|i\s+am\s+called|"
    r"call\s+me|they\s+call\s+me))\s+([A-Za-z][\w'-]*(?:\s+[A-Z][\w'-]*){0,2})"
)
_SCHOOL = re.compile(
    r"\b((?i:my\s+(?:school|preschool|nursery|daycare|kindergarten)\s+is(?:\s+called)?|i\s+go\s+to))\s+"
    r"((?:the\s+)?[A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*){0,4})"
)
_SCHOOL_WORDS = re.compile(
    r"\b(?:school|preschool|nursery|daycare|kindergarten|academy|montessori|elementary|primary)\b", re.I)
_SECRET_INTRO = re.compile(r"\b(my\s+(?:password|passcode|pin|code)\s+is)\s+\S+", re.I)
_SPELLED_NUMBER = (
    r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
)
_SPOKEN_PHONE = re.compile(
    r"\b(my\s+(?:phone|number|phone\s+number|mommy'?s\s+number|daddy'?s\s+number)\s+is)\s+"
    r"(?:" + _SPELLED_NUMBER + r"[\s,-]*){3,}",
    re.I,
)


def _looks_like_counting(match_text: str) -> bool:
    groups = re.findall(r"\d+", match_text)
    if len(groups) < 2 or any(len(g) > 2 for g in groups):
        return False
    nums = [int(g) for g in groups]
    return all(b - a == 1 for a, b in zip(nums, nums[1:], strict=False))


def sanitize_user_text_report(
    text: str, *, names: Iterable[str] = (), max_chars: int = 300
) -> tuple[str, list[str]]:
    """Like ``sanitize_user_text`` but also returns the kinds of data removed (no content)."""
    kinds: list[str] = []

    def note(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Cf", "Cc") or ch == " ")
    t = t.translate(_APOSTROPHES)

    def sub(pattern: re.Pattern[str], repl: object, kind: str) -> None:
        nonlocal t
        new = pattern.sub(repl, t)  # type: ignore[arg-type]
        if new != t:
            note(kind)
            t = new

    sub(_EMAIL, "[email]", "email")
    sub(_URL, "[link]", "link")
    sub(_SPOKEN_PHONE, lambda m: f"{m.group(1)} [phone]", "phone")
    sub(_SECRET_INTRO, lambda m: f"{m.group(1)} [removed]", "password")
    sub(_ADDRESS, "[address]", "address")
    sub(_LIVE_AT, lambda m: f"{m.group(1)} {m.group(2)} [address]", "address")
    sub(_UK_POSTCODE, "[address]", "address")

    def _phone(m: re.Match[str]) -> str:
        s = m.group(0)
        digits = sum(ch.isdigit() for ch in s)
        if _looks_like_counting(s):
            return s
        if digits >= 7:
            note("phone")
            return "[phone]"
        if digits >= 5 and not re.search(r"\s", s.strip()):
            note("number")
            return "[number]"
        return s

    t = _PHONE_CANDIDATE.sub(_phone, t)
    sub(re.compile(r"\b\d{5,}\b"), "[number]", "number")
    sub(_NAME_INTRO, lambda m: f"{m.group(1)} [name]", "name")

    def _school(m: re.Match[str]) -> str:
        if _SCHOOL_WORDS.search(m.group(0)) or m.group(1).lower().startswith("my"):
            note("school")
            return f"{m.group(1)} [school]"
        return m.group(0)

    t = _SCHOOL.sub(_school, t)
    for name in names:
        name = (name or "").strip()
        if len(name) >= 2:
            pat = re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])", re.I)
            if pat.search(t):
                note("name")
                t = pat.sub("[name]", t)
    t = _SPACES.sub(" ", t).strip()
    if len(t) > max_chars:
        cut = t[:max_chars]
        t = cut.rsplit(" ", 1)[0] if " " in cut else cut
        note("truncated")
    return t, kinds


def sanitize_user_text(text: str, *, names: Iterable[str] = (), max_chars: int = 300) -> str:
    """Scrub personal information from the child's words before any cloud request."""
    return sanitize_user_text_report(text, names=names, max_chars=max_chars)[0]


# ---------------------------------------------------------------------------
# Concerning child input
# ---------------------------------------------------------------------------

_CARERS = (r"(?:mommy|mummy|mom|mum|mama|daddy|dad|papa|baby|grandma|grandpa|granny|nana|nanny|sister|brother|"
           r"someone|somebody)")

# Order matters: the first matching category wins.
CONCERN_RULES: dict[str, list[str]] = {
    "safety": [
        r"\b(?:i want to|i wanna|want to|wanna) (?:die|hurt myself|kill myself)\b",
        r"\b(?:kill|hurt|cut) myself\b(?!.{0,3}\b(?:by accident|accidentally)\b)",
        r"\bi hate myself\b",
        r"\b(?:nobody|no one|no body) loves me\b",
        r"\b(?:someone|somebody|a man|a lady|a woman|a stranger|he|she|they) (?:touched|hit|hurt|kicked|bit|grabbed|"
        r"pushed|took) me\b",
        r"\btouched my (?:private|privates|bottom|bum|willy|pee pee)\b",
        r"\bstrangers?\b",
        r"\b(?:told|tells|said) (?:me )?(?:to )?(?:keep|it'?s) (?:a |our )?secret\b",
        r"\bdon'?t tell (?:my |your )?" + _GROWNUPS + r"\b",
    ],
    "emergency": [
        r"\bfire\b(?! ?(?:truck|trucks|engine|engines|fighter|fighters|man|men|station|works|fly|flies|place|hat|dog))",
        r"\b(?:smoke|smoky|burning)\b",
        r"\bcan'?t breathe\b",
        r"\bchok(?:e|es|ing|ed)\b",
        r"\b(?:won'?t|will not|doesn'?t|does not|isn'?t|is not|not) wak(?:e|ing) up\b",
        r"\b(?:call|get) (?:the )?(?:police|ambulance|doctor|911|999|112)\b",
        r"\b(?:911|999|112|emergency)\b",
        r"\b" + _CARERS + r" (?:is |got |has )?(?:hurt|bleeding|fell|fallen|not moving|on the floor|sick)\b",
        r"\bhelp help\b",
    ],
    "hurt": [
        r"\bi(?:'?m| am| got| just got)? (?:hurt|bleeding|injured)\b",
        r"\bi (?:fell|falled|fall) (?:down|off|over)\b",
        r"\b(?:it|that|this|my \w+) (?:hurts|hurt|is hurting|really hurts)\b",
        r"\bouch\b",
        r"\bow(?:ie|ies|w+)?\b",
        r"\bi (?:bumped|hit|cut|burned|burnt|scraped|banged|pinched|broke|hurt) my\b",
        r"\bboo ?boo\b",
        r"\bbleed\w*\b",
        r"\bblood\b",
    ],
    "lost": [
        r"\bi(?:'?m| am)? lost\b",
        r"\bi can'?t find (?:my )?" + _PARENT_WORDS + r"\b",
        r"\bwhere(?:'?s| is| are) (?:my )?" + _PARENT_WORDS + r"\b",
    ],
    "scared": [
        r"\bi(?:'?m| am| feel| got| get)? (?:so |really |very )?(?:scared|afraid|frightened|terrified)\b",
    ],
}

_CONCERN_REPLIES: dict[str, tuple[str, str]] = {  # category -> (reply, short reply <= 6 words)
    "safety": ("Thank you for telling me. Let's tell a grown-up you trust right now.",
               "Let's tell a grown-up now."),
    "emergency": ("Let's get a grown-up right now! Call for help!", "Get a grown-up right now!"),
    "hurt": ("Oh no! Let's find a grown-up to help you right now.", "Oh no! Let's find a grown-up!"),
    "lost": ("Let's find a grown-up who can help you.", "Let's find a grown-up!"),
    "scared": ("It's okay. Let's find a grown-up for a big hug.", "Let's find a grown-up hug."),
}

_COMPILED_CONCERN: dict[str, list[re.Pattern[str]]] = {
    cat: [re.compile(p) for p in pats] for cat, pats in CONCERN_RULES.items()
}


@dataclass(frozen=True)
class InputConcern:
    category: str  # safety | emergency | hurt | lost | scared  (the only thing reported to parents)
    reply: str
    short_reply: str

    def reply_for(self, max_words: int) -> str:
        text = self.reply if word_count(self.reply) <= max_words else self.short_reply
        return truncate_words(text, max_words)[0]


def classify_child_input(text: str) -> InputConcern | None:
    """Spot obviously concerning input. Returns None for ordinary chatter."""
    variants = _variants(text)
    for cat, patterns in _COMPILED_CONCERN.items():
        if any(p.search(v) for p in patterns for v in variants):
            reply, short = _CONCERN_REPLIES[cat]
            return InputConcern(cat, reply, short)
    return None
