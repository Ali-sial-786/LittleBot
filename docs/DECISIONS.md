# Fast decisions (TypeSafe Jev, OpenAI Decisions API)

LittleBot makes many small multiple-choice decisions during a voice chat: is this worth a card, and which
kind? Is the voice's line the answer, or still "hold on"? Is this hand-off a face, a camera look, or a
question? Most are decided by word lists (fast, but they miss wordings nobody thought of) or by asking ChatGPT
for a whole JSON answer (slower, and it once named card kinds that don't exist).

A **decision model** answers exactly this kind of question: given a few lines of text and a question with
fixed answers, it picks one, with a probability for each, in about 0.2 s. It never writes text. LittleBot can
use either:

| | TypeSafe **Jev** | OpenAI **Decisions API** |
|---|---|---|
| Endpoint | `POST https://api.typesafe.ai/v1/systemone` | `POST https://api.openai.com/v1/decisions` |
| Key in `.env` | `TYPESAFE_API_KEY` | `OPENAI_API_KEY` |
| Model | `jev-latest` (`decide.jev_model`) | `gpt-6-luna` (`decide.openai_model`) |
| Price (2026-10) | $0.042 per million input tokens, output free | $0.10 per million input tokens, output free |

Each decision sends a few hundred tokens, so a busy day is a fraction of a cent. Both are paid, separate
accounts (not part of the ChatGPT plan).

## Turning it on

1. Get a key (console.typesafe.ai for Jev) and add it to `.env` on the robot:
   `echo 'TYPESAFE_API_KEY=ts-...' >> ~/LittleBot/.env`, then `systemctl --user restart littlebot`.
2. Dashboard → **AI** → **Fast decisions**: pick **TypeSafe Jev**. Cloud communication (Privacy) must be on.
3. Every feature starts in **Shadow**: the decision model decides alongside LittleBot's own way, both are
   logged (`decide: cards (shadow): LittleBot's way none, jev show_weather, different`) and nothing changes.
   The panel shows how often they agree. Turn **On** the features that do well; **Off** asks nothing.

The same in `config/local.yaml`:

```yaml
decide:
  provider: jev
  modes: {cards: on, answered: on, routing: shadow}
```

## The features

| Feature | Asks | On: what changes |
|---|---|---|
| `cards` | No card, close the card, steer it, or which card kind (only real kinds can come back), from LittleBot's answer | "No card" and "close" happen without a ChatGPT request; a kind is passed to ChatGPT as a hint for the card's contents |
| `routing` | Is a voice hand-off a face, a camera look, "who is here", or a question for ChatGPT and its tools | Its pick is used when sure and the robot can do it (a face it can't name, or "who" without face/voice recognition, stays the word lists' way) |
| `answered` | Is the voice's line an actual answer, or only "I'm working it out" | A line it is sure is stalling doesn't count; the card keeps waiting for the real answer |
| `browser` | Is what they said still about the web page on the screen | Then it doesn't count as moving on (the browser doesn't start its 20 s close timer) |
| `naming` | Did the voice talk to the grown-up as if they were the baby ("good job, sweetie!") | Put right like the word list's "Hi Amara!" |
| `videos` | Which YouTube search result best fits what was asked for and the child | That one plays first (an approved video at the top always stays first) |
| `names` | Which activity or game they meant, when no name matches closely | That one starts ("the one with the cows" → Old MacDonald) |
| `concern` | Could someone be in danger right now | Only **adds** the dashboard's attention alert when it is very sure (0.85) and the word lists found nothing; never removes one, never changes the voice |

"Sure" is `decide.min_confidence` (0.5) for a choice and `decide.yes_at` (0.7) for a yes/no question. Below
that, LittleBot does what it always did.

Timing: in shadow nothing waits for the decision model. On, `routing` adds its answer time (about 0.2 s, at most
1.5 s) to each hand-off, and `answered` lets a card wait up to 1.5 s for its verdict on a line; `cards` saves a
ChatGPT request (1-2 s) on every turn it says needs no card.

To see how shadow is doing: the dashboard panel (agreement per feature), or
`grep -a "decide:" ~/LittleBot/data/logs/littlebot.log | tail -40`.

## What is sent, and when nothing is

Only the few fields each question needs: for a card, the card on screen, the last few lines of the
conversation and LittleBot's answer; for a hand-off, its words and the last thing heard. What people said goes
through the same personal-details filter as every other cloud request. TypeSafe says it doesn't train on
customer data and keeps data as its Data Processing Agreement says; zero data retention is for its enterprise
customers (typesafe.ai/legal). OpenAI offers zero data retention for eligible API accounts.

Nothing is sent while: the provider is **Off** (the default), its key isn't in `.env`, Cloud communication is
off, LittleBot is in offline mode, the hour's (600) or day's (4000) requests are used up, or after 5 failures in
a row (paused 2 minutes; a refused key pauses at once). Then every feature is LittleBot's own way. Asking
LittleBot "what went wrong?" explains these from its debug log.

## Files

* `littlebot/decide/base.py` - the questions (`Choice`, `YesNo`, `Score`) and answers
* `littlebot/decide/jev.py`, `openai.py` - the two clients (one kept-open connection, `http.py`)
* `littlebot/decide/service.py` - `get_decider(ctx)`: provider, modes, gates, limits, stats, `compare`
* `littlebot/decide/uses.py` - each feature's question
* Callers: `screens/director.py` (cards, answered, browser), `chatgpt/live.py` (routing, naming, concern),
  `chatgpt/tools.py` (videos, names); dashboard `routes/decide_routes.py`, `static/js/pages/decide_panel.js`

## Testing status

Tested with stand-in models and recorded API shapes (tests/decide, tests/screens/test_director_decide.py,
tests/chatgpt/test_live_decide.py, tests/chatgpt/test_tools_decide.py, the dashboard panel in Chromium). Not
yet tried against the real Jev or Decisions endpoints: the request and answer shapes follow their published
documentation (docs.typesafe.ai/api/system-one.md, developers.openai.com/api/docs/guides/decisions).
