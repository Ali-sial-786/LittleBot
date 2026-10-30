# Good morning: what changed overnight

Everything is on the **`rethink`** branch. `main` (what the robot runs now) is untouched, and so is the robot.
The full test suite passes on the branch (about 5,400 tests; the only failures are 2-3 that fail in the cloud
build machine for reasons unrelated to the code: no sound card there, running as root).

## In short

1. **A new, much faster voice** ("the assistant"). Instead of GPT-Live handing questions on to other models
   through ten layers of word lists, one brain answers each question: fast speech-to-text that knows when you've
   finished → **one** Claude Haiku 5.5 call that talks, uses LittleBot's tools, sets the face and puts a card on the
   screen → a natural streaming voice. It starts speaking as soon as its first sentence is written. Expected time
   from the end of your question to the first word: about **0.8-1.1 s** (not measured on the robot yet).
2. **It keeps listening for 8 seconds** after answering: ask the next question without "Hey LittleBot".
   Interrupt it by talking over it; "stop" quiets it at once; "louder", "pause", "what time is it", "thanks"
   are instant (no model at all).
3. **Ten new abilities ("skills")** that work with both voices: memory, reminders and alarms, lists, morning /
   evening briefings with news, smart home (Home Assistant), routines made by voice, music, translator,
   personality styles, and new voice games for grown-ups (trivia, hangman, 20 questions, riddles, would you
   rather).
4. **The face follows the answer**: the brain tags its sentences with moods, and the face changes sentence by
   sentence; it glances up while thinking, looks at you while listening, nods at the end of an answer. Double-tap
   the face for **today at a glance** (time, weather, what's coming up).
5. **Movement is ready for the motors**: a pan/tilt head and two arms (with gestures that go with the
   conversation) and a drive base for the wheels later, all on the simulator; nothing physical is switched on.
6. **The dashboard has an Assistant page**: type to LittleBot, intercom ("say this"), send a note to its screen,
   see and edit its lists, reminders and memories, and which voice is on and why.

**Until you add two keys (Deepgram and Cartesia), the wake word still starts GPT-Live exactly as today**, but the
new skills already work through it ("Hey LittleBot, remind me at 6 to call mom").

## Try it on the robot

Each line works as it is in PowerShell and in a shell on the Pi.

1. Put the robot on the new branch (and make sure two small Python packages are there; usually they already are):

   ```
   ssh alisial3@littlebot.local "cd ~/LittleBot && git fetch -q && git checkout rethink && .venv/bin/pip install -q websockets anthropic && systemctl --user restart littlebot"
   ```

2. See that it started and which voice the wake word uses:

   ```
   ssh alisial3@littlebot.local "grep -a voice.pipeline ~/LittleBot/data/logs/littlebot.log | tail -3"
   ```

3. Add the keys for the fast voice (each asks for the key at a hidden prompt; paste it and press Enter):

   ```
   ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_key.py deepgram"
   ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_key.py cartesia"
   ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_key.py --check && systemctl --user restart littlebot"
   ```

   Once both are set, "Hey LittleBot" uses the assistant by itself (`assistant.pipeline: auto`).

4. Want to try the assistant **before** adding keys? It then uses OpenAI's speech-to-text and voice with the
   OpenAI key already on the robot (slower than Deepgram + Cartesia, but already faster than GPT-Live's
   hand-offs):

   ```
   ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_config.py assistant.pipeline cascade && systemctl --user restart littlebot"
   ```

5. Measure the speed (types questions to the brain and times the voices; nothing is kept):

   ```
   ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/try_assistant.py --measure"
   ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/try_assistant.py --voices"
   ssh alisial3@littlebot.local "grep -a assistant.turn ~/LittleBot/data/logs/littlebot.log | tail -10"
   ```

   The last one shows each real answer's timing (`first_audio_ms` = end of your question to the first sound).

6. Going back: GPT-Live only, on the new branch:
   `ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/set_config.py assistant.pipeline live && systemctl --user restart littlebot"`;
   or the old code entirely:
   `ssh alisial3@littlebot.local "cd ~/LittleBot && git checkout main && systemctl --user restart littlebot"`.

## Keys to add

| Key | For | Where to get it | Cost (about) |
|---|---|---|---|
| **Deepgram** (`scripts/set_key.py deepgram`) | speech-to-text that knows when you finished talking (Flux) | console.deepgram.com (new accounts get free credit) | $0.0077 per minute listened |
| **Cartesia** (`scripts/set_key.py cartesia`) | the voice: the fastest natural one | play.cartesia.ai → API keys | a few dollars a month at home use (credits per character) |
| ElevenLabs (optional, instead of Cartesia) | another natural voice | elevenlabs.io → API keys | per character |
| Home Assistant (optional) | the smart home | your Home Assistant: Profile → Long-lived access tokens; then `scripts/set_config.py skills.home_assistant_url http://homeassistant.local:8123` | free |

Claude (the brain) and OpenAI (backup speech) use the keys already on the robot. "Think harder" questions use
Claude Sonnet 5.5 with the same Anthropic key. Details and prices: docs/ASSISTANT_SPEECH.md.

## What to say

| Try | Say |
|---|---|
| Speed and follow-ups | "Hey LittleBot, what's the weather tomorrow?" … then, without the wake word, "and the weekend?" |
| Interrupt | ask for a long answer ("tell me about the Roman Empire") and talk over it, or say "stop" |
| Instant | "louder", "volume 40", "what time is it?", "pause" (during a video), "thanks" |
| Think harder | "If I invest 300 a month at 6 percent, how much will I have in 15 years?" |
| Memory | "Remember that my sister's birthday is May 3rd." … later "When is my sister's birthday?" / "What do you know about me?" |
| Reminders | "Remind me in 20 minutes to check the oven", "Wake me up at 7 on weekdays", "What are my reminders?" (when it rings: "snooze", "I'm up") |
| Lists | "Add milk, eggs and bread to the shopping list", "What's on my shopping list?", "I bought the milk" |
| Briefing | "Good morning" (the first chat of the day offers it by itself), "What's the news?", "Good night" |
| Personality | "Be more concise", "Be witty", "Back to normal" |
| Routines | "When I say good night, turn off the lights and set an alarm for 7" … then "Good night" |
| Music | "Play some jazz", "Next song", "Pause the music" |
| Translator | "Be my interpreter for Spanish" … "Stop translating" |
| Smart home | "Turn off the kitchen lights" (after the Home Assistant key and address) |
| Games | "Let's play trivia" (answer "B" or the answer), "Let's play hangman", "Twenty questions", "Tell me a riddle", "Would you rather" |
| Screen | double-tap the face (today at a glance); "Show me a recipe for pancakes" |
| Phone | the dashboard's new **Assistant** tab |

## What changed, and why

* **The voice pipeline** (`littlebot/assistant`, docs/ASSISTANT.md). GPT-Live could only *talk*; everything else
  went through hand-offs re-classified by regex word lists, a routing decider, a separate screen director (two
  more model calls per card) and several overlapping stop / "is it a child" / "said to me?" checks. The assistant
  makes one streamed call per question: the words, the tools, the face tags and the card come together. It reuses
  the parts that were proven on the robot: the call audio with echo cancelling and barge-in, the lip-sync, the
  wake word and what you say in the same breath as it, the safety check on every sentence.
* **Speed**: Deepgram Flux decides the end of your turn (no fixed silence wait) and even starts the brain early
  when you're "probably done" (the answer is only said once your turn really ended); Claude Haiku 5.5 with
  thinking off and the instructions cached; the first sentence goes to the voice while the rest is written;
  connections are opened while the microphone opens; instant answers need no model.
* **Skills** (`littlebot/skills`): every new ability is a small module with tools and a note for the brain, used
  by both voices, so nothing new is hard-wired into the voice code again.
* **Fallbacks everywhere**: no Deepgram → OpenAI → Vosk on the robot; no Cartesia → ElevenLabs → OpenAI → espeak;
  Claude Haiku 5.5 → Haiku 4.5 → ChatGPT; no speech-to-text at all → GPT-Live.
* **Kept for now**: GPT-Live and all its layers stay as they are, so nothing is lost while you try the new one.
  Once you're happy, the old hand-off code in `chatgpt/live.py` and `screens/director.py` can be retired (a
  cleanup I didn't do tonight on purpose).

## Still open

* **Nothing has run against the real services or on the robot yet**: no keys and no robot here. Everything is
  tested with fakes (the requests follow each service's current documentation). The first run with keys is the
  real test; the log says clearly when a service refuses a key, and LittleBot falls back meanwhile.
* Barge-in uses GPT-Live's tuned settings (pitch 220 Hz on your robot); it may want tuning with the new voice.
* The music screen keeps the YouTube video visible in a smaller box (YouTube's rules don't allow hiding it).
* Deepgram Flux is English only (other languages would use the OpenAI speech-to-text).
* Movement (docs/MOVEMENT.md): the head now has a tilt joint (`head_tilt`, channel 3) next to the pan (`head`). When
  the servos arrive, calibrate all of them (`scripts/calibrate_servos.py`); a robot without a tilt servo lists only
  its other joints under `motion.servos`. The wheels' drive base isn't connected to any tool yet.
