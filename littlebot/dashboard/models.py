"""Pydantic request bodies for the dashboard API (strict, with length limits)."""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from littlebot.brain.remembered import MAX_LEN as MAX_NOTE_LEN
from littlebot.brain.remembered import MAX_NOTES
from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS, SOUND_NAMES
from littlebot.vision.faces import LABEL_MAX, clean_label

MAX_PROFILE_AGE_MONTHS = 72  # LittleBot is designed for 6 months - 5 years


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def clean_text(value: str, *, max_len: int, field: str = "text", allow_newlines: bool = False) -> str:
    """Normalise unicode, strip control characters and enforce a maximum length."""
    value = unicodedata.normalize("NFC", str(value)).strip()
    out = []
    for ch in value:
        if ch == "\n" and allow_newlines:
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    value = "".join(out).strip()
    if len(value) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return value


# ----------------------------------------------------------------- auth
class SecretBody(BaseModel):
    """Passwords are taken verbatim (no whitespace stripping)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class LoginBody(SecretBody):
    password: str = Field(min_length=1, max_length=256)


class SetupBody(SecretBody):
    setup_code: str = Field(min_length=4, max_length=32)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordBody(SecretBody):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


# ----------------------------------------------------------------- face
class ExpressionBody(Body):
    name: str = Field(max_length=32)
    hold: float | None = Field(default=None, ge=0, le=600)

    @field_validator("name")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in EXPRESSIONS:
            raise ValueError(f"unknown expression; choose one of: {', '.join(EXPRESSIONS)}")
        return v


class BlinkBody(Body):
    double: bool = False


class GazeBody(Body):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    hold: float | None = Field(default=None, ge=0, le=60)


class AnimationBody(Body):
    name: str = Field(max_length=32)

    @field_validator("name")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in FACE_ANIMATIONS:
            raise ValueError(f"unknown animation; choose one of: {', '.join(FACE_ANIMATIONS)}")
        return v


class FaceSleepBody(Body):
    """Dozing (an idle look, littlebot/face/doze.py) and the deeper sleep after a long quiet spell."""

    doze_enabled: bool | None = None
    doze_after_seconds: int | None = Field(default=None, ge=5, le=3600)
    awake_on_arrival_seconds: int | None = Field(default=None, ge=0, le=3600)
    sleep_after_idle_minutes: float | None = Field(default=None, ge=0, le=720)  # 0: never


# ----------------------------------------------------------------- activities
ACTIVITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class RespondBody(Body):
    response: str = Field(min_length=1, max_length=64)
    prompt_id: str | None = Field(default=None, max_length=64)

    @field_validator("response")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = clean_text(v, max_len=64, field="response")
        if not v:
            raise ValueError("response is empty")
        return v


class ActivityPrefsBody(Body):
    enabled: bool | None = None
    favourite: bool | None = None


# ----------------------------------------------------------------- media
class MediaAddBody(Body):
    url: str = Field(min_length=5, max_length=500)
    title: str | None = Field(default=None, max_length=120)
    # "playlist" keeps the whole playlist of a watch?v=..&list=.. link; None lets the media module decide.
    kind: Literal["video", "playlist"] | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if any(ch.isspace() for ch in v):
            raise ValueError("the link must not contain spaces")
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = clean_text(v, max_len=120, field="title")
        return v or None


class MediaUpdateBody(Body):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = clean_text(v, max_len=120, field="title")
        if not v:
            raise ValueError("title is empty")
        return v


class MediaReorderBody(Body):
    ids: list[int] = Field(min_length=1, max_length=500)


class MediaPlayBody(Body):
    item_id: int = Field(ge=0)


class VolumeBody(Body):
    volume: float = Field(ge=0.0, le=1.0)


class MediaSettingsBody(Body):
    enabled: bool | None = None
    session_limit_minutes: int | None = Field(default=None, ge=1, le=180)
    daily_limit_minutes: int | None = Field(default=None, ge=0, le=600)
    allow_under_min_age: bool | None = None
    # Must be true when switching allow_under_min_age on: the parent has read the warning.
    confirm_under_min_age: bool = False


# ----------------------------------------------------------------- profile
LANGUAGES = {"en": "English", "es": "Español", "fr": "Français", "de": "Deutsch", "it": "Italiano",
             "pt": "Português", "nl": "Nederlands", "ar": "العربية", "ur": "اردو", "hi": "हिन्दी",
             "zh": "中文", "ja": "日本語", "pl": "Polski", "tr": "Türkçe"}
_NAME_RE = re.compile(r"^[\w][\w .'\-]*$", re.UNICODE)


def _clean_list(values: list[str] | None, field: str, *, max_items: int = 12, max_len: int = 40) -> list[str]:
    if values is None:
        return []
    if len(values) > max_items:
        raise ValueError(f"{field}: at most {max_items} items")
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            raise ValueError(f"{field}: items must be text")
        v = clean_text(v, max_len=max_len, field=f"{field} item")
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


class ProfileBody(Body):
    nickname: str | None = Field(default=None, max_length=30)
    birth_month: str | None = Field(default=None, max_length=7)
    age_months: int | None = Field(default=None, ge=0, le=MAX_PROFILE_AGE_MONTHS)
    language: str | None = Field(default=None, max_length=8)
    favourite_animals: list[str] | None = Field(default=None, max_length=12)
    favourite_songs: list[str] | None = Field(default=None, max_length=12)
    interests: list[str] | None = Field(default=None, max_length=12)
    remembered: list[str] | None = Field(default=None, max_length=MAX_NOTES)  # what the voice chat remembers

    @field_validator("nickname")
    @classmethod
    def _nickname(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = clean_text(v, max_len=30, field="nickname")
        if v and not _NAME_RE.match(v):
            raise ValueError("nickname may only contain letters, numbers, spaces, apostrophes and hyphens")
        return v

    @field_validator("birth_month")
    @classmethod
    def _birth_month(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        m = re.fullmatch(r"(\d{4})-(\d{2})", v)
        if not m:
            raise ValueError("birth month must look like YYYY-MM")
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            raise ValueError("birth month: month must be 01-12")
        today = _dt.date.today()
        months = (today.year - year) * 12 + (today.month - month)
        if months < 0:
            raise ValueError("birth month cannot be in the future")
        if months > MAX_PROFILE_AGE_MONTHS:
            raise ValueError("birth month is too long ago (LittleBot is designed for children up to 5 years)")
        return f"{year:04d}-{month:02d}"

    @field_validator("language")
    @classmethod
    def _language(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in LANGUAGES:
            raise ValueError(f"unsupported language; choose one of: {', '.join(LANGUAGES)}")
        return v

    @field_validator("favourite_animals", "favourite_songs", "interests")
    @classmethod
    def _lists(cls, v: list[str] | None, info: object) -> list[str] | None:
        if v is None:
            return None
        return _clean_list(v, getattr(info, "field_name", "list"))

    @field_validator("remembered")
    @classmethod
    def _remembered(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_list(v, "remembered", max_items=MAX_NOTES, max_len=MAX_NOTE_LEN)


# ----------------------------------------------------------------- who's who (littlebot/vision/faces.py)
class PersonBody(Body):
    """Someone LittleBot may recognise on its camera: a name and a short role ("Ali", "dad")."""

    name: str = Field(min_length=1, max_length=LABEL_MAX)
    role: str = Field(default="", max_length=LABEL_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return clean_label(v, field="name")

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        return clean_label(v, field="role", required=False)


class PersonUpdateBody(Body):
    name: str | None = Field(default=None, min_length=1, max_length=LABEL_MAX)
    role: str | None = Field(default=None, max_length=LABEL_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return None if v is None else clean_label(v, field="name")

    @field_validator("role")
    @classmethod
    def _role(cls, v: str | None) -> str | None:
        return None if v is None else clean_label(v, field="role", required=False)


# ----------------------------------------------------------------- AI
class AISettingsBody(Body):
    enabled: bool | None = None
    provider: Literal["mock", "anthropic", "openai", "chatgpt"] | None = None
    max_words: int | None = Field(default=None, ge=3, le=150)
    max_requests_per_hour: int | None = Field(default=None, ge=0, le=600)
    max_requests_per_day: int | None = Field(default=None, ge=0, le=5000)
    daily_budget_usd: float | None = Field(default=None, ge=0.0, le=20.0)
    share_child_name: bool | None = None  # send the nickname to the cloud AI (opt-in)
    share_interests: bool | None = None  # send favourite animals / interests to the cloud AI (opt-in)


class ProviderTestBody(Body):
    provider: Literal["mock", "anthropic", "openai", "chatgpt"]


# ----------------------------------------------------------------- privacy
class PrivacyBody(Body):
    camera_enabled: bool | None = None
    microphone_enabled: bool | None = None
    cloud_enabled: bool | None = None
    record_history: bool | None = None
    # Lets the AI take one photo when someone asks it about what LittleBot sees (off by default).
    ai_camera_enabled: bool | None = None
    # Lets LittleBot recognise the family members a parent added, on the robot (off by default).
    face_recognition_enabled: bool | None = None
    # Lets LittleBot recognise the family's voices, on the robot (off by default; voiceprints only, never recordings).
    voice_recognition_enabled: bool | None = None


# ----------------------------------------------------------------- audio
class AudioSettingsBody(Body):
    volume: float | None = Field(default=None, ge=0.0, le=1.0)
    tts_rate: float | None = Field(default=None, ge=0.5, le=1.5)


class MicTestBody(Body):
    seconds: float = Field(default=2.0, ge=0.5, le=5.0)


class SayBody(Body):
    text: str = Field(min_length=1, max_length=200)

    @field_validator("text")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = clean_text(v, max_len=200, field="text")
        if not v:
            raise ValueError("text is empty")
        return v


class SoundBody(Body):
    name: str = Field(max_length=32)

    @field_validator("name")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in SOUND_NAMES:
            raise ValueError("unknown sound")
        return v


# ----------------------------------------------------------------- motion
class GestureBody(Body):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")


class JointBody(Body):
    joint: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    angle: float = Field(ge=0.0, le=180.0)
    speed_dps: float | None = Field(default=None, gt=0.0, le=360.0)


ARM_PHRASE = "ARM MOTORS"


class ArmBody(Body):
    armed: bool
    confirmation: str = Field(default="", max_length=40)


# ----------------------------------------------------------------- vision / simulator
class FaceSimBody(Body):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    size: float = Field(default=0.25, gt=0.0, le=1.0)


class VisionSettingsBody(Body):
    follow_faces: bool


class SpeechSimBody(Body):
    text: str = Field(min_length=1, max_length=200)
    route: Literal["auto", "brain", "microphone"] = "auto"
    speak: bool = True

    @field_validator("text")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = clean_text(v, max_len=200, field="text")
        if not v:
            raise ValueError("text is empty")
        return v


class CameraSimBody(Body):
    event: Literal["face", "no_face", "person_left"]
    x: float = Field(default=0.5, ge=0.0, le=1.0)
    y: float = Field(default=0.5, ge=0.0, le=1.0)
    size: float = Field(default=0.25, gt=0.0, le=1.0)


# ----------------------------------------------------------------- system
class PowerBody(Body):
    action: Literal["shutdown", "restart"]
    confirm: bool = False


# ----------------------------------------------------------------- the battery (littlebot/power)
class PowerSettingsBody(Body):
    low_percent: int | None = Field(default=None, ge=5, le=50)  # "please charge me" at or below this
    critical_percent: int | None = Field(default=None, ge=2, le=20)  # goodbye and a clean shutdown
    announce_low: bool | None = None  # LittleBot says it out loud (else only the dashboard banner)
    shutdown_when_empty: bool | None = None


class PowerSimBody(Body):
    percent: float | None = Field(default=None, ge=0, le=100)
    charging: bool | None = None
    temperature_c: float | None = Field(default=None, ge=-30, le=100)


# ----------------------------------------------------------------- live voice
class LiveStartBody(Body):
    # The parent confirms they are with the child for the whole conversation.
    parent_present: bool = False


class LookBody(Body):
    # "How many fingers am I holding up?" - one photo is taken and sent with this question.
    question: str | None = Field(default=None, max_length=300)
    speak: bool = False

    @field_validator("question")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return None if v is None else (clean_text(v, max_len=300, field="question") or None)
