// What the v5 assembly video needs from the model (make_video.sh):
//   part = "hw5"            echoes hw5 = [screws, nuts, cables, params, glue]: every screw (where its head bears, the way it
//                           goes in, the tool that drives it), every nut (and the way it goes into its slot), every cable's
//                           route, and the positions the electronics sit at
//   part = "vid5", which=.. one small part where it sits in the robot (the assembled STLs lump some together)
// Robot coordinates as the model: x across (+x = the viewer's right = the robot's left), y front -> back, z up.
include <../littlebot_v5.scad>
part = "hw5";
which = "";

// --- frames (as v4's video) ---
function Ry(a) = [[cos(a), 0, sin(a)], [0, 1, 0], [-sin(a), 0, cos(a)]];
function Rx(a) = [[1, 0, 0], [0, cos(a), -sin(a)], [0, sin(a), cos(a)]];
function Rz(a) = [[cos(a), -sin(a), 0], [sin(a), cos(a), 0], [0, 0, 1]];
function mx(s, q) = [s * q[0], q[1], q[2]];
function sh_pt(s, p) = mx(s, sh_c + Ry(-sh_tilt) * p);                   // place_sh(s)
function sh_dir(s, v) = mx(s, Ry(-sh_tilt) * v);
function arm_pt(s, p) = sh_pt(s, Rx(-arm_angle) * p);                     // place_arm3(s, arm_angle)
function arm_dir(s, v) = sh_dir(s, Rx(-arm_angle) * v);
function sv_arm(p) = [p[2] + sv_spline_top, p[0], p[1]];                  // arm_servo3: a servo point -> the shoulder's frame
function head_bot(Y, x) = let (k = k4(Y - Yc)) z4c - H4[1] * k * pow(max(0, 1 - pow(abs(x) / (H4[0] * k), H4_n)), 1 / H4_n);   // the head's underside
function nk(o, z) = [o[0], neck_y + o[1], z];                              // a point in the neck's bore
function stk5(u, v, w) = stk(u, v, w) + S5;                               // the Pi + UPS, where v5 has it
function wall_pt(j, s, z, d) = let (p = js_pt(j, s), n = js_n(j, s)) [p[0] + d * n[0], p[1] + d * n[1], z];   // d along the wall's outward normal
function wall_n(j, s) = let (n = js_n(j, s)) [n[0], n[1], 0];
chg_o = [chg_x, tub_by(chg_x), chg_z]; chg_R = Rz(tub_lean(chg_x));
function chg_pt(p) = chg_o + chg_R * p;                                   // place_chg(): +y out through the back wall
function pol(r, a, z) = [r * cos(a), neck_y + r * sin(a), z];             // round the neck's axis

// --- screws: [step, what, d, length, head, where the head bears, the way it goes in]. head "btn" = an M3 / M2.5 button
// head (hex socket: 2 mm key), "pan" = a Phillips pan head (screwdriver). Listed in the order they go in.
function hw5_screws() = concat(
    // wheel pods, at the bench: the motor by its face, from the wheel's side; the wheel's own screw into the coupler
    [for (s = [1, -1], k = [-1, 1]) ["pod_bench", "M3 × 8", 3, 8, "btn", [s * (xf + pod_t), yw + k * mot_holes / 2, az], [-s, 0, 0]]],
    [for (s = [1, -1]) ["pod_bench", "wheel’s own screw", 3, 10, "pan", [s * (xf + 44.5), yw, az], [-s, 0, 0]]],
    // casters: their own #2-56 screws up through their base into the floor (the tub upside down), nuts on top
    [for (y = cas_y, s = [-1, 1]) ["casters", "#2-56 × 7/16 (caster’s own)", 2.2, 11.1, "pan", [s * 7.75, y, cas_seat - 1.5], [0, 0, 1]]],
    // the pods down onto the tub's ridges
    [for (s = [1, -1], p = pod_scr) ["pods_in", "M3 × 12", 3, 12, "btn", [s * p[0], yw + p[1] * (pod_fl[0] + pod_fl[1] / 2), pod_fl[2] + 3.1], [0, 0, -1]]],
    // v4's keeper over the Pi + UPS
    [for (x = keeper_x) ["stack", "M3 × 20", 3, 20, "btn", [x, stk_yc, keeper_mouth(ups_plate_t, ups_plate[1]) + keeper_t - 1.8] + S5, [0, 0, -1]]],
    // the deck: the motor driver onto its 4 posts (at the bench), then the deck onto the tub's 4 posts
    [for (sx = [-1, 1], sy = [-1, 1]) ["deck_bench", "M3 × 8", 3, 8, "btn", [drv_c[0] + sx * drv_holes[0] / 2, drv_c[1] + sy * drv_holes[1] / 2, deck_z + deck_t + 4 + 1.6], [0, 0, -1]]],
    [for (p = deck_posts) ["deck_in", "M3 × 8", 3, 8, "btn", [p[0], p[1], deck_z + deck_t], [0, 0, -1]]],
    // the drawer: through the door's lower corners into the 2 posts behind it
    [for (s = [-1, 1]) ["drawer", "M3 × 10", 3, 10, "btn", [s * (door_hw - 2.6), tub_fy(door_hw, 0) + 0.9, tray_z + 6], [0, 1, 0]]],
    // the body: arm servos (from v4), the ReSpeaker onto its holder
    [for (s = [-1, 1]) for (hy = [sv_hole_a, sv_hole_b]) ["arm_servos", "servo screws", 2, 8, "pan", sh_pt(s, [sv_tab_top, hy, 0]), sh_dir(s, [-1, 0, 0])]],
    [for (h = rsl_holes) ["mic", "M2 × 8", rsl_scr[0], rsl_scr[1], "pan", rsl_pt([h[0], h[1], rsl[2]]), -rsl_n]],
    // the body onto the tub: through the rim (heads in their flat seats) into the tongue's nuts
    [for (j = js, s = [-1, 1]) ["body_on", "M3 × 10", 3, 10, "btn", wall_pt(j, s, js_z, -1.2), -wall_n(j, s)]],
    // the neck, at the bench: the ring gear onto the hub's flange (from below), the black neck onto the hub
    [for (a = ring_screw_angles) ["ring_gear", "M3 × 12", 3, 12, "btn", torso_pt([ring_screw_r * cos(a), neck_yt + ring_screw_r * sin(a), gear_z0 + 0.6 + 1.8]), [0, 0, 1]]],
    [for (a = neck_scr) ["black_neck", "M3 × 12", 3, 12, "btn", pol(head_screw_r, a, hub_top + 2.8), [0, 0, -1]]],
    // the head servo onto its frame, the frame's collars round the cap's pillars
    [for (hx = [sv_hole_a, sv_hole_b]) ["head_servo", "servo screws", 2, 8, "pan", torso_pt([hx, pinion_yt, hs_tab_top]), [0, 0, -1]]],
    [for (k = [0, 1]) let (o = k == 0 ? 1 : -1) ["head_servo", "M3 × 10", 3, 10, "btn",
        torso_pt([frame_pillar_x[k] - o * 4.5, pinion_yt + 7.6, hs_tab_top - sv_tab_t + collar_h/2]), [o, 0, 0]]],
    // the cap onto the collar: straight down, heads sunk; they cut their own thread
    [for (p = cap_scr) ["cap_on", "M3 × 12", 3, 12, "btn", [p[0], p[1], zc0 + 2.8], [0, 0, -1]]],
    // the head (v4's): onto the black neck, into the hub's nuts; then as v4
    [for (a = head4_scr) ["head_on", "M3 × 20", 3, 20, "btn", pol(head_screw_r, a, hd_z0 + 4), [0, 0, -1]]],
    [for (h = scr_holes) let (q = hole_xz(h)) ["screen", "M2.5 × 6", 2.5, 6, "pan", [q[0], scr_back_y + scr_standoff_h + 2, q[1]], [0, -1, 0]]],
    [for (s = [-1, 1]) ["camera", "M3 × 12", 3, 12, "btn", [s * cam4_scr_x, y4f + cam_board_y + cam_pivot_dy, cam4_z], [-s, 0, 0]]],
    [for (x = cov4_scr) ["back", "M3 × 12", 3, 12, "btn", [x, y4f + H4_split + 5, head_bot(H4_split + 5, x)], [0, 0, 1]]],
    [for (s = [-1, 1]) ["arms", "spline screw", 2.5, 5, "pan", arm_pt(s, [arm_gap3 + horn_plate_t, 0, 0]), arm_dir(s, [-1, 0, 0])]]
);

// --- nuts: [step, centre, the nut's axis, the way it moves into its slot, across flats, thickness]
function hw5_nuts() = concat(
    [for (y = cas_y, s = [-1, 1]) ["casters", [s * 7.75, y, cas_top + 0.8], [0, 0, 1], [0, 0, -1], 4.8, 1.6]],                       // #2-56, on the seats
    // the tongue's 8: each slot is open at the tongue's lower edge (with the body upside down they drop in)
    [for (j = js, s = [-1, 1]) ["body_nuts", wall_pt(j, s, js_z, -(tw + tg[0] + 3 + 0.2 + nut_t / 2)), wall_n(j, s), [0, 0, 1], nut_af, nut_t]],
    // the hub's 3: they slide into its side slots, flat
    [for (a = head4_scr) ["black_neck", pol(head_screw_r, a, hub_nut_z + 0.2 + nut_t / 2), [0, 0, 1], [-cos(a), -sin(a), 0], nut_af, nut_t]]
);

// --- cables: [name, step, group, colour, diameter, label, route, plugs (boxes [lo, hi] at its ends)].
// The cables up the neck are two pieces each, joined in the neck's bore 15 mm above the head's floor: the body piece
// waits there for the head, the head piece goes on with the head.
join_z = hd_z0 + 15;
nk_off = [["hdmi", [-4.5, 2.5]], ["usbc", [4.5, 3]], ["camera", [4.5, -3.5]]];
function nko(n) = nk_off[search([n], nk_off)[0]][1];
function usb_box5(v, h) = [stk5(pi_l + 3, v + 7, stk_pi_w1 + h - 4), stk5(pi_l + 21, v - 7, stk_pi_w1 + h + 4)];   // a USB-A plug in the Pi
function usb_end5(v, h) = stk5(pi_l + 21, v, stk_pi_w1 + h);
// the servo power board (perfboard on the deck): 3 servo headers along its back edge, plugs standing on them
deck_top = deck_z + deck_t;
function perf_ch(k) = [perf_c[0] - 8 + 8 * k, perf_c[1] + perf[1] / 2 - 9, deck_top + 0.2 + 1.6];
function servo_plug5(k) = [perf_ch(k) - [1.3, 4, 0], perf_ch(k) + [1.3, 4, 14]];
hs_exit = torso_pt([sv_l - sv_shaft_off + 7.5, pinion_yt, hs_spline_z - sv_spline_h + 3.5]);
function as_exit(s) = sh_pt(s, sv_arm([sv_l - sv_shaft_off + 7.5, 0, -sv_spline_h + 3.5]));
// the screen's plugs (screen_plugs3): ends where their cables leave them
xe = -scr[0] / 2; zh = scr_pt([0, scr_ports[3]])[1]; zu = scr_pt([0, scr_ports[2]])[1];
fpc_lo = [xe - fpc_plug[0], g3_y + 1, zh - fpc_plug[1]/2]; usbc_lo = [xe - usbc_l[0], g3_y + 1, zu - usbc_l[1]/2];
cam_sock = [cam_conn[0], y4f + cam_board_y + cam_pcb_t, cam4_z - 16 + cam_conn[1]];
rsl_plug_end = rsl_pt([112.9, 27.8, 3]);                                  // where the cable leaves its plug (rsl_plug)
// the base's ends: the UPS's USB-C (POWER), the charging socket's back, the motors' tails, the driver's terminals,
// the RP2040's pins, the AA holder's leads, the drive switch's tabs
ups_c = stk5(ups_ports[2][0], 0, stk_ups_w0 + ups_pcb_t + ups_ports[2][2] / 2);
chg_back = chg_pt([0, -usbc_sock[2] - 1, 0]);
function mot_tail(s) = [s * (xf - mot[1] + 1), yw, az + 8];
drv_term = [drv_c[0], drv_c[1] + drv[1] / 2 - 4, deck_top + 4 + 1.6 + 9];
bb_pin = [bb_c[0] - 30, bb_c[1] + 14, deck_top + bb[2]];
aa_lead = [aa[0] / 2 - 4, aa_y0 + aa[1] - 6, tray_z + 2 + aa[2]];
sw_back = let (p = js_pt(sw_at, 1), n = js_n(sw_at, 1)) [p[0] - (sw_len + 2) * n[0], p[1] - (sw_len + 2) * n[1], sw_z];
lead_gap = [-52, 88];                                                    // where leads come up past the deck, beside the Pi's mount
// The body's cables are plugged in with the body held over the tub (H_body up), and the head servo onto its extension
// with the cap held over the body (H_cap up): there's no reaching the Pi or the deck through the top once the body is on.
// Each such cable has a "_h" version, its first n points (the body's or the cap's end) raised; the video shows it while
// the part hovers, then the cable in place once the part is down.
H_body = 150; H_cap = 60;
function hov(r, n, h) = [for (i = [0:len(r) - 1]) i < n ? r[i] + [0, 0, h] : r[i]];
spk_route = [[spk_w/2 + 1.6, spk3_c[1] + spk_d/2, spk3_c[2] + spk_h/2 - 4], [62, 30, 128], [84, 90, 108], [84, 128, 86], [76, 150, 50], usb_end5(47, 4) + [4, 0, 0], usb_end5(47, 4)];
mic_route = [rsl_plug_end, rsl_plug_end + [2, 8, -6], [70, 52, 140], [90, 100, 112], [88, 134, 84], [80, 156, 58], usb_end5(47, 12) + [4, 0, 0], usb_end5(47, 12)];
function arm_route(s, k) = [as_exit(s), [s * 56, 104, 184], [s * 30, 104, 130], [perf_ch(k)[0] + s * 2, 90, 104], perf_ch(k) + [0, 0, 26], perf_ch(k) + [0, 0, 14]];
ext_top = [16, 66, 266];                                                  // the head servo's extension: its end, up out of the body's top
hs_route = [hs_exit, [28, 62, 196], [perf_ch(0)[0] + 4, 92, 130], [perf_ch(0)[0] + 2, 90, 104], perf_ch(0) + [0, 0, 26], perf_ch(0) + [0, 0, 14]];
function hw5_cables() = let (hdmi0 = stk5(25.8, -0.5, stk_pi_w1 + 1.7), pv = perf_c + [-perf[0] / 2 + 6, -perf[1] / 2 + 8]) [
    // the base
    ["charging", "charge", "wires", "#2b2b2b", 4, "UPS USB-C (POWER) → charging socket",
        [ups_c + [0, 0, 18], ups_c + [0, 0, 26], [-10, 150, 104], [-58, 152, 96], [-62, 148, 60], chg_pt([0, -34, 0]), chg_pt([0, -usbc_sock[2] - 4, 0]), chg_back],
        [[ups_c - [4.5, 2, 0], ups_c + [4.5, 2, 18]]]],
    ["motor_l", "deck_in", "wires", "#c0392b", 3, "motors → motor driver",
        [mot_tail(1), [6, 80, 50], [lead_gap[0], 80, 52], [lead_gap[0], lead_gap[1], 60], [lead_gap[0], lead_gap[1], deck_top + 22], drv_term + [6, 4, 8], drv_term + [6, 0, 0]], []],
    ["motor_r", "deck_in", "wires", "#e74c3c", 3, "",
        [mot_tail(-1), [-6, 78, 48], [lead_gap[0] + 3, 78, 50], [lead_gap[0] + 3, lead_gap[1] - 2, 60], [lead_gap[0] + 3, lead_gap[1] - 2, deck_top + 20], drv_term + [-6, 4, 8], drv_term + [-6, 0, 0]], []],
    ["enc_l", "deck_in", "wires", "#2e86de", 2.6, "encoders → RP2040",
        [mot_tail(1) + [0, 4, 0], [10, 82, 52], [-lead_gap[0], 82, 54], [-lead_gap[0], lead_gap[1], 60], [-lead_gap[0], lead_gap[1], deck_top + 24], bb_pin + [4, 4, 14], bb_pin + [4, 0, 0]], []],
    ["enc_r", "deck_in", "wires", "#5dade2", 2.6, "",
        [mot_tail(-1) + [0, 4, 0], [-4, 84, 50], [-lead_gap[0] - 3, 84, 52], [-lead_gap[0] - 3, lead_gap[1] - 2, 60], [-lead_gap[0] - 3, lead_gap[1] - 2, deck_top + 22], bb_pin + [-4, 4, 12], bb_pin + [-4, 0, 0]], []],
    ["battery", "switch", "wires", "#c0392b", 3, "lead with a 5 A fuse → the drawer's bay",
        [sw_back, sw_back + [0, 0, 4], [sw_back[0] - 4, sw_back[1] + 14, sw_z + 8], [aa[0] / 2 + 8, 30, tray_z + 26], aa_lead + [0, 0, 12], aa_lead + [0, 0, 6]],
        [[aa_lead + [-3, -2.5, 0], aa_lead + [3, 2.5, 6]]]],
    ["switched", "deck_in", "wires", "#1d1d1d", 3, "switch → deck (driver, servo power)",
        [sw_back + [0, 0, -4], sw_back + [-6, 6, -6], [40, 6, 52], [6, -7, 58], [6, -7, deck_top + 12], [pv[0], pv[1] - 4, deck_top + 14], [pv[0], pv[1], deck_top + 3.2]], []],
    // the body's: speaker and ReSpeaker into the Pi's USB ports, the arm servos onto the servo power board
    ["speaker_h", "body_on", "wires", "#16a085", 3.5, "speaker → Pi USB", hov(spk_route, 3, H_body), [usb_box5(47, 4)]],
    ["mic_h", "body_on", "wires", "#e67e22", 3.5, "ReSpeaker USB-C → Pi USB", hov(mic_route, 4, H_body), [usb_box5(47, 12)]],
    ["servo_left_h", "body_on", "wires", "#8d6e63", 2.4, "left arm servo → servo power board", hov(arm_route(1, 1), 3, H_body), [servo_plug5(1)]],
    ["servo_right_h", "body_on", "wires", "#8d6e63", 2.4, "right arm servo → servo power board", hov(arm_route(-1, 2), 3, H_body), [servo_plug5(2)]],
    ["speaker", "body_on", "wires", "#16a085", 3.5, "", spk_route, [usb_box5(47, 4)]],
    ["mic", "body_on", "wires", "#e67e22", 3.5, "", mic_route, [usb_box5(47, 12)]],
    ["servo_left", "body_on", "wires", "#8d6e63", 2.4, "", arm_route(1, 1), [servo_plug5(1)]],
    ["servo_right", "body_on", "wires", "#8d6e63", 2.4, "", arm_route(-1, 2), [servo_plug5(2)]],
    // up the neck (they wait in the neck's bore for the head)
    ["hdmi", "cables_up", "wires", "#1d1d1d", 5, "HDMI: Pi → head",
        [hdmi0 + [0, 0, 20], hdmi0 + [0, 0, 40], [-12, 118, 160], nk(nko("hdmi"), 225), nk(nko("hdmi"), join_z)], [[hdmi0 - [5.5, 3.5, 0], hdmi0 + [5.5, 3.5, 20]]]],
    ["usbc", "cables_up", "wires", "#2e86de", 4, "screen USB-C: Pi USB → head",
        [usb_end5(29.1, 4), usb_end5(29.1, 4) + [12, 0, 0], [80, 150, 96], [36, 110, 170], nk(nko("usbc"), 225), nk(nko("usbc"), join_z)], [usb_box5(29.1, 4)]],
    ["camera", "cables_up", "wires", "#7d3c98", 3.5, "camera: Pi USB → head",
        [usb_end5(29.1, 12), usb_end5(29.1, 12) + [14, 0, 0], [84, 156, 98], [40, 112, 172], nk(nko("camera"), 225), nk(nko("camera"), join_z)], [usb_box5(29.1, 12)]],
    // the head servo: an extension from the servo power board, its end up out of the body's top (it goes up with the
    // cables above); the servo's own lead onto it with the cap held over the body
    ["servo_ext", "cables_up", "wires", "#a1887f", 2.4, "head servo's extension (3 jumpers)",
        [perf_ch(0) + [0, 0, 14], perf_ch(0) + [0, 0, 26], [perf_ch(0)[0] + 4, 90, 104], [10, 84, 170], [14, 70, 236], ext_top],
        [servo_plug5(0), [ext_top - [1.3, 4, 14], ext_top]]],
    ["servo_head_h", "cap_on", "wires", "#8d6e63", 2.4, "head servo → its extension",
        [hs_exit + [0, 0, H_cap], hs_exit + [-2, 8, H_cap + 4], ext_top + [0, 0, 14], ext_top + [0, 0, 1]], []],
    ["servo_head", "cap_on", "wires", "#8d6e63", 2.4, "", hs_route, [servo_plug5(0)]],
    // in the head (v4's)
    ["hdmi_head", "plugs", "head", "#1d1d1d", 5, "HDMI cable's plug → coupler",
        [nk(nko("hdmi"), join_z), nk(nko("hdmi"), join_z + 8), [16, 76, cpl_c[2]], [cpl_c[0] + hdmi_cpl[0]/2 + hdmi_plug[0] + 4, cpl_c[1], cpl_c[2]],
         [cpl_c[0] + hdmi_cpl[0]/2 + hdmi_plug[0], cpl_c[1], cpl_c[2]]], []],
    ["ribbon", "plugs", "head", "#9c8a5a", 2.2, "ribbon HDMI: coupler → screen",
        [[cpl_c[0] - hdmi_cpl[0]/2, cpl_c[1], cpl_c[2]], [cpl_c[0] - hdmi_cpl[0]/2 - 6, cpl_c[1] + 6, cpl_c[2] + 2], [xe - fpc_plug[0]/2, g3_y + 1 + fpc_plug[1] + 6, cpl_c[2] + 10],
         [xe - fpc_plug[0]/2, g3_y + 1 + fpc_plug[1] + 6, zh - 1], [xe - fpc_plug[0]/2, g3_y + 1 + fpc_plug[1], zh - 1]], []],
    ["usbc_head", "plugs", "head", "#2e86de", 4, "right-angle USB-C → screen",
        [nk(nko("usbc"), join_z), nk(nko("usbc"), join_z + 10), [-20, 86, 304], [-70, 74, zu], [xe - usbc_l[0]/2, usbc_lo[1] + usbc_l[2] + 8, zu],
         [xe - usbc_l[0]/2, usbc_lo[1] + usbc_l[2], zu]], []],
    ["camera_head", "camera", "head", "#7d3c98", 3.5, "camera's 5-pin plug",
        [nk(nko("camera"), join_z), nk(nko("camera"), join_z + 10), [8, 84, 330], [6, 82, 384], [cam_sock[0], cam_sock[1] + cam_plug + 6, cam_sock[2]],
         [cam_sock[0], cam_sock[1] + cam_plug, cam_sock[2]]],
        [[cam_sock - [cam_conn_box[0]/2 + 0.5, 0, cam_conn_box[1]/2 + 0.5], cam_sock + [cam_conn_box[0]/2 + 0.5, cam_plug, cam_conn_box[1]/2 + 0.5]]]]
];

// --- glue (v4's): [name, step, group, the bead's route, the way into the pocket]
function cross_route(f, r) = [f(r, 0), f(0, 0), f(0, r), f(0, 0), f(-r, 0), f(0, 0), f(0, -r)];
function pin_pt(u, v) = let (c = cos(7.5), s = sin(7.5)) torso_pt([u * c - v * s, pinion_yt + u * s + v * c, gear_z0 + 1.5]);
function hw5_glue() = concat(
    [["g_pinion", "pinion_glue", "hservo", cross_route(function (u, v) pin_pt(u, v), 9.5), [0, 0, 1]]],
    [for (s = [-1, 1]) [s < 0 ? "g_arm_r" : "g_arm_l", "arm_glue", s < 0 ? "arm_a" : "arm_b",
        cross_route(function (u, v) arm_pt(s, [arm_gap3 + 1.5, u, v]), 11.5), arm_dir(s, [1, 0, 0])]]
);

// what place_parts.py needs to put the electronics where they are
function hw5_params() = [
    ["hd_z0", hd_z0], ["neck_y", neck_y], ["zc0", zc0], ["BT", BT],
    ["cam", [y4f + cam_board_y, cam4_z, cam_pcb_t, cam_board]], ["cam_lens_l", cam_board_y - H4_face_t - 0.5],
    ["sh", [for (s = [-1, 1]) [sh_pt(s, [0, 0, 0]), sh_dir(s, [1, 0, 0])]]],
    ["arm_gap3", arm_gap3], ["horn_plate_t", horn_plate_t], ["pinion_horn", torso_pt([0, pinion_yt, gear_z0 + 0.6])],
    ["tab_hinge", [y4f + H4_split, cov4_tab_z + 1.25]], ["slider_travel", ups_knob[2]],
    ["arm_pocket", [for (s = [-1, 1]) arm_pt(s, [arm_gap3, 0, 0])]], ["rsl_c", rsl_pt([41, 17, 0])],
    ["chg", [chg_o, tub_lean(chg_x), tw]], ["usbc_sock", usbc_sock],
    ["marks", [["ups_usbc", stk5(ups_ports[2][0], -0.5, stk_ups_w0 + ups_pcb_t + ups_ports[2][2] / 2)], ["pi_usbc", stk5(11.2, -0.5, stk_pi_w1 + 1.6)],
               ["socket", chg_pt([0, tw + 0.5, 0])], ["drive_switch", let (p = js_pt(sw_at, 1), n = js_n(sw_at, 1)) [p[0] + 4 * n[0], p[1] + 4 * n[1], sw_z]],
               ["ups_knob", K5]]],
    ["deck", [deck_z, deck_t]], ["bb", [bb, bb_c]], ["drv", [drv, drv_c, drv_holes]], ["perf", [perf, perf_c]], ["buck", [buck, buck_c]],
    ["perf_ch", [for (k = [0:2]) perf_ch(k)]],
    ["aa", [aa, aa_y0, tray_z]], ["wheel", [xf, yw, az, wh_d, wh_w, pod_t]], ["cas_y", cas_y],
    ["body_z", [z_bt, BT]], ["tub_z", [z_fl, z_bt]]
];
if (part == "hw5") echo(hw5 = [hw5_screws(), hw5_nuts(), hw5_cables(), hw5_params(), hw5_glue()]);

// --- small parts, each where it sits (part = "vid5", which = name) ---
module horn() {   // a cross horn: hub on the spline, plate in the pocket (z = 0 .. horn_plate_t)
    translate([0, 0, -3]) cylinder(d = 7, h = 3 + horn_plate_t, $fn = 24);
    for (a = [0, 90]) rotate(a) hull() for (k = [-1, 1]) translate([k * 12, 0, 0]) cylinder(d = 5, h = horn_plate_t, $fn = 16);
}
module vid5(w) {
    if (w == "ring_gear")  v5_ring_gear();
    if (w == "pinion")     place_torso() translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion();
    if (w == "servo_head") place_torso() translate([0, pinion_yt, hs_spline_z]) servo_dummy();
    if (w == "servo_r")    arm_servo3(-1);          // the robot's right arm (viewer's left)
    if (w == "servo_l")    arm_servo3(1);           // the robot's left arm
    if (w == "horn_head")  place_torso() translate([0, pinion_yt, gear_z0 + 0.6]) rotate(7.5) horn();   // in the pinion's pocket, on the spline
    if (w == "horn_r")     place_arm3(-1, arm_angle) translate([arm_gap3, 0, 0]) rotate([0, 90, 0]) horn();
    if (w == "horn_l")     place_arm3(1, arm_angle) translate([arm_gap3, 0, 0]) rotate([0, 90, 0]) horn();
    // the screen's plugs, as screen_plugs3()
    if (w == "fpc")        translate([xe - fpc_plug[0], g3_y + 1, zh - fpc_plug[1]/2]) cube(fpc_plug);
    if (w == "usbc_plug")  translate([xe - usbc_l[0], g3_y + 1, zu - usbc_l[1]/2]) cube([usbc_l[0], usbc_l[2], usbc_l[1]]);
    if (w == "coupler4")   translate([cpl_c[0] - hdmi_cpl[0]/2, cpl_c[1] - hdmi_cpl[2]/2, cpl_c[2] - hdmi_cpl[1]/2]) cube([hdmi_cpl[0], hdmi_cpl[2], hdmi_cpl[1]]);
    if (w == "hdmi_plug")  translate([cpl_c[0] + hdmi_cpl[0]/2, cpl_c[1] - hdmi_plug[2]/2, cpl_c[2] - hdmi_plug[1]/2]) cube([hdmi_plug[0], hdmi_plug[2], hdmi_plug[1]]);
    if (w == "mic_plug")   place_rsl() rsl_plug();                       // the ReSpeaker's USB-C cable's plug
    if (w == "eyes")       translate([0, g3_y - 0.05, scr_cz]) rotate([90, 0, 0]) linear_extrude(0.4) eyes4_2d();
    // the base's: each wheel pod, motor (with its coupler), wheel and caster on its own
    if (w == "pod_l")      place_pod(1) v5_pod();                        // x > 0: the robot's left
    if (w == "pod_r")      place_pod(-1) v5_pod();
    if (w == "motor_l")    motor5(1);
    if (w == "motor_r")    motor5(-1);
    if (w == "coupler_l")  coupler5(1);
    if (w == "coupler_r")  coupler5(-1);
    if (w == "wheel_l")    translate([xf + 44.5 - wh_w / 2, yw, az]) rotate([0, 90, 0]) wheel5();
    if (w == "wheel_r")    translate([-(xf + 44.5 - wh_w / 2), yw, az]) rotate([0, -90, 0]) wheel5();
    if (w == "pi_ups")     place_stack() ups_stack();                    // without the plugs: the cables bring their own
    if (w == "caster_f")   caster5(cas_y[0]);
    if (w == "caster_r")   caster5(cas_y[1]);
}
// the motor without its coupler (it goes in from inside the pod, its shaft through the plate's hole), and the coupler
// (it goes onto the shaft from the wheel's side): a round end with the grub screw, a 12 mm hex for the wheel
module motor5(s) translate([0, yw, az]) rotate([0, s * 90, 0]) {
    translate([0, 0, xf - mot[1]]) cylinder(d = mot[0], h = mot[1], $fn = 48);
    translate([0, 0, xf]) cylinder(d = mot_boss[0], h = mot_boss[1], $fn = 32);
    translate([0, 0, xf]) cylinder(d = 4, h = mot[2], $fn = 16);
}
module coupler5(s) translate([0, yw, az]) rotate([0, s * 90, 0]) translate([0, 0, xf + pod_t + 0.8]) difference() {
    union() { cylinder(d = 10, h = 9, $fn = 32); translate([0, 0, 9]) cylinder(d = 12 / cos(30), h = 9, $fn = 6); }
    translate([0, 0, 4.5]) rotate([90, 0, 0]) cylinder(d = 2.5, h = 6, $fn = 12);                  // the grub screw's hole
}
// a wheel: tyre, rim, a hub with its screw's recess (axis z, centred; +z = outwards)
module wheel5() difference() {
    union() { cylinder(d = wh_d, h = wh_w - 4, center = true, $fn = 64); cylinder(d = wh_d - 6, h = wh_w, center = true, $fn = 64); }
    translate([0, 0, wh_w / 2 - 3]) cylinder(d = wh_d - 22, h = 4, $fn = 48);
}
module caster5(y) { translate([0, y, cas_lift + 9.525]) sphere(d = 19.05, $fn = 32); translate([0, y, cas_seat - 12]) cylinder(d1 = 16, d2 = 22.9, h = 12, $fn = 32);
                    translate([-12.2, y - 6, cas_seat - 1.6]) cube([24.4, 12, 1.6]); }
if (part == "vid5") vid5(which);
