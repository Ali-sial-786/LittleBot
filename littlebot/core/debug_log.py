"""LittleBot's own debug log, read back for LittleBot itself ("what went wrong?").

The tools' ``read_debug_log`` (littlebot/chatgpt/tools.py) gives the text model the last minutes of the JSON log
(``logging.file``, data/logs/littlebot.log and its rotated copies) as a compact timeline, so a grown-up can ask
LittleBot why something didn't work and it can work it out. Like scripts/log_timeline.py: damaged lines are
skipped, the voice's many "LISTENING -> SPEAKING" turns are folded into one line, gaps are marked, and warnings and
errors stand out (with the last lines of a traceback). ``about``: only lines with one of these words (warnings and
errors always stay).

What is in it: what LittleBot did, timings, warnings and errors. Modules log only kinds, counts and timings, not
what people said or what LittleBot answered; the one exception, the fast decisions' context (``decide: ... |
they_asked: ...``, littlebot/decide, ``decide.log_context``) for a parent to read, is cut off here, so none of it
reaches the text model. Secrets are redacted as the log is written (core/logging_setup.py). Reading changes
nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

MAX_MINUTES = 120
MAX_READ_BYTES = 3_000_000  # of the newest files, read from the end
MAX_LINES = 160  # the newest are kept
MAX_CHARS = 9000
GAP_S = 60.0
NOISY_LOGGERS = ("uvicorn.access", "httpx", "httpcore", "aiortc", "aioice", "PIL", "asyncio")
FLIP = re.compile(r"^state (LISTENING|SPEAKING|THINKING) -> (LISTENING|SPEAKING|THINKING) \(live voice\)$")
CALL_START = "live voice: parent started a conversation"
CALL_END = re.compile(r"live voice: ending \((.*)\)$")
MSG_CHARS = 300
# What some lines mean, added after the lines when one of them is there (the model can't look at the code).
HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"HTTP 429|\b429\b|rate limit|quota", re.I),
     "HTTP 429 means ChatGPT's usage limit on the family's plan was reached; it works again once the plan's "
     "allowance resets (nothing is broken). An 'uh-oh' sound when the voice chat won't start is this."),
    (re.compile(r"HTTP 40[13]\b|sign-in (?:failed|no longer valid)|unauthori", re.I),
     "The ChatGPT sign-in was refused or ran out: a grown-up signs in again on the dashboard (ChatGPT)."),
    (re.compile(r"stood still|memory is low|swap", re.I),
     "The Pi ran short of memory and stalled for a while; the browser view and videos use the "
     "most. Closing them helps."),
    (re.compile(r"took too long|timed out|TimeoutError", re.I),
     "Something took too long: usually slow Wi-Fi or a busy ChatGPT."),
    (re.compile(r"unknown card", re.I),
     "The screen was asked for a kind of info card it doesn't have, so nothing was shown for that turn."),
    (re.compile(r"not sure from just now|too_short", re.I),
     "Voice recognition heard too little of their voice to be sure who was talking (it needs about a second "
     "of clear speech)."),
    (re.compile(r"camera (?:lost|read failed)|no photo", re.I),
     "The camera stopped giving pictures; it is retried by itself (a loose USB cable can do this)."),
    (re.compile(r"decide: .*(?:http_401|http_403|no_key|isn't set)", re.I),
     ("The fast decision model (Jev or OpenAI's Decisions API) refused its key, or it isn't in .env: LittleBot "
      "uses its own way meanwhile.")),
    (re.compile(r"decide: .*(?:timeout|network|http_5|http_429)", re.I),
     "The fast decision model was slow or unreachable, so LittleBot decided its own way that time."),
    (re.compile(r"microphone (?:unavailable|failed)|stopped \(nothing from it", re.I),
     "The microphone or speaker stopped working for a moment (USB audio); LittleBot tries to reopen it."),
)


def log_path(config: Any) -> Path | None:
    """The JSON log file LittleBot writes (``logging.file``, under the data folder), or None when there is none."""
    name = getattr(getattr(config, "logging", None), "file", None)
    if not name:
        return None
    path = Path(name)
    return path if path.is_absolute() else Path(config.data_path) / path


def _files(path: Path) -> list[Path]:
    """Oldest first: littlebot.log.3, .2, .1, then littlebot.log."""
    rotated = [p for p in path.parent.glob(path.name + ".*") if p.suffix[1:].isdigit()]
    rotated.sort(key=lambda p: -int(p.suffix[1:]))
    return rotated + ([path] if path.exists() else [])


def _raw_lines(path: Path) -> list[bytes]:
    """The newest ``MAX_READ_BYTES`` of the log, across its rotated copies, oldest first."""
    chunks: list[bytes] = []
    left = MAX_READ_BYTES
    for p in reversed(_files(path)):
        if left <= 0:
            break
        try:
            with p.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - left))
                data = f.read()
        except OSError:
            continue
        left -= len(data)
        chunks.insert(0, data)
    return b"\n".join(chunks).split(b"\n")


def records(path: Path, since: dt.datetime) -> list[dict[str, Any]]:
    out = []
    for raw in _raw_lines(path):
        line = raw.replace(b"\x00", b"").decode("utf-8", "replace").strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
            d["_t"] = dt.datetime.fromisoformat(str(d["ts"]))
        except (ValueError, KeyError, TypeError):
            continue
        if d["_t"] >= since:
            msg = str(d.get("msg") or "")
            if msg.startswith("decide: ") and " | " in msg:
                d["msg"] = msg.split(" | ", 1)[0]  # a decision's context (what was heard and said) stays out
            out.append(d)
    return out


def _wanted(d: dict[str, Any], about: list[str]) -> bool:
    level = str(d.get("level") or "")
    if level in ("WARNING", "ERROR", "CRITICAL"):
        return True
    if level != "INFO" or str(d.get("logger") or "").startswith(NOISY_LOGGERS):
        return False
    if not about:
        return True
    text = f"{d.get('logger') or ''} {d.get('msg') or ''}".lower()
    return any(word in text for word in about)


def read(config: Any, *, minutes: float = 15.0, about: str = "", now: dt.datetime | None = None) -> dict[str, Any]:
    """The last ``minutes`` of the log: ``{ok, minutes, lines: [...], summary: {...}, truncated}``, or ``{ok: False,
    error}`` when there is no log file."""
    path = log_path(config)
    if path is None or not _files(path):
        return {"ok": False, "error": "no_log", "detail": "LittleBot isn't writing a log file (logging.file)."}
    minutes = max(1.0, min(float(MAX_MINUTES), float(minutes)))
    now = now or dt.datetime.now()
    since = now - dt.timedelta(minutes=minutes)
    words = [w for w in re.split(r"[,;]+|\s+or\s+", str(about or "").lower()) if w.strip()]
    words = [w.strip()[:40] for w in words][:6]
    lines: list[str] = []
    summary: dict[str, Any] = {"voice_chats": 0, "chat_endings": {}, "wake_words": 0, "warnings": 0, "errors": 0}
    flips, flip_at = 0, None
    last: dt.datetime | None = None

    def flush() -> None:
        nonlocal flips, flip_at
        if flips:
            lines.append(f"{flip_at:%H:%M:%S}  (the voice took {flips} turns of talking and listening)")
        flips, flip_at = 0, None

    for d in records(path, since):
        t, msg, level = d["_t"], str(d.get("msg") or ""), str(d.get("level") or "")
        if last is not None and (t - last).total_seconds() >= GAP_S:
            flush()
            lines.append(f"          ... nothing logged for {int((t - last).total_seconds())} s ...")
        last = t
        if msg.startswith(CALL_START):
            summary["voice_chats"] += 1
        m = CALL_END.search(msg)
        if m:
            summary["chat_endings"][m.group(1)] = summary["chat_endings"].get(m.group(1), 0) + 1
        if msg.startswith("wake word heard"):
            summary["wake_words"] += 1
        if level == "WARNING":
            summary["warnings"] += 1
        elif level in ("ERROR", "CRITICAL"):
            summary["errors"] += 1
        if not _wanted(d, words):
            continue
        if FLIP.match(msg):
            if not words:
                flip_at = flip_at or t
                flips += 1
            continue
        flush()
        tag = {"WARNING": "WARN ", "ERROR": "ERROR", "CRITICAL": "ERROR"}.get(level, "     ")
        lines.append(f"{t:%H:%M:%S} {tag} {msg[:MSG_CHARS]}")
        exc = [ln.strip() for ln in str(d.get("exc") or "").splitlines() if ln.strip()]
        lines.extend(f"                 | {ln[:200]}" for ln in exc[-3:])
    flush()
    truncated = False
    if len(lines) > MAX_LINES:
        lines, truncated = lines[-MAX_LINES:], True
    while lines and sum(len(ln) + 1 for ln in lines) > MAX_CHARS:
        lines.pop(0)
        truncated = True
    seen = "\n".join(lines)
    hints = [text for pattern, text in HINTS if pattern.search(seen)]
    return {"ok": True, "minutes": minutes, "about": words, "lines": lines, "summary": summary,
            "truncated": truncated, "now": f"{now:%H:%M:%S}", "hints": hints}


def as_text(result: dict[str, Any]) -> str:
    """``read``'s result as one block of text for a model."""
    if not result.get("ok"):
        return f"error: {result.get('detail') or result.get('error')}"
    s = result["summary"]
    head = (f"LittleBot's debug log, the last {result['minutes']:.0f} min (now {result['now']})"
            + (f", only lines about {', '.join(result['about'])} (and every warning and error)" if result["about"]
               else "") + (", the oldest lines left out" if result["truncated"] else "") + ". "
            f"Voice chats started: {s['voice_chats']}; how they ended: {json.dumps(s['chat_endings'])}; wake words "
            f"heard: {s['wake_words']}; warnings: {s['warnings']}; errors: {s['errors']}.")
    out = head + "\n" + ("\n".join(result["lines"]) or "(nothing logged in that time)")
    if result.get("hints"):
        out += "\nWhat some of these mean:\n" + "\n".join(f"- {h}" for h in result["hints"])
    return out


__all__ = ["as_text", "log_path", "read", "records"]
