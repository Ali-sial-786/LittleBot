# Claude as LittleBot's text model

During a voice chat the live voice (GPT-Live) talks. Behind it, a **text model** does the written work:

* the **screen director**: which card to show (or none), and the card's words;
* **hand-offs**: questions the voice passes to LittleBot, with LittleBot's tools (volume, videos, games,
  weather, web search...);
* the browser's search words;
* optionally the **decider**: the small multiple-choice checks in [DECISIONS.md](DECISIONS.md).

That text model is **Claude Haiku 5.5** (Anthropic's API, your own API key), with thinking off. If it fails
or is slow to say its first word, the same request goes to **Claude Haiku 4.5**, then to **ChatGPT** (your
sign-in). Without a key ChatGPT answers, as before. Haiku 5.5 was the best and cheapest text model in the
2026-10-29 test of 34 everyday questions (no clear mistakes, 1.16 s median, under a cent for all 34).

What doesn't change:

* the **live voice** is always GPT-Live;
* **camera looks** ("what am I holding?") are always ChatGPT;
* the typed / push-to-talk chat has its own setting (`ai.provider`, which can already be `anthropic`).

> **Status.** Tested against Anthropic's real API from a laptop (with a real GPT-Live call, 2026-10-29),
> **not yet on the robot**. Run `scripts/compare_text_models.py` (below) once the key is set to see it work
> on the Pi.

## 1. Install the Anthropic SDK

```bash
cd ~/LittleBot && .venv/bin/pip install -e '.[claude]'
```

Without it LittleBot keeps using ChatGPT and logs once: `Claude isn't set up (not_installed)`.

## 2. Put your API key on the robot

The key is read **on the robot only**. It never goes to the dashboard's pages, the browser or the logs. The
dashboard only shows whether a key is set. Use **one** of these:

* **`.env`** in `~/LittleBot` (the file LittleBot already reads for keys):

  ```bash
  ANTHROPIC_API_KEY=sk-ant-...
  ```

* **or a key file** `~/LittleBot/data/anthropic_api_key` holding just the key, readable only by you:

  ```bash
  nano ~/LittleBot/data/anthropic_api_key      # paste the key, save
  chmod 600 ~/LittleBot/data/anthropic_api_key
  ```

  A key file others can read is **ignored**. The log then says `anthropic_api_key is readable by other
  users (mode 644): not used; run chmod 600 on it` (never the key itself).

`ANTHROPIC_API_KEY` from `.env` or the environment wins over the file. In offline mode no key is loaded.

A key stored as a **GitHub secret** (for example in the repository's Actions secrets) is only available
to GitHub Actions workflows. The robot can't read it, so the key still has to be put on the robot as above.

## 3. Switch the text model

`config/default.yaml` already has:

```yaml
text:
  provider: claude          # chatgpt | claude
claude:
  model: claude-haiku-5-5   # the Claude model; nothing is sent to Claude while this is unset
  fallback_models: [claude-haiku-4-5]   # tried in turn before ChatGPT
  thinking: disabled
```

Change any of it in `config/local.yaml`. For ChatGPT alone: `text: {provider: chatgpt}`.

Optionally, the decider too (it may use a different provider from the cards):

```yaml
decide:
  provider: claude          # off | jev | openai | claude
  # claude_model: claude-haiku-5-5   # unset = claude.model
```

The dashboard's decision panel can also pick Claude for the decider. That choice wins over the config.

Restart: `systemctl --user restart littlebot`. To go back, set `text.provider: chatgpt` (or remove the
lines) and restart.

Other `claude:` settings (`littlebot/core/config.py`, `ClaudeConfig`):

| Key | Default | Meaning |
|---|---|---|
| `model` | `claude-haiku-5-5` | the model name. Unset (`null`): Claude isn't used |
| `fallback_models` | `[claude-haiku-4-5]` | asked in turn, with the same request, when `model` fails or is slow; ChatGPT comes after them |
| `effort` | `low` | `low` / `medium` / `high`. Requests LittleBot marks as simple (light hand-offs, cards) go at `low` with thinking off |
| `thinking` | `disabled` | `adaptive`: the model thinks when it helps. `disabled`: never (fastest). Haiku 4.5 never thinks either way |
| `max_tokens` | `2048` | the most it may write per request, thinking included. Answers are still cut short by LittleBot afterwards |
| `first_token_timeout_seconds` | `4.0` | no first word by then: the next model is asked instead |
| `web_search` | `true` | Anthropic's web search for hand-offs about current facts (billed per search). Refused by the API: not offered again until restart |
| `web_search_max_uses` | `2` | searches per hand-off |
| `key_file` | `anthropic_api_key` | the key file under `data/` |

`text.fallback: false` turns both fallbacks (Haiku 4.5 and ChatGPT) off. Then a Claude failure is the answer's failure
("I'm not sure"), which is only useful for measuring Claude on its own.

## 4. The same rules

Whichever model answers, LittleBot checks the words the same way **afterwards**:

* child safety and the **medicine dose rule** (a dose becomes "check the label or ask a pharmacist");
* **danger alerts** on what was heard;
* **`ai.share_child_name`**: the prompts are built before the model is chosen, so the child's name is sent
  to Claude only when it would be sent to ChatGPT.

**Offline mode** and the Privacy page's **Cloud communication** switch stop Claude exactly as they stop
ChatGPT. Then nothing is sent to either, and there is no fallback either.

A tool already run, or words already handed to the voice, are never repeated: if Claude fails after
that, there is no second try with ChatGPT for that request.

## 5. Speed and cost

What LittleBot does to keep Claude quick:

* **Streaming**: the first sentence goes to the voice as soon as it is written.
* **Prompt caching**: the fixed part (instructions and tool list) is cached, so repeat requests within a few
  minutes send it again at a tenth of the price and start sooner.
* **Short limits**: 4 s to the first word, then ChatGPT; no SDK retries.
* **Less thinking**: no thinking for simple requests.

What a request costs: Claude Haiku 5.5 is $0.10 per million input tokens and $0.50 per million output
tokens (for prompts up to 100K tokens; cached input costs a tenth). LittleBot's requests are a few thousand
tokens in and under 200 out:

| Request | About | Cost each (uncached / cached) |
|---|---|---|
| card decision | 2,700 tokens in | ≈ $0.0003 / $0.00005 |
| hand-off (tools offered) | 3,700 tokens in | ≈ $0.0004 / $0.0001 |
| decider question | a few hundred tokens | < $0.0001 |
| web search | | $10 per 1,000 searches (1 cent each), on top |

So a busy day of 100 hand-offs and 100 cards is a few cents. Web searches cost the most, so
`web_search: false` is the setting to change if cost matters. Check the current prices on Anthropic's
pricing page. ChatGPT, by comparison, has no per-call bill but counts against your plan's limits.

Speed: **not measured yet.** Measure it on the robot (next section) before deciding.

## 6. Measuring

Every text-model call logs one line (labels and numbers only, never the words or a key):

```
text model: provider=claude model=claude-haiku-5-5 purpose=handoff result=ok first_token_ms=612 total_ms=1480 tool_rounds=1 tokens_in=3710 tokens_out=74 cache_read=3200
```

`purpose` is `handoff`, `screen` (cards and the browser), `decide` or `look`. A fallback adds a warning
line, `text model: Claude failed (timeout) after 4001 ms; asking ChatGPT instead`.

```bash
journalctl --user -u littlebot --since today | grep 'text model:'
```

To compare the two on the same requests:

```bash
.venv/bin/python scripts/compare_text_models.py              # card, light hand-off, decider; twice each
.venv/bin/python scripts/compare_text_models.py --rounds 1   # once each
.venv/bin/python scripts/compare_text_models.py --only claude
```

It builds the requests the way the robot does, offers LittleBot's tools but never runs them (a tool call is
answered "only a test: nothing was done"), and prints each call's time to the first word, its total time,
the medians and its Claude cost estimate. A default run makes about 10 short calls, well under a cent.

## 7. Troubleshooting

| Log line | Meaning |
|---|---|
| `Claude isn't set up (no_model)` | set `claude.model` in config/local.yaml |
| `Claude isn't set up (no_key)` | no `ANTHROPIC_API_KEY` in `.env` and no readable key file |
| `Claude isn't set up (not_installed)` | run `.venv/bin/pip install -e '.[claude]'` |
| `Claude failed (auth)` | the key was refused: check it in the Anthropic console |
| `Claude failed (model_unavailable)` | the model name is wrong or not on your account |
| `Claude failed (rate_limit)` / `(overloaded)` | Anthropic is busy or your account's limits were reached: ChatGPT answered |
| `Claude failed (timeout)` | no first word within `first_token_timeout_seconds` |

Code: `littlebot/brain/providers/claude.py` (the provider), `littlebot/brain/text_model.py` (the choice and
the fallback), `littlebot/decide/claude.py` (the decider), `littlebot/brain/providers/timing.py` (the log line).
