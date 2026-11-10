#!/usr/bin/env bash
# Run the v4 interference checks (checks_v4.scad) in parallel. PASS = empty result (or zero volume: designed contact).
#   ./run_checks_v4.sh                          # the full list
#   ./run_checks_v4.sh "arms_vs_body -D arm_angle=45"
set -uo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
tmp=$(mktemp -d)
one() {
    local name=$1; shift
    local tag; tag=$(echo "$name $*" | tr -c 'a-zA-Z0-9_' '_')
    $OPENSCAD -o "$tmp/$tag.stl" -D "check=\"$name\"" "$@" checks_v4.scad > "$tmp/$tag.log" 2>&1
    local vol=0
    if grep -q "facet" "$tmp/$tag.stl" 2>/dev/null; then vol=$(python3 ../enclosure_v2/stl_tools.py volume "$tmp/$tag.stl" 2>/dev/null | awk '{print $1}'); fi
    local empty; empty=$(awk -v v="$vol" 'BEGIN { print (v < 0.01) ? 1 : 0 }')
    if [[ $name == slider_moves_knob || $name == pivot_screws_bite || $name == keeper_rests ]]; then empty=$((1 - empty)); fi
    if ((empty)); then printf 'PASS  %-22s %s\n' "$name" "$*"; else printf 'FAIL  %-22s %s  (%s mm3: %s)\n' "$name" "$*" "$vol" "$tmp/$tag.stl"; fi
}
export -f one; export tmp OPENSCAD
{
    if (($#)); then printf '%s\n' "$@"; else
    for c in body_vs_electronics ring_vs_electronics body_vs_floor ring_vs_floor body_vs_ring body_vs_plate plate_vs_neck frame_vs_plate \
             slider_vs_parts slider_grips_knob slider_moves_knob chest_vs_neck panel_vs_body box_vs_head head_vs_screen plugs_go_in glass_untouched cover_vs_frame hood_vs_camera \
             pivot_screws_clear pivot_screws_bite cradle_vs_frame cradle_vs_camera cradle_screws_clear keeper_fit keeper_rests mic_holder_vs_body mic_fit mic_vs_parts selftest_must_fail; do echo "$c"; done
    for h in -60 0 60; do echo "body_vs_neck -D head_angle=$h"; echo "head_vs_body -D head_angle=$h"; echo "puck_vs_static -D head_angle=$h"; echo "puck_vs_coupler -D head_angle=$h"; done
    for a in -90 -60 -30 0 8 30 60 90 95; do echo "arms_vs_body -D arm_angle=$a"; echo "arms_vs_plate -D arm_angle=$a"; done
    for h in -60 0 60; do for a in 60 90 95; do echo "pinch_arm_head -D head_angle=$h -D arm_angle=$a"; done; done
    fi
} | xargs -P "${JOBS:-4}" -I{} bash -c 'one {}' | sort | tee "$tmp/summary.txt"
# the self-test must FAIL (find an overlap), everything else must pass
fails=$(grep '^FAIL' "$tmp/summary.txt" | grep -vc selftest_must_fail)
grep -q '^FAIL  selftest_must_fail' "$tmp/summary.txt" || { (($# == 0)) && { echo "self-test found no overlap: the checks prove nothing"; fails=$((fails + 1)); }; }
echo "$fails failed (besides the self-test, which must fail)"
exit $((fails > 0))
