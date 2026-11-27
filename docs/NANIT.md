# The Nanit baby camera on LittleBot

LittleBot can show your Nanit baby camera's live picture on its screen. Say **"Hey LittleBot, show the baby
camera"** (or "let me see the nursery", "show me the baby"), or tap **Show** on the dashboard. The picture fills
LittleBot's screen, with the baby's sounds, until you close it. Then the face comes back.

It is **unofficial**. Nanit has no public way to do this, so LittleBot uses a free community program, the
"bridge" ([indiefan/home_assistant_nanit](https://github.com/indiefan/home_assistant_nanit), the same one people use
with Home Assistant). The bridge signs in to your Nanit account and asks the camera to send its picture straight to
LittleBot over your home Wi-Fi. A Nanit update can break it at any time.

## Setting it up (once)

On LittleBot, in a terminal (on its own screen, or over `ssh`):

```bash
bash ~/LittleBot/scripts/setup_nanit.sh
```

It needs no password for LittleBot (no `sudo`) and takes a few minutes. It:

1. downloads Go (a programming tool, only used to build the bridge) into `~/.local/nanit-go`, and checks it is the
   real download from go.dev;
2. downloads the bridge (a fixed version) into `~/.local/nanit-bridge` and builds it;
3. asks you to **sign in to Nanit**: you type your Nanit email and password, then the code Nanit emails you. Your
   password is not kept anywhere; only the sign-in the bridge needs is saved;
4. starts the bridge as a background service, `littlebot-nanit`, that starts again by itself after a restart.

That's it. After a few seconds the dashboard's **Baby camera (Nanit)** card (Assistant page) lists your cameras.

To see what is set up without changing anything:

```bash
bash ~/LittleBot/scripts/setup_nanit.sh --check
```

## Using it

* Say **"Hey LittleBot, show the baby camera"**. With more than one camera, name it: "show the nursery camera".
  Without a name you get the first one.
* Or on the dashboard: **Assistant page, Baby camera (Nanit), Show &lt;camera&gt;**.
* To close it: say **"close the camera"** or **"stop"**, tap **Close the camera** on the card, or the big STOP.

It also closes by itself:

* after **30 minutes** (so it doesn't run all night by accident; `nanit.max_minutes` in the config, 0 = never);
* when LittleBot goes to sleep, or a video, game or activity starts;
* when the picture stops (the camera went offline);
* if no picture comes within about 20 seconds; LittleBot then says why (the bridge isn't running, or the camera
  isn't sending).

LittleBot's voice keeps working while the camera shows, so you can talk to it and say "stop".

**Sound** is on by default (it's a baby monitor). The **Baby camera sound** switch on the card turns it off from the
next time the camera opens. The baby's sounds play from LittleBot's speaker beside its voice.

The **Allow the baby camera** switch on the card turns the whole thing off (the voice then doesn't offer it).

## What can go wrong

* **A Nanit update breaks the bridge.** It's unofficial. If the camera stops working after an update, check the
  bridge's page for a newer version; a newer one needs this script updated.
* **The Nanit app may switch to its cloud stream** while LittleBot shows the local picture. The camera sends one
  local stream at a time. The app still works.
* **The sign-in runs out.** Nanit's sign-in lasts a long time but not forever. If the card says the bridge is
  stopped, sign in again:

  ```bash
  bash ~/LittleBot/scripts/setup_nanit.sh --login-again
  ```

* **LittleBot's address changed** (your router gave it a new one). The bridge works out the address each time it
  starts, so a restart fixes it: `systemctl --user restart littlebot-nanit`.
* **The bridge isn't running.** To see why:

  ```bash
  systemctl --user status littlebot-nanit
  journalctl --user -u littlebot-nanit -n 50
  ```

* **"No camera found yet"**: the bridge lists the cameras a few seconds after it starts. Wait a minute.

## Privacy

* The bridge keeps a **sign-in to your Nanit account** on LittleBot, in `~/LittleBot/data/nanit/session.json`. Only
  your user on LittleBot can read it (mode 600, in a folder only you can open). Anyone who has that file can use
  your Nanit account, so don't copy it anywhere. LittleBot itself never reads it, never shows it on the dashboard and
  never sends it anywhere; it only checks that it exists. The dashboard only ever sees the camera names.
* Your password is not stored. You type it once into the setup script, which sends it only to Nanit.
* **The video stays on your home network**: the camera sends it to LittleBot over your Wi-Fi, and LittleBot shows it.
  Nothing is recorded.
* The bridge listens on **port 1935** (RTMP) on your home network. **Never open or forward that port on your router
  to the internet.** The bridge refuses to start if LittleBot's address isn't a home-network one, and it only uses
  IPv4.
* While the bridge runs, it keeps asking the camera to send its picture to LittleBot, even when the screen doesn't
  show it. That stays on your home network.

## Removing it

```bash
bash ~/LittleBot/scripts/setup_nanit.sh --remove
```

This stops the bridge and deletes the service, the bridge, Go and the Nanit sign-in. To also remove the sign-in from
your Nanit account's side, use the Nanit app.

## For the curious (how it works)

* `scripts/setup_nanit.sh` pins the bridge at commit `d01a3cb958f152816529326eff96a3b26b220a27` and Go `go1.27.3`.
* `scripts/nanit_helper.py login` signs in the same way as the bridge's own `init-nanit.sh` (email and password,
  then the emailed code) and writes the bridge's session file.
* The service runs `scripts/nanit_bridge.sh`, which works out the robot's address, starts the bridge with
  `NANIT_RTMP_ADDR=<address>:1935`, `NANIT_SESSION_FILE` and `NANIT_DATA_DIR` in `data/nanit`, and writes
  `data/nanit/cameras.json` (each camera's name and baby id) for LittleBot. Memory is capped at 200 MB.
* LittleBot plays `rtmp://127.0.0.1:1935/local/<baby id>` with `ffplay`, full screen over the face
  (`littlebot/nanit/service.py`). It takes the screen like a phone being mirrored: a game or a video wins.
* Config: the `nanit:` section in `config/default.yaml` (`enabled`, `sound`, `max_minutes`,
  `start_timeout_seconds`, `ffplay_command`).
