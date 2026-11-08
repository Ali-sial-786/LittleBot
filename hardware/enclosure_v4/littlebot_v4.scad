// =============================================================================
//  LittleBot v4 "Soft Arc" - parametric OpenSCAD model (mm)
// =============================================================================
//  The Soft Arc concept with the parts already bought: a soft egg-shaped body on a dark base, a black neck, a
//  rounded head around the 7" touch screen with a camera hood on top (black arch), paddle arms.
//    sa_body       the white body (prints upright, no supports)
//    v3_ring       dark base (v3's, unchanged in shape: its notches are the concept's base vents)
//    v3_neck_plate the body's top: carries v2's neck (v3's, unchanged)
//    sa_puck       the black neck: on v2's coupler, under the head (turns with it)
//    sa_head_frame the head's front: face, screen, camera hood (dark arch printed in, same print)
//    sa_head_back  the rounded back of the head (slits for the screen's own speakers); 2 screws from below
//    sa_door       dark door over the AA box on the back
//    sa_arm        x2, paddles on the shoulder servos
//    v3_ups_slider pushes the UPS's power switch (v3's)
//  What goes where (and why):
//    - sound: the screen's own speakers (HDMI audio), behind slits in the head's back: no USB speaker, no grille
//    - mic: the USB mic behind the chest's left dot
//    - Pi 5 + UPS: on v2's floor in the base, its fan blowing down through the floor vents and out under the base;
//      fresh air in through slits high on the back
//    - 4xAA servo box: behind the dark door on the back (1 screw)
//    - charging + UPS switch: below the door, as in v3
//    - camera: v2's bracket in the hood, 2 pivot screws through the hood's sides (tilt adjustable)
//  Reused from v2 as printed: floor (Pi + UPS), stack keeper, PCA clamps, coupler, ring gear, pinion, servo frame,
//  camera bracket. Reused from v3: base ring, neck plate, UPS slider.
//  Coordinates as in v2/v3: x across (+x = the viewer's right), y front -> back, z up from the table.
//    openscad -o layout.png -D 'part="layout4"' littlebot_v4.scad
// =============================================================================
// overrides of v3's variables are evaluated where v3 defines them: what they use must be set before the include
puck_t = 8;                              // the black neck raises the head this much
mic_dot = [20, 150];                     // the chest's two dots: x, z (the mic behind the left one)
H4_wall = 2.5;                           // the head's walls
H4 = [246, 146];                         // width, height (front view): the A1's bed takes 256
// v4's own constants
egg = [4, 3.5]; egg_t = 0.6;             // the bulge (half width, half depth) and how far up it reaches
puck_r = 31; puck_skirt = [26, 198];     // skirt inner radius (v2's coupler is r 25), its bottom (the plate's top is 197)
puck_scr = [30, 140, 290];               // into the coupler's own holes (where v3's head screwed)
head4_scr = [85, 215, 340];              // the head into the puck
H4_R = 44;                               // corner radius (front view)
H4_d = 104;                              // the face -> the back
H4_fr = 16;                              // the face's edge radius (starts at 45 degrees: prints face down)
H4_y1 = 40;                              // the back starts rounding here
H4_smin = 0.5;                           // the flat of the back (fraction of the face)
H4_face_t = 1.8;                         // the face (the plugs start 1 mm behind the glass)
H4_split = 72;                           // frame | back cover (from the face): the neck's pad is all in the frame
hood_ab = [32, 31]; hood_n = 2.5;        // the camera hood's arch (front view): half sizes, roundness
hood_d = 58;                             // depth: full height to 44 (the camera bracket), then it slopes into the head
inlay_t = 1.6;                           // the dark arch: the face's first 8 layers
bat4_clr = 0.5;
bo4_z0 = 5.5;                            // the opening's bottom edge, above the door's (the door's frame: the panel's local z)
door4_t = 2.5;
WHITE4 = [0.95, 0.94, 0.92]; DARK4 = [0.16, 0.17, 0.19];
cov4_scr = [-55, 55];                    // the back cover's 2 screws, from below, 5 mm behind the split
cov4_tabs = [-40, 40];                   // the frame's 2 tabs into slots under the cover's roof
pad4 = [38, 105, 0.85];                  // width, length (axis -> tip), thickness / half width
include <../enclosure_v3/littlebot_v3.scad>
part = "layout4";        // overrides v3's (v4's parts: layout4, print4 + which, assy4 + which)
which = "";

/* [body: a soft egg of superellipse sections] (overrides v3's: same bottom and top sections, so v3's ring and
   neck plate fit unchanged) */
nsec = 32;
// v3's bell (its upper part leaves the hanging arms room) plus a soft egg bulge low down, under the arms' tips
function egg_k(t) = t >= egg_t ? 0 : pow(sin(180 * t / egg_t), 2);
function bell_sec(t) = [bell[0] - (bell[0] - bell[2]) * bt(t) + egg[0] * egg_k(t), bell[1] - (bell[1] - bell[3]) * bt(t) + egg[1] * egg_k(t),
                        4 - 1.2 * t, sh0 + (b_top - sh0) * t];
function front_y(x, z) = let (s = bell_at(z)) b_cy - s[1] * pow(1 - pow(abs(x) / s[0], s[2]), 1 / s[2]);
br_screws = [[-60, 1], [-33, 1], [33, 1], [60, 1]];   // body -> ring: all 4 at the back (the front stays clean)

/* [the black neck] */

/* [head] */
g3_y = neck_y - 40;                      // 49: the glass front (the neck 40 mm behind it: under the head's weight)
y4f = g3_y - 1;                          // the face (1 mm proud of the glass, as v3's bezel)
hd_z0 = torso_z0 + torso_h + bearing_h + coupler_h + puck_t;   // 215.8: the head's bottom
z4c = hd_z0 + H4[1] / 2;                 // the head's centre
scr_cz = hd_z0 + H4[1] / 2 - 6;          // the screen: 6 mm below the head's middle (the hood above)
scr_top = scr_cz + scr[1] / 2;
cpl_c = [-45, scr_back_y + 3 + hdmi_cpl[2] / 2, hd_z0 + 4 + hdmi_cpl[1] / 2 + 0.3];   // on the 4 mm floor
/* [camera hood] */
cam4_z = hd_z0 + H4[1] + 4;              // lens centre: 4 mm above the head's top
hood_c = cam4_z - 6;                     // the arch's centre height
module place_cam4() translate([0, y4f + cam_board_y, cam4_z]) children();

/* [AA box door, on the back] */
bat_z0 = 86;                             // the box's bottom (the door clears the UPS slider's tunnel)
bo4 = [bat[0] + 2 * bat4_clr, bat[1] + 5];   // the opening: the box + 4 mm under it for the door's hooks
bat4_lz = bo4_z0 + 4;                    // the box's bottom (on its shelf)
door4 = [96, bo4_z0 + bo4[1] + 10.5, 12];    // width, height, corner radius
door4_z0 = bat_z0 - bat4_lz;


// -----------------------------------------------------------------------------
// helpers
// -----------------------------------------------------------------------------
module se2d(a, b, n) polygon(se(a, b, n, 0, 120));
function y_ref(y) = y - y4f;
module xz_slice(y, a, b, n, zc) translate([0, y, zc]) rotate([-90, 0, 0]) mirror([0, 1, 0]) linear_extrude(0.01) se2d(a, b, n);
module xz_rr(y, w, h, r, zc) translate([0, y, zc]) rotate([-90, 0, 0]) linear_extrude(0.01) rr(w, h, max(0.5, r), $fn = 64);

// -----------------------------------------------------------------------------
// BODY
// -----------------------------------------------------------------------------
// the AA box door: a flat panel set into the back, flush at its sides, a little recessed at its middle
dp_y0 = back_y(door4[0] / 2, door4_z0); dp_y1 = back_y(door4[0] / 2, door4_z0 + door4[1]);
dp_tilt = atan((dp_y0 - dp_y1) / door4[1]);          // the back leans in going up
// the door's frame: x across, y out of the body (the panel's outer face is y = 0), z up the panel
module place_door() translate([0, dp_y0, door4_z0]) rotate([dp_tilt, 0, 0]) children();
module door_prism(grow, y0, y1) translate([0, y0, door4[1] / 2]) rotate([-90, 0, 0]) linear_extrude(y1 - y0) rr(door4[0] + 2 * grow, door4[1] + 2 * grow, door4[2] + grow);
module box_prism(y0, y1, grow = 0) translate([-bo4[0] / 2 - grow, y0, bo4_z0 - grow]) cube([bo4[0] + 2 * grow, y1 - y0, bo4[1] + 2 * grow]);
dr_fr = 4;                               // the frame behind the door (it rests on it), mm behind the door
module aa_bay() place_door() {      // frame round the opening, rails, lips (stop the box), shelf
    by = -door4_t - bat[2] - 2 * bat4_clr;   // the box's inner face
    hy = -door4_t - dr_fr - 3;               // the shelf starts behind the door's hooks
    difference() {
        union() {
            intersection() { door_prism(3, -door4_t - dr_fr, 40); place_door_inv() body_outer(); }    // solid from the frame out to the skin
            for (s = [-1, 1]) translate([s > 0 ? bo4[0] / 2 : -bo4[0] / 2 - 3, by - 3, bat4_lz - 3]) cube([3, -by + 3 - door4_t - dr_fr, bat[1] + 3]);   // rails
            for (s = [-1, 1]) translate([s > 0 ? bo4[0] / 2 - 4 : -bo4[0] / 2, by - 3, bat4_lz]) cube([4, 3, bat[1]]);                               // lips
            hull() {                // shelf, 45 degrees underneath
                translate([-bo4[0] / 2 - 3, by - 3, bat4_lz - 3]) cube([bo4[0] + 6, hy - by + 3, 3]);
                translate([-bo4[0] / 2 - 3, hy - 1, bat4_lz - 3 - (hy - by + 3)]) cube([bo4[0] + 6, 1, 1]);
            }
            translate([-6, -door4_t - dr_fr - 3, bo4_z0 + bo4[1] + 0.5]) cube([12, 3.01, 8]);   // the door screw's boss (3 mm overhang)
        }
        box_prism(-door4_t - dr_fr - 1, 41);
        translate([-14, by - 4, bat4_lz - 4]) cube([10, 8, 5]);                                         // the box's leads: into the body
        translate([0, -door4_t - dr_fr - 3.5, door4[1] - 6]) rotate([-90, 0, 0]) cylinder(d = pilot_m3, h = 10, $fn = fn_small);   // M3 x 8
    }
}
module place_door_inv() rotate([-dp_tilt, 0, 0]) translate([0, -dp_y0, -door4_z0]) children();
module aa_cuts() place_door() {
    door_prism(0.3, -door4_t - 0.01, 30);         // the pocket the door sits in
    box_prism(-door4_t - 6, 1);                   // the opening
}
module sa_door() place_door() difference() {
    door_prism(0, -door4_t, 0);
    translate([0, 1, door4[1] - 6]) rotate([90, 0, 0]) { cylinder(d = clear_m3, h = 5, $fn = fn_small); cylinder(d = 6.4, h = 2.5, $fn = 24); }
}
// the door's two hooks (behind the opening's bottom edge): part of the door
module sa_door_hooks() place_door() for (s = [-1, 1]) translate([s * 22 - 5, 0, 0]) {
    translate([0, -door4_t - dr_fr - 0.6, bo4_z0 + 0.4]) cube([10, dr_fr + 0.6 + 0.01, 3]);
    translate([0, -door4_t - dr_fr - 0.6 - 2, bo4_z0 - 3]) cube([10, 2, 6.4]);
}
module sa_door_all() union() { sa_door(); sa_door_hooks(); }
module bat4_dummy() color("#151515") place_door() translate([-bat[0] / 2, -door4_t - bat4_clr - bat[2], bat4_lz]) cube([bat[0], bat[2], bat[1]]);

// the mic behind the chest's left dot; the right dot is a dimple
// (the mic and its extension socket stand clear of the wall at their top, where the chest leans back)
mic3 = [-mic_dot[0] - 5.5, max(front_y(-mic_dot[0] - 5.5, mic_dot[1] + 9.5), front_y(mic_dot[0] - 3.3, mic_dot[1] + 9.5)) + b_wall + 1.6, mic_dot[1]];
module mic4_cuts() {
    translate([-mic_dot[0], -10, mic_dot[1]]) rotate([-90, 0, 0]) cylinder(d = 3, h = front_y(-mic_dot[0], mic_dot[1]) + 10 + b_wall + 1, $fn = 20);
    translate([mic_dot[0], front_y(mic_dot[0], mic_dot[1]) - 5, mic_dot[1]]) rotate([-90, 0, 0]) cylinder(d = 3, h = 5 + 1.2, $fn = 20);
}
// fresh air in, high on the back
module back_vents() if (detail) for (i = [-3:3]) translate([i * 7 - 1.5, b_cy, 172]) cube([3, 120, 15]);

module sa_body() {
    difference() {
        union() {
            difference() { body_outer(); body_inner(); }
            for (k = [-1, 1]) intersection() { body_outer(); place_sh(k) sh_block(); }
            top_ledge();
            mic3_sleeve();
            ups_slider_tunnel();
            chg_sleeve();
            intersection() { body_outer(); aa_bay(); }
        }
        translate([0, 0, sh0 - 5]) linear_extrude(5) sq2d(300, 300, 4);
        for (k = [-1, 1]) place_sh(k) sh_cut();
        top_cuts();
        mic4_cuts();
        ups_slider_cut();
        chg_cut();
        br_holes();
        aa_cuts();
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
        translate([0, neck_y, puck_skirt[1]]) difference() { cylinder(r = puck_r, h = pz0 - puck_skirt[1] + 0.01, $fn = 96); translate([0, 0, -1]) cylinder(r = puck_skirt[0], h = 40, $fn = 96); }
    }
    translate([0, neck_y, pz0 - 1]) cylinder(r = neck_bore_r, h = puck_t + 2, $fn = 48);
    for (a = puck_scr) translate([0, neck_y, 0]) rotate(a) translate([head_screw_r, 0, pz0 - 1]) {
        cylinder(d = clear_m3, h = puck_t + 2, $fn = fn_small);
        translate([0, 0, 1 + puck_t - 4.5]) cylinder(d = 6.4, h = 10, $fn = 24);      // head 4.5 deep: 7.5 mm of the 12 in the coupler
    }
    for (a = head4_scr) translate([0, neck_y, 0]) rotate(a) translate([head_screw_r, 0, hd_z0 - 7.5]) cylinder(d = pilot_m3, h = 8, $fn = fn_small);
}

// -----------------------------------------------------------------------------
// HEAD
// -----------------------------------------------------------------------------
function e_front(yy, r = H4_fr) = yy >= r ? 0 : yy <= r * (1 - 1 / sqrt(2)) ? r * (2 - sqrt(2)) - yy : r - sqrt(r * r - (r - yy) * (r - yy));
function s_back(yy) = yy <= H4_y1 ? 1 : let (q = min(1, (yy - H4_y1) / (H4_d - H4_y1))) 1 - (1 - H4_smin) * (1 - sqrt(1 - q * q));
H4_ys = concat([0, 0.6, 1.2, 2, 3, 4, 5.5, 7, 9, 12], [for (k = [0:12]) H4_y1 + (H4_d - H4_y1) * (1 - cos(90 * k / 12))]);
module head4_shape(i, y0 = 0, y1 = H4_d) intersection() {
    hull() for (yy = H4_ys) let (s = s_back(yy), e = e_front(yy) + i)
        xz_rr(y4f + min(H4_d - i, max(i > 0 ? H4_face_t : 0, yy)), H4[0] * s - 2 * e, H4[1] * s - 2 * e, H4_R * s - e, z4c);
    translate([-300, y4f + y0, 0]) cube([600, y1 - y0, 800]);
}
// the hood: an arch on the head's top front, sloping back into the head
module hood_shape(i) hull() {
    for (yy = [0, 0.6, 1.2, 2, 3, 4, 5.5, 7, 9, 12]) let (e = e_front(yy) + i)
        translate([0, y4f + max(i > 0 ? H4_face_t : 0, yy), hood_c]) rotate([-90, 0, 0]) mirror([0, 1, 0]) linear_extrude(0.01) se2d(hood_ab[0] - e, hood_ab[1] - e, hood_n);
    translate([0, y4f + hood_d - 14, hood_c]) rotate([-90, 0, 0]) mirror([0, 1, 0]) linear_extrude(0.01) se2d(hood_ab[0] - i, hood_ab[1] - i, hood_n);
    translate([0, y4f + hood_d - i, hood_c - 12]) rotate([-90, 0, 0]) mirror([0, 1, 0]) linear_extrude(0.01) se2d(hood_ab[0] * 0.8 - i, hood_ab[1] * 0.6 - i, hood_n);
    // flare: a wider foot just under the head's top, so the hood grows out of it
    for (yy = [0, 1.2, 3, 5.5, 9, 12, hood_d - 24]) let (e = e_front(yy) + i, w = hood_ab[0] + 7 - e)
        translate([-w, y4f + max(i > 0 ? H4_face_t : 0, yy), hd_z0 + H4[1] - 4 - e]) cube([2 * w, 0.01, 1]);
}
module head4_outer(y0 = 0, y1 = H4_d) intersection() {
    union() { head4_shape(0); hood_shape(0); if (y0 < H4_split) translate([0, neck_y, hd_z0]) cylinder(r = puck_r, h = 8, $fn = 96); }
    translate([-300, y4f + y0, 0]) cube([600, y1 - y0, 800]);
}
module head4_inner() union() { head4_shape(H4_wall); hood_shape(H4_wall); }

// the dark arch (front view, x-z): the hood's outline inset 5 mm, above the screen
module arch2d() intersection() { translate([0, hood_c]) se2d(hood_ab[0] - 4.5, hood_ab[1] - 4.5, hood_n); translate([-100, scr_top + 8]) square([200, 100]); }
module arch_inlay() difference() {
    translate([0, y4f - 0.01, 0]) rotate([90, 0, 0]) mirror([0, 0, 1]) linear_extrude(inlay_t + 0.01) arch2d();
    translate([0, y4f - 1, cam4_z]) rotate([-90, 0, 0]) cylinder(d = cam_aperture, h = 10, $fn = 48);
}

h4_scr_bosses_y = scr_back_y + 14;       // the screen's bosses run back to here (wells from behind)
cov4_tab_z = z4c + H4[1] / 2 * s_back(H4_split + 10) - H4_wall - 0.5 - 2.5;   // the tabs' underside: under the roof at their back end
// the frame's lip: a ring just inside the cover's rim (locates it), joined to the frame's wall by a ledge
module frame_lip() difference() {
    union() {
        intersection() { head4_shape(H4_wall + 0.3); translate([-300, y4f + H4_split - 1.5, 0]) cube([600, 4.5, 800]); }
        intersection() { head4_shape(H4_wall - 0.5); translate([-300, y4f + H4_split - 1.5, 0]) cube([600, 1.5, 800]); }
    }
    head4_shape(H4_wall + 1.6);
    for (x = cov4_scr) translate([x - 8, 0, hd_z0 - 1]) cube([16, 300, 16]);
    for (x = cov4_tabs) translate([x - 11, 0, hd_z0 + H4[1] - 20]) cube([22, 300, 30]);
}
module sa_head_frame_body() {
    zt = hd_z0 + H4[1];
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
            for (dx = [-11, 11]) translate([cpl_c[0] + dx - 3, cpl_c[1] - hdmi_cpl[2]/2 - 1.8, hd_z0 + 1]) difference() {
                cube([6, hdmi_cpl[2] + 3.6, hdmi_cpl[1] * 0.6 + 3.3]);
                translate([-1, 1.5, 3.3]) cube([8, hdmi_cpl[2] + 0.6, 40]);
            }
            // the back cover's fixings, all reaching back past the split (they print upwards: the frame prints face down)
            frame_lip();
            for (x = cov4_scr) intersection() {      // bosses over the cover's floor: its 2 screws come up into them
                union() { head4_shape(0.01, 0, H4_split); head4_shape(H4_wall + 0.3, H4_split - 0.01, H4_split + 10); }
                translate([x - 6, y4f + H4_split - 12, hd_z0]) cube([12, 22, 12]);
            }
            for (x = cov4_tabs) {                    // tabs into the slots under the cover's roof, from blocks under the frame's roof
                intersection() { head4_shape(0.01); translate([x - 5.7, y4f + H4_split - 12, cov4_tab_z]) cube([11.4, 12, 40]); }
                translate([x - 5.7, y4f + H4_split - 1, cov4_tab_z]) cube([11.4, 11, 2.5]);
            }
            // the camera bracket's pivot bosses, from the hood's sides (45 degrees towards the face: prints face down)
            for (s = [-1, 1]) intersection() {
                hood_shape(0.01);
                translate([s * (cam_brk_wo + 0.2), y4f + cam_board_y + cam_pivot_dy, cam4_z]) rotate([0, s * 90, 0]) hull() {
                    cylinder(d = 6.6, h = 20, $fn = 32);
                    translate([0, -8, 0]) cylinder(d = 0.1, h = 20);
                }
            }
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
        for (x = cov4_scr) translate([x, y4f + H4_split + 5, hd_z0]) cylinder(d = pilot_m3, h = 14, $fn = fn_small);   // the cover's screws
        // camera: the lens, the pivot screws (M3 x 12 from outside, heads sunk)
        translate([0, y4f - 1, cam4_z]) rotate([-90, 0, 0]) cylinder(d = cam_aperture, h = H4_face_t + 2, $fn = 48);
        for (s = [-1, 1]) translate([s * 24, y4f + cam_board_y + cam_pivot_dy, cam4_z]) {
            rotate([0, 90, 0]) cylinder(d = clear_m3, h = 20, center = true, $fn = fn_small);
            translate([s * 26, 0, 0]) rotate([0, s * 90, 0]) cylinder(d = 6.4, h = 12, $fn = 24);
        }
    }
}
module sa_head_frame() difference() { sa_head_frame_body(); arch_inlay(); }   // white
module sa_head_inlay() intersection() { sa_head_frame_body(); arch_inlay(); } // dark: printed in the same print

// the back: a lip inside the frame's rim, 2 tabs under the roof, 2 screw tabs over the frame's floor bosses,
// slits behind the screen's speakers
module sa_head_back() {
    zt = hd_z0 + H4[1];
    difference() {
        union() {
            difference() { head4_outer(H4_split, H4_d); head4_inner(); }
            for (x = cov4_tabs) intersection() {    // the slots for the frame's tabs, under the roof
                head4_shape(0.01, H4_split, H4_d);
                translate([x - 9, y4f + H4_split, cov4_tab_z - 3]) difference() {
                    cube([18, 14, 20]);
                    translate([3, -1, 2.7]) cube([12, 12.5, 2.5 + 0.6]);
                }
            }
        }
        for (x = cov4_scr) translate([x, y4f + H4_split + 5, hd_z0 - 1]) {   // M3 x 12 from below into the frame's bosses
            cylinder(d = clear_m3, h = 14, $fn = fn_small);
            cylinder(d = 6.4, h = 1 + 4, $fn = 24);
        }
        // the speaker slits: behind the screen's two speakers
        if (detail) for (r = scr_spk) let (xa = scr_pt([r[0], 0])[0], xb = scr_pt([r[1], 0])[0], xm = (xa + xb) / 2)
            for (k = [-1.5:1:1.5]) translate([xm + k * 6 - 1.5, y4f + H4_split + 5, scr_top - 26]) cube([3, 60, 13]);
    }
}

// -----------------------------------------------------------------------------
// ARMS: soft paddles (local frame of the +x shoulder, as v3's)
// -----------------------------------------------------------------------------
module sa_arm_solid() intersection() {
    union() {
        ball([0, 0, 0], cap_d, 0.5);
        hull() { ball([0, 0, -6], pad4[0] + 2, pad4[2]); ball([0, -5, -pad4[1] + pad4[0] / 2], pad4[0] - 4, pad4[2]); }
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
// LAYOUT
// -----------------------------------------------------------------------------
module sa_electronics() {
    color("#d9d4c8") base_floor();
    color("#c8c2b4") { stack_keeper(); pca_clamp(0); pca_clamp(1); }
    ups_stack(); color("#2a2") pca_floor(); color("#333") usbc_socket();
    mic3_dummy(); bat4_dummy(); ups_knob_dummy();
}
module sa_layout() {
    sa_electronics();
    v3_neck_parts(head_angle);
    color(DARK4) v3_ring();
    color(WHITE4) sa_body();
    color(WHITE4) v3_neck_plate();
    color(DARK4) v3_ups_slider();
    color(DARK4) sa_door_all();
    place_head3(head_angle) {
        color(DARK4) sa_puck();
        color(WHITE4) render() sa_head_frame(); color(DARK4) render() sa_head_inlay(); color(WHITE4) render() sa_head_back();
        screen_dummy3(); screen_plugs3();
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
module print4(w) {
    if (w == "body")        translate([0, 0, -sh0]) sa_body();                                                   // upright (5 mm brim)
    if (w == "ring")        v3_ring();                                                                          // upright
    if (w == "neck_plate")  translate([0, 0, b_top]) mirror([0, 0, 1]) v3_neck_plate();                        // top face down
    if (w == "head_frame")  translate([0, 0, -y4f]) rotate([90, 0, 0]) sa_head_frame();                       // face down (tree supports)
    if (w == "head_inlay")  translate([0, 0, -y4f]) rotate([90, 0, 0]) sa_head_inlay();                       // the same print, dark filament
    if (w == "head_back")   translate([0, 0, -(y4f + H4_split)]) rotate([90, 0, 0]) sa_head_back();           // rim down (tree supports inside)
    if (w == "puck")        translate([0, 0, hd_z0]) mirror([0, 0, 1]) sa_puck();                             // top down
    if (w == "door")        rotate([-90, 0, 0]) place_door_inv() sa_door_all();                               // outer face down
    if (w == "arm_left")    rotate([0, -90, 0]) translate([-arm_gap3, 0, 0]) sa_arm();                         // inner face down
    if (w == "arm_right")   mirror([0, 1, 0]) print4("arm_left");
    if (w == "ups_slider")  translate([0, 0, sl_z0 + sl_h]) mirror([0, 0, 1]) v3_ups_slider();               // top down
}
if (part == "print4") print4(which);

// -----------------------------------------------------------------------------
// ASSEMBLED (part = "assy4", which = name): each component where it sits in the robot, for the assembled 3MF
// -----------------------------------------------------------------------------
module eyes4_2d() for (sx = [-1, 1]) translate([sx * 38, 4]) intersection() {   // happy closed eyes, as the concept
    difference() { circle(r = 17, $fn = 64); circle(r = 10.5, $fn = 64); }
    translate([0, 20 + 3]) square(40, center = true);
}
module assy4(w) {
    if (w == "ring")        v3_ring();
    if (w == "body")        sa_body();
    if (w == "neck_plate")  v3_neck_plate();
    if (w == "ups_slider")  v3_ups_slider();
    if (w == "door")        sa_door_all();
    if (w == "arm_left")    place_arm3(-1, arm_angle) sa_arm();
    if (w == "arm_right")   place_arm3(1, arm_angle) sa_arm();
    if (w == "puck")        place_head3(head_angle) sa_puck();
    if (w == "head_frame")  place_head3(head_angle) sa_head_frame();
    if (w == "head_inlay")  place_head3(head_angle) sa_head_inlay();
    if (w == "head_back")   place_head3(head_angle) sa_head_back();
    if (w == "camera_bracket") place_head3(head_angle) place_cam4() camera_bracket();
    if (w == "floor")       base_floor();
    if (w == "floor_clips") { stack_keeper(); pca_clamp(0); pca_clamp(1); }
    if (w == "coupler")     place_torso() translate([0, neck_yt, torso_h]) coupler();
    if (w == "gears")       place_torso() { translate([0, neck_yt, gear_z0]) ring_gear(); translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); }
    if (w == "servo_frame") place_torso() translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    if (w == "servos")      { place_torso() translate([0, pinion_yt, hs_spline_z]) servo_dummy(); for (k = [-1, 1]) arm_servo3(k); }
    if (w == "pi_ups")      { ups_stack(); ups_plugs(); }
    if (w == "pca")         pca_floor();
    if (w == "mic")         mic3_dummy();
    if (w == "aa_box")      bat4_dummy();
    if (w == "charge_socket") { usbc_socket(); charge_cable(); }
    if (w == "screen")      place_head3(head_angle) screen_dummy3();
    if (w == "screen_plugs") place_head3(head_angle) screen_plugs3();
    if (w == "eyes")        place_head3(head_angle) translate([0, g3_y - 0.05, scr_cz]) rotate([90, 0, 0]) linear_extrude(0.4) eyes4_2d();
    if (w == "camera")      place_head3(head_angle) {
                                translate([-cam_board/2, y4f + cam_board_y, cam4_z - cam_board/2]) cube([cam_board, cam_pcb_t, cam_board]);
                                translate([0, y4f + cam_board_y, cam4_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - H4_face_t - 0.5); }
}
if (part == "assy4") assy4(which);

echo(str("LittleBot v4: total height ", hood_c + hood_ab[1], " mm; head ", H4[0], " x ", H4[1], " x ", H4_d,
         "; door tilt ", dp_tilt, "; mic ", mic3));
