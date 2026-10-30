# Overnight rethink: progress

Heartbeat: 2026-10-30T04:37:51-04:00
Status: IN PROGRESS (cloud session building PLAN.md; an hourly task resumes it in this session).

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

## Next
- Merge the five worktree branches as they finish; integrate (persona prompt, voice games in the session,
  routines); GPT-Live instructions for the new skills; docs/ASSISTANT.md; MORNING.md.
