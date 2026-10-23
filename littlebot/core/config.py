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
    tap_reaction: bool = True  # tapping the face makes LittleBot giggle (cause-and-effect play)
    # Screen mode to request from the Wayland compositor before going fullscreen, e.g.
    # "720x480@60" (None = leave the desktop's mode alone). See docs/DISPLAY_SETUP.md.
    output_mode: str | None = None
    output_name: str = "HDMI-A-1"  # the Pi 5's HDMI0 port
    # SDL video driver to prefer, e.g. "wayland" (None = SDL's own choice, which is X11 via
    # Xwayland under labwc). An SDL_VIDEODRIVER environment variable always wins.
    video_driver: str | None = None


class FaceConfig(_Section):
    eye_color: tuple[int, int, int] = (90, 210, 255)
    background_color: tuple[int, int, int] = (255, 214, 228)  # soft light pink (a dark colour gives glowing eyes)
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
    # --- added by vision (all optional, defaults keep previous behaviour) ---
    fourcc: str = "MJPG"  # requested pixel format; MJPG keeps USB 2.0 bandwidth low ("" = driver default)
    stall_seconds: float = 3.0  # no frame for this long => treated as disconnected
    fixture: str | None = None  # dev aid: image / video / directory used instead of a real camera
    mock_face: bool = False  # mock camera draws a slowly moving synthetic face (demos)


class VisionConfig(_Section):
    detector: Literal["haar", "yunet", "none"] = "haar"
    min_face_fraction: float = 0.08  # min face width relative to frame width
    present_after_seconds: float = 0.6  # hysteresis before person.present
    left_after_seconds: float = 3.0  # hysteresis before person.left
    gaze_smoothing: float = 0.35  # 0..1 exponential smoothing factor
    gestures_enabled: bool = False  # experimental wave/motion detection
    # --- added by vision (all optional) ---
    yunet_model: str = "models/face_detection_yunet_2023mar.onnx"  # relative to the project root
    yunet_score_threshold: float = 0.7
    haar_cascade: str | None = None  # explicit cascade XML path; None = search OpenCV / system / models/
    haar_min_neighbors: int = 5  # higher = fewer false positives, more misses
    haar_scale_factor: float = 1.2  # image-pyramid step; 1.1 finds slightly more faces but costs ~2x CPU
    opencv_threads: int = 2  # cv2.setNumThreads for detection (0 = OpenCV default); leaves cores for the UI
    gaze_vertical_offset: float = 0.0  # added to gaze_y (camera mounted above/below the eyes)
    simulated_face_seconds: float = 3.0  # simulate_face() expires unless refreshed
    gesture_fps: float = 10.0  # frame rate for the experimental gesture analyser (when enabled)
    pause_during_media: bool = True  # PLAYING_MEDIA: no frame decoding / detection (video needs the CPU)
    sleep_detect_fps: float = 1.5  # SLEEPING: reduced detection rate (a person can still wake the robot)


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


class WakeConfig(_Section):
    """Wake word: saying a phrase starts live voice hands-free (littlebot/audio/wakeword.py).

    Recognised on the robot with Vosk (offline); nothing is sent anywhere until a phrase is heard."""

    enabled: bool = True  # default of the parent's dashboard switch (setting ``wake.enabled``)
    # Words the model knows; "littlebot" is heard as "little bot". Two-word phrases ("hey bot")
    # trigger by mistake more often (e.g. on "hey but"): remove it here if that happens.
    phrases: list[str] = Field(default_factory=lambda: ["hey little bot", "hey littlebot", "hey bot"])
    model_path: str = "models/vosk-model-small-en-us-0.15"  # scripts/download_models.py vosk-en
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)  # per word; higher = fewer false triggers
    distractors: bool = True  # also offer common words to the recogniser: far fewer false triggers
    cooldown_seconds: float = Field(default=4.0, ge=0.0)  # no new trigger this soon after the last one
    sound: str = "listen"  # earcon played when a phrase is heard


class AIConfig(_Section):
    provider: Literal["mock", "anthropic", "openai", "chatgpt"] = "mock"
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
    # --- brain module extensions (see docs/modules/brain.md) ---
    # Anthropic request tuning. Current Claude models think adaptively by default and
    # thinking tokens count toward max_tokens, so we ask for low effort and add headroom.
    anthropic_effort: Literal["low", "medium", "high"] | None = "low"
    anthropic_thinking: Literal["adaptive", "disabled"] = "adaptive"  # "disabled": Haiku 5.5 only
    thinking_headroom_tokens: int = 400
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_base_url: str = "https://api.openai.com/v1"
    # Estimated USD per million tokens {model: [input, output]}; overrides the built-in table.
    price_per_mtok: dict[str, tuple[float, float]] = Field(default_factory=dict)
    # Tiny in-memory conversation context (never persisted unless store_transcripts).
    context_turns: int = 3
    context_ttl_seconds: float = 120.0
    max_input_chars: int = 300
    listen_timeout_seconds: float = 8.0
    say_didnt_hear: bool = True
    # Reply to audio.utterance events that arrive while LittleBot is idle (not mid-activity).
    reply_to_utterance_events: bool = True
    # Personal info is NOT sent to cloud providers unless a parent opts in.
    share_child_name: bool = False
    share_interests: bool = False
    # Safety-rule groups switched off for AI replies (parent's choice; [] = every rule on). "links" lets it
    # talk about YouTube, shows, apps and searching (links themselves are still stripped before speaking).
    relaxed_rules: list[str] = Field(default_factory=lambda: ["links"])


class ChatGPTConfig(_Section):
    """Sign in with ChatGPT: the parent's own ChatGPT plan, no API key (see docs/CHATGPT.md).

    Uses OpenAI's Codex sign-in and the undocumented Codex endpoints (as the Codex CLI and
    WatchGPT do). They are not a public API and may change or stop working without notice.
    """
    originator: str = "littlebot"  # how requests identify themselves to OpenAI
    token_file: str = "chatgpt_auth.json"  # under data_dir, mode 0600; never sent to the browser
    # Text model for typed / push-to-talk replies and live-voice look-ups: the first one that works.
    models: list[str] = Field(default_factory=lambda: ["gpt-6-luna", "gpt-6-sol", "gpt-5.6-luna"])
    reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    # --- live voice (GPT-Live speech-to-speech over WebRTC; needs the "live" extra: aiortc) ---
    live_model: str = "gpt-live-1-codex"
    # The voice to ask for first; if OpenAI refuses it, each of live_voice_fallbacks in turn.
    # Known GPT-Live voices: cove | arbor | breeze | ember | juniper | maple | sol | spruce | vale.
    live_voice: str = "bubbie"
    live_voice_fallbacks: list[str] = Field(default_factory=lambda: ["sol"])
    live_session_minutes: float = 10.0  # hard limit per conversation
    live_idle_hangup_seconds: float = 90.0  # hang up after this long with nobody talking
    live_connect_timeout_seconds: float = 20.0
    # The Pi has no echo cancellation: don't send the microphone while LittleBot's voice plays
    # (and for live_echo_tail_ms after), or the model hears itself. Grown-ups can't talk over it.
    live_half_duplex: bool = True
    live_echo_tail_ms: int = 450
    live_voice_level: float = 0.012  # RMS (0..1) above which the model's audio counts as speaking
    live_volume: float | None = None  # None = the speaker volume setting (always capped at max_volume)
    # --- asking LittleBot to do things (function calling, littlebot/chatgpt/tools.py) ---
    # Typed / push-to-talk chat and live-voice hand-offs may control the robot: volume, faces,
    # videos (YouTube search), activities, games, sleep, status ("how hot are you?").
    tools_enabled: bool = True
    max_tool_rounds: int = 6  # tool calls per answer ("play baby shark" = search + play)
    # OpenAI's own web search (the hosted "web_search" tool, as the Codex CLI sends it) for news, scores,
    # opening hours and other facts that change. Offered with the tools; if the endpoint refuses it,
    # LittleBot asks again without it and doesn't offer it again until restarted.
    web_search: bool = True


class InfoConfig(_Section):
    """Live information for the tools: the weather (Open-Meteo, no key) and the time (littlebot/info).

    The parent's choice on the dashboard (Settings, "Weather & time") wins over ``location`` / ``units``."""

    # Home, for the weather: a place ("Toronto", "Springfield, Illinois") or "lat, lon". "" = approximate,
    # from the robot's internet connection (``locate_by_ip``).
    location: str = ""
    units: Literal["auto", "celsius", "fahrenheit"] = "auto"  # auto: from home's country, else the locale
    locate_by_ip: bool = True  # no home place set: ask a free service where the robot's public IP is (cached)
    timezone: str = ""  # IANA name ("America/Toronto") for the time tool; "" = the robot's own time zone
    timeout_seconds: float = Field(default=3.0, gt=0, le=30)  # per answer, all requests together
    cache_minutes: float = Field(default=10.0, ge=0)  # a forecast is reused this long


class MediaConfig(_Section):
    # auto: Pi -> kiosk (if Chromium is installed), desktop -> browser, headless/offline -> none
    player: Literal["auto", "browser", "kiosk", "none"] = "auto"
    browser_command: str | None = None  # e.g. "chromium-browser"; None = autodetect
    session_limit_minutes: int = 15
    daily_limit_minutes: int = 30
    min_age_months_for_video: int = 18
    default_volume: float = 0.5
    max_volume: float = 0.8  # ceiling for the YouTube player volume (0..1)
    # Base URL the robot's own browser uses to reach the player page (the dashboard server).
    # None = http://127.0.0.1:<dashboard.port>
    player_base_url: str | None = None
    youtube_host: Literal["nocookie", "standard"] = "nocookie"  # youtube-nocookie.com or youtube.com embeds
    oembed_titles: bool = False  # look up missing titles via youtube.com/oembed (network call; off by default)
    max_items: int = 200  # allowlist size cap
    connect_timeout_seconds: float = 60.0  # stop if the player page never connects
    heartbeat_timeout_seconds: float = 15.0  # stop if a connected player page goes silent
    max_pause_minutes: float = 10.0  # a paused video gives the screen back after this long
    limit_check_seconds: float = 1.0  # limit/timeout watchdog period while a session is active
    kiosk_close_delay_seconds: float = 1.5  # let the page fade out before the kiosk window closes
    kiosk_extra_args: list[str] = Field(default_factory=list)
    # YouTube search (no API key: yt-dlp's "ytsearch"). allow_search lets the AI find and play any
    # YouTube video when asked, not only the approved list (the time limits and age gate still apply).
    allow_search: bool = True
    search_results: int = 5
    search_timeout_seconds: float = 20.0
    ytdlp_command: str | None = None  # None = the yt_dlp module in LittleBot's Python, else yt-dlp on PATH


class ServoChannelConfig(_Section):
    # Angles are joint angles in degrees (0..180). See docs/modules/motion.md.
    # Validated by littlebot.motion.joints (invalid values disable motion, they never crash the app).
    channel: int
    neutral: float = 90.0
    min_angle: float = 45.0
    max_angle: float = 135.0
    max_speed_dps: float = 90.0  # degrees per second
    max_accel_dps2: float = 200.0  # acceleration limit, degrees per second^2
    inverted: bool = False  # servo angle = 180 - joint angle (mirrored mounting)
    min_pulse_us: int = 500  # servo pulse at servo angle 0 (absolute bounds 500..2500)
    max_pulse_us: int = 2500  # servo pulse at servo angle 180
    # Which limit a positive normalised gesture value moves toward: "raise" for arms,
    # "look to the robot's left" for the head.
    positive_toward: Literal["max", "min"] = "max"


def _default_servos() -> dict[str, ServoChannelConfig]:
    return {
        "head": ServoChannelConfig(channel=0, neutral=90, min_angle=50, max_angle=130, max_speed_dps=60),
        "left_arm": ServoChannelConfig(channel=1, neutral=20, min_angle=10, max_angle=150, max_speed_dps=90),
        "right_arm": ServoChannelConfig(channel=2, neutral=160, min_angle=30, max_angle=170, max_speed_dps=90, inverted=True,
                                        positive_toward="min"),
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
    positions_hz: float = 10.0  # motion.positions event rate while moving
    speed_scale: float = 1.0  # global slow-down factor for every movement (0 < x <= 1)
    min_move_seconds: float = 0.25  # no movement is ever shorter than this
    settle_seconds: float = 0.5  # hold at the assumed position after energising a joint, before moving
    stop_all_returns_to_neutral: bool = True  # parent STOP: halt smoothly, then drift slowly back to neutral
    homing_speed_factor: float = 0.4  # fraction of max speed used for that return to neutral
    release_when_idle_seconds: float = 0.0  # >0: de-energise servos after this long without motion (0 = hold)


class DashboardConfig(_Section):
    enabled: bool = True
    host: str = "127.0.0.1"  # localhost only by default
    port: int = 8765
    lan_access: bool = False  # explicit opt-in: binds 0.0.0.0 so phones on the LAN can connect
    show_setup_code_on_screen: bool = True  # first-run code on the robot's own screen (physical presence)
    session_hours: float = 12.0
    login_attempts_per_minute: int = 5
    # Extra names the dashboard answers to ("name" or "name:port"), e.g. a reverse proxy or SSH tunnel.
    # Always allowed: localhost / 127.0.0.1 / ::1, this machine's hostname and <hostname>.local and,
    # with lan_access, its LAN IP addresses. Anything else gets 400 (DNS-rebinding protection).
    allowed_hosts: list[str] = Field(default_factory=list)


class ActivitiesConfig(_Section):
    time_scale: float = 1.0  # tests use small values to run scripts quickly
    caregiver_timeout_seconds: float = 45.0  # wait this long for caregiver confirmation
    record_history: bool = False  # optional, off by default
    # Extra folders of parent-authored activity YAML (relative paths are under data_dir).
    # Missing folders are ignored; built-in scripts always come from assets/activities.
    custom_dirs: list[str] = Field(default_factory=lambda: ["activities"])
    random_seed: int | None = None  # fixed seed = reproducible random choices (demos / tests)


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
    wake: WakeConfig = Field(default_factory=WakeConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    chatgpt: ChatGPTConfig = Field(default_factory=ChatGPTConfig)
    info: InfoConfig = Field(default_factory=InfoConfig)
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
