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

Deepgram also says when you are *probably* done ("eager end of turn"): the brain starts then and its first
sentence is already made into voice, but it is heard (and its tools run) only once your turn really ended with the
same words; if you carry on, it is dropped. At 0.3 that signal comes right as you stop, about 0.3 s before the turn
really ends, so the answer is nearly ready by then (`assistant.deepgram_eager_eot_threshold`, null: off).

Things that need no model at all are instant: "stop", "never mind", "louder", "volume 40", "pause", "skip",
"what time is it?", "thanks", "bye", "snooze" / "I'm up" while an alarm rings, and a voice game's moves ("let's
play trivia", "B!", "Paris", "is there an E?", "play again").

## Turning it on

`assistant.pipeline` (config/local.yaml):

* `auto` (the default): the assistant once **both** a `DEEPGRAM_API_KEY` and a `CARTESIA_API_KEY` (or
  `ELEVENLABS_API_KEY`) are set; until then GPT-Live, exactly as before.
* `cascade`: always the assistant, with whatever is there (OpenAI speech-to-text and voice with the OpenAI key the
  robot already has, or the robot's own Vosk and espeak).
* `live`: always GPT-Live.

The speech services (which ones, prices, measured latencies, privacy): [ASSISTANT_SPEECH.md](ASSISTANT_SPEECH.md).

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
   With echo cancelling on, the assistant starts listening the moment it hears its name, and plays the "listening"
   sound itself only if you wait for it (none when you go straight on). Speech-to-text then hears your question as
   you say it, not as a recording it has to catch up on: on the robot that ended turns about half a second sooner.
2. LittleBot answers; its face follows its mood sentence by sentence (the brain tags its sentences `[happy]`,
   `[curious]` ...), and a card goes up when seeing helps (the forecast, a recipe, a list, a timer).
3. It keeps listening for **8 seconds** (`assistant.follow_up_seconds`; 12 s when it asked you something): ask the
   next question without the wake word. Then it goes back to waiting for "Hey LittleBot".
4. Talk over it to interrupt (echo cancelling must be on, as for GPT-Live's barge-in). "Stop" quiets it at once.
   Telling it to stop in any words ("stop talking", "shh", "end the call", "that's all", "can you be quiet")
   ends the conversation **without a word**; "wait" / "hold on" only pauses it and it listens. Words said while
   it's still thinking are added to your question.
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
  rule); `ai.relaxed_rules` applies as for GPT-Live;
* short answers: one or two sentences of about 15 words (a how-to, a comparison or a week's weather: three), the
  card holding the details. A sentence of the brain's own plan ("no tools needed, keep it brief") or about its card
  ("I'll put it on the screen") is never said, nor a second "Done, I've..." for an action it already announced.

## Who is talking

Both are opt-in and run on the robot only (only a name reaches the brain, never a voiceprint or a picture):

* **Voice** (docs/VOICES.md): with **Recognise family voices** on (dashboard → Privacy) and each grown-up's voice
  recorded (Profile → Who's who), the assistant recognises who said "Hey LittleBot" and who said each turn, in the
  background (an answer never waits for it), and the brain is told "the person talking is Ali (dad)". It then uses
  your name now and then and your own memories ("my sister" means *your* sister).
* **Over the whole conversation** (`littlebot/assistant/who.py`): one clear turn is enough; after that a short
  "yes" or an unclear turn that still sounds closest to you keeps you as the speaker, while a turn that clearly
  sounds like someone else switches at once (and one that is too close to call makes it "probably" only).
* **Voice and face together**: a voice that isn't clear on its own but is closest to the one grown-up in front of
  the camera is them. With face recognition on and no voice to go on, the one grown-up in view is "probably" them.
* **Learning by talking**: say "I'm Ali" (or "it's her dad") and that turn's voiceprint is kept for you; turns it is
  very sure about sharpen your voiceprint over time (a few at most, oldest first: recordings are never pushed out).
* **Faces**: blurred and turned-away faces are no longer compared (that's where most wrong or missed matches came
  from); new samples must be sharp and facing the camera too.
* **"Do you know who I am?"**: the brain takes a fresh look and puts the face and the voice together (`who_is_here`),
  and never guesses a name.

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
| Music | "Play some jazz", "Play my liked songs", "Next song", "What's playing?", "Play it on YouTube" | Spotify by default once it is set up ([SPOTIFY.md](SPOTIFY.md): Premium, `scripts/setup_spotify.sh`, Connect on the dashboard), with a now-playing card; else YouTube audio with a now-playing screen |
| Translator | "Translate to Urdu", "Be my interpreter for Spanish", "Stop translating" | |
| Voice games | "Let's play trivia", "Hangman", "20 questions", "Tell me a riddle", "Would you rather" | answers judged instantly on the robot; scores kept |

## Screen

The brain shows a card when seeing helps (weather, a definition, steps, a recipe, a list, maths, a timer, a
note, today at a glance). If it answers with facts but forgets the card, a short second request makes the card
from what it said, while it is still talking (`card_backup`). A card the brain sends in an odd shape is still
shown: if it can't be made as sent, its text is shown as a facts card (only the safety rules, or a card with no
text at all, keep one off the screen). Double-tap the face for **today at a glance**
(time, weather, what's coming up). The dashboard's Assistant page can send a note to the screen or have LittleBot say something (intercom).

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
| `cartesia_voice` / `elevenlabs_voice` / `openai_tts_voice` | Tessa / Jessica / coral | the voice |
| `cartesia_speed` | `1.0` | 0.6-1.5 (1.0 normal; 0.9 is a little slower) |
| `cartesia_emotion` | `calm` | the voice's emotion when a sentence has no mood; tagged sentences get a gentle matching one ([happy] → content, [sad] → sympathetic, [excited] → happy) |
| `cartesia_concurrency` | `1` | Cartesia requests open at once (the free plan allows 2: one is kept spare for a sentence just cut off). The next sentence is made while one plays, so there's no gap |
| `pronunciations` | `{Genghis: Jenghis}` | words the voice says wrong, respelled for it (whole words, any case); add your own, e.g. a name |
| `follow_up_seconds` | `8` | listening after an answer without the wake word (0: one question per wake word) |
| `deep_model` | `claude-sonnet-5-5` | think_harder's model (null: off) |
| `barge_in` | `true` | talk over LittleBot to interrupt it |
| `end_of_turn_ms` | `650` | without Deepgram: quiet this long ends your turn (shorter after a full sentence, longer after "and", "um") |
| `history_turns` / `history_minutes` | `12` / `15` | earlier exchanges sent with each question (RAM only) |
| `expressions` | `true` | the face follows the brain's mood tags |
| `card_backup` | `true` | an answer with facts said without a card gets one made from its words (a cheap Haiku call, no wait) |
| `log_timings` | `true` | the "assistant turn:" line |

Change one with `scripts/set_config.py`, e.g.
`ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_config.py assistant.follow_up_seconds 10"`.

## Troubleshooting

| Log line | Meaning |
|---|---|
| `assistant: voice pipeline GPT-Live (auto: GPT-Live until DEEPGRAM_API_KEY ... are set)` | add the keys, or set `assistant.pipeline cascade` |
| `assistant: speech-to-text deepgram didn't open (auth)` | the Deepgram key was refused: the next speech-to-text took over |
| `assistant: microphone littlebot_ec_mic, speaker littlebot_ec_speaker, volume 100%` but nothing is heard | the echo canceller plays on another output: LittleBot moves it back by itself (`audio: the echo canceller plays on ...: moved it back`); by hand: `.venv/bin/python -m littlebot.audio.ec_route --fix` (docs/TROUBLESHOOTING.md, "No sound") |
| `assistant tts: cartesia is busy; asking again` | Cartesia refused a request (too many at once): it is asked again half a second later, so the voice stays the same |
| `assistant tts: cartesia failed (rate_limit); trying the next voice` | still busy: the next voice (OpenAI's, a different one) says that sentence, and Cartesia rests 15 s |
| `assistant: no voice for a sentence (...)` | every voice failed for one sentence (it was skipped) |
| `assistant: the brain failed (timeout)` | no answer in time: LittleBot says sorry; check the internet |
| `assistant: no speech-to-text is available; GPT-Live answers instead` | no key, no OpenAI key and no Vosk model |

Code: `littlebot/assistant/` (service, session, agent, sentences, intents, voice_out, tts/, stt/),
`littlebot/skills/`, `littlebot/games/voice/`. Tests: `tests/assistant/`, `tests/skills/`.
