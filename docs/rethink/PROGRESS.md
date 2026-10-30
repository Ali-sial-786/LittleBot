# Overnight rethink: progress

Heartbeat: 2026-10-30T13:41:21-04:00
Status: IN PROGRESS (follow-up asked 2026-10-30 afternoon: Spotify as default music; voice + face recognition in the assistant).

Started from main 01d9259 (tag `checkpoint-2026-10-30-before-rethink`). Read BRIEF.md first.

## Done
- Checkpoint tag + backup branch; `rethink` branch.
- ANALYSIS.md sections 1-4 (core, screens/face/games/media, research, conversation pipeline).

- PLAN.md (cloud session, 04:20 EDT). Baseline: 4,930 tests pass; 3 fail only in the cloud container (no
  PortAudio, running as root, an install-script dry run with the extras already installed).

- Skills hub (littlebot/skills) offered to both voices; config sections + new keys (Deepgram, Cartesia,
  ElevenLabs, Home Assistant).
- The assistant (littlebot/assistant): sentence streamer, instant intents, the brain (agent.py), VoiceOut,
  VoiceSession, AssistantService; wake word + dashboard Talk button route through voice_front().
- Cards: note, glance. Dashboard: Assistant page + /api/assistant.
- In parallel (subagents in worktrees, to merge): speech providers (assistant/tts, assistant/stt), skills
  memory/reminders/lists/persona, skills briefing/home/routines/music/translator, voice games, movement + body
  language.

- Merged: speech providers (+ early answers on Deepgram's eager end of turn), skills memory / reminders /
  lists / persona, skills briefing / news / home / routines / music / translator. Alarms wake the robot and are
  stopped or snoozed by voice; GPT-Live's remember goes to the memory skill; music starts after the turn.
- Dashboard: lists, reminders and memories (tick off, cancel, forget). scripts/set_key.py, try_assistant.py.

- Merged voice games (trivia, hangman, 20 questions, riddles, would you rather; played without a model in the
  assistant) and movement (head tilt, look_at, conversation gestures, body language, drive base mock).
- Independent review of the voice session: 12 findings fixed (cancellation, barge-in, announcements, long answers).
- Full suite: 5,680 pass; 2 fail only in the cloud container (also on main there).

## Next
- Follow-up: Spotify integration (builder in a worktree), voice + face recognition in the assistant's context;
  then MORNING.md update and Status: DONE again.
- Nothing else for tonight. On the robot: try it (MORNING.md), add the Deepgram and Cartesia keys, measure.
- Later: retire GPT-Live's hand-off layers (chatgpt/live.py, screens/director.py) once the assistant is trusted.
