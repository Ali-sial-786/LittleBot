"""Recorded voice lines: fixed sentences LittleBot says, recorded once in the ChatGPT (GPT-Live) voice.

GPT-Live speaks only inside a live call (audio-only WebRTC, no text-to-speech endpoint), so
``scripts/record_voice_lines.py`` (or the dashboard's "Record activity voices" button) records each
fixed line once and saves it here::

    data/voice_lines/<hash>.wav     the line, trimmed and levelled (mono, 22.05 kHz)
    data/voice_lines/index.json     {"version": 1, "lines": {<hash>: {text, hash, voice, date, ...}}}

``<hash>`` is :func:`line_hash` of the line's normalised text (:func:`normalise`: case, punctuation
and spacing don't matter). ``AudioService.say()`` plays the recording when one exists for exactly
that (normalised) line and uses text-to-speech otherwise. ``data/`` is outside the code: updates
never touch the recordings.

Reading is cheap and thread-safe: the index is re-read only when the file changes (a recording made
while LittleBot runs is used at once), decoded clips are cached (a few, bounded). Writes go through
a lock file, so the dashboard and the command line can't clobber each other's index.
"""

from __future__ import annotations

import collections
import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from littlebot.audio import dsp
from littlebot.audio.tts.base import clean_text

log = logging.getLogger("littlebot.audio.voice_lines")

FOLDER = "voice_lines"  # under the data folder
INDEX = "index.json"
VERSION = 1
CLIP_CACHE = 24  # decoded clips kept in memory (~2 s each: about 4 MB at most)

_NOT_WORD = re.compile(r"[^\w\s]")


def normalise(text: str) -> str:
    """The form lines are matched in: cleaned like speech, casefolded, no punctuation, single spaces.

    "Peekaboo! I see you, Amara." and "peekaboo i see you amara" are the same line."""
    s = unicodedata.normalize("NFKC", clean_text(text)).casefold()
    s = s.replace("’", "").replace("'", "")  # "let's" == "lets": apostrophes join, not split
    s = _NOT_WORD.sub(" ", s).replace("_", " ")
    return " ".join(s.split())


def line_hash(text: str) -> str:
    """File name (without .wav) of a line's recording: 16 hex characters of SHA-1 of :func:`normalise`."""
    return hashlib.sha1(normalise(text).encode("utf-8")).hexdigest()[:16]


def default_folder(data_path: str | Path) -> Path:
    return Path(data_path) / FOLDER


_KEY = "voice_lines"


def get_store(ctx: Any) -> "VoiceLineStore":
    """The robot's store (``ctx.extras["voice_lines"]``; data/voice_lines), shared by the audio service and
    the dashboard."""
    store = ctx.extras.get(_KEY)
    if store is None:
        store = ctx.extras[_KEY] = VoiceLineStore(default_folder(ctx.config.data_path))
    return store


class VoiceLineStore:
    """The recorded lines in one folder (``data/voice_lines``)."""

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)
        self.index_path = self.folder / INDEX
        self._lock = threading.Lock()
        self._lines: dict[str, dict[str, Any]] = {}
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size) of the index last read
        self._clips: collections.OrderedDict[str, tuple[np.ndarray, int]] = collections.OrderedDict()

    # ------------------------------------------------------------ reading
    def _fresh(self) -> dict[str, dict[str, Any]]:
        """The index, re-read if the file changed since (one ``stat`` per call). Caller holds the lock."""
        try:
            st = self.index_path.stat()
            stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp = stamp
            self._lines = _read_index(self.index_path) if stamp is not None else {}
            self._clips.clear()  # a line may have been re-recorded
        return self._lines

    def entry(self, text: str) -> dict[str, Any] | None:
        """The index entry recorded for ``text`` (its normalised form), if its clip exists."""
        key = line_hash(text)
        with self._lock:
            entry = self._fresh().get(key)
        if entry is None or not (self.folder / f"{key}.wav").is_file():
            return None
        return dict(entry)

    def has(self, text: str) -> bool:
        return self.entry(text) is not None

    def get(self, text: str) -> tuple[np.ndarray, int, dict[str, Any]] | None:
        """(samples, sample_rate, index entry) of the recording for ``text``, or None. Never raises."""
        if not normalise(text):
            return None
        key = line_hash(text)
        try:
            with self._lock:
                entry = self._fresh().get(key)
                hit = self._clips.get(key) if entry is not None else None
                if hit is not None:
                    self._clips.move_to_end(key)
            if entry is None:
                return None
            if hit is None:
                hit = dsp.read_wav(self.folder / f"{key}.wav")
                with self._lock:
                    self._clips[key] = hit
                    while len(self._clips) > CLIP_CACHE:
                        self._clips.popitem(last=False)
        except (OSError, ValueError) as e:
            log.warning("recorded line %s could not be read (%s); using text-to-speech", key, e)
            return None
        return hit[0], hit[1], dict(entry)

    def entries(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._fresh().items()}

    def recorded(self, texts: list[str]) -> set[str]:
        """The hashes of ``texts`` that have a recording."""
        lines = self.entries()
        return {h for h in (line_hash(t) for t in texts) if h in lines and (self.folder / f"{h}.wav").is_file()}

    def __len__(self) -> int:
        with self._lock:
            return len(self._fresh())

    # ------------------------------------------------------------ writing
    def save(self, text: str, samples: np.ndarray, sample_rate: int, *, voice: str, source: str = "",
             extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store (or replace) the recording of ``text``; returns its index entry."""
        key = line_hash(text)
        self.folder.mkdir(parents=True, exist_ok=True)
        clip = np.asarray(samples, dtype=np.float32).reshape(-1)
        wav = self.folder / f"{key}.wav"
        _atomic_write(wav, dsp.encode_wav(clip, sample_rate))
        entry = {"text": clean_text(text), "hash": key, "voice": voice,
                 "date": _dt.datetime.now().isoformat(timespec="seconds"),
                 "seconds": round(len(clip) / float(sample_rate), 2), "source": source, **(extra or {})}
        with self._writing() as lines:
            lines[key] = entry
        return dict(entry)

    def remove(self, text: str) -> bool:
        key = line_hash(text)
        with self._writing() as lines:
            found = lines.pop(key, None) is not None
        with contextlib.suppress(OSError):
            (self.folder / f"{key}.wav").unlink()
        return found

    @contextlib.contextmanager
    def _writing(self) -> Iterator[dict[str, dict[str, Any]]]:
        """Read-modify-write the index under a lock file (other processes too), then publish it atomically."""
        self.folder.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.folder / ".lock"):
            lines = _read_index(self.index_path)
            yield lines
            body = json.dumps({"version": VERSION, "lines": dict(sorted(lines.items()))}, indent=1, ensure_ascii=False)
            _atomic_write(self.index_path, body.encode("utf-8"))
        with self._lock:
            self._stamp = None  # read it again next time


def _read_index(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("voice line index %s unreadable (%s); ignoring it", path, e)
        return {}
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, dict):
        return {}
    return {str(k): v for k, v in lines.items() if isinstance(v, dict) and re.fullmatch(r"[0-9a-f]{16}", str(k))}


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    try:
        import fcntl  # noqa: PLC0415 - POSIX only (the Pi); elsewhere the in-process lock is enough
    except ImportError:  # pragma: no cover
        yield
        return
    with open(path, "a+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
