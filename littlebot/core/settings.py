"""Parent-editable runtime settings, persisted in SQLite.

Static config (YAML) supplies defaults; anything a parent changes from the
dashboard is stored here and survives restarts. Every change publishes
``settings.changed`` so modules can react immediately (e.g. camera off).

Keys are dotted strings. Known keys and their defaults are registered in
``DEFAULTS``; unknown keys are rejected so typos cannot silently create
settings.
"""

from __future__ import annotations

import copy
import datetime as _dt
import logging
import threading
from typing import Any, Callable

from littlebot.core.config import LittleBotConfig
from littlebot.core.events import EventBus, Topics
from littlebot.core.storage import Database

log = logging.getLogger(__name__)

_PREFIX = "setting:"


def build_defaults(cfg: LittleBotConfig) -> dict[str, Any]:
    return {
        # privacy
        "privacy.camera_enabled": cfg.privacy.camera_enabled,
        "privacy.microphone_enabled": cfg.privacy.microphone_enabled,
        "privacy.cloud_enabled": cfg.privacy.cloud_enabled and not cfg.is_offline,
        "privacy.record_history": cfg.activities.record_history,
        # Let ChatGPT see one still photo when someone asks a question that needs it. Off until a
        # parent opts in; also needs the camera and cloud switches (never in offline mode).
        "privacy.ai_camera_enabled": False,
        # Recognise the family members a parent added (littlebot/vision/faces.py), on the robot. Off until a
        # parent opts in; also needs the camera switch.
        "privacy.face_recognition_enabled": False,
        # Recognise the voices of the family members a parent added (littlebot/voices/), on the robot: only
        # voiceprints are kept, never recordings. Off until a parent opts in; also needs the microphone switch.
        "privacy.voice_recognition_enabled": False,
        # audio
        "audio.volume": cfg.audio.speaker.volume,
        "audio.tts_rate": cfg.audio.tts.rate,
        "audio.input_gain": cfg.audio.microphone.input_gain,
        # wake word: "Hey LittleBot" starts live voice (heard on the robot, offline)
        "wake.enabled": cfg.wake.enabled,
        # ai
        "ai.enabled": False,  # cloud conversation, parent must opt in
        "ai.provider": cfg.ai.provider,
        "ai.max_words": cfg.ai.max_words,
        "ai.max_requests_per_hour": cfg.ai.max_requests_per_hour,
        "ai.max_requests_per_day": cfg.ai.max_requests_per_day,
        "ai.daily_budget_usd": cfg.ai.daily_budget_usd,
        "ai.share_child_name": cfg.ai.share_child_name,  # send nickname to cloud AI only with opt-in
        "ai.share_interests": cfg.ai.share_interests,  # favourite animals / interests in cloud prompt
        # media
        "media.enabled": True,
        "media.volume": cfg.media.default_volume,
        "media.session_limit_minutes": cfg.media.session_limit_minutes,
        "media.daily_limit_minutes": cfg.media.daily_limit_minutes,
        "media.allow_under_min_age": False,  # explicit parent authorisation for <18 months
        # vision
        "vision.follow_faces": True,
        "vision.gestures_enabled": cfg.vision.gestures_enabled,
        # motion
        "motion.physical_armed": False,  # never persisted as True across restarts (see Settings.load)
        # face
        "face.expression_default": "neutral",
        # screen brightness 0.15..1 (a left-edge swipe on the face; littlebot/face/edge_controls.py)
        "display.brightness": 1.0,
        # the idle face dozes (droopy eyes, a "zzz") when nothing is happening; only a look, the robot stays
        # awake and listening (littlebot/face/doze.py). Someone walking in wakes it for a while.
        "face.doze_enabled": True,
        "face.doze_after_seconds": 30,
        "face.awake_on_arrival_seconds": 20,
        # system
        "system.sleep_after_idle_minutes": cfg.system.sleep_after_idle_minutes,
        # live information (littlebot/info): the weather's home place as the parent chose it on the
        # dashboard ({} = config ``info.location``, else from the internet connection), and the units
        "info.location": {},
        "info.units": cfg.info.units,
        # info cards on the screen during voice chats (littlebot/screens), and whether they may show
        # pictures from the internet (strict safe search, never kept)
        "screens.enabled": True,
        "screens.web_pictures": True,
        # the browser view: Google (SafeSearch on) on the screen when a grown-up explicitly asks (littlebot/browser)
        "browser.enabled": True,
        # ... and whether it keeps its cookies (Google's "you're a person"), history and cache between searches
        # (data/browser-profile); off: a fresh browser that is deleted afterwards
        "browser.keep_signins": False,
        # fast decision models (littlebot/decide): the provider (off | jev | openai | claude) and each feature's mode
        # (off | shadow | on); {} = the config's decide.modes / decide.default_mode
        "decide.provider": cfg.decide.provider,
        "decide.modes": {},
        "decide.log_context": cfg.decide.log_context,  # each logged decision also says what was heard / recognised
        # the battery (littlebot/power; only used while a battery monitor is fitted): when LittleBot says it needs
        # charging (and whether it says it out loud), and when it says goodbye and shuts down cleanly
        "power.low_percent": cfg.power.low_percent,
        "power.critical_percent": cfg.power.critical_percent,
        "power.announce_low": True,
        "power.shutdown_when_empty": True,
        # child profile (minimum necessary information; all optional)
        "profile": {
            "nickname": "",
            "birth_month": "",  # "YYYY-MM"
            "age_months": None,  # manual override when birth month not given
            "language": "en",
            "favourite_animals": [],
            "favourite_songs": [],
            "interests": [],
            "remembered": [],  # what a grown-up asked the voice chat to remember (littlebot/brain/remembered.py)
            "activity_preferences": {},  # {activity_id: {"enabled": bool, "favourite": bool}}
        },
    }


# Settings that are deliberately reset to default at every start.
_NON_PERSISTENT = {"motion.physical_armed"}


class Settings:
    def __init__(self, db: Database, bus: EventBus, defaults: dict[str, Any]) -> None:
        self._db = db
        self._bus = bus
        self._defaults = copy.deepcopy(defaults)
        self._values: dict[str, Any] = copy.deepcopy(defaults)
        self._lock = threading.RLock()
        self._validators: dict[str, Callable[[Any], Any]] = {}
        self.load()

    def load(self) -> None:
        stored = self._db.kv_items(_PREFIX)
        with self._lock:
            for full_key, value in stored.items():
                key = full_key[len(_PREFIX):]
                if key in self._defaults and key not in _NON_PERSISTENT:
                    self._values[key] = value

    def register_validator(self, key: str, fn: Callable[[Any], Any]) -> None:
        """fn(value) -> cleaned value, or raise ValueError."""
        self._validators[key] = fn

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._values:
                return copy.deepcopy(self._values[key])
        return default

    def set(self, key: str, value: Any, *, source: str = "") -> Any:
        if key not in self._defaults:
            raise KeyError(f"unknown setting {key!r}")
        if key in self._validators:
            value = self._validators[key](value)
        else:
            value = _coerce_like(self._defaults[key], value, key)
        with self._lock:
            old = self._values.get(key)
            self._values[key] = copy.deepcopy(value)
        if key not in _NON_PERSISTENT:
            self._db.kv_set(_PREFIX + key, value)
        if old != value:
            self._bus.publish(Topics.SETTINGS_CHANGED, {"key": key, "value": value, "old": old}, source=source or "settings")
        return value

    def update(self, values: dict[str, Any], *, source: str = "") -> dict[str, Any]:
        return {k: self.set(k, v, source=source) for k, v in values.items()}

    def reset(self, key: str) -> Any:
        self._db.kv_delete(_PREFIX + key)
        return self.set(key, copy.deepcopy(self._defaults[key]))

    def all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._values)

    def defaults(self) -> dict[str, Any]:
        return copy.deepcopy(self._defaults)

    # -- convenience --------------------------------------------------------
    def child_age_months(self, today: _dt.date | None = None) -> int | None:
        return age_months_from_profile(self.get("profile") or {}, today)


def age_months_from_profile(profile: dict[str, Any], today: _dt.date | None = None) -> int | None:
    if profile.get("age_months") not in (None, ""):
        try:
            return max(0, int(profile["age_months"]))
        except (TypeError, ValueError):
            pass
    bm = profile.get("birth_month") or ""
    try:
        year, month = (int(p) for p in str(bm).split("-")[:2])
    except ValueError:
        return None
    today = today or _dt.date.today()
    months = (today.year - year) * 12 + (today.month - month)
    return max(0, months)


def _coerce_like(default: Any, value: Any, key: str) -> Any:
    if isinstance(default, bool):
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
            raise ValueError(f"{key}: expected boolean")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key}: expected integer") from e
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{key}: expected number") from e
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{key}: expected text")
        return value
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{key}: expected object")
        merged = copy.deepcopy(default)
        merged.update(value)
        return merged
    return value
