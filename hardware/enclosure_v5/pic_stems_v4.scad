// For the README's "stems or collar" picture (make_pics_v5.sh): the top of v4's body, its front cut away, the 4 stems
// that carried the neck plate in orange.
include <../enclosure_v4/littlebot_v4.scad>
part = "none"; detail = false;
module keep() { translate([-300, 70, 150]) cube([600, 400, 200]); }   // the cut: through the front pair of stems
module stems() difference() {
    intersection() { body_inner(); for (p = np_screw_pts) hull() { translate([p[0], p[1], b_top - np_t - 7]) cylinder(d = 11, h = 7); translate(np_boss_to(p)) cylinder(d = 11, h = 0.01); } }
    top_cuts(); }
color(WHITE4) render() difference() { intersection() { sa_body(); keep(); } stems(); }
color("#e8742c") render() intersection() { keep(); stems(); }
