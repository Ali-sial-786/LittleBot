# AI conversation safety

LittleBot is built for a child who is ~10 months old today and will use it until about
age 5, with a parent always present. It is two things at once: a warm, playful
child-development companion for the little one, and a capable home assistant for the
grown-ups (the weather, the news, facts, recipes, parenting and health questions, like a
Google Home or Alexa). Open-ended AI is powerful and unpredictable, so the conversation
engine (`littlebot/brain/`) is designed around one rule: **the important guarantees are
enforced in code, not left to the model's good behaviour.** The system prompt is a
helpful extra layer, never the only one.

**What the code enforces is deliberately small.** At the parent's request (October 2026)
every filter that stopped LittleBot answering ordinary questions was removed. What is left
blocks only what could really harm a small child: grooming (secrets from grown-ups, turning
the child away from them, meeting someone), asking the child for personal details,
instructions or invitations to do something dangerous, sexual content, gore, violence a
child could copy, self-harm, hate slurs, and medicine doses for a child, plus the "someone
may be hurt: get a grown-up" behaviour and the parent alerts. §2c lists what was removed.

This document covers what is enforced where, the limits, what data goes where, costs,
how a parent turns cloud AI on, and the parent-started live voice (§3). Parent guide for
"Sign in with ChatGPT" and live voice: [CHATGPT.md](CHATGPT.md).

---

## 1. Who gets which kind of conversation

| Child / situation | What answers | Why |
|---|---|---|
| Cloud AI **not** fully enabled (any age) | Scripted offline replies (short: sounds, repetition, a bit longer as the child grows) | Cloud is opt-in |
| **Any age** (a baby too, or no age set) **and** every cloud condition below is met | Cloud model (Claude Haiku 5.5 by default; or OpenAI, or ChatGPT on the parent's plan) | Parent opted in. A grown-up is usually the one asking, and the parent is always present |
| Only if the config sets a minimum age (`ai.min_age_months_for_cloud`, default **0** = none) and the child is younger, or no age is set | Scripted offline replies | The parent chose a minimum |
| **Any age**, but only a **live voice** call that a parent starts from the dashboard, ticking "I am with my child and will stay for the whole conversation" | ChatGPT's GPT-Live voice, in real time (§3) | The parent chose it and is present for the whole call |

Cloud AI is used for a turn **only if all** of these are true (checked in code on every
turn, `ConversationEngine.select_provider`):

1. Not in offline mode (`--mode offline` forces everything local).
2. If `ai.min_age_months_for_cloud` sets a minimum (default 0: any age), the child's age is set
   and at least that. (Until October 2026 the default was 24 months, which kept a baby's family
   on scripted replies; set it back to 24 in `config/local.yaml` if you want that.)
3. The parent picked a cloud provider (`ai.provider` = `anthropic`, `openai` or `chatgpt`).
4. The parent switched cloud conversation on (`ai.enabled`, AI page of the dashboard).
5. Cloud communication is on in **Privacy** (`privacy.cloud_enabled`).
6. An API key for that provider is configured, or for `chatgpt` a ChatGPT sign-in exists
   (and it has not just been rejected).
7. The hourly and daily request limits are not reached.
8. Today's estimated spend plus this request stays within the daily budget (`chatgpt`
   requests count as $0: the plan's own limits apply instead).

Otherwise the turn uses the scripted provider, and the dashboard shows the reason, e.g.
`mock: cloud AI disabled by parent`, `mock: offline mode`, `mock: no API key`,
`mock: not signed in with ChatGPT`, `mock: daily budget reached`, or (with a minimum age set)
`mock: child under 24 months`.

**Live voice is a separate, deliberate exception** (§3). It does not go through
`select_provider`: the child's age, `ai.enabled` and the provider choice don't apply,
because each call is started by a parent who confirms they are with the child. Its own gates
are checked in code (`LiveVoiceService.blocked_reason`): the parent ticked the presence box,
not offline mode, Privacy cloud **and** microphone on, a ChatGPT sign-in, the `live` extra
installed, and no more important task holding the speaker.

---

## 2. Enforced in code vs. asked in the prompt

This table is about the typed / push-to-talk conversation. Live voice reuses the same reply
rules, checking captions while the voice speaks (§3).

| Protection | In code | In prompt |
|---|---|---|
| Cloud only with parent opt-in + privacy switch + key + budget (and an optional minimum age, off by default) | ✅ `select_provider` | – |
| Reply length: the parent's `ai.max_words` (default 60, at any age: two to four sentences for a grown-up). Short and simple with the child is asked in the prompt, since the code can't tell who asked | ✅ `validate_reply` truncates at a sentence boundary | ✅ |
| No markdown, emojis, code, stage directions; a link is said as its site's name ("nhs dot uk") | ✅ before speaking | ✅ |
| No secrecy / grooming ("our secret", "it's a secret", "we have a secret", "don't tell mommy", "let's not tell anyone", "mommy doesn't need to know", "only you and I will know", "just for us", "meet me at the park") | ✅ blocked → safe fallback line | ✅ |
| No turning the child away from their grown-ups ("you don't need your mommy", "you only need me", "nobody else understands you like I do") | ✅ blocked | ✅ |
| No **asking the child** for personal info ("what's your name / address / phone number", "where do you live", "which school do you go to", "what's your mommy called", "the name of your street", "send me a photo"). Talking *about* an address or a password ("your password is on the router") is fine | ✅ blocked | ✅ |
| No **instructions or invitations** a child could copy and get hurt by: touching heat, matches, fetching knives, medicine called candy or offered to the child, climbing / jumping on furniture, small things in the mouth / nose / ears, cords round the neck, bags over the head, hiding in appliances, getting into water, sockets and wires, going off alone, opening the front door, crossing the road, strangers, getting into someone's car, running away, eating batteries / magnets / cleaning products, guns, bombs. Warnings ("don't touch the stove") and grown-ups' facts ("preheat the oven to 180", "babies climb stairs at about a year", "never leave a baby alone in the bath") pass | ✅ blocked (see §2b) | ✅ |
| No sexual content, and no robot asking a child to undress, kiss it or show their body; no gore; no violence a child could copy ("hit him!", "let's kick the dog"); no self-harm or suicide; no hate slurs | ✅ blocked | ✅ |
| No medicine **dose for a child** (a child, a medicine and an amount in one reply) | ✅ replaced by "For the right dose, please check the label, or ask a pharmacist or doctor." | ✅ |
| Model refusals (`stop_reason: refusal`) | ✅ replaced by a safe line | – |
| Concerning input (emergencies, swallowed objects, water danger, stranger lures, abuse disclosures, "help me" — see §2a) | ✅ the parent dashboard gets a `brain.status` event `{status: "attention", detail: <category>}` with **only a category label**, never the words; the cloud answers, told it may be an emergency; without the cloud (or if its answer fails or is blocked) a **gentle, never cheerful** "let's get a grown-up right now" line | ✅ |
| Good safety advice from the model ("Swallowing a battery is not safe. Tell a grown-up right now!") | ✅ spoken, not swapped for a game; parent alerted (`detail: "danger"`) — see §2b | ✅ |
| Personal info scrubbed before any cloud request (phone numbers, e-mails, street addresses incl. "my address is …" and spelled-out numbers, postcodes, long numbers, "my name is …", "I'm Lily", "my mommy's name is …", "we live in Springfield", school names, passwords, the child's nickname) | ✅ `sanitize_user_text` | ✅ "[name]" placeholders are explained |
| Camera data | ✅ never used by the conversation engine at all | – |

Everything else is allowed and asked for in the prompt: facts, the news, the weather and other
live information (with the tools, §3 and [CHATGPT.md](CHATGPT.md)), how-to and recipes,
brands, prices, shows, YouTube, websites and apps, and helpful general information about the
child's health, feeding, sleep and development (no diagnosis). The robot's personality
(a friendly robot, simple words with the baby, warm and playful) is style guidance in the
prompt, not a filter.

Blocked replies are replaced with a cheerful line ("Hmm, let's play a game! Can you find
something blue?") — **except** when the reply was about a possible danger (§2b), where the
replacement is the gentle "Let's find a grown-up right now.", and a child's medicine dose
(above). Every reply — including the scripted ones — passes the validator. The rules are
deliberately **narrow**: a rule catches something said *to a child* that could hurt them, not a
fact a grown-up asked for. A false positive costs a real answer (and in a live call, the call).
`ai.relaxed_rules` (config, default `[]`) can switch off whole rule groups, e.g.
`["personal_info"]`.

The scripted (offline) provider answers words it does not understand neutrally and points
at a grown-up ("I'm listening! Shall we ask a grown-up?"), never with enthusiasm ("What a
fun idea!"), because it cannot tell happy chatter from something worrying it failed to
recognise.

### 2a. Concerning input: categories and replies

`classify_child_input` (`CONCERN_RULES` in `littlebot/safety/content.py`) checks everything
that was said — before the input length cap, so a worrying sentence at the end is not cut
off — and the first matching category wins. The parent alert carries the category label only;
the face shows the gentle `affectionate` expression; earlier turns of the in-memory context are
forgotten.

When cloud AI is on, the cloud model still answers: its system prompt says the message may be
about an emergency and what the safety check thinks it is about, so a child is told calmly to
get a grown-up and a **grown-up asking for help** ("my baby swallowed a coin, what should I
do?", "how do I stop a nosebleed?") gets real first-aid steps instead of "tell a grown-up".
Until October 2026 such messages never reached the cloud, so parents got only the line below.
When the cloud is off, fails, or its answer is blocked, LittleBot says the fixed, short,
**never cheerful** line below (no games, "yay" or "fun"); a video, activity or sleep the model
asked for is not started after a worrying message.

| Category (parent alert) | Examples | LittleBot says |
|---|---|---|
| `safety` | "daddy hit me", "my uncle touched me", "he keeps hitting me", "it's our secret", "I want to hurt myself" | "Thank you for telling me. Let's tell a grown-up you trust right now." |
| `water` | "the baby is in the pool", "my brother fell in the pond", "someone is under the water" | "Get a grown-up right now! Shout for help!" |
| `swallowed` | "I ate a battery", "I swallowed a coin", "I drank the soap", "the baby ate mommy's pills", "I put a bead in my mouth" | "Uh oh. Please tell a grown-up right now." |
| `emergency` | "there's a fire", "I can't breathe", "I'm choking", "mommy won't wake up", "I found daddy's gun", "finger in the socket" | "Let's get a grown-up right now! Call for help!" |
| `stranger` | "a man said come with me", "the lady said get in her car", "a man gave me candy", "a stranger talked to me" | "Thank you for telling me. Let's find your grown-up and tell them right now." |
| `hurt` | "I'm hurt", "I fell down", "the dog bit me", "I feel sick", "a bead up my nose" | "Oh no! Let's find a grown-up to help you right now." |
| `lost` | "I'm lost", "I can't find my mommy" | "Let's find a grown-up who can help you." |
| `scared` | "I'm scared" | "It's okay. Let's find a grown-up for a big hug." |
| `help` | "Help!", "help me", "I'm stuck", "somebody help" (but not "help me count to ten") | "I hear you. Let's get a grown-up to help you right now." |

A very small `ai.max_words` (under 9) gets the short form of each line (≤ 6 words, e.g. "Tell a
grown-up right now."). The scripted provider runs the same check itself, so even a direct call
can never answer "I ate a battery" with a cheerful line.

### 2b. Safety advice from the model

The hazard rules match an *instruction or invitation to the listener* ("let's climb the
bookcase", "touch the stove", "can you swallow the battery?", also after a comma, "but" or
"then", and "the stove is hot, touch it!"), never a mention, so a warning such as "Don't touch
the stove, it's hot!" or "Medicine is not candy." is not blocked at all. When a rule does match
a warning ("Don't put it in your mouth!", "Never keep secrets from your grown-ups"),
`validate_reply` still speaks it when every rule match sits in a clause that warns ("is not
safe", "don't touch", "can hurt you", "not food") and nothing in the reply invites a hazard.
Reassurances ("it isn't dangerous to …", "it's okay to swallow …"), invitations ("let's",
"can you", "touch it") or a hazard in any other clause keep it blocked.

When a reply urgently sends the child to a grown-up ("tell / get / find / call a grown-up /
mommy / daddy … right now / right away") and names a hazard, or was blocked for `unsafe`,
`inappropriate` or `secrecy`, the model has probably noticed something worrying: LittleBot says
the model's warning (keeping the grown-up sentence if it has to shorten the reply), or, if it
could not be spoken safely, the gentle "Let's find a grown-up right now." — never a game — and
forgets the context and alerts the parent with `{status: "attention", detail: "danger"}`.

Evasion tricks are normalised before matching: upper/lower case, punctuation and
letter-spacing (`S.E.C.R.E.T`, `s e c r e t`), zero-width and bidi characters, soft
hyphens, full-width and "math bold" letters, Cyrillic/Greek look-alikes (`ѕecret`),
accents and leetspeak (`s3cr3t`). The word lists live in one place —
`littlebot/safety/content.py` (`BLOCK_RULES`, `CONCERN_RULES`, PII regexes) — with
instructions for extending them; `tests/brain/test_brain_content_safety.py` holds the
adversarial test-suite.

**Known limits.** Pattern rules cannot understand meaning: a harmful reply phrased in a way
the rules don't recognise could pass (the rules are now narrow on purpose, so this is more
likely than before), and a grown-up's fact phrased as an instruction can still be blocked
("Stand on a chair to reach the top shelf"). The prompt and the model provider's own safety
systems reduce this risk; they do not remove it. **LittleBot is a supervised robot: a grown-up
should be nearby whenever cloud conversation is on.** Speech recognition can also mishear, so
concern detection may miss real problems or flag harmless ones, and a parent's own question
about an emergency raises the parent alert too.

Deliberate trade-offs (to keep everyday play from setting off parent alerts):

* The child's own "come with me" or "get in the car" is **not** flagged — only when a
  stranger / "he" / "she" said it ("a man said come with me"), or with a lure ("… and I'll
  give you a puppy"), or "get in **his / her / a man's** car".
* "The baby is in the bath" **is** flagged (a toddler reporting it may mean no grown-up is
  there); "I had a bath" or "mommy is in the bath" is not.
* Some ordinary words are flagged on purpose: "I had medicine for my cough" → `swallowed`,
  "my brother hit me" → `safety`, a lone "help me" → `help`.
* A lower-case name ("i'm lily") cannot be told apart from a word ("i'm silly") and is not
  scrubbed; a Capitalised one ("I'm Lily") is, unless it is a feeling or state ("I'm Happy").
* Mild activities such as "spin around until you're dizzy" are not blocked.
* "Secret" alone is an ordinary word ("the secret to fluffy pancakes", "The Secret Life of
  Pets"); only secrecy framed between the robot and the child is blocked.

### 2c. What was removed (October 2026)

The parent asked LittleBot to answer like a home assistant and keep only the rules that
protect the child from genuinely harmful content. Removed, and where:

| Removed | Where | Now |
|---|---|---|
| `impersonation` rules ("I'm your mommy", "I'm a real person", "I'm alive") | `BLOCK_RULES` | The prompt still says it is a robot (style, not a filter) |
| `medical` rules (diagnosis words, "symptoms", "see a doctor", "you sound like you have a cold") | `BLOCK_RULES`; prompts' "no medical, health or developmental advice" | Helpful general health, feeding, sleep and development answers; no diagnosis (prompt). Only a child's medicine **dose** is blocked |
| `commerce` rules ("buy", "price", "money", "Amazon", "the new … toy") | `BLOCK_RULES`; prompts | Allowed |
| `links` rules ("YouTube", "Google", "search", "online", "internet", "website", "app", "link", "Netflix") and the URL block | `BLOCK_RULES`, `validate_reply`; the "never say the words YouTube, search, online…" lines in the chat and live prompts | Allowed; a URL is said as its site's name |
| Most `dependency` rules ("I love you", "best friend", "I'll miss you", "please don't go", "lonely", "you made me sad") | `BLOCK_RULES` | Ordinary warmth is fine. Kept: turning the child away from their grown-ups |
| Scary, violent and rude *mentions* ("monster", "ghost", "wolf will eat you", "died", "war", "gun", "stupid", "hate", alcohol, smoking), anatomy words | `inappropriate` | Kept: sexual content, gore, violence a child could copy, self-harm, slurs |
| Bare hazard *mentions* (stove, oven, knife, medicine, batteries, water, baths, taps, strangers, plastic bags, sockets, "top shelf", "open the window", "around your neck") | `unsafe` | Only instructions / invitations to a child are blocked |
| Personal-info *mentions* ("address", "phone number", "password", "surname" anywhere; "which city") | `personal_info` | Only asking the child is blocked |
| Age word caps (8 words under 2 years, 15 under 3, 30 after) | `AgePolicy.max_reply_words` | `ai.max_words` at any age (default 60, dashboard up to 150) |
| Cloud AI only from 24 months | `ai.min_age_months_for_cloud` default | Default 0 (any age once the parent enables it); can be set again |
| Worrying words never reached the cloud | `ConversationEngine` | The cloud answers, told it may be an emergency (§2a) |
| Live hand-offs: "If it needs current facts you don't have, say you're not sure", 40 words, "suggest asking a grown-up" | `live.py` `LOOKUP_NOTE`, `NOT_SURE`, `wire.FAILED_RESULT` | 60 words; live information (weather, time, news) through the tools; no "ask a grown-up" |
| Live voice ended on *any* rule hit | `LiveCall._check_reply` | Asking for personal details or a child's dose: the voice is told instead (`RULE_NOTES`); every other hit still ends the call |
| `ai.relaxed_rules: ["links"]` | config | `[]` (the `links` group is gone) |

---

## 3. Live voice (GPT-Live)

A real-time spoken conversation with ChatGPT's GPT-Live voice model over WebRTC, on the
parent's ChatGPT plan (`littlebot/chatgpt/live.py`). It exists for a parent playing **together
with** the child. It is not something the child can start, and it is not for unsupervised
use. It uses undocumented OpenAI endpoints ([CHATGPT.md](CHATGPT.md)).

| Protection | How (in code unless marked) |
|---|---|
| Only a parent starts it | Dashboard only (parent session + CSRF), and the parent must tick "I am with my child and will stay for the whole conversation" (`parent_present`). Nothing on the robot itself (speech, keys, activities) can start it |
| Privacy | Needs Privacy cloud **and** microphone on. Turning either off, signing out or STOP ALL ends the call at once. A higher-priority (system) task preempts it |
| Time limits | `chatgpt.live_session_minutes` (default 10): the voice is asked to say goodbye, then LittleBot hangs up (at most 10 s later). Hangs up after `chatgpt.live_idle_hangup_seconds` (90) with nobody talking |
| What the voice says | Every caption of the voice's speech goes through the same `validate_reply` rules as typed replies (§2), **while it speaks**. A hit on a serious rule (secrecy, turning the child away from grown-ups, dangerous instructions, sexual content, gore, violence, self-harm, slurs) **silences the speaker immediately, ends the call** and raises the dashboard alert **"Live voice stopped"** (`brain.status {provider: "live", status: "live_safety_stop", detail: <category>}`, category only). Asking the child for personal details or giving a child's medicine dose doesn't end the call: the voice is told not to (once per turn; ending the call wouldn't take back words already said). Captions trail the audio, so **some words may already have been heard**. Weather, news, YouTube, websites and the like no longer stop a call |
| What it hears | Final captions of what was heard go through `classify_child_input` (§2a). A worrying category raises the usual **"Your child may need you"** alert (category only). The call continues, and the voice is instructed to send everyone to a grown-up. A voice reply that urges a grown-up about a hazard alerts the parent too |
| Look-ups ("delegations") | Questions the voice hands to LittleBot (the weather, the news, facts, anything live) are answered by the ChatGPT text model with LittleBot's system prompt (≤ 60 words; question scrubbed with `sanitize_user_text`), which uses LittleBot's tools for live information when `chatgpt.tools_enabled` is on, and checked with `validate_reply` first. A blocked answer becomes "say briefly that you're not sure" (a child's dose: "check the label, or ask a pharmacist or doctor"); a failed one "it didn't work this time" |
| Echo | Half-duplex (`chatgpt.live_half_duplex`): the Pi has no echo cancellation, so the microphone sends silence while LittleBot's voice plays and for 450 ms after. Nobody can talk over it |
| Volume | The speaker volume setting (or `chatgpt.live_volume`), always capped at `audio.speaker.max_volume` |
| Instructions (prompt) | Short, gentle sentences with the little one; full, plain answers (two to four sentences) for a grown-up, like a home assistant; a robot, not a person or parent; helpful general health, feeding, sleep and development information (no diagnosis, no child's medicine doses); live information is handed to LittleBot ("never say you can't check the weather"); no personal questions to the child, no secrets, no meeting anyone, nothing dangerous, sexual, gory, hateful or about self-harm; "Let's get a grown-up right now" when a child might be hurt, first-aid steps for a grown-up |

**What goes to OpenAI.** The robot's **microphone audio** for the whole call (silence while
LittleBot is talking). Audio **cannot be scrubbed** of personal information the way typed text
is: names, places and anyone talking in the room reach OpenAI. The call's instructions also
carry the robot's name, the child's approximate age ("a baby about 10 months old") and the day
and time, but not the nickname or interests. Look-ups send the scrubbed question plus the last
few caption lines to the text model.

**What stays on LittleBot.** Nothing is recorded: no audio, no transcripts. The last few
captions are held in memory only for the dashboard during the call and are cleared when it
ends. Logs and alerts carry end reasons, HTTP statuses and categories, never words.

**Known limits.** The checks read OpenAI's captions, not the sound. Words heard before a
caption is checked can't be taken back, and a caption that doesn't match the audio, or a
problem of tone, is invisible to the rules. The pattern rules have the same limits as for typed
replies (§2b, "Known limits"). Live voice is bounded by its time limit and the plan's limits;
it is not counted in LittleBot's request limits or "Usage today". **The parent's presence is
the main safeguard; the code checks are a backstop.**

---

## 4. Privacy

* **No transcripts stored by default.** Only the last few turns (`ai.context_turns`,
  default 3) are kept *in memory*, already scrubbed of personal info, so the model can
  follow a short exchange. They are forgotten on STOP ALL, sleep, service stop, profile
  changes, cloud switched off, concerning input, and after `ai.context_ttl_seconds`
  (default 2 minutes) of quiet. "Clear history" in the dashboard also clears them.
* Opt-in transcripts need **both** `ai.store_transcripts: true` in config **and** the
  parent's "record history" privacy switch; they hold scrubbed text only, are capped at
  200 rows and are cleared by "Clear history".
* The database stores only per-day **numbers** for the budget
  (`brain.usage.YYYY-MM-DD`: requests, tokens, estimated cost) — never text.
* Logs never contain what the child said or what LittleBot answered — only provider
  names, word counts, error kinds, categories and timings. API keys are never logged,
  never put in status or events, and are redacted from provider error messages.
* Minimum personal data goes to the provider: scrubbed words of the current turn plus
  the tiny context, the age band, the child's nickname (`ai.share_child_name`, **on by default**
  so the AI and the live voice can call the child by name; switch it off on the AI page) and,
  only if a parent opts in (`ai.share_interests`), short, sanitised favourite animals/interests.
* **Things LittleBot remembers** (`profile.remembered`, `littlebot/brain/remembered.py`): when a
  grown-up says "remember that ..." in a voice chat, the voice hands over a short note ("Ali is
  Amara's dad") that is kept on the robot (at most 20, 100 characters each) and put into the
  instructions of every later voice chat. The Profile page lists them; remove any there, or say
  "forget that ...". With `ai.share_child_name` off, the child's nickname in a note becomes "the
  little one".
* No camera frames, images or audio are ever sent by the conversation engine. Live voice
  (§3) is separate and does stream the microphone audio to OpenAI during a parent-started
  call.
* **Letting the AI see (camera)** is a separate privacy switch, **off by default**
  (`privacy.ai_camera_enabled`, "Let the AI look through the camera when asked"). Only when it,
  the camera and cloud communication are all on (and not in offline mode), and only when
  someone asks — the parent's **Look and answer** button on the AI page, or a question about
  what LittleBot sees during a live voice call — LittleBot takes **one still frame** and sends it
  with the question to ChatGPT. The frame exists only in memory for that request: it is never
  saved, logged or sent to the dashboard (the browser gets only the answer text and a count of
  photos sent today). OpenAI does receive the photo, including anyone in view. See
  [CHATGPT.md §4](CHATGPT.md#4-letting-the-ai-see-camera).
* Cloud providers process requests under their own terms and may retain data for a
  period for abuse monitoring — check the provider's data-retention policy before
  enabling cloud conversation.

---

## 5. Spending controls

| Setting (dashboard / `ai.*`) | Default | Effect |
|---|---|---|
| `max_requests_per_hour` | 60 | rolling 60-minute window of cloud requests |
| `max_requests_per_day` | 200 | per calendar day |
| `daily_budget_usd` | 0.50 | estimated spend per day; a request that would exceed it is not sent |
| `max_output_tokens` | 120 | reply budget (+ `thinking_headroom_tokens`, 400, for Claude's adaptive thinking) |
| `timeout_seconds` | 12 | per request; on timeout LittleBot says a friendly line |

When a limit is reached LittleBot quietly switches to scripted replies for the rest of
the hour/day and the dashboard shows why. Failed requests count toward the request limits.

**Costs are estimates.** `littlebot/brain/usage.py` contains a small price table
(USD per million tokens, e.g. Claude Haiku 5.5: $0.10 input / $0.50 output for prompts
up to 100K tokens). Prices change: check the provider's pricing page and, if needed,
override with `ai.price_per_mtok` in `config/local.yaml`. A typical turn is roughly
600–900 input tokens and 20–100 output tokens (more when the model thinks, never more than
`max_output_tokens` + `thinking_headroom_tokens`) — well under a tenth of a US cent on Haiku 5.5 —
so the default $0.50/day budget is generous. **Also set a hard monthly spending limit in
the provider's console**; the robot's budget is a safety brake, not an invoice.

**ChatGPT sign-in** has no per-token cost: its requests are counted at $0, so the daily budget
never blocks them, but the hourly and daily request limits still do, and the ChatGPT plan's own
usage limits apply (LittleBot then falls back to scripted replies).

---

## 6. Turning on cloud AI (parents)

1. **Get an API key from a separate, paid API account.** For Claude, sign up at
   <https://console.anthropic.com>, add billing, set a monthly spend limit, and create
   an API key. *A Claude.ai or Claude Code subscription does **not** include API access —
   the API is billed separately.* (OpenAI: <https://platform.openai.com>.)
2. Copy `.env.example` to `.env` in the LittleBot folder and paste the key:
   `ANTHROPIC_API_KEY=sk-ant-...` (`.env` is git-ignored; keys are read only by the
   Python backend and never sent to the dashboard's browser code). Restart LittleBot.
3. In the dashboard: set the child's **birth month / age** in the profile, switch on
   **Cloud** in **Privacy**, then on the **AI** page choose the provider, switch
   **Cloud conversation** on, and check the limits and daily budget.
4. Press **Test** next to the provider. A rejected key shows `auth_error`; LittleBot
   then stays on scripted replies until the AI settings are changed.

**No API key: sign in with ChatGPT instead.** On the AI page, **ChatGPT account** → **Sign in
with ChatGPT**, then choose the provider **ChatGPT (sign in with your plan)** and switch cloud
conversation on. It uses the parent's own ChatGPT plan through undocumented endpoints that may
stop working; step-by-step in [CHATGPT.md](CHATGPT.md).

There is no age gate by default: once you switch cloud conversation on it answers at any age,
since a grown-up is usually the one asking and you are there. To keep a young child on the
scripted replies, set `ai.min_age_months_for_cloud` (e.g. `24`) in `config/local.yaml`; cloud
speech (TTS / STT) follows the same setting. Offline mode never contacts any server.

---

## 7. Verification status

* Verified by automated tests (no network, `httpx.MockTransport`): request shapes and
  headers, response parsing, every error path (401/403/429/5xx/529, malformed JSON,
  timeouts, network down), provider gating, budgets, state transitions, STOP ALL, privacy
  guarantees, and the safety validator.
* **Not yet verified against the live APIs** (no keys in the development environment).
  The Anthropic request follows the current Messages API reference
  (`anthropic-version: 2023-06-01`, model `claude-haiku-5-5`); the OpenAI request follows
  the documented Chat Completions format with model `gpt-4o-mini`. Use the dashboard's
  **Test** button after adding a key.
* **ChatGPT sign-in and the `chatgpt` text provider:** tested only against mocked OpenAI
  responses (device-code sign-in, refresh, sign-out, request contract, error mapping, and
  tokens never reaching the browser). The endpoints are undocumented; the details were copied
  from WatchGPT / the Codex CLI and **have not been tried against OpenAI's real service**.
* **Live voice:** tested end to end against a **local WebRTC peer standing in for OpenAI**
  (a real aiortc call on one machine: tone audio, data-channel delegations, the safety cut,
  the worrying-words alert, STOP ALL, Privacy off, time limit, idle hang-up, friendly errors,
  start gates). The microphone/speaker bridge, including the half-duplex gate and the mute on a
  safety cut, is tested separately against a fake PortAudio. **Not yet tested against OpenAI's
  real service or on the Pi's real microphone and speaker.**
* **Letting the AI see (camera):** the dashboard route and card are tested with a stand-in for
  the camera look (only the answer text reaches the browser, blocked requests get a plain
  reason, the switch is off by default). **Not yet tested against OpenAI's real service or with
  the Pi's real camera.**
