# Info cards: LittleBot's screen while it talks

During a voice chat LittleBot's face screen can show what was just asked: the weather, what a word
means, the key points of an explanation, a recipe one step at a time, a timer, a clock, a sum. The
screen backs up the voice: the card shows the gist, big and readable, and the voice says the rest.

LittleBot's own eyes stay on the screen, small, in a corner of the card: the **presenter**. They blink
now and then, glance at the card every few seconds, and a small mouth under them moves with the voice
(the same lip-sync the big face uses), so it looks like LittleBot is presenting the card.

Code: `littlebot/screens/` (see its `__init__.py` for the modules). The voice side (what to show for
what was asked) is `littlebot/screens/director.py` and the live voice's tools.

## How it looks

* The 720x480 canvas, like the on-screen games, scaled into the face area: the games' soft pink
  background (`littlebot/games/art.py` `background`), rounded candy-coloured shapes, dark plum text.
  The screen service paints that background first, so every card sits on the same calm pink.
* The presenter's box is `Card.presenter_box`, top-left by default (`base.PRESENTER_BOX`,
  150x86 px). The eyes are the games' hosting eyes (`art.draw_eyes`: rim, gradient, big pupil, two
  catchlights) in the face's eye colour, on a little patch of LittleBot's pink face with a white rim;
  under them the real face's mouth (`face/frame.py` `mouth_edges`, `face/mouth.py` spoken shapes) at
  that size. A card may move the box but keeps it on the screen.
* Moods (`Card.mood`): `neutral`, `happy` (closed happy arcs and blush), `surprised` (wide eyes, an
  "o" mouth), `curious` (looking up a little).
* Pictures from the internet are fetched only when a card needs one (`littlebot/screens/images.py`):
  strict safe search, shown with a small credit line, never saved.

See them without a robot: `.venv/bin/python scripts/screen_preview.py --out /tmp/cards` renders every
card type's samples to PNGs (`--kind weather` for one kind, `--mouth 0.8` for a talking presenter).

## The games menu

Cards generally appear only during a voice chat (the director decides). One exception: the **games
menu** (`littlebot/screens/cards/games.py`, kind `games`), a scrollable list of every game on the
dashboard's Games page, each with a small drawn icon, its name, a few words and the ages.

* Opened by "show me the games" in a chat (`{"card": "games"}`; optional `"ages"`: `all`, `child` for
  the child's age, `little` or `big`), or by a **long press** - holding a finger still on the idle face
  for about a second (`face/display.py`, `LONG_PRESS_SECONDS`). A plain tap still makes LittleBot
  giggle: babies tap the face all the time.
* Drag to scroll (with a little fling), or the arrow keys (Enter / space plays the highlighted game).
* Tap a game to play it: `ctx.games.start(id, source="screen")`, like the dashboard's Play button (a
  voice chat keeps talking while the game takes the screen). The game takes the screen and the menu goes;
  if it can't start, the menu says why for a moment.
* `describe()` lists the names in order, so the voice can read them out.
* Outside a chat it goes a minute after the last touch (the card's own `linger_seconds`), on a tap on
  the presenter's eyes, or on Esc.

## How a card goes away

* While a voice chat is on (the live voice or a conversation holds the face / speaker / microphone)
  the card stays. A new card replaces the old one.
* After the chat ends it stays **30 s** (`screens.linger_seconds` in the config; a card may set its
  own `linger_seconds`), then the face comes back. A new chat in those 30 s keeps it, and so does a touch
  on the card (the wait starts again).
* A **running timer** (a `sticky` card) stays until it is done, chat or no chat, and keeps LittleBot
  from falling asleep meanwhile (it publishes `screen.keep_awake` every minute).
* A running timer is **never lost to another card**. When a new card replaces it, or a video, a game or
  an activity takes the screen, it carries on behind it (`screens.behind`): it still chimes and tells
  the voice at zero, LittleBot stays awake for it, and the timer card comes back by itself once the
  screen has been free for a second (the card on top went, the video ended), or at once when a timer
  rings. Put away by hand (a tap on the eyes, Esc, "close it", the dashboard) it comes back only when a
  timer rings. A timer that already rang is let go. A new timer joins the one behind the screen, and
  the voice can still add time to it, pause it or cancel it. STOP ALL, sleep and the parent's switch
  end it.
* Sooner: a **tap on the presenter's little eyes**, "close it" to the voice, **Esc** in the
  simulator, the dashboard's *Close the card* button, or a card that has nothing more to show.
* Always: STOP ALL, LittleBot going to sleep, a game, a video or an activity starting (they take the
  screen; a game always wins over a card; a timer carries on behind them, above), and the parent
  switching cards off.

A card takes only the screen (`Resource.SCREEN`, at conversation priority). The face and speaker stay
with the voice, so a card never stops the voice; a game or a video simply takes the screen from it.
Other taps go to the card (next page, next step).

**A card's picture, full screen.** On a card with a picture (explain, facts, a word's meaning), "zoom into
that", "make the picture bigger" or "make the image take over the screen" show the picture on the whole
screen; "zoom in" again goes closer (1.5x, 2.25x, 3x: the middle of the picture), "zoom out" steps back, and
"back to the card" or a tap shows the whole card again. The screen service does it for every such card
(action `picture`, `base.PICTURE_ACTION`; words in `director._PICTURE_VIEWS` and, looser, in
`screens/picture_words.py`: "show the picture in full screen", "can I see it bigger?", "exit full screen"); the
same command heard twice within 2.5 s (the voice's hand-off and the screen director) counts once. When the voice
hands such a request over, LittleBot does it at once without the text model (`chatgpt/quick.py`), and the text
model has a `change_card` tool (offered only while a card is up, naming it and what it can do) for anything the
words miss, so it changes the card instead of making a new one or opening Google.

## The parent's switches

Dashboard → **Privacy** → *LittleBot's screen*:

| Switch | Setting | Default | What it does |
|---|---|---|---|
| Info cards on the screen during voice chats | `screens.enabled` | on | Off: the voice still answers, the screen keeps the face. Switching it off closes a card on the screen. |
| Show pictures from the internet on LittleBot's screen | `screens.web_pictures` | on | Off: cards show no pictures. On: a picture of what was asked about, found with strict safe search; only the search words are sent, nothing is kept. |

The same section shows what is on the screen now, with a button to close it.

**The browser view** is the other thing that can take the screen in a chat: only when a grown-up
explicitly says "google it" / "look it up online" / "search that in the browser" (never a card for those
words; no card is decided while it shows). Its own switch, *Allow the browser view (Google with
SafeSearch, grown-ups only)*, is in this section too. See [BROWSER.md](BROWSER.md).

`screens.enabled: false` in the config (`config/default.yaml`, `screens:` section) switches the feature
off whatever the parent chose; `linger_seconds` and `presenter_backing` live there too.

API (parent login, like every dashboard route): `GET /api/screens` (status), `POST /api/screens/close`,
`PUT /api/screens/settings` `{enabled?, web_pictures?}`, and `POST /api/screens/show` `{spec}` to try a
card from the dashboard (`{"spec": {"card": "explain", "title": "...", "points": [...]}}`).

## The screen service (`ctx.screens`)

`littlebot/screens/service.py`, `ScreenService`:

* `await show(spec, source="voice")` → `{"ok", "showing", "error"}`; errors `"disabled"`, `"busy"` (a
  game, video or activity has the screen, or LittleBot is asleep), `"no_card"`.
* `show_card(card, source=...)` for a card already built (`make_host()` gives the real host to build with).
* `command(action, args)` → bool, `close(reason)` → bool, `current`, `active`, `describe()`, `status()`.
* Display hooks for `face/display.py`: `render(surface, rect, dt)`, `handle_tap(nx, ny)`,
  `handle_press` / `handle_drag` / `handle_release(nx, ny)` (the finger's path: a card with an optional
  `drag(dx, dy)` method, and `drag_end()`, scrolls, and a press-and-release that didn't move is its tap;
  other cards get their tap on the press, like the games), `handle_key(name)`, `animating` (full frame rate while the card pops in, the presenter talks, or the
  card sets `animating = True`; otherwise the idle rate).
* Bus: `screen.card_shown` {kind, title, source}, `screen.card_closed` {kind, reason},
  `screen.news` {kind, title, text}, `screen.keep_awake` {kind}.
* The presenter's mouth follows `Topics.AUDIO_VISEME` ({level, shape}) from the voice.

The host a card gets: `sound(name)` and `say(text)` go through `ctx.audio` fire-and-forget (`say`
becomes `tell` while a chat is on, so a card never talks over the voice); `tell(text)` publishes
`screen.news`, which the live voice passes into the call as a quiet note; `picture(query, url)` calls
`images.fetch(ctx, query=..., url=...)` (when pictures are off, or `images.py` isn't there, it
returns a picture that is `ready` with no `surface`, as a failed fetch would: the card shows its
no-picture layout); `close()` takes the card off.

## Adding a card type

A card is a small object driven by the display loop, like a game (`littlebot/screens/base.py`):

```python
from littlebot.games import art
from littlebot.screens.base import BODY_PX, TITLE_PX, W, Card, CardType, register


class ShoppingCard(Card):
    kind = "shopping"

    def __init__(self, spec, host=None):
        super().__init__(spec, host)
        self.items = [str(i)[:40] for i in spec.get("items", [])][:5]

    def update(self, dt):           # logic only, no drawing
        super().update(dt)

    def draw(self, surf):           # the whole 720x480 canvas; leave presenter_box free
        surf.blit(art.background(), (0, 0))
        art.blit_text(surf, self.title, TITLE_PX, (W / 2 + 60, 52))
        for i, item in enumerate(self.items):
            surf.blit(art.text(item, BODY_PX), (200, 130 + i * 56))

    def command(self, action, args):  # the voice steering it
        return False

    def describe(self):             # one line for the voice: what the screen shows
        return f"{self.title}: {len(self.items)} things"


async def _build(spec, ctx, host):  # fetch what it needs; None = nothing to show
    return ShoppingCard(spec, host) if spec.get("items") else None


register(CardType(
    kind="shopping", description="A short shopping list", build=_build, required=("title", "items"),
    fields={"title": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
))

SAMPLES = [{"card": "shopping", "title": "Shopping", "items": ["Milk", "Bananas", "Nappies"]}]
```

* Put it in `littlebot/screens/cards/<kind>.py` and add the module name to `_MODULES` in
  `littlebot/screens/cards/__init__.py`.
* `fields` / `required` are what the AI fills in (JSON Schema properties); `actions` lists what
  `command()` understands, for the AI ({"next": "the next step"}).
* Set `self.mood` for the presenter, `self.sticky = True` while something runs that must outlive the
  chat (and `self.done = True` when it is over), `self.presenter_box` to move the eyes.
* Ask the host for sounds, notes to the voice and pictures; never touch the robot directly. Never
  block: `update` / `draw` / `tap` run on the display thread at up to 30 fps on a Pi 5.
* A module-level `SAMPLES` list feeds `scripts/screen_preview.py`. Test cards with `RecordingHost`
  and `littlebot/screens/preview.py` (`preview.render(card, "out.png")`).

## Readability rules

A 3.5" screen read across a room, or by a parent with flour on their hands:

* Text at least `BODY_PX` (32 px on the canvas); titles `TITLE_PX` (44), a recipe step `STEP_PX` (42);
  nothing smaller than `SMALL_PX` (26), and that only for labels and credits.
* At most `MAX_LINES` (5) lines of text at a time: more is paged (a tap, a swipe or "next").
* The gist, not the transcript: a few words per line; the voice says the rest.
* Dark plum text on light shapes (`art.INK`), no text over busy pictures, no thin fonts.
* Keep the presenter's box free and on screen.
