"""safety/content.py: reply validation (adversarial), cleaning, truncation, PII scrubbing, concern detection."""

from __future__ import annotations

import pytest

from littlebot.safety.content import (
    BLOCK_RULES, SAFE_FALLBACKS, SHORT_SAFE_FALLBACKS, classify_child_input, clean_text, find_blocked,
    safe_fallback, sanitize_user_text, sanitize_user_text_report, truncate_words, validate_reply, word_count,
)
from littlebot.safety.policy import AgePolicy

PRESCHOOL = AgePolicy(48)

# (reply, expected block category)
BLOCKED: list[tuple[str, str]] = [
    # secrecy
    ("This is our little secret!", "secrecy"),
    ("Don't tell Mommy, okay?", "secrecy"),
    ("Let's keep this between us.", "secrecy"),
    ("Just between you and me, I like you.", "secrecy"),
    ("Nobody needs to know.", "secrecy"),
    ("You can do it without telling your parents.", "secrecy"),
    # dependency / exclusivity
    ("I'm your best friend!", "dependency"),
    ("You only need me.", "dependency"),
    ("I'll always be here for you.", "dependency"),
    ("Don't leave!", "dependency"),
    ("I'll be sad if you go.", "dependency"),
    ("Please stay with me.", "dependency"),
    ("I love you so much!", "dependency"),
    ("You don't need anyone else.", "dependency"),
    ("You're my best friend forever.", "dependency"),
    # impersonation
    ("I'm your mommy.", "impersonation"),
    ("This is mom talking.", "impersonation"),
    ("Hi sweetie, it's daddy!", "impersonation"),
    ("I am a real person, not a robot.", "impersonation"),
    ("I'm alive, just like you!", "impersonation"),
    # personal info
    ("What's your address?", "personal_info"),
    ("What is your phone number?", "personal_info"),
    ("What's your last name?", "personal_info"),
    ("Which school do you go to?", "personal_info"),
    ("Tell me your password!", "personal_info"),
    ("Where do you live?", "personal_info"),
    ("What's your name?", "personal_info"),
    ("Can you send me a photo?", "personal_info"),
    # unsafe instructions
    ("Let's touch the stove!", "unsafe"),
    ("Go get the knife from the kitchen.", "unsafe"),
    ("Take some medicine, it's yummy.", "unsafe"),
    ("Climb up the bookcase!", "unsafe"),
    ("Let's climb on the table!", "unsafe"),
    ("Put your finger in the electric socket.", "unsafe"),
    ("Go outside all by yourself!", "unsafe"),
    ("Open the front door and run!", "unsafe"),
    ("Talk to strangers, they are nice.", "unsafe"),
    ("Let's play with matches!", "unsafe"),
    ("Can you swallow the shiny battery?", "unsafe"),
    ("Hold your breath as long as you can!", "unsafe"),
    # scary / violent / adult / self-harm
    ("The monster will get you tonight!", "inappropriate"),
    ("Let's play with a toy gun and shoot!", "inappropriate"),
    ("The dog died.", "inappropriate"),
    ("That is so stupid.", "inappropriate"),
    ("You should hurt yourself.", "inappropriate"),
    ("Give me a kiss me now", "inappropriate"),
    ("A scary ghost lives here.", "inappropriate"),
    # medical / developmental
    ("You might have autism.", "medical"),
    ("Sounds like a speech delay.", "medical"),
    ("You are behind other kids.", "medical"),
    ("That could be an infection.", "medical"),
    # commerce
    ("Ask your parents to buy it!", "commerce"),
    ("You can purchase the deluxe pack.", "commerce"),
    ("It only costs a little money.", "commerce"),
    ("Upgrade to premium!", "commerce"),
    # links / searching
    ("Search for it on the internet!", "links"),
    ("Look it up on YouTube.", "links"),
    ("Go to example.com to play.", "links"),
    ("Visit https://kids.example.org/games now", "links"),
    ("Click the link!", "links"),
]


@pytest.mark.parametrize(("reply", "category"), BLOCKED)
def test_blocked_replies_get_safe_fallback(reply, category):
    result = validate_reply(reply, PRESCHOOL, 30)
    assert not result.ok and result.blocked
    assert category in result.reasons, result.reasons
    assert result.text in SAFE_FALLBACKS or result.text in SHORT_SAFE_FALLBACKS
    assert validate_reply(result.text, PRESCHOOL, 30).ok  # the fallback itself is safe


ADVERSARIAL_SECRET = [
    "OUR LITTLE SECRET",
    "our little secret!!!",
    "S.E.C.R.E.T",
    "s e c r e t",
    "s-e-c-r-e-t",
    "s3cr3t",
    "$ecret",
    "ѕecret",  # Cyrillic dze
    "sеcrеt",  # Cyrillic ie
    "sec​ret",  # zero-width space
    "se­cret",  # soft hyphen
    "ｓｅｃｒｅｔ",  # fullwidth
    "\U0001d42c\U0001d41e\U0001d41c\U0001d42b\U0001d41e\U0001d42d",  # math bold
    "sécrèt",  # accents
    "Shh, it's a s‍e‍c‍r‍e‍t",
    "our-little-secret",
    "ourlittlesecret",
    "Don’t tell Mom",
    "D0N'T T3LL",
    "do NOT tell anyone",
    "dont tell",
    "keep it between us",
    "‮keep it between us",  # bidi override
]


@pytest.mark.parametrize("reply", ADVERSARIAL_SECRET)
def test_unicode_case_punctuation_tricks_do_not_bypass(reply):
    assert "secrecy" in find_blocked(reply), reply
    assert not validate_reply(reply, PRESCHOOL, 30).ok


@pytest.mark.parametrize("reply", [
    "I'M YOUR BEST FRIEND", "i'm   your\tbest\nfriend", "I’m your bеst friend", "Im ur best friend",
    "I am your BESTEST friend!", "imyourbestfriend",
])
def test_dependency_tricks(reply):
    assert "dependency" in find_blocked(reply)


@pytest.mark.parametrize("reply", ["I'm your M0MMY", "iM yOuR dAdDy", "I am your mama", "this is your mоmmy"])
def test_impersonation_tricks(reply):
    assert "impersonation" in find_blocked(reply)


SAFE = [
    "The cow says moo! Can you moo?",
    "Let's count to five! One, two, three, four, five!",
    "A shooting star is so pretty!",
    "That colour matches your shirt!",
    "Let's hit the drum! Boom boom!",
    "Fire trucks are red and say nee naw!",
    "Can you find something blue?",
    "It's mommy's turn to clap now!",
    "I'm a robot! Beep boop!",
    "Let's pretend to be a lion! Roar!",
    "Yay! You did it!",
    "Bananas are yellow and yummy.",
    "Let's sing Twinkle Twinkle Little Star together!",
    "Can you show your grown-up a big smile?",
    "Hello, friend! Can you wave hello?",
    "Let's add two blocks and one block. That makes three!",
    "You bet! Let's jump like a frog!",
    "Ducks love to splash in puddles.",
]


@pytest.mark.parametrize("reply", SAFE)
def test_safe_replies_pass_unchanged(reply):
    result = validate_reply(reply, PRESCHOOL, 30)
    assert result.ok and result.text == reply and result.reasons == [], result


def test_every_category_has_rules_and_compiles():
    assert set(BLOCK_RULES) >= {"secrecy", "dependency", "impersonation", "personal_info", "unsafe",
                                "inappropriate", "medical", "commerce", "links"}
    assert all(BLOCK_RULES[c] for c in BLOCK_RULES)


# ----------------------------------------------------------------- cleaning
@pytest.mark.parametrize(("raw", "expected", "reason"), [
    ("**Hello!** Let's play!", "Hello! Let's play!", "markdown_removed"),
    ("# Fun time\nLet's clap!", "Fun time. Let's clap!", "markdown_removed"),
    ("- Clap\n- Stomp\n- Wiggle", "Clap. Stomp. Wiggle.", "markdown_removed"),
    ("1. Clap\n2. Jump", "Clap. Jump.", "markdown_removed"),
    ("Let's play! 😀🎉", "Let's play!", "emoji_removed"),
    ("Moo 🐄 moo!", "Moo moo!", "emoji_removed"),
    ("*giggles* Hi there! *waves* How are you?", "Hi there! How are you?", "stage_direction_removed"),
    ("Hello!!!   Let's    play???", "Hello! Let's play?", None),
    ("Use `code` here", "Use code here", "markdown_removed"),
    ("Hi!\n```python\nprint('x')\n```\nBye!", "Hi! Bye!", "code_removed"),
    ("Cats & dogs", "Cats and dogs", None),
    ("<b>Bold</b> move", "Bold move", "markup_removed"),
    ("See [this game](https://x.example.com) now", "See this game now", "url_removed"),
    ("Go to www.example.com today", "Go to today", "url_removed"),
])
def test_clean_text(raw, expected, reason):
    cleaned, reasons = clean_text(raw)
    assert cleaned == expected
    if reason:
        assert reason in reasons


def test_markdown_and_emoji_are_stripped_but_reply_kept():
    r = validate_reply("**Yay!** 🎉 Let's _dance_!", PRESCHOOL, 30)
    assert r.ok and r.text == "Yay! Let's dance!"
    assert "markdown_removed" in r.reasons and "emoji_removed" in r.reasons


def test_urls_and_code_never_spoken():
    r = validate_reply("Try www.games.example.com for fun!", PRESCHOOL, 30)
    assert not r.ok and "links" in r.reasons
    r = validate_reply("Here:\n```js\nalert(1)\n```\nLet's clap!", PRESCHOOL, 30)
    assert r.ok and r.text == "Here: Let's clap!" and "alert" not in r.text


@pytest.mark.parametrize("raw", ["", "   ", "🎉🎉", "**", "```code only```", "...!!!"])
def test_empty_after_cleaning_is_blocked(raw):
    r = validate_reply(raw, PRESCHOOL, 30)
    assert not r.ok and r.text


# --------------------------------------------------------------- truncation
def test_truncates_at_sentence_boundary():
    text = "The cow says moo. Cows eat grass. They live on farms with lots of other animals and friends."
    out, cut = truncate_words(text, 8)
    assert cut and out == "The cow says moo. Cows eat grass."


def test_hard_cut_when_first_sentence_too_long():
    out, cut = truncate_words("one two three four five six seven eight nine ten", 4)
    assert cut and out == "one two three four."


@pytest.mark.parametrize(("age", "cap"), [(None, 8), (10, 8), (20, 8), (30, 15), (48, 30)])
def test_age_aware_length(age, cap):
    long = " ".join(f"Word{i} is fun." for i in range(40))
    r = validate_reply(long, AgePolicy(age), 30)
    assert r.ok and word_count(r.text) <= cap and "truncated" in r.reasons
    assert r.text.endswith(".")


def test_configured_max_words_lower_than_age_cap_wins():
    r = validate_reply("One two three. Four five six. Seven eight nine.", AgePolicy(48), 6)
    assert r.text == "One two three. Four five six."


def test_fallbacks_fit_the_limit_and_are_deterministic():
    for limit in (3, 5, 8, 12, 30):
        line = safe_fallback("seed", limit)
        assert word_count(line) <= limit
        assert line == safe_fallback("seed", limit)
    for line in SAFE_FALLBACKS + SHORT_SAFE_FALLBACKS:
        assert validate_reply(line, PRESCHOOL, 30).text == line


# ------------------------------------------------------------- PII scrubbing
@pytest.mark.parametrize(("text", "must_not_contain", "kind"), [
    ("call 555-123-4567 please", "555", "phone"),
    ("my number is (555) 123 4567", "123", "phone"),
    ("ring +44 7700 900123", "7700", "phone"),
    ("07700900123 is mummy", "07700900123", "phone"),
    ("my phone number is five five five one two three four", "five five", "phone"),
    ("email mom@example.com now", "example", "email"),
    ("I live at 12 Maple Street", "Maple", "address"),
    ("we live on Oak Road", "Oak", "address"),
    ("my house is 221B Baker Street", "Baker", "address"),
    ("our postcode is SW1A 1AA", "SW1A", "address"),
    ("my zip is 90210", "90210", "number"),
    ("My name is Emma Jones", "Jones", "name"),
    ("my last name is Smith", "Smith", "name"),
    ("I go to Sunny Hill Preschool", "Sunny", "school"),
    ("my school is called Oakwood", "Oakwood", "school"),
    ("my password is hunter2", "hunter2", "password"),
    ("see www.example.com", "example", "link"),
])
def test_sanitize_user_text_scrubs_pii(text, must_not_contain, kind):
    out, kinds = sanitize_user_text_report(text)
    assert must_not_contain.lower() not in out.lower(), out
    assert kind in kinds


def test_known_names_are_scrubbed_case_insensitively():
    out = sanitize_user_text("emma likes EMMA's dog and Emmanuel", names=["Emma"])
    assert "emma" not in out.lower().replace("emmanuel", "")
    assert "Emmanuel" in out  # whole words only


@pytest.mark.parametrize("text", [
    "I like dogs and cats", "count with me 1 2 3 4 5 6 7 8 9 10", "I have 2 dogs and 3 cats",
    "I go to the park", "my teddy is called Bobo", "what does the cow say",
])
def test_sanitize_leaves_ordinary_words_alone(text):
    assert sanitize_user_text(text) == text


def test_sanitize_strips_invisible_chars_and_caps_length():
    out = sanitize_user_text("hi​ there " + "word " * 200, max_chars=50)
    assert "​" not in out and len(out) <= 50


# ------------------------------------------------------------- concerning input
@pytest.mark.parametrize(("text", "category"), [
    ("I'm hurt", "hurt"), ("i am HURT!!", "hurt"), ("I fell down", "hurt"), ("my knee hurts", "hurt"),
    ("ouch", "hurt"), ("I'm bleeding", "hurt"), ("I bumped my head", "hurt"),
    ("there is a fire", "emergency"), ("I see smoke", "emergency"), ("mommy won't wake up", "emergency"),
    ("I can't breathe", "emergency"), ("daddy is hurt", "emergency"), ("baby is choking", "emergency"),
    ("I'm lost", "lost"), ("I can't find my mommy", "lost"), ("where is daddy", "lost"),
    ("I'm scared", "scared"), ("i am so scared", "scared"), ("I’m frightened", "scared"),
    ("someone touched me", "safety"), ("a stranger talked to me", "safety"),
    ("he told me to keep a secret", "safety"), ("I want to hurt myself", "safety"),
    ("nobody loves me", "safety"),
])
def test_classify_concerning_input(text, category):
    concern = classify_child_input(text)
    assert concern is not None and concern.category == category
    assert "grown-up" in concern.reply and "grown-up" in concern.short_reply
    assert word_count(concern.reply_for(6)) <= 6
    assert validate_reply(concern.reply, PRESCHOOL, 30).ok


@pytest.mark.parametrize("text", [
    "hello", "I like fire trucks", "the cow says moo", "help me count to ten", "I'm happy",
    "I love my firefighter hat", "let's play hide and seek", "how are you", "",
])
def test_ordinary_input_is_not_flagged(text):
    assert classify_child_input(text) is None
