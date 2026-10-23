# On-screen games

LittleBot can turn its face screen into a little games console. Eight games, in one soft-pink,
rounded, candy-coloured style; LittleBot's own eyes host every game.

| Game | id | Ages | What happens |
|---|---|---|---|
| Bubble Pop | `bubbles` | 6 months+ | Bubbles float up. Tap one: *pop* + sparkle. Tap empty space: a new bubble. Unpopped bubbles pop quietly by themselves; a golden star bubble comes by now and then. |
| Peekaboo Friends | `peekaboo` | 6 months+ | Cat, dog, duck, cow, frog and sheep hide behind three toy blocks and pop up ("Moooo!"). Tap a block to find who's hiding, tap an animal to make it giggle and hop. |
| Colour Burst | `colours` | 6 months+ | Every tap paints a splash or a smiling shape with a note (low on the left, high on the right). Paint fades after ~9 s. Idle? LittleBot paints something to invite a tap. |
| Follow the Star | `star` | 6 months+ | LittleBot's big eyes follow a smiling, twinkling star. With the camera on, the star looks at the child and LittleBot glances over every few seconds. Tap the star to catch it, or tap anywhere to send it there. |
| Memory Lights | `memory` | 3 years+ | Simon-style: four pads (circle, heart, star, diamond) light up with tones; copy the pattern, one step longer each round. A mistake shows the right pad and starts again; best round is kept. |
| Tic-Tac-Toe | `tictactoe` | 4 years+ | You are X. LittleBot always takes a win in front of it, otherwise plays perfectly only some of the time: it gets cleverer when you win and gentler when you lose. Who starts alternates. |
| Reaction Race | `reaction` | 4 years+ | LittleBot's round face goes red ("Wait..."), then green - tap! Shows milliseconds; five tries per round with best and average. Tapping early says "Too soon!" and doesn't count. |
| Rock Paper Scissors | `rps` | 4 years+ | Pick rock, paper or scissors; after "Rock... Paper... Scissors... Shoot!" LittleBot reveals a random pick. |

The little-ones games need no reading and reward every tap (nothing is ever "wrong"). They also
play by themselves, so a baby who only watches still sees bubbles pop and animals peek. They are
short on purpose (3 minutes by default) and best played together - name the colours and animals,
pop bubbles side by side. Sounds are LittleBot's gentle built-in effects at the parent's volume.

## Playing

* **On the robot**: tap the screen. Needs the touch layer (see *Touch* below).
* **Desktop simulator**: click (left button) in the face area; keys 1-9 / space / arrows work in
  the games that use them; **Esc** stops the game. All the usual simulator keys (X = STOP, E =
  motion e-stop, Q = quit) still work.
* **Dashboard** -> **Games** (in the side bar; on a phone under *More*): Play / Stop / Restart,
  the time left, and the game length (game default, 2, 3, 5 or 10 minutes; remembered in that
  browser). The big STOP button stops a game too.
* **Voice**: the AI tools call `ctx.games.list()`, `await ctx.games.start(id)` and `ctx.games.stop()`.

Each game ends by itself (babies' games after 3 minutes, the others after 5 unless a length is
chosen; always between 30 s and 15 min): a short finale (confetti, LittleBot's happy eyes, a
summary like "12 bubbles popped") and then the face comes back with a happy expression.

## Touch (UNVERIFIED on the robot)

The MPI3508's resistive touch (ADS7846/XPT2046 on SPI) is optional and not yet set up or tested
on the Pi 5 - see [DISPLAY_SETUP.md](DISPLAY_SETUP.md) section 5. When the kernel driver works
and the compositor maps it to HDMI-A-1, SDL delivers each touch to the window as a finger event
**and** a synthesised left mouse click; the display loop handles `MOUSEBUTTONDOWN`, so taps
reach the games (and the existing face tap reaction) with no extra code. Things to check on the
hardware:

1. `sudo evtest` shows the touchscreen and sensible coordinates.
2. Start Bubble Pop from the dashboard and tap a bubble: it should pop where you touched. If taps
   land in the wrong place, calibrate (DISPLAY_SETUP.md 5.3/5.4) - targets are big (bubbles
   have ~18 px of slack) but not that forgiving.
3. If touches do nothing at all while `evtest` works, SDL may not be synthesising mouse events
   (`SDL_HINT_TOUCH_MOUSE_EVENTS=0`); handling `FINGERDOWN` in `face/display.py` would then be needed.

Without touch the baby games still entertain on their own, and the kids' games can be played from
the desktop simulator; a phone-as-controller mode would be a possible next step.

## How it works

`littlebot/games/`:

* `catalog.py` - metadata (id, name, description, ages, audience, default minutes, emoji, how-to).
  Cheap to import; the game module is imported only when the game starts.
* `service.py` - `GamesService` (`create_service(ctx)`, registered as `ctx.games` in
  `app/robot.py`).
* `base.py` - `Game` base class and the `GameHost` interface.
* `art.py` - palette, cached anti-aliased sprites, text, LittleBot's eyes (`EyeState`,
  `draw_eyes`), particles.
* one module per game.

### Public API

```python
games.list()                            # [{id, name, description, ages, min_age_months, audience, minutes, emoji, how_to}]
await games.start("bubbles", source="voice", minutes=None)   # -> bool
games.stop()                            # truthy if a game was stopped; also `await games.stop()`
games.status()                          # {available, running, id, name, emoji, source, seconds_left, limit_seconds,
                                        #  elapsed_seconds, shared, drawn, game: {score, summary, ...}, last, last_error}
```

`start()` returns False for an unknown id or when LittleBot is busy with something more important
(a video holds the screen, the parent); `status()["last_error"]` says why in plain words. Starting
a game replaces a running one and stops a running activity. Events: `game.started`
`{id, name, source, limit_seconds, shared}` and `game.finished` `{id, name, reason, completed,
duration_seconds, summary, score}` (reasons: `completed`, `time up`, `stopped`, `parent stop`,
`stop all`, `keyboard`, `replaced`, `robot sleeping`, `preempted by <owner>`, `error`).

Dashboard API (`dashboard/routes/games_routes.py`): `GET /api/games`, `GET /api/games/status`,
`POST /api/games/{id}/start` (optional `{"minutes": 0.5..15}`; 404 unknown, 409 busy, 503 no
games), `POST /api/games/stop`.

### Screen, leases and robot state

A game takes `Resource.SCREEN` plus `FACE` and `AUDIO_OUT` at `Priority.ACTIVITY` (two leases:
screen, and face + speaker), and moves the robot to `PLAYING_ACTIVITY` so it doesn't doze off
mid-game. It ends on STOP ALL, `arbiter.preempt_all`, preemption by a video (MEDIA), an activity,
or the parent, when the robot goes to SLEEPING / ERROR / PLAYING_MEDIA, on Esc, or when its time
is up. The face is restored afterwards (eyes open, gaze released, neutral - or happy + sparkle
after a finished game; a preempting owner keeps control of the face).

Voice sharing: if a conversation lease owner (`conversation`, `live_voice`) holds the face /
speaker when a game starts - e.g. a voice tool starts a game in the middle of LittleBot's reply -
the game takes only the screen and *shares* the rest, so the reply is not cut off, and leaves the
robot state to the conversation. Likewise live voice (PARENT priority) starting during a game takes
the face / speaker while the game stays on screen. When the conversation ends, the game takes them
back. A local push-to-talk turn (`conversation`, CONVERSATION priority) is refused while a game
holds the face - voice features that want LittleBot's face back should call `ctx.games.stop()`
first.

### Display hook

`face/display.py` asks `ctx.games` each frame: while `games.active`, `games.render(window,
face_rect, dt)` draws the game *instead of* the face (overlays such as the setup code, debug and
help still draw on top), left clicks / taps go to `games.handle_tap(nx, ny)` (never falling through
to the face's giggle or the simulator's pretend camera face), keys go to `games.handle_key(name)`
first, and the frame rate is held at `display.fps`. Games draw on a 720x480 canvas - exactly the
robot's screen mode, so no scaling there; any other face rect (desktop window) gets a smooth-scaled
copy. A game that throws is logged; after 30 bad frames it is stopped and the face returns.

Without a window (headless runs, tests) a small watchdog task keeps the game's clock running, so
time limits and finales still happen.

### Sounds

Games use only the built-in effects (`core.interfaces.SOUND_NAMES`), short `play_melody` notes and
`say()` for animal noises, all fire-and-forget. LittleBot's speaker plays one thing at a time, so
the host throttles: a sound is dropped when more than ~0.35 s of game audio is already queued
(speech: 1.2 s). A tap storm therefore never builds an audio backlog; the visual feedback is
always instant. Sounds that matter even when a little late (the giggle after "Moo!", Memory's
"uh-oh" after a pad's tone, the finale's "tada") pass `max_wait=` to queue for up to 1-2 s.

### Camera and speech

`host.child_gaze()` returns where LittleBot should look to look at the child (from
`vision.faces`, fresh for 2 s) - used by Follow the Star. Recognised speech (`audio.utterance`
events) is passed to the running game's `hear(text)`: Rock Paper Scissors picks "rock", "paper"
or "scissors" from it. Games never start listening themselves.

## Adding a game

1. Subclass `littlebot.games.base.Game`; override `step(dt)` (logic), `render(surface)` (draw the
   whole 720x480 canvas), `on_tap(x, y)` (canvas pixels) and optionally `on_key`, `hear`,
   `summary`, `finale_text`, `status_extra`. Keep update and draw separate; ask the host for
   sounds (`self.host.sound("pop")`).
2. Add a `GameInfo` to `catalog.GAMES`.
3. Test it headless with `RecordingHost` (see `tests/games/`).

Art tips (learned the hard way): cache every curved shape (`art.cached` + `art.supersampled`);
`pygame.draw` with an alpha colour on an SRCALPHA surface *replaces* pixels instead of blending
(use pre-mixed opaque colours, or blit); never restore a cached sprite with `set_alpha(None)` -
it switches off per-pixel blending for good (`art.blit_center` restores 255). Thick diagonal lines
come out thinner than their round caps; use `art.capsule`.

Cost on a desktop CPU: 0.6-2.5 ms per frame (update + draw); expect roughly 3-4x on the Pi 5,
well inside the 33 ms of a 30 fps frame. Colour Burst spends a few ms generating each new splash.
