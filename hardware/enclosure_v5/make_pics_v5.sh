#!/usr/bin/env bash
# Re-make the README's pictures (needs xvfb-run and Pillow):  OPENSCAD=... ./make_pics_v5.sh
set -euo pipefail
cd "$(dirname "$0")"
OPENSCAD=${OPENSCAD:-openscad}
tmp=$(mktemp -d)
shot() { xvfb-run -a -s "-screen 0 1700x1700x24" "$OPENSCAD" -q -o "$tmp/$1.png" --imgsize="$2" --camera="$3" --colorscheme=Tomorrow "${@:5}" "$4" 2>&1 | grep -v -i warning || true; }
shot front 1000,1300 "0,78,225,80,0,22,1500" littlebot_v5.scad &
shot back 1000,1300 "0,78,225,78,0,200,1500" littlebot_v5.scad &
shot side 1000,1300 "0,78,225,90,0,90,1400" littlebot_v5.scad --projection=o &
wait
shot neck 1500,900 "0,100,236,82,0,180,420" pics_v5.scad -D 'pic="neck"' &
shot base 1100,1100 "0,78,45,0,0,0,640" pics_v5.scad -D 'pic="base"' --projection=o &
shot deck 1100,880 "0,78,45,48,0,22,640" pics_v5.scad -D 'pic="deck"' &
wait
for v in "above 0,95,212,55,0,0,430" "below 0,95,205,118,0,0,430"; do set -- $v
    shot stems_$1 1100,800 "$2" pic_stems_v4.scad -D detail=false &
    shot collar_$1 1100,800 "$2" pics_v5.scad -D 'pic="collar"' -D detail=false &
done
wait
shot capoff 1100,800 "0,80,232,38,0,0,360" pics_v5.scad -D 'pic="capoff"' -D detail=false &
shot capon 1100,800 "0,80,232,38,0,0,360" pics_v5.scad -D 'pic="capon"' -D detail=false &
wait
python3 - "$tmp" <<'PY'
import sys
from PIL import Image, ImageDraw, ImageFont
tmp = sys.argv[1]
def font(sz, bold=False):
    for f in (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else []) + ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        try: return ImageFont.truetype(f, sz)
        except OSError: pass
    return ImageFont.load_default()
bg = (248, 248, 248)
ims = [Image.open(f"{tmp}/{n}.png").convert("RGB") for n in ("front", "side", "back")]
W = sum(i.width for i in ims); H = max(i.height for i in ims)
out = Image.new("RGB", (W, H + 110), bg); x = 0
for i in ims: out.paste(i, (x, 110)); x += i.width
d = ImageDraw.Draw(out)
d.text((40, 28), "LITTLEBOT v5 — ROVER", fill=(25, 25, 25), font=font(54, True))
d.text((40, H + 60), "v4's head and arms on a new body and a wheeled base  •  84 interference checks pass  •  4 plates on a Bambu A1", fill=(90, 90, 90), font=font(26))
out.resize((W * 2 // 3, (H + 110) * 2 // 3), Image.LANCZOS).save("LittleBot_v5_model.png")
Image.open(f"{tmp}/neck.png").convert("RGB").save("neck_section.png")
# the base from above (orthographic: 640 * 2 * tan(11.25 deg) mm across the picture's height), labelled; beside it, the deck on
b = Image.open(f"{tmp}/base.png").convert("RGB"); d = ImageDraw.Draw(b); k = b.height / (640 * 0.39782); f = font(20)
def px(x, y): return (b.width / 2 + x * k, b.height / 2 - (y - 78) * k)
for y in (0, 144.6):
    cx, cy = px(0, y); d.ellipse([cx - 9.5 * k, cy - 9.5 * k, cx + 9.5 * k, cy + 9.5 * k], outline=(230, 60, 60), width=3)
for text, x, y in [("wheel pod", -87, 70), ("wheel pod", 87, 70), ("2 gear motors", 0, 70), ("Pi 5 + UPS", 0, 118), ("6xAA drawer", 0, 22), ("battery door", -30, -30),
                   ("drive switch", 96, -24), ("caster (under the floor)", 0, 0), ("caster (under the floor)", 0, 144.6), ("USB-C charging", -76, 172), ("deck post", -55.5, 47),
                   ("deck post", 55.5, 47), ("deck post", -55.5, 88), ("deck post", 55.5, 88), ("fan vents", -62, 140), ("FRONT", 0, -44), ("BACK", 0, 190)]:
    cx, cy = px(x, y); w = d.textlength(text, font=f)
    d.rectangle([cx - w / 2 - 5, cy - 14, cx + w / 2 + 5, cy + 14], fill=(255, 255, 255), outline=(120, 120, 120))
    d.text((cx - w / 2, cy - 12), text, fill=(20, 20, 20), font=f)
dk = Image.open(f"{tmp}/deck.png").convert("RGB")
out = Image.new("RGB", (b.width + dk.width, b.height), bg); out.paste(b, (0, 0)); out.paste(dk, (b.width, (b.height - dk.height) // 2))
d = ImageDraw.Draw(out); d.text((b.width + 30, (b.height - dk.height) // 2 - 10), "with the deck on: motor driver (red), servo power board (brown),", fill=(60, 60, 60), font=font(22))
d.text((b.width + 30, (b.height - dk.height) // 2 + 18), "RP2040 on its breadboard (green on white), 5 V converter (blue)", fill=(60, 60, 60), font=font(22))
out.resize((out.width * 3 // 4, out.height * 3 // 4), Image.LANCZOS).save("base_top.png")
# v4's stems beside v5's collar, from above and from below
g = [[Image.open(f"{tmp}/{n}_{v}.png").convert("RGB") for n in ("stems", "collar")] for v in ("above", "below")]
w, h = g[0][0].size; out = Image.new("RGB", (2 * w, 2 * h + 90), bg)
for r, row in enumerate(g):
    for c, im in enumerate(row): out.paste(im, (c * w, 90 + r * h))
d = ImageDraw.Draw(out)
d.text((40, 24), "v4: 4 stems carry the neck and head", fill=(25, 25, 25), font=font(40, True))
d.text((w + 40, 24), "v5: a solid collar, part of the wall", fill=(25, 25, 25), font=font(40, True))
d.text((40, 100), "from above, the front cut away", fill=(90, 90, 90), font=font(26)); d.text((40, 100 + h), "from below, looking up", fill=(90, 90, 90), font=font(26))
d.line([(w, 90), (w, 2 * h + 90)], fill=(200, 200, 200), width=2)
out.resize((out.width * 2 // 3, out.height * 2 // 3), Image.LANCZOS).save("collar_vs_stems.png")
# the cap's screws
g = [Image.open(f"{tmp}/{n}.png").convert("RGB") for n in ("capoff", "capon")]
w, h = g[0].size; out = Image.new("RGB", (2 * w, h + 100), bg)
for c, im in enumerate(g): out.paste(im, (c * w, 100))
d = ImageDraw.Draw(out)
d.text((40, 20), "The collar: 8 holes, ringed in red", fill=(25, 25, 25), font=font(38, True))
d.text((40, 68), "part of the body; nothing to put in them", fill=(90, 90, 90), font=font(26))
d.text((w + 40, 20), "Cap on: 8 screws straight down from the top", fill=(25, 25, 25), font=font(38, True))
d.text((w + 40, 68), "M3 x 12, they cut their own thread; before the head goes on", fill=(90, 90, 90), font=font(26))
d.line([(w, 100), (w, h + 100)], fill=(200, 200, 200), width=2)
out.resize((out.width * 2 // 3, out.height * 2 // 3), Image.LANCZOS).save("cap_screws.png")
PY
rm -rf "$tmp"
