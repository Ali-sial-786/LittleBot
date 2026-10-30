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
littlebot (Python: face hidden + low fps, audio idle, vision, dashboard server, wake word listening)
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

The wake word keeps listening while a video plays (section 10), so the video's own sound reaches
the microphone. Without echo cancelling (the default) a quieter `media.volume` makes "Hey LittleBot"
much easier to hear (section 10 has the numbers). With `scripts/setup_echo_cancel.sh`
([PI_SETUP.md](PI_SETUP.md#echo-cancelling)) PipeWire's echo-cancelled speaker becomes the default
output, so Chromium plays through it and much of the video is taken out of what the wake word hears
(*unverified* on the Pi; LittleBot opens the same PipeWire devices, which are not exclusive).

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
12. **Talking during a video** (section 10): play a song video at the usual volume and say "Hey
    LittleBot" from where the child sits (1-2 m), ten times: how many paused the video? Then let a
    few typical videos play for 30 minutes with nobody talking and count the false starts
    (`journalctl --user-unit littlebot | grep -c "wake word heard"`) and what was ignored
    (`grep "ignored while a video plays"`). Check that the earcon is heard, the face is back
    within ~2 s, "keep watching" carries on at the right place (Chromium restarts: note how long
    it takes), and `free -m` during a video with the wake word on (the model stays loaded).

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
| Pause for a chat (`hold`): position, face back, kiosk closed, resume where it was, countdown, tools, "keep watching" / "stop the video" | verified with fakes | pytest (`tests/media/test_media_hold.py`, `tests/audio/test_wakeword_video.py`, `tests/chatgpt/test_live_video.py`) |
| Wake word over a video's sound | measured with **synthetic** voices and music (section 10) | real rooms, children's voices and the Pi's microphone / speaker: **NOT verified** |

## 10. Talking to LittleBot during a video

"Hey LittleBot" works while a video plays: the video pauses, the face comes back and the live voice
starts, as it does when LittleBot is idle. Afterwards the video carries on where it was, or stops.

### What happens, step by step

1. **Listening during the video.** The wake word (littlebot/audio/wakeword.py) keeps listening
   while a video plays (`wake.during_videos`). The video's sound reaches the microphone too, so
   stricter rules apply then: only `wake.video_phrases` count (default: just "hey little bot";
   "hey bot" alone is what videos say by mistake most) and each word needs
   `wake.video_min_confidence` (0.7, instead of 0.35). Phrases heard that don't count are logged:
   `wake word: 'hey bot' ignored while a video plays (...)`; the dashboard's wake word line says
   "Listening for “hey little bot” (a video is playing)."
2. **"Hey LittleBot" heard.** The video is paused first (`MediaController.hold`): the page is told
   to pause and LittleBot waits up to `wake.video_pause_wait_seconds` (1 s) for its "paused"
   report, which has the exact position (without one, the position is the last report plus the
   time since). The player is then torn down and the kiosk Chromium closed (YouTube's rules allow
   no hidden player, and the kiosk window covers the face); the screen, speaker and face go back
   to LittleBot (`PLAYING_MEDIA -> IDLE`) and the face shows again. No screen time is counted while
   the video waits.
3. **Earcon and live voice.** Then, exactly as when idle: the "listen" earcon (heard over a quiet
   speaker, because the video paused first), the face listens, live voice starts. When its data
   channel opens, the voice is told (a note) that the video "<title>" is paused and how to carry
   on with it or stop it (the title is shortened as for anything said aloud, `speakable_title`).
4. **"Keep watching", "resume the video", "play it again", "go back to the video"**, said by the
   person (LittleBot recognises their own words: `VIDEO_RESUME` in live.py) or handed over by the
   voice: the voice is told to say a short goodbye, the call ends as with any hand-over, and the
   video carries on **2 s before** where it stopped (Chromium starts again: a few seconds on a Pi,
   *estimate*). Once asked for, it carries on even if the call ends before the goodbye.
5. **"Stop the video", "turn it off", "no more videos"**: the video stops at once; the face stays
   and the chat goes on.
6. **Neither:**
   * the chat couldn't start or connect (not signed in, no internet ...): "uh-oh", and the video
     carries on;
   * nobody said anything within `wake.video_no_words_seconds` (15 s) of the call connecting (most
     likely the wake word heard the video itself): the call ends and the video carries on;
   * otherwise (they talked, and said nothing about the video) the video stays paused with the
     face showing. The dashboard (Home and Videos) shows "Paused at m:ss for a chat. It stops in
     ..." with Resume and Stop, and it stops `media.chat_pause_minutes` (2) after the chat ended.
     Tapping Resume, or "Hey LittleBot ... keep watching", carries on meanwhile.
7. The text model's tools do the same in typed chat and in hand-offs: `resume_video` carries on
   with a video paused for a chat once the reply was spoken / the call ended, `stop_video` stops
   it, `get_robot_status` says `paused_while_talking`.

The parent's own **Pause** on the dashboard is unchanged (the paused player stays on the screen).

### Settings

```yaml
wake:                                # config/local.yaml
  during_videos: true                # false: not listening while a video plays (frees the ~130 MB model then)
  video_phrases: ["hey little bot"]  # only these count during a video; [] = all of wake.phrases
  video_min_confidence: 0.7          # per word during a video (wake.min_confidence is 0.35)
  video_duck: false                  # turn the video down after a "hey ..." (see below)
  video_duck_volume: 0.3             # ... to this share of its volume
  video_duck_seconds: 2.0            # ... for this long
  video_pause_wait_seconds: 1.0      # the earcon waits at most this long for the video to pause
  video_no_words_seconds: 15         # nobody talks this long after the call connected: back to the video (0 = off)
media:
  chat_pause_minutes: 2              # a video paused for a chat stops this long after the chat
```

### What was measured (and what wasn't)

Measured on a desktop with the real recogniser (Vosk small English model, LittleBot's grammar,
fed 0.1 s at a time as on the robot) and **synthetic** sound: espeak-ng voices saying the wake
phrases (8 voices x 2 speeds), mixed with a "video" played through a simulated room (0.35 s
reverb): kids-video style speech (30 lines from 4 other voices, with many near misses such as
"hey little boy", "hey buddy", "hey, but ..."), synthetic music (melody, bass, drums), or both
(a "song"). SNR is the person's level over the video's at the microphone.

| | all phrases, 0.5 (before) | "hey little bot" only, 0.7 (video default) |
|------|------|------|
| False starts, 30 min of video speech | 27 (54 / h) | 5 (10 / h) |
| False starts, 30 min of song | 16 (32 / h) | 3 (6 / h) |
| False starts, 30 min of music without words | 0 | 0 |
| Clips of single video lines that started it (of 360) | 16 | 1 |

The corpus says "hey" in almost every line, so these rates are a worst case; real videos should
start it far less often. A stricter confidence alone helps little (most false starts have word
confidence 1.0: 27 -> 23 at 0.7); keeping only the long phrase does most of it.

"Hey little bot" heard (said by the person), with the video defaults:

| Video sound | +20 dB | +10 dB | +5 dB | 0 dB | -5 dB |
|------|------|------|------|------|------|
| music | 78 % | 66 % | 62 % | 44 % | 3 % |
| song (music + words) | 75 % | 62 % | 22 % | 12 % | 3 % |
| speech | 44 % | 28 % | 9 % | 6 % | 6 % |

Without a video the same clips are heard 78 % of the time (94 % for "hey little bot"; espeak-ng's
"hey littlebot" in one word only 62 %). So it works when the person is clearly louder than the
video at the microphone, and mostly fails against loud talking or singing. On the robot the speaker is centimetres from the microphone and
the child a metre or two away, so the SNR may well be 0 dB or less at a normal video volume:
**this is the part to check on the Pi** (section 6, step 12). What helps, best first:

* a lower `media.volume` (moves you to the left in the table: half the volume is 6 dB);
* saying it louder or closer, and a short pause after "hey";
* PipeWire's echo cancellation (`libpipewire-module-echo-cancel`, a WebRTC echo canceller):
  `scripts/setup_echo_cancel.sh` ([PI_SETUP.md](PI_SETUP.md#echo-cancelling)) makes its sink the
  default output (Chromium plays into it) and LittleBot records from its source, so much of the
  video should be removed from what the wake word hears. Not measured: *unverified* on the Pi;
* `wake.video_duck: true`. With the video turned down to 30 % 0.6 s after a heard "hey" (the page
  polls twice a second), "hey little bot" was heard more often against speech (+10 dB: 28 -> 44 %,
  +5 dB: 9 -> 28 %, 0 dB: 6 -> 16 %) and songs (+5 dB: 22 -> 31 %), not against music. But the
  recogniser heard "hey" 7-10 times a minute in this corpus, and each one dips the video for
  `video_duck_seconds`, so it is off by default. Try it if the robot misses you during videos.

Not measured: real children's and adults' voices, accents, real rooms, the Pi's USB microphone and
speaker, how often real videos start it, and the Pi's CPU for listening during a video (about 4 %
of one desktop core measured for the wake word; roughly 10 % of one Pi 5 core expected, on top of
Chromium). The wake word's model now stays loaded during a video (about 130 MB); on a 4 GB Pi there
is room for it. On a 2 GB one it is tight next to Chromium (section 3): `wake.during_videos: false`
frees it, as before.
