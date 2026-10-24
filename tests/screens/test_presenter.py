"""The presenter: LittleBot's small eyes and lip-synced mouth in a card's corner."""

from __future__ import annotations

import itertools

import pytest
from screens_helpers import DummyCard

from littlebot.screens import presenter, preview
from littlebot.screens.base import PRESENTER_BOX, H, W

pygame = pytest.importorskip("pygame")

BG = (40, 160, 90)


def canvas():
    s = pygame.Surface((W, H))
    s.fill(BG)
    return s


def changed(surf, rect):
    """Pixels (coarse sample) inside / outside ``rect`` that differ from the plain background."""
    r = pygame.Rect(rect)
    inside = outside = 0
    for x in range(0, W, 4):
        for y in range(0, H, 4):
            if tuple(surf.get_at((x, y)))[:3] != BG:
                if r.inflate(16, 16).collidepoint(x, y):  # (+ the patch's soft shadow)
                    inside += 1
                else:
                    outside += 1
    return inside, outside


@pytest.mark.parametrize("mood", ["neutral", "happy", "surprised", "curious", "unknown"])
@pytest.mark.parametrize("mouth", [0.0, 0.5, 1.0])
def test_draws_only_inside_its_box(mood, mouth):
    surf = canvas()
    presenter.draw(surf, PRESENTER_BOX, mood=mood, mouth=mouth, color=(90, 210, 255), t=2.0)
    inside, outside = changed(surf, PRESENTER_BOX)
    assert inside > 200 and outside == 0


def test_moved_box_and_no_backing():
    box = (540, 370, 160, 96)
    surf = canvas()
    presenter.draw(surf, box, t=3.0, backing=False)
    inside, outside = changed(surf, box)
    assert inside > 50 and outside == 0


def test_eyes_use_the_eye_colour():
    surf = canvas()
    presenter.draw(surf, PRESENTER_BOX, color=(255, 60, 60), t=2.0, backing=False)
    x, y, w, h = PRESENTER_BOX
    reds = sum(1 for px in range(x, x + w, 2) for py in range(y, y + h, 2)
               if (c := surf.get_at((px, py)))[0] > 180 and c[1] < 140 and c[2] < 140)
    assert reds > 30


def test_pops_in():
    surf = canvas()
    presenter.draw(surf, PRESENTER_BOX, t=0.0)
    assert changed(surf, PRESENTER_BOX) == (0, 0)


def test_blinks_now_and_then_and_is_deterministic():
    ts = [i / 100 for i in range(3000)]
    closed = [t for t in ts if presenter.openness(t) < 0.5]
    assert closed  # it blinks
    assert len(closed) / len(ts) < 0.08  # but the eyes are open nearly all the time
    starts = [t for prev, t in itertools.pairwise(ts) if presenter.openness(prev) >= 0.5 > presenter.openness(t)]
    assert 5 <= len(starts) <= 14  # every few seconds over 30 s
    assert [presenter.openness(t) for t in ts[:500]] == [presenter.openness(t) for t in ts[:500]]


def test_glances_at_the_card_now_and_then():
    looks = [presenter.gaze(i / 10, PRESENTER_BOX) for i in range(9, 200)]
    at_card = [g for g in looks if g[0] > 0.6 and g[1] > 0.4]  # the card is right of / below the corner
    at_viewer = [g for g in looks if abs(g[0]) < 0.1]
    assert at_card and at_viewer and len(at_viewer) > len(at_card)


def test_mouth_opens_with_the_voice():
    closed = presenter.mouth_sprite(presenter.mouth_pose("neutral", 0.0), 0.6)
    open_ = presenter.mouth_sprite(presenter.mouth_pose("neutral", 0.9, "ah"), 0.6)
    assert open_.get_height() > closed.get_height() + 6
    assert presenter.mouth_pose("neutral", 0.0) == presenter.REST["neutral"]
    assert presenter.mouth_pose("happy", 0.0).smile > presenter.mouth_pose("neutral", 0.0).smile
    assert presenter.mouth_pose("neutral", 0.5, "oo").round == 1.0  # the face's spoken shapes
    assert presenter.mouth_pose("neutral", 0.5, "ee").teeth == 1.0


def test_layout_fits_the_box():
    for box in (PRESENTER_BOX, (0, 0, 120, 70), (300, 200, 240, 140)):
        cx, cy, s, my = presenter.layout(box)
        x, y, w, h = box
        assert x < cx < x + w and y < cy < my < y + h
        assert (presenter.art.EYE_GAP + presenter.art.EYE_W) * s <= w


def test_preview_draws_the_presenter(tmp_path):
    out = preview.render(DummyCard({"title": "x"}), tmp_path / "card.png", seconds=1.0, mouth=0.8)
    img = pygame.image.load(str(out))
    assert img.get_size() == (W, H)
    x, y, w, h = PRESENTER_BOX
    assert tuple(img.get_at((x + w // 2, y + h // 2)))[:3] != BG  # the pink patch, not the card's green


def _script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "screen_preview.py"
    spec = importlib.util.spec_from_file_location("screen_preview_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_screen_preview_script_uses_the_card_modules_samples(tmp_path, dummy_kind, monkeypatch):
    import screens_helpers

    script = _script()
    ct = script.base.card_types()[dummy_kind]
    assert script.samples_for(ct) == []  # no SAMPLES and no built-in one: skipped
    monkeypatch.setattr(screens_helpers, "SAMPLES", [{"card": dummy_kind, "title": "One"},
                                                     {"card": "other", "title": "not mine"},
                                                     {"card": dummy_kind, "title": "Two"}], raising=False)
    assert [s["title"] for s in script.samples_for(ct)] == ["One", "Two"]
    assert script.main(["--out", str(tmp_path), "--kind", dummy_kind, "--mouth", "0.6"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{dummy_kind}_1.png", f"{dummy_kind}_2.png"]
    assert script.main(["--out", str(tmp_path), "--kind", "no_such_kind"]) == 1
