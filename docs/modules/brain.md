# Module: brain (conversation engine)

`littlebot/brain/` turns something the child said into one short, safe, spoken reply.
Safety rationale and parent instructions: [../AI_SAFETY.md](../AI_SAFETY.md).

```
littlebot/brain/
  __init__.py          create_service(ctx) -> ConversationEngine
  conversation.py      ConversationEngine (BrainAPI + Service): gating, turn flow, events
  prompts.py           age-aware system prompt (cloud only), profile sanitising
  usage.py             request limits + estimated daily budget (persisted per day, numbers only)
  providers/
    base.py            ProviderReply, BaseProvider, typed errors
    http.py            shared httpx plumbing: client injection, status -> error mapping, key redaction
    mock.py            MockProvider: offline, deterministic, rule-based, age-aware; neutral "I'm listening"
                       replies for unknown input, grown-up line for concerning input
    anthropic.py       AnthropicProvider: POST /v1/messages (raw REST)
    openai.py          OpenAIProvider: POST /v1/chat/completions (raw REST)
littlebot/chatgpt/responses.py  ChatGPTProvider ("chatgpt"): the parent's ChatGPT plan via the
                              undocumented Codex Responses endpoint (sign-in: littlebot/chatgpt/auth.py,
                              ../CHATGPT.md); requests counted at $0 in the usage tracker
littlebot/safety/content.py   validate_reply, sanitize_user_text, classify_child_input (word lists),
                              directs_to_grownup, grownup_fallback
```

No vendor SDKs: providers use `httpx` directly (small install on the Pi, deterministic
tests with `httpx.MockTransport`).

## Public API (`core.interfaces.BrainAPI`)

| Method | Returns |
|---|---|
| `await handle_utterance(text, *, source="simulated", speak=True)` | `{reply, provider, filtered, error, reason, expression, latency_ms, spoken, checks[, attention][, detail]}` |
| `await converse_once()` | push-to-talk: listen → think → speak. Same keys + `heard` |
| `providers()` | `[{name, configured, available, model, cloud, status}]` (`status`: ok / not_configured / auth_error) |
| `await test_provider(name)` | `{ok, latency_ms, error, provider, model[, reply]}` |
| `status()` | `{provider, active_provider, cloud_allowed, reason, usage: {requests_hour, requests_today, failed_today, cost_today_usd, budget_usd, max_requests_per_hour, max_requests_per_day, ...}, last_error, busy, phase, models, provider_status, last_provider, context_turns, store_transcripts, service}` |
| `clear_history()` | forgets the in-memory context (+ opted-in transcripts); used by the dashboard "Clear history" |

`error` values: `busy` (lease refused / already talking / state does not allow it), `stopped`
(STOP ALL or preempted), `empty`, `no_audio`, `microphone_disabled`, `nothing_heard`, and
provider failures `auth_error`, `rate_limited`, `quota_exceeded`, `timeout`, `network`,
`server_error`, `overloaded`, `malformed`, `bad_request`, `not_found`, `billing`,
`internal_error`. Provider failures still produce a friendly spoken `reply`
("Hmm, my thinking cap is sleepy. Let's play instead!") with a `confused` face.

`provider` is `mock`, `anthropic`, `openai`, `chatgpt` or `safety` (concerning-input reply). `attention` is present
when the parent was alerted: a concern category (`safety`, `water`, `swallowed`, `emergency`, `stranger`,
`hurt`, `lost`, `scared`, `help`) for concerning input, or `danger` when a cloud reply talked about a hazard
while urging a grown-up (see AI_SAFETY.md §2a/§2b).

## Turn flow

1. **Lease** `face` (+ `audio_out`, `microphone` when speaking) at `Priority.CONVERSATION`.
   Refused while an activity (50) or media (60) holds them → `{"error": "busy"}`; lower
   priority reactions are preempted.
2. **State** `LISTENING` (push-to-talk only; face `listening`) → `THINKING` (face `thinking`).
   A sleeping robot is woken (`SLEEPING → IDLE`) by an explicit turn.
3. **Concerning input?** (`classify_child_input`, run on the whole input *before* the `max_input_chars`
   cap) → fixed, gentle, never cheerful "let's get a grown-up right now" reply for the category,
   `affectionate` face, context cleared,
   `brain.status {provider: "safety", status: "attention", detail: <category>}`, no cloud.
4. **Provider choice** (`select_provider`, see AI_SAFETY.md §1) — reason reported in `status()`.
5. **Cloud call**: scrub PII (`sanitize_user_text`), age-aware system prompt, tiny context,
   `asyncio.wait_for(timeout_seconds + 0.5)`; usage counted before sending, tokens/cost after.
6. **Validate** every reply (`validate_reply`): strip, cap words (age-aware), block → fallback.
   A reply that urges a grown-up about a hazard (`ValidationResult.concern == "danger"`) is either spoken
   (pure safety advice: every hazard mention is a warning) or replaced by the gentle
   `GROWNUP_FALLBACK` ("Let's find a grown-up right now.") — never a cheerful game — and the engine
   clears the context and publishes `brain.status {provider: <cloud>, status: "attention", detail: "danger"}`.
7. Publish `brain.reply {text, provider, filtered}`.
8. **Speak**: `SPEAKING`, expression from the reply (mock hint or heuristic), `audio.say()`
   (bounded), then release back to `IDLE` — only if the robot is still in the state the
   engine entered (never clobbers an activity that took over).

`ctx.audio` / `ctx.face` may be `None`: the turn still runs and returns the reply.

## Events

| Topic | When | Payload |
|---|---|---|
| `brain.reply` | every answered turn | `{text, provider, filtered}` |
| `brain.status` | start (`ready`), provider failure (`error` / `auth_error`), concerning input or a reply that urges a grown-up about a hazard (`attention`), successful key test (`ok`) | `{provider, status, detail}` — `detail` is a reason / error kind / category (`safety`, `water`, `swallowed`, `emergency`, `stranger`, `hurt`, `lost`, `scared`, `help`, `danger`), never transcript text |

Subscribed: `system.stop_all` (cancel the in-flight request, stop speech, clear context),
`state.changed` (clear context on `SLEEPING`), `settings.changed` (any `ai.*` change clears
an `auth_error`; profile / cloud-off clear context), `audio.utterance` (auto-reply while
`IDLE` and not busy; disable with `ai.reply_to_utterance_events: false`; repeats of the
turn just handled are ignored).

## Configuration (`ai:` in config/default.yaml)

Existing: `provider`, `anthropic_model` (`claude-haiku-5-5`), `openai_model` (`gpt-4o-mini`),
`timeout_seconds`, `max_output_tokens`, `max_words`, `max_requests_per_hour`,
`max_requests_per_day`, `daily_budget_usd`, `min_age_months_for_cloud`, `store_transcripts`.

Added by this module:

| Key | Default | Meaning |
|---|---|---|
| `anthropic_effort` | `low` | `output_config.effort` (thinking depth / cost / latency) |
| `anthropic_thinking` | `adaptive` | `adaptive` = omit the param (model default); `disabled` sends `thinking: {type: disabled}` — accepted by Haiku 5.5 at low–high effort, **400 on Opus 5.5 / Sonnet 5.5** |
| `thinking_headroom_tokens` | 400 | added to `max_tokens` for Anthropic (thinking counts toward `max_tokens`) |
| `anthropic_base_url` / `openai_base_url` | official endpoints | for proxies / tests |
| `price_per_mtok` | `{}` | `{model: [input, output]}` USD per 1M tokens, overrides the estimate table |
| `context_turns` / `context_ttl_seconds` | 3 / 120 | in-memory context size and lifetime |
| `max_input_chars` | 300 | the child's words are capped before processing |
| `listen_timeout_seconds` | 8 | push-to-talk listen timeout |
| `say_didnt_hear` | true | say "I didn't hear that…" when nothing was heard |
| `reply_to_utterance_events` | true | answer `audio.utterance` events while idle |
| `share_child_name` / `share_interests` | false | parent opt-in to include the nickname / favourite animals + interests in the cloud prompt (also read from settings keys `ai.share_child_name` / `ai.share_interests` if those get registered) |

Parent-editable settings used: `ai.enabled`, `ai.provider`, `ai.max_words`,
`ai.max_requests_per_hour`, `ai.max_requests_per_day`, `ai.daily_budget_usd`, `profile`,
`privacy.cloud_enabled`, `privacy.microphone_enabled`, `privacy.record_history`.

## Anthropic request (verified against the claude-api reference, Oct 2026; not yet live-tested)

```
POST https://api.anthropic.com/v1/messages
x-api-key: <key>            anthropic-version: 2023-06-01      content-type: application/json
{"model": "claude-haiku-5-5", "max_tokens": 520, "system": "...",
 "messages": [{"role": "user", "content": "..."}], "output_config": {"effort": "low"}}
```

Only `text` content blocks are spoken (`thinking` blocks are ignored); usage =
`input_tokens` (+ cache tokens) / `output_tokens`; `stop_reason: "refusal"` → safe fallback.
No `temperature`/`top_p`/`top_k` (non-default values return 400 on current models), no
assistant prefill (the message list always ends with the user turn).

## Extending

* **New scripted topic**: add a `_r(...)` rule or vocabulary entry in `providers/mock.py`;
  `tests/brain/test_brain_mock.py` checks every reply passes the validator at every age. Keep
  `_UNKNOWN` replies neutral (no "wow" / "fun idea"): they also answer things the rules missed.
* **New block rule / concern rule**: `safety/content.py` (`BLOCK_RULES` / `CONCERN_RULES`),
  plus a case in `tests/brain/test_brain_content_safety.py` (blocked *and* a must-still-pass control).
  A new concern category also needs a gentle line in `_CONCERN_REPLIES` (reply + ≤ 6-word short reply,
  containing "grown-up", no games or "yay"); the tests enforce this for every category.
* **New provider**: subclass `HttpProvider`, implement `reply()`, register it in
  `ConversationEngine._build_providers`, add its prices to `usage.PRICES_PER_MTOK`, and add
  it to `CLOUD_PROVIDERS` / the `ai.provider` literal in `core/config.py`.

## Tests

`.venv/bin/python -m pytest -q tests/brain` — no network, no keys, ~4 s. Helpers in
`tests/brain/brain_helpers.py` (`FakeAPI` MockTransport handler, `cloud_context()`).
