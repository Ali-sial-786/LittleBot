"""Activity model + YAML step language: parsing, validation messages, templates, loading."""

from __future__ import annotations

import textwrap

import pytest

from activity_helpers import activity_yaml, shared_data
from littlebot.activities import model as m
from littlebot.activities.model import (
    ActivityError, ActivityLibrary, load_directories, parse_activities_text, parse_notes, render_template,
    template_placeholders,
)


def steps(block: str) -> str:
    return textwrap.indent(textwrap.dedent(block).strip("\n"), "  ") + "\n"


def parse_one(block: str, **kw):
    return parse_activities_text(activity_yaml(steps(block), **kw), shared_data=shared_data())[0]


def error_of(block: str, **kw) -> str:
    with pytest.raises(ActivityError) as e:
        parse_one(block, **kw)
    return str(e.value)


# ----------------------------------------------------------------- short forms
def test_short_forms_parse_into_typed_steps():
    act = parse_one("""
        - say: "Hi {child_name}!"
          expression: happy
          animation: bounce
          pause: 1.5
        - close_eyes
        - open_eyes:
        - wait: 2
        - look: left
        - look: [0.3, 0.7]
        - expression: surprised
          hold: 2
        - sound: peekaboo
        - melody: "C4 E4 G4:2 R:0.5"
          tempo: 120
        - gesture: wave
        - blink: true
        - note: "Get a cloth"
        - set: {colour: red}
        - pick: animal
          from: animals
        - say: "{animal.name}"
        - end: all done
    """)
    kinds = [s.kind for s in act.steps]
    assert kinds == ["say", "close_eyes", "open_eyes", "wait", "look", "look", "expression", "sound", "melody",
                     "gesture", "blink", "note", "set", "pick", "say", "end"]
    say = act.steps[0]
    assert isinstance(say, m.SayStep) and say.expression == "happy" and say.pause == 1.5
    assert (act.steps[4].x, act.steps[4].y) == m.LOOK_TARGETS["left"]
    assert act.steps[8].notes == [("C4", 1.0), ("E4", 1.0), ("G4", 2.0), ("R", 0.5)]
    assert act.steps[10].double is True
    assert act.steps[-1].reason == "all done"


def test_nested_structures_and_single_step_branches():
    act = parse_one("""
        - repeat: 2
          steps:
            - say: hi
        - prompt: "Did she clap?"
          options: ["Clapped!", "Not yet"]
          branches:
            "clapped": {sound: yay}
            "Not yet": [{say: ok}, end]
          on_timeout: {say: later}
        - choice:
            - say: a
            - steps: [{say: b}, {sound: pop}]
            - [{say: c}]
        - wait_for: vision.gesture
          match: {gesture: wave}
          ask_caregiver: "Tap when she waves"
          on_event: {say: yay}
        - listen: 5
          expect: [moo]
          on_match: {say: moo}
        - for_each: n
          in: [3, 2, 1]
          steps: [{say: "{n}"}]
        - song: Twinkle
          lines:
            - {words: "Twinkle, twinkle", notes: "C4 C4 G4 G4"}
            - {words: "little star"}
    """)
    rep, prompt, choice, wf, listen, fe, song = act.steps
    assert rep.times == 2 and rep.steps[0].kind == "say"
    assert [s.kind for s in prompt.branch_for("Clapped!")] == ["sound"]
    assert [s.kind for s in prompt.branch_for("not yet")] == ["say", "end"]
    assert [len(o) for o in choice.options] == [1, 2, 1]
    assert wf.event == ["vision.gesture"] and wf.match == {"gesture": "wave"}
    assert listen.timeout == 5 and listen.expect == ["moo"]
    assert fe.in_ == [3, 2, 1]
    assert len(song.lines) == 2 and song.lines[1].notes == []
    paths = [p for p, _ in act.all_steps()]
    assert "steps[1].branches['Not yet'][1]" in paths


def test_explicit_kind_form_is_accepted():
    act = parse_one("""
        - {kind: say, text: hello}
    """)
    assert act.steps[0].text == "hello"


# ----------------------------------------------------------------- errors
@pytest.mark.parametrize("block, fragment", [
    ("- sayy: hi", "unknown step type 'sayy'"),
    ("- jump", "unknown step 'jump'"),
    ("- say: hi\n  expression: grumpy", "unknown expression 'grumpy'"),
    ("- animation: explode", "unknown animation 'explode'"),
    ("- sound: kaboom", "unknown sound 'kaboom'"),
    ("- gesture: fly", "unknown gesture 'fly'"),
    ("- melody: H4", "bad note name 'H4'"),
    ("- repeat: 3", "steps"),
    ("- say: hi\n  volume: 3", "volume"),
    ("- say: \"{nope}\"", "unknown placeholder {nope}"),
    ("- prompt: q\n  options: [a, b]\n  branches: {c: []}", "branch 'c' does not match any option"),
    ("- prompt: q\n  options: [a, skip]", "'skip' is always available"),
    ("- pick: x\n  from: unicorns", "unknown data list 'unicorns'"),
    ("- pick: x\n  from: animals\n- say: \"{x.colour}\"", "have no field 'colour'"),
    ("- wait_for: activity.finished", "cannot wait on"),
    ("- close_eyes: 3", "takes no value"),
    ("- set: {round: 2}", "built-in placeholder"),
    ("- wait: 500", "seconds"),
])
def test_validation_errors_are_readable(block, fragment):
    msg = error_of(block)
    assert fragment in msg
    assert "activity 'test_act'" in msg


def test_error_names_the_nested_step_path():
    msg = error_of("""
        - repeat: 2
          steps:
            - say: ok
            - prompt: q
              options: [a]
              branches:
                a:
                  - expression: grumpy
    """)
    assert "steps[0] (repeat).steps[1] (prompt).branches['a'][0] (expression)" in msg


def test_metadata_validation():
    with pytest.raises(ActivityError, match="unknown category"):
        parse_one("- say: hi", category="cooking")
    with pytest.raises(ActivityError, match="max_age_months must be greater"):
        parse_one("- say: hi", min_age=24, max_age=12)


def test_activity_needs_safety_notes():
    text = activity_yaml(steps("- say: hi")).replace("safety_notes: [Supervise.]", "safety_notes: []")
    with pytest.raises(ActivityError, match="safety_notes"):
        parse_activities_text(text)


# ----------------------------------------------------------------- templates / notes
def test_render_template_fallbacks_and_tidying():
    values = {"child_name": "", "animal": {"name": "cow", "sound": "moo"}, "list": ["a", "b", "c"], "n": 3.0}

    def look(name):
        root, *rest = name.split(".")
        v = values.get(root, "")
        for r in rest:
            v = v.get(r, "") if isinstance(v, dict) else ""
        return v

    fb = {"child_name": "friend"}
    assert render_template("{child_name}, can you clap?", look, fb) == "Friend, can you clap?"
    assert render_template("For {child_name|baby} and me", look, fb) == "For baby and me"
    assert render_template("The {animal.name} says {animal.sound}!", look, fb) == "The cow says moo!"
    assert render_template("{list}!", look, fb) == "A, b and c!"
    assert render_template("Count {n} , go", look, fb) == "Count 3, go"
    assert render_template("{{literal}} {missing}", look, fb) == "{literal}"
    assert render_template("{animal.sound}! {animal.sound}! Ready? {list}.", look, fb) == "Moo! Moo! Ready? A, b and c."
    assert render_template("Here a {animal.sound}, there a {animal.sound}", look, fb) == "Here a moo, there a moo"
    assert template_placeholders("Hi {child_name|you} and {animal.name}") == ["child_name", "animal.name"]
    with pytest.raises(ActivityError):
        template_placeholders("bad {not valid}")


def test_parse_notes_forms():
    assert parse_notes("C4 D#4:0.5 Bb3:2 R") == [("C4", 1.0), ("D#4", 0.5), ("Bb3", 2.0), ("R", 1.0)]
    assert parse_notes([["C4", 1], "G4:2"]) == [("C4", 1.0), ("G4", 2.0)]
    for bad in ("", "C9", "C4:0", "C4:x", [["C4"]]):
        with pytest.raises(ValueError):
            parse_notes(bad)


def test_age_group_labels_and_range_text():
    act = parse_one("- say: hi", min_age=6, max_age=18)
    assert act.age_groups() == ["6–12 months", "12–18 months"]
    assert act.age_range_label() == "6–18 months"
    older = parse_one("- say: hi", min_age=36, max_age=72)
    assert older.age_groups() == ["3–5 years", "5 years +"]
    assert older.age_range_label() == "3–6 years"
    assert m.age_range_label(18, 48) == "18 months – 4 years"
    assert older.suitable_for(36) and not older.suitable_for(72)


# ----------------------------------------------------------------- loading
def test_loader_skips_broken_files_and_reports(tmp_path):
    good = activity_yaml(steps("- say: hi"), id="good_one")
    (tmp_path / "a_good.yaml").write_text(good)
    (tmp_path / "b_bad.yaml").write_text(activity_yaml(steps("- say: hi\n  expression: grumpy"), id="bad_one"))
    (tmp_path / "c_dupe.yaml").write_text(good)
    (tmp_path / "d_syntax.yaml").write_text("id: [unclosed\n")
    (tmp_path / "_words.yaml").write_text("data:\n  fruits: [apple, pear]\n")
    (tmp_path / "e_uses_shared.yaml").write_text(
        activity_yaml(steps("- pick: f\n  from: fruits\n- say: '{f}'"), id="fruity"))
    report = load_directories([tmp_path, tmp_path / "missing_dir"])
    assert sorted(report.activities) == ["fruity", "good_one"]
    assert report.shared_data == {"fruits": ["apple", "pear"]}
    joined = "\n".join(report.errors)
    assert "b_bad.yaml" in joined and "grumpy" in joined
    assert "duplicate activity id 'good_one'" in joined
    assert "d_syntax.yaml" in joined and "YAML syntax error" in joined
    lib = ActivityLibrary([tmp_path])
    assert len(lib.load()) == 3 and "good_one" in lib
    with pytest.raises(ActivityError):
        lib.load(strict=True)


def test_multi_activity_file_and_list_form():
    a = activity_yaml(steps("- say: one"), id="first_one")
    b = activity_yaml(steps("- say: two"), id="second_one")
    as_list = "activities:\n" + "\n".join("  - " + "\n    ".join(x.strip().splitlines()) for x in (a, b))
    assert [x.id for x in parse_activities_text(as_list)] == ["first_one", "second_one"]


def test_guide_examples_are_valid():
    """The complete example and snippets in docs/ACTIVITY_GUIDE.md must stay valid."""
    import re as _re

    from littlebot.core.paths import PROJECT_ROOT

    guide = (PROJECT_ROOT / "docs" / "ACTIVITY_GUIDE.md").read_text(encoding="utf-8")
    blocks = _re.findall(r"```yaml\n(.*?)```", guide, flags=_re.S)
    full = [b for b in blocks if b.startswith("id: ")]
    assert full, "the guide should contain a complete example"
    for b in full:
        assert parse_activities_text(b, shared_data=shared_data())
    snippets = [b for b in blocks if not b.startswith("id: ")]
    assert len(snippets) >= 4
    for b in snippets:
        if b.startswith("data:"):
            data_part, steps_part = b.split("steps:\n", 1)
            text = activity_yaml(steps_part, extra=data_part)
        else:
            text = activity_yaml(b, extra="variables: {animal: {name: cow, sound: moo}}\n")
        acts = parse_activities_text(text, shared_data={**shared_data(), "fruits": [{"name": "a", "colour": "b"}]})
        assert acts[0].steps
