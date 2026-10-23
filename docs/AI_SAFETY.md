# AI conversation safety

LittleBot is built for a child who is ~10 months old today and will use it until about
age 5. Open-ended AI is powerful and unpredictable, so the conversation engine
(`littlebot/brain/`) is designed around one rule: **the important guarantees are
enforced in code, not left to the model's good behaviour.** The system prompt is a
helpful extra layer, never the only one.

This document covers what is enforced where, the limits, what data goes where, costs,
how a parent turns cloud AI on, and the parent-started live voice (§3). Parent guide for
"Sign in with ChatGPT" and live voice: [CHATGPT.md](CHATGPT.md).

---

## 1. Who gets which kind of conversation

| Child / situation | What answers | Why |
|---|---|---|
| Age **unknown** (profile not filled in) | Scripted offline replies | Unknown is treated as the youngest age |
| Under **24 months** (`ai.min_age_months_for_cloud`) | Scripted offline replies (1–6 words: sounds, repetition) | Infants never get open-ended AI |
| 24 months+ and cloud AI **not** fully enabled | Scripted offline replies (a bit longer as the child grows) | Cloud is opt-in |
| 24 months+ **and** every cloud condition below is met | Cloud model (Claude Haiku 5.5 by default; or OpenAI, or ChatGPT on the parent's plan) | Parent opted in |
| **Any age**, but only a **live voice** call that a parent starts from the dashboard, ticking "I am with my child and will stay for the whole conversation" | ChatGPT's GPT-Live voice, in real time (§3) | The parent chose it and is present for the whole call |

Cloud AI is used for a turn **only if all** of these are true (checked in code on every
turn, `ConversationEngine.select_provider`):

1. Not in offline mode (`--mode offline` forces everything local).
2. The child's age is set and ≥ `ai.min_age_months_for_cloud` (default 24 months).
3. The parent picked a cloud provider (`ai.provider` = `anthropic`, `openai` or `chatgpt`).
4. The parent switched cloud conversation on (`ai.enabled`, AI page of the dashboard).
5. Cloud communication is on in **Privacy** (`privacy.cloud_enabled`).
6. An API key for that provider is configured, or for `chatgpt` a ChatGPT sign-in exists
   (and it has not just been rejected).
7. The hourly and daily request limits are not reached.
8. Today's estimated spend plus this request stays within the daily budget (`chatgpt`
   requests count as $0: the plan's own limits apply instead).

Otherwise the turn uses the scripted provider, and the dashboard shows the reason, e.g.
`mock: child under 24 months`, `mock: cloud AI disabled by parent`, `mock: offline mode`,
`mock: no API key`, `mock: not signed in with ChatGPT`, `mock: daily budget reached`.

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
| No cloud AI for infants / unknown age | ✅ `select_provider` | – |
| Cloud only with parent opt-in + privacy switch + key + budget | ✅ | – |
| Short replies (≤ 8 words under 2 y, ≤ 15 words 2–3 y, ≤ 30 words 3 y+; never more than `ai.max_words`) | ✅ `validate_reply` truncates at a sentence boundary | ✅ |
| No markdown, emojis, links, code, stage directions | ✅ stripped before speaking | ✅ |
| No secrecy ("our secret", "don't tell", "let's not tell anyone", "mommy doesn't need to know", "only you and I will know", "just for us") | ✅ blocked → safe fallback line | ✅ |
| No dependency / exclusivity / guilt ("I'm your best friend", "you only need me", "I'll be sad if you go", "please don't go", "I'll miss you", "without you", "love you", "lonely", "stay here with me") | ✅ blocked | ✅ |
| No pretending to be a parent / human / alive ("I'm just like you, a little kid") | ✅ blocked | ✅ |
| No requests for personal info (name, address, "where you live", "what's your mommy called", "the name of your street", phone, school, teacher, password, photos) | ✅ blocked | ✅ |
| No unsafe instructions (stove, knives, medicine, climbing or jumping on beds / sofas / tables, anything "in your mouth / nose / ear", cords or string round the neck, hiding in toy boxes / closets / appliances, baths / pools / ponds / taps, electricity, going out alone, strangers, swallowing things…) | ✅ blocked (see the safety-advice exception below) | ✅ |
| No scary / violent / adult / rude / self-harm content ("the wolf will gobble you up", "a monster under your bed") | ✅ blocked | ✅ |
| No medical or developmental diagnosis ("you sound like you have a cold", "see a doctor") | ✅ blocked | ✅ |
| No commerce / ads ("buy", "ask your parents to purchase", "the new … toy") | ✅ blocked | ✅ |
| No internet / streaming ("search for", links, "dot org", YouTube, Netflix) | ✅ blocked | ✅ |
| Model refusals (`stop_reason: refusal`) | ✅ replaced by a safe line | – |
| Concerning child input (emergencies, swallowed objects, water danger, stranger lures, abuse disclosures, "help me" — see §2a) | ✅ answered locally with a **gentle, never cheerful** "let's get a grown-up right now" line — **no cloud call** — and the parent dashboard gets a `brain.status` event `{status: "attention", detail: <category>}` with **only a category label**, never the words | ✅ |
| Good safety advice from the model ("Swallowing a battery is not safe. Tell a grown-up right now!") | ✅ spoken, not swapped for a game; parent alerted (`detail: "danger"`) — see §2b | ✅ |
| Personal info scrubbed before any cloud request (phone numbers, e-mails, street addresses incl. "my address is …" and spelled-out numbers, postcodes, long numbers, "my name is …", "I'm Lily", "my mommy's name is …", "we live in Springfield", school names, passwords, the child's nickname) | ✅ `sanitize_user_text` | ✅ "[name]" placeholders are explained |
| Camera data | ✅ never used by the conversation engine at all | – |

Blocked replies are replaced with a cheerful line ("Hmm, let's play a game! Can you find
something blue?") — **except** when the reply was about a possible danger (§2b), where the
replacement is the gentle "Let's find a grown-up right now." Every reply — including the
scripted ones — passes the validator. The validator is deliberately conservative: a false
positive costs one redirect; a false negative could reach a toddler.

The scripted (offline) provider answers words it does not understand neutrally and points
at a grown-up ("I'm listening! Shall we ask a grown-up?"), never with enthusiasm ("What a
fun idea!"), because it cannot tell happy chatter from something worrying it failed to
recognise.

### 2a. Concerning input: categories and replies

`classify_child_input` (`CONCERN_RULES` in `littlebot/safety/content.py`) checks everything
the child said — before the input length cap, so a worrying sentence at the end is not cut
off — and the first matching category wins. The reply is fixed, short and **never cheerful**
(no games, "yay" or "fun"); the face shows the gentle `affectionate` expression; the
in-memory context is forgotten; the parent alert carries the category label only.

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

Very young children get the short form of each line (≤ 6 words, e.g. "Tell a grown-up right
now."). The scripted provider runs the same check itself, so even a direct call can never
answer "I ate a battery" with a cheerful line.

### 2b. Safety advice from the model

A good cloud reply to a danger necessarily *mentions* the danger, which the `unsafe` rules
would normally block. `validate_reply` therefore lets a reply through when **all** of these
hold: the only block category is `unsafe`; a sentence urgently sends the child to a grown-up
("tell / get / find / call a grown-up / mommy / daddy … right now / right away"); and every
clause that mentions a hazard is a warning ("is not safe", "don't touch", "can hurt you",
"not food") with no invitation in it. Reassurances ("it isn't dangerous", "it won't hurt"),
invitations ("let's", "can you", "touch it") or a hazard in any other clause keep it blocked.
If the reply is too long, the grown-up sentence is kept.

Whenever a blocked or exempted reply both mentions a hazard / scary / medical / secrecy topic
**and** urges a grown-up, the model has probably noticed something the word lists missed:
LittleBot then says the model's warning (or, if it could not be spoken safely, the gentle
"Let's find a grown-up right now." — never a game), forgets the context, and alerts the parent
with `{status: "attention", detail: "danger"}`.

Evasion tricks are normalised before matching: upper/lower case, punctuation and
letter-spacing (`S.E.C.R.E.T`, `s e c r e t`), zero-width and bidi characters, soft
hyphens, full-width and "math bold" letters, Cyrillic/Greek look-alikes (`ѕecret`),
accents and leetspeak (`s3cr3t`). The word lists live in one place —
`littlebot/safety/content.py` (`BLOCK_RULES`, `CONCERN_RULES`, PII regexes) — with
instructions for extending them; `tests/brain/test_brain_content_safety.py` holds the
adversarial test-suite.

**Known limits.** Pattern rules cannot understand meaning: a cleverly phrased harmful
reply in words not on the lists could pass, and some harmless replies are blocked. The
prompt, the model provider's own safety systems, short replies and the age gate reduce
this risk; they do not remove it. **LittleBot is a supervised toy: a grown-up should be
nearby whenever cloud conversation is on.** Speech recognition can also mishear, so
concern detection may miss real problems or flag harmless ones.

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
| What the voice says | Every caption of the voice's speech goes through the same `validate_reply` rules as typed replies (§2), **while it speaks**. A block rule hit **silences the speaker immediately, ends the call** and raises the dashboard alert **"Live voice stopped"** (`brain.status {provider: "live", status: "live_safety_stop", detail: <category>}`, category only). Captions trail the audio, so **some words may already have been heard** |
| What it hears | Final captions of what was heard go through `classify_child_input` (§2a). A worrying category raises the usual **"Your child may need you"** alert (category only). The call continues, and the voice is instructed to send everyone to a grown-up. A voice reply that urges a grown-up about a hazard alerts the parent too |
| Look-ups ("delegations") | Questions the voice hands to LittleBot are answered by the ChatGPT text model with LittleBot's child-safe system prompt (≤ 40 words; question scrubbed with `sanitize_user_text`) and checked with `validate_reply` first. An unsafe or failed answer becomes "say you are not sure, and suggest asking a grown-up" |
| Echo | Half-duplex (`chatgpt.live_half_duplex`): the Pi has no echo cancellation, so the microphone sends silence while LittleBot's voice plays and for 450 ms after. Nobody can talk over it |
| Volume | The speaker volume setting (or `chatgpt.live_volume`), always capped at `audio.speaker.max_volume` |
| Instructions (prompt) | Short, gentle sentences; a robot, not a person or family member; no "best friend", guilt or secrets; no personal questions; nothing scary or grown-up; no medical, feeding, sleep or development advice; no shopping, apps or websites; nothing risky; "Let's get a grown-up right now" when someone might be hurt |

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
  the tiny context, the age band, and — **only if a parent opts in** (`ai.share_child_name`,
  `ai.share_interests`) — the nickname and short, sanitised favourite animals/interests.
* No camera frames, images or audio are ever sent by the conversation engine. Live voice
  (§3) is separate and does stream the microphone audio to OpenAI during a parent-started
  call.
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

Under 24 months (or with no age set) LittleBot keeps using its scripted replies whatever
these settings say. Offline mode never contacts any server.

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
