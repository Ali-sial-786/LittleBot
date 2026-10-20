# Dashboard module (`littlebot/dashboard/`)

The parent dashboard: a FastAPI JSON API plus a static, framework-free, mobile-first
single-page app. It runs **inside the robot's asyncio loop** (uvicorn as a task, no extra
threads or loops), listens on `127.0.0.1` by default and only on `0.0.0.0` when LAN access
is explicitly enabled. Parent-facing instructions: [DASHBOARD_GUIDE.md](../DASHBOARD_GUIDE.md).

## Files

| File | Contents |
|------|----------|
| `__init__.py` | `create_service(ctx)` -> `DashboardService` (uvicorn task on the current loop, pre-bound socket, port-in-use handling), re-exports `create_app`, `resolve_bind_host` |
| `app.py` | `create_app(ctx, robot=None)`: security middleware (auth, CSRF, Origin), security headers, error mapping, static files, media player router mount |
| `auth.py` | `AuthManager`: setup code, scrypt password hash, sessions (hashed, SQLite + memory cache), CSRF tokens, `RateLimiter`; `log_setup_code()` |
| `deps.py` | `DashState` (ctx, lazy robot facade, auth, background tasks, parent alerts, aggregated `status()`, STOP fallback), bind-host / URL helpers |
| `models.py` | pydantic request bodies (`extra="forbid"`, length limits, unicode/control-char cleaning) |
| `scrub.py` | second safety net that hides secret-looking keys/values in anything sent to the browser |
| `routes/*.py` | one router per section (auth, robot/system, face, activities, media, profile, ai, privacy, devices, motion, simulator) |
| `__main__.py` | `python -m littlebot.dashboard reset-password` (forgotten password recovery, run on the robot) |
| `static/` | `index.html`, `app.css`, `favicon.svg`, `js/app.js` (boot, auth screens, router, polling), `js/api.js`, `js/ui.js`, `js/pages/*.js` |

## Wiring

* `app/robot.py` builds the dashboard last (`create_service(ctx)`) and starts it with the
  other services. The robot facade is looked up lazily from `ctx.extras["robot"]` (absent in
  tests: every action has a context-only fallback, e.g. STOP publishes `system.stop_all`,
  cancels motion, stops audio / activity / media, preempts leases and forces `IDLE`).
* Subsystems are used **only** through `core/interfaces.py` protocols. Any of
  `ctx.face/audio/vision/motion/media/brain/activities` may be `None`: status shows
  `{"status": "unavailable"}`, actions answer `503` with a friendly message.
* Optional duck-typed extras: `activities.clear_history()`, `brain.clear_history()`,
  `media.update_item(id, title=...)` (rename; otherwise remove + re-add).
* If `littlebot.media.player_routes.create_player_router(media)` is importable it is mounted
  as-is at `/player*` **without parent auth** (the player checks its own kiosk token) and
  keeps its own CSP / frame headers (the dashboard only adds `nosniff` + `Referrer-Policy`
  when missing).
* Bus: the dashboard subscribes to `brain.status` (status `attention` -> parent alert) and
  `system.error` (-> warning alert), and to `system.stop_all` to cancel its own background
  tasks (face demo, gestures). It publishes only `system.stop_all` (STOP fallback) and, from
  the simulator when no vision module exists, `vision.faces` / `vision.person_present` /
  `vision.person_left`.

## Service lifecycle

```
start():  resolve_bind_host(cfg.dashboard)      127.0.0.1 | 0.0.0.0 (lan_access only)
          bind socket ourselves                  EADDRINUSE -> log error, FAILED, return (robot keeps running)
          uvicorn.Server(Config(app, lifespan="off", log_level="warning")).serve(sockets=[sock])
              as a task on the running loop     SystemExit/any error inside is caught (never kills the loop)
          wait (bounded) until accepting, RUNNING
          no password yet -> issue + log a one-time setup code (WARNING, contains "setup code:")
stop():   cancel background tasks, unsubscribe, should_exit=True, await <=4 s, then force_exit + cancel
```

`DashboardService(ctx, port=0)` binds an ephemeral port (tests); `health()` adds
`host`, `port`, `lan_access`. Lifecycle state is `service_status` (core `Service`).

## Security design

| Threat | Mitigation |
|--------|-----------|
| Default / weak credentials | No default password. First run prints a one-time **setup code** (8 chars, ~40 bits, alphabet without 0/O/1/I/L) to the robot's console/log only; its SHA-256 is kept in memory. After 10 wrong codes it is burned and a new one is logged. Passwords >= 8 chars. |
| Password theft from disk | `hashlib.scrypt` (N=2^14, r=8, p=1, 16-byte random salt per hash), stored in SQLite kv `dashboard:password`; constant-time compare; hashing in `asyncio.to_thread`. |
| Session theft | 32-byte `secrets.token_urlsafe` tokens; only SHA-256 stored (`dashboard_sessions` table) and cached in memory; cookie `littlebot_session` `HttpOnly; SameSite=Strict; Path=/`, `Secure` over https; absolute expiry `dashboard.session_hours`; max 32 sessions; logout, logout-everywhere, password change logs out other devices. |
| CSRF | Every non-GET `/api/*` request needs `X-CSRF-Token` equal to the session's own token (from `GET /api/session`), compared in constant time. Origin (or Referer) host must equal the Host header when present (`null` origin rejected). Login/setup (no session yet) require `Content-Type: application/json` (forces a CORS preflight, which is never granted) and the Origin check. |
| Brute force | `dashboard.login_attempts_per_minute` per client IP, separately for login, setup code and change-password (sliding window, `429` + `Retry-After`). Generic errors ("Incorrect password."). |
| Unauthenticated access | Middleware: every `/api/*` path except `/api/health`, `/api/session`, `/api/login`, `/api/setup` returns `401` without a valid session - including unknown paths. A test walks every router route. |
| XSS / injection | Frontend builds DOM with `textContent` only (no HTML strings), no inline scripts/styles in markup, strict CSP (below). Request bodies: pydantic, `extra="forbid"`, length limits, NFC + control-character stripping, enum/regex validation. Validation errors never echo input values. |
| Clickjacking / leaks | `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `Referrer-Policy: same-origin`, `X-Content-Type-Options: nosniff`, `Cross-Origin-Opener-Policy`/`-Resource-Policy: same-origin`, `Permissions-Policy` (no camera/mic/geo), `Cache-Control: no-store` on the API. No `/docs` / OpenAPI. |
| API key exposure | Keys are never read by the dashboard; the AI page shows only `has_anthropic_key` / `has_openai_key` (`config.public_dict()`). `scrub.py` additionally hides keys named `*api_key/secret/password/token/cookie/authorization` and values like `sk-…` in status, config, events and subsystem results. A test sets fake keys and scans every GET response. |
| Network exposure | Binds `127.0.0.1` unless `dashboard.lan_access: true` / `--lan`; a non-loopback `dashboard.host` without `lan_access` is refused (falls back to localhost, warning logged). |
| Dangerous actions | Shutdown/restart need `confirm: true`; video under 18 months needs `confirm_under_min_age: true`; arming servos needs `motion.physical_enabled: true` **and** the typed phrase `ARM MOTORS`; e-stop also disarms. Media play always passes `parent_override=False` (the media module enforces the age gate and limits from settings). |

Content-Security-Policy for dashboard pages:

```
default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://i.ytimg.com;
connect-src 'self'; font-src 'self'; media-src 'none'; object-src 'none'; frame-src 'none';
frame-ancestors 'none'; base-uri 'none'; form-action 'self'
```

(`i.ytimg.com` is only for thumbnails of approved videos; they degrade to an icon offline.)

Error mapping: `PermissionError` -> 403, `KeyError` -> 404, `ValueError` (incl. pydantic) -> 422,
missing subsystem -> 503. Bodies are `{"detail": "<friendly message>", "code": "..."}`;
422 adds `errors: [{field, message}]`.

## API reference

All JSON. "CSRF" = needs the `X-CSRF-Token` header (every POST/PUT/PATCH/DELETE below
except login/setup). All routes except the four public ones need a session cookie.

### Session and account

| Method | Path | Body / query | Notes |
|---|---|---|---|
| GET | `/api/health` | | public, `{"ok": true}` only |
| GET | `/api/session` | | public: `{authenticated, setup_required, min_password_length}` or, logged in, `{authenticated: true, csrf_token, expires_at, robot_name}` |
| POST | `/api/setup` | `{setup_code, password}` | public; 401 wrong code, 409 already set up, 422 weak password, 429; sets cookie |
| POST | `/api/login` | `{password}` | public; 401, 409 (not set up), 429; sets cookie |
| POST | `/api/logout` | | ends this session |
| POST | `/api/auth/logout-all` | | ends every session |
| POST | `/api/auth/password` | `{current_password, new_password}` | 403 wrong current; other sessions ended |
| GET | `/api/auth/sessions` | | `{active_sessions, current: {created, expires}, session_hours}` |

### Robot, alerts, system

| Method | Path | Body / query | Calls |
|---|---|---|---|
| GET | `/api/status` | | `robot.status()` + `uptime_seconds`, `age_group_label`, `child.nickname`, `settings` subset, `dashboard`, `alerts`, `server_time`. Cheap; the UI polls every 1.5 s while visible |
| POST | `/api/stop` | | `robot.stop_all("dashboard")` (or the context fallback) |
| POST | `/api/sleep`, `/api/wake` | | `robot.sleep()/wake()`; 409 if sleep is not possible now |
| POST | `/api/alerts/{id}/dismiss`, `/api/alerts/dismiss-all` | | parent alerts (attention / errors; category labels only, kept 30 min) |
| GET | `/api/system` | | version, mode, service `health()`, monitor, dashboard bind/URLs |
| GET | `/api/system/config` | | `config.public_dict()` (scrubbed) |
| GET | `/api/system/events` | `?n=1..300&pattern=glob` | `bus.recent()` newest first, scrubbed |
| POST | `/api/system/power` | `{action: shutdown\|restart, confirm: true}` | `robot.request_shutdown("dashboard <action>")` after the reply |

### Face

| Method | Path | Body | Calls |
|---|---|---|---|
| GET | `/api/face` | | status + `EXPRESSIONS`, `FACE_ANIMATIONS`, `FACE_STATES`, `demo_running` |
| POST | `/api/face/expression` | `{name ∈ EXPRESSIONS, hold?}` | `set_expression(name, hold=)` |
| POST | `/api/face/blink` | `{double}` | `blink(double=)` |
| POST | `/api/face/gaze` | `{x, y ∈ 0..1, hold?}` | `look_at()` |
| POST | `/api/face/gaze/clear` | | `clear_gaze()` |
| POST | `/api/face/animation` | `{name ∈ FACE_ANIMATIONS}` | `play_animation()` |
| POST | `/api/face/demo`, `/api/face/demo/stop` | | ~15 s scripted sequence via `ctx.face` (uses `ctx.clock`), cancelled by STOP |

### Activities

| Method | Path | Body / query | Calls |
|---|---|---|---|
| GET | `/api/activities` | `?age_months=&category=&age_group=&favourites=` | `list(age_months, category)`; `age_group` = overlap filter; items gain `enabled`, `favourite`, `age_label`, `suitable_now` |
| GET | `/api/activities/status` | | `status()` |
| GET | `/api/activities/{id}` | | `get(id)`; 404 |
| POST | `/api/activities/{id}/start` | | `start(id, source="dashboard")`; 404 / 403 |
| POST | `/api/activities/stop`, `/pause`, `/resume` | | `stop("parent stop")`, `pause()`, `resume()` |
| POST | `/api/activities/respond` | `{response, prompt_id?}` | `respond()`; 409 if nothing is waiting |
| PUT | `/api/activities/{id}/preferences` | `{enabled?, favourite?}` | stored in `profile.activity_preferences` (defaults are not stored) |

### Media

| Method | Path | Body | Calls |
|---|---|---|---|
| GET | `/api/media` | | `list_items()`, `status()`, settings, age policy, `supports_update` |
| POST | `/api/media/items` | `{url <= 500, title?}` | `add_item()` (worker thread); 201; `ValueError` -> 422 |
| PATCH | `/api/media/items/{id}` | `{title}` | `update_item()` if present, else remove + re-add |
| DELETE | `/api/media/items/{id}` | | `remove_item()`; 404 |
| POST | `/api/media/play` | `{item_id}` | `play(id, parent_override=False)`; 403 age gate / limits, 404 |
| POST | `/api/media/pause`, `/resume`, `/stop`, `/skip` | | protocol methods |
| POST | `/api/media/volume` | `{volume 0..1}` | `set_volume()` + `media.volume` |
| PUT | `/api/media/settings` | `{enabled?, session_limit_minutes 1..180?, daily_limit_minutes 0..600?, allow_under_min_age?, confirm_under_min_age}` | settings `media.*`; enabling under-18-months video without the confirm flag -> 422; session > daily -> 422 |

The Media page's "Open player on this device" uses `status()["player"]["path"]` (relative,
same origin) or, failing that, `["url"]` with a localhost host rewritten to the page's origin.

### Child profile

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/profile` | | profile fields, computed `age_months`, `age_source`, `age_group(_label)`, `video_allowed`, `cloud_ai_age_ok`, languages |
| PUT | `/api/profile` | `{nickname? (<=30, letters/digits/space/'-), birth_month? "YYYY-MM" (not future, <= 72 months), age_months? 0..72, language?, favourite_animals?/favourite_songs?/interests? (<=12 × <=40 chars)}` | birth month and manual age are alternatives (setting one clears the other); `activity_preferences` preserved |
| POST | `/api/profile/clear` | | `settings.reset("profile")` |

### AI

| Method | Path | Body | Calls |
|---|---|---|---|
| GET | `/api/ai` | | settings `ai.*`, `keys.has_*_key`, models, `brain.status()`, `brain.providers()`, age policy, privacy |
| PUT | `/api/ai/settings` | `{enabled?, provider? ∈ mock\|anthropic\|openai, max_words 3..60?, max_requests_per_hour 0..600?, max_requests_per_day 0..5000?, daily_budget_usd 0..20?, share_child_name?, share_interests?}` | enabling in offline mode -> 403 |
| GET | `/api/ai/providers` | | `brain.providers()` |
| POST | `/api/ai/test` | `{provider}` | `brain.test_provider()`; cloud provider while privacy cloud is off -> 403 |
| POST | `/api/ai/push-to-talk` | | `await brain.converse_once()` (45 s timeout -> 504) |

### Privacy

| Method | Path | Body | Calls |
|---|---|---|---|
| GET | `/api/privacy` | | `privacy.snapshot()` + `stores_audio`, `stores_transcripts`, `history_clearable` |
| PUT | `/api/privacy` | `{camera_enabled?, microphone_enabled?, cloud_enabled?, record_history?}` | `privacy.set(**flags)` (publishes `privacy.changed`); cloud in offline mode -> 403 |
| POST | `/api/privacy/clear-history` | | `activities.clear_history()`, `brain.clear_history()` if present |
| POST | `/api/privacy/stop-interactions` | | same as STOP (`source="privacy"`) |

### Audio and camera ("Audio & devices" page)

| Method | Path | Body | Calls |
|---|---|---|---|
| GET | `/api/audio` | | `audio.status()`, `audio.volume`, `audio.tts_rate`, max volume, `SOUND_NAMES` |
| GET | `/api/audio/devices` | | `list_devices()` in a worker thread |
| PUT | `/api/audio/settings` | `{volume 0..1?, tts_rate 0.5..1.5?}` | `set_volume()` (stores the clamped value), `audio.tts_rate` |
| POST | `/api/audio/test-speaker` | | `test_speaker()` |
| POST | `/api/audio/test-microphone` | `{seconds 0.5..5}` | `test_microphone()`; 403 while the microphone is off |
| POST | `/api/audio/say` | `{text <= 200}` | `say()` in the background |
| POST | `/api/audio/sound` | `{name ∈ SOUND_NAMES}` | `play_sound()` in the background |
| POST | `/api/audio/stop` | | `stop_output()` |
| GET | `/api/vision` | | `vision.status()`, `vision.follow_faces`, camera privacy |
| PUT | `/api/vision/settings` | `{follow_faces}` | `vision.follow_faces` (+ `clear_gaze()` when off) |
| POST | `/api/vision/simulate-face` | `{x, y, size?}` | `simulate_face()` |
| POST | `/api/vision/simulate-no-face` | | `simulate_no_face()` |

### Motion

| Method | Path | Body | Calls |
|---|---|---|---|
| GET | `/api/motion` | | `status()`, `gestures()`, `physical_enabled` (config), `armed` (module), `arm_requested` (setting), `simulation_only`, `reason`, servo limits |
| POST | `/api/motion/gesture` | `{name ∈ gestures()}` | `run_gesture()` in the background; 404 unknown, 409 while e-stopped |
| POST | `/api/motion/joint` | `{joint, angle, speed_dps?}` | `move_joint()`; 422 outside the joint's min/max |
| POST | `/api/motion/neutral`, `/cancel` | | `neutral()`, `cancel()` |
| POST | `/api/motion/estop` | | `emergency_stop("dashboard")` (synchronous) + disarm |
| POST | `/api/motion/estop/reset` | | `reset_emergency_stop()` |
| POST | `/api/motion/arm` | `{armed, confirmation}` | disarm always allowed; arm only with `motion.physical_enabled` (else 403 "simulation only") and `confirmation == "ARM MOTORS"` (else 422) |

The Motion page draws the robot from `joints[*].position` (-1..1, 0 = neutral, +1 = arm
raised / head turned to the robot's left), falling back to angles and limits.

### Simulator

| Method | Path | Body | Calls |
|---|---|---|---|
| POST | `/api/sim/speech` | `{text <= 200, route: auto\|brain\|microphone, speak}` | `auto`: microphone (`audio.inject_utterance`) while an activity runs or audio is listening, else `brain.handle_utterance(text, source="simulated", speak=)` |
| POST | `/api/sim/caregiver` | `{response, prompt_id?}` | `activities.respond()`; 409 if nothing waiting |
| POST | `/api/sim/camera` | `{event: face\|no_face\|person_left, x, y, size}` | `vision.simulate_face/no_face()`, or bus events when there is no vision module |

## Frontend

* No build step, no frameworks, no CDN: ES modules served from `/static/js`. Works offline.
* Hash router (`#/home`, `#/activities`, ...). Phone: sticky top bar (live face + state + red
  **STOP**), bottom tab bar (Home, Activities, Videos, Face, More). Desktop >= 960 px: sidebar.
* `GET /api/status` every 1.5 s while the tab is visible (stops when hidden, resumes on
  focus); an offline bar appears when the robot can't be reached; a 401 returns to login.
* A pending caregiver prompt shows prominently on Home and Activities and as a bar on other
  pages; parent alerts (e.g. "Your child may need you") show on every page until dismissed.
* Light/dark via `prefers-color-scheme`, 44 px+ touch targets, labelled controls, visible
  focus rings, `prefers-reduced-motion` honoured.

## Tests

`tests/dashboard/` (fakes in `dashboard_fakes.py`: activities, media, brain, vision, robot
facade; face/audio/motion fakes from `littlebot.testing`):

* `test_dashboard_auth.py` - setup code flow, login, rate limits, sessions (expiry, logout,
  logout-all, restart persistence, hashed storage), password change, CSRF + Origin, every
  protected route -> 401, security headers, static files, no external resources, API keys
  never returned.
* `test_dashboard_api.py` - every section calls the right protocol methods; validation and
  error mapping; real `LittleBot` facade; media player router mount; parent alerts.
* `test_dashboard_service.py` - bind-host logic, real uvicorn on an ephemeral port in the test
  loop (setup code logged, HTTP round trip), port in use -> FAILED without crashing, STOP
  cancels dashboard tasks, `reset-password` CLI.

Visual QA (local only): Playwright screenshots of every page at 390 px and 1280 px, light and
dark, saved under `artifacts/dashboard/` (git-ignored).
