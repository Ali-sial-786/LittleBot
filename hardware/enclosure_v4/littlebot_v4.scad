// =============================================================================
//  LittleBot v4 "Soft Arc" - parametric OpenSCAD model (mm)
// =============================================================================
//  The Soft Arc concept with the parts already bought: a smooth egg of a body on a dark base, a black neck, a
//  round head (a superellipsoid with its face cut flat) round the 7" touch screen, a camera hood with a dark
//  arch, one-piece arms with hands.
//    sa_body        the white egg (prints upright, no supports)
//    v3_ring        dark base (v3's shape: its notches are the concept's base vents)
//    v3_neck_plate  the body's top: carries v2's neck (v3's)
//    sa_puck        the black neck: on v2's coupler, under the head (turns with it)
//    sa_head_frame  the head's front half: face, screen, camera hood (dark arch printed in, same print), AA box
//    sa_head_back   the head's back half (vent slits); 2 screws from below
//    sa_port_panel  dark panel on the back round the charging socket and the UPS switch
//    sa_arm         x2: shoulder, arm and hand in one smooth piece
//    v3_ups_slider  the UPS's power switch (v3's), its tab standing out of the dark panel
//  What goes where (and why):
//    - the neck stands 45 mm higher than v2/v3's (v2's neck parts unchanged): a taller egg, like the concept
//    - sound: the USB speaker behind a grille in the chest; mic behind the left dot's holes (the right dot matches)
//    - Pi 5 + UPS on v2's floor: its fan blows down through the floor vents and out under the base; fresh air in
//      through the slits high on the back
//    - 4xAA servo box: in the head behind the back cover (it balances the screen); its leads go down the neck
//    - charging + UPS switch: in the dark panel low on the back
//    - camera: v2's bracket in the hood, 2 pivot screws through the hood's sides (tilt adjustable)
//  Reused from v2 as printed: floor (Pi + UPS), stack keeper, PCA clamps, coupler, ring gear, pinion, servo frame,
//  camera bracket. Reused from v3: base ring, neck plate, UPS slider.
//  Coordinates as in v2/v3: x across (+x = the viewer's right), y front -> back, z up from the table.
//    openscad -o layout.png -D 'part="layout4"' littlebot_v4.scad
// =============================================================================
// overrides of v3's variables are evaluated where v3 defines them: what they use must be set before the include
neck_lift = 45;                          // the neck and head stand this much higher than v2/v3's: a taller body
puck_t = 8;                              // the black neck raises the head this much more
H4 = [121, 79, 80];                      // head: half width, half height, half depth behind its widest section
H4_wall = 2.5;
egg4 = [104, 91, 92]; egg4_k = 2.4;      // the egg: its widest half width / half depth, their height; shoulder roundness
mic_dot = [20, 187];                     // the chest's two dots: x, z (the mic behind the left one)
spk4_z = 128;                            // the speaker's centre height
pp4 = [124, 58]; pp4_z0 = 25.5; pp4_t = 2.5;   // the dark port panel: width, height; its bottom; thickness
// v4's own constants
puck_r = 31; puck_skirt = 26;            // the puck; its skirt's inner radius (v2's coupler is r 25)
puck_scr = [30, 140, 290];               // into the coupler's own holes (where v3's head screwed)
head4_scr = [85, 215, 340];              // the head into the puck
H4_n = 3.2; H4_m = 2.6;                  // head roundness: front view, depth
H4_f = 0.62;                             // the face's flat cut: this much of H4[2] in front of the widest section
H4_ch = [3, 1.5];                        // the face's edge: inset, depth (a soft bevel onto the round)
H4_face_t = 1.8;                         // the face (the plugs start 1 mm behind the glass)
H4_split = 83;                           // frame | back cover (from the face): the neck's pad is all in the frame
hood_ab = [38, 33]; hood_n = 2.5;        // the camera hood's arch (front view): half sizes, roundness
hood_d = 58;                             // depth: full height to 44 (the camera bracket), then it slopes into the head
hood_rise = 18;                          // its top above the head's top
inlay_t = 1.6;                           // the dark arch: the face's first 8 layers
WHITE4 = [0.95, 0.94, 0.92]; DARK4 = [0.16, 0.17, 0.19];
cov4_scr = [-55, 55];                    // the back cover's 2 screws, from below, 5 mm behind the split
cov4_tabs = [-40, 40];                   // the frame's 2 tabs into slots under the cover's roof
include <../enclosure_v3/littlebot_v3.scad>
part = "layout4";        // overrides v3's (v4's parts: layout4, print4 + which, assy4 + which)
which = "";

/* [the neck, 45 mm higher] (v2's parts and their spacing unchanged) */
torso_z0 = base_top + neck_lift;
b_top = torso_z0 + torso_h;              // 242: the body's top, where the neck plate sits

/* [body: an egg of superellipse sections] (overrides v3's; the same bottom and top sections, so v3's ring and
   neck plate fit unchanged) */
nsec = 40;
// half width (or depth) at height z: from the ring's section up to the egg's widest (sin^2: no step at the ring),
// then a superellipse arc rolling into the top section
function egg_at(z, v0, vmax, v1) = z <= egg4[2]
    ? v0 + (vmax - v0) * pow(sin(90 * max(0, z - sh0) / (egg4[2] - sh0)), 2)
    : let (zv = egg4[2] + (b_top - egg4[2]) / pow(1 - pow(v1 / vmax, egg4_k), 1 / egg4_k), u = min(1, (z - egg4[2]) / (zv - egg4[2])))
      vmax * pow(1 - pow(u, egg4_k), 1 / egg4_k);
function bell_sec(t) = let (z = sh0 + (b_top - sh0) * t)
    [egg_at(z, bell[0], egg4[0], bell[2]), egg_at(z, bell[1], egg4[1], bell[3]), 4 - 1.2 * t, z];
function front_y(x, z) = let (s = bell_at(z)) b_cy - s[1] * pow(1 - pow(min(1, abs(x) / s[0]), s[2]), 1 / s[2]);
sh_tilt = 16;                            // the arms splay out more (they stand off the body, as the concept)
// the neck plate's 4 nut bosses (v3's), braced out to the egg's wall at 45 degrees (it is fuller than v3's bell)
function np_boss_to(p) = p[1] < neck_y ? [p[0] + sign(p[0]) * 40, p[1], b_top - np_t - 47] : [p[0] + sign(p[0]) * 14, p[1] + 50, b_top - np_t - 60];
sh_c = [bell_at(b_top - 31)[0] + 3.5, 82, b_top - 31];   // the shoulders: 3.5 mm out of the egg (the arms clear its fullness below)
br_screws = [[-52, 1], [-30, 1], [30, 1], [52, 1]];   // body -> ring: 4 at the back, through the dark panel
// (v3's, taken at the body's bottom edge, where the egg is narrowest, and at the tab's outer corner)
function br_wall_y(p, inset) = let (s = bell_at(sh0 + 0.01), yy = s[1] * pow(1 - pow((abs(p[0]) + 6) / s[0], s[2]), 1 / s[2]))
    p[1] == 0 ? b_cy - yy + inset : b_cy + yy - inset;
sl_wall_y = back_y(ups_knob_c[0], sl_z0 + sl_h / 2) + pp4_t;   // the slider's tab stands out of the dark panel
spk3_c = [0, max([for (x = [-1, 1]) for (z = [-1, 1]) front_y(x * spk_w / 2, spk4_z + z * spk_h / 2)]) + b_wall + 1.5, spk4_z];
mic3 = [-mic_dot[0] - 5.5, max(front_y(-mic_dot[0] - 5.5, mic_dot[1] + 9.5), front_y(mic_dot[0] - 3.3, mic_dot[1] + 9.5)) + b_wall + 1.6, mic_dot[1]];

/* [head] */
g3_y = neck_y - 49;                      // the glass front: the neck 49 mm behind it (the face over the body's front)
y4f = g3_y - 1;                          // the face (1 mm proud of the glass, as v3's bezel)
hd_z0 = torso_z0 + torso_h + bearing_h + coupler_h + puck_t;   // the head's bottom
z4c = hd_z0 + H4[1];                     // its centre
scr_cz = hd_z0 + H4[1];                  // the screen, centred on the face
scr_top = scr_cz + scr[1] / 2;
// the HDMI coupler on the head's floor (it curves up towards the face: the coupler sits on its front edge)
cpl_c = [-45, scr_back_y + 3 + hdmi_cpl[2] / 2, let (k = k4(scr_back_y + 3 - (g3_y - 1) - (H4_ch[1] + H4_f * H4[2])))
    hd_z0 + H4[1] - H4[1] * k * pow(1 - pow((45 + hdmi_cpl[0] / 2) / (H4[0] * k), H4_n), 1 / H4_n) + H4_wall + 0.5 + hdmi_cpl[1] / 2];
Yc = H4_ch[1] + H4_f * H4[2];            // the widest section, behind the face
H4_D = Yc + H4[2];                       // the head's depth
function k4(yp) = pow(max(0, 1 - pow(min(1, abs(yp) / H4[2]), H4_m)), 1 / H4_n);   // section scale, yp from the widest
kf = k4(-H4_f * H4[2]);                  // at the face
H4_yps = concat([for (i = [0:16]) -H4_f * H4[2] * (1 - i / 16)], [for (i = [1:24]) H4[2] * sin(90 * i / 24)]);
function head_top(Y, x = 0) = let (k = k4(Y - Yc)) z4c + H4[1] * k * pow(max(0, 1 - pow(abs(x) / (H4[0] * k), H4_n)), 1 / H4_n);   // the head's top at Y (from the face), x
/* [camera hood] */
hood_c = z4c + H4[1] + hood_rise - hood_ab[1];
cam4_z = hood_c + 6;                     // the lens's centre
module place_cam4() translate([0, y4f + cam_board_y, cam4_z]) children();
cam4_scr_x = 26;                         // the pivot screws' heads (|x|, on the cheeks): 12 mm reach 5 mm into the bracket's pivots, as v2
/* [4xAA box in the head] */
bat4_y = 61;                             // its front face, from the face (behind the cables' bore)
bat4_clr = 0.5;

// -----------------------------------------------------------------------------
// helpers
// -----------------------------------------------------------------------------
module se2d(a, b, n) polygon(se(a, b, n, 0, 120));
module xz_slice(y, a, b, n, zc) translate([0, y, zc]) rotate([-90, 0, 0]) mirror([0, 1, 0]) linear_extrude(0.01) se2d(a, b, n);
module eball(c, ry, rx) translate(c) scale([rx / ry, 1, 1]) sphere(r = ry, $fn = 40);   // a sphere squashed to rx in x

// -----------------------------------------------------------------------------
// BODY
// -----------------------------------------------------------------------------
// the dark port panel: a curved patch on the skin (2.5 mm proud), round the USB-C socket and the UPS switch's tab;
// held by the 4 body -> ring screws along its bottom and 1 at its top
module pp4_prism(grow = 0) translate([0, 0, pp4_z0 + pp4[1] / 2]) rotate([-90, 0, 0]) linear_extrude(400) rr(pp4[0] + 2 * grow, pp4[1] + 2 * grow, 12 + grow);
pp4_top_scr = [0, pp4_z0 + pp4[1] - 6];
module pp4_holes() {
    chg_cut();
    translate([ups_knob_c[0] - 8 - ups_knob[2] - 1, 0, sl_z0 - 6 - 0.6]) cube([16 + 2 * ups_knob[2] + 2, 400, sl_h + 6 + 1.2]);   // the switch's tab and its travel
    for (p = br_screws) translate([p[0], 0, br_z]) rotate([-90, 0, 0]) cylinder(d = clear_m3, h = 400, $fn = fn_small);
    translate([pp4_top_scr[0], 0, pp4_top_scr[1]]) rotate([-90, 0, 0]) cylinder(d = clear_m3, h = 400, $fn = fn_small);
}
module sa_port_panel() difference() {
    intersection() { difference() { body_outer(-pp4_t); body_outer(); } pp4_prism(); translate([-300, b_cy, 0]) cube([600, 300, 300]); }
    pp4_holes();
}
module pp4_boss() intersection() {      // the panel's top screw: a boss inside the wall (45 degrees under it)
    body_inner();
    translate([pp4_top_scr[0], back_y(0, pp4_top_scr[1]), pp4_top_scr[1]]) hull() {
        rotate([90, 0, 0]) cylinder(d = 9, h = 9, $fn = 32);
        translate([0, -1, -9]) rotate([90, 0, 0]) cylinder(d = 0.5, h = 1);
    }
}
module pp4_cuts() translate([pp4_top_scr[0], back_y(0, pp4_top_scr[1]) - 9, pp4_top_scr[1]]) rotate([-90, 0, 0]) cylinder(d = pilot_m3, h = 20, $fn = fn_small);   // M3 x 8

// the speaker behind a grille in the chest (its ribs from v3, grown to the egg's wall)
module sa_speaker_ribs() intersection() {
    body_inner();
    for (s = [-1, 1]) let (x0 = s > 0 ? spk_w/2 + 0.3 : -spk_w/2 - 3.3) {
        hull() {
            translate([x0, -40, spk3_c[2] - spk_h/2 - 2]) cube([3, spk3_c[1] + spk_d + 43, spk_h + 4]);
            translate([x0, -40, spk3_c[2] - spk_h/2 - 2 - (spk3_c[1] + spk_d + 43)]) cube([3, 1, 1]);
        }
        hull() {
            translate([s > 0 ? spk_w/2 - 3 : -spk_w/2, -40, spk3_c[2] - spk_h/2 - 2]) cube([3, spk3_c[1] + spk_d + 36, 2]);
            translate([s > 0 ? spk_w/2 + 0.3 : -spk_w/2, -40, spk3_c[2] - spk_h/2 - 6]) cube([0.01, spk3_c[1] + spk_d + 36, 2]);
        }
    }
}
module sa_speaker_cuts() {
    if (detail) for (row = [-3:3]) for (gx = [-35:5:35]) let (px = gx + (abs(row) % 2) * 2.5, pz = row * 4.4)
        if (in_stadium(px, pz, spk_grille_hw, spk_grille_r)) translate([px, -40, spk3_c[2] + pz]) rotate([-90, 0, 0]) cylinder(d = 2.6, h = spk3_c[1] + 41, $fn = fn_small);
    for (s = [-1, 1]) for (zz = [spk3_c[2] - spk_h/2 + 8, spk3_c[2] + spk_h/2 - 8])
        translate([s * (spk_w/2 + 1.8), spk3_c[1] + spk_d + 0.5, zz]) cube([5, 3, 3.5], center = true);
}
// the mic (Adafruit 3367) flat behind the left dot, its USB plug pointing +x to the extension socket
module sa_mic_sleeve() intersection() {
    body_inner();
    difference() {
        hull() {
            translate([mic3[0] - 2, -40, mic3[2] - mic_w/2 - 2.4]) cube([mic_body + 2, mic3[1] + mic_t + 42.5, mic_w + 4.8]);
            translate([mic3[0] - 2, -40, mic3[2] - mic_w/2 - 2.4 - (mic3[1] + mic_t + 42.5)]) cube([mic_body + 2, 1, 1]);
        }
        translate([mic3[0], -41, mic3[2] - mic_w/2 - 0.4]) cube([40, mic3[1] + mic_t + 41.4, mic_w + 0.8]);
    }
}
// the two dots: 7 small holes each (the left one was the Adafruit mic's), and the ReSpeaker's two (below)
module dot7(x, z, d = 1.6, y0 = -40, h = 100) for (a = [0:60:300], r = [0, 2.6]) if (r > 0 || a == 0)
    translate([x + r * cos(a), y0, z + r * sin(a)]) rotate([-90, 0, 0]) cylinder(d = d, h = h, $fn = 10);
module dot_holes() {
    for (s = [-1, 1]) dot7(s * mic_dot[0], mic_dot[1], h = mic3[1] + 41);
    for (x = rsl_mic_x) let (m = rsl_pt([x, rsl_mic_y, -rsl_mic_h])) dot7(m[0], m[2], h = m[1] + 40 - 0.5);
}

// -----------------------------------------------------------------------------
// THE MIC: a Seeed ReSpeaker Lite (USB, 2 mics; it replaced v2's Adafruit mic) on a holder inside the chest.
// The board lies across the chest, its back (the mics' side: their sound holes face out) towards the wall, its USB-C
// towards +x (the Pi's USB end), screwed to the holder through its own 2 holes (2 x M2 x 8). The holder's base rests
// on the wall round the old mic sleeve (nothing to cut off, no glue): a key slides tight into the sleeve's pocket (the
// old mic's) and clamps the base to the wall. A tube runs from each mic straight out to its dot; the base guides a
// 1.6 mm drill for the 2 dots (7 holes each, as the chest's dots) on a body printed before they were added.
// Board frame (Seeed's 3D model, v1.1): x along it from its plain end (0) to its connector end (82), y across it from
// its top edge, z out of its back face through the board (it's z 0 .. 1.51; the mics and 2 buttons stand off its back).
// -----------------------------------------------------------------------------
rsl = [82, 34, 1.51];
rsl_mic_x = [5.5, 76.5]; rsl_mic_y = 17.0; rsl_mic_h = 1.28;   // its 2 mics (MSM261D3526H1CPM, top port), on its back
rsl_back_h = 2.58;                       // its tallest part on the back: 2 buttons at the plain end
rsl_conn_x = 64;                         // beyond here the 3.5 mm jack, the speaker JST and the USB-C
rsl_holes = [[2.49, 31.58], [60.2, 2.66]]; rsl_hole_d = 2.2;   // its 2 mounting holes (M2)
rsl_scr = [2, 8];                        // M2 x 8 into the holder's 2 posts
rsl_base_t = 1.6;                        // the holder's base, on the wall
rsl_z = mic_dot[1];                      // the mics' height: the chest's row of dots
rsl_a = atan((front_y(0, rsl_z + 5) - front_y(0, rsl_z - 5)) / 10);   // how far the wall leans back there
rsl_t = [0, -sin(rsl_a), -cos(rsl_a)];   // down the board (its y)
rsl_n = [0, cos(rsl_a), -sin(rsl_a)];    // through it, from its back into the body (its z)
rsl_zb = rsl_z - rsl_mic_h * sin(rsl_a); // so the mics' faces sit at rsl_z
// the old sleeve (sa_mic_sleeve): its block and the 45-degree wedge under it, which the board must clear; its pocket
sl4_x = [mic3[0] - 2, mic3[0] + mic_body]; sl4_z = [mic3[2] - mic_w/2 - 2.4, mic3[2] + mic_w/2 + 2.4]; sl4_y1 = mic3[1] + mic_t + 2.5;
sl4_pocket = [[mic3[0], sl4_x[1]], [mic3[2] - mic_w/2 - 0.4, mic3[2] + mic_w/2 + 0.4], mic3[1] + mic_t + 0.4];   // x, z, its back (y)
function sl4_y(z) = z >= sl4_z[0] ? sl4_y1 : sl4_y1 - (sl4_z[0] - z);
// how far behind the wall the board's back must sit: its back parts (+ 0.6) clear the holder's base everywhere, and the
// old sleeve where they pass in front of it (only 1.3 mm stands off its back, but 2.6 at its 2 buttons)
function rsl_k(u, v) = ((u > 0.3 && u < 14.4 && v > 3.6 && v < 7.9 ? rsl_back_h : rsl_mic_h) + 0.6) / cos(rsl_a);
function rsl_need(u, v) = let (x = u - rsl[0]/2, dy = (v - rsl_mic_y) * rsl_t[1], z = rsl_zb + (v - rsl_mic_y) * rsl_t[2], k = rsl_k(u, v))
    max(front_y(x, z) + (b_wall + rsl_base_t) / cos(rsl_a) + k - dy,
        x > sl4_x[0] - 0.5 && x < sl4_x[1] + 0.5 && z < sl4_z[1] + 0.5 ? sl4_y(z) + k - dy : -1e9);
rsl_yb = max([for (u = [0:1:82]) for (v = [0:1:34]) rsl_need(u, v)]);
function rsl_pt(p) = [p[0] - rsl[0]/2, rsl_yb + (p[1] - rsl_mic_y) * rsl_t[1] + p[2] * rsl_n[1], rsl_zb + (p[1] - rsl_mic_y) * rsl_t[2] + p[2] * rsl_n[2]];
rsl_M = let (o = rsl_pt([0, 0, 0])) [[1, 0, 0, o[0]], [0, rsl_t[1], rsl_n[1], o[1]], [0, rsl_t[2], rsl_n[2], o[2]], [0, 0, 0, 1]];
module place_rsl() multmatrix(rsl_M) children();
module rsl_board(s = 0) {                // board frame; s > 0 shrinks it (checks), s < 0 grows it
    difference() {
        translate([s, s, s]) cube(rsl - [2*s, 2*s, 2*s]);
        for (h = rsl_holes) translate([h[0], h[1], -1]) cylinder(d = rsl_hole_d + 2*s, h = 4, $fn = 16);
    }
    for (x = rsl_mic_x) translate([x - 1.33 + s, 15.26 + s, -rsl_mic_h + s]) cube([2.65 - 2*s, 3.5 - 2*s, rsl_mic_h]);
    for (b = [[0.83, 6.23], [8.52, 13.92]]) translate([b[0] + s, 4.11 + s, -rsl_back_h + s]) cube([b[1] - b[0] - 2*s, 3.3 - 2*s, rsl_back_h]);
    translate([39.9 + s, 14.18 + s, -1.19 + s]) cube([2.1 - 2*s, 3.77 - 2*s, 1.19]);                 // its LEDs
    translate([4.9 + s, 0.75 + s, 1.5]) cube([rsl_conn_x - 4.9 - 2*s, 32.5 - 2*s, 2.6 - s]);         // its top parts (4.03 high,
    translate([0.9 + s, 0.75 + s, 1.5]) cube([4 - 2*s, 28.5 - 2*s, 2.6 - s]);                        // clear of the screws' heads)
    translate([rsl_conn_x + s, 2.18 + s, 0.65 + s]) cube([83.6 - rsl_conn_x - 2*s, 31.42 - 2*s, 8.4 - 2*s]);   // jack, JST, USB-C
}
module rsl_screws() for (h = rsl_holes) translate([h[0], h[1], rsl[2]]) {   // board frame: M2 x 8, pan heads on its top face
    translate([0, 0, -rsl_scr[1]]) cylinder(d = rsl_scr[0], h = rsl_scr[1], $fn = 12); cylinder(d = 3.8, h = 1.4, $fn = 16); }
module rsl_plug(s = 0) translate([82.9 + s, 21.8 + s, -0.6 + s]) cube([30 - 2*s, 12 - 2*s, 7.2 - 2*s]);   // a USB-C cable's plug
// the sound tubes: straight out from each mic to its dot (along y, as the dots), ending clear of the board's back
function rsl_mic(i) = rsl_pt([rsl_mic_x[i], rsl_mic_y, -rsl_mic_h]);
function rsl_tube_end(i, r) = rsl_mic(i)[1] - (0.3 + r * sin(rsl_a)) / cos(rsl_a);
module rsl_tubes() for (i = [0, 1]) let (m = rsl_mic(i)) translate([m[0], 0, m[2]]) rotate([-90, 0, 0]) difference() {
    translate([0, 0, -40]) cylinder(d = 9.6, h = 40 + rsl_tube_end(i, 4.8), $fn = 40);
    translate([0, 0, front_y(m[0], m[2]) + (b_wall + rsl_base_t) / cos(rsl_a)]) cylinder(d = 7.2, h = 60, $fn = 40);   // the base stays: its floor
}
module rsl_holder_raw() {                // board frame; reaches back past the wall (clipped to the body's inside)
    zb = -40;
    for (y = [[0.3, 2.5], [31.5, 33.7]]) translate([0.5, y[0], zb]) cube([81, y[1] - y[0], -zb]);             // back pads
    for (h = rsl_holes) translate([h[0], h[1], zb]) cylinder(d = 5.6, h = -zb, $fn = 32);                     // the screws' posts
}
module sl4_envelope(g) hull() {          // the old sleeve, grown by g
    translate([sl4_x[0] - g, -40, sl4_z[0] - g]) cube([sl4_x[1] - sl4_x[0] + 2*g, sl4_y1 + 40 + g, sl4_z[1] - sl4_z[0] + 2*g]);
    translate([sl4_x[0] - g, -40, sl4_z[0] - g - (sl4_y1 + 40)]) cube([sl4_x[1] - sl4_x[0] + 2*g, 1, 1]);
}
module sa_rsl_holder() difference() {
    union() {
        intersection() { difference() { body_outer(b_wall); body_outer(b_wall + rsl_base_t); }
                         place_rsl() translate([-3, -4.5, -60]) cube([rsl[0] + 6, rsl[1] + 9, 60]); }     // the base
        intersection() { body_outer(b_wall); union() { place_rsl() rsl_holder_raw(); rsl_tubes(); } }
    }
    sl4_envelope(0.3);
    place_rsl() { rsl_board(-0.15); for (h = rsl_holes) translate([h[0], h[1], -rsl_scr[1] + rsl[2] - 0.8]) cylinder(d = 1.75, h = 10, $fn = 12); }   // M2 pilots
    for (i = [0, 1]) let (m = rsl_mic(i)) dot7(m[0], m[2]);                                                // drill guides
}
// the key: slides tight into the old sleeve's pocket from its open end (crush ribs on its back), its head clamps the
// holder's base to the wall beside the pocket. Prints on its side (its top face down).
rsl_key_head = 8;                        // the head's length beyond the pocket
module sa_rsl_key(ribs = true) {
    px = sl4_pocket[0]; pz = sl4_pocket[1]; c = 0.15;
    difference() {
        union() {
            // in the pocket and out over the holder's base (its head); its underside 0.1 over the base's level all along, so it
            // slides in over the base, and its crush ribs press the head down on the base
            intersection() { body_outer(b_wall + rsl_base_t + 0.1);
                             translate([px[0] + 0.4, -40, pz[0] + c]) cube([px[1] + 0.3 + rsl_key_head - px[0] - 0.4, sl4_pocket[2] - c + 40, pz[1] - pz[0] - 2*c]); }
            if (ribs) for (z = [pz[0] + 4, (pz[0] + pz[1]) / 2, pz[1] - 4]) hull() {      // crush ribs, a lead-in at the tip
                translate([px[0] + 3, sl4_pocket[2] - c - 0.01, z - 0.4]) cube([px[1] - px[0] - 3, 0.25, 0.8]);
                translate([px[0] + 0.4, sl4_pocket[2] - c - 0.01, z - 0.4]) cube([0.01, 0.01, 0.8]);
            }
        }
        translate([px[0] + 0.4, sl4_pocket[2] - c, pz[0]]) rotate([0, 0, 45]) cube([1.5, 1.5, pz[1] - pz[0]], center = false);   // a lead-in on the tip's back edge
    }
}
module rsl_dummy() { color("#1b1d22") place_rsl() rsl_board(); }
// fresh air in, high on the back
module back_vents() if (detail) for (i = [-3:3]) translate([i * 7 - 1.5, b_cy, b_top - 34]) cube([3, 120, 16]);

module sa_body() {
    difference() {
        union() {
            difference() { body_outer(); body_inner(); }
            for (k = [-1, 1]) intersection() { body_outer(); place_sh(k) sh_block(); }
            top_ledge();
            sa_speaker_ribs();
            sa_mic_sleeve();
            ups_slider_tunnel();
            chg_sleeve();
            pp4_boss();
        }
        translate([0, 0, sh0 - 5]) linear_extrude(5.02) sq2d(300, 300, 4);   // (0.02 off the ring's top: no coplanar contact)
        for (k = [-1, 1]) place_sh(k) sh_cut();
        top_cuts();
        sa_speaker_cuts();
        dot_holes();
        ups_slider_cut();
        chg_cut();
        br_holes();
        pp4_cuts();
        back_vents();
    }
}

// -----------------------------------------------------------------------------
// THE BLACK NECK: a puck on v2's coupler (3 x M3 x 12 into its own holes), the head on the puck (3 x M3 x 10)
// -----------------------------------------------------------------------------
pz0 = hd_z0 - puck_t;   // the coupler's top
module sa_puck() difference() {
    union() {
        translate([0, neck_y, pz0]) cylinder(r = puck_r, h = puck_t, $fn = 96);
        translate([0, neck_y, b_top + 1]) difference() { cylinder(r = puck_r, h = pz0 - b_top - 1 + 0.01, $fn = 96); translate([0, 0, -1]) cylinder(r = puck_skirt, h = 40, $fn = 96); }
    }
    translate([0, neck_y, pz0 - 1]) cylinder(r = neck_bore_r, h = puck_t + 2, $fn = 48);
    for (a = puck_scr) translate([0, neck_y, 0]) rotate(a) translate([head_screw_r, 0, pz0 - 1]) {
        cylinder(d = clear_m3, h = puck_t + 2, $fn = fn_small);
        translate([0, 0, 1 + puck_t - 4.5]) cylinder(d = 6.4, h = 10, $fn = 24);      // head 4.5 deep: 8.5 mm of the 12 in the coupler
    }
    for (a = head4_scr) translate([0, neck_y, 0]) rotate(a) translate([head_screw_r, 0, hd_z0 - 7.5]) cylinder(d = pilot_m3, h = 8, $fn = fn_small);
}

// -----------------------------------------------------------------------------
// HEAD: a superellipsoid (round every way), its front cut flat for the face, a soft bevel round the face
// -----------------------------------------------------------------------------
module head4_shape(i, y0 = 0, y1 = H4_D) intersection() {
    hull() {
        xz_slice(y4f + (i > 0 ? H4_face_t : 0), H4[0] * kf - H4_ch[0] - i, H4[1] * kf - H4_ch[0] - i, H4_n, z4c);   // the face
        for (yp = H4_yps) let (k = k4(yp), Y = Yc + yp) if (H4[1] * k - i > 1)
            xz_slice(y4f + min(H4_D - i, max(i > 0 ? H4_face_t : 0, Y)), H4[0] * k - i, H4[1] * k - i, H4_n, z4c);
    }
    translate([-300, y4f + y0, 0]) cube([600, y1 - y0, 800]);
}
// the hood: an arch on the head's top front, sloping back into the head, its foot flared into the head's top
module arch_slice(Y, a, b, dz = 0) translate([0, y4f + Y, hood_c + dz]) rotate([-90, 0, 0]) mirror([0, 1, 0]) linear_extrude(0.01) se2d(a, b, hood_n);
module hood_shape(i) hull() {
    arch_slice(i > 0 ? H4_face_t : 0, hood_ab[0] - H4_ch[0] - i, hood_ab[1] - H4_ch[0] - i);
    arch_slice(max(i > 0 ? H4_face_t : 0, H4_ch[1]), hood_ab[0] - i, hood_ab[1] - i);
    arch_slice(hood_d - 14, hood_ab[0] - i, hood_ab[1] - i);
    arch_slice(hood_d - i, hood_ab[0] * 0.8 - i, hood_ab[1] * 0.6 - i, -12);
    for (Y = [0, H4_ch[1], 6, 14, 24, hood_d - 24]) let (w = hood_ab[0] + 7 - (Y < H4_ch[1] ? H4_ch[0] : 0) - i)
        translate([-w, y4f + max(i > 0 ? H4_face_t : 0, Y), head_top(Y) - 4 - (Y < H4_ch[1] ? H4_ch[0] : 0)]) cube([2 * w, 0.01, 1]);
}
module head4_outer(y0 = 0, y1 = H4_D) intersection() {
    union() { head4_shape(0); hood_shape(0); if (y0 < H4_split) translate([0, neck_y, hd_z0]) cylinder(r = puck_r, h = 8, $fn = 96); }
    translate([-300, y4f + y0, 0]) cube([600, y1 - y0, 800]);
}
module head4_inner() union() { head4_shape(H4_wall); hood_shape(H4_wall); }

// the dark arch (front view, x-z): the hood's outline inset 4.5 mm, above the screen
module arch2d() intersection() { translate([0, hood_c]) se2d(hood_ab[0] - 4.5, hood_ab[1] - 4.5, hood_n); translate([-100, scr_top + 8]) square([200, 100]); }
module arch_inlay() difference() {
    translate([0, y4f - 0.01, 0]) rotate([90, 0, 0]) mirror([0, 0, 1]) linear_extrude(inlay_t + 0.01) arch2d();
    translate([0, y4f - 1, cam4_z]) rotate([-90, 0, 0]) cylinder(d = cam_aperture, h = 10, $fn = 48);
}

h4_scr_bosses_y = scr_back_y + 14;       // the screen's bosses run back to here (wells from behind)
cov4_tab_z = head_top(H4_split + 10, 46) - H4_wall - 0.5 - 2.5;   // the frame's tabs' underside: under the cover's roof at their back, outer corner
// the frame's lip: a ring just inside the cover's rim (locates it), joined to the frame's wall by a ledge
module frame_lip() difference() {
    union() {
        intersection() { head4_shape(H4_wall + 0.3); translate([-300, y4f + H4_split - 1.5, 0]) cube([600, 4.5, 800]); }
        intersection() { head4_shape(H4_wall - 0.5); translate([-300, y4f + H4_split - 1.5, 0]) cube([600, 1.5, 800]); }
    }
    head4_shape(H4_wall + 1.6);
    for (x = cov4_scr) translate([x - 8, 0, hd_z0 - 1]) cube([16, 300, 16]);
    for (x = cov4_tabs) translate([x - 11, 0, cov4_tab_z - 6]) cube([22, 300, 40]);
    translate([-38, 0, hd_z0 - 1]) cube([76, 300, 12]);                                  // under the AA box
}
// the 4xAA box (65 x 70 x 20) stands on the head's floor behind the neck, lid backwards: rails, a stop in front
bat4_z0 = hd_z0 + 5.2;                   // on the floor (it rises towards the back)
module bat4_cradle() intersection() {
    head4_shape(0.01, 0, H4_split);
    difference() {
        union() {
            for (s = [-1, 1]) translate([s > 0 ? bat[0] / 2 + bat4_clr : -bat[0] / 2 - bat4_clr - 3, y4f + bat4_y - 2, hd_z0]) cube([3, bat[2] + 2 * bat4_clr + 2, 5.2 + 30]);
            for (s = [-1, 1]) translate([s > 0 ? 14 : -26, y4f + bat4_y - 2, hd_z0]) cube([12, 2, 5.2 + 18]);   // stops, beside the bore
            translate([-bat[0] / 2, y4f + bat4_y, hd_z0]) cube([bat[0], bat[2] + 2 * bat4_clr, 5.2]);       // its seat, level
        }
        for (s = [-1, 1]) for (zz = [12, 26]) translate([s * (bat[0] / 2 + bat4_clr - 0.2), y4f + bat4_y + bat[2] / 2, bat4_z0 + zz]) cube([0.8, 3, 8], center = true);   // crush ribs
    }
}
module bat4_dummy() color("#151515") translate([-bat[0] / 2, y4f + bat4_y + bat4_clr, bat4_z0]) cube([bat[0], bat[2], bat[1]]);
// the camera bracket hangs between 2 cheeks: walls beside it from the face back past its pivots, up into the roof (they
// print straight up from the face). Its 2 pivot screws (M3 x 12) go in from inside the head: heads on the cheeks' outer
// faces, at |x| = cam4_scr_x, 5 mm into the bracket's pivots.
cheek4_x = [cam_brk_wo + 0.2, cam4_scr_x];              // 19.2 .. 26
cam_piv = [y4f + cam_board_y + cam_pivot_dy, cam4_z];    // the pivots (y, z)
module cam_cheeks() for (s = [-1, 1]) mirror([s < 0 ? 1 : 0, 0, 0]) translate([cheek4_x[0], 0, 0]) rotate([90, 0, 90])
    linear_extrude(cheek4_x[1] - cheek4_x[0]) hull() {
        translate([y4f + H4_face_t - 0.01, cam4_z - 14]) square([4, 44]);             // along the face
        translate(cam_piv) circle(r = 7, $fn = 40);                                     // round the pivot
        translate([cam_piv[0] - 7, cam4_z]) square([14, 30]);                          // up into the roof
    }
// for a head frame printed with the old bosses (before 2026-11-10): the cheeks on a plate glued inside the face
cradle_win = [20, 26];                                   // the lens's window through the plate (its tilt)
module sa_cam_cradle() difference() {
    intersection() {
        union() { head4_shape(H4_wall + 0.4); hood_shape(H4_wall + 0.4); }
        union() {
            cam_cheeks();
            translate([-cheek4_x[1], y4f + H4_face_t, cam4_z - 18]) cube([2 * cheek4_x[1], 2.5, 36]);
        }
    }
    translate([0, 0, cam4_z]) rotate([-90, 0, 0]) linear_extrude(200) rr(cradle_win[0], cradle_win[1], 4);
    for (s = [-1, 1]) translate([s * 24, cam_piv[0], cam_piv[1]]) rotate([0, 90, 0]) cylinder(d = clear_m3, h = 20, center = true, $fn = fn_small);
}
module sa_head_frame_body(cheeks = true) {
    zt = hd_z0 + 2 * H4[1];
    difference() {
        union() {
            difference() { head4_outer(0, H4_split); head4_inner(); }
            // the screen's 4 bosses (behind its corner standoffs), webbed to the roof / floor
            intersection() {
                head4_shape(0.01);
                for (h = scr_holes) let (q = hole_xz(h), up = q[1] > scr_cz ? 1 : -1) {
                    translate([q[0], scr_back_y, q[1]]) rotate([-90, 0, 0]) cylinder(d = 9.5, h = h4_scr_bosses_y - scr_back_y);
                    translate([q[0] - 3, scr_back_y, up > 0 ? q[1] : hd_z0]) cube([6, h4_scr_bosses_y - scr_back_y, up > 0 ? zt - q[1] : q[1] - hd_z0]);
                }
            }
            // the floor round the neck: 4 mm
            intersection() { head4_shape(0.01); translate([0, neck_y, hd_z0]) cylinder(r = puck_r, h = 4, $fn = 96); }
            // the HDMI coupler's cradle: two clips on the floor
            let (cb = cpl_c[2] - hdmi_cpl[1] / 2 - (hd_z0 + 1)) for (dx = [-11, 11])
                intersection() { head4_shape(0.01); translate([cpl_c[0] + dx - 3, cpl_c[1] - hdmi_cpl[2]/2 - 1.8, hd_z0 + 1]) difference() {
                    cube([6, hdmi_cpl[2] + 3.6, hdmi_cpl[1] * 0.6 + cb]);
                    translate([-1, 1.5, cb]) cube([8, hdmi_cpl[2] + 0.6, 40]);
                } }
            bat4_cradle();
            // the back cover's fixings, all reaching back past the split (they print upwards: the frame prints face down)
            frame_lip();
            for (x = cov4_scr) intersection() {      // bosses over the cover's floor: its 2 screws come up into them
                union() { head4_shape(0.01, 0, H4_split); head4_shape(H4_wall + 0.3, H4_split - 0.01, H4_split + 10); }
                translate([x - 6, y4f + H4_split - 12, hd_z0]) cube([12, 22, 14]);
            }
            for (x = cov4_tabs) {                    // tabs into the slots under the cover's roof, from blocks under the frame's roof
                intersection() { head4_shape(0.01); translate([x - 5.7, y4f + H4_split - 12, cov4_tab_z]) cube([11.4, 12, 40]); }
                translate([x - 5.7, y4f + H4_split - 1, cov4_tab_z]) cube([11.4, 11, 2.5]);
            }
            // the camera bracket's 2 cheeks, fused to the face and the roof
            if (cheeks) intersection() { union() { head4_shape(0.01); hood_shape(0.01); } cam_cheeks(); }
        }
        // the screen: its opening (bz_gap clear of the glass all round) and its pocket
        translate([0, y4f - 1, scr_cz]) rotate([-90, 0, 0]) linear_extrude(H4_face_t + 2) rr(scr[0] + 2 * bz_gap, scr[1] + 2 * bz_gap, 0.8);
        translate([0, g3_y - 1, scr_cz]) rotate([-90, 0, 0]) linear_extrude(scr[2] + 1.2) rr(scr[0] + 0.6, scr[1] + 0.6, 2);
        for (h = scr_holes) let (q = hole_xz(h)) translate([q[0], 0, q[1]]) {
            translate([0, scr_back_y - 1, 0]) rotate([-90, 0, 0]) cylinder(d = 6.2, h = 1 + scr_standoff_h);
            translate([0, scr_back_y, 0]) rotate([-90, 0, 0]) cylinder(d = 3.4, h = 60, $fn = 16);
            translate([0, scr_back_y + scr_standoff_h + 2, 0]) rotate([-90, 0, 0]) cylinder(d = 6.4, h = 60, $fn = 24);
        }
        // the neck: the cables' bore, 3 screws into the puck (M3 x 10 through the 4 mm floor)
        translate([0, neck_y, hd_z0 - 1]) cylinder(r = neck_bore_r, h = 10, $fn = 48);
        for (a = head4_scr) translate([0, neck_y, 0]) rotate(a) translate([head_screw_r, 0, hd_z0 - 1]) cylinder(d = clear_m3, h = 10, $fn = fn_small);
        for (x = cov4_scr) translate([x, y4f + H4_split + 5, hd_z0]) cylinder(d = pilot_m3, h = 16, $fn = fn_small);   // the cover's screws
        // camera: the lens, the pivot screws through the cheeks (M3 x 12, from inside the head)
        translate([0, y4f - 1, cam4_z]) rotate([-90, 0, 0]) cylinder(d = cam_aperture, h = H4_face_t + 2, $fn = 48);
        for (s = [-1, 1]) translate([s * 24, cam_piv[0], cam_piv[1]]) rotate([0, 90, 0]) cylinder(d = clear_m3, h = 20, center = true, $fn = fn_small);
    }
}
module sa_head_frame() difference() { sa_head_frame_body(); arch_inlay(); }   // white
module sa_head_inlay() intersection() { sa_head_frame_body(); arch_inlay(); } // dark: printed in the same print

// the back: slots under its roof for the frame's tabs, 2 screws from below, slits (the concept's back)
module sa_head_back() {
    difference() {
        union() {
            difference() { head4_outer(H4_split, H4_D); head4_inner(); }
            for (x = cov4_tabs) intersection() {
                head4_shape(0.01, H4_split, H4_D);
                translate([x - 9, y4f + H4_split, cov4_tab_z - 3]) difference() {
                    cube([18, 14, 20]);
                    translate([3, -1, 2.7]) cube([12, 12.5, 2.5 + 0.6]);
                }
            }
        }
        for (x = cov4_scr) translate([x, y4f + H4_split + 5, hd_z0 - 1]) {   // M3 x 12 from below into the frame's bosses
            cylinder(d = clear_m3, h = 16, $fn = fn_small);
            cylinder(d = 6.4, h = 1 + 5, $fn = 24);
        }
        if (detail) for (s = [-1, 1]) for (k = [-1.5:1:1.5]) translate([s * 44 + k * 6 - 1.5, y4f + H4_split + 5, z4c + 18]) cube([3, 100, 13]);
    }
}

// -----------------------------------------------------------------------------
// ARMS (local frame of the +x shoulder, as v3's: x out from the shoulder face, y = robot y, z up the face):
// shoulder, arm and hand as one smooth piece, flat on its inner face (it prints on it)
// -----------------------------------------------------------------------------
arm4 = [[0, 0, 0], [0, 3, -48], [0, -12, -82]];         // the arm's centre line (a curve): shoulder, bend, wrist
arm4_r = [[24, 27], [21, 23.5], [17, 19]];              // half width, half thickness: shoulder, middle, wrist (chunky, as the concept)
function bez4(t) = (1 - t) * (1 - t) * arm4[0] + 2 * (1 - t) * t * arm4[1] + t * t * arm4[2];
function r4(t) = t < 0.5 ? arm4_r[0] + (arm4_r[1] - arm4_r[0]) * 2 * t : arm4_r[1] + (arm4_r[2] - arm4_r[1]) * (2 * t - 1);
module hand4() {
    w = bez4(1); d = (arm4[2] - arm4[1]) / norm(arm4[2] - arm4[1]); s = [0, -d[2], d[1]];   // down the hand; across it (backwards)
    hull() { eball(w + [5.7, 0, 0], 17, 19); eball(w + 15 * d - 1 * s + [4, 0, 0], 18.5, 13); }                  // wrist into palm
    for (i = [0:3]) let (o = (-1.5 + i) * 7.4, L = [15, 18, 17, 13][i], fan = (-1.5 + i) * 4, f0 = w + 24 * d + o * s,
                         dir = cos(fan) * d + sin(fan) * s)
        hull() { eball(f0 + [2.5, 0, 0], 3.3, 6.5); eball(f0 + L * dir + [2.5, 0, 0], 2.9, 5.5); }              // fingers
    let (t0 = w + 11 * d - 13 * s, td = (d - 0.9 * s) / norm(d - 0.9 * s))
        hull() { eball(t0 + [2.5, 0, 0], 4.8, 7); eball(t0 + 15 * td + [2.5, 0, 0], 3.5, 5.5); }                // thumb (front)
}
module sa_arm_solid() intersection() {
    union() {
        for (i = [0:11]) hull() for (t = [i / 12, (i + 1) / 12]) let (r = r4(t)) eball(bez4(t) + [r[1] * 0.3, 0, 0], r[0], r[1]);
        hand4();
    }
    translate([0, -200, -200]) cube([60, 400, 400]);
}
module sa_arm() difference() {
    translate([arm_gap3, 0, 0]) sa_arm_solid();
    translate([arm_gap3 - eps, 0, 0]) rotate([0, 90, 0]) horn_pocket(horn_plate_t + 0.6, 15);
    translate([arm_gap3 - 1, 0, 0]) rotate([0, 90, 0]) cylinder(d = 5.5, h = cap_d);
    for (a = [0, 90, 180, 270]) for (r = [7.5, 10.5]) translate([arm_gap3 - 1, r * cos(a), r * sin(a)]) rotate([0, 90, 0]) cylinder(d = 1.3, h = 7, $fn = 10);
}

// -----------------------------------------------------------------------------
// STACK KEEPER (overrides v2's): v2's was a 1.6 mm bar (0.6 mm over its V-groove) so it fitted under v2's base, and its
// 6.4 mm screw recesses were wider than the 6 mm bar: it broke. v4 has room above the stack: a 9 x 9 mm bar over the
// same V, the ends 11 mm wide round the screw heads. The same seat, so the same 2 x M3 x 20 into v2's floor.
// Two arms reach back over the stack's ends and rest on the UPS board's and the Pi's top edges (clear of their ports,
// the standoffs and the slider), so the Pi end can't rock; fingers beside the boards' ends stop it sliding along the plate.
// Robot coordinates; prints top face down.
// -----------------------------------------------------------------------------
keeper4_h = 9;                           // the bar's top, above its V's mouth
keeper4_w = 9;                           // the bar's width (across the plate)
keeper4_end = [11, 5.5];                 // the ends: width round the screw heads, reach beyond the screws
keeper4_top = keeper_mouth(ups_plate_t, ups_plate[1]) + keeper4_h;
keeper4_arm = 10;                        // the arms' width, centred on the UPS board's ends
module keeper4_arms() {
    zb = stk_ztop;                                                                   // the boards' top (port) edges
    ups_y = [stk_y0 + stk_ups_w0, stk_y0 + stk_ups_w0 + ups_pcb_t]; pi_y = [stk_y0 + stk_pi_w0, stk_y0 + stk_pi_w1];
    y0 = stk_yc + keeper4_w/2 - eps; y1 = pi_y[1] + 2.5;
    for (xb = [stk_x0, stk_x0 + ups_l]) translate([xb - keeper4_arm/2, y0, zb]) cube([keeper4_arm, y1 - y0, keeper4_top - zb]);
    // fingers 0.3 mm beyond the boards' ends, 4 mm down: the UPS board's two ends, the Pi's left end (its USB / Ethernet
    // jacks overhang its right end)
    for (f = [[stk_x0 - 1.4, ups_y], [stk_x0 + ups_l + 0.3, ups_y], [stk_x0 - 1.4, pi_y]])
        translate([f[0], f[1][0] - 1, zb - 4]) cube([1.1, f[1][1] - f[1][0] + 2, 4 + eps]);
}
module stack_keeper() {
    zm = keeper_mouth(ups_plate_t, ups_plate[1]);
    x0 = keeper_x[0] - keeper4_end[1]; x1 = keeper_x[1] + keeper4_end[1];
    px0 = stk_x0 + ups_l/2 - plate_l_max/2 - 0.2; px1 = stk_x0 + ups_l/2 + plate_l_max/2 + 0.2;   // where a plate can be
    wz1 = keeper_mouth_lo - 1;                                                       // the floor's wall in front: its top
    seat = zm + keeper_t - 1.8;                                                      // the screw heads' seat, as v2's keeper
    difference() {
        union() {
            translate([x0, stk_yc - keeper4_w/2, zm]) cube([x1 - x0, keeper4_w, keeper4_h]);                       // the bar
            keeper4_arms();
            for (e = [[x0, px0], [px1, x1]]) {
                translate([e[0], wall_y1 + 0.2, zm - keeper_drop]) cube([e[1] - e[0], stk_yc + keeper4_w/2 - wall_y1 - 0.2, keeper_drop + eps]);   // down to the bosses
                translate([e[0], stk_yc - keeper4_end[0]/2, wz1 + 0.4]) cube([e[1] - e[0], keeper4_end[0], zm + keeper4_h - wz1 - 0.4]);   // round the heads
            }
        }
        vee_cut(px0, px1, zm, false);
        // room for the upper long standoffs (a short, thin plate puts them up to 0.53 mm above the mouth)
        for (u = [3.5, 84.5]) translate([stk_x0 + u - 4.2, stk_yc + 0.6, zm - 1]) cube([8.4, 5, 1 + 1.1]);
        for (x = keeper_x) translate([x, stk_yc, zm - keeper_drop - 1]) {
            cylinder(d = clear_m3, h = 30, $fn = fn_small);
            translate([0, 0, seat - (zm - keeper_drop - 1)]) cylinder(d = 6.4, h = 20, $fn = 24);              // the head, sunk
        }
    }
}

// -----------------------------------------------------------------------------
// LAYOUT
// -----------------------------------------------------------------------------
module eyes4_2d() for (sx = [-1, 1]) translate([sx * 38, 4]) intersection() {   // happy closed eyes, as the concept
    difference() { circle(r = 17, $fn = 64); circle(r = 10.5, $fn = 64); }
    translate([0, 20 + 3]) square(40, center = true);
}
module sa_electronics() {
    color("#d9d4c8") base_floor();
    color("#c8c2b4") { stack_keeper(); pca_clamp(0); pca_clamp(1); }
    ups_stack(); color("#2a2") pca_floor(); color("#333") usbc_socket();
    speaker3_dummy(); mic3_dummy(); ups_knob_dummy();
}
module sa_layout() {
    sa_electronics();
    v3_neck_parts(head_angle);
    color(DARK4) v3_ring();
    color(WHITE4) sa_body();
    color(WHITE4) v3_neck_plate();
    color(WHITE4) v3_ups_slider();
    color(DARK4) render() sa_port_panel();
    place_head3(head_angle) {
        color(DARK4) sa_puck();
        color(WHITE4) render() sa_head_frame(); color(DARK4) render() sa_head_inlay(); color(WHITE4) render() sa_head_back();
        screen_dummy3(); screen_plugs3(); bat4_dummy();
        color("#4FE3E0") translate([0, g3_y - 0.05, scr_cz]) rotate([90, 0, 0]) linear_extrude(0.4) eyes4_2d();
        color("#aab") place_cam4() camera_bracket();
    }
    for (s = [-1, 1]) { arm_servo3(s); color(WHITE4) place_arm3(s, arm_angle) sa_arm(); }
}
if (part == "layout4") translate([0, b_cy, 0]) rotate(spin) translate([0, -b_cy, 0]) {
    if (cut) difference() { sa_layout(); translate([0, -200, -10]) cube([400, 400, 600]); } else sa_layout();
}

// -----------------------------------------------------------------------------
// PRINT (part = "print4", which = name): each part in its print orientation, on z = 0
// -----------------------------------------------------------------------------
module rsl_print() let (Mi = [[1, 0, 0, 0], [0, rsl_t[1], rsl_t[2], 0], [0, rsl_n[1], rsl_n[2], 0], [0, 0, 0, 1]], o = rsl_pt([0, 0, 0]))
    multmatrix(Mi) translate(-o) sa_rsl_holder();   // in the board's frame (z up through it, the base underneath); export_v4.sh drops it onto z = 0
module print4(w) {
    if (w == "body")        translate([0, 0, -sh0]) sa_body();                                                   // upright (5 mm brim)
    if (w == "ring")        v3_ring();                                                                          // upright
    if (w == "neck_plate")  translate([0, 0, b_top]) mirror([0, 0, 1]) v3_neck_plate();                        // top face down
    if (w == "head_frame")  translate([0, 0, -y4f]) rotate([90, 0, 0]) sa_head_frame();                       // face down (tree supports)
    if (w == "head_inlay")  translate([0, 0, -y4f]) rotate([90, 0, 0]) sa_head_inlay();                       // the same print, dark filament
    if (w == "head_back")   translate([0, 0, -(y4f + H4_split)]) rotate([90, 0, 0]) sa_head_back();           // rim down (tree supports inside)
    if (w == "puck")        translate([0, 0, hd_z0]) mirror([0, 0, 1]) sa_puck();                             // top down
    if (w == "port_panel")  translate([0, 0, -pp4_z0]) sa_port_panel();                                       // upright, as it sits (3 mm brim)
    if (w == "arm_left")    rotate([0, -90, 0]) translate([-arm_gap3, 0, 0]) sa_arm();                         // inner face down
    if (w == "arm_right")   mirror([0, 1, 0]) print4("arm_left");
    if (w == "ups_slider")  translate([0, 0, sl_z0 + sl_h]) mirror([0, 0, 1]) v3_ups_slider();               // top down
    if (w == "camera_cradle") translate([0, 0, -(y4f + H4_face_t)]) rotate([90, 0, 0]) sa_cam_cradle();     // plate down (old head frames only)
    if (w == "stack_keeper") translate([0, 0, keeper4_top]) mirror([0, 0, 1]) stack_keeper();                // top face down
    if (w == "mic_holder")  rsl_print();                                                                     // base down (tree supports under it)
    if (w == "mic_key")     translate([0, 0, -sl4_pocket[1][0] - 0.15]) sa_rsl_key();                     // its bottom face down
}
if (part == "print4") print4(which);

// -----------------------------------------------------------------------------
// ASSEMBLED (part = "assy4", which = name): each component where it sits in the robot, for the assembled 3MF
// -----------------------------------------------------------------------------
module assy4(w) {
    if (w == "ring")        v3_ring();
    if (w == "body")        sa_body();
    if (w == "neck_plate")  v3_neck_plate();
    if (w == "ups_slider")  v3_ups_slider();
    if (w == "port_panel")  sa_port_panel();
    if (w == "arm_left")    place_arm3(-1, arm_angle) sa_arm();
    if (w == "arm_right")   place_arm3(1, arm_angle) sa_arm();
    if (w == "puck")        place_head3(head_angle) sa_puck();
    if (w == "head_frame")  place_head3(head_angle) sa_head_frame();
    if (w == "head_inlay")  place_head3(head_angle) sa_head_inlay();
    if (w == "head_back")   place_head3(head_angle) sa_head_back();
    if (w == "camera_bracket") place_head3(head_angle) place_cam4() camera_bracket();
    if (w == "camera_cradle") place_head3(head_angle) sa_cam_cradle();
    if (w == "mic_holder")  sa_rsl_holder();
    if (w == "mic_key")     sa_rsl_key();
    if (w == "respeaker")   { place_rsl() rsl_board(); }
    if (w == "floor")       base_floor();
    if (w == "floor_clips") { stack_keeper(); pca_clamp(0); pca_clamp(1); }
    if (w == "coupler")     place_torso() translate([0, neck_yt, torso_h]) coupler();
    if (w == "gears")       place_torso() { translate([0, neck_yt, gear_z0]) ring_gear(); translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); }
    if (w == "servo_frame") place_torso() translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    if (w == "servos")      { place_torso() translate([0, pinion_yt, hs_spline_z]) servo_dummy(); for (k = [-1, 1]) arm_servo3(k); }
    if (w == "pi_ups")      { ups_stack(); ups_plugs(); }
    if (w == "pca")         pca_floor();
    if (w == "speaker")     speaker3_dummy();
    if (w == "mic")         mic3_dummy();
    if (w == "aa_box")      place_head3(head_angle) bat4_dummy();
    if (w == "charge_socket") { usbc_socket(); charge_cable(); }
    if (w == "screen")      place_head3(head_angle) screen_dummy3();
    if (w == "screen_plugs") place_head3(head_angle) screen_plugs3();
    if (w == "eyes")        place_head3(head_angle) translate([0, g3_y - 0.05, scr_cz]) rotate([90, 0, 0]) linear_extrude(0.4) eyes4_2d();
    if (w == "camera")      place_head3(head_angle) {
                                translate([-cam_board/2, y4f + cam_board_y, cam4_z - cam_board/2]) cube([cam_board, cam_pcb_t, cam_board]);
                                translate([0, y4f + cam_board_y, cam4_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - H4_face_t - 0.5); }
}
if (part == "assy4") assy4(which);

echo(str("ReSpeaker holder: board back face at y ", rsl_yb, ", wall lean ", rsl_a, " deg, mics at ", rsl_pt([rsl_mic_x[0], rsl_mic_y, -rsl_mic_h]), " and ", rsl_pt([rsl_mic_x[1], rsl_mic_y, -rsl_mic_h])));
echo(str("LittleBot v4: total height ", hood_c + hood_ab[1], " mm; head ", 2 * H4[0], " x ", 2 * H4[1], " x ", H4_D,
         "; body widest ", 2 * egg4[0], " x ", 2 * egg4[1], "; shoulders ", sh_c, "; face ", 2 * H4[0] * kf, " x ", 2 * H4[1] * kf));
