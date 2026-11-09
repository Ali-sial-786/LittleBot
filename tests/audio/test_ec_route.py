"""littlebot/audio/ec_route.py: the echo canceller is moved back to the USB speaker when WirePlumber linked it
elsewhere. The graph is the robot's own (2026-10-31): the canceller played into the ReSpeaker Lite's headphone
socket instead of the UACDemoV1.0 speaker. ``pw-link`` is faked: nothing here touches a real PipeWire."""

from __future__ import annotations

import logging
from pathlib import Path

from littlebot.audio import ec_route

SPK = "alsa_output.usb-Jieli_Technology_UACDemoV1.0_4150344E37343316-00.analog-stereo"
MIC_OUT = "alsa_output.usb-Seeed_Studio_ReSpeaker_Lite_0000000001-00.analog-stereo"
MIC = "alsa_input.usb-Seeed_Studio_ReSpeaker_Lite_0000000001-00.analog-stereo"
HDMI = "alsa_output.platform-107c701400.hdmi.hdmi-stereo"

DROP_IN = f"""context.modules = [
    {{   name = libpipewire-module-echo-cancel
        args = {{
            capture.props = {{
                node.name = "littlebot_ec_capture"
                target.object = "{MIC}"
                node.passive = true
            }}
            playback.props = {{
                node.name = "littlebot_ec_playback"
                target.object = "{SPK}"
                node.passive = true
            }}
        }}
    }}
]
"""


class FakePW:
    """``pw-link -o / -i / -l``, ``pw-link OUT IN`` and ``pw-link -d OUT IN`` on a little graph."""

    def __init__(self, links: set[tuple[str, str]], *, sinks: tuple[str, ...] = (SPK, MIC_OUT, HDMI),
                 canceller: bool = True, refuse: bool = False) -> None:
        self.links = set(links)
        self.sinks = sinks
        self.canceller = canceller
        self.refuse = refuse  # pw-link can't change anything
        self.calls: list[list[str]] = []

    def outputs(self) -> list[str]:
        ports = [f"{MIC}:capture_FL", f"{MIC}:capture_FR"]
        if self.canceller:
            ports += ["littlebot_ec_playback:output_FL", "littlebot_ec_playback:output_FR",
                      "littlebot_ec_mic:capture_MONO"]
        return ports

    def inputs(self) -> list[str]:
        ports = [f"{s}:playback_{ch}" for s in self.sinks for ch in ("FL", "FR")]
        if self.canceller:
            ports += ["littlebot_ec_capture:input_FL", "littlebot_ec_capture:input_FR"]
        return ports

    def __call__(self, args: list[str]) -> str | None:
        self.calls.append(args)
        if args == ["-o"]:
            return "\n".join(self.outputs()) + "\n"
        if args == ["-i"]:
            return "\n".join(self.inputs()) + "\n"
        if args == ["-l"]:
            lines = []
            for out in sorted({o for o, _ in self.links}):
                lines.append(out)
                lines += [f"  |-> {i}" for o, i in sorted(self.links) if o == out]
            for inp in sorted({i for _, i in self.links}):  # pw-link -l lists each input's sources too
                lines.append(inp)
                lines += [f"  |<- {o}" for o, i in sorted(self.links) if i == inp]
            return "\n".join(lines) + "\n"
        if self.refuse:
            return None
        if args[0] == "-d":
            self.links.discard((args[1], args[2]))
        else:
            self.links.add((args[0], args[1]))
        return ""

    def changes(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] not in ("-o", "-i", "-l")]


def _wrong() -> set[tuple[str, str]]:
    """The robot's graph on 2026-10-31."""
    return {("littlebot_ec_playback:output_FL", f"{MIC_OUT}:playback_FL"),
            ("littlebot_ec_playback:output_FR", f"{MIC_OUT}:playback_FR"),
            (f"{MIC}:capture_FL", "littlebot_ec_capture:input_FL")}


def _right() -> set[tuple[str, str]]:
    return {("littlebot_ec_playback:output_FL", f"{SPK}:playback_FL"),
            ("littlebot_ec_playback:output_FR", f"{SPK}:playback_FR"),
            (f"{MIC}:capture_FL", "littlebot_ec_capture:input_FL")}


def _drop_in(tmp_path: Path, text: str = DROP_IN) -> Path:
    p = tmp_path / "60-littlebot-echo-cancel.conf"
    p.write_text(text)
    return p


def test_the_robots_graph_is_seen_as_wrong(tmp_path):
    route = ec_route.check(FakePW(_wrong()), _drop_in(tmp_path))
    assert route is not None and not route.ok
    assert route.playing_on == [MIC_OUT] and route.speaker == SPK and route.mic == MIC
    assert "not on the speaker" in route.describe()


def test_it_is_moved_to_the_speaker_with_the_channels_kept(tmp_path, caplog):
    pw = FakePW(_wrong())
    with caplog.at_level(logging.WARNING, logger="littlebot.audio.devices"):
        route = ec_route.ensure(pw, _drop_in(tmp_path))
    assert route is not None and route.ok
    assert pw.links == _right()
    assert sorted(pw.changes()) == sorted([
        ["-d", "littlebot_ec_playback:output_FL", f"{MIC_OUT}:playback_FL"],
        ["-d", "littlebot_ec_playback:output_FR", f"{MIC_OUT}:playback_FR"],
        ["littlebot_ec_playback:output_FL", f"{SPK}:playback_FL"],
        ["littlebot_ec_playback:output_FR", f"{SPK}:playback_FR"]])
    assert "moved it back to the speaker" in caplog.text


def test_right_already_changes_nothing(tmp_path):
    pw = FakePW(_right())
    route = ec_route.ensure(pw, _drop_in(tmp_path))
    assert route is not None and route.ok and pw.changes() == []


def test_not_linked_at_all_is_linked(tmp_path):
    pw = FakePW({(f"{MIC}:capture_FL", "littlebot_ec_capture:input_FL")})
    route = ec_route.ensure(pw, _drop_in(tmp_path))
    assert route is not None and route.ok and pw.links == _right()


def test_without_the_drop_in_the_usb_output_that_isnt_the_microphones_is_chosen(tmp_path):
    pw = FakePW(_wrong())
    route = ec_route.ensure(pw, tmp_path / "missing.conf")
    assert route is not None and route.ok and route.speaker == SPK


def test_never_moved_to_the_microphones_own_socket(tmp_path):
    pw = FakePW(_wrong(), sinks=(MIC_OUT, HDMI))  # the USB speaker is unplugged
    route = ec_route.ensure(pw, _drop_in(tmp_path))
    assert route is not None and not route.ok and route.speaker == ""
    assert pw.changes() == []


def test_a_move_pw_link_refuses_is_reported(tmp_path, caplog):
    pw = FakePW(_wrong(), refuse=True)
    with caplog.at_level(logging.WARNING, logger="littlebot.audio.devices"):
        route = ec_route.ensure(pw, _drop_in(tmp_path))
    assert route is not None and not route.ok
    assert "couldn't be moved" in caplog.text


def test_no_canceller_or_no_pw_link_is_none(tmp_path):
    assert ec_route.check(FakePW(set(), canceller=False), _drop_in(tmp_path)) is None
    assert ec_route.check(lambda args: None, _drop_in(tmp_path)) is None


def test_the_drop_ins_speaker_is_read_from_playback_props_only(tmp_path):
    assert ec_route.configured_speaker(_drop_in(tmp_path)) == SPK
    assert ec_route.configured_speaker(tmp_path / "missing.conf") == ""


def test_parse_links_reads_only_where_outputs_go():
    text = FakePW(_wrong())(["-l"])
    links = ec_route.parse_links(text or "")
    assert links["littlebot_ec_playback:output_FL"] == [f"{MIC_OUT}:playback_FL"]
    assert all(not port.startswith(MIC_OUT) for port in links)  # "|<-" lines are not outputs


def test_ensure_soon_does_nothing_without_pw_link(monkeypatch):
    monkeypatch.setattr(ec_route.shutil, "which", lambda name: None)
    started = []
    monkeypatch.setattr(ec_route.threading, "Thread", lambda **kw: started.append(kw))
    ec_route.ensure_soon()
    assert started == []


def test_the_command_line_keeps_the_device_names(monkeypatch, capsys):
    pw = FakePW(_wrong())
    monkeypatch.setattr(ec_route, "_pw_link", pw)
    monkeypatch.setattr(ec_route, "DROP_IN", Path("/nonexistent"))
    monkeypatch.setattr(ec_route.check, "__defaults__", (pw, Path("/nonexistent")))
    monkeypatch.setattr(ec_route.ensure, "__defaults__", (pw, Path("/nonexistent")))
    assert ec_route.main([]) == 2
    out = capsys.readouterr().out
    assert out.startswith(f"The echo canceller plays on {MIC_OUT}, not on the speaker {SPK}.")
    assert "--fix" in out
    assert ec_route.main(["--fix"]) == 0
    assert pw.links == _right()
