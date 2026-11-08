// =============================================================================
//  LittleBot v3 "Sleek" - parametric OpenSCAD model (mm)
// =============================================================================
//  A tapered one-piece body on v2's floor, a 7" screen head turning on v2's geared neck,
//  and one-piece arms on the two shoulder servos. Few, big parts:
//    v3_body        the whole body, base to shoulders (prints UPRIGHT, open top and bottom, no supports)
//    v3_neck_plate  closes the top; carries v2's neck (coupler, gears, servo frame) - assembled on the bench
//    v3_arm_left, v3_arm_right   upper arm + forearm + hand in one piece (inner face down); swap = 1 screw
//    v3_head, v3_head_back       (layout only until the screen arrives and is measured)
//  Reused from v2 as printed: base_floor (with the Pi + UPS, stack_keeper, pca_clamps), coupler, ring_gear,
//  pinion, servo_frame, camera_bracket.
//  Every screw is an M3 button head from the kit (2 mm key) going into a nut or into a hole an earlier
//  v2 screw already threaded; nothing is drilled. The servos use their own screws, as in v2.
//
//  Coordinates as in v2: x across (+x = the viewer's right), y front -> back (front at y = 0), z up from the table.
//    openscad -o layout.png -D 'part="layout"' littlebot_v3.scad
// =============================================================================
include <../enclosure_v2/littlebot_v2.scad>
part = "layout";       // overrides v2's default
cut = false;           // layout: cut away x > 0 to show the inside
arm_angle = 8;         // layout / checks: arm angle, degrees (+ = forwards / up), v2's neutral is 8
head_angle = 0;

/* [v3 body] */
// sections, bottom to top: [width, depth, front y, front corner r, back corner r, z]. The bottom is v2's base outline,
// so v2's floor fits; the profile only ever narrows going up, so the body prints upright without supports.
b_secs = [
    [base_w, base_d,  0, base_rf, base_rb,   0],
    [base_w, base_d,  0, base_rf, base_rb,  42],
    [150,    130,    10, 55,      45,      150],
    [134,    110,    19, 50,      40,      184],
    [118,     94,    27, 44,      34,      197]];
b_wall = base_wall;                     // 2.6, as v2's base (the floor is 0.3 inside it)
b_top = b_secs[len(b_secs) - 1][5];     // 197 = v2's torso top: the neck, gears and coupler sit as in v2
skirt_top = 24;                         // dark colour below this (filament change at this height)

/* [v3 shoulders] (the +x side; the -x side is its mirror image) */
sh_c = [74, 82, 166];                   // centre of the flat shoulder face (the arm's axis)
sh_tilt = 8;                            // the axis points 8 degrees up: the hanging arm splays out, clear of the wider body below
sh_face_d = 50;                         // flat face (part of the body's hull)
arm_gap3 = 3;                           // shoulder face -> arm's inner face
sv_spline_top = arm_gap3 + horn_plate_t - 0.6;          // local x of the servo's spline top (v2's horn fit)
sv_tab_top = sv_spline_top - (sv_spline_h - sv_tab_z - sv_tab_t);   // the tabs' outer face, inside the face
sv_slot = [sv_tab_l + 0.6, sv_w + 0.6]; // the servo goes in from outside through this slot (local y, z)
sv_seat_t = 5;                          // the tabs rest on this, their screws go into it

/* [v3 neck plate] */
np_t = torso_top_t;                     // 3, as v2's torso top
np_clr = 0.3;
np_ledge = 5;                           // the ledge under the plate's edge
np_screw_pts = [[-32, 40], [32, 40], [-30, 110], [30, 110]];   // 4 x M3 x 8 from the top into nuts (the back two are under the head)

/* [v3 floor] */
floor_screw_z = well_low_z;             // the floor's four well-cover posts: M3 x 10 through the skirt into them

/* [v3 arms] */
ua_len = 50; fa_len = 44; elbow_bend = 30;
arm3_t = 22;                            // arm thickness (shoulder cap, upper arm); the hand is thinner
cap_d = 48;                             // shoulder cap: covers the servo slot at every angle

/* [v3 head - provisional until the screen is measured] */
scr = [160, 9, 100];                    // GeeekPi 7" (listing: 160 x 100 x 9); its ports and volume wheel are on its left end (seen from the back)
hd = [200, 64, 156];                    // head: width, depth, height
hd_r = 16;
hd_scr_z = -14;
hd_cam_z = hd_scr_z + scr[2]/2 + 20;
hd_neck_y = 34;
hd_y0 = neck_y - hd_neck_y;
hd_z0 = head_z0;                        // 207.8: on v2's coupler

// -----------------------------------------------------------------------------
// helpers
// -----------------------------------------------------------------------------
function sec_at(i) = b_secs[i];
// a horizontal hole along x with its top pointed (45 degrees), so it prints upright without sagging
module tear_x(d, len, center = false) translate([center ? -len/2 : 0, 0, 0]) rotate([0, 90, 0]) linear_extrude(len)
    hull() { circle(d = d, $fn = 24); translate([-d/2 * 1.414, 0]) square(0.01, center = true); }
module sec2d(s, inset = 0) translate([0, s[2]]) offset(delta = -inset) shape2d(s[0], s[1], s[3], s[4]);
module slab(z0, z1) translate([-300, -300, z0]) cube([600, 600, z1 - z0]);
// the shoulder frame: local x = the arm's axis (outwards), y = robot y, z = up the face
module place_sh(s) { if (s > 0) translate(sh_c) rotate([0, -sh_tilt, 0]) children();
                     else mirror([1, 0, 0]) translate(sh_c) rotate([0, -sh_tilt, 0]) children(); }
function sh_pt(s, p) = let (q = [p[0] * cos(sh_tilt) - p[2] * sin(sh_tilt), p[1], p[0] * sin(sh_tilt) + p[2] * cos(sh_tilt)] + sh_c)
    [s * q[0], q[1], q[2]];

// -----------------------------------------------------------------------------
// BODY
// -----------------------------------------------------------------------------
module body_outer(inset = 0) {
    hull() {
        for (s = b_secs) translate([0, 0, s[5] - (s[5] == b_top ? 0.01 : 0)]) linear_extrude(0.01) sec2d(s, inset);
        for (k = [-1, 1]) place_sh(k) translate([-inset - 0.01, 0, 0]) rotate([0, 90, 0]) cylinder(d = sh_face_d - 2 * inset, h = 0.01);
    }
}
module body_inner() intersection() { body_outer(b_wall); slab(-1, b_top + 1); }

// shoulder pocket (local frame): the slot through the face, the seat under the tabs, the servo below it
module sh_block() {      // solid added inside the wall around the servo
    hull() {
        translate([sv_tab_top - sv_tab_t - sv_seat_t, -15.5, -10.5]) cube([sv_seat_t + sv_tab_t - sv_tab_top - 0.5, 40, 21]);
        translate([-b_wall - 0.5, -15.5, -10.5 - 28]) cube([0.5, 40, 21 + 28]);           // the underside slopes steeply to the wall (prints)
    }
}
module sh_cut() {
    translate([sv_tab_top - sv_tab_t, sv_mid - sv_slot[0]/2, -sv_slot[1]/2]) cube([30, sv_slot[0], sv_slot[1]]);     // slot: tabs + case top
    translate([-40, sv_mid - sv_l/2 - clr, -sv_w/2 - clr]) cube([40 + 1, sv_l + 2*clr, sv_w + 2*clr]);              // the case through the seat
    for (hy = [sv_hole_a, sv_hole_b]) translate([sv_tab_top - sv_tab_t + 0.01, hy, 0]) rotate([0, -90, 0])
        cylinder(d = pilot_m2, h = sv_seat_t + 1, $fn = fn_small);                                                     // the servo's own screws
}

// the floor: four M3 x 10 through the skirt into its well-cover posts (their pilots are already threaded by v2's covers)
function floor_screw_pts() = [for (s = [-1, 1]) for (u = [-1, 1]) [s, wheel_y + u * well_screw_u]];
// floor stops: short ribs over the floor's top edge (it rests on them while the body is upside down)
floor_stops = [[-20, 0], [20, 0], [-55, 1], [55, 1], [0, 2]];   // [x or y, side]: 0 front, 1 back (x), 2 = both sides (y)
module floor_stop_ribs() {
    zt = floor_low + floor_low_t + 0.2;
    intersection() {
        translate([0, 0, zt]) linear_extrude(4) difference() { sec2d(b_secs[0], b_wall - 0.01); sec2d(b_secs[0], b_wall + 1.6); }
        union() {
            for (x = [-20, 20]) translate([x - 5, -1, 0]) cube([10, 20, 40]);
            for (x = [-50, 50]) translate([x - 5, base_d - 20, 0]) cube([10, 21, 40]);
            for (s = [-1, 1]) translate([s > 0 ? base_w/2 - 20 : -base_w/2 - 1, wheel_y - 5, 0]) cube([21, 10, 40]);
        }
    }
}

// the top: a ledge the neck plate rests on (45-degree underside), four nut bosses
module np_outline(extra = 0) sec2d(b_secs[len(b_secs) - 1], b_wall + np_clr - extra);
module top_ledge() {
    zt = b_top - np_t;
    difference() {
        intersection() { body_inner(); slab(zt - 40, zt); }
        hull() { translate([0, 0, zt - 2]) linear_extrude(10) np_outline(-np_ledge); translate([0, 0, zt - 2 - 40]) linear_extrude(0.01) np_outline(-np_ledge + 40); }
    }
    // nut bosses at the front corners, down to the wall at 45 degrees
    intersection() {
        body_inner();
        for (p = np_screw_pts) hull() { translate([p[0], p[1], zt - 7]) cylinder(d = 11, h = 7);
            translate(p[1] < neck_y ? [p[0] + sign(p[0]) * 12, p[1] - 24, zt - 34] : [p[0] + sign(p[0]) * 8, p[1] + 34, zt - 40]) cylinder(d = 11, h = 0.01); }
    }
}
module top_cuts() {
    zt = b_top - np_t;
    for (p = np_screw_pts) translate([p[0], p[1], zt - 12]) {
        cylinder(d = clear_m3, h = 13, $fn = fn_small);
        translate([0, 0, 12 - 2.6]) rotate(30) cylinder(d = 6.35 + 0.3, h = 3, $fn = 6);                  // M3 nut, dropped in from above
    }
}

// speaker (Adafruit 3369) behind a chest grille; mic (Adafruit 3367) in a sleeve above it
spk3_c = [0, 19, 121];                // speaker front face centre
module speaker3_dummy() color("#333") translate([-spk_w/2, spk3_c[1], spk3_c[2] - spk_h/2]) cube([spk_w, spk_d, spk_h]);
module speaker_ribs() {
    intersection() {
        body_inner();
        for (s = [-1, 1]) let (x0 = s > 0 ? spk_w/2 + 0.3 : -spk_w/2 - 3.3) {
            hull() {      // the rib: from the front wall back past the speaker, its underside at 45 degrees
                translate([x0, 0, spk3_c[2] - spk_h/2 - 2]) cube([3, spk3_c[1] + spk_d + 3, spk_h + 4]);
                translate([x0, 0, spk3_c[2] - spk_h/2 - 2 - (spk3_c[1] + spk_d + 3)]) cube([3, 1, 1]);
            }
            hull() {      // a foot under the speaker's lower edge
                translate([s > 0 ? spk_w/2 - 3 : -spk_w/2, 0, spk3_c[2] - spk_h/2 - 2]) cube([3, spk3_c[1] + spk_d - 4, 2]);
                translate([s > 0 ? spk_w/2 + 0.3 : -spk_w/2, 0, spk3_c[2] - spk_h/2 - 6]) cube([0.01, spk3_c[1] + spk_d - 4, 2]);
            }
        }
    }
}
module speaker_cuts() {
    if (detail) for (row = [-3:3]) for (gx = [-35:5:35]) let (px = gx + (abs(row) % 2) * 2.5, pz = row * 4.4)
        if (in_stadium(px, pz, spk_grille_hw, spk_grille_r)) translate([px, -1, spk3_c[2] + pz]) rotate([-90, 0, 0]) cylinder(d = 3, h = 20, $fn = fn_small);
    for (s = [-1, 1]) for (zz = [spk3_c[2] - spk_h/2 + 8, spk3_c[2] + spk_h/2 - 8])        // zip-tie slots
        translate([s * (spk_w/2 + 1.8), spk3_c[1] + spk_d + 0.5, zz]) cube([5, 3, 3.5], center = true);
}

// mic: lies flat behind its holes (above the grille, viewer's left), its USB plug pointing +x to the extension socket
mic3 = [-25, 21.5, 168];                  // closed end x, front face y, centre z
module mic3_sleeve() {
    intersection() {
        body_inner();
        difference() {
            hull() {
                translate([mic3[0] - 2, 0, mic3[2] - mic_w/2 - 2.4]) cube([mic_body + 2, mic3[1] + mic_t + 2.5, mic_w + 4.8]);
                translate([mic3[0] - 2, 0, mic3[2] - mic_w/2 - 2.4 - (mic3[1] + mic_t + 2.5)]) cube([mic_body + 2, 1, 1]);   // 45-degree underside
            }
            translate([mic3[0], 0, mic3[2] - mic_w/2 - 0.4]) cube([40, mic3[1] + mic_t + 0.4, mic_w + 0.8]);
        }
    }
}
module mic3_cuts() if (detail) for (dz = [-5, 0, 5]) for (dx = [2, 5.5, 9]) translate([mic3[0] + dx, -1, mic3[2] + dz]) rotate([-90, 0, 0]) cylinder(d = 2.2, h = mic3[1] + 1, $fn = 10);
module mic3_dummy() color("#222") translate([mic3[0], mic3[1], mic3[2] - mic_w/2]) cube([mic_l, mic_t, mic_w]);

// the Pi 5's power button: on the SD-card end of the Pi, 18.4 mm from its USB-C edge, pressed towards +x.
// A printed push rod slides in a guide through the -x wall; its outer end is a button flush with the body.
pib = [stk_x0 - 0.45, stk(0, 18.4, stk_pi_w1 + 1.5)[1], stk_ztop - 18.4];   // MEASURE (owner): about 44 above the table
rod_d = 5; rod_guide_x0 = -66;          // guide from the wall to here; the rod reaches the button
module rod_guide() {   // a tube from the wall, pointed underneath (45 degrees), on a web whose underside rises at 45 degrees from the wall
    tube = rod_d + 5; apex = pib[2] - tube/2 * 1.414;
    intersection() {
        body_inner();
        union() {
            translate([-120, pib[1], pib[2]]) rotate([0, 90, 0]) linear_extrude(120 + rod_guide_x0)
                hull() { circle(d = tube, $fn = 24); translate([tube/2 * 1.414, 0]) square(0.01, center = true); }
            translate([0, pib[1] + 3, 0]) rotate([90, 0, 0]) linear_extrude(3)
                polygon([[-120, apex - (rod_guide_x0 + 120)], [rod_guide_x0, apex], [rod_guide_x0, pib[2]], [-120, pib[2]]]);
        }
    }
}
module rod_cut() translate([-130, pib[1], pib[2]]) tear_x(rod_d + 0.6, 130 + rod_guide_x0 + 1);
// the push rod: printed lying on its flat; its collar (inside, against the guide) keeps it in, its outer end is flush
rod_out_x = -(sec_at(1)[0]/2) + 0.3;   // the side wall is vertical here (z < 42 + a little): 0.3 inside its face
rod_gap = 0.6;                         // rod end -> button at rest
module v3_pi_rod() {
    translate([rod_out_x, pib[1], pib[2]]) rotate([0, 90, 0]) {
        cylinder(d = rod_d, h = pib[0] - rod_gap - rod_out_x, $fn = 24);
        translate([0, 0, rod_guide_x0 - rod_out_x]) cylinder(d = 9, h = 3, $fn = 32);
    }
}

module v3_body() {
    difference() {
        union() {
            difference() { body_outer(); body_inner(); }
            for (k = [-1, 1]) intersection() { body_outer(); place_sh(k) sh_block(); }
            top_ledge();
            floor_stop_ribs();
            speaker_ribs();
            mic3_sleeve();
            rod_guide();
        }
        translate([0, 0, b_top - np_t]) linear_extrude(10) np_outline();          // the opening the neck plate drops into
        for (k = [-1, 1]) place_sh(k) sh_cut();
        top_cuts();
        speaker_cuts();
        mic3_cuts();
        rod_cut();
        // the floor's screws (button heads proud on the skirt) and relief around its posts
        for (p = floor_screw_pts()) translate([p[0] * (base_w/2 + 1), p[1], floor_screw_z]) tear_x(clear_m3, 10, true);
        for (p = floor_screw_pts()) translate([p[0] * well_pillar_x, p[1], -1]) cylinder(d = 8 + 2 * clr, h = base_z0 + base_floor_t + 10 + 1);
        // charging socket slot (the floor's cradle holds the socket 0.5 mm behind the wall, as in v2)
        translate([rear_x, base_d - b_wall - 1, rear_z]) rotate([-90, 0, 0]) hull() for (s = [-1, 1]) translate([s * 4, 0, 0]) cylinder(d = 8.4, h = b_wall + 2, $fn = 24);
    }
}

// -----------------------------------------------------------------------------
// NECK PLATE (robot coordinates; prints top face down)
// -----------------------------------------------------------------------------
module v3_neck_plate() {
    zt = b_top - np_t;
    difference() {
        union() {
            translate([0, 0, zt]) linear_extrude(np_t) np_outline();
            // v2's torso-top features under it: gear guard, the servo frame's pillars, ribs to the edge
            place_torso() {
                torso_gear_guard();
                for (px = frame_pillar_x) translate([px, pinion_yt, hs_tab_top - sv_tab_t]) cylinder(d = 8, h = torso_top_in - hs_tab_top + sv_tab_t + eps);
            }
            intersection() {
                translate([0, 0, zt - 8]) linear_extrude(8 + eps) np_outline(-0.6);
                union() {
                    for (a = [0, 90]) translate([0, neck_y, zt - 4]) rotate(a) cube([200, 2.4, 8], center = true);
                    translate([0, neck_y, zt - 8]) difference() { cylinder(r = ring_rp + gear_m + 3.6, h = 8); cylinder(r = ring_rp + gear_m + 2.0, h = 20, center = true); }
                }
            }
        }
        translate([0, neck_y, zt - 10]) cylinder(r = spigot_r + clr, h = np_t + 20);                // the coupler's spigot
        translate([0, neck_y, zt - 10]) linear_extrude(10 - eps) hull() { circle(r = ring_rp + gear_m + 2.0); translate([0, pinion_yt - neck_yt]) circle(r = pin_rp + gear_m + 2.0); }   // the gears turn here
        for (p = np_screw_pts) translate([p[0], p[1], zt - 1]) cylinder(d = clear_m3, h = np_t + 2, $fn = fn_small);
        // cable gaps in the ribs (the servo lead, the neck cables)
        translate([0, neck_y + 30, zt - 9]) cube([20, 8, 12], center = true);
    }
}

// -----------------------------------------------------------------------------
// ARM (local frame of the +x shoulder: x = outwards from the shoulder face, y = robot y, z = up the face)
// -----------------------------------------------------------------------------
function arm_path() = let (e = [0, 0, -ua_len], d = [0, -sin(elbow_bend), -cos(elbow_bend)])
    [[0, 0, 0], e, e + fa_len * d, e + (fa_len + 20) * d, d];       // shoulder, elbow, wrist, hand centre, forearm direction
// The arm is half-round: round on the outside, its inner face flat (it prints on it), 3 mm from the shoulder face.
module ball(c, d, sx = 1) translate(c) scale([sx, 1, 1]) sphere(d = d, $fn = 40);
module v3_arm_solid() {
    P = arm_path(); d = P[4]; side = [0, cos(elbow_bend), -sin(elbow_bend)];   // side = the thumb side (backwards)
    intersection() {
        union() {
            ball(P[0], cap_d, 0.5);                                       // shoulder cap, 24 thick
            hull() { ball(P[0], 30); ball(P[1], 27); }                    // upper arm
            hull() { ball(P[1], 27); ball(P[2], 21); }                    // forearm
            hull() { ball(P[2], 21, 0.75); ball(P[3], 30, 0.5); ball(P[3] + 10 * d, 26, 0.5); }   // wrist + mitten
            hull() { ball(P[3] - 2 * d + 4 * side, 14, 0.6); ball(P[3] - 9 * d + 15 * side, 12, 0.6); }   // thumb
        }
        translate([0, -200, -200]) cube([60, 400, 400]);
    }
}
module v3_arm() {   // the +x arm, in the shoulder frame (inner face at local x = arm_gap3)
    difference() {
        translate([arm_gap3, 0, 0]) v3_arm_solid();
        // v2's horn fit: the horn in a pocket in the inner face, its own screws into the arm; the spline screw through the middle
        translate([arm_gap3 - eps, 0, 0]) rotate([0, 90, 0]) horn_pocket(horn_plate_t + 0.6, 15);
        translate([arm_gap3 - 1, 0, 0]) rotate([0, 90, 0]) cylinder(d = 5.5, h = cap_d);
        for (a = [0, 90, 180, 270]) for (r = [7.5, 10.5]) translate([arm_gap3 - 1, r * cos(a), r * sin(a)]) rotate([0, 90, 0]) cylinder(d = 1.3, h = 7, $fn = 10);
    }
}
module place_arm3(s, a) place_sh(s) rotate([-a, 0, 0]) children();   // + = forwards (towards -y)

// -----------------------------------------------------------------------------
// HEAD (provisional): rounded box, screen window, lens hole
// -----------------------------------------------------------------------------
module rbox(sz, r) { hull() for (x = [-1,1], y = [-1,1], z = [-1,1]) translate([x*(sz[0]/2-r), y*(sz[1]/2-r), z*(sz[2]/2-r)]) sphere(r); }
module head_v3() {
    difference() {
        translate([0, hd[1]/2, hd[2]/2]) rbox(hd, hd_r);
        translate([0, hd[1]/2, hd[2]/2]) rbox(hd - [5, 5, 5], hd_r - 2.5);
        translate([0, -1, hd[2]/2 + hd_scr_z]) rotate([-90, 0, 0]) linear_extrude(6) offset(r = 6) square([scr[0] - 6 - 12, scr[2] - 10 - 12], center = true);
        translate([0, -1, hd[2]/2 + hd_cam_z]) rotate([-90, 0, 0]) cylinder(d = 17, h = 6);
    }
}
module screen_v3() {
    color([0.08, 0.08, 0.1]) translate([-scr[0]/2, 3, hd[2]/2 + hd_scr_z - scr[2]/2]) cube(scr);
    color([1.0, 0.84, 0.89]) translate([0, 2.9, hd[2]/2 + hd_scr_z]) rotate([90, 0, 0]) linear_extrude(0.2) offset(r = 4) square([146, 80], center = true);
    for (s = [-1, 1]) color([0.15, 0.15, 0.17]) translate([s * 42 - 18, 3 + scr[1], hd[2]/2 + hd_scr_z + 22]) cube([36, 9, 18]);
}
module place_head3(a = 0) translate([0, neck_y, hd_z0]) rotate(a) translate([0, -hd_neck_y, 0]) children();

// -----------------------------------------------------------------------------
// LAYOUT
// -----------------------------------------------------------------------------
module v3_electronics() {
    color("#d9d4c8") base_floor();
    color("#c8c2b4") { stack_keeper(); pca_clamp(0); pca_clamp(1); }
    ups_stack(); color("#2a2") pca_floor(); color("#333") usbc_socket();
    speaker3_dummy(); mic3_dummy();
    color("#c33") v3_pi_rod();
}
module v3_neck_parts(a = 0) place_torso() {
    translate([0, neck_yt, 0]) rotate(a) translate([0, -neck_yt, 0]) {
        color("#7fb3d5") translate([0, neck_yt, torso_h]) coupler();
        color("#e6b35a") translate([0, neck_yt, gear_z0]) ring_gear();
    }
    color("#e6b35a") translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion();
    color("#aab") translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    color("#111") translate([0, pinion_yt, hs_spline_z]) servo_dummy();
}
module arm_servo3(s) place_sh(s) translate([sv_spline_top, 0, 0]) rotate([0, 90, 0]) rotate(90) servo_dummy();
module layout() {
    v3_electronics();
    v3_neck_parts(head_angle);
    color([0.25, 0.27, 0.31]) intersection() { v3_body(); slab(-1, skirt_top); }
    color([0.96, 0.95, 0.92]) intersection() { v3_body(); slab(skirt_top, 300); }
    color([0.85, 0.85, 0.83]) v3_neck_plate();
    place_head3(head_angle) { color([0.96, 0.95, 0.92]) head_v3(); screen_v3(); }
    for (s = [-1, 1]) {
        arm_servo3(s);
        color([0.96, 0.95, 0.92]) place_arm3(s, arm_angle) if (s > 0) v3_arm(); else v3_arm();
    }
}
if (part == "layout") {
    if (cut) difference() { layout(); translate([0, -200, -10]) cube([400, 400, 500]); } else layout();
}
else if (part == "v3_body") v3_body();
else if (part == "v3_neck_plate") v3_neck_plate();
else if (part == "v3_arm") v3_arm();
else if (part == "v3_pi_rod") v3_pi_rod();

echo(str("LittleBot v3: total height ", hd_z0 + hd[2], " mm; body ", base_w, " x ", base_d, " x ", b_top,
         "; Pi button at ", pib, "; servo tab seat at local x ", sv_tab_top - sv_tab_t));
