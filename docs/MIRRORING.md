# Phone mirroring

LittleBot can show a grown-up's phone screen on its own screen: photos from the weekend, a video call face, a
recipe. Two kinds of phone:

* **iPhone or iPad**: by AirPlay, the same way an iPhone mirrors to an Apple TV. LittleBot runs **UxPlay**, an
  AirPlay receiver called "LittleBot".
* **Android phone**: by **scrcpy** over the Wi-Fi, using Android's Wireless debugging (adb). No app on the phone.

The phone's screen fills LittleBot's screen until the phone stops, someone says "stop mirroring", or something more
important needs the screen. Then the face comes back.

## Starting and stopping

* Say **"Hey LittleBot, mirror my iPhone"** (or "show my Android phone on your screen", "cast my phone").
* Or on the dashboard: **Assistant page → Phone mirroring → Mirror an iPhone / Mirror an Android**.
* To stop: stop mirroring on the phone, say **"stop mirroring"**, press **Stop** on the card, or the big STOP.

It also stops by itself when LittleBot goes to sleep, a video, game or activity starts, or the parent's switch
goes off.

## Setting up (once)

### 1. Install the programs

In a terminal on LittleBot, in the LittleBot folder:

```bash
bash scripts/setup_mirroring.sh
```

It checks what is missing and prints the one command that installs it. It only runs that command (with `sudo`, so
it asks for your password) after you answer **y**. On Debian 13 (trixie) the command is:

```bash
sudo apt install -y uxplay adb && echo 'deb http://deb.debian.org/debian trixie-backports main contrib' | sudo tee /etc/apt/sources.list.d/trixie-backports.list >/dev/null && sudo apt update && sudo apt install -y -t trixie-backports scrcpy
```

(`scrcpy` is not in trixie itself, only in trixie-backports. The GStreamer plugins UxPlay needs,
`gstreamer1.0-libav`, `gstreamer1.0-plugins-bad` and `gstreamer1.0-plugins-good`, are added too if they are
missing, and `avahi-daemon` must be running so iPhones can find LittleBot.) You only need the iPhone part (uxplay)
or the Android part (adb, scrcpy) if you only have one kind of phone. Until a program is installed, the dashboard
card says "not installed" with the command to copy, and the voice says it isn't set up yet.

### 2. iPhone

Nothing to pair. Each time:

1. Say "mirror my iPhone" (or tap **Mirror an iPhone**).
2. On the iPhone, open **Control Center** (swipe down from the top-right corner) and tap **Screen Mirroring**.
3. Pick **LittleBot**. The iPhone must be on the same Wi-Fi.

### 3. Android phone (Android 11 or newer)

Pair each phone once:

1. On the phone: **Settings → About phone → tap "Build number" 7 times**. This switches on Developer options.
2. **Settings → Developer options → Wireless debugging**: on (the phone on the same Wi-Fi as LittleBot).
3. Tap **Pair device with pairing code**. The phone shows an IP address and port (like `192.168.1.23:37099`) and a
   six-digit code.
4. On the dashboard: **Assistant → Phone mirroring → Pair a phone**, type both, and tap **Pair**.

After that, whenever you want to mirror: switch Wireless debugging on, then say "mirror my Android phone" or tap
**Mirror an Android**. LittleBot finds the phone on the Wi-Fi by itself (the port changes every time, so it looks
it up instead of remembering it).

## What it can't do

* No sound by default: the phone's sound stays on the phone. `mirror.sound: true` in `config/local.yaml` plays it
  from LittleBot's speaker, beside LittleBot's own voice (the wake word and "stop" then hear the phone too).
* Android: taps on LittleBot's screen don't work the phone (so a child can't use a grown-up's phone through the
  robot). `mirror.touch_control: true` turns that on.
* Only one phone at a time. Asking for the other kind stops the first.
* An iPhone may still send a bigger picture than the screen; it is scaled to fit. The Pi 5 decodes it in software
  (it has no H.264 decoder chip), which uses one or two CPU cores while a phone shows.
* Wireless debugging switches itself off after a while on some phones; switch it on again before mirroring.
* Phones older than Android 11 have no Wireless debugging (they would need a USB cable once, which LittleBot
  doesn't set up).

## Privacy

* The AirPlay receiver runs **only while mirroring was asked for**. If no iPhone starts mirroring within
  `mirror.wait_seconds` (90 seconds), it stops again, so LittleBot isn't left open on the Wi-Fi. While it waits,
  any iPhone on your Wi-Fi could pick "LittleBot".
* Nothing from the phone is recorded or kept. LittleBot's log says only that mirroring started and stopped, and why.
* Android pairing: LittleBot keeps a list of the paired phones (name, address, adb's id) to show on the dashboard.
  The pairing keys are adb's own (in `~/.android` on the robot). The six-digit code is handed to adb directly and
  never saved or logged. **Forget** on the card takes a phone off the list and disconnects it; to remove LittleBot
  from the phone too: Wireless debugging → Paired devices → LittleBot's computer → Forget.
* The parent's switch (**Allow phone mirroring** on the card) turns it all off; `mirror.enabled: false` in the
  config switches the feature off whatever the switch says.

## Settings (`mirror:` in config/local.yaml)

| Setting | Default | What it does |
|---|---|---|
| `enabled` | true | the feature at all |
| `wait_seconds` | 90 | how long the AirPlay receiver waits for an iPhone |
| `max_size` | 0 | the picture's longer side in pixels (0: the screen's) |
| `fps` | 30 | the most frames per second asked of the phone |
| `android_bit_rate_mbps` | 4 | scrcpy's video bit rate |
| `sound` | false | the phone's sound from LittleBot's speaker |
| `touch_control` | false | Android: taps on LittleBot's screen work the phone |
| `airplay_name` | LittleBot | the name in the iPhone's Screen Mirroring list |
| `video_sink` | waylandsink | UxPlay's video output (`autovideosink`, `kmssink` on a desktop-less Pi) |
| `uxplay_command`, `scrcpy_command`, `adb_command` | uxplay, scrcpy, adb | where the programs are |

## How it works (for the curious)

`littlebot/mirror/service.py` (`ctx.mirror`):

* **iPhone**: `uxplay -n LittleBot -nh -fs -avdec -vs waylandsink -s 480x320 -fps 30 -as 0`, run through
  `stdbuf -oL` so its log lines arrive at once. "raop_rtp_mirror starting mirroring" means a phone started; then
  "raop_rtp_mirror->running is no longer true", "Open connections: 0" or "lost connection with client" means it
  stopped. LittleBot takes the screen only once a phone really mirrors, so cards still show while it waits.
* **Android**: `adb devices`, else `adb mdns services` and `adb connect` to each paired phone it sees. Debian's
  adb can't look for phones itself (it was built without mDNS: "adb mdns check" prints nothing), so after pairing
  the phone was never found again. LittleBot looks for them on the Wi-Fi with the zeroconf package instead (the
  `mirror` extra: `.venv/bin/python -m pip install -e '.[mirror]'`), paired phones first. Then
  `scrcpy --serial=<phone> --fullscreen --window-borderless --max-size=480 --video-bit-rate=4000000 --max-fps=30
  --no-audio --no-control`. scrcpy exits when the phone goes away or stops sharing.
* The screen lease is the browser view's: taken like a video, held at priority 45, so a game or video wins and
  mirroring stops. LittleBot's speaker and microphone are never taken: the voice chat keeps working.
* Programs run in their own process group, are stopped with SIGTERM (then SIGKILL after 2 s), and are stopped when
  LittleBot exits.
