"""Cross-module contracts.

Each subsystem implements one of these protocols. Other subsystems (and the
dashboard) only depend on the protocol, never on the concrete class, so
everything can be swapped for fakes in tests (see ``littlebot.testing``).

Threading rules
---------------
* ``FaceAPI`` methods are synchronous, cheap and thread-safe (they enqueue a
  command for the render thread).
* Methods marked ``async`` run on the AsyncRuntime loop.
* ``status()`` methods are synchronous, cheap, thread-safe and return plain
  JSON-serialisable dicts.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Shared vocabularies
# ---------------------------------------------------------------------------

EXPRESSIONS: tuple[str, ...] = (
    "neutral", "happy", "excited", "curious", "surprised", "sleepy", "sad", "thinking",
    "listening", "speaking", "confused", "playful", "affectionate", "celebrating",
)

# Face "states" - ongoing modes that drive background animation.
FACE_STATES: tuple[str, ...] = ("idle", "listening", "thinking", "speaking", "sleeping", "error", "hidden")

# One-shot face animations layered on top of the current expression.
FACE_ANIMATIONS: tuple[str, ...] = (
    "bounce", "wiggle", "nod", "shake", "spin_eyes", "sparkle", "hearts", "zzz", "confetti", "peek",
)

# Motion gestures (motion/gestures.py implements them; activities may reference them).
GESTURE_NAMES: tuple[str, ...] = (
    "wave", "look_left", "look_right", "raise_arms", "celebrate", "dance", "shake", "neutral",
)

# Built-in sound effects (generated procedurally into assets/sounds/*.wav).
SOUND_NAMES: tuple[str, ...] = (
    "chime", "pop", "boop", "boing", "peekaboo", "tada", "clap", "whoosh", "sparkle", "drum",
    "bell", "giggle", "uhoh", "yay", "blip", "notes", "sleep", "wake", "click", "success",
)

# Approximate developmental age groups (flexible recommendations, never diagnostic).
AGE_GROUPS: tuple[tuple[str, int, int, str], ...] = (
    ("infant", 6, 12, "6–12 months"),
    ("young_toddler", 12, 18, "12–18 months"),
    ("toddler", 18, 24, "18–24 months"),
    ("two_year", 24, 36, "2–3 years"),
    ("preschool", 36, 60, "3–5 years"),
)

ACTIVITY_CATEGORIES: tuple[str, ...] = (
    "social", "music", "language", "movement", "cognitive", "sensory", "emotional",
    "numeracy", "literacy", "creativity", "story",
)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class FaceAPI(Protocol):
    def set_expression(self, name: str, *, hold: float | None = None) -> None:
        """Change expression with a smooth transition. ``hold``: seconds before
        reverting to the state-driven default; None keeps it until changed."""

    def look_at(self, x: float, y: float, *, hold: float | None = None) -> None:
        """Gaze target in normalised screen coords (0..1, 0.5/0.5 = centre).
        Explicit gazes override face tracking for ``hold`` seconds (config default)."""

    def track_gaze(self, x: float, y: float) -> None:
        """Soft camera-tracking gaze target (lower priority than look_at, which wins while it holds)."""

    def clear_gaze(self) -> None:
        """Return gaze to tracking / idle wandering."""

    def blink(self, double: bool = False) -> None: ...

    def close_eyes(self) -> None:
        """Close eyelids and keep them closed (peekaboo, sleeping)."""

    def open_eyes(self) -> None: ...

    def set_state(self, state: str) -> None:
        """One of FACE_STATES. Normally driven automatically by RobotState."""

    def play_animation(self, name: str) -> None:
        """One of FACE_ANIMATIONS, layered on the current expression."""

    def status(self) -> dict[str, Any]:
        """{expression, state, gaze: {x, y}, eyes_closed, fps, visible} (+ optional extra keys
        such as talking, listening, animations, display)"""


@runtime_checkable
class AudioAPI(Protocol):
    async def say(self, text: str, *, rate: float | None = None) -> bool:
        """Speak text (half-duplex: microphone muted). Publishes speech_started /
        speech_finished (face animates). ``rate`` overrides the audio.tts_rate setting
        for this call. Returns False if interrupted or no real speaker (events are still
        published so the face animates)."""

    async def play_sound(self, name: str, *, wait: bool = True, volume: float | None = None) -> bool:
        """Play a named sound effect (SOUND_NAMES) or a path under assets/sounds.
        ``volume`` is relative to the master volume (never louder than the parent's setting)."""

    async def play_melody(self, notes: list[tuple[str, float]], *, tempo_bpm: float = 100, wait: bool = True) -> bool:
        """Play a simple synthesised melody, e.g. [("C4", 1), ("C4", 1), ("G4", 1)]. 'R' = rest."""

    def stop_output(self) -> None:
        """Interrupt any current speech / sound immediately."""

    async def listen(self, *, timeout: float | None = None) -> str | None:
        """Capture one utterance and transcribe it. ``timeout``: how long to wait for
        speech to *start*. None if nothing heard, microphone disabled/unavailable, or interrupted."""

    def inject_utterance(self, text: str) -> None:
        """Simulated microphone input (simulator / tests). Satisfies a pending listen()."""

    def set_volume(self, volume: float) -> float:
        """Set output volume 0..1; clamped to the configured safe maximum. Returns applied value."""

    def list_devices(self) -> dict[str, Any]:
        """{inputs: [{index, name, default}], outputs: [...]}"""

    async def test_speaker(self) -> dict[str, Any]:
        """Plays a short chime. {ok, backend, device, detail}"""

    async def test_microphone(self, seconds: float = 2.0) -> dict[str, Any]:
        """Records briefly (never stored). {ok, backend, device, peak, rms, detail}"""

    def status(self) -> dict[str, Any]:
        """{speaker: {backend, device, status, detail}, microphone: {...}, volume, max_volume,
        speaking, listening, playing, queued, mic_muted, half_duplex, tts_rate, tts_provider,
        stt_provider, last_error}"""


@runtime_checkable
class VisionAPI(Protocol):
    def simulate_face(self, x: float, y: float, size: float = 0.25) -> None:
        """Inject a simulated detected face at normalised CAMERA-IMAGE coords (before mirroring:
        with camera.mirror, image x=0.2 makes the eyes look towards screen x~0.8). Expires
        after a few seconds unless refreshed."""

    def simulate_no_face(self) -> None: ...

    def status(self) -> dict[str, Any]:
        """{camera: ok|disconnected|disabled|unavailable, detail, backend, detector, detector_detail,
        device, resolution ("640x480"), fourcc, fps, detect_fps, detect_ms, faces, person_present,
        gaze: {x, y} | None, gestures_enabled, simulated, detect_mode: full|reduced|paused, robot_state}"""


@runtime_checkable
class ActivityAPI(Protocol):
    def list(self, age_months: int | None = None, category: str | None = None) -> list[dict[str, Any]]:
        """Activity summaries: {id, name, description, min_age_months, max_age_months,
        age_groups, category, participation, parent_instructions, safety_notes,
        duration_seconds, pausable}"""

    def get(self, activity_id: str) -> dict[str, Any] | None: ...

    async def start(self, activity_id: str, *, source: str = "dashboard") -> dict[str, Any]:
        """Start (stopping any running one). Raises KeyError if unknown,
        PermissionError if refused (lease/age/privacy). Returns status()."""

    async def stop(self, reason: str = "stopped") -> None: ...

    async def pause(self) -> bool: ...

    async def resume(self) -> bool: ...

    def respond(self, response: str, prompt_id: str | None = None) -> bool:
        """Caregiver answer to the current prompt (e.g. "done", "skip", an option)."""

    def status(self) -> dict[str, Any]:
        """{running, paused, id, name, step_index, total_steps, step_text, prompt: {...}|None}"""


@runtime_checkable
class MediaAPI(Protocol):
    def list_items(self) -> list[dict[str, Any]]:
        """{id, kind: video|playlist, youtube_id, title, url, added_at}"""

    def add_item(self, url: str, title: str | None = None, *, kind: str | None = None) -> dict[str, Any]:
        """Validate + add to allowlist. Raises ValueError for malformed/unsupported URLs.
        ``kind="playlist"`` adds the whole playlist of a watch?v=..&list=.. link."""

    def remove_item(self, item_id: int) -> bool: ...

    async def play(self, item_id: int, *, parent_override: bool = False) -> dict[str, Any]:
        """Raises PermissionError (age gate / limits / disabled), KeyError (unknown)."""

    async def pause(self) -> bool: ...

    async def resume(self) -> bool: ...

    async def stop(self, reason: str = "stopped") -> None: ...

    async def skip(self) -> dict[str, Any]: ...

    def set_volume(self, volume: float) -> float: ...

    def status(self) -> dict[str, Any]:
        """{state: idle|loading|playing|paused|error, item, session_seconds,
        session_limit_seconds, daily_used_seconds, daily_limit_seconds, player, volume}"""


@runtime_checkable
class MotionAPI(Protocol):
    def gestures(self) -> list[str]: ...

    async def run_gesture(self, name: str, *, priority: int | None = None, owner: str | None = None) -> bool:
        """False if cancelled, refused (estop / lease held by higher priority) or unknown.
        ``owner``: a behaviour that already holds Resource.MOTION runs the gesture under its lease."""

    async def move_joint(self, joint: str, angle: float, *, speed_dps: float | None = None,
                         priority: int | None = None, owner: str | None = None) -> bool: ...

    async def neutral(self, *, priority: int | None = None, owner: str | None = None) -> bool: ...

    def cancel(self) -> None: ...

    def emergency_stop(self, source: str = "unknown") -> None:
        """Immediately halt all motion and de-energise outputs. Latched until reset."""

    def reset_emergency_stop(self) -> bool: ...

    def status(self) -> dict[str, Any]:
        """{backend, physical_enabled, armed, estop, moving, gesture, simulation_only, reason,
        joints: {name: {angle, target, min, max, neutral, channel, position}}}
        (position: -1..1 normalised, 0 = neutral, +1 = arm raised / head turned to robot's left)"""


@runtime_checkable
class BrainAPI(Protocol):
    async def handle_utterance(self, text: str, *, source: str = "simulated", speak: bool = True) -> dict[str, Any]:
        """Run one think -> speak turn for an utterance. {reply, provider, filtered, error}"""

    async def converse_once(self) -> dict[str, Any]:
        """Push-to-talk: listen -> think -> speak. {heard, reply, provider, ...}"""

    def providers(self) -> list[dict[str, Any]]:
        """{name, configured, available, model}"""

    async def test_provider(self, name: str) -> dict[str, Any]:
        """{ok, latency_ms, error}"""

    def status(self) -> dict[str, Any]:
        """{provider, active_provider, cloud_allowed, reason, usage: {...}, last_error, busy}"""


@runtime_checkable
class SystemMonitorAPI(Protocol):
    def status(self) -> dict[str, Any]:
        """{cpu_percent, memory_mb, memory_percent, system_memory_percent,
        temperature_c, is_raspberry_pi, platform, uptime_seconds}"""
