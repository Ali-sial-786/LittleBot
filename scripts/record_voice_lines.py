#!/usr/bin/env python3
"""Record LittleBot's fixed lines in its ChatGPT voice, so activities speak with it (uses the robot's ChatGPT sign-in).

    .venv/bin/python scripts/record_voice_lines.py --dry-run     # list the lines and how long it takes; record nothing
    .venv/bin/python scripts/record_voice_lines.py               # record every line -> data/voice_lines/
    .venv/bin/python scripts/record_voice_lines.py --missing     # only lines not recorded yet (carry on after a stop)
    .venv/bin/python scripts/record_voice_lines.py --only peekaboo --only again_again   # just these activities
    .venv/bin/python scripts/record_voice_lines.py --for-age     # only the activities that suit your child now
    .venv/bin/python scripts/record_voice_lines.py --with-name   # also the lines with your child's name

The dashboard's Activities page has the same thing as a button ("Record activity voices in the
ChatGPT voice"), with a progress bar.

What gets recorded: every fixed line the activities say (with your child's nickname and favourites
from the Child profile filled in, and one line per animal, colour... of the lists they use), what the
screen games say, and the scripted chat replies and greetings. Lines that can't be known in advance
(an AI's answer, what your child said) keep the robot's text-to-speech voice.

How: one GPT-Live call records many lines (calls take a few seconds to connect). For each line the
voice is asked "Say exactly, word for word, nothing else: <line>", its sound is kept until its turn
ends, and what it said (its transcript) is checked against the line; a mismatch is asked again
(--retries). Lines that never matched are listed at the end and keep the old voice. The robot's
microphone and speaker are not used: LittleBot can keep running meanwhile, and it uses each new
recording at once (no restart needed).

Your child's name is only sent to ChatGPT with your OK: lines with it are recorded when "share the
child's name with the AI" is on (AI page) or with --with-name.

Recordings: data/voice_lines/<hash>.wav plus index.json (text, hash, voice, date). Updates never
touch data/. Delete the folder to go back to the robot voice everywhere.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")


def _minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"about {m} minute{'s' if m != 1 else ''}"


def _resolve_only(names: list[str], library: Any) -> list[str]:
    """--only values as activity ids / groups: an activity's name works too ("Again! Again!")."""
    from littlebot.app.spoken_lines import GROUPS
    from littlebot.audio.voice_lines import normalise

    by_name = {normalise(a.name): a.id for a in library.all()}
    out = []
    for raw in names:
        name = raw.strip()
        if name in GROUPS or name in library:
            out.append(name)
        elif normalise(name) in by_name:
            out.append(by_name[normalise(name)])
        else:
            raise KeyError(name)
    return out


def build_context(mode: str = "pi") -> Any:
    """The robot's config, parent settings (profile, privacy) and ChatGPT sign-in, without its devices."""
    from littlebot.chatgpt import get_auth
    from littlebot.core.config import load_config
    from littlebot.core.context import build_core_context
    from littlebot.hardware.system import MockSystemMonitor

    ctx = build_core_context(load_config(mode=mode), monitor=MockSystemMonitor())
    get_auth(ctx)
    return ctx


def plan(ctx: Any, args: argparse.Namespace) -> tuple[list[Any], dict[str, Any]]:
    """The lines to record now, and what to tell the parent about them."""
    from littlebot.activities.engine import default_script_dirs
    from littlebot.activities.model import ActivityLibrary
    from littlebot.app.spoken_lines import collect
    from littlebot.audio.voice_lines import VoiceLineStore, default_folder

    library = ActivityLibrary(default_script_dirs(ctx))
    library.load()
    only = _resolve_only(args.only or [], library)
    lines = collect(ctx.config, ctx.settings, library=library, only=only or None, all_ages=args.all_ages,
                    for_age=args.for_age)
    share = bool(ctx.settings.get("ai.share_child_name", False))
    with_name = share if args.with_name is None else args.with_name
    named = [x for x in lines if x.uses_name]
    if not with_name:
        lines = [x for x in lines if not x.uses_name]
    store = VoiceLineStore(default_folder(ctx.config.data_path))
    already = store.recorded([x.text for x in lines])
    if args.missing:
        lines = [x for x in lines if x.hash not in already]
    nickname = str((ctx.settings.get("profile") or {}).get("nickname") or "").strip()
    return lines, {"named": len(named), "with_name": with_name, "already": len(already), "store": store,
                   "nickname": nickname}


async def main_async(args: argparse.Namespace, ctx: Any = None) -> int:
    from littlebot.app.spoken_lines import estimate_seconds
    from littlebot.chatgpt import get_auth
    from littlebot.chatgpt.live import aiortc_error
    from littlebot.chatgpt.voice_record import VoiceLineJob

    ctx = ctx or build_context()
    try:
        lines, info = plan(ctx, args)
    except KeyError as e:
        from littlebot.app.spoken_lines import GROUPS, activity_ids

        print(f"No activity called {e.args[0]!r}. Use an activity id, or one of: {', '.join(GROUPS)}.")
        print("Activity ids: " + ", ".join(activity_ids(ctx.config)))
        return 2
    store = info["store"]
    groups: dict[str, int] = {}
    for x in lines:
        groups[x.group] = groups.get(x.group, 0) + 1
    words = sum(len(x.text.split()) for x in lines)
    seconds = estimate_seconds(lines, args.lines_per_call)
    what = ", ".join(f"{n} {g}" for g, n in groups.items()) or "nothing"
    print(f"{len(lines)} lines to record in the ChatGPT voice ({what}; {words} words).")
    before = " and are skipped (--missing)." if args.missing else (" and are recorded again." if info["already"] else ".")
    print(f"That takes {_minutes(seconds)}: one call per {args.lines_per_call} lines. "
          f"{info['already']} of these lines were recorded before{before}")
    who = info["nickname"] or "your child"
    if info["named"] and not info["with_name"]:
        print(f"{info['named']} lines with {who}'s name are left out: the name is only sent to ChatGPT with your OK. "
              "Add --with-name to record them too (or turn on sharing the child's name on the AI page).")
    elif info["named"]:
        print(f"{info['named']} lines have {who}'s name in them; they are recorded with it.")
    if args.dry_run:
        for n, x in enumerate(lines, 1):
            print(f"  {n:4d}. [{x.source}] {x.text}")
        print("Dry run: nothing was recorded.")
        print(json.dumps({"lines": len(lines), "minutes": round(seconds / 60, 1), "dry_run": True}))
        return 0
    if not lines:
        print("Nothing to record: every line is recorded already.")
        return 0
    if ctx.config.is_offline:
        print("LittleBot is in offline mode: recording needs the internet.")
        return 1
    if not ctx.privacy.cloud_enabled:
        print("Cloud communication is turned off (dashboard > Privacy). Turn it on to record with ChatGPT.")
        return 1
    auth = get_auth(ctx)
    if not auth.signed_in:
        print("Not signed in with ChatGPT on this robot. Sign in on the dashboard's AI page first.")
        return 1
    err = aiortc_error()
    if err:
        print(f"Live voice isn't installed ({err}): .venv/bin/python -m pip install -e '.[live]'")
        return 1
    print(f"Recording into {store.folder}. LittleBot can keep running; nothing plays on its speaker. Ctrl+C stops "
          "(what is done is kept; --missing carries on).", flush=True)

    def on_line(i: int, total: int, line: Any, take: Any, saved: bool) -> None:
        mark = "ok  " if saved else "MISS"
        tries = f" (try {take.tries})" if take.tries > 1 else ""
        heard = "" if saved else f"  -> the voice said: {take.said or '(nothing)'}"
        print(f"[{i + 1:4d}/{total}] {mark} {line.text}{tries}{heard}", flush=True)

    def on_phase(phase: str) -> None:
        if phase == "connecting":
            print("Calling the GPT-Live voice...", flush=True)

    job = VoiceLineJob(store, auth, ctx.config.chatgpt, lines, retries=args.retries,
                       lines_per_call=args.lines_per_call, on_line=on_line, on_phase=on_phase)
    task = asyncio.ensure_future(job.run())
    try:
        result = await asyncio.shield(task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Stopping after this line...", flush=True)
        job.stop()
        result = await task
    print(f"Done: {result['saved']} of {result['total']} lines recorded in the voice {result['voice'] or '?'}"
          + (f" ({result['done']} asked before stopping)." if result["phase"] == "stopped" else "."))
    if result["error"]:
        print(f"Stopped early: {result['error']}")
    if job.failed:
        print(f"{len(job.failed)} lines never matched (they keep the robot voice; run again with --missing to retry):")
        for f in job.failed:
            print(f"  - {f['text']}  (the voice said: {f['said'] or 'nothing'})")
    if result["saved"]:
        print("LittleBot uses the new recordings at once (no restart needed).")
    print(json.dumps({"lines": result["total"], "saved": result["saved"], "failed": len(job.failed),
                      "phase": result["phase"]}))
    return 0 if result["phase"] in ("done", "stopped") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="list the lines and how long recording takes; record nothing")
    ap.add_argument("--only", action="append", metavar="ACTIVITY",
                    help="only this activity (id or name; repeatable), or a group: activities, games, replies, robot")
    ap.add_argument("--missing", action="store_true", help="only lines not recorded yet")
    ap.add_argument("--for-age", action="store_true", help="only activities that suit your child's age now")
    ap.add_argument("--all-ages", action="store_true", help="the scripted chat replies for every age, not just now")
    name = ap.add_mutually_exclusive_group()
    name.add_argument("--with-name", dest="with_name", action="store_true", default=None,
                      help="also record the lines with your child's name (sends the name to ChatGPT)")
    name.add_argument("--without-name", dest="with_name", action="store_false", help="leave the lines with the name out")
    ap.add_argument("--retries", type=int, default=2, help="ask again this often when a take doesn't match (default 2)")
    ap.add_argument("--lines-per-call", type=int, default=40, help="lines recorded per call (default 40)")
    try:
        return asyncio.run(main_async(ap.parse_args()))
    except KeyboardInterrupt:
        print("Stopped. What was recorded is kept; run again with --missing to carry on.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
