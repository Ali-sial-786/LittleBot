"""Desktop simulator side panel: a little drawn robot (head + arms from the
simulated servos), an E-STOP indicator and a compact status read-out.

Only used by ``display.py`` when ``display.show_sim_panel`` is on. All inputs
are plain dicts (``robot.status()`` / ``motion.status()``) that may be missing
or partial; the panel never raises because of odd data.
"""

from __future__ import annotations

import math
from typing import Any

import pygame

PANEL_BG = (16, 19, 28)
PANEL_LINE = (40, 46, 62)
TEXT = (210, 220, 235)
DIM = (130, 140, 160)
ACCENT = (90, 210, 255)
SCREEN_PINK = (255, 214, 228)  # the face's screen colour (face.background_color default)
INK = (38, 70, 95)  # eye rims on the pink screen
MOUTH_INK = (66, 32, 62)
GOOD = (90, 220, 150)
WARN = (255, 196, 90)
BAD = (255, 90, 90)
BODY = (70, 82, 104)
BODY_EDGE = (120, 136, 168)
RGB = tuple[int, int, int]

# Fallback joint layout when motion is unavailable (matches config defaults).
DEFAULT_JOINTS: dict[str, dict[str, float]] = {
    "head": {"angle": 90, "target": 90, "min": 50, "max": 130, "neutral": 90},
    "left_arm": {"angle": 20, "target": 20, "min": 10, "max": 150, "neutral": 20},
    "right_arm": {"angle": 160, "target": 160, "min": 30, "max": 170, "neutral": 160},
}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def arm_rotation(joint: dict[str, Any]) -> float:
    """Degrees the arm is rotated away from hanging straight down (0 = down, 180 = up).

    Servos mounted mirrored have their neutral near the top of their range
    (e.g. right_arm neutral 160, range 30..170); those are inverted.
    """
    angle = _num(joint.get("angle"), _num(joint.get("neutral"), 90))
    lo, hi = _num(joint.get("min"), 0), _num(joint.get("max"), 180)
    neutral = _num(joint.get("neutral"), (lo + hi) / 2)
    return 180.0 - angle if neutral > (lo + hi) / 2 else angle


def _get(d: Any, *path: str, default: Any = None) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p)
    return default if d is None else d


class SimPanel:
    def __init__(self) -> None:
        self._fonts: dict[int, pygame.font.Font] = {}
        self._text_cache: dict[tuple[str, int, tuple[int, int, int]], pygame.Surface] = {}
        self.estop_rect: pygame.Rect | None = None

    def text(self, text: str, size: int, color: tuple[int, int, int]) -> pygame.Surface:
        """Rendered text, cached (the panel redraws every frame but its text rarely changes)."""
        key = (text, size, color)
        img = self._text_cache.get(key)
        if img is None:
            if len(self._text_cache) > 400:
                self._text_cache.clear()
            img = self._text_cache[key] = self.font(size).render(text, True, color)
        return img

    def font(self, size: int) -> pygame.font.Font:
        f = self._fonts.get(size)
        if f is None:
            if not pygame.font.get_init():
                pygame.font.init()
            f = pygame.font.Font(None, size)
            self._fonts[size] = f
        return f

    # ------------------------------------------------------------------ draw
    def draw(
        self,
        surf: pygame.Surface,
        rect: pygame.Rect,
        status: dict[str, Any] | None,
        motion: dict[str, Any] | None,
        *,
        fps: float = 0.0,
        caption: str = "",
    ) -> None:
        status = status or {}
        pygame.draw.rect(surf, PANEL_BG, rect)
        pygame.draw.line(surf, PANEL_LINE, rect.topleft, rect.bottomleft, 2)
        x, y = rect.x + 14, rect.y + 10
        title = self.text(f"{status.get('name') or 'LittleBot'} simulator", 24, TEXT)
        surf.blit(title, (x, y))
        mode = str(status.get("mode") or "")
        if mode:
            m = self.text(mode, 17, DIM)
            surf.blit(m, (rect.right - m.get_width() - 12, y + 4))
        y += 28
        robot_rect = pygame.Rect(rect.x + 10, y, rect.w - 20, 178)
        self._draw_robot(surf, robot_rect, motion)
        y = robot_rect.bottom + 6
        y = self._draw_estop(surf, rect, y, motion)
        y += 6
        for label, value, color in self._lines(status, motion, fps):
            if y > rect.bottom - 34:
                break
            lab = self.text(label, 18, DIM)
            surf.blit(lab, (x, y))
            val = self.text(self._fit(value, rect.w - 110, 18), 18, color)
            surf.blit(val, (x + 86, y))
            y += 19
        hint = self.text(caption or "H: keyboard help", 17, ACCENT if caption else DIM)
        surf.blit(hint, (x, rect.bottom - 24))

    def _fit(self, text: str, width: int, size: int) -> str:
        f = self.font(size)
        if f.size(text)[0] <= width:
            return text
        while text and f.size(text + "…")[0] > width:
            text = text[:-1]
        return text + "…"

    def _lines(self, s: dict[str, Any], motion: dict[str, Any] | None, fps: float) -> list[tuple[str, str, RGB]]:
        out: list[tuple[str, str, RGB]] = []
        st = _get(s, "state", "state", default="?")
        since = _get(s, "state", "since_seconds", default=None)
        out.append(("state", f"{st}" + (f"  ({since:.0f}s)" if isinstance(since, (int, float)) else ""), TEXT))
        face = s.get("face") if isinstance(s.get("face"), dict) else {}
        out.append(("face", f"{face.get('expression', '?')} / {face.get('state', '?')}  {fps:.0f} fps", TEXT))
        act = s.get("activities") if isinstance(s.get("activities"), dict) else {}
        if act.get("running"):
            step = ""
            if act.get("total_steps"):
                step = f" {int(_num(act.get('step_index'))) + 1}/{act.get('total_steps')}"
            paused = " (paused)" if act.get("paused") else ""
            out.append(("activity", f"{act.get('name') or act.get('id')}{step}{paused}", ACCENT))
        else:
            out.append(("activity", "-" if act.get("status") != "unavailable" else "unavailable", DIM))
        media = s.get("media") if isinstance(s.get("media"), dict) else {}
        mstate = media.get("state") or media.get("status") or "-"
        out.append(("media", str(mstate), ACCENT if mstate == "playing" else TEXT if mstate != "unavailable" else DIM))
        vision = s.get("vision") if isinstance(s.get("vision"), dict) else {}
        cam = vision.get("camera") or vision.get("status") or "-"
        faces = vision.get("faces")
        cam_txt = f"{cam}" + (f" ({vision.get('backend')})" if vision.get("backend") else "")
        if isinstance(faces, (int, float)) or isinstance(faces, list):
            n = len(faces) if isinstance(faces, list) else int(faces)
            cam_txt += f"  faces:{n}"
        out.append(("camera", cam_txt, self._health_color(cam)))
        audio = s.get("audio") if isinstance(s.get("audio"), dict) else {}
        mic = _get(audio, "microphone", "status", default=audio.get("status", "-"))
        spk = _get(audio, "speaker", "status", default=audio.get("status", "-"))
        out.append(("mic", str(mic) + ("  (listening)" if audio.get("listening") else ""), self._health_color(mic)))
        out.append(("speaker", str(spk) + ("  (speaking)" if audio.get("speaking") else ""), self._health_color(spk)))
        brain = s.get("brain") if isinstance(s.get("brain"), dict) else {}
        provider = brain.get("active_provider") or brain.get("provider") or brain.get("status") or "-"
        out.append(("brain", str(provider), TEXT))
        sysd = s.get("system") if isinstance(s.get("system"), dict) else {}
        cpu, mem, temp = sysd.get("cpu_percent"), sysd.get("memory_mb"), sysd.get("temperature_c")
        parts = []
        if isinstance(cpu, (int, float)):
            parts.append(f"cpu {cpu:.0f}%")
        if isinstance(mem, (int, float)):
            parts.append(f"{mem:.0f} MB")
        if isinstance(temp, (int, float)):
            parts.append(f"{temp:.0f}°C")
        out.append(("system", "  ".join(parts) or "-", TEXT))
        leases = s.get("leases") if isinstance(s.get("leases"), dict) else {}
        held = sorted({str(v) for v in leases.values() if v})
        out.append(("leases", ", ".join(held) or "-", TEXT if held else DIM))
        return out

    @staticmethod
    def _health_color(v: Any) -> RGB:
        v = str(v).lower()
        if v in ("ok", "running", "ready", "idle"):
            return GOOD
        if v in ("simulated", "mock", "fake"):
            return TEXT
        if v in ("disabled", "unavailable", "-", "none"):
            return DIM
        if v in ("disconnected", "error", "failed"):
            return BAD
        return WARN

    def _draw_estop(self, surf: pygame.Surface, rect: pygame.Rect, y: int, motion: dict[str, Any] | None) -> int:
        estop = bool(motion and motion.get("estop"))
        physical = bool(motion and motion.get("physical_enabled"))
        if motion is None:
            text, col = "motion unavailable", DIM
        elif estop:
            text, col = "E-STOP ACTIVE", BAD
        else:
            text, col = ("motors: PHYSICAL" if physical else "motors: simulated") + "   (E = e-stop)", GOOD
        pill = pygame.Rect(rect.x + 12, y, rect.w - 24, 24)
        pygame.draw.rect(surf, (*col,) if estop else PANEL_LINE, pill, border_radius=12)
        pygame.draw.rect(surf, col, pill, width=2, border_radius=12)
        t = self.text(text, 19, (255, 255, 255) if estop else col)
        surf.blit(t, (pill.centerx - t.get_width() // 2, pill.centery - t.get_height() // 2 + 1))
        self.estop_rect = pill
        return pill.bottom

    def _draw_robot(self, surf: pygame.Surface, rect: pygame.Rect, motion: dict[str, Any] | None) -> None:
        joints = DEFAULT_JOINTS
        if motion and isinstance(motion.get("joints"), dict) and motion["joints"]:
            joints = {**DEFAULT_JOINTS, **{k: v for k, v in motion["joints"].items() if isinstance(v, dict)}}
        pygame.draw.rect(surf, (22, 26, 38), rect, border_radius=10)
        cx = rect.centerx
        body = pygame.Rect(0, 0, 92, 62)
        body.midbottom = (cx, rect.bottom - 16)
        # base / stand
        pygame.draw.rect(surf, (44, 52, 68), pygame.Rect(cx - 60, rect.bottom - 18, 120, 10), border_radius=5)
        # arms behind the body
        moving = bool(motion and motion.get("moving"))
        # The robot faces the viewer: its left arm is on the viewer's right (as for the head yaw
        # below and the dashboard's drawing).
        for name, side in (("left_arm", 1), ("right_arm", -1)):
            j = joints.get(name, DEFAULT_JOINTS[name])
            shoulder = (body.centerx + side * (body.w // 2 - 4), body.top + 12)
            rot = math.radians(arm_rotation(j))
            target = dict(j)
            target["angle"] = j.get("target", j.get("angle"))
            trot = math.radians(arm_rotation(target))
            length = 52
            if abs(trot - rot) > 0.02:
                tip = (shoulder[0] + side * math.sin(trot) * length, shoulder[1] + math.cos(trot) * length)
                pygame.draw.line(surf, PANEL_LINE, shoulder, tip, 9)
            tip = (shoulder[0] + side * math.sin(rot) * length, shoulder[1] + math.cos(rot) * length)
            pygame.draw.line(surf, BODY_EDGE, shoulder, tip, 11)
            pygame.draw.line(surf, BODY, shoulder, tip, 7)
            pygame.draw.circle(surf, BODY_EDGE, (int(tip[0]), int(tip[1])), 8)
            pygame.draw.circle(surf, ACCENT if moving else BODY, (int(tip[0]), int(tip[1])), 5)
            lbl = self.text(f"{_num(j.get('angle')):.0f}°", 15, DIM)
            lx = rect.x + 6 if side < 0 else rect.right - lbl.get_width() - 6
            surf.blit(lbl, (lx, rect.bottom - 40))
        pygame.draw.rect(surf, BODY, body, border_radius=14)
        pygame.draw.rect(surf, BODY_EDGE, body, width=2, border_radius=14)
        # head: yaw shown by squashing + sliding the screen sideways
        head = joints.get("head", DEFAULT_JOINTS["head"])
        yaw = _num(head.get("angle"), 90) - _num(head.get("neutral"), 90)
        k = math.sin(math.radians(yaw))
        hw = int(118 * (1 - 0.25 * abs(k)))
        hrect = pygame.Rect(0, 0, hw, 76)
        hrect.midbottom = (int(cx + k * 14), body.top - 6)
        pygame.draw.rect(surf, (52, 60, 78), pygame.Rect(cx - 8, body.top - 10, 16, 12))
        pygame.draw.rect(surf, BODY, hrect, border_radius=16)
        pygame.draw.rect(surf, BODY_EDGE, hrect, width=2, border_radius=16)
        screen = hrect.inflate(-16, -16)
        pygame.draw.rect(surf, SCREEN_PINK, screen, border_radius=10)  # same pastel screen as the face
        ex = screen.centerx + int(k * 10)
        for s in (-1, 1):
            er = pygame.Rect(0, 0, 13, 17)
            er.center = (ex + s * 17, screen.centery - 3)
            pygame.draw.rect(surf, INK, er.inflate(4, 4), border_radius=8)
            pygame.draw.rect(surf, ACCENT, er, border_radius=6)
        pygame.draw.arc(surf, MOUTH_INK, pygame.Rect(ex - 6, screen.centery + 4, 12, 9), math.pi * 1.1,
                        math.pi * 1.9, 2)
        lbl = self.text(f"head {_num(head.get('angle'), 90):.0f}°", 15, DIM)
        surf.blit(lbl, (rect.centerx - lbl.get_width() // 2, rect.y + 4))
