"""Every part of LittleBot v4 where it sits in the assembled robot, for the assembly video (index.html):

    python3 place_parts.py build        # writes build/meshes/*.bin and build/manifest.json

Robot coordinates (mm, z up, x = the viewer's right, y = front -> back), as littlebot_v4.scad.
  - printed parts: ../stl/assembled/*.stl (export_v4.sh), already in place;
  - small parts the assembled STLs lump together: build/stl/*.stl (video4.scad, part="vid4", make_video.sh);
  - the electronics as blocks, every screw, nut and cable, and the tools that drive the screws: built here from
    hardware4.json (video4.scad, part="hw4": the model's own positions).
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
screws, nuts, cables, params = json.loads((HERE / "hardware4.json").read_text())
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

WHITE, DARK, V2, GREY = "#f2efe8", "#2b2d31", "#e3ded2", "#a9adbb"
ASSY = HERE.parent / "stl" / "assembled"; SMALL = OUT / "stl"
for name, colour, group in [("ring", DARK, "ring"), ("body", WHITE, "body"), ("port_panel", DARK, "ring"), ("ups_slider", WHITE, "ring"),
                            ("neck_plate", WHITE, "neck"), ("coupler", "#7fb3d5", "neck"), ("servo_frame", GREY, "hservo"), ("puck", DARK, "neck"),
                            ("head_frame", WHITE, "head"), ("head_inlay", DARK, "head"), ("head_back", WHITE, "head"),
                            ("camera_bracket", GREY, "head"), ("floor", "#d9d4c8", "floor")]:
    add(name, load(ASSY / f"{name}.stl"), colour, group)
add("arm_a", load(ASSY / "arm_left.stl"), WHITE, "arm_a")         # x < 0: the robot's right arm
add("arm_b", load(ASSY / "arm_right.stl"), WHITE, "arm_b")        # x > 0: the robot's left arm
for name, colour, group in [("ring_gear", "#e6b35a", "neck"), ("pinion", "#e6b35a", "hservo"), ("horn_head", "#f7f7f7", "hservo"),
                            ("servo_head", "#3a6fb0", "hservo"), ("servo_r", "#3a6fb0", "body"), ("servo_l", "#3a6fb0", "body"),
                            ("horn_r", "#f7f7f7", "arm_a"), ("horn_l", "#f7f7f7", "arm_b"), ("keeper", "#c8c2b4", "floor"),
                            ("clamp0", "#c8c2b4", "floor"), ("clamp1", "#c8c2b4", "floor"), ("pca", "#2a8a2a", "floor"),
                            ("screen", "#15171c", "head"), ("fpc", "#c9a74a", "head"), ("usbc_plug", "#3a3d44", "head"),
                            ("coupler4", "#16181c", "head"), ("hdmi_plug", "#2a2d33", "head"), ("eyes", "#4fe3e0", "eyes")]:
    add(name, load(SMALL / f"{name}.stl"), colour, group)

# the Pi 5 on the UPS HAT (E), on its side on the floor (v2's ups_stack): u along the boards, v down, w across
stk_x0, stk_y0, stk_ztop = P["stk"]; plate_t, ups_w0, ups_top, pi_w0, pi_w1 = P["stk_w"]
def sbox(u0, u1, v0, v1, w0, w1): return box([stk_x0 + u0, stk_y0 + w0, stk_ztop - v1], [stk_x0 + u1, stk_y0 + w1, stk_ztop - v0])
def pbox(x0, x1, z0, z1, h0, h1): return sbox(x0, x1, z0, z1, pi_w1 + h0, pi_w1 + h1)
ups_plate = P["ups_plate"]; pv = -(ups_plate[1] - 56) / 2
add("ups_plate", sbox(-(ups_plate[0] - 88) / 2, 88 - (ups_plate[0] - 88) / 2, pv, 56 - pv, 0, plate_t), "#cfe3ea", "stack")
cells = [xf(cyl(21, 0, 70, 32), to_z([1, 0, 0]), [stk_x0 + 9, stk_y0 + b, stk_ztop - a]) for a in (17.25, 38.75) for b in (plate_t + 11.5, ups_w0 - 11.5)]
add("ups_cells", cat(*cells), "#e8742c", "stack")
ku = P["ups_ports"][0][0]; kw, kout, ktr = P["ups_knob"]
add("ups_board", cat(sbox(0, 88, 0, 56, ups_w0, ups_top), *[sbox(c - w / 2, c + w / 2, -0.3, 9.9, ups_top, ups_top + h) for c, w, h in P["ups_ports"]],
                     sbox(ku - kw / 2, ku + kw / 2, -kout, -0.3, ups_top + 1.2, ups_top + 3.4)), "#2a5caa", "stack")
hexd = P["standoff_af"] / np.cos(np.radians(30))
add("ups_standoffs", cat(*[xf(cyl(hexd, 0, ups_w0 - plate_t, 6), to_z([0, 1, 0]), [stk_x0 + u, stk_y0 + plate_t, stk_ztop - v]) for u, v in P["ups_corner_pts"]],
                         *[xf(cyl(hexd, 0, pi_w0 - ups_top, 6), to_z([0, 1, 0]), [stk_x0 + u, stk_y0 + ups_top, stk_ztop - v]) for u, v in P["pi_hole_pts"]]), "#c9a74a", "stack")
add("pi", cat(sbox(0, 85, 0, 56, pi_w0, pi_w1), pbox(7.13, 57.93, 50.02, 55, 0, 8.57),
              pbox(69.5, 88, 2, 54, 0, 15.8), pbox(22.05, 29.55, -0.5, 7.5, 0, 3.4), pbox(35.45, 42.95, -0.5, 7.5, 0, 3.4), pbox(6.7, 15.7, -0.5, 7.5, 0, 3.2)),
    "#1a9a55", "stack")
ch = P["cooler_h"]
add("cooler", cat(pbox(6.5, 64, 9, 49, 0, 3), pbox(7, 27.5, 9, 49, 0, ch), pbox(27.5, 64, 18, 49, 0, ch)), "#8a8f98", "stack")
us, (rx, rz), base_d, base_wall = P["usbc_sock"], P["rear"], P["base_d"], P["base_wall"]
add("usbc_socket", box([rx - us[0] / 2, base_d - base_wall - 0.5 - us[2], rz - us[1] / 2], [rx + us[0] / 2, base_d - base_wall - 0.5, rz + us[1] / 2]), "#333333", "floor")
# speaker, mic and its USB extension (in the body)
(sc, (sw, sd, sh)), (mc, (ml, mt, mw)) = P["spk"], P["mic"]
add("speaker", box([sc[0] - sw / 2, sc[1], sc[2] - sh / 2], [sc[0] + sw / 2, sc[1] + sd, sc[2] + sh / 2]), "#202124", "body")
add("mic", cat(box([mc[0], mc[1], mc[2] - mw / 2], [mc[0] + ml, mc[1] + mt, mc[2] + mw / 2]),
               box(P["mic_ext"], np.add(P["mic_ext"], [32, 9.5, 18]))), "#45474d", "body")
# the camera (v2's board and lens) in the hood
cy, cz, ct, cw = P["cam"]
add("cam_board", cat(box([-cw / 2, cy, cz - cw / 2], [cw / 2, cy + ct, cz + cw / 2]), xf(cyl(14, 0, P["cam_lens_l"]), to_z([0, -1, 0]), [0, cy, cz])), "#8a3b3b", "head")
# the 4xAA box in the head: open tray (lid side backwards), 4 cells, the lid with its switch
(bx, by, bz), (bw, bh, bt) = P["bat_lo"], P["bat"]
w = 1.6
add("aa_tray", cat(box([bx, by, bz], [bx + bw, by + w, bz + bh]), box([bx, by, bz], [bx + w, by + bt - w, bz + bh]), box([bx + bw - w, by, bz], [bx + bw, by + bt - w, bz + bh]),
                   box([bx, by, bz], [bx + bw, by + bt - w, bz + w]), box([bx, by, bz + bh - w], [bx + bw, by + bt - w, bz + bh])), "#18191c", "head")
for k, x in enumerate((-21.8, -7.3, 7.3, 21.8)):
    add(f"aa_cell{k}", cat(xf(cyl(14.3, 0, 48.5, 24), I, [x, by + bt / 2, bz + 10.5]), xf(cyl(5.5, 48.5, 50, 16), I, [x, by + bt / 2, bz + 10.5])), "#3aa655", "head")
add("aa_lid", cat(box([bx, by + bt - w, bz], [bx + bw, by + bt, bz + bh]), dark(box([bx + bw / 2 - 5, by + bt, bz + bh - 18], [bx + bw / 2 + 5, by + bt + 2.5, bz + bh - 8]))), "#18191c", "head")


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


def nut_mesh():   # M3: 5.5 across flats, 2.4 thick, axis z, centred
    return cat(prism(5.5 / np.cos(np.radians(30)), 5.5 / np.cos(np.radians(30)), -1.2, 1.2, 6, np.radians(30)), dark(cyl(3.2, -1.25, 1.25, 16)))


TOOL = {"camera": "lkey", "back": "lkey"}                             # else: a driver by the head type
def screw_group(step, what, pos):   # the assembly a screw moves with
    if step == "arms": return "arm_a" if pos[0] < 0 else "arm_b"
    if step == "head_servo": return "neck" if what.startswith("M3") else "hservo"
    if step == "stack": return "floor" if what.startswith("M3") else "stack"
    return {"ring_gear": "neck", "puck": "neck", "plate_in": "neck", "arm_servos": "body", "pca": "floor", "floor_in": "floor",
            "panel": "ring", "head_on": "head", "screen": "head", "camera": "head", "back": "head"}[step]
LEVER = {"camera": lambda p: [0, 1, 0], "back": lambda p: [np.sign(p[0]), 0, 0]}   # where an L-key's long leg points
screw_list = []
for k, (step, what, d, l, head, pos, v) in enumerate(screws):
    v = np.asarray(v, float); v /= np.linalg.norm(v); R = to_z(v)
    name = f"s{k:02d}_{step}"
    tool = TOOL.get(step, "hexdriver" if head == "btn" else "phdriver")
    lever = [float(c) for c in LEVER[step](pos)] if step in LEVER else None
    screw_list.append(name)
    add(name, xf(screw_mesh(d, l, head), R, pos), "#3b4049", screw_group(step, what, pos), kind="screw", step=step, screw=what, pos=pos, dir=v.round(4).tolist(), l=l,
        tool=tool, lever=lever, head_h=(1.65 * d / 3 if head == "btn" else 0.62 * d))
for k, (step, c, axis) in enumerate(nuts):
    add(f"n{k:02d}_{step}", xf(nut_mesh(), to_z(axis), c), "#9aa0a8", {"ring_nuts": "ring", "plate_nuts": "body"}[step], kind="nut", step=step, pos=c, dir=[0, 0, -1])
# the tools: local frame, the tip at the origin, the tool behind it (z < 0); index.html turns +z onto the screw's axis
add("tool_hexdriver", cat(prism(2.3, 2.3, 0, -42, 6), cyl(5.5, -42, -48, 16), prism(17, 18, -48, -104, 8, 0, 2), prism(18, 12, -104, -108, 8)), "#e08a1e", "tool", kind="tool")
add("tool_phdriver", cat(prism(1.2, 3, 0, -4, 8), cyl(3, -4, -46, 12), cyl(6, -46, -52, 16), prism(17, 18, -52, -108, 8, 0, 2), prism(18, 12, -108, -112, 8)), "#c0392b", "tool", kind="tool")
add("tool_lkey", cat(prism(2.3, 2.3, 0, -20, 6), ball([0, 0, -20], 1.25, 8, 6), xf(prism(2.3, 2.3, 0, 42, 6), to_z([1, 0, 0]), [0, 0, -20])), "#3c4250", "tool", kind="tool")

# ---------------------------------------------------------------- cables (they grow along their routes)
LABEL_AT_END = {"i2c", "aa", "hdmi_head", "ribbon", "usbc_head", "camera_head", "aa_head", "servo_head"}
for name, step, group, colour, d, label, route, plugs in cables:
    t = tube(route, d / 2)
    for lo, hi in plugs:
        b = box(lo, hi); end = np.linalg.norm(np.add(lo, hi) / 2 - route[-1]) < np.linalg.norm(np.add(lo, hi) / 2 - route[0])
        t = np.concatenate([t, with_s(b, 1.0 if end else 0.0)])
    add(f"c_{name}", t, colour, "head" if group == "head" else "floor" if group == "floor" else "wires", kind="cable", step=step,
        label=label.replace("->", "→"), label_at=[round(float(c), 1) for c in route[-1 if name in LABEL_AT_END else 0]])

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
(OUT / "scene.json").write_text(json.dumps({"sh": P["sh"], "tab_hinge": P["tab_hinge"], "slider_travel": P["slider_travel"]}))   # for index.html
kinds = {}
for m in manifest: kinds[m.get("kind", "part")] = kinds.get(m.get("kind", "part"), 0) + 1
print(kinds, "triangles", sum(m["tris"] for m in manifest))
