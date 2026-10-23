// Preview of the build plates (images/plates.png): openscad -o images/plates.png --imgsize=1400,1400 --camera=270,-270,0,35,0,0,2300 --projection=o --colorscheme=Tomorrow plates_view.scad
names = ["01_base", "02_torso", "03_head", "04_floor", "05_back_cap", "06_arms", "07_small_parts", "08_visor", "09_glow"];
cols = ["#f2efe8", "#f2efe8", "#f2efe8", "#f2efe8", "#f2efe8", "#f2efe8", "#e6b35a", "#202428", "#bfe3f5"];
for (i = [0:8]) translate([(i % 3) * 270, -floor(i / 3) * 270, 0]) {
    color("#9aa3ad") translate([-110, -110, -1]) difference() { cube([220, 220, 1]); translate([2, 2, -1]) cube([216, 216, 3]); }
    color(cols[i]) import(str("plates/", names[i], ".stl"));
}
