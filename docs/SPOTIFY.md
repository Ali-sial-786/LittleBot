# Spotify on LittleBot

LittleBot can play your Spotify. Say "Hey LittleBot, play some jazz" and it plays from Spotify, on its own
speaker. Once Spotify is set up, it is where music comes from by default. YouTube is still there when you ask
for it ("play it on YouTube"), and LittleBot uses it when Spotify can't play.

The robot also shows up as a speaker called **LittleBot** in the Spotify app on your phone or computer, so you
can send music to it yourself, like to any Spotify Connect speaker.

> **Status.** Written from Spotify's and librespot's documentation and tested against a fake Spotify,
> **not yet on the robot**. After setting it up, run `scripts/setup_spotify.sh --check` and try it.

## What you need

* **Spotify Premium.** Spotify only lets apps (and Connect speakers) play music on a Premium account. A free
  account doesn't work.
* **A Spotify developer app.** It's free and takes two minutes. It gives LittleBot a **Client ID**. There's no
  secret to copy: LittleBot signs in with PKCE, which needs no client secret.
* The robot on Wi-Fi, and **Cloud communication** switched on in the dashboard's Privacy page. Spotify never
  works in offline mode.

## Setting it up

### 1. Make the robot a Spotify speaker

From your PC (PowerShell) or a terminal on the robot:

```
ssh -t alisial3@littlebot.local "cd ~/LittleBot && bash scripts/setup_spotify.sh"
```

It asks for your password once, to install a package. It explains each step as it goes:

1. It installs **librespot**, the open-source Spotify Connect receiver, from raspotify (raspotify's official
   install script adds its apt repository).
2. It switches off raspotify's own system service, which would be a second receiver outside your sound
   session.
3. It runs librespot as **your** user service, `littlebot-spotify`. Its sound goes through PipeWire into the
   echo-cancelled speaker, so LittleBot's microphone hears the music taken out and the music never sets off
   the wake word. Settings: name LittleBot, 160 kbit/s, volume 60% at the start, no music stored, autoplay off.
4. It starts the service and checks everything.

To see what it would do without changing anything, add `--dry-run`. To undo it all, run `--uninstall`.

### 2. Pick LittleBot once in the Spotify app

On a phone on the **same Wi-Fi**, open Spotify, play anything, tap the speaker icon and pick **LittleBot**. You
should hear it on the robot. This signs the receiver in to your account, and it remembers that. From then on
it signs in by itself at every start, so LittleBot can find it. You only do this once.

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

Every line should say PASS. A WARN says what to do.

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
* **The receiver** keeps only its own sign-in, in `~/.cache/littlebot-spotify`. No music is stored on the
  robot. `setup_spotify.sh --uninstall` deletes it.
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
| the receiver | `scripts/setup_spotify.sh` |

Settings (`skills:` in config/local.yaml): `music_provider` (auto), `spotify` (true: on), `spotify_client_id`
(the dashboard sets it too), `spotify_device_name` (LittleBot: the receiver's name; `setup_spotify.sh --name`
must match), `spotify_duck_percent` (20), `spotify_explicit` (false), `spotify_timeout_seconds` (6).

For the assistant's instant intents: `ctx.skills.get("spotify")` has `playing()` (Spotify plays on LittleBot;
no network), `paused()` (it is paused on LittleBot) and `await control("pause" | "resume" | "next" | "previous" | "stop" | "volume_up" | ...)`, which
returns a short sentence to say ("Paused.") and never raises.
