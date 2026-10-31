# Testing the rethink on the robot from the laptop

The cloud session can't reach the robot. A Claude Code session on the laptop next to it can: it has SSH to the
robot and the laptop's speaker and microphone beside it. Paste the prompt below into Claude Code on the laptop.

```
You are continuing work on LittleBot (GitHub: Ali-sial-786/LittleBot), a Raspberry Pi 5 voice robot, on the branch
`rethink`. The robot is on the network as alisial3@littlebot.local and this computer already has SSH to it. The
robot is switched on, right next to this computer, whose speaker and microphone are on. Ali (the dad, the main user)
is asleep: don't ask anything, make every decision yourself.

Rules (from Ali, still in force):
- Work only on `rethink`. NEVER merge into or push to `main` (the robot normally pulls main; Ali switches it). Never
  force-push. Commit small and often, with clear messages.
- Never print or write API keys or tokens (they live on the robot in data/*_api_key and ~/LittleBot/.env).
- The full test suite must pass on the branch (tests/audio/test_audio_scripts.py::test_check_audio_help_and_list and
  tests/test_deploy_scripts.py::test_install_live_voice_extra[default] fail only in the cloud container).
- Keep docs in plain words. Read docs/ASSISTANT.md (the new voice pipeline) and docs/rethink/MORNING.md first.

Where the code is: edit in a local clone of the repo on this computer (clone it and check out `rethink` if there
isn't one), push to `rethink`, then on the robot:
  ssh alisial3@littlebot.local "cd ~/LittleBot && git fetch -q && git checkout -q rethink && git pull -q && systemctl --user restart littlebot"
If the tests don't run here (Windows), run them on the robot after pulling:
  ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python -m pytest -q -n 4 -p no:cacheprovider"

How to test on the real robot:
1. The brain alone, with the robot's real keys and tools (prints each sentence, the tools used, the timings):
   ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/try_assistant.py --ask who was Kublai Khan"
2. The whole voice path: say the question out of this computer's speaker so the robot hears it, e.g. in PowerShell:
   Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate = -1; $s.Speak("Hey LittleBot. Who was Kublai Khan?")
   (pause a second after "Hey LittleBot" if the wake word misses it; try another installed voice with
   $s.GetInstalledVoices() / $s.SelectVoice(...) if it never wakes). Then read what the robot did:
   ssh alisial3@littlebot.local "tail -n 400 ~/LittleBot/data/logs/littlebot.log | grep -a -E 'littlebot.(assistant|screens|skills|info|chatgpt.live|audio)' | tail -40"
   The log never has the words; it has one "assistant turn:" line per answer (first_audio_ms, tools, tts, mouth_frames),
   "screens card ... shown" for cards, barge-in and stop lines, and errors. Use try_assistant.py for the words.
3. Whether it is heard: the echo canceller must play on the USB speaker (UACDemoV1.0), not the ReSpeaker Lite:
   ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python -m littlebot.audio.ec_route"

What Ali reported on the robot on 2026-10-31, and what was changed for it (all pushed to rethink; check each one live,
and fix what still isn't right):
- Knowledge questions ("Who was Kublai Khan?"): it paused first (a web search), once said it didn't know, said
  "Genga Khan" for Genghis Khan, and showed no card. Now: the brain answers known facts at once from its own knowledge,
  searches rather than say it doesn't know, assistant.pronunciations respells Genghis; a card for knowledge topics:
  see the latest commits (if no card goes up for "who was Kublai Khan?", find out why: littlebot/assistant/agent.py
  SYSTEM prompt and show_on_screen, littlebot/screens/cards/, and compare with how main's screen director chose cards).
- "What's the weather tomorrow?" gave the evening briefing with news: get_briefing no longer claims weather questions.
- "It doesn't have an hourly forecast": get_weather now says it has the next hours and takes hourly=true.
- Stop: "shh" was ignored, and "stop talking" made it say it was stopping. Now any stop ("stop", "shh", "stop talking",
  "Hey LittleBot, stop talking", "end the call", "that's all") ends the call without a word; "wait"/"hold on" pauses
  and listens.
- It paused 2.5 s in the middle of answers: its own echo taken for someone talking over it. Now an echo is a false
  alarm after 0.6 s (log: "false alarm (quiet as soon as the voice stopped: its own echo").
- A second voice in the background: Cartesia refused (2 requests at once on the free plan) and OpenAI's voice took
  over. Now one Cartesia request at a time and a busy answer is retried; turn lines should say tts=cartesia only.
- Silent answers right after cutting in ([silent]): the brain is now told those words are for it.
- No sound at all: the echo canceller played into the ReSpeaker Lite's headphone socket; LittleBot now moves it back
  to the USB speaker by itself (log: "audio: the echo canceller plays on ...: moved it back to the speaker").
- Voice: Cartesia "Tessa", calm, normal speed; Ali likes it. Keep it.

Then keep improving it as the best fast, warm voice assistant: test many everyday questions (weather today/tomorrow/
hourly, timers, reminders, lists, maths, facts, news, music on Spotify, YouTube, games, stop/wait/volume, barge-in),
fix what goes wrong, add a test for each fix (tests/assistant, tests/chatgpt, tests/skills ...), and keep the
first_audio_ms in the "assistant turn:" lines low. Write what you did, what you measured and what's still open in
docs/rethink/MORNING.md (a short "Laptop session" section, plain words) and push.
```
