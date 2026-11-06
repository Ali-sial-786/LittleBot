# Photos and videos

LittleBot can take photos and record videos with its camera when you ask. It keeps them in a gallery you can see
on its screen and on the dashboard, and it can send one to your phone.

## Saying it

Start each one with **"Hey LittleBot"**.

| You say | What happens |
|---|---|
| "Take a picture" / "take a photo of us" / "take a selfie" | "Okay, smile!". The chat ends, the screen shows what the camera sees with **3, 2, 1**, then a click and a flash. The new photo stays on the screen for 8 seconds. |
| "Record this" / "start recording" / "make a video of the baby" | "Recording!". The chat ends, so LittleBot doesn't listen or talk while people are being filmed. A red **REC 0:42** sign shows at the top of the screen. |
| "Stop recording" (or just "stop", "cut", "that's it" while it records) | The video is saved. LittleBot says how long it is. |
| "Show me my photos" / "show the videos" / "open the gallery" | The gallery, newest first. Swipe, tap the left or right edge, or say "next" / "previous". Tap a video (or say "play") to play it full screen with its sound. |
| "Send it to my phone" / "send that photo to my phone" | The photo or video on the screen (else the newest) goes to your phone as a notification. |
| "Show me a code for it" | A QR code on the screen. Point any phone's camera at it to open the photo. |

Anything longer ("take a picture and send it to my phone", "show me yesterday's video") goes to LittleBot's brain,
which uses the same tools.

A recording also stops by itself:

* after **10 minutes** (`capture.max_video_minutes`);
* when someone presses **STOP ALL**, or a YouTube video starts (LittleBot says what it saved);
* when the camera is switched off in Privacy;
* when LittleBot shuts down.

What was recorded up to then is always saved. A recording cut off by a power cut is lost, and its unfinished file
is removed on the next start.

## The dashboard's Photos page

On a phone it is under **More**; on a computer, in the side menu.

* every photo and video, newest first, grouped by day. Tap one to see it full screen (swipe or use the arrow keys
  for the next one);
* **Take a photo** (at once, no countdown) and **Start recording** / **Stop recording**;
* for each one: **Save** (to the phone or computer you're using), **Send to phone**, **Share link** (a QR code
  and a link to copy, and it can show the QR code on LittleBot's screen too), **Show on LittleBot** and **Delete**;
* the switches **Allow photos and videos** and **Sound in videos**;
* **Stop every share link**: every link already sent stops working.

## Sending to a phone

A robot can't push a file straight onto a phone. AirDrop only works between Apple devices (it needs Apple's own
Wi-Fi radio), and AirPlay goes the other way: a phone can show its screen on LittleBot, but an iPhone can't receive
a video by AirPlay. So LittleBot does what works on every phone:

1. It makes a **link to that one photo or video on LittleBot itself**, such as
   `http://192.168.2.112:8765/share/Xy3...`. The long random part is the password. The link needs no login and
   works for **24 hours** (`capture.share_hours`). Only that one item can be seen with it.
2. It gets the link to your phone in one of two ways:
   * **a phone notification** (ntfy). Tap it and the photo opens. Press and hold it (iPhone) or tap **Save**
     to keep it in your photos. Only the link goes through ntfy's server. The preview picture in the
     notification is fetched by your phone straight from LittleBot.
   * **a QR code on LittleBot's screen**. Any phone's camera opens it. No set-up needed.

The phone has to be **on the home Wi-Fi** to open the link, because the file never leaves LittleBot. Nothing is
uploaded anywhere.

### What it needs

* **LAN access on**. The dashboard is only reachable from LittleBot itself by default. Add this to
  `config/local.yaml` and restart:

  ```yaml
  dashboard:
    lan_access: true
  ```

  Without it LittleBot says phones can't open its links yet.
* **For notifications**, a phone set up once for LittleBot's notifications: dashboard, Assistant page,
  **Calendars and phones** (or `scripts/setup_phone.py`). Each grown-up can have their own. When voice or face
  recognition knows who asked, it goes to their phone, else to the family's. **Cloud communication** must be on
  (Privacy page). Without a phone set up, LittleBot shows the QR code instead.

## Privacy

* Photos and videos are taken **only when someone asks**. LittleBot never records by itself.
* Everything stays in `data/gallery` on the robot, readable only by LittleBot's user. Delete them on the Photos
  page. Deleting one also stops its links.
* The **camera** privacy switch stops a recording at once. The **microphone** switch keeps the sound out of new
  videos. The dashboard's **Sound in videos** switch does the same.
* While recording, the red REC sign is always on the screen, over cards too.
* A share link works only for that one item and only for 24 hours. **Stop every share link** on the Photos page ends them
  all sooner.

## Settings

In `config/local.yaml` under `capture:` (defaults in `config/default.yaml`):

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` switches the feature off whatever the dashboard says |
| `countdown_seconds` | `3` | the countdown before a voice photo (0 = at once) |
| `show_seconds` | `8` | how long a new photo stays on the screen |
| `photo_width` | `1920` | photo size in pixels (the camera's own 1080p picture at most) |
| `video_width` / `video_fps` | `1280` / `30` | 720p at 30 frames a second. 1080p makes the Pi 5 work much harder |
| `video_crf` / `video_preset` | `23` / `veryfast` | H.264 quality (lower is better and bigger) and encoding speed |
| `video_sound` | `true` | the room's sound in videos (the dashboard switch starts from this) |
| `audio_source` | `default` | the PipeWire / Pulse microphone ffmpeg records from |
| `max_video_minutes` | `10` | a recording stops by itself after this long |
| `min_free_mb` | `1000` | no new photo or video when less storage than this is free |
| `share_hours` | `24` | how long a link sent to a phone works |
| `push_attach` | `true` | the notification shows a preview (your phone fetches it from LittleBot) |

Recording needs **ffmpeg** (`sudo apt install ffmpeg`; it also brings **ffplay**, which plays the videos). Without
it photos still work.

## What can go wrong

| LittleBot says / the log says | Why, and what to do |
|---|---|
| "The camera is switched off in Privacy." | switch the camera on (Privacy page) |
| "LittleBot's storage is nearly full" | delete photos or videos on the Photos page |
| "ffmpeg isn't installed" | `sudo apt install ffmpeg` on the robot |
| "phones can't open LittleBot's links yet" | turn on LAN access (above) |
| The phone says it can't reach the page | the phone isn't on the home Wi-Fi, or the link is older than 24 hours |
| `capture: the recording stopped by itself` | the camera stopped sending pictures (unplugged?). What was recorded is saved |
| A video has no sound | the microphone was off in Privacy, **Sound in videos** was off, or ffmpeg couldn't open the microphone (the log says `no sound`) |

Code: `littlebot/capture/` (service, gallery files, the recorder, the QR code), `littlebot/skills/camera.py` (the
voice tools), `littlebot/screens/cards/gallery.py` (the gallery and QR cards),
`littlebot/dashboard/routes/capture_routes.py` (the dashboard and `/share/` pages).
