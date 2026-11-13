// Interference checks for v5 "Rover" (run_checks_v5.sh). PASS = empty (or zero-volume contact).
include <littlebot_v5.scad>
part = "none";
check = "";
detail = false;
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
module mic_s() place_rsl() { rsl_board(s); rsl_plug(s); }
module speaker_s() translate([-spk_w/2 + s, spk3_c[1] + s, spk3_c[2] - spk_h/2 + s]) cube([spk_w - 2*s, spk_d - 2*s, spk_h - 2*s]);
module stack_s() place_stack() { ups_stack(s); ups_plugs(s); }
module keeper5() place_stack() stack_keeper();
module motors_s() for (k = [-1, 1]) motor_dummy(k, s);
// the wheel wherever it can be on its coupler, its screw's head beyond it
module wheels_s() for (k = [-1, 1]) translate([k * (wh_x[0] + s), yw, az]) rotate([0, k * 90, 0]) { cylinder(d = wh_d - 2*s, h = wh_x[1] - wh_x[0] - 2*s); cylinder(d = 9, h = wh_x[1] - wh_x[0] + 3.5 - 2*s); }
module casters_s() for (y = cas_y) { translate([0, y, cas_seat - 12]) cylinder(d1 = 16, d2 = 22.9 - 2*s, h = 12 - s);
    for (k = [-1, 1]) { translate([k * 7.75, y, cas_seat - 2]) cylinder(d = 2.2, h = 2 + 8.9, $fn = 12); translate([k * 7.75, y, cas_top + s]) cylinder(d = 5.4, h = 1.8, $fn = 6); } }   // its screws and nuts
module aa_s() translate([-aa[0]/2 + s, aa_y0 + s, tray_z + 2 + s]) cube(aa - [2*s, 2*s, 2*s]);
module switch_s() switch_dummy(s);   // its body, its flange on the wall's flat seat
module base_stuff() { stack_s(); motors_s(); wheels_s(); casters_s(); aa_s(); switch_s(); usbc_socket5(s); deck_dummies(s); }
module pods() for (k = [-1, 1]) place_pod(k) v5_pod();
module neck_static() place_torso() { translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }
module neck_moving(a) place_head3(a) { v5_ring_gear(); v5_hub(); v5_neck(); }
module arms_at(a) for (k = [-1, 1]) place_arm3(k, a) sa_arm();
module arm_grown(g) minkowski() { sa_arm(); sphere(r = g, $fn = 8); }
module arms_grown_at(a) for (k = [-1, 1]) place_arm3(k, a) arm_grown(grow);
module head_all(a) place_head3(a) { sa_head_frame_body(); sa_head_back(); v5_neck(); }
module slider_positions() for (st = [-1, 1]) translate([st * ups_knob[2], 0, 0]) v5_slider();
module spk_keeper_rigid(dx = 0) difference() { sa_spk_keeper(dx); translate([dx - spk_w/2 - 0.3 + spk_k_gap - 6, 0, spk3_c[2] - spk_k_hz + 3.45]) cube([6 + spk_k_leaf[0] + 0.3, 60, spk_k_leaf[1] + 2]); }
// screws: a shank from under its head (d wide, len long, along the frame's -z) and its head; an M3 nut (shrunk)
module shank(len, d = 2.9) { translate([0, 0, -len]) cylinder(d = d, h = len, $fn = 12); cylinder(d = 5.6, h = 3, $fn = 16); }
module nut_s() cylinder(d = (nut_af - 2*s) / cos(30), h = nut_t - 2*s, $fn = 6, center = true);
module joint_screws() for (j = js, k = [-1, 1]) place_js(j, k) translate([0, 0, -1.2]) shank(10);
module joint_nuts(d = 0) for (j = js, k = [-1, 1]) place_js(j, k) translate([0, 0, -(tw + tg[0] + 3 + 0.2 + nut_t / 2) + d]) rotate(30) nut_s();
module cap_screws() for (p = cap_scr) translate([p[0], p[1], zc0 + 2.8]) shank(12);
module cap_nuts(d = 0) for (p = cap_scr) translate([p[0], p[1], zc0 - 3.5 - 0.2 - nut_t / 2 + d]) rotate(cap_side(p) == 0 ? 0 : 30) nut_s();
// (shanks only: the rear one's head sits 1.2 mm higher, on the head's old AA seat; it still passes its nut by 1 mm)
module head_screws() translate([0, neck_y, 0]) for (a = head4_scr) rotate(a) translate([head_screw_r, 0, hd_z0 + 4 - 20]) cylinder(d = 2.9, h = 20, $fn = 12);
module head_nuts(d = 0) translate([0, neck_y, 0]) for (a = head4_scr) rotate(a) translate([head_screw_r, 0, hub_nut_z + 0.2 + nut_t / 2 + d]) nut_s();

// --- the base ---
if (check == "tub_vs_stuff")        intersection() { v5_tub(); base_stuff(); }
if (check == "tub_vs_parts")        intersection() { v5_tub(); union() { pods(); v5_tray(); v5_deck(); keeper5(); } }          // contact only
if (check == "pods_vs_stuff")       intersection() { pods(); union() { stack_s(); wheels_s(); casters_s(); aa_s(); switch_s(); deck_dummies(s); for (k = [-1, 1]) motor_dummy(k, s); } }
if (check == "pods_vs_parts")       intersection() { pods(); union() { v5_tray(); v5_deck(); keeper5(); } }
if (check == "tray_vs_stuff")       intersection() { v5_tray(); union() { stack_s(); motors_s(); wheels_s(); casters_s(); aa_s(); switch_s(); deck_dummies(s); } }
if (check == "tray_vs_parts")       intersection() { v5_tray(); union() { v5_deck(); keeper5(); } }
if (check == "tray_slides_out")     intersection() { for (d = [10, 30, 50]) translate([0, -d, 0]) { v5_tray(); aa_s(); } union() { v5_tub(); v5_deck(); casters_s(); pods(); switch_s(); } }
if (check == "deck_vs_stuff")       intersection() { v5_deck(); union() { stack_s(); motors_s(); wheels_s(); casters_s(); aa_s(); switch_s(); deck_dummies(s); } }
if (check == "deck_vs_keeper")      intersection() { v5_deck(); keeper5(); }
if (check == "keeper_fit")          intersection() { keeper5(); union() { v5_tub(); place_stack() { ups_stack(0); for (i = [0, 2, 4, 5]) ups_plug(i, 0); } v5_slider(); } }
if (check == "keeper_rests")        intersection() { place_stack() { translate([0, 0, -0.2]) keeper4_arms(); ups_stack(0); } }      // must NOT be empty
if (check == "stuff_vs_stuff")      intersection() { stack_s(); union() { motors_s(); wheels_s(); casters_s(); aa_s(); switch_s(); deck_dummies(s); usbc_socket5(s); } }
if (check == "motor_screws_clear")  intersection() { pods(); for (k = [-1, 1], q = [-1, 1]) translate([k * (xf + pod_t), yw + q * mot_holes / 2, az]) rotate([0, k * 90, 0]) shank(8); }
// --- the body on the tub ---
if (check == "body_vs_tub")         intersection() { v5_body(); v5_tub(); }                                                     // contact only (the rim's land)
if (check == "body_vs_base")        intersection() { v5_body(); union() { base_stuff(); pods(); v5_tray(); v5_deck(); keeper5(); } }
if (check == "body_goes_on")        intersection() { for (d = [4, 10, 25]) translate([0, 0, d]) v5_body(); union() { v5_tub(); stack_s(); pods(); v5_tray(); v5_deck(); keeper5(); deck_dummies(s); } }
if (check == "joint_screws_clear")  intersection() { union() { v5_body(); v5_tub(); pods(); v5_deck(); stack_s(); keeper5(); } union() { joint_screws(); joint_nuts(); } }
if (check == "joint_nuts_held")     intersection() { v5_body(); joint_nuts(1.2); }                                              // must NOT be empty: tongue in front of each nut
if (check == "joint_nuts_go_in")    intersection() { v5_body(); for (j = js, k = [-1, 1], d = [3, 6, 9]) place_js(j, k) translate([0, -d, -(tw + tg[0] + 3 + 0.2 + nut_t / 2)]) rotate(30) nut_s(); }
if (check == "body_vs_electronics") intersection() { v5_body(); union() { mic_s(); speaker_s(); arm_servo_s(-1); arm_servo_s(1); } }
if (check == "body_vs_cap")         intersection() { v5_body(); v5_cap(); }                                                     // contact only
if (check == "cap_vs_neck")         intersection() { v5_cap(); union() { neck_moving(head_angle); neck_static(); head_servo_s(); } }
if (check == "body_vs_neck")        intersection() { v5_body(); union() { neck_moving(head_angle); neck_static(); head_servo_s(); } }
if (check == "neck_goes_in")        intersection() { v5_body(); for (d = [5, 15, 30]) translate([0, 0, d]) { v5_cap(); neck_moving(0); neck_static(); head_servo_s(); } }
if (check == "chest_vs_neck")       intersection() { union() { speaker_s(); mic_s(); arm_servo_s(-1); arm_servo_s(1); } union() { neck_static(); head_servo_s(); neck_moving(head_angle); v5_cap(); } }
if (check == "frame_on_pillars")    intersection() { v5_cap(); place_torso() translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }   // contact only
if (check == "hub_vs_neck")         intersection() { v5_hub(); v5_neck(); }
if (check == "hub_vs_gears")        intersection() { place_head3(head_angle) { v5_hub(); v5_neck(); } union() { place_head3(head_angle) v5_ring_gear(); neck_static(); head_servo_s(); } }
if (check == "head_vs_neck")        intersection() { union() { sa_head_frame_body(); sa_head_back(); } union() { v5_neck(); v5_hub(); } }   // contact only
if (check == "head_screws_clear")   intersection() { union() { v5_hub(); v5_neck(); sa_head_frame_body(); } union() { head_screws(); head_nuts(); } }
if (check == "head_nuts_held")      intersection() { v5_hub(); head_nuts(1.2); }                                                // must NOT be empty
if (check == "cap_screws_clear")    intersection() { union() { v5_body(); v5_cap(); neck_moving(0); neck_static(); head_servo_s(); arm_servo_s(-1); arm_servo_s(1); } union() { cap_screws(); cap_nuts(); } }
if (check == "cap_nuts_held")       intersection() { v5_body(); cap_nuts(1.2); }                                                // must NOT be empty
if (check == "head_vs_body")        intersection() { union() { v5_body(); v5_cap(); }; head_all(head_angle); }
if (check == "slider_vs_parts")     difference() { intersection() { slider_positions(); union() { v5_body(); v5_tub(); keeper5(); place_stack() { ups_stack(s); for (i = [0, 1, 2, 4, 5]) ups_plug(i, s); } v5_deck(); } }
    translate([K5[0] - 4, K5[1] - 1.8, K5[2] - 0.5]) cube([8, 3.6, ups_knob[1] + 1]); }
if (check == "slider_grips_knob")   intersection() { v5_slider(); knob5_dummy(0); }
if (check == "slider_moves_knob")   intersection() { translate([ups_knob[2] + 1.5, 0, 0]) v5_slider(); knob5_dummy(0); }   // must NOT be empty
// --- the mic and the speaker, as v4's ---
if (check == "mic_holder_vs_body")  intersection() { v5_rsl_holder(); v5_body(); }
if (check == "mic_fit")             intersection() { v5_rsl_holder(); mic_s(); }
if (check == "mic_key_fit")         intersection() { sa_rsl_key(false); union() { v5_body(); v5_rsl_holder(); } }
if (check == "mic_key_clamps")      intersection() { translate([0, -0.2, 0]) sa_rsl_key(false); v5_rsl_holder(); }            // must NOT be empty
if (check == "mic_key_slides_in")   intersection() { for (d = [2.5, 5, 7.5, 10]) translate([d, 0, 0]) sa_rsl_key(false); union() { v5_body(); v5_rsl_holder(); } }
if (check == "mic_screws_bite")     intersection() { place_rsl() rsl_screws(); v5_rsl_holder(); }                               // must NOT be empty
if (check == "spk_keeper_fit")      intersection() { spk_keeper_rigid(); union() { v5_body(); speaker_s(); mic_s(); v5_rsl_holder(); sa_rsl_key(false); } }
if (check == "spk_keeper_goes_in")  intersection() { for (d = [0.5, 1.5, spk_k_gap - 0.05]) spk_keeper_rigid(-d); union() { v5_body(); speaker_s(); } }
if (check == "spk_keeper_holds")    intersection() { translate([0, 2, 0]) spk_keeper_rigid(); v5_body(); }                    // must NOT be empty
if (check == "chest_vs_base")       intersection() { union() { speaker_s(); mic_s(); sa_spk_keeper(); v5_rsl_holder(); } union() { base_stuff(); v5_deck(); keeper5(); } }
// --- arms ---
if (check == "arms_vs_body")        intersection() { union() { v5_body(); v5_tub(); v5_cap(); }; arms_at(arm_angle); }
if (check == "arms_vs_neck")        intersection() { neck_moving(head_angle); arms_at(arm_angle); }
if (check == "pinch_arm_head")      intersection() { head_all(head_angle); arms_grown_at(arm_angle); }
if (check == "selftest_must_fail")  intersection() { v5_body(); translate([96, 70, 100]) cube(10); }
