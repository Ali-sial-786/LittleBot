"""Every part of LittleBot v5 where it sits in the assembled robot, for the assembly video (index.html):

    python3 place_parts.py build        # writes build/meshes/*.bin, build/manifest.json and build/scene.json

Robot coordinates (mm, z up, x = the viewer's right, y = front -> back), as littlebot_v5.scad.
  - printed parts and the bought parts' stand-ins: ../stl/assembled/*.stl (export_v5.sh), already in place;
  - small parts the assembled STLs lump together: build/stl/*.stl (video5.scad, part="vid5", make_video.sh);
  - the deck's boards, the 6xAA holder, the camera, the charging socket and charger, every screw, nut and cable, and
    the tools that drive the screws: built here from hardware5.json (video5.scad, part="hw5": the model's own positions).
Each mesh: x y z nx ny nz s (float32). s: along a cable (0..1, it grows), else 0; -1 = a dark detail (a screw's
hex socket, a nut's hole, a tool's grip) drawn in the same mesh.
"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "enclosure_v2"))
from stl_tools import read_stl  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "build")
(OUT / "meshes").mkdir(parents=True, exist_ok=True)
screws, nuts, cables, params, glue = json.loads((HERE / "hardware5.json").read_text())
P = dict(params)
I = np.eye(3)


# ---------------------------------------------------------------- mesh helpers (triangles: (n, 3, 3), or (n, 3, 4) with s)
def box(lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], float)
    f = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v[np.array(f)]


def prism(d1, d2, z0, z1, n=24, phase=0.0, dark_every=0):
    """A frustum / prism along z (n sides), from z0 (diameter d1) to z1 (d2), capped. dark_every: every k-th side dark."""
    a = np.linspace(0, 2 * np.pi, n + 1) + phase
    t, s = [], []
    for i in range(n):
        c0, c1 = np.array([np.cos(a[i]), np.sin(a[i])]), np.array([np.cos(a[i + 1]), np.sin(a[i + 1])])
        p00, p01 = (*c0 * d1 / 2, z0), (*c1 * d1 / 2, z0)
        p10, p11 = (*c0 * d2 / 2, z1), (*c1 * d2 / 2, z1)
        side = -1.0 if dark_every and i % dark_every == 0 else 0.0
        t += [[p00, p01, p11], [p00, p11, p10], [(0, 0, z0), p01, p00], [(0, 0, z1), p10, p11]]
        s += [side, side, 0.0, 0.0]
    return with_s(np.array(t, float), np.array(s))


def cyl(d, z0, z1, n=24): return prism(d, d, z0, z1, n)


def with_s(t, s):
    t = np.asarray(t, float)
    if t.shape[-1] == 4: return t
    s = np.broadcast_to(np.asarray(s, float).reshape(-1, 1), t.shape[:2])
    return np.concatenate([t, s[..., None]], axis=2)


def dark(t): t = with_s(t, 0.0); t[..., 3] = -1.0; return t


def xf(t, A=I, o=(0, 0, 0)):
    t = with_s(t, 0.0).copy(); t[..., :3] = t[..., :3] @ np.asarray(A, float).T + np.asarray(o, float); return t


def cat(*a): return np.concatenate([with_s(x, 0.0) for x in a])


def to_z(v):
    """Rotation taking +z onto v."""
    v = np.asarray(v, float) / np.linalg.norm(v); z = np.array([0, 0, 1.0]); c = float(z @ v)
    if c > 0.9999: return I.copy()
    if c < -0.9999: return np.diag([1.0, -1, -1])
    k = np.cross(z, v); k /= np.linalg.norm(k); K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return I + np.sin(np.arccos(c)) * K + (1 - c) * K @ K


def rot_z(deg):
    a = np.radians(deg); return np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1.0]])


def ball(c, r, n=10, m=6):
    c = np.asarray(c, float)
    pt = lambda i, j: c + r * np.array([np.sin(np.pi * j / m) * np.cos(2 * np.pi * i / n), np.sin(np.pi * j / m) * np.sin(2 * np.pi * i / n), np.cos(np.pi * j / m)])
    return np.array([tri for i in range(n) for j in range(m) for tri in ([pt(i, j), pt(i, j + 1), pt(i + 1, j + 1)], [pt(i, j), pt(i + 1, j + 1), pt(i + 1, j)])])


def rod(a, b, r, n=12):
    a, b = np.asarray(a, float), np.asarray(b, float); d = (b - a) / np.linalg.norm(b - a)
    u = np.cross(d, [1.0, 0, 0] if abs(d[0]) < 0.9 else [0, 1.0, 0]); u /= np.linalg.norm(u); v = np.cross(d, u)
    ring = [r * (np.cos(k * 2 * np.pi / n) * u + np.sin(k * 2 * np.pi / n) * v) for k in range(n + 1)]
    return np.array([tri for k in range(n) for tri in ([a + ring[k], a + ring[k + 1], b + ring[k + 1]], [a + ring[k], b + ring[k + 1], b + ring[k]])])


def tube(pts, r):
    """A cable along pts; s = how far along (0..1), so it can grow."""
    pts = np.asarray(pts, float); L = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]; L /= L[-1]; out = []
    for i in range(len(pts) - 1):
        seg = rod(pts[i], pts[i + 1], r)
        near0 = np.linalg.norm(seg - pts[i], axis=2) < np.linalg.norm(seg - pts[i + 1], axis=2)
        out.append(np.concatenate([seg, np.where(near0, L[i], L[i + 1])[..., None]], axis=2))
    for c, li in zip(pts, L):
        b = ball(c, r); out.append(with_s(b, li))
    return np.concatenate(out)


def load(path): return read_stl(path).astype(np.float64)


# ---------------------------------------------------------------- the parts
parts = []   # (name, tris, colour, group, extra)
def add(name, tris, colour, group, **extra): parts.append((name, with_s(tris, 0.0) if tris.shape[-1] == 3 else tris, colour, group, extra))

WHITE, DARK, V2, GREY, KEEP = "#f2efe8", "#2b2d31", "#e3ded2", "#a9adbb", "#c8c2b4"
ASSY = HERE.parent / "stl" / "assembled"; SMALL = OUT / "stl"
for name, colour, group in [("tub", DARK, "tub"), ("tray", "#34373d", "tray"), ("deck", "#d9d4c8", "deck"), ("switch", "#3a3d44", "tub"),
                            ("stack_keeper", KEEP, "tub"),
                            ("body", WHITE, "body"), ("slider", WHITE, "body"), ("speaker", "#202124", "body"), ("spk_keeper", KEEP, "body"),
                            ("mic_holder", WHITE, "body"), ("mic_key", KEEP, "body"), ("respeaker", "#1f2328", "body"),
                            ("cap", WHITE, "neck"), ("hub", "#7fb3d5", "neck"), ("neck", DARK, "neck"), ("servo_frame", GREY, "hservo"),
                            ("head_frame", WHITE, "head"), ("head_inlay", DARK, "head"), ("head_back", WHITE, "head"), ("camera_bracket", GREY, "head"),
                            ("screen", "#15171c", "head")]:
    add(name, load(ASSY / f"{name}.stl"), colour, group)
add("arm_a", load(ASSY / "arm_left.stl"), WHITE, "arm_a")         # x < 0: the robot's right arm
add("arm_b", load(ASSY / "arm_right.stl"), WHITE, "arm_b")        # x > 0: the robot's left arm
for name, colour, group in [("ring_gear", "#e6b35a", "neck"), ("pinion", "#e6b35a", "hservo"), ("horn_head", "#f7f7f7", "hservo"),
                            ("servo_head", "#3a6fb0", "hservo"), ("servo_r", "#3a6fb0", "body"), ("servo_l", "#3a6fb0", "body"),
                            ("horn_r", "#f7f7f7", "arm_a"), ("horn_l", "#f7f7f7", "arm_b"),
                            ("fpc", "#c9a74a", "head"), ("usbc_plug", "#3a3d44", "head"), ("coupler4", "#16181c", "head"), ("hdmi_plug", "#2a2d33", "head"),
                            ("mic_plug", "#3a3d44", "body"), ("eyes", "#4fe3e0", "eyes"),
                            ("pod_l", "#3a3d44", "podL"), ("pod_r", "#3a3d44", "podR"), ("motor_l", "#a9aeb6", "podL"), ("motor_r", "#a9aeb6", "podR"),
                            ("wheel_l", "#1c1d20", "podL"), ("wheel_r", "#1c1d20", "podR"), ("caster_f", "#b9bec6", "tub"), ("caster_r", "#b9bec6", "tub"),
                            ("pi_ups", "#1a9a55", "stack")]:
    add(name, load(SMALL / f"{name}.stl"), colour, group)

# the 6xAA holder in the drawer: its box and 6 cells lying front to back
(aw, ad, ah), aa_y0, tray_z = P["aa"]
az0 = tray_z + 2; w = 1.4
add("aa_holder", cat(box([-aw / 2, aa_y0, az0], [aw / 2, aa_y0 + ad, az0 + w]), box([-aw / 2, aa_y0, az0], [-aw / 2 + w, aa_y0 + ad, az0 + ah]),
                     box([aw / 2 - w, aa_y0, az0], [aw / 2, aa_y0 + ad, az0 + ah]), box([-aw / 2, aa_y0, az0], [aw / 2, aa_y0 + w, az0 + ah]),
                     box([-aw / 2, aa_y0 + ad - w, az0], [aw / 2, aa_y0 + ad, az0 + ah])), "#18191c", "tray")
cells = [xf(cyl(14.3, 0, 50.5, 20), to_z([0, 1, 0]), [-aw / 2 + 7.3 + k * 15.08, aa_y0 + 3.2, az0 + 8.6]) for k in range(6)]
add("aa_cells", cat(*cells), "#3aa655", "tray")

# the deck's boards: breadboard with the RP2040 on it, the motor driver on its 4 posts, the servo power board, the converter
deck_z, deck_t = P["deck"]; top = deck_z + deck_t
(bbw, bbd, bbh), (bx, by) = P["bb"]
add("breadboard", cat(box([bx - bbw / 2, by - bbd / 2, top], [bx + bbw / 2, by + bbd / 2, top + bbh]),
                      dark(box([bx - bbw / 2 + 3, by - 0.9, top + bbh - 0.6], [bx + bbw / 2 - 3, by + 0.9, top + bbh + 0.05]))), "#f4f4f2", "deck")
add("rp2040", cat(box([bx - 26.7, by - 11.4, top + bbh + 8.4], [bx + 26.7, by + 11.4, top + bbh + 10]),
                  dark(box([bx - 25.4, by - 10.6, top + bbh], [bx + 25.4, by - 8.0, top + bbh + 8.4])), dark(box([bx - 25.4, by + 8.0, top + bbh], [bx + 25.4, by + 10.6, top + bbh + 8.4])),
                  box([bx - 6, by - 6, top + bbh + 10], [bx + 6, by + 6, top + bbh + 11.2]), box([bx - 29, by - 4.5, top + bbh + 10], [bx - 22, by + 4.5, top + bbh + 13.2])),
    "#1f7a4a", "deck")
(dw, dd), (dx, dy), (hx, hy) = P["drv"]
dz = top + 4
add("driver", cat(box([dx - dw / 2, dy - dd / 2, dz], [dx + dw / 2, dy + dd / 2, dz + 1.6]),
                  *[xf(cyl(6, 0, 4, 16), I, [dx + sx * hx / 2, dy + sy * hy / 2, top]) for sx in (-1, 1) for sy in (-1, 1)],
                  box([dx - 9, dy - 12, dz + 1.6], [dx + 9, dy + 6, dz + 5])), "#b8312f", "deck")
add("driver_terms", cat(box([dx - 17, dy + dd / 2 - 8, dz + 1.6], [dx + 17, dy + dd / 2 - 0.5, dz + 11.6]),
                        *[dark(box([dx - 15 + k * 7.6, dy + dd / 2 - 7, dz + 11.6], [dx - 12 + k * 7.6, dy + dd / 2 - 1.5, dz + 11.7])) for k in range(5)]), "#2d6cb5", "deck")
(pw, pd), (px, py) = P["perf"]
add("perfboard", cat(box([px - pw / 2, py - pd / 2, top + 0.2], [px + pw / 2, py + pd / 2, top + 1.8]),
                     *[box([c[0] - 1.3, c[1] - 3.9, c[2]], [c[0] + 1.3, c[1] + 3.9, c[2] + 2.5]) for c in P["perf_ch"]],
                     box([px + pw / 2 - 16, py - pd / 2 + 4, top + 1.8], [px + pw / 2 - 4, py - pd / 2 + 14, top + 11])), "#c9a24a", "deck")
(kw, kd, kh), (kx, ky) = P["buck"]
add("converter", box([kx - kw / 2, ky - kd / 2, top + 0.2], [kx + kw / 2, ky + kd / 2, top + 0.2 + kh]), "#1c3d6e", "deck")

# the camera (v2's board and lens) in the hood
cy, cz, ct, cw = P["cam"]
add("cam_board", cat(box([-cw / 2, cy, cz - cw / 2], [cw / 2, cy + ct, cz + cw / 2]), xf(cyl(14, 0, P["cam_lens_l"]), to_z([0, -1, 0]), [0, cy, cz])), "#8a3b3b", "head")

# the charging socket in its cradle at the back's left, its zip tie, and the charger: built in the socket's frame
# (place_chg(): +y out through the wall, the origin on the wall's inner face), then turned and moved into place
chg_o, lean, wall = P["chg"]; R = rot_z(lean)
def chg(t): return xf(t, R, chg_o)
def chg_p(p): return (R @ np.asarray(p, float) + np.asarray(chg_o)).tolist()
us = P["usbc_sock"]                                                # width, height, length
add("usbc_socket", chg(cat(box([-us[0] / 2, -us[2] - 1, -us[1] / 2], [us[0] / 2, -1, us[1] / 2]), dark(box([-4.2, -1.05, -1.3], [4.2, -0.9, 1.3])))), "#333333", "tub")
zy, zx0, zx1, zz0, zz1, zt = -1 - us[2] / 2, -us[0] / 2 - 3.3, us[0] / 2 + 0.8, -us[1] / 2 - 3.3, us[1] / 2 + 0.8, 0.9
add("zip_tie", chg(cat(box([zx0, zy - 1.6, zz1 - zt], [zx1, zy + 1.6, zz1]), box([zx0, zy - 1.6, zz0], [zx1, zy + 1.6, zz0 + zt]),
                       box([zx0, zy - 1.6, zz0], [zx0 + zt, zy + 1.6, zz1]), box([zx1 - zt, zy - 1.6, zz0], [zx1, zy + 1.6, zz1]),
                       box([-2.5, zy - 2.2, zz1], [2.5, zy + 2.2, zz1 + 2.6]))), "#26282c", "tub")
z_table = -float(chg_o[2])                                         # the floor, in the socket's frame
cy1, cy_out, pl = -1.0, float(wall), 24.0                          # the socket's mouth, the wall's outer face; the plug's moulding length
add("chg_plug", chg(cat(dark(box([-4.15, cy1 - 6.6, -1.25], [4.15, cy1 + 0.6, 1.25])), box([-5.5, cy1 + 0.6, -3.25], [5.5, cy1 + 0.6 + pl, 3.25]),
                        xf(cyl(5, 0, 8, 16), to_z([0, 1, 0]), [0, cy1 + 0.6 + pl, 0]))), "#ededed", "charger")
add("charger", chg(cat(box([70, cy_out + 95, z_table], [116, cy_out + 125, z_table + 52]), dark(box([88.5, cy_out + 94.95, z_table + 40], [97.5, cy_out + 95.5, z_table + 43.2])))),
    "#ededed", "charger")
chg_route = [chg_p(p) for p in [[0, cy1 + 8.6 + pl, 0], [0, cy1 + 40 + pl, -2], [18, cy_out + 52, z_table + 3], [70, cy_out + 70, z_table + 3],
                                [93, cy_out + 82, z_table + 30], [93, cy_out + 88, z_table + 41.6], [93, cy_out + 95, z_table + 41.6]]]


# ---------------------------------------------------------------- screws, nuts, tools
def screw_mesh(d, l, head):
    """Local: the head bears at z = 0, the shank runs to z = +l (the way it goes in)."""
    k = d / 3
    if head == "btn":     # ISO 7380 button head, hex socket
        h = cat(prism(5.7 * k, 3.6 * k, 0, -1.65 * k, 28), dark(prism(2.3 * k, 2.3 * k, -1.66 * k, -1.7 * k, 6)))
    else:                 # Phillips pan head
        hh = 0.62 * d
        h = cat(prism(1.9 * d, 1.75 * d, 0, -hh, 24), dark(box([-0.7 * d, -0.13 * d, -hh - 0.05], [0.7 * d, 0.13 * d, -hh + 0.02])),
                dark(box([-0.13 * d, -0.7 * d, -hh - 0.05], [0.13 * d, 0.7 * d, -hh + 0.02])))
    return cat(cyl(d, 0, l, 12), h)


def nut_mesh(af, t):   # axis z, centred
    d = af / np.cos(np.radians(30))
    return cat(prism(d, d, -t / 2, t / 2, 6, np.radians(30)), dark(cyl(af * 0.58, -t / 2 - 0.05, t / 2 + 0.05, 16)))


TOOL = {"camera": "lkey", "back": "lkey"}                             # else: a driver by the head type
def screw_group(step, what, pos):   # the assembly a screw moves with
    if step == "arms": return "arm_a" if pos[0] < 0 else "arm_b"
    if step in ("pod_bench", "pods_in"): return "podL" if pos[0] > 0 else "podR"
    if step == "head_servo": return "neck" if what.startswith("M3") else "hservo"
    return {"casters": "tub", "stack": "tub", "deck_bench": "deck", "deck_in": "deck", "drawer": "tray", "arm_servos": "body", "mic": "body",
            "body_on": "tub", "ring_gear": "neck", "black_neck": "neck", "cap_on": "neck", "head_on": "head", "screen": "head", "camera": "head",
            "back": "head"}[step]
LEVER = {"camera": lambda p: [0, 1, -0.35], "back": lambda p: [np.sign(p[0]), 0, 0]}   # where an L-key's long leg points
for k, (step, what, d, l, head, pos, v) in enumerate(screws):
    v = np.asarray(v, float); v /= np.linalg.norm(v); R = to_z(v)
    tool = TOOL.get(step, "hexdriver" if head == "btn" else "phdriver")
    lever = [float(c) for c in LEVER[step](pos)] if step in LEVER else None
    add(f"s{k:02d}_{step}", xf(screw_mesh(d, l, head), R, pos), "#3b4049", screw_group(step, what, pos), kind="screw", step=step, screw=what, pos=pos,
        dir=v.round(4).tolist(), l=l, tool=tool, lever=lever, head_h=(1.65 * d / 3 if head == "btn" else 0.62 * d))
NUT_GROUP = {"casters": "tub", "body_nuts": "body", "black_neck": "neck"}
NUT_NAME = {"casters": "#2-56 nut", "body_nuts": "M3 nut", "black_neck": "M3 nut"}
for k, (step, c, axis, way, af, t) in enumerate(nuts):
    add(f"n{k:02d}_{step}", xf(nut_mesh(af, t), to_z(axis), c), "#9aa0a8", NUT_GROUP[step], kind="nut", step=step, pos=c,
        dir=[round(float(x), 4) for x in np.asarray(way, float) / np.linalg.norm(way)], nut=NUT_NAME[step])
# the tools: local frame, the tip at the origin, the tool behind it (z < 0); index.html turns +z onto the screw's axis
add("tool_hexdriver", cat(prism(2.3, 2.3, 0, -42, 6), cyl(5.5, -42, -48, 16), prism(17, 18, -48, -104, 8, 0, 2), prism(18, 12, -104, -108, 8)), "#e08a1e", "tool", kind="tool")
add("tool_phdriver", cat(prism(1.2, 3, 0, -4, 8), cyl(3, -4, -46, 12), cyl(6, -46, -52, 16), prism(17, 18, -52, -108, 8, 0, 2), prism(18, 12, -108, -112, 8)), "#c0392b", "tool", kind="tool")
# a 2-part epoxy syringe: mixing nozzle (dark), barrel, plunger
add("tool_glue", cat(dark(prism(1.1, 3.6, 0, -16, 12)), cyl(5, -16, -20, 12), cyl(13, -20, -78, 20), cyl(22, -78, -80, 20), cyl(4, -80, -94, 8),
                     cyl(13, -94, -96, 16)), "#e9e4d8", "tool", kind="tool")
add("tool_lkey", cat(prism(2.3, 2.3, 0, -20, 6), ball([0, 0, -20], 1.25, 8, 6), xf(prism(2.3, 2.3, 0, 42, 6), to_z([1, 0, 0]), [0, 0, -20])), "#3c4250", "tool", kind="tool")

# ---------------------------------------------------------------- cables (they grow along their routes)
LABEL_AT_END = {"motor_l", "enc_l", "switched", "servo_head", "servo_left", "servo_right", "hdmi_head", "ribbon", "usbc_head", "camera_head"}
for name, step, group, colour, d, label, route, plugs in cables:
    t = tube(route, d / 2)
    for lo, hi in plugs:
        b = box(lo, hi); end = np.linalg.norm(np.add(lo, hi) / 2 - route[-1]) < np.linalg.norm(np.add(lo, hi) / 2 - route[0])
        t = np.concatenate([t, with_s(b, 1.0 if end else 0.0)])
    add(f"c_{name}", t, colour, group, kind="cable", step=step,
        label=label.replace("->", "→"), label_at=[round(float(c), 1) for c in route[-1 if name in LABEL_AT_END else 0]])

add("c_charger", tube(chg_route, 2.0), "#ededed", "charger", kind="cable", step="charging", label="charger's cable", label_at=[round(float(c), 1) for c in chg_route[3]])

# the epoxy beads in the horns' pockets (they grow along their routes; the syringe follows the tip)
for name, step, group, route, v in glue:
    r = np.asarray(route, float); L = np.r_[0, np.cumsum(np.linalg.norm(np.diff(r, axis=0), axis=1))]
    add(name, tube(route, 0.8), "#e0a526", group, kind="glue", step=step, route=[[round(float(c), 3) for c in q] for q in route],
        frac=[round(float(x), 4) for x in L / L[-1]], dir=list(np.round(np.asarray(v, float) / np.linalg.norm(v), 4)),
        label="2-part epoxy" if name in ("g_pinion", "g_arm_r") else "", label_at=[round(float(c), 1) for c in route[0]])

# ---------------------------------------------------------------- write
manifest = []
for name, tris, colour, group, extra in parts:
    t = tris.astype(np.float32); s = t[:, :, 3]; t = t[:, :, :3]
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]); ln = np.linalg.norm(n, axis=1, keepdims=True); n = n / np.where(ln > 0, ln, 1)
    data = np.concatenate([t.reshape(-1, 3), np.repeat(n, 3, axis=0), s.reshape(-1, 1)], axis=1).astype(np.float32)
    (OUT / "meshes" / f"{name}.bin").write_bytes(data.tobytes())
    lo, hi = t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)
    manifest.append({"name": name, "file": f"meshes/{name}.bin", "colour": colour, "group": group, "tris": len(t),
                     "lo": [round(float(c), 1) for c in lo], "hi": [round(float(c), 1) for c in hi],
                     **{k: (float(v) if isinstance(v, (np.floating,)) else v) for k, v in extra.items()}})
(OUT / "manifest.json").write_text(json.dumps(manifest, indent=0))
(OUT / "scene.json").write_text(json.dumps({"sh": P["sh"], "tab_hinge": P["tab_hinge"], "slider_travel": P["slider_travel"], "marks": dict(P["marks"]),
                                            "pinion_horn": P["pinion_horn"], "arm_pocket": P["arm_pocket"], "rsl_c": P["rsl_c"],
                                            "z_table": 0.0}))   # for index.html
kinds = {}
for m in manifest: kinds[m.get("kind", "part")] = kinds.get(m.get("kind", "part"), 0) + 1
print(kinds, "triangles", sum(m["tris"] for m in manifest))
