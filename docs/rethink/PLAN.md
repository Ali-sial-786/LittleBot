# Overnight rethink: the plan (a checklist)

Written 2026-10-30 ~04:20 EDT by the cloud session, from BRIEF.md and ANALYSIS.md. Ticked items are done,
committed and pushed on `rethink` with the full test suite passing.

## The idea in one paragraph

Today a wake word starts GPT-Live, which talks by itself and hands harder questions to a text model through
~10 layers of regex re-routing, a separate screen director (a second and third model call per answer), and
several overlapping stop / "is it a child" / "said to me?" checks. The rethink replaces that with **one fast
brain per turn**: streaming speech-to-text with real end-of-turn detection → one Claude Haiku 5.5 call that
speaks, uses tools, picks the face's expression and puts a card on the screen in the same streamed answer →
streaming human-sounding text-to-speech, with barge-in and a follow-up window (no wake word for the next
question). New abilities are **skills** (tools + a prompt line + optional cards) that plug into that brain
and into GPT-Live's hand-offs alike, so they work whichever voice is on. GPT-Live stays as the fallback, so the
robot keeps working exactly as today until the new keys are added.

## Decisions (made overnight; easy to change later)

- **Voice pipeline**: new `littlebot/assistant` ("cascade"). `assistant.pipeline: auto` uses it when a streaming
  STT key (Deepgram) and a fast TTS key (Cartesia or ElevenLabs) are set, else GPT-Live as today; `cascade` forces
  it (then it uses whatever exists: OpenAI STT/TTS with the OpenAI key already on the robot, local Vosk/espeak
  last); `live` keeps GPT-Live.
- **STT**: Deepgram Flux (streaming, end-of-turn built in) → OpenAI gpt-4o-mini-transcribe after local
  end-of-turn → local Vosk. **TTS**: Cartesia Sonic (lowest time-to-first-audio) → ElevenLabs Flash v2.5 →
  OpenAI gpt-4o-mini-tts → local (piper/espeak). Chatterbox needs a GPU: not on the Pi.
- **Brain**: Claude Haiku 5.5 (thinking off, streamed, prompt-cached) → Haiku 4.5 → ChatGPT, through the existing
  `text_model` chain. A `think_harder` tool hands hard questions to a stronger model (Claude Sonnet 5.5, adaptive
  thinking) while a filler line plays.
- **Audio transport**: reuse `chatgpt/live_audio.LiveAudio` (full duplex 48 kHz, echo tail, barge-in detector,
  lip-sync, device stall/reopen: all proven on the robot) instead of writing a new one.
- **Screen**: no screen director on the new path: the brain calls `show_on_screen` itself (marked a final tool,
  so it never costs a round trip after the words). New card kinds for the new skills.
- **Face**: inline expression tags in the brain's words (`[happy]`, `[curious]` …, stripped before TTS) drive the
  face sentence by sentence; attention cues (look up while thinking, look at the user while listening, nods on
  phrase ends) come from one "body language" module that drives the face now and the arms/head later.
- **Games**: keep the toddler touch games; add voice + screen games for grown-ups and family (trivia, hangman,
  20 questions, riddles, would-you-rather), judged locally (no model wait for "B!" or "Paris"), scores kept.
- **Movement**: head pan + tilt and two arms as ready-for-hardware modules (mock/simulator), driven by body
  language; a drive-base protocol + mock for the wheels phase. Nothing physical is switched on.

## Checklist

### 0. Setup
- [x] Read BRIEF/ANALYSIS/PROGRESS; hourly resume task; baseline tests (4,930 pass; 3 container-only failures:
      no PortAudio, running as root, install-script dry run with extras already installed).
- [x] PLAN.md.

### 1. The fast voice pipeline (`littlebot/assistant`)
- [x] Config `assistant:` section (pipeline, stt, tts, voice ids, follow-up seconds, persona, deep model) + key
      loading (DEEPGRAM_API_KEY, CARTESIA_API_KEY, ELEVENLABS_API_KEY; .env or data/<name>_api_key, chmod 600).
- [x] TTS providers (cartesia, elevenlabs, openai, local) behind one streaming protocol + auto chain + tests.
- [x] STT providers (deepgram flux streaming, openai batch, vosk local) behind one protocol + endpointing + tests.
- [x] Sentence streamer: model deltas → speakable sentences (tags stripped, expressions extracted).
- [x] Agent: one brain call per turn (persona, memory, live context, tools incl. skills, final-tool cards,
      expression tags, filler on slow tools, think_harder) + instant local intents (stop, volume, pause …).
- [x] VoiceSession: LiveAudio, carry from the wake word, STT stream, turn end → agent → TTS, barge-in
      (BargeInDetector + STT words confirm), follow-up window, idle end, take-overs after speaking, cleanup.
- [x] AssistantService (`ctx.assistant`): start_session/stop_session/status like live; handle_text for typed chat;
      wake word routes to it when the pipeline says so; timing log line per turn (stt/brain/tts first audio).

### 2. Skills (new abilities, in both pipelines)
- [x] Skill registry: tools + prompt notes + run; offered by RobotToolbox (GPT-Live hand-offs) and the agent.
- [x] Memory (remember / recall / forget; facts in the prompt).
- [x] Reminders and alarms (natural times, repeat daily, announced proactively with a card, snooze).
- [x] Lists (shopping, to-do, any name): add / remove / read / clear; list card.
- [x] Personality styles (warm, witty, calm, concise, playful) switchable by voice.
- [x] Briefing ("good morning"): weather, reminders, lists, headlines (RSS, no key); evening version.
- [x] Smart home (Home Assistant REST: url + token), not offered until set up.
- [x] Routines made by voice ("when I say good night, …").
- [x] Music mode (YouTube audio with a now-playing screen) and translator mode.

### 3. Games
- [ ] Voice game framework (local judging, cards, scores in SQLite, "what's my high score").
- [ ] Trivia (model-written questions, offline bank), Hangman, 20 Questions, Riddles, Would You Rather.

### 4. Face and screen
- [ ] Expression tags → face per sentence; thinking/listening/speaking attention cues (body language module).
- [x] New cards: list, reminders, quiz, hangman, briefing, home (glanceable time/weather/next reminder).

### 5. Movement (ready for hardware)
- [ ] Head pan + tilt joints, look_at(x, y), nod / tilt / shrug / think gestures; body language drives them.
- [ ] Drive base protocol + mock (wheels phase), off by default.

### 6. Dashboard companion
- [x] Assistant page: type to LittleBot, intercom ("say this"), send to screen, lists / reminders / memories.

### 7. Wrap-up
- [ ] docs/ASSISTANT.md (how it works, settings, keys); MORNING.md; Status: DONE.
