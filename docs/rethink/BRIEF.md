# Overnight rethink: the brief

Written 2026-10-30 ~04:00 EDT for whichever session carries this on (a cloud session, or the hourly task on
the user's PC). Everything needed is in this folder: BRIEF.md (this), ANALYSIS.md (the codebase and research),
PLAN.md (the plan, a checklist), PROGRESS.md (done / next / heartbeat), MORNING.md (for the user to read).

## The goal (the user's words, condensed)

Rethink LittleBot from scratch knowing the end goal, instead of patchwork fixes, for a much better and faster
experience. The primary user is the dad (Ali), not just the child: it should be an amazing smart assistant
(think ChatGPT voice / Google Home, but better and faster) that can move around and show things on its
screen, keeping a personality, a face and expressions like now (which may be redesigned to be better). Speed
is a big priority. Any mix of GPT (incl. GPT-Live), Claude and the OpenAI Decisions API may be used. If
GPT-Live slows things down compared with fast thinking models, consider a human-sounding TTS (Chatterbox or
alternatives) instead. Keep key things like YouTube playback; the card system may be completely rethought.
Rethink and improve the games, add more. Research what comparable assistants/robots offer and add it. Add at
least 5 completely new cool features he didn't mention (no upper limit). Total decision-making control: he is
asleep, do NOT ask questions; if he doesn't like something he'll change it later.

## His answers before sleeping

- Movement: no motors yet. Arms and a pan/tilt head come first, wheels a later phase. Build movement as
  ready-for-hardware modules (with the mock/simulator), not wired to anything physical.
- No robot access tonight (it is switched off): everything is done in the repo and tested in simulation.
- New paid services are fine ("anything reasonable"); he adds API keys in the morning. Until a key is set,
  everything falls back to what works today.
- The work stays on the `rethink` branch. NEVER merge into or push to `main` (the robot pulls main); he
  switches the robot over himself.

## The robot today (from the sessions on 2026-10-29)

- Raspberry Pi 5, 4 GB, user `alisial3`, repo at `~/LittleBot`, systemd user service
  (`systemctl --user restart littlebot`), log `~/LittleBot/data/logs/littlebot.log` (JSON lines; grep -a).
  Config on the robot: `config/local.yaml` (change it with `scripts/set_config.py section.setting value`).
  Robot-specific values there today: `decide.min_confidence: 0.4`, `wake.min_confidence: 0.35`,
  `chatgpt.live_barge_in_max_pitch_hz: 220`, `audio.echo_cancel: auto`.
- Audio: ReSpeaker Lite (USB, the mic) + a Jieli UACDemo USB speaker; PipeWire software echo cancelling
  (`littlebot_ec_mic`) is needed because the ReSpeaker can't cancel a separate speaker. `echo_cancel: off`
  sends sound to the ReSpeaker's own jack (silent). No camera attached. Waveshare UPS HAT (E) on I2C 0x2D.
  3.5" HDMI touch screen, 480x320 native, shown at 720x480.
- Voice today: wake word (Vosk grammar mode, "hey little bot", min_confidence 0.35) → GPT-Live (ChatGPT's
  voice over WebRTC, signed in with the user's ChatGPT account; voice "sol") which answers itself and hands
  harder questions ("delegations") to the text model chain Claude Haiku 5.5 (thinking off) → Haiku 4.5 →
  ChatGPT. A decision model (OpenAI, `littlebot/decide`) answers small yes/no and choice questions in many
  places (cards, answered, addressed, routing, naming, concern). Cards come from `screens/director.py`.
- Keys on the robot: ChatGPT sign-in (token file: never read or print it), Anthropic key in
  `~/LittleBot/data/anthropic_api_key` (600), OpenAI key for the decision model. Never print or write keys.
- The user's voice is 107-154 Hz (the barge-in pitch rule). The child (Amara) is too young to talk.
  Location: Hamilton (Ontario). The family: Ali (dad), Amara (baby), Amara's mom.
- What the user hit on 2026-10-29: cards missing or late (fixed in steps), wake word sometimes missed,
  barge-in/stop unreliable (fixed in steps: Vosk stop words, room-talk check), GPT-Live's captions of
  cut-ins are fragments ("Under the Great" for "tell me more about Alexander the Great"), the voice misheard
  "Kublai" as "Kubra". These patch layers are what the rethink should replace with a cleaner design.
- Commands for the user: always quote-free one-liners `ssh alisial3@littlebot.local "..."` that work both in
  PowerShell on his PC and pasted into a shell already on the Pi (no quotes inside, no `$`, no editors).

## How to work

- Branch `rethink` (from main 01d9259; rollback tag `checkpoint-2026-10-30-before-rethink`). Commit small and
  often; push with `git push origin HEAD:refs/heads/rethink`. Never force-push. Commit messages end with
  "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>".
- Heartbeat: keep `Heartbeat: <ISO time>` in PROGRESS.md fresh (every commit, at least every 30 min): a
  backup task on the user's PC resumes the work only when the heartbeat on origin is over 50 minutes old.
- Keep the robot working: new paths behind config switches; the old ones kept until the new are tested; the
  full test suite passing on the branch (`python -m pytest -q`; the suite is ~2,580 tests, ~3.5 min on Linux).
- Conventions: docs/DEV_CONVENTIONS.md (ctx protocols and the bus between subsystems, never block the loop,
  leases, privacy checks, deterministic tests with fakes and no network/keys, type hints, logging names).
  Write code like the surrounding code (comment density, naming, idiom).
- Keep PLAN.md / PROGRESS.md / MORNING.md current so the next session can carry on. When it is all done and
  the tests pass, set `Status: DONE` in PROGRESS.md and push.
- MORNING.md is for the user: what changed and why, how to try it on the robot (quote-free ssh one-liners),
  which keys to add and where, what to say to try each new thing, what is still open. Plain words.
