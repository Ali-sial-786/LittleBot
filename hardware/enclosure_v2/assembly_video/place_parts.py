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
glass_y, lcd_x0, lcd_z0, pi_x0, pi_z0, pi_top_y, pi_bot_y = 3.5, -45.075, 40.0, -44.595, 40.4, 26.86, 28.26

def load(name): return read_stl(Path(__file__).resolve().parent.parent / "stl" / f"{name}.stl").astype(np.float64)
def xf(tris, A=I, t=(0, 0, 0)): return tris @ np.asarray(A).T + np.asarray(t, float)

parts = []   # (name, tris, colour, group)
def add(name, tris, colour, group): parts.append((name, tris, colour, group))

# --- printed parts: L = inverse(print_part) of the STL, then the assembly placement ---
add("base", xf(load("base") - [0, 0, 72], MZ), "#f2efe8", "base")
add("base_floor", load("base_floor") + [0, 0, base_z0], "#d9d4c8", "base")
add("shelf", load("shelf") + [0, 0, base_z0 + base_floor_t + bat_h], "#bbbbbb", "base")
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
add("pi_carrier", xf(load("pi_carrier") - [0, 0, 35.26], Rx(90)) + head, "#aaaabb", "stack")
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
add("pi", cat(pbox(0, 85, 0, 56, -1.4, 0), pbox(7.13, 57.93, 50.02, 55, 0, 2.5)) + head, "#1a9a55", "pi")
add("pi_ports", cat(pbox(69.5, 88, 2, 54, 0, 15.8), pbox(22.05, 29.55, -0.5, 7.5, 0, 3.4), pbox(35.45, 42.95, -0.5, 7.5, 0, 3.4),
                    pbox(6.7, 15.7, -0.5, 7.5, 0, 3.2)) + head, "#c0c0c8", "pi")
add("cooler", cat(pbox(6.5, 64, 9, 49, 0, 3), pbox(7, 27.5, 9, 49, 0, 12), pbox(27.5, 64, 18, 49, 0, 12)) + head, "#8a8f98", "pi")
add("hdmi_adapter", box(pi_x0 + 20.5, pi_x0 + 46.5, glass_y + 5, pi_top_y, lcd_z0 - 14, pi_z0 - 0.6) + head, "#222222", "screen")
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
add("pca9685", box(-31.115, 31.115, 40, 65.4, 10 + 3 + 30 + 2.5 + 4, 10 + 3 + 30 + 2.5 + 4 + 12), "#2a8a2a", "base")

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
