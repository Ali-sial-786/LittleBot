#!/usr/bin/env bash
# Run the interference checks in checks.scad (in parallel).
# PASS = the two things never intersect (OpenSCAD reports an empty result).
#   ./run_checks.sh                                   # the full list below
#   ./run_checks.sh "arms_vs_torso -D arm_angle=45"   # just these
set -uo pipefail
cd "$(dirname "$0")"
tmp=$(mktemp -d)
one() {
    local name=$1; shift
    local tag; tag=$(echo "$name $*" | tr -c 'a-zA-Z0-9_' '_')
    openscad -o "$tmp/$tag.stl" -D "check=\"$name\"" "$@" checks.scad > "$tmp/$tag.log" 2>&1
    # Empty result, or only zero-volume contact (parts designed to touch), counts as a pass.
    if grep -q "Current top level object is empty" "$tmp/$tag.log" || ! grep -q "facet" "$tmp/$tag.stl" 2>/dev/null; then
        printf 'PASS  %-26s %s\n' "$name" "$*"
    else
        printf 'FAIL  %-26s %s  (overlap: %s)\n' "$name" "$*" "$tmp/$tag.stl"
    fi
}
export -f one; export tmp
{
    if (($#)); then printf '%s\n' "$@"; else
    for c in electronics_vs_torso electronics_overlap torso_parts_vs_electronics frame_vs_servo pinion_vs_torso \
             gears_mesh_clear back_cover_vs_torso display_vs_head camera_vs_head bracket_vs_pod \
             head_parts_overlap torso_vs_base base_parts_overlap electronics_vs_base sonar_vs_base \
             wheels_vs_mobile_base; do echo "$c"; done
    for a in -60 -30 0 30 60; do echo "neck_vs_torso -D head_angle=$a"; echo "head_vs_torso -D head_angle=$a"; done
    # arm range -90..+100 degrees (+ = forwards/up): the arm never touches the torso and stays
    # >= 12 mm from the head and the base (no closing finger gap). 110 is expected to fail.
    for a in -90 -60 -30 0 30 60 90 100; do echo "arms_vs_torso -D arm_angle=$a"; echo "pinch_arm_base -D arm_angle=$a"; done
    for h in -60 -30 0 30 60; do for a in 30 60 80 90 100; do echo "pinch_arm_head -D head_angle=$h -D arm_angle=$a"; done; done
    echo "arms_vs_neck -D arm_angle=100"
    fi
} | xargs -P "${JOBS:-4}" -I{} bash -c 'one {}' | sort | tee "$tmp/summary.txt"
echo "$(grep -c '^PASS' "$tmp/summary.txt") passed, $(grep -c '^FAIL' "$tmp/summary.txt") failed"
! grep -q '^FAIL' "$tmp/summary.txt"
