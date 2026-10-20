# Activities module (`littlebot/activities/`)

Caregiver-led, YAML-scripted activities (games, songs, stories) for a child of about
6 months to 5 years. The engine implements `core.interfaces.ActivityAPI` + `core.service.Service`.
Parent-facing usage and the full script-language reference: [ACTIVITY_GUIDE.md](../ACTIVITY_GUIDE.md).

## Files

| File | Contents |
|------|----------|
| `__init__.py` | `create_service(ctx)` -> `ActivityEngine` (loads the scripts; broken ones are logged and skipped) |
| `model.py` | Step DSL (pydantic models), YAML parsing with readable error paths, placeholder templates, notes, `Activity`, `check_activity` (cross-references), `load_directories`, `ActivityLibrary`. Pure: no runtime deps on other subsystems |
| `engine.py` | `ActivityEngine`: run lifecycle, leases, state, prompts, waiting, device wrappers, catalogue (`list/get/recommended`), opt-in history |
| `__main__.py` | `python -m littlebot.activities [paths] [-v]` validator for script authors (exit 1 on problems) |
| `assets/activities/*.yaml` | content: `baby_play`, `songs`, `learning`, `thinking`, `movement`, `stories`, `feelings`, `pretend` (43 activities) |
| `assets/activities/_data.yaml` | shared word lists (animals, colours, shapes, body parts, letters, opposites, zoo animals) |

## Script model

```
YAML ──yaml.CSafeLoader──> dicts ──normalise_step (first key = type)──> {"kind": ..., fields}
    ──parse_step (recursive for nested step lists, path tracked)──> Step models (extra="forbid")
    ──Activity.model_validate──> metadata checks (category, ages, finish_expression ...)
    ──check_activity──> placeholders exist, data lists exist, {var.field} exists in every item
```

* 23 step kinds (`STEP_TYPES`): `say expression animation sound melody song gesture look
  clear_gaze close_eyes open_eyes blink wait note prompt wait_for listen repeat for_each
  choice set pick end`. Every step also accepts `pause` (seconds after it).
* Vocabularies are validated against `core.interfaces` (`EXPRESSIONS`, `FACE_ANIMATIONS`,
  `SOUND_NAMES`, `ACTIVITY_CATEGORIES`) and `model.GESTURES`.
* Errors carry the full path: `songs.yaml: activity 'x': steps[2] (repeat).steps[0] (prompt).branches['a'][0] (expression): unknown expression 'grumpy'; choose one of: ...`.
* `load_directories` never raises for content problems: broken activities are skipped,
  problems collected in `report.errors` (logged at ERROR, service status `degraded`).
  Files starting with `_` provide shared `data:` lists. Duplicate ids: first wins.
* Script folders: `ACTIVITY_SCRIPTS_DIR` + `config.activities.custom_dirs` (relative to
  `data_dir`, default `data/activities`; missing folders ignored).

## Run lifecycle

```
start(id, source)                                   (async, on the runtime loop)
  KeyError if unknown
  _stop_current("replaced")                         previous run fully finished first
  arbiter.acquire("activities", {face, audio_out}, Priority.ACTIVITY, on_preempt)
      None -> PermissionError
  state.transition(PLAYING_ACTIVITY)  refused -> release lease, PermissionError
  task = create_task(_run_activity)   + done-callback safety net
  publish activity.started {id, name, category, source}; return status()

_run_activity: open_eyes -> _run_steps(top=True) -> completed / end step / cancelled / error
_finish (idempotent, sync; the ONLY cleanup path):
  clear pending prompt (activity.prompt_cleared {cancelled})
  if stopped: cancel our background gesture (+ motion.cancel), audio.stop_output()
  face.open_eyes(); expression finish_expression (hold 2 s) or neutral when stopped
  lease.release(); state.release(PLAYING_ACTIVITY)  (never clobbers a newer state)
  history row if privacy.record_history; publish activity.finished {id, name, completed, reason, duration_seconds}
```

Ways a run ends (reason in `activity.finished`): `completed`, an `end` step's reason
(completed=True), `stop(reason)` (e.g. `parent stop`), `replaced`, `stop all`
(`system.stop_all`), `preempted by <owner>` (lease), `robot state sleeping|error|playing_media`
(someone else switched state), `error` (unexpected exception, logged), `cancelled`.

* **Preemption:** `on_preempt` runs synchronously in the preempter's thread: it marks the
  run, calls `audio.stop_output()` and `face.open_eyes()` *before* the new holder starts
  output, then cancels the task thread-safely. Cleanup then skips the face reset and
  `stop_output` so it never interferes with the new holder.
* **Cancellation before the first step:** a task cancelled before it ever runs never
  enters its `finally`; a task done-callback calls `_finish` (idempotent) as a safety net.
* `stop()` waits (bounded, 3 s) for the task; if it hangs, cleanup is forced.

## Waiting, timing, pause

* All waits go through `_await_any(run, futures, timeout)`: timeout in *script seconds*
  scaled by `ctx.clock` (`time_scale=0` in tests -> instant), and **frozen while paused**
  (the remaining time is tracked across pause/resume).
* `pause()` (pausable activities only): sets the pause events, `audio.stop_output()`,
  cancels a running gesture, neutral face, `activity.paused`. The run blocks at the next
  step boundary (`_checkpoint`). A `say` / melody interrupted by the pause (returns False
  and the pause epoch changed) is repeated once after `resume()`.
* Prompts can be answered while paused; the branch runs after resuming.

## Caregiver prompts

`prompt` and `wait_for ... ask_caregiver` open a `_Prompt` (id `<run>-<n>`), publish
`activity.step` then `activity.prompt {id, prompt_id, text, options, timeout_seconds, kind}`
and wait. `respond(response, prompt_id=None)` is thread-safe (lock + `call_soon_threadsafe`,
or direct set on the loop thread so synchronous auto-responders work with `time_scale=0`).
Matching: option label (case/punctuation-insensitive), `skip` (no branch), a 1-based
number, or `done/ok/yes/next/continue` (= first option). Returns False for a stale
`prompt_id`, an unknown answer, or an already answered prompt. `caregiver.response
{response, prompt_id}` events are routed to `respond()`. `activity.prompt_cleared {id,
prompt_id, response, timed_out?|skipped?|cancelled?}` always follows.

## wait_for and listen

* `wait_for` subscribes (sync bus callback -> future, thread-safe) **before** publishing its
  step event. `match` = payload equality (text case-insensitive), `contains` = word match.
  Patterns whose source is disabled are not subscribed (`vision.*` needs camera on;
  `vision.gesture` also needs `vision.gestures_enabled`; `audio.utterance` needs mic on).
  Nothing subscribable and no caregiver button -> immediate `on_timeout` (no pointless wait).
* `listen` calls `ctx.audio.listen(timeout)` only if `privacy.microphone_enabled`
  (bounded by `asyncio.wait_for`); no mic / no audio / error = silence (`on_timeout`).
  Heard text is never logged, published or stored (only optionally kept in the run's
  in-memory variables via `store_as`).

## Robustness

`ctx.face`, `ctx.audio`, `ctx.motion` may be `None` or raise. Every call goes through a
guard (`_face`, `_guard`, `_audio_stop`, `_motion_cancel`) that logs once per run and
carries on. Missing audio keeps the pacing with a short estimated sleep. Gestures are
optional: skipped if motion is None, `status()["estop"]`, or the name isn't in
`motion.gestures()`; non-waiting gestures run as one background task (a new gesture is
skipped while one is still moving). Bus publishing is guarded too.

## Catalogue

* `list(age_months=None, category=None)` - the full catalogue entries
  `{id, name, description, min_age_months, max_age_months, age_groups (labels),
  age_range, category, participation, parent_instructions, safety_notes,
  duration_seconds, pausable, tags, materials, enabled, favourite, suitable}`.
  With `age_months`: only `min <= age < max`, most suitable first (near the middle of a
  narrow range, favourites first). Without: everything; suitable-for-profile-age first,
  or youngest first when the age is unknown. Disabled activities are *included*
  (`enabled: False`) so parents can still start / re-enable them.
* `recommended(age_months=None, category=None, limit=None)` - enabled + suitable
  (profile age by default), favourites first. This is the listing that respects
  `profile.activity_preferences[id].enabled`.
* `get(id)` - summary + `step_count`, `source`, `outline` (top-level step texts).
* `reload()` re-reads scripts; `categories()`; `wait_until_finished(timeout)`.

## Privacy and the non-diagnostic stance

* No scores, no "performance", nothing diagnostic is computed or stored. Content tests
  reject judging language ("wrong", "score", "delay" ...).
* Optional history (`privacy.record_history`, default False): table `activity_history
  (activity_id, started, finished, completed, reason, source)` created lazily only when
  enabled; `history(limit)`, `clear_history()`.
* Profile text (nickname, favourites) is sanitised (printable, no braces, 32 chars) and
  only ever substituted as a value - never re-rendered as a template.

## Events published

`activity.started {id, name, category, source}` · `activity.step {id, step_index, kind,
text}` (say, note, melody, song lines, prompt, wait_for, listen) · `activity.prompt` ·
`activity.prompt_cleared` · `activity.paused {id}` · `activity.resumed {id}` ·
`activity.finished {id, name, completed, reason, duration_seconds}`.
Subscribed: `system.stop_all`, `caregiver.response`, `state.changed`.

## Contract notes

* `Service.start()/stop()` vs `ActivityAPI.start(id)/stop(reason)`: `start()` without an id
  is the lifecycle start (returns `health()`); `stop()` always stops the current activity
  (exactly what shutdown needs).
* `Service.__init__` assigns `self.status` (a `ServiceStatus`), which would shadow
  `ActivityAPI.status()`. The engine therefore does not call `Service.__init__` and keeps
  its service status in `_svc_status` (overriding `set_status` / `health`).

## Tests

`tests/activities/`: `test_activity_model.py` (DSL, errors, templates, loading),
`test_activity_engine.py` (lifecycle, prompts, wait_for, listen, pause, preemption,
STOP ALL, state, devices failing/missing, catalogue, history, runtime-thread
integration), `test_activity_content.py` (every script loads, vocabulary, age coverage,
kind language, public-domain songs, and **every activity run end to end** with four
answer strategies, without devices, and with speech heard). Helpers in
`activity_helpers.py`.
