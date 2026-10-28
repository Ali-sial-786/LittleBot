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

With --live (on the robot: a network that lets WebRTC through, and espeak-ng), each model gets a real GPT-Live
call through ``LiveCall.run()``: the child's lines from the log are spoken into the call's microphone with espeak, one
after another, and the voice hears them, hands off (or not) and answers as it does with a child. A test sound card
stands in for the microphone and speaker (nothing is played; the robot's own microphone and speaker are left alone),
so each turn is timed from the end of the child's words: the voice's transcript, its first sound, the hand-off, the
text model, what the voice was sent and when, when it began saying the answer, when it went quiet, and the card.

    .venv/bin/python scripts/time_live_handoff.py --live \
        --model gpt-6-luna --model gpt-5.6-luna --model claude-haiku-5-5 --model claude-haiku-4-5

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


class _TestAudio:
    """The live call's sound card for --live: the child's words (handed to ``say``) are its microphone, and the
    voice's sound is timed instead of played. 20 ms frames, 48 kHz mono, as LiveAudio's."""

    LOUD = 0.01  # RMS of a frame that counts as the voice making sound (voice_record.LOUD)

    def __init__(self) -> None:
        self.level = 0.0
        self.mic_open = True
        self.loud: list[float] = []  # when each of the voice's loud frames came
        self._queue: list[tuple[Any, asyncio.Event]] = []
        self._pos = 0
        self._next: float | None = None

    @property
    def speaking(self) -> bool:
        return bool(self.loud) and time.monotonic() - self.loud[-1] < 0.3

    def open(self, loop: Any) -> None:
        pass

    def close(self) -> None:
        pass

    def mute(self) -> None:
        pass

    def drop_voice(self) -> None:
        pass

    def say(self, pcm: Any) -> asyncio.Event:
        """Queue ``pcm`` (int16 at 48 kHz) for the microphone; the event is set once all of it was sent."""
        ended = asyncio.Event()
        self._queue.append((pcm, ended))
        return ended

    async def read_frame(self) -> Any:
        import numpy as np

        now = time.monotonic()
        self._next = now if self._next is None or self._next < now - 0.2 else self._next
        await asyncio.sleep(max(0.0, self._next - now))
        self._next += 0.02
        pcm = np.zeros(960, dtype=np.int16)
        if self._queue:
            sound, ended = self._queue[0]
            chunk = sound[self._pos:self._pos + 960]
            pcm[:len(chunk)] = chunk
            self._pos += 960
            if self._pos >= len(sound):
                self._queue.pop(0)
                self._pos = 0
                ended.set()
        return pcm

    def play_frame(self, frame: Any) -> None:
        import numpy as np

        x = frame.to_ndarray().astype(np.float64) / 32768.0
        self.level = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        if self.level > self.LOUD:
            self.loud.append(time.monotonic())


def conversation() -> list[str]:
    """What the child said in the 26 Oct log, in order (each line once)."""
    out: list[str] = []
    for r in LOG_REQUESTS:
        text = r.get("ask") or r["lines"][-1][1]
        if text not in out:
            out.append(text)
    return out


def _speech(text: str) -> Any:
    from littlebot.chatgpt.voice_record import espeak_pcm

    pcm = espeak_pcm(text)
    if pcm is None:
        raise SystemExit("--live speaks the child's words with espeak-ng: sudo apt install espeak-ng")
    return pcm


async def live_turn(call: Any, audio: _TestAudio, events: list[tuple[float, str, Any]],
                    sent: list[tuple[float, str, str]], logs: _Logs, cards: list[tuple[float, dict[str, Any]]],
                    words: str, max_wait: float) -> dict[str, Any]:
    """Say ``words`` into the call, wait for the voice to finish with it, and time what happened in between."""
    from littlebot.chatgpt import wire
    from littlebot.chatgpt.live import LATE_ANSWER, MORE_ANSWER, STILL_WORKING

    marks = (len(events), len(sent), len(audio.loud), len(logs.rows), len(cards), len(logs.lines))
    t_start = time.monotonic()
    await audio.say(_speech(words)).wait()
    t0 = time.monotonic()  # the child has stopped talking
    deadline = t0 + max_wait
    while time.monotonic() < deadline and not call._ended.is_set():
        await asyncio.sleep(0.1)
        last = max([t0] + [e[0] for e in events[marks[0]:]] + [s[0] for s in sent[marks[1]:]]
                   + audio.loud[marks[2]:])
        answered = any(isinstance(e[2], wire.AssistantText) and e[2].final for e in events[marks[0]:])
        if answered and call._delegations == 0 and time.monotonic() - last > 2.0:
            break
    ev, out, loud = events[marks[0]:], sent[marks[1]:], audio.loud[marks[2]:]
    rows = logs.rows[marks[3]:]
    hand = [r for r in rows if r.get("purpose") == "handoff"]
    lines = [m for _t, _n, m in logs.lines[marks[5]:]]

    def first(pred: Any, items: list[Any]) -> Any:
        return next((x for x in items if pred(x)), None)

    heard = first(lambda e: isinstance(e[2], wire.UserText) and e[2].final, ev)
    deleg = first(lambda e: isinstance(e[2], wire.Delegation), ev)
    results = [s for s in out if s[1] == "delegation.context.append"]
    late = (LATE_ANSWER.split("{")[0], MORE_ANSWER.split("{")[0])  # the rest / a late answer, as notes
    answer = [s for s in results if s[2] not in (STILL_WORKING, wire.FAILED_RESULT)] \
        + [s for s in out if s[1] == "session.context.append" and s[2].startswith(late)]
    answer.sort()
    first_out = results[0] if results else None
    if first_out is None:
        first_what = "-"
    elif first_out[2] == STILL_WORKING:
        first_what = "wait (3.5 s)"
    elif first_out[2] == wire.FAILED_RESULT:
        first_what = "failed"
    elif any("answered with its first sentence" in m for m in lines):
        first_what = "1st sentence"
    else:
        first_what = "answer"
    answer_at = answer[0][0] if answer else None
    spoke_answer = first(lambda t: t > answer_at, loud) if answer_at is not None else None
    said = " ".join(e[2].text for e in ev if isinstance(e[2], wire.AssistantText) and e[2].final)
    kind = next((m.split("kind=")[1].split(")")[0].split(",")[0] for m in lines if "hand-off received" in m), "-")
    return {
        "words": words, "speech_ms": _ms(t0, t_start), "heard_ms": _ms(heard[0], t0) if heard else None,
        "voice_sound_ms": _ms(loud[0], t0) if loud else None,
        "handoff_ms": _ms(deleg[0], t0) if deleg else None, "kind": kind,
        "text_model": ",".join(dict.fromkeys(r.get("model", "?") for r in hand)) or "-",
        "first_token_ms": hand[0].get("first_token_ms") if hand else None,
        "model_total_ms": sum(int(r.get("total_ms", 0)) for r in hand) if hand else None,
        "to_voice_ms": _ms(first_out[0], t0) if first_out else None, "first_what": first_what,
        "answer_sent_ms": _ms(answer[-1][0], t0) if answer else None,
        "answer_spoken_ms": _ms(spoke_answer, t0) if spoke_answer else None,
        "voice_done_ms": _ms(loud[-1], t0) if loud else None,
        "card_ms": _ms(cards[marks[4]][0], t0) if len(cards) > marks[4] else None,
        "card": str(cards[marks[4]][1].get("kind")) if len(cards) > marks[4] else "none",
        "said": " ".join(said.split())[:80],
    }


async def live_call(ctx: Any, logs: _Logs, model: str, rnd: int, args: argparse.Namespace,
                    **svc_options: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One real GPT-Live call through LiveCall.run(), the child's lines from the log spoken into it in turn."""
    from littlebot.chatgpt import wire
    from littlebot.chatgpt.live import LiveCall, LiveVoiceService

    audio = _TestAudio()
    svc = LiveVoiceService(ctx, audio_factory=lambda: audio, **svc_options)
    call = LiveCall(svc)
    events: list[tuple[float, str, Any]] = []
    sent: list[tuple[float, str, str]] = []
    cards: list[tuple[float, dict[str, Any]]] = []
    own = call._on_message

    def on_message(message: Any) -> None:
        parsed = wire.parse(message)
        if parsed is not None:
            events.append((time.monotonic(), parsed[0], parsed[1]))
        own(message)

    call._on_message = on_message  # LiveCall._connect hands this to the data channel
    sub = ctx.bus.subscribe("screen.card_shown", lambda e: cards.append((time.monotonic(), dict(e.data))))
    t0 = time.monotonic()
    task = asyncio.ensure_future(call.run())
    setup: dict[str, Any] = {"model": model, "round": rnd}
    turns: list[dict[str, Any]] = []
    try:
        while not task.done() and time.monotonic() - t0 < args.channel_wait + 25:
            ch = call.channel
            if ch is not None and ch.readyState == "open":
                break
            await asyncio.sleep(0.02)
        setup["connected_ms"] = call.connect_ms
        ch = call.channel
        if task.done() or ch is None or ch.readyState != "open":
            setup["error"] = call.error or ("the data channel didn't open" + (
                f" ({args.channel_wait + 25:.0f} s after the start; UDP blocked?)" if call.connect_ms else ""))
            return setup, turns
        setup["channel_ms"] = _ms(time.monotonic(), t0)
        setup["voice"] = call.voice
        send = ch.send

        def recorded(frame: str) -> None:
            obj = json.loads(frame)
            sent.append((time.monotonic(), str(obj.get("type")),
                         "".join(c.get("text", "") for c in obj.get("content", []))))
            send(frame)

        ch.send = recorded
        await asyncio.sleep(1.0)  # the call's own first notes (who is here, a paused video) go first
        for words in conversation():
            print(f"round {rnd}, {model}: saying {words!r} ...", flush=True)
            turns.append({"model": model, "round": rnd, **await live_turn(call, audio, events, sent, logs, cards,
                                                                        words, args.turn_wait)})
            if call._ended.is_set():
                break
    finally:
        sub.cancel()
        call.end("timing test done")
        try:
            await asyncio.wait_for(task, 10)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        await svc.stop()
    return setup, turns


def print_live(setups: list[dict[str, Any]], turns: list[dict[str, Any]], models: list[str]) -> None:
    def v(x: Any) -> str:
        return "-" if x is None else str(x)

    print("\nCALL SETUP (ms from the start; \"connected\" is where the robot logs it, then the data channel opens)")
    print(f"{'model':18} {'round':>5} {'connected':>9} {'data channel':>12}  voice")
    for s in setups:
        if "error" in s:
            print(f"{s['model']:18} {s['round']:>5} {v(s.get('connected_ms')):>9}  failed: {s['error']}")
        else:
            print(f"{s['model']:18} {s['round']:>5} {v(s['connected_ms']):>9} {v(s['channel_ms']):>12}  {s['voice']}")
    print("\nTURNS (ms from the end of the child's words)")
    print(f"{'model':18} {'rnd':>3} {'heard':>5} {'voice sound':>11} {'hand-off':>8} {'kind':7} {'model 1st':>9} "
          f"{'model total':>11} {'to voice':>8} {'what':13} {'answer sent':>11} {'answer spoken':>13} "
          f"{'voice done':>10} {'card at':>7} {'card':8} words / said")
    for t in turns:
        print(f"{t['model']:18} {t['round']:>3} {v(t['heard_ms']):>5} {v(t['voice_sound_ms']):>11} "
              f"{v(t['handoff_ms']):>8} {t['kind']:7} {v(t['first_token_ms']):>9} {v(t['model_total_ms']):>11} "
              f"{v(t['to_voice_ms']):>8} {t['first_what']:13} {v(t['answer_sent_ms']):>11} "
              f"{v(t['answer_spoken_ms']):>13} {v(t['voice_done_ms']):>10} {v(t['card_ms']):>7} {t['card']:8} "
              f"{t['words'][:40]!r} / {t['said']}")
    print("\nMEDIANS per model (ms; hand-offs only for the hand-off columns)")
    print(f"{'model':18} {'connected':>9} {'data channel':>12} {'voice sound':>11} {'hand-off':>8} {'to voice':>8} "
          f"{'answer spoken':>13} {'card at':>7}  hand-offs  waits")
    for model in models:
        ss = [s for s in setups if s["model"] == model and "error" not in s]
        tt = [t for t in turns if t["model"] == model]
        hh = [t for t in tt if t["handoff_ms"] is not None]

        def med(vals: list[Any]) -> str:
            got = [int(x) for x in vals if x is not None]
            return str(int(statistics.median(got))) if got else "-"

        print(f"{model:18} {med([s['connected_ms'] for s in ss]):>9} {med([s['channel_ms'] for s in ss]):>12} "
              f"{med([t['voice_sound_ms'] for t in tt]):>11} {med([t['handoff_ms'] for t in hh]):>8} "
              f"{med([t['to_voice_ms'] for t in hh]):>8} {med([t['answer_spoken_ms'] for t in hh]):>13} "
              f"{med([t['card_ms'] for t in tt]):>7}  {len(hh):>4}/{len(tt):<4} "
              f"{sum(t['first_what'].startswith('wait') for t in hh):>5}")


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
    if args.live:
        for rnd in range(1, args.rounds + 1):
            for model in args.model:
                configure(ctx, model)
                print(f"round {rnd}, {model}: calling GPT-Live ...", flush=True)
                s, t = await live_call(ctx, logs, model, rnd, args)
                setups.append(s)
                results.extend(t)
                ctx.screens.close("next call")
        ctx.screens.detach()
        ctx.db.close()
        print_live(setups, results, args.model)
        if args.json:
            Path(args.json).write_text(json.dumps({"setups": setups, "turns": results}, indent=1))
        return 0
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
    ap.add_argument("--live", action="store_true",
                    help="a real GPT-Live call per model, the child's words spoken into it (needs espeak-ng and a "
                         "network that lets WebRTC through: the robot's)")
    ap.add_argument("--turn-wait", type=float, default=40.0, help="--live: seconds at most for each turn")
    ap.add_argument("--json", help="also write the results here")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
