"""The gallery's files (littlebot/capture/gallery.py): photos and finished videos, their notes, newest first, private
files, and what a power cut leaves behind."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from littlebot.capture.gallery import Gallery, jpeg_size, summary, valid_id

# A real (tiny) JPEG's start: SOI, then a SOF0 frame header saying 4x3.
JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08\x00\x03\x00\x04\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\x00" * 32 + b"\xff\xd9")


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _gallery(tmp_path: Path, t: float = 1_762_400_000.0) -> tuple[Gallery, Clock]:
    clock = Clock(t)
    return Gallery(tmp_path / "gallery", clock=clock), clock


def test_jpeg_size_reads_the_frame_header() -> None:
    assert jpeg_size(JPEG) == (4, 3)
    assert jpeg_size(b"not a jpeg") == (0, 0)


def test_ids_are_only_the_gallerys_own() -> None:
    assert valid_id("20261106-143105") and valid_id("20261106-143105-2")
    for bad in ("../etc/passwd", "20261106-143105.jpg", "", None, "2026-11-06", "20261106-143105-1234"):
        assert not valid_id(bad)


def test_a_photo_is_kept_with_its_note_and_thumbnail(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    item = g.add_photo(JPEG, taken_at=clock.t, thumb=b"thumb", source="voice", who="Ali")
    assert item.kind == "photo" and (item.width, item.height) == (4, 3) and item.bytes == len(JPEG)
    assert g.file(item).read_bytes() == JPEG and g.thumb(item.id).read_bytes() == b"thumb"
    again = g.get(item.id)
    assert again == item and again.who == "Ali" and again.source == "voice"
    assert item.public()["kind"] == "photo" and item.media_type == "image/jpeg"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_files_are_private(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    item = g.add_photo(JPEG, taken_at=clock.t)
    assert g.root.stat().st_mode & 0o777 == 0o700
    assert g.file(item).stat().st_mode & 0o777 == 0o600


def test_two_photos_in_the_same_second_get_their_own_ids(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    a = g.add_photo(JPEG, taken_at=clock.t)
    b = g.add_photo(JPEG, taken_at=clock.t)
    assert a.id != b.id and valid_id(a.id) and valid_id(b.id)
    assert {it.id for it in g.items()} == {a.id, b.id}


def test_newest_first_and_by_kind(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    first = g.add_photo(JPEG, taken_at=clock.t)
    clock.t += 60
    vid_id = g.new_id()
    g.part(vid_id).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    video = g.finish_video(vid_id, taken_at=clock.t, seconds=12.4, width=1280, height=720, thumb=b"t")
    clock.t += 60
    last = g.add_photo(JPEG, taken_at=clock.t)
    assert [it.id for it in g.items()] == [last.id, video.id, first.id]
    assert [it.id for it in g.items("video")] == [video.id]
    assert g.latest("photo").id == last.id and g.latest().id == last.id
    assert video.seconds == pytest.approx(12.4) and video.media_type == "video/mp4"
    assert not g.part(vid_id).exists() and g.file(video).is_file()
    assert g.usage() == {"photos": 2, "videos": 1, "bytes": 2 * len(JPEG) + video.bytes}
    assert summary(g.items()) == "2 photos and 1 video"
    assert summary([]) == "nothing yet"


def test_an_empty_video_is_not_kept(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    vid_id = g.new_id()
    g.ensure()
    g.part(vid_id).write_bytes(b"")
    assert g.finish_video(vid_id, taken_at=clock.t, seconds=0, width=0, height=0, thumb=None) is None
    assert g.items() == []


def test_delete_removes_every_file(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    item = g.add_photo(JPEG, taken_at=clock.t, thumb=b"t")
    assert g.delete(item.id) is True
    assert g.get(item.id) is None and list(g.root.iterdir()) == []
    assert g.delete(item.id) is False


def test_a_note_without_its_file_is_not_listed(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    item = g.add_photo(JPEG, taken_at=clock.t)
    g.file(item).unlink()
    assert g.get(item.id) is None and g.items() == []


def test_unfinished_recordings_are_cleaned_up(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    g.ensure()
    old, new = g.part("20261101-100000"), g.part("20261106-100000")
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    hours_ago = time.time() - 2 * 3600
    os.utime(old, (hours_ago, hours_ago))
    assert g.clean_parts() == 1
    assert not old.exists() and new.exists()  # one being recorded right now stays


def test_a_broken_note_is_skipped(tmp_path: Path) -> None:
    g, clock = _gallery(tmp_path)
    item = g.add_photo(JPEG, taken_at=clock.t)
    note = g.root / f"{item.id}.json"
    later = note.stat().st_mtime + 10
    note.write_text("{not json")
    os.utime(note, (later, later))  # (the cache goes by the note's time)
    assert g.items() == []
