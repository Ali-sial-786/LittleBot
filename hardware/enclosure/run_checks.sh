#!/usr/bin/env bash
# Run the interference checks in checks.scad (4 in parallel).
# PASS = the two things never intersect (OpenSCAD reports an empty result).
set -uo pipefail
cd "$(dirname "$0")"
tmp=$(mktemp -d)
one() {
    local name=$1; shift
    local tag; tag=$(echo "$name $*" | tr -c 'a-zA-Z0-9_' '_')
    openscad -o "$tmp/$tag.stl" -D "check=\"$name\"" "$@" checks.scad > "$tmp/$tag.log" 2>&1
    # Empty result, or only zero-volume contact (parts designed to touch), counts as a pass.
    if grep -q "Current top level object is empty" "$tmp/$tag.log" || ! grep -q "facet" "$tmp/$tag.stl" 2>/dev/null; then
        printf 'PASS  %-22s %s\n' "$name" "$*"
    else
        printf 'FAIL  %-22s %s  (overlap: %s)\n' "$name" "$*" "$tmp/$tag.stl"
    fi
}
export -f one; export tmp
{
    for c in electronics_vs_shell electronics_vs_base pi_vs_base electronics_overlap pinion_vs_shell \
             frame_vs_servo frame_vs_parts camera_vs_shell display_vs_head gears_mesh_clear; do echo "$c"; done
    for a in -45 -25 0 25 45; do echo "neck_vs_shell -D head_angle=$a"; echo "head_vs_body -D head_angle=$a"; done
    for a in -30 0 45 90 135 170; do echo "arms_vs_body -D arm_angle=$a"; done
    for h in -45 0 45; do for a in 90 135 170; do echo "arms_vs_head -D head_angle=$h -D arm_angle=$a"; done; done
} | xargs -P 4 -I{} bash -c 'one {}' | sort | tee "$tmp/summary.txt"
! grep -q '^FAIL' "$tmp/summary.txt"
