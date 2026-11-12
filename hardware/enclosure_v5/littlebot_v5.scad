// =============================================================================
//  LittleBot v5 "Rover" - parametric OpenSCAD model (mm)
// =============================================================================
//  v4's head and arms on a new body and a wheeled base. What changed, and why:
//    - the base is a dark tub that carries everything heavy, low: 2 gear motors with 65 mm wheels in the middle,
//      a ball caster front and rear, the Pi + UPS behind the motors, the 6xAA drawer in front of them
//    - the body is a bell: v4's egg above the shoulders, widening in a straight line down to the tub
//    - every joint is bolted through thick plastic into a nut that the part itself holds:
//        body -> tub      8 x M3 x 10 through the tub's rim into nuts in the body's tongue (it plugs into the tub)
//        cap -> body      8 x M3 x 12 down into nuts in a solid collar that is part of the body's wall (v4's plate
//                         stood on 4 thin bosses, and its nuts sat above them: nothing held it down)
//        head -> neck     the head's 3 screws (M3 x 20) go through the black neck into nuts in the hub
//    - the neck turns on a 62 mm flange under the cap and a 72 mm foot on top of it (v4: a 50 mm flange on a
//      3 mm plate); lifting the robot by its head pulls the flange against the cap, not on screws
//  New parts:
//    v5_tub        the dark base (prints floor down, no supports)
//    v5_pod        x2 (the left one mirrored): carries a motor by its face, covers its wheel
//    v5_tray       the 6xAA drawer; its front is the battery door, with the drive switch in it
//    v5_deck       the electronics deck over the motors: RP2040 breadboard, motor driver, 5 V converter, servo power
//    v5_body       the white bell (prints upright, tongue down, no supports)
//    v5_cap        the body's top: carries the neck (prints top face down)
//    v5_hub        the neck's lower half: flange, spigot, the ring gear under it
//    v5_neck       the black neck: over the spigot, under the head
//    v5_slider     the UPS's power switch, through the body's back
//    mic holder + key, speaker keeper: v4's, made again for this body (the mic sits lower)
//  Reused as printed: v4's head (frame, back), arms, camera bracket, stack keeper; v2's ring gear, pinion and
//  servo frame.
//  Coordinates as before: x across (+x = the viewer's right), y front -> back, z up from the floor.
//    openscad -o layout.png -D 'part="layout5"' littlebot_v5.scad
// =============================================================================
// --- the outline: set before the include (v4's own variables use it through bell_sec) ---
E5 = [104, 91, 92]; k5 = 2.4; top5 = [60, 51]; BT = 242;   // v4's egg: widest half sizes and their height, roundness, top section, top
z_fl = 17;                               // the floor's underside: ground clearance
fl_t = 3.5; zf = z_fl + fl_t;            // the floor; its top
z_bt = 78;                               // the tub's top = the body's bottom
tuck = 3;                                // the tub's bottom edge, bevelled
T5 = [115, 102];                         // the tub: half width, half depth
tw = 3;                                  // the tub's wall
function arc5(z, vmax, v1) = let (zv = E5[2] + (BT - E5[2]) / pow(1 - pow(v1 / vmax, k5), 1 / k5), u = min(1, max(0, (z - E5[2]) / (zv - E5[2]))))
    vmax * pow(1 - pow(u, k5), 1 / k5);
function darc5(z, vmax, v1) = arc5(z + 0.25, vmax, v1) - arc5(z - 0.25, vmax, v1);   // its slope, per 0.5 mm
// where a straight line from the tub's rim touches v4's arc (the body is that line below, v4's arc above)
function tan5(vmax, v1, vt) = let (c = [for (z = [95:0.5:230]) abs(arc5(z, vmax, v1) + 2 * darc5(z, vmax, v1) * (z_bt - z) - vt)]) 95 + 0.5 * search(min(c), c)[0];
ztw = tan5(E5[0], top5[0], T5[0]); ztd = tan5(E5[1], top5[1], T5[1]);
function half5(z, vmax, v1, vt, zt) = z >= zt ? arc5(z, vmax, v1) : z >= z_bt ? vt + (arc5(zt, vmax, v1) - vt) * (z - z_bt) / (zt - z_bt) : vt;
function tk5(z) = max(0, z_fl + tuck - z);
function n5(z) = 4 - 1.2 * max(0, min(1, (max(z, z_bt) - 24) / (BT - 24)));
function sec5(z) = [half5(z, E5[0], top5[0], T5[0], ztw) - tk5(z), half5(z, E5[1], top5[1], T5[1], ztd) - tk5(z), n5(z), z];

include <../enclosure_v4/littlebot_v4.scad>
part = "layout5";        // layout5, print5 + which, assy5 + which
which = "";
torso_top_t = 9.5;       // v2's neck parts hang 6.5 mm lower: the hub's flange sits between the cap and the gears
b_wall = 3.0;
mic_dot = [20, 166];     // the chest's dots (the ReSpeaker behind them): below the top's collar
spk4_z = 116;            // the speaker, lower too
function bell_sec(t) = sec5(24 + (BT - 24) * t);   // overrides v4's: its body modules follow the new outline

/* [the wheels: JGA25-370 gear motors with encoders, 65 mm wheels on 12 mm hex couplers] */
yw = 70;                                 // the wheels' axis (y): 8 mm ahead of the body's centre, the Pi + UPS fit behind the motors
wh_d = 65; wh_w = 27;                    // MEASURE the wheels
mot = [25, 60.7, 12.1];                  // the motor: diameter, length without its shaft, shaft beyond the gearbox face
mot_boss = [7, 2.5]; mot_holes = 17;     // the boss round the shaft; its 2 x M3 holes (4.5 deep), this far apart
xf = 61.5;                               // the gearbox faces (|x|): the motors' tails 1.6 mm apart at the middle
pod_t = 4;                               // the pod's plate: M3 x 8 reach 4 mm into the gearbox
wh_x = [xf + pod_t + 1.5, xf + 44.5 + 0.5];   // where the wheel can be (|x|): on the coupler's hex, or wholly beyond it (its screw's head 3.5 more)
az = wh_d / 2;                           // the axis' height
pod_y = 37; pod_top = 69;                // the pod: half length, top
/* [casters: Pololu 3/4" metal ball (20.9 high, base 22.9, 2 holes 15.5 apart)] */
cas_y = [0, 144.6]; cas_h = 20.9; cas_lift = 0.8;   // front, rear; the balls stand this far off a flat floor (the wheels carry it)
cas_seat = cas_h + cas_lift;             // their mounting face
cas_top = cas_seat + 3;
/* [the Pi + UPS: v2's mount, behind the motors, over the rear caster (its screws come up between the UPS board and the Pi)] */
S5 = [0, 41.5, cas_top - (floor_low + floor_low_t)];   // from where v2 has it
/* [6xAA: DFRobot FIT0266 holder, 90 x 57 x 16, in a drawer] */
aa = [90, 57, 16];                       // MEASURE
aa_y0 = 2 * 78 - (78 + (T5[1] - tw) * pow(1 - pow((aa[0] / 2 + 1) / (T5[0] - tw), n5(0)), 1 / n5(0))) + 0.6;   // its front face: just behind the door's corners
tray_z = 31;                             // the drawer's underside: over the front caster's screws
door_hw = 55;                            // the door: half width
sw_cut = [19.2, 13]; sw_c = [28, 58.5];  // the rocker switch's cut-out (KCD1: 13 x 19.2), its centre on the door (x, z)
/* [the deck] */
deck_z = 70; deck_t = 2.5;
/* [the body's joint to the tub] */
tg = [0.3, 4, 10];                       // the tongue: clearance in the tub, thickness, depth
js_z = z_bt - 6;                         // the joint's 8 screws
js = [[0, 22], [0, 124], [1, 70], [2, 62]];   // [0 side / 1 front / 2 back, y (sides) or x]: each on both sides, square to the wall
/* [the top] */
zc0 = BT - 6;                            // the cap's underside = the collar's top
open5 = [[-42, 42], [37, 121.5], 20];    // the opening: x, y, corner radius
cap_scr = [[-52, 62], [52, 62], [-52, 100], [52, 100], [-26, 32.5], [26, 32.5], [-38, 119.5], [38, 119.5]];
bay_r = 21;                              // a bay in the opening's front edge, round the pinion
hub_r = 26; hub_fl = [31, 3]; hub_play = 0.3;   // the hub: spigot radius; flange radius, thickness; up-and-down play
neck_foot = 36;                          // the black neck's foot on the cap
hub_top = hd_z0 - 6;                     // the spigot's top, under the black neck's 6 mm top
nut_af = 5.5; nut_t = 2.4;               // M3 nuts

function clamp01(v) = max(0, min(1, v));
module sec2d(z, inset = 0) let (s = sec5(z)) sq2d(s[0] - inset, s[1] - inset, s[2]);
module slice5(z, inset = 0) translate([0, 0, z]) linear_extrude(0.01) sec2d(z, inset);
module tub_prism(z0, z1, inset = 0) translate([0, 0, z0]) linear_extrude(z1 - z0) sec2d(z_bt, inset);
// the tub's inner wall: y at x (back, front), and how far its normal leans from the y axis
function tub_by(x, inset = tw) = let (s = sec5(z_bt)) b_cy + (s[1] - inset) * pow(1 - pow(abs(x) / (s[0] - inset), s[2]), 1 / s[2]);
function tub_fy(x, inset = tw) = 2 * b_cy - tub_by(x, inset);
function tub_x(y, inset = tw) = let (s = sec5(z_bt)) (s[0] - inset) * pow(1 - pow(abs(y - b_cy) / (s[1] - inset), s[2]), 1 / s[2]);
function tub_lean(x, inset = tw) = let (s = sec5(z_bt), a = s[0] - inset, b = s[1] - inset) atan2(pow(abs(x) / a, s[2] - 1) / a, pow((tub_by(x, inset) - b_cy) / b, s[2] - 1) / b);

// -----------------------------------------------------------------------------
// BODY OUTLINE (overrides v3's): from the tub's rim to the top
// -----------------------------------------------------------------------------
module body_outer(inset = 0, ext = 0) hull() {
    for (i = [0:nsec]) slice5(z_bt + (BT - z_bt) * i / nsec - (i == nsec ? 0.01 : 0), inset);
    if (ext > 0) { translate([0, 0, z_bt - ext]) linear_extrude(0.01) sec2d(z_bt, inset); translate([0, 0, BT + ext]) linear_extrude(0.01) sec2d(BT, inset); }
    for (k = [-1, 1]) place_sh(k) translate([-inset - 0.01, 0, 0]) rotate([0, 90, 0]) cylinder(d = sh_face_d - 2 * inset, h = 0.01);
}

// inside the body; it reaches down inside the tongue, so the speaker's ribs (45 degrees underneath) run on down to the
// tongue's lower edge: they start on the print bed
module body_inner() { body_outer(b_wall, 2); tub_prism(z_bt - tg[2], z_bt, tw + tg[0] + 0.01); }

// -----------------------------------------------------------------------------
// THE PI + UPS, moved: v2's stack, v4's keeper and the mount under them
// -----------------------------------------------------------------------------
module place_stack() translate(S5) children();
K5 = ups_knob_c + S5;                    // the UPS switch's knob
zs0 = floor_low + floor_low_t;           // v2's floor top: the plinth's top once moved
module stack_mount() {                   // on the tub's floor
    rx0 = keeper_x[0]; rx1 = keeper_x[1]; wy0 = wall_y1 - mount_t; wz1 = keeper_mouth_lo - 1;
    difference() {
        union() {
            place_stack() {
                translate([rx0, wy0, zs0 - eps]) cube([rx1 - rx0, mount_t, wz1 - zs0 + eps]);                         // the wall in front of the plate
                translate([rx0, stk_yc - vee_w/2 - 1.2, zs0 - eps]) cube([rx1 - rx0, vee_w + 2.4, vee_z - zs0 + eps]); // the rail with the lower V
                for (x = keeper_x) translate([x, stk_yc, zs0 - eps]) cylinder(d = keeper_boss_d, h = keeper_boss_top - zs0 + eps);
                for (x = [rx0 - 3.5, rx1 + 0.5]) translate([x, wy0, zs0 - eps]) rotate([90, 0, 90]) linear_extrude(3)   // gussets behind the wall's ends
                    polygon([[0, 0], [mount_t + 14, 0], [mount_t, 30], [0, 30]]);
            }
            // the plinth: lifts v2's mount over the rear caster's seat
            translate([rx0 - 4, S5[1] + wy0, zf - eps]) cube([rx1 - rx0 + 8, mount_t + vee_w + 3.2 + 14, cas_top - zf + eps]);
        }
        place_stack() {
            vee_cut(rx0 + keeper_boss_d/2 - 0.5, rx1 - keeper_boss_d/2 + 0.5, vee_z);
            for (u = [3.5, 84.5]) translate([stk_x0 + u - 4.2, stk_yc + 0.6, zs0]) cube([8.4, 5, vee_z]);
            for (x = keeper_x) translate([x, stk_yc, keeper_boss_top - 19]) cylinder(d = pilot_m3, h = 20, $fn = fn_small);   // the keeper's M3 x 20
        }
    }
}

// -----------------------------------------------------------------------------
// THE TUB
// -----------------------------------------------------------------------------
module tub_outer() hull() { translate([0, 0, z_fl]) linear_extrude(0.01) sec2d(z_bt, tuck); tub_prism(z_fl + tuck, z_bt); }
module tub_cavity(inset = tw) tub_prism(zf, z_bt + 1, inset);
// the rim: a 0.8 mm land, then a 45-degree seat for the body's bevel
module rim_cut() hull() { translate([0, 0, z_bt + 0.01]) linear_extrude(0.01) sec2d(z_bt, 0.8); translate([0, 0, z_bt - tw + 0.8]) linear_extrude(0.01) sec2d(z_bt, tw); }
module door_notch(g = 0) translate([-door_hw - g, -100, tray_z + (g < 0 ? 0 : -0.3)]) cube([2 * (door_hw + g), 100 + b_cy - 60, 200]);   // the wall there is the door (the tub's sill 0.3 under it)
// a joint screw's frame: its origin on the wall's outer face, z out along the wall's normal, y up
function js_pt(j, s) = j[0] == 0 ? [s * tub_x(j[1], 0), j[1]] : [s * j[1], j[0] == 1 ? tub_fy(j[1], 0) : tub_by(j[1], 0)];
function js_n(j, s) = let (p = js_pt(j, s), q = sec5(z_bt), g = [sign(p[0]) * pow(abs(p[0]) / q[0], q[2] - 1) / q[0], sign(p[1] - b_cy) * pow(abs(p[1] - b_cy) / q[1], q[2] - 1) / q[1]]) g / norm(g);
module place_js(j, s) let (p = js_pt(j, s), n = js_n(j, s)) multmatrix([[-n[1], 0, n[0], p[0]], [n[0], 0, n[1], p[1]], [0, 1, 0, js_z], [0, 0, 0, 1]]) children();
module caster_seat(y) translate([-15.5, y - 15.5, zf - eps]) cube([31, 31, cas_top - zf + eps]);
module caster_cut(y) {
    translate([-12.3, y - 12.3, z_fl - 1]) cube([24.6, 24.6, cas_seat - z_fl + 1]);             // the caster's base in a square recess (its roof bridges)
    for (s = [-1, 1]) translate([s * 7.75, y, 0]) cylinder(d = 2.7, h = 60, $fn = 12);           // #2-56 x 7/16 from below, nuts on top
}
// the charging socket (a USB-C extension's female end): square to the wall at the back's left
chg_x = -84; chg_z = 46;
module place_chg() translate([chg_x, tub_by(chg_x), chg_z]) rotate(tub_lean(chg_x)) children();   // +y out through the wall, the origin on its inner face
module chg_cradle() place_chg() {
    w = usbc_sock[0]; h = usbc_sock[1]; l = usbc_sock[2];
    difference() {
        union() {
            translate([-w/2 - 2.5, -l - 3, -h/2 - 2.5]) cube([w + 5, l + 2, h/2 + 2.5]);          // the cradle
            hull() { translate([-w/2 - 2.5, -l - 3, -h/2 - 2.5 - eps]) cube([w + 5, l + 2, eps]); translate([-4, -l - 3, -h/2 - 2.5 - w/2 - 1]) cube([8, l + 2, eps]); }
            translate([-4, -l - 3, zf - chg_z]) cube([8, l + 2, chg_z - zf - h/2 - 2.5 - w/2 - 1 + eps]);   // its post, from the floor
        }
        translate([-w/2, -l - 3.01, -h/2]) cube([w, l + 5, h]);
        translate([-w/2 - 3, -l/2 - 4, -h/2 - 1.6]) cube([w + 6, 4, 1.2]);                       // zip-tie slot
    }
}
module chg_hole() place_chg() rotate([-90, 0, 0]) translate([0, 0, -3]) linear_extrude(12) hull() for (s = [-1, 1]) translate([s * 3.3, 0]) circle(d = 8.6, $fn = 24);
module usbc_socket5(s = 0) place_chg() translate([-usbc_sock[0]/2 + s, -usbc_sock[2] - 1 + s, -usbc_sock[1]/2 + s]) cube([usbc_sock[0], usbc_sock[2], usbc_sock[1]] - [2*s, 2*s, 2*s]);
deck_posts = [[-55.5, 47], [55.5, 47], [-55.5, 88], [55.5, 88]];
pod_scr = [[76, -1], [98, -1], [76, 1], [98, 1]];           // the pod's 4 screws: |x|, front / rear flange
pod_fl = [pod_y + 0.5, 10, 26.5];                           // the flanges: from here (|y - yw|), width; their underside (the tub's ridges' top)
module v5_tub() {
    difference() {
        union() {
            difference() {
                tub_outer();
                tub_cavity();
                rim_cut();
                door_notch();
            }
            intersection() {
                tub_cavity(tw - 0.01);
                union() {
                    for (y = cas_y) caster_seat(y);
                    stack_mount();
                    chg_cradle();
                    // the wheel pods' ridges (their flanges screw down onto them)
                    for (s = [-1, 1], k = [-1, 1]) translate([s > 0 ? xf + pod_t + 0.5 : -200, yw + (k > 0 ? pod_fl[0] : -pod_fl[0] - pod_fl[1]), zf - eps]) cube([200 - xf - pod_t - 0.5, pod_fl[1], pod_fl[2] - zf + eps]);
                    // the deck's 4 posts
                    for (p = deck_posts) translate([p[0], p[1], zf - eps]) { cylinder(d = 8, h = deck_z - zf + eps); cylinder(d1 = 11, d2 = 8, h = 6); }
                    // the door's 2 posts (its screws), the drawer's rails, side guides and back stop
                    for (s = [-1, 1]) {
                        translate([s > 0 ? door_hw - 6 : -door_hw - 4, -60, zf - eps]) cube([10, 60 + tub_fy(door_hw) + 9, tray_z + 14 - zf]);
                        translate([s > 0 ? 34 : -46, -5, zf - eps]) cube([12, 45, tray_z - zf]);
                        translate([s > 0 ? aa[0]/2 + 4.2 : -aa[0]/2 - 6.2, 16, zf - eps]) cube([2, 24, tray_z + 5 - zf]);
                    }
                    translate([-10, tray_y1 + 0.4, zf - eps]) cube([20, 2.5, tray_z + 5 - zf]);
                    // under each motor's tail: a saddle
                    for (s = [-1, 1]) translate([s > 0 ? 8 : -16, yw - 11, zf - eps]) cube([8, 22, 6]);
                }
            }
        }
        // the wheels come through the floor
        for (s = [-1, 1]) translate([s > 0 ? wh_x[0] - 1 : -wh_x[1] - 1, yw - wh_d/2 - 2, z_fl - 1]) cube([wh_x[1] - wh_x[0] + 2, wh_d + 4, fl_t + 2]);
        for (y = cas_y) caster_cut(y);
        // the motors: a shallow trough in the floor and the saddles
        for (s = [-1, 1]) translate([s > 0 ? 0.3 : -xf - 0.5, yw, az]) rotate([0, 90, 0]) cylinder(d = mot[0] + 1.4, h = xf + 0.2);
        for (s = [-1, 1], k = [-1, 1]) translate([s * 12 - 2, yw + k * 14.5 - 1.2, z_fl - 1]) cube([4, 2.4, fl_t + 2]);           // zip-tie slots beside the saddles
        // the pods' screws (M3 x 12 into the ridges), the deck's (M3 x 8), the door's (M3 x 10)
        for (s = [-1, 1], p = pod_scr) translate([s * p[0], yw + p[1] * (pod_fl[0] + pod_fl[1] / 2), z_fl + 0.8]) cylinder(d = pilot_m3, h = 20, $fn = fn_small);
        for (p = deck_posts) translate([p[0], p[1], deck_z - 12]) cylinder(d = pilot_m3, h = 13, $fn = fn_small);
        for (s = [-1, 1]) translate([s * (door_hw - 2.6), tub_fy(door_hw) - 4, tray_z + 6]) rotate([-90, 0, 0]) cylinder(d = pilot_m3, h = 14, $fn = fn_small);
        // the body's 8 screws: through the rim, a flat seat for each head
        for (j = js, s = [-1, 1]) place_js(j, s) { translate([0, 0, -12]) cylinder(d = clear_m3, h = 14, $fn = fn_small); translate([0, 0, -1.2]) cylinder(d = 6.6, h = 4, $fn = 24); }
        chg_hole();
        // vents under the Pi's fan (the Pi's SD-card end) and under the motor driver
        if (detail) for (vy = [stk_y0 + 40 : 6 : stk_y0 + 76]) translate([stk_x0 - 28, S5[1] + vy, z_fl - 1]) cube([22, 2.5, fl_t + 2]);
    }
}
module wheel_dummy(s, x0 = xf + 44.5 - wh_w) color("#222") translate([s * (x0 + wh_w/2), yw, az]) rotate([0, 90, 0]) cylinder(d = wh_d, h = wh_w, center = true);
module motor_dummy(s, g = 0) translate([0, yw, az]) rotate([0, s * 90, 0]) {
    translate([0, 0, xf - mot[1] + g]) cylinder(d = mot[0] - 2*g, h = mot[1] - 2*g);                 // encoder, can, gearbox
    translate([0, 0, xf]) cylinder(d = mot_boss[0], h = mot_boss[1]); translate([0, 0, xf]) cylinder(d = 4, h = mot[2]);
    translate([0, 0, xf + pod_t + 0.5]) cylinder(d = 12 - 2*g, h = 18);                              // the coupler
}
module caster_dummy(y) { color("#ccc") translate([0, y, cas_lift + 9.525]) sphere(d = 19.05, $fn = 32); color("#222") translate([0, y, cas_seat - 12]) cylinder(d1 = 16, d2 = 22.9, h = 12); }

// -----------------------------------------------------------------------------
// WHEEL POD (the +x one; the other is its mirror image): a plate the motor screws to by its face (2 x M3 x 8 from the
// wheel's side), a hood over the wheel, 2 flanges that screw down onto the tub's ridges (4 x M3 x 12). The motor,
// coupler and wheel go on at the bench. Prints plate down.
// -----------------------------------------------------------------------------
module v5_pod() intersection() {
    tub_cavity(tw + tg[0] + tg[1] + 0.6);                    // inside the body's tongue
    difference() {
        union() {
            translate([xf, yw - pod_y, zf + 0.2]) cube([pod_t, 2 * pod_y, pod_top - zf - 0.2]);                                // the plate
            translate([xf, yw - pod_y, pod_top - 2]) cube([200, 2 * pod_y, 2]);                                                // the hood: roof,
            for (k = [-1, 1]) translate([xf, yw + (k > 0 ? pod_y - 2 : -pod_y), zf + 0.2]) cube([200, 2, pod_top - zf - 0.2]); // front and rear walls
            for (k = [-1, 1]) translate([xf, yw + (k > 0 ? pod_y - eps : -pod_y - pod_fl[1] + 0.5), pod_fl[2] + 0.1]) cube([200, pod_fl[1] - 0.5 + eps, 3]);   // flanges
        }
        translate([xf - 1, yw, az]) rotate([0, 90, 0]) {
            cylinder(d = mot_boss[0] + 0.8, h = pod_t + 2, $fn = 32);
            for (k = [-1, 1]) translate([0, k * mot_holes / 2, 0]) cylinder(d = clear_m3, h = pod_t + 2, $fn = fn_small);          // the motor's 2 x M3
            for (k = [-1, 1]) translate([k * mot_holes / 2, 0, 0]) cylinder(d = 6.2, h = 1 + 1.5, $fn = 24);                       // room for the gearbox's own 2 screw heads
        }
        for (p = pod_scr) translate([p[0], yw + p[1] * (pod_fl[0] + pod_fl[1] / 2), pod_fl[2] - 1]) cylinder(d = clear_m3, h = 6, $fn = fn_small);
    }
}
module place_pod(s) { if (s > 0) children(); else mirror([1, 0, 0]) children(); }

// -----------------------------------------------------------------------------
// THE 6xAA DRAWER: a tray for the holder; its front is the tub's wall there (the battery door), the drive switch in it.
// It slides in on the tub's rails; 2 x M3 x 10 through the door's lower corners. Prints tray down.
// -----------------------------------------------------------------------------
tray_y1 = aa_y0 + aa[1] + 0.6 + 2;
module door_shell() difference() {
    intersection() { difference() { tub_outer(); tub_cavity(tw - 0.05); rim_cut(); } door_notch(-0.3); translate([-200, -100, 0]) cube([400, 100 + b_cy - 61, 200]); }
}
module v5_tray() {
    hw = aa[0] / 2 + 0.6; y0 = aa_y0 - 0.6;
    difference() {
        union() {
            door_shell();
            intersection() {
                tub_cavity(tw - 0.3);                               // 0.3 into the door: one piece
                union() {
                    translate([-hw - 2, -60, tray_z]) cube([2 * hw + 4, 60 + tray_y1, 2]);                                     // the tray
                    for (s = [-1, 1]) translate([s > 0 ? hw : -hw - 2, -60, tray_z]) cube([2, 60 + tray_y1, 9]);
                    translate([-hw - 2, tray_y1 - 2, tray_z]) cube([2 * hw + 4, 2, 9]);
                    // the switch's wall, thin for a snap-in rocker; ribs from the door to the tray
                    for (s = [-1, 1]) translate([s * (hw + 1) - 1, -60, tray_z]) cube([2, 60 + y0 + 14, 16]);
                }
            }
        }
        translate([sw_c[0] - sw_cut[0]/2, -100, sw_c[1] - sw_cut[1]/2]) cube([sw_cut[0], 120, sw_cut[1]]);
        translate([sw_c[0] - sw_cut[0]/2 - 2.5, tub_fy(sw_c[0], 0) + 1.6, sw_c[1] - sw_cut[1]/2 - 2.5]) cube([sw_cut[0] + 5, 20, sw_cut[1] + 5]);
        // its 2 screws; slots over the front caster's screw ends; 2 pilot holes for the holder's own screws
        for (s = [-1, 1]) translate([s * (door_hw - 2.6), tub_fy(door_hw, 0) - 1, tray_z + 6]) rotate([-90, 0, 0]) { cylinder(d = clear_m3, h = 12, $fn = fn_small); cylinder(d = 6.6, h = 1 + 0.9, $fn = 24); }
        for (s = [-1, 1]) translate([s * 7.75 - 3.5, cas_y[0] - 3.5, tray_z - 1]) cube([7, 100, 4]);
        for (s = [-1, 1]) translate([s * 29.5, aa_y0 + aa[1] / 2, tray_z - 1]) cylinder(d = pilot_m3, h = 4, $fn = fn_small);
    }
}
module aa_dummy(s = 0) color("#151515") translate([-aa[0]/2 + s, aa_y0 + s, tray_z + 2 + s]) cube(aa - [2*s, 2*s, 2*s]);
module switch_dummy(s = 0) color("#333") translate([sw_c[0] - sw_cut[0]/2 + s, tub_fy(sw_c[0], 0) - 2, sw_c[1] - sw_cut[1]/2 + s]) cube([sw_cut[0] - 2*s, 24, sw_cut[1] - 2*s]);

// -----------------------------------------------------------------------------
// THE DECK: a plate on the tub's 4 posts (4 x M3 x 8), over the motors and the pods. On it, wired at the bench: the
// RP2040 on its breadboard, the motor driver (Cytron MDD3A, 4 x M3 x 6 into posts), the servo power board, the 5 V
// converter. Prints flat.
// -----------------------------------------------------------------------------
bb = [83, 55, 8.5]; bb_c = [53.9, 56];             // the breadboard and its centre
drv = [41.66, 48.26]; drv_c = [-74, 68]; drv_holes = [36.58, 43.18];   // the MDD3A, its terminals towards the back
perf = [50, 70]; perf_c = [-15, 45];             // the servo power board (5 x 7 cm perfboard)
buck = [23, 18]; buck_c = [61.5, 13];              // DFR0753
module deck2d() difference() {
    intersection() { sec2d(z_bt, tw + tg[0] + js_boss + 1.5); translate([-200, -7]) square([400, 100]); }
    translate([keeper_x[0] - 6.5, S5[1] + wall_y1 - mount_t - 1.5]) square([keeper_x[1] - keeper_x[0] + 13.5, 100]);       // round the Pi + UPS's mount (the motors' leads come up here)
    translate([0, -7]) rr(30, 14, 4);                                                                                    // the battery's leads
    for (s = [-1, 1]) translate([s * (spk_w / 2 + 0.3) - 4.5, -20]) square([9, 20 + spk3_c[1] + spk_d + 3 - (spk3_c[2] - spk_h / 2 - 2 - deck_z) + 3]);   // the speaker ribs' lower ends
}
module v5_deck() difference() {
    union() {
        translate([0, 0, deck_z]) linear_extrude(deck_t) deck2d();
        for (sx = [-1, 1], sy = [-1, 1]) translate([drv_c[0] + sx * drv_holes[0] / 2, drv_c[1] + sy * drv_holes[1] / 2, deck_z]) cylinder(d = 6, h = deck_t + 4, $fn = 24);
        // low fences: round the breadboard, at the perfboard's corners, round the converter
        translate([0, 0, deck_z]) linear_extrude(deck_t + 2.5) intersection() { deck2d(); union() {
            difference() { translate(bb_c) rr(bb[0] + 4, bb[1] + 4, 2); translate(bb_c) rr(bb[0] + 0.8, bb[1] + 0.8, 1); translate(bb_c) square([bb[0] - 20, 80], center = true); translate(bb_c) square([120, bb[1] - 20], center = true); }
            difference() { translate(perf_c) rr(perf[0] + 4, perf[1] + 4, 2); translate(perf_c) rr(perf[0] + 0.8, perf[1] + 0.8, 1); translate(perf_c) square([perf[0] - 16, 80], center = true); translate(perf_c) square([120, perf[1] - 16], center = true); }
            difference() { translate(buck_c) rr(buck[0] + 4, buck[1] + 4, 2); translate(buck_c) rr(buck[0] + 0.6, buck[1] + 0.6, 1); translate(buck_c) square([buck[0] - 8, 60], center = true); }
        } }
    }
    for (p = deck_posts) translate([p[0], p[1], deck_z - 1]) { cylinder(d = clear_m3, h = 10, $fn = fn_small); }
    for (sx = [-1, 1], sy = [-1, 1]) translate([drv_c[0] + sx * drv_holes[0] / 2, drv_c[1] + sy * drv_holes[1] / 2, deck_z - 1]) cylinder(d = pilot_m3, h = 12, $fn = fn_small);
    // zip-tie slots: across the perfboard, the converter and the breadboard
    for (c = [[perf_c, perf], [buck_c, buck], [bb_c, bb]]) for (s = [-1, 1]) translate([c[0][0] - 2.5, c[0][1] + s * (c[1][1] / 2 + 3.6) - 1.2, deck_z - 1]) cube([5, 2.4, deck_t + 5]);
}
module deck_dummies(s = 0) {
    color("#eee") translate([bb_c[0] - bb[0]/2 + s, bb_c[1] - bb[1]/2 + s, deck_z + deck_t]) cube([bb[0] - 2*s, bb[1] - 2*s, bb[2]]);
    color("#1a5") translate([bb_c[0] - 26.7, bb_c[1] - 11.4, deck_z + deck_t + bb[2]]) cube([53.34, 22.86, 12]);                 // the RP2040 on its pins
    color("#a22") translate([drv_c[0] - drv[0]/2 + s, drv_c[1] - drv[1]/2 + s, deck_z + deck_t + 4]) cube([drv[0] - 2*s, drv[1] - 2*s, 13.6]);
    color("#b84") translate([perf_c[0] - perf[0]/2 + s, perf_c[1] - perf[1]/2 + s, deck_z + deck_t + 0.2]) cube([perf[0] - 2*s, perf[1] - 2*s, 12]);
    color("#124") translate([buck_c[0] - buck[0]/2 + s, buck_c[1] - buck[1]/2 + s, deck_z + deck_t + 0.2]) cube([buck[0] - 2*s, buck[1] - 2*s, 7]);
}

// -----------------------------------------------------------------------------
// BODY (the white bell): from the tub's rim to the cap
// -----------------------------------------------------------------------------
// its bottom: a 0.95 mm land on the tub's rim, a bevel in the rim's seat, a tongue down inside the tub
module joint_solid() {
    hull() { translate([0, 0, z_bt]) linear_extrude(0.01) sec2d(z_bt, 0.95); translate([0, 0, z_bt - (tw + tg[0] - 0.95)]) linear_extrude(0.01) sec2d(z_bt, tw + tg[0]); }
    tub_prism(z_bt - tg[2], z_bt - (tw + tg[0] - 0.95) + 0.01, tw + tg[0]);
}
band_h = 6;                              // above the rim the wall is as thick as wall + tongue for this far, then it thins at 45 degrees
module body_cavity() {
    intersection() { body_outer(b_wall, 2); slab(z_bt + band_h + tw + tg[0] + tg[1] - b_wall - 0.01, BT + 10); }
    hull() { tub_prism(z_bt - tg[2] - 1, z_bt, tw + tg[0] + tg[1]); slice5(z_bt + band_h, tw + tg[0] + tg[1]); slice5(z_bt + band_h + tw + tg[0] + tg[1] - b_wall, b_wall); }
}
js_boss = 9.5;                           // the bosses behind the tongue at the 8 screws
module tongue_bosses() intersection() {
    tub_prism(z_bt - tg[2], z_bt + band_h, tw + tg[0]);
    union() for (j = js, s = [-1, 1]) place_js(j, s) translate([-6.5, -40, -(tw + tg[0] + js_boss)]) cube([13, 80, 40]);
}
// each nut's slot: open at the tongue's lower edge (the nut drops in with the body upside down), 3 mm of tongue in front of it
module js_cuts() for (j = js, s = [-1, 1]) place_js(j, s) {
    translate([0, 0, -(tw + tg[0] + js_boss) - 2]) cylinder(d = clear_m3, h = js_boss + 4, $fn = fn_small);
    translate([-nut_af / 2 - 0.1, -30, -(tw + tg[0] + 3 + nut_t + 0.4)]) cube([nut_af + 0.2, 30 + nut_af / 2 + 0.4, nut_t + 0.4]);
}
// the top: a solid collar under the cap, part of the wall (45 degrees underneath: it prints without supports)
col_t = 9.2;
cap_in = 3.2;                            // the cap's top edge, in from the body's top edge
module open2d(g = 0) { translate([(open5[0][0] + open5[0][1]) / 2, (open5[1][0] + open5[1][1]) / 2]) rr(open5[0][1] - open5[0][0] + 2 * g, open5[1][1] - open5[1][0] + 2 * g, open5[2] + g);
                       translate([0, torso_y0 + pinion_yt]) circle(r = bay_r + g); }
module collar5() difference() {
    intersection() { body_inner(); slab(zc0 - 110, zc0); }
    hull() { translate([0, 0, zc0 - col_t]) linear_extrude(col_t + 12) open2d(); translate([0, 0, zc0 - col_t - 110]) linear_extrude(0.01) open2d(110); }
}
function cap_side(p) = abs(p[0]) > 45 ? 0 : p[1] < b_cy ? 1 : 2;   // which edge of the opening a cap screw's nut goes in from
module collar_cuts() for (p = cap_scr) {
    translate([p[0], p[1], zc0 - 14]) cylinder(d = clear_m3, h = 15, $fn = fn_small);
    translate([0, 0, zc0 - 3.5 - nut_t - 0.4]) linear_extrude(nut_t + 0.4) {
        if (cap_side(p) == 0) translate([p[0] > 0 ? open5[0][1] - 1 : p[0] - nut_af / 2 - 0.5, p[1] - nut_af / 2 - 0.1]) square([abs(p[0]) - open5[0][1] + 1 + nut_af / 2 + 0.5, nut_af + 0.2]);
        if (cap_side(p) == 1) translate([p[0] - nut_af / 2 - 0.1, p[1] - nut_af / 2 - 0.5]) square([nut_af + 0.2, 16]);
        if (cap_side(p) == 2) translate([p[0] - nut_af / 2 - 0.1, p[1] + nut_af / 2 + 0.5 - 16]) square([nut_af + 0.2, 16]);
    }
}
// the UPS's switch slider through the back, just above the thick band
sl5_z0 = K5[2] + ups_knob[1] - 2.5;
sl5_wall_y = back_y(K5[0], sl5_z0 + sl_h / 2);
sl5_tab_y = back_y(abs(K5[0]) - 8 - ups_knob[2], sl5_z0 - 3) + 0.4;   // the tab clears the wall wherever it slides
module slider5_tunnel() intersection() {
    body_inner();
    hull() {
        translate([K5[0] - sl_tun[0]/2 - 2.5, sl5_wall_y - 16, sl5_z0 - 2.7]) cube([sl_tun[0] + 5, 24, sl_tun[1] + 5]);
        translate([K5[0] - sl_tun[0]/2 - 2.5, sl5_wall_y - 1, sl5_z0 - 2.7 - 15]) cube([sl_tun[0] + 5, 6, 1]);
    }
}
module slider5_cut() translate([K5[0] - sl_tun[0]/2, sl5_wall_y - 30, sl5_z0 - 0.2]) cube([sl_tun[0], 40, sl_tun[1]]);
module v5_slider() {
    y0 = K5[1] - 5; y1 = sl5_tab_y;
    difference() {
        union() {
            translate([K5[0] - sl_w/2, y0 + 8, sl5_z0]) cube([sl_w, y1 - y0 - 8 + eps, sl_h]);                       // bar
            translate([K5[0] - sl_head/2, y0, sl5_z0]) cube([sl_head, 10, sl_h]);                                   // fork head
            translate([K5[0] - 8, y1, sl5_z0 - 3]) cube([16, 4, sl_h + 3]);                                          // thumb tab (outside)
        }
        translate([K5[0], y0 - 1, sl5_z0 - 1]) linear_extrude(ups_knob[1] + 1.5 + 1) polygon([[-2.15, 0], [2.15, 0], [2.15, 13], [-2.15, 13]]);
        translate([K5[0], y0 - 1, sl5_z0 - 0.01]) rotate([-90, 0, 0]) linear_extrude(13) polygon([[-2.15 - 2.6, 0], [2.15 + 2.6, 0], [2.15, -2.6], [-2.15, -2.6]]);
        for (k = [-1, 1]) translate([K5[0] + k * 5, y1 + 4, sl5_z0 + sl_h/2 - 1]) rotate([90, 0, 0]) cylinder(d = 2.4, h = 2, center = true, $fn = 12);   // grip dimples
    }
}
module knob5_dummy(st = 0) color("#222") translate([K5[0] + st * ups_knob[2] - ups_knob[0]/2, K5[1] - 1.1, K5[2] + 0.3]) cube([ups_knob[0], 2.2, ups_knob[1] - 0.3]);
module back_vents5() if (detail) for (i = [-3:3]) translate([i * 7 - 1.5, b_cy, 168]) cube([3, 140, 14]);

module v5_body() {
    difference() {
        union() {
            difference() { union() { body_outer(); joint_solid(); } body_cavity(); }
            for (k = [-1, 1]) intersection() { body_outer(); place_sh(k) sh_block(); }
            collar5();
            sa_speaker_ribs(); sa_mic_sleeve();
            slider5_tunnel();
            tongue_bosses();
        }
        slab(zc0, BT + 10);
        for (k = [-1, 1]) place_sh(k) sh_cut();
        collar_cuts();
        sa_speaker_cuts();
        dot_holes();
        slider5_cut();
        back_vents5();
        js_cuts();
    }
}
// v4's mic holder on this body (clear of the collar's lowest edge)
module v5_rsl_holder() difference() { sa_rsl_holder(); minkowski() { collar5(); cube(0.4, center = true); } }

// -----------------------------------------------------------------------------
// THE CAP: the body's top, on the collar (8 x M3 x 12 from above, heads sunk). Under it: a lip in the opening, the
// head servo frame's 2 pillars (thick at their roots), a fence round the gears. Prints top face down.
// -----------------------------------------------------------------------------
gz0 = torso_z0 + gear_z0; pin_y = torso_y0 + pinion_yt;   // the gears' underside, the pinion's axis (robot coordinates)
frame_top = torso_z0 + hs_tab_top - sv_tab_t;            // the servo frame's top: the pillars' ends
module v5_cap() difference() {
    union() {
        hull() { slice5(zc0); translate([0, 0, BT - 0.6]) linear_extrude(0.01) sec2d(zc0, cap_in); translate([0, 0, BT - 0.01]) linear_extrude(0.01) sec2d(zc0, cap_in + 0.6); }   // its edge: 60 degrees
        translate([0, 0, zc0 - 4]) linear_extrude(4 + eps) difference() { open2d(-0.3); open2d(-2.8); translate([0, neck_y]) circle(r = hub_fl[0] + 1.5); }
        intersection() {
            translate([0, 0, gz0 - 1.6]) linear_extrude(zc0 - gz0 + 1.6 + eps) difference() {
                hull() { translate([0, neck_y]) circle(r = hub_fl[0] + 1.5 + 1.6); translate([0, pin_y]) circle(r = pin_rp + gear_m + 2.0 + 1.6); }
                hull() { translate([0, neck_y]) circle(r = hub_fl[0] + 1.5); translate([0, pin_y]) circle(r = pin_rp + gear_m + 2.0); }
            }
            translate([-100, 0, 0]) cube([200, open5[1][1] - 3.5, 400]);
        }
        for (px = frame_pillar_x) translate([px, pin_y, 0]) {
            translate([0, 0, frame_top]) cylinder(d = 8, h = zc0 - frame_top + eps);
            translate([0, 0, frame_top + collar_h + 0.6]) cylinder(d1 = 8, d2 = 13, h = 4);
            translate([0, 0, frame_top + collar_h + 4.6 - eps]) cylinder(d = 13, h = zc0 - frame_top - collar_h - 4.6 + eps);
        }
    }
    translate([0, neck_y, zc0 - 30]) cylinder(r = hub_r + 0.25, h = 60, $fn = 96);
    for (p = cap_scr) translate([p[0], p[1], zc0 - 1]) { cylinder(d = clear_m3, h = 10, $fn = fn_small); translate([0, 0, 1 + 2.8]) cylinder(d = 6.4, h = 10, $fn = 24); }
}

// -----------------------------------------------------------------------------
// THE NECK: a hub (flange under the cap, spigot up through it; v2's ring gear screws on under the flange) and the
// black neck (over the spigot, its foot on the cap; 3 x M3 x 12 down into the spigot). The head's 3 screws (M3 x 20)
// go through the head's floor and the black neck's top into 3 nuts in the spigot's side slots.
// -----------------------------------------------------------------------------
hub_nut_z = hd_z0 + 4 - 20 + 2.2;        // the nuts: 2.2 mm of screw beyond them
neck_scr = [30, 140, 290];               // the black neck's own 3 screws into the hub (M3 x 12, heads sunk): they hold the neck together before the head goes on
hub_key = 150;                           // a groove up the spigot / a rib in the black neck: it only goes on one way
module v5_hub() translate([0, neck_y, 0]) difference() {
    union() {
        translate([0, 0, zc0 - hub_play - hub_fl[1]]) cylinder(r = hub_fl[0], h = hub_fl[1], $fn = 96);
        translate([0, 0, zc0 - hub_play - eps]) cylinder(r = hub_r, h = hub_top - zc0 + hub_play + eps, $fn = 96);
    }
    translate([0, 0, zc0 - 10]) cylinder(r = neck_bore_r, h = 60, $fn = 48);
    for (a = ring_screw_angles) rotate(a) translate([ring_screw_r, 0, zc0 - hub_play - hub_fl[1] - 1]) cylinder(d = pilot_m3, h = 10, $fn = fn_small);   // the ring gear's M3 x 12
    for (a = head4_scr) rotate(a) {
        translate([head_screw_r, 0, hub_nut_z - 4]) cylinder(d = clear_m3, h = 30, $fn = fn_small);
        translate([head_screw_r - nut_af / 2 - 0.6, -nut_af / 2 - 0.1, hub_nut_z]) cube([20, nut_af + 0.2, nut_t + 0.4]);
    }
    rotate(hub_key) translate([hub_r - 1.6, -1.7, BT + 1]) cube([5, 3.4, 40]);
    for (a = neck_scr) rotate(a) translate([head_screw_r, 0, hub_top - 9.5]) cylinder(d = pilot_m3, h = 10, $fn = fn_small);
}
module v5_neck() translate([0, neck_y, 0]) difference() {
    union() {
        translate([0, 0, BT + 8]) cylinder(r = puck_r, h = hd_z0 - BT - 8, $fn = 96);
        hull() { translate([0, 0, BT + 0.05]) cylinder(r = neck_foot, h = 1.2, $fn = 96); translate([0, 0, BT + 8]) cylinder(r = puck_r, h = 0.01, $fn = 96); }
    }
    difference() {
        translate([0, 0, BT - 1]) cylinder(r = hub_r + 0.2, h = hub_top - BT + 1, $fn = 96);
        rotate(hub_key) translate([hub_r - 1.3, -1.4, BT + 1.5]) cube([5, 2.8, 40]);                // its key
    }
    translate([0, 0, BT]) cylinder(r = neck_bore_r, h = 40, $fn = 48);
    for (a = head4_scr) rotate(a) translate([head_screw_r, 0, hub_top - 1]) cylinder(d = clear_m3, h = 10, $fn = fn_small);
    for (a = neck_scr) rotate(a) translate([head_screw_r, 0, hub_top - 1]) { cylinder(d = clear_m3, h = 10, $fn = fn_small); translate([0, 0, 1 + 2.8]) cylinder(d = 6.4, h = 10, $fn = 24); }
}

// -----------------------------------------------------------------------------
// LAYOUT
// -----------------------------------------------------------------------------
module v5_neck_static() place_torso() {
    color("#e6b35a") translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion();
    color("#aab") translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    color("#111") translate([0, pinion_yt, hs_spline_z]) servo_dummy();
}
module v5_ring_gear() place_torso() translate([0, neck_yt, gear_z0 + 0.6]) ring_gear();     // on the hub's flange: 0.6 mm above the pinion's level
module v5_base_parts(deck = true) {
    color(DARK4) render() v5_tub();
    for (s = [-1, 1]) { color("#3a3d44") place_pod(s) render() v5_pod(); wheel_dummy(s); color("#999") motor_dummy(s); }
    for (y = cas_y) caster_dummy(y);
    place_stack() { ups_stack(); color("#c8c2b4") stack_keeper(); }
    color(DARK4) render() v5_tray(); aa_dummy(); switch_dummy();
    if (deck) { color("#d9d4c8") render() v5_deck(); deck_dummies(); }
    color("#333") usbc_socket5(); knob5_dummy();
}
module v5_layout() {
    v5_base_parts();
    color(WHITE4) render() v5_body();
    color(WHITE4) render() v5_cap();
    color(WHITE4) v5_slider();
    speaker3_dummy(); rsl_dummy(); color("#c8c2b4") { render() v5_rsl_holder(); sa_rsl_key(); sa_spk_keeper(); }
    v5_neck_static();
    place_head3(head_angle) {
        color("#e6b35a") v5_ring_gear(); color("#7fb3d5") render() v5_hub(); color(DARK4) render() v5_neck();
        color(WHITE4) render() sa_head_frame(); color(DARK4) render() sa_head_inlay(); color(WHITE4) render() sa_head_back();
        screen_dummy3(); screen_plugs3();
        color("#4FE3E0") translate([0, g3_y - 0.05, scr_cz]) rotate([90, 0, 0]) linear_extrude(0.4) eyes4_2d();
        color("#aab") place_cam4() camera_bracket();
    }
    for (s = [-1, 1]) { arm_servo3(s); color(WHITE4) place_arm3(s, arm_angle) sa_arm(); }
}
if (part == "layout5") translate([0, b_cy, 0]) rotate(spin) translate([0, -b_cy, 0]) {
    if (cut) difference() { v5_layout(); translate([0, -200, -10]) cube([400, 500, 600]); } else v5_layout();
}

// -----------------------------------------------------------------------------
// PRINT (part = "print5", which = name): each part in its print orientation, on z = 0
// -----------------------------------------------------------------------------
module print5(w) {
    if (w == "tub")        translate([0, 0, -z_fl]) v5_tub();                                                   // floor down
    if (w == "pod_right")  rotate([0, -90, 0]) translate([-xf, -yw, 0]) v5_pod();                             // plate down
    if (w == "pod_left")   mirror([0, 1, 0]) print5("pod_right");
    if (w == "tray")       translate([0, 0, -tray_z]) v5_tray();                                               // tray down, the door standing
    if (w == "deck")       translate([0, 0, -deck_z]) v5_deck();                                               // flat
    if (w == "body")       translate([0, 0, -(z_bt - tg[2])]) v5_body();                                      // upright, tongue down (5 mm brim)
    if (w == "cap")        translate([0, 0, BT]) mirror([0, 0, 1]) v5_cap();                                   // top face down
    if (w == "hub")        translate([0, -neck_y, -(zc0 - hub_play - hub_fl[1])]) v5_hub();                    // flange down
    if (w == "neck")       translate([0, neck_y, hd_z0]) mirror([0, 0, 1]) v5_neck();                          // top down
    if (w == "slider")     translate([0, 0, sl5_z0 + sl_h]) mirror([0, 0, 1]) v5_slider();                     // top down
    if (w == "mic_holder") let (Mi = [[1, 0, 0, 0], [0, rsl_t[1], rsl_t[2], 0], [0, rsl_n[1], rsl_n[2], 0], [0, 0, 0, 1]], o = rsl_pt([0, 0, 0])) multmatrix(Mi) translate(-o) v5_rsl_holder();
    if (w == "mic_key")    translate([0, 0, -sl4_pocket[1][0] - 0.15]) sa_rsl_key();
    if (w == "spk_keeper") translate([0, 0, -(spk3_c[1] + spk_d + 0.15)]) rotate([90, 0, 0]) sa_spk_keeper();
}
if (part == "print5") print5(which);

// -----------------------------------------------------------------------------
// ASSEMBLED (part = "assy5", which = name): each component where it sits, for the assembled 3MF
// -----------------------------------------------------------------------------
module assy5(w) {
    if (w == "tub")         v5_tub();
    if (w == "pods")        for (s = [-1, 1]) place_pod(s) v5_pod();
    if (w == "tray")        v5_tray();
    if (w == "deck")        v5_deck();
    if (w == "body")        v5_body();
    if (w == "cap")         v5_cap();
    if (w == "slider")      v5_slider();
    if (w == "hub")         place_head3(head_angle) v5_hub();
    if (w == "neck")        place_head3(head_angle) v5_neck();
    if (w == "gears")       { place_head3(head_angle) v5_ring_gear(); place_torso() translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); }
    if (w == "servo_frame") place_torso() translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    if (w == "servos")      { place_torso() translate([0, pinion_yt, hs_spline_z]) servo_dummy(); for (k = [-1, 1]) arm_servo3(k); }
    if (w == "arm_left")    place_arm3(-1, arm_angle) sa_arm();
    if (w == "arm_right")   place_arm3(1, arm_angle) sa_arm();
    if (w == "head_frame")  place_head3(head_angle) sa_head_frame();
    if (w == "head_inlay")  place_head3(head_angle) sa_head_inlay();
    if (w == "head_back")   place_head3(head_angle) sa_head_back();
    if (w == "camera_bracket") place_head3(head_angle) place_cam4() camera_bracket();
    if (w == "screen")      place_head3(head_angle) screen_dummy3();
    if (w == "screen_plugs") place_head3(head_angle) screen_plugs3();
    if (w == "eyes")        place_head3(head_angle) translate([0, g3_y - 0.05, scr_cz]) rotate([90, 0, 0]) linear_extrude(0.4) eyes4_2d();
    if (w == "mic_holder")  v5_rsl_holder();
    if (w == "mic_key")     sa_rsl_key();
    if (w == "spk_keeper")  sa_spk_keeper();
    if (w == "respeaker")   place_rsl() rsl_board();
    if (w == "speaker")     speaker3_dummy();
    if (w == "pi_ups")      place_stack() { ups_stack(); ups_plugs(); }
    if (w == "stack_keeper") place_stack() stack_keeper();
    if (w == "motors")      for (s = [-1, 1]) motor_dummy(s);
    if (w == "wheels")      for (s = [-1, 1]) translate([s * (xf + 44.5 - wh_w / 2), yw, az]) rotate([0, 90, 0]) cylinder(d = wh_d, h = wh_w, center = true);
    if (w == "casters")     for (y = cas_y) { translate([0, y, cas_lift + 9.525]) sphere(d = 19.05, $fn = 32); translate([0, y, cas_seat - 12]) cylinder(d1 = 16, d2 = 22.9, h = 12); }
    if (w == "aa_holder")   translate([-aa[0]/2, tub_fy(0) + 0.5, tray_z + 2]) cube(aa);
    if (w == "switch")      translate([sw_c[0] - sw_cut[0]/2, tub_fy(sw_c[0], 0) - 2, sw_c[1] - sw_cut[1]/2]) cube([sw_cut[0], 24, sw_cut[1]]);
    if (w == "boards")      deck_dummies();
    if (w == "charge_socket") usbc_socket5();
}
if (part == "assy5") assy5(which);

echo(str("LittleBot v5: total height ", hood_c + hood_ab[1], " mm; base ", 2 * T5[0], " x ", 2 * T5[1], "; ground clearance ", z_fl, "; wheel track up to ", 2 * xf + 89 - wh_w,
         "; the body leaves v4's egg at z ", ztw, " (sides) / ", ztd, " (front, back)"));
