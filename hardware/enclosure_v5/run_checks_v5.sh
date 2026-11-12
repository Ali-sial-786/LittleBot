#!/usr/bin/env bash
# Run the v5 interference checks (checks_v5.scad) in parallel. PASS = empty result (or zero volume: designed contact).
#   ./run_checks_v5.sh                          # the full list
#   ./run_checks_v5.sh "arms_vs_body -D arm_angle=45"
set -uo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
tmp=$(mktemp -d)
one() {
    local name=$1; shift
    local tag; tag=$(echo "$name $*" | tr -c 'a-zA-Z0-9_' '_')
    $OPENSCAD -o "$tmp/$tag.stl" -D "check=\"$name\"" "$@" checks_v5.scad > "$tmp/$tag.log" 2>&1
    local vol=0
    if grep -q "facet" "$tmp/$tag.stl" 2>/dev/null; then vol=$(python3 ../enclosure_v2/stl_tools.py volume "$tmp/$tag.stl" 2>/dev/null | awk '{print $1}'); fi
    local empty; empty=$(awk -v v="$vol" 'BEGIN { print (v < 0.01) ? 1 : 0 }')
    if [[ $name == slider_moves_knob || $name == keeper_rests || $name == mic_key_clamps || $name == mic_screws_bite || $name == spk_keeper_holds || $name == joint_nuts_held || $name == head_nuts_held || $name == cap_nuts_held ]]; then empty=$((1 - empty)); fi
    if ((empty)); then printf 'PASS  %-22s %s\n' "$name" "$*"; else printf 'FAIL  %-22s %s  (%s mm3: %s)\n' "$name" "$*" "$vol" "$tmp/$tag.stl"; fi
}
export -f one; export tmp OPENSCAD
{
    if (($#)); then printf '%s\n' "$@"; else
    for c in tub_vs_stuff tub_vs_parts pods_vs_stuff pods_vs_parts tray_vs_stuff tray_vs_parts tray_slides_out deck_vs_stuff deck_vs_keeper keeper_fit keeper_rests stuff_vs_stuff motor_screws_clear \
             body_vs_tub body_vs_base body_goes_on joint_screws_clear joint_nuts_held joint_nuts_go_in body_vs_electronics body_vs_cap neck_goes_in frame_on_pillars hub_vs_neck head_vs_neck head_screws_clear head_nuts_held cap_screws_clear cap_nuts_held \
             slider_vs_parts slider_grips_knob slider_moves_knob mic_holder_vs_body mic_fit mic_key_fit mic_key_clamps mic_key_slides_in mic_screws_bite \
             spk_keeper_fit spk_keeper_goes_in spk_keeper_holds chest_vs_base selftest_must_fail; do echo "$c"; done
    for h in -60 0 60; do echo "cap_vs_neck -D head_angle=$h"; echo "body_vs_neck -D head_angle=$h"; echo "chest_vs_neck -D head_angle=$h"; echo "hub_vs_gears -D head_angle=$h"; echo "head_vs_body -D head_angle=$h"; done
    for a in -90 -60 -30 0 8 30 60 90 95; do echo "arms_vs_body -D arm_angle=$a"; done
    for h in -60 0 60; do for a in 60 90 95; do echo "pinch_arm_head -D head_angle=$h -D arm_angle=$a"; echo "arms_vs_neck -D head_angle=$h -D arm_angle=$a"; done; done
    fi
} | xargs -P "${JOBS:-4}" -I{} bash -c 'one {}' | sort | tee "$tmp/summary.txt"
# the self-test must FAIL (find an overlap), everything else must pass
fails=$(grep '^FAIL' "$tmp/summary.txt" | grep -vc selftest_must_fail)
grep -q '^FAIL  selftest_must_fail' "$tmp/summary.txt" || { (($# == 0)) && { echo "self-test found no overlap: the checks prove nothing"; fails=$((fails + 1)); }; }
echo "$fails failed (besides the self-test, which must fail)"
exit $((fails > 0))
