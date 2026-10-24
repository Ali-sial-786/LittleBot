"""Every fixed line the activities speak, worked out from the scripts without running them.

Used to record the lines once in the ChatGPT voice (``scripts/record_voice_lines.py``, the
dashboard's "Record activity voices" button); ``AudioService.say()`` then plays a recording when
one exists for exactly the line an activity says.

A line is a ``say`` text (each choice of a list) or a sung line's words. Its placeholders are filled
the way the engine fills them (``model.render_template`` with the engine's fallbacks), with every
value they can have in that place:

* built-ins from the profile: ``{child_name}`` (the nickname, e.g. "Amara"), ``{favourite_animal}``,
  ``{robot_name}``... (empty -> the fallback: "friend", "puppy"...), ``{round}`` (1..N in a repeat);
* ``pick`` / ``for_each`` items from a data list (``{animal.sound}``: one line per animal), ``set``
  values, the activity's ``variables``.

A line whose values can't be known in advance stays text-to-speech: what the child said
(``listen: store_as``), several items picked at once (``pick: count: 2``), or more than
``max_variants`` combinations of one template.

The walk follows the script's order: a variable set by ``set`` / ``pick`` counts from there on (for
the rest of the run, like ``run.values``); branches, choices and loops merge what each way could leave.
"""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from littlebot.activities import model as m
from littlebot.activities.engine import _FALLBACKS, _lookup
from littlebot.activities.model import Activity, ActivityLibrary
from littlebot.audio.tts.base import clean_text, scrub_names

MAX_VARIANTS = 40  # combinations of one template's values; more = not a "fixed" line

Scope = dict[str, "list[Any] | None"]  # variable -> the values it can have here (None: unknown)


@dataclass(frozen=True)
class ActivityLine:
    activity: str  # activity id
    text: str  # as LittleBot says it (placeholders filled, cleaned like speech)
    uses_name: bool  # the child's nickname (or its initial) is in it


def spoken_lines(library: ActivityLibrary, values: dict[str, Any], *, only: Iterable[str] | None = None,
                 max_variants: int = MAX_VARIANTS) -> list[ActivityLine]:
    """The fixed lines of every activity (or the ids in ``only``), in script order, without repeats.

    ``values``: the built-in placeholders (``engine.profile_values(profile, robot_name)``)."""
    wanted = set(only) if only is not None else None
    out: list[ActivityLine] = []
    for act in library.all():
        if wanted is not None and act.id not in wanted:
            continue
        out.extend(_ActivityWalk(library, act, values, max_variants).run())
    return out


def _key(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _union(*pools: list[Any]) -> list[Any]:
    seen: dict[str, Any] = {}
    for pool in pools:
        for v in pool:
            seen.setdefault(_key(v), v)
    return list(seen.values())


def _merge(scopes: list[Scope]) -> Scope:
    """What a variable can be after one of several ways through the script.

    A way that leaves it unset adds nothing: that is a rare path (the caregiver skipped the prompt
    that sets it) and its line, with a hole where the value goes, isn't worth recording."""
    out: Scope = {}
    for name in {k for s in scopes for k in s}:
        pools = [s[name] for s in scopes if name in s]
        out[name] = None if any(p is None for p in pools) else _union(*pools)  # type: ignore[arg-type]
    return out


class _ActivityWalk:
    def __init__(self, library: ActivityLibrary, act: Activity, values: dict[str, Any], max_variants: int) -> None:
        self.library, self.act, self.max_variants = library, act, max_variants
        self.nickname = str(values.get("child_name") or "")
        self.start: Scope = {k: [v] for k, v in values.items()}
        self.start.update({k: [v] for k, v in act.variables.items()})
        self.lines: dict[str, ActivityLine] = {}

    def run(self) -> list[ActivityLine]:
        self._walk(self.act.steps, dict(self.start))
        return list(self.lines.values())

    # ------------------------------------------------------------ text
    def _render(self, text: str, scope: Scope) -> list[str] | None:
        """Every way ``text`` can read here, or None when that can't be known (or there are too many)."""
        roots = list(dict.fromkeys(n.split(".")[0] for n in m.template_placeholders(text)))
        pools: list[list[Any]] = []
        for root in roots:
            pool = scope.get(root, [""])
            if pool is None:
                return None
            pools.append(pool or [""])
        if math.prod(len(p) for p in pools) > self.max_variants:
            return None
        out: list[str] = []
        for combo in itertools.product(*pools):
            vals = dict(zip(roots, combo))
            out.append(m.render_template(text, lambda name, vals=vals: _lookup(vals, name), _FALLBACKS))
        return list(dict.fromkeys(out))

    def _emit(self, text: str, scope: Scope) -> None:
        rendered = self._render(text, scope)
        if rendered is None:
            return
        initial = "child_initial" in {n.split(".")[0] for n in m.template_placeholders(text)}
        for line in rendered:
            line = clean_text(line)
            if line and line not in self.lines:
                uses_name = bool(self.nickname) and (initial or scrub_names(line, [self.nickname]) != line)
                self.lines[line] = ActivityLine(self.act.id, line, uses_name)

    def _items(self, source: str | list[Any]) -> list[Any]:
        return self.library.data_list(source, self.act) if isinstance(source, str) else list(source)

    # ------------------------------------------------------------ steps
    def _walk(self, steps: list[m.Step], scope: Scope) -> Scope:
        for step in steps:
            scope = self._step(step, scope)
        return scope

    def _loop(self, steps: list[m.Step], scope: Scope, loop_vars: Scope) -> Scope:
        """A loop body: twice, so what one round sets is seen by the next; loop variables restored after."""
        inner = {**scope, **loop_vars}
        after = self._walk(steps, inner)
        after = self._walk(steps, _merge([inner, {**after, **loop_vars}]))
        return {**_merge([scope, after]), **{k: scope.get(k, [""]) for k in loop_vars}}

    def _step(self, step: m.Step, scope: Scope) -> Scope:  # noqa: C901 - one branch per step type
        if isinstance(step, m.SayStep):
            for text in step.templates():
                self._emit(text, scope)
        elif isinstance(step, m.SongStep):
            if step.style != "play_only":
                for line in step.lines:
                    if line.words:
                        self._emit(line.words, scope)
        elif isinstance(step, m.SetStep):
            scope = dict(scope)
            for k, v in step.values.items():
                scope[k] = self._render(v, scope) if isinstance(v, str) else [v]
        elif isinstance(step, m.PickStep):
            scope = {**scope, step.var: self._items(step.from_) if step.count == 1 else None}
        elif isinstance(step, m.ForEachStep):
            items = self._items(step.in_)
            n = min(len(items), step.limit or len(items))
            if not step.shuffle:
                items = items[:n]
            scope = self._loop(step.steps, scope, {step.var: items, "round": list(range(1, n + 1))})
        elif isinstance(step, m.RepeatStep):
            scope = self._loop(step.steps, scope, {"round": list(range(1, step.times + 1))})
        elif isinstance(step, m.ChoiceStep):
            scope = _merge([self._walk(option, scope) for option in step.options])
        elif isinstance(step, m.PromptStep):
            ways = [self._walk(branch, scope) for branch in step.branches.values()]
            scope = _merge([scope, self._walk(step.on_timeout, scope), *ways])
        elif isinstance(step, m.WaitForStep):
            scope = _merge([scope, self._walk(step.on_event, scope), self._walk(step.on_timeout, scope)])
        elif isinstance(step, m.ListenStep):
            if step.store_as:
                scope = {**scope, step.store_as: None}  # what the child said: never known in advance
            ways = [self._walk(s, scope) for s in (step.on_match, step.on_heard, step.on_timeout)]
            scope = _merge([scope, *ways])
        return scope
