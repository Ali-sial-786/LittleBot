#!/usr/bin/env python3
"""Compare the speech-to-text services on the same speech (``assistant.stt_shadow``).

While ``assistant.stt_shadow`` names other services (cartesia, assemblyai), every chat's sound goes to each, and the
log gets a "assistant stt: compare <name> '<words>'" line from each for every turn (only with ``assistant.log_words``
on). This pairs them up by time and shows where they heard different words:

    .venv/bin/python scripts/stt_compare.py            # today's and the older logs in data/logs
    .venv/bin/python scripts/stt_compare.py --all      # also the turns where both heard the same

Nothing is sent anywhere: it only reads the robot's own log files.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINE = re.compile(r"^assistant stt: compare (\w+) (.*)$")
PAIR_S = 4.0  # two services' ends of the same turn come this close together


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^\w' ]+", " ", text.lower()).split())


def read(paths: list[Path]) -> list[tuple[dt.datetime, str, str]]:
    out: list[tuple[dt.datetime, str, str]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            if "compare" not in raw:
                continue
            try:
                rec = json.loads(raw)
                m = LINE.match(str(rec.get("msg", "")))
                if not m:
                    continue
                said = ast.literal_eval(m.group(2))
                out.append((dt.datetime.fromisoformat(rec["ts"]), m.group(1), str(said)))
            except (ValueError, KeyError, SyntaxError, TypeError):
                continue
    return sorted(out)


def pair(rows: list[tuple[dt.datetime, str, str]]) -> list[tuple[dt.datetime, dict[str, str]]]:
    turns: list[tuple[dt.datetime, dict[str, str]]] = []
    for ts, name, said in rows:
        last = turns[-1] if turns else None
        if last is not None and name not in last[1] and (ts - last[0]).total_seconds() <= PAIR_S:
            last[1][name] = said
        else:
            turns.append((ts, {name: said}))
    return turns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true", help="also show the turns both heard the same")
    ap.add_argument("--logs", default=str(ROOT / "data" / "logs"), help="the log folder")
    args = ap.parse_args()
    paths = sorted(Path(args.logs).glob("littlebot.log*"), reverse=True)
    rows = read(paths)
    if not rows:
        print("No comparison lines yet: set assistant.stt_shadow (and assistant.log_words: true), then talk to it.")
        return 1
    turns = pair(rows)
    names = sorted({name for _, name, _ in rows})
    same = differ = alone = 0
    for ts, heard in turns:
        if len(heard) < len(names):
            alone += 1
            kind = "only one heard it"
        elif len({_words(v) for v in heard.values()}) == 1:
            same += 1
            if not args.all:
                continue
            kind = "the same"
        else:
            differ += 1
            kind = "different"
        print(f"{ts:%m-%d %H:%M:%S}  {kind}")
        for name in names:
            print(f"    {name:9} {heard.get(name, '(nothing)')!r}")
    print(f"\n{len(turns)} turns: {same} heard the same, {differ} different, {alone} heard by one only "
          f"({', '.join(names)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
