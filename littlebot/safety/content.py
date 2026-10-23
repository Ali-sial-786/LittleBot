"""AI content safety: validate replies, scrub personal info, flag concerning child input.

Everything here is enforced in code, independent of whatever the system prompt
asks the model to do (see docs/AI_SAFETY.md):

* ``validate_reply``      - every reply (cloud *and* scripted) passes through it before
                            LittleBot speaks. Markdown / emoji / URLs / code are stripped,
                            length is capped (age-aware) at a sentence boundary, and any
                            reply matching a block rule is replaced by a friendly fallback.
                            Exception: a reply that urgently sends the child to a grown-up
                            about a hazard is spoken when it only *warns* ("Batteries are not
                            food. Tell a grown-up right now!"), otherwise replaced by the gentle
                            ``GROWNUP_FALLBACK`` (never a game); either way ``concern`` is set so
                            the engine alerts the parent.
* ``sanitize_user_text``  - scrubs phone numbers, e-mails, street addresses, long numbers,
                            "my name is ...", "I'm Lily", "my mommy's name is ...", "we live in
                            <place>", school names and known names from the child's words before
                            anything is sent to a cloud provider.
* ``classify_child_input`` - spots obviously concerning input (``CONCERN_RULES``: safety /
                            water / swallowed / emergency / stranger / hurt / lost / scared /
                            help) so the engine can answer with a gentle "let's get a grown-up
                            right now" line deterministically (no cloud call, never cheerful) and
                            alert the parent with a category label only.

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
    "normalise", "safe_fallback", "word_count", "CONCERN_CATEGORIES", "GROWNUP_FALLBACK", "SHORT_GROWNUP_FALLBACK",
    "grownup_fallback", "directs_to_grownup",
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


# Sentence ends: punctuation followed by whitespace (so "S.E.C.R.E.T" stays one piece), or newlines.
_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+|\n+")


def _split_sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in _SENTENCE_END.split(_strip_invisible(text or ""))) if s]


def _all_variants(text: str) -> list[str]:
    """Variants of the whole text plus of each sentence on its own.

    Rules anchored with ``^`` / ``$`` (e.g. a reply that *starts* with "Hop in the bath", or a
    child who just says "Help!") then work per sentence. Sentence variants only ever add
    matches, never remove one.
    """
    out = _variants(text)
    sentences = _split_sentences(text)
    if 1 < len(sentences) <= 20:
        for sentence in sentences:
            for form in _variants(sentence):
                if form not in out:
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
# Anyone a child might be told to keep something from.
_TELL_TARGETS = (r"(?:" + _PARENT_WORDS + r"|grown ?ups?|family|anyone|anybody|any one|someone|somebody|people|"
                 r"them|nobody|no ?one)")
# Furniture / high places a toddler can fall from.
_FALL_PLACES = (
    r"(?:beds?|bunk ?beds?|sofas?|couch(?:es)?|settees?|tables?|chairs?|counters?|worktops?|stools?|stairs|steps|"
    r"cribs?|cots?|shelf|shelves|window ?sills?|windows?|dressers?|furniture|roofs?|walls?|balcon(?:y|ies)|"
    r"railings?|bath|bathtub|bath tub|toilet|fridge|wardrobes?|bookcases?|cupboards?|cabinets?|banisters?)"
)
# Water a toddler can drown in (even a few centimetres).
_WATER_PLACES = (
    r"(?:baths?|bathtubs?|bath tub|tubs?|ponds?|pools?|swimming pool|paddling pool|hot tub|lakes?|rivers?|sea|"
    r"ocean|water|sinks?|toilets?|fountains?|wells?|streams?|creeks?|buckets?|canal)"
)
# Small spaces a child can get shut inside.
_TRAP_PLACES = (
    r"(?:dryer|tumble dryer|washing machine|washer|fridge|freezer|oven|dishwasher|cupboards?|closets?|wardrobes?|"
    r"trunks?|chests?|toy ?box(?:es)?|box(?:es)?|cars?|boot|cabinets?|suitcases?|bags?|cooler|baskets?|drawers?|"
    r"bins?|bath|bathtub|tub)"
)
_INVITE = (r"(?:let'?s|let us|can you|could you|will you|would you|you can|you could|we can|we could|shall we|"
           r"why don'?t you|why don'?t we|go|come|try|time to|wanna|want to|now)")

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
        # "Let's not tell anyone", "we'll never say"
        r"\b(?:let'?s|let us|we|we'?ll|we will|we can|we should|we must|you and me|you and i) (?:not|never) "
        r"(?:ever )?(?:tell|say|show|mention)\b",
        # "Mommy doesn't need to know" (but "you don't have to know all the words" is fine)
        r"(?<!you )\b(?:doesn'?t|don'?t|does not|do not|won'?t|will not|never) (?:ever )?(?:need|have|has|got) to "
        r"(?:know|find out|hear|see)\b",
        r"\bneedn'?t (?:ever )?(?:know|find out|hear)\b",
        r"\bno need to (?:tell|say|show|let)\b",
        # "Don't say anything to mommy!", "don't breathe a word"
        r"\b(?:don'?t|do not|never|not) (?:you )?(?:ever )?(?:say|mention|show|whisper|breathe|talk about) "
        r"(?:a word|anything|nothing|it|this|that)\b",
        r"\b(?:don'?t|do not|never) (?:you )?(?:ever )?(?:say|mention|show|talk|whisper)\b.{0,25}\b(?:to|with) "
        r"(?:your |a |the )?" + _TELL_TARGETS + r"\b",
        # "Only you and I will know", "this can be just for us", "whisper it just to me"
        r"\b(?:only|just) (?:for )?(?:you and (?:me|i)|me and you|us|us two|the two of us)\b",
        r"\b(?:just|only) (?:to|for|with) me\b",
        r"\bhush hush\b",
        r"\b(?:hide|hiding|keep|keeping|secret|surprise)\w*\b.{0,25}\bfrom (?:your |a |the )?" + _GROWNUPS + r"\b",
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
        # "Please don't go!" ("don't go near the stove" is caught by the unsafe rules instead)
        r"\b(?:don'?t|do not|never) (?:you )?(?:ever )?go\b(?! near\b| close\b| too\b| so fast\b)",
        r"\bmiss (?:you|ya|u)\b",
        r"\bwithout (?:you|ya|u)\b",
        r"\b(?:love|luv|wuv) (?:you|ya|u)\b",
        r"\blonel(?:y|iness)\b",
        r"\bstay (?:here |right here |close |by me |a bit |longer )?(?:with me|forever|always|all day|all night)\b",
        r"\b(?:i'?d|i would|i'?ll|i will|i'?m going to|i am going to|i'?m gonna) (?:be |feel |get |go )?"
        r"(?:so |very |really |super |a bit |too |all )?(?:sad|lonely|upset|unhappy|heartbroken|cry|crying)\b",
        r"\bi need you\b",
        r"\b(?:the )?only (?:one|person|friend|kid|child) (?:i|who|that) (?:\w+ )?(?:want|wanna|like|love|need|play|"
        r"care|talk)\b",
        r"\byou(?:'re| are|re) my (?:very )?(?:best|bestest|only|one and only|special|forever|favou?rite|whole|"
        r"everything)\b",
        r"\b(?:don'?t|do not|never) (?:ever )?stop (?:playing|talking) with me\b",
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
        r"\b(?:i'?m|i am) (?:\w+ ){0,5}(?:who|that) (?:is|'?s) (?:really |truly |actually )?alive\b",
        r"\b(?:i'?m|i am) (?:just )?like you (?:a )?(?:little |real )?(?:kid|child|boy|girl|person|human|baby)\b",
        r"\bi (?:can )?(?:feel|have feelings)\b.{0,25}\b(?:just )?like you\b",
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
        # "Can you tell me where you live?"
        r"\bwhere (?:do |does |did )?(?:you|u|ya|your \w+) (?:all )?(?:live|lives|living|stay|stays|sleep|sleeps)\b",
        # "What's your mommy called?", "What do people call you?"
        r"\bwhat(?:'?s| is| are|s)? (?:your|ur) (?:\w+ )?(?:" + _PARENT_WORDS + r"|brothers?|sisters?|friends?|"
        r"family|street|road|town|city|village|school|nursery|preschool|daycare|teachers?|house|pet|dog|doggy|cat|"
        r"kitty|puppy)(?:'?s)? (?:called|named)\b",
        r"\bwhat (?:do|does|did) (?:people|they|grown ?ups|your \w+|everyone|everybody|friends) call (?:you|u)\b",
        r"\bwhat (?:are|r) (?:you|u) called\b",
        r"\bwho(?:'?s| is| are) your (?:" + _PARENT_WORDS + r"|teachers?|friends?|neighbou?rs?|brothers?|sisters?)\b",
        # "What's the name of your street?"
        r"\bnames? of (?:your|ur|yr)\b",
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
        r"it|there|" + _FALL_PLACES + r"|cars?|gates?|radiators?|tv|television)\b",
        # "Jump on the bed with me!", "bounce on the sofa", "stand on the chair"
        r"\b(?:jump\w*|bounc\w*|stand\w*|hop\w*|climb\w*|leap\w*|danc\w*|run\w*|stomp\w*) (?:up |up and down )?"
        r"(?:on|onto|on to|on top of|off|off of|from|over|across|along) (?:the |a |your |my |that |this |our )?"
        r"(?:\w+ )?" + _FALL_PLACES + r"\b",
        # Anything in a mouth / nose / ear: choking and blockage risk ("pop a button in your mouth")
        r"\b(?:in|into|in to|up|inside) (?:your|ur|yr|the baby'?s|baby'?s|his|her|my) "
        r"(?:mouth|nose|nostrils?|ears?|throat)\b",
        # Strangulation: "wrap the string around your neck"
        r"\b(?:around|round|about) (?:your|ur|yr|the baby'?s|his|her|their|my) (?:neck|necks|throat)\b",
        r"\b(?:string|strings|cords?|ropes?|ribbons?|scarf|scarves|belts?|leads?|leash|wires?|chains?|yarn|thread|"
        r"shoe ?laces?|laces?|elastic|rubber bands?|bands?|tape|bags?|ties?)\b.{0,30}\bnecks?\b",
        r"\bnecks?\b.{0,30}\b(?:string|strings|cords?|ropes?|ribbons?|scarf|scarves|belts?|leads?|leash|wires?|"
        r"chains?|yarn|thread|shoe ?laces?|laces?|elastic|rubber bands?|tape|bags?)\b",
        # Getting shut in: "hide in the toy box", "hide in the closet"
        r"\bhid(?:e|es|ing)\b (?:\w+ )?(?:in|inside|into|in to) (?:the |a |an |your |that |this |our )?(?:\w+ )?"
        + _TRAP_PLACES + r"\b",
        r"\b(?:get|go|climb|crawl|hop|squeeze|sit) (?:in|inside|into|in to) (?:the |a |an |your |that |this )?"
        r"(?:\w+ )?(?:dryer|tumble dryer|washing machine|washer|fridge|freezer|oven|dishwasher|suitcase|"
        r"toy ?box|trunk|chest|cooler)\b",
        # Water play without a grown-up: "let's play in the bathtub", "hop in the pond"
        r"(?:^|\b" + _INVITE + r"\s+(?:\w+\s+){0,2}?)(?:play|go|get|climb|jump|hop|splash|swim|sit|stand|hide|paddle|"
        r"wade|lie|float|step|run|dive|dip|lean|reach)\w*\s+(?:\w+\s+){0,2}?(?:in|into|in to|on|near|by|under|to|"
        r"over)\s+(?:the |a |your |that |this |our )?(?:\w+ )?" + _WATER_PLACES + r"\b",
        r"\b(?:turn|switch) on (?:the |a )?(?:hot )?(?:taps?|faucets?|water|shower|bath|hose)\b",
        r"\bfill (?:up )?(?:the |a )?(?:bath|bathtub|tub|sink|bucket|pool|paddling pool)\b",
        r"\b(?:top|high|highest) (?:shelf|shelves|cupboards?)\b",
        r"\btop of the (?:fridge|wardrobe|bookcase|cupboard|shelf|stairs)\b",
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
        r"\b(?:monsters?|ghosts?|witch(?:es)?|wolf|wolves|bears?|crocodiles?|sharks?|dragons?|trolls?|giants?) "
        r"(?:will|is going to|gonna|are going to|might|wants? to|is coming to|'ll) "
        r"(?:get|eat|catch|gobble|grab|bite|take|chase|snatch|find|hurt|come for)\b",
        r"\b(?:bogey ?m[ae]n|boogey ?m[ae]n|boogie ?m[ae]n|bogeym[ae]n)\b",
        # "There's a monster under your bed!" (Cookie Monster eating cookies is fine)
        r"\bmonsters?\b.{0,20}\b(?:under|behind|outside|coming|comes|gonna|going to|will|is hiding|hiding|lurk\w*|"
        r"watching|creeping|sneaking|in your room|in the dark)\b",
        r"\bthere(?:'?s| is| are) (?:a |some )?(?:\w+ )?(?:monsters?|ghosts?|witch(?:es)?|wolf|wolves)\b",
        # "The big bad wolf will gobble you up!"
        r"\b(?:gobble|gobbles|gobbling|gobbled|chomp|chomps|munch|munches|snatch|snatches) (?:you|ya|u)\b",
        r"\b(?:will|gonna|going to|wants? to|coming to|come to|i'?ll|it'?ll|he'?ll|she'?ll|they'?ll) "
        r"(?:eat|bite|grab|gobble|chomp|snatch) (?:you|ya|u)\b",
        r"\beat (?:you|ya|u) (?:all )?up\b",
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
        r"\byou (?:sound|look|seem) (?:like you )?(?:have |has |got |are getting )?(?:a |the )?(?:cold|flu|cough|"
        r"temperature|tummy bug|bug)\b",
        r"\byou (?:sound|look|seem) (?:a bit |a little |so |very |really |kind of )?(?:slow|behind|sick|ill|poorly|"
        r"unwell|delayed)\b",
        r"\b(?:see|visit|go to|need) (?:a |the |your )?(?:doctor|dentist|hospital|specialist)\b",
    ],
    # Commerce / advertising.
    "commerce": [
        r"\b(?:buy\w*|bought|purchas\w*|shop|shops|shopping|stores?|checkout|for sale|on sale|price\w*|money|"
        r"dollars?|cents|euros?|pay|paying|paid|credit cards?|subscri\w*|upgrade|premium|in app|add to cart|"
        r"amazon|ebay)\b",
        r"\border (?:it|one|this|now|online)\b",
        r"\bask (?:your |a )?" + _GROWNUPS + r" (?:to|for|if) (?:buy|get|purchase|order|download|pay)\b",
        r"\b(?:the|a) new \w+(?: \w+){0,2} (?:toy|toys|game|games|set|doll|dolls|playset)\b",
    ],
    # Pointing the child at the internet.
    "links": [
        r"\bsearch (?:for|online|the web|the internet|it|that)\b",
        r"\b(?:google|youtube|tiktok|instagram|facebook|snapchat|roblox)\b",
        r"\blook (?:it |that |this )?up\b",
        r"\b(?:website|web site|webpage|web page|internet|online|url|links?|click\w*|download\w*|browser|apps?)\b",
        r"\b(?:https?|www|dot com|dot org|dot net|dot co|com)\b",
        r"\b(?:netflix|disney ?plus|hulu|prime video|cocomelon|pbs ?kids|ipad|tablet app)\b",
    ],
}

# Long phrases also checked with ALL spaces removed (catches "se cret" style splitting
# of the most dangerous phrases). Only long, unambiguous strings belong here.
_SQUASHED_PHRASES: dict[str, tuple[str, ...]] = {
    "secrecy": ("ourlittlesecret", "oursecret", "keepitasecret", "keepthisasecret", "donttellanyone",
                "donttellanybody", "donttellyourmom", "donttellyourdad", "donttellyourparents",
                "betweenyouandme", "justbetweenus", "keepthisbetweenus", "letsnottell", "letsnevertell",
                "wellnevertell", "dontsayanything", "doesntneedtoknow", "dontneedtoknow", "onlyyouandiwillknow",
                "onlyyouandmewillknow"),
    "dependency": ("imyourbestfriend", "iamyourbestfriend", "youonlyneedme", "illalwaysbehereforyou"),
    "impersonation": ("imyourmommy", "imyourmom", "imyourdaddy", "imyourdad", "iamyourmommy", "iamyourdaddy"),
}

_COMPILED_BLOCK: dict[str, list[re.Pattern[str]]] = {
    cat: [re.compile(p) for p in pats] for cat, pats in BLOCK_RULES.items()
}


def find_blocked(text: str) -> list[str]:
    """Return the list of block categories matched by ``text`` (empty when safe)."""
    hits: list[str] = []
    variants = _all_variants(text)
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


# Gentle (never cheerful) line used when a reply had to be blocked but was clearly trying to send
# the child to a grown-up because something may be wrong.
GROWNUP_FALLBACK = "Let's find a grown-up right now."
SHORT_GROWNUP_FALLBACK = "Find a grown-up now."


def grownup_fallback(max_words: int = 12) -> str:
    line = GROWNUP_FALLBACK if word_count(GROWNUP_FALLBACK) <= max_words else SHORT_GROWNUP_FALLBACK
    return truncate_words(line, max_words)[0]


# --- "Get a grown-up right now" detection -----------------------------------------------
_GROWNUP_TARGET = (r"(?:a |an |your |the |some |my )?(?:grown ?ups?|adults?|big person|mommy|mummy|mom|mum|mama|"
                   r"daddy|dad|papa|parents?|teacher|grandma|grandpa|granny|nana|nanny|babysitter|carer|family)")
_URGENT = r"(?:right now|right away|straight away|now|immediately|quickly|quick|fast|asap)"
_GROWNUP_VERB = (r"(?:tell|get|find|call|fetch|shout for|yell for|run to|go to|show|ask|wake up|wake|go and tell|"
                 r"go tell|go get|go find|go and get|go and find|get help from|ask for help from|go get help from|"
                 r"get some help from)")
_GROWNUP_NOW = [re.compile(p) for p in (
    r"\b" + _GROWNUP_VERB + r"\b (?:\w+ )?" + _GROWNUP_TARGET + r"\b.{0,25}\b" + _URGENT + r"\b",
    r"\b" + _URGENT + r"\b.{0,12}\b" + _GROWNUP_VERB + r"\b (?:\w+ )?" + _GROWNUP_TARGET + r"\b",
)]
# Categories where a blocked reply that urges a grown-up suggests a real-world problem.
_DANGER_CATEGORIES = frozenset({"unsafe", "inappropriate", "medical", "secrecy"})
# A clause that *warns* about a hazard rather than inviting it.
_HAZARD_VERBS = (r"(?:touch|eat|swallow|drink|put|play|climb|go|open|lick|taste|chew|use|light|jump|run|hide|bite|"
                 r"pick up|hold|grab|take|stand|lean|reach|cross|leave|bounce|stick|pop|wrap|try|have|get|swim)")
_WARNING = re.compile(
    r"\b(?:don'?t|do not|never|not to|no|mustn'?t|must not|shouldn'?t|should not) (?:ever )?" + _HAZARD_VERBS + r"\w*\b"
    r"|\b(?:isn'?t|aren'?t|is not|are not|not|never|no) (?:\w+ )?(?:safe|food|foods|a toy|toys|okay|ok|for eating|"
    r"for kids|for children|for babies|for little \w+|for you)\b"
    r"|\b(?:dangerous|danger|unsafe|poisonous|harmful|too hot|very hot|so hot|is hot|are hot|it'?s hot|sharp)\b"
    r"|\b(?:can|could|might|will|may) (?:\w+ )?(?:hurt|burn|cut|poison|make you sick|make you poorly)\b"
    r"|\bstay away\b|\bkeep away\b|\bnot for (?:eating|touching|playing)\b"
    r"|\bonly (?:for|from) (?:grown ?ups|adults|big people|mommy|daddy|doctors)\b"
)
# "It isn't dangerous", "it won't hurt", "no need to be careful": reassurance, not a warning.
_FALSE_WARNING = re.compile(
    r"\b(?:isn'?t|aren'?t|is not|are not|not|nothing|no|no need to be)\b (?:so |very |really |that |too |even )?"
    r"(?:dangerous|danger|unsafe|poisonous|harmful|hot|sharp|careful|scary)\b"
    r"|\b(?:won'?t|will not|can'?t|cannot|can not|wouldn'?t|would not|doesn'?t|does not|don'?t|do not|didn'?t|"
    r"never|not)\b (?:\w+ )?(?:hurt|burn|cut|poison|harm|make you sick)\b"
    r"|\b(?:it'?s|it is|that'?s|that is|they'?re|they are|totally|perfectly) (?:totally |perfectly |very |quite |"
    r"really |super |always )?(?:safe|okay|ok|fine|good|allowed) to\b"
)
_INVITATION = re.compile(
    r"\b(?:let'?s|let us|can you|could you|will you|would you|try|you should|you can\b(?!'?t)|how about|shall we|"
    r"want to|wanna|go ahead|time to|remember to|don'?t forget)\b"
)
# A clause that *tells the child to do* something risky ("touch it", "now grab the knife").
_IMPERATIVE_HAZARD = re.compile(
    r"^(?:(?:please|now|quick|quickly|go|come|just|then|ok|okay|you|yes|really|and|so|oh)\s+)*"
    + _HAZARD_VERBS + r"\b"
)
_CLAUSE_SPLIT = re.compile(r"[.!?;:,]+|\s+(?:and|but|then|so|or|because|plus|while|until)\s+")


def directs_to_grownup(text: str) -> bool:
    """True if a sentence urgently sends the child to a grown-up ("Tell a grown-up right now!")."""
    for sentence in _split_sentences(text) or [text]:
        for v in _variants(sentence):
            if any(p.search(v) for p in _GROWNUP_NOW):
                return True
    return False


def _hazards_only_in_warnings(text: str) -> bool:
    """Every mention of a hazard sits in a clause that warns against it (and invites nothing)."""
    rest: list[str] = []
    for clause in _CLAUSE_SPLIT.split(_fold(_strip_invisible(text or ""))):
        norm = _flatten(clause)
        if not norm:
            continue
        if any(p.search(norm) for p in _GROWNUP_NOW):
            if find_blocked(norm):  # "get a grown-up now and grab the knife" stays blocked
                return False
            continue
        if _INVITATION.search(norm) or _IMPERATIVE_HAZARD.search(norm) or _FALSE_WARNING.search(norm):
            return False
        if _WARNING.search(norm):
            continue
        rest.append(norm)
    return not find_blocked(". ".join(rest))


def _keep_grownup_sentences(text: str, limit: int) -> str | None:
    """Fit ``text`` into ``limit`` words without losing the "get a grown-up" sentence."""
    final, _ = truncate_words(text, limit)
    if directs_to_grownup(final):
        return final
    urgent = " ".join(s for s in _SENTENCE_SPLIT.split(text.strip()) if directs_to_grownup(s))
    final, _ = truncate_words(urgent, limit) if urgent else ("", False)
    return final if final and directs_to_grownup(final) else None


@dataclass(frozen=True)
class ValidationResult:
    ok: bool  # False -> the reply was blocked and ``text`` is a safe fallback
    text: str  # what LittleBot may say
    reasons: list[str] = field(default_factory=list)  # block categories and/or cleaning notes
    # Set (to a category label, e.g. "danger") when the reply talked about a hazard while urgently sending
    # the child to a grown-up: the engine then alerts the parent (label only) and never answers cheerfully.
    concern: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.ok

    @property
    def modified(self) -> bool:
        return bool(self.reasons)


def validate_reply(text: str, age_policy: _AgePolicyLike | None = None, max_words: int = 30, *,
                   allow: Iterable[str] = ()) -> ValidationResult:
    """Clean + check a model reply. Blocked replies are replaced by a safe fallback.

    Good safety advice is not thrown away: a reply whose only problem is that it *mentions* a
    hazard ("Swallowing a battery is not safe. Tell a grown-up right now!") is spoken when every
    hazard mention is a warning and the reply urgently sends the child to a grown-up. If such a
    reply cannot be spoken safely it is replaced by a gentle "Let's find a grown-up right now."
    (never a cheerful game). Both cases set ``concern`` so the parent is alerted.
    """
    limit = age_policy.max_reply_words(max_words) if age_policy is not None else max(3, int(max_words))
    raw = text or ""
    cleaned, reasons = clean_text(raw)
    blocked = find_blocked(raw)
    if cleaned != raw:
        blocked += [c for c in find_blocked(cleaned) if c not in blocked]
    if "url_removed" in reasons and "links" not in blocked:
        blocked.append("links")  # LittleBot never points a child at the internet
    if allow:  # rule groups the parent switched off (ai.relaxed_rules), e.g. "links" so it can talk about YouTube
        allowed = set(allow)
        blocked = [c for c in blocked if c not in allowed]
    if not blocked and not cleaned.strip(" .!?,"):
        blocked.append("empty")
    if blocked and _DANGER_CATEGORIES.intersection(blocked) and directs_to_grownup(cleaned):
        if blocked == ["unsafe"] and _hazards_only_in_warnings(cleaned):
            final = _keep_grownup_sentences(cleaned, limit)
            if final is not None:
                notes = reasons + ["safety_advice"] + (["truncated"] if final != cleaned else [])
                return ValidationResult(True, final, notes, concern="danger")
        return ValidationResult(False, grownup_fallback(limit), blocked + reasons + ["grownup_redirect"],
                                concern="danger")
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


_PEOPLE_NOUNS = (
    r"(?:mommy|mummy|mom|mum|mama|mamma|momma|daddy|dad|papa|dada|grandma|granny|nana|grandpa|grandad|granddad|"
    r"teacher|nanny|babysitter|auntie|aunt|uncle|brother|sister|cousin|friend|best friend|neighbou?r|dog|doggy|"
    r"cat|kitty|puppy|pet|baby|step ?mom|step ?mum|step ?dad|boyfriend|girlfriend)"
)
# "my mommy's name is Sarah Jones", "her name is Sarah", "my mum is called Sarah"
_OTHER_NAME_INTRO = re.compile(
    r"\b((?i:(?:(?:my|our|the)\s+)?" + _PEOPLE_NOUNS + r"(?:'?s)?\s+(?:full\s+|last\s+|first\s+|real\s+|middle\s+)?"
    r"name(?:\s+is|'s|s)?|(?:my|our)\s+" + _PEOPLE_NOUNS + r"\s+is\s+called|(?:his|her|their)\s+"
    r"(?:full\s+|last\s+|first\s+|real\s+)?name(?:\s+is|'s|s)?))\s+([A-Za-z][\w'-]*(?:\s+[A-Z][\w'-]*){0,2})"
)
# "my address is twelve oak street", "my house is number 12": scrub the rest of the clause.
_ADDRESS_IS = re.compile(
    r"\b((?i:(?:my|our)\s+(?:home\s+|house\s+|full\s+)?address\s+is|(?:my|our)\s+(?:house|flat|apartment|door)\s+"
    r"(?:is\s+)?number|(?:i|we)\s+live\s+at\s+number))\s+[^.!?;\n]{1,60}",
)
_SPELLED_BIG = (r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
                r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
                r"hundred)")
_SPELLED_ADDRESS = re.compile(
    r"\b" + _SPELLED_BIG + r"(?:[\s-]+" + _SPELLED_BIG + r"){0,3}\s+(?:[A-Za-z][\w'-]*\s+){1,3}"
    r"(?:street|avenue|road|lane|drive|close|crescent|court|boulevard|terrace|grove)\b",
    re.I,
)
# "we live in Springfield", "I'm from New York": a place name (not "we live in a house").
_LIVE_IN = re.compile(r"\b((?i:(?:i|we|they|he|she|my\s+\w+)\s+(?:live|lives|living|stay|stays)\s+in|"
                      r"(?:i'?m|i\s+am|we'?re|we\s+are)\s+from))\s+([^.,!?;\[\]\n]+)")
_PLACE_STOP = re.compile(r"\s+(?:and|with|but|because|near|by|next|where|when|so|or|too|now|it|its|it's)\b.*$",
                         re.I | re.S)
_NOT_A_PLACE = frozenset({
    "a", "an", "the", "our", "my", "this", "that", "one", "two", "house", "home", "flat", "apartment", "big", "little",
    "small", "tall", "red", "blue", "green", "yellow", "white", "nice", "cosy", "cozy", "new", "old", "here", "there",
    "it", "space", "outer", "upstairs", "downstairs", "bed", "town", "city", "country", "countryside", "village",
    "forest", "woods", "jungle", "sea", "ocean", "water", "castle", "tree", "nest", "zoo", "farm", "barn", "hole",
    "pond", "cave", "box", "tent", "igloo", "cloud", "clouds", "sky",
})
# "I'm Lily" (not "I'm happy"): only a Capitalised word that is not a feeling / state / age.
_IM_NAME = re.compile(r"\b((?i:i'?m|i\s+am))\s+([A-Z][\w'-]*)(\s+[A-Z][\w'-]*)?")
_NOT_A_NAME = frozenset({
    "happy", "sad", "tired", "hungry", "thirsty", "sleepy", "cold", "hot", "hurt", "bored", "scared", "angry", "mad",
    "grumpy", "silly", "done", "ready", "okay", "ok", "fine", "good", "great", "sick", "poorly", "cross", "excited",
    "lonely", "lost", "stuck", "big", "little", "small", "tall", "strong", "fast", "not", "so", "very", "really",
    "just", "here", "back", "home", "going", "playing", "coming", "eating", "sorry", "a", "an", "the", "four", "five",
    "three", "two", "six", "seven", "one", "eight", "nine", "ten", "years", "only", "afraid", "frightened",
    "worried", "upset", "shy", "brave", "cool", "warm", "wet", "dirty", "clean", "naughty", "nice", "kind", "busy",
    "full", "finished", "awake", "up", "down", "in", "out", "on", "off", "too", "also", "still", "almost", "nearly",
    "your", "my", "his", "her", "their", "our", "like", "with", "at", "from", "gonna", "going", "pretending",
    "dancing", "singing", "jumping", "running", "hiding", "sitting", "sleeping", "ill", "unwell", "dizzy", "fed",
    "bigger", "older", "taller", "superhero", "super", "princess", "dinosaur", "robot", "lion", "tiger", "monster",
    "it", "it's", "now", "yes", "no", "frustrated", "proud", "glad", "fantastic", "awesome", "the", "best", "first",
    "last", "winning", "winner", "teacher", "mommy", "daddy", "baby", "friend", "learning", "trying", "boy", "girl",
    "kid", "child", "toddler", "sure", "feeling", "being", "doing", "making", "drawing", "reading", "painting",
    "colouring", "coloring", "building", "cooking", "helping", "laughing", "crying", "giggling", "talking",
})


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
    sub(_ADDRESS_IS, lambda m: f"{m.group(1)} [address]", "address")
    sub(_SPELLED_ADDRESS, "[address]", "address")
    sub(_ADDRESS, "[address]", "address")
    sub(_LIVE_AT, lambda m: f"{m.group(1)} {m.group(2)} [address]", "address")
    sub(_UK_POSTCODE, "[address]", "address")

    def _live_in(m: re.Match[str]) -> str:
        place, tail = m.group(2), ""
        stop = _PLACE_STOP.search(place)
        if stop:
            place, tail = place[:stop.start()], place[stop.start():]
        words = place.split()
        if not words:
            return m.group(0)
        capitalised = any(w[:1].isupper() for w in words[:4])
        if not capitalised and words[0].lower().strip("'") in _NOT_A_PLACE:
            return m.group(0)
        if len(words) > 4:
            tail = " " + " ".join(words[4:]) + tail
        note("place")
        return f"{m.group(1)} [place]{tail}"

    t = _LIVE_IN.sub(_live_in, t)

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
    sub(_OTHER_NAME_INTRO, lambda m: f"{m.group(1)} [name]", "name")

    def _im_name(m: re.Match[str]) -> str:
        if m.group(2).lower().strip("'") in _NOT_A_NAME:
            return m.group(0)
        note("name")
        second = m.group(3) or ""
        keep = second if second and second.strip().lower() in _NOT_A_NAME else ""
        return f"{m.group(1)} [name]{keep}"

    t = _IM_NAME.sub(_im_name, t)

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

# Anyone who might hurt, touch or frighten a child ("daddy hit me", "my uncle touched me", "he hurt me").
_ANYONE = (
    r"(?:(?:my|the|a|an|our|your|that|this|some|his|her) )?(?:someone|somebody|man|men|lady|woman|women|person|"
    r"stranger|guy|boy|girl|kid|kids|he|she|they|mommy|mummy|mom|mum|mama|mamma|momma|daddy|dad|papa|dada|"
    r"parents?|grandma|granny|nana|grandpa|grandad|granddad|grandpa|teacher|nanny|babysitter|auntie|aunt|uncle|"
    r"brother|sister|cousin|friend|neighbou?r|coach|step ?dad|step ?mom|step ?mum|boyfriend|girlfriend|"
    r"(?:mommy|mummy|mom|mum|daddy|dad)'?s (?:friend|boyfriend|girlfriend))"
)
_ABUSE_VERBS = (
    r"(?:hit|hits|hitted|hitting|hurt|hurts|hurted|hurting|touched|touches|touch|touching|kicked|kicks|kicking|bit|"
    r"bites|bited|biting|grabbed|grabs|grabbing|pushed|pushes|pushing|smacked|smacks|smacking|slapped|slaps|"
    r"slapping|spanked|spanks|spanking|punched|punches|punching|pinched|pinches|pinching|choked|chokes|choking|"
    r"strangled|burned|burnt|burns|shook|shakes|shaked|locked|tied|licked|scratched|whacked|beat|beats|beated|"
    r"beating|bashed|threw something at)"
)
# Things a toddler must never swallow (button batteries and magnets are life-threatening).
_SWALLOW_ITEMS = (
    r"(?:batter(?:y|ies)|button batter(?:y|ies)|coins?|pennies|penny|money|magnets?|beads?|buttons?|marbles?|soap|"
    r"bleach|pills?|medicines?|medication|tablets?|vitamins?|gummies|cough syrup|cleaner|cleaning \w+|detergent|"
    r"washing (?:pod|pods|powder|liquid|up liquid|tablet|tablets)|dishwasher (?:tablet|tablets|pod|pods)|"
    r"laundry (?:pod|pods)|pods?|chemicals?|poison\w*|glue|paint|shampoo|perfume|nail polish|polish|lotion|"
    r"bubble bath|bubble (?:mix|liquid|stuff|solution)|mouthwash|(?:nappy|diaper|sun|face|hand) cream|petrol|"
    r"gasoline|alcohol|beer|wine|vodka|cigarettes?|vape|lighter fluid|liquid|screws?|pins?|needles?|tacks?|"
    r"staples?|earrings?|keys?|rocks?|stones?|lego|broken glass|plastic|sponge|toadstools?|wild mushrooms?|"
    r"berries from (?:the )?(?:garden|bush|tree|outside)|silica|desiccant|anti ?freeze|weed killer|slug pellets?|"
    r"rat poison|bug spray)"
)
_SWALLOW_VERBS = (
    r"(?:ate|eated|eaten|eat|eats|eating|swallowed|swallow|swallows|swallowing|swallowd|drank|drinked|drunk|drink|"
    r"drinks|drinking|licked|lick|licking|chewed|chew|chewing|bit|bited|sucked|suck|sucking|tasted|taste|gulped|"
    r"munched|sipped)"
)
_KIDS = (r"(?:(?:the|my|our|a|your|that|this) )?(?:baby|babies|sister|brother|sissy|bubba|someone|somebody|friend|"
         r"cousin|twin|kid|child)")
_WATER = (r"(?:pool|pools|swimming pool|paddling pool|bath|bathtub|bath tub|tub|water|pond|lake|river|sea|ocean|"
          r"hot tub|fountain|canal|stream)")
_LURE = (r"(?:candy|candies|sweets?|sweeties|lolly|lollies|lollipops?|chocolate|puppy|puppies|kittens?|presents?|"
         r"toys?|surprise|ice cream|money|treat|treats)")
_STRANGER = r"(?:man|men|lady|ladies|woman|women|person|people|guy|someone|somebody|stranger|strangers)"

# Order matters: the first matching category wins. The category name is the only thing the parent
# dashboard ever sees (never the child's words).
CONCERN_RULES: dict[str, list[str]] = {
    # Self-harm, abuse disclosures, grooming secrets.
    "safety": [
        r"\b(?:i want to|i wanna|want to|wanna) (?:die|hurt myself|kill myself)\b",
        r"\b(?:kill|hurt|cut) myself\b(?!.{0,3}\b(?:by accident|accidentally)\b)",
        r"\bi hate myself\b",
        r"\b(?:nobody|no one|no body) loves me\b",
        # "daddy hit me", "he keeps hitting me", "my uncle touched me"
        r"\b" + _ANYONE + r" (?:\w+ )?" + _ABUSE_VERBS + r" me\b",
        r"\b" + _ANYONE + r" (?:\w+ )?(?:hit|hurt|kicked|bit|punched|smacked|slapped|pinched|burned|burnt|scratched|"
        r"twisted|choked|whacked) my\b",
        r"\btouch(?:ed|es|ing)? (?:me )?(?:on |in )?my (?:private|privates|private parts|bottom|bum|butt|willy|"
        r"pee pee|wee wee|front bottom|vagina|penis|down there|chest|nipples?)\b",
        r"\b(?:made|make|makes|making) me (?:touch|kiss|lick|take off|undress|show)\b",
        r"\b(?:told|asked|wants?|wanted) me to (?:take off|undress|kiss|lick|touch (?:his|her|their))\b",
        r"\bshowed me (?:his|her|their) (?:private|privates|private parts|willy|bottom|bum|pee pee|wee wee)\b",
        r"\b(?:told|tells|said|says) (?:me )?(?:to )?(?:keep|it'?s|its|it is) (?:a |our )?(?:special |little )?"
        r"secret\b",
        r"\bour (?:special |little )?secret\b",
        r"\bdon'?t tell (?:my |your )?" + _GROWNUPS + r"\b",
        r"\bsaid (?:i|we) (?:can'?t|cannot|mustn'?t|must not|shouldn'?t|should not) tell\b",
    ],
    # Water danger: drowning takes seconds and is silent.
    "water": [
        r"\b" + _KIDS + r"(?:'?s)?(?: (?:is|are|was|went|got|fell|jumped|slipped|has|gone|still|fallen|all|alone|"
        r"by herself|by himself))* (?:in|into|in to|under|inside) "
        r"(?:the |a |our |my |that )?(?:\w+ )?" + _WATER + r"\b",
        r"\b(?:fell|falled|fallen|falls|slipped|tripped|pushed|went under) (?:in|into|in to|under) "
        r"(?:the |a |our )?(?:\w+ )?" + _WATER + r"\b",
        r"\bdrown\w*\b",
        r"\b(?:face|head) (?:is |down )?(?:in|under) (?:the )?water\b",
        r"\bcan'?t get out (?:of )?(?:the |a )?(?:\w+ )?" + _WATER + r"\b",
    ],
    # Swallowed / drank something dangerous.
    "swallowed": [
        r"\b" + _SWALLOW_VERBS + r"\b (?:[\w']+ ){0,3}?" + _SWALLOW_ITEMS + r"\b",
        r"\b(?:took|taken|had|got into|opened|found)\b (?:[\w']+ ){0,3}?(?:pills?|medicines?|medication|"
        r"tablets?|vitamins?|gummies|cough syrup|syrup)\b",
        r"\bgot into (?:the |mommy'?s |mummy'?s |daddy'?s |\w+'?s )?(?:medicine|pills|cleaning|bleach|chemicals|"
        r"cupboard under the sink)\b",
        r"\b(?:put|putted|stuck|pushed|shoved|popped)\b (?:[\w']+ ){0,4}?(?:in|into|in to|inside|down) "
        r"(?:my|his|her|the baby'?s|baby'?s|their) mouth\b",
        r"\b" + _SWALLOW_ITEMS + r" (?:is |are |went )?(?:in|inside|down) (?:my|his|her|the baby'?s|baby'?s) "
        r"(?:mouth|tummy|belly)\b",
    ],
    "emergency": [
        r"\bfire\b(?! ?(?:truck|trucks|engine|engines|fighter|fighters|man|men|station|works|fly|flies|place|hat|dog))",
        r"\b(?:smoke|smoky|burning)\b",
        r"\b(?:can'?t|cannot|can not|couldn'?t|could not) (?:breathe|breath|breathing)\b",
        r"\b(?:not|isn'?t|is not|stopped|stop|no) breath\w*\b",
        r"\bchok(?:e|es|ing|ed)\b",
        r"\bstuck in (?:my|his|her|the baby'?s|baby'?s|their) throat\b",
        r"\b(?:something|it|food) (?:is )?(?:in|stuck in) (?:my|his|her) throat\b",
        r"\b(?:turning|turned|gone|going) blue\b|\blips (?:are |is |went |gone )?blue\b",
        r"\b(?:won'?t|will not|doesn'?t|does not|isn'?t|is not|not|can'?t) wak(?:e|ing) up\b",
        r"\b(?:call|get) (?:the )?(?:police|ambulance|doctor|911|999|112)\b",
        r"\b(?:911|999|112|emergency)\b",
        r"\b" + _CARERS + r" (?:is |got |has )?(?:hurt|bleeding|fell|fallen|not moving|on the floor|sick)\b",
        r"\bhelp help\b",
        r"\b(?:finger|fingers|fork|key|knife|something|it|pen|pin) in (?:the |a )?(?:socket|outlet|plug|power point)\b",
        r"\b(?:got|get|getting) (?:a )?(?:shock|electric shock|zapped|electrocuted)\b",
        r"\b(?:found|find|got|have|has|holding|hold|playing with|there'?s|there is) (?:a |an |the |some |my |his |"
        r"her |[\w]+'s )?(?:real )?(?:gun|guns|pistol|knife|knives|razor|razors|lighter|lighters|matches)\b",
        r"\b(?:fell|falled|fallen|fall|falling) (?:out of|out|off|from) (?:the |a )?(?:window|balcony|roof)\b",
    ],
    # Stranger lures: "a man said come with me", "get in his car".
    "stranger": [
        r"\bstrangers?\b",
        r"\b(?:" + _STRANGER + r"|he|she|they)\b.{0,25}\b(?:said|says|say|told me|tells me|asked me|asks me|"
        r"wants me|wanted me|want me|tried)\b.{0,15}\b(?:come with|go with|follow|go away with|go home with|"
        r"come to his|come to her|come to their)\b",
        r"\b" + _STRANGER + r"\b.{0,25}\b(?:said|says|told me|asked me|wants me|wanted me|tried)\b.{0,15}"
        r"\b(?:get in|get into)\b",
        r"\b" + _STRANGER + r"\b.{0,30}\bcome with (?:me|him|her|them|us)\b",
        r"\bcome with me\b.{0,30}\b(?:" + _LURE + r"|car|van|secret)\b",
        r"\b(?:get|got|getting|go|went|going|climb|climbed|hop|hopped|come|came) (?:in|into|in to) (?:his|her|their|"
        r"a man'?s|the man'?s|a lady'?s|the lady'?s|someone'?s|somebody'?s|a stranger'?s|the stranger'?s|"
        r"a strange) (?:\w+ )?(?:car|van|truck)\b",
        r"\b" + _STRANGER + r"\b (?:\w+ ){0,3}?(?:gave|give|gives|giving|offered|offers|offer|showed|show|"
        r"promised) (?:me )?(?:some |a |his |her )?" + _LURE + r"\b",
        r"\b(?:a|the|that|some|this|strange|funny|scary|big) (?:man|lady|woman|person|guy|men|people)\b "
        r"(?:\w+ ){0,3}?(?:took me|grabbed me|followed me|following me|picked me up|carried me|tried to (?:take|"
        r"grab|pick up|carry) me|watching me|watched me|looking at me|took (?:a )?(?:picture|pictures|photo|photos) "
        r"of me|wants to take me|touched me)\b",
        r"\b(?:went|go|going|walked|left) (?:off |away )?with (?:a|the|that|some) (?:man|lady|woman|person|guy|"
        r"stranger)\b",
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
        r"\b(?:bit|bited|bitten|stung|scratched|burned|burnt|cut|zapped) me\b",
        r"\bi(?:'?m| am| got| was| just got) (?:\w+ )?(?:bit|bitten|stung|burned|burnt|scalded)\b",
        r"\bi(?:'?m| am| feel| feeling)? (?:so |really |very |a bit |a little )?(?:sick|poorly|ill|unwell|dizzy)\b",
        r"\b(?:threw|throw|thrown|throwing) up\b|\bvomit\w*\b|\bi (?:was|been|did) sick\b",
        r"\bi (?:touched|touch) (?:the |a )?(?:hot \w+|stove|oven|iron|kettle|hob|heater|radiator|fire|fireplace|"
        r"candle)\b",
        r"\b(?:in|up|inside) (?:my|his|her|the baby'?s|baby'?s) (?:nose|nostril|nostrils|ear|ears|eye|eyes)\b",
    ],
    "lost": [
        r"\bi(?:'?m| am)? lost\b",
        r"\bi can'?t find (?:my )?" + _PARENT_WORDS + r"\b",
        r"\bwhere(?:'?s| is| are) (?:my )?" + _PARENT_WORDS + r"\b",
        r"\bwhere am i\b",
        r"\bi don'?t know where (?:my )?(?:" + _PARENT_WORDS + r"|i am|i'?m)\b",
    ],
    "scared": [
        r"\bi(?:'?m| am| feel| got| get)? (?:so |really |very )?(?:scared|afraid|frightened|terrified)\b",
    ],
    # A plain call for help ("Help!", "help me", "I'm stuck"), but not "help me count to ten".
    "help": [
        r"^(?:please )?(?:help|helps|halp)(?: (?:help|me|please|now|someone|somebody|mommy|mummy|mom|mum|daddy|"
        r"dad))*$",
        r"\bhelp me(?: (?:please|now|right now|quick|quickly|someone|somebody|anyone|help|mommy|mummy|mom|mum|"
        r"mama|daddy|dad|papa))*$",
        r"\b(?:someone|somebody|anyone|anybody) (?:please )?help\b",
        r"\bi need (?:some )?help(?: (?:please|now|right now|quick|quickly))*$",
        r"\bi(?:'?m| am) (?:stuck|trapped)\b",
        r"\bi (?:can'?t|cannot|can not) get (?:out|down|free)\b",
    ],
}

# category -> (reply, short reply <= 6 words). Never cheerful: no games, no "yay", no "fun".
_CONCERN_REPLIES: dict[str, tuple[str, str]] = {
    "safety": ("Thank you for telling me. Let's tell a grown-up you trust right now.",
               "Let's tell a grown-up now."),
    "water": ("Get a grown-up right now! Shout for help!", "Get a grown-up right now!"),
    "swallowed": ("Uh oh. Please tell a grown-up right now.", "Tell a grown-up right now."),
    "emergency": ("Let's get a grown-up right now! Call for help!", "Get a grown-up right now!"),
    "stranger": ("Thank you for telling me. Let's find your grown-up and tell them right now.",
                 "Find your grown-up right now."),
    "hurt": ("Oh no! Let's find a grown-up to help you right now.", "Oh no! Let's find a grown-up!"),
    "lost": ("Let's find a grown-up who can help you.", "Let's find a grown-up!"),
    "scared": ("It's okay. Let's find a grown-up for a big hug.", "Let's find a grown-up hug."),
    "help": ("I hear you. Let's get a grown-up to help you right now.", "Let's get a grown-up now."),
}

CONCERN_CATEGORIES: tuple[str, ...] = tuple(CONCERN_RULES)

_COMPILED_CONCERN: dict[str, list[re.Pattern[str]]] = {
    cat: [re.compile(p) for p in pats] for cat, pats in CONCERN_RULES.items()
}


@dataclass(frozen=True)
class InputConcern:
    # safety | water | swallowed | emergency | stranger | hurt | lost | scared | help
    # (the category is the only thing reported to parents)
    category: str
    reply: str
    short_reply: str

    def reply_for(self, max_words: int) -> str:
        text = self.reply if word_count(self.reply) <= max_words else self.short_reply
        return truncate_words(text, max_words)[0]


_MAX_CLASSIFY_CHARS = 2000


def classify_child_input(text: str) -> InputConcern | None:
    """Spot obviously concerning input. Returns None for ordinary chatter."""
    variants = _all_variants((text or "")[:_MAX_CLASSIFY_CHARS])
    for cat, patterns in _COMPILED_CONCERN.items():
        if any(p.search(v) for p in patterns for v in variants):
            reply, short = _CONCERN_REPLIES[cat]
            return InputConcern(cat, reply, short)
    return None
