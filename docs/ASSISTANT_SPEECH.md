# The assistant's ears and voice (speech services)

The new voice pipeline (`littlebot/assistant`, the "cascade") listens with a **speech-to-text** service and
answers with a **text-to-speech** voice. Each side is a list of services tried in order; the first one that is
set up and allowed is used, and if it fails LittleBot quietly moves to the next. Without any new keys it still
works, with the OpenAI key the robot already has, or entirely on the robot (Vosk and espeak) with no network.

> **Status (2026-10-30).** Written and tested overnight with fake services only (no keys on the build
> machine): the requests follow the services' current API documents (links below), but **none of them has
> been tried against the real service or on the robot yet**. The first run with real keys is the test.

## 1. What is used, in order

| Listening (`assistant.stt`) | What it is | Needs |
|---|---|---|
| `deepgram` | **Deepgram Flux**: streams the words while you talk and itself decides when you finished (it hears the difference between a pause and the end of a sentence) | `DEEPGRAM_API_KEY`, the `websockets` package, cloud on |
| `openai` | **OpenAI gpt-4o-mini-transcribe**: the whole turn is sent once LittleBot decides you finished | `OPENAI_API_KEY` (already on the robot for GPT-Live), cloud on |
| `vosk` | **Vosk** on the robot, the small English model the wake word already uses | nothing (offline). Less accurate |

| Speaking (`assistant.tts`) | What it is | Needs |
|---|---|---|
| `cartesia` | **Cartesia Sonic**: the fastest to start talking, natural voice ("Katie") | `CARTESIA_API_KEY`, cloud on |
| `elevenlabs` | **ElevenLabs Flash v2.5**: very natural voice ("Jessica") | `ELEVENLABS_API_KEY`, cloud on |
| `openai` | **OpenAI gpt-4o-mini-tts**, voice "coral", told to sound warm, natural and calm | `OPENAI_API_KEY`, cloud on |
| `local` | the robot's own voice: piper if `audio.tts.provider: piper` and its model is installed, else espeak-ng | nothing (offline). Robotic with espeak. Always there, always last |

"Cloud on" means the parent's **cloud switch** (dashboard, Privacy) is on and the robot is not in offline mode.
When the switch is off, only Vosk and the local voice are used, and nothing is sent anywhere.

What happens when something is missing or goes wrong:

* **No key** (or no `websockets` package, or no Vosk model): that service is skipped. The log says so once,
  for example `assistant tts: cartesia skipped (no_key)`.
* **A voice fails or is slow:** if it hasn't made a sound within `assistant.tts_first_audio_timeout_seconds`
  (2.5 s), the same sentence goes to the next voice, so you never wait long. The slow or failing voice is then
  left alone for a while (20 s; a refused key or a wrong voice id: 10 minutes; "too many requests": 1 minute),
  so the next sentences don't wait for it again. Log: `assistant tts: cartesia failed (timeout); trying the
  next voice`.
* A voice that fails **in the middle** of a sentence is not replaced for that sentence (you would hear the
  start of it twice); the next sentence goes to the next voice.
* **Listening fails** (Deepgram can't connect, the connection drops, the key is refused): that service is left
  alone for a while the same way and the conversation carries on with the next one (OpenAI, then Vosk).

The dashboard and the log can show which service would be used now and which was used last
(`describe()`); they never show a key.

## 2. Putting the keys on the robot

The keys are read **on the robot only**, exactly like the Anthropic key. They never go to the dashboard's
pages, the browser or the logs (the dashboard only says whether a key is set). Use **one** of these per key:

* **`.env`** in `~/LittleBot` (the file LittleBot already reads):

  ```bash
  DEEPGRAM_API_KEY=...
  CARTESIA_API_KEY=sk_car_...
  ELEVENLABS_API_KEY=...
  ```

* **or a key file** in `~/LittleBot/data/` holding just the key, readable only by you:

  ```bash
  nano ~/LittleBot/data/deepgram_api_key        # paste the key, save
  chmod 600 ~/LittleBot/data/deepgram_api_key
  ```

  The file names are `deepgram_api_key`, `cartesia_api_key` and `elevenlabs_api_key`. A key file other users
  can read is **ignored** (the log says to run `chmod 600`, never what is in it).

The variable in `.env` (or the environment) wins over the file. In offline mode no key is loaded at all.

Where to get them: Deepgram at console.deepgram.com (new accounts get $200 of free use), Cartesia at
play.cartesia.ai/keys, ElevenLabs at elevenlabs.io (Profile, API keys).

Deepgram also needs the `websockets` package (it isn't one of LittleBot's standard packages yet):

```bash
cd ~/LittleBot && .venv/bin/pip install websockets
```

Without it Deepgram is skipped (`assistant stt: deepgram skipped (not_installed)`).

## 3. How fast, and what it costs

All figures are the services' own unless marked; none was measured on the robot yet. Prices read on
2026-10-30, pay-as-you-go, in US dollars.

| Service | Latency | Price | Sources |
|---|---|---|---|
| Deepgram Flux (listening) | ends a turn ~260 ms after you stop (p95 1.5 s) | $0.0077 per minute of listening | [pricing](https://deepgram.com/pricing), [Flux launch](https://deepgram.com/learn/introducing-flux-conversational-speech-recognition) |
| OpenAI gpt-4o-mini-transcribe | LittleBot's pause (0.65 s) + roughly 0.3-0.8 s upload and transcription (estimate) | about $0.003 per minute (third-party estimate from OpenAI's token prices) | [API reference](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create) |
| Vosk (on the robot) | LittleBot's pause; the words are ready right after | free | |
| Cartesia Sonic | first sound under 100 ms of model time (vendor); one independent benchmark reported ~190 ms median end to end | 1 credit per character (~750-800 per minute of speech); free plan 20,000 credits a month (~27 min), Pro $5/month for 100,000 (~133 min) | [API reference](https://docs.cartesia.ai/api-reference/tts/bytes), [pricing](https://cartesia.ai/pricing) |
| ElevenLabs Flash v2.5 | first sound ~75 ms of model time (vendor) | about $0.04 per 1,000 characters (roughly a minute of speech); a free tier of 20,000 characters | [API reference](https://elevenlabs.io/docs/api-reference/text-to-speech/stream), [pricing](https://elevenlabs.io/pricing/api) |
| OpenAI gpt-4o-mini-tts | no official figure | about $0.015 per minute (third-party estimate from OpenAI's token prices: $0.60 per million text tokens in, $12 per million audio tokens out) | [API reference](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create) |
| Local (espeak / piper) | espeak ~0.1 s, piper ~0.5-1.5 s per sentence on a Pi 5 (estimate) | free | |

On top of the model's time comes the trip over the internet (usually 50-150 ms each way from home). LittleBot
keeps the connection to each voice open between sentences (and opens it before the first one), so the slow
part of connecting (~0.1-0.4 s on Wi-Fi) is paid once, not per sentence.

**Note:** OpenAI says the gpt-4o-mini-tts models shut down on **2027-01-06**. Before then, set
`assistant.openai_tts_model` to its successor (or rely on Cartesia / ElevenLabs).

A rough example: half an hour of conversation a day, of which LittleBot talks about 15 minutes. Deepgram is
paid for every minute the microphone is streamed (the whole conversation): 15 hours a month, about **$7**.
LittleBot's 7.5 hours of talking (~360,000 characters) would be about **$14** with ElevenLabs, about **$7**
with OpenAI, and more than Cartesia's $5 Pro plan includes (its next plan, Startup, is $49 a month for ~1,667
minutes). Less talking (a few questions a day) fits in Cartesia's or ElevenLabs' free or $5 plans.

## 4. Privacy

* Nothing is recorded or stored: the microphone audio of a turn is kept in memory only until it has been
  turned into words, then dropped. Sentences to be spoken are sent and forgotten.
* Every cloud service is used **only while the cloud switch is on** (checked for every sentence and every
  80 ms of audio: switching it off mid-conversation stops sending at once, and LittleBot carries on with
  Vosk and the local voice).
* Deepgram is asked not to use the audio to improve its models (`mip_opt_out=true`; since 2026-03-05 this
  doesn't change the price for pay-as-you-go accounts).
* Audio goes to Deepgram as raw 16 kHz sound in 80 ms pieces; to OpenAI as one short WAV per turn, made in
  memory.

## 5. Settings (config `assistant:` section)

Change them in `config/local.yaml` (or with `scripts/set_config.py`):

| Setting | Default | What it does |
|---|---|---|
| `stt` | `[deepgram, openai, vosk]` | the listening services, in order |
| `tts` | `[cartesia, elevenlabs, openai, local]` | the voices, in order (local is always added last) |
| `deepgram_eot_threshold` | 0.6 | how sure Flux must be that you finished (higher: fewer interruptions, a little slower) |
| `deepgram_eager_eot_threshold` | 0.4 | when the brain starts on "probably done" (null: off) |
| `deepgram_eot_timeout_ms` | 3000 | Flux ends the turn after this much silence, however unsure it is |
| `cartesia_voice`, `cartesia_speed` | Katie, normal | the Cartesia voice id and speed (slowest ... fastest) |
| `elevenlabs_voice` | Jessica | the ElevenLabs voice id |
| `openai_tts_voice` | coral | alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer, verse, marin, cedar |
| `tts_first_audio_timeout_seconds` | 2.5 | how long a voice may take to start before the next one is asked |
| `end_of_turn_ms` | 650 | for OpenAI and Vosk listening: how long a pause ends your turn |
| `max_turn_seconds` | 30 | a turn ends after this long whatever happens |

**How LittleBot decides you finished** (for OpenAI and Vosk; Deepgram decides by itself): it listens to the
loudness of the microphone. You start a turn once it hears about a tenth of a second of voice (louder than
`audio.microphone.vad_threshold`, or three times the room's background noise when that is louder). Your turn
ends after a pause of `end_of_turn_ms`. With Vosk it also knows the words so far: after something that sounds
like a whole sentence ("what time is it in Tokyo") it waits less (0.7x, about 0.45 s), after "and", "um",
"the", "because" ... it waits longer (1.8x, at most 2 s), so you aren't cut off mid-thought. Coughs, claps
and door bangs (less than a quarter second of voice) don't count as a turn.

## 6. For developers

* `build_tts_chain(ctx)` (`littlebot/assistant/tts/__init__.py`) -> `TTSChain`: `stream(text)` yields
  float32 mono chunks at 48 kHz; `warm()`, `aclose()`, `describe()`, `last_provider`.
* `build_stt(ctx)` (`littlebot/assistant/stt/__init__.py`) -> `STTChain` or None: `await open(on_event)`
  gives an `STTStream` (`feed`, `finish`, `reset`, `aclose`) that emits `start_of_turn`, `partial`,
  `end_of_turn` and `error` events (`littlebot/assistant/speech.py`).
* Tests: `tests/assistant/test_speech_tts.py`, `test_speech_stt.py`, `test_endpointer.py` (fake HTTP and
  websocket servers; no network, no keys).
