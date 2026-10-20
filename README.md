# LittleBot 🤖

A small, gentle, expressive companion robot for a young child — animated eyes,
songs, scripted developmental play, parent-approved videos, optional AI
conversation for older children, and a phone-friendly parent dashboard.

> **LittleBot is a supervised prototype.** It is not a babysitter, not a
> child-safety device and not a substitute for a caregiver. Software tests do
> not establish physical toy safety (see `docs/MOTION_SAFETY.md` and
> `HARDWARE_ARRIVAL_CHECKLIST.md`).

*This README is updated as development progresses — see `PROJECT_STATUS.md`
for the honest, item-by-item status.*

## Quick start (desktop simulation)

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[vision,audio,dev]"
python -m littlebot                 # face window + dashboard on http://127.0.0.1:8765
```

## Documentation map

| Document | What it covers |
|---|---|
| `ARCHITECTURE.md` | Design and module interactions |
| `PROJECT_STATUS.md` | What works, what's mock-tested, what awaits hardware |
| `NEXT_STEPS.md` | Prioritised remaining work |
| `HARDWARE_ARRIVAL_CHECKLIST.md` | Step-by-step for the day the parts arrive |
| `docs/DEV_CONVENTIONS.md` | How the code is organised |
