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
module pi_s() {
    translate([pi_x0 + s, pi_y1 - 56 + s, z_base_top + pi_standoff + s]) cube([85 - 2*s, 56 - 2*s, 1.6 - 2*s]);
    translate([pi_x0 + 85 - 21, pi_y1 - 56 + 2, z_base_top + pi_standoff + 1.6]) cube([21 + 2, 53, 16]);
    translate([pi_x0 + 2, pi_y1 - 48.5, z_base_top + pi_standoff + 1.6]) cube([63.5, 42.5, 14.5]);   // official Active Cooler (63.5 x 42.5 x 13.7 + pad)
    // plugs (USB-C power + two micro-HDMI)
    translate([pi_usbc_x() - 4.5, pi_y1 - 7, z_base_top + pi_standoff + 1.6]) cube([9, 22, 3.2]);
    for (hx = [25.8, 39.2]) translate([pi_x0 + hx - 4, pi_y1 - 5, z_base_top + pi_standoff + 1.6]) cube([8, 24, 4]);
    // USB-A plugs on the right edge (mic dongle, speaker, camera): 35 mm long
    translate([pi_x0 + 85, pi_y1 - 54, z_base_top + pi_standoff + 1.6]) cube([33, 50, 16]);
}
module speaker_s() { translate([-spk_w/2 + s, y_in0 + 1 + s, z_base_top + s]) cube([spk_w - 2*s, spk_d - 2*s, spk_h - 2*s]); }
module pca_s() { translate([pca_wall_x + 6.5 + s, pca_y0 + s, pca_z0 + s]) cube([1.6 + 11, pca_l - 2*s, pca_w - 2*s]); }  // board + parts
module camera_s() {
    translate([-cam_board/2 + s, cam_board_y + s, cam_z - cam_board/2 + s]) cube([cam_board - 2*s, 1.6 + 8, cam_board - 2*s]);
    translate([0, cam_board_y, cam_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - wall - 0.6);
}
module electronics_s() { head_servo_s(); arm_servos_s(); pi_s(); speaker_s(); pca_s(); camera_s(); }
module moving_neck() { rotate_about_neck(head_angle) { place_coupler() translate([0, 0, s]) coupler(); place_ring() ring_gear(); } }
module rotate_about_neck(a) { translate([0, neck_y, 0]) rotate(a) translate([0, -neck_y, 0]) children(); }
module arm_at(k, a) { translate([k * arm_inner_x, shoulder_y, shoulder_z]) rotate([-a, 0, 0]) if (k > 0) rotate([0, 90, 0]) children(); else rotate([0, -90, 0]) rotate([0, 0, 180]) children(); }

if (check == "electronics_vs_shell")  intersection() { body_shell(); electronics_s(); }
if (check == "electronics_vs_base")   intersection() { translate([0, 0, -s]) base_plate(); union() { head_servo_s(); arm_servos_s(); speaker_s(); camera_s(); } }
if (check == "pi_vs_base")            intersection() { base_plate(); translate([0, 0, s]) pi_s(); }
if (check == "electronics_overlap")   union() {
    intersection() { head_servo_s(); union() { arm_servos_s(); pi_s(); pca_s(); camera_s(); } }
    intersection() { arm_servos_s(); union() { pi_s(); pca_s(); camera_s(); speaker_s(); } }
    intersection() { pi_s(); union() { pca_s(); camera_s(); speaker_s(); } }
    intersection() { camera_s(); speaker_s(); }
}
if (check == "neck_vs_shell")         intersection() { body_shell(); moving_neck(); }
if (check == "pinion_vs_shell")       intersection() { body_shell(); place_pinion() pinion(); }
if (check == "frame_vs_servo")        intersection() { place_frame() servo_frame(); head_servo_s(); }
if (check == "frame_vs_parts")        intersection() { place_frame() servo_frame(); union() { place_pinion() pinion(); place_ring() ring_gear(); pi_s(); } }
if (check == "camera_vs_shell")       intersection() { body_shell(); place_camera() camera_bracket(); }
if (check == "arms_vs_body")          intersection() { body_shell(); for (k = [-1, 1]) arm_at(k, arm_angle) arm(); }
if (check == "arms_vs_head")          intersection() { rotate_about_neck(head_angle) place_head() head_shell(); for (k = [-1, 1]) arm_at(k, arm_angle) arm(); }
if (check == "head_vs_body")          intersection() { body_shell(); rotate_about_neck(head_angle) translate([0, 0, s]) place_head() head_shell(); }
if (check == "display_vs_head")       intersection() { head_shell(); translate([0, 0.05, 0]) {
    translate([lcd_x0 + s, head_wall + s, lcd_z0 + s]) cube([lcd_w - 2*s, 5.7 - 2*s, lcd_h - 2*s]);
    translate([lcd_x0 + s, head_wall + 5.7 + s, lcd_z0 - pcb_overhang + s]) cube([75 - 2*s, 1.6 - 2*s, lcd_h + 2*pcb_overhang - 2*s]);
    translate([lcd_x0 + 32 - 10.5, head_wall + 7.32, lcd_z0 - pcb_overhang - 31]) cube([21, 12, 31]); } }
if (check == "gears_mesh_clear")      intersection() { place_pinion() pinion(); place_ring() ring_gear(); }
// Self-test: this MUST report an overlap (proves the checks can fail).
if (check == "selftest_must_fail")    intersection() { body_shell(); translate([60, 60, 40]) cube(10); }
