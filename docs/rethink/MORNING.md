# Good morning: what changed overnight

Everything is on the **`rethink`** branch. `main` is untouched; since the laptop session the robot runs `rethink`
(going back: step 6 of "Try it on the robot").
The full test suite passes on the branch (about 5,760 tests, 830 of them new; the only failures are 2-3 that
also fail on `main` in the cloud build machine: no sound card there, running as root). The new voice session was
also read by an independent reviewer and its 12 findings fixed.

## Laptop session (the night of 30-31 October, newest)

The robot now runs `rethink` with the fast voice (`assistant.pipeline: cascade`); `main` is still untouched. All night
the laptop next to the robot played questions in recorded voices, recorded what came back, and read the robot's log.
Each fix below has a test, and most were checked again live. The full test suite on this Windows PC: 5,722
passed; the 19 failures are all Windows-only (file permissions, time zones, Linux paths and tools, no camera library
installed here), none from tonight's changes.

**One thing for you first: Cartesia (the fast voice) is out of credits** since about 7 am (all the testing used
the plan's allowance). The robot switched to OpenAI's voice by itself, which starts about half a second later. To get it
back: play.cartesia.ai → your subscription → top up, or allow extra usage. The robot tries Cartesia again every
hour, or at once after `ssh alisial3@littlebot.local "systemctl --user restart littlebot"`.

**How fast it is now** (end of your question → its first sound, measured on the robot):

* With Cartesia: about 1.0 s (half the answers faster, the slowest 1.3 s; before tonight up to 2.7 s).
  With OpenAI's voice this morning: about 1.4 s (0.8-2.3 s). Instant answers ("louder", "what time is it"): 0.3 s.
* The end of your question is noticed usually 0.1-0.5 s after your last word (now and then about 1 s, after an
  unusual name). "Stop" while it talks: in 20-40 ms.
* Where the time goes: the ReSpeaker and the echo canceller take about 0.35-0.4 s before your voice even reaches
  the robot's software; deciding you've finished 0.1-0.5 s; the brain's first words 0.6 s (rarely 1.2 s); then the
  voice's first sound (Cartesia's is the quickest). A lookup (weather, news) adds 0.5-1.5 s, covered by a short
  "Let me check" or a soft sound.

**Fixed tonight** (the main ones, in plain words):

* It listens the moment it hears its name, so a question said in the same breath goes in live, and the "ding"
  plays only if you pause. Leftovers of its name heard as part of the question ("little bug", "middle butt",
  "little dots") are taken off.
* "Stop!" over a long answer works now. The echo canceller damages a short word said over the voice, so it
  scored under the bar and its pitch read like a child's; the news was read on for 38 s. Now it stops at once.
  "Stop talking", "shh" and a lone "Talking." work too.
* Its own voice heard back no longer cuts it off, and the start of its own sentences can't be taken for you
  cutting in. But you repeating a request isn't mistaken for its echo any more.
* It remembers what its tools really did. It had said "I should correct myself, I never actually checked the
  volume"; after an answer was cut off, it had queued a video that never started, and said "Good morning" again.
* Actions are confirmed once ("Sure. Done, I'll remind you at 7:38"), not before and after.
* If the brain's first word hasn't come in 2 s, the backup model answers (a 4 s silence happened once).
* The news comes in 1.7 s instead of 4.5 s when one news site is slow (the CBC kept timing out).
* A service out of credits is logged as that, not as a bad key, and the next one takes over for an hour.
* Spotify: every search had been refused (a wrong request setting); fixed.

**Checked, not changed:**

* The ReSpeaker firmware wasn't flashed. Both of its channels are the same processed sound, so other firmware or
  I2S wouldn't change the 0.3 s round trip through the ReSpeaker and the USB speaker. The cheap test that could:
  plug a speaker into the ReSpeaker's own headphone jack. That also lets its echo canceller hear exactly what is
  played.
* A whole question said over a long answer is sometimes erased by the echo canceller at 70% loudness. At 60% one
  got through. "Stop" works either way. The speaker on the ReSpeaker's jack would help most.
* The volume ceiling is 100%, not 70%: `LITTLEBOT__AUDIO__SPEAKER__MAX_VOLUME` in `~/LittleBot/.env` beats the 0.7
  in `config/local.yaml`. (An earlier note here said "100%" was really 70%; that was wrong.) It is back at 100%,
  where you had it.

**Fixed later in the morning** (each with a test, checked with the real brain and live; full test suite after:
5,759 passed, the same 19 Windows-only failures):

* "Turn the volume down a bit", "a little louder, please", "it's too loud", "set the volume to one hundred percent"
  are instant now (about 0.8 s with OpenAI's voice). Before, they went to the brain, which checked the volume first
  (2-2.3 s). Speech-to-text writing "Turned the volume down a bit." counts too.
* When the brain starts a lookup or an action before saying anything, LittleBot says "One sec." or "Sure." at once
  (on the first chat of the day, "Good morning."), and the brain is told it was said. With the real brain the first
  word came at about 0.8 s, where the news once took 3.7 s.
* The brain is told the volume tools report the new level, so it no longer checks first. A request that makes no
  sense as heard ("a little leopard, please") is treated as misheard: in 4 of 4 tries it asked "Did you mean a
  little louder?" and started no video. "I want to watch a video about leopards" still plays one.
* Speech-to-text (Deepgram) listens harder for "LittleBot", "louder", "quieter" and "volume"
  (`assistant.deepgram_keyterms`).
* Saying the date before the weather didn't happen again in the 3 weather answers after these fixes; nothing was
  changed for it.
* "Hey little bot, stop the video" in one breath: during a video the words after its name were thrown away, so it
  woke, heard nothing and the video carried on. The video plays through the echo-cancelled speaker, and the
  echo-cancelled microphone was near silent under its narration, so those words are kept now (checked live: it
  heard "stop the video" and stopped it).
* Still weak: waking it over a loud video (100%). The echo canceller erased "little bot" from what it heard
  ("Hey, stop the video."). It woke 2 times out of 6 (`wake.video_duck`, turning the video down after "hey", was
  tried and put back off: 1 of 3 with it). The speaker on the ReSpeaker's jack, or a quieter video, should help.

**Cards, and what the card tests turned up (early afternoon)** (each with a test; checked with the real brain and
live on the robot):

* Every kind of card was tried with the real brain: 12 card questions (a person, a place, a word, how-to, a
  recipe, a comparison, a number, a sum, the time somewhere) and 6 chit-chat ones. All 12 got a card, and none of
  the chit-chat did. This morning it was 2 or 3 of 8.
* A comparison ("crocodile or alligator?") often came in a shape the screen couldn't read, so either no card came
  or two cut-off lines did. It now shows side by side, each animal's facts as short points.
* The brain still forgets the card now and then (resetting a breaker, cups in a liter, the time in Tokyo). A quick
  second request now makes the card from what it said, while it is still talking (about 1.5 s; it says "none" for
  chit-chat). `assistant.card_backup: false` turns it off.
* "What's the square root of seventeen sixty four?": speech-to-text writes numbers as words, and the brain once
  said "about forty-one point nine". LittleBot now works out a sum said in words itself and gives the brain the
  exact result.
* The time in Tokyo: it said "nine hours ahead" (13 is right). The time tool now says how far ahead or behind home
  a place is.
* It once said its own reasoning out loud ("... is general how-to knowledge, so I can answer directly."). The
  prompt says never to, and a sentence like that is dropped before it is said.
* It said "Done, I've forgotten your favorite color" without forgetting it (once; 12 tries after that were fine).
  The prompt now says never to say something was done unless its tool did it, and the log warns if it happens. The
  test's fact was taken out of its memory afterwards.
* It stopped itself in the middle of an answer: its own voice, leaking through the echo canceller, was heard as
  "no stop". OpenAI's voice (used while Cartesia has no credits) is about 200 Hz, inside a grown-up's range, so the
  pitch rule let it through. A stop word heard at the pitch the voice itself was playing at that moment, and not
  clearly said, no longer stops it. The log line for a stop now says how sure it was and the voice's own pitch.
* Square roots work on the maths card, and a sum isn't titled "Converting" any more. A recipe card is only for
  cooking (a flat tire got one, with "Ingredients").
* Still weak: talking over the end of a long answer. "Set an alarm for 7", said while it was still finishing the
  Tokyo answer, was never heard (the echo canceller again), so no alarm was set.

**Polish (late afternoon)** (each with a test; checked with the real brain and live on the robot):

* Shorter answers. With the old "one to three short sentences" the brain averaged 60 words (92 at most; the breaker
  answer took 34 s). The prompt now asks for one or two sentences of about 15 words (three for a how-to, a
  comparison or a week's weather), the card holding the details: 36-41 words on average over 3 runs of 12
  questions. Live, the breaker answer took 17 s and the crocodile one two sentences.
* One confirmation per action. "Got it, I'll remember that... Done, I've saved..." now says it once: the tool's
  result tells the brain what was already said, and a second "Done, I've..." is dropped anyway.
* Its plan said aloud. With shorter answers, the breaker question said things like "Lead with the answer briefly, no
  tools needed." or "Safety: mention checking for water". A sentence with three or more words about answering
  (answer, tools, card, briefly, needed...) isn't said, nor "I'll put the comparison on the screen for you". Checked
  on all 232 different sentences in the robot's logs: only those two card sentences were caught.
* Comparison cards fit: the label column is as wide as its labels (it was 150 px), a cell may take three lines and
  shows "..." where it is cut, and a page holds as many rows as fit. "Lower teeth show when mouth is closed" is
  whole now, and three rows fit on one page.
* Like on main, no card the brain sends is turned away for its shape: a string, a list, steps as one string, a
  field under a near name ("meaning"), text under a made-up name, or a card too long is still shown (its text as a
  facts card when the card itself can't be made). Only the safety rules, or a card with no text at all, keep one
  off the screen.

**Still open:**

* Cartesia credits (above).
* Spotify can't play yet: the robot isn't a Spotify speaker. Run
  `ssh -t alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh"` (it asks for your password),
  then pick **LittleBot** once in Spotify on your phone.
* The test logging of words heard and said (`assistant.log_words`) was on for the tests and is off again.

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

## Added in the afternoon (your follow-up)

**Spotify is the default for music** (docs/SPOTIFY.md). Needs **Spotify Premium**. The robot becomes a Spotify Connect
speaker called "LittleBot" (also castable from your phone); "play some jazz", "play my liked songs", "next", "pause",
"what's playing" go to Spotify, "play X on YouTube" still uses YouTube, and without Spotify set up it's YouTube as
before. Music turns down while you talk to LittleBot and comes back after; it's audio only, so the face stays on
screen with a small now-playing card. Set-up, once:

1. Make the robot a Spotify speaker (asks for your password once):
   `ssh -t alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh"`
2. On your phone (same Wi-Fi), open Spotify, tap the speaker icon, pick **LittleBot** once.
3. At developer.spotify.com/dashboard: create an app (tick Web API), add the Redirect URI
   `http://127.0.0.1:8888/callback` exactly, copy the Client ID.
4. Dashboard → Assistant → Spotify: paste the Client ID, press **Connect Spotify**, approve; the tab then lands on a
   127.0.0.1 page that doesn't load (expected): copy that whole address, paste it in the box, press
   **Finish connecting**.
5. `ssh alisial3@littlebot.local "systemctl --user restart littlebot"`, then check:
   `ssh alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh --check"`

Spotify no longer lets new apps open its personalised playlists (Discover Weekly, Daily Mix): LittleBot says so;
your own playlists and liked songs work.

**Better voice and face recognition** (docs/ASSISTANT.md, "Who is talking"): who is talking is tracked over the
whole conversation (a short "yes" no longer loses you), voice and face are put together, "I'm Ali" teaches it your
voice, sure turns sharpen your voiceprint, blurred or turned-away faces are no longer compared, "do you know who I
am?" takes a fresh look, and Profile → Who's who → **Who's talking?** shows who it thinks is talking and how close.
Voice recognition needs `sherpa-onnx` and its model on the robot (once):
`ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/pip install -q sherpa-onnx && .venv/bin/python scripts/download_models.py voices && systemctl --user restart littlebot"`,
then Privacy → "Recognise family voices", and record each grown-up's voice under Who's who.

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

## Games

The toddler touch games are all still there (long-press the face). New, for grown-ups and the family, played by
voice with a card on the screen (tap an answer on the card, or say it): **trivia** (10 categories, three levels;
fresh questions written by Claude each round, a built-in bank of 113 when offline), **hangman** ("is there an E?",
"is it elephant?"), **20 questions** (LittleBot thinks of something; you ask yes/no questions), **riddles** and
**would you rather** (it remembers what the family picks). Answers are judged on the robot instantly, with no model
wait; "hint", "skip", "stop the game", "what's my high score in trivia?" all work. Details: docs/GAMES.md.

## Movement

docs/MOVEMENT.md. The head has pan and tilt, the arms have gestures that go with the conversation (a nod as an
answer ends, a curious tilt, a shrug, a wave hello and goodbye, pointing at the screen when a card goes up), and the
eyes lead while the head follows a face. All of it runs on the simulator (the dashboard's Motion page draws the
tilt); the real servos stay off until you arm them as before. A drive base for the wheels is ready behind
`motion.drive.enabled: false`.

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
