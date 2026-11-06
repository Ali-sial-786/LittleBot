"""Dashboard: photos and videos (``/api/capture``, parents only) and the share links a phone opens without a login
(``/share/<token>``): the gallery, a photo from the button, files and previews, delete, a link with its QR code, the
switches, CSRF, 503 without the service, and a plain 404 for a wrong or old link."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from littlebot.capture.service import CaptureService
from littlebot.vision.service import PhotoUnavailable

JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08\x00\x03\x00\x04\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\x00" * 32 + b"\xff\xd9")


class FakeVision:
    def status(self) -> dict[str, Any]:
        return {"camera": "ok"}

    async def capture_photo(self, **_kw: Any) -> bytes:
        return JPEG

    def add_frame_sink(self, *_a: Any, **_kw: Any) -> None:
        raise PhotoUnavailable("no_camera", "not in this test")

    def remove_frame_sink(self, owner: str) -> bool:
        return False


@pytest.fixture
async def capture(dctx: Any, tmp_path: Path):
    dctx.vision = FakeVision()
    svc = CaptureService(dctx, which=lambda cmd: None, gallery_dir=tmp_path / "gallery", screen=False)
    await svc.start()
    svc.base_urls = lambda: (["http://192.168.2.112:8765"], True)  # type: ignore[method-assign]
    dctx.capture = svc
    yield svc
    await svc.stop()
    dctx.capture = None


async def _photo(authed: Any) -> dict[str, Any]:
    r = await authed.post("/api/capture/photo")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    return r.json()["item"]


async def test_without_the_service(authed: Any, dctx: Any) -> None:
    dctx.capture = None
    d = (await authed.get("/api/capture")).json()
    assert d["available"] is False and d["items"] == []
    assert (await authed.post("/api/capture/photo")).status_code == 503
    assert (await authed.get("/share/AbCdEfGhIjKlMnOpQrStUv")).status_code == 404


async def test_parents_only(client: Any, capture: Any) -> None:
    for method, url in (("get", "/api/capture"), ("post", "/api/capture/photo"), ("delete", "/api/capture/shares")):
        r = await getattr(client, method)(url)
        assert r.status_code in (401, 403), (url, r.status_code)
    assert capture.gallery.items() == []


async def test_a_photo_from_the_button_and_the_gallery(authed: Any, capture: Any) -> None:
    item = await _photo(authed)
    d = (await authed.get("/api/capture")).json()
    assert d["available"] is True and d["photos"] == 1 and d["ffmpeg"] is False
    (listed,) = d["items"]
    assert listed["id"] == item["id"] and listed["source"] == "dashboard"
    assert listed["file"] == f"/api/capture/items/{item['id']}/file" and listed["download_name"].endswith(".jpg")
    assert d["phones"] == [] and "Calendars and phones" in d["phone_help"]
    r = await authed.get(listed["file"])
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-disposition"].startswith("inline")
    r = await authed.get(listed["file"] + "?download=1")
    assert r.headers["content-disposition"].startswith("attachment")
    assert (await authed.get(listed["thumb"])).status_code == 200  # (the photo itself when it has no preview)


async def test_bad_ids_are_refused(authed: Any, capture: Any) -> None:
    for bad in ("..%2F..%2Fetc%2Fpasswd", "nope", "20261106-143105.jpg"):
        assert (await authed.get(f"/api/capture/items/{bad}/file")).status_code == 404
    assert (await authed.delete("/api/capture/items/20991231-235959")).status_code == 404


async def test_delete(authed: Any, capture: Any) -> None:
    item = await _photo(authed)
    r = await authed.delete(f"/api/capture/items/{item['id']}")
    assert r.status_code == 200 and r.json()["status"]["items"] == [] and capture.gallery.items() == []


async def test_no_video_without_ffmpeg(authed: Any, capture: Any) -> None:
    r = await authed.post("/api/capture/video/start")
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["error"] == "no_encoder"
    assert (await authed.post("/api/capture/video/stop")).json()["error"] == "not_recording"


async def test_a_share_link_opens_without_a_login(authed: Any, client: Any, capture: Any) -> None:
    item = await _photo(authed)
    r = await authed.post(f"/api/capture/items/{item['id']}/share", json={})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and d["reachable"] and d["url"].startswith("http://192.168.2.112:8765/share/")
    assert d["qr"].startswith("data:image/svg+xml;base64,")
    assert base64.b64decode(d["qr"].split(",", 1)[1]).startswith(b"<svg")
    path = "/share/" + d["url"].rsplit("/share/", 1)[1]
    page = await client.get(path)  # (not logged in)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert f'src="{path}/file"' in page.text and "Save" in page.text and "<script" not in page.text
    assert page.headers["referrer-policy"] == "no-referrer" and "noindex" in page.headers["x-robots-tag"]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    f = await client.get(path + "/file")
    assert f.status_code == 200 and f.content == JPEG
    assert (await client.get(path + "/file?download=1")).headers["content-disposition"].startswith("attachment")
    assert (await client.get(path + "/thumb")).status_code == 200
    # it opens only that one item, and nothing else on the dashboard
    assert (await client.get("/api/capture")).status_code in (401, 403)


@pytest.mark.parametrize("token", ["AbCdEfGhIjKlMnOpQrStUv", "short", "..%2F..", "x" * 70])
async def test_a_wrong_link_is_a_plain_404(client: Any, capture: Any, token: str) -> None:
    for suffix in ("", "/file", "/thumb"):
        r = await client.get(f"/share/{token}{suffix}")
        assert r.status_code == 404, (token, suffix)


async def test_stopping_every_link(authed: Any, client: Any, capture: Any) -> None:
    item = await _photo(authed)
    url = (await authed.post(f"/api/capture/items/{item['id']}/share", json={})).json()["url"]
    path = "/share/" + url.rsplit("/share/", 1)[1]
    assert (await client.get(path)).status_code == 200
    r = await authed.delete("/api/capture/shares")
    assert r.json()["revoked"] == 1
    assert (await client.get(path)).status_code == 404


async def test_a_deleted_item_stops_its_link(authed: Any, client: Any, capture: Any) -> None:
    item = await _photo(authed)
    url = (await authed.post(f"/api/capture/items/{item['id']}/share", json={})).json()["url"]
    await authed.delete(f"/api/capture/items/{item['id']}")
    assert (await client.get("/share/" + url.rsplit("/share/", 1)[1])).status_code == 404


async def test_send_to_phone_reports_why_it_didnt(authed: Any, capture: Any) -> None:
    item = await _photo(authed)
    d = (await authed.post(f"/api/capture/items/{item['id']}/share", json={"push": True})).json()
    assert d["ok"] and d["pushed"] is False and "Calendars and phones" in d["push_error"]


async def test_the_switches(authed: Any, dctx: Any, capture: Any) -> None:
    r = await authed.put("/api/capture/settings", json={"video_sound": False})
    assert r.status_code == 200 and r.json()["video_sound"] is False and dctx.settings.get("capture.video_sound") is False
    r = await authed.put("/api/capture/settings", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert (await authed.post("/api/capture/photo")).json()["error"] == "disabled"
    assert (await authed.put("/api/capture/settings", json={})).status_code == 422
    assert (await authed.put("/api/capture/settings", json={"enabled": "yes", "x": 1})).status_code in (400, 422)


async def test_csrf_is_needed(authed: Any, capture: Any) -> None:
    token = authed.headers.pop("X-CSRF-Token")
    assert (await authed.post("/api/capture/photo")).status_code == 403
    assert (await authed.delete("/api/capture/shares")).status_code == 403
    assert capture.gallery.items() == []
    authed.headers["X-CSRF-Token"] = token
