// Interference checks for LittleBot v2.
// Each check renders the INTERSECTION of two things that must not touch.
// An empty result ("Current top level object is empty") means PASS.
//   openscad -o /tmp/x.stl -D 'check="electronics_vs_torso"' checks.scad
include <littlebot_v2.scad>
part = "none";
detail = false;
$fn = 32;
check = "";
head_angle = 0;     // head rotation (degrees) for the sweep checks
arm_angle = 0;      // arm rotation (degrees, + = forwards/up) for the sweep checks

s = 0.25;           // shrink dummies by this much: touching faces are allowed

module servo_shrunk() {
    translate([-sv_shaft_off + s, -sv_w/2 + s, -sv_spline_h + s]) cube([sv_l - 2*s, sv_w - 2*s, sv_case_h - 2*s]);
    translate([sv_mid - sv_tab_l/2 + s, -sv_w/2 + s, -sv_spline_h + sv_tab_z + s]) cube([sv_tab_l - 2*s, sv_w - 2*s, sv_tab_t - 2*s]);
    translate([0, 0, -sv_spline_h + sv_case_h]) cylinder(d = sv_boss_d - 0.4 - 2*s, h = sv_boss_h - sv_case_h - s);
    translate([sv_l - sv_shaft_off - 4, -2, -sv_spline_h + 1.5]) cube([10, 4, 4]);        // cable exit
}
// --- torso electronics (torso coordinates) ---
module head_servo_s() { translate([0, pinion_yt, hs_spline_z]) servo_shrunk(); }
module arm_servos_s() { for (k = [-1, 1]) translate([k * as_spline_x, shoulder_yt, shoulder_zt]) rotate(k > 0 ? [90, 0, 90] : [-90, 0, 90]) servo_shrunk(); }
module pi_s() {
    yb = pi_spine_y + pi_standoff;
    translate([-28 + s, yb + s, pi_z_top - 85 + s]) cube([56 - 2*s, 1.6 - 2*s, 85 - 2*s]);                 // board
    translate([-20, yb + 1.6, pi_z_top - 60]) cube([40, 12, 34]);                                     // active cooler
    translate([-27, yb + 1.6, pi_z_top - 85 - 2]) cube([53, 16, 21]);                                 // USB + Ethernet block
    translate([-25, yb + 1.6, pi_z_top - 85 - 36]) cube([50, 14, 36]);                                // USB plugs (pointing down)
    for (px = [11.2, 25.8, 39.2]) translate([28, yb + 1.6, pi_z_top - px - 4]) cube([24, 4, 8]);     // USB-C + 2 micro-HDMI plugs
    translate([-26.8, yb + 1.6, pi_z_top - 58]) cube([5.2, 22.5, 51]);                                // GPIO header + jumper wires
}
module speaker_s() { translate([-spk_w/2 + s, spk_y + s, spk_z + s]) cube([spk_w - 2*s, spk_d - 2*s, spk_h - 2*s]); }
module torso_electronics_s() { head_servo_s(); arm_servos_s(); pi_s(); speaker_s(); }

// --- head electronics (head coordinates) ---
module display_s() {
    translate([lcd_x0 + s, visor_back_y + 0.05 + s, lcd_z0 + s]) cube([lcd_w - 2*s, 5.7 - 2*s, lcd_h - 2*s]);
    translate([lcd_x0 + s, visor_back_y + 5.7 + s, lcd_z0 - pcb_overhang + s]) cube([75 - 2*s, 1.6 - 2*s, lcd_h + 2*pcb_overhang - 2*s]);
    translate([lcd_x0 + 32 - 10.5, visor_back_y + 7.32, lcd_z0 - pcb_overhang - 31]) cube([21, 12, 31]);   // straight HDMI plug
    translate([lcd_x0 + 2, visor_back_y + 7.32, lcd_z1 - 10]) cube([34, 9, 10]);                        // 26-pin header + jumpers
}
module camera_s() {
    translate([-cam_board/2 + s, cam_board_y + s, cam_z - cam_board/2 + s]) cube([cam_board - 2*s, 1.6 - 2*s, cam_board - 2*s]);
    translate([0, cam_board_y, cam_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - pod_y0 - 2.2 - 0.3);    // lens
    translate([-6, cam_board_y + 1.6, cam_z - 12]) cube([12, 6, 8]);                                                     // cable connector
}

// --- base electronics (robot coordinates) ---
module bank_s()  { translate([-bat_w/2 + 1 + s, bat_y1 - 1 - 96 + s, base_z0 + base_floor_t + s]) cube([bat_w - 2 - 2*s, 96 - 2*s, 26 - 2*s]); }
module pca_s()   { translate([-30, 40, base_z0 + base_floor_t + bat_h + 2.5 + s]) cube([62.5, 25.4, 12]); }
module sonar_s() { translate([-22.5, 12 - bezel_t + s, front_z + 4 - 10]) cube([45, 1.6 + 4, 20]);           // board + parts on its back
                   translate([-5.5, 12 - bezel_t + s, front_z + 4 - 10]) cube([11, 1.6 + 20, 4.5]);       // header + straight dupont plug
                   for (k = [-1, 1]) translate([k * sonar_sp/2, -bezel_t + 0.5, front_z + 4]) rotate([-90, 0, 0]) cylinder(d = 16, h = 12); }
// mobile phase: the wheels stick out 8 mm below the skirt
module wheels_s() { for (k = [-1, 1]) translate([k * wheel_x, wheel_y, wheel_d/2 - 8]) rotate([0, 90, 0]) cylinder(d = wheel_d + 2, h = wheel_w + 1, center = true); }

// --- moving assemblies ---
module rotate_about_neck(a) { translate([0, neck_y, 0]) rotate(a) translate([0, -neck_y, 0]) children(); }
module head_all() { place_head() { head_shell(); head_back(); visor(); ear(); mirror([1, 0, 0]) ear(); camera_pod(); } }
module moving_neck() { place_torso() translate([0, neck_yt, 0]) rotate(head_angle) translate([0, -neck_yt, 0]) {
    translate([0, neck_yt, torso_h + s]) coupler(); translate([0, neck_yt, gear_z0]) ring_gear(); } }
module arms_at(a) { for (k = [-1, 1]) place_arm(k, a); }
module base_parts() { base_shell(); base_floor(false); translate([0, 0, base_z0 + base_floor_t + bat_h]) shelf(); }

if (check == "electronics_vs_torso")  intersection() { torso_shell(); torso_electronics_s(); }
if (check == "electronics_overlap")   place_torso() union() {
    intersection() { head_servo_s(); union() { arm_servos_s(); pi_s(); speaker_s(); } }
    intersection() { arm_servos_s(); union() { pi_s(); speaker_s(); } }
    intersection() { pi_s(); speaker_s(); } }
if (check == "torso_parts_vs_electronics") intersection() { union() { translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    translate([0, neck_yt, gear_z0]) ring_gear(); translate([0, pinion_yt, gear_z0]) pinion(); translate([-46 + clr, torso_d - torso_wall, 0]) torso_back(); }
    union() { arm_servos_s(); pi_s(); speaker_s(); } }
if (check == "frame_vs_servo")        intersection() { translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); head_servo_s(); }
if (check == "pinion_vs_torso")       intersection() { torso_shell(); translate([0, pinion_yt, gear_z0]) pinion(); }
if (check == "gears_mesh_clear")      intersection() { translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); translate([0, neck_yt, gear_z0]) ring_gear(); }
if (check == "back_cover_vs_torso")   intersection() { torso_shell(); translate([0, s, 0]) translate([-46 + clr, torso_d - torso_wall, 0]) torso_back(); }
if (check == "neck_vs_torso")         intersection() { place_torso() torso_shell(); moving_neck(); }
if (check == "head_vs_torso")         intersection() { place_torso() torso_shell(); translate([0, 0, s]) rotate_about_neck(head_angle) head_all(); }
if (check == "display_vs_head")       intersection() { place_head() union() { head_shell(); visor(); } place_head() display_s(); }
if (check == "camera_vs_head")        intersection() { place_head() union() { head_shell(); camera_pod(); translate([0, cam_board_y - 2.5, cam_z]) camera_bracket(); } place_head() camera_s(); }
if (check == "bracket_vs_pod")        intersection() { place_head() union() { head_shell(); camera_pod(); } place_head() translate([0, cam_board_y - 2.5, cam_z]) camera_bracket(); }
if (check == "head_parts_overlap")    place_head() union() {
    intersection() { head_shell(); translate([0, -s, 0]) visor(); }
    intersection() { head_shell(); translate([0, s, 0]) head_back(); }
    intersection() { head_shell(); for (k = [-1, 1]) translate([k * s, 0, 0]) mirror([k < 0 ? 1 : 0, 0, 0]) ear(); }
    intersection() { head_shell(); translate([0, 0, s]) camera_pod(); } }
if (check == "torso_vs_base")         intersection() { base_shell(); translate([0, 0, s]) place_torso() torso_shell(); }
if (check == "base_parts_overlap")    union() {
    intersection() { base_shell(); translate([0, 0, -s]) base_floor(false); }
    intersection() { base_floor(false); translate([0, 0, base_z0 + base_floor_t + bat_h + s]) shelf(); }
    intersection() { base_shell(); translate([0, 0, base_z0 + base_floor_t + bat_h]) shelf(); }
    intersection() { union() { base_shell(); base_floor(false); } for (k = [-1, 1]) translate([k * s, 0, 0]) place_well_cover(k) well_cover(); }
    intersection() { union() { base_shell(); base_floor(false); } translate([0, -s, 0]) place_front_plate() port_plate(front_w, front_h, "sonar"); }
    intersection() { union() { base_shell(); base_floor(false); } translate([0, -s, 0]) place_front_plate() port_plate(front_w, front_h, "blank"); }
    intersection() { union() { base_shell(); base_floor(false); } translate([0, s, 0]) place_rear_plate() port_plate(rear_w, rear_h, "rear"); } }
if (check == "electronics_vs_base")   intersection() { union() { base_parts(); place_torso() torso_shell(); }
    union() { bank_s(); pca_s(); place_torso() pi_s(); } }
if (check == "sonar_vs_base")         intersection() { union() { base_parts(); place_front_plate() port_plate(front_w, front_h, "sonar"); } sonar_s(); }
if (check == "wheels_vs_mobile_base") intersection() { union() { base_shell(); base_floor(true); place_torso() torso_shell(); } wheels_s(); }
if (check == "arms_vs_torso")         intersection() { place_torso() torso_shell(); arms_at(arm_angle); }
if (check == "arms_vs_base")          intersection() { base_shell(); arms_at(arm_angle); }
if (check == "arms_vs_head")          intersection() { rotate_about_neck(head_angle) head_all(); arms_at(arm_angle); }
if (check == "arms_vs_neck")          intersection() { place_torso() translate([0, neck_yt, torso_h]) cylinder(r = coupler_r + 1, h = bearing_h + coupler_h); arms_at(arm_angle); }
// Self-test: this MUST report an overlap (proves the checks can fail).
if (check == "selftest_must_fail")    intersection() { torso_shell(); translate([50, 50, 40]) cube(10); }

// Finger-pinch checks: the arm grown by `grow` mm. Empty = the arm stays at least `grow` mm away.
// (Gaps between 5 and 12 mm that close as a part moves can trap a child's finger.)
grow = 12;
module arm_grown(g) { translate([0, 0, -g]) linear_extrude(arm_t + 1 + 2*g) offset(r = g) {
    circle(r = 17); seg2d_capsule([0, 0], elbow, 11); seg2d_capsule(elbow, hand - [5, 3], 10); translate(hand) scale([1, 1.1]) circle(r = 11.5); } }
module arms_grown_at(a) { for (k = [-1, 1]) translate([k * arm_inner_x, torso_y0 + shoulder_yt, torso_z0 + shoulder_zt]) rotate([-a, 0, 0])
    if (k < 0) multmatrix([[0, 0, -1, 0], [0, -1, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]) arm_grown(grow);
    else       multmatrix([[0, 0, 1, 0], [0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 0, 1]]) mirror([0, 1, 0]) arm_grown(grow); }
// the torso is excluded: the arm's inner face runs parallel to it at a constant 3 mm gap
if (check == "pinch_arm_head")        intersection() { rotate_about_neck(head_angle) head_all(); arms_grown_at(arm_angle); }
if (check == "pinch_arm_base")        intersection() { base_shell(); arms_grown_at(arm_angle); }
