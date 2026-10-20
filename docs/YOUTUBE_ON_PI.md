# YouTube on the Raspberry Pi 5 (2 GB) - investigation

> **Status: nothing in this document has been measured on a Raspberry Pi yet.**
> Numbers marked *estimate* come from general Chromium / ARM experience and must be replaced
> by measurements when the hardware arrives (section 6). The playback code itself is tested
> (section 9 lists exactly what was and wasn't verified).

LittleBot plays **parent-approved** YouTube videos through the **official YouTube IFrame Player
API** in a Chromium window that covers the 480x320 screen while the video plays. The robot never
downloads, extracts or caches video, never searches, and never plays anything that a parent did
not add to the allowlist. Design and safety rules: [modules/media.md](modules/media.md).

## 1. What runs during a video

```
littlebot (Python: face hidden + low fps, audio idle, vision, dashboard server)
   └─ chromium --kiosk --app=http://127.0.0.1:8765/player?token=...   (only while a video plays)
        ├─ browser process, GPU process, network / audio / storage utility processes
        ├─ renderer: our player page (tiny: polls the robot twice a second)
        └─ renderer: the youtube-nocookie.com player iframe (site isolation -> own process)
```

Chromium is started when a session starts and terminated (whole process group) when it ends,
so its memory is only used while a video plays. `MediaController.stop()` - also called on app
shutdown - waits for that termination (SIGTERM, SIGKILL after 2 s), so it does not rely on
systemd: the unit's `KillMode=mixed` only sends SIGTERM to the main Python process. An `atexit`
hook signals any kiosk still running if Python exits without a clean stop.

## 2. Video decoding on the Pi 5

* The Pi 5 (BCM2712 / VideoCore VII) has a hardware decoder **only for HEVC (H.265)**. The
  H.264 hardware decoder of the Pi 4 is gone; Raspberry Pi's position is that the Cortex-A76
  cores decode H.264 fast enough in software. VP9 and AV1 have always been software-only.
* YouTube on the web does **not** serve HEVC, so **every YouTube stream is decoded in
  software** on the Pi 5: VP9 (most likely), AV1 (if Chromium advertises it; decoded by
  dav1d, roughly 1.5-2x the cost of VP9), or H.264 if the Chromium build has proprietary
  codecs. Which one YouTube picks is *unverified* - read it from "Stats for nerds" (section 6).
* **Resolution is the main CPU lever, and we can't set it directly.** YouTube's adaptive
  player picks the quality from the player size x device pixel ratio and the bandwidth.
  `setPlaybackQuality()` / `suggestedQuality` and the old `vq=` URL parameter have been ignored
  since YouTube deprecated manual quality control (2019), so they are not used. What we *can*
  do:
  * keep the player small: it fills the kiosk window, i.e. the screen mode. At 480x320 expect
    roughly 240p-360p; if the HDMI output runs at 720x480 (DISPLAY_SETUP.md option B) expect
    up to 480p (*estimate*);
  * pass `--force-device-scale-factor=1` (done) so a desktop scaling setting can't double the
    pixel count YouTube sees.
* *Estimate*: software VP9 at 360p takes ~10-25 % of one A76 core at 2.4 GHz; Chromium's
  compositing, JavaScript and audio another ~10-30 % of a core. That is about one of four
  cores - feasible, but it competes with the face renderer and camera detection (section 4).

## 3. Memory (2 GB)

| Process (while a video plays) | RSS *estimate* |
|------|------|
| Chromium browser process | 100-150 MB |
| GPU process | 60-120 MB |
| Player page renderer | 40-70 MB |
| YouTube iframe renderer (site isolation) | 120-250 MB |
| Network / audio / storage utility processes | 40-80 MB |
| **Chromium total** | **~400-700 MB** (PSS is lower: shared libraries) |

Add the desktop session (~150-250 MB, DISPLAY_SETUP.md) and LittleBot itself (PERFORMANCE.md):
a 2 GB Pi is **tight but plausible**. What the module already does to help:

* Chromium only exists during a session; `--incognito`, 32 MB disk cache cap, no extensions,
  no sync, a dedicated small profile in `data/media/chromium-profile`.
* The face renderer drops to a very low frame rate while hidden (face module), and the
  behaviour glue ignores camera faces during `PLAYING_MEDIA`.

To try if memory or CPU is short (each must be measured; add via `media.kiosk_extra_args`):

| Option | Effect | Trade-off |
|------|------|------|
| swap / zram | survives peaks instead of OOM-killing | check `swapon --show`; if there is no or only a small swap file: `sudo apt install zram-tools`, set `ALGO=zstd` and `PERCENT=50` in `/etc/default/zramswap` |
| `--renderer-process-limit=2` | fewer renderer processes | none for a single page |
| `--disable-site-isolation-trials` | page + YouTube iframe share one renderer (-100 MB *estimate*) | weaker isolation between our page and YouTube's code |
| pause camera detection during `PLAYING_MEDIA` | frees CPU (vision module; not implemented here) | robot doesn't track faces while the screen shows video anyway |
| HDMI at 480x320 instead of 720x480 | smaller player -> lower stream | see DISPLAY_SETUP.md for mode support |

## 4. Screen, face window and touch

* The pygame face window is full screen. The kiosk Chromium window must appear **above** it
  and the face must be visible again when Chromium exits. *Unverified* under the Pi's Wayland
  compositor (labwc on current Raspberry Pi OS; Wayfire on older Bookworm images). With
  `SDL_VIDEODRIVER=kmsdrm` (no desktop) Chromium cannot show at all - video needs the desktop.
* `--window-size=480,320` is ignored in kiosk mode: the window takes the whole output.
* **Touch**: if the screen's touch panel is active, a child can tap the YouTube player. We do
  **not** put anything over the player to block taps (YouTube forbids overlays in front of
  the player and blocking player functionality - section 8). Instead:
  * `controls=0`, `disablekb=1`, `fs=0`, `rel=0`, `iv_load_policy=3` (documented player
    parameters) leave little to tap;
  * if a tap opens a YouTube link (new tab/window), our page becomes hidden or navigates
    away, reports it, and the robot stops the session and kills the kiosk's whole process
    group - the popup dies with it;
  * the page checks that the video playing is the approved one (or in the approved playlist)
    and tears the player down otherwise;
  * if you don't need touch on the robot, leaving the touch controller unconfigured removes
    the issue entirely.
  * We tried Chromium's `--block-new-web-contents` switch: in this Chromium build (under
    Playwright) it did **not** block `target=_blank` links or `window.open`, so it is not used.

## 5. Audio

Chromium plays through the default PipeWire/PulseAudio output. LittleBot's audio module also
uses the default (PipeWire) device and closes its stream after 3 s of silence, so they should
coexist; the media session holds the `audio_out` lease so LittleBot itself stays quiet. If
`audio.speaker.device` is set to a raw `hw:` ALSA device it is **exclusive** - Chromium (or
LittleBot) then gets "device busy". `media.volume` (0..`media.max_volume` = 0.8) sets the
YouTube player's volume relative to the system volume. *Unverified on the Pi.*

## 6. Measure on arrival

Run LittleBot normally (`--mode pi`, face + camera + audio), then in a second terminal:

1. **Baseline**: `free -m`, `swapon --show`, `scripts/perf_monitor.py` for 2 minutes.
2. **Start a video** from the dashboard (an approved, child-friendly video). Note the time
   from tapping Play to the `media.started` / player `playing` event (dashboard status).
3. **During playback** (5 minutes): `scripts/perf_monitor.py` (counts all Chromium processes),
   `ps -o pid,rss,pcpu,args -C chromium,chromium-browser`, `vcgencmd measure_temp`,
   `vcgencmd get_throttled`, `free -m` (the "available" column must stay > ~150 MB).
4. **Codec and resolution**: on the desktop (not kiosk) open the player link from
   `media.status()["player"]["url"]` in Chromium with a 480x320 window, right-click the
   video -> *Stats for nerds*: note codec (vp09 / av01 / avc1), resolution, dropped frames.
5. **Smoothness** on the robot screen: does the video stutter? Does the face come back
   within ~2 s after "All done!"?
6. **Window stacking**: kiosk appears above the face window; face reappears on exit.
7. **Audio**: sound comes out of the robot's speaker at a sensible level; LittleBot's own
   sounds still work after the video.
8. **Autoplay**: the dashboard never reports `autoplay_blocked` in kiosk mode.
9. **YouTube availability from your home network**: in this project's sandbox every video
   was refused (section 9). Check that a normal embeddable video actually plays at home.
10. **Touch** (if the panel has touch): tap the video, the corners and the YouTube logo;
    playback must either continue or stop cleanly with the face returning - never show YouTube.
11. **Endurance**: three back-to-back 15-minute sessions; then `journalctl -k | grep -i oom`
    (must be empty) and `pgrep -fa chromium-profile` (must be empty after each session).

Record results in the PERFORMANCE.md table and update this document.

## 7. Fallback plan if it is too heavy

In order of preference:

1. **Tune** (section 3 options, HDMI mode, pause vision during video).
2. **Play on another screen**: set `media.player: none`. The dashboard's "open player on this
   device" link (`media.status()["player"]["path"]`, needs `dashboard.lan_access: true` for a
   phone) opens the same player page on a parent's phone, tablet or a TV browser. The same
   allowlist, limits, stop button and end-of-video handling apply (the page obeys the robot);
   the robot shows its calm hidden-face screen meanwhile. Locking the phone hides the page,
   which stops playback (no background playback).
3. **Audio-only songs from the built-in activities** (music category; melodies are
   synthesised locally by the audio module) - no network, no screen takeover. YouTube audio
   is never extracted for this.
4. **Turn video off** (`media.enabled: false` or `media.player: none` without opening the
   link).

## 8. YouTube terms - how the module is designed to comply

Based on the YouTube API Services Developer Policies and the Required Minimum Functionality
(RMF) page as read in October 2026. This is an engineering summary, not legal advice.

* **Official embed only**: IFrame Player API (`https://www.youtube.com/iframe_api`), player
  served from `youtube-nocookie.com` (privacy-enhanced mode) or `youtube.com`.
* **No downloading, caching, offline playback or separating audio/video** (Policies III.E.1,
  III.I.7). The robot never sees stream URLs; the YouTube player fetches its own streams.
* **No ad blocking, no blocking or modifying the player** (III.I.5, III.I.6). Some videos
  contain ads; we don't hide them (YouTube says Made-for-Kids videos only get non-personalised
  ads). Only documented player parameters and API calls are used (`playVideo`, `pauseVideo`,
  `setVolume`, `mute`, `destroy` ...). An earlier draft put a transparent "tap shield" over the
  player; it was removed because RMF forbids "overlays, frames, or other visual elements in
  front of any part of a YouTube embedded player" and III.I.6 forbids blocking player
  functionality. Our calm screens ("All done!", loading, idle) are only shown when **no**
  player exists.
* **Player size**: RMF requires at least 200x200 px and recommends at least 480x270 for 16:9.
  The player fills the 480x320 window (a 480x270 video area) - exactly the recommended size.
* **Autoplay**: RMF requires automatic playback to start only when the player is visible, and
  at most one autoplaying player per screen. The page autoplays only the item the parent just
  chose, in a fully visible full-screen player.
* **No background playback** (III.I.9): if the page is hidden it stops and reports; the robot
  ends the session.
* **End of video**: on `ENDED` the page calls `destroy()` at once and shows its own screen, so
  YouTube's end screen and related videos never render. (`rel=0` alone only restricts related
  videos to the same channel.)
* **Child-directed clients** (III.J): no YouTube write actions, no personalised ads (we serve
  none), no data collection (the robot stores only the allowlist and daily minutes), and
  privacy-enhanced embeds. The IFrame API needs no API key; the policies have no explicit
  personal-use exemption and say API clients must comply - whether a private household robot
  needs an API project or audit is **unclear**; read the terms yourself.
* **No AI-selected content**: the robot never searches YouTube or picks videos.

## 9. What was verified, and how

| Item | Status | How |
|------|------|------|
| URL parsing / validation, allowlist CRUD, duplicates, titles | verified | pytest (`tests/media/test_media_urls.py`, `test_media_repository.py`) |
| Age gate, limits, leases, state, events, STOP_ALL, preemption, skip, volume | verified | pytest with fakes (`test_media_controller.py`) |
| Player bridge: token (401/403), polling, events, stale-event filtering | verified | pytest + httpx ASGI (`test_media_player_routes.py`) |
| Kiosk command line, launch, SIGTERM/SIGKILL of the process group, missing browser | verified with a fake launcher and a real dummy process | `test_media_display.py` |
| Player page JS state machine (load, pause/resume, volume, ENDED -> destroy + "All done!", errors, unapproved video, playlist, limit fade, hidden page, stale token) | verified **with a stubbed `window.YT`** | headless Chromium via Playwright (`test_media_player_page.py`) |
| Real IFrame API script loads under our CSP, player iframe created, Referer accepted (no error 153), real `ready` / `buffering` / `error` events reach the robot, error 150 mapped and shown | verified once, **manually** in the dev sandbox (headless Chromium, datacenter network) | not a committed test |
| **Real video playback** | **NOT verified** | YouTube answered "Video unavailable - This content isn't available" (reported as error 150) for every video tried (`jNQXAC9IVRw`, `aqz-KE-bpKQ`, `M7lc1UVf-VE`) on both embed hosts, most likely because of the sandbox's datacenter IP / headless browser. Opening an embed URL directly (no embedding page) gave error 153 ("player configuration error", missing referrer), confirming why the page sends `Referrer-Policy: strict-origin-when-cross-origin`. |
| Pi CPU / memory / decode / thermal, kiosk stacking, touch, audio device sharing | **NOT verified** | needs the hardware (section 6) |
