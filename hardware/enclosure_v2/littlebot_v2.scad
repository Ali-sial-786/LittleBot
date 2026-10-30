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
//    screen_clamp  presses the screen into the visor, on the visor posts (pi_carrier: no longer used)
//    coupler, ring_gear, pinion, servo_frame         geared neck (same principle as v1)
//    torso       torso shell (print TOP DOWN)          torso_back rear cover (flat)
//    arm_left, arm_right                               (outer face down)
//    base        base shell (TOP DOWN)                 base_floor removable floor (flat, stationary)
//    base_floor_mobile  floor for the wheel phase      shelf      electronics shelf
//    well_cover  x2 wheel-well covers (stationary)
//    front_blank / front_sonar   front plate (light bar; HC-SR04)   rear_blank / rear_usb  rear plate
//    assembly, exploded, section_side, section_plan, charging, cables   (preview only)
//    hardware_list  echoes every screw and cable route (build.sh -> assembly_video/hardware.json)
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

/* [Screen - MPI3508, plugged straight onto the Pi's GPIO header (lcdwiki drawing 2025-08-18)] */
// Screen frame: x along the glass from its wide-border end (left with the HDMI socket pointing
// down), z up from the HDMI edge, depth from the glass front. The screen has NO mounting holes:
// the stack (screen + Pi) is held by the Pi's four M2.5 holes, see pi_carrier().
lcd_w = 85.51; lcd_h = 56.0;           // glass outline
lcd_glass_t = 5.70;                    // glass front -> front of the screen PCB (touch 1.2 + LCD 2.49 + 2.01)
lcd_stack = 7.32;                      // glass front -> back of the screen PCB
aa_w = 73.44; aa_h = 48.96;            // active area
lcd_aa_offset = 2.32;                  // MEASURE: AA centre right of the glass centre (borders 8.35 / 3.72 mm)
window_margin = 0.5;                   // window = AA + this each side (the black mask is about AA + 0.9)
lcd_gasket = 0.5;                      // 1 mm foam tape between visor and glass, compressed
lcd_ear = 2.3;                         // PCB tabs beyond both long edges at x 10..20.5 and 64.5..75
lcd_sock_h = 13.54;                    // MEASURE: screen's 2x13 GPIO socket below its PCB (20.86 - 7.32)
lcd_pin1 = [8.88, 51.64];              // socket pin 1 (inner row, 8.88 from the end, 1.82 + 2.54 from the edge)
lcd_hdmi_x = 33.0; lcd_hdmi_z = 1.70;  // full-size HDMI socket: centre / opening above the glass edge
lcd_jack_x = 53.68;                    // 3.5 mm audio jack, sticks out 1.79 below the glass edge

/* [Raspberry Pi 5 + official Active Cooler (Raspberry Pi drawings)] */
pi_l = 85; pi_wd = 56; pi_pcb_t = 1.4;
pi_hdr_h = 2.5;                        // plastic of the Pi's 40-pin header (pins reach 8.57)
pi_pin1 = [8.40, 51.24];               // header pin 1 (inner row) from the SD-card end / USB-C edge
pi_hole_pts = [[3.5, 3.5], [61.5, 3.5], [3.5, 52.5], [61.5, 52.5]];   // M2.5, 2.7 mm holes
cooler_h = 12.0;                       // MEASURE: top of the Active Cooler above the Pi PCB (about 11.5..12)
pi_standoff = 4;                       // Pi underside -> carrier plate

/* [HDMI link screen <-> Pi: micro-HDMI to HDMI "U" adapter for 3.5" screens on a Pi 4/5] */
hdmi_link = [26, 14];                  // keep-out below the stack: width along the edge, drop below the glass edge

/* [Camera - InnoMaker U20CAM-1080P] */
cam_board = 32;
cam_hole_sp = [26, 28.5];              // MEASURE: M2 hole spacing; the bracket holes are slots covering this range
pod_w = 56; pod_d = 40; pod_h = 40; pod_y0 = 6;
cam_board_y = 26;                      // board position from the head front (lens length)
cam_aperture = 17;

/* [Speaker - Adafruit 3369] */
spk_w = 84; spk_h = 43; spk_d = 32; spk_z = 41;   // MEASURE; spk_z = bottom of the speaker (torso coords)
spk_y = 6;                                        // speaker front, behind the chest grille
spk_grille_hw = 34; spk_grille_r = 15;            // pill-shaped grille: half width, end radius (the speaker's front is an oval grille)

/* [Microphone - Adafruit 3367 mini USB mic, 22.2 x 18.3 x 7.0] */
mic_l = 22.2; mic_w = 18.3; mic_t = 7.0; mic_body = 10.2;  // body = the part outside the USB plug
mic_x0 = -34; mic_zc = 100;            // closed end of the sleeve / centre height (torso coords)
mic_y0 = torso_wall + 1.5;             // mic face 1.5 mm behind the chest wall (leaves room for the socket)

/* [Servos - MG90S (TowerPro drawing: A 32.5, B 22.8, C 28.4, D 12.4, E 32.1, F 18.5)] */
sv_l = 22.8; sv_w = 12.4; sv_tab_l = 32.1; sv_tab_t = 2.5; sv_tab_z = 15.9;
sv_case_h = 22.7; sv_boss_h = 28.4; sv_spline_h = 32.5; sv_shaft_off = 6.1;
sv_hole_sp = 27.6; sv_boss_d = 12.4;   // MEASURE sv_hole_sp, sv_tab_z (F = 18.5 is the top of the tabs)

/* [PCA9685 - Adafruit 815 layout (most clones copy it)] */
pca_l = 62.23; pca_w = 25.4;
pca_holes = [[3.30, 3.30], [59.18, 3.30], [3.30, 22.35], [59.18, 22.35]];   // 2.5 mm holes, 55.88 x 19.05
pca_x0 = -pca_l/2; pca_y0 = 40;        // board corner on the shelf (base coordinates)
pca_y0_floor = 10;                     // ... on the stationary floor instead (since the Pi + UPS moved into the base)

/* [Pi 5 + Waveshare UPS HAT (E) in the base (changed 2026-10-30)] */
// The Pi sits on the UPS and is powered through its pogo pins. The screen is no longer plugged onto
// the Pi: an HDMI cable and 6 touch wires run up the neck. The stack lies on its side in the base,
// its port edge (the Pi's micro-HDMI, the UPS's switch, USB-A and USB-C) up, under the cable hole to
// the torso. Waveshare publishes only the 88 x 56 mm board: the MEASURE values come from photos.
ups_l = 88; ups_w = 56; ups_pcb_t = 1.6;
ups_plate = [90, 58]; ups_plate_t = 3; // MEASURE clear base plate: length, width (across the boards), thickness
ups_body = 46;                         // MEASURE clear plate (inner face) -> underside of the UPS board (cells + holders)
ups_pi_gap = 10;                       // MEASURE UPS board (top face) -> underside of the Pi (brass standoffs)
ups_ports = [[12.5, 9, 4.5], [28, 13.5, 7], [42, 9, 3.4]];   // MEASURE switch, USB-A, USB-C on the UPS: centre from the Pi's SD-card end, width, height
floor_low = 3; floor_low_t = 2.4;      // stationary floor: underside 3 mm above the table (inside the skirt), thickness
stk_x0 = -40;                          // the Pi's SD-card end (robot x); its USB / Ethernet end points to +x
stk_y0 = 50;                           // the clear plate's outer face = the mounting wall's inner face (robot y)
stk_zb = floor_low + floor_low_t + 2;  // lower edge of the clear plate (it stands on a 2 mm rail)
stk_ztop = stk_zb + (ups_plate[1] + ups_w) / 2;   // the boards' port edge (top)
mount_t = 4;                           // mounting wall (the stack hangs on it by its 4 bottom screws)
stk_ups_w0 = ups_plate_t + ups_body;   // UPS board, from the plate's outer face
stk_pi_w0 = stk_ups_w0 + ups_pcb_t + ups_pi_gap;  // Pi board (underside)
stk_pi_w1 = stk_pi_w0 + pi_pcb_t;      // the Pi's component side

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
// The screen (head coordinates). The pi_* values below place a Pi plugged onto it, as in the old
// layout (pi_carrier); since 2026-10-30 the Pi is in the base (stk_*), and the screen clamp sits where the carrier did.
glass_y = visor_back_y + lcd_gasket;   // glass front
pi_off = lcd_pin1 - pi_pin1;           // Pi corner (SD-card end, USB-C edge) in the screen frame
pi_x0 = lcd_x0 + pi_off[0];  pi_z0 = lcd_z0 + pi_off[1];
pi_top_y = glass_y + lcd_stack + lcd_sock_h + pi_hdr_h;   // component side of the Pi
pi_bot_y = pi_top_y + pi_pcb_t;
carrier_y = pi_bot_y + pi_standoff;  carrier_t = 3;
function pi_h(p) = [pi_x0 + p[0], pi_z0 + p[1]];          // Pi board point -> head (x, z)
function lcd_h_pt(p) = [lcd_x0 + p[0], lcd_z0 + p[1]];    // screen point -> head (x, z)

echo(str("LittleBot v2: total height ", head_z0 + head_h, " mm; widest ", 2 * (arm_inner_x + arm_t), " mm (arms) / ", base_w, " (base)"));
echo(str("LittleBot v2: screen PCB -> Pi PCB ", lcd_sock_h + pi_hdr_h, " mm; glass front -> Pi underside ", pi_bot_y - glass_y,
         " mm; Pi corner at head (", pi_x0, ", ", pi_z0, ")"));

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

function in_stadium(x, z, hw, r) = abs(x) <= hw - r ? abs(z) <= r : norm([abs(x) - (hw - r), z]) <= r;
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

// mobile = false: stationary floor for the Pi + UPS stack (base_floor_ups). mobile = true: the wheel
// phase's floor (wheel slots, motor + caster grids; not updated for the UPS yet).
module base_floor(mobile = false) { if (mobile) base_floor_v1(true); else base_floor_ups(); }
module base_floor_v1(mobile = false) {
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

// --- the Pi + UPS stack (robot coordinates): u along the boards from the Pi's SD-card end (+x), v across
// them from the port edge (down), w through the stack from the clear plate's outer face (+y) ---
function stk(u, v, w) = [stk_x0 + u, stk_y0 + w, stk_ztop - v];
module sbox(u0, u1, v0, v1, w0, w1, s = 0) { hbox(stk_x0 + u0, stk_x0 + u1, stk_y0 + w0, stk_y0 + w1, stk_ztop - v1, stk_ztop - v0, s); }
module pi_box(x0, x1, z0, z1, h0, h1, s = 0) { sbox(x0, x1, z0, z1, stk_pi_w1 + h0, stk_pi_w1 + h1, s); }   // the Pi's own frame (as pbox)
function stk_plate_v0() = -(ups_plate[1] - ups_w) / 2;
module ups_stack(s = 0) {
    pv0 = stk_plate_v0();
    color("#cfe3ea") sbox(-(ups_plate[0] - ups_l) / 2, ups_l - (ups_plate[0] - ups_l) / 2, pv0, ups_w - pv0, 0, ups_plate_t, s);   // clear plate
    color("#e8742c") sbox(2, ups_l - 2, 4, ups_w - 4, ups_plate_t, stk_ups_w0, s);                                   // cells + holders
    color("#c9a74a") for (p = pi_hole_pts) translate(stk(p[0], p[1], ups_plate_t)) rotate([-90, 0, 0]) cylinder(d = 5.8 - 2*s, h = stk_pi_w0 - ups_plate_t, $fn = 6);   // brass standoffs
    color("#2a5caa") sbox(0, ups_l, 0, ups_w, stk_ups_w0, stk_ups_w0 + ups_pcb_t, s);                                // UPS board
    color("#c0c0c8") for (pt = ups_ports) sbox(pt[0] - pt[1]/2, pt[0] + pt[1]/2, -0.8, 9, stk_ups_w0 + ups_pcb_t, stk_ups_w0 + ups_pcb_t + pt[2], s);
    color("#1a9a55") sbox(0, pi_l, 0, pi_wd, stk_pi_w0, stk_pi_w1, s);                                               // the Pi
    color("#222") pi_box(7.13, 57.93, 50.02, 55.0, 0, 8.57, s);                                                      // GPIO header
    color("#c0c0c8") {
        pi_box(69.5, pi_l + 3, 2, 54, 0, 15.8, s);                                                                   // USB-A + Ethernet
        for (x = [25.8, 39.2]) pi_box(x - 3.75, x + 3.75, -0.5, 7.5, 0, 3.4, s);                                     // micro-HDMI
        pi_box(11.2 - 4.5, 11.2 + 4.5, -0.5, 7.5, 0, 3.2, s);                                                        // USB-C (unused: power comes from the pogo pins)
    }
    color("#333") pi_box(-2.5, 12, 22.7, 33.7, -pi_pcb_t - 1.6, -pi_pcb_t, s);                                       // microSD card
    color("#8a8f98") { pi_box(6.5, 64, 9.0, 49, 0, 3, s); pi_box(7.0, 27.5, 9.0, 49, 0, cooler_h, s); pi_box(27.5, 64, 18, 49, 0, cooler_h, s); }   // Active Cooler
}
// what plugs into the stack: up through the cable hole (HDMI to the screen, USB-A to the screen's power,
// USB-C for charging, a finger on the switch), the Pi's USB-A plugs (camera, speaker, mic) and the jumper
// housings on the GPIO header (touch wires, PCA9685, the UPS's battery data)
module ups_plug(i, s = 0) {   // 0 micro-HDMI, 1 USB-A, 2 USB-C, 3 switch, 4 the Pi's USB-A plugs, 5 GPIO jumpers
    up = 45; u = stk_ups_w0 + ups_pcb_t;
    if (i == 0) sbox(25.8 - 5.5, 25.8 + 5.5, -up, -0.5, stk_pi_w1 + 1.7 - 3.5, stk_pi_w1 + 1.7 + 3.5, s);
    if (i == 1) sbox(ups_ports[1][0] - 8, ups_ports[1][0] + 8, -up, -0.8, u + 3.5 - 4.25, u + 3.5 + 4.25, s);
    if (i == 2) sbox(ups_ports[2][0] - 6.25, ups_ports[2][0] + 6.25, -up, -0.8, u + 1.7 - 3.75, u + 1.7 + 3.75, s);
    if (i == 3) sbox(ups_ports[0][0] - 4, ups_ports[0][0] + 4, -25, -0.8, u, u + 6, s);                      // a finger on the switch
    if (i == 4) for (z = [29.1, 47.0]) pi_box(pi_l + 3, pi_l + 3 + 25, z - 7, z + 7, 0, 15.8, s);   // 25 mm incl. cable bend
    if (i == 5) pi_box(7.13, 57.93, 49.6, 56, 2.5, 2.5 + 14 + 4, s);                               // Dupont housings on the header
}
module ups_plugs(s = 0) { for (i = [0:5]) ups_plug(i, s); }
module pca_floor(s = 0) { translate([pca_x0 + s, pca_y0_floor + s, floor_low + floor_low_t + pca_standoff + s]) cube([pca_l - 2*s, pca_w - 2*s, 12 - 2*s]); }
// Charging: a USB-C extension (male -> female). Its male end goes into the UPS's USB-C (on the UPS board, beside
// the Pi's second micro-HDMI port), never into the Pi's own USB-C (on the Pi, by the SD card: it stays
// empty). The cable loops up through the cable hole and back down behind the cooler to its female end, held
// behind the rear plate's slot: the charger plugs in there.
usbc_sock = [13, 7, 18];               // MEASURE the female end: width, height, length
charge_plug = [12.2, 6.8, 18];         // MEASURE the male end's moulding: along the port, across it, length
charge_cable_d = 4;
module usbc_socket(s = 0) { translate([rear_x - usbc_sock[0]/2 + s, base_d - base_wall - 0.5 - usbc_sock[2] + s, rear_z - usbc_sock[1]/2 + s]) cube(usbc_sock - [2*s, 2*s, 2*s]); }
function charge_port() = stk(ups_ports[2][0], -0.8, stk_ups_w0 + ups_pcb_t + ups_ports[2][2] / 2);   // mouth of the UPS's USB-C
function charge_path() = let (p = charge_port(), q = [rear_x, base_d - base_wall - 0.5 - usbc_sock[2], rear_z]) [
    p + [0, 0, charge_plug[2] + 6], p + [0, 0, 30], p + [3, 8, 35], p + [6, 14, 30], p + [8, 16.5, 20], p + [8, 16.5, 1],
    [q[0] - 2, q[1] - 6.4, p[2] - 2.2], [q[0] - 1, q[1] - 3.4, p[2] - 8.2], [q[0] - 0.5, q[1] - 3.4, q[2] + 6],
    [q[0], q[1] - 3.5, q[2] + 1.5], [q[0], q[1] - charge_cable_d/2 - 0.3, q[2]]];
module charge_cable(s = 0) {
    p = charge_port();
    translate(p + [-charge_plug[0]/2 + s, -charge_plug[1]/2 + s, s]) cube(charge_plug - [2*s, 2*s, 2*s]);   // male end
    translate(p + [0, 0, charge_plug[2] - eps]) cylinder(d = 5.5 - 2*s, h = 6, $fn = 16);                   // strain relief
    pts = charge_path();
    for (i = [0 : len(pts) - 2]) hull() for (q = [pts[i], pts[i + 1]]) translate(q) sphere(d = charge_cable_d - 2*s, $fn = 12);
}

// Stationary floor for the Pi + UPS stack. It sits lower than before (3 mm above the table, inside
// the skirt) to give the stack, on its side, room under the base's top. Columns carry it up to the
// base's corner pillars; the well-cover bosses are the same as before.
module base_floor_ups() {
    zf = floor_low; zt = floor_low + floor_low_t;
    wx0 = stk_x0 - (ups_plate[0] - ups_l) / 2 - 5; wx1 = wx0 + ups_plate[0] + 10;      // mounting wall
    wz1 = stk_ztop - stk_plate_v0();
    difference() {
        union() {
            translate([0, 0, zf]) linear_extrude(floor_low_t) offset(-base_wall - clr) shape2d(base_w, base_d, base_rf, base_rb);
            for (p = base_corner_pts()) translate([p[0], p[1], zf]) cylinder(d = 9, h = base_z0 + base_floor_t - zf);
            intersection() {
                translate([0, 0, zf]) linear_extrude(30) offset(-base_wall - clr) shape2d(base_w, base_d, base_rf, base_rb);
                for (s = [-1, 1]) for (u = [-1, 1]) translate([s * well_pillar_x, wheel_y + u * well_screw_u, zt - eps]) cylinder(d = 8, h = base_z0 + base_floor_t + 10 - zt);
            }
            // mounting wall with three gussets in front, and the rail the clear plate stands on
            translate([wx0, stk_y0 - mount_t, zt - eps]) cube([wx1 - wx0, mount_t, wz1 - zt + eps]);
            for (gx = [wx0 + 2, (wx0 + wx1) / 2 - 1.5, wx1 - 5]) translate([gx, stk_y0 - mount_t + eps, zt - eps]) rotate([90, 0, 90])
                linear_extrude(3) polygon([[0, 0], [-(stk_y0 - mount_t - (pca_y0_floor + pca_w + 2)), 0], [0, wz1 - zt - 8]]);
            translate([wx0 + 5, stk_y0 - eps, zt - eps]) cube([wx1 - wx0 - 10, ups_plate_t + 1, stk_zb - zt + eps]);
            // PCA9685 standoffs (front of the base)
            for (h = pca_holes) translate([pca_x0 + h[0], pca_y0_floor + h[1], zt - eps]) cylinder(d = 5.5, h = pca_standoff + eps);
            // charging socket holder: a cradle behind the rear plate's slot, on a post beside the GPIO wires
            difference() {
                union() {
                    translate([rear_x - usbc_sock[0]/2 - 2.5, base_d - base_wall - 0.5 - usbc_sock[2], rear_z - usbc_sock[1]/2 - 2.5]) cube([usbc_sock[0] + 5, usbc_sock[2], usbc_sock[1]/2 + 2.5]);
                    translate([rear_x + usbc_sock[0]/2 - 1.5, base_d - base_wall - 0.5 - usbc_sock[2], zt - eps]) cube([6.5, usbc_sock[2], rear_z - usbc_sock[1]/2 - zt + eps]);
                    // 45-degree support under the cradle's overhanging side (prints without supports)
                    let (x0 = rear_x - usbc_sock[0]/2 - 2.5, x1 = rear_x + usbc_sock[0]/2 - 1.5, z1 = rear_z - usbc_sock[1]/2 - 2.5)
                        translate([0, base_d - base_wall - 0.5, 0]) rotate([90, 0, 0]) linear_extrude(usbc_sock[2]) polygon([[x1, z1 - (x1 - x0)], [x1, z1 + eps], [x0, z1 + eps]]);
                }
                usbc_socket();
                translate([rear_x - usbc_sock[0]/2 - 3, base_d - base_wall - 0.5 - usbc_sock[2]/2 - 2, rear_z - usbc_sock[1]/2 - 2]) cube([usbc_sock[0] + 6, 4, 1.2]);   // zip-tie slot
            }
        }
        // corner screws: M3 x 12 from below, the head in a pocket
        for (p = base_corner_pts()) translate([p[0], p[1], zf - 1]) { cylinder(d = clear_m3, h = 20, $fn = fn_small); cylinder(d = 6.4, h = 1 + 4, $fn = 24); }
        for (s = [-1, 1]) for (u = [-1, 1]) translate([s * well_pillar_x, wheel_y + u * well_screw_u, well_low_z]) rotate([0, 90, 0]) cylinder(d = pilot_m3, h = 12, center = true, $fn = fn_small);
        // the stack's 4 bottom screws (M3, through the wall and the clear plate into the standoffs)
        for (p = pi_hole_pts) translate(stk(p[0], p[1], -mount_t - 1)) rotate([-90, 0, 0]) cylinder(d = clear_m3, h = mount_t + 2, $fn = fn_small);
        for (h = pca_holes) translate([pca_x0 + h[0], pca_y0_floor + h[1], zt + 0.8]) cylinder(d = pilot_m25, h = 10, $fn = fn_small);
        // vents beside the cooler's exhaust (the Pi's SD-card end), 2.5 mm slots
        if (detail) for (vy = [stk_y0 + 40 : 6 : stk_y0 + 80]) translate([stk_x0 - 28, vy, zf - 1]) cube([22, 2.5, floor_low_t + 2]);
    }
}

pca_standoff = 4;    // PCA9685 standoffs on the shelf (its solder joints clear the shelf)
module shelf() {
    sx = shelf_post_x + 5; y0 = bat_y0 + 1; y1 = bat_y1 - 1;
    difference() {
        union() {
            translate([0, (y0 + y1)/2, 0]) linear_extrude(2.5) rrect(2 * sx, y1 - y0, 4);
            for (h = pca_holes) translate([pca_x0 + h[0], pca_y0 + h[1], 2.5 - eps]) cylinder(d = 5.5, h = pca_standoff + eps);
        }
        for (s = [-1, 1]) for (yy = [bat_y0 + 6, bat_y1 - 6]) translate([s * shelf_post_x, yy, -1]) cylinder(d = clear_m3, h = 5, $fn = fn_small);
        // tie-down grid (zip ties / later boards), clear of the PCA9685 standoffs
        for (gx = [-25:10:25]) for (gy = [y0 + 9:10:y1 - 9]) if (gy < 88 && (gy < pca_y0 - 3 || gy > pca_y0 + pca_w + 3 || abs(gx) < 20))
            translate([gx, gy, -1]) cylinder(d = 2.5, h = 5, $fn = 10);
        // PCA9685 (Adafruit 815 hole pattern): M2.5 self-tapping into the standoffs
        for (h = pca_holes) translate([pca_x0 + h[0], pca_y0 + h[1], 0.8]) cylinder(d = pilot_m25, h = 10, $fn = fn_small);
        translate([0, torso_y0 + 81, -1]) linear_extrude(5) rrect(62, 34, 6);    // cable pass-through to the torso
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

// The Pi 5 lived here, then in the head, and since 2026-10-30 in the base (ups_stack). The two columns
// that used to carry it stay: they support the torso top under the neck.
torso_col_y = 60.75;

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
                    // two columns under the top plate (the neck sits between them)
                    for (s = [-1, 1]) translate([s * 32, torso_col_y, 10]) cylinder(d = 8, h = torso_top_in - 10 + eps);
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
        // arm shaft holes
        for (s = [-1, 1]) translate([s * (torso_w1/2 - torso_wall/2), shoulder_yt, shoulder_zt]) teardrop_x(sv_boss_d + 1.6, torso_wall + 2);
        // arm-servo tab pilots and windows are in torso_arm_ribs()
        // chest speaker grille: 3 mm holes in a pill shape over the speaker's oval front (both drivers)
        if (detail) for (row = [-3:3]) for (gx = [-35:5:35]) let (px = gx + (abs(row) % 2) * 2.5, pz = row * 4.4)
            if (in_stadium(px, pz, spk_grille_hw, spk_grille_r)) translate([px, -1, spk_z + spk_h/2 + pz]) rotate([-90, 0, 0]) cylinder(d = 3, h = 8, $fn = fn_small);
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
        // vent slots + a round "dot" grille
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
function visor_screw_pts() = [for (sx = [-1, 1]) for (sz = [-1, 1]) [sx * (visor_w/2 - 10), visor_z + sz * (visor_h/2 - 10)]];
// Posts on the back of the visor carrying the Pi carrier: inside the head's face opening, outside
// the stack, its plugs and the SD card. Two left of the screen, one beside the Pi's (unused)
// Ethernet jack, one above the top-right PCB tab.
visor_post_d = 7;
function visor_post_pts() = [[lcd_x0 - 4.4, visor_z - 5], [lcd_x0 - 4.4, visor_z + 10],
                             [pi_h([pi_l + 3, 0])[0] + 5.1, visor_z - 14], [12, visor_z + 35]];
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
        // vents for the Pi (2.5 mm slots, too narrow for little fingers): the cooler's fins blow towards
        // the Pi's SD-card end (viewer's left) -> slots in the left wall; fresh air in under the face
        if (detail) {
            for (zz = [lcd_z0 + 13 : 6 : lcd_z1 - 12]) translate([-head_w/2 - 10, glass_y + 9, zz - 1.25]) cube([20, 18, 2.5]);
            for (xx = [-15 : 5 : 15]) translate([xx - 1.25, glass_y + 9, -1]) cube([2.5, 16, head_bottom_t + 2]);
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
    // local: front face y = -1 (proud), back face y = visor_back_y; in head coordinates.
    // The screen has no mounting holes: its glass sits in the locating ribs (on 1 mm foam) and the
    // Pi carrier, screwed to the four posts, holds the whole screen + Pi stack against the visor.
    difference() {
        union() {
            xz_extrude(visor_back_y - visor_t, visor_back_y) translate([0, visor_z]) rrect(visor_w, visor_h, visor_r);
            // glass locating ribs (3 mm: they reach past the 1.2 mm touch panel onto the LCD glass)
            xz_extrude(visor_back_y - eps, visor_back_y + 3) difference() {
                translate([(lcd_x0 + lcd_x1)/2, (lcd_z0 + lcd_z1)/2]) square([lcd_w + 2*clr + 2.4, lcd_h + 2*clr + 2.4], center = true);
                translate([(lcd_x0 + lcd_x1)/2, (lcd_z0 + lcd_z1)/2]) square([lcd_w + 2*clr, lcd_h + 2*clr], center = true);
                for (cx = [lcd_x0, lcd_x1]) for (cz = [lcd_z0, lcd_z1]) translate([cx, cz]) square(10, center = true);
            }
            // posts for the Pi carrier
            for (p = visor_post_pts()) translate([p[0], 0, p[1]]) cyl_y(visor_post_d, visor_back_y - eps, carrier_y);
        }
        hull() {
            xz_extrude(visor_back_y - visor_t - 1, visor_back_y + 1) translate([0, visor_z]) rrect(aa_w + 2*window_margin, aa_h + 2*window_margin, 2.5);
            xz_extrude(visor_back_y - visor_t - 1, visor_back_y - visor_t + eps) translate([0, visor_z]) rrect(aa_w + 2*window_margin + 5, aa_h + 2*window_margin + 5, 4);
        }
        for (p = visor_post_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m25, carrier_y - 10, carrier_y + 1, fn_small);
        for (p = visor_screw_pts()) translate([p[0], 0, p[1]]) cyl_y(pilot_m25, visor_back_y - visor_t + 1.2, visor_back_y + 1, fn_small);
    }
}

// Pi carrier (no longer used since the Pi moved into the base; screen_clamp replaces it).
// A frame behind the Pi. The Pi screws onto its four standoffs (M2.5 from the
// component side, before the screen goes on); the frame screws onto the visor posts from behind
// (oversized holes, so the glass can settle in the visor ribs before tightening).
// Head coordinates; prints back face down.
module pi_carrier() {
    st = [for (p = pi_hole_pts) pi_h(p)];                    // standoffs (x, z)
    posts = visor_post_pts();
    bar = 8;
    module seg(a, b, w) { hull() { translate(a) circle(d = w); translate(b) circle(d = w); } }
    difference() {
        union() {
            translate([0, carrier_y + carrier_t, 0]) rotate([90, 0, 0]) linear_extrude(carrier_t) union() {
                seg(st[0], st[1], bar); seg(st[1], st[3], bar); seg(st[3], st[2], bar); seg(st[2], st[0], bar);
                seg(posts[0], [st[0][0], posts[0][1]], bar); seg(posts[1], [st[0][0], posts[1][1]], bar);
                seg(posts[2], [st[1][0], posts[2][1]], bar); seg(posts[3], st[3], bar);
                for (p = posts) translate(p) circle(d = visor_post_d + 2);
            }
            for (p = st) translate([p[0], 0, p[1]]) cyl_y(6.5, pi_bot_y, carrier_y + eps);
        }
        for (p = st) translate([p[0], 0, p[1]]) cyl_y(pilot_m25, pi_bot_y - 1, carrier_y + carrier_t + 1, fn_small);
        for (p = posts) translate([p[0], 0, p[1]]) cyl_y(clear_m25 + 0.6, carrier_y - 1, carrier_y + carrier_t + 1, fn_small);
    }
}

// Screen clamp (replaces the Pi carrier since the Pi moved into the base, 2026-10-30). It screws onto the
// visor's four posts and presses the screen's four PCB tabs (beyond its long edges) into the visor, so the
// screen needs no holes. The two lower fingers end in hooks under the lower tabs: the screen sits on the
// clamp as well as on the visor's lower rib, so it can't slide down. Head coordinates; prints back face down.
clamp_fw = 9.5;                        // finger width along the tab (the tabs are 10.5 wide)
module screen_clamp() {
    yb = glass_y + lcd_stack;          // back face of the screen PCB = the fingers' tips
    tx = [for (a = [[10, 20.5], [64.5, 75]]) lcd_x0 + (a[0] + a[1]) / 2];
    zt = lcd_z1 + 5.5; zb = lcd_z0 - 4.0;   // where the fingers meet the frame
    posts = visor_post_pts();
    bar = 7;
    module seg(a, b, w) { hull() { translate(a) circle(d = w); translate(b) circle(d = w); } }
    difference() {
        union() {
            translate([0, carrier_y + carrier_t, 0]) rotate([90, 0, 0]) linear_extrude(carrier_t) union() {
                seg(posts[0], posts[1], bar); seg(posts[0], [tx[0], zb], bar);
                seg(posts[1], [posts[1][0], zt + 1], bar); seg([posts[1][0], zt + 1], [tx[0], zt + 1], bar);   // around the touch wires (pin 1)
                seg(posts[2], [tx[1], zb], bar); seg(posts[3], [tx[1], zt], bar); seg(posts[2], posts[3], bar); seg([tx[0], zt + 1], [tx[1], zt + 1], bar);
                for (pp = posts) translate(pp) circle(d = visor_post_d + 2);
                for (x = tx) { translate([x - clamp_fw/2, lcd_z1 + 0.2]) square([clamp_fw, zt + 3 - lcd_z1 - 0.2]); translate([x - clamp_fw/2, zb - 3]) square([clamp_fw, lcd_z0 - 0.2 - (zb - 3)]); }
            }
            for (x = tx) {
                // upper fingers: press on the upper tabs only (z above the PCB's edge)
                translate([x - clamp_fw/2, yb, lcd_z1 + 0.2]) cube([clamp_fw, carrier_y - yb + eps, zt + 3 - lcd_z1 - 0.2]);
                // lower fingers: press on the lower tabs, with a hook under them
                translate([x - clamp_fw/2, yb, zb - 3]) cube([clamp_fw, carrier_y - yb + eps, lcd_z0 - 0.2 - (zb - 3)]);
                translate([x - clamp_fw/2, glass_y + lcd_glass_t + 0.2, zb - 3]) cube([clamp_fw, yb - (glass_y + lcd_glass_t + 0.2) + eps, lcd_z0 - lcd_ear - 0.3 - (zb - 3)]);
            }
        }
        for (pp = posts) translate([pp[0], 0, pp[1]]) cyl_y(clear_m25 + 0.6, carrier_y - 1, carrier_y + carrier_t + 1, fn_small);
    }
}
// cables plugged into the screen (head coordinates): a right-angle HDMI plug under its HDMI socket (the cable
// leaves backwards), a straight micro-USB power plug at its wide-border end, and the touch wires' Dupont
// plugs in socket pins 19-26 (and pin 1, if needed), the wires going down to the neck
module screen_plugs(s = 0) {
    lbox(lcd_hdmi_x - 11, lcd_hdmi_x + 11, -18, 1.7, lcd_stack - 1.9, lcd_stack + 10, s);                    // right-angle HDMI plug
    lbox(lcd_hdmi_x - 4, lcd_hdmi_x + 4, -14, -6, lcd_stack + 10, lcd_stack + 45, s);                         // its cable, backwards
    lbox(-20, 0.4, 10.1 - 5.5, 10.1 + 5.5, lcd_stack + 1.7 - 3.75, lcd_stack + 1.7 + 3.75, s);                 // micro-USB plug (power)
    lbox(8.88 + 9 * 2.54 - 1.6, 8.88 + 12 * 2.54 + 1.6, lcd_pin1[1] - 1.6, lcd_pin1[1] + 2.54 + 1.6, lcd_stack + lcd_sock_h, lcd_stack + lcd_sock_h + 18, s);   // pins 19-26
    lbox(8.88 - 1.6, 8.88 + 1.6, lcd_pin1[1] - 1.6, lcd_pin1[1] + 1.6, lcd_stack + lcd_sock_h, lcd_stack + lcd_sock_h + 18, s);                         // pin 1
    lbox(8.88 + 9 * 2.54 - 1.6, 8.88 + 12 * 2.54 + 1.6, -10, lcd_pin1[1], lcd_stack + lcd_sock_h + 12, lcd_stack + lcd_sock_h + 18, s);                 // the wires, down
}

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
        // lens holder (14 mm block, screw ears 18 mm apart) and the 5-pin cable connector below it
        cube([17, 20, 17], center = true);
        cube([25, 20, 7], center = true);
        translate([-6, -1, -cam_board/2 - 3]) cube([16, pt + 2, cam_board/2 + 3 - 7]);
        // board screws: diagonal slots for 26..28.5 mm hole spacing
        for (sx = [-1, 1]) for (sz = [-1, 1]) hull() for (sp = cam_hole_sp) translate([sx * sp/2, -1, sz * sp/2]) rotate([-90, 0, 0]) cylinder(d = 2.4, h = pt + 2, $fn = fn_small);
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

// --- the screen (and, for the old layout, the Pi plugged onto it) in head coordinates, from the lcdwiki and Raspberry Pi drawings ---
// s shrinks every box (the interference checks allow touching faces).
module hbox(x0, x1, y0, y1, z0, z1, s = 0) { translate([x0 + s, y0 + s, z0 + s]) cube([x1 - x0 - 2*s, y1 - y0 - 2*s, z1 - z0 - 2*s]); }
// box in the screen frame: x from the wide-border end, z from the HDMI edge, y depth from the glass front
module lbox(x0, x1, z0, z1, y0, y1, s = 0) { hbox(lcd_x0 + x0, lcd_x0 + x1, glass_y + y0, glass_y + y1, lcd_z0 + z0, lcd_z0 + z1, s); }
// box in the Pi frame: x from the SD-card end, z from the USB-C edge, h = height above the component side (towards the screen)
module pbox(x0, x1, z0, z1, h0, h1, s = 0) { hbox(pi_x0 + x0, pi_x0 + x1, pi_top_y - h1, pi_top_y - h0, pi_z0 + z0, pi_z0 + z1, s); }

module screen_dummy(s = 0) {
    lbox(0, lcd_w, 0, lcd_h, 0, lcd_glass_t, s);                                         // glass (touch + LCD + backlight)
    lbox(0.4, lcd_w, 0, lcd_h, lcd_glass_t, lcd_stack, s);                              // screen PCB
    for (x = [[10, 20.5], [64.5, 75]]) for (z = [[-lcd_ear, 0], [lcd_h, lcd_h + lcd_ear]]) lbox(x[0], x[1], z[0], z[1], lcd_glass_t, lcd_stack, s);
    // parts on the back of the screen PCB
    lbox(lcd_pin1[0] - 1.27, lcd_pin1[0] - 1.27 + 33.02, lcd_pin1[1] - 1.27, lcd_pin1[1] + 3.81, lcd_stack, lcd_stack + lcd_sock_h, s);  // 2x13 socket
    lbox(lcd_hdmi_x - 7.5, lcd_hdmi_x + 7.5, lcd_hdmi_z, 10.65, lcd_stack, lcd_stack + 6.7, s);            // HDMI socket
    lbox(lcd_jack_x - 3.45, lcd_jack_x + 3.45, -1.79, 12.2, lcd_stack, lcd_stack + 5.1, s);                // 3.5 mm jack
    lbox(0.4, 6.1, 6.1, 14.1, lcd_stack, lcd_stack + 3.4, s);                                              // micro-USB (power in)
    lbox(14, 58, 13, 47, lcd_stack, lcd_stack + 1.8, s);                                                   // scaler IC and small parts
}
module pi_dummy(s = 0) {
    pbox(0, pi_l, 0, pi_wd, -pi_pcb_t, 0, s);                                            // board
    pbox(7.13, 57.93, 50.02, 55.0, 0, pi_hdr_h, s);                                      // header plastic
    pbox(40.15, 57.93, 50.02, 55.0, pi_hdr_h, 8.57, s);                                  // pins 27..40 (not covered by the screen socket)
    pbox(69.5, pi_l + 3, 2, 54, 0, 15.8, s);                                             // 2 x USB-A + Ethernet
    for (x = [25.8, 39.2]) pbox(x - 3.75, x + 3.75, -0.5, 7.5, 0, 3.4, s);               // micro-HDMI
    pbox(11.2 - 4.5, 11.2 + 4.5, -0.5, 7.5, 0, 3.2, s);                                  // USB-C
    pbox(-2.5, 12, 22.7, 33.7, -pi_pcb_t - 1.6, -pi_pcb_t, s);                           // microSD card (underside)
}
module cooler_dummy(s = 0) {   // official Active Cooler: push pins in the Pi's two 3 mm holes
    pbox(6.5, 64, 9.0, 49, 0, 3, s);                                                     // heatsink base (edge 2.5 mm in from the outline)
    pbox(0.5, 6.5, 6.5, 49, 0, 3, s);                                                    // push-pin boss corner
    pbox(7.0, 27.5, 9.0, 49, 0, cooler_h, s);                                            // fin block
    pbox(27.5, 64, 18, 49, 0, cooler_h, s);                                              // blower fan + right end
    for (p = [[3.5, 9.5], [61.5, 46.5]]) translate([pi_h(p)[0], 0, pi_h(p)[1]]) cyl_y(3 - 2*s, pi_bot_y, pi_bot_y + 1.0 - s, 12);  // pin tails
}
// cables plugged into the stack: USB-A (camera, speaker, mic), USB-C power, HDMI adapter keep-out
module stack_plugs(s = 0) {
    for (z = [29.1, 47.0]) pbox(pi_l + 3, pi_l + 3 + 25, z - 7, z + 7, 0, 15.8, s);     // USB-A plugs, 25 mm incl. cable bend
    pbox(11.2 - 6.25, 11.2 + 6.25, -31, -0.5, 1.6 - 3.75, 1.6 + 3.75, s);                // USB-C plug, 30 mm
    hdmi_zone(s);
}
module hdmi_zone(s = 0) {   // micro-HDMI -> HDMI U adapter (about 35 x 20 x 10) between the Pi's HDMI0 and the screen
    hbox(pi_h([20.5, 0])[0], pi_h([20.5 + hdmi_link[0], 0])[0], glass_y + 5, pi_top_y, lcd_z0 - hdmi_link[1], pi_z0 - 0.6, s);
}
module pi_screws() {   // M2.5 pan heads on the component side + shafts into the carrier standoffs
    for (p = pi_hole_pts) translate([pi_h(p)[0], 0, pi_h(p)[1]]) { cyl_y(4.6, pi_top_y - 1.8, pi_top_y, 16); cyl_y(2.5, pi_top_y, carrier_y + carrier_t - 0.5, 12); }
}
module stack_assembly() {
    color("#0a0a0a") screen_dummy();
    color("#1a5") pi_dummy();
    color("#777") cooler_dummy();
    color("#222") stack_plugs();
}

module electronics() {
    place_torso() {
        color("#111") translate([-spk_w/2, spk_y, spk_z]) cube([spk_w, spk_d, spk_h]);
        color("#333") translate([mic_x0 + 0.3, mic_y0, mic_zc - mic_w/2]) cube([mic_l, mic_t, mic_w]);
        translate([0, pinion_yt, hs_spline_z]) servo_dummy();
        for (s = [-1, 1]) translate([s * as_spline_x, shoulder_yt, shoulder_zt]) rotate(s > 0 ? [90, 0, 90] : [-90, 0, 90]) servo_dummy();
    }
    place_head() {
        color("#0a0a0a") screen_dummy();
        color("#844") translate([-cam_board/2, cam_board_y, cam_z - cam_board/2]) cube([cam_board, 1.6, cam_board]);
    }
    ups_stack();                                                // Pi 5 on the UPS HAT (E), on its side in the base
    color("#2a2") pca_floor();                                  // PCA9685 at the front of the floor
    color("#333") usbc_socket();                                // charging socket behind the rear plate
    color("#333") charge_cable();                               // ... and its cable, into the UPS's USB-C
}

module place_arm(s, a = arm_neutral) {
    translate([s * arm_inner_x, torso_y0 + shoulder_yt, torso_z0 + shoulder_zt]) rotate([-a, 0, 0])
        if (s < 0) multmatrix([[0, 0, -1, 0], [0, -1, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]) arm_left();
        else       multmatrix([[0, 0, 1, 0], [0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]) arm_right();
}

module assembly(explode = 0) {
    color("#f2efe8") base_shell();
    color("#d9d4c8") translate([0, 0, -explode]) base_floor();
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
        color("#aab") translate([0, explode * 0.35, 0]) place_head() screen_clamp();
        color("#9fd3f5") place_head() { ear(); mirror([1, 0, 0]) ear(); }
        color("#f7f4ee") translate([0, 0, explode * 0.3]) place_head() camera_pod();
        color("#aab") place_head() translate([0, cam_board_y - 2.5, cam_z]) camera_bracket();
    }
    for (s = [-1, 1]) color("#f2efe8") translate([s * explode * 0.5, 0, explode * 0.4]) place_arm(s);
    if (explode == 0) electronics();
}

// =============================================================================
//  Screws and cables, as assembled (robot coordinates): the assembly video draws them (build.sh exports
//  them to assembly_video/hardware.json), checks.scad runs every cable route against the parts, and the
//  "cables" preview shows them inside the see-through shells.
// =============================================================================
function torso_pt(p) = p + [0, torso_y0, torso_z0];
function head_pt(p) = p + [0, head_y0, head_z0];
function rot_x(a) = [[1, 0, 0], [0, cos(a), -sin(a)], [0, sin(a), cos(a)]];
function arm_m(s) = s < 0 ? [[0, 0, -1], [0, -1, 0], [-1, 0, 0]] : [[0, 0, 1], [0, 1, 0], [-1, 0, 0]];
function arm_dir(s, v) = rot_x(-arm_neutral) * (arm_m(s) * v);            // as place_arm()
function arm_pt(s, p) = [s * arm_inner_x, torso_y0 + shoulder_yt, torso_z0 + shoulder_zt] + arm_dir(s, p);

// [step, what, d, length, head ("pan" / "csk"), where the head bears (pan) or the top of the countersink, the
// direction it goes in]. The steps are the video's (assembly order in the README).
function hw_screws() = concat(
    [for (a = ring_screw_angles) ["ring", "M2.5 × 12", 2.5, 12, "pan", torso_pt([ring_screw_r * cos(a), neck_yt + ring_screw_r * sin(a), gear_z0 + 2.5]), [0, 0, 1]]],
    [for (a = [0, 180]) ["head_servo", "horn screws", 2, 5, "pan", torso_pt([7.5 * cos(a + 7.5), pinion_yt + 7.5 * sin(a + 7.5), gear_z0 + 0.6]), [0, 0, 1]]],
    [for (px = frame_pillar_x) ["head_servo", "M3 × 10", 3, 10, "pan", torso_pt([px, pinion_yt, hs_tab_top - sv_tab_t - frame_t]), [0, 0, 1]]],
    [for (hx = [sv_hole_a, sv_hole_b]) ["head_servo", "servo screws", 2, 8, "pan", torso_pt([hx, pinion_yt, hs_tab_top]), [0, 0, -1]]],
    [for (s = [-1, 1]) for (hy = [sv_hole_a, sv_hole_b]) ["arm_servos", "servo screws", 2, 8, "pan", torso_pt([s * (as_tab_top_x - sv_tab_t), shoulder_yt + hy, shoulder_zt]), [s, 0, 0]]],
    [for (p = pi_hole_pts) ["stack", "M2.5 (UPS kit)", 2.5, 6, "pan", stk(p[0], p[1], stk_pi_w1), [0, -1, 0]]],
    [for (p = pi_hole_pts) ["stack", "M3 × 12", 3, 12, "pan", stk(p[0], p[1], -mount_t), [0, 1, 0]]],
    [for (h = pca_holes) ["pca", "M2.5 × 6", 2.5, 6, "pan", [pca_x0 + h[0], pca_y0_floor + h[1], floor_low + floor_low_t + pca_standoff + 1.6], [0, 0, -1]]],
    [for (p = torso_fix_pts()) ["torso_on", "M3 × 10", 3, 10, "pan", [p[0], torso_y0 + p[1], base_top - base_top_t - 5], [0, 0, 1]]],
    [for (p = base_corner_pts()) ["floor_in", "M3 × 12", 3, 12, "pan", [p[0], p[1], floor_low + 4], [0, 0, 1]]],
    [for (s = [-1, 1]) for (u = [-1, 1]) for (zz = [well_low_z, well_screw_z]) ["covers", "M3 × 10 countersunk", 3, 10, "csk", [s * base_w/2, wheel_y + u * well_screw_u, zz], [-s, 0, 0]]],
    [for (p = front_screw_pts()) ["covers", "M3 × 10 countersunk", 3, 10, "csk", [p[0], -bezel_t, p[1]], [0, 1, 0]]],
    [for (p = rear_screw_pts()) ["covers", "M3 × 10 countersunk", 3, 10, "csk", [p[0], base_d + bezel_t, p[1]], [0, -1, 0]]],
    // the back cover fits lip outwards (inwards the lip would hit the screw columns), so its countersinks
    // face in: pan heads, through the lip
    [for (s = [-1, 1]) for (zz = [22, 100]) ["torso_back", "M3 × 12", 3, 12, "pan", torso_pt([s * 44, torso_d + 1.6, zz]), [0, -1, 0]]],
    [for (p = visor_screw_pts()) ["visor", "M2.5 × 6", 2.5, 6, "pan", head_pt([p[0], visor_back_y + 3, p[1]]), [0, -1, 0]]],
    [for (p = visor_post_pts()) ["clamp", "M2.5 × 8", 2.5, 8, "pan", head_pt([p[0], carrier_y + carrier_t, p[1]]), [0, -1, 0]]],
    [for (s = [-1, 1]) for (dz = [-ear_screw_dz, ear_screw_dz]) ["ears", "M3 × 20", 3, 20, "pan", head_pt([s * (head_w/2 - 11), ear_y, ear_z + dz]), [s, 0, 0]]],
    [for (sx = [-1, 1]) for (sz = [-1, 1]) ["camera", "M2 × 6 + nut", 2, 6, "pan", head_pt([sx * 13.6, cam_board_y - 2.5, cam_z + sz * 13.6]), [0, 1, 0]]],
    [for (s = [-1, 1]) ["camera", "M2 × 8", 2, 8, "pan", head_pt([s * 18, cam_board_y + 5.5, cam_z]), [s, 0, 0]]],
    [for (p = pod_screw_pts) ["camera", "M3 × 10", 3, 10, "pan", head_pt([p[0], p[1], head_h - head_wall - 2]), [0, 0, 1]]],
    [for (a = head_screw_angles) ["head_on", "M3 × 10", 3, 10, "pan", head_pt([head_screw_r * cos(a), head_neck_y + head_screw_r * sin(a), head_bottom_t]), [0, 0, -1]]],
    [for (p = head_back_boss_pts()) ["back_cap", "M3 × 30 countersunk", 3, 30, "csk", head_pt([p[0], head_d, p[1]]), [0, -1, 0]]],
    [for (s = [-1, 1]) for (a = [0, 180]) ["arms", "horn screws", 2, 5, "pan", arm_pt(s, [7.5 * cos(a), 7.5 * sin(a), 0]), arm_dir(s, [0, 0, 1])]],
    [for (s = [-1, 1]) ["arms", "centre screw", 2.5, 6, "pan", arm_pt(s, [0, 0, horn_plate_t + 0.6]), arm_dir(s, [0, 0, -1])]]
);
module screw_shape(sc, s = 0) {
    d = sc[2]; l = sc[3]; p = sc[5]; v = sc[6] / norm(sc[6]);
    b = acos(v[2]); c = atan2(v[1], v[0]);                     // rotate +z onto v
    translate(p) rotate([0, b, c]) {
        cylinder(d = d - 2*s, h = l - s, $fn = 12);
        if (sc[4] == "pan") translate([0, 0, -0.65 * d + s]) cylinder(d = 1.9 * d - 2*s, h = 0.65 * d - s + eps, $fn = 16);
        else translate([0, 0, s]) cylinder(d1 = 2 * d - 2*s, d2 = d - 2*s, h = 0.55 * d, $fn = 16);
    }
}

// Cables: [name, step, group, colour, diameter, label, route, plugs]. The route runs from where the cable
// starts (the Pi or the UPS for most) to where it plugs in; plugs are boxes [lo, hi] at its ends. The video
// draws each one growing along its route in its step; "group" says which assembly it moves with there.
// HDMI, screen power, touch and camera are two pieces each, joined 15 mm above the head's floor: the body
// piece hangs out of the neck until the head goes on.
function neck_pt(o, z) = [o[0], neck_y + o[1], z];
hw_join_z = head_z0 + 15;
hw_neck = [["hdmi", [-4.5, 2.5]], ["power", [4.5, 3]], ["touch", [0, -5]], ["camera", [4.5, -3.5]]];
function hw_no(n) = hw_neck[search([n], hw_neck)[0]][1];
function sbox_pts(u0, u1, v0, v1, w0, w1) = [stk(u0, v1, w0), stk(u1, v0, w1)];     // a box in the stack frame -> [lo, hi]
function hdr_box(u0, u1) = sbox_pts(u0 - 1.3, u1 + 1.3, pi_pin1[1] - 1.3, pi_pin1[1] + 2.54 + 1.3, stk_pi_w1 + 2.5, stk_pi_w1 + 16.5);   // Dupont housings
function usb_box(v, h) = [stk(pi_l + 3, v + 8, stk_pi_w1 + h - 4), stk(pi_l + 21, v - 8, stk_pi_w1 + h + 4)];                       // USB-A plug in the Pi
function pca_ch(k) = [pca_x0 + 12 + 2.54 * k, pca_y0_floor + pca_w - 4, floor_low + floor_low_t + pca_standoff + 1.6];          // servo channel k
function servo_route(k, top, x) = concat(top, [[x, 95 - (x + 10) / 8, 78], [x, 95 - (x + 10) / 8, 67.2], [x, 70, 67.2], [x, 52, 67.3], [x, 44.5, 67.3], [x, 42, 60], [x, 40, 45],
                                     pca_ch(k) + [0, 0, 16.5]]);
pca_i2c = [pca_x0 + 1.9, pca_y0_floor + 10.6];           // the PCA9685's I2C input header (its other end chains on)
function hw_cables() = let (
    hdmi0 = stk(25.8, -0.5, stk_pi_w1 + 1.7), usba = stk(ups_ports[1][0], -0.8, stk_ups_w0 + ups_pcb_t + 3.5), cp = charge_port(),
    tch = stk(35.07, 52.5, stk_pi_w1 + 16.5), i2c = stk(10.94, 52.5, stk_pi_w1 + 16.5),
    cam_usb = stk(pi_l + 21, 29.1, stk_pi_w1 + 12), spk_usb = stk(pi_l + 21, 47, stk_pi_w1 + 4), mic_usb = stk(pi_l + 21, 47, stk_pi_w1 + 12),
    hp = [lcd_x0 + lcd_hdmi_x, glass_y + lcd_stack + 10.5, lcd_z0 - 10])
[
    ["charging", "charge", "floor", "#2b2b2b", charge_cable_d, "charging: UPS USB-C -> socket at the back",
        concat([cp + [0, 0, charge_plug[2]]], charge_path()), [[cp - [charge_plug[0]/2, charge_plug[1]/2, 0], cp + [charge_plug[0]/2, charge_plug[1]/2, charge_plug[2]]]]],
    ["i2c", "pca", "floor", "#d4a017", 4, "I2C: Pi pins 1, 3, 5, 6 -> PCA9685",
        [i2c, [-40, 134, 14], [-52, 128, 14], [-54, 100, 14], [-54, 52, 18], [-50, 40, 28], [-36, 28, 31], [pca_i2c[0], pca_i2c[1], 27.5]],
        [hdr_box(7.13 + 1.3, 13.48), [[pca_i2c[0] - 1.3, pca_i2c[1] - 6.4, 13.5], [pca_i2c[0] + 1.3, pca_i2c[1] + 6.4, 27.5]]]],
    ["hdmi", "cables_up", "none", "#1d1d1d", 5, "HDMI: Pi -> screen",
        [hdmi0 + [0, 0, 20], [-12, 112, 100], [-8, 100, 140], [-5, 92, 170], neck_pt(hw_no("hdmi"), 180), neck_pt(hw_no("hdmi"), hw_join_z)],
        [[hdmi0 - [5.5, 3.5, 0], hdmi0 + [5.5, 3.5, 20]]]],
    ["power", "cables_up", "none", "#c0392b", 3.5, "screen power: UPS USB-A -> screen",
        [usba + [0, 0, 28], [-8, 100, 120], [2, 94, 165], neck_pt(hw_no("power"), 180), neck_pt(hw_no("power"), hw_join_z)],
        [[usba - [8, 4.25, 0], usba + [8, 4.25, 28]]]],
    ["touch", "cables_up", "none", "#2e86de", 4, "touch: Pi pins 19-26 -> screen",
        [tch, [-5, 134, 16], [-6, 134, 55], [-4, 128, 62], [-4, 120, 67], [-4, 117, 80], [-4, 105, 120], [-2, 90, 165], neck_pt(hw_no("touch"), 180), neck_pt(hw_no("touch"), hw_join_z)],
        [hdr_box(31.26, 38.88)]],
    ["camera", "cables_up", "none", "#7d3c98", 3.5, "camera: Pi USB -> camera",
        [cam_usb, [73, 123, 42], [75, 122, 58], [68, 121, 66], [30, 119, 66], [24, 117, 76], [12, 105, 120], [7, 90, 165], neck_pt(hw_no("camera"), 180), neck_pt(hw_no("camera"), hw_join_z)],
        [usb_box(29.1, 12)]],
    ["speaker", "cables_body", "none", "#16a085", 3.5, "speaker -> Pi USB",
        [torso_pt([spk_w/2 + 1.6, spk_y + spk_d/2, spk_z + spk_h/2]), torso_pt([50, 30, 62]), torso_pt([50, 60, 50]), torso_pt([40, 75, 20]), torso_pt([26, 90, 0]),
         [28, 118.5, 66], [60, 120, 66], [72, 117, 58], [72, 116, 26], spk_usb], [usb_box(47, 4)]],
    ["mic", "cables_body", "none", "#e67e22", 3.5, "mic -> Pi USB",
        [torso_pt([mic_x0 + mic_body + 34, mic_y0 + mic_t/2, mic_zc]), torso_pt([14, 9, 100]), torso_pt([26, 12, 96]), torso_pt([28, 40, 88]), torso_pt([20, 70, 50]),
         torso_pt([22, 88, 10]), [22, 116, 70], [26, 120, 66], [60, 123, 66], [76, 125, 50], [74, 124, 24], mic_usb], [usb_box(47, 12)]],
    ["servo_head", "cables_body", "none", "#8d6e63", 2.4, "head servo -> PCA9685 channel 0",
        servo_route(0, [torso_pt([sv_l - sv_shaft_off + 7.5, pinion_yt, hs_spline_z - sv_spline_h + 3.5]), torso_pt([26, 25, 88.8]), torso_pt([26, 40, 84]),
                        torso_pt([14, 66, 40]), torso_pt([-10, 68, 14])], -10), [[pca_ch(0) - [1.3, 4, 0], pca_ch(0) + [1.3, 4, 16.5]]]],
    ["servo_left", "cables_body", "none", "#8d6e63", 2.4, "left arm servo -> PCA9685 channel 1",
        servo_route(1, [torso_pt([as_spline_x - 29, shoulder_yt + sv_l - sv_shaft_off + 7.5, shoulder_zt]), torso_pt([36, 72, 95]), torso_pt([20, 78, 60]), torso_pt([-6, 68.5, 6])], -6),
        [[pca_ch(1) - [1.3, 4, 0], pca_ch(1) + [1.3, 4, 16.5]]]],
    ["servo_right", "cables_body", "none", "#8d6e63", 2.4, "right arm servo -> PCA9685 channel 2",
        servo_route(2, [torso_pt([-(as_spline_x - 29), shoulder_yt + sv_l - sv_shaft_off + 7.5, shoulder_zt]), torso_pt([-36, 72, 95]), torso_pt([-20, 78, 60]), torso_pt([-14, 67.5, 6])], -14),
        [[pca_ch(2) - [1.3, 4, 0], pca_ch(2) + [1.3, 4, 16.5]]]],
    // the head pieces (they go on with the head)
    ["hdmi_head", "head_on", "head", "#1d1d1d", 5, "HDMI (right-angle plug)",
        [neck_pt(hw_no("hdmi"), hw_join_z), head_pt([-9, 60, 22]), head_pt([hp[0], 56, hp[2]]), head_pt(hp)],
        [[head_pt([hp[0] - 11, glass_y + lcd_stack - 1.9, lcd_z0 - 18]), head_pt([hp[0] + 11, glass_y + lcd_stack + 10, lcd_z0 + 1.7])]]],
    ["power_head", "head_on", "head", "#c0392b", 3.5, "screen power (micro-USB)",
        [neck_pt(hw_no("power"), hw_join_z), head_pt([-5, 62, 9]), head_pt([-35, 52, 14]), head_pt([-58, 40, 30]), head_pt([-65, 26, 42]), head_pt([-66.5, 16, 48.5]), head_pt([lcd_x0 - 20.5, 12.5, lcd_z0 + 10.1])],
        [[head_pt([lcd_x0 - 20, glass_y + lcd_stack + 1.7 - 3.75, lcd_z0 + 10.1 - 5.5]), head_pt([lcd_x0 + 0.4, glass_y + lcd_stack + 1.7 + 3.75, lcd_z0 + 10.1 + 5.5])]]],
    ["touch_head", "head_on", "head", "#2e86de", 4, "touch wires (pins 19-26)",
        [neck_pt(hw_no("touch"), hw_join_z), head_pt([-9.5, 45, 25]), head_pt([-9.5, 40.5, 40]), head_pt([-9.5, 40.5, 88]), head_pt([-9.5, glass_y + lcd_stack + lcd_sock_h + 14.5, lcd_z0 + lcd_pin1[1] + 1.27])],
        [[head_pt([lcd_x0 + 8.88 + 9 * 2.54 - 1.6, glass_y + lcd_stack + lcd_sock_h, lcd_z0 + lcd_pin1[1] - 1.6]), head_pt([lcd_x0 + 8.88 + 12 * 2.54 + 1.6, glass_y + lcd_stack + lcd_sock_h + 14, lcd_z0 + lcd_pin1[1] + 2.54 + 1.6])]]],
    ["camera_head", "head_on", "head", "#7d3c98", 3.5, "camera (5-pin plug)",
        [neck_pt(hw_no("camera"), hw_join_z), head_pt([7, 50, 40]), head_pt([9, 46, 100]), head_pt([9, 38, 124]), head_pt([9, 36, 136]), head_pt([20.5, 33, 137]),
         head_pt([20.5, 21, 137]), head_pt([14.5, 19, 137.5]), head_pt([11, 13.8, 138]), head_pt([2, 13.8, cam_z - 12])],
        [[head_pt([-6, 14, cam_z - 16]), head_pt([10, 26, cam_z - 8])]]]
];
module cable_route(c, s = 0) {
    pts = c[6];
    for (i = [0 : len(pts) - 2]) hull() for (q = [pts[i], pts[i + 1]]) translate(q) sphere(d = c[4] - 2*s, $fn = 10);
}
module cable_plugs(c) { for (b = c[7]) translate(b[0]) cube(b[1] - b[0]); }
module hw_cable_colour(c) { color(c[3]) { cable_route(c); cable_plugs(c); } }
// preview: the cables and the electronics inside the see-through shells
module cables_view() {
    %base_shell(); %place_torso() torso_shell(); %place_head() head_shell();
    color("#d9d4c8") base_floor();
    electronics();
    place_head() color("#aab") screen_clamp();
    for (c = hw_cables()) hw_cable_colour(c);
    color("#3c434d") for (sc = hw_screws()) screw_shape(sc);
}

// =============================================================================
//  Print layouts
// =============================================================================
module print_part(p) {
    if (p == "base")             translate([0, 0, base_top]) mirror([0, 0, 1]) base_shell();
    else if (p == "base_floor")  translate([0, 0, -floor_low]) base_floor(false);
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
    else if (p == "pi_carrier")  translate([0, 0, carrier_y + carrier_t]) rotate([-90, 0, 0]) pi_carrier();   // back face down (no longer used)
    else if (p == "screen_clamp") translate([0, 0, carrier_y + carrier_t]) rotate([-90, 0, 0]) screen_clamp(); // back face down
    else if (p == "arm_left")    arm_left();      // inner (flat) face down
    else if (p == "arm_right")   arm_right();
}

// Preview: where the charger goes. The floor with the Pi + UPS (as it sits in the base), the USB-C extension
// from the UPS's USB-C to its socket, the rear wall with the plate whose slot the charger plugs into.
charging_cam = [45, 0, 180];           // the view's rotation (build.sh renders it from there): labels face it
module label(t, at, to = undef, size = 4.5) {
    translate(at) rotate(charging_cam) linear_extrude(0.6) text(t, size = size, halign = "center", valign = "bottom", font = "DejaVu Sans:style=Bold");
    if (!is_undef(to)) hull() { translate(at - [0, 0, 1]) sphere(d = 1, $fn = 8); translate(to) sphere(d = 1, $fn = 8); }
}
module charging_view() {
    color("#d9d4c8") base_floor();
    ups_stack();
    color("#2a2") pca_floor();
    color("#333") { usbc_socket(); charge_cable(); }
    %intersection() { base_shell(); translate([-100, base_d - 12, -1]) cube([200, 20, 64]); }               // the base's rear wall (see-through)
    color("#e8e4da") place_rear_plate() port_plate(rear_w, rear_h, "rear_usb");
    color("#777") translate([rear_x, base_d - base_wall - 0.5, rear_z]) {                                     // the charger's plug
        translate([-charge_plug[0]/2, 0, -charge_plug[1]/2]) cube([charge_plug[0], charge_plug[2] + 4, charge_plug[1]]);
        hull() { translate([0, charge_plug[2] + 4, 0]) rotate([-90, 0, 0]) cylinder(d = 4, h = 6, $fn = 12); translate([12, charge_plug[2] + 24, -24]) sphere(d = 4, $fn = 12); }
    }
    p = charge_port(); pi_c = stk(11.2, -0.5, stk_pi_w1 + 1.6);
    color("#17803a") { label("UPS USB-C: charging", p + [36, -10, 58]); label("the extension plugs in here", p + [36, -10, 52], p + [0, 0, 24], size = 3.6); }
    color("#c0392b") label("Pi USB-C: leave empty", pi_c + [-22, -10, 44], pi_c + [0, 0, 1]);
    color("#17803a") { label("your charger", [-58, base_d + 10, rear_z + 4]); label("plugs in here", [-58, base_d + 15, rear_z - 1], [rear_x - charge_plug[0]/2 - 1, base_d + 14, rear_z]); }
}

section_cut = 0;
if (part == "assembly") assembly(0);
else if (part == "charging") charging_view();
else if (part == "cables") cables_view();
else if (part == "hardware_list") echo(hw = [hw_screws(), hw_cables()]);
else if (part == "exploded") assembly(70);
else if (part == "section_side") difference() { assembly(0); translate([section_cut, -50, -10]) cube([300, 400, 500]); }
else if (part == "section_plan") difference() { assembly(0); translate([-200, -50, section_cut]) cube([400, 400, 500]); }
else print_part(part);
