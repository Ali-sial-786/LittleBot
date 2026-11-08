// =============================================================================
//  LittleBot v3 "Bell" - parametric OpenSCAD model (mm)
// =============================================================================
//  A smooth bell-shaped body on a dark base ring, a slim head shaped like its 7" screen turning on
//  v2's geared neck, a camera pod on the head, one-piece arms. Few, big parts:
//    v3_ring        dark base ring: v2's floor screws into it; swapped for a wheeled ring later
//    v3_body        the white bell, from the ring up to the shoulders (prints upright, no supports)
//    v3_neck_plate  closes the top; carries v2's neck (coupler, gears, servo frame), assembled on the bench
//    v3_head        slim shell behind the screen (prints standing on its foot)
//    v3_pod         camera pod on the head (holds v2's camera bracket)
//    v3_arm         x2 (the right one mirrored): upper arm + forearm + hand, inner face down
//    v3_ups_slider  pushes the UPS's power switch from outside the back
//  Reused from v2 as printed: base_floor (with the Pi + UPS, stack_keeper, pca_clamps), coupler, ring_gear,
//  pinion, servo_frame, camera_bracket.
//  Screws: M3 button heads from the kit (2 mm key) into nuts or into holes v2's screws already threaded;
//  the screen's own corner inserts; the servos' own screws. Nothing is drilled.
//  Coordinates as in v2: x across (+x = the viewer's right), y front -> back (front at y = 0), z up from the table.
//    openscad -o layout.png -D 'part="layout"' littlebot_v3.scad
// =============================================================================
include <../enclosure_v2/littlebot_v2.scad>
part = "layout";       // overrides v2's default
cut = false;           // layout: cut away x > 0 to show the inside
arm_angle = 8;         // arm angle, degrees (+ = forwards / up)
head_angle = 0;
spin = 0;              // layout: turn the robot round (for lit back views)

/* [v3 body: a bell of superellipse sections] */
b_cy = 78;                              // the sections' centre (y)
ring_ab = [93, 80]; ring_n = 4;         // base ring outline (half sizes); v2's floor fits inside (checked)
ring_h = 22; accent_h = 2;              // dark ring, then a 2 mm accent band (filament change)
sh0 = ring_h + accent_h;                // 24: the white body starts here
b_top = 197;                            // = v2's torso top: the neck, gears and coupler sit as in v2
bell = [92.7, 79.7, 60, 51];            // half width / depth at the bottom, at the top
b_wall = 2.6;
nsec = 24;
function bt(t) = 0.25 * t + 0.75 * pow(t, 5);    // gentle taper, then a rolled shoulder
function bell_sec(t) = [bell[0] - (bell[0] - bell[2]) * bt(t), bell[1] - (bell[1] - bell[3]) * bt(t), 4 - 1.2 * t, sh0 + (b_top - sh0) * t];
function bell_at(z) = bell_sec(max(0, min(1, (z - sh0) / (b_top - sh0))));

/* [v3 shoulders] (the +x side; the -x side is its mirror image) */
sh_c = [78, 82, 166];                   // centre of the flat shoulder face (the arm's axis)
sh_tilt = 12;                           // the axis points 12 degrees up: the hanging arm splays out, clear of the body
sh_face_d = 50;
arm_gap3 = 3;
sv_spline_top = arm_gap3 + horn_plate_t - 0.6;
sv_tab_top = sv_spline_top - (sv_spline_h - sv_tab_z - sv_tab_t);
sv_slot = [sv_tab_l + 0.6, sv_w + 0.6];
sv_seat_t = 5;

/* [v3 neck plate] */
np_t = torso_top_t; np_clr = 0.3; np_ledge = 5;
np_screw_pts = [[-46, 70], [46, 70], [-40, 106], [40, 106]];   // 4 x M3 x 8 from the top into dropped-in nuts; clear of the head

/* [v3 arms] */
ua_len = 50; fa_len = 44; elbow_bend = 30; cap_d = 48;

/* [AA box for the servos (front cradle)] */
bat = [65, 70, 20];                     // MEASURE yours: width, height, thickness (listings: 64-65 x 70 x 18-20)
bat_z0 = 34;                            // the box's bottom
bat_y = -1.6;                           // the box's back face (just in front of the body)

/* [7" screen: 52Pi EP-0177 (GeeekPi), from 52Pi's dimension drawing] */
// screen frame: x across its back from the end WITHOUT the ports, y down from its top edge (as in the drawing)
scr = [164.9, 100, 9];                  // outline and thickness (the standard 7" 1024x600 module)
scr_holes = [[4.7, 5.15], [160.2, 5.15], [4.7, 95.3], [160.2, 95.3]];   // threaded corner standoffs (M2.5 or M3)
scr_spk = [[20.5, 58.1], [105.3, 142.9]];   // the two speaker boxes on the back (x ranges), y 1.2 .. 28.9
spk_box_h = 9;
scr_standoff_h = 3;                     // MEASURE: how far the corner standoffs stand off the screen's board (the bosses sit on them)                          // MEASURE how far the speaker boxes stand off the board
scr_ports = [18, 38.8, 52.7, 71.4];     // on the end at x = 164.9: volume/backlight wheel, audio, USB-C (power + touch), HDMI
// plugs at the port end (all inside the head): the flat-ribbon HDMI's angled plug, the right-angle USB-C
fpc_plug = [8, 22, 20];                 // beyond the port: out, along the edge, back (its ribbon leaves backwards)
usbc_l = [15, 12, 16];                  // right-angle USB-C head beyond the port: out, along the edge, back (cable backwards)
hdmi_cpl = [38, 22, 11.5];              // HDMI female-female coupler: length, width, thickness
hdmi_plug = [36, 21, 11];               // the original cable's full-size plug (moulding + strain relief)

/* [v3 head] */
hd_end = 18;                            // frame beside the screen's ends (the plugs fit in it)
hd_tb = 4;                              // frame above and below the screen
hd = [scr[0] + 2 * hd_end, scr[1] + 2 * hd_tb];   // 200.9 x 108
hd_d = 46;                              // glass front -> back: the camera, its bracket and plug need 41 in the pod
hd_r = 12; hd_ch = 10;                  // corner radius (front view); the back's 45-degree edge
hd_wall = 2;
hd_axis = 24;                           // neck axis behind the glass: all 3 coupler screws are under the head
g3_y = neck_y - hd_axis;                // 65: the glass front
hd_z0 = torso_z0 + torso_h + bearing_h + coupler_h;   // 207.8: the head's bottom sits on v2's coupler
scr_cz = hd_z0 + hd[1] / 2;             // screen centre height
function scr_pt(p) = [scr[0] / 2 - p[0], scr_cz + scr[1] / 2 - p[1]];   // screen-frame point -> robot (x, z), seen from the front

// -----------------------------------------------------------------------------
// helpers
// -----------------------------------------------------------------------------
function se(a, b, n, cy = 0, N = 96) = [for (k = [0:N-1]) let (t = 360 * k / N, c = cos(t), s = sin(t))
    [a * sign(c) * pow(abs(c), 2/n), cy + b * sign(s) * pow(abs(s), 2/n)]];
module sq2d(a, b, n, cy = b_cy) polygon(se(a, b, n, cy));
module slab(z0, z1) translate([-300, -300, z0]) cube([600, 600, z1 - z0]);
module rr(w, h, r) offset(r = r) square([w - 2*r, h - 2*r], center = true);
// a horizontal hole along x (or y) with its top pointed (45 degrees): prints upright without sagging
module tear_x(d, len, center = false) translate([center ? -len/2 : 0, 0, 0]) rotate([0, 90, 0]) linear_extrude(len)
    hull() { circle(d = d, $fn = 24); translate([-d/2 * 1.414, 0]) square(0.01, center = true); }
module tear_y(d, len, center = false) rotate(90) tear_x(d, len, center);
module place_sh(s) { if (s > 0) translate(sh_c) rotate([0, -sh_tilt, 0]) children();
                     else mirror([1, 0, 0]) translate(sh_c) rotate([0, -sh_tilt, 0]) children(); }

// -----------------------------------------------------------------------------
// BODY (the white bell)
// -----------------------------------------------------------------------------
module bell_slice(s, inset, dz = 0) translate([0, 0, s[3] + dz]) linear_extrude(0.01) sq2d(s[0] - inset, s[1] - inset, s[2]);
module body_outer(inset = 0, ext = 0) hull() {
    for (i = [0:nsec]) bell_slice(bell_sec(i / nsec), inset, i == nsec ? -0.01 : 0);
    if (ext > 0) { bell_slice(bell_sec(0), inset, -ext); bell_slice(bell_sec(1), inset, ext); }
    for (k = [-1, 1]) place_sh(k) translate([-inset - 0.01, 0, 0]) rotate([0, 90, 0]) cylinder(d = sh_face_d - 2 * inset, h = 0.01);
}
module body_inner() body_outer(b_wall, 2);

// shoulder pocket (local frame): the servo goes in from outside, its tabs screwed from outside
module sh_block() hull() {
    translate([sv_tab_top - sv_tab_t - sv_seat_t, -15.5, -10.5]) cube([sv_seat_t + sv_tab_t - sv_tab_top - 0.5, 40, 21]);
    translate([-b_wall - 0.5, -15.5, -10.5 - 30]) cube([0.5, 40, 21 + 30]);
}
module sh_cut() {
    translate([sv_tab_top - sv_tab_t, sv_mid - sv_slot[0]/2, -sv_slot[1]/2]) cube([30, sv_slot[0], sv_slot[1]]);
    translate([-40, sv_mid - sv_l/2 - clr, -sv_w/2 - clr]) cube([40 + 1, sv_l + 2*clr, sv_w + 2*clr]);
    for (hy = [sv_hole_a, sv_hole_b]) translate([sv_tab_top - sv_tab_t + 0.01, hy, 0]) rotate([0, -90, 0]) cylinder(d = pilot_m2, h = sv_seat_t + 1, $fn = fn_small);
}

// the top: a ledge the neck plate rests on (45-degree underside), four nut bosses
module np_outline(extra = 0) sq2d(bell[2] - b_wall - np_clr + extra, bell[3] - b_wall - np_clr + extra, bell_sec(1)[2]);
function np_boss_to(p) = p[1] < neck_y ? [p[0] + sign(p[0]) * 16, p[1], b_top - np_t - 28] : [p[0] + sign(p[0]) * 14, p[1] + 34, b_top - np_t - 40];
module top_ledge() {
    zt = b_top - np_t;
    difference() {
        intersection() { body_inner(); slab(zt - 40, zt); }
        hull() { translate([0, 0, zt - 2]) linear_extrude(10) np_outline(-np_ledge); translate([0, 0, zt - 2 - 40]) linear_extrude(0.01) np_outline(-np_ledge + 40); }
    }
    intersection() {
        body_inner();
        for (p = np_screw_pts) hull() { translate([p[0], p[1], zt - 7]) cylinder(d = 11, h = 7); translate(np_boss_to(p)) cylinder(d = 11, h = 0.01); }
    }
}
module top_cuts() {
    zt = b_top - np_t;
    for (p = np_screw_pts) translate([p[0], p[1], zt - 12]) {
        cylinder(d = clear_m3, h = 13, $fn = fn_small);
        translate([0, 0, 12 - 2.6]) rotate(30) cylinder(d = 6.35 + 0.3, h = 3, $fn = 6);      // M3 nut, dropped in from above
    }
}

// speaker (Adafruit 3369) behind the chest grille, above the AA box
spk3_c = [0, 14, 120];                // the speaker's front face centre
module speaker3_dummy() color("#333") translate([-spk_w/2, spk3_c[1], spk3_c[2] - spk_h/2]) cube([spk_w, spk_d, spk_h]);
module speaker_ribs() intersection() {
    body_inner();
    for (s = [-1, 1]) let (x0 = s > 0 ? spk_w/2 + 0.3 : -spk_w/2 - 3.3) {
        hull() {
            translate([x0, 0, spk3_c[2] - spk_h/2 - 2]) cube([3, spk3_c[1] + spk_d + 3, spk_h + 4]);
            translate([x0, 0, spk3_c[2] - spk_h/2 - 2 - (spk3_c[1] + spk_d + 3)]) cube([3, 1, 1]);
        }
        hull() {
            translate([s > 0 ? spk_w/2 - 3 : -spk_w/2, 0, spk3_c[2] - spk_h/2 - 2]) cube([3, spk3_c[1] + spk_d - 4, 2]);
            translate([s > 0 ? spk_w/2 + 0.3 : -spk_w/2, 0, spk3_c[2] - spk_h/2 - 6]) cube([0.01, spk3_c[1] + spk_d - 4, 2]);
        }
    }
}
module speaker_cuts() {
    if (detail) for (row = [-3:3]) for (gx = [-35:5:35]) let (px = gx + (abs(row) % 2) * 2.5, pz = row * 4.4)
        if (in_stadium(px, pz, spk_grille_hw, spk_grille_r)) translate([px, -1, spk3_c[2] + pz]) rotate([-90, 0, 0]) cylinder(d = 3, h = 30, $fn = fn_small);
    for (s = [-1, 1]) for (zz = [spk3_c[2] - spk_h/2 + 8, spk3_c[2] + spk_h/2 - 8])
        translate([s * (spk_w/2 + 1.8), spk3_c[1] + spk_d + 0.5, zz]) cube([5, 3, 3.5], center = true);
}
// mic (Adafruit 3367): flat behind its holes above the grille, its USB plug pointing +x to the extension socket
mic3 = [-25, 25.5, 176];
module mic3_sleeve() intersection() {
    body_inner();
    difference() {
        hull() {
            translate([mic3[0] - 2, 0, mic3[2] - mic_w/2 - 2.4]) cube([mic_body + 2, mic3[1] + mic_t + 2.5, mic_w + 4.8]);
            translate([mic3[0] - 2, 0, mic3[2] - mic_w/2 - 2.4 - (mic3[1] + mic_t + 2.5)]) cube([mic_body + 2, 1, 1]);
        }
        translate([mic3[0], 0, mic3[2] - mic_w/2 - 0.4]) cube([40, mic3[1] + mic_t + 0.4, mic_w + 0.8]);
    }
}
module mic3_cuts() if (detail) for (dz = [-5, 0, 5]) for (dx = [2, 5.5, 9]) translate([mic3[0] + dx, -1, mic3[2] + dz]) rotate([-90, 0, 0]) cylinder(d = 2.2, h = mic3[1] + 1, $fn = 10);
module mic3_dummy() color("#222") translate([mic3[0], mic3[1], mic3[2] - mic_w/2]) cube([mic_l, mic_t, mic_w]);

// AA box cradle on the front: a backing plate, two rails with lips over the box's edges, two stops under its
// corners; the box slides in from above, its lid (with the switch) facing out. Its leads go in through a slot
// behind its bottom, straight down to the PCA9685's V+ terminal.
bat_lip = 3; bat_rail = 3; bat_c = 0.4;
module bat_cradle() {
    w2 = bat[0]/2 + bat_c; y1 = bat_y - bat[2] - bat_c;          // the box's front face
    difference() {
        union() {
            difference() {      // backing plate: fills the gap between the body and the box's back plane, fused to the wall
                translate([-w2 - bat_rail, bat_y, bat_z0 - 3]) cube([2 * (w2 + bat_rail), 20, bat[1] + 3]);
                body_inner();
                translate([0, 0, sh0 - 10]) linear_extrude(10) sq2d(300, 300, 4);
            }
            for (s = [-1, 1]) {
                // rail: side wall + lip; its lower end slopes back at 45 degrees (prints), the lip starts above that
                translate([s > 0 ? w2 : -w2 - bat_rail, 0, 0]) rotate([90, 0, 90]) linear_extrude(bat_rail)
                    polygon([[bat_y + 0.01, bat_z0 - 3], [y1 - 2.5, bat_z0 - 3 + (bat_y - y1 + 2.5)], [y1 - 2.5, bat_z0 + bat[1]], [bat_y + 0.01, bat_z0 + bat[1]]]);
                translate([s > 0 ? w2 - bat_lip : -w2, 0, 0]) rotate([90, 0, 90]) linear_extrude(bat_lip)
                    polygon([[y1 + 0.01, bat_z0 + (bat_y - y1)], [y1 - 2.5, bat_z0 + (bat_y - y1) + 2.5], [y1 - 2.5, bat_z0 + bat[1]], [y1 + 0.01, bat_z0 + bat[1]]]);
                // stop under the box's corner (45-degree underside)
                translate([s > 0 ? w2 - 9 : -w2, 0, 0]) rotate([90, 0, 90]) linear_extrude(9)
                    polygon([[bat_y + 0.01, bat_z0 - 6.4], [bat_y - 6.4, bat_z0], [bat_y + 0.01, bat_z0]]);
                // crush ribs on the rails' inner faces (centre the box, hold it)
                for (zz = [bat_z0 + 15, bat_z0 + 55]) translate([s * (w2 - 0.2), (bat_y + y1) / 2, zz]) cube([0.8, 3, 20], center = true);
            }
        }
        translate([-10, bat_y - 1, bat_z0 - 6]) cube([20, 30, 5]);                                 // the leads' slot (under the box, into the body)
    }
}
module bat_dummy() color("#151515") translate([-bat[0]/2, bat_y - bat_c - bat[2], bat_z0]) cube([bat[0], bat[2], bat[1]]);

// the UPS's power switch: a slider through the back wall with a fork over the switch's knob. Its fork is flared:
// pushing the floor in, the knob finds the fork whichever way the switch is set.
ups_knob_c = [stk_x0 + ups_ports[0][0], stk_y0 + stk_ups_w0 + ups_pcb_t + 2.3, stk_ztop];   // the knob: centre x, y; the board edge z
sl_z0 = ups_knob_c[2] + ups_knob[1] - 2.5;    // the bar's underside (the fork overlaps the knob by 2.5)
sl_h = 8.2; sl_w = 5; sl_head = 10; sl_travel = 2.5;
sl_tun = [sl_head + 0.4, sl_h + 0.4];         // the tunnel / wall slot (the fork passes through it)
function back_y(x, z) = let (s = bell_at(z)) b_cy + s[1] * pow(1 - pow(abs(x) / s[0], s[2]), 1 / s[2]);
sl_wall_y = back_y(ups_knob_c[0], sl_z0 + sl_h/2);
module ups_slider_tunnel() intersection() {
    body_inner();
    hull() {
        translate([ups_knob_c[0] - sl_tun[0]/2 - 2.5, sl_wall_y - 22, sl_z0 - 2.7]) cube([sl_tun[0] + 5, 30, sl_tun[1] + 5]);
        translate([ups_knob_c[0] - sl_tun[0]/2 - 2.5, sl_wall_y - 1, sl_z0 - 2.7 - 21]) cube([sl_tun[0] + 5, 6, 1]);
    }
}
module ups_slider_cut() translate([ups_knob_c[0] - sl_tun[0]/2, sl_wall_y - 30, sl_z0 - 0.2]) cube([sl_tun[0], 40, sl_tun[1]]);
module v3_ups_slider() {   // robot coordinates, at the switch's centre position
    y0 = ups_knob_c[1] - 5; y1 = sl_wall_y + 0.6;
    difference() {
        union() {
            translate([ups_knob_c[0] - sl_w/2, y0 + 8, sl_z0]) cube([sl_w, y1 - y0 - 8, sl_h]);                       // bar
            translate([ups_knob_c[0] - sl_head/2, y0, sl_z0]) cube([sl_head, 10, sl_h]);                             // fork head
            translate([ups_knob_c[0] - 8, y1, sl_z0 - 6]) cube([16, 4, sl_h + 6]);                                    // thumb tab (outside)
        }
        translate([ups_knob_c[0], y0 - 1, sl_z0 - 1]) linear_extrude(ups_knob[1] + 1.5 + 1)            // the knob's notch (open below and at the front)
            polygon([[-2.15, 0], [2.15, 0], [2.15, 13], [-2.15, 13]]);
        translate([ups_knob_c[0], y0 - 1, sl_z0 - 0.01]) rotate([-90, 0, 0]) linear_extrude(13)   // flare: 45-degree lead-in under the tines
            polygon([[-2.15 - 2.6, 0], [2.15 + 2.6, 0], [2.15, -2.6], [-2.15, -2.6]]);
        for (k = [-1, 1]) translate([ups_knob_c[0] + k * 6.5, y1 + 2, sl_z0 + sl_h/2]) rotate([90, 0, 0]) cylinder(d = 2, h = 3, center = true, $fn = 12);   // grip dimples
    }
}
module ups_knob_dummy(st = 0) color("#222") translate([ups_knob_c[0] + st * ups_knob[2] - ups_knob[0]/2, ups_knob_c[1] - 1.1, ups_knob_c[2] + 0.3]) cube([ups_knob[0], 2.2, ups_knob[1] - 0.3]);

// charging: the socket stays in v2's floor cradle; a sleeve guides the plug from the slot to it
chg_wall_y = back_y(rear_x, rear_z);
module chg_sleeve() intersection() {
    body_inner();
    difference() {
        hull() {
            translate([rear_x - 9.5, base_d - base_wall - 0.3, rear_z - 6.5]) cube([19, chg_wall_y - (base_d - base_wall - 0.3) + 1, 13]);
            translate([rear_x - 9.5, chg_wall_y - 1, rear_z - 6.5 - 8]) cube([19, 2, 1]);
        }
        translate([rear_x - 7.5, base_d - base_wall - 1, rear_z - 4.5]) cube([15, 20, 9]);
    }
}
module chg_cut() translate([rear_x, base_d - base_wall - 1, rear_z]) rotate([-90, 0, 0]) linear_extrude(30) hull() for (s = [-1, 1]) translate([s * 4, 0]) circle(d = 8.6, $fn = 24);

// body <-> ring: 4 screws through the body's wall into nuts in tabs that rise from the ring (2 at the front,
// hidden under the AA box; 2 at the back). The sides stay clear for the wheels of a later wheeled ring.
br_screws = [[-22, 0], [22, 0], [-45, 1], [45, 1]];    // [x, 0 front / 1 back]
br_z = sh0 + 6;
function br_wall_y(p, inset) = let (s = bell_at(br_z), yy = s[1] * pow(1 - pow(abs(p[0]) / s[0], s[2]), 1 / s[2]))
    p[1] == 0 ? b_cy - yy + inset : b_cy + yy - inset;
module br_holes() for (p = br_screws) let (dir = p[1] == 0 ? -1 : 1) translate([p[0], br_wall_y(p, 0), br_z]) {
    rotate([90, 0, 0]) cylinder(d = clear_m3, h = 12, center = true, $fn = fn_small);
    translate([0, dir * 0.6, 0]) rotate([dir * -90, 0, 0]) cylinder(d = 6.4, h = 5, $fn = 24);      // flat seat for the head
}

module v3_body() {
    difference() {
        union() {
            difference() { body_outer(); body_inner(); }
            for (k = [-1, 1]) intersection() { body_outer(); place_sh(k) sh_block(); }
            top_ledge();
            speaker_ribs();
            mic3_sleeve();
            ups_slider_tunnel();
            chg_sleeve();
            bat_cradle();
        }
        translate([0, 0, sh0 - 5]) linear_extrude(5) sq2d(300, 300, 4);                     // nothing below the ring's top
        for (k = [-1, 1]) place_sh(k) sh_cut();
        top_cuts();
        speaker_cuts();
        mic3_cuts();
        ups_slider_cut();
        chg_cut();
        br_holes();
    }
}

// -----------------------------------------------------------------------------
// BASE RING (dark): v2's floor screws into it from the sides; the body sits on its lip
// -----------------------------------------------------------------------------
floor_screw_z = well_low_z;
function floor_screw_pts() = [for (s = [-1, 1]) for (u = [-1, 1]) [s, wheel_y + u * well_screw_u]];
function ring_x(y, inset) = (ring_ab[0] - inset) * pow(1 - pow(abs(y - b_cy) / (ring_ab[1] - inset), ring_n), 1 / ring_n);
lip_h = 5;
module ring2d(inset) sq2d(ring_ab[0] - inset, ring_ab[1] - inset, ring_n);
module v3_ring() {
    s0 = bell_sec(0);
    difference() {
        union() {
            difference() { linear_extrude(sh0) ring2d(0); translate([0, 0, -1]) linear_extrude(sh0 + 2) ring2d(b_wall); }
            // lip inside the body's bottom (locates it)
            let (sl = bell_at(sh0 + lip_h + 1)) difference() {
                translate([0, 0, sh0 - 0.01]) linear_extrude(lip_h) difference() { sq2d(sl[0] - b_wall - 0.6, sl[1] - b_wall - 0.6, sl[2]); sq2d(sl[0] - b_wall - 3.6, sl[1] - b_wall - 3.6, sl[2]); }
                translate([rear_x - 11, b_cy, sh0 - 1]) cube([22, 100, lip_h + 2]);
            }
            // a ledge from the wall in to the lip (45-degree underside), the lip and the tabs stand on it
            difference() {
                translate([0, 0, sh0 - 11]) linear_extrude(11) difference() { ring2d(b_wall - 0.01); ring2d(b_wall + 6); }
                hull() { translate([0, 0, sh0 - 11 - 0.01]) linear_extrude(0.01) ring2d(b_wall); translate([0, 0, sh0 - 0.5]) linear_extrude(0.01) ring2d(b_wall + 6.5); }
            }
            // pads between the wall and v2's floor posts, under 45 degrees
            for (p = floor_screw_pts()) intersection() {
                translate([0, 0, 0]) linear_extrude(sh0) ring2d(1);
                hull() {
                    translate([p[0] * (well_pillar_x + 4.3), p[1] - 6, floor_screw_z - 6]) cube([p[0] * 10, 12, sh0 - floor_screw_z + 6]);
                    translate([p[0] * (well_pillar_x + 12), p[1] - 6, floor_screw_z - 14]) cube([p[0] * 1, 12, 1]);
                }
            }
            // tabs up into the body for its 4 screws, each with a nut slot open at the top
            for (p = br_screws) let (dir = p[1] == 0 ? 1 : -1, y = br_wall_y(p, b_wall + 0.3), st = bell_at(sh0 + 14)) intersection() {
                translate([0, 0, sh0]) linear_extrude(16) sq2d(st[0] - b_wall - 0.6, st[1] - b_wall - 0.6, st[2]);
                translate([p[0] - 6, dir > 0 ? y : y - 5, sh0 - 0.01]) cube([12, 5, 13]);
            }
            // stops: short ribs over the floor's top edge (it rests on them while the robot is upside down)
            intersection() {
                translate([0, 0, floor_low + floor_low_t + 0.2]) linear_extrude(4) difference() { ring2d(b_wall - 0.01); ring2d(b_wall + 2.2); }
                union() { for (x = [-20, 20]) translate([x - 5, -1, 0]) cube([10, 30, 40]);
                          for (x = [-50, 50]) translate([x - 5, base_d - 30, 0]) cube([10, 40, 40]); }
            }
        }
        // v2's floor: relief round its posts, the screws through the wall and pad (button heads on the ring)
        for (p = floor_screw_pts()) translate([p[0] * well_pillar_x, p[1], -1]) cylinder(d = 8 + 2 * clr, h = base_z0 + base_floor_t + 10 + 1);
        for (p = floor_screw_pts()) translate([p[0] * (ring_x(p[1], 0) + 1), p[1], floor_screw_z]) tear_x(clear_m3, 20, true);
        // the body's screws: through the tabs, nut slots from the top
        for (p = br_screws) let (dir = p[1] == 0 ? 1 : -1, y = br_wall_y(p, b_wall + 0.3)) {
            translate([p[0], y, br_z]) rotate([90, 0, 0]) tear_y(clear_m3, 14, true);
            translate([p[0] - 3.05, dir > 0 ? y + 1.6 : y - 1.6 - 2.7, br_z - 3.5]) cube([6.1, 2.7, 20]);
        }
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
        translate([0, neck_y, zt - 10]) cylinder(r = spigot_r + clr, h = np_t + 20);
        translate([0, neck_y, zt - 10]) linear_extrude(10 - eps) hull() { circle(r = ring_rp + gear_m + 2.0); translate([0, pinion_yt - neck_yt]) circle(r = pin_rp + gear_m + 2.0); }
        for (p = np_screw_pts) translate([p[0], p[1], zt - 1]) cylinder(d = clear_m3, h = np_t + 2, $fn = fn_small);
        translate([0, neck_y + 30, zt - 9]) cube([20, 8, 12], center = true);
    }
}

// -----------------------------------------------------------------------------
// ARM (local frame of the +x shoulder: x = outwards from the shoulder face, y = robot y, z = up the face)
// -----------------------------------------------------------------------------
function arm_path() = let (e = [0, 0, -ua_len], d = [0, -sin(elbow_bend), -cos(elbow_bend)])
    [[0, 0, 0], e, e + fa_len * d, e + (fa_len + 20) * d, d];
module ball(c, d, sx = 1) translate(c) scale([sx, 1, 1]) sphere(d = d, $fn = 40);
// The hand: palm, four fingers and a thumb in a relaxed open pose, in the arm's plane (the whole arm still prints
// flat on its inner face). Half-round like the arm; fingers 5.6 mm thick.
module v3_arm_solid() {
    P = arm_path(); d = P[4]; side = [0, cos(elbow_bend), -sin(elbow_bend)];   // side: across the hand (towards the back)
    wr = P[2] + 6 * d;                                                           // wrist
    intersection() {
        union() {
            ball(P[0], cap_d, 0.5);                                              // shoulder cap
            hull() { ball(P[0], 30); ball(P[1], 27); }                           // upper arm
            hull() { ball(P[1], 27); ball(P[2], 21); }                           // forearm
            hull() { ball(P[2], 21, 0.75); ball(wr + 4 * d, 26, 0.55); ball(wr + 18 * d + 1 * side, 28, 0.5); }   // wrist + palm
            for (i = [0:3]) let (o = (-1.5 + i) * 6.6, L = [17, 23, 22, 17][i], fan = (-1.5 + i) * 3, f0 = wr + 26 * d + o * side,
                                 dir = cos(fan) * d + sin(fan) * side, bend = [0, cos(elbow_bend + 14), -sin(elbow_bend + 14)])
                hull() { ball(f0, 8.4, 1.3); ball(f0 + L * dir, 7, 1.3); }                                    // fingers
            let (t0 = wr + 12 * d - 12 * side, tdir = (d - side) / norm(d - side))
                hull() { ball(t0, 10, 1.1); ball(t0 + 16 * tdir, 7.6, 1.2); }                                // thumb (front edge)
        }
        translate([0, -200, -200]) cube([60, 400, 400]);
    }
}
module v3_arm() difference() {
    translate([arm_gap3, 0, 0]) v3_arm_solid();
    translate([arm_gap3 - eps, 0, 0]) rotate([0, 90, 0]) horn_pocket(horn_plate_t + 0.6, 15);
    translate([arm_gap3 - 1, 0, 0]) rotate([0, 90, 0]) cylinder(d = 5.5, h = cap_d);
    for (a = [0, 90, 180, 270]) for (r = [7.5, 10.5]) translate([arm_gap3 - 1, r * cos(a), r * sin(a)]) rotate([0, 90, 0]) cylinder(d = 1.3, h = 7, $fn = 10);
}
module place_arm3(s, a) place_sh(s) rotate([-a, 0, 0]) children();

// -----------------------------------------------------------------------------
// HEAD: a shell the shape of the screen. The screen is held only by its own 4 corner standoffs, screwed from the
// back; nothing touches the glass. Every plug hides inside. Prints standing on its bottom.
// -----------------------------------------------------------------------------
module head_shape(i) translate([0, g3_y, scr_cz]) rotate([-90, 0, 0]) hull() {
    translate([0, 0, -1]) linear_extrude(hd_d - hd_ch + 1) rr(hd[0] - 2 * i, hd[1] - 2 * i, hd_r - i);
    linear_extrude(hd_d - i) rr(hd[0] - 2 * hd_ch - 2 * i * 0.41, hd[1] - 2 * hd_ch - 2 * i * 0.41, max(1, hd_r - hd_ch));
}
module head_outer() intersection() { head_shape(0); translate([0, g3_y, 0]) cube([400, 2 * hd_d, 800], center = true); }
module head_inner() head_shape(hd_wall);
scr_back_y = g3_y + scr[2];
hd_split = g3_y + hd_d - hd_ch - 6;      // the back cover starts here (16 mm deep incl. its 45-degree edge)
pod_scr = [[-16, g3_y + 18.5], [16, g3_y + 18.5]];   // the camera pod's two screws (robot x, y)
cov_scr = [for (x = [-1, 1], z = [-1, 1]) [x * (hd[0]/2 - 9), scr_cz + z * (hd[1]/2 - 9)]];   // the cover's 4 screws (x, z)
function hole_xz(h) = scr_pt(h);
cpl_c = [-45, scr_back_y + 3 + hdmi_cpl[2] / 2, hd_z0 + hd_wall + hdmi_cpl[1] / 2 + 0.3];   // the coupler, on edge on the head's floor
module y_slab(y0, y1) translate([-300, y0, -10]) cube([600, y1 - y0, 800]);
// FRAME: holds the screen by its 4 standoffs, sits on v2's coupler, carries the camera pod and the coupler; prints face down
module v3_head_frame() {
    zt = hd_z0 + hd[1];
    difference() {
        union() {
            intersection() { difference() { head_outer(); head_inner(); } y_slab(g3_y - 1, hd_split); }
            // 4 bosses behind the screen's corner standoffs, webbed to the roof / floor (tree supports under them)
            for (h = scr_holes) let (q = hole_xz(h), up = q[1] > scr_cz ? 1 : -1) {
                translate([q[0], scr_back_y, q[1]]) rotate([-90, 0, 0]) cylinder(d = 9.5, h = hd_split - scr_back_y);
                translate([q[0] - 3, scr_back_y, up > 0 ? q[1] : q[1] - (q[1] - (hd_z0 + 1))]) cube([6, hd_split - scr_back_y, up > 0 ? zt - 1 - q[1] : q[1] - hd_z0 - 1]);
            }
            // the camera pod's nut bosses under the roof (front face at 45 degrees: prints face down)
            for (p = pod_scr) hull() {
                translate([p[0] - 5.5, scr_back_y + 7.8, zt - hd_wall - 7]) cube([11, hd_split - scr_back_y - 7.8, 7.01]);
                translate([p[0] - 5.5, scr_back_y + 0.8, zt - hd_wall - 0.5]) cube([11, 1, 0.51]);
            }
            // the cover's 4 nut bosses in the frame's corners, up to the split
            for (c = cov_scr) translate([c[0], hd_split - 12, c[1]]) rotate([-90, 0, 0]) cylinder(d = 9, h = 12);
            // cradle: two clips on the floor hold the HDMI coupler (the original cable's plug goes into its end)
            for (dx = [-11, 11]) translate([cpl_c[0] + dx - 3, cpl_c[1] - hdmi_cpl[2]/2 - 1.8, hd_z0 + 1]) difference() {
                cube([6, hdmi_cpl[2] + 3.6, hdmi_cpl[1] * 0.6 + 1.3]);
                translate([-1, 1.5, 1.3]) cube([8, hdmi_cpl[2] + 0.6, 40]);
            }
        }
        translate([0, g3_y - 1, scr_cz]) rotate([-90, 0, 0]) linear_extrude(scr[2] + 1 + 0.2) rr(scr[0] + 0.6, scr[1] + 0.6, 2);   // the screen
        for (h = scr_holes) let (q = hole_xz(h)) translate([q[0], 0, q[1]]) {
            translate([0, scr_back_y - 1, 0]) rotate([-90, 0, 0]) cylinder(d = 6.2, h = 1 + scr_standoff_h);   // the standoff; the boss sits on its top
            translate([0, scr_back_y, 0]) rotate([-90, 0, 0]) cylinder(d = 3.4, h = 60, $fn = 16);        // M2.5 x 6 (UPS kit) or M3 x 6 through a 2 mm floor
            translate([0, scr_back_y + scr_standoff_h + 2, 0]) rotate([-90, 0, 0]) cylinder(d = 6.4, h = 60, $fn = 24);   // the head's well, from the back
        }
        // the cover's screws: nuts slide in from the inside
        for (c = cov_scr) translate([c[0], hd_split - 13, c[1]]) {
            rotate([-90, 0, 0]) cylinder(d = clear_m3, h = 14, $fn = fn_small);
            translate([c[0] > 0 ? -12 : 0, 4.5 - 0.15, -3.05]) cube([12, 2.7, 6.1]);
        }
        // the cables' way down into the neck and the head's 3 screws into v2's coupler (M3 x 12, as v2)
        translate([0, neck_y, hd_z0 - 1]) cylinder(r = neck_bore_r, h = hd_wall + 2);
        for (a = head_screw_angles) translate([0, neck_y, hd_z0 - 1]) rotate(a) translate([head_screw_r, 0, 0]) cylinder(d = clear_m3, h = hd_wall + 2, $fn = fn_small);
        // the camera pod's screws (nuts slide in from the back with the cover off) and the camera cable's hole
        for (p = pod_scr) {
            translate([p[0], p[1], zt - 14]) cylinder(d = clear_m3, h = 20, $fn = fn_small);
            translate([p[0] - 3.05, p[1] - 3.2, zt - hd_wall - 4.4]) cube([6.1, hd_split - p[1] + 4, 2.7]);
        }
        translate([0, g3_y + 33, zt - 5]) linear_extrude(10) rr(18, 10, 2);
    }
}
// BACK COVER: grilles over the screen's speakers, vents, a finger slot at the volume wheel; 4 x M3 x 10 into the frame.
// Prints back face down.
module v3_head_back() {
    zt = hd_z0 + hd[1];
    difference() {
        union() {
            intersection() { difference() { head_outer(); head_inner(); } y_slab(hd_split, g3_y + hd_d + 1); }
            // a lip inside the frame's walls (locates the cover)
            intersection() {     // only on clear stretches of the walls (the frame's bosses, clips and the coupler are elsewhere)
                y_slab(hd_split - 3, hd_split + 0.01);
                difference() { head_shape(hd_wall + 0.3); head_shape(hd_wall + 1.6); }
                union() {
                    for (x = [-1, 1]) translate([x * hd[0]/2, 0, scr_cz]) cube([30, 400, 50], center = true);          // ends, mid height
                    translate([0, 0, hd_z0 + hd[1]]) cube([12, 400, 30], center = true);                               // top, centre
                    for (x = [-1, 1]) translate([x * 48, 0, hd_z0 + hd[1]]) cube([36, 400, 30], center = true);        // top, either side
                    translate([48, 0, hd_z0]) cube([36, 400, 30], center = true);                                     // bottom, right
                }
            }
            for (c = cov_scr) translate([c[0], hd_split, c[1]]) rotate([-90, 0, 0]) cylinder(d = 9, h = hd_d - hd_ch);
        }
        if (detail) {
            for (r = scr_spk) let (xa = scr_pt([r[0], 0])[0], xb = scr_pt([r[1], 0])[0], zc = scr_pt([0, 15])[1])
                for (gx = [min(xa, xb) + 4 : 4.5 : max(xa, xb) - 4]) for (k = [-2:2])
                    translate([gx + (k % 2 == 0 ? 0 : 2.25), g3_y + hd_d - 8, zc + k * 4.5]) rotate([-90, 0, 0]) cylinder(d = 2.6, h = 12, $fn = 12);
            for (x = [-56:8:56]) for (zz = [scr_cz - 22, scr_cz + 4]) translate([x - 1.6, g3_y + hd_d - 8, zz - 7]) cube([3.2, 12, 14]);
        }
        translate([-scr[0]/2 - 14, g3_y + hd_d - 14, scr_pt([0, scr_ports[0]])[1] - 8]) cube([12, 20, 16]);          // volume wheel
        for (c = cov_scr) translate([c[0], 0, c[1]]) {
            translate([0, hd_split - 1, 0]) rotate([-90, 0, 0]) cylinder(d = clear_m3, h = 40, $fn = fn_small);
            translate([0, hd_split + 2, 0]) rotate([-90, 0, 0]) cylinder(d = 6.4, h = 40, $fn = 24);              // M3 x 10: head on a 2 mm floor
        }
        for (h = scr_holes) let (q = hole_xz(h)) translate([q[0], hd_split - 1, q[1]]) rotate([-90, 0, 0]) cylinder(d = 7, h = 40);   // over the screen's screws
    }
}
module v3_head() { v3_head_frame(); v3_head_back(); }
module screen_dummy3(s = 0) {
    color([0.06, 0.06, 0.08]) translate([0, g3_y + 0.15 + s, scr_cz]) rotate([-90, 0, 0]) linear_extrude(scr[2] - 2*s) rr(scr[0] - 2*s, scr[1] - 2*s, 2);
    for (r = scr_spk) let (xa = scr_pt([r[0], 0])[0], xb = scr_pt([r[1], 0])[0])
        color([0.15, 0.15, 0.17]) translate([min(xa, xb) + s, scr_back_y, scr_pt([0, 28.9])[1] + s]) cube([abs(xb - xa) - 2*s, spk_box_h - s, 27.7 - 2*s]);
    for (h = scr_holes) let (q = hole_xz(h)) color("#c9a74a") translate([q[0], scr_back_y, q[1]]) rotate([-90, 0, 0]) cylinder(d = 5 - 2*s, h = scr_standoff_h - s);
}
// what plugs into the screen, all inside the head: the ribbon HDMI's angled plug, the right-angle USB-C (cable
// backwards), the coupler with the original cable's plug in its cradle
module screen_plugs3(s = 0) {
    xe = -scr[0] / 2;
    zh = scr_pt([0, scr_ports[3]])[1]; zu = scr_pt([0, scr_ports[2]])[1];
    color("#c9a74a") translate([xe - fpc_plug[0] + s, g3_y + 1 + s, zh - fpc_plug[1]/2 + s]) cube(fpc_plug - [2*s, 0, 2*s]);
    color("#333") translate([xe - usbc_l[0] + s, g3_y + 1 + s, zu - usbc_l[1]/2 + s]) cube([usbc_l[0] - 2*s, usbc_l[2] - 2*s, usbc_l[1] - 2*s]);
    color("#111") translate([cpl_c[0] - hdmi_cpl[0]/2 + s, cpl_c[1] - hdmi_cpl[2]/2 + s, cpl_c[2] - hdmi_cpl[1]/2 + s]) cube([hdmi_cpl[0], hdmi_cpl[2], hdmi_cpl[1]] - [2*s, 2*s, 2*s]);
    color("#222") translate([cpl_c[0] + hdmi_cpl[0]/2 + s, cpl_c[1] - hdmi_plug[2]/2 + s, cpl_c[2] - hdmi_plug[1]/2 + s]) cube([hdmi_plug[0], hdmi_plug[2], hdmi_plug[1]] - [2*s, 2*s, 2*s]);
}

// -----------------------------------------------------------------------------
// CAMERA POD: on the head's roof, 4 mm behind the face (nothing of it in front), flush with the head's back;
// v2's bracket inside (tilt on 2 side screws). Prints standing on its bottom.
// -----------------------------------------------------------------------------
pod_set = 4;                            // behind the glass
pod3 = [58, hd_d - pod_set, 46];        // width, depth, height
pod3_r = 9; pod3_w = 2.2;
pod3_y0 = g3_y + pod_set;
cam3_z = hd_z0 + hd[1] + 22;            // lens centre
module place_cam3() translate([0, pod3_y0 + cam_board_y, cam3_z]) children();
module pod3_shape(i) translate([0, pod3_y0, hd_z0 + hd[1]]) intersection() {
    hull() {
        for (x = [-1, 1]) translate([x * (pod3[0]/2 - pod3_r), i, pod3[2] - pod3_r]) rotate([-90, 0, 0]) cylinder(r = pod3_r - i, h = pod3[1] - 5 - 2 * i);
        translate([-pod3[0]/2 + i, i, 0]) cube([pod3[0] - 2 * i, pod3[1] - 5 - 2 * i, 1]);
        translate([-pod3[0]/2 + 5 + i, i, 0]) cube([pod3[0] - 2 * (5 + i), pod3[1] - 2 * i, pod3[2] - 5 - i]);
    }
    translate([-200, -1, i > 0 ? -1 : 0]) cube([400, pod3[1] - i + 1, 200]);
}
module v3_pod() {
    zt = hd_z0 + hd[1];
    difference() {
        union() {
            difference() { pod3_shape(0); pod3_shape(pod3_w); }
            for (s = [-1, 1]) intersection() { pod3_shape(0); translate([s * (cam_brk_wo + 0.2 + 3.3), pod3_y0 + cam_board_y + cam_pivot_dy, cam3_z - 6]) cylinder(d = 6.6, h = 40); }
            for (p = pod_scr) intersection() { pod3_shape(0); translate([p[0], p[1], zt]) cylinder(d = 10.4, h = pod3[2]); }
        }
        translate([0, pod3_y0 - 1, cam3_z]) rotate([-90, 0, 0]) cylinder(d = cam_aperture, h = pod3_w + 2);
        translate([0, pod3_y0 - 0.01, cam3_z]) rotate([-90, 0, 0]) cylinder(d1 = cam_aperture + 6, d2 = cam_aperture, h = pod3_w * 0.8);
        for (s = [-1, 1]) {
            translate([s * 24, pod3_y0 + cam_board_y + cam_pivot_dy, cam3_z]) rotate([0, 90, 0]) cylinder(d = clear_m3, h = 14, center = true, $fn = fn_small);
            translate([s * (pod3[0]/2 - 1.8), pod3_y0 + cam_board_y + cam_pivot_dy, cam3_z]) rotate([0, s * 90, 0]) cylinder(d = 6.4, h = 6, $fn = 24);
        }
        for (p = pod_scr) translate([p[0], p[1], zt - 1]) { cylinder(d = clear_m3, h = pod3[2] + 2, $fn = fn_small); translate([0, 0, 1 + 3]) cylinder(d = 6.4, h = pod3[2], $fn = 24); }
        translate([0, g3_y + 33, zt - 1]) linear_extrude(5) rr(18, 10, 2);
    }
}

// -----------------------------------------------------------------------------
// LAYOUT
// -----------------------------------------------------------------------------
module v3_electronics() {
    color("#d9d4c8") base_floor();
    color("#c8c2b4") { stack_keeper(); pca_clamp(0); pca_clamp(1); }
    ups_stack(); color("#2a2") pca_floor(); color("#333") usbc_socket();
    speaker3_dummy(); mic3_dummy(); bat_dummy(); ups_knob_dummy();
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
module place_head3(a = 0) translate([0, neck_y, 0]) rotate(a) translate([0, -neck_y, 0]) children();
WHITE3 = [0.96, 0.95, 0.92]; DARK3 = [0.13, 0.14, 0.16];
module layout() {
    v3_electronics();
    v3_neck_parts(head_angle);
    color(DARK3) v3_ring();
    color(WHITE3) v3_body();
    color([0.85, 0.85, 0.83]) v3_neck_plate();
    color(DARK3) v3_ups_slider();
    place_head3(head_angle) {
        color(WHITE3) v3_head_frame(); color([0.92, 0.91, 0.88]) v3_head_back(); screen_dummy3(); screen_plugs3();
        color(WHITE3) v3_pod(); color("#aab") place_cam3() camera_bracket();
    }
    for (s = [-1, 1]) { arm_servo3(s); color(WHITE3) place_arm3(s, arm_angle) v3_arm(); }
}
if (part == "layout") translate([0, b_cy, 0]) rotate(spin) translate([0, -b_cy, 0]) {
    if (cut) difference() { layout(); translate([0, -200, -10]) cube([400, 400, 500]); } else layout();
}
else if (part == "v3_body") v3_body();
else if (part == "v3_ring") v3_ring();
else if (part == "v3_neck_plate") v3_neck_plate();
else if (part == "v3_arm") v3_arm();
else if (part == "v3_head") v3_head();
else if (part == "v3_head_frame") v3_head_frame();
else if (part == "v3_head_back") v3_head_back();
else if (part == "v3_pod") v3_pod();
else if (part == "v3_ups_slider") v3_ups_slider();
else if (part == "print") print_v3(which);

// -----------------------------------------------------------------------------
// PRINT ORIENTATIONS (part = "print", which = name): each part on the bed (z = 0) as it should print
// -----------------------------------------------------------------------------
which = "";
module print_v3(w) {
    if (w == "body")        translate([0, 0, -sh0]) v3_body();                                           // upright
    if (w == "ring")        v3_ring();                                                                  // upright
    if (w == "neck_plate")  translate([0, 0, b_top]) mirror([0, 0, 1]) v3_neck_plate();                // top face down
    if (w == "head_frame")  translate([0, 0, -g3_y]) rotate([90, 0, 0]) v3_head_frame();              // face down (tree supports under 4 bosses)
    if (w == "head_back")   translate([0, 0, g3_y + hd_d]) rotate([-90, 0, 0]) v3_head_back();       // back face down
    if (w == "pod")         translate([0, 0, hd_z0 + hd[1] + pod3[2]]) mirror([0, 0, 1]) v3_pod();    // top down (open bottom up)
    if (w == "arm_left")    rotate([0, -90, 0]) translate([-arm_gap3, 0, 0]) v3_arm();                // inner face down
    if (w == "arm_right")   mirror([0, 1, 0]) print_v3("arm_left");
    if (w == "ups_slider")  translate([0, 0, sl_z0 + sl_h]) mirror([0, 0, 1]) v3_ups_slider();       // top down
}

echo(str("LittleBot v3: total height ", hd_z0 + hd[1] + pod3[2], " mm; head ", hd[0], " x ", hd[1], " x ", hd_d,
         "; slider wall y ", sl_wall_y, "; charging wall y ", chg_wall_y));
