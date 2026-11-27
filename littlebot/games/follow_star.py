"""Follow the Star: LittleBot's big eyes follow a twinkly, smiling star as it drifts around the
screen leaving a sparkle trail. When the camera sees the child, the star looks at them and every
few seconds LittleBot glances over to the child too, then the star swoops their way.
Tap the star to catch it (it bursts into sparkles and a new one flies in), or tap anywhere to
send it there. Visual tracking practice for babies; no reading."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pygame

from littlebot.games import art
from littlebot.games.base import Game

# px on a 480 px tall canvas; a game scales them to its own (x1.25 on the 1024x600 screen)
STAR_R = 46
CATCH_RADIUS = 86  # generous: babies swipe near it
SPEED = 150.0
TWINKLES = 22
CHILD_LOOK_EVERY = (6.0, 9.0)
CHILD_LOOK_SECONDS = 1.8
SKY_TOP: art.Color = (255, 226, 240)
SKY_BOTTOM: art.Color = (246, 206, 236)


@dataclass(eq=False)
class Star:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    tx: float = 0.0
    ty: float = 0.0
    born: float = 0.0
    gone: float = 0.0  # > 0: caught, respawning in this many seconds


class FollowTheStar(Game):
    id = "star"

    def __init__(self, host: Any = None, *, size: tuple[int, int] = (art.W, art.H), seed: int | None = None) -> None:
        super().__init__(host, size=size, seed=seed)
        k = self.k = self.h / 480  # px scale (1.25 on the 1024x600 screen)
        self.star_r = STAR_R * k
        self.catch_radius = CATCH_RADIUS * k
        self.eyes = art.EyeState(self.w / 2, self.h / 2 + 6 * k, 1.55 * k, self.eye_color)
        self.star = Star(self.w * 0.2, self.h * 0.25)
        self.pick_target(far=True)
        self.caught = 0
        self.trail_t = 0.0
        self.child_look_in = self.rng.uniform(*CHILD_LOOK_EVERY)
        self.child_look_left = 0.0
        self.child_looks = 0
        self.twinkles = [(self.rng.uniform(20 * k, self.w - 20 * k), self.rng.uniform(16 * k, self.h - 16 * k),
                          self.rng.uniform(0, 6.28), int(self.rng.choice((10, 12, 14)) * k)) for _ in range(TWINKLES)]

    def begin(self) -> None:
        super().begin()
        self.host.sound("boop", volume=0.6)

    # ------------------------------------------------------------ logic
    def pick_target(self, *, far: bool = False, x: float | None = None, y: float | None = None) -> None:
        st = self.star
        k = self.k
        if x is not None and y is not None:
            st.tx, st.ty = art.clamp(x, 50 * k, self.w - 50 * k), art.clamp(y, 50 * k, self.h - 50 * k)
            return
        for _ in range(12):
            tx, ty = self.rng.uniform(70 * k, self.w - 70 * k), self.rng.uniform(60 * k, self.h - 60 * k)
            if art.dist(tx, ty, st.x, st.y) > (260 if far else 180) * k:
                break
        st.tx, st.ty = tx, ty

    def child_point(self) -> tuple[float, float] | None:
        g = self.host.child_gaze()
        if g is None:
            return None
        return (art.clamp(g[0], 0, 1) * self.w, art.clamp(g[1], 0, 1) * self.h)

    def step(self, dt: float) -> None:
        st = self.star
        if st.gone > 0:
            st.gone -= dt
            if st.gone <= 0:
                self.respawn()
        else:
            dx, dy = st.tx - st.x, st.ty - st.y
            d = math.hypot(dx, dy)
            if d < 26 * self.k:
                self.pick_target()
            else:
                sp = (SPEED * min(1.0, d / (110 * self.k)) + 30) * self.k
                k = min(1.0, 2.4 * dt)
                st.vx += (dx / d * sp - st.vx) * k
                st.vy += (dy / d * sp - st.vy) * k
            st.x += st.vx * dt
            st.y += st.vy * dt
            self.trail_t -= dt
            if self.trail_t <= 0:
                self.trail_t = 0.045
                self.particles.burst(st.x - st.vx * 0.08, st.y - st.vy * 0.08, 1,
                                     [art.LEMON, art.WHITE, art.BUBBLEGUM][self.rng.randrange(3):][:1],
                                     speed=(5 * self.k, 40 * self.k), life=(0.6, 1.0), size=(5 * self.k, 9 * self.k),
                                     kinds=("spark", "star"))
        child = self.child_point()
        if self.child_look_left > 0:
            self.child_look_left -= dt
            if child is not None:
                self.eyes.look_at_point(child[0], child[1], 300 * self.k)
        else:
            if st.gone <= 0:
                self.eyes.look_at_point(st.x, st.y, 300 * self.k)
            self.child_look_in -= dt
            if self.child_look_in <= 0:
                self.child_look_in = self.rng.uniform(*CHILD_LOOK_EVERY)
                if child is not None and not self.in_finale:
                    self.child_look_left = CHILD_LOOK_SECONDS
                    self.child_looks += 1
                    self.eyes.feel("happy", 1.0)
                    self.pick_target(x=child[0], y=child[1])
        self.eyes.update(dt, self.rng)

    def respawn(self) -> None:
        st = self.star
        side = self.rng.randrange(4)
        k = self.k
        across, down = self.rng.uniform(60 * k, self.w - 60 * k), self.rng.uniform(60 * k, self.h - 60 * k)
        off = 40.0 * k
        st.x, st.y = [(-off, down), (self.w + off, down), (across, -off), (across, self.h + off)][side]
        st.vx = st.vy = 0.0
        st.gone = 0.0
        st.born = self.t
        self.pick_target(far=True)
        self.host.sound("whoosh", volume=0.5)

    def catch(self) -> None:
        st = self.star
        self.caught += 1
        self.score += 1
        st.gone = 0.9
        k = self.k
        self.burst(st.x, st.y, [art.LEMON, art.WHITE, art.TANGERINE, art.BUBBLEGUM], 26, speed=(160 * k, 380 * k),
                   life=(0.6, 1.2), size=(6 * k, 12 * k), kinds=("star", "spark", "dot"), gravity=200 * k)
        self.eyes.feel("happy", 1.4)
        self.host.sound("sparkle")

    def on_tap(self, x: float, y: float) -> bool:
        st = self.star
        if st.gone <= 0 and art.dist(x, y, st.x, st.y) <= self.catch_radius:
            self.catch()
        elif st.gone <= 0:
            self.pick_target(x=x, y=y)
            k = self.k
            self.burst(x, y, [art.WHITE, art.LEMON], 5, speed=(40 * k, 110 * k), life=(0.4, 0.7), size=(6 * k, 9 * k),
                       kinds=("spark",))
            self.host.sound("boop", volume=0.5)
        return True

    def on_key(self, name: str) -> bool:
        if name in ("space", "return") and self.star.gone <= 0:
            self.catch()
            return True
        return False

    # ------------------------------------------------------------ drawing
    def render(self, surf: pygame.Surface) -> None:
        surf.blit(art.background(self.size, top=SKY_TOP, bottom=SKY_BOTTOM, seed=21), (0, 0))
        for x, y, ph, sz in self.twinkles:
            a = 0.5 + 0.5 * math.sin(self.t * 2.2 + ph)
            art.blit_center(surf, art.sparkle(sz, (255, 255, 255)), (x, y), int(60 + 150 * a))
        self.eyes.draw(surf)
        st = self.star
        if st.gone > 0:
            return
        pulse = 1.0 + 0.08 * math.sin(self.t * 5)
        grow = art.ease_out_back(min(1.0, (self.t - st.born) / 0.5)) if self.t - st.born < 0.5 else 1.0
        r = self.star_r
        art.blit_center(surf, art.glow(r * 2.3 * pulse * grow, art.LEMON, 1.0), (st.x, st.y))
        spr = art.star(r * grow, art.LEMON, rot_deg=math.sin(self.t * 1.7) * 14)
        art.blit_center(surf, spr, (st.x, st.y))
        child = self.child_point()
        reach = 200 * self.k
        if child is not None:
            look = ((child[0] - st.x) / reach, (child[1] - st.y) / reach)
        else:
            look = ((self.eyes.x - st.x) / reach, (self.eyes.y - st.y) / reach)
        art.cute_face(surf, st.x, st.y + 5 * self.k * grow, r * 1.7 * grow, look=look)

    def summary(self) -> str:
        return f"{self.caught} star{'s' if self.caught != 1 else ''} caught"

    def finale_text(self) -> str:
        return "Twinkle twinkle!"

    def status_extra(self) -> dict[str, Any]:
        return {"caught": self.caught, "child_looks": self.child_looks, "sees_child": self.child_point() is not None,
                "star": {"x": round(self.star.x), "y": round(self.star.y)}}
