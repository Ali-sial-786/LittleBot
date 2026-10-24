"""Renderer: every expression / state / animation draws onto a 480x320 surface with visible eyes and mouth."""

from __future__ import annotations

import dataclasses

import pytest
from face_stubs import make_animator, step

from littlebot.core.interfaces import EXPRESSIONS, FACE_ANIMATIONS, FACE_STATES
from littlebot.face.effects import ANIMATION_DURATIONS
from littlebot.face.frame import (
    MOUTH_W, EyeFrame, FaceFrame, MouthFrame, Sprite, catchlights, is_light_background, open_span_at, rotate,
)

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


def _visible_pixels(surf, rect, bg, threshold=60):
    """Count pixels in rect that clearly stand out from the background colour (lighter or darker)."""
    rect = rect.clip(surf.get_rect())
    n = 0
    for x in range(rect.left, rect.right, 2):
        for y in range(rect.top, rect.bottom, 2):
            c = surf.get_at((x, y))
            if abs(c.r - bg[0]) + abs(c.g - bg[1]) + abs(c.b - bg[2]) > threshold:
                n += 1
    return n


def _close_to(c, ref, tol):
    return abs(c[0] - ref[0]) + abs(c[1] - ref[1]) + abs(c[2] - ref[2]) <= tol


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
        assert _visible_pixels(surf, r, f.background) > area * 0.25, f"{name}: eye not visible"
    # the corners keep the background colour (no runaway glow / shadow)
    for corner in ((2, 2), (477, 317)):
        assert _close_to(surf.get_at(corner), f.background, 45)


@pytest.mark.parametrize("state", FACE_STATES)
def test_every_state_renders(renderer, state):
    a = _settled(state=state, seconds=1.5)
    f = a.frame()
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    if state == "hidden":
        assert f.hidden == 1.0 and f.mouth is None
        # calm, dim screen: nothing but the small glyph near the bottom-right corner
        veil = tuple(surf.get_at((240, 160)))[:3]
        assert sum(veil) < sum(f.background) * 0.5
        assert _visible_pixels(surf, pygame.Rect(40, 40, 360, 220), veil, 20) == 0
        assert _visible_pixels(surf, pygame.Rect(430, 280, 50, 40), veil, 25) > 0
    else:
        for e in f.eyes:
            assert _visible_pixels(surf, _eye_rect(e).inflate(8, 8), f.background) > 3
        m = f.mouth
        assert _visible_pixels(surf, pygame.Rect(int(m.cx) - 30, int(m.cy) - 20, 60, 40), f.background) > 5


def test_closed_eyes_draw_a_line(renderer):
    a = _settled()
    a.close_eyes()
    step(a, 0.5)
    f = a.frame()
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    for e in f.eyes:
        assert _visible_pixels(surf, _eye_rect(e).inflate(4, 24), f.background) > 10
        # nothing drawn far above the closed line
        above = pygame.Rect(int(e.cx - e.w / 2), int(e.cy - 60), int(e.w), 40)
        assert _visible_pixels(surf, above, f.background, 90) == 0
        # on the pink screen a closed eye is a dark ink line
        line = [surf.get_at((int(e.cx) + dx, int(e.cy) + dy)) for dx in range(-20, 21, 5) for dy in range(-6, 7)]
        assert min(c.r + c.g + c.b for c in line) < 260


@pytest.mark.parametrize("name", FACE_ANIMATIONS)
def test_every_animation_renders(renderer, name):
    a = _settled(expression="happy", seconds=0.3)
    a.play_animation(name)
    surf = pygame.Surface((480, 320))
    for _ in range(6):
        step(a, ANIMATION_DURATIONS[name] / 6, 1 / 30)
        f = a.frame()
        renderer.render(f, surf)
        m = f.mouth  # the mouth is drawn in every frame of every animation
        assert _visible_pixels(surf, pygame.Rect(int(m.cx) - 30, int(m.cy) - 20, 60, 40), f.background) > 5, name


def test_mouth_and_all_sprite_kinds_render(renderer):
    a = _settled()
    f0 = a.frame()
    kinds = ["heart", "sparkle", "z", "question", "dot", "cloud", "puff", "confetti", "blush"]  # "play" only shows when hidden
    sprites = tuple(Sprite(k, 60 + i * 50, 280, 24, 1.0, angle=17 * i, color=(255, 200, 120))
                    for i, k in enumerate(kinds))
    f = FaceFrame(width=480, height=320, t=1.0, background=f0.background, color=f0.color, glow=1.0,
                  eyes=f0.eyes, mouth=MouthFrame(240, 250, 0.7, 1.0), sprites=sprites, ring=0.8, hidden=0.0)
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    for s in sprites:
        assert _visible_pixels(surf, pygame.Rect(int(s.x) - 14, int(s.y) - 14, 28, 28), f.background, 40) > 0, s.kind
    # mouth drawn
    assert _visible_pixels(surf, pygame.Rect(215, 235, 50, 40), f.background) > 0
    # listening ring shows along the screen edge
    assert _visible_pixels(surf, pygame.Rect(0, 100, 14, 100), f.background, 30) > 0


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
    c = surf.get_at((int(e.cx), int(e.cy + e.h * 0.42)))  # iris, below the (dark) pupil
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


# ------------------------------------------------------------------ cartoon eyes: pupils + catchlights
def _dark_centroid(surf, e: EyeFrame, threshold=90):
    """Mean offset from the eye centre of the dark (pupil) pixels inside the eye's box."""
    xs, ys = [], []
    for x in range(int(e.cx - e.w / 2) + 6, int(e.cx + e.w / 2) - 6):
        for y in range(int(e.cy - e.h / 2) + 6, int(e.cy + e.h / 2) - 6):
            c = surf.get_at((x, y))
            if c.r + c.g + c.b < threshold:
                xs.append(x - e.cx)
                ys.append(y - e.cy)
    assert len(xs) > 200, "no pupil drawn"
    return sum(xs) / len(xs), sum(ys) / len(ys)


def test_pupils_are_drawn_and_follow_the_gaze(renderer):
    offsets = {}
    for name, (x, y) in {"left": (0.0, 0.5), "right": (1.0, 0.5), "up": (0.5, 0.0), "down": (0.5, 1.0)}.items():
        a = _settled()
        a.look_at(x, y, hold=10)
        step(a, 1.0, 1 / 30)
        f = a.frame()
        surf = pygame.Surface((480, 320))
        renderer.render(f, surf)
        offsets[name] = [_dark_centroid(surf, e) for e in f.eyes]
    for i in range(2):  # both eyes: the pupil moves inside the eye, not just the whole eye
        assert offsets["left"][i][0] < -6 < 6 < offsets["right"][i][0]
        assert offsets["up"][i][1] < offsets["down"][i][1] - 12


def test_catchlights_are_white_and_stay_put_while_the_pupil_moves(renderer):
    spots = []
    for x in (0.0, 1.0):
        a = _settled()
        a.look_at(x, 0.5, hold=10)
        step(a, 1.0, 1 / 30)
        f = a.frame()
        surf = pygame.Surface((480, 320))
        renderer.render(f, surf)
        e = f.eyes[0]
        lights = catchlights(e)
        assert len(lights) == 2
        for ox, oy, _r in lights:
            c = surf.get_at((int(round(e.cx + ox)), int(round(e.cy + oy))))
            assert min(c.r, c.g, c.b) > 225, c  # crisp white
        spots.append([(round(ox), round(oy)) for ox, oy, _r in lights])
    left, right = spots
    for (ax, ay), (bx, by) in zip(left, right):
        assert abs(ax - bx) <= 4 and abs(ay - by) <= 4  # only the eye's perspective changes them


@pytest.mark.parametrize("name", ["neutral", "sleepy", "thinking", "curious", "sad", "excited", "happy"])
def test_pupil_and_catchlights_never_draw_outside_the_eye(renderer, name):
    a = _settled(expression=name)
    a.look_at(1.0, 0.0, hold=10)  # extreme gaze, pushes the pupil towards the lids / outline
    step(a, 1.0, 1 / 30)
    f = a.frame()
    bare = dataclasses.replace(f, eyes=tuple(dataclasses.replace(e, pupil=0.0, shine=0.0) for e in f.eyes))
    with_parts, without = pygame.Surface((480, 320)), pygame.Surface((480, 320))
    renderer.render(f, with_parts)
    renderer.render(bare, without)
    changed = 0
    for e in f.eyes:
        box = _eye_rect(e).inflate(40, 40).clip(with_parts.get_rect())
        for x in range(box.left, box.right):
            for y in range(box.top, box.bottom):
                if with_parts.get_at((x, y)) == without.get_at((x, y)):
                    continue
                changed += 1
                lx, ly = rotate(x + 0.5 - e.cx, y + 0.5 - e.cy, -e.tilt)
                top, bottom = open_span_at(e, lx)
                assert abs(lx) <= e.w / 2 + 1.5 and top - 1.5 <= ly <= bottom + 1.5, (name, x, y)
    assert changed > 50  # the overlays really were drawn


# ------------------------------------------------------------------ pink screen + mouth shapes
def test_default_look_is_light_pink_with_inked_eyes(renderer):
    a = _settled()
    f = a.frame()
    assert is_light_background(f.background)
    r, g, b = f.background
    assert r > 240 and r > b > g > 180  # a soft light pink
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    for x, y in ((2, 2), (240, 12), (470, 300)):
        c = surf.get_at((x, y))
        assert c.r > 230 and c.r > c.b > c.g  # pink everywhere around the face
    e = f.eyes[0]
    left_edge = [surf.get_at((int(e.cx - e.w / 2) + dx, int(e.cy) + 10)) for dx in range(0, 7)]
    assert min(c.r + c.g + c.b for c in left_edge) < 200  # a dark ink rim: crisp against the pink


def test_dark_background_still_glows(renderer):
    a = _settled(background_color=(6, 8, 14))
    f = a.frame()
    assert not is_light_background(f.background)
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    c = surf.get_at((2, 2))
    assert c.r + c.g + c.b < 60
    e = f.eyes[0]
    halo = surf.get_at((int(e.cx), int(e.cy - e.h / 2 - 6)))  # just above the eye: glow
    assert halo.b > 25


def _mouth_box(renderer, m, bg=(255, 214, 228), color=(90, 210, 255)):
    """Bounding box of the drawn mouth and the number of near-white (teeth) pixels in it."""
    surf = pygame.Surface((200, 160))
    surf.fill(bg)
    renderer._draw_mouth(surf, m, color, is_light_background(bg))
    xs, ys, white = [], [], 0
    for x in range(200):
        for y in range(160):
            c = surf.get_at((x, y))
            if _close_to(c, bg, 40):
                continue
            xs.append(x)
            ys.append(y)
            white += c.r > 235 and c.g > 235 and c.b > 235
    assert xs, "mouth not drawn"
    return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, white


def test_mouth_shapes_render_distinctly(renderer):
    base = dict(cx=100, cy=70, alpha=1.0)
    closed = _mouth_box(renderer, MouthFrame(open=0.0, width=MOUTH_W * 0.92, smile=0.4, **base))
    ah = _mouth_box(renderer, MouthFrame(open=0.9, width=MOUTH_W * 1.06, smile=0.4, **base))
    ee = _mouth_box(renderer, MouthFrame(open=0.35, width=MOUTH_W * 1.3, smile=0.4, teeth=1.0, **base))
    oo = _mouth_box(renderer, MouthFrame(open=0.5, width=MOUTH_W * 0.6, round=1.0, **base))
    assert closed[1] < 16 < ah[1]  # a line vs. a wide-open mouth
    assert ah[1] > ee[1] and ee[0] > ah[0] > oo[0]  # ee is wide and flat, oo narrow
    assert oo[1] >= oo[0] * 0.85  # round
    assert ee[2] > 10 and ah[2] == 0  # teeth only for "ee"
    frown = _mouth_box(renderer, MouthFrame(open=0.0, width=MOUTH_W, smile=-0.8, **base))
    assert frown[1] < 20
    tongue = _mouth_box(renderer, MouthFrame(open=0.0, width=MOUTH_W, smile=0.8, tongue=1.0, **base))
    assert tongue[1] > closed[1] + 8  # the cheeky tongue hangs below the lip


def test_mouth_cache_reuses_shapes(renderer):
    renderer.clear_cache()
    before = renderer.cache_info()
    surf = pygame.Surface((480, 320))
    for i in range(3):  # tiny differences quantise to the same cached image
        m = MouthFrame(cx=240 + i, cy=246, open=0.42 + i * 0.002, alpha=1.0, smile=0.6)
        renderer._draw_mouth(surf, m, (90, 210, 255), True)
    info = renderer.cache_info()
    assert info["mouth_misses"] - before["mouth_misses"] == 1
    assert info["mouth_hits"] - before["mouth_hits"] == 2


@pytest.mark.parametrize("expression", ["neutral", "happy", "sad", "surprised", "playful", "celebrating"])
def test_mouth_is_dark_ink_on_the_pink_screen(renderer, expression):
    a = _settled(expression=expression)
    f = a.frame()
    surf = pygame.Surface((480, 320))
    renderer.render(f, surf)
    m = f.mouth
    box = pygame.Rect(int(m.cx) - 40, int(m.cy) - 25, 80, 60)
    darkest = min(surf.get_at((x, y)).r + surf.get_at((x, y)).g + surf.get_at((x, y)).b
                  for x in range(box.left, box.right) for y in range(box.top, box.bottom))
    assert darkest < 250, expression
