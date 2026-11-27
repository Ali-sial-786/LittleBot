# Spotify on LittleBot

LittleBot can play your Spotify. Say "Hey LittleBot, play some jazz" and it plays from Spotify, on its own
speaker. Once Spotify is set up, it is where music comes from by default. YouTube is still there when you ask
for it ("play it on YouTube"), and LittleBot uses it when Spotify can't play.

The robot also shows up as a speaker called **LittleBot** in the Spotify app on your phone or computer, so you
can send music to it yourself, like to any Spotify Connect speaker.

> **Status.** The receiver changed to **Spotify Soloist** on 2026-11-02 (see below). Written from Spotify's
> Soloist documentation and tested against fakes, **not yet run on the robot**. After setting it up, run
> `scripts/setup_spotify.sh --check` and try it.

## Why Soloist, not librespot

The robot's speaker used to be **librespot** (from raspotify), an open-source Spotify Connect receiver. It
signed in fine, but Spotify then refused to give it the key for each song ("error audio key 0 1",
"Service unavailable { audio key error }"), so every song was skipped. Many people have the same problem
(librespot issue #1649, raspotify issue #774), and it isn't something LittleBot can fix.

**Spotify Soloist** is Spotify's own receiver for Linux and the Raspberry Pi, released in August 2026
([Spotify's page](https://developer.spotify.com/documentation/soloist)). Spotify makes it, so it gets the songs.
Two things are different:

* It needs a **Soloist API key** from your Spotify account (free, but the account must have Premium).
* Each build **stops working 90 days after Spotify built it**. The setup adds a weekly check that fetches the
  newest build by itself, so you shouldn't notice.

## What you need

* **Spotify Premium.** Spotify only lets apps (and Connect speakers) play music on a Premium account. A free
  account doesn't work.
* **A Soloist API key** (for the robot's speaker): on
  [developer.spotify.com/dashboard/soloist](https://developer.spotify.com/dashboard/soloist), logged in with
  your Spotify account, accept the terms if it asks, and make a key. The setup asks for it.
* **A Spotify developer app** (for LittleBot to choose the music). It's free and takes two minutes. It gives
  LittleBot a **Client ID**. There's no secret to copy: LittleBot signs in with PKCE, which needs no client
  secret.
* The robot on Wi-Fi, and **Cloud communication** switched on in the dashboard's Privacy page. Spotify never
  works in offline mode.

## Setting it up

### 1. Make the robot a Spotify speaker

From your PC (PowerShell) or a terminal on the robot. Use `ssh -t`: the script asks you things.

```
ssh -t alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh"
```

It needs no sudo for itself. It explains each step as it goes:

1. It downloads **Soloist** from Spotify and puts it in `~/.local/bin/soloist`. Spotify only offers "the newest
   build" (no version numbers and no published checksum), so the script checks the download against the
   checksum Spotify's server sends, checks that it runs, and notes its SHA-256 in
   `~/.config/littlebot-spotify/soloist-sha256`.
2. It retires the old receiver. librespot's old sign-in is deleted. If raspotify (librespot) is still installed,
   it **asks** whether to remove it (`sudo apt-get remove raspotify`): say y and type your password, or n to
   keep it. If you keep it, it asks whether to switch off raspotify's own system service. Nothing uses sudo
   without your y.
3. It asks for your **Soloist API key**. Paste it and press Enter; nothing shows as you type. It is kept only
   on the robot, in `~/.config/littlebot-spotify/api-key.env`, readable only by you.
4. It runs Soloist as **your** user service, `littlebot-spotify` (the same service librespot used, so there are
   never two "LittleBot" speakers). Its sound goes through PipeWire into the echo-cancelled speaker, so
   LittleBot's microphone hears the music taken out and the music never sets off the wake word. Settings:
   name LittleBot, volume 60% at the start. It also adds the weekly update check
   (`littlebot-spotify-update.timer`).
5. It starts it and checks everything.

To see what it would do without changing anything, add `--dry-run`. To undo it all, run `--uninstall`.

### 2. Pick LittleBot once in the Spotify app

On a phone on the **same Wi-Fi**, open Spotify, play anything, tap the speaker icon and pick **LittleBot**. You
should hear it on the robot. This signs the receiver in to your account, and it remembers that. From then on
it signs in by itself at every start, so LittleBot can find it. You only do this once.

You did this before, for librespot? Do it **once more**: Soloist is a new receiver and has its own sign-in.

**LittleBot isn't in the list?** Run the pairing on its own:

```
ssh -t alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh --pair"
```

It waits up to 3 minutes for you to pick LittleBot. Phones find speakers through a port that the robot's
avahi-daemon (the thing that makes `littlebot.local` work) may already hold. If the pairing doesn't work, the
script asks whether to pause avahi-daemon during a second try (sudo, y/N), and starts it again afterwards.
Once signed in, the speaker shows up through Spotify's servers, so this is only needed for the pairing.

### 3. Make a Spotify developer app

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in with the
   Spotify account LittleBot will use.
2. **Create app**. Any name and description ("LittleBot").
3. **Redirect URI**: add exactly `http://127.0.0.1:8888/callback` and press Add. Spotify only accepts https
   addresses or a loopback IP like this one (not "localhost").
4. Under "Which API/SDKs are you planning to use?", tick **Web API**. Accept the terms and **Save**.
5. Open the app's **Settings** and copy the **Client ID** (32 letters and numbers).

Someone else in the family with their own account? Add their email under the app's **User Management**
first. A new app is in "development mode", which only works for the people listed there.

Spotify's rules for development-mode apps since February 2026
([their post](https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security)):
the account that makes the app needs **Premium**, each account can have **one** such app, and at most **five**
people can use it. Everything LittleBot uses is still allowed (search, playback on a device, your liked songs,
playlists and top tracks); search answers at most 10 results, and LittleBot asks for 8.

### 4. Connect on the dashboard

On the dashboard, open **Assistant** and find the **Spotify** section.

1. Paste the Client ID and press **Connect Spotify**. Spotify's sign-in page opens in a new tab.
2. Check that it is your account and press **Agree**.
3. The new tab then tries to open `http://127.0.0.1:8888/callback?code=...`, and **the page doesn't load. That
   is expected**: 127.0.0.1 is your phone or PC itself, and nothing is listening there.
4. Copy the **whole address** from that tab's address bar, paste it into the box on the dashboard and press
   **Finish connecting**.

The section then says **Connected to Spotify** and whether **LittleBot's Spotify speaker is ready**. A pasted
address works once, and only for the sign-in you just started. If it is refused, press Connect Spotify again.

### 5. Check

```
ssh alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh --check"
```

Every line should say PASS. A WARN says what to do. It also says how many days the Soloist build has left.

The dashboard was already connected for librespot? Nothing to do there: LittleBot finds the new receiver by its
name, LittleBot.

## Updates and expiry

Spotify makes every Soloist build stop working **90 days** after it was built. When that happens the receiver
stops (exit code 10) and LittleBot can't play on Spotify (it says so, and plays from YouTube in Auto).

* **By itself:** every Monday morning, `littlebot-spotify-update.timer` runs `setup_spotify.sh --update`. It asks
  Spotify's server whether there is a newer build (a few bytes), downloads it only when there is, and restarts
  the receiver. If music is playing on LittleBot just then, it waits and restarts next time instead. No sudo.
* **By hand:** `ssh alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh --update"`
* **How long is left:** `--check` says "the build works for N more day(s)". Under 14 days it warns.
* The old build is kept as `~/.local/bin/soloist.old`, in case a new one misbehaves.
* To insist on one exact download, set `LITTLEBOT_SOLOIST_SHA256` to its SHA-256: anything else is refused.

See when it last ran: `ssh alisial3@littlebot.local "journalctl --user-unit littlebot-spotify-update -n 30 --no-pager"`

## What to say

| Say | What happens |
|---|---|
| "Play some jazz", "Play something relaxing" | a playlist for that style or mood |
| "Play Bohemian Rhapsody", "Play Hello by Adele" | the song, then the rest of its album |
| "Put on Taylor Swift", "Play the album Rumours" | the artist's music, or the album |
| "Play my liked songs" | your Liked Songs, shuffled |
| "Play my road trip playlist", "Play my Sunday chill playlist" | one of your own playlists, by name |
| "Play something I like" | your most played songs, shuffled |
| "Pause", "Carry on", "Next song", "Go back a song" | controls the music on LittleBot |
| "Turn the music up", "Music volume 40", "Shuffle on" | the music's own volume and shuffle |
| "What's playing?", "What song is this?" | the song and the artist, with the cover on the screen |
| "Play jazz on YouTube" | YouTube, whatever the setting says |
| "Play it on Spotify" | Spotify, even when it would have used YouTube |

The music starts **once LittleBot has finished its answer** ("Here's some jazz!"), so it never plays over what
LittleBot says. While the music plays, the screen shows the **now-playing card**: the album cover, the song,
the artist and how far in it is. Tap the card to pause or carry on. LittleBot's eyes stay in their corner.

**While you talk to LittleBot**, the music turns down. When it hears "Hey LittleBot", Spotify goes down to 20%
(`skills.spotify_duck_percent`). It comes back up when the conversation ends. If you paused it meanwhile, it
comes back up when the music carries on. If you changed the volume yourself meanwhile, your choice stays.

Starting a video, a game or an activity pauses Spotify, and so does the dashboard's big **Stop** button.

## Spotify or YouTube

The dashboard's Spotify section has **Music plays from**:

* **Auto** (the default): Spotify when it is connected and LittleBot's speaker is ready, otherwise YouTube.
  Auto never takes Spotify away from another device: if your account is playing on your phone or in the car
  right now, "play some jazz" plays from YouTube instead. Say "on Spotify" to take it over anyway. (Spotify
  plays on one device at a time per account.)
* **Always Spotify**: Spotify whenever it is connected. When it can't play, LittleBot says why instead of
  using YouTube.
* **Always YouTube**: YouTube, unless you ask for Spotify or for your own music ("my liked songs").

The config default is `skills.music_provider: auto` (auto | spotify | youtube). The dashboard's choice wins
over it once you have picked one there.

Your **own** music (liked songs, your playlists, "something I like") always needs Spotify.

## Troubleshooting

| What you see or hear | What to do |
|---|---|
| "LittleBot's Spotify receiver isn't running" / the dashboard says the speaker isn't visible | Run `setup_spotify.sh --check`. If the receiver isn't signed in yet, pick LittleBot once in the Spotify app on the same Wi-Fi (step 2). Restart it: `ssh alisial3@littlebot.local "systemctl --user restart littlebot-spotify"` |
| "the receiver is running but isn't signed in" | pick LittleBot once in the Spotify app on the same Wi-Fi, or run `setup_spotify.sh --pair` (step 2) |
| LittleBot isn't in the Spotify app's speaker list | the phone must be on the same Wi-Fi. Then `setup_spotify.sh --pair`, and say y when it offers to pause avahi-daemon |
| `--check` says the build expired, or the log says it exited with code 10 | `setup_spotify.sh --update` (the weekly timer normally does this: `systemctl --user enable --now littlebot-spotify-update.timer`) |
| `--check` says "no Soloist API key", or the log says the key was refused | make a new key on [developer.spotify.com/dashboard/soloist](https://developer.spotify.com/dashboard/soloist) and run `ssh -t alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh --new-key"` |
| The first song after setting it up is very loud | a known Soloist problem (it can start at 100% with PipeWire, Soloist issue #23). Turn it down once in the Spotify app or say "music volume 40"; PipeWire remembers it |
| Songs are skipped, the log says "audio key error" | that is librespot, the old receiver: run `setup_spotify.sh` again so the service runs Soloist |
| `--check` says librespot or raspotify is still running | a second "LittleBot". Run `setup_spotify.sh` and say y to removing raspotify, or `sudo apt-get remove raspotify` yourself |
| "Spotify only lets apps play music on an account with Spotify Premium" | the account isn't Premium; LittleBot plays from YouTube meanwhile |
| "Spotify no longer lets apps open its own personal playlists" | since November 2024, apps can't open Spotify-made playlists such as Discover Weekly, Release Radar or Daily Mix. Start it in the Spotify app and pick LittleBot as the speaker, or save its songs into a playlist of your own |
| "the songs Spotify found for that are marked explicit" | LittleBot skips songs marked explicit (`skills.spotify_explicit: false`). Albums and playlists play as they are, so also turn off **Allow explicit content** in the Spotify app's settings |
| Spotify's page says "INVALID_CLIENT: Invalid redirect URI" | the app's Redirect URI must be exactly `http://127.0.0.1:8888/callback` (step 3) |
| "that address is from a different sign-in" | press Connect Spotify again and paste the new address |
| "Spotify ended LittleBot's sign-in" | you removed the app's access in your Spotify account; connect again on the dashboard |
| Music plays but the wake word goes off by itself | the music isn't going through the echo-cancelled speaker: run `scripts/setup_echo_cancel.sh --check`, then `setup_spotify.sh --check` |
| No sound from the robot, but Spotify shows LittleBot playing | look at the receiver's log: `ssh alisial3@littlebot.local "journalctl --user-unit littlebot-spotify -n 50 --no-pager"` |
| "cloud communication is off in Privacy" | switch Cloud communication on in the dashboard's Privacy page |

LittleBot's own log lines start with `spotify:` (never a token):

```
ssh alisial3@littlebot.local "journalctl --user-unit littlebot -n 300 --no-pager | grep -i spotify"
```

## Privacy

* **What goes to Spotify:** only the search words for music ("jazz", "Bohemian Rhapsody") and playback
  commands (play, pause, next, volume). Nothing else about your family: no voices, no pictures, no names,
  nothing LittleBot remembers.
* **Spotify's sign-in** stays on the robot, in `data/spotify_token.json`, readable only by you (chmod 600). It
  is never shown on the dashboard, never sent anywhere else and never written to a log. **Disconnect Spotify**
  on the dashboard deletes it. You can also remove LittleBot's access from your Spotify account page (Apps).
* **The receiver** (Soloist) keeps its sign-in in `~/.local/share/soloist` and a playback cache in
  `~/.cache/soloist` (100 MB at most). Its local control port only listens on the robot itself (127.0.0.1).
  `setup_spotify.sh --uninstall` deletes all of it.
* **The Soloist API key** is in `~/.config/littlebot-spotify/api-key.env`, readable only by you, and never
  printed. Soloist only accepts it on its command line, so other users logged in to the robot could see it in
  the process list (Soloist issue #24). On LittleBot you are the only user. If it ever leaks, make a new one on
  the Soloist key page and run `--new-key`.
* Spotify is treated like the other cloud services: it never works in offline mode, and it stops while Cloud
  communication is off in Privacy.
* Spotify's app, and your account's listening history, will show what LittleBot played, as for any speaker.

## For developers

| Piece | File |
|---|---|
| sign-in (PKCE), tokens, Web API requests | `littlebot/skills/spotify_api.py` |
| the Spotify skill: search, play, control, ducking | `littlebot/skills/spotify.py` |
| where music plays from, the tools (`play_music`, `music_control`, `whats_playing`) | `littlebot/skills/music.py` |
| the now-playing card | `littlebot/screens/cards/music.py` |
| the dashboard routes (`/api/assistant/spotify/...`) | `littlebot/dashboard/routes/assistant_routes.py` |
| the receiver (Soloist: install, key, service, weekly update, pairing) | `scripts/setup_spotify.sh` |

The receiver: Soloist runs as `~/.local/bin/littlebot-spotify` → `soloist --device-name LittleBot --api-key …
--data-dir ~/.local/share/soloist --cache-dir ~/.cache/soloist --cache-size 100 --initial-volume 60
--ws 127.0.0.1:0`. Soloist has no bitrate, device type or autoplay option: the quality and autoplay follow the
Spotify app's settings. It plays to the default PipeWire output (the echo-cancelled speaker). Its local
WebSocket API writes its port to `ws.port` in the data folder; `soloist ctl status` / `soloist ctl now` use it,
and so does `Spotify.receiver()` (running, signed in), which the dashboard shows. That is only a hint: LittleBot
plays through the Web API on the device named LittleBot, whatever its type.

Settings (`skills:` in config/local.yaml): `music_provider` (auto), `spotify` (true: on), `spotify_client_id`
(the dashboard sets it too), `spotify_device_name` (LittleBot: the receiver's name; `setup_spotify.sh --name`
must match), `spotify_duck_percent` (20), `spotify_explicit` (false), `spotify_timeout_seconds` (6).

For the assistant's instant intents: `ctx.skills.get("spotify")` has `playing()` (Spotify plays on LittleBot;
no network), `paused()` (it is paused on LittleBot) and `await control("pause" | "resume" | "next" | "previous" | "stop" | "volume_up" | ...)`, which
returns a short sentence to say ("Paused.") and never raises.
