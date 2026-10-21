// =============================================================================
//  LittleBot v2 "companion" - parametric OpenSCAD model (mm)
// =============================================================================
//  A friendlier character design: round helmet head with ears and a black
//  visor, camera pod on top, rounded torso with a chest speaker, jointed-looking
//  arms, and a mobility-ready base (wheel wells, sensor window, battery bay,
//  electronics shelf, LED channel) for later phases.
//
//  Parts (-D 'part="..."'):
//    head        head shell (print FACE DOWN)          head_back  back cap (back face down)
//    visor       black screen visor (face down)        ear_left, ear_right (outer face down)
//    camera_pod  camera housing on the head (top down)  camera     camera bracket
//    lcd_tabs    display clamp tabs
//    coupler, ring_gear, pinion, servo_frame         geared neck (same principle as v1)
//    torso       torso shell (print TOP DOWN)          torso_back rear cover (flat)
//    arm_left, arm_right                               (outer face down)
//    base        base shell (TOP DOWN)                 base_floor removable floor (flat, stationary)
//    base_floor_mobile  floor for the wheel phase      shelf      electronics shelf
//    well_cover  x2 wheel-well covers (stationary)
//    front_blank / front_sonar   front plate (light bar; HC-SR04)   rear_blank / rear_usb  rear plate
//    assembly, exploded, section_side, section_plan   (preview only)
//
//  Coordinates: x = width (viewer's right +x), y = depth (FRONT of the base at
//  y = 0, back is +y), z = up (floor/table z = 0).
//  !! MEASURE-marked values are datasheet/typical values: check your parts.
// =============================================================================

part = "assembly";

/* [Quality] */
$fn = 64;
fn_small = 16;
detail = true;          // false: skip grilles/vents/text (fast interference checks)

/* [Fit] */
clr = 0.3;
pilot_m3 = 2.7;  clear_m3 = 3.4;
pilot_m25 = 2.2; clear_m25 = 2.8;
pilot_m2 = 1.8;

eps = 0.01;

/* [Base - mobility ready] */
base_w = 176;  base_d = 152;  base_h = 62;
base_rf = 42;  base_rb = 30;          // corner radii front / back
base_z0 = 10;                          // underside of the floor (the skirt below it stands on the table)
base_wall = 2.6; base_top_t = 3; base_floor_t = 3; base_chamfer = 6;
wheel_d = 65; wheel_w = 27; wheel_x = 72; wheel_y = 76;   // future TT-motor wheels (touch the table at z = 0)
well_w = 72; well_top = wheel_d + 2;                      // wheel-well opening in each side wall
bat_w = 68; bat_y0 = 34; bat_y1 = 134; bat_h = 30;        // battery bay: power bank <= 98 x 66 x 28
front_z = 40; front_w = 70; front_h = 34;                 // front sensor / light-bar plate opening
rear_x = 14; rear_z = 36; rear_w = 48; rear_h = 24;       // rear port plate opening
bezel_t = 2;                                              // plates sit 2 mm proud, held by countersunk M3
sonar_sp = 26;                                            // MEASURE: HC-SR04 transducer spacing
glow_w = 56; glow_h = 6; glow_skin = 1.0;                 // light bar: LED strip glows through a 1 mm skin

/* [Torso] */
torso_w0 = 118;  torso_w1 = 132;  torso_wz = 85;   // width at the bottom / from z=85 upwards
torso_d = 98;    torso_h = 125;
torso_rf = 34;   torso_rb = 16;
torso_wall = 2.5; torso_top_t = 3; torso_chamfer = 6;
torso_y0 = 27;                         // torso front, in base coordinates
neck_yt = 62;                          // neck axis in torso coordinates (pinion clears the chamfered front)

/* [Neck - geared turntable] */
gear_m = 1.25;  ring_teeth = 36;  pinion_teeth = 24;   // head = 0.67 x servo angle
gear_t = 6; gear_backlash = 0.15; gear_cd_extra = 0.15; gear_gap = 0.4;
bore_r = 13; spigot_r = 17.6; coupler_r = 25; coupler_h = 10; bearing_h = 0.8;

/* [Head] */
head_w = 150; head_h = 132; head_d = 120;
head_r = 50;                           // corner radius (front view)
head_back_w = 112; head_back_h = 104; head_back_r = 40;
head_wall = 2.5; head_bottom_t = 3; head_chamfer = 4;
head_split = 100;                      // back cap starts here (y, head coordinates)
head_neck_y = 60;                      // neck axis from the head front
visor_w = 112; visor_h = 86; visor_r = 30; visor_t = 4; visor_z = 68;
ear_d = 52; ear_z = 66; ear_y = 62; ear_proud = 12; ear_screw_dz = 23;
led_ring_r0 = 11; led_ring_r1 = 19.5;   // pocket for a 12-LED ring (37 mm OD, 23 mm ID)

/* [Display - MPI3508] */
lcd_w = 85.51; lcd_h = 56.0; lcd_stack = 7.32;
aa_w = 73.44; aa_h = 48.96; lcd_aa_offset = 3.03; window_margin = 1.0; pcb_overhang = 2.3;

/* [Camera - U20CAM-1080P] */
cam_board = 32; cam_hole_sp = 28;      // MEASURE
pod_w = 56; pod_d = 40; pod_h = 40; pod_y0 = 6;
cam_board_y = 26;                      // board position from the head front (lens length)
cam_aperture = 17;

/* [Speaker - Adafruit 3369] */
spk_w = 84; spk_h = 43; spk_d = 32; spk_z = 41;   // MEASURE; spk_z = bottom of the speaker (torso coords)
spk_y = 6;                                        // speaker front, behind the chest grille

/* [Microphone - Adafruit 3367 mini USB mic, 22.2 x 18.3 x 7.0] */
mic_l = 22.2; mic_w = 18.3; mic_t = 7.0; mic_body = 10.2;  // body = the part outside the USB plug
mic_x0 = -34; mic_zc = 100;            // closed end of the sleeve / centre height (torso coords)
mic_y0 = torso_wall + 1.5;             // mic face 1.5 mm behind the chest wall (leaves room for the socket)

/* [Servos - MG90S] */
sv_l = 22.8; sv_w = 12.2; sv_tab_l = 32.2; sv_tab_t = 2.5; sv_tab_z = 15.9;
sv_case_h = 22.7; sv_boss_h = 28.5; sv_spline_h = 32.0; sv_shaft_off = 6.1;
sv_hole_sp = 27.6; sv_boss_d = 12.4;

/* [Arms] */
shoulder_yt = 45; shoulder_zt = 100;   // torso coordinates
arm_gap = 3; arm_t = 15; horn_plate_t = 1.6;
arm_neutral = 8;
arm_min = -90; arm_max = 100;      // safe arm range (degrees, + = forwards/up), verified by run_checks.sh

// -----------------------------------------------------------------------------
// Derived
// -----------------------------------------------------------------------------
base_top = base_z0 + base_h;
torso_z0 = base_top;
torso_top_in = torso_h - torso_top_t;
ring_rp = gear_m * ring_teeth / 2;  pin_rp = gear_m * pinion_teeth / 2;
gear_cd = ring_rp + pin_rp + gear_cd_extra;
pinion_yt = neck_yt - gear_cd;         // pinion IN FRONT of the neck
gear_z1 = torso_top_in - gear_gap;  gear_z0 = gear_z1 - gear_t;
hs_spline_z = gear_z0 + 2.2;
hs_tab_top = hs_spline_z - sv_spline_h + sv_tab_z + sv_tab_t;
sv_mid = sv_l/2 - sv_shaft_off;
sv_hole_a = sv_mid - sv_hole_sp/2;  sv_hole_b = sv_mid + sv_hole_sp/2;
frame_t = 3; frame_pillar_x = [-28, 30];
arm_inner_x = torso_w1/2 + arm_gap;
as_spline_x = arm_inner_x + horn_plate_t - 0.6;
as_tab_top_x = as_spline_x - (sv_spline_h - sv_tab_z - sv_tab_t);
rib_t = 3;
neck_y = torso_y0 + neck_yt;
head_y0 = neck_y - head_neck_y;
head_z0 = torso_z0 + torso_h + bearing_h + coupler_h;
lcd_x0 = -lcd_w/2 - lcd_aa_offset;  lcd_x1 = lcd_x0 + lcd_w;
lcd_z0 = visor_z - lcd_h/2;  lcd_z1 = lcd_z0 + lcd_h;
visor_back_y = 3;                      // visor seat depth in the head front

echo(str("LittleBot v2: total height ", head_z0 + head_h, " mm; widest ", 2 * (arm_inner_x + arm_t), " mm (arms) / ", base_w, " (base)"));
echo(str("LittleBot v2: HDMI plug clearance below display = ", lcd_z0 - pcb_overhang - head_bottom_t, " mm (needs >= 31 for a straight plug)"));

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------
module rrect(w, d, r) { rr = min(r, w/2 - eps, d/2 - eps); offset(r = rr) square([w - 2*rr, d - 2*rr], center = true); }
// footprint with different front/back corner radii (front at y=0)
module shape2d(w, d, rf, rb) {
    hull() for (s = [-1, 1]) { translate([s * (w/2 - rf), rf]) circle(r = rf); translate([s * (w/2 - rb), d - rb]) circle(r = rb); }
}
module xz_extrude(y0, y1) { translate([0, y1, 0]) rotate([90, 0, 0]) linear_extrude(y1 - y0) children(); }
module cyl_y(d, y0, y1, fn = $fn) { translate([0, y0, 0]) rotate([-90, 0, 0]) cylinder(d = d, h = y1 - y0, $fn = fn); }
module teardrop_x(d, len) { rotate([0, 90, 0]) linear_extrude(len, center = true) hull() { circle(d = d, $fn = 32); translate([d/2 * 1.414, 0]) square(eps, center = true); } }
// horizontal standoff along +y whose underside is pointed (prints without support when the part is upside down)
module teardrop_y(d, len, tip = -1) { rotate([-90, 0, 0]) linear_extrude(len) hull() { circle(d = d, $fn = 24); translate([0, -tip * d/2 * 1.414]) square(eps, center = true); } }

function polar(r, a) = [r * cos(a), r * sin(a)];
function inv_deg(a) = (tan(a) - a * PI / 180) * 180 / PI;
function flank(r, rb, th, invpa) = th + invpa - inv_deg(acos(min(1, rb / r)));
function gear_pts(m, z, pa = 20, bl = 0.15, n = 7) =
    let (rp = m * z / 2, rb = rp * cos(pa), ra = rp + m, rf = rp - 1.25 * m, th = 90 / z - (bl / (2 * rp)) * 180 / PI,
         invpa = inv_deg(pa), r0 = max(rb, rf), rs = [for (i = [0:n]) r0 + (ra - r0) * i / n], fb = flank(r0, rb, th, invpa))
    [for (k = [0:z-1]) let (c = k * 360 / z) each concat([polar(rf, c - fb)], [for (r = rs) polar(r, c - flank(r, rb, th, invpa))],
        [for (i = [n:-1:0]) polar(rs[i], c + flank(rs[i], rb, th, invpa))], [polar(rf, c + fb)])];
module gear2d(m, z) { polygon(gear_pts(m, z, bl = gear_backlash)); }

module horn_pocket(depth, arm_r = 14) {
    linear_extrude(depth) { circle(r = 4.4, $fn = 32);
        for (a = [0:90:270]) rotate(a) hull() { square([0.1, 6.2], center = true); translate([arm_r - 2.4, 0]) circle(r = 2.4, $fn = 24); } }
}

// =============================================================================
//  BASE
// =============================================================================
// The walls run down to the table (a skirt): no separate feet, which would be small parts.
module base_outer() {
    hull() {
        linear_extrude(base_top - base_chamfer) shape2d(base_w, base_d, base_rf, base_rb);
        translate([0, 0, base_top - eps]) linear_extrude(eps) offset(-base_chamfer) shape2d(base_w, base_d, base_rf, base_rb);
    }
}
module base_inner() {
    c = max(0.5, base_chamfer - base_wall * 0.41);
    translate([0, 0, -1]) hull() {
        linear_extrude(base_top - base_top_t - c + 1) offset(-base_wall) shape2d(base_w, base_d, base_rf, base_rb);
        translate([0, 0, base_top - base_top_t + 1 - eps]) linear_extrude(eps) offset(-base_wall - c) shape2d(base_w, base_d, base_rf, base_rb);
    }
}
function base_corner_pts() = [[-62, 18], [62, 18], [-66, 134], [66, 134]];          // floor screws
function torso_fix_pts() = [[-46, 12], [46, 12], [-50, 86], [50, 86]];              // torso coordinates
function front_screw_pts() = [for (s = [-1, 1]) [s * (front_w/2 + 5), front_z]];    // (x, z)
function rear_screw_pts() = [for (s = [-1, 1]) [rear_x + s * (rear_w/2 + 5), rear_z]];
well_screw_u = 30; well_screw_z = 60; well_low_z = base_z0 + base_floor_t + 5; well_pillar_x = base_w/2 - base_wall - 4 + eps;
// wheel-well opening, 2D in (u = y - wheel_y, z); open at the bottom
module well_shape() { offset(r = 8) offset(delta = -8) translate([-well_w/2, base_z0 - 30]) square([well_w, well_top - base_z0 + 30]); }

module base_shell() {
    zt = base_top - base_top_t;
    difference() {
        union() {
            difference() { base_outer(); base_inner(); }
            intersection() {
                base_outer();
                union() {
                    for (p = base_corner_pts()) translate([p[0], p[1], base_z0 + base_floor_t]) cylinder(d = 9, h = zt - base_z0 - base_floor_t + eps);
                    // plate screw pillars (front + rear), fused to the wall
                    for (p = front_screw_pts()) translate([p[0], base_wall + 3, p[1] - 8]) cylinder(d = 8, h = zt - p[1] + 8 + eps);
                    for (p = rear_screw_pts()) translate([p[0], base_d - base_wall - 3, p[1] - 8]) cylinder(d = 8, h = zt - p[1] + 8 + eps);
                    for (p = torso_fix_pts()) translate([p[0], torso_y0 + p[1], zt - 5]) cylinder(d = 9, h = 5 + eps);    // torso screw bosses
                }
            }
        }
        // floor screw pilots
        for (p = base_corner_pts()) translate([p[0], p[1], base_z0 - 1]) cylinder(d = pilot_m3, h = 14, $fn = fn_small);
        // wheel wells (closed by well_cover until the wheels are fitted)
        for (s = [-1, 1]) translate([s * (base_w/2 - 3), wheel_y, 0]) rotate([90, 0, 90]) linear_extrude(14, center = true) well_shape();
        // front sensor / light-bar window and rear port window, with the plates' screw holes
        translate([0, -1, front_z]) rotate([-90, 0, 0]) linear_extrude(base_wall + 2) rrect(front_w, front_h, 6);
        for (p = front_screw_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m3, -1, base_wall + 9, fn_small);
        translate([rear_x, base_d - base_wall - 1, rear_z]) rotate([-90, 0, 0]) linear_extrude(base_wall + 2) rrect(rear_w, rear_h, 5);
        for (p = rear_screw_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m3, base_d - base_wall - 9, base_d + 1, fn_small);
        // rear: servo power jack + switch (future 5 V servo supply)
        translate([-46, base_d - base_wall - 1, 56]) rotate([-90, 0, 0]) cylinder(d = 8.2, h = base_wall + 2);
        translate([-30, base_d - base_wall - 1, 56]) rotate([-90, 0, 0]) cylinder(d = 6.3, h = base_wall + 2);
        if (detail) translate([-38, base_d - 0.6, 62]) rotate([90, 0, 180]) linear_extrude(1)
            text("SERVO 5V", size = 4, halign = "center", font = "DejaVu Sans:style=Bold");
        // top: cable pass-through under the torso, torso screw holes
        translate([0, torso_y0, zt - 1]) linear_extrude(base_top_t + 2) translate([0, 81]) rrect(64, 30, 6);
        for (p = torso_fix_pts()) translate([p[0], torso_y0 + p[1], zt - 6]) cylinder(d = clear_m3, h = 10, $fn = fn_small);
    }
    // well-cover top screw pillars: hang from the top, above the future wheels' reach
    difference() {
        intersection() { base_outer(); for (s = [-1, 1]) for (u = [-1, 1])
            translate([s * well_pillar_x, wheel_y + u * well_screw_u, well_screw_z - 4]) cylinder(d = 8, h = zt - well_screw_z + 4 + eps); }
        for (s = [-1, 1]) for (u = [-1, 1]) translate([s * well_pillar_x, wheel_y + u * well_screw_u, well_screw_z]) rotate([0, 90, 0]) cylinder(d = pilot_m3, h = 12, center = true, $fn = fn_small);
    }
}

// mobile = false: stationary floor (feet). mobile = true: wheel slots, motor + caster grids.
module base_floor(mobile = false) {
    zf = base_z0;
    difference() {
        union() {
            translate([0, 0, zf]) linear_extrude(base_floor_t) offset(-base_wall - clr) shape2d(base_w, base_d, base_rf, base_rb);
            // battery bay walls (+ posts carrying the shelf)
            for (s = [-1, 1]) translate([s * (bat_w/2 + 1.2), (bat_y0 + bat_y1)/2, zf + base_floor_t + bat_h/2 - eps]) cube([2.4, bat_y1 - bat_y0, bat_h], center = true);
            for (s = [-1, 1]) for (yy = [bat_y0 + 6, bat_y1 - 6]) translate([s * shelf_post_x, yy, zf + base_floor_t - eps]) cylinder(d = 7, h = bat_h);
            // well-cover bottom screw bosses (stationary floor only)
            if (!mobile) intersection() {
                translate([0, 0, zf]) linear_extrude(20) offset(-base_wall - clr) shape2d(base_w, base_d, base_rf, base_rb);
                for (s = [-1, 1]) for (u = [-1, 1]) translate([s * well_pillar_x, wheel_y + u * well_screw_u, zf + base_floor_t - eps]) cylinder(d = 8, h = 10);
            }
        }
        if (!mobile) for (s = [-1, 1]) for (u = [-1, 1]) translate([s * well_pillar_x, wheel_y + u * well_screw_u, well_low_z]) rotate([0, 90, 0]) cylinder(d = pilot_m3, h = 12, center = true, $fn = fn_small);
        // shelf post pilots
        for (s = [-1, 1]) for (yy = [bat_y0 + 6, bat_y1 - 6]) translate([s * shelf_post_x, yy, zf + base_floor_t + bat_h - 8]) cylinder(d = pilot_m3, h = 10, $fn = fn_small);
        // countersunk screws into the base corner pillars
        for (p = base_corner_pts()) translate([p[0], p[1], zf - 1]) {
            cylinder(d = clear_m3, h = base_floor_t + 2, $fn = fn_small);
            translate([0, 0, 1 - eps]) cylinder(d1 = 6.4, d2 = clear_m3, h = 1.6, $fn = 24);
        }
        // strap slots for the battery (hook-and-loop strap)
        for (yy = [bat_y0 + 20, bat_y1 - 20]) for (s = [-1, 1]) translate([s * (bat_w/2 - 4) - 1.5, yy - 13, zf - 1]) cube([3, 26, base_floor_t + 2]);
        if (mobile) {
            // wheel slots (open to the side), motor mounting grid (M3, 10 mm pitch), caster grids front + back
            for (s = [-1, 1]) translate([s > 0 ? wheel_x - wheel_w/2 - 1.5 : -base_w/2 - 1, wheel_y - well_w/2, zf - 1])
                cube([base_w/2 + 1 - (wheel_x - wheel_w/2 - 1.5), well_w, base_floor_t + 2]);
            for (s = [-1, 1]) for (gx = [40, 50]) for (gy = [46:10:106]) translate([s * gx, gy, zf - 1]) cylinder(d = pilot_m3, h = base_floor_t + 2, $fn = 12);
            for (cy = [12, bat_y1 + 7]) for (gx = [-10:10:10]) for (gy = [-5, 5]) translate([gx, cy + gy, zf - 1]) cylinder(d = pilot_m3, h = base_floor_t + 2, $fn = 12);
        }
    }
}
shelf_post_x = bat_w/2 + 3.5;   // posts just outside the bay walls

module shelf() {
    sx = shelf_post_x + 5; y0 = bat_y0 + 1; y1 = bat_y1 - 1;
    difference() {
        translate([0, (y0 + y1)/2, 0]) linear_extrude(2.5) rrect(2 * sx, y1 - y0, 4);
        for (s = [-1, 1]) for (yy = [bat_y0 + 6, bat_y1 - 6]) translate([s * shelf_post_x, yy, -1]) cylinder(d = clear_m3, h = 5, $fn = fn_small);
        for (gx = [-25:10:25]) for (gy = [y0 + 9:10:y1 - 9]) if (gy < 88) translate([gx, gy, -1]) cylinder(d = 2.5, h = 5, $fn = 10);
        translate([0, torso_y0 + 81, -1]) linear_extrude(5) rrect(62, 34, 6);    // room for the Pi's USB plugs
    }
}

// Wheel-well cover: fills the opening flush (keeps little fingers out until the wheels are
// fitted). Pushed in from outside after the floor; 4 countersunk M3: two into pillars under
// the top, two into bosses on the stationary floor.
// Local: x = u (along the robot's depth), y = z (height, absolute), z = 0 outer face, +z inwards.
module well_cover() {
    difference() {
        linear_extrude(base_wall) intersection() { offset(-clr) well_shape(); translate([-100, clr]) square([200, 100]); }
        for (u = [-1, 1]) for (zz = [well_low_z, well_screw_z]) translate([u * well_screw_u, zz, -1]) {
            cylinder(d = clear_m3, h = 10, $fn = fn_small);
            translate([0, 0, 1 - eps]) cylinder(d1 = 6.4, d2 = clear_m3, h = 1.6, $fn = 24);
        }
    }
}

// Front / rear plates: a bezel 2 mm proud of the wall + a plug in the window, two countersunk
// M3 through the bezel and wall into pillars. Local: face at z = 0, +z inwards, centred on the window.
// kind: "blank" (light bar + two headlight dimples), "sonar" (HC-SR04 + light bar), "rear", "rear_usb".
module port_plate(w, h, kind) {
    front = kind == "blank" || kind == "sonar";
    sy = front ? 4 : 0;                          // sonar / dimple centre height
    difference() {
        union() {
            hull() { linear_extrude(eps) rrect(w + 18, h + 6, 6); translate([0, 0, 1]) linear_extrude(bezel_t - 1) rrect(w + 20, h + 8, 7); }
            translate([0, 0, bezel_t - eps]) linear_extrude(base_wall) rrect(w - 2*clr, h - 2*clr, 5);
            if (kind == "sonar") for (sx = [-1, 1]) for (sz = [-1, 1]) translate([sx * 20.5, sy + sz * 8.5, bezel_t + base_wall - eps]) cylinder(d = 5, h = 12 - bezel_t - base_wall);
        }
        for (s = [-1, 1]) translate([s * (w/2 + 5), 0, -1]) {
            cylinder(d = clear_m3, h = 10, $fn = fn_small);
            translate([0, 0, 1 - eps]) cylinder(d1 = 6.4, d2 = clear_m3, h = 1.6, $fn = 24);
        }
        if (front) translate([0, -11, glow_skin]) linear_extrude(10) rrect(glow_w, glow_h, 2);        // light-bar pocket (LED strip from behind)
        if (kind == "sonar") {
            for (s = [-1, 1]) translate([s * sonar_sp/2, sy, -1]) cylinder(d = 16.6, h = 20);
            for (sx = [-1, 1]) for (sz = [-1, 1]) translate([sx * 20.5, sy + sz * 8.5, bezel_t + 0.6]) cylinder(d = pilot_m2, h = 20, $fn = 10);  // MEASURE HC-SR04 holes
        }
        if (kind == "blank") for (s = [-1, 1]) translate([s * sonar_sp/2, sy, -eps]) cylinder(d1 = 12, d2 = 8, h = 1);   // friendly "headlight" dimples
        if (kind == "rear_usb") hull() for (s = [-1, 1]) translate([s * 4, 0, -1]) cylinder(d = 8, h = 20, $fn = 24);     // charging-cable slot
    }
}
module place_front_plate() { translate([0, -bezel_t, front_z]) rotate([-90, 0, 0]) rotate([0, 0, 180]) children(); }
module place_rear_plate()  { translate([rear_x, base_d + bezel_t, rear_z]) rotate([90, 0, 0]) children(); }
module place_well_cover(s) { translate([s * base_w/2, wheel_y, 0]) rotate([90, 0, s > 0 ? -90 : 90]) children(); }

// =============================================================================
//  TORSO  (local: front y=0, bottom z=0)
// =============================================================================
function torso_w(z) = z >= torso_wz ? torso_w1 : torso_w0 + (torso_w1 - torso_w0) * z / torso_wz;
module tshape(z, inset = 0) { offset(-inset) shape2d(torso_w(z), torso_d, torso_rf, torso_rb); }
module torso_outer() {
    hull() {
        linear_extrude(eps) tshape(0);
        translate([0, 0, torso_wz]) linear_extrude(torso_h - torso_chamfer - torso_wz) tshape(torso_wz);
        translate([0, 0, torso_h - eps]) linear_extrude(eps) tshape(torso_h, torso_chamfer);
    }
}
module torso_inner() {
    c = max(0.5, torso_chamfer - torso_wall * 0.41);
    hull() {
        translate([0, 0, -1]) linear_extrude(eps) tshape(0, torso_wall);
        translate([0, 0, torso_wz]) linear_extrude(torso_top_in - c - torso_wz) tshape(torso_wz, torso_wall);
        translate([0, 0, torso_top_in - eps]) linear_extrude(eps) tshape(torso_top_in, torso_wall + c);
    }
}

// Pi 5 mounted upright on the spine: USB ports point DOWN (into the base),
// USB-C + micro-HDMI point to the robot's left (+x), GPIO on the right.
pi_z_top = 100; pi_spine_y = 62; pi_standoff = 6;   // Pi board at y 68 so GPIO jumpers clear the back cover
function pi_holes_t() = [for (px = [3.5, 61.5]) for (py = [3.5, 52.5]) [28 - py, pi_z_top - px]];   // (x, z)

module torso_shell() {
    difference() {
        union() {
            difference() { torso_outer(); torso_inner(); }
            intersection() {
                torso_outer();
                union() {
                    torso_gear_guard();
                    for (px = frame_pillar_x) translate([px, pinion_yt, hs_tab_top - sv_tab_t]) cylinder(d = 8, h = torso_top_in - hs_tab_top + sv_tab_t + eps);
                    torso_arm_ribs();
                    // Pi spine: plate + two columns up to the top plate
                    translate([0, pi_spine_y, 0]) rotate([90, 0, 0]) linear_extrude(2.5) polygon([[-30, 10], [30, 10], [30, 113], [0, 83], [-30, 113]]);
                    for (s = [-1, 1]) translate([s * 32, pi_spine_y - 1.25, 10]) cylinder(d = 8, h = torso_top_in - 10 + eps);
                    for (p = pi_holes_t()) translate([p[0], pi_spine_y - eps, p[1]]) teardrop_y(6, pi_standoff, tip = 1);
                    // speaker ribs (zip-tie slots)
                    for (s = [-1, 1]) translate([s > 0 ? spk_w/2 + 0.3 : -spk_w/2 - 3.3, 0, spk_z - 2]) {
                        translate([0, spk_y - 4, 0]) cube([3, spk_d + 3, torso_top_in - spk_z + 2 + eps]);
                        translate([0, spk_y + spk_d - 2, 0]) cube([3, 4, spk_h + 6]); }
                    // rear-cover columns
                    for (s = [-1, 1]) translate([s * 44, torso_d - torso_wall - 3.5, 8]) cylinder(d = 8, h = torso_top_in - 8 + eps);
                    // microphone sleeve: the dongle lies flat behind the chest holes, its USB plug pointing
                    // to the middle, where the extension cable's socket plugs on (open end, +x)
                    difference() {
                        translate([mic_x0 - 2, 0, mic_zc - mic_w/2 - 2.4]) cube([mic_body + 2, mic_y0 + mic_t + 2.5, torso_top_in - mic_zc + mic_w/2 + 2.4 + eps]);
                        translate([mic_x0, torso_wall - 1, mic_zc - mic_w/2 - 0.4]) cube([30, mic_y0 + mic_t + 0.5 - torso_wall + 1, mic_w + 0.8]);
                    }
                    // base fixing wedges (screws come up from inside the base)
                    for (p = torso_fix_pts()) let (sx = sign(p[0]), reach = 57 - (abs(p[0]) - 5)) translate([p[0], p[1], 0]) hull() {
                        translate([-5, -5, 0]) cube([10, 10, 9]); translate([sx * (57 - abs(p[0])) - 0.005, -5, 0]) cube([0.01, 10, 9 + reach]); }
                }
            }
        }
        // neck hole
        translate([0, neck_yt, torso_top_in - 1]) cylinder(r = spigot_r + clr, h = torso_top_t + 2);
        // Pi standoff pilots
        for (p = pi_holes_t()) translate([p[0], pi_spine_y - 3, p[1]]) rotate([-90, 0, 0]) cylinder(d = pilot_m25, h = pi_standoff + 4, $fn = fn_small);
        // arm shaft holes
        for (s = [-1, 1]) translate([s * (torso_w1/2 - torso_wall/2), shoulder_yt, shoulder_zt]) teardrop_x(sv_boss_d + 1.6, torso_wall + 2);
        // arm-servo tab pilots and windows are in torso_arm_ribs()
        // chest speaker grille: rings of 3 mm holes
        if (detail) translate([0, -1, spk_z + spk_h/2]) rotate([-90, 0, 0]) {
            cylinder(d = 3, h = 8, $fn = fn_small);
            for (ring = [1:4]) for (k = [0:6 * ring - 1]) rotate(k * 360 / (6 * ring)) translate([ring * 6, 0, 0]) cylinder(d = 3, h = 8, $fn = fn_small);
        }
        // microphone holes
        if (detail) for (dz = [-5, 0, 5]) for (dx = [2, 5.5, 9]) translate([mic_x0 + dx, -1, mic_zc + dz]) rotate([-90, 0, 0]) cylinder(d = 2.2, h = 8, $fn = 10);
        // rear opening (U-shaped, open at the bottom -> prints without bridging)
        translate([-46, torso_d - torso_wall - 1, -1]) cube([92, torso_wall + 2, 113]);
        for (s = [-1, 1]) for (zz = [22, 100]) translate([s * 44, torso_d - 9, zz]) rotate([-90, 0, 0]) cylinder(d = pilot_m3, h = 12, $fn = fn_small);
        // base fixing pilots
        for (p = torso_fix_pts()) translate([p[0], p[1], -1]) cylinder(d = pilot_m3, h = 10, $fn = fn_small);
        // speaker cable windows through the ribs (whichever end the cable leaves from)
        for (s = [-1, 1]) translate([s * (spk_w/2 + 1.8), spk_y + spk_d/2 + 1, spk_z + spk_h/2]) cube([5, 18, 14], center = true);
        // speaker zip-tie slots
        for (s = [-1, 1]) for (zz = [spk_z + 8, spk_z + spk_h - 8]) translate([s * (spk_w/2 + 1.8), spk_y + spk_d + 0.5, zz]) cube([5, 3, 3.5], center = true);
    }
}

module torso_gear_guard() {
    gz0 = gear_z0 - 1.6;
    translate([0, 0, gz0]) linear_extrude(torso_top_in - gz0 + eps) difference() {
        hull() { translate([0, neck_yt]) circle(r = ring_rp + gear_m + 3.6); translate([0, pinion_yt]) circle(r = pin_rp + gear_m + 3.6); }
        hull() { translate([0, neck_yt]) circle(r = ring_rp + gear_m + 2.0); translate([0, pinion_yt]) circle(r = pin_rp + gear_m + 2.0); }
    }
}

module torso_arm_ribs() {
    ry0 = shoulder_yt + sv_hole_a - 5; ry1 = shoulder_yt + sv_hole_b + 5;
    for (s = [-1, 1]) difference() {
        translate([s > 0 ? as_tab_top_x : -as_tab_top_x - rib_t, ry0, shoulder_zt - sv_w/2 - 7])
            cube([rib_t, ry1 - ry0, torso_top_in - (shoulder_zt - sv_w/2 - 7) + eps]);
        translate([s * (as_tab_top_x + rib_t/2), shoulder_yt + sv_mid, shoulder_zt]) cube([rib_t + 2, sv_l + 2*clr, sv_w + 2*clr], center = true);
        for (hy = [sv_hole_a, sv_hole_b]) translate([s * (as_tab_top_x + rib_t/2), shoulder_yt + hy, shoulder_zt])
            rotate([0, 90, 0]) cylinder(d = pilot_m2, h = rib_t + 2, center = true, $fn = fn_small);
    }
}

module torso_back() {
    // rear cover: flush panel + inner lip; 4 countersunk M3 into the columns
    difference() {
        union() {
            translate([0, 0, 0]) cube([92 - 2*clr, torso_wall, 112 - clr]);
            translate([-3, torso_wall - eps, -eps]) cube([98, 1.6, 112 + 2.5]);
        }
        for (s = [-1, 1]) for (zz = [22, 100]) translate([46 + s * 44 - clr, -1, zz]) {
            rotate([-90, 0, 0]) cylinder(d = clear_m3, h = 10, $fn = fn_small);
            rotate([-90, 0, 0]) cylinder(d1 = 6.4, d2 = clear_m3, h = 1.6, $fn = 24);
        }
        // cooling slots in front of the Pi's fan + a round "dot" grille
        if (detail) for (vx = [-15:5:15]) translate([46 + vx - 1.25 - clr, -1, 40]) cube([2.5, 6, 34]);
        if (detail) for (ring = [0:2]) for (k = [0:max(1, 6 * ring) - 1]) rotate([0, 360 * k / max(1, 6 * ring), 0])
            translate([46 - clr + ring * 5, -1, 92]) rotate([-90, 0, 0]) cylinder(d = 2.5, h = 6, $fn = 10);
    }
}

// =============================================================================
//  NECK (coupler + gears + servo frame) - same design as v1
// =============================================================================
head_screw_angles = [30, 140, 290];
ring_screw_angles = [80, 200, 320];
head_screw_r = 20.5;
ring_screw_r = (bore_r + spigot_r) / 2;

module coupler() {
    difference() {
        union() {
            difference() { cylinder(r = coupler_r - 0.5, h = bearing_h + eps); translate([0, 0, -1]) cylinder(r = coupler_r - 4, h = 3); }
            translate([0, 0, bearing_h]) hull() {
                cylinder(r = coupler_r - 1.2, h = eps);
                translate([0, 0, 1.2]) cylinder(r = coupler_r, h = coupler_h - 2.4);
                translate([0, 0, coupler_h - eps]) cylinder(r = coupler_r - 1.2, h = eps);
            }
            translate([0, 0, -torso_top_t - gear_gap]) cylinder(r = spigot_r, h = torso_top_t + gear_gap + bearing_h + eps);
        }
        translate([0, 0, -20]) cylinder(r = bore_r, h = 50);
        for (a = head_screw_angles) rotate(a) translate([head_screw_r, 0, bearing_h + coupler_h - 9]) cylinder(d = pilot_m3, h = 10, $fn = fn_small);
        for (a = ring_screw_angles) rotate(a) translate([ring_screw_r, 0, -torso_top_t - gear_gap - 1]) cylinder(d = pilot_m25 - 0.1, h = 11, $fn = fn_small);
        rotate(-90) translate([coupler_r - 0.6, 0, bearing_h + 3]) cylinder(d = 1.6, h = coupler_h, $fn = 12);
    }
}
module ring_gear() {
    difference() {
        linear_extrude(gear_t) gear2d(gear_m, ring_teeth);
        translate([0, 0, -1]) cylinder(r = bore_r, h = gear_t + 2);
        for (a = ring_screw_angles) rotate(a) translate([ring_screw_r, 0, 0]) {
            translate([0, 0, -1]) cylinder(d = clear_m25, h = gear_t + 2, $fn = fn_small);
            translate([0, 0, -eps]) cylinder(d = 5.2, h = 2.5, $fn = 24);
        }
    }
}
module pinion() {
    difference() {
        linear_extrude(gear_t) gear2d(gear_m, pinion_teeth);
        translate([0, 0, -eps]) horn_pocket(2.2, 12.5);
        translate([0, 0, -1]) cylinder(d = 6, h = gear_t + 2);
        for (a = [0, 180]) for (r = [7.5, 10]) rotate(a) translate([r, 0, 1]) cylinder(d = 1.3, h = gear_t, $fn = 10);
    }
}
module servo_frame() {
    difference() {
        union() {
            hull() for (px = frame_pillar_x) translate([px, 0, 0]) cylinder(d = 10, h = frame_t);
            translate([sv_mid, 0, frame_t / 2]) cube([sv_tab_l + 6, sv_w + 10, frame_t], center = true);
            for (hx = [sv_hole_a, sv_hole_b]) translate([hx, 0, -5]) cylinder(d = 6.5, h = 5 + eps);
        }
        translate([sv_mid, 0, 0]) cube([sv_l + 2*clr, sv_w + 2*clr, 30], center = true);
        for (px = frame_pillar_x) translate([px, 0, -1]) cylinder(d = clear_m3, h = frame_t + 2, $fn = fn_small);
        for (hx = [sv_hole_a, sv_hole_b]) translate([hx, 0, -6]) cylinder(d = pilot_m2, h = 10, $fn = fn_small);
    }
}

// =============================================================================
//  HEAD  (local: front face y=0, bottom z=0)
// =============================================================================
module hslice(w, h, r, inset = 0) { translate([0, h/2]) offset(-inset) rrect(w, h, r); }   // 2D in (x, z), bottom at z=0
module head_solid() {
    hull() {
        xz_extrude(0, eps) translate([0, head_chamfer]) hslice(head_w - 4 - 2*head_chamfer, head_h - 2 - 2*head_chamfer, head_r - head_chamfer);
        xz_extrude(head_chamfer, head_chamfer + eps) hslice(head_w - 4, head_h - 2, head_r);
        xz_extrude(60, 60 + eps) hslice(head_w, head_h, head_r);
        xz_extrude(head_d - eps, head_d) hslice(head_back_w, head_back_h, head_back_r);
    }
}
module head_cavity() {
    w = head_wall;
    intersection() {
        hull() {
            xz_extrude(w, w + eps) hslice(head_w - 4, head_h - 2, head_r, w);
            xz_extrude(60, 60 + eps) hslice(head_w, head_h, head_r, w);
            xz_extrude(head_d - w - eps, head_d - w) hslice(head_back_w, head_back_h, head_back_r, w);
        }
        translate([-200, -1, head_bottom_t]) cube([400, head_d + 2, 400]);
    }
}
function head_back_boss_pts() = [[-36, 18], [36, 18], [-36, 86], [36, 86]];   // (x, z) - inside the back face
function lcd_boss_pts() = [[lcd_x0 - 3.6, lcd_z0 + 18], [lcd_x0 - 3.6, lcd_z1 - 11], [lcd_x1 + 3.9, lcd_z0 + 11], [lcd_x1 + 3.9, lcd_z1 - 11]];
function visor_screw_pts() = [for (sx = [-1, 1]) for (sz = [-1, 1]) [sx * (visor_w/2 - 10), visor_z + sz * (visor_h/2 - 10)]];
pod_screw_pts = [[-20, pod_y0 + 6.2], [20, pod_y0 + 6.2]];   // front corners, clear of the camera bracket
cam_z = head_h + 18;  // lens centre height in the pod (head coordinates)

module head_shell() {
    difference() {
        union() {
            difference() { head_solid(); head_cavity(); }
            intersection() {
                head_solid();
                union() {
                    // back-cover bosses, each tied to the nearest wall (top or bottom) by a rib
                    // (45-degree front edge so they print face down without support)
                    for (p = head_back_boss_pts()) let (top = p[1] > head_h/2, dist = top ? head_h - p[1] : p[1]) hull() {
                        translate([p[0], 0, p[1]]) cyl_y(8, head_split - 14, head_split);
                        translate([p[0] - 4, head_split - 14 - dist, top ? head_h : 0]) cube([8, 14 + dist, eps]);
                    }
                    // ledge behind the visor seat (the seat is deeper than the front wall)
                    xz_extrude(0, visor_back_y + 2.5) translate([0, visor_z]) rrect(visor_w + 2*clr + 10, visor_h + 2*clr + 10, visor_r + 5);
                    // visor screw tabs (corners of the opening)
                    for (p = visor_screw_pts()) translate([p[0], 0, p[1]]) cyl_y(10, visor_back_y - eps, visor_back_y + 3);
                    // ear screw bosses
                    for (s = [-1, 1]) for (dz = [-ear_screw_dz, ear_screw_dz]) translate([s * (head_w/2 - 6), ear_y, ear_z + dz]) rotate([0, 90, 0])
                        linear_extrude(10, center = true) hull() { circle(d = 8); translate([0, -4 * 1.414]) square(eps, center = true); }
                }
            }
        }
        // back cap is a separate part
        translate([-200, head_split, -1]) cube([400, 200, 400]);
        // visor seat + opening
        xz_extrude(-1, visor_back_y) translate([0, visor_z]) rrect(visor_w + 2*clr, visor_h + 2*clr, visor_r);
        difference() {
            xz_extrude(-1, visor_back_y + 4) translate([0, visor_z]) rrect(108, 82, visor_r - 2);
            for (p = visor_screw_pts()) translate([p[0], 0, p[1]]) cyl_y(10, -2, visor_back_y + 5);
        }
        for (p = visor_screw_pts()) translate([p[0], 0, p[1]]) cyl_y(clear_m25, -1, visor_back_y + 5, fn_small);
        // ears: screws + LED wire hole
        for (s = [-1, 1]) {
            for (dz = [-ear_screw_dz, ear_screw_dz]) translate([s * (head_w/2 - 4), ear_y, ear_z + dz]) rotate([0, 90, 0]) cylinder(d = clear_m3, h = 20, center = true, $fn = fn_small);
            translate([s * (head_w/2 - 4), ear_y, ear_z - 15]) rotate([0, 90, 0]) cylinder(d = 6, h = 20, center = true, $fn = 20);   // LED ring wires
        }
        // camera pod: opening for the board + screws
        translate([0, cam_board_y + 9, head_h - 30]) linear_extrude(40) rrect(24, 14, 4);
        for (p = pod_screw_pts) translate([p[0], p[1], head_h - 20]) cylinder(d = clear_m3, h = 30, $fn = fn_small);
        // neck: cable bore (teardrop - prints face down) + screws
        translate([0, head_neck_y, -1]) linear_extrude(head_bottom_t + 4) hull() { circle(r = bore_r); translate([0, bore_r * 1.35]) circle(r = 0.5); }
        for (a = head_screw_angles) translate([0, head_neck_y, -1]) rotate(a) translate([head_screw_r, 0, 0]) cylinder(d = clear_m3, h = head_bottom_t + 4, $fn = fn_small);
        for (p = head_back_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m3, head_split - 13, head_split + 1, fn_small);
    }
}

module head_back() {
    difference() {
        union() {
            intersection() { difference() { head_solid(); head_cavity(); } translate([-200, head_split, -1]) cube([400, 200, 400]); }
            for (p = head_back_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(9, head_split, head_d - head_wall + eps);
        }
        for (p = head_back_boss_pts()) translate([p[0], 0, p[1]]) {
            cyl_y(clear_m3, head_split - 1, head_d + 1, fn_small);
            translate([0, head_d + eps, 0]) rotate([90, 0, 0]) cylinder(d1 = 6.4, d2 = clear_m3, h = 1.6, $fn = 24);
        }
        // round speaker-style dot grille on the back of the head (also venting)
        if (detail) for (ring = [0:3]) for (k = [0:max(1, 6 * ring) - 1])
            translate([0, head_d - 5, 58]) rotate([0, 360 * k / max(1, 6 * ring), 0]) translate([ring * 5, 0, 0]) rotate([-90, 0, 0]) cylinder(d = 2.5, h = 10, $fn = 10);
    }
}

module visor() {
    // local: front face y = -1 (proud), back face y = visor_back_y; in head coordinates
    difference() {
        union() {
            xz_extrude(visor_back_y - visor_t, visor_back_y) translate([0, visor_z]) rrect(visor_w, visor_h, visor_r);
            // display locating ribs + clamp bosses on the back
            xz_extrude(visor_back_y - eps, visor_back_y + 2) difference() {
                translate([(lcd_x0 + lcd_x1)/2, (lcd_z0 + lcd_z1)/2]) square([lcd_w + 2*clr + 2.4, lcd_h + 2*clr + 2.4], center = true);
                translate([(lcd_x0 + lcd_x1)/2, (lcd_z0 + lcd_z1)/2]) square([lcd_w + 2*clr, lcd_h + 2*clr], center = true);
                for (cx = [lcd_x0, lcd_x1]) for (cz = [lcd_z0, lcd_z1]) translate([cx, cz]) square(10, center = true);
            }
            for (p = lcd_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(7, visor_back_y - eps, visor_back_y + lcd_stack + 0.8);
        }
        hull() {
            xz_extrude(visor_back_y - visor_t - 1, visor_back_y + 1) translate([0, visor_z]) rrect(aa_w + 2*window_margin, aa_h + 2*window_margin, 2.5);
            xz_extrude(visor_back_y - visor_t - 1, visor_back_y - visor_t + eps) translate([0, visor_z]) rrect(aa_w + 2*window_margin + 5, aa_h + 2*window_margin + 5, 4);
        }
        for (p = lcd_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m25, visor_back_y, visor_back_y + lcd_stack + 2, fn_small);
        for (p = visor_screw_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m25, visor_back_y - visor_t + 1.2, visor_back_y + 1, fn_small);
    }
}

module lcd_tab() { difference() { hull() { cylinder(d = 7, h = 3); translate([5.5, 0, 0]) cylinder(d = 6, h = 3); }
    translate([0, 0, -1]) cylinder(d = clear_m25, h = 5, $fn = fn_small); translate([0, 0, 1.8]) cylinder(d = 5, h = 2, $fn = 24); } }
module lcd_tabs() { for (i = [0:3]) translate([i * 16, 0, 0]) lcd_tab(); }

module ear() {
    // in head coordinates for the +x side; the left ear is the same part mirrored in place.
    // The LED ring sits in a pocket on the inner side, behind a 1 mm skin: it glows through
    // white / natural / translucent filament and the electronics stay out of reach.
    x0 = head_w/2 - 20; x1 = head_w/2 + ear_proud;
    difference() {
        translate([x0, ear_y, ear_z]) rotate([0, 90, 0]) hull() {
            cylinder(d = ear_d, h = x1 - x0 - 2);
            cylinder(d = ear_d - 4, h = x1 - x0);
        }
        head_solid();
        translate([x0 - 1, ear_y, ear_z]) rotate([0, 90, 0]) difference() {
            cylinder(r = led_ring_r1, h = x1 - glow_skin - x0 + 1);
            translate([0, 0, -1]) cylinder(r = led_ring_r0, h = 50);
        }
        for (dz = [-ear_screw_dz, ear_screw_dz]) translate([x0 - 1, ear_y, ear_z + dz]) rotate([0, 90, 0]) cylinder(d = pilot_m3, h = x1 - x0 - 1, $fn = fn_small);
    }
}

module camera_pod() {
    // in head coordinates; hollow, open where it meets the head
    pw = 2.2;
    difference() {
        union() {
            difference() {
                translate([0, pod_y0, head_h - 12]) pod_box(pod_w, pod_d, pod_h + 12, 10);
                translate([0, pod_y0 + pw, head_h - 13]) pod_box(pod_w - 2*pw, pod_d - 2*pw, pod_h + 12 - pw + 1, 8);
            }
            // pivot pillars for the camera bracket + screw bosses
            for (s = [-1, 1]) translate([s * 23, cam_board_y + 5.5, cam_z - 6]) cylinder(d = 5, h = head_h + pod_h - cam_z + 6 - pw + eps);
            for (p = pod_screw_pts) translate([p[0], p[1], head_h - 6]) cylinder(d = 8, h = pod_h + 6 - pw + eps);
        }
        head_solid();
        // lens aperture (flared)
        translate([0, pod_y0 - 1, cam_z]) rotate([-90, 0, 0]) cylinder(d = cam_aperture, h = pw + 2);
        translate([0, pod_y0 - eps, cam_z]) rotate([-90, 0, 0]) cylinder(d1 = cam_aperture + 6, d2 = cam_aperture, h = pw * 0.8);
        for (s = [-1, 1]) translate([s * 23, cam_board_y + 5.5, cam_z]) rotate([0, 90, 0]) cylinder(d = pilot_m2, h = 8, center = true, $fn = 10);
        for (p = pod_screw_pts) translate([p[0], p[1], head_h - 7]) cylinder(d = pilot_m3, h = 12, $fn = fn_small);
    }
}
module pod_box(w, d, h, r) { hull() { translate([0, d/2, 0]) linear_extrude(h - r/2) rrect(w, d, r); translate([0, d/2, h - eps]) linear_extrude(eps) rrect(w - r, d - r, r/2); } }

module camera_bracket() {
    pt = 2.5;
    difference() {
        union() {
            translate([0, pt/2, 0]) cube([cam_board + 4, pt, cam_board + 4], center = true);
            for (s = [-1, 1]) translate([s * 19.25, 0, 0]) hull() {
                translate([0, pt / 2, 0]) cube([2.5, pt, 14], center = true);
                translate([0, 5, 0]) rotate([0, 90, 0]) cylinder(d = 10, h = 2.5, center = true);
                translate([0, 11, 0]) rotate([0, 90, 0]) cylinder(d = 10, h = 2.5, center = true);
            }
        }
        cube([17, 20, 17], center = true);
        for (sx = [-1, 1]) for (sz = [-1, 1]) translate([sx * cam_hole_sp/2, -1, sz * cam_hole_sp/2]) rotate([-90, 0, 0]) cylinder(d = 2.4, h = pt + 2, $fn = fn_small);
        for (s = [-1, 1]) translate([s * 19.25, 0, 0]) hull() for (yy = [5, 11]) translate([0, yy, 0]) rotate([0, 90, 0]) cylinder(d = 2.3, h = 6, center = true, $fn = fn_small);
    }
}

// =============================================================================
//  ARMS (left = proper part; right = its mirror image)
// =============================================================================
// local: x = "down" when hanging, y = forward, z = 0 inner face (towards the body)
module seg2d_capsule(p0, p1, r) { hull() { translate(p0) circle(r = r); translate(p1) circle(r = r); } }
module rounded_slab(t) { hull() { linear_extrude(eps) offset(-1.2) children(); translate([0, 0, 1.2]) linear_extrude(t - 3.2) children();
    translate([0, 0, t - eps]) linear_extrude(eps) offset(-2) children(); } }
elbow = [44, 0]; hand = [73, 20];
module arm_body() {
    rounded_slab(arm_t) circle(r = 17);                                       // shoulder cap
    rounded_slab(arm_t - 1) seg2d_capsule([0, 0], elbow, 11);                // upper arm
    rounded_slab(arm_t - 1) seg2d_capsule(elbow, hand - [5, 3], 10);         // forearm
    rounded_slab(arm_t + 1) translate(hand) scale([1, 1.1]) circle(r = 11.5); // mitten hand
}
module arm_v2() {
    difference() {
        arm_body();
        translate([0, 0, -eps]) horn_pocket(horn_plate_t + 0.6, 15);
        translate([0, 0, -1]) cylinder(d = 5.5, h = arm_t + 3);
        for (a = [0, 180]) for (r = [7.5, 10.5]) rotate(a) translate([r, 0, -1]) cylinder(d = 1.3, h = 6, $fn = 10);
    }
}
module arm_left() { arm_v2(); }
module arm_right() { mirror([0, 1, 0]) arm_v2(); }

// =============================================================================
//  Dummies (assembly + checks)
// =============================================================================
module servo_dummy() {
    color("#3a6fb0") {
        translate([-sv_shaft_off, -sv_w/2, -sv_spline_h]) cube([sv_l, sv_w, sv_case_h]);
        translate([sv_mid - sv_tab_l/2, -sv_w/2, -sv_spline_h + sv_tab_z]) cube([sv_tab_l, sv_w, sv_tab_t]);
        translate([0, 0, -sv_spline_h + sv_case_h - eps]) cylinder(d = sv_boss_d - 0.4, h = sv_boss_h - sv_case_h);
    }
    color("silver") translate([0, 0, -(sv_spline_h - sv_boss_h) - eps]) cylinder(d = 4.8, h = sv_spline_h - sv_boss_h);
}
module place_torso() { translate([0, torso_y0, torso_z0]) children(); }
module place_head()  { translate([0, head_y0, head_z0]) children(); }
module pi_dummy_t() {   // torso coordinates
    color("green") translate([-28, pi_spine_y + pi_standoff, pi_z_top - 85]) cube([56, 1.6, 85]);
    color("#555") translate([-22, pi_spine_y + pi_standoff + 1.6, pi_z_top - 68]) cube([43.5, 14.5, 64.5]);           // official Active Cooler (63.5 x 42.5 x 13.7)
    color("#999") translate([-27, pi_spine_y + pi_standoff + 1.6, pi_z_top - 85 - 2]) cube([53, 16, 21]);              // USB/Ethernet block
    color("#222") translate([-25, pi_spine_y + pi_standoff + 1.6, pi_z_top - 85 - 33]) cube([50, 14, 33]);             // USB plugs (down)
    for (px = [11.2, 25.8, 39.2]) color("#222") translate([28, pi_spine_y + pi_standoff + 1.6, pi_z_top - px - 4]) cube([22, 4, 8]); // USB-C + HDMI plugs
}
module electronics() {
    place_torso() {
        pi_dummy_t();
        color("#111") translate([-spk_w/2, spk_y, spk_z]) cube([spk_w, spk_d, spk_h]);
        color("#333") translate([mic_x0 + 0.3, mic_y0, mic_zc - mic_w/2]) cube([mic_l, mic_t, mic_w]);
        translate([0, pinion_yt, hs_spline_z]) servo_dummy();
        for (s = [-1, 1]) translate([s * as_spline_x, shoulder_yt, shoulder_zt]) rotate(s > 0 ? [90, 0, 90] : [-90, 0, 90]) servo_dummy();
    }
    place_head() {
        color("#0a0a0a") translate([lcd_x0, visor_back_y, lcd_z0]) cube([lcd_w, 5.7, lcd_h]);
        color("#1a5") translate([lcd_x0, visor_back_y + 5.7, lcd_z0 - pcb_overhang]) cube([75, 1.6, lcd_h + 2*pcb_overhang]);
        color("#222") translate([lcd_x0 + 32 - 10.5, visor_back_y + 7.32, lcd_z0 - pcb_overhang - 31]) cube([21, 12, 37]);
        color("#844") translate([-cam_board/2, cam_board_y, cam_z - cam_board/2]) cube([cam_board, 1.6, cam_board]);
    }
    color("#333") translate([-bat_w/2 + 1, bat_y1 - 1 - 96, base_z0 + base_floor_t]) cube([bat_w - 2, 96, 26]);     // power bank (future)
    color("#2a2") translate([-30, 40, base_z0 + base_floor_t + bat_h + 2.5]) cube([62.5, 25.4, 12]);                   // PCA9685 on the shelf
}

module place_arm(s, a = arm_neutral) {
    translate([s * arm_inner_x, torso_y0 + shoulder_yt, torso_z0 + shoulder_zt]) rotate([-a, 0, 0])
        if (s < 0) multmatrix([[0, 0, -1, 0], [0, -1, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]) arm_left();
        else       multmatrix([[0, 0, 1, 0], [0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]) arm_right();
}

module assembly(explode = 0) {
    color("#f2efe8") base_shell();
    color("#d9d4c8") translate([0, 0, -explode]) base_floor();
    color("#bbb") translate([0, 0, base_z0 + base_floor_t + bat_h - explode * 0.6]) shelf();
    for (s = [-1, 1]) color("#e8e4da") translate([s * explode * 0.4, 0, 0]) place_well_cover(s) well_cover();
    color("#2b2f36") translate([0, -explode * 0.4, 0]) place_front_plate() port_plate(front_w, front_h, "blank");
    color("#e8e4da") translate([0, explode * 0.4, 0]) place_rear_plate() port_plate(rear_w, rear_h, "rear_usb");
    color("#f7f4ee") translate([0, 0, explode * 0.4]) place_torso() torso_shell();
    color("#e9e5dc") translate([0, explode * 0.6, explode * 0.4]) place_torso() translate([-46 + clr, torso_d - torso_wall, 0]) torso_back();
    place_torso() {
        color("#7fb3d5") translate([0, 0, explode * 0.9]) translate([0, neck_yt, torso_h]) coupler();
        color("#e6b35a") translate([0, neck_yt, gear_z0]) ring_gear();
        color("#e6b35a") translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion();
        color("#aab") translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    }
    translate([0, 0, explode * 1.4]) {
        color("#f7f4ee") place_head() head_shell();
        color("#e9e5dc") translate([0, explode * 0.5, 0]) place_head() head_back();
        color("#151a22") translate([0, -explode * 0.4, 0]) place_head() visor();
        color("#9fd3f5") place_head() { ear(); mirror([1, 0, 0]) ear(); }
        color("#f7f4ee") translate([0, 0, explode * 0.3]) place_head() camera_pod();
        color("#aab") place_head() translate([0, cam_board_y - 2.5, cam_z]) camera_bracket();
    }
    for (s = [-1, 1]) color("#f2efe8") translate([s * explode * 0.5, 0, explode * 0.4]) place_arm(s);
    if (explode == 0) electronics();
}

// =============================================================================
//  Print layouts
// =============================================================================
module print_part(p) {
    if (p == "base")             translate([0, 0, base_top]) mirror([0, 0, 1]) base_shell();
    else if (p == "base_floor")  translate([0, 0, -base_z0]) base_floor(false);
    else if (p == "base_floor_mobile") translate([0, 0, -base_z0]) base_floor(true);
    else if (p == "shelf")       shelf();
    else if (p == "well_cover")  well_cover();
    else if (p == "front_blank") port_plate(front_w, front_h, "blank");
    else if (p == "front_sonar") port_plate(front_w, front_h, "sonar");
    else if (p == "rear_blank")  port_plate(rear_w, rear_h, "rear");
    else if (p == "rear_usb")    port_plate(rear_w, rear_h, "rear_usb");
    else if (p == "torso")       translate([0, 0, torso_h]) mirror([0, 0, 1]) torso_shell();
    else if (p == "torso_back")  translate([0, 0, torso_wall + 1.6]) rotate([-90, 0, 0]) torso_back();
    else if (p == "coupler")     translate([0, 0, bearing_h + coupler_h]) mirror([0, 0, 1]) coupler();
    else if (p == "ring_gear")   ring_gear();
    else if (p == "pinion")      pinion();
    else if (p == "servo_frame") translate([0, 0, frame_t]) mirror([0, 0, 1]) servo_frame();
    else if (p == "head")        rotate([90, 0, 0]) head_shell();
    else if (p == "head_back")   translate([0, 0, head_d]) rotate([-90, 0, 0]) head_back();
    else if (p == "visor")       translate([0, 0, -(visor_back_y - visor_t)]) rotate([90, 0, 0]) translate([0, 0, -visor_z]) visor();
    else if (p == "ear_right")   translate([0, 0, head_w/2 + ear_proud]) rotate([0, 90, 0]) translate([0, -ear_y, -ear_z]) ear();
    else if (p == "ear_left")    mirror([0, 1, 0]) print_part("ear_right");     // the head tapers, so the ears are mirror images
    else if (p == "camera_pod")  translate([0, 0, head_h + pod_h]) mirror([0, 0, 1]) camera_pod();
    else if (p == "camera")      rotate([90, 0, 0]) camera_bracket();
    else if (p == "lcd_tabs")    lcd_tabs();
    else if (p == "arm_left")    arm_left();      // inner (flat) face down
    else if (p == "arm_right")   arm_right();
}

section_cut = 0;
if (part == "assembly") assembly(0);
else if (part == "exploded") assembly(70);
else if (part == "section_side") difference() { assembly(0); translate([section_cut, -50, -10]) cube([300, 400, 500]); }
else if (part == "section_plan") difference() { assembly(0); translate([-200, -50, section_cut]) cube([400, 400, 500]); }
else print_part(part);
