#!/usr/bin/env python3
"""Render a sample of every info card type to PNGs, with LittleBot's presenter eyes, without a window.

    .venv/bin/python scripts/screen_preview.py                      # every kind -> ./screen_previews/
    .venv/bin/python scripts/screen_preview.py --out /tmp/cards --kind weather
    .venv/bin/python scripts/screen_preview.py --mouth 0.8 --seconds 2

Sample specs come from the card types themselves: a module-level ``SAMPLES`` list in the card's module
(each a spec with ``"card": kind``), or a ``samples`` attribute on its CardType. Kinds without any fall
back to a small built-in dict below; kinds with no sample at all are skipped. Cards are built with a
RecordingHost on a test context (no robot, no pictures fetched; a card that needs the internet for its
sample may come out empty and is reported as skipped). See docs/SCREENS.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from littlebot.screens import base, preview  # noqa: E402

# Used only for kinds whose module has no SAMPLES of its own.
BUILTIN_SAMPLES: dict[str, list[dict[str, Any]]] = {
    "timer": [{"card": "timer", "title": "Pasta", "seconds": 600}],
    "maths": [{"card": "maths", "expression": "12 x 3", "answer": "36"}],
    "definition": [{"card": "definition", "word": "gentle", "meaning": "Kind and soft, not rough",
                    "example": "Be gentle with the kitty."}],
    "explain": [{"card": "explain", "title": "How rainbows form",
                 "points": ["Sunlight enters a raindrop", "The light bends and splits", "Each colour comes out apart"]}],
    "recipe": [{"card": "recipe", "title": "Pancakes", "steps": ["Mix flour, egg and milk", "Heat a pan",
                                                                 "Pour a little batter", "Flip when it bubbles"]}],
    "facts": [{"card": "facts", "title": "Red pandas", "facts": ["They live in the mountains", "They love bamboo",
                                                                 "They sleep in trees"]}],
}


def samples_for(ct: base.CardType) -> list[dict[str, Any]]:
    own = getattr(ct, "samples", None)
    if not own:
        mod = sys.modules.get(getattr(ct.build, "__module__", "") or "")
        own = getattr(mod, "SAMPLES", None)
    found = [dict(s) for s in (own or []) if isinstance(s, dict) and s.get("card", ct.kind) == ct.kind]
    return found or [dict(s) for s in BUILTIN_SAMPLES.get(ct.kind, [])]


async def render_all(out: Path, kinds: list[str] | None, *, seconds: float, mouth: float) -> int:
    from littlebot.testing import make_test_context  # noqa: PLC0415

    types = base.card_types()
    wanted = kinds or sorted(types)
    unknown = [k for k in wanted if k not in types]
    for k in unknown:
        print(f"{k}: no such card type (known: {', '.join(sorted(types)) or 'none'})")
    ctx = make_test_context()
    written = 0
    try:
        for kind in wanted:
            if kind not in types:
                continue
            samples = samples_for(types[kind])
            if not samples:
                print(f"{kind}: no sample spec, skipped")
                continue
            for i, spec in enumerate(samples, 1):
                spec.setdefault("card", kind)
                card = await base.build(spec, ctx, base.RecordingHost())
                if card is None:
                    print(f"{kind} #{i}: nothing to show for this sample, skipped")
                    continue
                path = preview.render(card, out / f"{kind}_{i}.png", seconds=seconds, mouth=mouth)
                print(f"{kind} #{i}: {path}  ({card.describe()})")
                written += 1
    finally:
        ctx.db.close()
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="screen_previews", help="folder for the PNGs (default: ./screen_previews)")
    ap.add_argument("--kind", action="append", help="only this card kind (repeatable)")
    ap.add_argument("--seconds", type=float, default=1.5, help="how long each card has been up (animations)")
    ap.add_argument("--mouth", type=float, default=0.0, help="the presenter's mouth, 0 closed .. 1 wide open")
    args = ap.parse_args(argv)
    written = asyncio.run(render_all(Path(args.out), args.kind, seconds=args.seconds, mouth=args.mouth))
    print(f"{written} preview(s) written to {Path(args.out).resolve()}")
    return 0 if written or args.kind is None else 1


if __name__ == "__main__":
    sys.exit(main())
