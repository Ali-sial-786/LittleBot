"""Allowlist CRUD, duplicates, ordering and daily usage persistence."""

from __future__ import annotations

import datetime as dt

import pytest

from littlebot.core.storage import Database
from littlebot.media.allowlist import (
    AllowlistFullError, DuplicateItemError, MediaURLError, parse_youtube_url,
)
from littlebot.media.repository import AllowlistRepository, UsageRepository

V1, V2, V3 = "dQw4w9WgXcQ", "M7lc1UVf-VE", "aqz-KE-bpKQ"
PL = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


@pytest.fixture
def repo():
    db = Database(":memory:")
    yield AllowlistRepository(db, max_items=5)
    db.close()


def _add(repo, url, title=None, **kw):
    return repo.add(parse_youtube_url(url, **kw), title)


def test_add_list_get_remove(repo):
    a = _add(repo, f"https://youtu.be/{V1}?t=30", "Twinkle")
    assert a["kind"] == "video" and a["youtube_id"] == V1 and a["list_id"] is None
    assert a["title"] == "Twinkle" and a["title_source"] == "parent"
    assert a["url"] == f"https://www.youtube.com/watch?v={V1}&t=30s" and a["start_seconds"] == 30
    assert a["enabled"] is True and a["added_at"] and a["play_count"] == 0 and a["last_error"] is None
    assert a["thumbnail_url"] == f"https://i.ytimg.com/vi/{V1}/mqdefault.jpg"
    p = _add(repo, f"https://www.youtube.com/playlist?list={PL}")
    assert p["kind"] == "playlist" and p["list_id"] == PL and p["youtube_id"] is None
    assert p["title"] == f"YouTube playlist {PL}" and p["title_source"] == "default"
    assert p["thumbnail_url"] is None
    assert [i["id"] for i in repo.list()] == [a["id"], p["id"]]
    assert repo.get(a["id"])["title"] == "Twinkle"
    assert repo.get(999) is None
    assert repo.remove(a["id"]) is True
    assert repo.remove(a["id"]) is False
    assert [i["id"] for i in repo.list()] == [p["id"]]


def test_duplicates_rejected_across_url_forms(repo):
    first = _add(repo, f"https://www.youtube.com/watch?v={V1}", "One")
    for dup in (f"https://youtu.be/{V1}", f"https://m.youtube.com/watch?v={V1}&t=40",
                f"https://www.youtube.com/shorts/{V1}", f"https://www.youtube-nocookie.com/embed/{V1}"):
        with pytest.raises(DuplicateItemError, match="already on the approved list") as info:
            _add(repo, dup, "Again")
        assert info.value.existing_id == first["id"]
        assert isinstance(info.value, ValueError)
    _add(repo, f"https://www.youtube.com/playlist?list={PL}")
    with pytest.raises(DuplicateItemError, match="playlist"):
        _add(repo, f"https://www.youtube.com/watch?v={V2}&list={PL}", kind="playlist")
    # The same video as a single item is still separate from the playlist.
    _add(repo, f"https://www.youtube.com/watch?v={V2}&list={PL}")
    assert repo.count() == 3


def test_titles_are_sanitised_and_length_limited(repo):
    item = _add(repo, f"https://youtu.be/{V1}", "  Baby‮  shark\n\x00 " + "!" * 300)
    assert "‮" not in item["title"] and "\x00" not in item["title"] and "\n" not in item["title"]
    assert item["title"].startswith("Baby shark !!!") and len(item["title"]) <= 100
    blank = _add(repo, f"https://youtu.be/{V2}", "   ​ ")
    assert blank["title"] == f"YouTube video {V2}" and blank["title_source"] == "default"
    with pytest.raises(ValueError):
        repo.update(item["id"], title="  ")
    assert repo.update(item["id"], title="New name")["title"] == "New name"
    assert repo.set_title_if_default(blank["id"], "From oEmbed") is True
    assert repo.get(blank["id"])["title_source"] == "oembed"
    assert repo.set_title_if_default(item["id"], "Should not replace") is False
    assert repo.get(item["id"])["title"] == "New name"


def test_allowlist_cap(repo):
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd", "eeeeeeeeeee"):
        _add(repo, f"https://youtu.be/{vid}")
    with pytest.raises(AllowlistFullError):
        _add(repo, f"https://youtu.be/{V1}")


def test_enable_disable_reorder_and_next(repo):
    a = _add(repo, f"https://youtu.be/{V1}", "A")
    b = _add(repo, f"https://youtu.be/{V2}", "B")
    c = _add(repo, f"https://youtu.be/{V3}", "C")
    assert repo.next_enabled(None)["id"] == a["id"]
    assert repo.next_enabled(a["id"])["id"] == b["id"]
    assert repo.next_enabled(c["id"])["id"] == a["id"]  # wraps
    repo.update(b["id"], enabled=False)
    assert repo.get(b["id"])["enabled"] is False
    assert [i["id"] for i in repo.list(enabled_only=True)] == [a["id"], c["id"]]
    assert repo.count(enabled_only=True) == 2
    assert repo.next_enabled(a["id"])["id"] == c["id"]
    assert repo.next_enabled(b["id"])["id"] == c["id"]  # current item disabled meanwhile
    repo.reorder([c["id"], a["id"]])
    assert [i["id"] for i in repo.list()] == [c["id"], a["id"], b["id"]]
    repo.update(a["id"], enabled=False)
    repo.update(c["id"], enabled=False)
    assert repo.next_enabled(a["id"]) is None
    assert repo.update(999, title="x") is None


def test_errors_and_play_counts(repo):
    a = _add(repo, f"https://youtu.be/{V1}", "A")
    repo.record_play(a["id"])
    repo.set_error(a["id"], "Unsupported video")
    got = repo.get(a["id"])
    assert got["play_count"] == 1 and got["last_played_at"] and got["last_error"] == "Unsupported video"
    assert got["last_error_at"]
    repo.set_error(a["id"], None)
    assert repo.get(a["id"])["last_error"] is None and repo.get(a["id"])["last_error_at"] is None


def test_schema_survives_reopen(tmp_path):
    path = tmp_path / "lb.db"
    db = Database(path)
    AllowlistRepository(db).add(parse_youtube_url(f"https://youtu.be/{V1}"), "Kept")
    UsageRepository(db).add(dt.date(2026, 1, 2), 33.5)
    db.close()
    db2 = Database(path)
    assert AllowlistRepository(db2).list()[0]["title"] == "Kept"
    assert UsageRepository(db2).get(dt.date(2026, 1, 2)) == pytest.approx(33.5)
    db2.close()


def test_usage_per_day_and_prune():
    db = Database(":memory:")
    usage = UsageRepository(db)
    d1, d2 = dt.date(2026, 10, 19), dt.date(2026, 10, 20)
    assert usage.get(d1) == 0.0
    usage.add(d1, 60)
    usage.add(d1, 30.5)
    usage.add(d2, 10)
    usage.add(d2, -5)  # ignored
    assert usage.get(d1) == pytest.approx(90.5) and usage.get(d2) == pytest.approx(10)
    usage.prune(d2 + dt.timedelta(days=61))
    assert usage.get(d1) == 0.0
    db.close()


def test_malformed_never_reaches_db(repo):
    with pytest.raises(MediaURLError):
        _add(repo, "javascript:alert(1)")
    assert repo.count() == 0
