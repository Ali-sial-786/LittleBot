"""Let ChatGPT look through LittleBot's camera to answer one question ("how many fingers?").

Privacy (the parent's standing rules): a photo is used ONLY when someone asks a question that needs
it, one still frame per question, and only when the parent turned on the AI camera switch
(``privacy.ai_camera_enabled``, off by default) with the camera and cloud switches on. The JPEG lives
in memory for this one request and is dropped right after; it is never written to disk, logged or
kept. Taking it is visible: the face peeks and ``vision.photo`` is published (only ``{asked_by}``:
never the question or the image).

Speed (a look used to take ~0.1 s for the photo + 1.2-1.8 s for ChatGPT, after the voice's "let me
look"):

* during a live conversation the camera keeps one recent photo warm in memory
  (``keep_warm``/``VisionService.keep_photo_warm``, only while the switches above allow looking),
  refreshed with every frame it reads, so a look doesn't wait for the camera; the connection to
  ChatGPT is opened ahead of time (and the sign-in renewed ahead of time), and the finger counter is
  loaded and warmed up;
* "how many fingers?" is counted on the robot when the optional hand models are installed
  (``littlebot.vision.hands``, ~0.1 s): when it is sure within ``COUNT_HEAD_START_S`` the photo never
  leaves the robot. When it is not sure, ChatGPT answers as before; when it is slow, ChatGPT is asked
  at the same time and the first sure answer wins;
* ChatGPT gets a lighter request: low image detail (a 512 px photo: that is all "low" looks at) and
  no reasoning for simple questions, high detail (and minimal reasoning) for counting or reading
  (``chatgpt.look_models`` / ``look_effort`` / ``look_count_effort`` / ``look_detail`` tune this); the
  answer comes first in the reply and ``on_first`` hands over the first sentence while the rest is
  still streaming.

Every look logs one ``camera look timing`` line (labels and milliseconds only) and returns the same
figures as ``timing``: where the photo came from and how old it was, the robot's count, and ChatGPT's
model, detail, effort, upload size, time to the first sentence and in all.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import logging
import re
import time
from typing import Any, Callable

from littlebot.brain.providers.base import (
    AuthenticationError, BaseProvider, NetworkError, ProviderError, ProviderTimeoutError, RateLimitError,
)
from littlebot.chatgpt import get_auth
from littlebot.chatgpt.auth import MESSAGES
from littlebot.core.events import Topics
from littlebot.safety.content import sanitize_user_text, validate_reply

log = logging.getLogger("littlebot.chatgpt")

_KEY = "camera_look"
MAX_WORDS = 40
DEFAULT_QUESTION = "What do you see?"
FALLBACK = "I'm not sure what I see. Can you show me again?"
PHOTO_MAX_WIDTH = 640  # the camera gives 640x480: the finger counter and "high" detail (512 px tiles) use it all
LOW_DETAIL_WIDTH = 512  # "low" detail looks at one 512 px view: 640 px would only be ~60% more bytes to upload
PHOTO_QUALITY = 75
PHOTO_TIMEOUT_S = 3.0
REPLY_TIMEOUT_S = 20.0
# A warm photo (kept during a live call) is used when it is at most this old. Hands move while
# someone asks "how many fingers?", so for questions about hands only a very recent one will do.
WARM_MAX_AGE_S = 1.0
WARM_MAX_AGE_HANDS_S = 0.35
# Refresh period of the warm photo: every frame the camera loop reads (camera.detect_fps, 6/s by default:
# one ~1 ms JPEG encode each). At 0.3 s it was refreshed every other frame (0.33 s), just under
# WARM_MAX_AGE_HANDS_S, so any delay made a hand question wait for a new photo (~50-100 ms with a 15 fps camera).
WARM_EVERY_S = 0.15
# "How many fingers?": the robot's own count gets this long on its own (a warm count takes ~0.05-0.1 s on a
# Pi 5). Sure by then: the answer, and the photo never leaves the robot. Not sure: ChatGPT is asked at once.
# Still counting (a cold or busy robot): ChatGPT is asked too, and the first sure answer wins. 0 asks both at
# once every time (an unsure count then costs nothing more, but every finger photo is sent to ChatGPT).
COUNT_HEAD_START_S = 0.15
# Reasoning effort per look (the endpoint falls back to the next costlier one when a model refuses
# one, see ChatGPTProvider.reply); chatgpt.look_effort / chatgpt.look_count_effort override them.
LOOK_EFFORT = "none"  # most questions: "what colour is this?", "how many fingers?", "what am I holding?"
COUNT_EFFORT = "minimal"  # reading and tiny things: a little thinking helps
KEEP_ALIVE_S = 50.0  # while warm: re-touch the ChatGPT connection this often so it is never cold
# ChatGPT's first word usually comes 0.9-2.1 s after the photo is sent; now and then it takes 10 s or more
# (seen on the robot). With no word after HEDGE_AFTER_S the same question goes again on a second connection,
# and whichever starts answering first is used (the other is cancelled). It costs a request only then.
HEDGE_AFTER_S = 2.5

# Questions that need the photo's fine detail: reading and small things. Counting doesn't: on the robot,
# "how many fingers?" with high detail and a little thinking took 1.7-2.1 s to ChatGPT's first word, with
# low detail and none 0.9-1.6 s; our photo is only 640x480, so "high" sees just 1.25x more anyway.
NEEDS_DETAIL = re.compile(r"\b(?:read\w*|letters?|words?|written|writing|says?|spell\w*|tiny|small|little)\b",
                          re.I)
# Questions about hands: something may still be moving into place, so the photo must be fresh.
ABOUT_HANDS = re.compile(r"\b(?:fingers?|hands?|holding|hold(?:s|ing)? up|showing|show(?:s|ing)?|wav\w*|doing|"
                         r"pointing)\b", re.I)
# "How many fingers am I holding up?": the robot can count those itself (littlebot.vision.hands).
COUNT_FINGERS = re.compile(r"\bhow many fingers\b|\bcount (?:my|our|his|her|their|the|these|those) fingers\b", re.I)
NOT_FINGERS = re.compile(r"\b(?:toes?|paws?|claws?|feet|foot|legs?)\b", re.I)
MIN_FIRST_WORDS = 2  # "Hmm..." or "Oh!" alone is not worth passing on early: wait for the next sentence
_SENTENCE_END = re.compile(r"(?<!\d)[.!?]+[\"')\]]*(?=\s|$)|[.!?]+[\"')\]]*(?=\s)")

# Friendly, parent-facing messages (no provider text is ever shown).
ERRORS: dict[str, str] = {
    # why looking is not possible right now (blocked_reason)
    "offline": "The robot is in offline mode, so ChatGPT cannot look through the camera.",
    "cloud_off": "Cloud features are off in Privacy. Turn them on to let ChatGPT look through the camera.",
    "camera_off": "The camera is turned off in Privacy.",
    "ai_camera_off": "Letting ChatGPT see the camera is off in Privacy.",
    "not_signed_in": MESSAGES["not_signed_in"],
    "no_camera": "No camera is working right now.",
    # what can go wrong while looking
    "busy": "LittleBot is already looking. Try again in a moment.",
    "photo_failed": "LittleBot could not take a photo. Try again.",
    "timeout": "ChatGPT took too long to answer. Try again.",
    "network": "Could not reach ChatGPT. Check the robot's internet connection.",
    "rate_limited": "ChatGPT is busy right now. Try again in a little while.",
    "usage_limit": "Your ChatGPT plan's usage limit was reached. Try again later.",
    "expired": MESSAGES["expired"],
    "auth": "ChatGPT did not accept the sign-in. Sign in again on the AI page.",
    "plan_not_included": "Your ChatGPT plan does not include this.",
    "refused": "ChatGPT would not answer that. Try asking another way.",
    "provider_error": "ChatGPT could not answer. Try again.",
}

# Its own instructions, not the chat prompt: that one says never to ask for photos, which made ChatGPT
# answer "I can't tell" on the robot. Safety still holds: the answer goes through validate_reply like every
# reply. Short, and the answer first: the robot starts saying the first sentence while the rest is still
# arriving.
LOOK_SYSTEM = """\
You are the eyes of {robot}, a small toy robot with a camera. The photo was taken just now because someone \
asked it to look; a family with a very young child and a grown-up are usually in front of it.

Reply with the answer first, in one short sentence if you can (never more than {words} words), in plain \
friendly words the robot can say out loud.
- When asked to count (for example how many fingers are held up), count carefully and say the number.
- Describe what is really there: hands, toys, objects, colours, actions. Read out text when asked (a book, a \
box, a label, a sign).
- If the photo is too dark or blurry, or the thing asked about isn't in the picture, say so (for example \
"I can't see your hand, hold it up in front of me") instead of guessing.
- Never say who a person is, and never guess anyone's name, age or location. Don't read out private details \
such as addresses, phone numbers or card numbers.
- No lists, emoji or links."""

QUESTION = "Look at this photo from your camera. {question}"

_ASKED_BY = re.compile(r"[a-z][a-z0-9_]{0,23}")


class CameraLook:
    """One photo per question, answered on the robot or by the ChatGPT text model. Shared via ``get_look(ctx)``."""

    def __init__(self, ctx: Any, *, provider: BaseProvider | None = None, finger_counter: Any = None,
                 effort: str | None = None, count_effort: str | None = None) -> None:
        self.ctx = ctx
        self._provider = provider
        self._spare: BaseProvider | None = None  # a second connection for slow answers (HEDGE_AFTER_S)
        self._busy = False
        self._day = _dt.date.today()
        self._photos_today = 0
        self._last_at: float | None = None
        self._counter = finger_counter  # None: loaded on first use when the models are installed
        cfg = getattr(ctx.config, "chatgpt", None)
        self.effort = effort or getattr(cfg, "look_effort", None) or LOOK_EFFORT
        self.count_effort = count_effort or getattr(cfg, "look_count_effort", None) or COUNT_EFFORT
        self.detail = getattr(cfg, "look_detail", None) or "auto"  # auto | low | high
        self._warm_owners: set[str] = set()
        self._keep_alive: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._last_request = 0.0

    @property
    def provider(self) -> BaseProvider:
        if self._provider is None:
            from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

            cfg = self.ctx.config.chatgpt
            models = list(dict.fromkeys([*getattr(cfg, "look_models", []), *cfg.models]))  # look_models first
            self._provider = ChatGPTProvider(get_auth(self.ctx), models=models, effort=cfg.reasoning_effort)
        return self._provider

    def _spare_provider(self) -> BaseProvider:
        """The provider for a second try: its own connection when it is ChatGPT's (the slow one may be stuck)."""
        if self._spare is None:
            from littlebot.chatgpt.responses import ChatGPTProvider  # noqa: PLC0415

            main = self.provider
            if isinstance(main, ChatGPTProvider):
                cfg = self.ctx.config.chatgpt
                self._spare = ChatGPTProvider(get_auth(self.ctx), models=list(main.models),
                                              effort=cfg.reasoning_effort)
            else:
                self._spare = main
        return self._spare

    # ------------------------------------------------------------ gates
    def blocked_reason(self) -> str | None:
        ctx = self.ctx
        privacy = ctx.privacy
        if ctx.config.is_offline:
            return "offline"
        if not privacy.cloud_enabled:
            return "cloud_off"
        if not privacy.camera_enabled:
            return "camera_off"
        if not getattr(privacy, "ai_camera_enabled", False):
            return "ai_camera_off"
        if not self.provider.configured():
            return "not_signed_in"
        vision = ctx.vision
        if vision is None or not hasattr(vision, "capture_photo"):
            return "no_camera"
        try:
            camera = (vision.status() or {}).get("camera")
        except Exception:  # noqa: BLE001
            camera = None
        if camera not in ("ok", "starting"):
            return "no_camera"
        return None

    def may_keep_warm(self) -> bool:
        """The consent part of ``blocked_reason`` (cheap, thread-safe: the vision thread asks it)."""
        p = self.ctx.privacy
        return (not self.ctx.config.is_offline and bool(p.cloud_enabled) and bool(p.camera_enabled)
                and bool(getattr(p, "ai_camera_enabled", False)))

    def status(self) -> dict[str, Any]:
        """``enabled`` is the parent's AI camera switch; ``blocked`` says why looking is not possible now."""
        self._roll_day()
        reason = self.blocked_reason()
        return {
            "enabled": bool(getattr(self.ctx.privacy, "ai_camera_enabled", False)),
            "blocked": reason,
            "blocked_detail": ERRORS.get(reason) if reason else None,
            "photos_today": self._photos_today,
            "last_at": self._last_at,
        }

    # ------------------------------------------------------------ keep warm
    def keep_warm(self, on: bool, owner: str = "live") -> None:
        """Get ready to look at once (a live conversation is on), or stop (it ended).

        On: the camera keeps one recent photo in memory while the consent switches allow looking
        (checked before every refresh), the connection to ChatGPT is opened and kept open, and the
        finger counter is loaded and warmed up. Off (last owner): the photo is dropped and the
        keep-alive stops."""
        vision = self.ctx.vision
        if on:
            self._warm_owners.add(owner)
        else:
            self._warm_owners.discard(owner)
        if vision is not None and hasattr(vision, "keep_photo_warm"):
            try:
                if on:
                    vision.keep_photo_warm(owner, True, allowed=self.may_keep_warm, every=WARM_EVERY_S,
                                           width=PHOTO_MAX_WIDTH, quality=PHOTO_QUALITY)
                else:
                    vision.keep_photo_warm(owner, False)
            except Exception:  # noqa: BLE001 - never break the conversation over this
                log.debug("camera look: keep_photo_warm failed", exc_info=True)
        if not on:
            if not self._warm_owners and self._keep_alive is not None:
                self._keep_alive.cancel()
                self._keep_alive = None
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._keep_alive is None or self._keep_alive.done():
            self._keep_alive = loop.create_task(self._keep_connection_alive(), name="camera-look-keepalive")
        if self._counter is None and self.may_keep_warm():
            self._run_background(asyncio.to_thread(self._finger_counter))

    async def _keep_connection_alive(self) -> None:
        """While warm: open the ChatGPT connection now, then touch it whenever it sat idle for KEEP_ALIVE_S.
        The sign-in is renewed here too when it is about to expire, so a look never waits for that."""
        while True:
            try:
                if self.may_keep_warm() and self.provider.configured():
                    await self._renew_sign_in()
                    if time.monotonic() - self._last_request >= KEEP_ALIVE_S:
                        await self._open_connection()
            except Exception:  # noqa: BLE001 - only a head start; looking works without it
                log.debug("camera look: keeping the connection open failed", exc_info=True)
            await asyncio.sleep(KEEP_ALIVE_S / 5)

    async def _renew_sign_in(self) -> None:
        auth = getattr(self.provider, "auth", None)
        if auth is None or not hasattr(auth, "bearer"):
            return
        try:
            await auth.bearer()  # refreshes the token only in its last 5 minutes; otherwise instant
        except Exception:  # noqa: BLE001 - the look itself reports a sign-in problem
            log.debug("camera look: renewing the sign-in ahead of time failed", exc_info=True)

    async def _open_connection(self) -> None:
        warm = getattr(self.provider, "warm", None)
        if warm is None:
            return
        t0 = time.monotonic()
        try:
            ok = await warm()
        except Exception:  # noqa: BLE001 - only a head start
            ok = False
        self._last_request = time.monotonic()
        log.info("camera look: ChatGPT connection %s in %d ms", "ready" if ok else "not opened",
                 int((self._last_request - t0) * 1000))

    def _run_background(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    def _finger_counter(self) -> Any:
        if self._counter is None:
            try:
                from littlebot.vision.hands import load_finger_counter  # noqa: PLC0415
            except ImportError:
                return None
            self._counter = load_finger_counter()
        return self._counter

    # ------------------------------------------------------------ look
    async def look(self, question: str, *, asked_by: str = "parent",
                   on_first: Callable[[str], Any] | None = None) -> dict[str, Any]:
        """Answer ``question`` from one photo:
        ``{ok, answer, error, detail, latency_ms, source, sent, first, rest, timing}``.

        ``on_first(text)``: called once, as early as possible, with the answer's first sentence
        (already checked by ``validate_reply``) while ChatGPT is still writing the rest. Then
        ``first`` is that sentence and ``rest`` the checked remainder (or None): the two together say
        what ``answer`` says. ``source``: "robot" (counted here) or "chatgpt"; ``sent``: whether the
        photo went to ChatGPT. ``timing``: where the time went (labels and milliseconds only, also
        logged; ``timing_text`` makes it one line)."""
        t0 = time.monotonic()
        asked_by = asked_by if isinstance(asked_by, str) and _ASKED_BY.fullmatch(asked_by) else "other"
        timing: dict[str, Any] = {}
        extra: dict[str, Any] = {"source": None, "sent": False, "first": None, "rest": None, "timing": timing}

        def result(answer: str | None = None, error: str | None = None) -> dict[str, Any]:
            return {"ok": error is None, "answer": answer if error is None else None, "error": error,
                    "detail": ERRORS.get(error) if error else None, "latency_ms": int((time.monotonic() - t0) * 1000),
                    **extra}

        def robot(answer: str) -> dict[str, Any]:
            extra.update(source="robot", first=answer)
            log.info("camera look: fingers counted on the robot, %s (asked_by=%s)",
                     "ChatGPT's answer was not needed" if extra["sent"] else "no photo sent", asked_by)
            if on_first is not None:
                _call(on_first, answer)
            return result(answer=answer)

        reason = self.blocked_reason()
        if reason:
            return result(error=reason)
        if self._busy:
            return result(error="busy")
        self._busy = True
        counting: asyncio.Task[str | None] | None = None
        asking: asyncio.Task[Any] | None = None
        try:
            text = sanitize_user_text(question or "", max_chars=300).strip() or DEFAULT_QUESTION
            fingers = bool(COUNT_FINGERS.search(text) and not NOT_FINGERS.search(text))
            detailed = bool(NEEDS_DETAIL.search(text))
            detail = self.detail if self.detail in ("low", "high") else "high" if detailed else "low"
            warm = self._warm_photo(WARM_MAX_AGE_HANDS_S if ABOUT_HANDS.search(text) else WARM_MAX_AGE_S)
            photo = warm[0] if warm else None
            timing["photo"] = "warm" if warm else "new"
            if warm:
                timing["photo_age_ms"] = int(warm[1] * 1000)
            # the counter and "high" detail use the whole frame; "low" detail only looks at 512 px
            width = PHOTO_MAX_WIDTH if warm or fingers or detail == "high" else LOW_DETAIL_WIDTH
            if photo is None:
                try:
                    photo = await self.ctx.vision.capture_photo(max_width=width, quality=PHOTO_QUALITY,
                                                                timeout=PHOTO_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001 - PhotoUnavailable(reason) or a broken camera
                    code = getattr(e, "reason", None)
                    code = code if code in ("camera_off", "no_camera") else "photo_failed"
                    log.warning("camera look: no photo (%s)", getattr(e, "reason", type(e).__name__))
                    timing["photo"] = f"failed:{code}"
                    return result(error=code)
            if not photo:
                return result(error="photo_failed")
            timing["photo_ms"] = _ms(t0)
            self._count_photo()
            self._show(asked_by)
            if fingers:
                counting = asyncio.ensure_future(self._count_fingers(photo, timing))
                await asyncio.wait({counting}, timeout=COUNT_HEAD_START_S)  # the robot's head start
                if counting.done():
                    answer, counting = counting.result(), None
                    if answer is not None:
                        return robot(answer)
            extra["source"] = "chatgpt"
            told: list[str] = []  # who handed over the first sentence while racing: only one of them ever does

            def first_sentence(sentence: str) -> None:
                if not told:
                    told.append("chatgpt")
                    on_first(sentence)  # type: ignore[misc]  # (only used when on_first is set)

            stream = _FirstSentence(first_sentence if counting is not None and on_first is not None else on_first,
                                    t0, timing)
            asking = asyncio.ensure_future(self._ask(photo, width, text, detail, detailed, stream, timing, asked_by))
            extra["sent"], photo = True, None  # the request holds the image now, and drops it when done
            if counting is not None:  # the robot is still counting: the first sure answer wins
                await asyncio.wait({counting, asking}, return_when=asyncio.FIRST_COMPLETED)
                if counting.done() and not asking.done() and not told and counting.result() is not None:
                    told.append("robot")
                    asking.cancel()
                    await asyncio.gather(asking, return_exceptions=True)
                    timing["chatgpt"] = "cancelled"
                    if self._warm_owners:  # cancelling it closed the connection: open the next look's now
                        self._run_background(self._open_connection())
                    return robot(counting.result())
            try:
                reply = await asking
            except (ProviderError, asyncio.TimeoutError, TimeoutError) as e:
                code = _error_code(e)
                log.warning("camera look: ChatGPT failed (%s)%s", code, _why(e))
                timing["chatgpt"] = f"failed:{code}"
                if counting is not None and not told:  # the robot may still be sure
                    answer = await counting
                    if answer is not None:
                        return robot(answer)
                return result(error=code)
            answer = _checked(reply.text or "", MAX_WORDS)
            if answer is None:  # say why in the log (rule names only, never the words)
                checked = validate_reply(_untagged(reply.text or ""), max_words=MAX_WORDS)
                log.info("camera look: answer replaced by the fallback (%s)", ",".join(checked.reasons[:3]) or "empty")
                answer = FALLBACK
            if stream.sent is not None:
                extra["first"] = stream.sent
                extra["rest"] = _checked(stream.rest(reply.text or ""), MAX_WORDS - len(stream.sent.split())) \
                    if answer != FALLBACK else None
            return result(answer=answer)
        finally:
            self._busy = False
            if asking is not None and not asking.done():
                asking.cancel()  # this look was cancelled itself: no request outlives it
            if timing:
                self._log_timing(t0, timing)

    async def _ask(self, photo: bytes, width: int, text: str, detail: str, detailed: bool, stream: "_FirstSentence",
                   timing: dict[str, Any], asked_by: str) -> Any:
        """The ChatGPT request for one look (a ``width`` px photo, made smaller first when ChatGPT only looks
        at 512 px)."""
        kwargs = self._request_options(detail=detail, effort=self.count_effort if detailed else self.effort,
                                       on_delta=stream.feed)
        if kwargs.get("detail") == "low" and width > LOW_DETAIL_WIDTH:
            from littlebot.vision.imaging import shrink_jpeg  # noqa: PLC0415

            photo = await asyncio.to_thread(shrink_jpeg, photo, LOW_DETAIL_WIDTH, PHOTO_QUALITY)
        timing.update(model=getattr(self.provider, "model", None) or "default", detail=kwargs.get("detail", "default"),
                      effort=kwargs.get("effort", "default"), photo_kb=round(len(photo) / 1024))
        log.info("photo sent to ChatGPT (asked_by=%s)", asked_by)
        t_req = stream.t0 = time.monotonic()  # first_token_ms / first_sentence_ms count from here too
        winner: list[str] = []  # the request whose words come first: only its words reach ``stream``
        spoke = asyncio.Event()

        def feed_for(name: str) -> Callable[[str], None]:
            def feed(piece: str) -> None:
                if not winner:
                    winner.append(name)
                    spoke.set()
                if winner[0] == name:
                    stream.feed(piece)
            return feed

        def request(provider: BaseProvider, name: str) -> "asyncio.Task[Any]":
            opts = {**kwargs, "on_delta": feed_for(name)} if "on_delta" in kwargs else kwargs
            task = asyncio.ensure_future(provider.reply(
                [{"role": "user", "content": QUESTION.format(question=text)}], system=self._system_prompt(),
                max_tokens=200, timeout=REPLY_TIMEOUT_S, images=[photo], **opts))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())  # a loser's error is not news
            return task

        first = request(self.provider, "first")
        second: asyncio.Task[Any] | None = None
        waiting = asyncio.ensure_future(spoke.wait())
        try:
            if "on_delta" in kwargs:  # without streaming there's no first word to wait for
                await asyncio.wait({first, waiting}, timeout=HEDGE_AFTER_S, return_when=asyncio.FIRST_COMPLETED)
            if not first.done() and not winner and "on_delta" in kwargs:
                log.info("camera look: no word from ChatGPT after %.1f s; asking again on a second connection",
                         HEDGE_AFTER_S)
                timing["hedge"] = "sent"
                second = request(self._spare_provider(), "second")
                pending: set[asyncio.Future[Any]] = {first, second, waiting}
                while not winner:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    ok = [t for t in (first, second) if t in done and not t.cancelled() and t.exception() is None]
                    if ok:  # it finished without streaming a word (a short reply): use it
                        winner.append("first" if ok[0] is first else "second")
                    elif pending <= {waiting}:
                        break  # both failed: the first one's error is the answer
                pick = second if winner and winner[0] == "second" else first
                (first if pick is second else second).cancel()
                timing["hedge"] = "won" if pick is second else "lost"
                if pick is second:  # the old connection was the slow one: use the new one from now on
                    self._provider, self._spare = self._spare, self._provider
                reply = await pick
            else:
                reply = await first
        finally:
            waiting.cancel()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()  # this look ended (or was cancelled): no request outlives it
            photo = b""  # drop the image right away (nothing else holds it)
            timing["chatgpt_ms"] = _ms(t_req)
            self._last_request = time.monotonic()
        if getattr(reply, "model", None):
            timing["model"] = reply.model  # the one that answered
        return reply

    async def _count_fingers(self, photo: bytes, timing: dict[str, Any]) -> str | None:
        """The robot's own answer to "how many fingers?", or None (no counter, or it isn't sure)."""
        t = time.monotonic()
        try:
            counter = self._counter if self._counter is not None else await asyncio.to_thread(self._finger_counter)
            if counter is None:
                timing["count"] = "no_models"
                return None
            count = await asyncio.to_thread(counter.count_jpeg, photo)
        except Exception:  # noqa: BLE001 - the counter is optional: ChatGPT answers instead
            log.warning("camera look: finger counter failed", exc_info=True)
            timing["count"] = "failed"
            return None
        finally:
            timing["count_ms"] = _ms(t)
        if not count.sure or count.fingers <= 0:
            timing["count"] = f"unsure:{count.why}"
            return None
        from littlebot.vision.hands import number_word  # noqa: PLC0415

        n = int(count.fingers)
        timing["count"] = f"sure:{n}"
        return f"I see {number_word(n)} finger{'' if n == 1 else 's'} held up!"

    def _warm_photo(self, max_age: float) -> tuple[bytes, float] | None:
        """(JPEG, age s) of the photo the camera keeps warm during a live call, if recent enough."""
        latest = getattr(self.ctx.vision, "latest_photo", None)
        if latest is None or not self._warm_owners:
            return None
        try:
            got = latest(max_age=max_age)
        except Exception:  # noqa: BLE001
            return None
        return (got[0], float(got[1])) if got and got[0] else None

    def _request_options(self, **wanted: Any) -> dict[str, Any]:
        """The per-call options this provider's ``reply`` takes (a plain provider gets none of them)."""
        try:
            params = inspect.signature(self.provider.reply).parameters
        except (TypeError, ValueError):
            return {}
        return {k: v for k, v in wanted.items() if v is not None and k in params}  # named ones only

    def _log_timing(self, t0: float, timing: dict[str, Any]) -> None:
        """One line per look: where the time went (labels and milliseconds only, never content)."""
        log.info("camera look timing: total %d ms | %s", _ms(t0), timing_text(timing))

    # ------------------------------------------------------------ helpers
    def _system_prompt(self) -> str:
        return LOOK_SYSTEM.format(robot=self.ctx.config.robot_name, words=MAX_WORDS)

    def _roll_day(self) -> None:
        today = _dt.date.today()
        if today != self._day:
            self._day, self._photos_today = today, 0

    def _count_photo(self) -> None:
        self._roll_day()
        self._photos_today += 1
        self._last_at = time.time()

    def _show(self, asked_by: str) -> None:
        """Make the photo visible: the face peeks and the dashboard hears about it."""
        face = self.ctx.face
        if face is not None:
            try:
                face.set_expression("curious", hold=2.0)
                face.play_animation("peek")
            except Exception:  # noqa: BLE001 - a face glitch must not stop the answer
                log.debug("camera look: face reaction failed", exc_info=True)
        self.ctx.bus.publish(Topics.VISION_PHOTO, {"asked_by": asked_by}, source="chatgpt")


class _FirstSentence:
    """Collects streamed answer text and hands over the first complete, checked sentence once."""

    def __init__(self, on_first: Callable[[str], Any] | None, t0: float, timing: dict[str, Any]) -> None:
        self.on_first = on_first
        self.t0 = t0
        self.timing = timing
        self.raw = ""
        self.cut = 0  # length of the raw text that was handed over
        self.sent: str | None = None
        self.done = False

    def feed(self, piece: str) -> None:
        if "first_token_ms" not in self.timing:
            self.timing["first_token_ms"] = _ms(self.t0)
        if self.done:
            return
        self.raw += piece
        sentence = next((self.raw[:m.end()] for m in _SENTENCE_END.finditer(self.raw)
                         if len(self.raw[:m.end()].split()) >= MIN_FIRST_WORDS), None)
        if sentence is None:
            return
        self.done = True  # only the first sentence is ever streamed
        checked = _checked(sentence, MAX_WORDS)
        if checked is None or self.on_first is None:
            return
        self.cut, self.sent = len(sentence), checked
        self.timing["first_sentence_ms"] = _ms(self.t0)
        _call(self.on_first, checked)

    def rest(self, full: str) -> str:
        """What ``full`` (the whole reply) says after the sentence that was handed over."""
        head, full = self.raw[:self.cut].strip(), full.strip()
        return full[len(head):] if head and full.startswith(head) else ""


def _call(fn: Callable[[str], Any], text: str) -> None:
    try:
        fn(text)
    except Exception:  # noqa: BLE001 - a listener's bug must not lose the answer
        log.debug("camera look: on_first failed", exc_info=True)


def _untagged(text: str) -> str:
    from littlebot.brain.face_actions import extract_tags  # noqa: PLC0415

    return extract_tags(text)[0]  # a stray [face: ...] tag must never be read out


def _checked(text: str, max_words: int) -> str | None:
    """``text`` as it may be said (validate_reply), or None if it may not or says nothing."""
    if max_words < 3 or not text.strip(" .!?,\n"):
        return None
    checked = validate_reply(_untagged(text), max_words=max_words)
    return checked.text if checked.ok and checked.text.strip() else None


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


def timing_text(timing: dict[str, Any]) -> str:
    """A look's ``timing`` as ``key=value`` pairs, in the order the steps happened. ``photo``: warm (with
    ``photo_age_ms``) or new; ``photo_ms``: until the photo was ready; ``count`` / ``count_ms``: the robot's
    finger count; ``model`` / ``detail`` / ``effort`` / ``photo_kb``: the ChatGPT request; ``first_token_ms`` /
    ``first_sentence_ms`` / ``chatgpt_ms``: counted from that request's start; ``chatgpt``: cancelled or
    failed:<code> when it gave no answer."""
    return " ".join(f"{k}={v}" for k, v in timing.items())


def _error_code(e: BaseException) -> str:
    if isinstance(e, (ProviderTimeoutError, asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(e, NetworkError):
        return "network"
    if isinstance(e, RateLimitError):
        return "usage_limit" if e.kind == "usage_limit" else "rate_limited"
    if isinstance(e, AuthenticationError):
        return e.kind if e.kind in ("not_signed_in", "expired") else "auth"
    kind = getattr(e, "kind", "")
    if kind in ("refused", "plan_not_included"):
        return kind
    return "provider_error"


def _why(e: BaseException) -> str:
    """Why the endpoint said no, for the journal: HTTP status, our kind and the error's type/code/param.

    Only labels the provider already reduced to identifiers (``ProviderError.api_error``); never the
    error message, the question, the photo or a token."""
    if not isinstance(e, ProviderError):
        return ""
    api = getattr(e, "api_error", None) or {}
    status = e.status_code if e.status_code is not None else api.get("status")
    fields = {"status": status, "kind": e.kind, **{k: api.get(k) for k in ("type", "code", "param")}}
    shown = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    return f" [{shown}]" if shown else ""


def get_look(ctx: Any) -> CameraLook:
    """The one CameraLook shared by live voice and the dashboard (``ctx.extras["camera_look"]``)."""
    look = ctx.extras.get(_KEY)
    if look is None:
        look = ctx.extras[_KEY] = CameraLook(ctx)
    return look


__all__ = ["ERRORS", "CameraLook", "get_look", "timing_text"]
