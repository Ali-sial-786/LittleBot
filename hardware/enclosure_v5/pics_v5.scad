// The README's pictures (make_pics_v5.sh): the top joint cut through the middle, the base with the body off.
include <littlebot_v5.scad>
part = "none";
pic = "neck";
cut_y = 100;                             // the cut: through the cap's 2 rear side screws, 11 mm behind the neck's axis
module cuty(c) color(c) render() difference() { children(); translate([-300, cut_y, -10]) cube([600, 400, 700]); }   // behind it cut away
if (pic == "neck") {
    cuty(WHITE4) v5_body();
    cuty("#b9c0cc") v5_cap();
    cuty("#7fb3d5") v5_hub();
    cuty(DARK4) v5_neck();
    cuty("#e6b35a") v5_ring_gear();
    cuty("#f4f1ea") sa_head_frame_body();
    for (k = [-1, 1]) cuty("#3a6fb0") arm_servo3(k);
    // the cap's 2 screws on the cut, and their nuts in the collar
    for (p = [cap_scr[2], cap_scr[3]]) color("#d22") {
        translate([p[0], cut_y + 0.3, zc0 + 2.8]) { translate([0, 0, -12]) cylinder(d = 3, h = 12, $fn = 16); cylinder(d = 5.6, h = 1.7, $fn = 16); }
        translate([p[0], cut_y + 0.3, zc0 - 3.5 - 1.4]) cylinder(d = 6.3, h = 2.4, $fn = 6, center = true);
    }
}
if (pic == "base") v5_base_parts(false);      // the deck off
if (pic == "deck") v5_base_parts();
// the top of the body, its front cut away (through the front pair of side screws), the collar in orange
module keep_top() { translate([-300, 62, 150]) cube([600, 400, 200]); }
module collar_shown() difference() { collar5(); collar_cuts(); for (k = [-1, 1]) place_sh(k) sh_cut(); back_vents5(); }
if (pic == "collar") {
    color(WHITE4) render() difference() { intersection() { v5_body(); keep_top(); } collar5(); }
    color("#e8742c") render() intersection() { keep_top(); collar_shown(); }
}
