#!/usr/bin/env python3
"""Time ChatGPT and Claude on the same typical requests LittleBot makes during a voice chat.

    .venv/bin/python scripts/compare_text_models.py              # each request twice per model (about 10 calls)
    .venv/bin/python scripts/compare_text_models.py --rounds 1   # once each
    .venv/bin/python scripts/compare_text_models.py --only claude

The requests are built the way the robot builds them (littlebot/screens/director.py, littlebot/chatgpt/live.py):

* card:     the screen director's card decision for "what is the tallest building in the world"
* hand-off: a light hand-off ("why is the sky blue", LittleBot's tools offered, no web search)
* decider:  the hand-off routing question (littlebot/decide/uses.py), with Claude and with the decision
            provider that has a key (Jev or OpenAI); ChatGPT itself isn't a decision model in LittleBot

Nothing is done on the robot: a tool the model asks for is answered with "(only a test: nothing was done)".
ChatGPT uses your sign-in (no charge per call); Claude uses ANTHROPIC_API_KEY (each request here is a few
thousand tokens in and under 200 out: well under a cent for the whole run; the script prints its estimate).
Needs Cloud communication on (Privacy page) and not offline mode, as LittleBot itself does.

Prints each call's time to the first word and total time (from the "text model:" log line), then the medians.
The first call to each model also opens its connection; --rounds 2 or more shows the warm numbers too.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Prices per million tokens (input, cached input, output); docs/CLAUDE.md has the current list.
PRICES = {"claude-haiku-5-5": (0.10, 0.01, 0.50), "claude-haiku-4-5": (1.00, 0.10, 5.00)}
CARD_HEARD = "What is the tallest building in the world?"
HANDOFF_ASK = "Why is the sky blue?"
ROUTE_ASK = "What's the weather like in Leeds this afternoon?"
NOT_DONE = "(only a test: nothing was done)"

# --from-log: the requests behind the slow ChatGPT calls in the robot's log of 2026-10-26, rebuilt. The log keeps
# only timings and card titles (never what was said), so the words are a close guess from those titles.
# chatgpt_ms: (total, first sentence) as logged; a card's time is the director's, from the words being heard.
_KUBLAI = "Who was Kublai Khan?"
_KUBLAI_SAID = "Kublai Khan was Genghis Khan's grandson and the first emperor of China's Yuan dynasty."
_VERSUS = "Who was the greater conqueror, Genghis Khan or Alexander the Great? Show it on the screen."
_VERSUS_SAID = "Genghis Khan built the bigger empire; Alexander conquered faster and never lost a battle."
LOG_REQUESTS = (
    {"at": "00:58:21", "kind": "card", "lines": (("heard", _KUBLAI),), "chatgpt_ms": [(17503, None)]},
    {"at": "00:59:15", "kind": "hand-off full", "ask": "Can you put some facts about Kublai Khan on the screen?",
     "history": (("heard", _KUBLAI), ("robot", _KUBLAI_SAID)), "light": False, "chatgpt_ms": [(12181, 11355)]},
    {"at": "01:14:00", "kind": "card", "lines": (("heard", _VERSUS),), "chatgpt_ms": [(26541, None)]},
    {"at": "01:14:03", "kind": "hand-off light", "ask": _VERSUS, "chatgpt_ms": [(3875, 3729)]},
    {"at": "01:15:14", "kind": "hand-off light", "ask": "Can you show the picture full screen?",
     "history": (("heard", _VERSUS), ("robot", _VERSUS_SAID)),
     "chatgpt_ms": [(2940, 2875), (3661, 3523), (7240, 7178)]},
)
_FIELD = re.compile(r"(\w+)=(\S+)")


class _Lines(logging.Handler):
    """Keeps the "text model:" lines (labels and numbers only) as dicts."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.rows: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith("text model: provider="):
            self.rows.append(dict(_FIELD.findall(msg)))


def build_context(mode: str) -> Any:
    from littlebot.chatgpt import get_auth
    from littlebot.core.config import load_config
    from littlebot.core.context import build_core_context
    from littlebot.hardware.system import MockSystemMonitor

    ctx = build_core_context(load_config(mode=mode), monitor=MockSystemMonitor())
    get_auth(ctx)
    return ctx


def providers(ctx: Any, purpose: str) -> dict[str, Any]:
    """ChatGPT with the models LittleBot uses for ``purpose``, and Claude (no fallback: each on its own)."""
    from littlebot.brain.text_model import make_claude
    from littlebot.chatgpt import get_auth
    from littlebot.chatgpt.responses import ChatGPTProvider

    cfg = ctx.config.chatgpt
    first = cfg.look_models if purpose == "screen" else cfg.handoff_models
    models = list(dict.fromkeys([*first, *cfg.models]))
    return {"chatgpt": ChatGPTProvider(get_auth(ctx), models=models, effort=cfg.reasoning_effort, purpose=purpose),
            "claude": make_claude(ctx, purpose=purpose)}


async def card(ctx: Any, provider: Any, lines: tuple[tuple[str, str], ...] = (("heard", CARD_HEARD),)) -> Any:
    """The director's own request (prompt, conversation, effort) for what was heard (and said)."""
    from littlebot.screens.director import ScreenDirector

    d = ScreenDirector(ctx, lambda: provider, note=lambda _t: None, spawn=lambda c: c.close())
    d._lines.extend(lines)  # what heard() / said() record, without starting a decision of their own
    return await d._ask()


async def handoff(ctx: Any, provider: Any, ask: str = HANDOFF_ASK, *, light: bool = True,
                  history: tuple[tuple[str, str], ...] = ()) -> Any:
    """A hand-off as live.py asks it: the same system prompt, tools and effort (light: minimal effort and no
    web search; full: the configured effort, with web search)."""
    from littlebot.brain import identity
    from littlebot.brain.prompts import build_system_prompt
    from littlebot.chatgpt import live
    from littlebot.chatgpt.tools import RobotToolbox

    box = RobotToolbox(ctx, mode="live", defer=True)
    system = build_system_prompt(robot_name=ctx.config.robot_name, age_months=ctx.age_policy().age_months,
                                 max_words=live.LOOKUP_MAX_WORDS, tools=True,
                                 about=identity.about(ctx.config)) + live.LOOKUP_NOTE + live.LIVE_TOOLS_NOTE \
        + box.instructions()
    specs = [s for s in box.specs() if not light or s.get("type") != "web_search"]

    async def run_tool(_name: str, _arguments: Any = None) -> str:
        return NOT_DONE

    messages = [{"role": "user" if who == "heard" else "assistant", "content": text} for who, text in history]
    return await provider.reply(messages + [{"role": "user", "content": ask}], system=system, max_tokens=200,
                                timeout=15.0, tools=specs, run_tool=run_tool,
                                max_tool_rounds=ctx.config.chatgpt.max_tool_rounds,
                                effort=live.LIGHT_EFFORT if light else None, final_tools=live.FINAL_TOOLS)


def decision_clients(ctx: Any) -> dict[str, Any]:
    from littlebot.brain.text_model import make_claude
    from littlebot.decide.claude import ClaudeDecisionsClient

    cfg = ctx.config.decide
    out: dict[str, Any] = {"claude": ClaudeDecisionsClient(
        make_claude(ctx, purpose="decide", model=cfg.claude_model or ctx.config.claude.model))}
    key = ctx.config.typesafe_api_key
    if key is not None and key.get_secret_value():
        from littlebot.decide.jev import JevClient

        out["jev"] = JevClient(key.get_secret_value(), model=cfg.jev_model, base_url=cfg.jev_base_url)
    key = ctx.config.openai_api_key
    if key is not None and key.get_secret_value():
        from littlebot.decide.openai import OpenAIDecisionsClient

        out["openai"] = OpenAIDecisionsClient(key.get_secret_value(), model=cfg.openai_model,
                                              base_url=cfg.openai_base_url)
    return out


async def decide(ctx: Any, client: Any) -> dict[str, Any]:
    from littlebot.decide import uses
    from littlebot.decide.base import Choice

    q = Choice("A voice assistant handed this request to its robot. Which part of the robot should handle it?",
               uses.ROUTES)
    return await client.ask({"handed_to_the_robot": ROUTE_ASK, "they_said_last": ROUTE_ASK}, {"route": q},
                            timeout=float(ctx.config.decide.timeout_seconds) + 2.0)


async def replay_log(args: argparse.Namespace, ctx: Any, lines: "_Lines", want: set[str], cards: dict[str, Any],
                     handoffs: dict[str, Any]) -> int:
    """The log's requests (LOG_REQUESTS) to the chosen models, printed next to ChatGPT's logged times.
    Each Claude model in ``--claude-model`` is asked in turn."""
    from littlebot.brain.providers.base import ProviderError
    from littlebot.brain.text_model import make_claude

    who: list[tuple[str, Any, Any]] = []
    if "chatgpt" in want:
        who.append(("chatgpt", cards["chatgpt"], handoffs["chatgpt"]))
    if "claude" in want:
        for model in _claude_models(args) or [ctx.config.claude.model]:
            who.append((model, make_claude(ctx, purpose="screen", model=model),
                        make_claude(ctx, purpose="handoff", model=model)))
    for _name, screen, hand in who:
        for p in (screen, hand):
            if getattr(p, "warm", None) is not None:
                await p.warm(5.0)  # opens the connection once; no model is asked
    costs: dict[str, float] = {}
    width = max(8, *(len(n) for n, _s, _h in who))
    print(f"{'logged at':9} {'request':15} {'ChatGPT then (log)':>20}   {'model':{width}} {'first word ms':>13} "
          f"{'total ms':>9}  answer")
    for req in LOG_REQUESTS:
        logged = ", ".join(f"{t}" + (f" (+{f})" if f else "") for t, f in req["chatgpt_ms"])
        for rnd in range(args.rounds):
            for i, (name, screen, hand) in enumerate(who):
                before = len(lines.rows)
                try:
                    if req["kind"] == "card":
                        reply = await card(ctx, screen, req["lines"])
                    else:
                        reply = await handoff(ctx, hand, req["ask"], light=req.get("light", True),
                                              history=req.get("history", ()))
                    note = " ".join(reply.text.split())[:60]
                except (ProviderError, asyncio.TimeoutError) as e:
                    note = f"failed: {getattr(e, 'kind', type(e).__name__)}"
                row = lines.rows[before] if len(lines.rows) > before else {}
                if name != "chatgpt" and row:
                    costs[name] = costs.get(name, 0.0) + _cost(name, row)
                first = (req["at"], req["kind"], logged) if rnd == 0 and i == 0 else ("", "", "")
                print(f"{first[0]:9} {first[1]:15} {first[2]:>20}   {name:{width}} "
                      f"{row.get('first_token_ms', '-'):>13} {row.get('total_ms', '-'):>9}  {note}")
    print("\nChatGPT's logged times are from the robot (its own network, the whole request); a card's logged time "
          "also\nincludes the director waiting for the words. The requests are rebuilt from the log's card titles.")
    for name, cost in costs.items():
        print(f"{name} cost for this run (estimate from the token counts, web searches not included): ${cost:.4f}")
    for _name, screen, hand in who:
        await screen.aclose()
        await hand.aclose()
    for p in {**cards, **handoffs}.values():
        await p.aclose()
    return 0


def _claude_models(args: argparse.Namespace) -> list[str]:
    models = getattr(args, "claude_model", None) or []
    return [models] if isinstance(models, str) else list(models)


def _cost(model: str, row: dict[str, str]) -> float:
    """USD for one call from its logged token counts (input, cached input, output per million tokens)."""
    price = PRICES.get(model, PRICES["claude-haiku-5-5"])
    t_in, cached, t_out = (int(row.get(k, 0) or 0) for k in ("tokens_in", "cache_read", "tokens_out"))
    return ((t_in - cached) * price[0] + cached * price[1] + t_out * price[2]) / 1e6


def _ms(v: str | None) -> int | None:
    return int(v) if v and v.isdigit() else None


def _median(values: list[int]) -> str:
    return f"{int(statistics.median(values))}" if values else "-"


async def run(args: argparse.Namespace) -> int:
    from littlebot.brain.providers.base import ProviderError
    from littlebot.brain.text_model import cloud_block
    from littlebot.decide.base import DecideError

    ctx = build_context(args.mode)
    if _claude_models(args):
        ctx.config.claude.model = _claude_models(args)[0]
    blocked = cloud_block(ctx)
    if blocked:
        print(f"Not asking any model: {blocked} (offline mode, or Cloud communication is off on the Privacy page).")
        return 2
    lines = _Lines()
    timing_log = logging.getLogger("littlebot.text_model")
    timing_log.addHandler(lines)
    timing_log.setLevel(logging.INFO)
    timing_log.propagate = False  # the table shows the numbers; the lines themselves aren't printed
    want = set(args.only or ["chatgpt", "claude", "jev", "openai"])

    cards, handoffs = providers(ctx, "screen"), providers(ctx, "handoff")
    for name in ("claude",):
        missing = cards[name].missing()
        if missing and name in want:
            want.discard("claude")
            print(f"Claude isn't set up ({missing}): see docs/CLAUDE.md."
                  + (" Timing the others only." if want else " Nothing to time."))
            if not want:
                return 2
    if getattr(args, "from_log", False):
        try:
            return await replay_log(args, ctx, lines, want, cards, handoffs)
        finally:
            ctx.db.close()
    deciders = {k: v for k, v in decision_clients(ctx).items() if k in want}

    results: list[dict[str, Any]] = []
    cost = 0.0
    for p in {**cards, **handoffs}.values():
        warm = getattr(p, "warm", None)
        if warm is not None and p.name in want:
            await warm(5.0)  # opens the connection once; no model is asked
    for rnd in range(1, args.rounds + 1):
        for request, table in (("card", cards), ("hand-off", handoffs)):
            for name, p in table.items():
                if name not in want:
                    continue
                before = len(lines.rows)
                note = ""
                try:
                    reply = await (card(ctx, p) if request == "card" else handoff(ctx, p))
                    note = " ".join(reply.text.split())[:70]
                except (ProviderError, asyncio.TimeoutError) as e:
                    note = f"failed: {getattr(e, 'kind', type(e).__name__)}"
                row = lines.rows[before] if len(lines.rows) > before else {}
                results.append({"request": request, "who": name, "round": rnd, "log": row, "note": note})
                if name == "claude" and row:
                    cost += _cost(str(row.get("model", "")), row)
        for name, client in deciders.items():
            before = len(lines.rows)
            t0 = asyncio.get_running_loop().time()
            try:
                answers = await decide(ctx, client)
                a = answers.get("route")
                note = f"route={getattr(a, 'value', '?')}"
            except DecideError as e:
                note = f"failed: {e.kind}"
            total = int((asyncio.get_running_loop().time() - t0) * 1000)
            row = lines.rows[before] if len(lines.rows) > before else {"total_ms": str(total)}
            results.append({"request": "decider", "who": name, "round": rnd, "log": row, "note": note})
            if name == "claude" and "tokens_in" in row:
                cost += _cost(str(row.get("model", "")), row)

    print(f"\n{'request':9} {'provider':8} {'model':18} {'round':>5} {'first word ms':>13} {'total ms':>9}  answer")
    for r in results:
        log_ = r["log"]
        print(f"{r['request']:9} {r['who']:8} {log_.get('model', '-'):18} {r['round']:>5} "
              f"{log_.get('first_token_ms', '-'):>13} {log_.get('total_ms', '-'):>9}  {r['note']}")
    print(f"\n{'request':9} {'provider':8} {'median first word ms':>21} {'median total ms':>16}"
          "  (rounds after the first)")
    for request in ("card", "hand-off", "decider"):
        for name in ("chatgpt", "claude", "jev", "openai"):
            rows = [r for r in results if r["request"] == request and r["who"] == name]
            if not rows:
                continue
            warm_rows = [r for r in rows if r["round"] > 1] or rows
            firsts = [x for x in (_ms(r["log"].get("first_token_ms")) for r in warm_rows) if x is not None]
            totals = [x for x in (_ms(r["log"].get("total_ms")) for r in warm_rows) if x is not None]
            print(f"{request:9} {name:8} {_median(firsts):>21} {_median(totals):>16}")
    if "claude" in want:
        print(f"\nClaude cost for this run (estimate from the token counts): ${cost:.4f}")
    for p in {**cards, **handoffs}.values():
        await p.aclose()
    ctx.db.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rounds", type=int, default=2, help="times to ask each request per model (1-5; default 2)")
    ap.add_argument("--only", action="append", choices=["chatgpt", "claude", "jev", "openai"],
                    help="only this model (repeat for more)")
    ap.add_argument("--mode", default="pi", help="config mode (default pi)")
    ap.add_argument("--from-log", action="store_true",
                    help="the requests behind the slow calls in the 2026-10-26 log, next to their logged times "
                         "(Claude only unless --only says otherwise; once each unless --rounds)")
    ap.add_argument("--claude-model", action="append",
                    help="the Claude model (default claude.model in config/local.yaml); with --from-log, repeat it "
                         "to time several, e.g. --claude-model claude-haiku-5-5 --claude-model claude-haiku-4-5")
    args = ap.parse_args()
    if args.from_log:
        args.only = args.only or ["claude"]
        args.rounds = args.rounds if "--rounds" in sys.argv else 1
    args.rounds = max(1, min(5, args.rounds))
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
