# Android on the robot (Waydroid)

**Why.** Aosu has no web viewer, no API and no video stream (no RTSP or ONVIF on the home network: checked
2026-11-03, docs/DOORBELL.md). Its phone app is the only way to see the doorbell's video. Waydroid runs a real
Android (LineageOS with Google Play) in a container on the Pi, in a window on the robot's screen, so that app can
run on the robot itself.

**Status: not tried on the robot yet.** `scripts/setup_waydroid.sh` does it step by step, and every step can be
undone.

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
| 2 | `scripts/setup_waydroid.sh install` | installs Waydroid from trixie-backports, downloads Android with Google Play (about 1 GB, from Waydroid's own server) |
| 3 | `scripts/setup_waydroid.sh start` | starts Android (a minute or two the first time) and shows it full screen; sign in to Google Play there |
| 4 | `scripts/setup_waydroid.sh play-id` | prints Android's ID: register it at https://www.google.com/android/uncertified, wait a few minutes, then `stop` and `start` |
| 5 | `scripts/setup_waydroid.sh aosu` | opens the Aosu app (install "aosu" from Play first; its package is `com.glazero.android`). Try a camera's live view |
| off | `scripts/setup_waydroid.sh stop` | Android off (its memory back for LittleBot) |
| undo | `scripts/setup_waydroid.sh undo` then `sudo reboot` | the usual 16K kernel back (the marked block and `psi=1` removed; one blank line stays in config.txt). `undo --remove` also uninstalls Waydroid and deletes Android with everything signed in to it |

**Memory.** Android uses about 1 to 1.5 GB while it runs, out of the robot's 4 GB. Stop it when you're done, and
don't run it during heavy jobs (wake-word training).

**Privacy.** Your Google and Aosu sign-ins live in the robot's Android only (`/var/lib/waydroid`,
`~/.local/share/waydroid`). Anyone with the robot's login can reach them. `undo --remove` deletes them.

**If it doesn't work.** `sudo waydroid log` says why Android didn't start. If Android works but Aosu's live view
doesn't play, the app probably refuses emulators. Then nothing on the robot will show it, and the cloud below won't
either.

**If it works.** LittleBot could start Android and open the doorbell's live view on a ring or when asked ("show
the front door"). That part isn't built yet.

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
