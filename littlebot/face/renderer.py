"""Pygame renderer: turns a :class:`FaceFrame` into pixels.

Look: a soft vignette background and two big, round cartoon eyes: an
iris-coloured body with a darker "ink" rim inside the outline (and along the
lids), a soft vertical gradient, a big dark pupil that looks around inside the
eye, and two bright white catchlights that stay put (they belong to the light,
not the pupil). Below them a little mouth, always there: a resting smile /
frown / "o" per expression, lip-synced to the voice while LittleBot talks.
Plus optional cute extras.

Two looks, picked from the background colour: on a light (pastel pink) screen
the features are inked: deep rims, dark closed-eye lines and mouth, and the
eyes cast a soft rosy shadow; on a dark screen the eyes glow instead.

Performance notes (the Pi 5 must hold 30 fps at low CPU):

* Eyes are drawn super-sampled (``supersample`` x) with plain pygame
  primitives and smooth-scaled down, which gives clean anti-aliased edges for
  any lid / crescent shape. The eye body (plus its blurred glow) is cached in a
  small LRU keyed by the quantised shape, so a calm face costs only blits.
* Pupil and catchlights are small cached sprites composited onto the cached
  body every frame in a reused scratch surface; the pupil is clipped to the
  body (``BLEND_RGBA_MIN`` against the body itself), the catchlights are kept
  inside the open eye by geometry. Gaze changes never re-render the eye, and
  nothing can poke outside the eye outline.
* Glow (or the shadow on a light screen) = core shape downscaled 4x,
  gaussian-blurred, scaled back up.
* The mouth is drawn from :func:`~littlebot.face.frame.mouth_edges` and cached
  by its quantised shape, so a resting mouth is one blit and talking reuses a
  few dozen cached shapes.
* Sprites (hearts, sparkles, Z's, confetti ...) are rendered once per
  (kind, size, colour, angle-bucket) and cached.
* Everything is drawn at the native 480x320 and scaled once at the end if the
  target is a different size.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from collections import OrderedDict
from typing import Any

import pygame

from littlebot.face.frame import (
    LID_BOW, MIN_LINE, MOUTH_LINE, NATIVE_H, NATIVE_W, RIM, SMILE_DEPTH, SMILE_RADIUS, Color, EyeFrame, FaceFrame,
    MouthFrame, Sprite, catchlights, eye_span, is_light_background, mouth_edges, pupil_geometry,
)

log = logging.getLogger("littlebot.face.renderer")

EYE_CACHE_SIZE = 96
SPRITE_CACHE_SIZE = 600
GLOW_MARGIN = 24
GLOW_DOWNSCALE = 4
GLOW_BLUR = 4
GLOW_ALPHA = 0.9  # base glow opacity at glow=1
SHADE_ROWS = 96  # resolution of the eye's vertical colour gradient
RIM_GRAY = 118  # rim brightness relative to the eye colour (255 = no rim)
RIM_GRAY_LIGHT = 60  # on a light screen the rim is a deep ink line: crisp edges against the pastel
SHADOW_OFFSET = 7  # px, the eyes' soft shadow on a light screen falls a little below them
SHADOW_ALPHA = 0.55
MOUTH_CACHE_SIZE = 160
# Mouth colours on a light screen (fixed, so a tinted eye colour never turns the mouth odd)
MOUTH_INK: Color = (66, 32, 62)  # deep plum line
MOUTH_INSIDE: Color = (146, 40, 82)  # raspberry
TONGUE: Color = (255, 120, 146)
TEETH: Color = (255, 255, 255)
CLOSED_THICKNESS = 11.0  # px, sleeping / peekaboo closed-eye arcs (grows from MIN_LINE as they curve)
CLOSED_SAG = 12.0  # px the closed-eye arc dips in the middle (content '‿')


def _q(v: float, step: float) -> int:
    return int(round(v / step))


def _lighten(c: Color, k: float) -> Color:
    return (int(c[0] + (255 - c[0]) * k), int(c[1] + (255 - c[1]) * k), int(c[2] + (255 - c[2]) * k))


def _scale(c: Color, k: float) -> Color:
    return (max(0, min(255, int(c[0] * k))), max(0, min(255, int(c[1] * k))), max(0, min(255, int(c[2] * k))))


def _closed_thickness(e: EyeFrame) -> float:
    return MIN_LINE + (CLOSED_THICKNESS - MIN_LINE) * max(0.0, min(1.0, e.closed_curve))


def _mix(a: Color, b: Color, k: float) -> Color:
    return (int(a[0] + (b[0] - a[0]) * k), int(a[1] + (b[1] - a[1]) * k), int(a[2] + (b[2] - a[2]) * k))


def _ink(c: Color) -> Color:
    """Deep shade of the eye colour for lines drawn on a light screen (closed eyes)."""
    return _mix(_scale(c, 0.42), (40, 30, 70), 0.3)


def _veil_color(bg: Color) -> Color:
    """The calm "video playing" screen: dimmed, and quite dark even when the face is pastel."""
    return _scale(bg, 0.32 if is_light_background(bg) else 0.6)


def _pupil_color(c: Color) -> Color:
    """Near-black navy with a hint of the eye colour: maximum contrast, still harmonious."""
    return (int(4 + c[0] * 0.06), int(7 + c[1] * 0.06), int(18 + c[2] * 0.06))


def _rrect(surf: pygame.Surface, col: tuple[int, ...], rect: pygame.Rect, radius: int) -> None:
    """Filled rounded rect from two fills + four circles: same shape as ``draw.rect(border_radius=)``, ~6x faster."""
    r = max(0, min(int(radius), rect.w // 2, rect.h // 2))
    if rect.w <= 0 or rect.h <= 0:
        return
    if r <= 0:
        surf.fill(col, rect)
        return
    surf.fill(col, pygame.Rect(rect.x + r, rect.y, rect.w - 2 * r, rect.h))
    surf.fill(col, pygame.Rect(rect.x, rect.y + r, rect.w, rect.h - 2 * r))
    for cx, cy in ((rect.x + r, rect.y + r), (rect.right - r - 1, rect.y + r),
                   (rect.x + r, rect.bottom - r - 1), (rect.right - r - 1, rect.bottom - r - 1)):
        pygame.draw.circle(surf, col, (cx, cy), r)


def _fast(surf: pygame.Surface) -> pygame.Surface:
    """Convert to the display's pixel format (faster blits) when a display exists."""
    try:
        if pygame.display.get_init() and pygame.display.get_surface() is not None:
            return surf.convert_alpha()
    except pygame.error:
        pass
    return surf


class _LRU(OrderedDict):  # type: ignore[type-arg]
    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0

    def get_or(self, key: Any, make: Any) -> Any:
        try:
            v = self[key]
            self.move_to_end(key)
            self.hits += 1
            return v
        except KeyError:
            self.misses += 1
            v = make()
            self[key] = v
            if len(self) > self.maxsize:
                self.popitem(last=False)
            return v


class FaceRenderer:
    """Draws face frames. Not thread-safe: use from the display (main) thread only."""

    def __init__(self, size: tuple[int, int] = (NATIVE_W, NATIVE_H), *, supersample: int = 3) -> None:
        self.size = (int(size[0]), int(size[1]))
        self.ss = max(1, int(supersample))
        self._native: pygame.Surface | None = None
        self._scaled: pygame.Surface | None = None
        self._eyes = _LRU(EYE_CACHE_SIZE)
        self._glows = _LRU(EYE_CACHE_SIZE)
        self._sprites = _LRU(SPRITE_CACHE_SIZE)
        self._mouths = _LRU(MOUTH_CACHE_SIZE)
        self._static: dict[Any, pygame.Surface] = {}
        self._comp: dict[tuple[int, int], pygame.Surface] = {}  # per-size eye compositing scratch
        self._tmp: pygame.Surface | None = None  # super-sampled scratch for rim bands (cache misses only)
        self._font: Any = None
        self._font_failed = False
        self.last_ms = 0.0

    # ------------------------------------------------------------------ API
    def render(self, frame: FaceFrame, target: pygame.Surface, rect: pygame.Rect | None = None) -> None:
        """Draw ``frame`` into ``target`` (optionally into ``rect`` of it, scaled smoothly)."""
        t0 = time.perf_counter()
        dest_rect = rect or target.get_rect()
        if dest_rect.size == self.size and dest_rect.topleft == (0, 0) and target.get_size() == self.size:
            self._draw(frame, target)
        else:
            if self._native is None:
                self._native = pygame.Surface(self.size)
            self._draw(frame, self._native)
            if dest_rect.size == self.size:
                target.blit(self._native, dest_rect.topleft)
            else:
                if self._scaled is None or self._scaled.get_size() != dest_rect.size:
                    self._scaled = pygame.Surface(dest_rect.size)
                pygame.transform.smoothscale(self._native, dest_rect.size, self._scaled)
                target.blit(self._scaled, dest_rect.topleft)
        self.last_ms = (time.perf_counter() - t0) * 1000.0

    def cache_info(self) -> dict[str, int]:
        return {"eye_hits": self._eyes.hits, "eye_misses": self._eyes.misses, "eye_entries": len(self._eyes),
                "glow_hits": self._glows.hits, "glow_misses": self._glows.misses,
                "sprite_hits": self._sprites.hits, "sprite_misses": self._sprites.misses,
                "sprite_entries": len(self._sprites), "mouth_hits": self._mouths.hits,
                "mouth_misses": self._mouths.misses}

    def clear_cache(self) -> None:
        self._eyes.clear()
        self._glows.clear()
        self._sprites.clear()
        self._mouths.clear()
        self._static.clear()
        self._comp.clear()
        self._tmp = None

    # ------------------------------------------------------------------ frame
    def _draw(self, f: FaceFrame, surf: pygame.Surface) -> None:
        surf.blit(self._background(f.background), (0, 0))
        if f.hidden >= 0.999:
            self._draw_hidden(f, surf)
            return
        light = is_light_background(f.background)
        if f.ring > 0.01:
            ring = self._ring(f.color, light)
            ring.set_alpha(int(255 * min(1.0, f.ring)))
            surf.blit(ring, (0, 0))
        sprites_front: list[Sprite] = []
        for s in f.sprites:
            if s.kind == "blush":
                self._draw_sprite(surf, s, light)
            elif s.kind != "play":
                sprites_front.append(s)
        for eye in f.eyes:
            self._draw_eye(surf, eye, f.color, f.glow, light)
        if f.mouth is not None:
            self._draw_mouth(surf, f.mouth, f.color, light)
        for s in sprites_front:
            self._draw_sprite(surf, s, light)
        if f.hidden > 0.001:
            veil = self._static.get(("veil", f.background))
            if veil is None:
                veil = pygame.Surface(self.size)
                veil.fill(_veil_color(f.background))
                self._static[("veil", f.background)] = veil
            veil.set_alpha(int(255 * min(1.0, f.hidden)))
            surf.blit(veil, (0, 0))
            for s in f.sprites:
                if s.kind == "play":
                    self._draw_sprite(surf, s)

    def _draw_hidden(self, f: FaceFrame, surf: pygame.Surface) -> None:
        surf.fill(_veil_color(f.background))
        for s in f.sprites:
            if s.kind == "play":
                self._draw_sprite(surf, s)

    # ------------------------------------------------------------------ background
    def _background(self, bg: Color) -> pygame.Surface:
        key = ("bg", bg, self.size)
        s = self._static.get(key)
        if s is None:
            s = self._make_background(bg)
            self._static[key] = s
        return s

    def _make_background(self, bg: Color) -> pygame.Surface:
        w, h = self.size
        surf = pygame.Surface((w, h))
        try:
            import numpy as np

            ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
            d = np.sqrt(((xs - w / 2) / (w * 0.62)) ** 2 + ((ys - h * 0.46) / (h * 0.7)) ** 2)
            k = np.clip(1.0 - d, 0.0, 1.0) ** 1.6  # 1 at centre .. 0 at edges
            if is_light_background(bg):
                # pastel: a creamy glow behind the face, a slightly deeper, rosier rim
                centre = np.array(_lighten(bg, 0.5), dtype=np.float32)
                edge = np.array(_scale(bg, 0.95), dtype=np.float32)
            else:
                centre = np.array(_lighten(bg, 0.045), dtype=np.float32) + np.array((2, 4, 10), dtype=np.float32)
                edge = np.array(_scale(bg, 0.7), dtype=np.float32)
            rgb = edge[None, None, :] + (centre - edge)[None, None, :] * k[..., None]
            # tiny ordered dither so the gradient never bands on 16-bit panels
            dither = ((xs.astype(np.int32) ^ ys.astype(np.int32)) & 1).astype(np.float32)[..., None] * 0.5
            arr = np.clip(rgb + dither, 0, 255).astype(np.uint8)
            pygame.surfarray.blit_array(surf, arr.transpose(1, 0, 2))
        except Exception:  # noqa: BLE001 - numpy/surfarray missing: flat background is fine
            surf.fill(bg)
        return surf

    def _ring(self, color: Color, light: bool = False) -> pygame.Surface:
        key = ("ring", color, self.size, light)
        s = self._static.get(key)
        if s is not None:
            return s
        w, h = self.size
        ss = 2
        big = pygame.Surface((w * ss, h * ss), pygame.SRCALPHA)
        inset, th = 7 * ss, 3 * ss
        rc = _mix(color, _ink(color), 0.3) if light else _lighten(color, 0.25)
        pygame.draw.rect(big, (*rc, 255), pygame.Rect(inset, inset, w * ss - 2 * inset, h * ss - 2 * inset),
                         width=th, border_radius=26 * ss)
        line = pygame.transform.smoothscale(big, (w, h))
        small = pygame.transform.smoothscale(line, (w // 4, h // 4))
        small = pygame.transform.gaussian_blur(small, 3)
        glow = pygame.transform.smoothscale(small, (w, h))
        out = pygame.Surface((w, h), pygame.SRCALPHA)
        out.blit(glow, (0, 0))
        if not light:
            out.blit(glow, (0, 0))
        line.set_alpha(210 if light else 150)
        out.blit(line, (0, 0))
        self._static[key] = out
        return out

    # ------------------------------------------------------------------ eyes
    def _draw_eye(self, surf: pygame.Surface, e: EyeFrame, color: Color, glow: float, light: bool = False) -> None:
        # The eye body (outline, rim, gradient, lids) is cached by its quantised shape. The pupil and
        # the catchlights move every frame, so they are composited on top per frame (a few small blits).
        # The pupil is clipped to the body with BLEND_RGBA_MIN against the body itself: alpha becomes
        # the body's alpha (lids / crescent cut hide it) and, as the pupil is darker than the body in
        # every channel, its colour is kept. Catchlights are placed inside the open eye by geometry.
        key = (
            _q(e.w, 0.5), _q(e.h, 0.5), _q(e.radius, 0.5), _q(e.lid_top, 0.01), _q(e.lid_angle, 0.5),
            _q(e.lid_bottom, 0.01), _q(e.smile, 0.01), _q(e.tilt % 360.0, 0.5), _q(e.closed_curve, 0.05),
            e.side, color, glow > 0.01, light,
        )
        body = self._eyes.get_or(key, lambda: self._make_eye(e, color, light))
        cw, ch = body.get_size()
        x, y = int(round(e.cx - cw / 2)), int(round(e.cy - ch / 2))
        if glow > 0.01:
            # the glow is blurry, so a much coarser key is visually identical and saves most recomputes
            gkey = (_q(e.w, 3), _q(e.h, 3), _q(e.lid_top, 0.05), _q(e.lid_angle, 3), _q(e.lid_bottom, 0.05),
                    _q(e.smile, 0.05), _q(e.tilt % 360.0, 3), _q(e.closed_curve, 0.25), e.side, color, light)
            if light:  # a soft rosy shadow just below the eye gives depth on the pastel screen
                glow_s = self._glows.get_or(gkey, lambda: self._make_shadow(body))
                glow_s.set_alpha(int(255 * SHADOW_ALPHA * min(1.0, glow)))
                dy = SHADOW_OFFSET
            else:
                glow_s = self._glows.get_or(gkey, lambda: self._make_glow(body))
                glow_s.set_alpha(int(255 * min(1.0, GLOW_ALPHA * glow)))
                dy = 0
            gw, gh = glow_s.get_size()
            surf.blit(glow_s, (x - (gw - cw) // 2, y - (gh - ch) // 2 + dy))
        closed = e.closed_curve > 0.0 and e.h <= MIN_LINE + 1.0
        pupil = None if closed else self._pupil_overlay(e, color, cw, ch)
        lights = [] if closed else self._catchlight_overlays(e, cw, ch)
        if pupil is None and not lights:
            surf.blit(body, (x, y))
            return
        comp = self._comp_surface(cw, ch)
        comp.fill((0, 0, 0, 0))
        comp.blit(body, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)  # exact copy, alpha included
        if pupil is not None:
            comp.blit(*pupil)
            comp.blit(body, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)  # clip to the eye silhouette
        for img, pos in lights:
            comp.blit(img, pos)
        surf.blit(comp, (x, y))

    def _pupil_overlay(
        self, e: EyeFrame, color: Color, cw: int, ch: int,
    ) -> tuple[pygame.Surface, tuple[int, int]] | None:
        """Pupil sprite and its top-left position inside the eye's canvas (None: no pupil)."""
        pupil = pupil_geometry(e)
        if pupil is None:
            return None
        ox, oy, rx, ry, alpha = pupil
        img = self._pupil_sprite(rx, ry, e.tilt, color)
        img.set_alpha(int(255 * alpha))
        return img, (int(round(cw / 2 + ox - img.get_width() / 2)), int(round(ch / 2 + oy - img.get_height() / 2)))

    def _catchlight_overlays(self, e: EyeFrame, cw: int, ch: int) -> list[tuple[pygame.Surface, tuple[int, int]]]:
        out: list[tuple[pygame.Surface, tuple[int, int]]] = []
        if e.shine > 0.02:
            # catchlights stay crisp white; a dull expression (sleepy) only makes them a bit smaller / softer
            alpha = int(255 * min(1.0, 0.55 + e.shine))
            size = 0.8 + 0.2 * min(1.0, e.shine)
            for ox, oy, r in catchlights(e):
                img = self._catchlight_sprite(r * size)
                img.set_alpha(alpha)
                out.append((img, (int(round(cw / 2 + ox - img.get_width() / 2)),
                                  int(round(ch / 2 + oy - img.get_height() / 2)))))
        return out

    def _comp_surface(self, w: int, h: int) -> pygame.Surface:
        s = self._comp.get((w, h))
        if s is None:
            if len(self._comp) >= 16:
                self._comp.clear()
            s = self._comp[(w, h)] = _fast(pygame.Surface((w, h), pygame.SRCALPHA))
        return s

    def _make_eye(self, e: EyeFrame, color: Color, light: bool = False) -> pygame.Surface:
        """Eye body (silhouette, rim, colour gradient, lids) or a closed eye's curved line."""
        ss = self.ss
        pad = 3
        w, h = max(2.0, e.w), max(2.0, e.h)
        closed = e.closed_curve > 0.0 and h <= MIN_LINE + 1.0
        # a closed eye is a thicker curved line: make room for its sag and thickness
        canvas_h = max(h, _closed_thickness(e) + CLOSED_SAG * e.closed_curve + 2) if closed else h
        cw, ch = int(math.ceil(w + 2 * pad)), int(math.ceil(canvas_h + 2 * pad))
        big = pygame.Surface((cw * ss, ch * ss), pygame.SRCALPHA)
        rx = (cw * ss - w * ss) / 2
        ry = (ch * ss - h * ss) / 2
        rect = pygame.Rect(int(rx), int(ry), int(round(w * ss)), int(round(h * ss)))
        # a nearly shut eye (blink) is a clean bright line: the rim and gradient fade out as it thins
        thin = max(0.0, min(1.0, (h - MIN_LINE) / 24.0))
        if closed:
            self._draw_closed(big, rect, e, color, light)
        else:
            self._draw_open(big, rect, e, thin, light)
        body = pygame.transform.smoothscale(big, (cw, ch))
        if not closed:
            # the gradient runs over the open part of the eye, so crescents and lidded eyes get all of it
            top, bottom = eye_span(e)
            span = max(bottom - top, 0.35 * h)
            self._shade(body, ch / 2 + top, span, color, thin)
        if abs(e.tilt % 360.0) > 0.25 and abs(e.tilt % 360.0 - 360.0) > 0.25:
            body = pygame.transform.rotozoom(body, e.tilt, 1.0)
        return _fast(body)

    def _draw_open(self, big: pygame.Surface, rect: pygame.Rect, e: EyeFrame, thin: float,
                   light: bool = False) -> None:
        """Eye silhouette in white with a grey "ink" rim inside the outline and along the lids.

        The colour comes later (:meth:`_shade` multiplies it in at native size), so the rim
        ends up a darker shade of the eye colour. A nearly shut eye (blink) becomes a clean
        line: bright on a dark screen, deep ink on a light one.
        """
        ss = self.ss
        W, H = big.get_size()
        radius = int(min(e.radius * ss, rect.h / 2, rect.w / 2))
        rim = int(round(min(RIM * ss, rect.h * 0.18, rect.w * 0.18))) if thin > 0.0 else 0
        if light:
            g = RIM_GRAY_LIGHT
            inner = int(255 - (255 - g) * (1.0 - thin))
        else:
            g = int(255 - (255 - RIM_GRAY) * thin)
            inner = 255
        fill = (inner, inner, inner, 255)
        if rim >= 1:
            _rrect(big, (g, g, g, 255), rect, radius)
            _rrect(big, fill, rect.inflate(-2 * rim, -2 * rim), radius - rim)
        else:
            _rrect(big, fill, rect, radius)
        # upper lid: gently arched edge (higher in the middle), droops towards the outer corner for
        # lid_angle > 0. Lids and the happy crescent get the same rim along their cut edge.
        lid: list[tuple[float, float]] | None = None
        if e.lid_top > 0.003:
            y0 = rect.top + e.lid_top * rect.h
            slope = math.tan(math.radians(e.lid_angle)) * e.side
            cx = rect.centerx
            half = rect.w / 2
            bow = LID_BOW * rect.h * min(1.0, e.lid_top * 3)
            lid = []
            n = 16
            for i in range(n + 1):
                x = W + 2.0 - (W + 4.0) * i / n
                u = max(-1.0, min(1.0, (x - cx) / half))
                lid.append((x, y0 + slope * (x - cx) - bow * (1 - u * u)))
        y1 = rect.bottom - e.lid_bottom * rect.h if e.lid_bottom > 0.003 else None
        smile_r = rect.w * SMILE_RADIUS
        smile_cy = rect.bottom + smile_r - e.smile * rect.h * SMILE_DEPTH if e.smile > 0.003 else None
        if rim >= 1 and (lid is not None or y1 is not None or smile_cy is not None):
            # paint the rim bands on a white scratch and darken the body with it (alpha untouched)
            tmp = self._tmp_surface(W, H)
            area = pygame.Rect(0, 0, W, H)
            tmp.fill((255, 255, 255), area)
            grey = (g, g, g)
            if lid is not None:
                pygame.draw.polygon(tmp, grey, [(-2.0, -2.0), (W + 2.0, -2.0)] + [(x, y + rim) for x, y in lid])
            if y1 is not None:
                pygame.draw.rect(tmp, grey, pygame.Rect(0, int(y1 - rim), W, H))
            if smile_cy is not None:
                pygame.draw.circle(tmp, grey, (rect.centerx, int(smile_cy)), int(smile_r + rim))
            big.blit(tmp, (0, 0), area, special_flags=pygame.BLEND_RGB_MIN)
        clear = (0, 0, 0, 0)
        if lid is not None:
            pygame.draw.polygon(big, clear, [(-2.0, -2.0), (W + 2.0, -2.0)] + lid)
        if y1 is not None:
            pygame.draw.polygon(big, clear, [(-2, y1), (W + 2, y1), (W + 2, H + 2), (-2, H + 2)])
        if smile_cy is not None:
            pygame.draw.circle(big, clear, (rect.centerx, int(smile_cy)), int(smile_r))

    def _shade(self, body: pygame.Surface, y0: float, h: float, color: Color, thin: float) -> None:
        """Multiply the eye colour in: deeper under the lid, glowing lighter towards the bottom.

        Done after the down-scale (same result, ~9x fewer pixels) by stretching a cached
        gradient column (keyed by colour only) over the eye: smooth, and a few blits per miss.
        """
        W, H = body.get_size()
        tq = _q(thin, 0.05)
        col = self._sprites.get_or(("shade", color, tq), lambda: self._shade_column(color, tq * 0.05))
        ya, yb = max(0, int(round(y0))), min(H, int(round(y0 + h)))
        mult = pygame.BLEND_RGB_MULT
        if ya > 0:
            body.fill(col.get_at((0, 0)), pygame.Rect(0, 0, W, ya), special_flags=mult)
        if yb > ya:
            body.blit(pygame.transform.scale(col, (W, yb - ya)), (0, ya), special_flags=mult)
        if yb < H:
            body.fill(col.get_at((0, SHADE_ROWS - 1)), pygame.Rect(0, yb, W, H - yb), special_flags=mult)

    @staticmethod
    def _shade_column(color: Color, thin: float) -> pygame.Surface:
        line = _lighten(color, 0.25)
        top = _mix(line, _scale(color, 0.8), thin)
        bottom = _mix(line, _lighten(color, 0.32), thin)
        col = pygame.Surface((1, SHADE_ROWS))
        for y in range(SHADE_ROWS):
            k = (y + 0.5) / SHADE_ROWS
            col.set_at((0, y), _mix(top, bottom, k * k * (3 - 2 * k)))
        return col

    def _tmp_surface(self, w: int, h: int) -> pygame.Surface:
        """Reusable opaque scratch surface at least ``w`` x ``h`` (grows, never shrinks)."""
        s = self._tmp
        if s is None or s.get_width() < w or s.get_height() < h:
            ow, oh = s.get_size() if s is not None else (0, 0)
            s = self._tmp = pygame.Surface((max(w, ow), max(h, oh)))
        return s

    def _draw_closed(self, big: pygame.Surface, rect: pygame.Rect, e: EyeFrame, color: Color,
                     light: bool = False) -> None:
        ss = self.ss
        th = max(2.0, _closed_thickness(e) * ss)
        sag = e.closed_curve * CLOSED_SAG * ss
        col = (*(_ink(color) if light else _lighten(color, 0.1)), 255)
        half = rect.w / 2 - th / 2
        cy = rect.centery - sag / 2
        n = max(8, int(rect.w / (th / 3)))
        for i in range(n + 1):
            x = -half + 2 * half * i / n
            y = cy + sag * (1 - (x / max(1.0, half)) ** 2)
            pygame.draw.circle(big, col, (rect.centerx + x, y), th / 2)

    def _pupil_sprite(self, rx: float, ry: float, tilt: float, color: Color) -> pygame.Surface:
        qx, qy = _q(rx, 0.5), _q(ry, 0.5)
        ang = 0 if abs(rx - ry) < 1.0 else (_q(tilt % 180.0, 5) * 5) % 180
        pc = _pupil_color(color)
        return self._sprites.get_or(("pupil", qx, qy, ang, pc), lambda: self._make_pupil(qx * 0.5, qy * 0.5, ang, pc))

    def _make_pupil(self, rx: float, ry: float, angle: float, pc: Color) -> pygame.Surface:
        ss = self.ss
        w, h = int(math.ceil(2 * rx)) + 2, int(math.ceil(2 * ry)) + 2
        big = pygame.Surface((w * ss, h * ss), pygame.SRCALPHA)
        r = pygame.Rect(0, 0, int(round(2 * rx * ss)), int(round(2 * ry * ss)))
        r.center = (w * ss // 2, h * ss // 2)
        pygame.draw.ellipse(big, (*pc, 255), r)
        img = pygame.transform.smoothscale(big, (w, h))
        if angle:
            img = pygame.transform.rotozoom(img, angle, 1.0)
        return _fast(img)

    def _catchlight_sprite(self, r: float) -> pygame.Surface:
        qr = _q(r, 0.25)
        return self._sprites.get_or(("catchlight", qr), lambda: self._make_dot(qr * 0.25, (255, 255, 255)))

    def _make_dot(self, r: float, col: Color) -> pygame.Surface:
        ss = self.ss
        d = int(math.ceil(2 * r)) + 2
        big = pygame.Surface((d * ss, d * ss), pygame.SRCALPHA)
        pygame.draw.circle(big, (*col, 255), (d * ss / 2, d * ss / 2), r * ss)
        return _fast(pygame.transform.smoothscale(big, (d, d)))

    def _make_shadow(self, core: pygame.Surface) -> pygame.Surface:
        """The eye's silhouette in a deep rose, blurred like the glow (light screens)."""
        sil = core.copy()
        sil.fill((255, 255, 255, 0), special_flags=pygame.BLEND_RGBA_MAX)  # white, alpha kept
        sil.fill((196, 92, 136, 255), special_flags=pygame.BLEND_RGBA_MULT)
        return self._make_glow(sil)

    def _make_glow(self, core: pygame.Surface) -> pygame.Surface:
        cw, ch = core.get_size()
        m = GLOW_MARGIN
        gw, gh = cw + 2 * m, ch + 2 * m
        d = GLOW_DOWNSCALE
        small = pygame.Surface((max(1, gw // d), max(1, gh // d)), pygame.SRCALPHA)
        small.blit(pygame.transform.smoothscale(core, (max(1, cw // d), max(1, ch // d))), (m // d, m // d))
        small = pygame.transform.gaussian_blur(small, GLOW_BLUR)
        return _fast(pygame.transform.smoothscale(small, (gw, gh)))

    # ------------------------------------------------------------------ mouth
    def _draw_mouth(self, surf: pygame.Surface, m: MouthFrame, color: Color, light: bool = False) -> None:
        if m.alpha <= 0.01:
            return
        # quantise the shape: the cached image for a key is always drawn from the same values
        q = dataclasses.replace(
            m, cx=0.0, cy=0.0, alpha=1.0, shape="",
            open=_q(m.open, 0.025) * 0.025, width=float(_q(m.width, 1.0)), smile=_q(m.smile, 0.05) * 0.05,
            round=_q(m.round, 0.05) * 0.05, teeth=_q(m.teeth, 0.1) * 0.1, tongue=_q(m.tongue, 0.05) * 0.05,
            angle=float(_q(m.angle, 1.0)),
        )
        key = (q.open, q.width, q.smile, q.round, q.teeth, q.tongue, q.angle, light, None if light else color)
        img = self._mouths.get_or(key, lambda: self._make_mouth(q, color, light))
        img.set_alpha(int(255 * min(1.0, m.alpha)))
        surf.blit(img, (int(round(m.cx - img.get_width() / 2)), int(round(m.cy - img.get_height() / 2))))

    def _make_mouth(self, m: MouthFrame, color: Color, light: bool) -> pygame.Surface:
        """Lips as an ink line along both edges; when open, a dark inside with teeth / tongue."""
        ss = self.ss
        if light:
            ink, inside, tongue_c, teeth_c = MOUTH_INK, MOUTH_INSIDE, TONGUE, TEETH
        else:
            ink, inside, tongue_c, teeth_c = _lighten(color, 0.15), _scale(color, 0.22), (255, 128, 160), (240, 248, 255)
        top, bot = mouth_edges(m, 56)
        lw = MOUTH_LINE
        out_h = 15.0 * m.tongue  # cheeky tongue below the lower lip
        ext_x = max(abs(x) for x, _ in top) + lw
        ext_y = max(max(abs(y) for _, y in top), max(abs(y) for _, y in bot) + out_h) + lw
        W, H = int(math.ceil(2 * ext_x + 6)), int(math.ceil(2 * ext_y + 6))
        big = pygame.Surface((W * ss, H * ss), pygame.SRCALPHA)
        ox, oy = W * ss / 2, H * ss / 2

        def pt(p: tuple[float, float]) -> tuple[float, float]:
            return ox + p[0] * ss, oy + p[1] * ss

        r = lw * ss / 2
        if m.tongue > 0.02:
            # poking out under the lip, a little off-centre; the lip line is drawn over its root
            hw = m.width / 2
            tx, ty = hw * 0.22, bot[len(bot) // 2 + 3][1]
            tw, th = max(6.0, hw * 0.62), out_h + 6.0
            rect = pygame.Rect(0, 0, int(tw * ss), int(th * ss))
            rect.midtop = (int(ox + tx * ss), int(oy + (ty - 5.0) * ss))
            pygame.draw.ellipse(big, (*ink, 255), rect.inflate(int(lw * 0.9 * ss), int(lw * 0.9 * ss)))
            pygame.draw.ellipse(big, (*tongue_c, 255), rect)
            pygame.draw.line(big, (*_scale(tongue_c, 0.8), 255), (rect.centerx, rect.top + rect.h * 0.35),
                             (rect.centerx, rect.bottom - rect.h * 0.25), max(1, int(ss * 1.2)))
        gap = max(b[1] - t[1] for t, b in zip(top, bot))
        if gap * ss >= 1.5:
            pygame.draw.polygon(big, (*inside, 255), [pt(p) for p in top] + [pt(p) for p in reversed(bot)])
            # teeth and tongue are bounded by the lip curves themselves, so they never spill outside
            n = len(top) - 1
            if m.teeth > 0.05:  # a row of upper teeth just under the top lip
                t_h = min(gap * 0.55, 8.0) * m.teeth + lw / 2
                edge = [(x, min(y + t_h * (1.0 - (2.0 * i / n - 1.0) ** 2) ** 0.35, b[1]))
                        for i, ((x, y), b) in enumerate(zip(top, bot))]
                pygame.draw.polygon(big, (*teeth_c, 255), [pt(p) for p in top] + [pt(p) for p in reversed(edge)])
            elif m.open > 0.38 and m.round < 0.5:  # a little tongue resting on the bottom lip
                k = min(1.0, (m.open - 0.38) / 0.4)
                th = gap * (0.3 + 0.2 * k)
                hw = max(abs(x) for x, _ in top)
                span = 0.6 * hw
                low = [(x, y) for (x, y) in bot if abs(x) < span]
                if len(low) >= 3:
                    floor = max(y for _, y in low)  # a round-topped tongue, cut off by the bottom lip
                    upper = [(x, min(y, max(t[1], floor - th * math.sqrt(max(0.0, 1.0 - (x / span) ** 2)))))
                             for (x, y), t in zip(bot, top) if abs(x) < span]
                    pygame.draw.polygon(big, (*tongue_c, 255), [pt(p) for p in upper] + [pt(p) for p in reversed(low)])
            curves = (top, bot)
        else:
            curves = (top,)
        for curve in curves:  # the lip line: overlapping round dots give an even, round-capped stroke
            for p in curve:
                pygame.draw.circle(big, (*ink, 255), pt(p), r)
        img = pygame.transform.smoothscale(big, (W, H))
        if abs(m.angle) > 0.5:
            img = pygame.transform.rotozoom(img, m.angle, 1.0)
        return _fast(img)

    # ------------------------------------------------------------------ sprites
    def _draw_sprite(self, surf: pygame.Surface, s: Sprite, light: bool = False) -> None:
        if s.alpha <= 0.01 or s.size < 1.0:
            return
        size_q = max(2, int(round(s.size)))
        ang_step = 10 if s.kind == "confetti" else 5
        ang = (_q(s.angle, ang_step) * ang_step) % 360 if s.kind not in ("dot", "blush", "play") else 0
        light = light and s.kind == "sparkle"  # only sparkles are drawn differently on a light screen
        key = (s.kind, size_q, s.color, ang, light)
        img = self._sprites.get_or(key, lambda: self._make_sprite(s.kind, size_q, s.color, ang, light))
        if img is None:
            return
        img.set_alpha(int(255 * min(1.0, s.alpha)))
        surf.blit(img, (int(s.x - img.get_width() / 2), int(s.y - img.get_height() / 2)))

    def _make_sprite(self, kind: str, size: int, color: Color, angle: float,
                     light: bool = False) -> pygame.Surface | None:
        ss = self.ss
        base: pygame.Surface | None
        if kind == "heart":
            base = self._heart(size, color)
        elif kind == "sparkle":
            base = self._sparkle_inked(size, color) if light else self._sparkle(size, color)
        elif kind == "z":
            base = self._z(size, color)
        elif kind == "question":
            base = self._question(size, color)
        elif kind == "dot":
            big = pygame.Surface((size * ss + 2, size * ss + 2), pygame.SRCALPHA)
            pygame.draw.circle(big, (*color, 255), (big.get_width() / 2, big.get_height() / 2), size * ss / 2)
            base = pygame.transform.smoothscale(big, (size + 1, size + 1))
        elif kind == "confetti":
            w, h = size, max(3, int(size * 0.5))
            base = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.rect(base, (*color, 255), base.get_rect(), border_radius=1)
        elif kind == "blush":
            base = self._blush(size, color)
        elif kind == "play":
            base = self._play(size, color)
        else:
            return None
        if angle:
            base = pygame.transform.rotozoom(base, angle, 1.0)
        return _fast(base)

    def _heart(self, size: int, color: Color) -> pygame.Surface:
        ss = self.ss
        S = size * ss
        big = pygame.Surface((S + 4 * ss, S + 4 * ss), pygame.SRCALPHA)
        o = 2 * ss
        r = S * 0.27
        col = (*color, 255)
        pygame.draw.circle(big, col, (o + S * 0.28, o + S * 0.32), r)
        pygame.draw.circle(big, col, (o + S * 0.72, o + S * 0.32), r)
        tip = [(o + S * 0.03, o + S * 0.40), (o + S * 0.97, o + S * 0.40), (o + S * 0.5, o + S * 0.93)]
        pygame.draw.polygon(big, col, tip)
        pygame.draw.circle(big, (255, 255, 255, 150), (o + S * 0.30, o + S * 0.28), S * 0.08)
        return pygame.transform.smoothscale(big, (size + 4, size + 4))

    def _sparkle_inked(self, size: int, color: Color) -> pygame.Surface:
        """A four-point star with a crisp darker outline (a soft halo looks muddy on a light screen)."""
        ss = self.ss
        S = size * ss
        big = pygame.Surface((S + 2, S + 2), pygame.SRCALPHA)
        c = S / 2

        def star(k: float) -> list[tuple[float, float]]:
            pts = []
            for i in range(8):
                r = (c if i % 2 == 0 else c * 0.3) * k
                a = math.pi / 4 * i - math.pi / 2
                pts.append((c + r * math.cos(a), c + r * math.sin(a)))
            return pts

        pygame.draw.polygon(big, (*_scale(color, 0.72), 255), star(1.0))
        pygame.draw.polygon(big, (*color, 255), star(0.74))
        pygame.draw.circle(big, (255, 255, 255, 255), (c, c), c * 0.16)
        return pygame.transform.smoothscale(big, (size + 1, size + 1))

    def _sparkle(self, size: int, color: Color) -> pygame.Surface:
        ss = self.ss
        S = size * ss
        big = pygame.Surface((S + 2, S + 2), pygame.SRCALPHA)
        c = S / 2
        pts = []
        for i in range(8):
            r = c if i % 2 == 0 else c * 0.26
            a = math.pi / 4 * i - math.pi / 2
            pts.append((c + r * math.cos(a), c + r * math.sin(a)))
        pygame.draw.polygon(big, (*color, 255), pts)
        pygame.draw.circle(big, (255, 255, 255, 255), (c, c), c * 0.2)
        img = pygame.transform.smoothscale(big, (size + 1, size + 1))
        # soft halo
        halo_s = max(2, (size + 1) // 3)
        halo = pygame.transform.smoothscale(img, (halo_s, halo_s))
        halo = pygame.transform.gaussian_blur(halo, 1) if halo_s > 3 else halo
        out = pygame.Surface((size + 1, size + 1), pygame.SRCALPHA)
        h2 = pygame.transform.smoothscale(halo, (size + 1, size + 1))
        h2.set_alpha(140)
        out.blit(h2, (0, 0))
        out.blit(img, (0, 0))
        return out

    def _z(self, size: int, color: Color) -> pygame.Surface:
        ss = self.ss
        S = size * ss
        th = max(2.0, S * 0.16)
        big = pygame.Surface((int(S * 0.9 + th * 2), int(S + th * 2)), pygame.SRCALPHA)
        o = th
        w = S * 0.8
        pts = [(o, o), (o + w, o), (o, o + S * 0.95), (o + w, o + S * 0.95)]
        col = (*color, 255)
        for a, b in zip(pts, pts[1:]):
            n = max(4, int(math.dist(a, b) / (th / 3)))
            for i in range(n + 1):
                k = i / n
                pygame.draw.circle(big, col, (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k), th / 2)
        return pygame.transform.smoothscale(big, (max(2, big.get_width() // ss), max(2, big.get_height() // ss)))

    def _question(self, size: int, color: Color) -> pygame.Surface:
        ss = 2
        font = self._get_font(int(size * ss * 1.25))
        if font is not None:
            try:
                txt = font.render("?", True, color)
                w, h = txt.get_size()
                return pygame.transform.smoothscale(txt, (max(2, w // ss), max(2, h // ss)))
            except Exception:  # noqa: BLE001
                log.debug("font render failed", exc_info=True)
        # fallback: hand-drawn question mark
        S = size * ss
        big = pygame.Surface((int(S * 0.7), S), pygame.SRCALPHA)
        th = max(2, int(S * 0.13))
        pygame.draw.arc(big, (*color, 255), pygame.Rect(th, th, int(S * 0.7) - 2 * th, int(S * 0.5)),
                        -math.pi / 2, math.pi, th)
        pygame.draw.line(big, (*color, 255), (S * 0.35, S * 0.5), (S * 0.35, S * 0.68), th)
        pygame.draw.circle(big, (*color, 255), (S * 0.35, S * 0.86), th * 0.8)
        return pygame.transform.smoothscale(big, (max(2, big.get_width() // ss), max(2, big.get_height() // ss)))

    def _get_font(self, px: int) -> Any:
        if self._font_failed:
            return None
        try:
            if not pygame.font.get_init():
                pygame.font.init()
            key = ("font", px)
            f = self._static.get(key)
            if f is None:
                f = pygame.font.Font(None, px)
                f.set_bold(True)
                self._static[key] = f  # type: ignore[assignment]
            return f
        except Exception:  # noqa: BLE001
            self._font_failed = True
            log.info("pygame font unavailable; using drawn glyphs")
            return None

    def _blush(self, size: int, color: Color) -> pygame.Surface:
        w, h = size + 16, int(size * 0.45) + 16
        s = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.ellipse(s, (*color, 255), pygame.Rect(8, 8, size, int(size * 0.45)))
        small = pygame.transform.smoothscale(s, (max(1, w // 2), max(1, h // 2)))
        small = pygame.transform.gaussian_blur(small, 2)
        return pygame.transform.smoothscale(small, (w, h))

    def _play(self, size: int, color: Color) -> pygame.Surface:
        ss = self.ss
        S = size * ss
        big = pygame.Surface((int(S * 1.4), S), pygame.SRCALPHA)
        col = (*_lighten(color, 0.2), 255)
        pygame.draw.rect(big, col, big.get_rect(), width=max(2, int(S * 0.08)), border_radius=int(S * 0.25))
        cx, cy = big.get_width() / 2, S / 2
        tri = [(cx - S * 0.16, cy - S * 0.22), (cx - S * 0.16, cy + S * 0.22), (cx + S * 0.24, cy)]
        pygame.draw.polygon(big, col, tri)
        return pygame.transform.smoothscale(big, (max(2, big.get_width() // ss), max(2, big.get_height() // ss)))
