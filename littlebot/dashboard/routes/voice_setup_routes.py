"""The voice pipeline's choices from the Assistant page (``/api/assistant/setup``): what listens first, what listens
alongside it to compare, what thinks (and takes the hard questions), what speaks and in which Cartesia voice, and
which voice the wake word starts (core/settings.py ``CONFIG_OVERRIDES``, docs/ASSISTANT_SPEECH.md).

A choice is kept on the robot and used instead of config/local.yaml's until "Back to the config file" (``/reset``);
one equal to the file's isn't kept at all, so the file decides again. It is used at once, or when the conversation
going on ends. Cartesia's voice library is searched from the robot (``/voices``) and a voice can be heard on
LittleBot's speaker before it is chosen (``/voices/try``). Keys never reach this page: only whether each one is set.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from littlebot.core.settings import CONFIG_OVERRIDES
from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body
from littlebot.dashboard.scrub import scrub

router = APIRouter(prefix="/api/assistant/setup", tags=["assistant"])

# what the page calls each choice -> its setting
SETTING = {"pipeline": "assistant.pipeline", "stt": "assistant.stt", "stt_shadow": "assistant.stt_shadow",
           "log_words": "assistant.log_words", "tts": "assistant.tts", "cartesia_voice": "assistant.cartesia_voice",
           "brain": "claude.model", "deep_model": "assistant.deep_model"}
KEYS = ("deepgram", "cartesia", "assemblyai", "openai", "elevenlabs", "anthropic")
VOICE_ID = r"^[A-Za-z0-9_-]{6,64}$"
SAMPLE = "Hi, I'm {name}. This is how I would sound, answering your questions."
SAY_WHY = {
    "busy": (409, "LittleBot is in a conversation: try it again when that ends."),
    "no_audio": (503, "LittleBot's speaker isn't running."),
    "no_key": (409, "No Cartesia key on the robot: add it on the Keys page."),
    "cloud_off": (403, "Cloud communication is off (Privacy page)."),
    "auth": (502, "Cartesia refused the robot's key: check it on the Keys page."),
    "quota": (502, "Cartesia's credits are used up: top them up on cartesia.ai."),
    "rate_limit": (502, "Cartesia is busy right now: try again in a moment."),
    "bad_request": (404, "Cartesia doesn't know that voice."),
    "timeout": (504, "Cartesia didn't answer in time."),
    "network": (502, "Couldn't reach Cartesia."),
}
_voices: dict[str, dict[str, Any]] = {}  # voice id -> what Cartesia says about it (names for the page)
_MAX_KEPT = 500


class SetupBody(Body):
    pipeline: Literal["auto", "cascade", "live"] | None = None
    listen: Literal["deepgram", "cartesia", "assemblyai", "openai", "vosk"] | None = None  # first; the rest behind
    compare: list[Literal["deepgram", "cartesia", "assemblyai"]] | None = Field(default=None, max_length=3)
    log_words: bool | None = None
    speak: Literal["cartesia", "elevenlabs", "openai", "local"] | None = None  # first; the rest behind
    cartesia_voice: str | None = Field(default=None, pattern=VOICE_ID)
    brain: Literal["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5"] | None = None
    deep_model: Literal["off", "claude-sonnet-5-5", "claude-opus-5-5"] | None = None


class TryBody(Body):
    id: str = Field(..., pattern=VOICE_ID)


def _setup(dash: DashState) -> dict[str, Any]:
    ctx = dash.ctx
    settings, defaults = ctx.settings, ctx.settings.defaults()
    choices = {name: {"value": settings.get(key), "file": defaults[key], "changed": settings.is_stored(key)}
               for name, key in SETTING.items()}
    svc = getattr(ctx, "assistant", None)
    st: dict[str, Any] = {}
    if svc is not None:
        try:
            st = svc.status()
        except Exception:  # noqa: BLE001 - the choices still show
            log.exception("assistant status failed")
    voice = _voices.get(str(choices["cartesia_voice"]["value"]))
    return scrub({
        "choices": choices,
        "keys": {name: bool(ctx.config.secret(f"{name}_api_key")) for name in KEYS},
        "cloud_ok": bool(ctx.privacy.cloud_enabled and not ctx.config.is_offline),
        "running": svc is not None,
        "live": {"pipeline": st.get("pipeline"), "reason": st.get("pipeline_reason"), "active": bool(st.get("active")),
                 "waiting": bool(st.get("choices_waiting")), "stt": st.get("stt"), "tts": st.get("tts")},
        "voice": voice,
    })


def _choose(dash: DashState, name: str, value: Any) -> None:
    """Keep ``value``, or forget the choice when it is the config file's (the file decides again)."""
    settings, key = dash.ctx.settings, SETTING[name]
    if value == settings.defaults()[key]:
        settings.reset(key, source="dashboard")
    else:
        settings.set(key, value, source="dashboard")


def _first(name: str, first: str, dash: DashState) -> list[str]:
    """``first`` ahead of the config file's order (the others stay behind it as backups)."""
    rest = [n for n in dash.ctx.settings.defaults()[SETTING[name]] if n != first]
    return [first, *rest]


def _cartesia_key(dash: DashState) -> str:
    key = dash.ctx.config.secret("cartesia_api_key")
    if not key:
        raise HTTPException(status_code=409, detail=SAY_WHY["no_key"][1])
    if dash.ctx.config.is_offline or not dash.ctx.privacy.cloud_enabled:
        raise HTTPException(status_code=403, detail=SAY_WHY["cloud_off"][1])
    return key


def _keep(voices: list[dict[str, Any]]) -> None:
    for v in voices:
        if len(_voices) >= _MAX_KEPT:
            _voices.pop(next(iter(_voices)))
        _voices[v["id"]] = v


def _failed(kind: str) -> HTTPException:
    code, text = SAY_WHY.get(kind, (502, "Cartesia didn't answer."))
    return HTTPException(status_code=code, detail=text)


@router.get("")
async def setup(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _setup(dash)


@router.put("")
async def choose(body: SetupBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    given = body.model_dump(exclude_none=True)
    if not given:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    if body.pipeline is not None:
        _choose(dash, "pipeline", body.pipeline)
    if body.listen is not None:
        _choose(dash, "stt", _first("stt", body.listen, dash))
    if body.compare is not None:
        _choose(dash, "stt_shadow", list(dict.fromkeys(body.compare)))
    if body.log_words is not None:
        _choose(dash, "log_words", body.log_words)
    if body.speak is not None:
        _choose(dash, "tts", _first("tts", body.speak, dash))
    if body.cartesia_voice is not None:
        _choose(dash, "cartesia_voice", body.cartesia_voice)
    if body.brain is not None:
        _choose(dash, "brain", body.brain)
    if body.deep_model is not None:
        _choose(dash, "deep_model", None if body.deep_model == "off" else body.deep_model)
    log.info("dashboard: voice choices changed: %s", ", ".join(f"{k}={v}" for k, v in given.items()))
    return _setup(dash)


@router.post("/reset")
async def reset(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Every choice back to config/local.yaml's."""
    for key in CONFIG_OVERRIDES:
        dash.ctx.settings.reset(key, source="dashboard")
    log.info("dashboard: voice choices back to the config file's")
    return _setup(dash)


@router.get("/voices")
async def voices(q: str = Query(default="", max_length=80),
                 gender: Literal["", "feminine", "masculine", "gender_neutral"] = "",
                 mine: bool = False, after: str | None = Query(default=None, pattern=VOICE_ID),
                 dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """A page of Cartesia's voices (asked by the robot, with its key), ``q`` searching names and descriptions."""
    from littlebot.assistant.speech import SpeechError  # noqa: PLC0415
    from littlebot.assistant.tts.cartesia import list_voices  # noqa: PLC0415

    key = _cartesia_key(dash)
    try:
        res = await list_voices(key, query=q, gender=gender or None, mine=mine, after=after,
                                language=dash.ctx.config.assistant.language or None)
    except SpeechError as e:
        raise _failed(e.kind) from None
    _keep(res["voices"])
    return scrub(res)


@router.get("/voice")
async def current_voice(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """The Cartesia voice in use, with its name (asked once, then remembered)."""
    from littlebot.assistant.speech import SpeechError  # noqa: PLC0415
    from littlebot.assistant.tts.cartesia import get_voice  # noqa: PLC0415

    vid = str(dash.ctx.settings.get(SETTING["cartesia_voice"]) or "")
    if vid in _voices:
        return scrub(_voices[vid])
    try:
        voice = await get_voice(_cartesia_key(dash), vid)
    except HTTPException:
        return {"id": vid, "name": None}
    except SpeechError as e:
        return {"id": vid, "name": None, "error": SAY_WHY.get(e.kind, (0, "Cartesia didn't answer."))[1]}
    _keep([voice])
    return scrub(voice)


@router.post("/voices/try")
async def try_voice(body: TryBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """LittleBot says a line in that voice (a few Cartesia credits), to hear it before choosing it."""
    svc = getattr(dash.ctx, "assistant", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="The assistant isn't running.")
    _cartesia_key(dash)
    res = await svc.try_voice(body.id, SAMPLE.format(name=dash.ctx.config.robot_name))
    if not res.get("ok"):
        raise _failed(str(res.get("error") or ""))
    return {"ok": True}
