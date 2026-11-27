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
| "Send it to my phone" / "send that photo to my phone" | The photo or video on the screen (else the newest) goes to **your** phone as a notification (see [Whose phone](#whose-phone)). |
| "Send it to my wife's phone" / "send it to Ali's phone" / "send the photo to my husband" | To that person's phone. |
| "Send it to everyone" / "put it on our phones" | To every phone set up for LittleBot. |
| "Show me a code for it" | A QR code on the screen. Point any phone's camera at it to open the photo. |

Anything longer ("take a picture and send it to my phone", "show me yesterday's video") goes to LittleBot's brain,
which uses the same tools.

The gallery and the QR code go away **30 seconds** after the chat is over (or after the last touch), and the face
comes back. It then dozes and sleeps as usual when nobody talks. A slideshow first goes once through every photo
(at most 5 minutes). A new photo stays 8 seconds.

A recording also stops by itself:

* after **10 minutes** (`capture.max_video_minutes`);
* when someone presses **STOP ALL**, or a YouTube video starts (LittleBot says what it saved);
* when the camera is switched off in Privacy;
* when LittleBot shuts down.

What was recorded up to then is always saved. A recording cut off by a power cut is lost, and its unfinished file
is removed on the next start.

## Always recording (the last 3 minutes)

LittleBot can keep the **last 3 minutes** of video, with the room's sound, so that something that just happened
can still be saved. It forgets them as it goes: nothing is kept unless someone asks.

| You say | What happens |
|---|---|
| "Save that" / "clip that" / "save that clip" / "save the last 3 minutes" | Everything it holds becomes one video in the gallery. "Saved the last 3 minutes." |
| "Save the last minute" / "save the last 30 seconds" | Only that much (rounded up to a whole 10-second piece). |
| "Turn on always recording" / "turn off always recording" | Changes the setting below to **On** / **Off**. |

The same **Save the last 3 minutes** button is on the dashboard's Photos page and in LittleBot's own settings
panel (hold a finger on the screen for 5 seconds; the last two rows). From a button, LittleBot says "Saved the last
3 minutes." out loud, so the room knows. The saved clip is a normal video: it shows in the gallery ("show me the
video") and can be sent to a phone. It isn't put on the screen by itself (a new video isn't either).

**When it records** (`capture.always_recording`, on the Photos page and in the settings panel):

* **Automatic** (the default): while LittleBot is plugged in, a full battery on the charger included. A LittleBot
  with no battery monitor counts as plugged in. Unplugged, it stops; what it held can still be saved for 3 more
  minutes, then it is wiped.
* **On**: on the battery too. When the battery gets low (the low level on the Home page, 20 %), it stops,
  LittleBot says "My battery is low, so I've stopped always recording.", and the setting goes back to Automatic.
  Plugging LittleBot back in also puts it back to Automatic.
* **Off**: nothing is recorded, and what was held is wiped at once.

**What it keeps, and where**

* 1280x720 at 30 pictures a second, H.264 with AAC sound, in pieces of about 10 seconds (MPEG-TS, which join
  without re-encoding), in `/dev/shm/littlebot-buffer`. That folder is **RAM** (tmpfs): never the SD card, gone at
  a power cut or a reboot. LittleBot refuses a `buffer_dir` that isn't a RAM disk.
* A piece older than 3 minutes is deleted as new ones arrive. The folder is emptied when LittleBot starts, stops
  and shuts down, and it never grows past `buffer_max_mb` (256 MB): the oldest pieces go first.
* A **small red dot** at the top centre of the screen (over cards too) shows whenever it is recording. It is
  still and has no words; the big REC sign takes its place during a recording you asked for.

**What stops it**

* The **camera** privacy switch stops it and **wipes** what it held, at once. So do **Off** and the Photos page's
  **Photos and videos by voice** switch.
* The **microphone** switch (and **Sound in videos**) takes the sound out of the pieces recorded from then on. A
  saved clip whose pieces don't all have sound is saved without sound.
* While you record a video ("record this"), the buffer **pauses**: there is only ever one encoder. It resumes
  when the recording ends. What it held before stays saveable for 3 minutes.
* While WhatsApp is open for a video call (the camera is lent to it, [CALLS.md](CALLS.md)) it pauses too: what it
  had stays saveable for the usual 3 minutes, and it starts again when WhatsApp is closed.
* While a YouTube video plays (the camera is paused) and whenever the camera sends no pictures, it pauses, and
  resumes by itself.
* **Asleep**, LittleBot keeps recording as long as the camera keeps sending pictures (it does: the camera stays
  at 30 pictures a second for the buffer). If fewer than 2 a second arrive for 3 seconds, it pauses.
* STOP ALL doesn't stop it. Offline mode makes no difference: nothing is uploaded.

**What it costs** (estimates, **to be measured on the robot**)

* CPU: about **one of the Pi 5's four cores** for the encoder (libx264 `veryfast`, 720p30, two threads), all the
  time it records. The camera also stays at 30 pictures a second instead of dropping to the face detector's 6 (or
  1.5 asleep), which costs some more. On the battery this shortens the running time: that is why Automatic
  only records while plugged in.
* RAM: about **60 to 100 MB** for 3 minutes of pieces, plus about 100 MB for ffmpeg itself.
* Saving stops the encoder for a second or two (so the last seconds are whole) and starts it again.

If it gets in the way: `capture: {buffer_enabled: false}` in `config/local.yaml` switches it off whatever the
dashboard says.

## The dashboard's Photos page

On a phone it is under **More**; on a computer, in the side menu.

* every photo and video, newest first, grouped by day. Tap one to see it full screen (swipe or use the arrow keys
  for the next one);
* **Take a photo** (at once, no countdown) and **Start recording** / **Stop recording**;
* for each one: **Save** (to the phone or computer you're using), **Send to phone**, **Share link** (a QR code
  and a link to copy, and it can show the QR code on LittleBot's screen too), **Show on LittleBot** and **Delete**;
* the switches **Allow photos and videos** and **Sound in videos**;
* **Always recording**: whether it is recording now (or why not), how much video is in memory and how many MB,
  the Automatic / On / Off choice, and **Save the last 3 minutes**;
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
  **Calendars and phones** (or `scripts/setup_phone.py`). Each grown-up can have their own. **Cloud
  communication** must be on (Privacy page). Without a phone set up, LittleBot shows the QR code instead.

### Whose phone

LittleBot never sends a photo to every phone unless you say so ("everyone", "the family", "our phones").

* **"My phone"** (or nothing said): the phone of whoever is talking. LittleBot listens to the **voice** first
  (voice recognition, Privacy page and Who's who). When the voice isn't sure, it **looks with the camera** at once
  (face recognition): the one grown-up in view, or the one the voice sounds most like.
* **A name or a role**: "Ali's phone", "dad's phone", "grandma's phone", from the names and roles on the Profile
  page's **Who's who**.
* **"My wife's / husband's / spouse's / partner's phone"**: the other parent. "Wife" is the one whose role is
  mum (mom, mommy, ...) and "husband" the one whose role is dad, whoever is talking. With other roles ("parent"),
  LittleBot has to know who is talking, and it's the other one.
* **When it can't tell**, it asks: "Whose phone should I send it to: Ali's or Sara's?" (with one phone: "Should I
  send it to Ali's phone?"). Answer "Ali's", "mine", "my wife's", "yes" or "no". The answer works for 2 minutes.
* **Someone whose phone isn't set up** gets the QR code instead, and LittleBot says so.

For "take a picture and send it to my phone", LittleBot works out whose phone **before** the photo, because the
chat ends when the photo starts.

## Privacy

* Photos and videos are **kept** only when someone asks. The one thing LittleBot records by itself is the
  [always-recording buffer](#always-recording-the-last-3-minutes): the last 3 minutes, in RAM only, with a red
  dot on the screen, forgotten as it goes unless someone says "save that". Switching the camera off wipes it;
  **Off** on the Photos page stops it for good.
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
| `buffer_enabled` | `true` | always recording: `false` switches it off whatever the dashboard says |
| `buffer_minutes` | `3` | how much it keeps (0.5 to 10) |
| `buffer_dir` | `/dev/shm/littlebot-buffer` | where the pieces live. Must be a RAM disk (tmpfs) |
| `buffer_segment_seconds` | `10` | the length of a piece |
| `buffer_width` / `buffer_fps` | `1280` / `30` | 720p at 30 pictures a second |
| `buffer_max_mb` | `256` | the most RAM the pieces may take |

The parent's choice is the setting `capture.always_recording` (`auto`, `on` or `off`), not a config key.

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
| "There's nothing to save." | always recording isn't running; LittleBot says why (off, on the battery, the camera off) |
| The Photos page says "The buffer's folder isn't a RAM disk" | `buffer_dir` must be on tmpfs (`/dev/shm/...`) |
| `the always-recording encoder stopped` in the log | ffmpeg ended by itself; LittleBot tries again after 30 seconds |
| A video has no sound | the microphone was off in Privacy, **Sound in videos** was off, or ffmpeg couldn't open the microphone (the log says `no sound`) |

Code: `littlebot/capture/` (service, gallery files, the recorder, the always-recording buffer, the QR code), `littlebot/skills/camera.py` (the
voice tools), `littlebot/screens/cards/gallery.py` (the gallery and QR cards),
`littlebot/dashboard/routes/capture_routes.py` (the dashboard and `/share/` pages).
