#!/usr/bin/env python3
"""Talk to the assistant's brain by typing, and measure how fast it and the voices are (docs/ASSISTANT.md).

    .venv/bin/python scripts/try_assistant.py                       # type questions; Ctrl+D to stop
    .venv/bin/python scripts/try_assistant.py --ask what is the capital of Peru
    .venv/bin/python scripts/try_assistant.py --measure             # 6 everyday questions: brain + voice timings
    .venv/bin/python scripts/try_assistant.py --voices              # each configured voice: time to first audio

It uses the robot's own config and keys (never printed) with a separate in-memory database, so nothing it does
is kept, and it doesn't need LittleBot to be stopped. Tools that need the robot's hardware (the speaker, videos)
answer "not available" here; the weather, time, web search, memory and lists work. Each answer prints its
sentences as they arrive, with the time from the question to the first words.
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

QUESTIONS = [
    "what's the weather like tomorrow?",
    "how many grams in a cup of flour?",
    "give me a fun fact about octopuses",
    "what's 15 percent of 84 dollars?",
    "remind me in 20 minutes to check the oven",
    "who won the last world cup?",
]


def build_context() -> object:
    from littlebot.core.config import load_config
    from littlebot.core.context import build_core_context
    from littlebot.hardware.system import MockSystemMonitor
    from littlebot.skills.hub import SkillHub

    cfg = load_config(include_local=True)
    ctx = build_core_context(cfg, db_path=":memory:", monitor=MockSystemMonitor())
    ctx.privacy.set(cloud_enabled=True)
    ctx.skills = SkillHub(ctx)
    return ctx


async def ask(brain: object, ctx: object, history: list[dict[str, str]], question: str) -> dict[str, object]:
    from littlebot.chatgpt.tools import RobotToolbox

    t0 = time.monotonic()
    first: list[float] = []

    def said(s: object) -> None:
        if not first:
            first.append(time.monotonic() - t0)
        tag = f"[{s.expression}] " if s.expression else ""
        print(f"  {time.monotonic() - t0:5.2f}s  {tag}{s.text}", flush=True)

    box = RobotToolbox(ctx, mode="chat", defer=True)
    res = await brain.respond(question, history=history, toolbox=box, on_sentence=said,
                              on_tool=lambda name: print(f"  {time.monotonic() - t0:5.2f}s  (tool: {name})", flush=True))
    total = time.monotonic() - t0
    if res.error:
        print(f"  error: {res.error}")
    print(f"  -- {res.model or res.provider}: first words {first[0]:.2f}s, all {total:.2f}s" if first
          else f"  -- {res.model or res.provider}: no words ({total:.2f}s)")
    if res.text:
        history += [{"role": "user", "content": question}, {"role": "assistant", "content": res.text}]
    return {"first": first[0] if first else None, "total": total, "error": res.error}


async def voices(ctx: object) -> None:
    from littlebot.assistant import tts as tts_mod

    line = "Sure! It'll be about twelve degrees and sunny tomorrow."
    for name in ctx.config.assistant.tts:
        ctx.config.assistant.tts = [name]
        chain = tts_mod.build_tts_chain(ctx)
        why = chain.missing()
        if why:
            print(f"  {name:11s} not set up ({why})")
            continue
        times = []
        for _ in range(3):
            t0 = time.monotonic()
            got = None
            try:
                async for chunk in chain.stream(line):
                    if got is None:
                        got = time.monotonic() - t0
            except Exception as e:  # noqa: BLE001
                print(f"  {name:11s} failed ({getattr(e, 'kind', type(e).__name__)})")
                break
            if got is not None:
                times.append(got)
        if times:
            print(f"  {name:11s} first audio: median {statistics.median(times):.2f}s (of {len(times)})")
        await chain.aclose()


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ask", nargs="+", help="one question, then stop")
    p.add_argument("--measure", action="store_true", help="time six everyday questions")
    p.add_argument("--voices", action="store_true", help="time each configured voice")
    args = p.parse_args(argv)
    ctx = build_context()
    from littlebot.assistant.agent import Brain

    brain = Brain(ctx)
    history: list[dict[str, str]] = []
    try:
        if args.voices:
            await voices(ctx)
            return 0
        if args.ask:
            await ask(brain, ctx, history, " ".join(args.ask))
            return 0
        if args.measure:
            await brain.warm()
            firsts = []
            for q in QUESTIONS:
                print(f"\n> {q}")
                r = await ask(brain, ctx, [], q)
                if r["first"] is not None:
                    firsts.append(r["first"])
            if firsts:
                print(f"\nfirst words: median {statistics.median(firsts):.2f}s, worst {max(firsts):.2f}s "
                      f"({len(firsts)} of {len(QUESTIONS)} answered)")
            return 0
        print("Type a question (Ctrl+D to stop).")
        while True:
            try:
                q = await asyncio.to_thread(input, "> ")
            except EOFError:
                print()
                return 0
            if q.strip():
                await ask(brain, ctx, history, q.strip())
    finally:
        await brain.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
