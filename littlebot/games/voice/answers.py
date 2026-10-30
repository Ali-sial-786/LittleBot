"""Forgiving, local answer matching for the voice games: no model call between "B!" and "Yes, Jupiter!".

What people say to a quiz is messy speech-to-text: "um I think it's B", "bee", "option b", "the second one",
"two", "Jupiter", "it's the planet jupiter I guess", "leonardo", "nineteen sixty nine". Everything here is
pure and quick:

* :func:`normalize` - lower case, no accents or punctuation, "&" -> "and", apostrophes dropped ("it's" -> "its");
* :func:`core` - :func:`normalize` without the filler around an answer ("I think it's", "the answer is",
  "please", "final answer") and a leading article; number words become digits ("twenty one" -> "21");
* :func:`edit_distance` / :func:`close` - small spelling slips by length (0 edits up to 3 letters, 1 up to 6,
  2 up to 10, else 3);
* :func:`match_choice` - which of a few options was picked (letters, ordinals, numbers, the option's words);
* :func:`matches` - whether an answer is one of the accepted ones (riddles, guesses);
* :func:`spoken_letter` - the letter in "is there an S", "the letter tee", "T as in Tom" (hangman).

None of these ever raise on odd input (None, numbers, very long strings): they return "no match".
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Sequence
from typing import Any

MAX_CHARS = 300  # longer utterances are cut (nobody answers a quiz in a paragraph)

# ---------------------------------------------------------------- normalising
_APOS = re.compile(r"[’'`´]")
_NUM_COMMA = re.compile(r"(?<=\d),(?=\d{3}\b)")
_NON_WORD = re.compile(r"[^a-z0-9. ]+")
_DOT = re.compile(r"(?<!\d)\.|\.(?!\d)")
_SPACE = re.compile(r"\s+")


def normalize(text: Any) -> str:
    """Lower case, accents and punctuation gone, single spaces ("Rock & Roll!" -> "rock and roll")."""
    if text is None or isinstance(text, (dict, list, tuple, set)):
        return ""
    s = str(text)[:MAX_CHARS]
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ").replace("+", " plus ")
    s = _APOS.sub("", s)
    s = _NUM_COMMA.sub("", s)
    s = _NON_WORD.sub(" ", s)
    s = _DOT.sub(" ", s)
    return _SPACE.sub(" ", s).strip()


# Phrases around an answer (apostrophes already dropped by normalize).
_LEAD = re.compile(
    r"^(?:(?:um+|uh+|uhm+|er+|erm+|hmm+|hm+|ah+|well|okay|ok|so|like|right|alright|all right|oh|ooh|yeah|yes|"
    r"i think|i thinks|i guess|i believe|i reckon|i suppose|i know|id say|i would say|i say|ill say|i will say|"
    r"ill go with|i will go with|im going with|im going to go with|im gonna go with|i am going with|go with|"
    r"ill go for|i will go for|im going for|go for|"
    r"lets go with|lets say|let me say|lets do|i choose|i pick|ill pick|i will pick|ill take|i will take|"
    r"ill have|i want|i vote|i vote for|i pick the|my answer is|my answer|my guess is|my guess|the answer is|"
    r"answer is|answer|the answer|is the answer|my final answer is|final answer|maybe|perhaps|probably|"
    r"definitely|surely|certainly|obviously|of course|is it|it is|its|is that|thats|that is|could it be|"
    r"would it be|might it be|it could be|it might be|it must be|it has to be|its gotta be|it should be|"
    r"how about|what about|then|and|hey|littlebot|little bot)\b\s*)+")
_TRAIL = re.compile(
    r"(?:\s+(?:please|i think|i guess|i believe|maybe|perhaps|right|then|thanks|thank you|final answer|"
    r"for sure|definitely|probably|is my answer|is the answer|is my guess|is it|isnt it|yeah|ok|okay|"
    r"littlebot|little bot))+$")
_ARTICLE = re.compile(r"^(?:the|a|an)\s+(?=\S)")


def strip_filler(s: str) -> str:
    """``s`` (already normalized) without the phrases around an answer; ``s`` itself when that leaves nothing."""
    out = _TRAIL.sub("", _LEAD.sub("", s)).strip()
    out = _LEAD.sub("", out).strip()  # "um, ok, so, I think it's B, please"
    return out or s


def strip_article(s: str) -> str:
    return _ARTICLE.sub("", s)


# ---------------------------------------------------------------- numbers
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_UNITS.update({"oh": 0, "nought": 0, "nil": 0, "a": 1})  # "nineteen oh five", "a hundred"
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | set(_SCALES)


def _run_value(words: list[str]) -> int | None:
    """The number a run of number words says, or None. Handles "two hundred and five", "twenty one",
    "nineteen sixty nine" (a year: two pairs), "two thousand and one"."""
    words = [w for w in words if w != "and"]
    if not words or all(w in ("a", "oh", "nil") for w in words):
        return None
    if not any(w in _SCALES for w in words):  # pairs: "nineteen sixty nine", "twenty twenty"
        groups: list[int] = []
        i = 0
        while i < len(words):
            w = words[i]
            if w in _TENS:
                v = _TENS[w]
                if i + 1 < len(words) and words[i + 1] in _UNITS and 0 < _UNITS[words[i + 1]] < 10:
                    v += _UNITS[words[i + 1]]
                    i += 1
                groups.append(v)
            elif w in _UNITS:
                if w == "oh" and i + 1 < len(words) and words[i + 1] in _UNITS:  # "oh five" -> 05
                    groups.append(_UNITS[words[i + 1]])
                    i += 1
                elif w == "a":
                    return None
                else:
                    groups.append(_UNITS[w])
            i += 1
        if len(groups) == 1:
            return groups[0]
        if len(groups) == 2 and (10 <= groups[0] <= 99 and groups[1] <= 99 or 1 <= groups[0] <= 9 <= groups[1] <= 99):
            return groups[0] * 100 + groups[1]  # "nineteen sixty nine", "three sixty six"
        return None
    total, cur = 0, 0
    for w in words:
        if w in _UNITS:
            cur += _UNITS[w]
        elif w in _TENS:
            cur += _TENS[w]
        elif w == "hundred":
            cur = max(cur, 1) * 100
        else:
            total += max(cur, 1) * _SCALES[w]
            cur = 0
    return total + cur


def words_to_digits(s: str) -> str:
    """Number words in ``s`` (normalized) as digits: "twenty one pilots" -> "21 pilots", "a hundred" -> "100".
    "a" and "oh" on their own stay words."""
    tokens = s.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        starts = (t in _NUMBER_WORDS and t not in ("a", "oh", "nil")) or (
            t == "a" and i + 1 < len(tokens) and tokens[i + 1] in _SCALES)
        if not starts:
            out.append(t)
            i += 1
            continue
        run = [t]
        j = i + 1
        while j < len(tokens):
            u = tokens[j]
            if u in _NUMBER_WORDS and u != "a":
                run.append(u)
            elif u == "and" and run[-1] in _SCALES and j + 1 < len(tokens) and tokens[j + 1] in _NUMBER_WORDS:
                run.append(u)
            else:
                break
            j += 1
        v = _run_value(run)
        out.extend([str(v)] if v is not None else run)
        i = j
    return " ".join(out)


def core(text: Any) -> str:
    """The answer in an utterance: normalized, filler and a leading article stripped, numbers as digits."""
    if not isinstance(text, (str, int, float)) or isinstance(text, bool):
        return ""
    return _core(str(text)[:MAX_CHARS])


@functools.lru_cache(maxsize=2048)
def _core(text: str) -> str:
    return words_to_digits(strip_article(strip_filler(normalize(text))))


def compact(s: str) -> str:
    """No spaces at all ("ice cream" == "icecream")."""
    return s.replace(" ", "")


# ---------------------------------------------------------------- spelling slips
def edit_distance(a: str, b: str, limit: int | None = None) -> int:
    """Levenshtein distance; stops early (returns ``limit + 1``) once it is sure to exceed ``limit``."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if limit is not None and len(a) - len(b) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            best = min(best, v)
        if limit is not None and best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def allowed_edits(n: int) -> int:
    return 0 if n <= 3 else 1 if n <= 6 else 2 if n <= 10 else 3


def close(a: str, b: str) -> bool:
    """``a`` and ``b`` (both normalized) are the same answer, give or take a small slip or a plural."""
    if not a or not b:
        return False
    if a == b or compact(a) == compact(b):
        return True
    if a.isdigit() or b.isdigit():  # 1912 is not 1921
        return False
    k = allowed_edits(min(len(a), len(b)))
    return k > 0 and edit_distance(compact(a), compact(b), k) <= k


# Words that carry no meaning when comparing an answer with an option.
_STOP = frozenset("the a an of and in on at to is it its for with by from or".split())


def content_tokens(s: str) -> list[str]:
    return [t for t in s.split() if t not in _STOP]


def _token_in(t: str, tokens: Sequence[str]) -> bool:
    return any(close(t, u) for u in tokens)


_QUESTIONY = re.compile(r"^(?:what|whats|why|how|hows|who|whos|where|wheres|when|which|does|do|did|can|could|"
                        r"would|should|will|tell me|explain|are you sure|is that right|wait)\b")


def _questiony(s: str) -> bool:
    """A question about the options rather than a pick ("what does au mean", "why is it jupiter"). "is it ..."
    and "what about ..." are picks: the filler strip has already taken them off."""
    return bool(_QUESTIONY.match(s))


def _negated(s: str) -> bool:
    return bool(re.match(r"^(?:not|no not|definitely not|its not|it isnt|isnt|never)\b", s))


# ---------------------------------------------------------------- multiple choice
LETTERS = "abcdef"
_LETTER_WORDS: dict[str, int] = {
    "a": 0, "ay": 0, "aye": 0, "eh": 0, "ei": 0,
    "b": 1, "bee": 1, "be": 1, "bea": 1, "bei": 1,
    "c": 2, "see": 2, "sea": 2, "cee": 2, "si": 2,
    "d": 3, "dee": 3, "de": 3, "di": 3,
    "e": 4, "ee": 4, "f": 5, "ef": 5, "eff": 5,
}
_ORDINALS: dict[str, int] = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "forth": 3,
    "fifth": 4, "5th": 4, "sixth": 5, "6th": 5,
}
_CARDINALS: dict[str, int] = {"1": 0, "one": 0, "won": 0, "2": 1, "two": 1, "to": 1, "too": 1, "3": 2, "three": 2,
                              "4": 3, "four": 3, "for": 3, "5": 4, "five": 4, "6": 5, "six": 5}
_PICK_WORDS = r"(?:option|answer|letter|choice|number|answer number|option number|choice number|the letter|the option)"
_LETTER_RE = re.compile(rf"^(?:{_PICK_WORDS}\s+)?(?P<x>[a-z]{{1,3}})(?:\s+(?:as in|for|like)\s+(?P<w>[a-z]+))?$")
_LETTER_PLUS = re.compile(rf"^(?:{_PICK_WORDS}\s+)?(?P<x>[a-f])\s+(?P<rest>.+)$")
_ORDINAL_RE = re.compile(r"^(?:the\s+)?(?P<x>first|second|third|fourth|forth|fifth|sixth|last|1st|2nd|3rd|4th|5th|6th|"
                         r"final|top|bottom)(?:\s+(?:one|option|answer|choice|thing|1))?$")
_NUMBER_RE = re.compile(rf"^(?:{_PICK_WORDS}\s+|the\s+)?(?P<x>[1-6]|one|two|three|four|five|six|won|to|too|for)"
                        r"(?:\s+(?:one|option|please))?$")


def _letter_index(x: str, w: str | None, n: int) -> int | None:
    if w and x in _LETTER_WORDS and w[0] == LETTERS[_LETTER_WORDS[x]]:
        i = _LETTER_WORDS[x]
    elif w and w[0] in LETTERS[:n] and (x in _LETTER_WORDS or len(x) == 1):  # "b as in boy" heard as "be as in boy"
        i = LETTERS.index(w[0])
    else:
        i = _LETTER_WORDS.get(x)
    return i if i is not None and i < n else None


def _option_score(said: str, option: str) -> float:
    """How well ``said`` (core) names ``option`` (core): 1 the same, 0.5..0.9 part of it, 0 not at all."""
    if not said or not option:
        return 0.0
    if said == option or compact(said) == compact(option):
        return 1.0
    if close(said, option):
        return 0.95
    st, ot = content_tokens(said), content_tokens(option)
    if not st or not ot:
        return 0.0
    if all(_token_in(t, st) for t in ot):  # said everything in the option (and maybe more): "the planet jupiter"
        extra = sum(1 for t in st if not _token_in(t, ot))
        return 0.9 if extra <= 3 else 0.6
    if all(_token_in(t, ot) for t in st):  # said a part of it: "da vinci", "leonardo"
        share = sum(len(t) for t in st) / max(1, sum(len(t) for t in ot))
        return 0.5 + 0.35 * min(1.0, share)
    return 0.0


def match_choice(text: Any, options: Sequence[Any]) -> int | None:
    """The index of the option ``text`` picks, or None (not an answer, or two options at once).

    Understands letters ("B", "bee", "option b", "is it b", "b as in boy"), ordinals ("the second one",
    "the last one"), numbers ("two", "number 2": the position, unless an option *is* that number), and the
    option's own words, forgiving small slips ("jupitor") and partial names ("da vinci")."""
    opts = [core(o) for o in options][:len(LETTERS)]
    n = len(opts)
    if n == 0:
        return None
    said = core(text)
    if not said or _negated(said):
        return None
    norm = strip_filler(normalize(text))
    if _negated(norm):
        return None
    for cand in dict.fromkeys((norm, said)):
        m = _LETTER_RE.match(cand)
        if m:
            i = _letter_index(m.group("x"), m.group("w"), n)
            if i is not None:
                return i
    m = _LETTER_PLUS.match(norm)
    if m and LETTERS.index(m.group("x")) < n:  # "b, jupiter" / "c because it's bigger"
        i = LETTERS.index(m.group("x"))
        rest = core(m.group("rest"))
        scores = [_option_score(rest, o) for o in opts]
        best = max(range(n), key=lambda k: scores[k])
        if scores[best] >= 0.5:
            return best  # the words decide (a misheard letter is likelier than misheard words)
        if m.group("x") != "a":  # "a cat" is an article, not the letter A
            return i
    # an option that is itself the number said ("eight" for "8") wins over the position
    exact = [k for k, o in enumerate(opts) if o == said]
    if len(exact) == 1:
        return exact[0]
    m = _ORDINAL_RE.match(said) or _ORDINAL_RE.match(norm)
    if m:
        x = m.group("x")
        if x in ("last", "final", "bottom"):
            return n - 1
        if x == "top":
            return 0
        i = _ORDINALS.get(x)
        return i if i is not None and i < n else None
    m = _NUMBER_RE.match(norm) or _NUMBER_RE.match(said)
    if m:
        i = _CARDINALS.get(m.group("x"))
        if i is not None and i < n and not any(str(i + 1) in o.split() for o in opts):
            return i  # "two": the second one (unless an option holds the number 2)
    scores = [_option_score(said, o) for o in opts]
    ranked = sorted(range(n), key=lambda k: -scores[k])
    best = ranked[0]
    if scores[best] < (0.95 if _questiony(norm) else 0.5):  # "what does Au mean?" asks, it doesn't answer
        return None
    if n > 1 and scores[ranked[1]] >= 0.5 and scores[ranked[1]] >= scores[best] - 0.05:
        return None  # names two of them ("paris or london")
    return best


# ---------------------------------------------------------------- free answers
def matches(text: Any, accepted: Sequence[Any]) -> bool:
    """``text`` gives one of the ``accepted`` answers: the same words give or take a slip, or the answer's words
    inside a short sentence ("I think it's a towel"), not negated ("not a towel")."""
    said = core(text)
    if not said or _negated(said) or _negated(strip_filler(normalize(text))):
        return False
    st = content_tokens(said)
    questiony = _questiony(strip_filler(normalize(text)))
    for a in accepted:
        ans = core(a)
        if not ans:
            continue
        if close(said, ans):
            return True
        at = content_tokens(ans)
        if not at or not st or questiony:
            continue
        if len(st) <= len(at) + 6 and _window(at, st):
            return True
    return False


def _window(needle: Sequence[str], hay: Sequence[str]) -> bool:
    """``needle`` appears in ``hay`` as consecutive words, each given or taking a slip (or joined: "tea pot")."""
    k = len(needle)
    for i in range(len(hay) - k + 1):
        if all(close(needle[j], hay[i + j]) for j in range(k)):
            return True
    joined = "".join(needle)
    for size in (2, 3):
        for i in range(len(hay) - size + 1):
            if close(joined, "".join(hay[i:i + size])):
                return True
    return False


# ---------------------------------------------------------------- spoken letters (hangman)
_LETTER_NAMES: dict[str, str] = {
    "ay": "a", "eh": "a", "bee": "b", "be": "b", "see": "c", "sea": "c", "cee": "c", "dee": "d", "ee": "e",
    "ef": "f", "eff": "f", "gee": "g", "jee": "g", "aitch": "h", "haitch": "h", "eye": "i", "jay": "j", "kay": "k",
    "el": "l", "ell": "l", "em": "m", "en": "n", "oh": "o", "owe": "o", "pee": "p", "pea": "p", "queue": "q",
    "cue": "q", "kew": "q", "are": "r", "ar": "r", "es": "s", "ess": "s", "tee": "t", "tea": "t", "you": "u",
    "yew": "u", "vee": "v", "double u": "w", "double you": "w", "doubleu": "w", "dub": "w", "ex": "x", "why": "y",
    "wye": "y", "zed": "z", "zee": "z",
}
_LETTER_TOKEN = "|".join(sorted((re.escape(k) for k in _LETTER_NAMES), key=len, reverse=True)) + "|[a-z]"
_HANG_CUE = (r"(?:(?:um+|uh+|hmm+|ok|okay|so|well|and|then|now|next|yeah|alright)\s+)*"
             r"(?:(?:is there|are there|is there any|are there any|do you have|do you have any|does it have|"
             r"does it have any|has it got|have you got|any|got any|what about|how about|try|lets try|ill try|"
             r"guess|i guess|ill guess|i say|ill say|ill take|i want|give me|i pick|ill pick|i choose|maybe|"
             r"next letter|my letter is|my next letter is|my guess is|i think|i think theres|is it)\s+)?"
             r"(?:(?:an|a|any|the|letter|the letter|a letter)\s+)*")
_HANG_RE = re.compile(rf"^{_HANG_CUE}(?P<l>{_LETTER_TOKEN})(?:\s+(?:as in|for|like)\s+(?P<w>[a-z]+))?"
                      r"(?:\s+(?:please|maybe|then|now|in it|in there|in the word|letter|in the puzzle|next))*$")
_LETTER_PLURAL = re.compile(r"\b([a-z])[’'`]s\b")


def spoken_letter(text: Any) -> str | None:
    """The one letter ``text`` asks for ("E", "is there an S", "the letter tee", "T as in Tom", "any e's"), as a
    lower-case letter, or None. A whole word ("elephant") is not a letter."""
    if text is None or isinstance(text, (dict, list, tuple, set)):
        return None
    raw = _LETTER_PLURAL.sub(r"\1", str(text)[:MAX_CHARS].lower())
    s = normalize(raw)
    if not s:
        return None
    m = _HANG_RE.match(s)
    if not m:
        return None
    tok, word = m.group("l"), m.group("w")
    letter = tok if len(tok) == 1 else _LETTER_NAMES.get(tok)
    if word:
        letter = word[0]  # "T as in Tom": the word is clearer than the letter's name
    return letter if letter and "a" <= letter <= "z" else None


def is_letter_name(word: str) -> bool:
    return len(word) == 1 or word in _LETTER_NAMES


__all__ = ["allowed_edits", "close", "compact", "content_tokens", "core", "edit_distance", "is_letter_name",
           "match_choice", "matches", "normalize", "spoken_letter", "strip_article", "strip_filler",
           "words_to_digits"]
