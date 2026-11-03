# Doorbell and camera alerts

When the doorbell rings or a camera sees someone or a package, LittleBot plays a chime, says it out loud and shows it
on its screen:

* "Someone rang the doorbell at the front door."
* "Someone's at the front door."
* "A package was left at the front door."
* "The backyard camera saw someone."

Later you can ask "Who was at the door?" or "Did a package come?": LittleBot remembers the last 20 alerts (in memory
only, until it restarts).

> **Status.** Tested with fake notifications on the PC. **Not yet tried with a real Aosu doorbell or phone.**

## How it works

Aosu doorbells and cameras can't be reached directly: there is no Aosu API, no video stream (RTSP/ONVIF) and no
Home Assistant add-on. The only thing the Aosu app gets is a notification on your phone. So LittleBot has a small,
secret web address on your home Wi-Fi, and something that sees the alert calls it:

* **an Android phone** that has the Aosu app passes each Aosu notification on (with MacroDroid, or with the Home
  Assistant app);
* **Home Assistant** calls it for a doorbell it already knows (Nest, Eufy, Ring, Reolink ...).

The address looks like this (yours is on the dashboard):

    http://192.168.1.20:8765/hooks/doorbell/Xy3...43 random letters...q9

Only **events** come in: what happened and which camera. No video and no pictures. (For the video itself, the
Aosu app could run in Android on the robot: docs/ANDROID_ON_PI.md. Not tried yet.)

## 0. First: let the phone reach LittleBot

The phone (or Home Assistant) has to reach LittleBot on the home Wi-Fi, so the dashboard's LAN access must be on. Add
this to `config/local.yaml` on the robot and restart LittleBot (or start it with `--lan`):

```yaml
dashboard:
  lan_access: true
```

Then open the dashboard: **Assistant → Doorbell and cameras**. The address is there, with **Copy**, **Send a test**
and **New address**. Press **Send a test**: LittleBot should say "This is a test. Someone rang the doorbell."

## 1. Aosu with an Android phone and MacroDroid (the simplest)

You need an Android phone with the Aosu app signed in that stays at home on the Wi-Fi. An old phone left plugged in
works well. MacroDroid is free (up to 5 macros).

1. Install **MacroDroid** from the Play Store. Open it and give it notification access when it asks.
2. **Add macro** → trigger **Notification → Notification Received**. Choose **Select application(s)** →
   **aosu**, and **Any content**.
3. Action **Connectivity → HTTP Request**:
   * Method **POST**, URL: the address from the dashboard.
   * Body / content: type **application/json**, and this text:

     ```json
     {"title":"[notification_title]","text":"[notification]","app":"aosu"}
     ```

     The words in square brackets are MacroDroid's "magic text" for the notification's title and its text. The names
     can differ between MacroDroid versions: tap the magic-text button (**...**) in the body box and pick
     "notification title" and "notification text" so they are right for yours. Plain text works too: LittleBot reads
     JSON, a form or just the words.
4. Save the macro and ring the doorbell.

LittleBot reads the notification's words: "Someone is at your Front Door" becomes "Someone's at the front door", "Your
doorbell is ringing" becomes a ring, "Motion detected by Backyard" becomes movement at the backyard camera, "A package
was detected" a package.

## 2. Aosu with an Android phone and the Home Assistant app

If you already use Home Assistant, its Companion app on the Android phone with Aosu can pass the notifications on.

1. In the Companion app: **Settings → Companion app → Manage sensors → Last notification**. Turn it on and add the
   Aosu app to its **allow list** (otherwise it sees every app's notifications).
2. In Home Assistant's `configuration.yaml` (restart Home Assistant after):

   ```yaml
   rest_command:
     littlebot_door:
       url: "http://192.168.1.20:8765/hooks/doorbell/YOUR-SECRET"   # the address from the dashboard
       method: POST
       content_type: "application/json"
       payload: >-
         {"event": {{ event | default('') | to_json }}, "camera": {{ camera | default('') | to_json }},
          "title": {{ title | default('') | to_json }}, "text": {{ text | default('') | to_json }}}
   ```

3. An automation (change `sensor.my_phone_last_notification` to your phone's sensor; Settings → Devices → your phone):

   ```yaml
   automation:
     - alias: "Aosu to LittleBot"
       trigger:
         - platform: state
           entity_id: sensor.my_phone_last_notification
       condition:
         - "{{ 'aosu' in (trigger.to_state.attributes.package | default('')) }}"
       action:
         - service: rest_command.littlebot_door
           data:
             title: "{{ trigger.to_state.attributes['android.title'] | default('') }}"
             text: "{{ trigger.to_state.state }}"
   ```

   Check the sensor's attributes once (Developer tools → States) to see Aosu's package name and the title attribute on
   your phone.

## 3. Home Assistant with Nest, Eufy, Ring or another doorbell

For a doorbell or camera Home Assistant already knows, add the `rest_command` above and one automation for each thing
to say. Pick your own entities in the triggers (the names below are examples):

```yaml
automation:
  - alias: "Doorbell to LittleBot"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_ding     # your doorbell's press
        to: "on"
    action:
      - service: rest_command.littlebot_door
        data:
          event: doorbell
          camera: Front Door
  - alias: "Package to LittleBot"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door_package  # your camera's package (or person) sensor
        to: "on"
    action:
      - service: rest_command.littlebot_door
        data:
          event: package        # or person, motion, vehicle, animal
          camera: Front Door
```

Newer Ring and Nest set-ups give an event entity (for example `event.front_door_ding`) instead: trigger on any change
of its state. Anything else that can send a web request can use the address too:

```sh
curl -X POST -H "Content-Type: application/json" -d '{"event":"doorbell","camera":"Front Door"}' \
     http://192.168.1.20:8765/hooks/doorbell/YOUR-SECRET
```

It answers `{"ok": true, "said": true}` (or `"said": false` when it kept quiet, see below).

## iPhone?

An iPhone doesn't let any app read another app's notifications, so it can't pass Aosu's alerts on. For Aosu you need
an Android phone at home (an old one on the Wi-Fi works), or Home Assistant with a doorbell it supports.

## What is said, and when

| Setting | Default | Change it |
|---|---|---|
| What is said out loud | doorbell presses, a person, a package | "Stop telling me about people", "Tell me about cars too", or the switches on the dashboard |
| Movement, cars, animals | not said | "Tell me about motion too" |
| A doorbell press at night | said | "Don't tell me about the doorbell at night" |
| Door alerts | on | "Stop the door alerts" |

* **Quiet hours** are the heads-ups' ones (9:30 pm to 7 am to start with: "don't speak up after 9"). Then, and while
  LittleBot sleeps, only a doorbell press is said.
* While a video plays or you are talking to LittleBot, a ring, a person or a package is still said; movement, cars and
  animals are not.
* The same thing from the same camera is said at most once every 30 seconds for a ring and every 3 minutes for the
  rest, and never more than 30 alerts a minute (a camera that keeps firing doesn't flood the room).

## Privacy and the secret address

* Nothing leaves LittleBot: the alert comes from your phone or Home Assistant over your home Wi-Fi and stays on the
  robot. What the notification said is never logged (only "person at Front Door") and is kept only in memory.
* The address works like a password: anyone on your Wi-Fi who knows it can make LittleBot say "someone's at the
  door". Don't share it, and never forward LittleBot's port on your router. It is kept in `data/doorbell_hook_token`
  (only readable by LittleBot's user).
* **New address** on the dashboard makes a new secret at once: the old address stops working, so put the new one in
  MacroDroid or Home Assistant.
* A wrong address just gets "Not Found", like any page that doesn't exist.
* What a notification says is only read for a few words (ring, someone, package ...) and a camera name. It is never
  taken as a request to LittleBot.

## Limits

* No video and no pictures: only what happened and which camera.
* It works only while the phone or Home Assistant can reach LittleBot on the home Wi-Fi, and only as fast as the Aosu
  notification reaches the phone (usually a second or two).
* LittleBot's guess of the camera's name comes from the notification's words. If it guesses wrong, send `camera`
  yourself (Home Assistant), or name your cameras simply in the Aosu app ("Front Door", "Backyard").
* Switch the whole thing off in `config/local.yaml` with `skills: {doorbell: false}`.
