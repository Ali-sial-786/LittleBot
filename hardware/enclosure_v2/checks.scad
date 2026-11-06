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
// Adafruit 3367 mic + a USB extension socket (about 18 x 9.5 x 32 mm) plugged onto it
module mic_s() { translate([mic_x0 + s, mic_y0 + s, mic_zc - mic_w/2 + s]) cube([mic_l - 2*s, mic_t - 2*s, mic_w - 2*s]);
                 translate([mic_x0 + mic_body + s, mic_y0 + mic_t/2 - 4.75, mic_zc - 9]) cube([32, 9.5, 18]); }
module speaker_s() { translate([-spk_w/2 + s, spk_y + s, spk_z + s]) cube([spk_w - 2*s, spk_d - 2*s, spk_h - 2*s]); }
module torso_electronics_s() { head_servo_s(); arm_servos_s(); speaker_s(); mic_s(); }

// --- head electronics (head coordinates) ---
// the screen (no longer on the Pi) and the cables plugged into it (littlebot_v2.scad), shrunk by s
module screen_s() { screen_dummy(s); screen_plugs(s); }
// the camera: board, lens, lens holder with its screw ears, and on the BACK the 5-pin socket with its plug and the
// wires' bend (owner's photos, 2026-11-03); the board's back components (up to 1.5 mm) and the holder's screw heads
module camera_s() {
    translate([-cam_board/2 + s, cam_board_y + s, cam_z - cam_board/2 + s]) cube([cam_board - 2*s, cam_pcb_t - 2*s, cam_board - 2*s]);
    translate([0, cam_board_y, cam_z]) rotate([90, 0, 0]) cylinder(d = 14, h = cam_board_y - pod_y0 - 2.2 - 0.3);    // lens
    translate([-7.5 + s, cam_board_y - 6, cam_z - 7.5 + s]) cube([15 - 2*s, 6 - s, 15 - 2*s]);                          // lens holder
    translate([-12.5 + s, cam_board_y - 2.5, cam_z - 3 + s]) cube([25 - 2*s, 2.5 - s, 6 - 2*s]);                         // its screw ears
    translate([-14, cam_board_y + cam_pcb_t, cam_z - 14]) cube([28, 1.5, 28]);                                          // parts on the back
    translate([cam_conn[0] - cam_conn_box[0]/2 - 0.5, cam_board_y + cam_pcb_t, cam_z - 16 + cam_conn[1] - cam_conn_box[1]/2 - 0.5])
        cube([cam_conn_box[0] + 1, cam_plug, cam_conn_box[1] + 1]);                                                     // 5-pin socket + plug
}

// --- base electronics (robot coordinates) ---
// the Pi 5 on the UPS HAT (E), lying on its side, with everything plugged into it; the PCA9685; the charging socket
module base_electronics_s() { ups_stack(s); ups_plugs(s); pca_floor(s); usbc_socket(s); }
module sonar_s() { translate([-22.5, 12 - bezel_t + s, front_z + 4 - 10]) cube([45, 1.6 + 4, 20]);           // board + parts on its back
                   translate([-5.5, 12 - bezel_t + s, front_z + 4 - 10]) cube([11, 1.6 + 20, 4.5]);       // header + straight dupont plug
                   for (k = [-1, 1]) translate([k * sonar_sp/2, -bezel_t + 0.5, front_z + 4]) rotate([-90, 0, 0]) cylinder(d = 16, h = 12); }
// mobile phase: the wheels stick out 8 mm below the skirt
module wheels_s() { for (k = [-1, 1]) translate([k * wheel_x, wheel_y, wheel_d/2 - 8]) rotate([0, 90, 0]) cylinder(d = wheel_d + 2, h = wheel_w + 1, center = true); }

// --- moving assemblies ---
module rotate_about_neck(a) { translate([0, neck_y, 0]) rotate(a) translate([0, -neck_y, 0]) children(); }
module head_all() { place_head() { head_shell(); head_back(); visor(); screen_clamp(); ear(); mirror([1, 0, 0]) ear(); camera_pod(); pod_bar(); } }
module moving_neck() { place_torso() translate([0, neck_yt, 0]) rotate(head_angle) translate([0, -neck_yt, 0]) {
    translate([0, neck_yt, torso_h + s]) coupler(); translate([0, neck_yt, gear_z0]) ring_gear(); } }
module arms_at(a) { for (k = [-1, 1]) place_arm(k, a); }
module base_parts() { base_shell(); base_floor(false); stack_keeper(); pca_clamp(0); pca_clamp(1); }

if (check == "electronics_vs_torso")  intersection() { torso_shell(); torso_electronics_s(); }
if (check == "electronics_overlap")   place_torso() union() {
    intersection() { head_servo_s(); union() { arm_servos_s(); speaker_s(); } }
    intersection() { arm_servos_s(); speaker_s(); }
    intersection() { mic_s(); union() { head_servo_s(); arm_servos_s(); speaker_s(); } } }
if (check == "torso_parts_vs_electronics") intersection() { union() { translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame();
    translate([0, neck_yt, gear_z0]) ring_gear(); translate([0, pinion_yt, gear_z0]) pinion(); translate([-46 + clr, torso_d - torso_wall, 0]) torso_back(); }
    union() { arm_servos_s(); speaker_s(); mic_s(); } }
if (check == "frame_vs_servo")        intersection() { translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); head_servo_s(); }
if (check == "pinion_vs_torso")       intersection() { torso_shell(); translate([0, pinion_yt, gear_z0]) pinion(); }
if (check == "gears_mesh_clear")      intersection() { translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion(); translate([0, neck_yt, gear_z0]) ring_gear(); }
if (check == "back_cover_vs_torso")   intersection() { torso_shell(); translate([0, s, 0]) translate([-46 + clr, torso_d - torso_wall, 0]) torso_back(); }
if (check == "neck_vs_torso")         intersection() { place_torso() torso_shell(); moving_neck(); }
if (check == "head_vs_torso")         intersection() { place_torso() torso_shell(); translate([0, 0, s]) rotate_about_neck(head_angle) head_all(); }
// the screen and the cables plugged into it vs the head, the visor (ribs, posts), the screen clamp and the camera
if (check == "screen_vs_head")        intersection() { place_head() union() { head_shell(); visor(); screen_clamp(); camera_pod(); pod_bar(); place_cam_bracket() camera_bracket(); }
                                                       place_head() screen_s(); }
// the stack's plugs don't run into each other or into the stack (the jumpers sit on the GPIO pins by design)
if (check == "ups_internal")          union() {
    for (i = [0:5]) intersection() { ups_plug(i, s); union() { for (j = [0:5]) if (j != i) ups_plug(j, s); } }
    intersection() { union() { for (i = [0:4]) ups_plug(i, s); } ups_stack(s); }
    intersection() { ups_plug(5, s); difference() { ups_stack(s); pi_box(7.13, 57.93, 50.02, 55.0, 0, 8.57); } }
    intersection() { pca_floor(s); union() { ups_stack(s); ups_plugs(s); usbc_socket(s); } }
    intersection() { usbc_socket(s); union() { ups_stack(s); ups_plugs(s); } } }
if (check == "camera_vs_head")        intersection() { place_head() union() { head_shell(); camera_pod(); pod_bar(); place_cam_bracket() camera_bracket(); } place_head() camera_s(); }
// the board sliding down into the bracket (2026-11-06): everything on it but its edges (the slots, ribs and hooks take
// those) must pass the bracket on the way in, from 40 mm above to its place. Bracket coordinates.
module cam_parts_local() {
    translate([-14, cam_pcb_t, -14]) cube([28, 1.5, 28]);                                                         // parts on the back
    translate([cam_conn[0] - cam_conn_box[0]/2 - 0.5, cam_pcb_t, -16 + cam_conn[1] - cam_conn_box[1]/2 - 0.5])
        cube([cam_conn_box[0] + 1, cam_conn_box[2], cam_conn_box[1] + 1]);                                        // the 5-pin socket
    translate([-7.5, -6, -7.5]) cube([15, 6, 15]);                                                                 // lens holder
    translate([-12.5, -2.5, -3]) cube([25, 2.5, 6]);                                                              // its screw ears
    rotate([90, 0, 0]) cylinder(d = 14, h = 20);                                                                  // lens
}
if (check == "camera_insert")         intersection() { camera_bracket(); hull() { cam_parts_local(); translate([0, 0, 40]) cam_parts_local(); } }
if (check == "bracket_vs_pod")        intersection() { place_head() union() { head_shell(); camera_pod(); pod_bar(); } place_head() place_cam_bracket() camera_bracket(); }
if (check == "head_parts_overlap")    place_head() union() {
    intersection() { head_shell(); translate([0, -s, 0]) visor(); }
    intersection() { head_shell(); translate([0, s, 0]) head_back(); }
    intersection() { head_shell(); for (k = [-1, 1]) translate([k * s, 0, 0]) mirror([k < 0 ? 1 : 0, 0, 0]) ear(); }
    intersection() { head_shell(); translate([0, 0, s]) camera_pod(); }
    intersection() { union() { head_shell(); camera_pod(); } translate([0, 0, -s]) pod_bar(); }
    intersection() { union() { head_shell(); camera_pod(); pod_bar(); } screen_clamp(); }
    intersection() { visor(); translate([0, s, 0]) screen_clamp(); } }
if (check == "torso_vs_base")         intersection() { base_shell(); translate([0, 0, s]) place_torso() torso_shell(); }
if (check == "base_parts_overlap")    union() {
    intersection() { base_shell(); translate([0, 0, -s]) base_floor(false); }
    intersection() { union() { base_shell(); base_floor(false); } for (k = [-1, 1]) translate([k * s, 0, 0]) place_well_cover(k) well_cover(); }
    intersection() { union() { base_shell(); base_floor(false); } translate([0, -s, 0]) place_front_plate() port_plate(front_w, front_h, "sonar"); }
    intersection() { union() { base_shell(); base_floor(false); } translate([0, -s, 0]) place_front_plate() port_plate(front_w, front_h, "blank"); }
    intersection() { union() { base_shell(); base_floor(false); } translate([0, s, 0]) place_rear_plate() port_plate(rear_w, rear_h, "rear"); } }
// the stack, its plugs (up through the cable hole into the torso), the PCA9685 and the charging socket vs the
// base, its floor, the keeper and the PCA9685 clamps (the stack touches the V-grooves and the clamps the board by design), the plates and the torso
if (check == "electronics_vs_base")   intersection() { union() { base_parts(); place_torso() torso_shell(); place_rear_plate() port_plate(rear_w, rear_h, "rear_usb");
    place_front_plate() port_plate(front_w, front_h, "blank"); for (k = [-1, 1]) place_well_cover(k) well_cover(); }
    base_electronics_s(); }
// the charging cable (a USB-C extension) from the UPS's USB-C to its socket behind the rear plate: clear of the
// printed parts (base, floor, torso and its back cover, rear plate) and of everything else in the base
if (check == "charge_cable_clear")    intersection() { charge_cable(s); union() { base_parts(); place_torso() { torso_shell(); translate([-46 + clr, torso_d - torso_wall, 0]) torso_back(); }
    place_rear_plate() port_plate(rear_w, rear_h, "rear_usb"); ups_stack(s); for (i = [0, 1, 3, 4, 5]) ups_plug(i, s); pca_floor(s); usbc_socket(s); } }
// every cable route (littlebot_v2.scad hw_cables(), without its plugs) clear of the printed parts and the other electronics:
// the body's cables (base, torso, up the neck) and the head's (from the neck to the screen and the camera)
if (check == "cables_body")           intersection() { union() { for (c = hw_cables()) if (c[2] != "head") cable_route(c, s); }
    union() { base_parts(); place_rear_plate() port_plate(rear_w, rear_h, "rear_usb"); place_front_plate() port_plate(front_w, front_h, "blank");
        for (k = [-1, 1]) place_well_cover(k) well_cover(); ups_stack(s); pca_floor(s); usbc_socket(s); place_head() head_shell();
        place_torso() { torso_shell(); translate([-46 + clr, torso_d - torso_wall, 0]) torso_back(); translate([0, neck_yt, torso_h + s]) coupler();
            translate([0, neck_yt, gear_z0]) ring_gear(); translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion();
            translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); torso_electronics_s(); } } }
if (check == "cables_head")           intersection() { union() { for (c = hw_cables()) if (c[2] == "head") cable_route(c, s); }
    place_head() union() { head_shell(); head_back(); visor(); screen_clamp(); ear(); mirror([1, 0, 0]) ear(); camera_pod(); pod_bar();
        place_cam_bracket() camera_bracket(); screen_dummy(s); camera_s(); } }
if (check == "sonar_vs_base")         intersection() { union() { base_parts(); place_front_plate() port_plate(front_w, front_h, "sonar"); } sonar_s(); }
if (check == "wheels_vs_mobile_base") intersection() { union() { base_shell(); base_floor(true); place_torso() torso_shell(); } wheels_s(); }
if (check == "arms_vs_torso")         intersection() { place_torso() torso_shell(); arms_at(arm_angle); }
if (check == "arms_vs_base")          intersection() { base_shell(); arms_at(arm_angle); }
if (check == "arms_vs_head")          intersection() { rotate_about_neck(head_angle) head_all(); arms_at(arm_angle); }
if (check == "arms_vs_neck")          intersection() { place_torso() translate([0, neck_yt, torso_h]) cylinder(r = coupler_r + 1, h = bearing_h + coupler_h); arms_at(arm_angle); }
// the servo frame's collars round the torso's solid pillars, its plate on their ends (touching)
if (check == "frame_vs_torso")        intersection() { torso_shell(); translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }
// the keeper and the floor's rail and bosses round the stack's clear plate, and the PCA9685's clamps (run with plate
// variants: -D ups_plate_t=1.4 / 3.2, -D 'ups_plate=[88,55]' / [90,58])
if (check == "stack_mount")           intersection() { union() { base_floor(false); stack_keeper(); pca_clamp(0); pca_clamp(1); } union() { ups_stack(s); pca_floor(s); } }

// --- screws (littlebot_v2.scad hw_screws()): every M3 screw and servo screw in the printed parts, as assembled ---
module printed_parts() {
    base_parts(); for (k = [-1, 1]) place_well_cover(k) well_cover();
    place_front_plate() port_plate(front_w, front_h, "blank"); place_rear_plate() port_plate(rear_w, rear_h, "rear_usb");
    place_torso() { torso_shell(); translate([-46 + clr, torso_d - torso_wall, 0]) torso_back(); translate([0, neck_yt, torso_h]) coupler();
        translate([0, neck_yt, gear_z0]) ring_gear(); translate([0, pinion_yt, gear_z0]) rotate(7.5) pinion();
        translate([0, pinion_yt, hs_tab_top - sv_tab_t - frame_t]) servo_frame(); }
    place_head() { head_shell(); head_back(); visor(); screen_clamp(); ear(); mirror([1, 0, 0]) ear(); camera_pod(); pod_bar(); place_cam_bracket() camera_bracket(); }
}
module electronics_all() { base_electronics_s(); charge_cable(s); place_torso() torso_electronics_s(); place_head() { screen_s(); camera_s(); }
    for (c = hw_cables()) cable_route(c, s); }
function screw_checked(sc) = sc[2] == 3 || sc[1] == "servo screws";        // horn screws, the arms' centre screws, the UPS kit's: not ours
module along(sc) { v = sc[6] / norm(sc[6]); translate(sc[5]) rotate([0, acos(v[2]), atan2(v[1], v[0])]) children(); }
// core: thinner than any pilot hole (2.7 / 1.8), so it only meets plastic a screw would have to force its way through
module screw_core(sc) { along(sc) translate([0, 0, 0.05]) cylinder(d = sc[2] == 3 ? 2.3 : 1.4, h = sc[3] - 0.05, $fn = 12); }
// thread: the ring between the pilot hole and the screw's diameter = where it cuts its thread
module screw_thread(sc) { along(sc) difference() { cylinder(d = sc[2], h = sc[3], $fn = 12); translate([0, 0, -1]) cylinder(d = sc[2] == 3 ? 2.75 : 1.85, h = sc[3] + 2, $fn = 12); } }
// head (button: 1.9 d x 0.55 d), shrunk 0.15 mm so a head bearing on a face doesn't count
module screw_head(sc) { along(sc) translate([0, 0, -0.55 * sc[2]]) translate([0, 0, 0.15]) cylinder(d = 1.9 * sc[2] - 0.3, h = 0.55 * sc[2] - 0.3, $fn = 16); }
if (check == "screws_forced")         intersection() { union() { for (sc = hw_screws()) if (screw_checked(sc)) screw_core(sc); } printed_parts(); }
if (check == "screws_thread")         intersection() { union() { for (sc = hw_screws()) if (screw_checked(sc)) screw_thread(sc); } printed_parts(); }
if (check == "screw_heads")           intersection() { union() { for (sc = hw_screws()) if (screw_checked(sc)) screw_head(sc); } union() { printed_parts(); electronics_all(); } }

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
