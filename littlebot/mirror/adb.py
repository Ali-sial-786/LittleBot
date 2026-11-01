"""adb for Android mirroring: checking what a grown-up typed, running adb, and reading what it prints.

Only argv lists are ever run (never a shell), and only after :func:`check_address` / :func:`check_code` accepted
the input: a pairing address is an IPv4 address and a port ("192.168.1.23:37099"), a pairing code six digits. The
code goes to ``adb pair`` on its standard input (adb asks for it when it isn't on the command line), so it is
never in a process list or a log.

What adb prints (adb 34, Debian trixie's ``adb`` package)::

    adb pair 192.168.1.23:37099        Enter pairing code: Successfully paired to 192.168.1.23:37099 [guid=adb-R5CT-xyz]
    adb mdns services                  List of discovered mdns services
                                       adb-R5CT-xyz	_adb-tls-connect._tcp	192.168.1.23:41235
    adb connect 192.168.1.23:41235     connected to 192.168.1.23:41235   (or "already connected to ...", "failed ...")
    adb devices                        List of devices attached
                                       192.168.1.23:41235	device
                                       adb-R5CT-xyz._adb-tls-connect._tcp	device   (adb's own mDNS auto-connect)
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from typing import Awaitable, Callable, Sequence

# (argv, timeout seconds, standard input) -> (exit code, what it printed: stdout and stderr together)
Runner = Callable[[Sequence[str], float, "bytes | None"], Awaitable[tuple[int, str]]]

ADDRESS_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})$")
CODE_RE = re.compile(r"^\d{6}$")
PAIRED_RE = re.compile(r"Successfully paired to (\S+)(?: \[guid=([\w.\-]+)\])?")
MDNS_RE = re.compile(r"^(\S+)\s+_adb-tls-connect\._tcp\.?\s+(\d{1,3}(?:\.\d{1,3}){3}:\d{1,5})\s*$")
DEVICE_RE = re.compile(r"^(\S+)\s+(device|offline|unauthorized|connecting|authorizing|unknown)\b")
GUID_RE = re.compile(r"^[\w.\-]{1,80}$")
MAX_OUTPUT = 20_000


def check_address(text: object) -> str | None:
    """ "192.168.1.23:37099" (the phone's "IP address & Port" under Wireless debugging), or None."""
    m = ADDRESS_RE.fullmatch(str(text or "").strip())
    if m is None:
        return None
    octets = [int(x) for x in m.groups()[:4]]
    port = int(m.group(5))
    if any(o > 255 for o in octets) or not 1 <= port <= 65535 or octets[0] == 0:
        return None
    return f"{'.'.join(str(o) for o in octets)}:{port}"


def check_code(text: object) -> str | None:
    """The six-digit Wi-Fi pairing code, or None."""
    code = str(text or "").strip()
    return code if CODE_RE.fullmatch(code) else None


def parse_pair(output: str) -> tuple[bool, str]:
    """(paired, guid) from ``adb pair``'s output (guid "" when adb didn't say one)."""
    m = PAIRED_RE.search(output or "")
    if m is None:
        return False, ""
    guid = m.group(2) or ""
    return True, guid if GUID_RE.match(guid) else ""


def parse_mdns(output: str) -> list[tuple[str, str]]:
    """[(service name, "ip:port")] of the phones adb's mDNS sees offering a (paired) wireless connection."""
    out = []
    for line in (output or "").splitlines():
        m = MDNS_RE.match(line.strip())
        if m is not None and check_address(m.group(2)):
            out.append((m.group(1), m.group(2)))
    return out


def parse_devices(output: str) -> list[tuple[str, str]]:
    """[(serial, state)] from ``adb devices``."""
    out = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        m = DEVICE_RE.match(line)
        if m is not None:
            out.append((m.group(1), m.group(2)))
    return out


def is_wifi(serial: str) -> bool:
    """A phone on the Wi-Fi ("ip:port", or adb's own mDNS name), not one on a USB cable."""
    return "._adb-tls-connect." in serial or check_address(serial) is not None


def connected(output: str) -> bool:
    low = (output or "").lower()
    return "connected to" in low and "failed" not in low and "cannot" not in low and "unable" not in low


async def default_runner(argv: Sequence[str], timeout: float, stdin: bytes | None = None) -> tuple[int, str]:
    """Run ``argv`` (no shell) and return (exit code, output). Killed after ``timeout`` seconds (code 124)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except (OSError, ValueError) as e:
        return 127, str(e)[:200]
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 2.0)
        except (asyncio.TimeoutError, ValueError):
            out = b""
        return 124, (out or b"")[:MAX_OUTPUT].decode("utf-8", "replace")
    return int(proc.returncode or 0), (out or b"")[:MAX_OUTPUT].decode("utf-8", "replace")


__all__ = ["Runner", "check_address", "check_code", "connected", "default_runner", "is_wifi", "parse_devices",
           "parse_mdns", "parse_pair"]
