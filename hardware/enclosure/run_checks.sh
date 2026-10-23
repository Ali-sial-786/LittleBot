#!/usr/bin/env bash
# Run the interference checks in checks.scad (in parallel).
# PASS = the two things never intersect (OpenSCAD reports an empty result).
#   ./run_checks.sh                                   # the full list below
#   ./run_checks.sh "arms_vs_head -D arm_angle=170"   # just these
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
    if (($#)); then printf '%s\n' "$@"; else
    for c in electronics_vs_shell electronics_vs_base electronics_overlap pinion_vs_shell \
             frame_vs_servo frame_vs_parts camera_vs_shell stack_vs_head carrier_vs_head pi_screws_clear stack_internal \
             gears_mesh_clear; do echo "$c"; done
    for a in -45 -25 0 25 45; do echo "neck_vs_shell -D head_angle=$a"; echo "head_vs_body -D head_angle=$a"; done
    for a in -30 0 45 90 135 170; do echo "arms_vs_body -D arm_angle=$a"; done
    for h in -45 -25 0 25 45; do for a in 90 135 160 170; do echo "arms_vs_head -D head_angle=$h -D arm_angle=$a"; done; done
    fi
} | xargs -P "${JOBS:-4}" -I{} bash -c 'one {}' | sort | tee "$tmp/summary.txt"
echo "$(grep -c '^PASS' "$tmp/summary.txt") passed, $(grep -c '^FAIL' "$tmp/summary.txt") failed"
status=0; grep -q '^FAIL' "$tmp/summary.txt" && status=1
if (($# == 0)); then
    # The self-test must find an overlap, otherwise the checks above prove nothing.
    openscad -o "$tmp/selftest.stl" -D 'check="selftest_must_fail"' checks.scad > "$tmp/selftest.log" 2>&1
    if grep -q "facet" "$tmp/selftest.stl" 2>/dev/null; then echo "PASS  selftest_must_fail (an overlap is detected)"; else echo "FAIL  selftest_must_fail"; status=1; fi
    # Parts compatibility from the drawings (not the enclosure): reported, never fails the run.
    openscad -o "$tmp/info.stl" -D 'check="info_cooler_vs_screen"' checks.scad > "$tmp/info.log" 2>&1
    if grep -q "facet" "$tmp/info.stl" 2>/dev/null; then
        echo "INFO  Active Cooler vs screen (drawings): overlap $(python3 stl_tools.py bbox "$tmp/info.stl") - see README, 'The screen sits on the Pi'"
    else echo "INFO  Active Cooler vs screen (drawings): no overlap"; fi
fi
exit $status
