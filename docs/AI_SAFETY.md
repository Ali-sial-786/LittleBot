# AI conversation safety

LittleBot is built for a child who is ~10 months old today and will use it until about
age 5. Open-ended AI is powerful and unpredictable, so the conversation engine
(`littlebot/brain/`) is designed around one rule: **the important guarantees are
enforced in code, not left to the model's good behaviour.** The system prompt is a
helpful extra layer, never the only one.

This document covers what is enforced where, the limits, what data goes where, costs,
and how a parent turns cloud AI on.

---

## 1. Who gets which kind of conversation

| Child / situation | What answers | Why |
|---|---|---|
| Age **unknown** (profile not filled in) | Scripted offline replies | Unknown is treated as the youngest age |
| Under **24 months** (`ai.min_age_months_for_cloud`) | Scripted offline replies (1–6 words: sounds, repetition) | Infants never get open-ended AI |
| 24 months+ and cloud AI **not** fully enabled | Scripted offline replies (a bit longer as the child grows) | Cloud is opt-in |
| 24 months+ **and** every cloud condition below is met | Cloud model (Claude Haiku 5.5 by default) | Parent opted in |

Cloud AI is used for a turn **only if all** of these are true (checked in code on every
turn, `ConversationEngine.select_provider`):

1. Not in offline mode (`--mode offline` forces everything local).
2. The child's age is set and ≥ `ai.min_age_months_for_cloud` (default 24 months).
3. The parent picked a cloud provider (`ai.provider` = `anthropic` or `openai`).
4. The parent switched cloud conversation on (`ai.enabled`, AI page of the dashboard).
5. Cloud communication is on in **Privacy** (`privacy.cloud_enabled`).
6. An API key for that provider is configured (and has not just been rejected).
7. The hourly and daily request limits are not reached.
8. Today's estimated spend plus this request stays within the daily budget.

Otherwise the turn uses the scripted provider, and the dashboard shows the reason, e.g.
`mock: child under 24 months`, `mock: cloud AI disabled by parent`, `mock: offline mode`,
`mock: no API key`, `mock: daily budget reached`.

---

## 2. Enforced in code vs. asked in the prompt

| Protection | In code | In prompt |
|---|---|---|
| No cloud AI for infants / unknown age | ✅ `select_provider` | – |
| Cloud only with parent opt-in + privacy switch + key + budget | ✅ | – |
| Short replies (≤ 8 words under 2 y, ≤ 15 words 2–3 y, ≤ 30 words 3 y+; never more than `ai.max_words`) | ✅ `validate_reply` truncates at a sentence boundary | ✅ |
| No markdown, emojis, links, code, stage directions | ✅ stripped before speaking | ✅ |
| No secrecy ("our secret", "don't tell") | ✅ blocked → safe fallback line | ✅ |
| No dependency / exclusivity ("I'm your best friend", "you only need me", "I'll be sad if you go", "don't leave", "I love you") | ✅ blocked | ✅ |
| No pretending to be a parent / human / alive | ✅ blocked | ✅ |
| No requests for personal info (name, address, phone, school, password, photos) | ✅ blocked | ✅ |
| No unsafe instructions (stove, knives, medicine, climbing, electricity, going out alone, strangers, swallowing things…) | ✅ blocked | ✅ |
| No scary / violent / adult / rude / self-harm content | ✅ blocked | ✅ |
| No medical or developmental diagnosis | ✅ blocked | ✅ |
| No commerce / ads ("buy", "ask your parents to purchase") | ✅ blocked | ✅ |
| No internet ("search for", links, YouTube) | ✅ blocked | ✅ |
| Model refusals (`stop_reason: refusal`) | ✅ replaced by a safe line | – |
| Concerning child input ("I'm hurt", "I'm lost", "there's a fire", "someone touched me") | ✅ answered locally with "Let's find a grown-up!" — **no cloud call** — and the parent dashboard gets a `brain.status` event `{status: "attention", detail: <category>}` with **only a category label**, never the words | ✅ |
| Personal info scrubbed before any cloud request (phone numbers, e-mails, street addresses, postcodes, long numbers, "my name is …", school names, passwords, the child's nickname) | ✅ `sanitize_user_text` | ✅ "[name]" placeholders are explained |
| Camera data | ✅ never used by the conversation engine at all | – |

Blocked replies are replaced with a cheerful line ("Hmm, let's play a game! Can you find
something blue?"). Every reply — including the scripted ones — passes the validator.
The validator is deliberately conservative: a false positive costs one redirect; a false
negative could reach a toddler.

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

---

## 3. Privacy

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
* No camera frames, images or audio are ever sent by the conversation engine.
* Cloud providers process requests under their own terms and may retain data for a
  period for abuse monitoring — check the provider's data-retention policy before
  enabling cloud conversation.

---

## 4. Spending controls

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

---

## 5. Turning on cloud AI (parents)

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

Under 24 months (or with no age set) LittleBot keeps using its scripted replies whatever
these settings say. Offline mode never contacts any server.

---

## 6. Verification status

* Verified by automated tests (no network, `httpx.MockTransport`): request shapes and
  headers, response parsing, every error path (401/403/429/5xx/529, malformed JSON,
  timeouts, network down), provider gating, budgets, state transitions, STOP ALL, privacy
  guarantees, and the safety validator.
* **Not yet verified against the live APIs** (no keys in the development environment).
  The Anthropic request follows the current Messages API reference
  (`anthropic-version: 2023-06-01`, model `claude-haiku-5-5`); the OpenAI request follows
  the documented Chat Completions format with model `gpt-4o-mini`. Use the dashboard's
  **Test** button after adding a key.
