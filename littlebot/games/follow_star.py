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

STAR_R = 46
CATCH_RADIUS = 86  # generous: babies swipe near it
SPEED = 150.0
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
        self.eyes = art.EyeState(self.w / 2, self.h / 2 + 6, 1.55, self.eye_color)
        self.star = Star(self.w * 0.2, self.h * 0.25)
        self.pick_target(far=True)
        self.caught = 0
        self.trail_t = 0.0
        self.child_look_in = self.rng.uniform(*CHILD_LOOK_EVERY)
        self.child_look_left = 0.0
        self.child_looks = 0
        self.twinkles = [(self.rng.uniform(20, self.w - 20), self.rng.uniform(16, self.h - 16),
                          self.rng.uniform(0, 6.28), self.rng.choice((10, 12, 14))) for _ in range(16)]

    def begin(self) -> None:
        super().begin()
        self.host.sound("boop", volume=0.6)

    # ------------------------------------------------------------ logic
    def pick_target(self, *, far: bool = False, x: float | None = None, y: float | None = None) -> None:
        st = self.star
        if x is not None and y is not None:
            st.tx, st.ty = art.clamp(x, 50, self.w - 50), art.clamp(y, 50, self.h - 50)
            return
        for _ in range(12):
            tx, ty = self.rng.uniform(70, self.w - 70), self.rng.uniform(60, self.h - 60)
            if art.dist(tx, ty, st.x, st.y) > (260 if far else 180):
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
            if d < 26:
                self.pick_target()
            else:
                sp = SPEED * min(1.0, d / 110) + 30
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
                                     speed=(5, 40), life=(0.6, 1.0), size=(5, 9), kinds=("spark", "star"))
        child = self.child_point()
        if self.child_look_left > 0:
            self.child_look_left -= dt
            if child is not None:
                self.eyes.look_at_point(child[0], child[1], 300)
        else:
            if st.gone <= 0:
                self.eyes.look_at_point(st.x, st.y, 300)
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
        across, down = self.rng.uniform(60, self.w - 60), self.rng.uniform(60, self.h - 60)
        st.x, st.y = [(-40.0, down), (self.w + 40.0, down), (across, -40.0), (across, self.h + 40.0)][side]
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
        self.burst(st.x, st.y, [art.LEMON, art.WHITE, art.TANGERINE, art.BUBBLEGUM], 26, speed=(160, 380),
                   life=(0.6, 1.2), size=(6, 12), kinds=("star", "spark", "dot"), gravity=200)
        self.eyes.feel("happy", 1.4)
        self.host.sound("sparkle")

    def on_tap(self, x: float, y: float) -> bool:
        st = self.star
        if st.gone <= 0 and art.dist(x, y, st.x, st.y) <= CATCH_RADIUS:
            self.catch()
        elif st.gone <= 0:
            self.pick_target(x=x, y=y)
            self.burst(x, y, [art.WHITE, art.LEMON], 5, speed=(40, 110), life=(0.4, 0.7), size=(6, 9), kinds=("spark",))
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
        art.blit_center(surf, art.glow(STAR_R * 2.3 * pulse * grow, art.LEMON, 1.0), (st.x, st.y))
        spr = art.star(STAR_R * grow, art.LEMON, rot_deg=math.sin(self.t * 1.7) * 14)
        art.blit_center(surf, spr, (st.x, st.y))
        child = self.child_point()
        if child is not None:
            look = ((child[0] - st.x) / 200, (child[1] - st.y) / 200)
        else:
            look = ((self.eyes.x - st.x) / 200, (self.eyes.y - st.y) / 200)
        art.cute_face(surf, st.x, st.y + 5 * grow, STAR_R * 1.7 * grow, look=look)

    def summary(self) -> str:
        return f"{self.caught} star{'s' if self.caught != 1 else ''} caught"

    def finale_text(self) -> str:
        return "Twinkle twinkle!"

    def status_extra(self) -> dict[str, Any]:
        return {"caught": self.caught, "child_looks": self.child_looks, "sees_child": self.child_point() is not None,
                "star": {"x": round(self.star.x), "y": round(self.star.y)}}
