# Calendar, heads-ups and your phone

LittleBot can read and add to your calendar, speak up by itself when something is coming, and send those heads-ups
and your reminders to your phone.

> **Status.** Tested against a fake Google Calendar, a fake iCal feed and a fake ntfy server. **Not yet tried with a
> real account.** The rain and snow heads-ups need no setup, so they work as soon as the robot is updated.

## What you can say

| Say | What happens |
|---|---|
| "What's on today?", "What's on Friday?", "What's my week look like?" | it says the events and shows them on the screen |
| "When is the dentist?" | it looks up to 6 months ahead |
| "Am I free Saturday afternoon?" | it checks that day |
| "What's next?" | answered at once: today's and tomorrow's events go with every question |
| "Add the dentist on Tuesday at 3", "Put Zayn's birthday on the 12th" | added to Google Calendar (all day when there's no time) |
| "Move it to 4", "Rename it to Dentist for Amara" | changed; a repeating event is changed only that once |
| "Cancel the dentist" | it asks "Cancel 'Dentist' on Tuesday at 3 pm?" first |
| "Good morning", "What's my day?" | the briefing now includes today's events (tomorrow's at night) |
| "Stop telling me about the weather", "Tell me an hour before my meetings", "Don't speak up after 9" | changes the heads-ups |
| "Send the shopping list to my phone" | a notification on your phone |

Double-tap the face for **today at a glance**: the next events are listed there too.

## 1. Connect your calendar

There are two ways. Do either one, or both.

### Option A: Google Calendar (see **and add** events)

Google has no ready-made "LittleBot" app, so you make one for yourself in your own Google Cloud project. It's free
and takes about 10 minutes, once.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in with the Google account that has
   the calendar. Make a new project (any name, e.g. "LittleBot").
2. **APIs & Services → Library**: search for **Google Calendar API** and press **Enable**.
3. **APIs & Services → OAuth consent screen** (it may be called "Google Auth Platform"):
   * User type **External**, app name "LittleBot", your email as the support and developer contact.
   * Under **Audience** (or "Test users"), add your own email.
   * Press **Publish app** ("In production"). Without this, Google ends the sign-in after 7 days. Google doesn't
     need to review an app that only you use.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**, application type **Desktop app**,
   any name. Copy the **Client ID** and the **Client secret**.
5. Run this from your PC (PowerShell):

   ```
   ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/setup_calendar.py"
   ```

   It asks for the Client ID and the secret (the secret is hidden as you paste it). Then it prints a Google
   address:
   * open it on your phone or PC and sign in;
   * Google says it **hasn't verified the app**: press **Advanced**, then **Go to LittleBot**;
   * tick both calendar boxes and press **Continue**;
   * the page then **fails to load** (an address starting `http://127.0.0.1:8889/`). That is expected: copy the
     **whole address** from the address bar and paste it into the script.

   The script then lists your calendars and how many events it sees this week.

LittleBot reads every calendar that is ticked in Google Calendar's own list (yours, the family's, holidays) and
adds new events to your main one. To add to another one by default, set `skills.calendar_default` to its name
(you can also say "add it to the Family calendar").

### Option B: a secret iCal address (see only; works with Google, Outlook and iCloud)

Quicker, but LittleBot can't add or change events this way.

* **Google Calendar**: on a computer, Settings → click the calendar on the left → **Secret address in iCal format**.
* **Outlook.com**: Settings → Calendar → Shared calendars → **Publish a calendar** → the **ICS** link.
* **iCloud**: share the calendar as a **Public calendar** and copy the link.

```
ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/setup_calendar.py --ics"
```

It is hidden as you paste it, checked, then saved. Run it again to add another calendar. Anyone with this address
can read the calendar, so it's kept like a key and never logged.

### Check, or undo

```
ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/setup_calendar.py --check"
ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/setup_calendar.py --disconnect"
ssh alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/setup_calendar.py --remove-ics"
```

LittleBot picks up a new sign-in within 5 minutes, or at once with `systemctl --user restart littlebot`.

## 2. Heads-ups (LittleBot speaks up by itself)

| Heads-up | When | Example |
|---|---|---|
| Calendar | 30 minutes before an event with a time (not all-day ones, and not one you just added by voice) | "Heads up: Dentist at 3 pm, in 30 minutes, at Main St Clinic." |
| Weather | rain, snow, freezing rain or storms likely (60% or more) to start within 3 hours while it's dry now; between 7 am and 8 pm; at most once every 6 hours | "Heads up: rain is likely around 3 pm. Take an umbrella if you go out." |
| Reminders | a reminder that rings is also sent to your phone | (the robot already says it) |

A soft chime, the sentence, and a card on the screen. During a conversation it's said in the conversation. A
weather heads-up waits until a video ends or a conversation is over (up to 30 minutes; your phone gets it at once).

Nothing is said out loud during **quiet hours** (9:30 pm to 7 am) or while LittleBot is asleep; your phone still
gets it.

Change them by voice ("stop telling me about the weather", "tell me 15 minutes before", "quiet hours from 10 pm to
6:30", "stop the heads-ups"), or set the defaults in `config/local.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `skills.heads_up` | `true` | heads-ups at all |
| `skills.heads_up_calendar_minutes` | `30` | minutes before a calendar event (0: none) |
| `skills.heads_up_weather` | `true` | rain, snow and storm heads-ups |
| `skills.heads_up_quiet_hours` | `"21:30-07:00"` | nothing out loud then (`""`: never quiet) |

What you change by voice is kept (settings `heads_up.*`) and wins over these.

## 3. Your phone

Heads-ups and reminders can also pop up on your phone, through **ntfy**: a free notification app with no account.

1. Install **ntfy** on your phone (Google Play or the App Store).
2. Run this from your PC:

   ```
   ssh -t alisial3@littlebot.local "cd ~/LittleBot && .venv/bin/python scripts/setup_phone.py"
   ```

   It makes a long random **topic** name and shows it.
3. In the ntfy app tap **+**, type that topic, and subscribe. Press Enter in the script: a test notification
   arrives.
4. Restart LittleBot: `ssh alisial3@littlebot.local "systemctl --user restart littlebot"`.

Do the same on each family phone (the script shows the same topic again). Anyone who knows the topic can read the
notifications, so keep it in the family; `--new` makes a new one. Messages go through ntfy.sh's servers; set
`skills.ntfy_server` to use your own ntfy server instead. "Stop sending to my phone" by voice, or `--remove`.

## Privacy

* Events are kept **in memory only** (the next 8 days, fetched every 5 minutes) and never written to disk.
* Today's and tomorrow's events (title, time, place) go to the brain with each question, like reminders do.
* The Google sign-in is in `data/google_calendar.json` and the iCal address in `data/calendar_ics_url`, both
  readable only by you. The ntfy topic is in `data/ntfy_topic`. None of them is ever logged.
* Nothing is fetched or sent in offline mode or while cloud communication is off in Privacy.

## Troubleshooting

| Log line (`grep -a calendar ~/LittleBot/data/logs/littlebot.log`) | Meaning |
|---|---|
| `calendar: Google refused the sign-in when refreshing` | the sign-in ended (password changed, access removed, or the app was still in "Testing" after 7 days): publish the app (step 3) and run `setup_calendar.py` again |
| `calendar: iCal feed 1 answered HTTP 404` | the secret address was reset: copy the new one and run `--remove-ics`, then `--ics` |
| `heads-up: calendar (phone)` | a heads-up went only to the phone (quiet hours, or the speaker is off for heads-ups) |
| `push: the notification didn't go (http_429)` | ntfy.sh is limiting this robot; try again later |

Code: `littlebot/skills/agenda.py` (the skill), `google_calendar.py`, `ics.py`, `heads_up.py`, `push.py`,
`littlebot/screens/cards/calendar.py`. Tests: `tests/skills/test_agenda.py`, `test_google_calendar.py`,
`test_ics.py`, `test_heads_up.py`, `test_calendar_card.py`.
