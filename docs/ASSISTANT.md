# The assistant: LittleBot's fast voice

LittleBot has two voice pipelines. The wake word ("Hey LittleBot") starts whichever one is in use.

* **The assistant** (new, `littlebot/assistant`): your words → fast speech-to-text that knows when you've
  finished → **one** Claude call that talks, uses LittleBot's tools, sets the face and puts a card on the screen →
  a natural streaming voice. It starts talking as soon as the first sentence is written.
* **GPT-Live** (as before, `littlebot/chatgpt`): ChatGPT's own voice, which hands harder questions to a text model.

## Why a new pipeline

GPT-Live talks by itself, then hands anything that needs LittleBot (the weather, a timer, a video, a card) to a
second model, and the screen asks a third. Each hand-off is a pause, and ten layers of word lists grew around it to
decide who handles what (see docs/rethink/ANALYSIS.md, section 4). The assistant replaces all of that with one
brain per question:

| Step | What | Time (typical) |
|---|---|---|
| You stop talking | Deepgram Flux decides your turn is over (no fixed silence wait) | ~0.25 s |
| Brain | Claude Haiku 5.5, thinking off, prompt cached, streamed | first words ~0.4-0.6 s |
| Voice | Cartesia Sonic (or ElevenLabs Flash) streams the first sentence | first audio ~0.1-0.2 s |
| **Total** | **from the end of your question to the first word you hear** | **about 0.8-1.1 s** |

Things that need no model at all are instant: "stop", "never mind", "louder", "volume 40", "pause", "skip",
"what time is it?", "thanks", "bye", and a voice game's answers ("B!", "Paris").

## Turning it on

`assistant.pipeline` (config/local.yaml):

* `auto` (the default): the assistant once **both** a `DEEPGRAM_API_KEY` and a `CARTESIA_API_KEY` (or
  `ELEVENLABS_API_KEY`) are set; until then GPT-Live, exactly as before.
* `cascade`: always the assistant, with whatever is there (OpenAI speech-to-text and voice with the OpenAI key the
  robot already has, or the robot's own Vosk and espeak).
* `live`: always GPT-Live.

Keys (each one: a hidden prompt, saved in `data/<name>_api_key` with chmod 600, never printed):

```
ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_key.py deepgram"
ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_key.py cartesia"
ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_key.py --check"
```

A key in `~/LittleBot/.env` (`DEEPGRAM_API_KEY=...`) works too and wins over the file. Restart after:
`systemctl --user restart littlebot`. The dashboard's **Assistant** page says which pipeline is on and why.

## How a conversation goes

1. "Hey LittleBot, what's the weather tomorrow?" in one breath works: what you said with the wake word is sent first.
2. LittleBot answers; its face follows its mood sentence by sentence (the brain tags its sentences `[happy]`,
   `[curious]` ...), and a card goes up when seeing helps (the forecast, a recipe, a list, a timer).
3. It keeps listening for **8 seconds** (`assistant.follow_up_seconds`; 12 s when it asked you something): ask the
   next question without the wake word. Then it goes back to waiting for "Hey LittleBot".
4. Talk over it to interrupt (echo cancelling must be on, as for GPT-Live's barge-in). "Stop" quiets it at once.
   Words said while it's still thinking are added to your question.
5. "Play a video", "start peekaboo", "go to sleep": it says so, then hands over the screen and speaker.

The last 12 exchanges (15 minutes, `history_turns` / `history_minutes`) are kept **in memory only**, so "and
tomorrow?" works across follow-ups and the next wake word. Nothing is recorded or written anywhere.

## The brain

`littlebot/assistant/agent.py`. One streamed request per question:

* model: `claude.model` (Haiku 5.5) → `claude.fallback_models` (Haiku 4.5) → ChatGPT (your sign-in): the next one
  is asked only if nothing was said or done yet;
* the system prompt (cached): who LittleBot is, its personality (`skills.persona`, or "be more witty"), how to talk
  for a voice, the face tags, when to use the screen, every skill's note;
* this turn's context: the time and date, home, what the screen shows, a video or timers running, what the skills
  hold (your memories, lists, next reminders, a game);
* tools: everything GPT-Live's hand-offs had (volume, videos, activities, games, weather, time, cards, camera,
  browser, battery, debug log) plus every skill's tools (below), Anthropic's web search, and **think_harder**:
  hard questions (multi-step maths, planning, comparisons) go to `assistant.deep_model` (Claude Sonnet 5.5 with
  thinking) while LittleBot says "Let me think about that";
* the same safety check on every sentence before it is said (`safety/content.validate_reply`, the medicine-dose
  rule); `ai.relaxed_rules` applies as for GPT-Live.

## Skills (both pipelines)

`littlebot/skills`: each skill adds tools and a note to the brain, and GPT-Live's hand-offs get the same tools.

| Skill | Say | Notes |
|---|---|---|
| Memory | "Remember that my sister's birthday is May 3rd", "What do you know about me?", "Forget my sister's birthday" | facts kept on the robot (SQLite); the recent ones go with every question |
| Reminders & alarms | "Remind me at 6 to call mom", "Wake me at 7 on weekdays", "Snooze", "What are my reminders?" | announced by voice with a card, even outside a conversation |
| Lists | "Add milk and eggs to the shopping list", "What's on my to-do list?", "Take milk off" | tap an item on the card to tick it off |
| Personality | "Be more concise", "Be witty", "Go back to normal" | warm, witty, calm, concise, playful |
| Briefing & news | "Good morning", "What's my day?", "What's the news?" | weather, reminders, lists, headlines (CBC, BBC) |
| Smart home | "Turn off the kitchen lights", "Set the thermostat to 21" | Home Assistant: `skills.home_assistant_url` + `scripts/set_key.py home_assistant` |
| Routines | "When I say good night, turn off the lights and set an alarm for 7" | then "Good night" runs it |
| Music | "Play some jazz", "Next song", "Pause the music" | YouTube audio with a now-playing screen |
| Translator | "Translate to Urdu", "Be my interpreter for Spanish", "Stop translating" | |
| Voice games | "Let's play trivia", "Hangman", "20 questions", "Tell me a riddle", "Would you rather" | answers judged instantly on the robot; scores kept |

## Screen

The brain shows a card when seeing helps (weather, a definition, steps, a recipe, a list, maths, a timer, a
note, today at a glance). Double-tap the face for **today at a glance** (time, weather, what's coming up). The
dashboard's Assistant page can send a note to the screen or have LittleBot say something (intercom).

## Measuring

Each answer logs one line (numbers and labels only, never the words):

```
assistant turn: by=brain stt=deepgram first_text_ms=480 first_audio_ms=690 total_ms=2210 tools=get_weather tts=cartesia model=claude-haiku-5-5
```

`first_audio_ms` is from the end of your turn to the first sound. To see them:

```
ssh alisial3@littlebot.local "grep -a assistant.turn ~/LittleBot/data/logs/littlebot.log | tail -20"
```

## Settings (`assistant:` in config/local.yaml, `littlebot/core/config.py` AssistantConfig)

| Key | Default | Meaning |
|---|---|---|
| `pipeline` | `auto` | see above |
| `stt` | `[deepgram, openai, vosk]` | speech-to-text, first that works |
| `tts` | `[cartesia, elevenlabs, openai, local]` | voice, first that works; a voice that fails before a sentence starts hands it to the next |
| `cartesia_voice` / `elevenlabs_voice` / `openai_tts_voice` | Katie / Jessica / coral | the voice |
| `follow_up_seconds` | `8` | listening after an answer without the wake word (0: one question per wake word) |
| `deep_model` | `claude-sonnet-5-5` | think_harder's model (null: off) |
| `barge_in` | `true` | talk over LittleBot to interrupt it |
| `end_of_turn_ms` | `650` | without Deepgram: quiet this long ends your turn (shorter after a full sentence, longer after "and", "um") |
| `history_turns` / `history_minutes` | `12` / `15` | earlier exchanges sent with each question (RAM only) |
| `expressions` | `true` | the face follows the brain's mood tags |
| `log_timings` | `true` | the "assistant turn:" line |

Change one with `scripts/set_config.py`, e.g.
`ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_config.py assistant.follow_up_seconds 10"`.

## Troubleshooting

| Log line | Meaning |
|---|---|
| `assistant: voice pipeline GPT-Live (auto: GPT-Live until DEEPGRAM_API_KEY ... are set)` | add the keys, or set `assistant.pipeline cascade` |
| `assistant: speech-to-text deepgram didn't open (auth)` | the Deepgram key was refused: the next speech-to-text took over |
| `assistant: no voice for a sentence (...)` | every voice failed for one sentence (it was skipped) |
| `assistant: the brain failed (timeout)` | no answer in time: LittleBot says sorry; check the internet |
| `assistant: no speech-to-text is available; GPT-Live answers instead` | no key, no OpenAI key and no Vosk model |

Code: `littlebot/assistant/` (service, session, agent, sentences, intents, voice_out, tts/, stt/),
`littlebot/skills/`, `littlebot/games/voice/`. Tests: `tests/assistant/`, `tests/skills/`.
