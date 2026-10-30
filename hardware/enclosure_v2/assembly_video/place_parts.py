"""Put every printed part (stl/, print orientation) where it sits on the assembled robot, plus box dummies
for the electronics, all in robot coordinates (mm, z up, front of the base at y = 0). Transforms are the
inverse of print_part() followed by assembly() from littlebot_v2.scad.

The derived positions below are copied from littlebot_v2.scad (2026-10-29): if you change a dimension
there, update them too (OpenSCAD's echo() prints most of them)."""
import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stl_tools import read_stl

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
def Rx(a):
    c, s = np.cos(np.radians(a)), np.sin(np.radians(a)); return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
def Ry(a):
    c, s = np.cos(np.radians(a)), np.sin(np.radians(a)); return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
def Rz(a):
    c, s = np.cos(np.radians(a)), np.sin(np.radians(a)); return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
def rot(a, b, c): return Rz(c) @ Ry(b) @ Rx(a)          # OpenSCAD rotate([a, b, c])
MZ = np.diag([1.0, 1, -1]); MX = np.diag([-1.0, 1, 1])
I = np.eye(3)

# derived values (littlebot_v2.scad "Derived")
base_z0, base_floor_t, bat_h, base_w, base_d = 10, 3, 30, 176, 152
torso_y0, torso_z0, torso_h, torso_d, torso_wall = 27, 72, 125, 98, 2.5
neck_yt, gear_z0, pinion_yt, hs_spline_z, hs_tab_top = 62, 115.6, 24.35, 117.8, 103.7
head = np.array([0, 29, 207.8]); torso = np.array([0, torso_y0, torso_z0])
glass_y, lcd_x0, lcd_z0 = 3.5, -45.075, 40.0
# the Pi + UPS stack in the base (since 2026-10-30)
floor_low = 3
ups_plate = (90, 58, 3); ups_body, ups_pi_gap = 46, 10
stk_x0, stk_y0 = -40, 50
stk_ztop = floor_low + 2.4 + 2 + (ups_plate[1] + 56) / 2
stk_ups_w0 = ups_plate[2] + ups_body; stk_pi_w0 = stk_ups_w0 + 1.6 + ups_pi_gap; stk_pi_w1 = stk_pi_w0 + 1.4

def load(name): return read_stl(Path(__file__).resolve().parent.parent / "stl" / f"{name}.stl").astype(np.float64)
def xf(tris, A=I, t=(0, 0, 0)): return tris @ np.asarray(A).T + np.asarray(t, float)

parts = []   # (name, tris, colour, group)
def add(name, tris, colour, group): parts.append((name, tris, colour, group))

# --- printed parts: L = inverse(print_part) of the STL, then the assembly placement ---
add("base", xf(load("base") - [0, 0, 72], MZ), "#f2efe8", "base")
add("base_floor", load("base_floor") + [0, 0, floor_low], "#d9d4c8", "floor")
for s, nm in ((-1, "well_cover_left"), (1, "well_cover_right")):
    add(nm, xf(load("well_cover"), rot(90, 0, -90 if s > 0 else 90), (s * base_w / 2, 76, 0)), "#e8e4da", "base")
add("front_plate", xf(xf(load("front_blank"), Rz(180)), Rx(-90), (0, -2, 40)), "#cfe8f5", "base")
add("rear_plate", xf(load("rear_usb"), Rx(90), (14, base_d + 2, 36)), "#e8e4da", "base")
add("torso", xf(load("torso") - [0, 0, torso_h], MZ) + torso, "#f7f4ee", "torso")
add("torso_back", xf(load("torso_back") - [0, 0, 4.1], Rx(90)) + torso + [-45.7, torso_d - torso_wall, 0], "#e9e5dc", "torso")
add("coupler", xf(load("coupler") - [0, 0, 10.8], MZ) + torso + [0, neck_yt, torso_h], "#7fb3d5", "torso")
add("ring_gear", load("ring_gear") + torso + [0, neck_yt, gear_z0], "#e6b35a", "torso")
add("pinion", xf(load("pinion"), Rz(7.5)) + torso + [0, pinion_yt, gear_z0], "#e6b35a", "torso")
add("servo_frame", xf(load("servo_frame") - [0, 0, 3], MZ) + torso + [0, pinion_yt, hs_tab_top - 2.5 - 3], "#aaaabb", "torso")
add("head", xf(load("head"), Rx(-90)) + head, "#f7f4ee", "head")
add("head_back", xf(load("head_back") - [0, 0, 120], Rx(90)) + head, "#e9e5dc", "head")
add("visor", xf(xf(load("visor") - [0, 0, 1], Rx(-90)) + [0, 0, 68]) + head, "#151a22", "head")
add("screen_clamp", xf(load("screen_clamp") - [0, 0, 35.26], Rx(90)) + head, "#aaaabb", "head")
ear_r = xf(load("ear_right") - [0, 0, 87], Ry(-90)) + [0, 62, 66]
add("ear_right", ear_r + head, "#9fd3f5", "head")
add("ear_left", xf(ear_r, MX)[:, ::-1] + head, "#9fd3f5", "head")      # mirrored: flip winding back
add("camera_pod", xf(load("camera_pod") - [0, 0, 172], MZ) + head, "#f7f4ee", "head")
add("camera_bracket", xf(load("camera"), Rx(-90)) + head + [0, 23.5, 150], "#aaaabb", "head")
M1 = np.array([[0, 0, -1], [0, -1, 0], [-1, 0, 0]]); M2 = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])
add("arm_left", xf(xf(load("arm_left"), M1), Rx(-8), (-69, 72, 172)), "#f2efe8", "arms")
add("arm_right", xf(xf(load("arm_right"), M2), Rx(-8), (69, 72, 172)), "#f2efe8", "arms")

# --- electronics: boxes and cylinders from the dummies ---
def box(x0, x1, y0, y1, z0, z1):
    v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], float)
    f = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return v[np.array(f)]
def cyl(d, h, n=24):   # along +z from z = 0
    a = np.linspace(0, 2 * np.pi, n + 1); r = d / 2; t = []
    for i in range(n):
        p0, p1 = (r * np.cos(a[i]), r * np.sin(a[i])), (r * np.cos(a[i + 1]), r * np.sin(a[i + 1]))
        t += [[(p0[0], p0[1], 0), (p1[0], p1[1], 0), (p1[0], p1[1], h)], [(p0[0], p0[1], 0), (p1[0], p1[1], h), (p0[0], p0[1], h)],
              [(0, 0, 0), (p1[0], p1[1], 0), (p0[0], p0[1], 0)], [(0, 0, h), (p0[0], p0[1], h), (p1[0], p1[1], h)]]
    return np.array(t, float)
def lbox(x0, x1, z0, z1, y0, y1): return box(lcd_x0 + x0, lcd_x0 + x1, glass_y + y0, glass_y + y1, lcd_z0 + z0, lcd_z0 + z1)
def pbox(x0, x1, z0, z1, h0, h1): return box(pi_x0 + x0, pi_x0 + x1, pi_top_y - h1, pi_top_y - h0, pi_z0 + z0, pi_z0 + z1)
cat = lambda *a: np.concatenate(a)

add("screen_glass", lbox(0, 85.51, 0, 56, 0, 5.70) + head, "#0a0a0a", "screen")
add("screen_pcb", cat(lbox(0.4, 85.51, 0, 56, 5.70, 7.32), lbox(8.88 - 1.27, 8.88 - 1.27 + 33.02, 51.64 - 1.27, 51.64 + 3.81, 7.32, 7.32 + 13.54),
                      lbox(33 - 7.5, 33 + 7.5, 1.7, 10.65, 7.32, 14.02)) + head, "#1f6f3a", "screen")
add("camera_board", box(-16, 16, 26, 27.6, 134, 166) + head, "#884444", "head")
def servo(): return cat(box(-6.1, -6.1 + 22.8, -6.2, 6.2, -32.5, -32.5 + 22.7), box(5.3 - 16.05, 5.3 + 16.05, -6.2, 6.2, -16.6, -14.1),
                        cyl(12, 5.7) + [0, 0, -9.8], cyl(4.8, 4.1) + [0, 0, -4.1])
add("head_servo", servo() + torso + [0, pinion_yt, hs_spline_z], "#3a6fb0", "torso")
for s, nm in ((-1, "arm_servo_left"), (1, "arm_servo_right")):
    add(nm, xf(servo(), rot(90 if s > 0 else -90, 0, 90)) + torso + [s * 70, 45, 100], "#3a6fb0", "torso")
add("speaker", box(-42, 42, 6, 38, 41, 84) + torso, "#202020", "torso")
add("mic", box(-33.7, -33.7 + 22.2, 4.0, 11.0, 90.85, 109.15) + torso, "#444444", "torso")
for sx in (-1, 1):   # eyes on the screen (only shown when the robot is finished)
    e = cyl(1, 0.4, 48) * [15, 22, 1]
    add(f"eye_{'left' if sx < 0 else 'right'}", xf(e, Rx(90)) + head + [sx * 17, 3.4, 70], "#8fe8ff", "eyes")
# the Pi 5 on the UPS HAT (E), on its side in the base (sbox / pi_box / ups_stack in littlebot_v2.scad)
def sbox(u0, u1, v0, v1, w0, w1):
    return box(stk_x0 + u0, stk_x0 + u1, stk_y0 + w0, stk_y0 + w1, stk_ztop - v1, stk_ztop - v0)
def pi_box(x0, x1, z0, z1, h0, h1): return sbox(x0, x1, z0, z1, stk_pi_w1 + h0, stk_pi_w1 + h1)
pv = -(ups_plate[1] - 56) / 2
add("ups_plate", sbox(-(ups_plate[0] - 88) / 2, 88 - (ups_plate[0] - 88) / 2, pv, 56 - pv, 0, ups_plate[2]), "#cfe3ea", "floor")
cells = []
for a in (14, 35):                     # 2 x 2 cells, 70 mm long, along the boards
    for b in (ups_plate[2] + 11.5, ups_plate[2] + 34.5):
        c = xf(cyl(21, 70, 32), Ry(90)) + [stk_x0 + 9, stk_y0 + b, stk_ztop - a]
        cells.append(c)
add("ups_cells", cat(*cells), "#e8742c", "floor")
add("ups_board", cat(sbox(0, 88, 0, 56, stk_ups_w0, stk_ups_w0 + 1.6),
                     *[sbox(c - w / 2, c + w / 2, -0.8, 9, stk_ups_w0 + 1.6, stk_ups_w0 + 1.6 + h) for c, w, h in ((12.5, 9, 4.5), (28, 13.5, 7), (42, 9, 3.4))]),
    "#2a5caa", "floor")
add("ups_standoffs", cat(*[xf(cyl(5.8, stk_pi_w0 - ups_plate[2], 6), Rx(-90)) + [stk_x0 + u, stk_y0 + ups_plate[2], stk_ztop - v]
                           for u, v in ((3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5))]), "#c9a74a", "floor")
add("pi", cat(sbox(0, 85, 0, 56, stk_pi_w0, stk_pi_w1), pi_box(7.13, 57.93, 50.02, 55, 0, 8.57)), "#1a9a55", "floor")
add("pi_ports", cat(pi_box(69.5, 88, 2, 54, 0, 15.8), pi_box(22.05, 29.55, -0.5, 7.5, 0, 3.4), pi_box(35.45, 42.95, -0.5, 7.5, 0, 3.4),
                    pi_box(6.7, 15.7, -0.5, 7.5, 0, 3.2)), "#c0c0c8", "floor")
add("cooler", cat(pi_box(6.5, 64, 9, 49, 0, 3), pi_box(7, 27.5, 9, 49, 0, 12), pi_box(27.5, 64, 18, 49, 0, 12)), "#8a8f98", "floor")
zf = floor_low + 2.4
add("pca9685", box(-31.115, 31.115, 10, 35.4, zf + 4, zf + 4 + 12), "#2a8a2a", "floor")
add("usbc_socket", box(14 - 6.5, 14 + 6.5, 152 - 2.6 - 0.5 - 18, 152 - 2.6 - 0.5, 36 - 3.5, 36 + 3.5), "#333333", "floor")

manifest = []
for name, tris, colour, group in parts:
    t = tris.astype(np.float32)
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]); ln = np.linalg.norm(n, axis=1, keepdims=True); n = n / np.where(ln > 0, ln, 1)
    data = np.concatenate([t.reshape(-1, 3), np.repeat(n, 3, axis=0)], axis=1).astype(np.float32)   # x y z nx ny nz per vertex
    (OUT / f"{name}.bin").write_bytes(data.tobytes())
    lo, hi = t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)
    manifest.append({"name": name, "file": f"meshes/{name}.bin", "colour": colour, "group": group, "tris": len(t),
                     "lo": [round(float(v), 1) for v in lo], "hi": [round(float(v), 1) for v in hi]})
(OUT.parent / "manifest.json").write_text(json.dumps(manifest, indent=1))
for m in manifest: print(f"{m['name']:<18}{m['tris']:>7}  x {m['lo'][0]:7.1f}..{m['hi'][0]:7.1f}  y {m['lo'][1]:7.1f}..{m['hi'][1]:7.1f}  z {m['lo'][2]:7.1f}..{m['hi'][2]:7.1f}")
print("total triangles", sum(m["tris"] for m in manifest))
