# Good morning: what changed overnight

(Draft while the work is still going; the final version replaces this.)

Everything is on the `rethink` branch. `main` (what the robot runs now) is untouched.

## In one paragraph

LittleBot now has a new, much faster voice pipeline (the "assistant") that replaces GPT-Live's hand-off
patchwork with one brain per question, plus a set of new abilities ("skills") that work with either voice:
memory, reminders and alarms, lists, briefings and news, smart home, routines, music, translator, personality
styles, and new voice games for grown-ups. Until you add two new keys, the wake word still starts GPT-Live,
exactly as today, but the new skills already work through it.

Details: docs/ASSISTANT.md, docs/ASSISTANT_SPEECH.md.
