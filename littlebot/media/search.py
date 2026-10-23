"""YouTube search without an API key, through yt-dlp.

    yt-dlp --flat-playlist -J --ignore-config "ytsearch5:QUERY"

``--flat-playlist`` reads only YouTube's search results page (no video page is fetched), so a
search takes a second or two. yt-dlp runs as a child process (``python -m yt_dlp``, or ``yt-dlp`` on
PATH): it needs no compiling (a pure-Python wheel, fine on the Pi's aarch64), its memory is given
back when it exits, and a hang is killed by the timeout. The query is one argv element after the
``ytsearchN:`` prefix (no shell), so it is always a search, never an option or a URL to download.

Results are only ids, titles, channels and durations; playing one goes through the normal player
(``MediaController.play_video``, which keeps the time limits and the player's video-id check).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from littlebot.media.allowlist import is_valid_video_id, sanitize_title

log = logging.getLogger("littlebot.media.search")

MAX_RESULTS = 10
MAX_QUERY_CHARS = 100
DEFAULT_TIMEOUT_S = 20.0


class SearchError(Exception):
    """``kind``: unavailable (yt-dlp not installed) | timeout | failed | bad_query."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


@dataclass(frozen=True)
class SearchResult:
    video_id: str
    title: str
    channel: str = ""
    duration_seconds: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Searcher(Protocol):
    """What the media controller needs; tests pass a fake."""

    @property
    def available(self) -> bool: ...

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]: ...


def clean_query(query: object) -> str:
    """One line of plain text, at most MAX_QUERY_CHARS; raises SearchError(bad_query) if empty."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(query or ""))
    text = " ".join(text.split())[:MAX_QUERY_CHARS].strip()
    if not text:
        raise SearchError("bad_query", "nothing to search for")
    return text


def ytdlp_command(configured: str | None = None) -> list[str] | None:
    """How to run yt-dlp here: the configured command, this Python's yt_dlp module, or yt-dlp on PATH."""
    if configured and configured.strip():
        return shlex.split(configured)
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    return [exe] if exe else None


def parse_results(raw: str | bytes, limit: int = MAX_RESULTS) -> list[SearchResult]:
    """Videos from yt-dlp's ``-J`` output for a ``ytsearchN:`` query (bad entries are skipped)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise SearchError("failed", "yt-dlp did not return JSON") from None
    entries = data.get("entries") if isinstance(data, dict) else None
    out: list[SearchResult] = []
    seen: set[str] = set()
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        vid = e.get("id")
        if not is_valid_video_id(vid) or vid in seen:
            continue
        if e.get("live_status") in ("is_upcoming", "post_live") or e.get("availability") in ("private",
                                                                                              "needs_auth"):
            continue
        title = _text(e.get("title")) or "YouTube video"
        channel = _text(e.get("channel")) or _text(e.get("uploader"))
        duration = e.get("duration")
        seconds = int(duration) if isinstance(duration, (int, float)) and duration > 0 else None
        seen.add(vid)
        out.append(SearchResult(video_id=vid, title=title, channel=channel, duration_seconds=seconds))
        if len(out) >= limit:
            break
    return out


def _text(value: object) -> str:
    return sanitize_title(value) if isinstance(value, str) else ""


class YtDlpSearcher:
    """``search(query)``: the first few YouTube results, via a yt-dlp child process."""

    def __init__(self, command: str | list[str] | None = None, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._configured = command
        self.timeout = float(timeout)

    def command(self) -> list[str] | None:
        if isinstance(self._configured, list):
            return list(self._configured) or None
        return ytdlp_command(self._configured)

    @property
    def available(self) -> bool:
        return self.command() is not None

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        text = clean_query(query)
        limit = max(1, min(MAX_RESULTS, int(limit)))
        cmd = self.command()
        if cmd is None:
            raise SearchError("unavailable", "yt-dlp is not installed")
        args = [*cmd, "--flat-playlist", "--dump-single-json", "--no-warnings", "--ignore-config",
                "--no-cache-dir", "--socket-timeout", "10", f"ytsearch{limit}:{text}"]
        try:
            proc = await asyncio.to_thread(subprocess.run, args, capture_output=True, timeout=self.timeout,
                                           stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            raise SearchError("timeout", "the YouTube search took too long") from None
        except OSError as e:
            raise SearchError("unavailable", f"yt-dlp could not run ({type(e).__name__})") from None
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            log.warning("youtube search failed (exit %s): %s", proc.returncode, (err[-1] if err else "")[:200])
            raise SearchError("failed", "the YouTube search failed")
        return parse_results(proc.stdout, limit)


__all__ = ["SearchError", "SearchResult", "Searcher", "YtDlpSearcher", "clean_query", "parse_results",
           "ytdlp_command"]
