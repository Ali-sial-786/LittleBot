// What the v4 assembly video needs from the model (make_video.sh):
//   part = "hw4"            echoes hw4 = [screws, nuts, cables, params]: every screw (where its head bears, the way it goes
//                           in, the tool that drives it), every nut, every cable's route, and the positions the electronics sit at
//   part = "vid4", which=.. one small part where it sits in the robot (the assembled STLs lump some together)
// Robot coordinates as the model: x across (+x = the viewer's right = the robot's left), y front -> back, z up.
include <../littlebot_v4.scad>
part = "hw4";
which = "";

// --- frames ---
function Ry(a) = [[cos(a), 0, sin(a)], [0, 1, 0], [-sin(a), 0, cos(a)]];
function Rx(a) = [[1, 0, 0], [0, cos(a), -sin(a)], [0, sin(a), cos(a)]];
function mx(s, q) = [s * q[0], q[1], q[2]];
function sh_pt(s, p) = mx(s, sh_c + Ry(-sh_tilt) * p);                   // place_sh(s)
function sh_dir(s, v) = mx(s, Ry(-sh_tilt) * v);
function arm_pt(s, p) = sh_pt(s, Rx(-arm_angle) * p);                     // place_arm3(s, arm_angle)
function arm_dir(s, v) = sh_dir(s, Rx(-arm_angle) * v);
function sv_arm(p) = [p[2] + sv_spline_top, p[0], p[1]];                  // arm_servo3: a servo point -> the shoulder's frame
function head_bot(Y, x) = let (k = k4(Y - Yc)) z4c - H4[1] * k * pow(max(0, 1 - pow(abs(x) / (H4[0] * k), H4_n)), 1 / H4_n);   // the head's underside
function nk(o, z) = [o[0], neck_y + o[1], z];                              // a point in the neck's bore

// --- screws: [step, what, d, length, head, where the head bears, the way it goes in]. head "btn" = an M3 / M2.5 button
// head (hex socket: 2 mm key), "pan" = a Phillips pan head (screwdriver). Listed in the order they go in.
horn_scr = [[0, 10.5], [180, 10.5]];                                      // the arm horn's 2 screws (angle, radius on the horn)
function hw4_screws() = concat(
    [for (a = ring_screw_angles) ["ring_gear", "M3 × 12", 3, 12, "btn", torso_pt([ring_screw_r * cos(a), neck_yt + ring_screw_r * sin(a), gear_z0 + 1.8]), [0, 0, 1]]],
    [for (a = [0, 180]) ["head_servo", "horn screws", 2, 5, "pan", torso_pt([7.5 * cos(a + 7.5), pinion_yt + 7.5 * sin(a + 7.5), gear_z0 + 0.6]), [0, 0, 1]]],
    [for (hx = [sv_hole_a, sv_hole_b]) ["head_servo", "servo screws", 2, 8, "pan", torso_pt([hx, pinion_yt, hs_tab_top]), [0, 0, -1]]],
    [for (k = [0, 1]) let (o = k == 0 ? 1 : -1) ["head_servo", "M3 × 10", 3, 10, "btn",
        torso_pt([frame_pillar_x[k] - o * 4.5, pinion_yt + 7.6, hs_tab_top - sv_tab_t + collar_h/2]), [o, 0, 0]]],
    [for (a = puck_scr) ["puck", "M3 × 12", 3, 12, "btn", [head_screw_r * cos(a), neck_y + head_screw_r * sin(a), pz0 + puck_t - 4.5], [0, 0, -1]]],
    [for (s = [-1, 1]) for (hy = [sv_hole_a, sv_hole_b]) ["arm_servos", "servo screws", 2, 8, "pan", sh_pt(s, [sv_tab_top, hy, 0]), sh_dir(s, [-1, 0, 0])]],
    [for (x = keeper_x) ["stack", "M3 × 20", 3, 20, "btn", [x, stk_yc, keeper_mouth(ups_plate_t, ups_plate[1]) + keeper_t - 1.8], [0, 0, -1]]],
    [for (p = pi_hole_pts) ["stack", "M2.5 × 6 (UPS kit)", 2.5, 6, "pan", stk(p[0], p[1], stk_pi_w1), [0, -1, 0]]],
    [for (x = pca_clamp_x) ["pca", "M3 × 10", 3, 10, "btn", [x, pca_y0_floor + pca_w/2, floor_low + floor_low_t + pca_standoff + 1.6 + pca_clamp_t], [0, 0, -1]]],
    [for (p = br_screws) ["panel", "M3 × 12", 3, 12, "btn", [p[0], back_y(p[0], br_z) + pp4_t, br_z], [0, -1, 0]]],
    [["panel", "M3 × 8", 3, 8, "btn", [pp4_top_scr[0], back_y(pp4_top_scr[0], pp4_top_scr[1]) + pp4_t, pp4_top_scr[1]], [0, -1, 0]]],
    [for (p = floor_screw_pts()) ["floor_in", "M3 × 12", 3, 12, "btn", [p[0] * ring_x(p[1], 0), p[1], floor_screw_z], [-p[0], 0, 0]]],
    [for (p = np_screw_pts) ["plate_in", "M3 × 8", 3, 8, "btn", [p[0], p[1], b_top], [0, 0, -1]]],
    [for (a = head4_scr) ["head_on", "M3 × 10", 3, 10, "btn", [head_screw_r * cos(a), neck_y + head_screw_r * sin(a), hd_z0 + 4], [0, 0, -1]]],
    [for (h = scr_holes) let (q = hole_xz(h)) ["screen", "M2.5 × 6", 2.5, 6, "pan", [q[0], scr_back_y + scr_standoff_h + 2, q[1]], [0, -1, 0]]],
    [for (s = [-1, 1]) ["camera", "M3 × 12", 3, 12, "btn", [s * cam4_scr_x, y4f + cam_board_y + cam_pivot_dy, cam4_z], [-s, 0, 0]]],
    [for (x = cov4_scr) ["back", "M3 × 12", 3, 12, "btn", [x, y4f + H4_split + 5, head_bot(H4_split + 5, x)], [0, 0, 1]]],
    [for (s = [-1, 1]) for (h = horn_scr) ["arms", "horn screws", 2, 6, "pan", arm_pt(s, [arm_gap3, h[1] * cos(h[0]), h[1] * sin(h[0])]), arm_dir(s, [1, 0, 0])]],
    [for (s = [-1, 1]) ["arms", "spline screw", 2.5, 5, "pan", arm_pt(s, [arm_gap3 + horn_plate_t, 0, 0]), arm_dir(s, [-1, 0, 0])]]
);

// --- nuts: [step, centre, axis] (M3: 5.5 across flats, 2.4 thick); each drops in from above
function hw4_nuts() = concat(
    [for (p = br_screws) ["ring_nuts", [p[0], br_wall_y(p, b_wall + 0.3) - 1.6 - 1.35, br_z], [0, 1, 0]]],     // the ring's 4 tabs
    [for (p = np_screw_pts) ["plate_nuts", [p[0], p[1], b_top - np_t - 2.6 + 1.2], [0, 0, 1]]]                // the body's ledge bosses
);

// --- cables: [name, step, group, colour, diameter, label, route, plugs (boxes [lo, hi] at its ends)].
// The cables up the neck are two pieces each, joined in the neck's bore 15 mm above the head's floor: the body piece
// waits there for the head, the head piece goes on with the head.
join_z = hd_z0 + 15;
nk_off = [["hdmi", [-4.5, 2.5]], ["usbc", [4.5, 3]], ["camera", [4.5, -3.5]], ["aa", [-4, -4.5]]];
function nko(n) = nk_off[search([n], nk_off)[0]][1];
function usb_box4(v, h) = [stk(pi_l + 3, v + 7, stk_pi_w1 + h - 4), stk(pi_l + 21, v - 7, stk_pi_w1 + h + 4)];   // a USB-A plug in the Pi
function usb_end(v, h) = stk(pi_l + 21, v, stk_pi_w1 + h);
function pca_ch4(k) = [pca_x0 + 12 + 2.54 * k, pca_y0_floor + pca_w - 4, floor_low + floor_low_t + pca_standoff + 1.6];
pca_vp = [pca_x0 + 34, pca_y0_floor + pca_w - 5, floor_low + floor_low_t + pca_standoff + 1.6];   // its V+ screw terminal (servo power)
function lead_route(top, x) = concat(top, [[x, 95 - (x + 10) / 8, 78], [x, 95 - (x + 10) / 8, servo_lead_z], [x, 70, servo_lead_z], [x, 52, servo_lead_z],
    [x, 44.5, servo_lead_z], [x, 42, 60], [x, 40, 45]]);
function servo_plug(k) = [pca_ch4(k) - [1.3, 4, 0], pca_ch4(k) + [1.3, 4, 16.5]];
hs_exit = torso_pt([sv_l - sv_shaft_off + 7.5, pinion_yt, hs_spline_z - sv_spline_h + 3.5]);
function as_exit(s) = sh_pt(s, sv_arm([sv_l - sv_shaft_off + 7.5, 0, -sv_spline_h + 3.5]));
// the screen's plugs (screen_plugs3): ends where their cables leave them
xe = -scr[0] / 2; zh = scr_pt([0, scr_ports[3]])[1]; zu = scr_pt([0, scr_ports[2]])[1];
fpc_lo = [xe - fpc_plug[0], g3_y + 1, zh - fpc_plug[1]/2]; usbc_lo = [xe - usbc_l[0], g3_y + 1, zu - usbc_l[1]/2];
cam_sock = [cam_conn[0], y4f + cam_board_y + cam_pcb_t, cam4_z - 16 + cam_conn[1]];
bat_lead = [-bat[0]/2 + 10, y4f + bat4_y + bat4_clr, bat4_z0 + 4];
function hw4_cables() = let (v2c = hw_cables(), hdmi0 = stk(25.8, -0.5, stk_pi_w1 + 1.7)) [
    v2c[0],                                                                // charging: the UPS's USB-C -> the socket at the back (v2's)
    v2c[1],                                                                // I2C: Pi pins 1, 3, 5, 6 -> the PCA9685 (v2's)
    ["speaker", "cables_body", "none", "#16a085", 3.5, "speaker → Pi USB",
        [[spk_w/2 + 1.6, spk3_c[1] + spk_d/2, spk3_c[2] + spk_h/2 - 4], [56, 30, 132], [72, 72, 92], [78, 106, 42], [74, 113.5, 15.5], usb_end(47, 4)], [usb_box4(47, 4)]],
    ["mic", "cables_body", "none", "#e67e22", 3.5, "mic → Pi USB",
        [[mic3[0] + mic_body + 32, mic3[1] + mic_t/2, mic3[2]], [30, 30, 172], [58, 82, 112], [80, 116, 46], [76, 121.5, 15.5], usb_end(47, 12)], [usb_box4(47, 12)]],
    ["servo_left", "cables_body", "none", "#8d6e63", 2.4, "left arm servo → PCA9685 channel 1",
        concat(lead_route([as_exit(1), [50, 104, 186], [24, 102, 120]], -6), [pca_ch4(1) + [0, 0, 16.5]]), [servo_plug(1)]],
    ["servo_right", "cables_body", "none", "#8d6e63", 2.4, "right arm servo → PCA9685 channel 2",
        concat(lead_route([as_exit(-1), [-50, 104, 186], [-24, 102, 120]], -14), [pca_ch4(2) + [0, 0, 16.5]]), [servo_plug(2)]],
    // up the neck (they wait in the neck's bore for the head)
    ["hdmi", "cables_up", "none", "#1d1d1d", 5, "HDMI: Pi → head",
        [hdmi0 + [0, 0, 20], hdmi0 + [0, 0, 40], [-12, 104, 140], nk(nko("hdmi"), 205), nk(nko("hdmi"), join_z)], [[hdmi0 - [5.5, 3.5, 0], hdmi0 + [5.5, 3.5, 20]]]],
    ["usbc", "cables_up", "none", "#2e86de", 4, "screen USB-C: Pi USB → head",
        [usb_end(29.1, 4), [74, 113.5, 33.4], [77, 110, 74], [36, 98, 150], nk(nko("usbc"), 205), nk(nko("usbc"), join_z)], [usb_box4(29.1, 4)]],
    ["camera", "cables_up", "none", "#7d3c98", 3.5, "camera: Pi USB → head",
        [usb_end(29.1, 12), [76, 121.5, 33.4], [80, 118, 74], [40, 100, 150], nk(nko("camera"), 205), nk(nko("camera"), join_z)], [usb_box4(29.1, 12)]],
    ["aa", "cables_up", "none", "#c0392b", 3, "AA box leads: PCA9685 V+ → head",
        [pca_vp + [0, 0, 19], pca_vp + [0, 0, 30], [pca_vp[0], 30, 80], [-4, 62, 150], nk(nko("aa"), 205), nk(nko("aa"), join_z)],
        [[pca_vp + [-5, -3.5, 12], pca_vp + [5, 3.5, 19]]]],
    // the head servo's lead: down to the PCA9685 before the neck plate goes in
    ["servo_head", "plate_in", "none", "#8d6e63", 2.4, "head servo → PCA9685 channel 0",
        concat(lead_route([hs_exit, [28, 62, 176], [10, 86, 112]], -10), [pca_ch4(0) + [0, 0, 16.5]]), [servo_plug(0)]],
    // in the head
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
        [[cam_sock - [cam_conn_box[0]/2 + 0.5, 0, cam_conn_box[1]/2 + 0.5], cam_sock + [cam_conn_box[0]/2 + 0.5, cam_plug, cam_conn_box[1]/2 + 0.5]]]],
    ["aa_head", "aa_box", "head", "#c0392b", 3, "AA box leads",
        [nk(nko("aa"), join_z), nk(nko("aa"), join_z + 4), [bat_lead[0], bat_lead[1] - 6, bat_lead[2]], bat_lead], []]
];

// what place_parts.py needs to put the electronics where they are
function hw4_params() = concat(video_params(), [
    ["keeper_x", keeper_x], ["pca_clamp_x", pca_clamp_x], ["hd_z0", hd_z0], ["neck_y", neck_y], ["b_top", b_top],
    ["bat", bat], ["bat_lo", [-bat[0]/2, y4f + bat4_y + bat4_clr, bat4_z0]], ["eyes_y", g3_y - 0.05], ["scr_cz", scr_cz],
    ["cpl_c", cpl_c], ["hdmi_cpl", hdmi_cpl], ["hdmi_plug", hdmi_plug], ["fpc_lo", fpc_lo], ["fpc_plug", fpc_plug], ["usbc_lo", usbc_lo], ["usbc_l", usbc_l],
    ["cam", [y4f + cam_board_y, cam4_z, cam_pcb_t, cam_board]], ["cam_lens_l", cam_board_y - H4_face_t - 0.5],
    ["sh", [for (s = [-1, 1]) [sh_pt(s, [0, 0, 0]), sh_dir(s, [1, 0, 0])]]],
    ["arm_axis", [for (s = [-1, 1]) [arm_pt(s, [0, 0, 0]), arm_dir(s, [1, 0, 0]), arm_dir(s, [0, 1, 0]), arm_dir(s, [0, 0, 1])]]],
    ["arm_gap3", arm_gap3], ["horn_plate_t", horn_plate_t], ["pinion_horn", torso_pt([0, pinion_yt, gear_z0 + 0.6])],
    ["tab_hinge", [y4f + H4_split, cov4_tab_z + 1.25]], ["mic_ext", [mic3[0] + mic_body, mic3[1] + mic_t/2 - 4.75, mic3[2] - 9]],
    ["slider_travel", ups_knob[2]], ["torso_z0", torso_z0], ["pp4", [pp4[0], pp4[1], pp4_z0]],
    ["spk", [spk3_c, [spk_w, spk_d, spk_h]]], ["mic", [mic3, [mic_l, mic_t, mic_w]]]
]);
if (part == "hw4") echo(hw4 = [hw4_screws(), hw4_nuts(), hw4_cables(), hw4_params()]);

// --- small parts, each where it sits (part = "vid4", which = name) ---
module horn() {   // a cross horn: hub on the spline, plate in the pocket (z = 0 .. horn_plate_t)
    translate([0, 0, -3]) cylinder(d = 7, h = 3 + horn_plate_t, $fn = 24);
    for (a = [0, 90]) rotate(a) hull() for (k = [-1, 1]) translate([k * 12, 0, 0]) cylinder(d = 5 - abs(k) * 0, h = horn_plate_t, $fn = 16);
}
module vid4(w) {
    if (w == "ring_gear")  place_torso() translate([0, neck_yt, gear_z0]) ring_gear();
    if (w == "pinion")     place_torso() translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion();
    if (w == "keeper")     stack_keeper();
    if (w == "clamp0")     pca_clamp(0);
    if (w == "clamp1")     pca_clamp(1);
    if (w == "servo_head") place_torso() translate([0, pinion_yt, hs_spline_z]) servo_dummy();
    if (w == "servo_r")    arm_servo3(-1);          // the robot's right arm (viewer's left)
    if (w == "servo_l")    arm_servo3(1);           // the robot's left arm
    if (w == "horn_head")  place_torso() translate([0, pinion_yt, gear_z0 + 0.6]) rotate(7.5) horn();   // in the pinion's pocket, on the spline
    if (w == "horn_r")     place_arm3(-1, arm_angle) translate([arm_gap3, 0, 0]) rotate([0, 90, 0]) horn();
    if (w == "horn_l")     place_arm3(1, arm_angle) translate([arm_gap3, 0, 0]) rotate([0, 90, 0]) horn();
    if (w == "screen")     screen_dummy3();
    if (w == "pca")        pca_floor();
    // the screen's plugs, as screen_plugs3()
    if (w == "fpc")        translate([xe - fpc_plug[0], g3_y + 1, zh - fpc_plug[1]/2]) cube(fpc_plug);
    if (w == "usbc_plug")  translate([xe - usbc_l[0], g3_y + 1, zu - usbc_l[1]/2]) cube([usbc_l[0], usbc_l[2], usbc_l[1]]);
    if (w == "coupler4")   translate([cpl_c[0] - hdmi_cpl[0]/2, cpl_c[1] - hdmi_cpl[2]/2, cpl_c[2] - hdmi_cpl[1]/2]) cube([hdmi_cpl[0], hdmi_cpl[2], hdmi_cpl[1]]);
    if (w == "hdmi_plug")  translate([cpl_c[0] + hdmi_cpl[0]/2, cpl_c[1] - hdmi_plug[2]/2, cpl_c[2] - hdmi_plug[1]/2]) cube([hdmi_plug[0], hdmi_plug[2], hdmi_plug[1]]);
    if (w == "eyes")       translate([0, g3_y - 0.05, scr_cz]) rotate([90, 0, 0]) linear_extrude(0.4) eyes4_2d();
}
if (part == "vid4") vid4(which);
