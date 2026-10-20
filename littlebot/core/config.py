"""Configuration loading for LittleBot.

Layering (later wins):
  1. Built-in defaults (the pydantic models below)
  2. config/default.yaml
  3. config/<mode>.yaml          (simulation | pi | offline)
  4. config/local.yaml           (git-ignored, per-machine overrides)
  5. an explicit --config file
  6. environment variables  LITTLEBOT__<SECTION>__<KEY>=value

Secrets (API keys) are never stored in YAML: they come from the environment
or a git-ignored `.env` file and are exposed as `SecretStr`.

Runtime, parent-editable settings (privacy toggles, volume, child profile...)
live in the SQLite-backed `Settings` store (core/settings.py), which uses the
values here only as defaults.
"""

from __future__ import annotations

import copy
import enum
import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from littlebot.core.paths import CONFIG_DIR, PROJECT_ROOT

log = logging.getLogger(__name__)

ENV_PREFIX = "LITTLEBOT__"


class Mode(str, enum.Enum):
    SIMULATION = "simulation"  # desktop, real webcam/mic/speaker if present, mocks otherwise
    PI = "pi"  # Raspberry Pi 5 with USB devices
    OFFLINE = "offline"  # no network, no keys, no devices required


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


DeviceBackend = Literal["auto", "real", "mock", "none"]


class LoggingConfig(_Section):
    level: str = "INFO"
    format: Literal["console", "json"] = "console"
    file: str | None = None  # relative to data_dir when not absolute
    max_bytes: int = 2_000_000
    backup_count: int = 3


class DisplayConfig(_Section):
    width: int = 480
    height: int = 320
    fps: int = 30
    idle_fps: int = 20  # lower frame rate when nothing is animating, to save CPU
    scale: float = 2.0  # desktop window scale factor (1.0 on the Pi)
    fullscreen: bool = False
    headless: bool = False  # no window at all (tests, dashboard-only runs)
    show_debug: bool = False
    show_sim_panel: bool = True  # side panel with simulated servos/status (desktop only)
    hide_cursor: bool = False


class FaceConfig(_Section):
    eye_color: tuple[int, int, int] = (90, 210, 255)
    background_color: tuple[int, int, int] = (6, 8, 14)
    glow: bool = True
    blink_interval: tuple[float, float] = (2.5, 6.0)  # random seconds between blinks
    double_blink_chance: float = 0.18
    idle_gaze: bool = True
    breathing: bool = True
    transition_seconds: float = 0.25
    look_hold_seconds: float = 2.0  # explicit look_at() overrides tracking for this long


class CameraConfig(_Section):
    backend: DeviceBackend = "auto"
    device: int | str = 0  # index or /dev/videoN path
    capture_width: int = 640
    capture_height: int = 480
    capture_fps: int = 15
    process_width: int = 320  # frames are downscaled to this width before detection
    detect_fps: float = 6.0  # detection rate (CPU budget)
    mirror: bool = True  # camera faces the child: image-right means "look to my right"
    reconnect_seconds: float = 3.0


class VisionConfig(_Section):
    detector: Literal["haar", "yunet", "none"] = "haar"
    min_face_fraction: float = 0.08  # min face width relative to frame width
    present_after_seconds: float = 0.6  # hysteresis before person.present
    left_after_seconds: float = 3.0  # hysteresis before person.left
    gaze_smoothing: float = 0.35  # 0..1 exponential smoothing factor
    gestures_enabled: bool = False  # experimental wave/motion detection


class MicrophoneConfig(_Section):
    backend: DeviceBackend = "auto"
    device: int | str | None = None  # None = system default; or name substring
    sample_rate: int = 16000
    channels: int = 1
    input_gain: float = 1.0
    max_record_seconds: float = 8.0
    silence_seconds: float = 1.2  # end of utterance after this much silence
    vad_threshold: float = 0.015  # RMS threshold for "speech" (0..1)


class SpeakerConfig(_Section):
    backend: Literal["auto", "sounddevice", "pygame", "mock", "none"] = "auto"
    device: int | str | None = None
    sample_rate: int = 22050
    volume: float = 0.5  # default volume 0..1
    max_volume: float = 0.8  # hard ceiling so LittleBot is never startlingly loud


class TTSConfig(_Section):
    provider: Literal["auto", "mock", "espeak", "piper", "openai"] = "auto"
    rate: float = 0.9  # speaking-speed multiplier (1.0 = normal)
    pitch: int = 65  # espeak pitch 0..99 (higher sounds younger / friendlier)
    voice: str = "en-us+f3"
    piper_model: str | None = None
    openai_voice: str = "shimmer"
    openai_model: str = "gpt-4o-mini-tts"


class STTConfig(_Section):
    provider: Literal["auto", "mock", "vosk", "openai"] = "auto"
    language: str = "en"
    vosk_model_path: str | None = None
    openai_model: str = "whisper-1"


class AudioConfig(_Section):
    microphone: MicrophoneConfig = Field(default_factory=MicrophoneConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    half_duplex: bool = True  # never listen while speaking (echo prevention)
    post_speech_mute_seconds: float = 0.4  # keep mic muted briefly after speaking
    store_recordings: bool = False  # never store audio unless explicitly enabled


class AIConfig(_Section):
    provider: Literal["mock", "anthropic", "openai"] = "mock"
    anthropic_model: str = "claude-haiku-5-5"
    openai_model: str = "gpt-4o-mini"
    timeout_seconds: float = 12.0
    max_output_tokens: int = 120
    max_words: int = 30  # validated after generation, independent of the prompt
    max_requests_per_hour: int = 60
    max_requests_per_day: int = 200
    daily_budget_usd: float = 0.50
    min_age_months_for_cloud: int = 24  # below this, scripted responses only
    store_transcripts: bool = False


class MediaConfig(_Section):
    player: Literal["auto", "browser", "kiosk", "none"] = "auto"
    browser_command: str | None = None  # e.g. "chromium-browser"; None = autodetect
    session_limit_minutes: int = 15
    daily_limit_minutes: int = 30
    min_age_months_for_video: int = 18
    default_volume: float = 0.5


class ServoChannelConfig(_Section):
    channel: int
    neutral: float = 90.0
    min_angle: float = 45.0
    max_angle: float = 135.0
    max_speed_dps: float = 90.0  # degrees per second
    inverted: bool = False
    min_pulse_us: int = 500
    max_pulse_us: int = 2500


def _default_servos() -> dict[str, ServoChannelConfig]:
    return {
        "head": ServoChannelConfig(channel=0, neutral=90, min_angle=50, max_angle=130, max_speed_dps=60),
        "left_arm": ServoChannelConfig(channel=1, neutral=20, min_angle=10, max_angle=150, max_speed_dps=90),
        "right_arm": ServoChannelConfig(channel=2, neutral=160, min_angle=30, max_angle=170, max_speed_dps=90, inverted=True),
    }


class MotionConfig(_Section):
    backend: Literal["mock", "pca9685"] = "mock"
    physical_enabled: bool = False  # MUST stay false until hardware is verified and calibrated
    i2c_bus: int = 1
    i2c_address: int = 0x40
    pwm_frequency: int = 50
    update_hz: float = 50.0
    servos: dict[str, ServoChannelConfig] = Field(default_factory=_default_servos)
    calibration_file: str = "config/calibration.yaml"


class DashboardConfig(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"  # localhost only by default
    port: int = 8765
    lan_access: bool = False  # explicit opt-in: binds 0.0.0.0 so phones on the LAN can connect
    session_hours: float = 12.0
    login_attempts_per_minute: int = 5


class ActivitiesConfig(_Section):
    time_scale: float = 1.0  # tests use small values to run scripts quickly
    caregiver_timeout_seconds: float = 45.0  # wait this long for caregiver confirmation
    record_history: bool = False  # optional, off by default


class PrivacyConfig(_Section):
    camera_enabled: bool = True
    microphone_enabled: bool = True
    cloud_enabled: bool = False  # all cloud communication off until a parent enables it


class SystemConfig(_Section):
    sleep_after_idle_minutes: float = 10.0
    monitor_interval_seconds: float = 2.0


class LittleBotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    mode: Mode = Mode.SIMULATION
    data_dir: str = "data"
    robot_name: str = "LittleBot"
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    face: FaceConfig = Field(default_factory=FaceConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    activities: ActivitiesConfig = Field(default_factory=ActivitiesConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)

    # Secrets: populated from environment / .env only, never from YAML.
    anthropic_api_key: SecretStr | None = Field(default=None, exclude=True)
    openai_api_key: SecretStr | None = Field(default=None, exclude=True)

    @field_validator("mode", mode="before")
    @classmethod
    def _lower_mode(cls, v: Any) -> Any:
        return v.lower() if isinstance(v, str) else v

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def is_offline(self) -> bool:
        return self.mode == Mode.OFFLINE

    def public_dict(self) -> dict[str, Any]:
        """Config as a plain dict with secrets removed (safe for logs / dashboard)."""
        d = self.model_dump(mode="json")
        d["has_anthropic_key"] = bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value())
        d["has_openai_key"] = bool(self.openai_api_key and self.openai_api_key.get_secret_value())
        return d


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a mapping at the top level")
    return data


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE lines). Existing environment wins."""
    env = os.environ if environ is None else environ
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in env:
            env[key] = value
            loaded[key] = value
    return loaded


def _coerce_env_value(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    for key, raw in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(ENV_PREFIX):].split("__") if p]
        if not parts:
            continue
        node = out
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = _coerce_env_value(raw)
    return out


def load_config(
    mode: str | Mode | None = None,
    config_file: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    config_dir: Path | None = None,
    environ: dict[str, str] | None = None,
    use_dotenv: bool = True,
    include_local: bool = True,
) -> LittleBotConfig:
    """Build the effective configuration. See module docstring for layering."""
    cdir = config_dir or CONFIG_DIR
    env = dict(os.environ) if environ is None else dict(environ)
    if use_dotenv:
        load_dotenv(PROJECT_ROOT / ".env", env)

    data: dict[str, Any] = _read_yaml(cdir / "default.yaml")
    chosen = mode or env.get("LITTLEBOT_MODE") or data.get("mode") or Mode.SIMULATION.value
    chosen = Mode(chosen.value if isinstance(chosen, Mode) else str(chosen).lower())
    data = deep_merge(data, _read_yaml(cdir / f"{chosen.value}.yaml"))
    if include_local:
        data = deep_merge(data, _read_yaml(cdir / "local.yaml"))
    if config_file:
        data = deep_merge(data, _read_yaml(Path(config_file)))
    data = deep_merge(data, env_overrides(env))
    if overrides:
        data = deep_merge(data, overrides)
    data["mode"] = chosen.value
    # Secrets never come from YAML.
    data.pop("anthropic_api_key", None)
    data.pop("openai_api_key", None)

    cfg = LittleBotConfig.model_validate(data)
    if chosen != Mode.OFFLINE:
        if env.get("ANTHROPIC_API_KEY"):
            cfg.anthropic_api_key = SecretStr(env["ANTHROPIC_API_KEY"])
        if env.get("OPENAI_API_KEY"):
            cfg.openai_api_key = SecretStr(env["OPENAI_API_KEY"])
    if chosen == Mode.OFFLINE:
        # Offline demo: never touch the network, whatever the overrides say.
        cfg.privacy.cloud_enabled = False
        cfg.ai.provider = "mock"
    return cfg
