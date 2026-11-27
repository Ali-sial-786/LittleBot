# Android on the robot (Waydroid)

**Why.** Aosu has no web viewer, no API and no video stream (no RTSP or ONVIF on the home network: checked
2026-11-03, docs/DOORBELL.md). Its phone app is the only way to see the doorbell's video. Waydroid runs a real
Android (LineageOS with Google Play) in a container on the Pi, in a window on the robot's screen, so that app can
run on the robot itself.

**Status: not tried on the robot yet.** `scripts/setup_waydroid.sh` does it step by step, and every step can be
undone. LittleBot's side is built (littlebot/android, 2026-11-04): it opens Android when asked and **shuts it
down as soon as it isn't on the screen**, so its memory is only in use while you are looking at it (below).

## What is known (checked on the robot, 2026-11-03)

| | |
|---|---|
| Ready | binder (Android's messaging) is built into the robot's kernels; the desktop (labwc) is there for Android's window; about 3 GB of memory and 24 GB of disk free; Waydroid 1.6.3 is in Debian's trixie-backports |
| Needed | the Pi's **4K-page kernel** (`kernel8.img`, the one the Pi 4 uses). The Pi 5 normally runs a 16K-page kernel, and Android's images only work with 4K pages. It costs a few percent of speed. Also `psi=1` (memory-pressure reporting, which Android uses) |
| Not known until tried | whether the Aosu app's **live view** plays in Waydroid (BlueStacks lists Aosu as supported, but there are no reports on live view, and Wyze and Eufy apps have broken in emulators before); how smooth 2K video is (the Pi 5 has no video decoder chip, so Android decodes in software); how well the Pi's graphics work in Android |
| Google Play | Waydroid's Android isn't certified by Google. Its ID is registered once, at google.com/android/uncertified (step 4), and Play works after that |

## Steps

Run them on the robot as the desktop user. Each step prints its command and runs it only after you answer **y**
(sudo asks for your password).

| Step | Command | What it does |
|---|---|---|
| check | `scripts/setup_waydroid.sh` | what is there and what is missing (changes nothing) |
| 1 | `scripts/setup_waydroid.sh kernel` then `sudo reboot` | backs up `/boot/firmware/config.txt` and `cmdline.txt` (`*.littlebot-16k`), adds `kernel=kernel8.img` in a marked block, adds `psi=1` |
| 2 | `scripts/setup_waydroid.sh install` | installs Waydroid from trixie-backports, downloads Android with Google Play (about 1 GB, from Waydroid's own server), and starts Android's container service at every boot (`systemctl enable --now waydroid-container`: LittleBot then starts Android without a password; it idles at about 40 MB) |
| 3 | `scripts/setup_waydroid.sh start` | starts Android (a minute or two the first time) and shows it full screen; sign in to Google Play there |
| 4 | `scripts/setup_waydroid.sh play-id` | prints Android's ID: register it at https://www.google.com/android/uncertified, wait a few minutes, then `stop` and `start` |
| 5 | `scripts/setup_waydroid.sh aosu` | opens the Aosu app (install "aosu" from Play first; its package is `com.glazero.android`). Try a camera's live view |
| type | `ssh -t alisial3@littlebot.local LittleBot/scripts/setup_waydroid.sh type` | no keyboard on the robot: tap a box in Android (email, password, code), then type the text on your computer. It isn't shown or saved, and goes into Android through `sudo waydroid shell` on stdin (Android's `input text`), never on a command line. An empty line ends it |
| off | `scripts/setup_waydroid.sh stop` | Android off (its memory back for LittleBot). LittleBot does this by itself once Android isn't on the screen |
| undo | `scripts/setup_waydroid.sh undo` then `sudo reboot` | the usual 16K kernel back (the marked block and `psi=1` removed; one blank line stays in config.txt). `undo --remove` also uninstalls Waydroid and deletes Android with everything signed in to it |

Over ssh, add `-t` so sudo can ask for the password: `ssh -t alisial3@littlebot.local LittleBot/scripts/setup_waydroid.sh kernel`.

## Only while it is on the screen

Android uses about 1 to 1.5 GB while it runs, out of the robot's 4 GB. So LittleBot runs it only while it is on
the screen (littlebot/android/service.py):

| | |
|---|---|
| Open | "Hey LittleBot, open Android", "open the Aosu app", or **Open Android** on the dashboard (Assistant page). The first start takes a minute or two; later ones less. It doesn't start with less than 1200 MB free (`android.min_free_mb`) |
| Shut down | its window closed (its title bar's X), or another window in front of it for 10 s (`android.off_screen_seconds`); the screen going dark after the quiet minutes (`face.screen_off_after_seconds`, 3 min: touching Android counts as something happening); "close Android"; the dashboard's **Close Android**; a video, game, activity, phone mirroring, the baby camera or the browser taking the screen; STOP ALL; LittleBot going to sleep; free memory under 250 MB (`android.min_free_mb_running`); its window not coming within 3 minutes of starting |
| Started by hand | `scripts/setup_waydroid.sh start` works too: LittleBot notices that Android within 15 s and shuts it down the same way |
| Shut down means | `waydroid session stop`: Android's processes end and the memory comes back. The log says how much, in a line like `android: Android is off (its window was closed) after <s> s; <free> MB of memory free (+<back> MB)` |
| While it's on | the wake word and the voice keep working ("Hey LittleBot, close Android"). "Stop" ends the chat but doesn't put LittleBot to sleep (that would close Android) |

How it knows: every 2 s it asks the desktop (labwc) which window is in front, over the compositor's own socket
(littlebot/android/windows.py; to see the desktop's windows yourself: `python3 -m littlebot.android.windows` on the
robot). Android's window is the one whose app id or title has "waydroid" in it (`android.window_match`). Touches
on the screen go to Android, not to LittleBot's face, so while Android runs LittleBot reads the touch screen's
input device directly, only the time of the last touch (littlebot/android/inputs.py).

Don't run Android during heavy jobs (wake-word training): it won't start without enough free memory anyway.

**Privacy.** Your Google and Aosu sign-ins live in the robot's Android only (`/var/lib/waydroid`,
`~/.local/share/waydroid`). Anyone with the robot's login can reach them. `undo --remove` deletes them.

**If it doesn't work.** `sudo waydroid log` says why Android didn't start. If Android works but Aosu's live view
doesn't play, the app probably refuses emulators. Then nothing on the robot will show it, and the cloud below won't
either.

**If it works.** "Open the Aosu app" already opens it (`android.apps`). Opening the doorbell's live view by itself
on a ring isn't built yet.

## In the cloud instead

If the Pi can't run it well, the same Android can run on a small rented ARM server: **Redroid** (Android in
Docker, arm64 images, Google Play added with its community script) on something like a Hetzner CAX11 (about €5
a month in 2026). It would be reached only through an SSH tunnel and shown on the robot with its Android
mirroring (scrcpy). Never open its adb port to the internet. That adds a monthly cost and one more hop for the
video, which already comes through Aosu's cloud.

Commercial "cloud phones" (Redfinger, VMOS Cloud, UgPhone and others) are not a good fit. Your Google account and
a camera inside your home would sit on a phone run by a company whose data handling is unclear.

## Sources (read 2026-11-03)

* Waydroid: Google Play certification https://docs.waydro.id/faq/google-play-certification, command line
  https://docs.waydro.id/usage/waydroid-command-line-options; on a Pi 5 https://pimylifeup.com/raspberry-pi-waydroid/
* Redroid https://github.com/remote-android/redroid-doc, Google Play for it https://github.com/ayasa520/redroid-script
* Aosu on Play (package): https://appgoblin.info/apps/com.glazero.android; BlueStacks' page
  https://www.bluestacks.com/apps/communication/aosu-on-pc.html
* Camera apps breaking in emulators: https://forums.wyze.com/t/newest-app-versions-running-on-bluestacks-fail-to-load-video/338915
