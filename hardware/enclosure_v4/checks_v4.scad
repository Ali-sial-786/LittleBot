// Interference checks for v4 "Soft Arc" (run_checks_v4.sh). PASS = empty (or zero-volume contact).
include <littlebot_v4.scad>
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
module mic_s() { translate([mic3[0] + s, mic3[1] + s, mic3[2] - mic_w/2 + s]) cube([mic_l - 2*s, mic_t - 2*s, mic_w - 2*s]);
                 translate([mic3[0] + mic_body + s, mic3[1] + mic_t/2 - 4.75, mic3[2] - 9]) cube([32, 9.5, 18]); }   // + extension socket
module bat4_s() place_door() translate([-bat[0] / 2 + s, -door4_t - bat4_clr - bat[2] + s, bat4_lz + s]) cube([bat[0], bat[2], bat[1]] - [2*s, 2*s, 2*s]);
module base_electronics_s() { ups_stack(s); ups_plugs(s); pca_floor(s); usbc_socket(s); charge_cable(s); }
module floor_parts() { base_floor(false); stack_keeper(); pca_clamp(0); pca_clamp(1); }
module neck_static() place_torso() { translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }
module neck_moving(a) translate([0, neck_y, 0]) rotate(a) translate([0, -neck_y, 0]) place_torso() {
    translate([0, neck_yt, torso_h + s]) coupler(); translate([0, neck_yt, gear_z0]) ring_gear(); }
module arms_at(a) for (k = [-1, 1]) place_arm3(k, a) sa_arm();
module arm_grown(g) minkowski() { sa_arm(); sphere(r = g, $fn = 8); }
module arms_grown_at(a) for (k = [-1, 1]) place_arm3(k, a) arm_grown(grow);
module head_all(a) place_head3(a) { sa_head_frame_body(); sa_head_back(); sa_puck(); }
module camera_s() {   // v2's camera board, lens and the 5-pin plug behind it, in the hood
    translate([-cam_board/2 + s, y4f + cam_board_y + s, cam4_z - cam_board/2 + s]) cube([cam_board - 2*s, cam_pcb_t - 2*s, cam_board - 2*s]);
    translate([0, y4f + cam_board_y, cam4_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - H4_face_t - 0.3);
    translate([-7.5 + s, y4f + cam_board_y - 6, cam4_z - 7.5 + s]) cube([15 - 2*s, 6 - s, 15 - 2*s]);
    translate([-12.5 + s, y4f + cam_board_y - 2.5, cam4_z - 3 + s]) cube([25 - 2*s, 2.5 - s, 6 - 2*s]);
    translate([-14, y4f + cam_board_y + cam_pcb_t, cam4_z - 14]) cube([28, 1.5, 28]);
    translate([cam_conn[0] - cam_conn_box[0]/2 - 0.5, y4f + cam_board_y + cam_pcb_t, cam4_z - 16 + cam_conn[1] - cam_conn_box[1]/2 - 0.5]) cube([cam_conn_box[0] + 1, cam_plug, cam_conn_box[1] + 1]);
}
module slider_positions() for (st = [-1, 1]) translate([st * ups_knob[2], 0, 0]) v3_ups_slider();

// body, ring, floor, electronics
if (check == "body_vs_electronics")  intersection() { sa_body(); union() { base_electronics_s(); mic_s(); arm_servo_s(-1); arm_servo_s(1); bat4_s(); } }
if (check == "ring_vs_electronics")  intersection() { v3_ring(); base_electronics_s(); }
if (check == "body_vs_floor")        intersection() { sa_body(); floor_parts(); }
if (check == "ring_vs_floor")        intersection() { v3_ring(); floor_parts(); }
if (check == "body_vs_ring")         intersection() { sa_body(); v3_ring(); }
if (check == "body_vs_plate")        intersection() { sa_body(); v3_neck_plate(); }
if (check == "plate_vs_neck")        intersection() { v3_neck_plate(); union() { neck_moving(head_angle); head_servo_s(); place_torso() translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); } }
if (check == "frame_vs_plate")       intersection() { v3_neck_plate(); place_torso() translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }
if (check == "body_vs_neck")         intersection() { sa_body(); union() { neck_moving(head_angle); neck_static(); head_servo_s(); } }
// the AA box behind its door: clear of everything inside; the door clear of the body (it sits in its pocket)
if (check == "box_vs_parts")         intersection() { bat4_s(); union() { base_electronics_s(); floor_parts(); v3_neck_plate(); neck_static(); head_servo_s(); arm_servo_s(-1); arm_servo_s(1); mic_s(); } }
if (check == "door_vs_body")         intersection() { sa_door_all(); sa_body(); }
if (check == "door_vs_box")          intersection() { sa_door_all(); bat4_s(); }
// the UPS switch slider, at either end of its travel: clear of everything but the knob it pushes
if (check == "slider_vs_parts")      difference() { intersection() { slider_positions(); union() { sa_body(); floor_parts(); ups_stack(s); for (i = [0, 1, 2, 4, 5]) ups_plug(i, s); charge_cable(s); bat4_s(); sa_door_all(); } }
    translate([ups_knob_c[0] - 4, ups_knob_c[1] - 1.8, ups_knob_c[2] - 0.5]) cube([8, 3.6, ups_knob[1] + 1]); }
if (check == "slider_grips_knob")    intersection() { v3_ups_slider(); ups_knob_dummy(0); }
if (check == "slider_moves_knob")    intersection() { translate([ups_knob[2] + 1.5, 0, 0]) v3_ups_slider(); ups_knob_dummy(0); }   // must NOT be empty
// the black neck: clear of the plate and the body whichever way the head turns
if (check == "puck_vs_static")       intersection() { place_head3(head_angle) sa_puck(); union() { sa_body(); v3_neck_plate(); neck_static(); } }
if (check == "puck_vs_coupler")      intersection() { place_head3(head_angle) sa_puck(); place_head3(head_angle) place_torso() translate([0, neck_yt, torso_h]) coupler(); }   // sits on it: contact only
// head: the screen and its plugs, the camera
if (check == "head_vs_screen")       intersection() { union() { sa_head_frame_body(); sa_head_back(); } union() { screen_dummy3(s); screen_plugs3(s); } }
if (check == "plugs_go_in")          intersection() { sa_head_frame_body(); union() { screen_plugs3(s, true); screen_dummy3(s); } }
if (check == "glass_untouched")      intersection() { sa_head_frame_body(); translate([0, g3_y - 0.5, scr_cz]) rotate([-90, 0, 0]) linear_extrude(scr[2] + 0.5) rr(scr[0] + 0.6, scr[1] + 0.6, 0.01); }
if (check == "cover_vs_frame")       intersection() { sa_head_frame_body(); sa_head_back(); }
if (check == "hood_vs_camera")       intersection() { union() { sa_head_frame_body(); sa_head_back(); } union() { camera_s(); place_cam4() camera_bracket(); } }
if (check == "head_vs_body")         intersection() { union() { sa_body(); v3_neck_plate(); }; head_all(head_angle); }
// arms
if (check == "arms_vs_body")         intersection() { union() { sa_body(); v3_ring(); }; arms_at(arm_angle); }
if (check == "arms_vs_plate")        intersection() { union() { v3_neck_plate(); neck_moving(head_angle); }; arms_at(arm_angle); }
if (check == "pinch_arm_head")       intersection() { head_all(head_angle); arms_grown_at(arm_angle); }
if (check == "selftest_must_fail")   intersection() { sa_body(); translate([86, 60, 60]) cube(10); }
