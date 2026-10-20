"""Renderer: every expression / state / animation draws onto a 480x320 surface with visible eyes."""

from __future__ import annotations

import pytest
from face_stubs import make_animator, step

from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS, FACE_STATES
from littlebot.face.effects import ANIMATION_DURATIONS
from littlebot.face.frame import EyeFrame, FaceFrame, MouthFrame, Sprite

pygame = pytest.importorskip("pygame")


@pytest.fixture(scope="module")
def renderer():
    pygame.display.init()
    pygame.font.init()
    from littlebot.face.renderer import FaceRenderer

    yield FaceRenderer()
    pygame.display.quit()


def _settled(expression=None, state=None, seconds=1.0, **cfg):
    a = make_animator(seed=4, idle_gaze=False, **cfg)
    a.next_blink = 1e9
    if state:
        a.set_state(state)
    if expression:
        a.set_expression(expression)
    step(a, seconds, 1 / 30)
    return a


def _bright_pixels(surf, rect, bg, threshold=60):
    """Count pixels in rect noticeably brighter than the background."""
    rect = rect.clip(surf.get_rect())
    n = 0
    for x in range(rect.left, rect.right, 2):
        for y in range(rect.top, rect.bottom, 2):
            c = surf.get_at((x, y))
            if (c.r + c.g + c.b) - sum(bg) > threshold:
                n += 1
    return n


def _eye_rect(e: EyeFrame):
    return pygame.Rect(int(e.cx - e.w / 2), int(e.cy - e.h / 2), int(e.w), int(e.h))


@pytest.mark.parametrize("name", EXPRESSIONS)
def test_every_expression_renders_visible_eyes(renderer, name):
    a = _settled(expression=name)
    f = a.frame()
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    for e in f.eyes:
        r = _eye_rect(e)
        area = max(1, (r.w // 2) * (r.h // 2))
        assert _bright_pixels(surf, r, f.background) > area * 0.25, f"{name}: eye not visible"
    # background stays dark in the corners (no runaway glow)
    for corner in ((2, 2), (477, 317)):
        c = surf.get_at(corner)
        assert c.r + c.g + c.b < 90


@pytest.mark.parametrize("state", FACE_STATES)
def test_every_state_renders(renderer, state):
    a = _settled(state=state, seconds=1.5)
    f = a.frame()
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    if state == "hidden":
        assert f.hidden == 1.0
        # calm dark screen, only the small glyph near the bottom-right corner
        assert _bright_pixels(surf, pygame.Rect(40, 40, 360, 220), f.background, 40) == 0
        assert _bright_pixels(surf, pygame.Rect(430, 280, 50, 40), f.background, 25) > 0
    else:
        for e in f.eyes:
            assert _bright_pixels(surf, _eye_rect(e).inflate(8, 8), f.background) > 3


def test_closed_eyes_draw_a_line(renderer):
    a = _settled()
    a.close_eyes()
    step(a, 0.5)
    f = a.frame()
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    for e in f.eyes:
        assert _bright_pixels(surf, _eye_rect(e).inflate(4, 24), f.background) > 10
        # nothing bright far above the closed line
        above = pygame.Rect(int(e.cx - e.w / 2), int(e.cy - 60), int(e.w), 40)
        assert _bright_pixels(surf, above, f.background, 90) == 0


@pytest.mark.parametrize("name", FACE_ANIMATIONS)
def test_every_animation_renders(renderer, name):
    a = _settled(expression="happy", seconds=0.3)
    a.play_animation(name)
    surf = pygame.Surface((480, 320))
    for _ in range(6):
        step(a, ANIMATION_DURATIONS[name] / 6, 1 / 30)
        renderer.render(a.frame(), surf)
    assert surf.get_at((240, 160)) is not None


def test_mouth_and_all_sprite_kinds_render(renderer):
    a = _settled()
    f0 = a.frame()
    kinds = ["heart", "sparkle", "z", "question", "dot", "confetti", "blush"]  # "play" only shows when hidden
    sprites = tuple(Sprite(k, 60 + i * 50, 280, 24, 1.0, angle=17 * i, color=(255, 200, 120))
                    for i, k in enumerate(kinds))
    f = FaceFrame(width=480, height=320, t=1.0, background=f0.background, color=f0.color, glow=1.0,
                  eyes=f0.eyes, mouth=MouthFrame(240, 250, 0.7, 1.0), sprites=sprites, ring=0.8, hidden=0.0)
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    for s in sprites:
        assert _bright_pixels(surf, pygame.Rect(int(s.x) - 14, int(s.y) - 14, 28, 28), f.background, 40) > 0, s.kind
    # mouth area lit
    assert _bright_pixels(surf, pygame.Rect(215, 235, 50, 40), f.background) > 0
    # listening ring lights up the screen edge
    assert _bright_pixels(surf, pygame.Rect(0, 100, 14, 100), f.background, 30) > 0


def test_render_into_scaled_rect_and_cache(renderer):
    a = _settled(expression="happy")
    window = pygame.Surface((1100, 700))
    rect = pygame.Rect(10, 20, 960, 640)
    renderer.render(a.frame(), window, rect)
    assert window.get_at((5, 5))[:3] == (0, 0, 0)  # outside the rect untouched
    assert renderer.last_ms > 0
    before = renderer.cache_info()["eye_hits"]
    renderer.render(a.frame(), window, rect)
    assert renderer.cache_info()["eye_hits"] >= before + 2


def test_config_colours_and_glow_off(renderer):
    a = _settled(eye_color=(255, 120, 40), background_color=(30, 0, 0), glow=False)
    f = a.frame()
    assert f.glow == 0.0
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    e = f.eyes[0]
    c = surf.get_at((int(e.cx), int(e.cy + e.h * 0.2)))
    assert c.r > c.b  # orange eyes
    corner = surf.get_at((1, 1))
    assert corner.r >= corner.g and corner.r >= corner.b  # reddish background


def test_renderer_handles_partial_hidden_fade(renderer):
    a = _settled()
    a.set_state("hidden")
    a.update(0.2)
    f = a.frame()
    assert 0 < f.hidden < 1
    renderer.render(f, pygame.Surface((480, 320)))
