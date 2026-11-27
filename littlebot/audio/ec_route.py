"""Where PipeWire's echo canceller really plays: the robot's USB speaker, or somewhere nobody hears it.

``scripts/setup_echo_cancel.sh`` pins the canceller's output stream (``littlebot_ec_playback``) to the USB speaker
with ``target.object``, but WirePlumber links it elsewhere when the speaker isn't there yet as PipeWire starts, and
leaves it there once it is. On the robot (2026-10-31, WirePlumber 0.5.8, after the USB plugs were swapped) it played
into the ReSpeaker Lite microphone's own headphone socket: LittleBot talked, its face moved, nothing was heard, and
every check passed.

``check()`` reads the links (``pw-link -l``) and says where the canceller plays and where it should (the speaker the
drop-in names, else the USB output that isn't the microphone's); ``ensure()`` moves it there when it is wrong: the
wrong links are removed and the right ones made with ``pw-link`` (they last until PipeWire restarts, when LittleBot
checks again). LittleBot runs ``ensure()`` when its audio starts and at every conversation.

By hand: ``.venv/bin/python -m littlebot.audio.ec_route`` (where it plays) or ``... --fix``.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("littlebot.audio.devices")

EC_PLAYBACK = "littlebot_ec_playback"
EC_CAPTURE = "littlebot_ec_capture"
DROP_IN = Path.home() / ".config/pipewire/pipewire.conf.d/60-littlebot-echo-cancel.conf"
TIMEOUT_S = 5.0
SOON_EVERY_S = 3.0  # ensure_soon(): at most one check this often (the audio service and a call open together)

Run = Callable[[list[str]], "str | None"]


@dataclasses.dataclass
class Route:
    playing_on: list[str]  # the nodes the canceller's sound goes to now (node.name)
    speaker: str  # where it should go ("" when no USB speaker was found)
    mic: str = ""  # where the canceller listens
    outputs: list[str] = dataclasses.field(default_factory=list)  # the canceller's output ports
    speaker_ports: list[str] = dataclasses.field(default_factory=list)  # the speaker's input ports

    @property
    def ok(self) -> bool:
        if self.speaker:
            return self.playing_on == [self.speaker]
        # no USB speaker: anywhere but the microphone's own socket (nobody hears that)
        return bool(self.playing_on) and not any(self.mic and device_of(n) == device_of(self.mic)
                                                 for n in self.playing_on)

    def describe(self) -> str:
        now = ", ".join(self.playing_on) or "nothing"
        if self.ok:
            return f"the echo canceller plays on {now}"
        return f"the echo canceller plays on {now}, not on the speaker {self.speaker or '(no USB speaker found)'}"


def _pw_link(args: list[str]) -> str | None:
    """``pw-link <args>``'s output, or None when it isn't there or fails."""
    exe = shutil.which("pw-link")
    if exe is None:
        return None
    try:
        res = subprocess.run([exe, *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def parse_links(text: str) -> dict[str, list[str]]:
    """``pw-link -l``: each output port and the input ports it feeds."""
    links: dict[str, list[str]] = {}
    port = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            port = line.strip()
            continue
        m = re.match(r"\s*\|->\s*(\S.*)$", line)
        if m and port:
            links.setdefault(port, []).append(m.group(1).strip())
    return links


def node_of(port: str) -> str:
    return port.rsplit(":", 1)[0]


def channel_of(port: str) -> str:
    return port.rsplit(":", 1)[-1].rsplit("_", 1)[-1]  # output_FL -> FL, playback_MONO -> MONO


def device_of(node: str) -> str:
    """alsa_output.usb-X-00.analog-stereo and alsa_input.usb-X-00.analog-stereo -> usb-X-00 (the same USB device)."""
    rest = node.split(".", 1)[1] if node.startswith(("alsa_output.", "alsa_input.")) else node
    return rest.rsplit(".", 1)[0]


def configured_speaker(path: Path = DROP_IN) -> str:
    """The speaker the drop-in pins the canceller's playback to (``target.object`` in ``playback.props``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"playback\.props\s*=\s*\{[^}]*?target\.object\s*=\s*\"([^\"]+)\"", text, re.S)
    return m.group(1) if m else ""


def check(run: Run = _pw_link, drop_in: Path = DROP_IN) -> Route | None:
    """Where the canceller plays and where it should; None when there is no canceller (or no ``pw-link``)."""
    outs, ins, linked = run(["-o"]), run(["-i"]), run(["-l"])
    if outs is None or ins is None or linked is None:
        return None
    outputs = [p.strip() for p in outs.splitlines() if node_of(p.strip()) == EC_PLAYBACK]
    if not outputs:
        return None
    links = parse_links(linked)
    playing_on = sorted({node_of(i) for p in outputs for i in links.get(p, [])})
    mic = next((node_of(p) for p, to in links.items() if any(node_of(i) == EC_CAPTURE for i in to)), "")
    sink_ports: dict[str, list[str]] = {}
    for p in (line.strip() for line in ins.splitlines()):
        if p.startswith("alsa_output.") and ":playback_" in p:
            sink_ports.setdefault(node_of(p), []).append(p)
    speaker = configured_speaker(drop_in)
    if speaker not in sink_ports:  # not named, or not plugged in: the USB output that isn't the microphone's
        usb = sorted(n for n in sink_ports if n.startswith("alsa_output.usb-"))
        apart = [n for n in usb if not mic or device_of(n) != device_of(mic)]
        speaker = (apart or ([] if mic else usb) or [""])[0]
    return Route(playing_on, speaker, mic, outputs, sink_ports.get(speaker, []))


def _wanted(out: str, speaker_ports: list[str]) -> list[str]:
    """The speaker's ports an output port goes to: the same channel, or a mono output to every channel."""
    by_ch = {channel_of(p): p for p in speaker_ports}
    ch = channel_of(out)
    if ch in by_ch:
        return [by_ch[ch]]
    if ch == "MONO":
        return sorted(speaker_ports)
    return [by_ch["MONO"]] if "MONO" in by_ch else []


def move(route: Route, run: Run = _pw_link) -> None:
    """Link the canceller's outputs to ``route.speaker`` only."""
    links = parse_links(run(["-l"]) or "")
    for out in route.outputs:
        want = _wanted(out, route.speaker_ports)
        for to in links.get(out, []):
            if to not in want:
                run(["-d", out, to])
        for to in want:
            if to not in links.get(out, []):
                run([out, to])


def ensure(run: Run = _pw_link, drop_in: Path = DROP_IN) -> Route | None:
    """Check where the canceller plays, and move it to the speaker when it plays anywhere else (logged)."""
    route = check(run, drop_in)
    if route is None or route.ok:
        return route
    if not route.speaker or not route.speaker_ports:
        log.warning("audio: %s; nothing to move it to: run scripts/setup_echo_cancel.sh --check", route.describe())
        return route
    move(route, run)
    after = check(run, drop_in)
    if after is not None and after.ok:
        log.warning("audio: %s: moved it back to the speaker", route.describe())
        return after
    log.warning("audio: %s, and it couldn't be moved: run scripts/setup_echo_cancel.sh --check",
                (after or route).describe())
    return after or route


_soon_lock = threading.Lock()
_soon_at = -1e9


def ensure_soon() -> None:
    """``ensure()`` in the background (a few milliseconds of ``pw-link``), at most once per ``SOON_EVERY_S``."""
    global _soon_at
    with _soon_lock:
        if time.monotonic() - _soon_at < SOON_EVERY_S or shutil.which("pw-link") is None:
            return
        _soon_at = time.monotonic()

    def job() -> None:
        try:
            ensure()
        except Exception:  # noqa: BLE001 - a check that fails changes nothing
            log.debug("audio: checking the echo canceller's speaker failed", exc_info=True)

    threading.Thread(target=job, name="littlebot-ec-route", daemon=True).start()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    route = ensure() if "--fix" in argv else check()
    if route is None:
        print("No echo canceller (littlebot_ec_playback) in PipeWire, or pw-link isn't installed.")
        return 1
    said = route.describe()
    print(said[:1].upper() + said[1:] + ".")  # not capitalize(): it lowercases the device names
    if not route.ok and "--fix" not in argv:
        print("Move it to the speaker: .venv/bin/python -m littlebot.audio.ec_route --fix")
    return 0 if route.ok else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
