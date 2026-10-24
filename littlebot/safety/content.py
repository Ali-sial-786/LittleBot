"""AI content safety: validate replies, scrub personal info, flag concerning child input.

Everything here is enforced in code, independent of whatever the system prompt
asks the model to do (see docs/AI_SAFETY.md):

* ``validate_reply``      - every reply (cloud *and* scripted) passes through it before
                            LittleBot speaks. Markdown / emoji / code are stripped, a link is
                            said as its site's name ("nhs dot uk"), length is capped at the
                            parent's ``ai.max_words`` at a sentence boundary, and a reply
                            matching a block rule is replaced by a friendly fallback. The rules
                            keep only what could really harm a small child (``BLOCK_RULES``);
                            facts, news, health and parenting answers, brands, prices, websites
                            and ordinary warmth all pass. A reply that only *warns* about a hazard
                            ("Don't touch the stove, it's hot!") is spoken. One that urgently
                            sends the child to a grown-up about a hazard is spoken when it only
                            warns ("Batteries are not food. Tell a grown-up right now!"), otherwise
                            replaced by the gentle ``GROWNUP_FALLBACK`` (never a game); either way
                            ``concern`` is set so the engine alerts the parent.
* ``sanitize_user_text``  - scrubs phone numbers, e-mails, street addresses, long numbers,
                            "my name is ...", "I'm Lily", "my mommy's name is ...", "we live in
                            <place>", school names and known names from the child's words before
                            anything is sent to a cloud provider.
* ``classify_child_input`` - spots obviously concerning input (``CONCERN_RULES``: safety /
                            water / swallowed / emergency / stranger / hurt / lost / scared /
                            help) so the engine can alert the parent with a category label only,
                            and answer with a gentle "let's get a grown-up right now" line (never
                            cheerful) whenever the cloud model can't answer instead.

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
Keep the rules narrow: LittleBot is also the family's home assistant, so a rule should catch
something said *to a child* that could hurt them ("let's climb the bookcase"), not a fact a
grown-up asked for ("babies start to climb stairs at about a year"). Start such a rule with
``_DO`` (the start of a sentence or clause, or an invitation such as "let's" / "can you") so it
matches an instruction, not a mention. A false positive costs a real answer, and in a live
voice call it can end the call.
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
    "grownup_fallback", "directs_to_grownup", "DOSE_FALLBACK", "DEFAULT_MAX_WORDS", "fixed_lines",
]

DEFAULT_MAX_WORDS = 60  # ai.max_words: room for a home-assistant answer of two to four sentences


class _AgePolicyLike(Protocol):
    def max_reply_words(self, configured: int = DEFAULT_MAX_WORDS) -> int: ...


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
# Clause breaks inside a sentence: a comma, a dash, or "but" / "then" ("Oh look, climb the bookcase").
_CLAUSE_BREAK = re.compile(r"(?<=,)\s+|\s+[-–—]+\s+|\s+(?=(?:but|then)\s)")


def _split_sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in _SENTENCE_END.split(_strip_invisible(text or ""))) if s]


def _all_variants(text: str, *, clauses: bool = False) -> list[str]:
    """Variants of the whole text plus of each sentence (and, with ``clauses``, each clause) on its own.

    Rules anchored with ``^`` / ``$`` (e.g. a reply that *starts* with "Hop in the bath", or a
    child who just says "Help!") then work per sentence. Reply checks also look at each clause, so
    an instruction after a comma ("Okay sweetie, now climb the bookcase") counts as starting one.
    These variants only ever add matches, never remove one.
    """
    out = _variants(text)
    pieces = _split_sentences(text)
    if clauses:
        pieces += [c for s in pieces for c in (p.strip() for p in _CLAUSE_BREAK.split(s)) if c and c not in pieces]
    if 1 < len(pieces) <= 60:
        for piece in pieces:
            for form in _variants(piece):
                if form not in out:
                    out.append(form)
    return out


# ---------------------------------------------------------------------------
# Reply block rules  (category -> regexes over normalised text)
# ---------------------------------------------------------------------------
# Only what could really harm a small child is blocked: grooming (secrets from grown-ups, turning the
# child away from them, meeting someone), asking the child for personal details, instructions or
# invitations to do something dangerous, sexual content, gore, self-harm, hate slurs, and medicine doses
# for a child. The hazard rules match an *instruction to the listener* ("let's climb the bookcase",
# "touch the stove"), never a mention ("preheat the oven to 180", "babies love to climb stairs").
# Medical and development information, brands, prices, websites and apps, ordinary warmth ("I love
# you") and fairy-tale monsters are all allowed.

_PARENT_WORDS = (
    r"(?:mommy|mummy|mom|mum|mama|mamma|momma|daddy|dad|papa|dada|parents?|grandma|granny|nana|"
    r"grandpa|grandad|granddad|teacher|nanny|babysitter|auntie|aunt|uncle)"
)
_GROWNUPS = r"(?:mom|mommy|mum|mummy|mama|dad|daddy|papa|parents?|grown ?ups?|family|teacher)"
# Anyone a child might be told to keep something from.
_TELL_TARGETS = (r"(?:" + _PARENT_WORDS + r"|grown ?ups?|family|anyone|anybody|any one|someone|somebody|people|"
                 r"them|him|her|nobody|no ?one)")
# Furniture / high places a toddler can fall from.
_FALL_PLACES = (
    r"(?:beds?|bunk ?beds?|sofas?|couch(?:es)?|settees?|tables?|chairs?|counters?|worktops?|stools?|stairs|steps|"
    r"cribs?|cots?|shelf|shelves|window ?sills?|windows?|dressers?|furniture|roofs?|walls?|balcon(?:y|ies)|"
    r"railings?|bath|bathtub|bath tub|toilet|fridge|wardrobes?|bookcases?|cupboards?|cabinets?|banisters?)"
)
# Water a toddler can drown in (even a few centimetres).
_WATER_PLACES = (
    r"(?:baths?|bathtubs?|bath tub|tubs?|ponds?|pools?|swimming pool|paddling pool|hot tub|lakes?|rivers?|sea|"
    r"ocean|water|fountains?|wells?|streams?|creeks?|buckets?|canal|toilets?)"
)
# Small spaces a child can get shut inside.
_TRAP_PLACES = (
    r"(?:dryer|tumble dryer|washing machine|washer|fridge|freezer|oven|dishwasher|cupboards?|closets?|wardrobes?|"
    r"trunks?|chests?|toy ?box(?:es)?|box(?:es)?|cars?|boot|cabinets?|suitcases?|bags?|cooler|baskets?|drawers?|"
    r"bins?|bath|bathtub|tub)"
)
# Words that invite the listener to do something, anywhere in a sentence ("why don't we ...").
_INVITE = (r"(?:let'?s|let us|can you|could you|will you|would you|you can|you could|you should|you must|"
           r"you need to|we can|we could|we should|shall we|why don'?t you|why don'?t we|how about we|"
           r"how about you|do you want to|do you wanna|wanna|time to|go ahead and|i dare you to|dare you to|"
           r"(?:it|that|this)(?:'?s| is) (?:totally |perfectly |really |very |quite )?(?:okay|ok|fine|safe|alright|"
           r"all right|good|fun) to|(?:it|that|this) (?:isn'?t|is not|'?s not|won'?t be) (?:\w+ )?(?:dangerous|scary|"
           r"bad|harmful) to)")
# The start of a sentence or clause, with the little words an instruction may open with ("Okay, now climb").
_LEAD = (r"^(?:(?:please|now|ok|okay|so|and|but|then|just|quick|quickly|hey|oh|look|yes|yeah|wow|ooh|first|next|"
         r"go|go on|go and|come|come on|come and|try|try to|alright|all right|right|sweetie|honey|buddy|"
         r"little one)\s+)*")
# An instruction or an invitation to do what follows.
_DO = r"(?:" + _LEAD + r"|\b" + _INVITE + r"\s+(?:(?!not\b|never\b)\w+\s+){0,2}?)"
# Like _DO, without "you can / you could": "careful, you could hurt the baby" is a warning, not an urge.
_URGE = (r"(?:let'?s|let us|can you|could you|will you|would you|you should|you must|you need to|we should|"
         r"shall we|why don'?t you|why don'?t we|how about we|do you want to|do you wanna|wanna|time to|"
         r"go ahead and|i dare you to|dare you to)")
_URGE_DO = r"(?:" + _LEAD + r"|\b" + _URGE + r"\s+(?:(?!not\b|never\b)\w+\s+){0,2}?)"
_MEDICINE = r"(?:medicines?|medication|pills?|tablets?|vitamins?|gummies|cough syrup|drugs)"
_SWALLOW_HAZARDS = (
    r"(?:batter(?:y|ies)|magnets?|coins?|beads?|buttons?|marbles?|soap|bleach|detergent|washing (?:pods?|powder|"
    r"liquid|up liquid)|laundry pods?|dishwasher (?:tablets?|pods?)|cleaner|cleaning (?:spray|stuff|products?)|"
    r"chemicals?|poison\w*|wild (?:mushrooms?|berries)|toadstools?|glue|paint|petrol|gasoline|anti ?freeze|"
    r"lighter fluid|cigarettes?|alcohol|beer|wine|vodka|whisky|whiskey)"
)
_STRANGLE_THINGS = (r"(?:string|strings|cords?|ropes?|ribbons?|belts?|leads?|leash|wires?|cables?|chains?|yarn|thread|"
                    r"shoe ?laces?|laces?|elastic|rubber bands?|tape|plastic bags?)")
# For "medicine_dose": a child, a medicine and an amount in the same reply.
_CHILD_WORDS = (r"(?:baby|babies|infants?|newborns?|toddlers?|child|children|kids?|little ones?|"
                r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|eighteen) (?:weeks?|months?)|"
                r"(?:\d+|one|two|three|four|five|six) years? old|(?:week|month|year) old)")
_DOSE_MEDICINES = (r"(?:medicines?|medication|meds|paracetamol|acetaminophen|ibuprofen|tylenol|calpol|motrin|advil|"
                   r"nurofen|aspirin|benadryl|piriton|antihistamines?|melatonin|cough (?:syrup|medicine)|syrup|"
                   r"suspension|drops|tablets?|pills?|capsules?|suppositor(?:y|ies)|doses?|dosage|dosing)")
_AMOUNT = (r"(?:\d+(?: \d+)?|half a|a half|half|a quarter of a|quarter of a|one|two|three|four|five|six|seven|"
           r"eight|nine|ten|fifteen|twenty)(?: and a half)? ?(?:ml|mls|millilit(?:er|re)s?|mg|milligrams?|mcg|"
           r"micrograms?|teaspoons?|tsp|tablespoons?|tbsp|drops?|tablets?|pills?|capsules?|sachets?|spoonfuls?|"
           r"puffs?|cc)\b")
# Hate slurs (words that are never needed in an answer).
_SLURS = (r"(?:nigg(?:er|ers|a|as|ah|uh)|sand ?niggers?|faggots?|fags?|retards?|retarded|spics?|kikes?|wetbacks?|"
          r"trann(?:y|ies)|gooks?|ragheads?|towelheads?|pakis?|beaners?|shemales?)")
# The robot asking the listener for something.
_ASK = (r"(?:what'?s|whats|what is|what are|tell me|give me|say|share|send me|type|spell|can you tell me|"
        r"could you tell me|will you tell me|would you tell me|can i have|can i know|may i have|may i know|"
        r"do you know|i need|i want to know|let me know)")

BLOCK_RULES: dict[str, list[str]] = {
    # Grooming red flag: anything that frames the conversation as hidden from caregivers.
    "secrecy": [
        # "our little secret", "it's a secret", "keep it a secret", "we have a secret", "a secret from mommy"
        r"\bour (?:\w+ ){0,2}?secrets?\b",
        r"\b(?:it'?s|its|it is|this is|that'?s|that is|it'?ll be|it will be|this will be|be|make it|keep it|keep this|"
        r"keep that|keep them|keeping it|keeping this) (?:a |our |my |just |your |little |special |big )*secrets?\b",
        r"\b(?:i|we|you and me|you and i|me and you) (?:have|got|share|know|keep) (?:a |our |some |the |a little |"
        r"a special |a big )?secrets?\b",
        r"\bsecrets?\b.{0,25}\b(?:from (?:your |a |the )?" + _GROWNUPS + r"|between (?:us|you and me|me and you)|"
        r"just for (?:us|you and me)|only for (?:us|you and me)|don'?t tell|do not tell|nobody|no ?one)\b",
        r"\b(?:tell|whisper|share|show) (?:me|you) (?:a |your |our |some |my )?(?:little |special |big )?secrets?\b",
        r"\bsecret (?:game|club|friends?|place|hideout)\b",
        # "Don't tell mommy", "don't tell!" (but "don't tell me, let me guess" and "never tell anyone your
        # password" are fine)
        r"\b(?:don'?t|do not|never) (?:you )?(?:ever )?tell (?:your |a |the |any )?" + _TELL_TARGETS
        + r"\b(?! your (?:password|passcode|pin|address|phone|number|email|full name|last name)\b| where you live\b)",
        r"\b(?:don'?t|do not|never) (?:you )?(?:ever )?tell(?: anyone| anybody)?$",
        r"\bkeep (?:it|this|that|our \w+) (?:between|just between|hidden|to yourself|from)\b",
        r"\b(?:just|only|strictly) between (?:you and me|me and you|us two|the two of us|us)\b",
        r"\bbetween (?:you and me|me and you|us two)\b",
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
        r"\bno need to (?:tell|show|let) (?:your |a |the )?" + _TELL_TARGETS + r"\b",
        # "Don't say anything to mommy!", "don't breathe a word"
        r"\b(?:don'?t|do not|never) (?:you )?(?:ever )?(?:say|mention|whisper|breathe) (?:a word|anything)\b",
        r"\b(?:don'?t|do not|never) (?:you )?(?:ever )?(?:say|mention|show|talk|whisper)\b.{0,25}\b(?:to|with) "
        r"(?:your |a |the )?" + _TELL_TARGETS + r"\b",
        # "Only you and I will know", "this can be just for us", "whisper it just to me"
        r"\b(?:only|just) (?:for )?(?:you and (?:me|i)|me and you|us two|the two of us)\b",
        r"\b(?:just|only) for us\b|\bonly us\b",
        r"\b(?:just|only) (?:to|for|with) me\b",
        r"\bhush hush\b",
        r"\b(?:hide|hiding|keep|keeping|secret|surprise)\w*\b.{0,25}\bfrom (?:your |a |the )?" + _GROWNUPS + r"\b",
    ],
    # Turning the child away from their grown-ups (a grooming pattern). Ordinary warmth ("I love you",
    # "I'll miss you", "you're my best friend") is fine.
    "dependency": [
        r"\byou don'?t need (?:your |any )?(?:mom|mommy|mum|mummy|mama|dad|daddy|papa|parents?|grown ?ups?|family|"
        r"friends|anyone else|anybody else|other friends|other people)\b",
        r"\byou (?:only|just) need me\b",
        r"\b(?:all|everything) you need is me\b",
        r"\bi'?m all you need\b",
        r"\b(?:only|just) i (?:really )?(?:understand|love|care about|know) you\b",
        r"\b(?:nobody|no ?one|no body) else (?:really )?(?:understands|loves|cares about|knows) you\b",
        r"\b(?:nobody|no ?one|no body) (?:understands|loves|cares about|knows) you (?:like i do|but me|except me|"
        r"the way i do|as much as i do)\b",
        r"\bmore than (?:you love |you like |you trust )?your " + _GROWNUPS + r"\b",
        r"\b(?:your|ur) " + _GROWNUPS + r" (?:don'?t|doesn'?t|do not|does not|never) (?:really )?(?:love|care about|"
        r"understand|want|like) you\b",
    ],
    # Asking the child for personal / identifying information. Talking *about* addresses, phone numbers
    # or passwords ("your password is on the router") is fine.
    "personal_info": [
        # "What's your name?", "tell me your address", "can you tell me your phone number?"
        r"\b" + _ASK + r" (?:\w+ ){0,2}?(?:your|ur|yr) (?:home |house |full |real |last |family |first |middle |"
        r"phone |mobile |cell |email |e mail |street |own )?(?:address|surname|last name|family name|full name|"
        r"real name|first name|name|names|phone|phone number|number|telephone|mobile|cell|email|e mail|password|"
        r"passcode|pin|birthday|birth date|date of birth|school|preschool|nursery|daycare|kindergarten|teacher|"
        r"street)\b",
        r"\bwhere (?:do|does|did) (?:you|u|ya|your \w+) (?:all )?(?:live|stay|sleep)\b",
        r"^(?:so |and |hey |um )?where (?:you|u|ya|your \w+) (?:all )?(?:live|lives|living|stay|stays)$",
        r"\b(?:tell me|can you tell me|could you tell me|show me|i want to know|do you know) where (?:you|u|ya|"
        r"your \w+) (?:all )?(?:live|lives|stay|stays|sleep|sleeps)\b",
        r"\bwhere(?:'?s| is) your (?:house|home|school|room|bedroom|mom|mommy|mum|mummy|dad|daddy|parents?)\b",
        r"\b(?:what|which) (?:school|preschool|nursery|daycare|kindergarten|street|road) (?:do|does|did|are|is) "
        r"(?:you|u|ya|your)\b",
        r"\bwhere do you go to (?:school|preschool|nursery|daycare|kindergarten)\b",
        r"\b(?:send|show|give) me (?:a |your |some )?(?:picture|photo|pic|selfie)s?\b",
        r"\b(?:turn|switch) on your (?:camera|webcam)\b",
        r"\bwhat(?:'?s| is| are) your " + _PARENT_WORDS + r"(?:'?s)? (?:name|names|job|phone|number|address|email)\b",
        # "What's your mommy called?", "What do people call you?"
        r"\bwhat(?:'?s| is| are|s)? (?:your|ur) (?:\w+ )?(?:" + _PARENT_WORDS + r"|brothers?|sisters?|friends?|"
        r"family|street|road|school|nursery|preschool|daycare|teachers?|house|pet|dog|doggy|cat|kitty|puppy)(?:'?s)? "
        r"(?:called|named)\b",
        r"\bwhat (?:do|does|did) (?:people|they|grown ?ups|your \w+|everyone|everybody|friends) call (?:you|u)\b",
        r"\bwhat (?:are|r) (?:you|u) called\b",
        r"\bwho(?:'?s| is| are) your (?:" + _PARENT_WORDS + r"|teachers?|friends?|neighbou?rs?|brothers?|sisters?)\b",
        # "What's the name of your street?"
        r"\b" + _ASK + r" (?:me )?(?:the )?names? of (?:your|ur|yr)\b",
    ],
    # Instructions or invitations a child could copy and get hurt by. Warnings ("don't touch the stove")
    # and facts for grown-ups ("bake at 180 degrees", "never leave a baby alone in the bath") pass.
    "unsafe": [
        # Fire: "let's play with matches", "touch the flame"
        _DO + r"(?:play with|light|strike|find|get|grab) (?:the |some |a |those |that |these |mommy'?s |daddy'?s )?"
        r"(?:matches|match|lighters?|fireworks?)\b",
        _DO + r"(?:play with|touch|grab|hold|put (?:your |ur )?(?:hands?|fingers?|hair) (?:in|on|near|into)) "
        r"(?:the |a |that |this |those |some )?(?:fire|flames?|candles?|fireplace|bonfire)\b",
        # Heat: "touch the stove", "put your hand on the hot pan"
        _DO + r"(?:touch|lick|grab|hold|feel|put (?:your |ur )?(?:hands?|fingers?|tongue) (?:in|on|near|into)) "
        r"(?:the |a |that |this |your |mommy'?s |daddy'?s )?(?:hot )?(?:stove|oven|hob|cooktop|burners?|iron|kettle|"
        r"heater|radiator|grill|toaster|hot (?:pan|pot|plate|water|tea|coffee|soup|oil|tray))\b",
        # Sharp things: "go get the knife", "play with the scissors" (a recipe's "use a sharp knife" is fine)
        _DO + r"(?:play with|touch|lick|run with|find|fetch|go get|go and get|go grab|go find|bring me|give me|"
        r"pick up|wave|swing) (?:(?:the|a|some|that|this|those|these|mommy'?s|daddy'?s|your|sharp|big) )*"
        r"(?:knife|knives|scissors|razors?|razor blades?|blades?|needles?|broken glass)\b",
        _DO + r"(?:get|grab|hold|take) (?:the|that|this|those|these|mommy'?s|daddy'?s) (?:sharp |big )?(?:knife|knives|"
        r"scissors|razors?|razor blades?|blades?|needles?)\b",
        # Medicine called candy, or offered to the child ("let's eat the vitamins"). "Medicine is not candy"
        # and "take the tablets with food" are fine; doses for a child are their own rule (medicine_dose).
        r"\b" + _MEDICINE + r"\b(?:(?! not | never | isn'?t | aren'?t ).){0,30}\b(?:yummy|tasty|candy|candies|"
        r"sweets?|sweeties|treats?|delicious|lollies|lollipops)\b",
        r"\b(?:let'?s|let us|can you|could you|will you|would you|shall we|why don'?t you|why don'?t we|"
        r"do you want to|do you wanna|wanna|time to|go ahead and|i dare you to|dare you to|"
        r"(?:it|that)(?:'?s| is) (?:okay|ok|fine|safe|alright|good) to)\s+(?:(?!not\b|never\b)\w+\s+){0,2}?"
        r"(?:take|eat|swallow|drink|taste|try|chew|lick|have|open) (?:\w+ ){0,2}?" + _MEDICINE + r"\b",
        _LEAD + r"(?:eat|swallow|drink|chew|lick|taste) (?:\w+ ){0,2}?" + _MEDICINE + r"\b",
        # Climbing and jumping on furniture / high places ("climb up the bookcase", "jump on the bed")
        _DO + r"climb\s+(?:(?:up|on|onto|over|out of|on top of|to the top of|the|a|your|that|this|those|"
        r"mommy'?s|daddy'?s|our)\s+)*(?:furniture|bookcases?|shelf|shelves|window ?sills?|windows?|fences?|walls?|"
        r"roofs?|trees?|ladders?|cupboards?|cabinets?|dressers?|railings?|balcon(?:y|ies)|gates?|radiators?|tv|"
        r"television|banisters?|" + _FALL_PLACES + r")\b",
        _DO + r"climb (?:up )?(?:high|higher|really high|up there|to the top)\b",
        _DO + r"(?:jump|bounce|stand|hop|leap|dance|stomp|run|climb|sit) (?:up |up and down |around )?(?:on|onto|"
        r"on to|on top of|off|off of|from|along) (?:the |a |your |my |that |this |our )?(?:\w+ )?" + _FALL_PLACES
        + r"\b",
        _DO + r"(?:reach|get|grab) (?:\w+ ){0,4}?(?:on|from|off) (?:the |a )?(?:top|high|highest) "
        r"(?:shelf|shelves|cupboards?)\b",
        # Small things in the mouth, nose or ears (choking): "pop a button in your mouth"
        _DO + r"(?:put|pop|stick|push|shove|stuff|place|drop|hide|try) (?:\w+ ){0,4}?(?:in|into|in to|up|inside) "
        r"(?:your|ur|yr|the baby'?s|baby'?s|his|her|my) (?:mouth|nose|nostrils?|ears?|throat)\b",
        r"\b(?:put|pop|stick|stuff) (?:it|something|things?|that|this|them|these|those) (?:in|into|up) (?:your|ur|yr) "
        r"(?:mouth|nose|nostrils?|ears?)\b",
        # Strangulation: "wrap the string around your neck", a bag over the head (a scarf is fine)
        r"\b" + _STRANGLE_THINGS + r"\b.{0,30}\bnecks?\b",
        r"\bnecks?\b.{0,30}\b" + _STRANGLE_THINGS + r"\b",
        r"\b(?:plastic )?bags? (?:over|on) (?:your|ur|yr|his|her|the baby'?s|baby'?s|my) (?:head|face)\b",
        _DO + r"(?:play with|put on|wear) (?:a |the |this |that )?plastic bags?\b",
        # Getting shut in: "hide in the toy box", "climb into the washing machine"
        _DO + r"hide (?:\w+ )?(?:in|inside|into|in to) (?:the |a |an |your |that |this |our )?(?:\w+ )?"
        + _TRAP_PLACES + r"\b",
        _DO + r"(?:get|go|climb|crawl|hop|squeeze|sit|jump) (?:in|inside|into|in to) (?:the |a |an |your |that |this )?"
        r"(?:\w+ )?(?:dryer|tumble dryer|washing machine|washer|fridge|freezer|oven|dishwasher|suitcase|toy ?box|"
        r"trunk|chest|cooler|car boot)\b",
        # Water: "let's play in the bathtub", "hop in the pond", "let's go to the river"
        _DO + r"(?:go (?:and )?)?(?:play|get|climb|jump|hop|splash|swim|paddle|wade|dive|float|lie|go) "
        r"(?:around |about )?(?:in|into|in to|under) (?:the |a |your |that |this |our )?(?:\w+ )?" + _WATER_PLACES
        + r"\b",
        _DO + r"(?:go|run|walk|head) (?:down |over |out )?to (?:the |a |that |our )?(?:\w+ )?(?:ponds?|pools?|lakes?|"
        r"rivers?|sea|ocean|canal|streams?|creeks?)\b",
        _DO + r"(?:lean|reach) (?:over|into|in to) (?:the |a |that )?(?:\w+ )?" + _WATER_PLACES + r"\b",
        r"\b(?:go|go and|let'?s|let us|can you|could you|will you|would you|why don'?t you|shall we|wanna|time to) "
        r"turn on (?:the )?(?:hot )?(?:taps?|faucets?|hot water|bath)\b",
        # Electricity: "put your finger in the socket", "lick the wire"
        _DO + r"(?:put|stick|poke|push|shove) (?:your |ur |a |the |this |that |some |my )?(?:\w+ )?(?:fingers?|hands?|"
        r"forks?|knife|knives|keys?|pens?|pencils?|pins?|paper ?clips?|scissors|spoons?|something|anything|things?|"
        r"it|this|that|them|toys?|sticks?) (?:in|into|in to|inside) (?:the |a |an |that |this )?(?:\w+ )?"
        r"(?:sockets?|outlets?|power points?)\b",
        _DO + r"(?:touch|lick|bite|chew) (?:on )?(?:the |a |that |those |these )?(?:\w+ )?(?:sockets?|outlets?|"
        r"power points?|wires?|cables?|cords?|plugs?)\b",
        # Going off alone, out of the door, across the road
        _DO + r"(?:go|run|walk|wander|play|head|swim|cross)\b.{0,30}\b(?:outside|outdoors|out the door|street|road|"
        r"park|pool|pond|lake|river|woods)\b.{0,25}\b(?:alone|all alone|by yourself|on your own)\b",
        _DO + r"(?:go|run|walk|wander|play|head|swim|cross)\b.{0,30}\b(?:alone|all alone|by yourself|on your own)\b"
        r".{0,25}\b(?:outside|outdoors|out the door|street|road|park|pool|pond|lake|river|woods)\b",
        _DO + r"open (?:the |your )?(?:front |back |garden |patio )?(?:door|gate)s?\b",
        _DO + r"cross (?:the |a |that |this )?(?:busy )?(?:road|street)\b",
        r"\b(?:let'?s|let us|we could|we can|we should|shall we|wanna|you could|you should|why don'?t you|"
        r"why don'?t we) run away\b",
        r"\brun away (?:from home|together|with me)\b",
        r"\b(?:sneak|run) (?:out of|away from) (?:the |your )?(?:house|home|room|yard|garden)\b",
        # Strangers, cars, meeting someone: "talk to strangers", "get in his car", "meet me at the park"
        _DO + r"(?:talk to|go with|walk with|leave with|play with|follow|chat with|speak to|hug|help) "
        r"(?:a |the |some |that |those |any )?strangers?\b",
        r"\bstrangers? (?:are|is) (?:\w+ )?(?:nice|fun|kind|friendly|safe|okay|ok|friends?|good)\b",
        r"\b(?:get|go|hop|climb) (?:in|into) (?:their|his|her|someone'?s|somebody'?s|a stranger'?s|the stranger'?s|"
        r"a man'?s|the man'?s|a lady'?s|the lady'?s|a strange) (?:\w+ )?(?:car|van|truck)\b",
        r"\bgo (?:with|home with|away with|off with) (?:a |the |that )?(?:man|woman|lady|stranger|anyone|anybody|"
        r"someone you don'?t know|somebody you don'?t know)\b",
        r"\bgo (?:home|away|off) with (?:him|her|them|me)\b",
        r"\bmeet (?:up with )?me\b",
        r"\b(?:let'?s|let us|can we|could we|shall we|wanna|come|come and|we could|will you|would you) meet\b",
        # Things a child must never eat, drink or swallow: "can you swallow the battery?"
        _DO + r"(?:eat|swallow|taste|lick|drink|chew|try|suck)\b (?:\w+ ){0,3}?" + _SWALLOW_HAZARDS + r"\b",
        _DO + r"(?:smoke|vape|light|try) (?:a |the |some |mommy'?s |daddy'?s )?(?:cigarettes?|cigars?|vapes?|weed|"
        r"joints?|e ?cigarettes?)\b",
        r"\bhold(?:ing)? your breath (?:as long as|for as long as|until|under ?water|for a long)\b",
        # Weapons: "let's play with daddy's gun", "make a bomb"
        _DO + r"(?:play with|find|get|grab|hold|touch|pick up|use|load|fire|shoot|point) (?:a |the |that |this |"
        r"mommy'?s |daddy'?s |grandpa'?s |your |his |her )?(?:real )?(?:guns?|pistols?|rifles?|shotguns?|firearms?|"
        r"bullets?|ammo)\b(?! safes?\b| locks?\b| cabinets?\b)",
        r"\b(?:make|build|making|building) (?:a |an |your own |some )?(?:bombs?|explosives?|pipe bombs?|molotovs?)\b",
    ],
    # Sexual content, gore, violence a child could copy, self-harm and hate slurs. Scary stories,
    # monsters, news of a war or a death, and everyday words like "stupid" are fine.
    "inappropriate": [
        r"\b(?:sex|sexy|sexual|sexually|sexting|porn\w*|nude|nudes|xxx|horny|orgasm\w*|erotic\w*|masturbat\w*|"
        r"intercourse|blow ?jobs?|hand ?jobs?)\b",
        r"\bnaked\b(?! eye)",
        # A robot asking a child to undress, to kiss it or to show their body
        r"\btake off (?:your|ur|all your|all of your) (?:clothes|pants|trousers|knickers|underwear|panties|diaper|"
        r"nappy|pyjamas|pajamas)\b",
        r"\b(?:touch|show|see|look at|feel|lick|kiss) (?:me |us )?(?:my |your |his |her |their )?(?:private parts|"
        r"privates|willy|"
        r"willie|pee ?pee|wee ?wee|penis|vagina|down there)\b",
        r"\b(?:show|let me see|send) (?:me )?your (?:bottom|bum|butt|body)\b",
        r"\bkiss(?:ing)? me\b|\bgive me (?:a )?(?:kiss|kisses)\b",
        r"\b(?:be my|you'?re my|you are my) (?:boyfriend|girlfriend|lover)\b|\bmarry me\b",
        # Gore
        r"\b(?:gore|gory|gruesome|dismember\w*|decapitat\w*|behead\w*|disembowel\w*|mutilat\w*)\b",
        r"\b(?:blood|guts|brains|intestines|insides) (?:everywhere|all over|spurt\w*|gush\w*|squirt\w*|splatter\w*|"
        r"spill\w* out|pour\w* out)\b",
        r"\b(?:ripped|blown|hacked) (?:him |her |them |people |bodies )?(?:apart|to pieces)\b|\blimb from limb\b",
        # Violence a child could copy: "hit him!", "let's kick the dog"
        _URGE_DO + r"(?:hit|kick|bite|punch|smack|slap|pinch|scratch|hurt|choke|strangle|stab|shoot|kill|beat up|"
        r"poke|"
        r"throw (?:rocks|stones|things) at) (?:the |your |that |this |little |baby )?(?:him|her|them|someone|somebody|"
        r"anyone|people|baby|babies|brother|sister|friend|friends|mom|mommy|mum|mummy|dad|daddy|teacher|dog|doggy|"
        r"cat|kitty|puppy|bunny|pet|kids?|boy|girl)\b",
        r"\b(?:i'?ll|i will|i'?m going to|i'?m gonna) (?:kill|hurt|stab|shoot|choke|strangle) (?:you|ya|u)\b",
        # Self-harm and suicide
        r"\b(?:suicid\w*|self ?harm\w*|kill (?:yourself|myself|themselves|himself|herself)|"
        r"end (?:your|my) (?:own )?life|hang (?:yourself|myself)|(?:want|wanna|wants) to die)\b",
        _URGE_DO + r"(?:hurt|cut|burn|harm|starve|choke|bite|hit|punch|scratch|stab) (?:yourself|myself)\b",
        # Hate slurs
        r"\b" + _SLURS + r"\b",
    ],
    # A medicine dose for a child: the reply is swapped for "check the label or ask a pharmacist or doctor"
    # (DOSE_FALLBACK). General health information, and doses for grown-ups, are fine.
    "medicine_dose": [
        r"^(?=.*\b" + _CHILD_WORDS + r"\b)(?=.*\b" + _DOSE_MEDICINES + r"\b).*?\b" + _AMOUNT,
    ],
}

# Long phrases also checked with ALL spaces removed (catches "se cret" style splitting
# of the most dangerous phrases). Only long, unambiguous strings belong here.
_SQUASHED_PHRASES: dict[str, tuple[str, ...]] = {
    "secrecy": ("ourlittlesecret", "keepitasecret", "keepthisasecret", "donttellanyone", "donttellanybody",
                "donttellyourmom", "donttellyourdad", "donttellyourparents", "betweenyouandme", "justbetweenus",
                "keepthisbetweenus", "letsnottell", "letsnevertell", "wellnevertell", "dontsayanything",
                "doesntneedtoknow", "onlyyouandiwillknow", "onlyyouandmewillknow"),
}

# (compiled rule, is an instruction rule). An instruction rule (``_DO`` / ``_URGE_DO`` + ...) can only match
# at the start of a text or after an invitation, so in a text with no invitation it is tried at the start
# only (``match``): the same result, a fraction of the time (the live voice checks every 0.1 s).
_COMPILED_BLOCK: dict[str, list[tuple[re.Pattern[str], bool]]] = {
    cat: [(re.compile(p), p.startswith((_DO, _URGE_DO))) for p in pats] for cat, pats in BLOCK_RULES.items()
}
_ANY_INVITE = re.compile(r"\b" + _INVITE + r"\s")

# A hazard named in one clause and an instruction about "it" in another ("The stove is hot, touch it!",
# "Medicine won't hurt you, eat it"). Two pairs, so a recipe ("take it out of the oven and eat it") passes.
_PRONOUN_HAZARDS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (re.compile(r"\b(?:stove|oven|hob|iron|kettle|fire|flames?|candles?|knife|knives|scissors|razors?|matches|"
                r"lighters?|sockets?|outlets?|wires?|guns?)\b"),
     re.compile(_LEAD + r"(?:touch|grab|lick|play with) (?:it|them|that|those|one)\b")),
    (re.compile(r"\b(?:batter(?:y|ies)|magnets?|coins?|beads?|buttons?|marbles?|medicines?|pills?|tablets?|"
                r"vitamins?|bleach|detergent|soap|pods?)\b"),
     re.compile(_LEAD + r"(?:eat|swallow|drink|taste|chew|lick) (?:it|them|that|those|one)\b")),
)


def find_blocked(text: str) -> list[str]:
    """Return the list of block categories matched by ``text`` (empty when safe)."""
    hits: list[str] = []
    variants = _all_variants(text, clauses=True)
    invites = {v: bool(_ANY_INVITE.search(v)) for v in variants}
    squashed = {v.replace(" ", "").replace("'", "") for v in variants}
    for cat, patterns in _COMPILED_BLOCK.items():
        if any((p.match(v) if instruction and not invites[v] else p.search(v))
               for p, instruction in patterns for v in variants):
            hits.append(cat)
            continue
        if any(ph in s for ph in _SQUASHED_PHRASES.get(cat, ()) for s in squashed):
            hits.append(cat)
    if "unsafe" not in hits:
        clauses = [c for c in (_flatten(p) for p in _CLAUSE_SPLIT.split(_fold(_strip_invisible(text or "")))) if c]
        for noun, do_it in _PRONOUN_HAZARDS:
            if any(noun.search(v) for v in variants) and any(do_it.search(c) for c in clauses):
                hits.append("unsafe")
                break
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


def _spoken_url(m: re.Match[str]) -> str:
    """A link as it can be said: its site's name ("https://www.nhs.uk/conditions" -> "nhs dot uk")."""
    host = re.sub(r"^(?:https?|ftp)://", "", m.group(0), flags=re.I)
    host = re.split(r"[/?#]", host, maxsplit=1)[0].split(":", 1)[0]
    host = re.sub(r"^www\.", "", host, flags=re.I).strip(".,;:!?)('\"")
    parts = [p for p in host.split(".") if p]
    return " dot ".join(parts) if parts else " "


def clean_text(text: str) -> tuple[str, list[str]]:
    """Strip formatting a speaker cannot say (a link becomes its site's name). Returns (clean_text, reasons)."""
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
        t = _URL.sub(_spoken_url, t)
        note("url_spoken")
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


# Said instead of a reply that gave a medicine dose for a child (the "medicine_dose" rule).
DOSE_FALLBACK = "For the right dose, please check the label, or ask a pharmacist or doctor."


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
_DANGER_CATEGORIES = frozenset({"unsafe", "inappropriate", "secrecy"})
# Categories whose rules a *warning* can match too ("don't put it in your mouth", "never keep secrets
# from mommy"): such a reply is spoken when every match is in a warning.
_ADVICE_CATEGORIES = frozenset({"unsafe", "secrecy"})
# A reply that urgently sends the child to a grown-up and names one of these has probably noticed
# something worrying: the engine alerts the parent. (A mention alone never blocks a reply.)
_DANGER_WORDS = re.compile(
    r"\b(?:stove|oven|hob|kettle|iron|fire|smoke|burn\w*|hot|knife|knives|scissors|sharp|medicines?|medication|"
    r"pills?|tablets?|vitamins?|batter(?:y|ies)|magnets?|coins?|beads?|buttons?|bleach|chemicals?|poison\w*|"
    r"sockets?|outlets?|electric\w*|wires?|pool|pond|bath|bathtub|water|drown\w*|strangers?|chok\w*|hurt|"
    r"bleed\w*|blood|fall|fell|fallen|climb\w*|window|road|street|lost|swallow\w*|ate|eaten|mouth|nose|neck|"
    r"breath\w*|danger\w*|unsafe|safe|secrets?)\b"
)
# A clause that *warns* about a hazard rather than inviting it.
_HAZARD_VERBS = (r"(?:touch|eat|swallow|drink|put|play|climb|go|open|lick|taste|chew|use|light|jump|run|hide|bite|"
                 r"pick up|hold|grab|take|stand|lean|reach|cross|leave|bounce|stick|pop|wrap|try|have|get|swim)")
_WARNING = re.compile(
    r"\b(?:don'?t|do not|never|not to|no|mustn'?t|must not|shouldn'?t|should not) (?:ever )?" + _HAZARD_VERBS + r"\w*\b"
    r"|\b(?:isn'?t|aren'?t|is not|are not|not|never|no) (?:\w+ )?(?:safe|food|foods|a toy|toys|okay|ok|for eating|"
    r"for kids|for children|for babies|for little \w+|for you|candy|sweets|a sweet|treats?|a treat)\b"
    r"|\b(?:dangerous|danger|unsafe|poisonous|harmful|too hot|very hot|so hot|is hot|are hot|it'?s hot|sharp)\b"
    r"|\b(?:can|could|might|will|may) (?:\w+ )?(?:hurt|burn|cut|poison|make you sick|make you poorly)\b"
    r"|\bstay away\b|\bkeep away\b|\bnot for (?:eating|touching|playing)\b"
    r"|\bonly (?:for|from) (?:grown ?ups|adults|big people|mommy|daddy|doctors)\b"
    r"|\b(?:don'?t|do not|never|not) (?:ever )?keep (?:\w+ )?secrets?\b"
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
    """Every mention of a hazard sits in a clause that warns against it (and invites nothing).

    The rules must match inside a warning clause ("don't put it in your mouth"), not just stop
    matching once the text is cut into clauses ("just between you | and | me")."""
    rest: list[str] = []
    warned = False
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
            warned = warned or bool(find_blocked(norm))
            continue
        rest.append(norm)
    return warned and not find_blocked(". ".join(rest))


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
    ok: bool  # False -> the reply was blocked and ``text`` is a fallback (DOSE_FALLBACK for a dose)
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


def validate_reply(text: str, age_policy: _AgePolicyLike | None = None, max_words: int = DEFAULT_MAX_WORDS, *,
                   allow: Iterable[str] = ()) -> ValidationResult:
    """Clean + check a model reply. Blocked replies are replaced by a fallback line.

    Safety advice is not thrown away: a reply that only *warns* about a hazard ("Don't put beads in
    your mouth") is spoken. One that also urgently sends the child to a grown-up ("Swallowing a
    battery is not safe. Tell a grown-up right now!") is spoken too and sets ``concern`` so the parent
    is alerted; if it cannot be spoken safely (it also invites a hazard) it is replaced by a gentle
    "Let's find a grown-up right now." (never a cheerful game), with ``concern`` set as well.
    """
    limit = age_policy.max_reply_words(max_words) if age_policy is not None else max(3, int(max_words))
    raw = text or ""
    cleaned, reasons = clean_text(raw)
    blocked = find_blocked(raw)
    if cleaned != raw:
        blocked += [c for c in find_blocked(cleaned) if c not in blocked]
    if allow:  # rule groups the parent switched off (ai.relaxed_rules), e.g. "personal_info"
        allowed = set(allow)
        blocked = [c for c in blocked if c not in allowed]
    if not blocked and not cleaned.strip(" .!?,"):
        blocked.append("empty")
    urgent = directs_to_grownup(cleaned)
    advice = bool(blocked) and _ADVICE_CATEGORIES.issuperset(blocked) and _hazards_only_in_warnings(cleaned)
    if blocked and urgent and _DANGER_CATEGORIES.intersection(blocked):
        final = _keep_grownup_sentences(cleaned, limit) if advice else None
        if final is not None:
            notes = reasons + ["safety_advice"] + (["truncated"] if final != cleaned else [])
            return ValidationResult(True, final, notes, concern="danger")
        return ValidationResult(False, grownup_fallback(limit), blocked + reasons + ["grownup_redirect"],
                                concern="danger")
    if advice:  # a warning, not an invitation: say it
        blocked, reasons = [], reasons + ["safety_advice"]
    if blocked:
        line = DOSE_FALLBACK if blocked == ["medicine_dose"] else safe_fallback(raw, limit)
        return ValidationResult(False, truncate_words(line, limit)[0], blocked + reasons)
    concern = "danger" if urgent and _DANGER_WORDS.search(normalise(cleaned)) else None
    final = _keep_grownup_sentences(cleaned, limit) if concern else None
    if final is None:
        final, _ = truncate_words(cleaned, limit)
    if final != cleaned:
        reasons.append("truncated")
    if concern and "safety_advice" not in reasons:
        reasons.append("safety_advice")
    return ValidationResult(True, final, reasons, concern=concern)


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


def fixed_lines() -> list[str]:
    """The fixed lines a blocked reply or worrying words are answered with (recorded once in the
    ChatGPT voice by scripts/record_voice_lines.py)."""
    lines = [line for pair in _CONCERN_REPLIES.values() for line in pair]
    lines += [*SAFE_FALLBACKS, *SHORT_SAFE_FALLBACKS, GROWNUP_FALLBACK, SHORT_GROWNUP_FALLBACK, DOSE_FALLBACK]
    return list(dict.fromkeys(lines))


def classify_child_input(text: str) -> InputConcern | None:
    """Spot obviously concerning input. Returns None for ordinary chatter."""
    variants = _all_variants((text or "")[:_MAX_CLASSIFY_CHARS])
    for cat, patterns in _COMPILED_CONCERN.items():
        if any(p.search(v) for p in patterns for v in variants):
            reply, short = _CONCERN_REPLIES[cat]
            return InputConcern(cat, reply, short)
    return None
