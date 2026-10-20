"""YouTube link parsing and validation for the parent-approved allowlist.

Only links a parent pastes into the dashboard ever reach the player, and only
after passing through :func:`parse_youtube_url`. The parser never trusts the
raw text: it extracts and validates the 11-character video id and/or the
playlist id, then rebuilds a canonical ``https://www.youtube.com/...`` URL from
those validated parts alone.

Accepted forms (http or https, with or without ``www.``):

* ``youtube.com/watch?v=ID`` (also ``m.``, ``music.``), ``youtu.be/ID``
* ``/shorts/ID``, ``/embed/ID``, ``/live/ID``, ``/v/ID``
* ``youtube-nocookie.com/embed/ID``
* ``/playlist?list=PL...``, ``/embed/videoseries?list=...``
* ``watch?v=ID&list=PL...``  -> a single video unless the parent asks for the playlist
* timestamps ``t=90`` / ``t=1m30s`` / ``start=90`` / ``#t=90``

Rejected with a clear message (all errors subclass :class:`ValueError`):
non-YouTube hosts, ``javascript:`` and other schemes, malformed or overly long
input, channel / search / home pages, channel uploads lists (``UU...``, ``FL...``,
``UL...``: they grow by themselves, so they'd approve a whole channel),
YouTube-generated mixes and private lists.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 100
MAX_START_SECONDS = 12 * 3600

YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
})

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_TIME_RE = re.compile(r"^(?:(\d{1,3})h)?(?:(\d{1,4})m)?(?:(\d{1,6})s?)?$")
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")

# Playlist ids that are generated per user / per session and are not a fixed list a parent can review.
_PRIVATE_LISTS = {"WL": "Watch Later", "LL": "Liked videos", "LM": "Liked music"}

# Auto-generated channel lists (case-sensitive YouTube id prefixes): every upload of a channel
# (``UU`` + channel id, and its ``UULF`` videos / ``UUSH`` shorts / ``UULV`` live / ``UUMO`` /
# ``UUPV`` / ``UUPS`` variants), and the ``FL`` / ``UL`` channel lists. They keep growing with
# whatever the channel posts next, so approving one would approve the whole channel.
_CHANNEL_LIST_PREFIXES = ("UU", "FL", "UL")  # "UU" also covers UULF, UUSH, UULV, UUMO, UUPV, UUPS

CHANNEL_MESSAGE = ("That link is a channel, which can't be approved as a whole. "
                   "Open one video (or one playlist) and copy its link instead.")
CHANNEL_LIST_MESSAGE = ("That playlist is a channel's automatic list of everything it uploads (it keeps "
                        "growing by itself), and whole channels can't be approved. "
                        "Open one video (or one of your own playlists) and copy its link instead.")

# Path prefixes that point at something other than a single video or playlist.
_UNSUPPORTED_PATHS: tuple[tuple[str, str], ...] = (
    ("/channel/", "a channel"), ("/c/", "a channel"), ("/user/", "a channel"), ("/@", "a channel"),
    ("/results", "a search page"), ("/search", "a search page"), ("/hashtag/", "a hashtag page"),
    ("/feed/", "a YouTube feed"), ("/clip/", "a clip"), ("/post/", "a community post"),
    ("/account", "an account page"), ("/redirect", "a redirect link"), ("/attribution_link", "a redirect link"),
    ("/podcasts", "a podcasts page"), ("/gaming", "a gaming page"), ("/browse", "a browse page"),
)


class MediaURLError(ValueError):
    """Base class for every allowlist link problem. ``reason`` is a short machine code."""

    reason = "invalid"


class MalformedURLError(MediaURLError):
    reason = "malformed"


class URLTooLongError(MalformedURLError):
    reason = "too_long"


class NotYouTubeURLError(MediaURLError):
    reason = "not_youtube"


class UnsupportedURLError(MediaURLError):
    reason = "unsupported"


class InvalidMediaIdError(MediaURLError):
    reason = "invalid_id"


class ChannelURLError(UnsupportedURLError):
    """A whole channel (channel page or its auto-growing uploads list): can't be approved as a whole."""


class DuplicateItemError(ValueError):
    """The video / playlist is already on the allowlist."""

    reason = "duplicate"

    def __init__(self, message: str, existing_id: int | None = None) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class AllowlistFullError(ValueError):
    reason = "full"


MediaKind = Literal["video", "playlist"]


@dataclass(frozen=True)
class ParsedMedia:
    kind: MediaKind
    video_id: str | None  # the video (or, for a playlist, the optional first video)
    list_id: str | None
    start_seconds: int
    canonical_url: str
    has_playlist: bool = False  # a watch?v=..&list=.. link: the parent could choose the playlist instead

    @property
    def dedupe_key(self) -> str:
        return f"playlist:{self.list_id}" if self.kind == "playlist" else f"video:{self.video_id}"


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def is_valid_video_id(value: object) -> bool:
    return isinstance(value, str) and bool(_VIDEO_ID_RE.match(value))


def validate_video_id(value: str) -> str:
    if not is_valid_video_id(value):
        raise InvalidMediaIdError(
            "That doesn't look like a valid YouTube video link (the video id must be 11 letters, digits, - or _)."
        )
    return value


def validate_list_id(value: str) -> str:
    if not isinstance(value, str) or not _LIST_ID_RE.match(value):
        raise InvalidMediaIdError("That doesn't look like a valid YouTube playlist id.")
    if value in _PRIVATE_LISTS:
        raise UnsupportedURLError(
            f"'{_PRIVATE_LISTS[value]}' is a private list. Make a public or unlisted playlist and add that link."
        )
    if value.startswith(_CHANNEL_LIST_PREFIXES):
        raise ChannelURLError(CHANNEL_LIST_MESSAGE)
    if value.startswith("RD"):
        raise UnsupportedURLError(
            "YouTube 'Mix' lists are picked automatically by YouTube, so they can't be approved. "
            "Add single videos or one of your own playlists instead."
        )
    if len(value) < 12:
        raise InvalidMediaIdError("That doesn't look like a valid YouTube playlist id.")
    return value


def parse_timestamp(value: str | None) -> int:
    """'90', '90s', '1m30s', '1h2m3s' -> seconds. Unparseable / out-of-range values give 0."""
    if not value:
        return 0
    text = value.strip().lower()
    if not text:
        return 0
    m = _TIME_RE.match(text)
    if not m or not any(m.groups()):
        return 0
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    total = h * 3600 + mi * 60 + s
    return total if 0 <= total <= MAX_START_SECONDS else 0


def sanitize_title(title: object, *, default: str = "", max_length: int = MAX_TITLE_LENGTH) -> str:
    """Strip control / formatting characters (incl. bidi overrides), collapse whitespace, cap length.

    The result is plain text: the dashboard must still HTML-escape it when rendering.
    """
    if title is None:
        return default
    if not isinstance(title, str):
        raise ValueError("The title must be text.")
    text = unicodedata.normalize("NFC", title[: max_length * 4])
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\t\n\r":
            cleaned.append(" ")
        elif cat.startswith("C") or cat in ("Zl", "Zp"):  # Cc control, Cf format (bidi), Co, Cs, Cn
            continue
        else:
            cleaned.append(ch)
    out = " ".join("".join(cleaned).split())
    if len(out) > max_length:
        out = out[: max_length - 1].rstrip() + "…"
    return out or default


def default_title(parsed: ParsedMedia) -> str:
    if parsed.kind == "playlist":
        return f"YouTube playlist {parsed.list_id}"
    return f"YouTube video {parsed.video_id}"


def canonical_url(kind: MediaKind, video_id: str | None, list_id: str | None, start_seconds: int = 0) -> str:
    if kind == "playlist":
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}&list={list_id}"
        return f"https://www.youtube.com/playlist?list={list_id}"
    url = f"https://www.youtube.com/watch?v={video_id}"
    if start_seconds:
        url += f"&t={int(start_seconds)}s"
    return url


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_youtube_url(url: object, *, kind: str | None = None) -> ParsedMedia:
    """Validate a pasted YouTube link and return its canonical form.

    ``kind``: None (default: a video link stays a video; a playlist-only link is a playlist),
    ``"video"`` or ``"playlist"`` (e.g. a parent choosing the whole playlist of a
    ``watch?v=..&list=..`` link). Raises a :class:`MediaURLError` subclass.
    """
    if kind not in (None, "video", "playlist"):
        raise ValueError("kind must be 'video' or 'playlist'")
    if not isinstance(url, str):
        raise MalformedURLError("Please paste a YouTube link as text.")
    if len(url) > MAX_URL_LENGTH:
        raise URLTooLongError(f"That link is too long (more than {MAX_URL_LENGTH} characters).")
    text = url.strip()
    if not text:
        raise MalformedURLError("Please paste a YouTube link.")
    if any(unicodedata.category(c).startswith("C") or c.isspace() for c in text) or "\\" in text:
        raise MalformedURLError("That link contains spaces or hidden characters. Please copy it again.")

    m = _SCHEME_RE.match(text)
    if m:
        scheme = m.group(1).lower()
        if scheme not in ("http", "https"):
            raise MalformedURLError("Only YouTube web links (https://...) can be added.")
        if not text[m.end():].startswith("//"):
            raise MalformedURLError("That doesn't look like a complete web link.")
    elif text.startswith("//"):
        text = "https:" + text
    else:
        first = text.split("/", 1)[0]
        if "." not in first:
            raise MalformedURLError("Please paste the full YouTube link (for example https://youtu.be/...).")
        text = "https://" + text

    try:
        parts = urlsplit(text)
        port = parts.port
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError as e:
        raise MalformedURLError("That doesn't look like a valid web link.") from e
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise MalformedURLError("That link has an unusual format (user name in the address). Please copy it again.")
    if not host:
        raise MalformedURLError("That doesn't look like a valid web link.")
    if host not in YOUTUBE_HOSTS:
        raise NotYouTubeURLError("Only YouTube links can be added (youtube.com or youtu.be).")
    if port not in (None, 80, 443):
        raise MalformedURLError("That link has an unusual format (port number). Please copy it again.")

    query = parse_qs(parts.query, keep_blank_values=False)

    def q(name: str) -> str | None:
        vals = query.get(name)
        return vals[0] if vals else None

    fragment = parse_qs(parts.fragment) if "=" in parts.fragment else {}
    start = parse_timestamp(q("t") or q("start") or q("time_continue") or (fragment.get("t") or [None])[0])
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    segments = [s for s in path.split("/") if s]
    video_id: str | None = None
    list_id: str | None = q("list")

    if host.endswith("youtu.be"):
        if len(segments) != 1:
            raise UnsupportedURLError("That youtu.be link doesn't point to a video.")
        video_id = validate_video_id(segments[0])
    else:
        lower = path.lower()
        for prefix, what in _UNSUPPORTED_PATHS:
            if lower.startswith(prefix):
                if what == "a channel":
                    raise ChannelURLError(CHANNEL_MESSAGE)
                raise UnsupportedURLError(
                    f"That link is {what}, which can't be approved as a whole. "
                    "Open one video (or one playlist) and copy its link instead."
                )
        head = segments[0].lower() if segments else ""
        if head == "watch" and len(segments) == 1:
            if q("v") is not None:
                video_id = validate_video_id(q("v") or "")
            elif list_id is None:
                raise MalformedURLError("That YouTube link is missing the video id (v=...).")
        elif head in ("shorts", "embed", "live", "v", "e") and len(segments) == 2:
            if head == "embed" and segments[1] == "videoseries":
                if list_id is None:
                    raise MalformedURLError("That playlist embed link is missing the playlist id (list=...).")
            else:
                video_id = validate_video_id(segments[1])
        elif head == "playlist" and len(segments) == 1:
            if list_id is None:
                raise MalformedURLError("That playlist link is missing the playlist id (list=...).")
        elif not segments:
            raise UnsupportedURLError("That's the YouTube home page. Open a video and copy its link instead.")
        else:
            raise UnsupportedURLError(
                "That kind of YouTube page isn't supported. Open a video or a playlist and copy its link."
            )

    has_playlist = False
    if list_id is not None:
        if video_id is not None and kind != "playlist":
            # A video opened from a playlist / mix: keep just the video. Validate quietly so an
            # odd list id (e.g. a mix) does not block adding the single video.
            try:
                validate_list_id(list_id)
                has_playlist = True
            except MediaURLError:
                has_playlist = False
            list_id = None
        else:
            list_id = validate_list_id(list_id)

    if kind == "playlist":
        if list_id is None:
            raise UnsupportedURLError("That link is a single video, not a playlist.")
        resolved: MediaKind = "playlist"
    elif kind == "video":
        if video_id is None:
            raise UnsupportedURLError("That link is a playlist, not a single video.")
        resolved = "video"
    else:
        resolved = "video" if video_id is not None else "playlist"

    if resolved == "video":
        return ParsedMedia("video", video_id, None, start, canonical_url("video", video_id, None, start), has_playlist)
    return ParsedMedia("playlist", video_id, list_id, start if video_id else 0,
                       canonical_url("playlist", video_id, list_id), False)
