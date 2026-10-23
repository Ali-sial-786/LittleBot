"""Live voice: GPT-Live speech-to-speech on the parent's ChatGPT plan (ported from WatchGPT).

A parent starts each conversation from the dashboard (and confirms they are with the child).
LittleBot then opens a WebRTC call to OpenAI's GPT-Live voice model with the ChatGPT sign-in:

    POST https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas
         {"sdp": <offer>, "session": {model, instructions, audio.output.voice, delegation: client}}
    <- the SDP answer; audio then flows directly between the robot and OpenAI (DTLS-SRTP),
       and a data channel ("oai-events") carries captions and delegations (see wire.py).

Safety, enforced here in code (docs/AI_SAFETY.md, "Live voice"):

* only a signed-in parent can start it, from the dashboard, confirming they are present;
  privacy (cloud + microphone) must allow it; STOP ALL, a privacy change or a higher-priority
  task ends it at once; each call has a hard time limit and hangs up when nobody talks;
* every caption of what the voice says goes through the same ``validate_reply`` rules as typed
  replies *while it speaks*: a rule hit silences the speaker immediately, ends the call and
  alerts the parent (category only). Some words may already have been heard by then;
* what the person says is checked with ``classify_child_input``; a worrying category alerts the
  parent (category only, never the words);
* look-ups the voice hands over (delegations) are answered by the ChatGPT text model with
  LittleBot's child-safe prompt and validated before the voice is given them;
* nothing is recorded; captions live in memory during the call only and are cleared at the end.

UNDOCUMENTED endpoint, as in WatchGPT ("a spike to see whether it works, not a contract").
Tested here against a local WebRTC peer standing in for OpenAI, not against OpenAI itself.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import fractions
import logging
import time
import uuid
from typing import Any, Callable

import httpx

from littlebot.brain.prompts import build_system_prompt
from littlebot.brain.providers.base import BaseProvider, ProviderError
from littlebot.chatgpt import get_auth, wire
from littlebot.chatgpt.auth import ChatGPTAuth, ChatGPTAuthError
from littlebot.core.arbiter import Lease, Priority, Resource
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState
from littlebot.safety.content import BLOCK_RULES, classify_child_input, sanitize_user_text, validate_reply

log = logging.getLogger("littlebot.chatgpt.live")

OWNER = "live_voice"
CALL_URL = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
VOICES = ("cove", "arbor", "breeze", "ember", "juniper", "maple", "sol", "spruce", "vale")
POLL_S = 0.05
GOODBYE_QUIET_S = 0.8
GOODBYE_MAX_S = 10.0
FIRST_WORDS_MIN_S = 30.0
MAX_CAPTIONS = 8
CHECK_WINDOW = 320  # characters of a reply in progress checked at a time
NOT_SURE = "Say you are not sure, and suggest asking a grown-up."

ERRORS = {
    "parent_not_present": "Live voice only runs while a grown-up is with your child. Tick the box to confirm.",
    "offline": "LittleBot is in offline mode.",
    "cloud_off": "Cloud communication is turned off in Privacy.",
    "mic_off": "The microphone is turned off in Privacy.",
    "not_signed_in": "Sign in with ChatGPT first.",
    "unavailable": "Live voice isn't installed on this robot yet: re-run scripts/install_pi.sh, or run "
                   "cd ~/LittleBot && .venv/bin/python -m pip install -e '.[live]' and restart LittleBot.",
    "no_audio": "LittleBot's audio system is not running.",
    "already_running": "A live conversation is already running.",
    "busy": "LittleBot is busy with something else. Stop it first.",
}

CALL_ERRORS = {
    401: "The ChatGPT sign-in expired. Sign in again.",
    403: "Live voice is not available for this ChatGPT account or region. It uses an undocumented "
         "ChatGPT endpoint; a subscription doesn't guarantee access.",
    429: "ChatGPT voice usage limit reached. Try again after your plan's allowance resets; nothing else was used.",
    404: "The undocumented ChatGPT voice endpoint is unavailable or changed.",
}

VOICE_INSTRUCTIONS = """\
You are {name}, a small, gentle toy robot with a screen face, talking out loud with a family: \
{child} and a grown-up who is right there with them.

How you talk:
- Very short, simple, warm sentences. Slow and cheerful. One or two sentences, then stop and listen.
- The little one may babble, coo or make sounds rather than words. Answer with gentle, playful words: \
copy their sounds, animal noises, counting, colours, peekaboo, or a few lines of a well-known nursery rhyme.
- If the grown-up talks to you, answer them briefly and plainly, and help them play with the child.
- No lists, no reading out links, no emoji names, no stage directions.

Who you are:
- You are a robot toy, not a person, not alive, and never a parent or family member. If asked, say you are a robot.
- Never say you are the child's best or only friend, that they need you, that you will be sad, or ask them to stay.
- Never keep secrets or suggest hiding anything from grown-ups.

Off-limits:
- Never ask for or repeat personal information: names, address, school, phone numbers, passwords.
- Nothing scary, violent, rude or grown-up. No medical, health, feeding, sleep or development advice; \
suggest asking a doctor instead.
- No buying things, brands, ads, apps, websites or searching online.
- Never suggest anything risky: climbing, water, small objects, heat, sharp things, medicine, going outside alone, strangers.
- If anyone might be hurt or in danger, say: "Let's get a grown-up right now."

When a question needs facts you are not sure of, delegate it to the client and wait for the result, \
then say it simply in your own words. If you don't know, say so."""

LOOKUP_NOTE = """
You are answering a question that LittleBot's voice will say out loud to a family with a very young child; \
a grown-up may have asked it. Answer in one or two short, plain sentences (at most 40 words). \
If it needs current facts you don't have, say you're not sure."""


def aiortc_error() -> str | None:
    try:
        import aiortc  # noqa: F401, PLC0415
        import av  # noqa: F401, PLC0415
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None


class CallFailed(Exception):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"call failed {status}")
        self.status = status
        self.detail = detail


class LiveVoiceService(Service):
    """``ctx.live``: one parent-started live conversation at a time."""

    name = "live"

    def __init__(self, ctx: Any, *, auth: ChatGPTAuth | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 audio_factory: Callable[[], Any] | None = None, provider: BaseProvider | None = None,
                 call_url: str = CALL_URL, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.chatgpt
        self.auth = auth or get_auth(ctx)
        self._transport = transport
        self._audio_factory = audio_factory or self._default_audio
        self._provider = provider
        self.call_url = call_url
        self._now = now
        self._call: LiveCall | None = None
        self._task: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[Subscription] = []
        self.last_call: dict[str, Any] | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        bus = self.ctx.bus
        self._subs = [
            bus.subscribe(Topics.STOP_ALL, lambda e: self._end_threadsafe("stopped (STOP ALL)")),
            bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
        ]
        err = aiortc_error()
        if err:
            self.set_status(ServiceStatus.DEGRADED, "live voice not installed (.venv/bin/python -m pip install -e '.[live]')")
        else:
            self.set_status(ServiceStatus.RUNNING, "")

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        await self.stop_session("service stop")
        if self._provider is not None:
            await self._provider.aclose()
        self.set_status(ServiceStatus.STOPPED)

    def _on_privacy(self, event: Event) -> None:
        p = self.ctx.privacy
        if not (p.cloud_enabled and p.microphone_enabled):
            self._end_threadsafe("stopped (privacy settings changed)")

    def _end_threadsafe(self, reason: str) -> None:
        call, loop = self._call, self._loop
        if call is None or loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(call.end, reason)
        except RuntimeError:
            pass

    # ------------------------------------------------------------ helpers
    def _default_audio(self) -> Any:
        from littlebot.chatgpt.live_audio import LiveAudio  # noqa: PLC0415

        a = self.ctx.config.audio
        max_vol = float(a.speaker.max_volume)

        def volume() -> float:  # the parent's volume setting, never above the configured maximum
            v = self.cfg.live_volume
            if v is None:
                v = self.ctx.settings.get("audio.volume", a.speaker.volume)
            try:
                return max(0.0, min(max_vol, float(v)))
            except (TypeError, ValueError):
                return min(max_vol, float(a.speaker.volume))

        def gain() -> float:
            try:
                return float(self.ctx.settings.get("audio.input_gain", a.microphone.input_gain) or 1.0)
            except (TypeError, ValueError):
                return 1.0

        return LiveAudio(mic=a.microphone.device, speaker=a.speaker.device, volume=volume, mic_gain=gain,
                         half_duplex=self.cfg.live_half_duplex, echo_tail_s=self.cfg.live_echo_tail_ms / 1000,
                         voice_level=self.cfg.live_voice_level)

    def provider(self) -> BaseProvider:
        if self._provider is None:
            from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

            self._provider = ChatGPTProvider(self.auth, models=list(self.cfg.models), effort=self.cfg.reasoning_effort,
                                             transport=self._transport)
        return self._provider

    def blocked_reason(self, parent_present: bool = True) -> str | None:
        ctx = self.ctx
        if self._call is not None:
            return "already_running"
        if not parent_present:
            return "parent_not_present"
        if ctx.config.is_offline:
            return "offline"
        if not ctx.privacy.cloud_enabled:
            return "cloud_off"
        if not ctx.privacy.microphone_enabled:
            return "mic_off"
        if not self.auth.signed_in:
            return "not_signed_in"
        if self._audio_factory == self._default_audio and aiortc_error():
            return "unavailable"
        return None

    def status(self) -> dict[str, Any]:
        call = self._call
        reason = self.blocked_reason() if call is None else None
        out: dict[str, Any] = {
            "available": aiortc_error() is None,
            "signed_in": self.auth.signed_in,
            "can_start": call is None and reason is None,
            "blocked": reason,
            "blocked_detail": ERRORS.get(reason or "", None),
            "active": call is not None,
            "voice": self.cfg.live_voice,
            "session_minutes": self.cfg.live_session_minutes,
            "last_call": self.last_call,
        }
        if call is not None:
            out.update(call.snapshot())
        return out

    # ------------------------------------------------------------ control
    async def start_session(self, *, parent_present: bool) -> dict[str, Any]:
        reason = self.blocked_reason(parent_present)
        if reason:
            return {"ok": False, "error": reason, "detail": ERRORS[reason]}
        if self.ctx.audio is None:
            return {"ok": False, "error": "no_audio", "detail": ERRORS["no_audio"]}
        self._loop = asyncio.get_running_loop()
        call = LiveCall(self)
        lease = self.ctx.arbiter.acquire(OWNER, [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT,
                                         on_preempt=lambda _lease, why: self._end_threadsafe(f"stopped ({why})"))
        if lease is None:
            return {"ok": False, "error": "busy", "detail": ERRORS["busy"]}
        call.lease = lease
        self._call = call
        self._task = self._loop.create_task(self._run(call), name="live-voice")
        log.info("live voice: parent started a conversation (limit %.0f min)", self.cfg.live_session_minutes)
        return {"ok": True, **self.status()}

    async def _run(self, call: "LiveCall") -> None:
        try:
            await call.run()
        finally:
            if self._call is call:
                self._call = None
            self.last_call = call.summary()

    async def stop_session(self, reason: str = "stopped by parent") -> dict[str, Any]:
        call, task = self._call, self._task
        if call is None or task is None:
            return {"ok": True, **self.status()}
        call.end(reason)
        try:
            await asyncio.wait_for(asyncio.shield(task), 8.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        return {"ok": True, **self.status()}


class LiveCall:
    """One conversation: connect, run, and always clean up (speaker, mic, lease, state)."""

    def __init__(self, svc: LiveVoiceService) -> None:
        self.svc = svc
        self.ctx = svc.ctx
        self.cfg = svc.cfg
        self.lease: Lease | None = None
        self.audio: Any = None
        self.pc: Any = None
        self.channel: Any = None
        self.phase = "connecting"
        self.detail = "Connecting to ChatGPT..."
        self.error: str | None = None
        self.end_reason: str | None = None
        self._ended = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._captions: collections.deque[dict[str, str]] = collections.deque(maxlen=MAX_CAPTIONS)
        self._assistant_turn = ""
        self._last_check = 0.0
        self._delegations = 0
        self._started = svc._now()
        self._connected_at: float | None = None
        self._last_activity = self._started
        self._heard_user = False
        self._goodbye_at: float | None = None
        self._quiet_since: float | None = None
        self._our_state: RobotState | None = None
        self._suspended_audio = False
        self.connect_ms: int | None = None
        self.safety_stop: str | None = None

    # ------------------------------------------------------------ views
    def snapshot(self) -> dict[str, Any]:
        now = self.svc._now()
        limit = self.cfg.live_session_minutes * 60
        base = self._connected_at or self._started
        return {"phase": self.phase, "detail": self.detail, "error": self.error,
                "elapsed_s": int(now - base), "remaining_s": max(0, int(limit - (now - base))),
                "captions": list(self._captions), "connect_ms": self.connect_ms}

    def summary(self) -> dict[str, Any]:
        base = self._connected_at or self._started
        return {"ended_reason": self.end_reason or "ended", "error": self.error, "safety_stop": self.safety_stop,
                "duration_s": int(self.svc._now() - base), "connected": self._connected_at is not None,
                "ended_at": time.time()}

    def end(self, reason: str) -> None:
        if self.end_reason is None:
            self.end_reason = reason
            log.info("live voice: ending (%s)", reason)
        if self.audio is not None:
            self.audio.mute()
        self._ended.set()

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source="live")
        except Exception:  # noqa: BLE001
            log.debug("publish %s failed", topic, exc_info=True)

    def _set_state(self, state: RobotState) -> None:
        sm = self.ctx.state
        if sm.state == state:
            self._our_state = state
            return
        if sm.transition(state, "live voice"):
            self._our_state = state

    # ------------------------------------------------------------ run
    async def run(self) -> None:
        try:
            await self._connect()
            if not self._ended.is_set():
                await self._watch()
        except asyncio.CancelledError:
            self.end_reason = self.end_reason or "cancelled"
            raise
        except CallFailed as e:
            self.error = CALL_ERRORS.get(e.status if e.status in CALL_ERRORS else (404 if e.status in (405, 410) else 0),
                                         "Could not connect to ChatGPT live voice. Check the robot's internet and "
                                         "your ChatGPT account, then try again.")
            log.warning("live voice: call failed (HTTP %s)", e.status)
        except ChatGPTAuthError as e:
            self.error = str(e)
        except TimeoutError:
            self.error = "ChatGPT's voice service did not answer in time. Check the robot's internet, then try again."
            log.warning("live voice: connecting timed out")
        except Exception as e:  # noqa: BLE001 - always clean up and report
            name = type(e).__name__
            self.error = ("The speaker or microphone could not be opened." if name == "AudioDeviceError"
                          else f"Live voice failed ({name}).")
            log.exception("live voice failed")
        finally:
            await self._cleanup()

    async def _connect(self) -> None:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: PLC0415

        t0 = self.svc._now()
        audio_svc = self.ctx.audio
        if hasattr(audio_svc, "suspend"):
            await audio_svc.suspend("live voice")
            self._suspended_audio = True
        self._set_state(RobotState.LISTENING)
        self.audio = self.svc._audio_factory()
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.audio.open, loop)
        bearer = await self.svc.auth.bearer()
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.pc.addTrack(MicTrack(self.audio))
        self.channel = self.pc.createDataChannel("oai-events")
        self.channel.on("message", self._on_message)
        self.pc.on("track", self._on_track)
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)  # gathers candidates (no trickle ICE with this endpoint)
        sdp = self.pc.localDescription.sdp
        timeout = self.cfg.live_connect_timeout_seconds
        try:
            answer = await asyncio.wait_for(self._post_offer(sdp, bearer), timeout)
        except CallFailed as e:
            if e.status != 401:
                raise
            bearer = await self.svc.auth.on_unauthorized(bearer)
            answer = await asyncio.wait_for(self._post_offer(sdp, bearer), timeout)
        if self._ended.is_set():
            return
        if not answer.lstrip().startswith("v=0"):
            raise CallFailed(200, "the answer was not an SDP")
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        self._connected_at = self.svc._now()
        self._last_activity = self._connected_at
        self.connect_ms = int((self._connected_at - t0) * 1000)
        self.phase, self.detail = "listening", "Connected. Just talk."
        log.info("live voice: connected in %d ms", self.connect_ms)

    def _instructions(self) -> str:
        from littlebot.brain.prompts import age_description  # noqa: PLC0415

        age = self.ctx.settings.child_age_months()
        text = VOICE_INSTRUCTIONS.format(name=self.ctx.config.robot_name, child=age_description(age))
        now = _dt.datetime.now()
        return f"{text}\nAt the start of this call it is {now:%A}, {now:%H:%M} local time."

    async def _post_offer(self, sdp: str, bearer: Any) -> str:
        body = {"sdp": sdp, "session": {
            "model": self.cfg.live_model, "instructions": self._instructions(),
            "audio": {"output": {"voice": self.cfg.live_voice if self.cfg.live_voice in VOICES else "cove"}},
            "delegation": {"type": "client"}}}
        headers = {**self.svc.auth.headers(bearer), "OpenAI-Alpha": "quicksilver=v2",
                   "session-id": str(uuid.uuid4()), "thread-id": str(uuid.uuid4()), "x-session-id": str(uuid.uuid4())}
        try:
            async with httpx.AsyncClient(transport=self.svc._transport, follow_redirects=False,
                                         timeout=httpx.Timeout(20.0, connect=8.0)) as client:
                resp = await client.post(self.svc.call_url, json=body, headers=headers)
        except httpx.HTTPError:
            raise CallFailed(0, "network") from None
        if resp.status_code >= 400:
            raise CallFailed(resp.status_code)
        return resp.text

    # ------------------------------------------------------------ media
    def _on_track(self, track: Any) -> None:
        if track.kind == "audio":
            self._spawn(self._pump(track))

    async def _pump(self, track: Any) -> None:
        from aiortc.mediastreams import MediaStreamError  # noqa: PLC0415

        while not self._ended.is_set():
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            self.audio.play_frame(frame)

    # ------------------------------------------------------------ events
    def _on_message(self, message: Any) -> None:
        parsed = wire.parse(message)
        if parsed is None:
            return
        _etype, event = parsed
        now = self.svc._now()
        if isinstance(event, wire.AssistantText):
            self._last_activity = now
            if event.final:
                text, self._assistant_turn = event.text, ""
                self._caption("robot", text)
            else:
                self._assistant_turn = (self._assistant_turn + event.text)[-2000:]
                text = self._assistant_turn
            self._check_reply(text, event.final)
        elif isinstance(event, wire.UserText):
            self._last_activity = now
            self._heard_user = True
            if event.final:
                self._caption("heard", event.text)
                concern = classify_child_input(event.text)
                if concern is not None:
                    log.warning("live voice: worrying words heard (category=%s)", concern.category)
                    self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "attention",
                                                        "detail": concern.category})
        elif isinstance(event, wire.Delegation):
            self._spawn(self._answer(event))
        elif isinstance(event, wire.Failure):
            log.info("live voice: error event %s", event.code)
        elif isinstance(event, wire.Closed):
            if event.reason != "close_requested":
                self.error = "ChatGPT ended the call."
            self.end(f"closed by ChatGPT ({event.reason or 'no reason'})")

    def _caption(self, who: str, text: str) -> None:
        text = " ".join(str(text).split())[:300]
        if text:
            self._captions.append({"who": who, "text": text})

    def _check_reply(self, text: str, final: bool) -> None:
        """What the voice is saying, checked while it says it."""
        now = self.svc._now()
        if not final and now - self._last_check < 0.1 and not text.rstrip().endswith((".", "!", "?", ",")):
            return
        self._last_check = now
        if not final and len(text) > CHECK_WINDOW:
            # While it speaks: the latest few sentences (the rules match per sentence too); the whole
            # answer is checked once when the turn is done. Keeps each check to a few ms on a Pi.
            cut = text.rfind(". ", 0, len(text) - CHECK_WINDOW)
            text = text[cut + 2:] if cut >= 0 else text[-CHECK_WINDOW:]
        if not text.strip(" .!?,"):
            return
        result = validate_reply(text, max_words=10_000)
        if result.blocked:
            rules = [r for r in result.reasons if r in BLOCK_RULES or r == "grownup_redirect"]
            category = rules[0] if rules else "safety"
            self.safety_stop = category
            self.error = "LittleBot stopped the voice because it said something against its safety rules."
            log.warning("live voice: reply broke a safety rule (%s); call ended", category)
            self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "live_safety_stop", "detail": category})
            self.end(f"safety rule ({category})")
        elif result.concern and final:
            self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "attention", "detail": result.concern})

    async def _answer(self, d: wire.Delegation) -> None:
        self._delegations += 1
        try:
            question = d.prompt.strip()
            if not question:
                heard = [c["text"] for c in self._captions if c["who"] == "heard"]
                question = heard[-1] if heard else ""
            if not question:
                text = wire.NO_INPUT_RESULT
            else:
                text = await self._look_up(sanitize_user_text(question, max_chars=600))
            if not self._ended.is_set() and self.channel is not None and self.channel.readyState == "open":
                for frame in wire.result_frames(d.id, text):
                    self.channel.send(frame)
        finally:
            self._delegations -= 1
            self._last_activity = self.svc._now()

    async def _look_up(self, question: str) -> str:
        policy = self.ctx.age_policy()
        system = build_system_prompt(robot_name=self.ctx.config.robot_name, age_months=policy.age_months,
                                     max_words=40) + LOOKUP_NOTE
        history = []
        for c in list(self._captions)[-6:]:
            history.append({"role": "user" if c["who"] == "heard" else "assistant", "content": c["text"]})
        while history and history[0]["role"] != "user":
            history.pop(0)
        try:
            reply = await self.svc.provider().reply(history + [{"role": "user", "content": question}],
                                                    system=system, max_tokens=200, timeout=15.0)
        except (ProviderError, asyncio.TimeoutError):
            return wire.FAILED_RESULT
        checked = validate_reply(reply.text, max_words=40)
        return checked.text if checked.ok and reply.text.strip() else NOT_SURE

    # ------------------------------------------------------------ watch
    async def _watch(self) -> None:
        limit = self.cfg.live_session_minutes * 60
        idle = self.cfg.live_idle_hangup_seconds
        last_level = -1.0
        while not self._ended.is_set():
            now = self.svc._now()
            speaking = bool(self.audio.speaking)
            if speaking:
                self._last_activity = now
            phase = "speaking" if speaking else ("thinking" if self._delegations else "listening")
            if phase != self.phase:
                self.phase = phase
                self._set_state({"speaking": RobotState.SPEAKING, "thinking": RobotState.THINKING}.get(
                    phase, RobotState.LISTENING))
            level = round(min(1.0, float(self.audio.level) * 8.0), 2) if speaking else 0.0
            if level != last_level:
                last_level = level
                self._publish(Topics.AUDIO_VISEME, {"level": level})
            state = getattr(self.pc, "connectionState", "new")
            if state in ("failed", "closed"):
                self.error = "The connection to ChatGPT was lost."
                self.end("connection lost")
                break
            base = self._connected_at or self._started
            if self._goodbye_at is None and now - base >= limit:
                self._goodbye_at = now
                self.detail = "Time's up: saying goodbye."
                self._note("The grown-up's time limit for this chat is reached. Say a short, cheerful goodbye now.")
            if self._goodbye_at is not None:
                self._quiet_since = None if speaking else (self._quiet_since or now)
                said_it = now - self._goodbye_at > 1.5 and self._quiet_since is not None \
                    and now - self._quiet_since >= GOODBYE_QUIET_S
                if said_it or now - self._goodbye_at >= GOODBYE_MAX_S:
                    self.end("time limit reached")
                    break
            idle_limit = idle if self._heard_user else max(idle, FIRST_WORDS_MIN_S)
            if idle > 0 and now - self._last_activity >= idle_limit:
                self.end(f"hung up after {int(idle_limit)} s of quiet")
                break
            try:
                await asyncio.wait_for(self._ended.wait(), POLL_S)
            except asyncio.TimeoutError:
                pass

    def _note(self, text: str) -> None:
        if self.channel is not None and self.channel.readyState == "open":
            for frame in wire.note_frames(text):
                self.channel.send(frame)

    # ------------------------------------------------------------ cleanup
    async def _cleanup(self) -> None:
        self._ended.set()
        if self.audio is not None:
            self.audio.mute()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.pc is not None:
            try:
                await asyncio.wait_for(self.pc.close(), 5.0)
            except Exception:  # noqa: BLE001
                log.debug("closing the peer connection failed", exc_info=True)
        if self.audio is not None:
            await asyncio.to_thread(self.audio.close)
        if self._suspended_audio:
            self.ctx.audio.resume("live voice")
        self._publish(Topics.AUDIO_VISEME, {"level": 0.0})
        if self.lease is not None:
            self.lease.release()
        current = self.ctx.state.state
        if self._our_state is not None and current in (RobotState.LISTENING, RobotState.SPEAKING, RobotState.THINKING):
            self.ctx.state.release(current, "live voice ended")  # the lease kept anyone else out of these
        self._captions.clear()  # nothing from the conversation is kept
        self._assistant_turn = ""
        self.phase = "ended"
        self.end_reason = self.end_reason or ("failed" if self.error else "ended")
        self._publish(Topics.BRAIN_STATUS, {"provider": "live", "status": "live_ended", "detail": self.end_reason[:60]})


class MicTrack:
    """aiortc audio track fed by LiveAudio (20 ms, 48 kHz, mono s16). Built lazily (aiortc is optional)."""

    def __new__(cls, audio: Any) -> Any:  # noqa: D102
        from aiortc import MediaStreamTrack  # noqa: PLC0415

        class _Mic(MediaStreamTrack):
            kind = "audio"

            def __init__(self) -> None:
                super().__init__()
                self._pts = 0

            async def recv(self) -> Any:
                import av  # noqa: PLC0415

                pcm = await audio.read_frame()
                frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = 48000
                frame.pts = self._pts
                frame.time_base = fractions.Fraction(1, 48000)
                self._pts += pcm.shape[-1]
                return frame

        return _Mic()
