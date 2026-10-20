"""Parent privacy controls (camera, microphone, cloud), backed by Settings."""

from __future__ import annotations

from typing import Any

from littlebot.core.events import Event, EventBus, Topics
from littlebot.core.settings import Settings

_KEYS = {
    "camera_enabled": "privacy.camera_enabled",
    "microphone_enabled": "privacy.microphone_enabled",
    "cloud_enabled": "privacy.cloud_enabled",
    "record_history": "privacy.record_history",
}


class PrivacyManager:
    def __init__(self, settings: Settings, bus: EventBus, *, offline: bool = False) -> None:
        self._settings = settings
        self._bus = bus
        self._offline = offline
        bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting)

    @property
    def camera_enabled(self) -> bool:
        return bool(self._settings.get(_KEYS["camera_enabled"]))

    @property
    def microphone_enabled(self) -> bool:
        return bool(self._settings.get(_KEYS["microphone_enabled"]))

    @property
    def cloud_enabled(self) -> bool:
        return (not self._offline) and bool(self._settings.get(_KEYS["cloud_enabled"]))

    @property
    def record_history(self) -> bool:
        return bool(self._settings.get(_KEYS["record_history"]))

    def set(self, **flags: bool) -> dict[str, Any]:
        for name, value in flags.items():
            if name not in _KEYS:
                raise KeyError(f"unknown privacy flag {name!r}")
            if name == "cloud_enabled" and value and self._offline:
                raise PermissionError("cloud communication is unavailable in offline mode")
            self._settings.set(_KEYS[name], bool(value), source="privacy")
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "camera_enabled": self.camera_enabled,
            "microphone_enabled": self.microphone_enabled,
            "cloud_enabled": self.cloud_enabled,
            "record_history": self.record_history,
            "offline_mode": self._offline,
        }

    def _on_setting(self, event: Event) -> None:
        if str(event.get("key", "")).startswith("privacy."):
            self._bus.publish(Topics.PRIVACY_CHANGED, self.snapshot(), source="privacy")
