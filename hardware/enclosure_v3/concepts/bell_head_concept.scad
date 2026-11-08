// Bell body + head that hides its plugs and camera (concept, real scale)
include <bell_body_concept.scad>
view = 0; spin = 0;
SW = 164.9; SH = 100; ST = 9;             // screen
end_b = 23; tb = 4;                       // frame: ends hold the plugs, 4 mm top/bottom
HW = SW + 2 * end_b; HH = SH + 2 * tb; HD = 42;    // head: glass -> back 40 (the camera needs 37)
hz0 = 207.8; neck_y0 = 89; gy = neck_y0 - 16;      // glass front
module rr(w, h, r) offset(r = r) square([w - 2*r, h - 2*r], center = true);
module head2() translate([0, gy, hz0 + HH/2]) {
    // shell: screen-shaped front, the back rounded off (r 12)
    color(WHITE) difference() {
        hull() { rotate([-90, 0, 0]) linear_extrude(HD - 12) rr(HW, HH, 12);
                 translate([0, HD - 12, 0]) scale([1, 12, 1]) rotate([-90, 0, 0]) linear_extrude(1) rr(HW - 22, HH - 22, 4);
                 for (x = [-1, 1], z = [-1, 1]) translate([x * (HW/2 - 12), HD - 12, z * (HH/2 - 12)]) sphere(r = 12); }
        translate([0, -1, 0]) rotate([-90, 0, 0]) linear_extrude(ST + 1) rr(SW + 0.6, SH + 0.6, 2);
    }
    color(GLASS) rotate([-90, 0, 0]) linear_extrude(ST - 0.5) rr(SW, SH, 2);
    color(PINK) translate([0, -0.05, 0]) rotate([90, 0, 0]) linear_extrude(0.1) rr(154.2, 85.9, 1);
    for (s = [-1, 1]) color(EYE) translate([s * 30, -0.2, 4]) rotate([90, 0, 0]) linear_extrude(0.1) rr(26, 40, 12);
    for (s = [-1, 1]) color("white") translate([s * 30 + 5, -0.3, 14]) rotate([90, 0, 0]) cylinder(d = 8, h = 0.1);
    color(EYE) translate([0, -0.2, -24]) rotate([90, 0, 0]) linear_extrude(0.1) difference() { circle(13); translate([0, 6]) square([40, 20], center = true); circle(9.5); }
    // back: vent slots (top and bottom rows), the 4 screen screws
    for (x = [-56:8:56]) for (z = [-1, 1]) color(GREY) translate([x, HD - 0.3, z * 30]) rotate([-90, 0, 0]) linear_extrude(0.5) rr(3.2, 14, 1.4);
    for (x = [-1, 1], z = [-1, 1]) color(GREY) translate([x * (SW/2 - 4.7), HD - 0.4, z * (SH/2 - 5)]) rotate([-90, 0, 0]) cylinder(d = 6, h = 0.6);
    // camera pod: on top, flush with the glass and with the back, narrower than the screen
    translate([0, 4, HH/2 - 0.5]) {     // the pod sits 4 mm behind the face: nothing of it in front
        PW = 58; PH = 40;
        color(WHITE) hull() {
            for (x = [-1, 1]) translate([x * (PW/2 - 10), 0, PH - 10]) rotate([-90, 0, 0]) cylinder(r = 10, h = HD - 16);
            translate([0, 0, 0.5]) rotate([-90, 0, 0]) linear_extrude(HD - 16) square([PW, 1], center = true);
            for (x = [-1, 1]) translate([x * (PW/2 - 12), HD - 16, PH - 12]) sphere(r = 12);
            translate([0, HD - 16, 0.5]) rotate([-90, 0, 0]) linear_extrude(12) square([PW - 2, 1], center = true);
        }
        color(GLASS) translate([0, -0.05, PH/2 + 2]) rotate([90, 0, 0]) linear_extrude(0.1) rr(30, 18, 8);
        color(GREY) translate([0, -0.12, PH/2 + 2]) rotate([90, 0, 0]) cylinder(d = 9, h = 0.1);
        color(ACC) translate([0, -0.2, PH/2 + 2]) rotate([90, 0, 0]) cylinder(d = 3.5, h = 0.1);
    }
}
// hands with fingers: an open hand (prints flat on its inner face), fingers slightly spread, thumb forward
module hand2() {
    color(WHITE) hull() { scale([0.55, 1, 1]) sphere(d = 22); translate([0, 1, -20]) scale([0.5, 1.2, 0.7]) sphere(d = 28); }      // palm
    for (i = [0:3]) let (fx = -9.6 + i * 6.8, L = [20, 25, 24, 18][i], a = [-5, -1.5, 1.5, 5][i], c = 14)
        translate([0, fx + 1, -29]) rotate([a, 0, 0]) color(WHITE) {
            hull() { scale([0.6, 1, 1]) sphere(d = 8.4); translate([0, 0, -L * 0.55]) scale([0.6, 1, 1]) sphere(d = 7.8); }
            translate([0, 0, -L * 0.55]) rotate([-c, 0, 0]) hull() { scale([0.6, 1, 1]) sphere(d = 7.8); translate([0, 0, -L * 0.45]) scale([0.6, 1, 1]) sphere(d = 6.8); }
        }
    color(WHITE) hull() { translate([0, -8, -8]) scale([0.6, 1, 1]) sphere(d = 12); translate([0, -15, -20]) scale([0.6, 1, 1]) sphere(d = 10); }
    translate([0, -15, -20]) rotate([-25, 0, 0]) color(WHITE) hull() { scale([0.6, 1, 1]) sphere(d = 9.6); translate([0, 0, -16]) scale([0.6, 1, 1]) sphere(d = 7.6); }   // thumb
}
module arm2(s, a) rotate([a, 0, 0]) {
    color(WHITE) scale([0.55, 1, 1]) sphere(d = 46);
    color(ACC) rotate([0, 90, 0]) translate([0, 0, s * 12.4]) cylinder(d = 18, h = 1.2, center = true);
    color(WHITE) hull() { translate([s * 6, 0, 0]) scale([0.6, 1, 1]) sphere(d = 30); translate([s * 9, 0, -50]) scale([0.6, 1, 1]) sphere(d = 26); }
    translate([s * 9, 0, -50]) { color(WHITE) scale([0.6, 1, 1]) sphere(d = 26.4);
        rotate([30, 0, 0]) { color(WHITE) hull() { scale([0.6, 1, 1]) sphere(d = 25); translate([0, 0, -42]) scale([0.6, 1, 1]) sphere(d = 20); }
            translate([0, 0, -48]) hand2(); } }
}
module robot2(armL = 10) {
    body1(); 
    chest_panel(69, 69, 74, -3);
    // speaker grille (bigger) and mic holes
    for (row = [-3:3]) for (gx = [-35:5:35]) let (px = gx + (abs(row) % 2) * 2.5, pz = row * 4.4)
        if (abs(px) < 36 - abs(pz) * 0.4) color(GREY) translate([px, 7.0 - 0.06 * abs(px) * 0, 126 + pz]) rotate([90, 0, 0]) cylinder(d = 2.8, h = 1, $fn = 12);
    for (dx = [0, 3.5, 7]) for (dz = [-3.5, 0, 3.5]) color(GREY) translate([-20 + dx, 15.2, 172 + dz]) rotate([90, 0, 0]) cylinder(d = 2, h = 1, $fn = 10);
    // back: vent slots over the Pi + UPS, the UPS switch slider, the charging port
    color(DARK) translate([0, 0, 0]) for (x = [-30:7:30]) translate([x, 156.6, 92]) rotate([90, 0, 0]) linear_extrude(1) rr(3.4, 34, 1.5);
    color(GREY) translate([-27, 157.3, 70]) cube([16, 3, 8], center = true);
    color(DARK) translate([14, 157.6, 36]) rotate([90, 0, 0]) hull() for (s = [-1, 1]) translate([s * 4, 0, 0]) cylinder(d = 8, h = 1, center = true);
    color(DARK) translate([0, 89, 196]) cylinder(r = 25, h = 12);
    head2();
    translate([-82, 82, 164]) arm2(-1, armL);
    translate([82, 82, 164]) arm2(1, 10);
}
if (view < 5) translate([0, 78, 0]) rotate(spin) translate([0, -78, 0]) robot2(view == 0 ? 60 : 10);
if (view == 5) rotate([0, 0, 0]) arm2(-1, 10);
