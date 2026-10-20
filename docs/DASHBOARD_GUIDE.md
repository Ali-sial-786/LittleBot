# The LittleBot parent dashboard

The dashboard is a small web page served by LittleBot itself. Use it from the robot's own
screen/computer, or from your phone on your home Wi-Fi once you've switched that on.
Nothing in it is sent to the internet.

## 1. First run: claim your dashboard

There is **no default password**. The first time LittleBot starts, it prints a one-time
**setup code** in its window/terminal and in its log file:

```
  ================================================================
   LittleBot parent dashboard - first-run setup
   Open the dashboard at http://127.0.0.1:8765/ and enter the one-time code below.

   Dashboard setup code: 8DCU-KXYF
  ================================================================
```

Find it again with:

```bash
grep -i "setup code" data/logs/littlebot.log | tail -1          # from the log file
journalctl --user -u littlebot -b | grep -i "setup code"       # when running as a service (Pi)
```

1. Open the dashboard (on the robot: <http://127.0.0.1:8765/>).
2. Type the setup code (upper/lower case and the dash don't matter).
3. Choose a parent password - at least 8 characters; a short sentence is easy to remember.

Only someone who can see the robot's console can read the code, so nobody else on your
network can claim the dashboard. A new code is printed at every start until setup is done,
and after 10 wrong tries.

**Forgot the password?** Stop LittleBot, then on the robot run
`python -m littlebot.dashboard reset-password` and start LittleBot again - it prints a new
setup code. (All devices are logged out.)

## 2. Using it from your phone (opt-in)

By default the dashboard only accepts connections from the robot itself. To use your phone:

1. Set your password first (above).
2. Turn on LAN access - either start with `python -m littlebot --lan`, or add this to
   `config/local.yaml` and restart:

   ```yaml
   dashboard:
     lan_access: true
   ```
3. LittleBot prints the address, e.g. `http://192.168.1.20:8765/` (on a Pi named
   `littlebot` usually also `http://littlebot.local:8765/`). Open it on a phone connected to
   the **same Wi-Fi** and log in. Tip: add it to your home screen.

Only do this on a home network you trust. Never forward the dashboard port on your router
or expose it to the internet. The **Settings** page shows whether LAN access is on and the
current address.

## 3. What each page does

| Page | What you can do |
|------|-----------------|
| **Home** | See what LittleBot is doing, its face, devices and system health. The big red **STOP** stops activity, video, speech, listening and movement at once (a STOP button is also always at the top of every page). Put LittleBot to sleep or wake it. When an activity asks you something ("Did your child clap?") the question and answer buttons appear right at the top. |
| **Activities** | Browse games by age group ("For your child" uses the profile's age) and category. Each card shows the age range, what your child does, tips **for grown-ups** and **safety notes**. Start / pause / stop, star favourites, and untick "Allowed" to keep an activity out of LittleBot's suggestions. |
| **Videos** | Add YouTube links to the approved list (only approved videos can ever play), rename or remove them, play / pause / skip / stop, set the volume and the minutes per session and per day. Videos are off for children under 18 months unless you explicitly authorise them (with a clear warning). "Open player on this device" shows the video player on the phone/computer you are using. |
| **Face** | Pick an expression, blink, choose where LittleBot looks by tapping/dragging in the box, play little animations or a 15-second demo. |
| **Child profile** | Optional nickname, birth month (month and year only) or age in months, language, favourite animals, songs and interests. LittleBot uses the age to choose suitable games; without an age it uses its gentlest settings. "Forget profile" clears it. |
| **AI** | Cloud conversation is **off by default** and LittleBot works fully offline with friendly scripted replies. Cloud AI needs a separate, paid API key (Anthropic or OpenAI - a Claude.ai or ChatGPT subscription does not include one) in the robot's `.env` file; the page only shows whether a key is configured, never the key. Children under 2 get scripted replies only. Choose the provider, reply length, request and budget limits, whether the nickname / interests may be shared, test the connection, see today's usage, and try **Push to talk**. |
| **Privacy** | Switch the camera, microphone and cloud communication on or off, and optional activity history. LittleBot never stores audio or video; camera images are analysed on the robot and discarded. Clear stored history, or stop all interactions. |
| **Motion** | A live drawing of LittleBot's head and arms, gestures, joint sliders, the **EMERGENCY STOP** (and its reset). Real servos stay in **simulation only** until they are enabled in the configuration *and* you arm them here by typing `ARM MOTORS` - see [MOTION_SAFETY.md](MOTION_SAFETY.md). |
| **Audio & devices** | Speaker, microphone and camera status; volume (capped for little ears) and speaking speed; test the speaker and microphone; make LittleBot say a sentence or play a sound effect; follow faces on/off; list audio devices. |
| **Simulator** | Try LittleBot without a child or devices: type what a child might say, tap the camera box to place a pretend face, "person left", and answer caregiver questions. |
| **Settings** | Change the password, log out (here or everywhere), see how to reach the dashboard, system details, recent events, the configuration (secrets are never shown), restart or shut down. |

If LittleBot hears something worrying (for example "I'm hurt"), it answers with "Let's find
a grown-up!" and a red **"Your child may need you"** notice appears on every page until you
tap OK. Only a category is shown - never the child's words.

## 4. Security notes

* Your password is stored only as a salted scrypt hash on the robot (`data/littlebot.db`).
* Sessions last 12 hours (`dashboard.session_hours`); log out everywhere from Settings,
  and changing the password logs out all other devices.
* Login attempts are limited (5 per minute per device by default).
* The dashboard uses plain `http` on your home network. Anyone who can watch your Wi-Fi
  traffic could see it, which is why LAN access is opt-in and meant for trusted home
  networks only.
* The page loads nothing from the internet except small thumbnails of the videos you
  approved (they simply don't show when offline).
* API keys stay in `.env` on the robot and are never sent to the browser.
