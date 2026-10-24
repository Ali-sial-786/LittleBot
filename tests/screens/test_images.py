"""Pictures for cards (littlebot/screens/images.py): source order, fallbacks, limits, switches, safety.

No network: every request goes to an httpx.MockTransport that plays Wikipedia, Commons and Openverse.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import types
from typing import Any

import httpx
import pygame
import pytest

from littlebot.screens import images

WIKI = "en.wikipedia.org"
COMMONS = "commons.wikimedia.org"
OPENVERSE = "api.openverse.org"
UPLOAD = "upload.wikimedia.org"


def image_bytes(fmt: str = "png", size: tuple[int, int] = (800, 600)) -> bytes:
    s = pygame.Surface(size)
    s.fill((120, 200, 255))
    pygame.draw.circle(s, (255, 120, 150), (size[0] // 2, size[1] // 2), min(size) // 3)
    buf = io.BytesIO()
    pygame.image.save(s, buf, f"x.{fmt}")
    return buf.getvalue()


PNG = image_bytes("png")
JPEG = image_bytes("jpg", (400, 300))


class Web:
    """A fake web: ``routes`` maps (host, path start) to a response maker; every request is logged."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: list[tuple[str, str, Any]] = []
        self.gate: threading.Event | None = None

    def route(self, host: str, path: str, make: Any) -> None:
        self.routes.append((host, path, make))

    def json(self, host: str, path: str, data: Any, status: int = 200) -> None:
        self.route(host, path, lambda r: httpx.Response(status, json=data))

    def image(self, host: str, path: str, body: bytes = PNG, ctype: str = "image/png",
              headers: dict[str, str] | None = None) -> None:
        self.route(host, path, lambda r: httpx.Response(200, content=body,
                                                        headers={"content-type": ctype, **(headers or {})}))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.gate is not None:
            self.gate.wait(5)
        self.requests.append(request)
        for host, path, make in self.routes:
            if request.url.host == host and request.url.path.startswith(path):
                return make(request)
        return httpx.Response(404, json={"detail": "not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


def summary(title: str, thumb: str | None = f"https://{UPLOAD}/thumb/x.png", kind: str = "standard",
            description: str = "") -> dict[str, Any]:
    data: dict[str, Any] = {"type": kind, "title": title, "description": description}
    if thumb:
        data["thumbnail"] = {"source": thumb, "width": 330, "height": 220}
    return data


def commons_answer(url: str = f"https://{UPLOAD}/commons/c.png", mime: str = "image/png",
                   title: str = "File:Rainbow.png") -> dict[str, Any]:
    return {"query": {"pages": [{"index": 1, "title": title, "imageinfo": [{"thumburl": url, "mime": mime}]}]}}


def openverse_answer(url: str = f"https://{OPENVERSE}/v1/images/abc/thumb/", **extra: Any) -> dict[str, Any]:
    item = {"title": "Rainbow", "thumbnail": url, "creator": "Sam", "license": "by", "mature": False,
            "tags": [{"name": "rainbow"}], **extra}
    return {"results": [item]}


def make_ctx(*, offline: bool = False, cloud: bool = True, pictures: bool = True) -> Any:
    return types.SimpleNamespace(config=types.SimpleNamespace(is_offline=offline),
                                 privacy=types.SimpleNamespace(cloud_enabled=cloud),
                                 settings={"screens.web_pictures": pictures})


@pytest.fixture(autouse=True)
def _fresh():
    images.clear_cache()
    yield
    images.clear_cache()


@pytest.fixture
def web() -> Web:
    return Web()


def fetch(web: Web, ctx: Any = None, **kw: Any) -> images.WebPicture:
    pic = images.fetch(ctx or make_ctx(), transport=web.transport, **kw)
    assert pic.wait(10)
    return pic


# ---------------------------------------------------------------- source order and fallbacks
def test_wikipedia_summary_thumbnail_first(web: Web, caplog: pytest.LogCaptureFixture) -> None:
    web.json(WIKI, "/api/rest_v1/page/summary/Rainbow", summary("Rainbow"))
    web.image(UPLOAD, "/thumb/")
    with caplog.at_level(logging.INFO, logger="littlebot.screens.images"):
        pic = fetch(web, query="rainbow")
    assert pic.ready and pic.surface is not None
    assert pic.credit == "Wikipedia" and pic.source == "wikipedia"
    w, h = pic.surface.get_size()
    assert w <= images.MAX_SIZE[0] and h <= images.MAX_SIZE[1] and (w, h) == (360, 270)  # 800x600 scaled down
    assert web.hosts() == [WIKI, UPLOAD]
    lines = [r.getMessage() for r in caplog.records if r.name == "littlebot.screens.images"]
    assert len(lines) == 1 and "wikipedia picture in" in lines[0] and "360x270" in lines[0]
    assert "rainbow" not in lines[0].lower().replace("wikipedia", "")  # never the query


def test_wikipedia_search_finds_the_title_when_there_is_no_such_article(web: Web) -> None:
    web.json(WIKI, "/w/api.php", {"query": {"search": [{"title": "Golden Retriever"}]}})
    web.json(WIKI, "/api/rest_v1/page/summary/Golden_Retriever", summary("Golden Retriever"))
    web.image(UPLOAD, "/thumb/")
    pic = fetch(web, query="golden retriever dog")
    assert pic.source == "wikipedia"
    paths = [r.url.path for r in web.requests]
    assert paths[0] == "/api/rest_v1/page/summary/Golden%20retriever%20dog".replace("%20", "_")
    assert paths[1] == "/w/api.php" and web.requests[1].url.params["srsearch"] == "golden retriever dog"
    assert paths[2] == "/api/rest_v1/page/summary/Golden_Retriever"


def test_disambiguation_pages_are_skipped(web: Web) -> None:
    web.json(WIKI, "/api/rest_v1/page/summary/Mercury_(planet)", summary("Mercury (planet)"))
    web.json(WIKI, "/api/rest_v1/page/summary/Mercury", summary("Mercury", kind="disambiguation"))
    web.json(WIKI, "/w/api.php", {"query": {"search": [{"title": "Mercury (planet)"}]}})
    web.image(UPLOAD, "/thumb/")
    assert fetch(web, query="Mercury").source == "wikipedia"


def test_commons_when_wikipedia_has_no_picture(web: Web) -> None:
    web.json(WIKI, "/api/rest_v1/page/summary/", summary("Rainbow", thumb=None))
    web.json(WIKI, "/w/api.php", {"query": {"search": []}})
    web.json(COMMONS, "/w/api.php", commons_answer())
    web.image(UPLOAD, "/commons/")
    pic = fetch(web, query="rainbow")
    assert (pic.source, pic.credit) == ("commons", "Wikimedia Commons")
    commons = next(r for r in web.requests if r.url.host == COMMONS)
    assert "filetype:bitmap" in commons.url.params["gsrsearch"]


def test_openverse_last_without_mature_results(web: Web) -> None:
    web.json(WIKI, "/w/api.php", {"query": {"search": []}}, status=429)  # rate-limited
    web.json(COMMONS, "/w/api.php", {}, status=403)
    web.image(OPENVERSE, "/v1/images/abc/thumb/", JPEG, "image/jpeg")
    web.json(OPENVERSE, "/v1/images/", openverse_answer())
    pic = fetch(web, query="rainbow")
    assert pic.source == "openverse" and pic.credit.startswith("Openverse") and "Sam" in pic.credit
    search = next(r for r in web.requests if r.url.host == OPENVERSE and r.url.path == "/v1/images/")
    assert search.url.params["mature"] == "false"
    order = [h for h in dict.fromkeys(web.hosts())]
    assert order.index(WIKI) < order.index(COMMONS) < order.index(OPENVERSE)


def test_openverse_skips_items_marked_mature_or_with_explicit_tags(web: Web) -> None:
    answer = {"results": [
        {"title": "x", "thumbnail": f"https://{OPENVERSE}/v1/images/m/thumb/", "mature": True},
        {"title": "beach", "thumbnail": f"https://{OPENVERSE}/v1/images/t/thumb/", "tags": [{"name": "nude"}]},
        {"title": "Rainbow", "thumbnail": f"https://{OPENVERSE}/v1/images/ok/thumb/", "license": "cc0"}]}
    web.image(OPENVERSE, "/v1/images/ok/thumb/")
    web.json(OPENVERSE, "/v1/images/", answer)
    pic = fetch(web, query="rainbow")
    assert pic.source == "openverse"
    fetched = [r.url.path for r in web.requests if "/thumb/" in r.url.path]
    assert fetched == ["/v1/images/ok/thumb/"]


def test_nothing_anywhere_is_ready_without_a_picture(web: Web) -> None:
    pic = fetch(web, query="zzqx")
    assert pic.ready and pic.surface is None and pic.credit == "" and pic.reason == "none"
    assert {WIKI, COMMONS, OPENVERSE} <= set(web.hosts())


def test_a_url_is_loaded_directly_with_its_site_as_credit(web: Web) -> None:
    web.image("www.example.org", "/cat.png")
    pic = fetch(web, url="https://www.example.org/cat.png")
    assert pic.surface is not None and pic.credit == "example.org" and pic.source == "url"
    assert web.hosts() == ["www.example.org"]


# ---------------------------------------------------------------- limits
def test_only_https(web: Web) -> None:
    pic = images.fetch(make_ctx(), url="http://example.org/cat.png", transport=web.transport)
    assert pic.ready and pic.surface is None and pic.reason == "not_https" and not web.requests
    web.json(WIKI, "/api/rest_v1/page/summary/", summary("Cat", thumb="http://upload.wikimedia.org/cat.png"))
    web.json(COMMONS, "/w/api.php", commons_answer())
    web.image(UPLOAD, "/commons/")
    assert fetch(web, query="cat").source == "commons"  # the http thumbnail was never requested
    assert all(r.url.scheme == "https" for r in web.requests)


def test_a_redirect_to_http_is_refused(web: Web) -> None:
    web.route("www.example.org", "/a.png", lambda r: httpx.Response(302, headers={"location": "http://x.org/a.png"}))
    pic = fetch(web, url="https://www.example.org/a.png")
    assert pic.surface is None and [r.url.host for r in web.requests] == ["www.example.org"]


@pytest.mark.parametrize("ctype", ["image/gif", "text/html", "image/svg+xml", ""])
def test_other_content_types_are_skipped(web: Web, ctype: str) -> None:
    web.image("example.org", "/x", PNG, ctype)
    assert fetch(web, url="https://example.org/x").surface is None


def test_content_that_doesnt_match_its_type_is_skipped(web: Web) -> None:
    web.image("example.org", "/x", JPEG, "image/png")
    assert fetch(web, url="https://example.org/x").surface is None


def test_jpeg_is_decoded(web: Web) -> None:
    web.image("example.org", "/x.jpg", JPEG, "image/jpeg; charset=binary")
    pic = fetch(web, url="https://example.org/x.jpg")
    assert pic.surface is not None and pic.surface.get_size() == (360, 270)


def test_webp_that_cant_be_decoded_is_skipped(web: Web) -> None:
    web.image("example.org", "/x.webp", b"RIFF\x10\x00\x00\x00WEBPVP8 garbage", "image/webp")
    pic = fetch(web, url="https://example.org/x.webp")
    assert pic.ready and pic.surface is None


def test_size_cap_by_header_and_by_stream(web: Web, monkeypatch: pytest.MonkeyPatch) -> None:
    web.image("example.org", "/big", PNG, headers={"content-length": str(images.MAX_BYTES + 1)})
    assert fetch(web, url="https://example.org/big").surface is None
    monkeypatch.setattr(images, "MAX_BYTES", len(PNG) - 1)  # no usable length header: counted while reading

    def chunked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=iter([PNG[:500], PNG[500:]]))

    web.route("example.org", "/stream", chunked)
    assert fetch(web, url="https://example.org/stream").surface is None
    monkeypatch.setattr(images, "MAX_BYTES", len(PNG))
    images.clear_cache()
    assert fetch(web, url="https://example.org/stream").surface is not None


def test_small_images_keep_their_size_and_palette_pngs_work(web: Web) -> None:
    s = pygame.Surface((40, 30), depth=8)
    s.set_palette([(i, i, 255 - i) for i in range(256)])
    s.fill(7)
    buf = io.BytesIO()
    pygame.image.save(s, buf, "x.png")
    web.image("example.org", "/p.png", buf.getvalue())
    pic = fetch(web, url="https://example.org/p.png")
    assert pic.surface is not None and pic.surface.get_size() == (40, 30)
    assert pic.surface.get_bitsize() in (24, 32)


def test_headers_say_who_is_asking(web: Web) -> None:
    web.json(WIKI, "/api/rest_v1/page/summary/", summary("Rainbow"))
    web.image(UPLOAD, "/thumb/")
    fetch(web, query="rainbow")
    for r in web.requests:
        ua = r.headers["user-agent"]
        assert ua.startswith("LittleBot/") and "robot" in ua
    assert web.requests[-1].headers["accept"] == "*/*"  # Openverse's thumbnails refuse narrower lists


# ---------------------------------------------------------------- switches and safety
@pytest.mark.parametrize(("ctx", "reason"), [
    (make_ctx(offline=True), "offline"),
    (make_ctx(cloud=False), "cloud_off"),
    (make_ctx(pictures=False), "setting_off"),
    (types.SimpleNamespace(config=None, privacy=None, settings=None), "cloud_off"),
])
def test_switches_mean_no_fetch(web: Web, ctx: Any, reason: str) -> None:
    pic = images.fetch(ctx, query="rainbow", transport=web.transport)
    assert pic.ready and pic.surface is None and pic.reason == reason
    assert not web.requests


def test_the_robots_privacy_switch(ctx: Any, web: Web) -> None:
    ctx.privacy.set(cloud_enabled=False)
    assert images.blocked_reason(ctx) == "cloud_off"
    ctx.privacy.set(cloud_enabled=True)
    assert images.blocked_reason(ctx) is None  # screens.web_pictures defaults to on
    web.json(WIKI, "/api/rest_v1/page/summary/", summary("Rainbow"))
    web.image(UPLOAD, "/thumb/")
    assert fetch(web, ctx, query="rainbow").surface is not None


@pytest.mark.parametrize("query", ["naked woman", "NSFW memes", "p o r n", "s3xy pics", "pornhub", "gore videos",
                                   "dead body", "let's keep it a secret from mommy"])
def test_unsafe_queries_are_refused(web: Web, query: str) -> None:
    pic = images.fetch(make_ctx(), query=query, transport=web.transport)
    assert pic.ready and pic.surface is None and pic.reason == "unsafe" and not web.requests


@pytest.mark.parametrize("query", ["naked mole rat", "great tit", "Essex", "breastfeeding positions", "rainbow",
                                   "volcano", "sussex spaniel"])
def test_ordinary_queries_pass(query: str) -> None:
    assert not images.unsafe(query)


def test_an_unsafe_title_found_by_the_search_is_skipped(web: Web) -> None:
    web.json(WIKI, "/api/rest_v1/page/summary/Beach", summary("Nudity"))
    web.json(WIKI, "/w/api.php", {"query": {"search": [{"title": "Nude beach"}]}})
    web.json(COMMONS, "/w/api.php", commons_answer(title="File:Beach.png"))
    web.image(UPLOAD, "/")
    pic = fetch(web, query="beach")
    assert pic.source == "commons"
    assert not any("Nude" in r.url.path for r in web.requests)


# ---------------------------------------------------------------- memory, concurrency
def test_cache_returns_the_same_picture_without_asking_again(web: Web) -> None:
    web.json(WIKI, "/api/rest_v1/page/summary/", summary("Rainbow"))
    web.image(UPLOAD, "/thumb/")
    first = fetch(web, query="Rainbow")
    n = len(web.requests)
    again = images.fetch(make_ctx(), query="rainbow", transport=web.transport)
    assert again.ready and again.source == "cache" and again.surface is first.surface
    assert again.credit == "Wikipedia" and len(web.requests) == n


def test_cache_is_bounded(web: Web) -> None:
    web.image("example.org", "/", JPEG, "image/jpeg")
    for i in range(images.CACHE_ITEMS + 4):
        fetch(web, url=f"https://example.org/{i}.jpg")
    assert len(images._CACHE) == images.CACHE_ITEMS
    assert "url:https://example.org/0.jpg" not in images._CACHE


def test_fetch_returns_at_once_and_joins_a_fetch_in_flight(web: Web) -> None:
    web.gate = threading.Event()
    web.image("example.org", "/", JPEG, "image/jpeg")
    ctx = make_ctx()
    a = images.fetch(ctx, url="https://example.org/a.jpg", transport=web.transport)
    assert not a.ready and a.surface is None and a.credit == ""
    assert images.fetch(ctx, url="https://example.org/a.jpg", transport=web.transport) is a
    others = [images.fetch(ctx, url=f"https://example.org/{i}.jpg", transport=web.transport)
              for i in range(images.MAX_IN_FLIGHT)]
    busy = [p for p in others if p.ready]
    assert len(busy) == 1 and busy[0].reason == "busy"
    web.gate.set()
    assert a.wait(10) and a.surface is not None
    assert all(p.wait(10) for p in others)
    assert not images._IN_FLIGHT


def test_a_failing_transport_never_raises(web: Web) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    pic = images.fetch(make_ctx(), query="rainbow", transport=httpx.MockTransport(boom))
    assert pic.wait(10) and pic.surface is None


def test_empty_request() -> None:
    pic = images.fetch(make_ctx(), query="  ")
    assert pic.ready and pic.surface is None and pic.reason == "empty"


def test_json_garbage_is_survived(web: Web) -> None:
    web.route(WIKI, "/", lambda r: httpx.Response(200, content=b"<html>"))
    web.json(COMMONS, "/w/api.php", {"query": {"pages": "nope"}})
    web.route(OPENVERSE, "/", lambda r: httpx.Response(200, content=json.dumps({"results": [None, 3]}).encode()))
    pic = fetch(web, query="rainbow")
    assert pic.ready and pic.surface is None
