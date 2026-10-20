// =============================================================================
//  LittleBot enclosure - parametric OpenSCAD model (all dimensions in mm)
// =============================================================================
//
//  Parts (choose with -D 'part="..."' or the Customizer):
//    body        body shell (print TOP DOWN - top face on the bed)
//    base        base plate with Pi 5 standoffs, speaker cradle, PCA9685 wall
//    head        head shell (print FACE DOWN - screen side on the bed)
//    head_back   head back cover (print flat, outside face down)
//    coupler     neck turntable (print with the flat top on the bed)
//    ring_gear   neck ring gear 36T, screws under the coupler
//    pinion      24T pinion for the head servo horn
//    arm         arm - print TWO (same part for left and right)
//    servo_frame head-servo mounting frame
//    camera      camera bracket (tilts + slides for focus distance)
//    lcd_tabs    four small clamp tabs that hold the display
//    assembly    everything in place with dummy electronics (preview only)
//    exploded    exploded assembly (preview only)
//
//  Coordinate system (assembly): x = robot's width (viewer's right is +x),
//  y = depth (front face of the body at y=0, back is +y), z = up (table z=0).
//
//  !! Values marked MEASURE come from datasheets / typical parts. Check your
//  !! actual parts with calipers before printing - see README.md.
// =============================================================================

part = "assembly";

/* [Quality] */
$fn = 64;
detail = true;         // false skips grille/vent holes and text (fast interference checks)
fn_small = 16;         // small holes

/* [Fit] */
clr = 0.3;             // general clearance for parts that slide together
pilot_m3 = 2.7;        // M3 self-tapping pilot hole (PLA/PETG)
clear_m3 = 3.4;        // M3 clearance hole
pilot_m25 = 2.2;       // M2.5 self-tapping pilot
clear_m25 = 2.8;
pilot_m2 = 1.8;        // servo tab screws (self-tapping M2 / M2.2)

/* [Body] */
body_w = 130;          // x
body_d = 126;          // y
body_h = 95;           // z (bottom of shell to top surface)
body_r = 18;           // vertical corner radius
body_chamfer = 5;      // top edge chamfer (45 deg - prints without support top-down)
wall = 2.4;
top_t = 3;
base_t = 3;

/* [Neck - geared turntable] */
neck_y = 55;           // neck axis position (y)
gear_m = 1.5;          // gear module
ring_teeth = 36;       // on the head side
pinion_teeth = 24;     // on the servo  -> head turns 24/36 = 0.67 x servo angle
gear_t = 6;            // gear thickness
gear_backlash = 0.15;  // tooth backlash
gear_cd_extra = 0.15;  // extra centre distance (printing tolerance)
gear_gap = 0.4;        // gap between ring gear and top plate (= allowed lift)
bore_r = 13;           // cable bore through the neck (dia 26)
spigot_r = 17.6;       // spigot outer radius (turns in the top plate hole)
coupler_r = 25;        // visible neck radius
coupler_h = 10;        // visible neck height
bearing_h = 0.8;       // raised bearing ring under the coupler (add PTFE tape)

/* [Head] */
head_w = 114;
head_d = 58;
head_r = 22;           // face corner radius
head_chamfer = 3;      // front edge chamfer (prints face-down without support)
head_wall = 2.5;
head_bottom_t = 3;
head_neck_from_front = 26; // neck axis distance from the head front face
// Space below the display for the HDMI plug. 34 fits a straight full-size HDMI
// plug. With a 90-degree HDMI adapter (plug pointing sideways) use ~16.
hdmi_clearance = 34;

/* [Display - MPI3508 (lcdwiki drawing 2025-08)] */
lcd_w = 85.51;         // LCD module outline, landscape width
lcd_h = 56.0;          // landscape height
lcd_stack = 7.32;      // front of glass to back of PCB
aa_w = 73.44;          // active area
aa_h = 48.96;
// The active area is NOT centred: the FPC end has a wider border. With the HDMI
// port pointing DOWN, the wide border is on the LEFT (viewed from the front)
// and the active-area centre sits this far right of the LCD outline centre.
// MEASURE: if your wide border is on the right, use -3.03.
lcd_aa_offset = 3.03;
window_margin = 1.0;   // black border shown around the active area
pcb_overhang = 2.3;    // PCB sticks out this far above and below the glass

/* [Camera - InnoMaker U20CAM-1080P] */
cam_board = 32;        // 32x32 board
cam_hole_sp = 28;      // MEASURE: M2 hole spacing (square)
cam_z = 64;            // lens centre height
cam_board_y = 23;      // board position behind the front face (lens length) - slots allow +-5
cam_aperture = 18;     // lens hole (flares outwards for a wide field of view)

/* [Speaker - Adafruit 3369 Mini External USB Stereo Speaker] */
spk_w = 84; spk_h = 43; spk_d = 32;

/* [Servos - MG90S] */
sv_l = 22.8;           // body length
sv_w = 12.2;           // body width
sv_tab_l = 32.2;       // MEASURE: length across the mounting tabs
sv_tab_t = 2.5;        // tab thickness
sv_tab_z = 15.9;       // MEASURE: servo bottom to underside of tabs
sv_case_h = 22.7;      // bottom to top of case
sv_boss_h = 28.5;      // bottom to top of the round gear boss
sv_spline_h = 32.0;    // MEASURE: bottom to top of the output spline
sv_shaft_off = 6.1;    // shaft centre from the near end of the body
sv_hole_sp = 27.6;     // MEASURE: tab hole spacing
sv_boss_d = 12.4;      // round boss around the shaft (+ the small bump beside it)

/* [Arms] */
shoulder_y = 62;
shoulder_z = 62;
arm_gap = 3;           // gap between arm and body side (constant: no pinch point)
arm_len = 36;          // pivot to tip-circle centre
arm_root_r = 12;
arm_tip_r = 8.5;
arm_t = 9;
horn_plate_t = 1.6;    // MEASURE: servo horn arm thickness
arm_neutral = 18;      // drawing angle only (calibration sets the real one)

// -----------------------------------------------------------------------------
// Derived values
// -----------------------------------------------------------------------------
eps = 0.01;
xi = body_w/2 - wall;               // interior half width
y_in0 = wall; y_in1 = body_d - wall; // interior y range
z_base_top = base_t;                 // top of base plate
z_top_in = body_h - top_t;           // underside of top plate

ring_rp = gear_m * ring_teeth / 2;
pin_rp = gear_m * pinion_teeth / 2;
gear_cd = ring_rp + pin_rp + gear_cd_extra;
pinion_y = neck_y + gear_cd;
gear_z1 = z_top_in - gear_gap;       // top of gears
gear_z0 = gear_z1 - gear_t;          // bottom of gears

// Head servo (vertical, under the pinion, body extending towards +x)
hs_spline_z = gear_z0 + 2.2;         // horn sits in a 2.2 mm pocket in the pinion
hs_bottom = hs_spline_z - sv_spline_h;
hs_tab_top = hs_bottom + sv_tab_z + sv_tab_t;
sv_mid = (sv_l/2 - sv_shaft_off);    // body centre relative to shaft
sv_hole_a = sv_mid - sv_hole_sp/2;   // tab holes relative to shaft
sv_hole_b = sv_mid + sv_hole_sp/2;
frame_t = 3;
frame_pillar_x = [-28, 30];

// Arm servos (horizontal, shaft pointing out of the side wall)
arm_inner_x = body_w/2 + arm_gap;                     // arm inner face
as_spline_x = arm_inner_x + horn_plate_t - 0.6;       // spline top (horn plate sits ~0.6 above it)
as_tab_top_x = as_spline_x - (sv_spline_h - sv_tab_z - sv_tab_t);
rib_t = 3;

// Display in head-local coordinates (head front face y=0, head bottom z=0)
lcd_z0 = head_bottom_t + hdmi_clearance;
lcd_z1 = lcd_z0 + lcd_h;
head_h = lcd_z1 + pcb_overhang + 8 + head_wall;     // room above the display for clamp bosses
lcd_x0 = -lcd_w/2 - lcd_aa_offset;                    // LCD outline left edge (AA centred on x=0)
lcd_x1 = lcd_x0 + lcd_w;
win_z = (lcd_z0 + lcd_z1) / 2;
head_y0 = neck_y - head_neck_from_front;              // head front (assembly y)
head_z0 = body_h + bearing_h + coupler_h;             // head bottom (assembly z)

echo(str("LittleBot: head height = ", head_h, " mm, total height = ", head_z0 + head_h, " mm"));
echo(str("LittleBot: gear centre distance = ", gear_cd, " mm, pinion at y = ", pinion_y));

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------
module rrect(w, d, r) {
    rr = min(r, w/2 - eps, d/2 - eps);
    offset(r = rr) square([w - 2*rr, d - 2*rr], center = true);
}

// Rounded box with a 45-degree chamfer on its TOP edge (z = h).
module chamfer_box(w, d, h, r, c) {
    hull() {
        linear_extrude(h - c) rrect(w, d, r);
        translate([0, 0, h - eps]) linear_extrude(eps) rrect(w - 2*c, d - 2*c, max(r - c, 1));
    }
}

// Extrude 2D children drawn in (x, z) along y from y0 to y1.
module xz_extrude(y0, y1) {
    translate([0, y1, 0]) rotate([90, 0, 0]) linear_extrude(y1 - y0) children();
}

// Hole along x through a wall, teardrop shaped (pointed top) so it prints
// without support when the axis is horizontal.
module teardrop_x(d, len) {
    rotate([0, 90, 0]) linear_extrude(len, center = true)
        hull() { circle(d = d, $fn = 32); translate([d/2 * 1.414, 0]) square(eps, center = true); }
}

module cyl_y(d, y0, y1, fn = $fn) { translate([0, y0, 0]) rotate([-90, 0, 0]) cylinder(d = d, h = y1 - y0, $fn = fn); }
module cyl_x(d, x0, x1, fn = $fn) { translate([x0, 0, 0]) rotate([0, 90, 0]) cylinder(d = d, h = x1 - x0, $fn = fn); }

// ---- involute spur gear (2D) ------------------------------------------------
function polar(r, a) = [r * cos(a), r * sin(a)];
function inv_deg(a) = (tan(a) - a * PI / 180) * 180 / PI;
function flank(r, rb, th, invpa) = th + invpa - inv_deg(acos(min(1, rb / r)));
function gear_pts(m, z, pa = 20, bl = 0.15, n = 7) =
    let (rp = m * z / 2, rb = rp * cos(pa), ra = rp + m, rf = rp - 1.25 * m,
         th = 90 / z - (bl / (2 * rp)) * 180 / PI,
         invpa = inv_deg(pa),
         r0 = max(rb, rf),
         rs = [for (i = [0:n]) r0 + (ra - r0) * i / n],
         fb = flank(r0, rb, th, invpa))
    [for (k = [0:z-1]) let (c = k * 360 / z) each concat(
        [polar(rf, c - fb)],
        [for (r = rs) polar(r, c - flank(r, rb, th, invpa))],
        [for (i = [n:-1:0]) polar(rs[i], c + flank(rs[i], rb, th, invpa))],
        [polar(rf, c + fb)])];

module gear2d(m, z) { polygon(gear_pts(m, z, bl = gear_backlash)); }

// Servo horn pocket (cross horn, or a single/double horn trimmed to r 14)
module horn_pocket(depth, arm_r = 14) {
    linear_extrude(depth) {
        circle(r = 4.4, $fn = 32);
        for (a = [0:90:270]) rotate(a) hull() {
            translate([0, 0]) square([0.1, 6.2], center = true);
            translate([arm_r - 2.4, 0]) circle(r = 2.4, $fn = 24);
        }
    }
}

// =============================================================================
//  BODY SHELL
// =============================================================================
module body_outer() { translate([0, body_d/2, 0]) chamfer_box(body_w, body_d, body_h, body_r, body_chamfer); }
module body_inner() {
    c_in = max(0.5, body_chamfer - wall * 0.41);
    translate([0, body_d/2, -1])
        chamfer_box(body_w - 2*wall, body_d - 2*wall, body_h - top_t + 1, body_r - wall, c_in);
}

// Corner positions for the base screws (inside the rounded corners)
function base_boss_pts() = let (ri = body_r - wall, cx = xi - ri, cy0 = y_in0 + ri, cy1 = y_in1 - ri, k = (ri - 4.6) * 0.7071)
    [[-cx - k, cy0 - k], [cx + k, cy0 - k], [-cx - k, cy1 + k], [cx + k, cy1 + k]];

module body_shell() {
    difference() {
        union() {
            difference() { body_outer(); body_inner(); }
            intersection() {   // everything that hangs inside is clipped to the outer skin
                body_outer();
                union() {
                    base_bosses();
                    gear_guard();
                    servo_frame_pillars();
                    arm_ribs();
                    camera_pillars();
                }
            }
        }
        // --- top plate ---
        translate([neck_y * 0, neck_y, z_top_in - 1]) cylinder(r = spigot_r + clr, h = top_t + 2);
        // --- front: speaker grille ---
        if (detail) for (gx = [-33:5.5:33]) for (gz = [10:5:40])
            let (ox = (round((gz - 10) / 5) % 2) * 2.75)
                if (abs(gx + ox) <= 34)
                    translate([gx + ox, -1, gz]) rotate([-90, 0, 0]) cylinder(d = 3, h = wall + 2, $fn = fn_small);
        // --- front: camera aperture (flared for a wide view, allows +-10 deg tilt) ---
        translate([0, 0, cam_z]) {
            rotate([-90, 0, 0]) translate([0, 0, -1]) cylinder(d = cam_aperture, h = wall + 2);
            rotate([-90, 0, 0]) translate([0, 0, -eps]) cylinder(d1 = cam_aperture + 2 * wall + 4, d2 = cam_aperture, h = wall * 0.75);
        }
        // --- sides: arm shaft holes ---
        for (s = [-1, 1]) translate([s * (body_w/2 - wall/2), shoulder_y, shoulder_z]) teardrop_x(sv_boss_d + 1.6, wall + 2);
        // --- right side: microphone grille ---
        if (detail) for (gy = [52:5:88]) for (gz = [10:5:27])
            translate([xi - 1, gy, gz]) rotate([0, 90, 0]) cylinder(d = 2.6, h = wall + 2, $fn = fn_small);
        // --- back: Pi USB-C power cable ---
        translate([pi_usbc_x(), y_in1 - 1, pi_port_z()]) rotate([-90, 0, 0]) linear_extrude(wall + 2) rrect(14, 9, 3);
        // --- back: servo power jack (8 mm panel jack) + servo power switch (6 mm toggle) ---
        translate([-42, y_in1 - 1, 74]) rotate([-90, 0, 0]) cylinder(d = 8.2, h = wall + 2);
        translate([-24, y_in1 - 1, 74]) rotate([-90, 0, 0]) cylinder(d = 6.3, h = wall + 2);
        if (detail) translate([-33, body_d - 0.6, 82]) rotate([90, 0, 180]) linear_extrude(1)
            text("SERVO 5V", size = 4.2, halign = "center", font = "DejaVu Sans:style=Bold");
        // --- back: cooling vents (2.5 mm slots: too narrow for little fingers) ---
        if (detail) for (vx = [2:6:50]) translate([vx, y_in1 - 1, 8]) cube([2.5, wall + 2, 22]);
        if (detail) for (vx = [-6:6:50]) translate([vx, y_in1 - 1, 76]) cube([2.5, wall + 2, 11]);
    }
}

// Bosses for the base-plate screws (attached to the corner walls)
module base_bosses() {
    for (p = base_boss_pts()) translate([p[0], p[1], 0]) difference() {
        translate([0, 0, z_base_top]) cylinder(d = 9, h = 13);
        translate([0, 0, z_base_top - 1]) cylinder(d = pilot_m3, h = 12, $fn = fn_small);
    }
}

// Wall around the neck gears, hanging from the top plate (keeps wires out of the mesh)
module gear_guard() {
    gz0 = gear_z0 - 1.6;
    translate([0, 0, gz0]) linear_extrude(z_top_in - gz0 + eps) difference() {
        hull() { translate([0, neck_y]) circle(r = ring_rp + gear_m + 3.6); translate([0, pinion_y]) circle(r = pin_rp + gear_m + 3.6); }
        hull() { translate([0, neck_y]) circle(r = ring_rp + gear_m + 2.0); translate([0, pinion_y]) circle(r = pin_rp + gear_m + 2.0); }
    }
}

// Pillars that carry the head-servo frame
module servo_frame_pillars() {
    zt = hs_tab_top - sv_tab_t;             // frame top = tab underside
    for (px = frame_pillar_x) translate([px, pinion_y, zt]) difference() {
        cylinder(d = 8, h = z_top_in - zt + eps);
        translate([0, 0, -1]) cylinder(d = pilot_m3, h = 9, $fn = fn_small);
    }
}

// Vertical ribs that hold the arm servos (tabs screw to the rib's inner face)
module arm_ribs() {
    ry0 = shoulder_y + sv_hole_a - 5; ry1 = shoulder_y + sv_hole_b + 5;
    for (s = [-1, 1]) difference() {
        translate([s > 0 ? as_tab_top_x : -as_tab_top_x - rib_t, ry0, shoulder_z - sv_w/2 - 7])
            cube([rib_t, ry1 - ry0, z_top_in - (shoulder_z - sv_w/2 - 7) + eps]);
        // servo body window (servo top section passes through towards the wall)
        translate([s * (as_tab_top_x + rib_t/2), shoulder_y + sv_mid, shoulder_z])
            cube([rib_t + 2, sv_l + 2*clr, sv_w + 2*clr], center = true);
        // tab screw pilots
        for (hy = [sv_hole_a, sv_hole_b]) translate([s * (as_tab_top_x + rib_t/2), shoulder_y + hy, shoulder_z])
            rotate([0, 90, 0]) cylinder(d = pilot_m2, h = rib_t + 2, center = true, $fn = fn_small);
    }
}

// Pillars for the camera bracket pivot
module camera_pillars() {
    for (s = [-1, 1]) translate([s * 24, cam_board_y + 5.5, cam_z - 9]) difference() {
        cylinder(d = 8, h = z_top_in - (cam_z - 9) + eps);
        translate([0, 0, 9]) rotate([0, 90, 0]) cylinder(d = pilot_m25, h = 10, center = true, $fn = fn_small);
    }
}

// =============================================================================
//  BASE PLATE  (Raspberry Pi 5, speaker, PCA9685, feet, ballast)
// =============================================================================
pi_x0 = -56;                 // Pi board left edge (edge D, SD card)
pi_y1 = 94.4;                // edge A (USB-C, micro-HDMI) faces the back
pi_standoff = 4;
function pi_usbc_x() = pi_x0 + 11.2;
function pi_port_z() = z_base_top + pi_standoff + 1.6 + 1.6;
function pi_holes() = [for (hx = [3.5, 61.5]) for (hy = [3.5, 52.5]) [pi_x0 + hx, pi_y1 - hy]];

pca_l = 62.5; pca_w = 25.4;  // Adafruit PCA9685 16-channel board
pca_holes = [[3.3, 3.3], [59.2, 3.3], [3.3, 22.1], [59.2, 22.1]]; // MEASURE (approx.)
pca_wall_x = -xi + 1.0;      // outer face of the PCA9685 mounting wall
pca_y0 = 40; pca_z0 = 25;

module base_plate() {
    difference() {
        union() {
            // plate (fits inside the shell opening)
            translate([0, body_d/2, 0]) linear_extrude(base_t) rrect(body_w - 2*wall - 2*clr, body_d - 2*wall - 2*clr, body_r - wall - clr);
            // Pi 5 standoffs
            for (p = pi_holes()) translate([p[0], p[1], base_t - eps]) cylinder(d = 6, h = pi_standoff + eps);
            // speaker cradle: side walls + rear stop
            for (s = [-1, 1]) translate([s * (spk_w/2 + 1.5), y_in0 + 1 + spk_d/2, base_t + 7]) difference() {
                cube([3, spk_d + 2, 14], center = true);
                translate([0, 0, 1]) cube([5, 5, 2.4], center = true);   // zip-tie slot
            }
            translate([-spk_w/2 + 10, y_in0 + 1 + spk_d + 0.6, base_t]) cube([spk_w - 20, 2.4, 3.5]);
            // ballast pockets (glue steel washers here to lower the centre of gravity)
            for (s = [-1, 1]) translate([s * (spk_w/2 + 10.5), y_in0 + 24, base_t]) linear_extrude(5) difference() {
                rrect(15, 18, 2); rrect(12, 15, 1);
            }
            // PCA9685 mounting wall (left side, board faces inwards)
            translate([pca_wall_x, pca_y0 - 4, base_t - eps]) cube([2.5, pca_l + 8, pca_z0 + pca_w + 2 - base_t]);
            for (h = pca_holes) translate([pca_wall_x + 2.5 - eps, pca_y0 + h[0], pca_z0 + h[1]])
                rotate([0, 90, 0]) rotate(180) linear_extrude(4) hull() { circle(d = 6, $fn = 24); translate([3.6, 0]) circle(d = 0.4, $fn = 8); }
        }
        // Pi standoff pilots
        for (p = pi_holes()) translate([p[0], p[1], 1]) cylinder(d = pilot_m25, h = 10, $fn = fn_small);
        // PCA9685 pilots
        for (h = pca_holes) translate([pca_wall_x - 1, pca_y0 + h[0], pca_z0 + h[1]]) rotate([0, 90, 0]) cylinder(d = pilot_m25, h = 10, $fn = fn_small);
        // countersunk screw holes into the shell's corner bosses
        for (p = base_boss_pts()) translate([p[0], p[1], 0]) {
            translate([0, 0, -1]) cylinder(d = clear_m3, h = base_t + 2, $fn = fn_small);
            translate([0, 0, -eps]) cylinder(d1 = 6.4, d2 = clear_m3, h = 1.6, $fn = 24);
        }
        // cooling slots under the Pi's active cooler (2.5 mm wide)
        for (vx = [-40:5:5]) translate([vx, 50, -1]) cube([2.5, 34, base_t + 2]);
        // rubber feet recesses (10 mm self-adhesive bumpers)
        for (p = base_boss_pts()) let (q = [p[0] * 0.86, body_d/2 + (p[1] - body_d/2) * 0.84])
            translate([q[0], q[1], -eps]) cylinder(d = 10.6, h = 1.2);
    }
}

// =============================================================================
//  NECK: coupler, ring gear, pinion, head-servo frame
// =============================================================================
head_screw_angles = [30, 140, 290];     // uneven spacing -> the head fits one way only; clear of the HDMI plug
ring_screw_angles = [80, 200, 320];
head_screw_r = 20.5;
ring_screw_r = (bore_r + spigot_r) / 2;

// Coupler: local z=0 at the body top surface
module coupler() {
    difference() {
        union() {
            // bearing ring
            difference() { cylinder(r = coupler_r - 0.5, h = bearing_h + eps); translate([0, 0, -1]) cylinder(r = coupler_r - 4, h = 3); }
            // visible neck, softly chamfered
            translate([0, 0, bearing_h]) hull() {
                cylinder(r = coupler_r - 1.2, h = eps);
                translate([0, 0, 1.2]) cylinder(r = coupler_r, h = coupler_h - 2.4);
                translate([0, 0, coupler_h - eps]) cylinder(r = coupler_r - 1.2, h = eps);
            }
            // spigot through the top plate
            translate([0, 0, -top_t - gear_gap]) cylinder(r = spigot_r, h = top_t + gear_gap + bearing_h + eps);
        }
        translate([0, 0, -20]) cylinder(r = bore_r, h = 50);
        for (a = head_screw_angles) rotate(a) translate([head_screw_r, 0, bearing_h + coupler_h - 9]) cylinder(d = pilot_m3, h = 10, $fn = fn_small);
        for (a = ring_screw_angles) rotate(a) translate([ring_screw_r, 0, -top_t - gear_gap - 1]) cylinder(d = pilot_m25 - 0.1, h = 11, $fn = fn_small);
        // orientation mark (faces the front at neutral)
        rotate(-90) translate([coupler_r - 0.6, 0, bearing_h + 3]) cylinder(d = 1.6, h = coupler_h, $fn = 12);
    }
}

module ring_gear() {
    difference() {
        linear_extrude(gear_t) gear2d(gear_m, ring_teeth);
        translate([0, 0, -1]) cylinder(r = bore_r, h = gear_t + 2);
        for (a = ring_screw_angles) rotate(a) translate([ring_screw_r, 0, 0]) {
            translate([0, 0, -1]) cylinder(d = clear_m25, h = gear_t + 2, $fn = fn_small);
            translate([0, 0, -eps]) cylinder(d = 5.2, h = 2.5, $fn = 24);    // counterbore (heads face down)
        }
    }
}

module pinion() {
    difference() {
        linear_extrude(gear_t) gear2d(gear_m, pinion_teeth);
        translate([0, 0, -eps]) horn_pocket(2.2);
        translate([0, 0, -1]) cylinder(d = 6, h = gear_t + 2);             // horn centre screw access
        for (a = [0, 180]) for (r = [7.5, 10.5]) rotate(a) translate([r, 0, 1]) cylinder(d = 1.3, h = gear_t, $fn = 10);
    }
}

// Head servo frame: tabs sit on it, it screws to the two pillars from below
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
//  ARM (same part for left and right; horn pocket on the inner face)
// =============================================================================
module arm_shape2d() { hull() { circle(r = arm_root_r); translate([arm_len, 0]) circle(r = arm_tip_r); } }

module arm() {
    // z=0 is the inner face (towards the body); the arm points along +x
    difference() {
        hull() {
            linear_extrude(eps) offset(-1.5) arm_shape2d();
            translate([0, 0, 0.6]) linear_extrude(eps) offset(-0.4) arm_shape2d();
            translate([0, 0, 1.5]) linear_extrude(arm_t - 3.5) arm_shape2d();
            translate([0, 0, arm_t - eps]) linear_extrude(eps) offset(-2) arm_shape2d();
        }
        translate([0, 0, -eps]) horn_pocket(horn_plate_t + 0.6, 15);
        translate([0, 0, -1]) cylinder(d = 5.5, h = arm_t + 2);             // centre screw access
        for (a = [0, 180]) for (r = [7.5, 10.5]) rotate(a) translate([r, 0, -1]) cylinder(d = 1.3, h = 6, $fn = 10);
    }
}

// =============================================================================
//  CAMERA BRACKET (pivots on two screws, slots allow focus-distance adjustment)
// =============================================================================
module camera_bracket() {
    // local: plate in the x-z plane, front face at y=0, centred on the lens
    pt = 2.5;
    difference() {
        union() {
            translate([0, pt/2, 0]) cube([cam_board + 4, pt, cam_board + 4], center = true);
            for (s = [-1, 1]) translate([s * 18.75, 0, 0]) hull() {         // pivot lugs
                translate([0, pt / 2, 0]) cube([2.5, pt, 14], center = true);
                translate([0, 5, 0]) rotate([0, 90, 0]) cylinder(d = 10, h = 2.5, center = true);
                translate([0, 11, 0]) rotate([0, 90, 0]) cylinder(d = 10, h = 2.5, center = true);
            }
        }
        cube([17, 20, 17], center = true);                                  // lens holder passes through
        for (sx = [-1, 1]) for (sz = [-1, 1]) translate([sx * cam_hole_sp/2, -1, sz * cam_hole_sp/2])
            rotate([-90, 0, 0]) cylinder(d = 2.4, h = pt + 2, $fn = fn_small);
        for (s = [-1, 1]) translate([s * 18.75, 0, 0]) hull() for (yy = [5, 11])   // pivot slots
            translate([0, yy, 0]) rotate([0, 90, 0]) cylinder(d = 2.9, h = 6, center = true, $fn = fn_small);
    }
}

// =============================================================================
//  HEAD
// =============================================================================
module head_outer2d() { translate([0, head_h / 2]) rrect(head_w, head_h, head_r); }

module head_solid() {
    hull() {
        xz_extrude(head_chamfer, head_d) head_outer2d();
        xz_extrude(0, eps) offset(-head_chamfer) head_outer2d();
    }
}

// LCD clamp boss positions (x, z) in head coordinates
function lcd_boss_pts() = [[lcd_x0 - 3.9, lcd_z0 + 18], [lcd_x0 - 3.9, lcd_z1 - 11],
                           [lcd_x1 + 3.9, lcd_z0 + 11], [lcd_x1 + 3.9, lcd_z1 - 11]];
function head_back_boss_pts() = [[-head_w/2 + 10, 10], [head_w/2 - 10, 10], [-head_w/2 + 10, head_h - 10], [head_w/2 - 10, head_h - 10]];
lcd_boss_h = lcd_stack + 0.8;          // lip sits 0.8 behind the PCB: use 1 mm foam tape

module head_shell() {
    hw = head_wall;
    difference() {
        union() {
            difference() {
                head_solid();
                // hollow (open at the back)
                xz_extrude(hw, head_d + 1) translate([0, head_bottom_t]) translate([0, (head_h - head_bottom_t - hw) / 2])
                    rrect(head_w - 2*hw, head_h - head_bottom_t - hw, head_r - hw);
            }
            intersection() {
                head_solid();
                union() {
                    // display locating ribs
                    xz_extrude(hw - eps, hw + 2) difference() {
                        translate([(lcd_x0 + lcd_x1)/2, (lcd_z0 + lcd_z1)/2]) square([lcd_w + 2*clr + 2.4, lcd_h + 2*clr + 2.4], center = true);
                        translate([(lcd_x0 + lcd_x1)/2, (lcd_z0 + lcd_z1)/2]) square([lcd_w + 2*clr, lcd_h + 2*clr], center = true);
                        for (cx = [lcd_x0, lcd_x1]) for (cz = [lcd_z0, lcd_z1]) translate([cx, cz]) square(10, center = true);
                    }
                    // display clamp bosses
                    for (p = lcd_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(7, hw - eps, hw + lcd_boss_h);
                    // back-cover bosses
                    for (p = head_back_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(8, head_d - 12, head_d - hw);
                }
            }
        }
        // screen window with a 45-degree bevel
        hull() {
            xz_extrude(-1, hw + 1) translate([0, win_z]) rrect(aa_w + 2*window_margin, aa_h + 2*window_margin, 2.5);
            xz_extrude(-1, eps) translate([0, win_z]) rrect(aa_w + 2*window_margin + 2*hw + 1, aa_h + 2*window_margin + 2*hw + 1, 4);
        }
        // LCD pocket clearance behind the bezel ribs is open; clamp boss pilots
        for (p = lcd_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m25, hw + 1, hw + lcd_boss_h + 1, fn_small);
        for (p = head_back_boss_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m3, head_d - 13, head_d + 1, fn_small);
        // neck: cable bore (teardrop: prints face-down without support) + screws
        translate([0, head_neck_from_front, -1]) linear_extrude(head_bottom_t + 4)
            hull() { circle(r = bore_r); translate([0, bore_r * 1.35]) circle(r = 0.5); }
        for (a = head_screw_angles) rotate([0, 0, 0]) translate([0, head_neck_from_front, 0]) rotate(a)
            translate([head_screw_r, 0, -1]) cylinder(d = clear_m3, h = head_bottom_t + 4, $fn = fn_small);
    }
}

module head_back() {
    hw = head_wall;
    // local: outside face at y = 0 .. inside y = -hw (drawn in head coords then moved)
    difference() {
        union() {
            xz_extrude(head_d - hw, head_d) translate([0, head_bottom_t + (head_h - head_bottom_t - hw)/2])
                rrect(head_w - 2*hw - 2*clr, head_h - head_bottom_t - hw - 2*clr, head_r - hw - clr);
            // outer lip covering the seam
            xz_extrude(head_d - eps, head_d + 1.2) offset(-1.2) head_outer2d();
        }
        for (p = head_back_boss_pts()) translate([p[0], 0, p[1]]) {
            cyl_y(clear_m3, head_d - hw - 1, head_d + 2, fn_small);
            translate([0, head_d + 1.2 + eps, 0]) rotate([90, 0, 0]) cylinder(d1 = 6.4, d2 = clear_m3, h = 1.6, $fn = 24);
        }
        // ventilation slots (2.5 mm)
        for (vx = [-30:6:30]) translate([vx, head_d - hw - 1, head_h - 32]) cube([2.5, hw + 4, 18]);
        for (vx = [-30:6:30]) translate([vx, head_d - hw - 1, 12]) cube([2.5, hw + 4, 14]);
    }
}

module lcd_tab() {
    // L-shaped clamp: sits on a boss, its lip presses the display edge (add 1 mm foam)
    difference() {
        hull() { cylinder(d = 7, h = 3); translate([5.5, 0, 0]) cylinder(d = 6, h = 3); }
        translate([0, 0, -1]) cylinder(d = clear_m25, h = 5, $fn = fn_small);
        translate([0, 0, 1.8]) cylinder(d = 5, h = 2, $fn = 24);
    }
}
module lcd_tabs() { for (i = [0:3]) translate([i * 16, 0, 0]) lcd_tab(); }

// =============================================================================
//  Dummy components (assembly / interference checks only)
// =============================================================================
module servo_dummy() {
    // shaft at origin, spline up (+z), body extends towards +x
    color("#3a6fb0") {
        translate([-sv_shaft_off, -sv_w/2, -sv_spline_h]) cube([sv_l, sv_w, sv_case_h]);
        translate([sv_mid - sv_tab_l/2, -sv_w/2, -sv_spline_h + sv_tab_z]) cube([sv_tab_l, sv_w, sv_tab_t]);
        translate([0, 0, -sv_spline_h + sv_case_h - eps]) cylinder(d = sv_boss_d - 0.4, h = sv_boss_h - sv_case_h);
    }
    color("silver") translate([0, 0, -(sv_spline_h - sv_boss_h) - eps]) cylinder(d = 4.8, h = sv_spline_h - sv_boss_h);
}

module pi_dummy() {
    color("green") translate([pi_x0, pi_y1 - 56, z_base_top + pi_standoff]) cube([85, 56, 1.6]);
    color("#999") translate([pi_x0 + 85 - 21, pi_y1 - 56 + 2, z_base_top + pi_standoff + 1.6]) cube([21 + 2, 53, 16]);   // USB/Ethernet
    color("#555") translate([pi_x0 + 20, pi_y1 - 46, z_base_top + pi_standoff + 1.6]) cube([40, 34, 12]);            // active cooler
    color("#222") translate([pi_usbc_x() - 4.5, pi_y1 - 7, z_base_top + pi_standoff + 1.6]) cube([9, 22, 3.2]);      // USB-C plug
    for (hx = [25.8, 39.2]) color("#222") translate([pi_x0 + hx - 4, pi_y1 - 5, z_base_top + pi_standoff + 1.6]) cube([8, 24, 4]);
}

module electronics() {
    pi_dummy();
    color("#111") translate([-spk_w/2, y_in0 + 1, z_base_top]) cube([spk_w, spk_d, spk_h]);                     // speaker
    color("#2a2") translate([pca_wall_x + 6.5, pca_y0, pca_z0]) cube([1.6, pca_l, pca_w]);                        // PCA9685
    color("#844") translate([-cam_board/2, cam_board_y, cam_z - cam_board/2]) cube([cam_board, 1.6, cam_board]);  // camera board
    color("#333") translate([0, cam_board_y + eps, cam_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - wall - 0.5);
    // head servo
    translate([0, pinion_y, hs_spline_z]) servo_dummy();
    // arm servos (shaft along +-x)
    for (s = [-1, 1]) translate([s * as_spline_x, shoulder_y, shoulder_z]) rotate(s > 0 ? [90, 0, 90] : [-90, 0, 90]) servo_dummy();
    // display in the head
    translate([0, head_y0, head_z0]) {
        color("#0a0a0a") translate([lcd_x0, head_wall, lcd_z0]) cube([lcd_w, 5.7, lcd_h]);
        color("#1a5") translate([lcd_x0, head_wall + 5.7, lcd_z0 - pcb_overhang]) cube([75, 1.6, lcd_h + 2*pcb_overhang]);
        color("#222") translate([lcd_x0 + 32 - 10.5, head_wall + 7.32, lcd_z0 - pcb_overhang - 31]) cube([21, 12, 31 + 6]);  // HDMI plug
    }
}

// =============================================================================
//  Assembly
// =============================================================================
module place_coupler()   { translate([0, neck_y, body_h]) children(); }
module place_ring()      { translate([0, neck_y, gear_z0]) children(); }
module place_pinion()    { translate([0, pinion_y, gear_z0]) rotate(7.5) children(); }
module place_frame()     { translate([0, pinion_y, hs_tab_top - sv_tab_t - frame_t]) children(); }
module place_head()      { translate([0, head_y0, head_z0]) children(); }
module place_camera()    { translate([0, cam_board_y - 2.5, cam_z]) children(); }
module place_arm(s)      {
    translate([s * arm_inner_x, shoulder_y, shoulder_z]) rotate([-arm_neutral, 0, 0])
        if (s > 0) rotate([0, 90, 0]) children(); else rotate([0, -90, 0]) rotate([0, 0, 180]) children();
}

module assembly(explode = 0) {
    color("#f4f1ea") body_shell();
    color("#cfc8b8") translate([0, 0, -explode * 1.2]) base_plate();
    color("#7fb3d5") translate([0, 0, explode * 0.6]) place_coupler() coupler();
    color("#e6b35a") translate([0, 0, -explode * 0.3]) place_ring() ring_gear();
    color("#e6b35a") translate([0, 0, -explode * 0.5]) place_pinion() pinion();
    color("#aab") translate([0, 0, -explode * 0.7]) place_frame() servo_frame();
    color("#f4f1ea") translate([0, 0, explode * 1.1]) place_head() head_shell();
    color("#dcd6c8") translate([0, explode * 0.8, explode * 1.1]) place_head() head_back();
    color("#7fb3d5") for (s = [-1, 1]) translate([s * explode * 0.5, 0, 0]) place_arm(s) arm();
    color("#aab") translate([0, -explode * 0.3, 0]) place_camera() camera_bracket();
    if (explode == 0) electronics();
}

// =============================================================================
//  Print layouts (each part in its print orientation, on z = 0)
// =============================================================================
module print_part(p) {
    if (p == "body")        translate([0, 0, body_h]) mirror([0, 0, 1]) body_shell();
    else if (p == "base")   base_plate();
    else if (p == "head")   rotate([90, 0, 0]) head_shell();                 // face on the bed
    else if (p == "head_back") translate([0, 0, head_d + 1.2]) rotate([-90, 0, 0]) head_back();
    else if (p == "coupler") translate([0, 0, bearing_h + coupler_h]) mirror([0, 0, 1]) coupler();
    else if (p == "ring_gear") ring_gear();
    else if (p == "pinion") pinion();
    else if (p == "arm")    translate([0, 0, arm_t]) mirror([0, 0, 1]) arm();      // outer face on the bed
    else if (p == "servo_frame") translate([0, 0, frame_t]) mirror([0, 0, 1]) servo_frame();
    else if (p == "camera") rotate([90, 0, 0]) camera_bracket();
    else if (p == "lcd_tabs") lcd_tabs();
}

section_cut = 0;     // for the section views
if (part == "assembly") assembly(0);
else if (part == "section_side") difference() { assembly(0); translate([section_cut, -50, -10]) cube([200, 300, 400]); }
else if (part == "section_plan") difference() { assembly(0); translate([-150, -50, section_cut]) cube([300, 300, 400]); }
else if (part == "section_front") difference() { assembly(0); translate([-150, section_cut - 300, -10]) cube([300, 300, 400]); }
else if (part == "exploded") assembly(60);
else print_part(part);
