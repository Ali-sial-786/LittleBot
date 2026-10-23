"""GPT-Live data-channel events (the Codex "quicksilver" variant), ported from WatchGPT.

The call's WebRTC data channel ("oai-events") carries JSON events: captions of what the
person and the voice say, and *delegations* - questions the voice model hands to the client.
The client answers a delegation with ``delegation.context.append`` frames and the voice then
says the answer in its own words. A delegation that is never answered leaves the voice waiting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Union

MAX_RESULT_CHARS = 1800
MAX_FRAME_BYTES = 500

FAILED_RESULT = "The lookup failed. Say briefly that it didn't work this time; they can ask again."
NO_INPUT_RESULT = "Ask them to say it again; no words were heard."


@dataclass(frozen=True)
class UserText:
    text: str
    final: bool


@dataclass(frozen=True)
class AssistantText:
    text: str
    final: bool


@dataclass(frozen=True)
class Delegation:
    id: str
    prompt: str  # the voice model's own wording of the task; may be empty


@dataclass(frozen=True)
class Failure:
    code: str | None


@dataclass(frozen=True)
class Closed:
    reason: str | None


@dataclass(frozen=True)
class Other:
    pass


Event = Union[UserText, AssistantText, Delegation, Failure, Closed, Other]


def _s(obj: Any, key: str) -> str | None:
    v = obj.get(key) if isinstance(obj, dict) else None
    return v if isinstance(v, str) else None


def _o(obj: Any, key: str) -> dict[str, Any] | None:
    v = obj.get(key) if isinstance(obj, dict) else None
    return v if isinstance(v, dict) else None


def parse(payload: str | bytes) -> tuple[str, Event] | None:
    """(event type, event) for one data-channel message, or None if it isn't a typed JSON object."""
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return None
    etype = _s(obj, "type")
    if etype is None:
        return None
    event: Event | None = None
    if etype == "input_transcript.added":
        text = _s(_o(obj, "item"), "text")
        event = UserText(text, False) if text is not None else None
    elif etype == "output_transcript.added":
        text = _s(_o(obj, "item"), "text")
        event = AssistantText(text, False) if text is not None else None
    elif etype == "turn.done":
        turn = _o(obj, "turn")
        text, role = _s(turn, "transcript"), _s(turn, "role")
        if text is not None and role == "user":
            event = UserText(text, True)
        elif text is not None and role == "assistant":
            event = AssistantText(text, True)
    elif etype == "delegation.created":
        item = _o(obj, "item")
        did = _s(item, "id")
        if item is not None and did and _s(item, "type") == "delegation" and _s(item, "target") == "client":
            content = item.get("content") if isinstance(item.get("content"), list) else []
            prompt = "".join(_s(c, "text") or "" for c in content if _s(c, "type") == "input_text")
            event = Delegation(did, prompt)
    elif etype == "error":
        event = Failure(_s(_o(obj, "error"), "code") or _s(obj, "code"))
    elif etype == "session.closed":
        event = Closed(_s(obj, "reason"))
    return etype, (event if event is not None else Other())


def bound(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS - 16].rstrip() + " [truncated]"


def chunk(text: str, max_bytes: int = MAX_FRAME_BYTES) -> list[str]:
    """Split at character boundaries so no piece is over ``max_bytes`` of UTF-8."""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if cur and size + n > max_bytes:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(ch)
        size += n
    if cur:
        out.append("".join(cur))
    return out


def _frames(base: dict[str, Any], text: str) -> list[str]:
    return [json.dumps({**base, "channel": "speakable", "content": [{"type": "input_text", "text": part}]})
            for part in chunk(bound(text))]


def result_frames(delegation_id: str, text: str) -> list[str]:
    """Frames that answer delegation ``delegation_id``; the voice says it in its own words."""
    return _frames({"type": "delegation.context.append", "delegation_item_id": delegation_id}, text)


def note_frames(text: str) -> list[str]:
    """Frames that tell the voice something that happened outside a hand-off; it may mention it."""
    return _frames({"type": "session.context.append"}, text)
