// Interference checks for the LittleBot enclosure.
// Each check renders the INTERSECTION of two things that must not touch.
// An empty result ("Current top level object is empty") means PASS.
//   openscad -o /tmp/x.stl -D 'check="servos_vs_shell"' checks.scad
include <littlebot_enclosure.scad>
part = "none";
detail = false;
$fn = 32;
check = "";
head_angle = 0;     // head rotation for the sweep checks
arm_angle = 0;      // arm rotation for the sweep checks

s = 0.25;           // shrink dummies by this much: touching faces are allowed

module servo_shrunk() {
    translate([-sv_shaft_off + s, -sv_w/2 + s, -sv_spline_h + s]) cube([sv_l - 2*s, sv_w - 2*s, sv_case_h - 2*s]);
    translate([sv_mid - sv_tab_l/2 + s, -sv_w/2 + s, -sv_spline_h + sv_tab_z + s]) cube([sv_tab_l - 2*s, sv_w - 2*s, sv_tab_t - 2*s]);
    translate([0, 0, -sv_spline_h + sv_case_h]) cylinder(d = sv_boss_d - 0.4 - 2*s, h = sv_boss_h - sv_case_h - s);
}
module head_servo_s() { translate([0, pinion_y, hs_spline_z]) servo_shrunk(); }
module arm_servos_s() { for (k = [-1, 1]) translate([k * as_spline_x, shoulder_y, shoulder_z]) rotate(k > 0 ? [90, 0, 90] : [-90, 0, 90]) servo_shrunk(); }
module speaker_s() { translate([-spk_w/2 + s, y_in0 + 1 + s, z_base_top + s]) cube([spk_w - 2*s, spk_d - 2*s, spk_h - 2*s]); }
module pca_s() { translate([pca_wall_x + 6.5 + s, pca_y0 + s, pca_z0 + s]) cube([1.6 + 11, pca_l - 2*s, pca_w - 2*s]); }  // board + parts
module camera_s() {
    translate([-cam_board/2 + s, cam_board_y + s, cam_z - cam_board/2 + s]) cube([cam_board - 2*s, 1.6 + 8, cam_board - 2*s]);
    translate([0, cam_board_y, cam_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - wall - 0.6);
    translate([-6, cam_board_y - 10, cam_z - 16]) cube([16, 10, 8]);                       // 5-pin connector + plug (lens side)
}
// screen + Pi stack, cooler, plugs (littlebot_enclosure.scad, head coordinates), shrunk by s
module stack_s() { screen_dummy(s); pi_dummy(s); cooler_dummy(s); stack_plugs(s); }
module electronics_s() { head_servo_s(); arm_servos_s(); speaker_s(); pca_s(); camera_s(); }
module moving_neck() { rotate_about_neck(head_angle) { place_coupler() translate([0, 0, s]) coupler(); place_ring() ring_gear(); } }
module rotate_about_neck(a) { translate([0, neck_y, 0]) rotate(a) translate([0, -neck_y, 0]) children(); }
module arm_at(k, a) { translate([k * arm_inner_x, shoulder_y, shoulder_z]) rotate([-a, 0, 0]) if (k > 0) rotate([0, 90, 0]) children(); else rotate([0, -90, 0]) rotate([0, 0, 180]) children(); }

if (check == "electronics_vs_shell")  intersection() { body_shell(); electronics_s(); }
if (check == "electronics_vs_base")   intersection() { translate([0, 0, -s]) base_plate(); union() { head_servo_s(); arm_servos_s(); speaker_s(); camera_s(); } }
if (check == "electronics_overlap")   union() {
    intersection() { head_servo_s(); union() { arm_servos_s(); pca_s(); camera_s(); } }
    intersection() { arm_servos_s(); union() { pca_s(); camera_s(); speaker_s(); } }
    intersection() { pca_s(); union() { camera_s(); speaker_s(); } }
    intersection() { camera_s(); speaker_s(); }
}
if (check == "neck_vs_shell")         intersection() { body_shell(); moving_neck(); }
if (check == "pinion_vs_shell")       intersection() { body_shell(); place_pinion() pinion(); }
if (check == "frame_vs_servo")        intersection() { place_frame() servo_frame(); head_servo_s(); }
if (check == "frame_vs_parts")        intersection() { place_frame() servo_frame(); union() { place_pinion() pinion(); place_ring() ring_gear(); } }
if (check == "camera_vs_shell")       intersection() { body_shell(); place_camera() camera_bracket(); }
if (check == "arms_vs_body")          intersection() { body_shell(); for (k = [-1, 1]) arm_at(k, arm_angle) arm(); }
if (check == "arms_vs_head")          intersection() { rotate_about_neck(head_angle) place_head() head_shell(); for (k = [-1, 1]) arm_at(k, arm_angle) arm(); }
if (check == "head_vs_body")          intersection() { body_shell(); rotate_about_neck(head_angle) translate([0, 0, s]) place_head() head_shell(); }
// the screen + Pi stack and everything plugged into it vs the head (ribs, posts) and the Pi carrier (head coordinates)
if (check == "stack_vs_head")         intersection() { union() { head_shell(); pi_carrier(); } stack_s(); }
if (check == "carrier_vs_head")       intersection() { head_shell(); translate([0, s, 0]) pi_carrier(); }
// the Pi's M2.5 screws (heads on the component side) clear the screen, its socket and the cooler
if (check == "pi_screws_clear")       intersection() { pi_screws(); union() { screen_dummy(); cooler_dummy(); pbox(7.13, 57.93, 50.02, 55.0, 0, 8.57); pbox(69.5, pi_l + 3, 2, 54, 0, 15.8); } }
// parts of the stack don't run into each other (plugs, HDMI adapter, SD card, cooler vs Pi)
if (check == "stack_internal")        union() {
    intersection() { screen_dummy(s); union() { pi_dummy(s); stack_plugs(s); } }
    intersection() { pi_dummy(s); union() { stack_plugs(s); cooler_dummy(s); } }
    intersection() { cooler_dummy(s); stack_plugs(s); }
    intersection() { hdmi_zone(s); union() { for (z = [29.1, 47.0]) pbox(pi_l + 3, pi_l + 28, z - 7, z + 7, 0, 15.8, s); pbox(11.2 - 6.25, 11.2 + 6.25, -31, -0.5, -2.15, 5.35, s); } } }
// INFO (parts compatibility, not the enclosure): the Active Cooler under the screen, from the drawings
if (check == "info_cooler_vs_screen") intersection() { screen_dummy(s); cooler_dummy(s); }
if (check == "gears_mesh_clear")      intersection() { place_pinion() pinion(); place_ring() ring_gear(); }
// Self-test: this MUST report an overlap (proves the checks can fail).
if (check == "selftest_must_fail")    intersection() { body_shell(); translate([60, 60, 40]) cube(10); }
