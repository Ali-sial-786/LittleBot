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
        """{expression, state, gaze: {x, y}, eyes_closed, fps, visible}"""


@runtime_checkable
class AudioAPI(Protocol):
    async def say(self, text: str, *, rate: float | None = None) -> bool:
        """Speak text (half-duplex: microphone muted). Publishes speech_started /
        speech_finished (face animates). Returns False if interrupted or no output."""

    async def play_sound(self, name: str, *, wait: bool = True, volume: float | None = None) -> bool:
        """Play a named sound effect (SOUND_NAMES) or a path under assets/sounds."""

    async def play_melody(self, notes: list[tuple[str, float]], *, tempo_bpm: float = 100, wait: bool = True) -> bool:
        """Play a simple synthesised melody, e.g. [("C4", 1), ("C4", 1), ("G4", 1)]. 'R' = rest."""

    def stop_output(self) -> None:
        """Interrupt any current speech / sound immediately."""

    async def listen(self, *, timeout: float | None = None) -> str | None:
        """Capture one utterance and transcribe it. None if nothing heard,
        microphone disabled/unavailable, or interrupted."""

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
        """{speaker: {backend, device, status}, microphone: {...}, volume, speaking,
        listening, tts_provider, stt_provider}"""


@runtime_checkable
class VisionAPI(Protocol):
    def simulate_face(self, x: float, y: float, size: float = 0.25) -> None:
        """Inject a simulated detected face (normalised image coords)."""

    def simulate_no_face(self) -> None: ...

    def status(self) -> dict[str, Any]:
        """{camera: ok|disconnected|disabled|unavailable, backend, detector,
        fps, faces, person_present, gaze: {x, y} | None}"""


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

    def add_item(self, url: str, title: str | None = None) -> dict[str, Any]:
        """Validate + add to allowlist. Raises ValueError for malformed/unsupported URLs."""

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

    async def run_gesture(self, name: str) -> bool:
        """False if cancelled, refused (estop) or unknown."""

    async def move_joint(self, joint: str, angle: float, *, speed_dps: float | None = None) -> bool: ...

    async def neutral(self) -> bool: ...

    def cancel(self) -> None: ...

    def emergency_stop(self, source: str = "unknown") -> None:
        """Immediately halt all motion and de-energise outputs. Latched until reset."""

    def reset_emergency_stop(self) -> bool: ...

    def status(self) -> dict[str, Any]:
        """{backend, physical_enabled, armed, estop, moving, gesture,
        joints: {name: {angle, target, min, max, neutral}}}"""


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
