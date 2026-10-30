"""scripts/set_config.py: one setting in config/local.yaml, the rest of the file (comments too) kept."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("set_config", Path(__file__).resolve().parents[1] / "scripts" / "set_config.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

# The end of the robot's local.yaml on 2026-10-29 (with the "audio:" an earlier fix appended)
ROBOT = """\
# Logging: JSON lines
logging:
  level: INFO              # DEBUG while bringing up hardware
  format: json

chatgpt:
  live_voice: sol
  live_barge_in_speech_ms: 800

decide:
  min_confidence: 0.4
audio:
  echo_cancel: auto
"""


def run(tmp_path: Path, *args: str, text: str = ROBOT) -> tuple[int, str]:
    f = tmp_path / "local.yaml"
    f.write_text(text, encoding="utf-8")
    code = sc.main([*args, "--file", str(f)])
    return code, f.read_text(encoding="utf-8")


def test_a_new_setting_goes_under_its_section(tmp_path, capsys):
    code, text = run(tmp_path, "chatgpt.live_barge_in_max_pitch_hz", "220")
    assert code == 0
    assert "chatgpt:\n  live_barge_in_max_pitch_hz: 220\n  live_voice: sol\n" in text
    assert "# DEBUG while bringing up hardware" in text and text.count("chatgpt:") == 1
    assert "line 7: live_barge_in_max_pitch_hz: 220" in capsys.readouterr().out


def test_a_setting_that_is_there_is_changed_in_place_keeping_its_comment(tmp_path):
    _, text = run(tmp_path, "logging.level", "DEBUG")
    assert "  level: DEBUG              # DEBUG while bringing up hardware\n" in text


def test_unset_removes_it_so_the_default_applies(tmp_path):
    code, text = run(tmp_path, "--unset", "chatgpt.live_barge_in_speech_ms")
    assert code == 0 and "live_barge_in_speech_ms" not in text and "  live_voice: sol\n" in text


def test_a_new_section_goes_at_the_end_and_words_yaml_reads_as_booleans_are_quoted(tmp_path):
    _, text = run(tmp_path, "wake.min_confidence", "0.35")
    assert text.endswith("audio:\n  echo_cancel: auto\n\nwake:\n  min_confidence: 0.35\n")
    _, text = run(tmp_path, "audio.echo_cancel", "off")
    assert '  echo_cancel: "off"\n' in text


def test_a_misspelt_name_or_a_wrong_value_changes_nothing(tmp_path, capsys):
    code, text = run(tmp_path, "chatgpt.live_barge_in_max_pitch", "220")
    assert code == 1 and text == ROBOT and "Not changed" in capsys.readouterr().out
    code, text = run(tmp_path, "chatgpt.live_barge_in_max_pitch_hz", "loud")
    assert code == 1 and text == ROBOT


def test_the_copy_of_a_section_that_counts_is_changed_and_a_second_copy_is_pointed_out(tmp_path, capsys):
    twice = "audio:\n  speaker:\n    volume: 0.4\n  echo_cancel: auto\n\n" + ROBOT
    _, text = run(tmp_path, "audio.echo_cancel", "on", text=twice)
    assert text.startswith("audio:\n  speaker:\n    volume: 0.4\n  echo_cancel: auto\n")  # the first copy as it was
    assert text.endswith('audio:\n  echo_cancel: "on"\n')  # the last one: the one YAML keeps
    assert "'audio:' is in local.yaml 2 times (lines 1, 17)" in capsys.readouterr().out


def test_a_nested_setting(tmp_path):
    _, text = run(tmp_path, "audio.speaker.volume", "0.5")
    assert text.endswith("audio:\n  speaker:\n    volume: 0.5\n  echo_cancel: auto\n")
