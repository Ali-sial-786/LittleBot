// LittleBot v3 - modern concepts at real scale (floor 176 x 152 inside, neck on v2's coupler at z 197, 7" screen 160 x 100)
variant = 1;      // 1 Bell, 2 Pebble, 3 Capsule
wave = true;
$fn = 72;
WHITE = [0.97, 0.965, 0.95]; DARK = [0.09, 0.10, 0.12]; GREY = [0.32, 0.34, 0.39]; GLASS = [0.05, 0.055, 0.07];
ACC = [0.42, 0.78, 1.0]; PINK = [1.0, 0.82, 0.88]; EYE = [0.30, 0.78, 1.0]; WARM = [1.0, 0.70, 0.40];
// superellipse outline (2D), centre (0, cy)
function se(a, b, n, cy = 0, N = 96) = [for (k = [0:N-1]) let (t = 360 * k / N, c = cos(t), s = sin(t))
    [a * sign(c) * pow(abs(c), 2/n), cy + b * sign(s) * pow(abs(s), 2/n)]];
module sec(a, b, n, z, cy = 78) translate([0, 0, z]) linear_extrude(0.01) polygon(se(a, b, n, cy));
// a smooth solid from a list of [a, b, n, z] sections (convex profile)
module loft(S, cy = 78) hull() for (s = S) sec(s[0], s[1], s[2], s[3], cy);
// profile helper: half-width at height t (0..1) for a bell: gentle taper + rolled shoulder
function bell(a0, a1, t, p = 5, lin = 0.25) = a0 - (a0 - a1) * (lin * t + (1 - lin) * pow(t, p));

// ---------------- bodies (z 0 .. 197, the neck plate on top) ----------------
module body1() {   // Bell: squircle sections, rolled shoulders, dark plinth with a glow line
    color(DARK) loft([[93, 80, 4, 0], [93, 80, 4, 22]]);                 // base ring: its own part (swapped for a wheeled one later)
    color(ACC) loft([[93.3, 80.3, 4, 22], [93.3, 80.3, 4, 23.6]]);
    color(WHITE) loft([for (i = [0:24]) let (t = i / 24) [bell(92.5, 58, t * 173/181 + 8/181), bell(79.5, 47, t * 173/181 + 8/181), 4 - 1.2 * t, 24 + t * 173]]);
}
module body2() {   // Pebble: dark plinth inset, the shell swells a little, then curves in like a pebble
    color(DARK) loft([[88, 75, 3.5, 0], [90, 77, 3.5, 12]]);
    color(WHITE) loft([for (i = [0:24]) let (t = i / 24, sw = 6 * sin(t * 180) * (1 - t)) [bell(93, 56, t, 3.2, 0.1) + sw, bell(80, 45, t, 3.2, 0.1) + sw * 0.8, 3.2 - 0.9 * t, 12 + t * 185]]);
}
module body3() {   // Capsule: straight soft sides, a big round shoulder into the top
    color(DARK) loft([[93, 80, 4.5, 0], [93, 80, 4.5, 18]]);
    color(WHITE) loft(concat([[93, 80, 4.5, 18], [90, 77, 4.5, 120]], [for (i = [0:12]) let (q = 90 * i / 12) [60 + 30 * cos(q), 48 + 29 * cos(q), 4.5 - q / 90, 120 + 77 * sin(q)]]));
}
module chest_panel(z, w, h, ang) {   // the AA box sits behind it; its switch through a slot; two status lights
    translate([0, 0, z]) rotate([ang, 0, 0]) {
        color(DARK) translate([0, -1, 0]) rotate([90, 0, 0]) linear_extrude(3) offset(r = 10) square([w - 20, h - 20], center = true);
        color(GREY) translate([w/2 - 14, -4.2, h/2 - 12]) rotate([90, 0, 0]) linear_extrude(1) offset(r = 1.5) square([9, 4], center = true);
        for (k = [-1, 1]) color(k < 0 ? ACC : WARM) translate([k * 9, -4.3, -h/2 + 12]) rotate([90, 0, 0]) cylinder(d = 5, h = 1);
    }
}
module grille(z, y, w) for (i = [-6:6]) for (j = [-1:1]) color(GREY) translate([i * 6 + (j % 2) * 3, y, z + j * 5]) rotate([90, 0, 0]) cylinder(d = 2.6, h = 1, $fn = 12);

// ---------------- heads (on the coupler at z 207.8; neck axis 34 mm behind the face) ----------------
module face(w, h, r, y, ang = 0) {   // black glass face with the screen in it
    translate([0, y, 0]) rotate([ang, 0, 0]) {
        color(GLASS) rotate([90, 0, 0]) linear_extrude(1) offset(r = r) square([w - 2*r, h - 2*r], center = true);
        color(PINK) translate([0, -1.05, -6]) rotate([90, 0, 0]) linear_extrude(0.2) offset(r = 3) square([148, 82], center = true);
        for (s = [-1, 1]) color(EYE) translate([s * 30, -1.3, 0]) rotate([90, 0, 0]) linear_extrude(0.2) offset(r = 12) square([2, 16], center = true);
        for (s = [-1, 1]) color("white") translate([s * 30 + 5, -1.5, 10]) rotate([90, 0, 0]) cylinder(d = 8, h = 0.2);
        color(EYE) translate([0, -1.3, -26]) rotate([90, 0, 0]) linear_extrude(0.2) difference() { circle(13); translate([0, 6]) square([40, 20], center = true); circle(9.5); }
    }
}
module cam_pod(z, y, w = 46, d = 34, h = 24) translate([0, y, z]) {   // pill-shaped pod on the head's roof; 2 screws from the top into nuts in the roof
    color(WHITE) hull() { for (s = [-1, 1]) translate([s * (w - d)/2, 0, 0]) scale([1, 1, 2 * h / d]) sphere(d = d); }
    color(GLASS) translate([0, -d/2 + 1.5, h * 0.45]) rotate([90, 0, 0]) cylinder(d = 17, h = 3);
    color(GREY) translate([0, -d/2 + 0.2, h * 0.45]) rotate([90, 0, 0]) cylinder(d = 8, h = 1);
    color(ACC) translate([0, -d/2 + 0.1, h * 0.45]) rotate([90, 0, 0]) cylinder(d = 3.5, h = 1);
}
module head1() {   // squircle face, domed back
    W = 198; H = 138; D = 64;
    color(WHITE) hull() {
        translate([0, 0, H/2]) rotate([-90, 0, 0]) linear_extrude(24) polygon(se(W/2, H/2, 4.2));
        translate([0, D - 8, H/2]) rotate([-90, 0, 0]) linear_extrude(8) polygon(se(W/2 - 34, H/2 - 26, 3));
    }
    translate([0, 0, H/2]) face(W - 14, H - 14, 26, -0.3);
    cam_pod(H - 2, 20);
}
module head2() {   // a pebble: rounder outline, the face inset in a soft frame, the back curving away
    W = 200; H = 140; D = 66;
    color(WHITE) hull() {
        translate([0, 6, H/2]) rotate([-90, 0, 0]) linear_extrude(16) polygon(se(W/2, H/2, 3.2));
        translate([0, 0, H/2]) rotate([-90, 0, 0]) linear_extrude(1) polygon(se(W/2 - 6, H/2 - 6, 3.2));
        translate([0, D - 6, H/2]) rotate([-90, 0, 0]) linear_extrude(6) polygon(se(W/2 - 40, H/2 - 30, 2.4));
    }
    translate([0, 0, H/2]) face(W - 22, H - 20, 34, 0.7);
    cam_pod(H - 1, 22, 44, 32, 22);
}
module head3() {   // capsule: a horizontal pill, the face flat and full-width, the back fully round
    W = 206; H = 136; D = 64; r = 30;
    color(WHITE) hull() {
        for (x = [-1, 1], z = [-1, 1]) translate([x * (W/2 - r), 12, H/2 + z * (H/2 - r)]) rotate([-90, 0, 0]) cylinder(r = r, h = 1);
        for (x = [-1, 1], z = [-1, 1]) translate([x * (W/2 - r - 6), D - r, H/2 + z * (H/2 - r - 4)]) scale([1, 1, 1]) sphere(r = r - 4);
        for (x = [-1, 1], z = [-1, 1]) translate([x * (W/2 - r), 0, H/2 + z * (H/2 - r)]) rotate([-90, 0, 0]) cylinder(r = r - 2, h = 1);
    }
    translate([0, 0, H/2]) face(W - 12, H - 12, r - 6, -0.3);
    cam_pod(H - 2, 22, 50, 32, 22);
}

// ---------------- neck + arms ----------------
module neck() { color(DARK) translate([0, 89, 196]) cylinder(r = 25, h = 12); color(ACC) translate([0, 89, 202]) cylinder(r = 25.3, h = 1.2); }
module arm(s, a) {   // ball shoulder, tapered half-round arm, mitten hand (real ones print on the flat inner face)
    rotate([a, 0, 0]) {
        color(WHITE) scale([0.55, 1, 1]) sphere(d = 46);
        color(ACC) rotate([0, 90, 0]) translate([0, 0, s * 12.4]) cylinder(d = 18, h = 1.2, center = true);
        color(WHITE) hull() { translate([s * 6, 0, 0]) sphere(d = 28); translate([s * 9, 0, -50]) sphere(d = 25); }
        translate([s * 9, 0, -50]) {
            color(WHITE) sphere(d = 25.4);
            rotate([30, 0, 0]) {
                color(WHITE) hull() { sphere(d = 25); translate([0, 0, -44]) sphere(d = 20); }
                translate([0, 0, -50]) color(WHITE) hull() { scale([0.62, 1, 1]) sphere(d = 30); translate([0, -2, -16]) scale([0.6, 1, 1.15]) sphere(d = 27);
                    translate([0, 14, -2]) scale([0.6, 1, 1]) sphere(d = 12); }
            }
        }
    }
}
module robot(v) {
    if (v == 1) { body1(); chest_panel(86, 74, 80, -6); grille(150, 12.5, 0); }
    if (v == 2) { body2(); chest_panel(84, 72, 80, -9); }
    if (v == 3) { body3(); chest_panel(80, 74, 82, 0); grille(148, -0.3, 0); }
    neck();
    translate([0, 89 - 34, 207.8]) { if (v == 1) head1(); if (v == 2) head2(); if (v == 3) head3(); }
    sx = v == 3 ? 94 : 82;
    for (s = [-1, 1]) translate([s * sx, 82, 164]) arm(s, s < 0 && wave ? 75 : 10);
}
if (is_undef(view)) robot(variant);
