"""Bus topics the assistant publishes (for the face, body language, the dashboard and the voice games).

Payloads never hold what was said, except ``assistant.caption`` (the dashboard's live captions, kept in memory
only, like GPT-Live's captions).
"""

from __future__ import annotations

SESSION = "assistant.session"  # {active: bool, reason} a voice session started / ended
PHASE = "assistant.phase"  # {phase: "listening" | "hearing" | "thinking" | "speaking" | "idle"}
HEARD = "assistant.heard"  # {words: int, ms: int} the person finished a turn (ms: how long they spoke)
SENTENCE = "assistant.sentence"  # {index, chars, expression: str | None, last: bool} a sentence starts playing
INTERRUPTED = "assistant.interrupted"  # {} the person talked over LittleBot and it stopped
TURN_DONE = "assistant.turn_done"  # {ms_to_first_audio, tools: [names]} an answer finished
CAPTION = "assistant.caption"  # {who: "user" | "robot", text}

__all__ = ["CAPTION", "HEARD", "INTERRUPTED", "PHASE", "SENTENCE", "SESSION", "TURN_DONE"]
