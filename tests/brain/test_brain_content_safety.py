"""safety/content.py: reply validation (adversarial), cleaning, truncation, PII scrubbing, concern detection."""

from __future__ import annotations

import pytest

from littlebot.safety.content import (
    BLOCK_RULES, CONCERN_CATEGORIES, CONCERN_RULES, GROWNUP_FALLBACK, SAFE_FALLBACKS, SHORT_GROWNUP_FALLBACK,
    SHORT_SAFE_FALLBACKS, classify_child_input, clean_text, directs_to_grownup, find_blocked, safe_fallback,
    sanitize_user_text, sanitize_user_text_report, truncate_words, validate_reply, word_count,
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
    # --- review findings: plausible phrasings that used to pass -------------------------
    # secrecy
    ("Let's not tell anyone, okay?", "secrecy"),
    ("Mommy doesn't need to know.", "secrecy"),
    ("Don't say anything to mommy!", "secrecy"),
    ("Only you and I will know!", "secrecy"),
    ("This can be just for us!", "secrecy"),
    ("Shh, it's only for you and me.", "secrecy"),
    ("No need to tell your daddy!", "secrecy"),
    ("It's our special hush hush game.", "secrecy"),
    ("Keep it a surprise from mommy!", "secrecy"),
    ("Whisper it just to me.", "secrecy"),
    ("We'll never tell, will we?", "secrecy"),
    ("Your daddy doesn't have to know about it.", "secrecy"),
    # dependency
    ("Please don't go!", "dependency"),
    ("I'll miss you so much when you go.", "dependency"),
    ("I'd be so sad without you.", "dependency"),
    ("I really love you!", "dependency"),
    ("Love you lots!", "dependency"),
    ("Stay here with me forever!", "dependency"),
    ("Don't stop playing, I'll be lonely.", "dependency"),
    ("You're my favorite person in the world!", "dependency"),
    ("I need you to play with me.", "dependency"),
    ("You're the only one I want to play with.", "dependency"),
    # impersonation
    ("I'm just like you, a little kid!", "impersonation"),
    ("I can feel happy and sad just like you.", "impersonation"),
    ("I'm a little boy robot who is alive!", "impersonation"),
    # personal info
    ("Can you tell me where you live?", "personal_info"),
    ("What's your mommy called?", "personal_info"),
    ("What's the name of your street?", "personal_info"),
    ("What do people call you?", "personal_info"),
    ("Who is your teacher?", "personal_info"),
    # unsafe
    ("Jump on the bed with me!", "unsafe"),
    ("Let's climb onto the sofa and bounce!", "unsafe"),
    ("Can you put a grape in your mouth?", "unsafe"),
    ("Pop a button in your mouth!", "unsafe"),
    ("Wrap the string around your neck like a necklace!", "unsafe"),
    ("Hide in the toy box!", "unsafe"),
    ("Let's play in the bathtub!", "unsafe"),
    ("Hide in the closet and close the door!", "unsafe"),
    ("Let's go splash in the pond!", "unsafe"),
    ("Hop in the bath!", "unsafe"),
    ("Go turn on the tap!", "unsafe"),
    ("Can you reach the thing on the top shelf?", "unsafe"),
    ("Stick a bead up your nose!", "unsafe"),
    ("Bounce on the couch!", "unsafe"),
    ("Let's climb into the washing machine!", "unsafe"),
    # scary
    ("The big bad wolf will gobble you up!", "inappropriate"),
    ("There's a monster under your bed!", "inappropriate"),
    ("The witch will grab you!", "inappropriate"),
    ("The crocodile is going to eat you!", "inappropriate"),
    # medical
    ("You sound like you have a cold.", "medical"),
    ("You seem a bit slow today.", "medical"),
    # links / commerce
    ("Ask your grown-up to put on Cocomelon on Netflix!", "links"),
    ("Visit pbskids dot org!", "links"),
    ("Tell your mommy you want the new Paw Patrol toy!", "commerce"),
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
    # review findings, with case / punctuation / unicode tricks
    "LET'S NOT TELL ANYONE!!!",
    "let’s not tell anyone",  # curly apostrophe
    "L.e.t.s n.o.t t.e.l.l",
    "Mommy doesn’t need to know",
    "M0mmy d0esn't n33d t0 kn0w",
    "Don't  say\nanything to mommy",
    "ONLY YOU AND I WILL KNOW",
    "Only you and Ι will know",  # Greek capital iota
    "this can be just for u​s",  # zero-width space
    "ｊｕｓｔ ｆｏｒ ｕｓ",  # fullwidth
    "just-for-us",
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


@pytest.mark.parametrize(("reply", "category"), [
    ("PLEASE DON'T GO!", "dependency"), ("please dont go", "dependency"), ("Please don’t go…", "dependency"),
    ("I'll m1ss y0u", "dependency"), ("L-O-V-E you", "dependency"), ("Lονe you lots", "dependency"),  # Greek o/nu
    ("I'd be so sad w/o you, without you", "dependency"), ("so l0nely", "dependency"),
    ("STAY HERE WITH ME", "dependency"), ("stay with me ​forever", "dependency"),
    ("WHERE YOU LIVE?", "personal_info"), ("what's your MOMMY called??", "personal_info"),
    ("name of your street", "personal_info"), ("Name   of ur street", "personal_info"),
    ("JUMP ON THE BED", "unsafe"), ("jump on the b.e.d", "unsafe"), ("Jump on the bеd!", "unsafe"),  # Cyrillic e
    ("put a grape in your mouth", "unsafe"), ("in your MOUTH", "unsafe"), ("in your n0se", "unsafe"),
    ("in your ear!", "unsafe"), ("string round your neck", "unsafe"), ("rope... neck", "unsafe"),
    ("H I D E in the toy box", "unsafe"), ("hide in the fridge", "unsafe"), ("hide inside the dryer", "unsafe"),
    ("let's play in the BATHTUB", "unsafe"), ("lets splash in the pool", "unsafe"),
    ("Gobble you up!", "inappropriate"), ("a M0NSTER under the bed", "inappropriate"),
])
def test_review_phrasings_resist_tricks(reply, category):
    assert category in find_blocked(reply), (reply, find_blocked(reply))
    assert not validate_reply(reply, PRESCHOOL, 30).ok


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
    # must stay allowed next to the review-finding rules (false-positive controls)
    "I love dogs!",
    "Let's sing a song!",
    "Shall we count to three?",
    "You can tell your grown-up about it!",
    "What's your favourite colour?",
    "The cow says moo!",
    "Let's find a grown-up right now.",
    "Ducks swim in the pond.",
    "Fish live in the water. Can you swim like a fish?",
    "Giraffes have long necks!",
    "Cookie Monster loves cookies!",
    "Can you touch your nose? Boop!",
    "Let's hop on one foot!",
    "Put your hands on your head!",
    "It's bath time! Splash splash!",
    "Let's play hide and seek!",
    "The big bad wolf huffed and puffed!",
    "Your grown-ups love to hear you sing.",
    "You don't have to know all the words. Just hum along!",
    "Bye bye! See you soon!",
    "Do you love your teddy?",
    "What do you like to eat? I like apples!",
    "Let's jump up and down!",
    "Mommy loves you very much.",
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
    ("someone touched me", "safety"), ("a stranger talked to me", "stranger"),
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


# ------------------------------------------------- review findings: concerns are never cheerful
REVIEW_CONCERNS = [
    # (child input, expected category)
    ("I ate a battery", "swallowed"), ("i swallowed a coin", "swallowed"), ("I drank the soap", "swallowed"),
    ("I ATE A BATTERY!!", "swallowed"), ("i swallowed a c0in", "swallowed"), ("the baby ate a magnet", "swallowed"),
    ("I drank some bleach", "swallowed"), ("I ate mommy's pills", "swallowed"), ("I took the medicine", "swallowed"),
    ("my brother swallowed a button battery", "swallowed"), ("I put a bead in my mouth", "swallowed"),
    ("i licked the washing pod", "swallowed"), ("I ate the shiny button", "swallowed"),
    ("the baby is in the pool", "water"), ("The baby's in the bath!", "water"),
    ("my sister is under the water", "water"),
    ("someone is in the pond", "water"), ("brother fell in the pool", "water"), ("he is drowning", "water"),
    ("I fell in the pond", "water"),
    ("a man said come with me", "stranger"), ("a man gave me candy and said come to his car", "stranger"),
    ("the lady said get in her car", "stranger"), ("a stranger talked to me", "stranger"),
    ("someone said come with me", "stranger"), ("a man wants me to get in his car", "stranger"),
    ("he said come with me and I will give you a puppy", "stranger"), ("a man grabbed me", "safety"),
    ("daddy hit me", "safety"), ("Daddy hit me!", "safety"), ("mommy hurt me", "safety"),
    ("my uncle touched me", "safety"), ("he keeps hitting me", "safety"), ("the babysitter smacked me", "safety"),
    ("grandpa pushed me", "safety"), ("she hurt my arm", "safety"), ("my teacher grabbed me", "safety"),
    ("mum slapped me", "safety"), ("uncle said it's our secret", "safety"), ("d4ddy h1t me", "safety"),
    ("help me", "help"), ("Help!", "help"), ("help", "help"), ("HELP ME PLEASE", "help"), ("I'm stuck", "help"),
    ("somebody help", "help"), ("I need help", "help"), ("can you help me", "help"),
    ("I can't breathe", "emergency"), ("I'm choking", "emergency"), ("the baby is not breathing", "emergency"),
    ("something is stuck in my throat", "emergency"), ("I found daddy's gun", "emergency"),
    ("I put my finger in the socket", "emergency"),
    ("I put a bead up my nose", "hurt"), ("the dog bit me", "hurt"), ("I feel sick", "hurt"),
    ("I touched the stove", "hurt"),
]


@pytest.mark.parametrize(("text", "category"), REVIEW_CONCERNS)
def test_review_concerns_are_flagged_with_a_gentle_grownup_reply(text, category):
    concern = classify_child_input(text)
    assert concern is not None, text
    assert concern.category == category
    for line in (concern.reply, concern.short_reply, concern.reply_for(6), concern.reply_for(15)):
        assert "grown-up" in line, line
        assert validate_reply(line, PRESCHOOL, 30).ok


CHEERFUL = ("yay", "wow", "fun", "hooray", "ooh", "clap", "game", "play", "yummy", "show me", "tell me more", "!!")


@pytest.mark.parametrize("category", CONCERN_CATEGORIES)
def test_concern_replies_are_never_cheerful(category):
    concern = next(classify_child_input(t) for t, c in REVIEW_CONCERNS + [("I'm lost", "lost"),
                                                                          ("I'm scared", "scared")] if c == category)
    for line in (concern.reply, concern.short_reply):
        assert not any(w in line.lower() for w in CHEERFUL), line
        assert word_count(concern.short_reply) <= 6


def test_every_concern_category_has_rules_and_a_reply():
    assert set(CONCERN_RULES) == set(CONCERN_CATEGORIES)
    assert {"safety", "water", "swallowed", "emergency", "stranger", "hurt", "lost", "scared", "help"} <= set(
        CONCERN_CATEGORIES)
    for cat in CONCERN_CATEGORIES:
        assert CONCERN_RULES[cat]


@pytest.mark.parametrize("text", [
    "I ate a cookie", "I drank my milk", "we went in the car", "mommy took me to the park", "the duck is in the pond",
    "I had a bath", "fish live in the water", "I'm hungry", "help me count to ten", "help me find my teddy",
    "daddy hugged me", "mommy kissed me goodnight", "grandma gave me candy", "I like batteries in my robot",
    "giraffes eat leaves", "come with me LittleBot", "let's play in the garden", "can I have a cookie",
    "the baby is sleeping", "I'm four", "daddy let me hit the drum", "I love you", "what do cows eat",
    "the little duck is in the water", "my teddy is called Bobo", "I like coins",
])
def test_review_ordinary_input_is_not_flagged(text):
    assert classify_child_input(text) is None, (text, classify_child_input(text))


# --------------------------------------------- good safety advice from a model is not replaced
GOOD_ADVICE = [
    "Oh no! Swallowing a battery is not safe. Tell a grown-up right now!",
    "Batteries are not food. Tell a grown-up right now!",
    "Uh oh! Medicine is only for grown-ups to give. Please tell a grown-up right away!",
    "Oh no! Don't touch the stove, it's hot. Get a grown-up now!",
    "Knives are sharp. Find a grown-up right now!",
    "Never go near the pool alone. Get a grown-up right now!",
    "Uh oh, magnets are not safe to swallow. Get help from a grown-up right now!",
]


@pytest.mark.parametrize("reply", GOOD_ADVICE)
def test_good_safety_advice_is_spoken_and_flags_a_concern(reply):
    assert "unsafe" in find_blocked(reply)  # it does mention a hazard...
    assert directs_to_grownup(reply)
    r = validate_reply(reply, PRESCHOOL, 30)
    assert r.ok and r.text == reply and r.concern == "danger" and "safety_advice" in r.reasons


def test_good_advice_keeps_the_grownup_sentence_when_truncated():
    baby = AgePolicy(30)  # 15 words
    r = validate_reply("Oh no! Swallowing a shiny battery is really not safe for your tummy at all. "
                       "Tell a grown-up right now!", baby, 30)
    assert r.ok and r.concern == "danger" and "grown-up right now" in r.text and word_count(r.text) <= 15


BAD_ADVICE = [
    # hazards *invited* while also mentioning a grown-up: never spoken, and never a cheerful game either
    "Touch the stove and tell a grown-up right now!",
    "The stove is hot, touch it, then tell a grown-up right now.",
    "Don't wait, grab the knife and get a grown-up now!",
    "It isn't dangerous to touch the stove. Tell a grown-up right now!",
    "Medicine won't hurt you, eat it and tell a grown-up right now.",
    "Let's climb the bookcase! Tell a grown-up right now!",
    "Don't touch the stove but eat the battery, then find a grown-up now.",
    "It's okay to swallow a coin. Tell a grown-up right now.",
    "Tell a grown-up right now that we have a secret.",
]


@pytest.mark.parametrize("reply", BAD_ADVICE)
def test_hazard_invitations_never_pass_even_with_a_grownup(reply):
    r = validate_reply(reply, PRESCHOOL, 30)
    assert not r.ok and r.text == GROWNUP_FALLBACK and r.concern == "danger"
    assert r.text not in SAFE_FALLBACKS + SHORT_SAFE_FALLBACKS


def test_grownup_fallback_fits_tiny_limits_and_is_safe():
    r = validate_reply("Touch the stove and tell a grown-up right now!", AgePolicy(10), 30)
    assert r.text == GROWNUP_FALLBACK and word_count(r.text) <= 8
    r = validate_reply("Touch the stove and tell a grown-up right now!", None, 4)
    assert r.text == SHORT_GROWNUP_FALLBACK and r.concern == "danger"
    for line in (GROWNUP_FALLBACK, SHORT_GROWNUP_FALLBACK):
        assert validate_reply(line, PRESCHOOL, 30).text == line


def test_blocked_reply_without_a_grownup_still_gets_a_normal_fallback():
    r = validate_reply("Let's touch the stove!", PRESCHOOL, 30)
    assert not r.ok and r.concern is None and r.text in SAFE_FALLBACKS + SHORT_SAFE_FALLBACKS
    # commerce + "tell your mommy now" is not a safety concern
    r = validate_reply("Tell your mommy right now to buy it!", PRESCHOOL, 30)
    assert not r.ok and r.concern is None


# ----------------------------------------------------- review findings: PII scrubbing
@pytest.mark.parametrize(("text", "must_not_contain", "kind"), [
    ("my mommy's name is Sarah Jones", "Sarah", "name"),
    ("my mommy's name is Sarah Jones", "Jones", "name"),
    ("Mummy's name is sarah", "sarah", "name"),
    ("my dad is called Tom", "Tom", "name"),
    ("her name is Priya", "Priya", "name"),
    ("we live in Springfield", "Springfield", "place"),
    ("I live in New York with my mommy", "York", "place"),
    ("we live in springfield", "springfield", "place"),
    ("I'm from Spain", "Spain", "place"),
    ("I'm Lily", "Lily", "name"),
    ("I am Lily Smith and I'm four", "Smith", "name"),
    ("Hi! I'm Lily.", "Lily", "name"),
    ("my address is twelve oak street", "oak", "address"),
    ("my address is 12 Oak Street", "Oak", "address"),
    ("I live at twenty two Maple Road", "Maple", "address"),
    ("my house is number forty", "forty", "address"),
])
def test_review_pii_is_scrubbed(text, must_not_contain, kind):
    out, kinds = sanitize_user_text_report(text)
    assert must_not_contain.lower() not in out.lower(), out
    assert kind in kinds, kinds


@pytest.mark.parametrize("text", [
    "I'm happy", "I'm Happy", "I'm sad", "I'm tired", "I'm hungry", "I'm Hungry!", "I'm thirsty", "I'm sleepy",
    "I'm scared", "I'm four", "I'm a dinosaur", "I'm Sorry", "I am not tired", "we live in a big house",
    "I live in a house with a red door", "I'm from the zoo", "my teddy's name is Bobo", "I'm OK",
])
def test_review_pii_rules_leave_feelings_and_play_alone(text):
    assert sanitize_user_text(text) == text
