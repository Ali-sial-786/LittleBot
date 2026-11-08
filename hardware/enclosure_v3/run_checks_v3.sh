#!/usr/bin/env bash
# Run the v3 interference checks (checks_v3.scad) in parallel. PASS = empty result (or zero volume: designed contact).
#   ./run_checks_v3.sh                          # the full list
#   ./run_checks_v3.sh "arms_vs_body -D arm_angle=45"
set -uo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
tmp=$(mktemp -d)
one() {
    local name=$1; shift
    local tag; tag=$(echo "$name $*" | tr -c 'a-zA-Z0-9_' '_')
    $OPENSCAD -o "$tmp/$tag.stl" -D "check=\"$name\"" "$@" checks_v3.scad > "$tmp/$tag.log" 2>&1
    local vol=0
    if grep -q "facet" "$tmp/$tag.stl" 2>/dev/null; then vol=$(python3 ../enclosure_v2/stl_tools.py volume "$tmp/$tag.stl" 2>/dev/null | awk '{print $1}'); fi
    local empty; empty=$(awk -v v="$vol" 'BEGIN { print (v < 0.01) ? 1 : 0 }')
    if [[ $name == rod_reaches_button ]]; then empty=$((1 - empty)); fi
    if ((empty)); then printf 'PASS  %-22s %s\n' "$name" "$*"; else printf 'FAIL  %-22s %s  (%s mm3: %s)\n' "$name" "$*" "$vol" "$tmp/$tag.stl"; fi
}
export -f one; export tmp OPENSCAD
{
    if (($#)); then printf '%s\n' "$@"; else
    for c in body_vs_electronics body_vs_floor body_vs_plate plate_vs_neck frame_vs_plate rod_vs_parts rod_reaches_button selftest_must_fail; do echo "$c"; done
    for h in -60 0 60; do echo "body_vs_neck -D head_angle=$h"; echo "head_vs_body -D head_angle=$h"; done
    for a in -90 -60 -30 0 8 30 60 90 100; do echo "arms_vs_body -D arm_angle=$a"; echo "arms_vs_plate -D arm_angle=$a"; done
    for h in -60 0 60; do for a in 60 90 100; do echo "pinch_arm_head -D head_angle=$h -D arm_angle=$a"; done; done
    fi
} | xargs -P "${JOBS:-4}" -I{} bash -c 'one {}' | sort | tee "$tmp/summary.txt"
# the self-test must FAIL (find an overlap), everything else must pass
fails=$(grep '^FAIL' "$tmp/summary.txt" | grep -vc selftest_must_fail)
grep -q '^FAIL  selftest_must_fail' "$tmp/summary.txt" || { (($# == 0)) && { echo "self-test found no overlap: the checks prove nothing"; fails=$((fails + 1)); }; }
echo "$fails failed (besides the self-test, which must fail)"
exit $((fails > 0))
