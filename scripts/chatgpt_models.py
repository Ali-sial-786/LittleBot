#!/usr/bin/env python3
"""List the text models the robot's ChatGPT sign-in can use (the Codex model list), and what LittleBot uses now.

    .venv/bin/python scripts/chatgpt_models.py
    .venv/bin/python scripts/chatgpt_models.py --json     # the list as JSON (names and labels only)

Asks chatgpt.com/backend-api/codex/models with the robot's sign-in (no model is run, nothing is charged).
Prints only model names and their labels: never the sign-in's tokens or account details. Pick a model from
the list for ``chatgpt.handoff_models`` in config/local.yaml (docs/CHATGPT.md "Trying a faster model").
The live voice's models aren't in this list (the voice uses chatgpt.live_model).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SAFE = ("slug", "id", "display_name", "description", "default_reasoning_level", "visibility", "supported_in_api",
        "priority", "context_window")


def build_context(mode: str) -> Any:
    from littlebot.chatgpt import get_auth
    from littlebot.core.config import load_config
    from littlebot.core.context import build_core_context
    from littlebot.hardware.system import MockSystemMonitor

    ctx = build_core_context(load_config(mode=mode), monitor=MockSystemMonitor())
    get_auth(ctx)
    return ctx


def models_in(body: Any) -> list[dict[str, Any]]:
    """The models in the answer, each with only the labels in SAFE (and its reasoning levels' names)."""
    items = (body.get("models") or body.get("data") or []) if isinstance(body, dict) else body
    out = []
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict):
            continue
        row = {k: m[k] for k in SAFE if k in m and isinstance(m[k], (str, int, float, bool))}
        levels = m.get("supported_reasoning_levels")
        if isinstance(levels, list):
            row["reasoning_levels"] = [str(x.get("effort") if isinstance(x, dict) else x) for x in levels]
        if row.get("slug") or row.get("id"):
            out.append(row)
    return out


async def fetch(ctx: Any, transport: Any = None) -> tuple[int, list[dict[str, Any]]]:
    import httpx

    from littlebot import __version__
    from littlebot.chatgpt import get_auth
    from littlebot.chatgpt.auth import CODEX_BASE

    auth = get_auth(ctx)
    bearer = await auth.bearer()
    async with httpx.AsyncClient(transport=transport, timeout=15.0) as client:
        r = await client.get(f"{CODEX_BASE}/models", params={"client_version": __version__},
                             headers=auth.headers(bearer))
    if r.status_code != 200:
        return r.status_code, []
    try:
        return 200, models_in(r.json())
    except ValueError:
        return 200, []


async def run(args: argparse.Namespace, ctx: Any = None, transport: Any = None) -> int:
    from littlebot.brain.text_model import cloud_block
    from littlebot.chatgpt.auth import ChatGPTAuthError

    ctx = ctx or build_context(args.mode)
    cfg = ctx.config.chatgpt
    print("LittleBot uses now (config):")
    print(f"  text models, in order:  {', '.join(cfg.models)} (reasoning effort {cfg.reasoning_effort})")
    print(f"  hand-offs try first:    {', '.join(cfg.handoff_models) or '(nothing extra: the text models)'}")
    print(f"  camera looks and cards: {', '.join(cfg.look_models) or '(the text models)'}")
    print(f"  live voice:             {cfg.live_model}, voice {cfg.live_voice}"
          f"{' (then ' + ', '.join(cfg.live_voice_fallbacks) + ')' if cfg.live_voice_fallbacks else ''}")
    print(f"  text.provider:          {ctx.config.text.provider}")
    blocked = cloud_block(ctx)
    if blocked:
        print(f"\nNot asking chatgpt.com: {blocked} (offline mode, or Cloud communication is off on the Privacy page).")
        return 2
    try:
        status, models = await fetch(ctx, transport)
    except ChatGPTAuthError as e:
        print(f"\nNot signed in to ChatGPT ({e}): sign in on the dashboard's ChatGPT page first.")
        return 2
    if status != 200:
        print(f"\nchatgpt.com answered {status} for the model list (the endpoint may have changed).")
        return 1
    if args.json:
        print(json.dumps(models, indent=2))
        return 0
    print(f"\nModels this sign-in offers ({len(models)}):")
    for m in models:
        name = m.get("slug") or m.get("id")
        bits = [m["display_name"]] if m.get("display_name") and m["display_name"] != name else []
        if m.get("reasoning_levels"):
            bits.append("effort " + "/".join(m["reasoning_levels"]))
        if m.get("visibility"):
            bits.append(str(m["visibility"]))
        used = " <- used now" if name in cfg.models or name in cfg.handoff_models else ""
        print(f"  {name}{'  (' + ', '.join(bits) + ')' if bits else ''}{used}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print the list as JSON")
    ap.add_argument("--mode", default="pi", help="config mode (default pi)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
