#!/usr/bin/env python3
"""Time a live voice call's setup and its hand-offs with each text model, through LittleBot's own call code.

    .venv/bin/python scripts/time_live_handoff.py --mode simulation \
        --model gpt-6-luna --model gpt-5.6-luna --model claude-haiku-5-5 --model claude-haiku-4-5

For each model (``--rounds`` times, models taking turns):

* setup: the ChatGPT sign-in's token, the WebRTC offer, the POST to the Codex realtime endpoint (with the call's
  real instructions and voice; the robot logs "connected" once its answer is applied), warming the text model and
  the screen director, then waiting for the call's data channel (``--channel-wait``). Where UDP is blocked the
  audio and the data channel never open: the table says so, and the call is closed.
* hand-offs: the requests behind the slow calls in the 26 Oct log (scripts/compare_text_models.py LOG_REQUESTS).
  The child's words go to the screen director as LittleBot's call gives them (``heard``), and the voice's hand-off
  goes to ``LiveCall._answer``, the robot's own code: routing, light or full, first sentence early, "still
  thinking" after 3.5 s, the rest as a note. A recorder stands in for the data channel and timestamps every frame
  the voice would be sent; ``ctx.screens`` is a real ScreenService (no display), so cards are decided and built
  as on the robot. GPT-Live itself (hearing the words, choosing to hand off, saying the answer) is not timed here:
  its hand-off is sent at once, in the child's words.

Claude needs ANTHROPIC_API_KEY (no fallback to ChatGPT while timing); ChatGPT and the voice need the ChatGPT sign-in
in the data folder. Tools are offered as on the robot but only the screen acts (simulation mode).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from compare_text_models import LOG_REQUESTS

HANDOFFS = [r for r in LOG_REQUESTS if r["kind"].startswith("hand-off")]
_FIELD = re.compile(r"(\w+)=(\S+)")


class _Logs(logging.Handler):
    """The text model's timing lines (as dicts) and the live voice's / director's lines, with their times."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.rows: list[dict[str, str]] = []
        self.lines: list[tuple[float, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith("text model: provider="):
            self.rows.append(dict(_FIELD.findall(msg)))
        else:
            self.lines.append((time.monotonic(), record.name, msg))


class _Channel:
    """Stands in for the call's data channel: keeps each frame the voice would be sent, and when."""

    readyState = "open"

    def __init__(self) -> None:
        self.frames: list[tuple[float, str, str]] = []

    def send(self, frame: str) -> None:
        obj = json.loads(frame)
        text = "".join(c.get("text", "") for c in obj.get("content", []))
        self.frames.append((time.monotonic(), str(obj.get("type")), text))


def configure(ctx: Any, model: str) -> None:
    cfg = ctx.config
    if model.startswith("claude"):
        cfg.text.provider = "claude"
        cfg.claude.model = model
        cfg.text.fallback = False  # Claude on its own: its failure is the answer's failure
    else:
        cfg.text.provider = "chatgpt"
        cfg.chatgpt.models = [model]
        cfg.chatgpt.handoff_models = []
        cfg.chatgpt.look_models = []


def _ms(t: float | None, t0: float) -> int | None:
    return None if t is None else int((t - t0) * 1000)


async def setup(call: Any, channel_wait: float) -> dict[str, Any]:
    """The call's setup as LiveCall._connect does it (no microphone or speaker), each step timed."""
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack

    from littlebot.chatgpt.live import CallFailed

    out: dict[str, Any] = {}
    t0 = time.monotonic()
    bearer = await call.svc.auth.bearer()
    out["token_ms"] = _ms(time.monotonic(), t0)
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTrack(AudioStreamTrack())  # silence for the microphone
    channel = pc.createDataChannel("oai-events")
    opened = asyncio.Event()
    channel.on("open", opened.set)
    ice: list[tuple[int, str]] = []
    pc.on("iceconnectionstatechange", lambda: ice.append((_ms(time.monotonic(), t0), pc.iceConnectionState)))
    try:
        t = time.monotonic()
        await pc.setLocalDescription(await pc.createOffer())
        out["offer_ms"] = _ms(time.monotonic(), t)
        cfg = call.cfg
        voices = list(dict.fromkeys(v.strip().lower() for v in [cfg.live_voice, *cfg.live_voice_fallbacks]
                                    if v and v.strip())) or ["sol"]
        t = time.monotonic()
        while True:
            try:
                answer = await asyncio.wait_for(call._post_offer(pc.localDescription.sdp, bearer, voices[0]),
                                                cfg.live_connect_timeout_seconds)
                break
            except CallFailed as e:
                if e.status in (400, 422) and len(voices) > 1:
                    voices.pop(0)
                    continue
                out["error"] = f"HTTP {e.status}"
                return out
            except TimeoutError:
                out["error"] = "the endpoint didn't answer in time"
                return out
        out["post_ms"] = _ms(time.monotonic(), t)
        out["voice"] = voices[0]
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        out["connected_ms"] = _ms(time.monotonic(), t0)  # where the robot logs "live voice: connected in N ms"
        t = time.monotonic()
        provider = call.svc.provider()
        warm = getattr(provider, "warm", None)
        jobs = [asyncio.ensure_future(warm())] if warm is not None else []
        director = call._director
        if director is not None:
            jobs.append(asyncio.ensure_future(director.warm()))
        if jobs:
            await asyncio.wait(jobs, timeout=10)
        out["warm_ms"] = _ms(time.monotonic(), t)
        try:
            await asyncio.wait_for(opened.wait(), channel_wait)
            out["channel_ms"] = _ms(time.monotonic(), t0)
        except TimeoutError:
            out["channel_ms"] = None
        out["ice"] = ice
        return out
    finally:
        await pc.close()


async def handoff(call: Any, logs: _Logs, req: dict[str, Any], n: int) -> dict[str, Any]:
    """One request: the child's words, then the voice's hand-off of them, as the call handles them."""
    from littlebot.chatgpt import wire

    call._captions.clear()
    for who, text in req.get("history", ()):
        call._captions.append({"who": who, "text": text})
    if call.ctx.screens is not None:
        call.ctx.screens.close("next request")
    shown: list[tuple[float, dict[str, Any]]] = []
    sub = call.ctx.bus.subscribe("screen.card_shown", lambda e: shown.append((time.monotonic(), dict(e.data))))
    call.channel = chan = _Channel()
    rows_before, lines_before = len(logs.rows), len(logs.lines)
    ask = req["ask"]
    t0 = time.monotonic()
    call._caption("heard", ask)
    if call._director is not None:
        call._director.heard(ask, handled=False)
    await call._answer(wire.Delegation(f"test-{n}", ask))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:  # notes still to come (the rest, a late answer) and the card
        pending = [t for t in list(call._tasks) if not t.done()]
        if not pending:
            break
        await asyncio.wait(pending, timeout=max(0.1, deadline - time.monotonic()))
    sub.cancel()
    from littlebot.chatgpt.live import STILL_WORKING

    first = chan.frames[0] if chan.frames else None
    said = [f for f in chan.frames if f[2] not in (STILL_WORKING, wire.FAILED_RESULT)]
    rows = logs.rows[rows_before:]
    hand = [r for r in rows if r.get("purpose") == "handoff"]
    lines = [m for _t, _n, m in logs.lines[lines_before:]]
    kind = next((m.split("kind=")[1].split(")")[0].split(",")[0] for m in lines if "hand-off received" in m), "?")
    if first is None:
        first_what = "nothing"
    elif first[2] == STILL_WORKING:
        first_what = "wait (3.5 s)"
    elif first[2] == wire.FAILED_RESULT:
        first_what = "failed"
    elif any("first sentence at" in m and "answered with" in m for m in lines):
        first_what = "1st sentence"
    else:
        first_what = "answer"
    return {
        "request": req["at"], "kind": kind,
        "model": ",".join(dict.fromkeys(r.get("model", "?") for r in hand)) or "-",
        "first_token_ms": hand[0].get("first_token_ms") if hand else None,
        "model_total_ms": sum(int(r.get("total_ms", 0)) for r in hand) if hand else None,
        "first_frame_ms": _ms(first[0], t0) if first else None, "first_what": first_what,
        "answer_done_ms": _ms(said[-1][0], t0) if said else None,
        "card_ms": _ms(shown[0][0], t0) if shown else None,
        "card": f"{shown[0][1].get('kind')}" if shown else "none",
        "screen_calls": len([r for r in rows if r.get("purpose") == "screen"]),
        "answer": " ".join(" ".join(f[2] for f in said).split())[:70],
    }


async def run(args: argparse.Namespace) -> int:
    from compare_text_models import build_context

    from littlebot.brain.text_model import cloud_block
    from littlebot.chatgpt.live import LiveCall, LiveVoiceService
    from littlebot.screens.service import ScreenService

    logs = _Logs()
    for name in ("littlebot.text_model", "littlebot.chatgpt.live", "littlebot.screens.director"):
        lg = logging.getLogger(name)
        lg.addHandler(logs)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    ctx = build_context(args.mode)
    blocked = cloud_block(ctx)
    if blocked:
        print(f"Not asking any model: {blocked}.")
        return 2
    ctx.screens = ScreenService(ctx)
    await ctx.screens.start()
    setups: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    n = 0
    for rnd in range(1, args.rounds + 1):
        for model in args.model:
            configure(ctx, model)
            svc = LiveVoiceService(ctx)
            call = LiveCall(svc)
            call._director = call._make_director()
            if not args.no_setup:
                print(f"round {rnd}, {model}: setting up the call ...", flush=True)
                s = await setup(call, args.channel_wait)
                setups.append({"model": model, "round": rnd, **s})
            for req in HANDOFFS:
                n += 1
                print(f"round {rnd}, {model}: hand-off {req['at']} ...", flush=True)
                results.append({"model": model, "round": rnd, **await handoff(call, logs, req, n)})
            call._ended.set()
            for t in list(call._tasks):
                t.cancel()
            await svc.stop()
    ctx.screens.detach()
    ctx.db.close()

    print("\nCALL SETUP (ms from the start; the robot logs \"connected\" when the endpoint's answer is applied)")
    print(f"{'model':18} {'round':>5} {'token':>6} {'offer':>6} {'POST':>6} {'connected':>9} {'warm':>6} "
          f"{'data channel':>13}  ICE")
    for s in setups:
        if "error" in s:
            print(f"{s['model']:18} {s['round']:>5}  failed: {s['error']}")
            continue
        ch = s["channel_ms"] if s["channel_ms"] is not None else f"none in {args.channel_wait:.0f}s"
        ice = ", ".join(f"{st}@{ms}" for ms, st in s["ice"]) or "-"
        print(f"{s['model']:18} {s['round']:>5} {s['token_ms']:>6} {s['offer_ms']:>6} {s['post_ms']:>6} "
              f"{s['connected_ms']:>9} {s['warm_ms']:>6} {ch:>13}  {ice}")
    print("\nHAND-OFFS (ms from the child's words; the voice's hand-off sent at once)")
    print(f"{'model':18} {'rnd':>3} {'logged at':9} {'kind':7} {'model 1st word':>14} {'model total':>11} "
          f"{'voice gets':>10} {'what':13} {'all said':>8} {'card at':>7} {'card':8} answer")
    for r in results:
        print(f"{r['model']:18} {r['round']:>3} {r['request']:9} {r['kind']:7} {r['first_token_ms'] or '-'!s:>14} "
              f"{r['model_total_ms'] or '-'!s:>11} {r['first_frame_ms'] or '-'!s:>10} {r['first_what']:13} "
              f"{r['answer_done_ms'] or '-'!s:>8} {r['card_ms'] or '-'!s:>7} {r['card']:8} {r['answer']}")
    print("\nMEDIANS per model (ms)")
    print(f"{'model':18} {'POST':>6} {'connected':>9} {'model 1st word':>14} {'voice gets':>10} {'all said':>8} "
          f"{'card at':>7}  waits  failed  cards")
    for model in args.model:
        ss = [s for s in setups if s["model"] == model and "error" not in s]
        rr = [r for r in results if r["model"] == model]

        def med(vals: list[Any]) -> str:
            v = [int(x) for x in vals if x not in (None, "-")]
            return str(int(statistics.median(v))) if v else "-"

        print(f"{model:18} {med([s['post_ms'] for s in ss]):>6} {med([s['connected_ms'] for s in ss]):>9} "
              f"{med([r['first_token_ms'] for r in rr]):>14} {med([r['first_frame_ms'] for r in rr]):>10} "
              f"{med([r['answer_done_ms'] for r in rr]):>8} {med([r['card_ms'] for r in rr]):>7}  "
              f"{sum(r['first_what'].startswith('wait') for r in rr):>5} "
              f"{sum(r['first_what'] == 'failed' for r in rr):>7} {sum(r['card'] != 'none' for r in rr):>3}/{len(rr)}")
    if args.json:
        Path(args.json).write_text(json.dumps({"setups": setups, "handoffs": results}, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", action="append", required=True,
                    help="a ChatGPT model (e.g. gpt-5.6-luna) or a Claude one (claude-...); more than once")
    ap.add_argument("--rounds", type=int, default=2, help="times through every model (default 2)")
    ap.add_argument("--mode", default="pi", help="config mode (default pi)")
    ap.add_argument("--channel-wait", type=float, default=8.0, help="seconds to wait for the data channel")
    ap.add_argument("--no-setup", action="store_true", help="hand-offs only (no call is set up)")
    ap.add_argument("--json", help="also write the results here")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
