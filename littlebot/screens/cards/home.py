"""The home card: a few smart-home devices and their state (littlebot/skills/home.py, Home Assistant).

Six tiles at a time (more are paged: swipe, or "next"), each with a drawn icon (a bulb that glows when it is on, a
plug, a fan that spins, a thermometer, blinds, a padlock, a TV, a scene's star, a script's play button), the
device's name and its state. A tap on a light, a plug or a fan switches it: the tile flips at once and the skill
asks Home Assistant on the robot's loop; if that fails the tile flips back and the voice is told.

Built by the skill's ``home_devices`` (the devices it found, in the spec) or from ``show_on_screen`` with a filter
("lights", "kitchen"): then the card asks the skill for them. Without the skill (not set up) there is no card.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Callable

import pygame

from littlebot.games import art
from littlebot.screens.base import BODY_PX, SMALL_PX, Card, CardHost, CardType, W, register
from littlebot.screens.cards import _kit, candy

log = logging.getLogger("littlebot.screens.home")

COLS, ROWS = 3, 2
GAP = 12
PAD = 16
SWIPE_PX = 60
TOGGLES = frozenset({"light", "switch", "fan"})
ON_STATES = frozenset({"on", "open", "opening", "playing", "heat", "cool", "heat_cool", "auto", "dry", "fan_only",
                       "unlocked"})
LIT: art.Color = art.LEMON


def _on(d: dict[str, Any]) -> bool:
    return str(d.get("state") or "").lower() in ON_STATES


def state_label(d: dict[str, Any]) -> str:
    """ "On 80%", "Off", "21°", "Open", "Locked", "Ready" for a scene."""
    kind, state = d.get("kind"), str(d.get("state") or "unknown").lower()
    if state in ("unavailable", "unknown"):
        return "Offline" if state == "unavailable" else "?"
    if kind == "climate":
        target = d.get("target_temperature")
        now = d.get("temperature_now")
        if isinstance(target, (int, float)):
            return f"{target:g}°" if state != "off" else "Off"
        return f"{now:g}°" if isinstance(now, (int, float)) else state.replace("_", " ").capitalize()
    if kind in ("scene", "script"):
        return "Running" if state == "on" and kind == "script" else "Ready"
    if kind == "light" and state == "on" and isinstance(d.get("brightness_percent"), int):
        return f"On {d['brightness_percent']}%"
    if kind == "cover" and isinstance(d.get("position_percent"), int) and 0 < d["position_percent"] < 100:
        return f"{d['position_percent']}% open"
    return state.replace("_", " ").capitalize()


# ---------------------------------------------------------------- icons
def _icon(kind: str, on: bool, size: int) -> pygame.Surface:
    def make() -> pygame.Surface:
        def draw(s: pygame.Surface, k: float) -> None:
            n = size * k
            ink = art.INK
            soft = art.mix(art.INK_SOFT, art.WHITE, 0.35)
            if kind == "light":
                fill = LIT if on else (232, 228, 236)
                if on:
                    glow = art.glow(n * 0.42, LIT, 0.9)
                    s.blit(glow, glow.get_rect(center=(n * 0.5, n * 0.4)))
                pygame.draw.circle(s, art.inkier(fill, 0.35), (n * 0.5, n * 0.4), n * 0.27)
                pygame.draw.circle(s, fill, (n * 0.5, n * 0.4), n * 0.23)
                pygame.draw.rect(s, soft, pygame.Rect(n * 0.38, n * 0.62, n * 0.24, n * 0.2),
                                 border_radius=int(n * 0.05))
                for y in (0.68, 0.75):
                    art.capsule(s, ink, (n * 0.38, n * y), (n * 0.62, n * y), n * 0.03)
                pygame.draw.ellipse(s, art.WHITE, pygame.Rect(n * 0.38, n * 0.26, n * 0.09, n * 0.13))
            elif kind == "switch":
                body = art.MINT if on else (226, 226, 234)
                r = pygame.Rect(n * 0.22, n * 0.2, n * 0.56, n * 0.6)
                pygame.draw.rect(s, art.inkier(body, 0.35), r.inflate(n * 0.05, n * 0.05), border_radius=int(n * 0.14))
                pygame.draw.rect(s, body, r, border_radius=int(n * 0.12))
                for x in (0.4, 0.6):
                    art.capsule(s, ink, (n * x, n * 0.4), (n * x, n * 0.52), n * 0.06)
                pygame.draw.circle(s, ink, (n * 0.5, n * 0.68), n * 0.035)
            elif kind == "fan":
                hub = art.SKY if on else (210, 210, 222)
                for i in range(3):
                    a = i * 2 * math.pi / 3
                    cx, cy = n * 0.5 + math.cos(a) * n * 0.2, n * 0.5 + math.sin(a) * n * 0.2
                    pygame.draw.ellipse(s, art.inkier(hub, 0.3),
                                        pygame.Rect(cx - n * 0.14, cy - n * 0.14, n * 0.28, n * 0.28))
                    pygame.draw.ellipse(s, art.lighten(hub, 0.3),
                                        pygame.Rect(cx - n * 0.11, cy - n * 0.11, n * 0.22, n * 0.22))
                pygame.draw.circle(s, ink, (n * 0.5, n * 0.5), n * 0.08)
            elif kind == "climate":
                red = art.STRAWBERRY if on else (220, 200, 210)
                art.capsule(s, art.inkier(art.WHITE, 0.3), (n * 0.5, n * 0.18), (n * 0.5, n * 0.62), n * 0.2)
                art.capsule(s, art.WHITE, (n * 0.5, n * 0.18), (n * 0.5, n * 0.62), n * 0.14)
                art.capsule(s, red, (n * 0.5, n * 0.4), (n * 0.5, n * 0.66), n * 0.07)
                pygame.draw.circle(s, art.inkier(red, 0.3), (n * 0.5, n * 0.72), n * 0.15)
                pygame.draw.circle(s, red, (n * 0.5, n * 0.72), n * 0.12)
            elif kind == "cover":
                frame = art.PERIWINKLE if on else (200, 200, 220)
                pygame.draw.rect(s, art.inkier(frame, 0.3), pygame.Rect(n * 0.16, n * 0.14, n * 0.68, n * 0.7),
                                 border_radius=int(n * 0.06))
                pygame.draw.rect(s, (236, 244, 255), pygame.Rect(n * 0.21, n * 0.19, n * 0.58, n * 0.6))
                slats = 2 if on else 5
                for i in range(slats):
                    y = n * (0.24 + i * 0.1)
                    art.capsule(s, frame, (n * 0.22, y), (n * 0.78, y), n * 0.07)
            elif kind == "lock":
                body = art.TANGERINE if on else art.MINT  # unlocked ("on") is the one to notice
                shackle = [(n * 0.32, n * 0.46), (n * 0.32, n * 0.3), (n * 0.5, n * 0.16), (n * 0.68, n * 0.3),
                           (n * 0.68, n * (0.36 if on else 0.46))]
                for a, b in zip(shackle, shackle[1:]):
                    art.capsule(s, ink, a, b, n * 0.08)
                r = pygame.Rect(n * 0.22, n * 0.44, n * 0.56, n * 0.42)
                pygame.draw.rect(s, art.inkier(body, 0.35), r.inflate(n * 0.04, n * 0.04), border_radius=int(n * 0.1))
                pygame.draw.rect(s, body, r, border_radius=int(n * 0.08))
                pygame.draw.circle(s, ink, (n * 0.5, n * 0.6), n * 0.05)
            elif kind == "media_player":
                screen = art.GRAPE if on else (90, 80, 110)
                r = pygame.Rect(n * 0.14, n * 0.2, n * 0.72, n * 0.48)
                pygame.draw.rect(s, ink, r.inflate(n * 0.06, n * 0.06), border_radius=int(n * 0.08))
                pygame.draw.rect(s, screen, r, border_radius=int(n * 0.05))
                art.capsule(s, ink, (n * 0.36, n * 0.82), (n * 0.64, n * 0.82), n * 0.06)
                art.capsule(s, ink, (n * 0.5, n * 0.7), (n * 0.5, n * 0.8), n * 0.05)
            elif kind == "scene":
                pts = art.star_points(n * 0.5, n * 0.52, n * 0.38, n * 0.17)
                pygame.draw.polygon(s, art.inkier(art.BUBBLEGUM, 0.3), pts)
                pygame.draw.polygon(s, art.BUBBLEGUM, art.star_points(n * 0.5, n * 0.52, n * 0.32, n * 0.14))
            else:  # script
                pygame.draw.circle(s, art.inkier(art.SKY, 0.3), (n * 0.5, n * 0.5), n * 0.36)
                pygame.draw.circle(s, art.SKY, (n * 0.5, n * 0.5), n * 0.31)
                pygame.draw.polygon(s, art.WHITE, [(n * 0.42, n * 0.34), (n * 0.66, n * 0.5), (n * 0.42, n * 0.66)])

        return art.supersampled((size, size), draw)

    return art.cached(("home_icon", kind, on, size), make)


# ---------------------------------------------------------------- the card
class HomeCard(Card):
    kind = "home"

    def __init__(self, spec: dict[str, Any], host: CardHost | None = None, *, devices: list[dict[str, Any]],
                 toggle: Callable[[str], Any] | None = None, loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__(spec, host)
        self.title = _kit.clean(spec.get("title"), 40) or "Smart home"
        self.devices = [dict(d) for d in devices if isinstance(d, dict) and d.get("name")][:24]
        self._toggle = toggle
        self._loop = loop
        self._waiting: dict[str, tuple[Any, str]] = {}  # entity id -> (future, the state before the tap)
        self.page = 0
        self._swipe = 0.0
        self.mood = "happy" if any(_on(d) for d in self.devices) else "neutral"

    @property
    def per_page(self) -> int:
        return COLS * ROWS

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.devices) / self.per_page))

    @property
    def animating(self) -> bool:
        return any(d.get("kind") == "fan" and _on(d) for d in self.shown())

    def shown(self) -> list[dict[str, Any]]:
        start = self.page * self.per_page
        return self.devices[start:start + self.per_page]

    def tile_rect(self, n: int) -> pygame.Rect:
        box = candy.PANEL
        tw = (box.w - 2 * PAD - (COLS - 1) * GAP) // COLS
        th = (box.h - 2 * PAD - (ROWS - 1) * GAP) // ROWS
        col, row = n % COLS, n // COLS
        return pygame.Rect(box.x + PAD + col * (tw + GAP), box.y + PAD + row * (th + GAP), tw, th)

    # ------------------------------------------------------------ steering
    def turn(self, step: int) -> bool:
        new = max(0, min(self.pages - 1, self.page + step))
        if new == self.page:
            return False
        self.page = new
        return True

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "next":
            return self.turn(1)
        if action == "prev":
            return self.turn(-1)
        return False

    def tap(self, x: float, y: float) -> bool:
        for n, d in enumerate(self.shown()):
            if self.tile_rect(n).collidepoint(x, y):
                return self.switch(d)
        return self.turn(1) or self.turn(-self.page)

    def key(self, name: str) -> bool:
        if name in ("right", "down"):
            return self.turn(1)
        if name in ("left", "up"):
            return self.turn(-1)
        return False

    def drag(self, dx: float, dy: float) -> None:
        self._swipe += dx

    def drag_end(self) -> None:
        swipe, self._swipe = self._swipe, 0.0
        if abs(swipe) >= SWIPE_PX:
            self.turn(1 if swipe < 0 else -1)

    def switch(self, d: dict[str, Any]) -> bool:
        """A tap on a light / plug / fan: flip it now, ask Home Assistant on the robot's loop."""
        eid = str(d.get("entity_id") or "")
        if d.get("kind") not in TOGGLES or not eid or self._toggle is None or eid in self._waiting:
            return False
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return False
        before = str(d.get("state") or "off")
        try:
            fut = asyncio.run_coroutine_threadsafe(self._toggle(eid), loop)
        except Exception:  # noqa: BLE001
            log.debug("home card: toggle failed to start", exc_info=True)
            return False
        d["state"] = "off" if _on(d) else "on"
        d.pop("brightness_percent", None)
        self._waiting[eid] = (fut, before)
        self.host.sound("pop")
        return True

    # ------------------------------------------------------------ the voice
    def describe(self) -> str:
        items = [f"{d['name']} {state_label(d).lower()}" for d in self.shown()]
        more = f" (page {self.page + 1} of {self.pages})" if self.pages > 1 else ""
        return f"{self.title}{more}: " + ", ".join(items) if items else self.title

    def status(self) -> dict[str, Any]:
        return {**super().status(), "page": self.page, "pages": self.pages, "devices": len(self.devices)}

    # ------------------------------------------------------------ frames
    def update(self, dt: float) -> None:
        super().update(dt)
        for eid, (fut, before) in list(self._waiting.items()):
            if not fut.done():
                continue
            del self._waiting[eid]
            d = next((x for x in self.devices if x.get("entity_id") == eid), None)
            if d is None:
                continue
            try:
                state = fut.result()
            except Exception as e:  # noqa: BLE001 - HomeError: the tile flips back, the voice is told
                d["state"] = before
                self.host.tell(f"Couldn't switch {d['name']}: {e}")
                continue
            if isinstance(state, str) and state:
                d["state"] = state

    def draw(self, surf: pygame.Surface) -> None:
        surf.blit(art.background((W, 480), seed=31), (0, 0))
        dy = candy.slide_in(self.t)
        candy.header(surf, self.title, dy=dy * 0.5)
        box = candy.panel(surf, dy=dy)
        for n, d in enumerate(self.shown()):
            k = _kit.appear(self.t, 0.05 * n, 0.3)
            if k <= 0:
                continue
            self._tile(surf, self.tile_rect(n).move(0, int(dy)), d, k)
        if self.pages > 1:
            _kit.page_dots(surf, self.pages, self.page, (box.centerx, box.bottom - 9))

    def _tile(self, surf: pygame.Surface, r: pygame.Rect, d: dict[str, Any], k: float) -> None:
        on = _on(d)
        kind = str(d.get("kind") or "switch")
        offline = str(d.get("state") or "") == "unavailable"
        fill = art.lighten(LIT, 0.72) if on and kind in ("light", "switch", "fan") else \
            (238, 236, 242) if offline else art.WHITE
        edge = art.inkier(LIT, 0.15) if on and kind in ("light", "switch", "fan") else (246, 210, 228)
        tile = art.rounded_rect(r.w, r.h, 24, fill, shadow=4, border=edge, border_w=4 if on else 3)
        if k < 0.99:
            art.blit_center(surf, art.scaled(tile, 0.85 + 0.15 * k), r.center)
            return
        surf.blit(tile, r.topleft)
        icon = _icon(kind, on, 70)
        if kind == "fan" and on:
            icon = art.rotated(icon, (self.t * 240) % 360, key=("home_fan", 70))
        art.blit_center(surf, icon, (r.x + 50, r.y + 50))
        if kind == "lock":
            tint = art.TANGERINE if on else art.MINT
        else:
            tint = art.MINT if on else art.mix(art.INK_SOFT, art.WHITE, 0.2)
        chip = _kit.chip(state_label(d), tint)
        surf.blit(chip, chip.get_rect(topright=(r.right - 12, r.y + 16)))
        name = candy.fit(str(d.get("name") or ""), BODY_PX, r.w - 24, art.INK, min_size=SMALL_PX)
        surf.blit(name, name.get_rect(bottomleft=(r.x + 14, r.bottom - 14)))
        if str(d.get("entity_id") or "") in self._waiting:  # asking Home Assistant: a soft pulsing dot
            a = 0.5 + 0.5 * math.sin(self.t * 8)
            art.blit_center(surf, art.circle(6, art.mix(art.INK_SOFT, art.WHITE, a)), (r.right - 20, r.bottom - 26))


def _skill(ctx: Any) -> Any:
    hub = getattr(ctx, "skills", None) if ctx is not None else None
    get = getattr(hub, "get", None)
    if get is None:
        return None
    try:
        skill = get("home")
        return skill if skill is not None and skill.available() else None
    except Exception:  # noqa: BLE001
        return None


async def build(spec: dict[str, Any], ctx: Any, host: CardHost) -> HomeCard | None:
    skill = _skill(ctx)
    devices = spec.get("devices")
    if not isinstance(devices, list):
        if skill is None:
            return None
        from littlebot.skills.home import HomeError, card_title  # noqa: PLC0415

        wanted = " ".join(str(spec.get("filter") or "").split())[:60]
        try:
            devices = await skill.device_list(wanted)
        except HomeError as e:
            log.info("screens: no home card (%s)", e.kind)
            return None
        spec = {**spec, "title": spec.get("title") or card_title(wanted)}
    devices = [d for d in devices if isinstance(d, dict) and d.get("name")]
    if not devices:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    return HomeCard(spec, host, devices=devices, toggle=getattr(skill, "toggle", None), loop=loop)


register(CardType(
    kind="home",
    description="Smart-home devices and their state (lights, plugs, the thermostat, blinds, locks ...); a tap "
                "switches a light or plug. home_devices shows it by itself; give a filter for a room or kind.",
    fields={"filter": {"type": "string", "description": "a room or a kind of device, e.g. 'kitchen', 'lights'"},
            "title": {"type": "string"}},
    build=build,
    actions={"next": "the next devices", "prev": "the devices before"},
))

SAMPLES: list[dict[str, Any]] = [
    {"card": "home", "title": "Lights", "devices": [
        {"entity_id": "light.kitchen", "name": "Kitchen Light", "kind": "light", "state": "on",
         "brightness_percent": 80},
        {"entity_id": "light.porch", "name": "Porch Light", "kind": "light", "state": "off"},
        {"entity_id": "switch.kettle", "name": "Kettle Plug", "kind": "switch", "state": "on"},
        {"entity_id": "climate.hall", "name": "Thermostat", "kind": "climate", "state": "heat",
         "target_temperature": 21, "temperature_now": 19.5},
        {"entity_id": "lock.front", "name": "Front Door", "kind": "lock", "state": "locked"},
        {"entity_id": "cover.garage", "name": "Garage Door", "kind": "cover", "state": "closed"},
        {"entity_id": "fan.bedroom", "name": "Bedroom Fan", "kind": "fan", "state": "on"},
        {"entity_id": "media_player.tv", "name": "Living Room TV", "kind": "media_player", "state": "playing"},
        {"entity_id": "scene.movie", "name": "Movie Night", "kind": "scene", "state": "scening"},
    ]},
]

__all__ = ["SAMPLES", "HomeCard", "state_label"]
