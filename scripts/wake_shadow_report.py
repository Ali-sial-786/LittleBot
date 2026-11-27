#!/usr/bin/env python3
"""What LittleBot's own wake word model would have done next to Vosk, from data/wake/shadow.jsonl.

    .venv/bin/python scripts/wake_shadow_report.py
    .venv/bin/python scripts/wake_shadow_report.py --since 2026-11-02     # only from that day on
    .venv/bin/python scripts/wake_shadow_report.py path/to/shadow.jsonl

The file is written while ``wake.oww_mode`` is shadow (littlebot/audio/oww.py, docs/WAKE_TRAINING.md): one line
for each time Vosk woke LittleBot (Vosk's word confidence and the model's score), how the chat went after it, the
model's own high scores when Vosk didn't wake, and how long it listened. Numbers only: no sound, no words.

It prints how many Vosk wakes there were, how many the model agreed with at a few thresholds, its own peaks per
hour, and which threshold (``wake.oww_threshold``, for ``wake.oww_mode: confirm``) would have kept the real requests
and dropped the false wakes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT = REPO_ROOT / "data" / "wake" / "shadow.jsonl"
THRESHOLDS = (0.1, 0.3, 0.5, 0.7, 0.9)
CHOICES = tuple(round(0.05 * i, 2) for i in range(1, 20))  # thresholds tried for the suggestion
FALSE = ("no_words", "dismissed")  # a wake after which nobody asked anything: most likely a false wake
ENOUGH = 20  # fewer wakes than this with an outcome: too few to choose a threshold


def read(paths: Iterable[Path], since: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and (since is None or str(rec.get("t", "")) >= since):
                    rows.append(rec)
    rows.sort(key=lambda r: str(r.get("t", "")))
    return rows


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.0f}%" if d else "-"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def report(rows: list[dict[str, Any]]) -> str:
    out: list[str] = []
    say = out.append
    wakes = [r for r in rows if r.get("kind") == "vosk_wake" and not r.get("blocked")]
    blocked = [r for r in rows if r.get("kind") == "vosk_wake" and r.get("blocked")]
    peaks = [r for r in rows if r.get("kind") == "model_only"]
    outcomes = {r.get("id"): r for r in rows if r.get("kind") == "outcome"}
    hours = sum(float(r.get("sound_s") or 0.0) for r in rows if r.get("kind") == "listened") / 3600.0
    if not rows:
        return "Nothing recorded yet. Is wake.oww_mode shadow, and has LittleBot been listening for a while?\n"
    say(f"From {rows[0].get('t')} to {rows[-1].get('t')}; the model listened for {hours:.1f} hours"
        + (" (the last hour is counted once it's over)" if hours < 1 else "") + ".")
    say("")
    say(f"Vosk woke LittleBot {_plural(len(wakes), 'time')}.")
    if blocked:
        say(f"Confirm mode stopped {_plural(len(blocked), 'more wake')} (the model's score was too low).")
    kinds: dict[str, int] = {}
    for w in wakes:
        o = outcomes.get(w.get("id"), {}).get("outcome", "unknown")
        kinds[o] = kinds.get(o, 0) + 1
    names = {"request": "a real request", "dismissed": "dismissed at once", "no_words": "nobody said anything",
             "not_connected": "the chat didn't connect", "not_started": "the chat couldn't start",
             "unknown": "not known (LittleBot restarted, or the chat is still going)"}
    for k in ("request", "dismissed", "no_words", "not_connected", "not_started", "unknown"):
        if kinds.get(k):
            say(f"  after {kinds[k]}: {names[k]}")
    if wakes:
        say("")
        say("How many of them the model agreed with (its score at the moment Vosk woke it / up to 0.6 s later):")
        for th in THRESHOLDS:
            at = sum(1 for w in wakes if float(w.get("model_score", 0)) >= th)
            after = sum(1 for w in wakes if float(w.get("model_score_after", 0)) >= th)
            say(f"  at {th:.1f}: {at} of {len(wakes)} ({_pct(at, len(wakes))}) / {after} ({_pct(after, len(wakes))})")
    say("")
    unsure = sum(1 for p in peaks if p.get("vosk_unsure"))
    rate = f", {len(peaks) / hours:.1f} an hour" if hours > 0 else ""
    say(f"The model's own peaks with no Vosk wake: {len(peaks)}{rate}"
        + (f"; at {unsure} of them Vosk heard a wake phrase but wasn't sure enough (perhaps a missed wake)"
           if unsure else "") + ".")
    if peaks:
        for th in THRESHOLDS:
            n = sum(1 for p in peaks if float(p.get("model_score", 0)) >= th)
            if n:
                say(f"  {n} at {th:.1f} or more" + (f" ({n / hours:.1f} an hour)" if hours > 0 else ""))
    say("")
    say(suggest(wakes, outcomes))
    return "\n".join(out) + "\n"


def suggest(wakes: list[dict[str, Any]], outcomes: dict[Any, dict[str, Any]]) -> str:
    """Which wake.oww_threshold would have kept the real requests and dropped the false wakes (the score at the
    wake: what confirm mode sees)."""
    known = [(w, outcomes[w.get("id")]["outcome"]) for w in wakes if w.get("id") in outcomes]
    real = [float(w.get("model_score", 0)) for w, o in known if o == "request"]
    false = [float(w.get("model_score", 0)) for w, o in known if o in FALSE]
    lines = ["Which threshold for confirm mode (wake.oww_threshold), by the score at the wake:"]
    if not real and not false:
        lines.append("  no wakes with a known outcome yet.")
        return "\n".join(lines)
    for th in (0.3, 0.5, 0.7):
        kept = sum(1 for s in real if s >= th)
        dropped = sum(1 for s in false if s < th)
        lines.append(f"  {th:.1f}: keeps {kept} of {len(real)} real requests, drops {dropped} of {len(false)} "
                     "false wakes")
    keeps_all = [th for th in CHOICES if all(s >= th for s in real)]
    if not keeps_all:
        lines.append("  No threshold keeps every real request.")
    else:
        # the lowest that drops the most false wakes: the most room for real requests that score a little lower
        th = max(keeps_all, key=lambda t: (sum(1 for s in false if s < t), -t))
        dropped = sum(1 for s in false if s < th)
        if not false:
            lines.append("  There were no false wakes yet, so there is nothing for confirm mode to drop.")
        elif dropped == 0:
            lines.append("  No threshold that keeps every real request would have dropped a false wake: confirm "
                         "mode wouldn't help yet.")
        else:
            lines.append(f"  Best: {th:.2f}. It keeps all {len(real)} real requests and drops {dropped} of "
                         f"{len(false)} false wakes.")
    if len(known) < ENOUGH:
        lines.append(f"  Only {len(known)} wakes with a known outcome: wait for at least {ENOUGH} before choosing.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path", nargs="?", type=Path, default=DEFAULT, help="the shadow.jsonl file")
    ap.add_argument("--since", help="only from this time on, e.g. 2026-11-02 or 2026-11-02T18:00")
    args = ap.parse_args(argv)
    path: Path = args.path
    paths = [path.with_name(path.name + ".1"), path]  # the older file first (it's moved aside past 4 MB)
    if not any(p.is_file() for p in paths):
        print(f"No {path} yet. It is written while wake.oww_mode is shadow (docs/WAKE_TRAINING.md).")
        return 1
    sys.stdout.write(report(read(paths, args.since)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
