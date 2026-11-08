// Interference checks for v3 (run_checks_v3.sh). PASS = empty (or zero-volume contact).
include <littlebot_v3.scad>
part = "none";
check = "";
s = 0.25;            // dummies shrunk by this much: touching faces are allowed
grow = 12;           // finger-pinch margin for the arms vs the head
module servo_shrunk() {
    translate([-sv_shaft_off + s, -sv_w/2 + s, -sv_spline_h + s]) cube([sv_l - 2*s, sv_w - 2*s, sv_case_h - 2*s]);
    translate([sv_mid - sv_tab_l/2 + s, -sv_w/2 + s, -sv_spline_h + sv_tab_z + s]) cube([sv_tab_l - 2*s, sv_w - 2*s, sv_tab_t - 2*s]);
    translate([0, 0, -sv_spline_h + sv_case_h]) cylinder(d = sv_boss_d - 0.4 - 2*s, h = sv_boss_h - sv_case_h - s);
    translate([sv_l - sv_shaft_off - 4, -2, -sv_spline_h + 1.5]) cube([10, 4, 4]);        // cable exit
}
module arm_servo_s(k) place_sh(k) translate([sv_spline_top, 0, 0]) rotate([0, 90, 0]) rotate(90) servo_shrunk();
module head_servo_s() place_torso() translate([0, pinion_yt, hs_spline_z]) servo_shrunk();
module speaker_s() translate([-spk_w/2 + s, spk3_c[1] + s, spk3_c[2] - spk_h/2 + s]) cube([spk_w - 2*s, spk_d - 2*s, spk_h - 2*s]);
module mic_s() { translate([mic3[0] + s, mic3[1] + s, mic3[2] - mic_w/2 + s]) cube([mic_l - 2*s, mic_t - 2*s, mic_w - 2*s]);
                 translate([mic3[0] + mic_body + s, mic3[1] + mic_t/2 - 4.75, mic3[2] - 9]) cube([32, 9.5, 18]); }   // + extension socket
module base_electronics_s() { ups_stack(s); ups_plugs(s); pca_floor(s); usbc_socket(s); charge_cable(s); }
module floor_parts() { base_floor(false); stack_keeper(); pca_clamp(0); pca_clamp(1); }
module neck_static() place_torso() { translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }
module neck_moving(a) translate([0, neck_y, 0]) rotate(a) translate([0, -neck_y, 0]) place_torso() {
    translate([0, neck_yt, torso_h + s]) coupler(); translate([0, neck_yt, gear_z0]) ring_gear(); }
module arms_at(a) for (k = [-1, 1]) place_arm3(k, a) v3_arm();
module arm_grown(g) minkowski() { v3_arm(); sphere(r = g, $fn = 8); }
module arms_grown_at(a) for (k = [-1, 1]) place_arm3(k, a) arm_grown(grow);
module head_all(a) place_head3(a) head_v3();
// the rod's path when pressed 1 mm (it must reach nothing but the button)
module rod_pressed() translate([1.0, 0, 0]) v3_pi_rod();

if (check == "body_vs_electronics")  intersection() { v3_body(); union() { base_electronics_s(); speaker_s(); mic_s(); arm_servo_s(-1); arm_servo_s(1); } }
if (check == "body_vs_floor")        intersection() { v3_body(); floor_parts(); }
if (check == "body_vs_plate")        intersection() { v3_body(); v3_neck_plate(); }
if (check == "plate_vs_neck")        intersection() { v3_neck_plate(); union() { neck_moving(head_angle); head_servo_s(); place_torso() translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); } }
if (check == "frame_vs_plate")       intersection() { v3_neck_plate(); place_torso() translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }
if (check == "body_vs_neck")         intersection() { v3_body(); union() { neck_moving(head_angle); neck_static(); head_servo_s(); } }
if (check == "rod_vs_parts")         intersection() { rod_pressed(); union() { v3_body(); floor_parts(); base_electronics_s(); } }
if (check == "rod_reaches_button")   intersection() { rod_pressed(); translate(pib) cube([1.2, 4, 4], center = true); }   // must NOT be empty
if (check == "arms_vs_body")         intersection() { v3_body(); arms_at(arm_angle); }
if (check == "arms_vs_plate")        intersection() { union() { v3_neck_plate(); neck_moving(head_angle); }; arms_at(arm_angle); }
if (check == "pinch_arm_head")       intersection() { head_all(head_angle); arms_grown_at(arm_angle); }
if (check == "head_vs_body")         intersection() { union() { v3_body(); v3_neck_plate(); }; head_all(head_angle); }
if (check == "selftest_must_fail")   intersection() { v3_body(); translate([80, 60, 30]) cube(10); }
