# The browser view: Google on LittleBot's screen

When a grown-up explicitly asks LittleBot to **google something** or **look it up online / in the
browser**, LittleBot shows Google's results (SafeSearch always on) on its screen instead of an info
card. It doesn't look like a browser: there is no address bar, tabs or menus, only the page, a little
pink strip in the top-right corner with LittleBot's mini eyes (blinking, glancing about) and a big **×**
to close it. Ordinary questions ("how tall are giraffes?") still get the voice's answer and, maybe, an
info card ([SCREENS.md](SCREENS.md)).

Code: `littlebot/browser/` (`service.py` the `BrowserView`, `ctx.browser`; `policy.py` where it may go;
`page_scripts.py` the strip and the page scripts; `cdp.py` the DevTools client).

## Asking for it

Explicit words only: "google it", "google how tall giraffes are", "can you google that?", "look it up
online", "look up the zoo opening hours online", "search that in the browser", "search for dinosaur facts
on the internet", "check the web for ...", "open the browser", "show me red pandas on Google".

* With their own words ("google *how tall giraffes are*") those words are searched.
* "Google it" / "look that up online": what *it* is comes from the conversation (one small request to the
  ChatGPT text model, like the info cards' decisions; their previous question if that fails). Nothing to
  go on: Google's start page.

While it shows:

| Say | Does |
|---|---|
| "scroll down", "scroll up a little", "go to the top", "keep scrolling" | scrolls |
| "open the first one", "click the third result", "result number two", "the last one" | opens that search result |
| "go back", "back to the results", "go forward", "refresh" | history |
| "make it bigger", "zoom out", "normal size" | zoom |
| "what does it say?", "read me the page" | the page's main text goes to the voice, which sums it up in a few words (it never reads a page out) |
| "close it", "close the browser", "we're done with Google" | closes it |

Or touch it: scroll with a finger, tap links. Links that would open a new window open in the same view.

Two paths ask for it, and the browser does each thing once even when both fire:

1. **The screen director** (`littlebot/screens/director.py`, `browser_rule`): the words above, matched on
   the robot as soon as the caption arrives. A browser turn never gets a card, and while the browser
   shows no card is decided at all.
2. **The voice's tools** (`littlebot/chatgpt/tools.py`): `search_in_browser {query}` and `browser
   {action, ...}` (scroll, open_result, back, forward, reload, zoom, read, close), offered only while
   the browser view can open, and described to the model as "ONLY when the grown-up explicitly asks".
   The voice's instructions say the same (`VOICE_INSTRUCTIONS`, added by `live.py`
   `_browser_instructions`).

## Safety

* **Grown-ups only.** A turn voice recognition says was the child's (`voice.speaker`, `is_child`) opens
  nothing, from the director or a tool (15 s window). The dashboard counts as a grown-up.
* **SafeSearch is forced on.** Every search is `https://www.google.com/search?q=...&safe=active`, and every
  page load the browser makes is checked (DevTools `Fetch.requestPaused`): a Google, Bing or DuckDuckGo
  search page without its safe-search setting (`safe=active`, `adlt=strict`, `kp=1`) is sent to the same
  search with it. Every page shown is checked again (`Page.frameNavigated`).
* **https only**: plain http, `file:`, `chrome:`, `data:`, `javascript:` pages, addresses with passwords,
  `localhost`, `*.local` and private network addresses are refused; the page is replaced by a short pink
  "LittleBot won't open that page" note.
* **The query passes LittleBot's safety rules** (`find_blocked`, as for anything it says); a blocked
  search is politely refused and nothing opens.
* **Nothing else gets out**: downloads denied, every permission (notifications, camera, microphone,
  location, ...) denied, pop-ups and new windows closed at once, file pickers and dialogs dismissed, sound
  muted (videos go through LittleBot's YouTube player, never in here), no autoplay without a tap.
* **Nothing is kept**: each time is a new incognito Chromium with a throwaway profile folder, deleted when
  it closes (left-overs from a crash are deleted at the next start).
* **Needs** the parent's switch, *Cloud communication* on (Privacy) and not offline mode. Cloud
  communication switched off closes it.
* **Logs**: one INFO line when it opens (where from; "a search" or the host), one per page (the host) and
  one when it closes (why, how long). Never the search words or what a page says.

## How it goes away

* The **×** on the strip, "close it" / "close the browser" / "show me your face" / "let me see your face"
  to the voice, the dashboard's *Close the browser*.
* **20 seconds** (`browser.moved_on_seconds`) after they talked about something else in the chat (a turn
  with no browser words), unless the page is used again meanwhile: a touch, "scroll down", a new search.
  A turn within 10 s of using it counts as about it (the voice's own search can come before its words).
* STOP ALL, LittleBot going to sleep, an error.
* A **video**, **game** or **activity** starting (they take the screen). Starting the browser stops a
  playing video first (one Chromium at a time); a video paused for the chat stays paused.
* **30 seconds** (`browser.idle_seconds`) after the voice chat ended with nobody touching the page. During
  a chat it stays; a touch starts the wait again (and keeps LittleBot from dozing off).
* The parent's switch or cloud communication going off, Chromium exiting.

It takes only the screen (`Resource.SCREEN`): taken like a video (a card or the games menu goes), then
held at priority 45, so a game (50) or a video (60) wins. The robot's state doesn't change and the face
keeps running underneath, so the voice chat carries on, and "Hey LittleBot" works as usual with the
browser up (its page is silent).

## The parent's switch

Dashboard → **Privacy** → *LittleBot's screen* → **Allow the browser view (Google with SafeSearch,
grown-ups only)** (`browser.enabled`, on by default). The same place shows what it shows now and has a
*Close the browser* button.

API (parent login): `GET /api/browser` (status), `POST /api/browser/close`, `PUT /api/browser/settings`
`{enabled}`.

Config (`config/default.yaml`, `browser:`):

| Key | Default | What |
|---|---|---|
| `enabled` | `true` | `false`: no browser view at all, whatever the parent's switch says |
| `idle_seconds` | `30` | how long it stays after the chat with nobody touching it |
| `moved_on_seconds` | `20` | during the chat: how long it stays after they talked about something else |
| `zoom` | `1.4` | how big pages are drawn (0.75 to 2.5) |
| `mobile_layout` | `true` | ask sites for their phone layout (fits the small screen) |
| `kiosk` | (Pi only) | full-screen kiosk window; off: an app window (the desktop simulator) |
| `read_chars` | `3000` | how much of a page "what does it say?" gives the voice |
| `start_timeout_seconds` | `20` | how long Chromium may take to start |

## How it works

* The window is the videos' Chromium kiosk (`littlebot/media/display.py` `BrowserKioskBackend`: the same
  binary, Wayland hint and shutdown) in `--kiosk --app=` mode: no address bar, tabs or menus. It opens on
  a pink "Looking it up..." page and goes to Google once everything below is in place.
* DevTools listens on `127.0.0.1` only, on a free port Chromium picks itself (`--remote-debugging-port=0`;
  it writes the port into `DevToolsActivePort` in the throwaway profile). `littlebot/browser/cdp.py` is a
  small WebSocket client using only Python's standard library (LittleBot has no WebSocket package) and
  refuses any host but this computer.
* The strip is added to every page in an **isolated world** (`Page.addScriptToEvaluateOnNewDocument` with
  a world name, `Runtime.addBinding` for that world only), inside a closed shadow root: the page's own
  scripts can't see, move or click it. Only real (trusted) taps count. It reports the × and touches back
  through the binding.
* Reading, scrolling and the results list run in another isolated world of LittleBot's
  (`Page.createIsolatedWorld`). Search results are Google's organic results (no ads).
* Zoom is `Emulation.setDeviceMetricsOverride` (the window's own size, drawn bigger), plus a phone user
  agent and touch emulation so sites send their small-screen layout.

## Troubleshooting

| Problem | Try |
|---|---|
| The voice says the browser isn't available | Chromium installed? (`sudo apt install chromium-browser`); not headless / offline mode; `browser.enabled` in the config |
| It never opens | Privacy: *Cloud communication* on, and *Allow the browser view* on; was it the child asking? The log says why (`browser: not opened (...)`) |
| It opens but stays on "Looking it up..." | No internet? The log says `the page didn't load` |
| It closes by itself | The log's `browser: closed (...)` line says why (a video, a game, nobody used it ...) |
| Text too small / too big | `browser.zoom` in the config, or "make it bigger" |
