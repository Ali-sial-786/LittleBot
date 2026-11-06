"""The gallery card and the QR card (littlebot/screens/cards/gallery.py): every mode draws, taps at the edges and
swipes move, a tap on a video plays it, the voice's actions, the slideshow, and pictures loaded off the display
thread."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from littlebot.capture.gallery import Gallery
from littlebot.screens import base
from littlebot.screens.base import H, RecordingHost, W
from littlebot.screens.cards import gallery as G

pygame = pytest.importorskip("pygame")


def _jpeg(tmp: Path, colour: tuple[int, int, int], size: tuple[int, int] = (640, 360)) -> bytes:
    surf = pygame.Surface(size)
    surf.fill(colour)
    path = tmp / f"src-{colour[0]}-{colour[1]}-{colour[2]}.jpg"
    pygame.image.save(surf, str(path))
    return path.read_bytes()


def _gallery(tmp: Path) -> tuple[Gallery, list[Any]]:
    g = Gallery(tmp / "gallery", clock=lambda: 1_762_400_000.0)
    a = g.add_photo(_jpeg(tmp, (200, 40, 40)), taken_at=1_762_400_000.0)
    vid = g.new_id(1_762_400_060.0)
    g.part(vid).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    v = g.finish_video(vid, taken_at=1_762_400_060.0, seconds=75, width=1280, height=720,
                       thumb=_jpeg(tmp, (40, 200, 40), (320, 180)))
    b = g.add_photo(_jpeg(tmp, (40, 40, 200), (360, 640)), taken_at=1_762_400_120.0)
    return g, g.items()  # newest first: b, v, a


def _settle(card: Any, surf: Any, seconds: float = 2.0) -> None:
    """Draw until the picture on show has loaded (it loads on a worker thread)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        card.update(1 / 30)
        card.draw(surf)
        ready, _ = card._pics.get(card.item.id, card._path(card.item))  # noqa: SLF001
        if ready:
            card.draw(surf)
            return
        time.sleep(0.02)
    raise AssertionError("the picture never loaded")


def test_browsing_draws_and_moves(tmp_path: Path) -> None:
    g, items = _gallery(tmp_path)
    played: list[str] = []
    shared: list[str] = []
    card = G.GalleryCard({"title": "Photos"}, RecordingHost(), gallery=g, items=items, on_play=played.append,
                         on_share=shared.append)
    surf = pygame.Surface((W, H))
    _settle(card, surf)
    assert surf.get_at((W // 2, H // 2))[:3] != G.BG  # the photo is there
    assert card.describe().startswith("the photo gallery: photo 1 of 3")
    assert card.tap(W - 5, H // 2) and card.item.kind == "video"  # the right edge: the next (older) one
    _settle(card, surf)
    assert "video (1:15) 2 of 3" in card.describe()
    assert card.tap(W // 2, H // 2) and played == [items[1].id]  # a tap on a video plays it
    assert card.tap(5, H // 2) and card.index == 0  # the left edge: back
    card.drag(-120, 0)
    card.drag_end()
    assert card.index == 1
    card.drag(30, 0)
    card.drag_end()
    assert card.index == 1  # too short to count
    assert card.key("right") and card.index == 2 and card.key("left") and card.index == 1
    st = card.status()
    assert st["id"] == items[1].id and st["count"] == 3 and st["countdown"] is None


def test_the_voices_actions(tmp_path: Path) -> None:
    g, items = _gallery(tmp_path)
    played: list[str] = []
    shared: list[str] = []
    card = G.GalleryCard({}, RecordingHost(), gallery=g, items=items, on_play=played.append, on_share=shared.append)
    assert card.command("next", {}) and card.index == 1
    assert card.command("play", {}) and played == [items[1].id]
    assert card.command("first", {}) and card.index == 2
    assert card.command("last", {}) and card.index == 0
    assert card.command("number", {"n": 2}) and card.index == 1
    assert not card.command("number", {"n": 9}) and not card.command("number", {"n": "x"})
    card.command("number", {"n": 1})
    assert not card.command("play", {})  # a photo: nothing to play
    assert card.command("share", {}) and shared == [items[0].id]
    assert not card.command("dance", {})
    actions = base.card_types()["gallery"].actions
    assert {"next", "prev", "play", "share", "slideshow", "number"} <= set(actions)


def test_slideshow_moves_on_by_itself(tmp_path: Path) -> None:
    g, items = _gallery(tmp_path)
    card = G.GalleryCard({}, RecordingHost(), gallery=g, items=items, slideshow=True)
    for _ in range(int(G.SLIDESHOW_S * 10) + 2):
        card.update(0.1)
    assert card.index == 1
    card.tap(W - 5, H // 2)  # touching it stops the slideshow
    assert card.slideshow is False


def test_say_cheese_then_the_new_photo(tmp_path: Path) -> None:
    g, items = _gallery(tmp_path)
    card = G.GalleryCard({"title": "Say cheese!"}, RecordingHost(), gallery=g, items=[], countdown=3, linger=8)
    surf = pygame.Surface((W, H))
    card.draw(surf)
    assert card.describe() == "the camera, counting down to take a photo" and card.animating
    card.set_preview(pygame.Surface((W, H)))
    card.set_count(1)
    card.update(0.1)
    card.draw(surf)
    assert card.tap(W // 2, H // 2) and not card.done  # taps do nothing while it counts
    card.taken(items, items[0].id)
    assert card.count is None and card.item.id == items[0].id and card.animating  # the flash
    card.update(0.1)
    card.draw(surf)
    for _ in range(10):
        card.update(0.1)
    assert not card.animating and card.linger_seconds == 8


def test_a_failed_photo_says_why(tmp_path: Path) -> None:
    card = G.GalleryCard({}, RecordingHost(), items=[], countdown=3)
    card.failed("The camera is switched off in Privacy.")
    surf = pygame.Surface((W, H))
    card.draw(surf)
    assert card.error and card.count is None
    assert card.tap(W // 2, H // 2) and card.done  # a tap closes it


def test_an_empty_gallery(tmp_path: Path) -> None:
    card = G.GalleryCard({}, RecordingHost(), items=[])
    card.draw(pygame.Surface((W, H)))
    assert card.describe() == "the photo gallery (empty)" and not card.move(1)


def test_a_broken_file_shows_as_missing(tmp_path: Path) -> None:
    g, items = _gallery(tmp_path)
    g.file(items[0]).write_bytes(b"not a jpeg at all")
    card = G.GalleryCard({}, RecordingHost(), gallery=g, items=items)
    surf = pygame.Surface((W, H))
    _settle(card, surf)  # never raises


def test_the_qr_card(tmp_path: Path) -> None:
    g, items = _gallery(tmp_path)
    url = "http://192.168.2.112:8765/share/AbCdEfGhIjKlMnOpQrStUv"
    card = G.ShareCard({"title": "Scan me"}, RecordingHost(), url=url, gallery=g, item=items[0], hours=24.0)
    surf = pygame.Surface((W, H))
    card.update(0.1)
    card.draw(surf)
    assert "QR code" in card.describe()
    assert card.tap(10, 10) and card.done
    off = G.ShareCard({}, RecordingHost(), url=url, reachable=False)
    off.draw(surf)
    assert "LAN access" in off.describe()


def test_the_viewfinder_is_mirrored(tmp_path: Path) -> None:
    surf = pygame.Surface((64, 36))
    surf.fill((0, 0, 0))
    surf.fill((255, 255, 255), pygame.Rect(0, 0, 32, 36))  # white on the left
    path = tmp_path / "half.png"
    pygame.image.save(surf, str(path))
    got = G.viewfinder_frame(path.read_bytes())
    assert got is not None
    w = got.get_width()
    assert got.get_at((w - 4, got.get_height() // 2))[0] > 200  # now on the right, like a mirror


async def test_built_from_a_spec_only_with_the_service(tmp_path: Path) -> None:
    from littlebot.testing import make_test_context

    ctx = make_test_context()
    assert await base.build({"card": "gallery"}, ctx, RecordingHost()) is None
    g, items = _gallery(tmp_path)
    ctx.capture = type("C", (), {"gallery": g})()
    card = await base.build({"card": "gallery"}, ctx, RecordingHost())
    assert isinstance(card, G.GalleryCard) and len(card.items) == 3
    ctx.db.close()
