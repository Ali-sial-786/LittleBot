"""Dependency-injection container passed to every subsystem factory.

Each subsystem package exposes ``create_service(ctx: RobotContext)`` returning
an object that implements its protocol (core/interfaces.py) and, if it has a
lifecycle, the ``Service`` base class. The composition root (app/robot.py)
creates them in dependency order and stores them back on the context, so later
subsystems can use earlier ones (e.g. activities use face + audio + motion).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from littlebot.core.arbiter import ResourceArbiter
from littlebot.core.clock import Clock
from littlebot.core.config import LittleBotConfig, load_config
from littlebot.core.events import EventBus
from littlebot.core.interfaces import (
    ActivityAPI, AudioAPI, BrainAPI, FaceAPI, MediaAPI, MotionAPI, SystemMonitorAPI, VisionAPI,
)
from littlebot.core.runtime import AsyncRuntime
from littlebot.core.settings import Settings, build_defaults
from littlebot.core.state import StateManager
from littlebot.core.storage import Database
from littlebot.safety.policy import AgePolicy
from littlebot.safety.privacy import PrivacyManager


@dataclass
class RobotContext:
    config: LittleBotConfig
    bus: EventBus
    state: StateManager
    arbiter: ResourceArbiter
    db: Database
    settings: Settings
    privacy: PrivacyManager
    clock: Clock
    monitor: SystemMonitorAPI
    runtime: AsyncRuntime | None = None
    face: FaceAPI | None = None
    audio: AudioAPI | None = None
    vision: VisionAPI | None = None
    motion: MotionAPI | None = None
    media: MediaAPI | None = None
    brain: BrainAPI | None = None
    activities: ActivityAPI | None = None
    games: Any = None  # on-screen games (littlebot.games.service.GamesService)
    screens: Any = None  # info cards over the face (littlebot.screens.service.ScreenService)
    live: Any = None  # live voice (littlebot.chatgpt.live.LiveVoiceService)
    wake: Any = None  # wake word, "Hey LittleBot" (littlebot.audio.wakeword.WakeWordService)
    voices: Any = None  # the family's voices, opt-in (littlebot.voices.service.VoiceRecognition; get_voices(ctx))
    extras: dict[str, Any] = field(default_factory=dict)

    def age_policy(self) -> AgePolicy:
        return AgePolicy(
            self.settings.child_age_months(),
            min_age_months_for_video=self.config.media.min_age_months_for_video,
            min_age_months_for_cloud_ai=self.config.ai.min_age_months_for_cloud,
        )


def build_core_context(
    config: LittleBotConfig | None = None,
    *,
    db_path: str | Path | None = None,
    monitor: SystemMonitorAPI | None = None,
) -> RobotContext:
    """Create the core (non-subsystem) part of the context."""
    cfg = config or load_config()
    bus = EventBus()
    if db_path is None:
        db_path = cfg.data_path / "littlebot.db"
    db = Database(db_path)
    settings = Settings(db, bus, build_defaults(cfg))
    if monitor is None:
        from littlebot.hardware.system import SystemMonitor

        monitor = SystemMonitor()
    return RobotContext(
        config=cfg,
        bus=bus,
        state=StateManager(bus),
        arbiter=ResourceArbiter(),
        db=db,
        settings=settings,
        privacy=PrivacyManager(settings, bus, offline=cfg.is_offline),
        clock=Clock(cfg.activities.time_scale),
        monitor=monitor,
    )
