# Media module (`littlebot/media/`)

Parent-controlled YouTube playback through the **official YouTube IFrame Player API**.
Only videos and playlists a parent added to the allowlist can play; the robot never searches,
recommends, downloads or extracts anything. `MediaController` implements
`core.interfaces.MediaAPI` + `core.service.Service`. Pi feasibility, YouTube terms and
verification status: [YOUTUBE_ON_PI.md](../YOUTUBE_ON_PI.md).

## Files

| File | Contents |
|------|----------|
| `__init__.py` | `create_service(ctx)` -> `MediaController`; `create_player_router(media)` (lazy re-export) |
| `allowlist.py` | `parse_youtube_url()` (validation + canonical URL), `sanitize_title()`, error classes (all `ValueError`) |
| `repository.py` | SQLite tables `media_items` (allowlist) and `media_usage` (seconds per day) via `ctx.db` |
| `controller.py` | `MediaController`: gating, session, limits watchdog, leases/state, events, player-event handling |
| `bridge.py` | `PlayerBridge`: desired state for the page (`seq`, `load_id`, action, item, volume), events, heartbeat, token |
| `player_routes.py` | `create_player_router(media)`: `/player` page + JSON bridge, token-protected, strict CSP |
| `display.py` | `BrowserKioskBackend` (Chromium on the robot), `DesktopBrowserBackend`, `NoneBackend`, `select_backend()` |
| `oembed.py` | optional title lookup / embeddability hint via `youtube.com/oembed` (off by default) |
| `static/player.html/.js/.css` | the player page (vanilla JS, no inline script/style) |

## How it fits together

```
dashboard (parent, authenticated)            robot screen (Chromium kiosk) / any browser
  add_item / play / pause / stop / skip            GET /player?token=T  (player.html)
            │                                                │ polls 2 Hz        │ events
            ▼                                                ▼                   ▼
      MediaController ──── load / command / volume ───► PlayerBridge ◄── ready/playing/ended/error/
        │  lease (screen+audio_out+face, MEDIA)            (token,          heartbeat/hidden ...
        │  RobotState.PLAYING_MEDIA                          seq, load_id)          │
        │  media.* + face.visible events                                           ▼
        └─ DisplayBackend.open(url) / close()            YouTube IFrame Player API (youtube-nocookie.com)
```

The page is dumb: it applies the desired state and reports what the YouTube player does.
All decisions (gating, limits, what plays next) are made in Python.

## API (`MediaAPI` + extras)

| Method | Behaviour |
|------|------|
| `list_items()` | `[{id, kind: video\|playlist, youtube_id, list_id, title, title_source, url, start_seconds, added_at, enabled, sort_order, play_count, last_played_at, last_error, last_error_at, thumbnail_url}]` in list order |
| `add_item(url, title=None, *, kind=None)` | validates + stores. `kind="playlist"` keeps the playlist of a `watch?v=..&list=..` link (default: the single video). Raises `MediaURLError` subclasses, `DuplicateItemError`, `AllowlistFullError` (all `ValueError`) with parent-friendly messages |
| `remove_item(id)` | `bool`; stops playback if that item is playing |
| `update_item(id, *, title=None, enabled=None)` | extra; `KeyError` if unknown; disabling a playing item stops it |
| `reorder_items(ids)`, `get_item(id)` | extras |
| `await play(id, *, parent_override=False)` | `KeyError` unknown; `PermissionError` with a friendly message for: item disabled, `media.enabled` off, offline mode, age gate, limits, lease refused, state refused, player window could not open. Playing another item during a session switches item (same session). Returns `status()` |
| `await pause()` / `await resume()` | `bool`; paused time does not count; resume re-checks limits |
| `await stop(reason="stopped")` | idempotent; also the `Service.stop` lifecycle hook (after `system.shutdown` it also tears down) |
| `await skip()` | next **enabled approved** item in list order, wrapping (a single item restarts); never anything else |
| `set_volume(v)` | clamps to `[0, media.max_volume]`, persists `media.volume`, pushes to the page. `ValueError` for non-numbers/NaN |
| `status()` | see below (cheap, thread-safe) |
| `player_url()` / `player_path()` / `player_token` | link with token: full URL for the robot's own browser, relative path for "open player on this device" |
| `close()` | lifecycle teardown (tests / shutdown) |

`status()`:

```
{state: idle|loading|playing|paused, item, session_id, session_seconds, session_limit_seconds,
 daily_used_seconds, daily_limit_seconds, daily_remaining_seconds, paused_seconds, volume,
 enabled, offline, age_gate: {video_allowed, min_age_months, age_months, parent_allowed_under_min_age},
 player: {backend, available, detail, running, url, path, connected, last_event, info},
 last_error: {item_id, title, code, message, at} | None, items}
```

`player.url` contains the player token - it is meant for the parent dashboard (authenticated)
and the robot itself; never put it anywhere public.

## Events

Published: `media.started {item_id, title, kind, session_id, skipped}`, `media.paused {item_id}`,
`media.resumed {item_id}`, `media.stopped {item_id, reason, title, session_id, session_seconds,
detail?}` with `reason` one of `ended | stopped | limit | error`, `media.error {item_id, error, code}`,
`face.visible {visible: false}` when a session starts and `{visible: true}` when it ends.
State: `IDLE -> PLAYING_MEDIA -> IDLE` via `ctx.state.release(PLAYING_MEDIA)` (never clobbers a
newer state).

Subscribed: `system.stop_all` (stop), `settings.changed` (`media.volume` -> page volume;
`media.enabled` false -> stop; `media.allow_under_min_age` / `profile` changes re-check the age
gate for sessions that were not started with an explicit parent override), `system.shutdown`.

## Safety rules (enforced in code)

1. **Allowlist only.** Links are parsed and validated; the stored URL is rebuilt from the
   validated 11-character video id / playlist id. Rejected: non-YouTube hosts, any scheme but
   http(s) (`javascript:`, `data:` ...), user-info / ports / backslashes / control characters,
   inputs over 2048 chars, channel / search / feed / hashtag / clip pages, YouTube "Mix" lists
   (`RD...`, picked by YouTube) and private lists (`WL`, `LL`). Duplicates (any URL form of the
   same video/playlist) are rejected. Titles are plain text, control/bidi characters removed,
   max 100 characters (the dashboard must still HTML-escape them).
2. **Age gate.** `ctx.age_policy().video_allowed(parent_override or media.allow_under_min_age)`:
   off under `media.min_age_months_for_video` (18) and for an unknown age, unless a parent
   explicitly allows it.
3. **Time limits.** `media.session_limit_minutes` (validated 0..120) and
   `media.daily_limit_minutes` (0..240, persisted per local calendar day in `media_usage`,
   flushed every 10 s and on stop). 0 means "no video". Time counts while loading/playing,
   not while paused. A watchdog (1 s) stops gently with reason `limit`: the page fades the
   volume over 1.5 s and shows "All done!". Play is refused with < 15 s of daily time left.
4. **Never into unapproved content.** On `ENDED` the page destroys the player at once and
   shows "All done!" - YouTube's end screen / related videos never render. Inside an approved
   playlist the page lets YouTube continue to the next track (`track_ended`) and ends after
   the last one. The page checks the playing video id (`getVideoUrl`, `getPlaylist`) and
   tears down + reports `unexpected_video` if it isn't the approved one.
5. **Page hidden or left** (a YouTube link opened a new tab/window, tab switched, page
   closed) -> the page reports `hidden` / `navigated_away`; the controller stops with
   `error` and the kiosk backend kills Chromium's whole process group (popups included).
6. **Leases and state.** `screen + audio_out + face` at `Priority.MEDIA`. A higher priority
   (parent / system) preempts: the page is told to stop immediately and the session ends.
   `system.stop_all` ends it too. Activities (lower priority) can't interrupt a video.
7. **Offline mode** refuses playback (no network) and selects the `none` backend;
   `media.enabled = false` refuses playback and stops a running session.
8. **YouTube terms**: official embed only, nothing drawn over or blocking a live player, no
   ad blocking, no downloading/stream extraction (details in YOUTUBE_ON_PI.md section 8).
9. **Health**: player page never connects (`connect_timeout_seconds`, 60), goes silent for
   `heartbeat_timeout_seconds` (15), kiosk process dies, or stays paused longer than
   `max_pause_minutes` (10) -> session ends and the face returns.

Player errors -> `media.error` + `media.stopped(error)` + the item's `last_error`:
2 invalid id, 5 HTML5 player error, 100 not found / private, 101 / 150 "Unsupported video"
(usually embedding disabled; YouTube also uses 150 when it refuses playback for other
reasons), 153 missing referrer, `api_load_failed` (no internet), `unexpected_video`.

## Player page protocol

All requests need the token: `?token=` or header `X-Player-Token` (constant-time compare,
missing -> 401, wrong -> 403). The token is random per process (`secrets.token_urlsafe(32)`).

* `GET /player/api/state` ->
  `{seq, action: idle|play|pause|stop, reason, load_id, item: {id, kind, video_id, list_id,
  start_seconds, title} | null, volume, youtube_host, poll_ms, heartbeat_ms}`. Each poll marks
  the page connected. The page applies a command once per `seq`; a new `load_id` means
  "tear down and load this item".
* `POST /player/api/event` `{type, load_id, code?, player_state?, current_time?, duration?,
  video_id?, detail?}` with `type` in `ready playing paused buffering cued ended track_ended
  error heartbeat autoplay_blocked hidden navigated_away`. Events whose `load_id` is not the
  current one are recorded but ignored (e.g. an `ended` arriving after a skip).
* `GET /player` sends `Content-Security-Policy: default-src 'none'; script-src 'self'
  https://www.youtube.com https://s.ytimg.com; frame-src https://www.youtube-nocookie.com
  https://www.youtube.com; connect-src 'self'; ...; frame-ancestors 'none'`,
  `Referrer-Policy: strict-origin-when-cross-origin` (YouTube embeds need a Referer - error
  153 without - and this sends only our origin, never the token), `X-Frame-Options: DENY`,
  `Cache-Control: no-store`. The dashboard mounts the router without parent auth and does not
  overwrite these headers (`EXTERNAL_PREFIXES`).

Player variables: `autoplay=1` (only the parent's choice), `controls=0`, `disablekb=1`,
`fs=0`, `iv_load_policy=3`, `rel=0`, `playsinline=1`, `cc_load_policy=0`, `enablejsapi=1`,
`origin`, `start` (from `t=`), `listType=playlist` + `list` for playlists.

## Display backends (`media.player`)

| Value | Backend |
|------|------|
| `auto` | offline or headless -> `none`; Raspberry Pi -> `kiosk` if Chromium is found, else `none` (warning); desktop -> `browser` |
| `kiosk` | `chromium(-browser) --kiosk --app=<url> --window-size=W,H --force-device-scale-factor=1 --autoplay-policy=no-user-gesture-required --noerrdialogs --disable-session-crashed-bubble --incognito --disable-extensions --user-data-dir=data/media/chromium-profile ...` (+ `media.kiosk_extra_args`), started with an asyncio subprocess in its own session; on stop: SIGTERM to the process group, SIGKILL after 3 s. Reused while a session continues; closed `kiosk_close_delay_seconds` (1.5 s) after `ended` and 1.5 s + the 1.5 s fade after `limit` (the child sees "All done!"), at once after a parent stop or error |
| `browser` | `webbrowser.open(url)` in a worker thread; only when no page is polling. Can't be closed: the page shows "All done!" / a calm blank screen and keeps polling |
| `none` | nothing launched; open `status()["player"]["url"]` (or `path` on the dashboard host) yourself |

`media.browser_command` overrides autodetection. `media.player_base_url` overrides
`http://127.0.0.1:<dashboard.port>` (the dashboard must be running: it serves the page).

## Parent workflow

1. Set the child's age in the profile. Under 18 months (or no age), video stays off unless
   the parent turns on "allow videos under 18 months" (or uses a one-off "play anyway").
2. In YouTube, open a video (or one of your playlists), *Share -> Copy link*, paste it in the
   dashboard's video list with a title you'll recognise. For a `watch?v=..&list=..` link
   choose whether you mean the single video (default) or the whole playlist. Playlists can
   change later on YouTube - for the youngest, prefer single videos.
3. Choose session and daily limits (defaults 15 / 30 minutes) and the volume.
4. Press Play next to an item. The face hides, the video plays on the robot's screen; pause,
   resume, skip (next approved item), stop, or the big STOP button work at any time.
5. When the video ends (or a limit is reached) the robot shows "All done!" and the face comes
   back. If a video can't be played (e.g. the owner disabled embedding) the list shows why.
6. No robot screen for video? Use "open player on this device" (YOUTUBE_ON_PI.md section 7).

## Testing

`.venv/bin/python -m pytest -q tests/media` - URL table, repository, controller (manual clock,
fake backend), routes (httpx ASGI), display backends (fake launcher + a real dummy process),
composition root, and `test_media_player_page.py`: the real page in headless Chromium driven
by the real controller/uvicorn with a **stubbed `window.YT`** (skipped without Playwright /
Chromium; set `LITTLEBOT_TEST_CHROMIUM=/path/to/chrome` if Playwright's own browser is missing).
Real YouTube playback is not part of the suite (YOUTUBE_ON_PI.md section 9).

## Known limitations

* Real playback and everything Pi-specific are unverified (YOUTUBE_ON_PI.md).
* The robot can't verify what a playlist contains in advance; playlists can change on YouTube.
* Desktop-browser tabs can't be closed by the robot.
* `face.visible` is published both by this module (session start/end) and by the face module
  (when its state enters/leaves `hidden`); consumers must treat it as idempotent.
