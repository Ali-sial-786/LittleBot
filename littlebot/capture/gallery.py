"""The gallery: the photos and videos LittleBot took because someone asked, as plain files on the robot.

Folder ``data/<capture.folder>`` (``data/gallery``). One item is

* ``<id>.jpg`` (a photo) or ``<id>.mp4`` (a video: H.264 + AAC, plays on any phone),
* ``<id>.thumb.jpg``, a small preview (the dashboard's grid, a video's still on the robot's screen),
* ``<id>.json``: when it was taken, its size, a video's length, who asked for it ("voice" / "dashboard") and,
  when voice recognition knew, their name.

An id is the local time it was taken, ``20261106-143012`` (``-2``, ``-3`` ... for more in the same second): sorted
by time, safe in a file name and in a web address. A video is written as ``<id>.mp4.part`` and renamed once it is
finished, so a half-written one never shows; a ``.part`` left by a power cut is removed at the next start.

Nothing here leaves the robot: the share links (littlebot/capture/service.py) serve these files to a phone on the
home Wi-Fi.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import struct
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("littlebot.capture")

ID_RE = re.compile(r"^\d{8}-\d{6}(?:-\d{1,3})?$")
KINDS = ("photo", "video")
EXT = {"photo": ".jpg", "video": ".mp4"}
MEDIA_TYPES = {"photo": "image/jpeg", "video": "video/mp4"}
THUMB = ".thumb.jpg"
META = ".json"
PART = ".part"
STALE_PART_S = 3600.0  # a .part older than this at start is what a power cut left
MAX_META_BYTES = 8 * 1024


@dataclass(frozen=True)
class Item:
    id: str
    kind: str  # photo | video
    taken_at: float  # wall-clock seconds
    width: int = 0
    height: int = 0
    seconds: float | None = None  # a video's length
    bytes: int = 0
    source: str = "voice"  # voice | dashboard
    who: str | None = None  # who asked, when voice recognition knew

    @property
    def filename(self) -> str:
        return f"{self.id}{EXT[self.kind]}"

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.kind]

    def public(self) -> dict[str, Any]:
        return asdict(self)


def valid_id(item_id: Any) -> bool:
    return isinstance(item_id, str) and bool(ID_RE.match(item_id))


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a JPEG's frame header, (0, 0) when it isn't one. No decoding."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return 0, 0
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return int(w), int(h)
        if marker == 0xDA:  # the picture data starts: no frame header before it
            break
        i += 2 + length
    return 0, 0


class Gallery:
    """The files in one folder. Thread-safe; every method is quick (small files, a directory listing)."""

    def __init__(self, root: str | os.PathLike[str], *, clock: Any = time.time) -> None:
        self.root = Path(root)
        self._clock = clock
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, Item]] = {}  # id -> (meta mtime, item)

    # ------------------------------------------------------------ paths
    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)  # family photos: only LittleBot's user
        return self.root

    def file(self, item: Item) -> Path:
        return self.root / item.filename

    def thumb(self, item_id: str) -> Path:
        return self.root / f"{item_id}{THUMB}"

    def _meta(self, item_id: str) -> Path:
        return self.root / f"{item_id}{META}"

    def part(self, item_id: str) -> Path:
        """Where a video is written while it is being recorded."""
        return self.root / f"{item_id}{EXT['video']}{PART}"

    def new_id(self, now: float | None = None) -> str:
        """A fresh id for something taken now (local time; never one already used)."""
        base = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock() if now is None else now))
        with self._lock:
            for n in range(1, 1000):
                cand = base if n == 1 else f"{base}-{n}"
                if not any((self.root / f"{cand}{s}").exists() for s in (META, EXT["photo"], EXT["video"],
                                                                          EXT["video"] + PART)):
                    return cand
        raise RuntimeError("too many photos in one second")

    # ------------------------------------------------------------ writing
    def add_photo(self, jpeg: bytes, *, taken_at: float | None = None, thumb: bytes | None = None,
                  source: str = "voice", who: str | None = None, item_id: str | None = None) -> Item:
        """Save a photo (and its preview); returns the new item."""
        self.ensure()
        with self._lock:
            item_id = item_id or self.new_id(taken_at)
            w, h = jpeg_size(jpeg)
            item = Item(id=item_id, kind="photo", taken_at=float(taken_at if taken_at is not None else self._clock()),
                        width=w, height=h, bytes=len(jpeg), source=str(source)[:20], who=_who(who))
            _write_atomic(self.file(item), jpeg)
            if thumb:
                _write_atomic(self.thumb(item_id), thumb)
            self._write_meta(item)
            return item

    def finish_video(self, item_id: str, *, taken_at: float, seconds: float, width: int, height: int,
                     thumb: bytes | None = None, source: str = "voice", who: str | None = None) -> Item | None:
        """The recording ended: ``<id>.mp4.part`` becomes ``<id>.mp4``. None when nothing was written."""
        part = self.part(item_id)
        with self._lock:
            try:
                size = part.stat().st_size
            except OSError:
                return None
            if size <= 0:
                with contextlib.suppress(OSError):
                    part.unlink()
                return None
            item = Item(id=item_id, kind="video", taken_at=float(taken_at), width=int(width), height=int(height),
                        seconds=round(max(0.0, float(seconds)), 1), bytes=size, source=str(source)[:20], who=_who(who))
            os.replace(part, self.file(item))
            if thumb:
                _write_atomic(self.thumb(item_id), thumb)
            self._write_meta(item)
            return item

    def discard_part(self, item_id: str) -> None:
        with contextlib.suppress(OSError):
            self.part(item_id).unlink()

    def _write_meta(self, item: Item) -> None:
        _write_atomic(self._meta(item.id), json.dumps(item.public(), separators=(",", ":")).encode())
        with contextlib.suppress(OSError):
            self._cache[item.id] = (self._meta(item.id).stat().st_mtime, item)

    def clean_parts(self) -> int:
        """Remove what a power cut left mid-recording (old ``.part`` files). Returns how many."""
        n = 0
        if not self.root.is_dir():
            return 0
        now = time.time()
        for p in self.root.glob(f"*{PART}"):
            with contextlib.suppress(OSError):
                if now - p.stat().st_mtime > STALE_PART_S:
                    p.unlink()
                    n += 1
        return n

    # ------------------------------------------------------------ reading
    def get(self, item_id: str) -> Item | None:
        if not valid_id(item_id):
            return None
        meta = self._meta(item_id)
        try:
            mtime = meta.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            cached = self._cache.get(item_id)
            if cached is not None and cached[0] == mtime:
                item = cached[1]
            else:
                item = _read_meta(meta, item_id)
                if item is None:
                    return None
                self._cache[item_id] = (mtime, item)
        return item if self.file(item).is_file() else None

    def items(self, kind: str | None = None, *, limit: int | None = None) -> list[Item]:
        """Newest first. ``kind``: "photo" / "video" / None (both)."""
        if not self.root.is_dir():
            return []
        ids = sorted((p.name[:-len(META)] for p in self.root.glob(f"*{META}")), reverse=True)
        out: list[Item] = []
        for item_id in ids:
            if not valid_id(item_id):
                continue
            item = self.get(item_id)
            if item is None or (kind and item.kind != kind):
                continue
            out.append(item)
            if limit is not None and len(out) >= limit:
                break
        return out

    def latest(self, kind: str | None = None) -> Item | None:
        found = self.items(kind, limit=1)
        return found[0] if found else None

    def delete(self, item_id: str) -> bool:
        item = self.get(item_id)
        if item is None:
            return False
        with self._lock:
            for p in (self.file(item), self.thumb(item_id), self._meta(item_id)):
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
            self._cache.pop(item_id, None)
        return True

    def usage(self) -> dict[str, int]:
        photos = videos = size = 0
        for item in self.items():
            size += item.bytes
            if item.kind == "photo":
                photos += 1
            else:
                videos += 1
        return {"photos": photos, "videos": videos, "bytes": size}

    def free_mb(self) -> int:
        target = self.root if self.root.exists() else self.root.parent
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            return int(shutil.disk_usage(target).free // (1024 * 1024))
        except OSError:
            return 0


def _who(who: str | None) -> str | None:
    s = " ".join(str(who or "").split())[:40]
    return s or None


def _read_meta(path: Path, item_id: str) -> Item | None:
    try:
        if path.stat().st_size > MAX_META_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        return None
    try:
        seconds = data.get("seconds")
        return Item(id=item_id, kind=str(data["kind"]), taken_at=float(data.get("taken_at") or 0.0),
                    width=int(data.get("width") or 0), height=int(data.get("height") or 0),
                    seconds=None if seconds is None else float(seconds), bytes=int(data.get("bytes") or 0),
                    source=str(data.get("source") or "voice")[:20], who=_who(data.get("who")))
    except (TypeError, ValueError):
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.replace(tmp, path)


def summary(items: Iterable[Item]) -> str:
    """"3 photos and 1 video" (for the voice)."""
    photos = videos = 0
    for it in items:
        photos += it.kind == "photo"
        videos += it.kind == "video"
    parts = []
    if photos:
        parts.append(f"{photos} photo{'s' if photos != 1 else ''}")
    if videos:
        parts.append(f"{videos} video{'s' if videos != 1 else ''}")
    return " and ".join(parts) or "nothing yet"


__all__ = ["EXT", "Gallery", "ID_RE", "Item", "KINDS", "MEDIA_TYPES", "jpeg_size", "summary", "valid_id"]
