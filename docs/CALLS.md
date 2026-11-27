# Video calls on the robot (WhatsApp)

**What it is.** LittleBot can make and answer WhatsApp video (and voice) calls on its own screen, with its own
camera, microphone and speaker. It uses WhatsApp Web, the same thing as WhatsApp in a browser on a computer, in a
Chromium window on the robot. It is **your own WhatsApp number**: the robot is linked to your phone once, with a
QR code, as one of your linked devices.

**Status: not tried on the robot yet.** Everything here is built and tested against a stand-in page, not against
the real WhatsApp. LittleBot works WhatsApp's page for you (it types in the search box and presses the call
button). WhatsApp can change that page whenever it likes, and then a step may stop working until
LittleBot's list of what to look for is updated (see "When a step stops working").

**The robot does not ring by itself.** WhatsApp runs only while it is on the screen, so that its memory is free the
rest of the time. When someone calls you, your phone rings. Say "Hey LittleBot, answer on WhatsApp": WhatsApp
opens on the robot, the ringing call shows there, and LittleBot presses Accept if it finds the button (if not, tap
it).

## First time: link it to your phone

1. On the dashboard (Assistant page, **WhatsApp calls**), add who can be called (below), then press
   **Open WhatsApp**. Or say "Hey LittleBot, open WhatsApp".
2. The robot's screen shows a big QR code, and LittleBot says what to do.
3. On your phone: WhatsApp → Settings → **Linked devices** → **Link a device**, and scan the code.
4. After a few seconds the screen says "WhatsApp is ready". The link is kept, so you do this once.

On your phone the robot shows in the list of linked devices as a Chrome browser on Linux.

## Who can be called

Only people on your list. On the dashboard (Assistant page, WhatsApp calls, **Who can be called**) each person has
two names:

| | |
|---|---|
| What you say | "Dad" (so "call Dad", "my dad", "daddy" all find him) |
| Name in WhatsApp | exactly how that person's chat is named in your WhatsApp, for example "Ali Khan". Left empty: the same as what you say |

Asked to call anyone else, LittleBot says who it can call. The list can also start from `calls.contacts` in
`config/local.yaml`.

## What to say

| Say | What happens |
|---|---|
| "Hey LittleBot, call Dad" / "video call Mum" / "WhatsApp Dad" / "call Mum on WhatsApp" | WhatsApp opens, LittleBot finds that chat with WhatsApp's search box and presses the video-call button |
| "voice call Dad" / "audio call Mum" | the same, a voice call |
| "open WhatsApp" | WhatsApp opens (to link it, or to wait for a call) |
| "answer on WhatsApp" (while your phone rings); "answer" once WhatsApp is open | WhatsApp opens and the ringing call is accepted when its button is found |
| "Hey LittleBot, hang up" / "end the call" / "close WhatsApp" | the call is ended with WhatsApp's own button if found, then WhatsApp is shut down |

Only a grown-up can do this. If voice recognition says the child was the one talking, LittleBot refuses, the same
way as for the browser.

During a call LittleBot ends its own voice chat when the call connects, so its voice doesn't talk into your call.
The wake word still works: say "Hey LittleBot, hang up". A plain "stop" ends LittleBot's chat, not your call.

You can also use the dashboard (Open / Close, a **Video call** button per person) and the settings panel on the
robot's screen (hold a finger on the screen for 5 seconds; the last row is **WhatsApp**: Open, or Close while it is
open).

## Your chats stay hidden

WhatsApp Web shows all your chats. On a robot in the living room that is not what you want, so LittleBot puts its
own cover over WhatsApp's whole page: the chat list, an open chat, messages, previews, search results and pop-ups
are never on the robot's screen. The cover is there from before the page first draws. It lets only two things
through:

* the **QR code** while WhatsApp isn't linked (LittleBot redraws it big on the cover);
* a **call**: the call's own box (ringing, in a call, Accept / Decline). If LittleBot can't tell which part of the
  page is the call's box, it shows only the live video pictures and puts its own big **Answer**, **Decline** and
  **Hang up** buttons on the cover, which press WhatsApp's buttons underneath.

The cover says what is going on ("Opening WhatsApp…", "Calling Dad…", "Waiting for the call…") and has a big
**Close** button. LittleBot finds the chat and presses the call button underneath the cover.

If a step fails, the chats are **not** uncovered. The cover says what went wrong ("I couldn't find the call
button") and LittleBot says the same out loud.

To see WhatsApp itself on the robot, a grown-up turns off **Hide my chats on the robot** on the dashboard (it asks
you to confirm). Nothing else can turn it off: not the voice, not the robot's own settings panel. With it off, a
failed step leaves WhatsApp on the screen and LittleBot says what to tap.

While the chats are hidden, WhatsApp's notifications are switched off in that Chromium too, so no message preview
pops up anywhere on the robot.

## When it shuts down

Chromium with WhatsApp takes several hundred MB of the robot's memory, so it is shut down completely as soon as it
isn't needed. It opens only with at least 700 MB free (`calls.min_free_mb`).

| What happens | Then |
|---|---|
| "hang up", "end the call", "close WhatsApp", the cover's Close, the dashboard's Close | the call is ended, WhatsApp is shut down |
| the call is over (the other side hung up, or you did) | shut down about 3 seconds later |
| WhatsApp is open with no call and nobody touches it for 2 minutes (`calls.idle_seconds`, 120) | shut down |
| its window is closed, or another window is in front of it for 10 s (`calls.off_screen_seconds`) | shut down |
| a video, a game, an activity, Android, phone mirroring, the baby camera or the browser takes the screen | shut down |
| STOP ALL, LittleBot going to sleep, an error, the screen going dark | shut down |
| "Allow WhatsApp calls" switched off; cloud communication or the microphone switched off in Privacy; the camera switched off in Privacy during a video call | shut down |
| free memory falls under 200 MB (`calls.min_free_mb_running`) | shut down: LittleBot comes first |
| LittleBot stops or restarts | shut down |

"Shut down" means every Chromium process of WhatsApp is killed (the whole process group, and then LittleBot looks
for anything of it still running and kills that too). The log says how much memory came back, in a line like
`calls: WhatsApp is off (the call ended) after 312 s; 1410 MB of memory free before, 1930 MB after (+520 MB)`.
The dashboard card shows the same for the last run.

## The camera

A USB camera can serve only one program at a time. While WhatsApp is open, LittleBot's own camera use steps aside
and Chromium gets the camera. During that time:

* LittleBot doesn't follow faces with its eyes, and "take a picture" says the camera is being used for a call;
* **always recording pauses.** What it had buffered stays saveable for the usual 3 minutes, and it starts again
  when WhatsApp is closed;
* the dashboard's Camera page says the camera is in a video call.

Your camera Privacy switch is not changed by any of this. If the camera is switched off in Privacy, WhatsApp never
gets it: video calls are refused and LittleBot offers a voice call instead.

## Privacy

* **Your chats.** WhatsApp Web has all your chats in it. With **Hide my chats on the robot** on (the default) they
  are behind LittleBot's cover and don't show on the robot's screen. With it off, anyone at the robot sees your chat
  list and messages while WhatsApp is open, and can tap into them.
* **What LittleBot does with them: nothing.** It never reads chats or messages out, never sends a message, never
  hands anything from WhatsApp to an AI model, and never writes any of it to the log. Its page scripts answer only
  "is this button there, and where". The logs name a person by the name on your list ("Dad"), never by the name in
  WhatsApp.
* **The sign-in is on the robot.** The link to your WhatsApp is kept in `data/whatsapp-profile` on the robot's
  storage. Anyone with that folder could open your WhatsApp. To remove it: dashboard → WhatsApp calls → **Sign out of
  WhatsApp on the robot** (the folder is deleted), and also remove the robot on your phone (WhatsApp → Settings →
  Linked devices).
* **Who gets the camera and microphone.** Only the page `https://web.whatsapp.com`, and only in that Chromium. That
  window can't go to any other site: a link in a chat to somewhere else is refused.
* **The call itself** goes through WhatsApp like any WhatsApp call. It needs cloud communication and the
  microphone on in Privacy.
* **Nothing is recorded** by LittleBot during a call, and always recording is paused.

## Settings

| Where | What |
|---|---|
| Dashboard, Assistant page, WhatsApp calls | Allow WhatsApp calls (`calls.enabled`); Hide my chats on the robot (`calls.hide_chats`, on); Who can be called (`calls.contacts`); Open / Close; Sign out of WhatsApp on the robot |
| `config/local.yaml`, `calls:` | `enabled`, `profile_dir` (`whatsapp-profile`, under `data/`), `min_free_mb` (700), `min_free_mb_running` (200), `idle_seconds` (120), `off_screen_seconds` (10), `window_match` (`whatsapp`), `load_timeout_seconds` (60), `zoom` (1.0), `contacts` |

## When a step stops working

LittleBot stops at the step that failed, leaves WhatsApp on the screen (covered) and says which step it was:
"I couldn't find WhatsApp's search box", "I couldn't find Dad in WhatsApp", "I couldn't open Dad's chat",
"I couldn't find the call button", "I pressed the call button, but the call didn't start".

* "I couldn't find Dad in WhatsApp": check the **Name in WhatsApp** on the dashboard. It must be the chat's name
  exactly as WhatsApp shows it.
* Anything else: WhatsApp has probably changed its page. With WhatsApp open on the robot, run

  ```
  cd ~/LittleBot && .venv/bin/python scripts/check_whatsapp.py
  ```

  It prints, for each thing LittleBot looks for (the search box, the call buttons, the call's box ...), which of
  its ways of finding it match right now, whether the cover is in place, and which call-box rule matched. It prints
  only LittleBot's own names and yes / no, never anything from your chats. Run it in the state that failed (a chat
  open, a call ringing, a call on). Everything LittleBot looks for is in one file,
  `littlebot/calls/page_scripts.py` (`SELECTORS`), so that is the only place to fix.

## What is not known until it is tried on the robot

* Whether WhatsApp Web offers calls in the Pi's Chromium at all (it does in Chrome on a computer).
* Whether the things LittleBot looks for match the real page. They are best guesses with several fall-backs each.
* Whether a call opens in WhatsApp's page or in a window of its own. A window of WhatsApp's own is allowed and
  watched for the call's buttons, but it is not covered (it has no chats in it) and may not be full screen.
* How well the Pi 5 copes with a video call (it has no video encoder chip), and how much memory it really takes.
* Whether Chromium picks the right microphone and speaker, and whether the far side hears an echo.
* The wake word during a call: the other person's voice comes out of the robot's speaker, so it may be harder to
  hear "Hey LittleBot", or it may wake by mistake.
* The cover's limits: something WhatsApp draws in the browser's very top layer (a full-screen video, a system
  dialog) could show above it.
