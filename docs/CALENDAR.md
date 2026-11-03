# The family's calendars

LittleBot can read your Google calendars and tell you what's on:

* "What's on today?" / "What's on this week?"
* "What's on Ali's work calendar tomorrow?"
* "Is Sam busy on Friday afternoon?"
* "When is the dentist?" (it looks a month ahead)

Each person can have **as many calendars as they like**, for example Ali: *Work* and *Personal*, and Sam:
*Personal*. You add each calendar once, on the dashboard. There's no Google sign-in, no Google Cloud project
and no API key. LittleBot only **reads** the calendars, and it never changes them.

> **Status.** Tested against stand-ins for Google's calendar feeds (repeating events, one-off changes,
> cancellations, all-day and multi-day events, time zones). Not yet tested against a real Google account or
> on the Pi.

## 1. Set it up (about 2 minutes per calendar)

On LittleBot's dashboard, open **Settings**, then **Calendars**. The page also has these steps.

For **each** calendar:

1. On a computer, open **calendar.google.com**, signed in as the calendar's owner.
   On a phone, open it in the browser and choose **Desktop site** in the browser's menu. The Google
   Calendar app doesn't show the setting you need.
2. On the left, under **My calendars**, point at the calendar, click its **⋮** menu, then **Settings and
   sharing**.
3. Scroll down to **Integrate calendar** and copy the **Secret address in iCal format**, using the copy
   button next to it. It starts with `https://calendar.google.com/calendar/ical/` and ends with `basic.ics`.
4. On the dashboard, press **Add a calendar** and fill in:
   * **Whose calendar**: the person's name, the way you'll say it ("Ali").
   * **Which calendar**: *Work*, *Personal*, *Family*... so you can ask for one. Leave it empty to use the
     calendar's own name.
   * **Secret address in iCal format**: paste the address.
5. Press **Add calendar**. LittleBot reads the calendar straight away. It says how many events are in the next
   two weeks and which one is next, or what's wrong with the address.

The form stays open with the same name, so a second calendar for the same person (Work, then Personal) only
needs its name and address.

**Cloud communication** (Privacy page) must be on for LittleBot to read the calendars. A calendar you add while
it is off is kept, and LittleBot reads it once cloud communication is on.

### Work calendars

Some workplaces (Google Workspace) switch off the secret address. Then **Integrate calendar** has no
*Secret address in iCal format*. In that case, ask your IT team, or add only your personal calendar.

### Other calendar apps

Any calendar app that can publish an iCal (`.ics`) link works the same way: Outlook ("Publish a calendar",
the ICS link), iCloud ("Public calendar"), and others. Paste that link instead.

### Public Google calendars

A calendar that is made public (holidays, a school's or a team's calendar) also works with its **Public
address in iCal format**, or simply its **Calendar ID** (`...@group.calendar.google.com`). Its embed or
subscribe link works too.

## 2. Asking

In a voice chat, or in typed chat on the AI page:

| You say | LittleBot looks at |
|---|---|
| "What's on today?" | everyone's calendars, today |
| "What's on Ali's work calendar tomorrow?" | Ali's *Work* calendar, tomorrow |
| "What does Sam have this week?" | Sam's calendars, today to Sunday |
| "Anything this weekend?" | everyone's, Saturday and Sunday |
| "When is the dentist?" | everyone's, a month ahead, events with "dentist" in their title |
| "What's on 20 November?" | everyone's, that day |

"My calendar": LittleBot uses the name of the person talking when it knows it (they said who they are, or
[voice](VOICES.md) or face recognition told it). Otherwise it asks whose calendar you mean. When only one person
has calendars, "my calendar" is theirs.

The same event on two people's calendars (a family dinner) is said once, as "Ali and Sam".

The dashboard's **What's on today?** button shows the same answer the AI gets.

## 3. Privacy

* **The address is a password.** Anyone with it can read that calendar. LittleBot keeps it on the robot only,
  in its database (`data/littlebot.db`, readable only by you). It never logs it, never shows it on the
  dashboard again (only "calendar.google.com, secret address") and never sends it to the AI.
* **What the AI gets**: only when someone asks, the events for the days asked about. That means each event's
  title, day, time and place, whose calendar it is on and which one. It never gets an event's description,
  notes, guests or video-call links.
* **Anyone near LittleBot can ask.** Add only calendars your family is happy to have read out in your home.
* **Remove** a calendar on the dashboard and LittleBot deletes its address. To make an old address stop
  working everywhere, go to Google Calendar's **Integrate calendar** and choose **Reset** next to the secret
  address. If you then still want that calendar on LittleBot, add the new address.
* Nothing is read in offline mode or while **Cloud communication** is off in Privacy.

## 4. Speed

LittleBot reads each calendar at most every 10 minutes, when someone asks. A voice chat starting reads them
ahead, so the first question is answered at once. When a copy is older than 10 minutes, LittleBot answers from
it straight away and reads a fresh one for next time. It waits for a new read only when its copy is more than
6 hours old. An answer waits at most 5 seconds for the calendars; a slower one is left out of that answer
(LittleBot says so) and is ready for the next question.

Calendars keep every event since they were made. LittleBot skips the old one-off events before reading, so even
a calendar with years of events reads in well under a second. In memory it keeps about three months of events
per calendar and a compressed copy of the file, not the whole calendar opened up (that could be 50 MB or more).

Change the timings in `config/local.yaml` if needed:

```yaml
calendar:
  timeout_seconds: 5     # an answer waits at most this long for the calendars
  cache_minutes: 10      # a calendar is read again after this long, when someone asks
  max_stale_hours: 6     # an older copy answers at once while a fresh one is read
```

## 5. Troubleshooting

| The dashboard says | What to do |
|---|---|
| "Paste the calendar's Secret address in iCal format" | You pasted another link, such as the calendar's web address or the "Public URL to this calendar". Copy the **Secret address in iCal format**. |
| "Google says there is no calendar at that link" | The secret address was reset, or it's the public address of a calendar that isn't public. Copy the secret address again. |
| "That link opens a web page" | Copy the address that ends in `.ics`. |
| "Cloud communication is off in Privacy" | Turn it on (Privacy page). The calendar is kept and read then. |
| "The calendar reader isn't installed" | Run `scripts/install_pi.sh` again (or `.venv/bin/python -m pip install -e .`), then restart LittleBot. |
| Not working, red badge | Press **Check**. The reason is shown under the calendar. |

Each read logs one line, with no address and no event names:

```
calendar: read a calendar in 412 ms (88 KB, 41 events in the next 92 days)
```

```bash
journalctl --user -u littlebot --since today | grep 'calendar:'
```

### "Access blocked: ... has not completed the Google verification process"

You see this when you try a **Google sign-in** (OAuth) for a Google Cloud project you made, for example to use
the Google Calendar API. LittleBot doesn't need that; the secret address above is all it needs. If you do want
to use your own Google Cloud app for something else:

1. Go to **console.cloud.google.com** and open **Google Auth Platform**, then **Audience**.
2. Under **Test users**, add the Google account you sign in with.
3. Sign in again and press **Continue** on "Google hasn't verified this app".

While the app is in *Testing*, Google ends its sign-ins after 7 days. Press **Publish app** to stop that.

## 6. For developers

`littlebot/info/calendar.py` has the store (table `calendars`), the link checks, reading and caching, and the
answers. The `get_calendar` tool is in `littlebot/chatgpt/tools.py`; it is offered only once a calendar has
been added. `littlebot/chatgpt/live.py` tells the voice to hand calendar questions over and reads the
calendars when a call starts. The dashboard API is `littlebot/dashboard/routes/calendar_routes.py`, and the
Settings panel is `littlebot/dashboard/static/js/pages/calendars_panel.js`. Tests are in
`tests/info/test_info_calendar.py`, which uses the `calendar_fakes.py` stand-ins.
