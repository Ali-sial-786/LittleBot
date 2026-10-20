"""YouTube link parsing / validation table."""

from __future__ import annotations

import pytest

from littlebot.media.allowlist import (
    MAX_URL_LENGTH, InvalidMediaIdError, MalformedURLError, MediaURLError, NotYouTubeURLError,
    UnsupportedURLError, URLTooLongError, parse_timestamp, parse_youtube_url, sanitize_title,
)

V = "dQw4w9WgXcQ"
PL = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


@pytest.mark.parametrize("url, video_id, start", [
    (f"https://www.youtube.com/watch?v={V}", V, 0),
    (f"https://youtube.com/watch?v={V}", V, 0),
    (f"http://www.youtube.com/watch?v={V}", V, 0),
    (f"www.youtube.com/watch?v={V}", V, 0),
    (f"youtube.com/watch?v={V}", V, 0),
    (f"  https://www.youtube.com/watch?v={V}  ", V, 0),
    (f"https://WWW.YOUTUBE.COM/watch?v={V}", V, 0),
    (f"https://www.youtube.com/watch?feature=share&v={V}", V, 0),
    (f"https://m.youtube.com/watch?v={V}&app=desktop", V, 0),
    (f"https://music.youtube.com/watch?v={V}&feature=share", V, 0),
    (f"https://youtu.be/{V}", V, 0),
    (f"https://youtu.be/{V}?si=AbCdEfGh123", V, 0),
    (f"youtu.be/{V}", V, 0),
    (f"https://youtu.be/{V}?t=42", V, 42),
    (f"https://www.youtube.com/watch?v={V}&t=90", V, 90),
    (f"https://www.youtube.com/watch?v={V}&t=90s", V, 90),
    (f"https://www.youtube.com/watch?v={V}&t=1m30s", V, 90),
    (f"https://www.youtube.com/watch?v={V}&t=1h2m3s", V, 3723),
    (f"https://www.youtube.com/watch?v={V}#t=65", V, 65),
    (f"https://www.youtube.com/watch?v={V}&t=garbage", V, 0),
    (f"https://www.youtube.com/shorts/{V}", V, 0),
    (f"https://youtube.com/shorts/{V}?feature=share", V, 0),
    (f"https://www.youtube.com/embed/{V}", V, 0),
    (f"https://www.youtube.com/embed/{V}?start=30&rel=0", V, 30),
    (f"https://www.youtube-nocookie.com/embed/{V}", V, 0),
    (f"https://youtube-nocookie.com/embed/{V}?start=5", V, 5),
    (f"https://www.youtube.com/live/{V}", V, 0),
    (f"https://www.youtube.com/live/{V}?feature=share", V, 0),
    (f"https://www.youtube.com/v/{V}?version=3", V, 0),
    (f"https://www.youtube.com/watch?v={V}&list={PL}&index=3", V, 0),  # video unless parent picks playlist
    (f"https://www.youtube.com/watch?v={V}&list=RD{V}&start_radio=1", V, 0),  # video from a mix: keep video
    (f"https://www.youtube.com/watch?v=%64Qw4w9WgXcQ", V, 0),  # percent-encoded id decodes to a valid id
])
def test_valid_video_links(url, video_id, start):
    p = parse_youtube_url(url)
    assert p.kind == "video"
    assert p.video_id == video_id
    assert p.list_id is None
    assert p.start_seconds == start
    expected = f"https://www.youtube.com/watch?v={video_id}" + (f"&t={start}s" if start else "")
    assert p.canonical_url == expected
    assert p.dedupe_key == f"video:{video_id}"


@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/playlist?list={PL}",
    f"https://m.youtube.com/playlist?list={PL}",
    f"https://music.youtube.com/playlist?list={PL}",
    f"https://www.youtube.com/watch?list={PL}",
    f"https://www.youtube.com/embed/videoseries?list={PL}",
    f"https://www.youtube-nocookie.com/embed/videoseries?list={PL}",
])
def test_valid_playlist_links(url):
    p = parse_youtube_url(url)
    assert p.kind == "playlist"
    assert p.list_id == PL
    assert p.video_id is None
    assert p.canonical_url == f"https://www.youtube.com/playlist?list={PL}"
    assert p.dedupe_key == f"playlist:{PL}"


def test_watch_with_list_is_video_by_default_and_playlist_when_parent_chooses():
    url = f"https://www.youtube.com/watch?v={V}&list={PL}&t=12"
    video = parse_youtube_url(url)
    assert video.kind == "video" and video.has_playlist and video.list_id is None and video.start_seconds == 12
    pl = parse_youtube_url(url, kind="playlist")
    assert pl.kind == "playlist" and pl.list_id == PL and pl.video_id == V
    assert pl.canonical_url == f"https://www.youtube.com/watch?v={V}&list={PL}"
    with pytest.raises(UnsupportedURLError, match="single video"):
        parse_youtube_url(f"https://youtu.be/{V}", kind="playlist")
    with pytest.raises(UnsupportedURLError, match="playlist"):
        parse_youtube_url(f"https://www.youtube.com/playlist?list={PL}", kind="video")


@pytest.mark.parametrize("url, error, message", [
    ("", MalformedURLError, "paste"),
    ("   ", MalformedURLError, "paste"),
    (None, MalformedURLError, "text"),
    (12345, MalformedURLError, "text"),
    ("javascript:alert(1)", MalformedURLError, "https"),
    ("JaVaScRiPt:alert(document.cookie)//youtube.com", MalformedURLError, "https"),
    ("data:text/html,<script>alert(1)</script>", MalformedURLError, "https"),
    ("file:///etc/passwd", MalformedURLError, "https"),
    ("ftp://youtube.com/watch?v=dQw4w9WgXcQ", MalformedURLError, "https"),
    ("https:/www.youtube.com/watch?v=dQw4w9WgXcQ", MalformedURLError, "complete"),
    ("dQw4w9WgXcQ", MalformedURLError, "full YouTube link"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ\n<script>", MalformedURLError, "hidden"),
    ("https://www.youtube.com/watch?v=dQw4 w9WgXcQ", MalformedURLError, "spaces"),
    ("https://www.youtube.com\\@evil.com/watch?v=dQw4w9WgXcQ", MalformedURLError, "hidden"),
    ("https://evil.com@www.youtube.com/watch?v=dQw4w9WgXcQ", MalformedURLError, "user name"),
    ("https://www.youtube.com:8080/watch?v=dQw4w9WgXcQ", MalformedURLError, "port"),
    ("https://[::1/watch", MalformedURLError, "valid web link"),
    ("https://www.youtube.com/watch", MalformedURLError, "missing the video id"),
    ("https://www.youtube.com/playlist", MalformedURLError, "missing the playlist id"),
    ("https://www.youtube.com/watch?v=short", InvalidMediaIdError, "11"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQQ", InvalidMediaIdError, "11"),
    ("https://www.youtube.com/watch?v=dQw4w9WgX<Q", InvalidMediaIdError, "11"),
    ("https://youtu.be/dQw4w9", InvalidMediaIdError, "11"),
    ("https://www.youtube.com/shorts/abc", InvalidMediaIdError, "11"),
    ("https://www.youtube.com/playlist?list=PL<script>", InvalidMediaIdError, "playlist id"),
    ("https://www.youtube.com/playlist?list=PLshort", InvalidMediaIdError, "playlist id"),
    ("https://vimeo.com/123456", NotYouTubeURLError, "Only YouTube"),
    ("https://www.youtube.com.evil.com/watch?v=dQw4w9WgXcQ", NotYouTubeURLError, "Only YouTube"),
    ("https://evilyoutube.com/watch?v=dQw4w9WgXcQ", NotYouTubeURLError, "Only YouTube"),
    ("https://www.yоutube.com/watch?v=dQw4w9WgXcQ", NotYouTubeURLError, "Only YouTube"),  # Cyrillic o
    ("https://youtube.co/watch?v=dQw4w9WgXcQ", NotYouTubeURLError, "Only YouTube"),
    ("https://www.google.com/url?q=https://youtu.be/dQw4w9WgXcQ", NotYouTubeURLError, "Only YouTube"),
    ("https://www.youtube.com/@SomeChannel", UnsupportedURLError, "channel"),
    ("https://www.youtube.com/@SomeChannel/videos", UnsupportedURLError, "channel"),
    ("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw", UnsupportedURLError, "channel"),
    ("https://www.youtube.com/c/SomeName", UnsupportedURLError, "channel"),
    ("https://www.youtube.com/user/SomeName", UnsupportedURLError, "channel"),
    ("https://www.youtube.com/results?search_query=nursery+rhymes", UnsupportedURLError, "search"),
    ("https://www.youtube.com/feed/trending", UnsupportedURLError, "feed"),
    ("https://www.youtube.com/hashtag/kids", UnsupportedURLError, "hashtag"),
    ("https://www.youtube.com/clip/UgkxAbc", UnsupportedURLError, "clip"),
    ("https://www.youtube.com/", UnsupportedURLError, "home page"),
    ("https://www.youtube.com", UnsupportedURLError, "home page"),
    ("https://www.youtube.com/watch/dQw4w9WgXcQ", UnsupportedURLError, "isn't supported"),
    ("https://youtu.be/", UnsupportedURLError, "doesn't point to a video"),
    ("https://www.youtube.com/playlist?list=WL", UnsupportedURLError, "private"),
    ("https://www.youtube.com/playlist?list=LL", UnsupportedURLError, "private"),
    (f"https://www.youtube.com/playlist?list=RD{V}", UnsupportedURLError, "Mix"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&" + "x" * MAX_URL_LENGTH, URLTooLongError, "too long"),
])
def test_rejected_links(url, error, message):
    with pytest.raises(error, match=message) as info:
        parse_youtube_url(url)
    assert isinstance(info.value, ValueError) and isinstance(info.value, MediaURLError)


def test_mix_rejected_when_parent_chooses_playlist():
    with pytest.raises(UnsupportedURLError, match="Mix"):
        parse_youtube_url(f"https://www.youtube.com/watch?v={V}&list=RD{V}", kind="playlist")


def test_error_reason_codes():
    cases = {"javascript:x": "malformed", "https://vimeo.com/1": "not_youtube",
             "https://www.youtube.com/@x": "unsupported", "https://youtu.be/abc": "invalid_id",
             "https://youtu.be/" + "a" * 3000: "too_long"}
    for url, reason in cases.items():
        with pytest.raises(MediaURLError) as info:
            parse_youtube_url(url)
        assert info.value.reason == reason


def test_canonical_url_is_built_only_from_validated_parts():
    p = parse_youtube_url(f"https://m.youtube.com/watch?v={V}&utm_source=x&foo=<b>&t=5")
    assert p.canonical_url == f"https://www.youtube.com/watch?v={V}&t=5s"


@pytest.mark.parametrize("value, seconds", [
    ("90", 90), ("90s", 90), ("2m", 120), ("1m5s", 65), ("1h", 3600), ("1h0m1s", 3601),
    ("", 0), (None, 0), ("abc", 0), ("-5", 0), ("99999999", 0), ("13h", 0),
])
def test_parse_timestamp(value, seconds):
    assert parse_timestamp(value) == seconds


def test_sanitize_title():
    assert sanitize_title("  Twinkle   twinkle\n little star ") == "Twinkle twinkle little star"
    assert sanitize_title("evil‮reversed​\x00\x07") == "evilreversed"
    assert sanitize_title("x" * 300) == "x" * 99 + "…"
    assert len(sanitize_title("y" * 5000)) == 100
    assert sanitize_title("", default="Fallback") == "Fallback"
    assert sanitize_title(None, default="D") == "D"
    assert sanitize_title("<b>Baby Shark</b>") == "<b>Baby Shark</b>"  # plain text; escaping is the renderer's job
    with pytest.raises(ValueError):
        sanitize_title(42)
