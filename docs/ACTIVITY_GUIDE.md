# LittleBot activity guide

LittleBot's activities are short games, songs and stories that **you and your child
play together**. LittleBot sets the scene - a funny face, a sound, a question, a tune -
and the real play happens between you, your child, and real things in the room: a
cloth for peekaboo, a ball for "first words", socks to match, a teddy to help.

Part 1 is for parents and caregivers. Part 2 explains how to write your own activity.

---

## Part 1 - Playing with LittleBot

### The idea in one minute

* **You are the most important part.** Babies and toddlers learn through warm,
  back-and-forth interaction with the people who love them. LittleBot is a playful prop
  and a source of ideas, not a teacher or a babysitter. Every activity assumes a grown-up
  is right there.
* **Short and often beats long.** Most activities take 2-4 minutes. Two or three in a row
  is plenty for a baby. Stop while it's still fun.
* **Real world over screen.** LittleBot's face is only eyes; the "content" is your voice,
  your hands, toys, and moving bodies.
* **No scores, no tests, no judging.** LittleBot never says "wrong", never counts right
  answers and never rates your child. Every try gets warm encouragement.

### Starting, pausing and stopping

Open the parent dashboard, go to **Activities**, pick one and press **Start**.

* **Pause / Resume** - LittleBot stops talking straight away and waits. Timers (for
  example "wait 30 seconds for an answer") stop counting while paused. If a sentence was
  cut off, LittleBot says it again after you resume.
* **Stop** - ends the activity immediately; LittleBot opens its eyes and returns to a
  calm face.
* **The big STOP button** stops everything (activity, sound, motion) at once.
* Starting a different activity simply replaces the current one.
* Something more important (for example a video you started) also politely ends the
  activity.

### Answering LittleBot's questions ("caregiver prompts")

Many activities ask *you* something on the dashboard - "Did she clap (or try)?",
"Hide the toy under the cloth, then tap Hidden", "Where should Pip go?". Tap a button:

* LittleBot reacts to your answer (a celebration, a hint, a story branch).
* **Skip** is always allowed - LittleBot just carries on.
* If nobody answers, LittleBot waits a while (usually 20-60 seconds) and then carries on
  gently by itself. Nothing bad happens.
* Some activities can also notice things themselves - a wave (if camera gesture
  detection is switched on) or a spoken word (if the microphone is on). Your button
  always works too.

### Ages are flexible suggestions - never a test

Every activity shows an age range, such as "6–24 months" or "3–6 years". These are
**loose suggestions to help you find something fun**, based on what children of that age
often enjoy. They are not milestones, and they say nothing about how your child is
developing.

* Children develop at very different paces, and every child has their own favourites.
* Younger children often love "older" activities with your help, and older children
  happily return to baby favourites.
* If your child isn't interested today, that's information about today - not about them.
* LittleBot never measures, scores or tracks progress. If you ever have questions about
  your child's development, talk to your health visitor, nurse or doctor - not a robot.

The dashboard lists activities that fit your child's age first (from the birth month in
the profile), but you can always browse and start anything.

### Making it personal

In the dashboard profile you can add (all optional):

* **Nickname** - LittleBot says "Hi Mia!" instead of "Hi friend!", and Pat-a-Cake marks
  the cake with her initial.
* **Favourite animals** - they appear first in Animal Sounds, Who Says That? and Old
  MacDonald.
* **Per-activity preferences** - mark favourites, or switch off activities you don't
  want suggested. Switched-off activities disappear from the suggestions but you can
  still start them from the full list.

### LittleBot's voice in activities (the ChatGPT voice)

Out of the box, activities speak with the robot's built-in voice (espeak), which sounds
robotic. If you signed in with ChatGPT ([CHATGPT.md](CHATGPT.md)), LittleBot can say its
activity lines in the same warm voice as in a voice chat ("bubbie"):

1. **Activities** page → **Activity voices** card → **Record activity voices in the ChatGPT
   voice**. The card shows how many lines are recorded, how long the rest takes, and a
   progress bar while it records.
2. That's it. Each line is used as soon as it is recorded - no restart. LittleBot can keep
   playing meanwhile: the recording doesn't use its speaker or microphone.

* **What gets recorded:** every fixed line the activities say (with your child's nickname
  and favourites from the profile filled in, and one line per animal, colour or shape of the
  lists they use), the screen games' animal noises, and the scripted chat replies and
  greetings. About 1,000 lines in all, roughly **1½ hours** the first time; the activities
  for your child's age come first (about 350 lines, half an hour for a baby). Stop any time
  and press the button again later to carry on.
* **Your child's name** is only sent to ChatGPT with your OK: tick *Also record the lines
  with …'s name* on the card (it is ticked already if you share the name with the AI on the
  AI page). Without it, the handful of lines with the name keep the robot voice.
* **Still robotic?** Lines that can't be known in advance keep the robot voice: what your
  child just said ("You said *ball*!"), two or three animals picked at once, and anything the
  AI makes up. A line the voice didn't say right (LittleBot checks what it said) is listed on
  the card and tried again next time you press the button. After you change the nickname or
  favourites, or add your own activity, press it again for the new lines.
* **Command line** (the same thing, for the terminal): `.venv/bin/python
  scripts/record_voice_lines.py --dry-run` lists the lines and the time it takes;
  `--missing` records what isn't recorded yet; `--only peekaboo` just one activity;
  `--with-name` includes the lines with the name. See [CHATGPT.md](CHATGPT.md#activities-in-the-chatgpt-voice).
* The recordings live in `data/voice_lines/` (updates never touch them). Delete that folder
  to go back to the robot voice everywhere.

### Tips for caregiver-led play

* **Follow your child's lead.** If she wants to bang the "shaker" instead of copying the
  wave, that's play too. Pause LittleBot and enjoy the moment.
* **Get face to face.** Sit so your child can see both you and LittleBot. Babies watch
  your face to understand what's happening.
* **Be the echo.** Say the words LittleBot says - "Moo!", "Ball!", "Peekaboo!" - your
  voice matters most.
* **Wait.** Count to five silently after a question. Little ones need time to respond,
  and a look, a kick or a babble is a response.
* **Repeat favourites.** Repetition is how young children learn and feel safe. Playing
  Peekaboo for the tenth time is a feature, not a problem.
* **Watch for "I'm done" signals** - looking away, arching, fussing, yawning. End with a
  goodbye (try *Bye-Bye Wave*) and move to something calm.
* **Narrate real life.** The best follow-up to *Colour Hunt* is "Look, a red bus!" on
  your next walk.
* **Mind the total screen time.** LittleBot's face is a screen, even if a tiny one. Use
  activities as a springboard for play away from the robot.

### Safety

Each activity lists its own safety notes on the dashboard. The general rules:

* **Always supervise.** LittleBot is not a babysitter or a baby monitor.
* **Small objects:** anything you use should be too big to fit in your child's mouth.
* **Cloths and blankets** (peekaboo, hide the toy): hide *your* face, not your baby's;
  put cloths away afterwards.
* **Movement games:** clear space away from corners, stairs and hard edges; hold hands
  for hopping with young toddlers; stop if anyone gets dizzy or tired.
* **Volume:** keep it gentle - LittleBot has a maximum volume, and you can turn it down
  further in the dashboard.
* **Motors:** LittleBot's arm and head movements are optional, simulated by default, and
  stop instantly with the STOP button.

### Privacy

* The microphone is used only during "listen" moments (for example "Can you say moo?")
  and only if it's switched on. What LittleBot hears is never recorded, logged or stored.
* The camera is used only to notice a wave, if gesture detection is switched on. Nothing
  is recorded.
* **Activity history is off by default.** If you switch on *Record history* in the
  privacy settings, LittleBot keeps only which activity ran, when, and whether it was
  finished or stopped - never scores, never recordings. You can clear it at any time.

### What's included

43 activities across all ages (the same list appears in the dashboard). Ages show where
each activity usually starts to click; the upper bound follows the same "up to, but not
including" convention as the dashboard's age groups.

#### Babies (about 6-18 months)

| Activity | Ages | Type | Grown-up | About |
|---|---|---|---|---|
| Again! Again! (`again_again`) | 6–18 months | cognitive | required | Each tap on "Again!" makes LittleBot do the same funny boing - babies love knowing what comes next. |
| Bye-Bye Wave (`bye_bye`) | 6–24 months | social | recommended | A cosy goodbye routine - wave bye-bye, blow a kiss, and see you soon. |
| Musical Turns (`music_turns`) | 6–24 months | music | recommended | LittleBot plays a little tune, then waits for your baby's turn to make music. |
| Peekaboo (`peekaboo`) | 6–24 months | social | recommended | LittleBot hides its eyes and pops back with a happy "Peekaboo!" - then it's your turn to hide. |
| Waving Hello (`wave_hello`) | 6–24 months | social | recommended | LittleBot waves hello and waits for a wave back - seen by the camera or confirmed by you. |
| Where Did It Go? (`hide_the_toy`) | 6–24 months | cognitive | required | A gentle hide-and-find game with a toy and a cloth. You hide, LittleBot adds the suspense. |
| Touch and Feel (`touch_and_feel`) | 6 months – 2½ years | sensory | required | Explore soft, smooth, crinkly and bumpy things together while LittleBot describes each one. |
| Pat-a-Cake (`song_pat_a_cake`) | 6 months – 3 years | music | recommended | The clapping rhyme where you bake a pretend cake - and mark it with a letter. |
| Baa, Baa, Black Sheep (`song_baa_baa_black_sheep`) | 6 months – 5 years | music | recommended | A short, sweet sheep song with the same tune as Twinkle, Twinkle. |
| Row, Row, Row Your Boat (`song_row_your_boat`) | 6 months – 6 years | music | recommended | A rocking, rowing song - perfect for sitting face to face and rowing back and forth. |
| Twinkle, Twinkle, Little Star (`song_twinkle_twinkle`) | 6 months – 6 years | music | recommended | The gentle classic. LittleBot sings it line by line, then plays the tune for you to sing. |
| Clap Along (`clapping_game`) | 8–24 months | movement | recommended | LittleBot claps, pauses, and cheers for every clap (or try) from your little one. |
| Animal Sounds (`animal_sounds`) | 8 months – 3 years | language | recommended | Meet friendly animals and their sounds - "The cow says moo!" Favourite animals come first. |
| First Words (`first_words`) | 9–24 months | language | required | Slow, repeated everyday words - mama, dada, ball, dog - while you point to the real thing. |
| Copy Me! (`copy_me`) | 9 months – 2½ years | social | recommended | Simple actions to copy - clap, wave, big smile, "ooh", and arms up "so big!" |
| Itsy Bitsy Spider (`song_itsy_bitsy_spider`) | 9 months – 6 years | music | recommended | Up the water spout, down came the rain, out came the sun - with finger actions. |
| Old MacDonald Had a Farm (`song_old_macdonald`) | 9 months – 6 years | music | recommended | Visit the farm, verse by verse - favourite animals join in first. |
| The Wheels on the Bus (`song_wheels_on_the_bus`) | 9 months – 6 years | music | recommended | Round and round, swish swish swish, beep beep beep - a song full of hand actions. |

#### Toddlers (about 12 months - 3 years)

| Activity | Ages | Type | Grown-up | About |
|---|---|---|---|---|
| Goodnight, Farm (`story_goodnight_farm`) | 12 months – 4 years | story | recommended | A soft, slow wind-down story that says goodnight to the farm animals, ending with a lullaby tune. |
| Where's Your Nose? (`body_parts`) | 12 months – 4 years | language | recommended | A finding game with your own body - nose, toes, tummy - plus a silly action for each. |
| Dance Party (`dance_party`) | 12 months – 5 years | movement | recommended | Bouncy music, simple dance moves and lots of wiggles - for little dancers of every size. |
| If You're Happy and You Know It (`song_if_youre_happy`) | 12 months – 6 years | music | recommended | Clap your hands, stomp your feet and shout hooray - an action song for little bodies. |
| One, Two, Three! (`count_to_three`) | 15 months – 3 years | numeracy | recommended | Counting to three with claps, toes and kisses - short, bouncy and repeated. |
| Head, Shoulders, Knees and Toes (`song_head_shoulders`) | 15 months – 6 years | music | recommended | Touch each body part as you sing - then try it a little faster! |
| Colour Hunt (`colour_hunt`) | 18 months – 4 years | cognitive | required | LittleBot names a colour and you hunt for something that colour together. |
| Who Says That? (`who_says_that`) | 18 months – 4 years | language | recommended | Listen to a sound and guess the animal - "Who says moo?" Then LittleBot reveals it. |
| Feelings Faces (`feelings_faces`) | 18 months – 5 years | emotional | recommended | LittleBot shows happy, surprised, sleepy, sad and excited faces and names each feeling. |
| Move Like the Animals (`animal_moves`) | 18 months – 5 years | movement | recommended | Stomp like an elephant, hop like a bunny, flap like a bird - big movements with fun sounds. |
| The Alphabet Song (`song_abc`) | 18 months – 6 years | literacy | optional | The ABC song, line by line, then once more with the tune for you to sing - early letter exposure. |
| Find the Pairs (`matching_pairs`) | 2–5 years | cognitive | required | Mix up a few pairs of everyday things - socks, cups, blocks - and find the ones that match. |
| Help Teddy! (`teddy_problem`) | 2–5 years | cognitive | required | Teddy has little problems - a ball out of reach, a wobbly tower, a chilly evening. What could we do? |
| Shape Spotting (`shape_spotting`) | 2–5 years | cognitive | recommended | Learn a shape, draw it in the air, then spot it around the room. |
| Tea Party (`tea_party`) | 2–5 years | creativity | required | Pour pretend tea, blow on it, share cake with Teddy - a cosy pretend party. |
| Freeze Dance (`freeze_dance`) | 2–6 years | movement | recommended | Dance while the music plays - when it stops, freeze like a statue! |
| Pip's Big Day (`story_pip_the_puppy`) | 2–6 years | story | recommended | An interactive story about a little puppy - your child chooses where Pip goes and who Pip meets. |

#### Preschool (about 2½-5 years)

| Activity | Ages | Type | Grown-up | About |
|---|---|---|---|---|
| Big and Small (`opposites`) | 2½–6 years | language | recommended | A word game with opposites - big and small, fast and slow, loud and quiet - acted out with your body. |
| Question Time (`question_time`) | 2½–6 years | language | recommended | Simple, friendly questions to talk about - every answer is welcome, and LittleBot shares a fun fact. |
| Smell the Flower, Blow the Candle (`calm_breathing`) | 2½–6 years | emotional | recommended | A short calming game - breathe in to smell a flower, breathe out to blow out a candle. |
| Zoo Keeper (`zoo_keeper`) | 2½–6 years | creativity | recommended | Be the zoo keeper! Guess who's making each sound, learn a fact, and feed the hungry animals. |
| Count to Ten and Blast Off! (`count_to_ten`) | 3–6 years | numeracy | recommended | Count fingers to ten, do five big jumps, then a rocket countdown - 5, 4, 3, 2, 1, blast off! |
| I Spy Clues (`i_spy`) | 3–6 years | language | required | LittleBot gives a simple clue - "something that keeps your feet warm" - and you find it together. |
| Letter of the Day (`letter_sounds`) | 3–6 years | literacy | recommended | Meet a letter, hear its sound, draw it in the air and think of words that start with it. |
| Remember With Me (`memory_game`) | 3–6 years | cognitive | recommended | LittleBot says two or three animals; can your child say them back? Hear them again as often as you like. |

All songs are traditional nursery rhymes in the public domain; the melodies are simple
arrangements for LittleBot's little synthesiser. The stories are original.

---

## Part 2 - Writing your own activity

Activities are plain YAML text files. You don't need to know Python.

### Where files go

* Built-in activities: `assets/activities/*.yaml` (part of LittleBot's code).
* **Your own activities: `data/activities/*.yaml`** (create the folder). This folder is
  listed in `activities.custom_dirs` in the config, survives updates, and is loaded at
  start-up. Use a different id from the built-in activities.
* Files whose name starts with `_` hold shared word lists (see `_data.yaml`).

**Check a file before using it:**

```bash
.venv/bin/python -m littlebot.activities data/activities/my_game.yaml   # one file
.venv/bin/python -m littlebot.activities -v                            # everything
```

Problems are reported with the file, activity and exact step, for example
`my_game.yaml: activity 'bath_time': steps[3] (say).expression: unknown expression 'grumpy'; choose one of: neutral, happy, ...`.
A broken file is skipped (and logged) - it never stops LittleBot from starting.

### A complete example

```yaml
id: bath_time_ducks                 # lowercase letters, digits and _
name: Bath Time Ducks
description: Count rubber ducks and quack together before bath time.
min_age_months: 12                  # flexible suggestion, never a test
max_age_months: 36                  # "up to, but not including"
category: numeracy                  # see the list below
participation: recommended          # required | recommended | optional
duration_seconds: 120               # rough estimate for the dashboard
materials: [two or three rubber ducks]
parent_instructions: >
  Line up the ducks. Count them together, touching each one, and quack!
safety_notes:
  - Never leave a child alone near water, even for a moment.
steps:
  - say: "Hi {child_name}! Let's count the ducks!"
    expression: happy
    animation: bounce
  - repeat: 3
    steps:
      - say: "{round}!"
      - sound: pop
        pause: 0.8                  # every step can wait afterwards
  - prompt: "Did you count three ducks together?"
    options: ["Yes!", "Count again"]
    branches:
      "Count again":
        - say: "One, two, three ducks!"
  - say: "Quack, quack! Bath time!"
    expression: celebrating
    animation: confetti
```

A file can hold one activity (like above) or several under `activities:` (a list).

### Metadata fields

| Field | Required | Notes |
|---|---|---|
| `id` | yes | unique, `a-z 0-9 _`, 2-64 characters |
| `name` | yes | up to 60 characters |
| `description` | yes | one short sentence for the dashboard |
| `min_age_months`, `max_age_months` | yes | suggested range; `min <= age < max` |
| `category` | yes | `social`, `music`, `language`, `movement`, `cognitive`, `sensory`, `emotional`, `numeracy`, `literacy`, `creativity`, `story` |
| `participation` | no (`recommended`) | how much a grown-up needs to join in |
| `parent_instructions` | yes | what the grown-up does; be concrete and warm |
| `safety_notes` | yes (at least one) | supervision, small parts, space, volume ... |
| `duration_seconds` | yes | estimate (10-3600) |
| `pausable` | no (`true`) | set `false` only if pausing makes no sense |
| `finish_expression` | no (`happy`) | face shown at the end (e.g. `sleepy` for bedtime) |
| `tags`, `materials` | no | lists of short words / things to prepare |
| `variables` | no | starting values for your own placeholders |
| `data` | no | named lists for `pick` / `for_each` (overrides `_data.yaml`) |
| `steps` | yes | what happens, in order (below) |

### Steps

Each step is a YAML item whose **first key names the step type**. Extra keys on the same
item are options. Steps run one after another; the activity ends after the last step (or
at an `end` step).

| Step | Example | What it does |
|---|---|---|
| `say` | `say: "Hello!"` / `say: ["Hi!", "Hello!"]` | Speak (a list = pick one at random). Options: `expression`, `animation` (shown first), `rate` (0.5-1.5, slower < 1). |
| `expression` | `expression: surprised` + `hold: 2` | Change the face. `hold` = seconds before returning to normal. |
| `animation` | `animation: confetti` | One-shot face animation. |
| `sound` | `sound: peekaboo` | Sound effect. Options: `wait` (default true), `volume` (0-1). |
| `melody` | `melody: "C4 E4 G4:2 R:0.5"` + `tempo: 110` | Play notes. `G4:2` = two beats, `R` = rest. Also `[[C4, 1], [G4, 2]]`. |
| `song` | see below | A song line by line, words shown on the dashboard. |
| `gesture` | `gesture: wave` | Optional body movement (skipped without motors or during an emergency stop). `wait: true` to wait for it. |
| `look` | `look: left` / `look: [0.3, 0.6]` | Gaze: `left`, `right`, `up`, `down`, `center`, ... or x, y from 0 to 1. Option `hold`. |
| `close_eyes` / `open_eyes` / `blink` / `clear_gaze` | `- close_eyes` | Eyes. `blink: true` = double blink. |
| `wait` | `wait: 2` | Pause (seconds). Gives the child time to respond. |
| `note` | `note: "Hide the toy now"` | A tip shown to the grown-up on the dashboard (not spoken). |
| `prompt` | see below | Ask the grown-up; branch on the answer. |
| `wait_for` | see below | Wait for something LittleBot can notice (a wave, a person). |
| `listen` | see below | Listen for one spoken answer (microphone). |
| `repeat` | `repeat: 3` + `steps: [...]` | Repeat steps. `{round}` = 1, 2, 3 ... |
| `for_each` | `for_each: n` + `in: [5, 4, 3]` + `steps: [...]` | Run steps once per item, in order. Options `shuffle`, `limit`. |
| `choice` | `choice: [ {say: a}, {steps: [...]} ]` | Run ONE option at random (no repeats until all were used; `avoid_repeats: false` to allow). Optional `weights`. |
| `pick` | `pick: animal` + `from: animals` | Pick a random item into a variable (see placeholders). Options: `prefer_profile: favourite_animals`, `count: 2` (a list), `avoid_repeats`. |
| `set` | `set: {colour: red}` | Set variables (text may use placeholders). |
| `end` | `- end` / `end: all done` | Finish now (counts as completed). |

**Any step** can have `pause: <seconds>` to wait after it - ideal after a question:
`say: "Can you clap?"` + `pause: 3`.

#### Placeholders

Write `{name}` inside any text:

| Placeholder | Value | When empty |
|---|---|---|
| `{child_name}` | profile nickname | `friend` |
| `{child_initial}` | first letter of the nickname | `B` |
| `{favourite_animal}` | first favourite animal | `puppy` |
| `{favourite_song}` | first favourite song | `Twinkle, Twinkle` |
| `{robot_name}` | the robot's name | - |
| `{round}` | current `repeat` / `for_each` round | `1` |
| `{x}` / `{x.field}` | your variables from `set`, `pick`, `for_each`, `listen.store_as`, `variables` | empty |

Give your own fallback with `|`: `"for {child_name|baby} and me"` says "for baby and me"
when no nickname is set. Lists read naturally ("cow, dog and cat"). A placeholder value
at the start of a sentence gets a capital letter. Write `{{` / `}}` for literal braces.

Placeholders are checked when the file loads: a typo like `{chlid_name}` or
`{animal.colour}` (when animals have no `colour`) is reported.

#### Data lists, `pick` and `for_each`

```yaml
data:
  fruits:
    - {name: apple, colour: red}
    - {name: banana, colour: yellow}
steps:
  - pick: fruit
    from: fruits                 # or an inline list: [apple, banana]
  - say: "A {fruit.colour} {fruit.name}!"
  - for_each: f
    in: fruits
    steps:
      - say: "{round}. {f.name}"
```

Shared lists in `assets/activities/_data.yaml`: `animals` (name, sound, aliases),
`farm_animals`, `colours` (name, things), `shapes` (name, hint), `body_parts` (name,
question, action), `letters` (name, sound, words), `opposites` (a, b, a_line, b_line,
a_act, b_act), `zoo_animals` (name, sound, food, fact). `prefer_profile:
favourite_animals` picks the child's favourites first (matching `name` or `aliases`).

#### Caregiver prompts

```yaml
- prompt: "Did {child_name|your little one} clap (or try)?"
  options: ["Clapped!", "Not this time"]     # 1-6 short button labels (default: [Done])
  timeout: 20                                # seconds; default: activities.caregiver_timeout_seconds
  branches:                                  # steps per option (optional)
    "Clapped!":
      - sound: yay
      - say: "You clapped! Hooray!"
  on_timeout:                                # nobody answered (optional)
    - say: "Clap, clap! Good watching!"
```

Answers are matched ignoring case and punctuation. "skip" is always accepted (carry on
with no branch); "done", "ok", "yes", "next" mean the first option; "1", "2", ... pick an
option by number. The timeout does not run while the activity is paused. Prompts publish
`activity.prompt` and are answered via the dashboard (`respond()`) or a
`caregiver.response` event.

#### Waiting for something to happen: `wait_for`

```yaml
- wait_for: vision.gesture          # a bus event pattern (or a list of them)
  match: {gesture: wave}            # payload must match (text is case-insensitive)
  timeout: 20
  ask_caregiver: "Tap when she waves (or tries!)"   # optional: a button that also counts
  caregiver_option: "Waved!"
  on_event: [{say: "You waved!"}]
  on_timeout: [{say: "I'm waving to you!"}]
```

Useful events: `vision.gesture` (needs camera + gesture detection on),
`vision.person_present`, `vision.person_left`. `contains: {text: [moo]}` matches words
inside a text field. If the camera is off and there is no `ask_caregiver`, the step does
not wait at all - always add a caregiver button so the step works everywhere.

#### Listening: `listen`

```yaml
- listen: 5                         # seconds
  expect: ["{animal.name}"]         # optional words (placeholders allowed)
  on_match: [{say: "Yes! The {animal.name}!"}]     # heard an expected word
  on_heard: [{say: "Good thinking!"}]              # heard something else
  on_timeout: [{say: "It's the {animal.name}!"}]   # silence, or the microphone is off
```

When the microphone is off, `listen` simply behaves like silence. What LittleBot hears is
never logged or stored (`store_as: var` keeps it in memory for this run only). Keep
responses warm whatever was heard - toddlers' words are hard to recognise.

#### Songs

```yaml
- song: "Twinkle, Twinkle, Little Star"
  tempo: 96
  style: speak_then_play        # speak_then_play | play_then_speak | play_only | speak_only
  lines: &twinkle               # an anchor, to reuse the lines later
    - {words: "Twinkle, twinkle, little star,", notes: "C4 C4 G4 G4 A4 A4 G4:2"}
    - {words: "How I wonder what you are.", notes: "F4 F4 E4 E4 D4 D4 C4:2"}
- say: "Now you sing, and I'll play the tune!"
- song: "Twinkle, Twinkle, Little Star"
  style: play_only              # words appear on the dashboard for the grown-up to sing
  lines: *twinkle
```

Only use songs that are traditional / public domain (or that you wrote yourself).

#### Your lines in the ChatGPT voice

Your own activities' `say` lines and sung words are recorded like the built-in ones (press
**Record activity voices** again, or `scripts/record_voice_lines.py --missing --only my_game`).
The recorder works the lines out from the script: placeholders are filled with the profile
values and with **every item** of a `pick` / `for_each` list (`{animal.sound}` = one line per
animal), `set` values and `{round}` numbers. A line stays in the robot voice when its words
can't be known in advance: `listen: store_as` text, `pick` with `count` above 1, or more than 40
combinations of values in one sentence. Prompts, notes and `ask_caregiver` texts are not spoken,
so they are not recorded. A recording is matched to what LittleBot says ignoring case,
punctuation and spacing; change a word and that line is the robot voice again until recorded.

### Writing good activities for little ones

* **Short phrases.** 3-10 words. One idea per sentence.
* **Repeat.** Same words, same rhythm, every round. Predictability is the fun.
* **Pause for the child.** Add `pause: 2`-`4` after every invitation ("Can you wave?").
* **Praise effort, not results.** "You tried! Great clapping!" - never "wrong", "no" or
  "try harder". If something didn't happen, carry on warmly ("That's okay! Let's clap
  together").
* **No scores, no tests.** Don't count right answers, compare children or describe
  ages as "should". Offer hints and "hear it again", never a verdict.
* **Put the grown-up to work.** Ask them to show real objects, hide the toy, sing along.
  Use `note` for tips and `prompt` for the moments that need them.
* **Always offer a way forward.** Give every `prompt` and `wait_for` a sensible
  `on_timeout` (or let it carry on), so the activity flows even if nobody taps.
* **Keep it gentle.** Calm expressions and soft sounds for babies; save `confetti` for
  celebrations; use `sleepy` and slow `rate` to wind down.
* **Write real safety notes.** Small parts, cloths, water, space to move, volume.
* **Keep it short.** 2-4 minutes is plenty.

### Vocabulary

* **Expressions:** neutral, happy, excited, curious, surprised, sleepy, sad, thinking,
  listening, speaking, confused, playful, affectionate, celebrating
* **Animations:** bounce, wiggle, nod, shake, spin_eyes, sparkle, hearts, zzz, confetti, peek
* **Sounds:** chime, pop, boop, boing, peekaboo, tada, clap, whoosh, sparkle, drum, bell,
  giggle, uhoh, yay, blip, notes, sleep, wake, click, success (or a `.wav` file under
  `assets/sounds/`)
* **Gestures:** wave, look_left, look_right, raise_arms, celebrate, dance, shake (gentle head shake), neutral

### Troubleshooting

| Message | Fix |
|---|---|
| `unknown step type 'sayy'` | Typo in the first key of a step. |
| `unknown placeholder {chlid_name}` | Check spelling; declare your own with `set`, `pick`, `for_each` or `variables`. |
| `'animal' items have no field 'colour'` | Every item of the data list needs that field. |
| `branch 'Yes' does not match any option` | Branch keys must be option labels. |
| `bad note name 'H4'` | Notes are A-G (+ `#`/`b`) and an octave 0-8, or `R`. |
| `duplicate activity id` | Pick a new `id`. |
| `YAML syntax error ... line N` | Usually indentation or a missing quote; text containing `:` or `{` needs quotes. |
