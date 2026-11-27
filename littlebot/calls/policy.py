"""Where the WhatsApp window may go: pages on web.whatsapp.com, and nowhere else.

* ``check_page(url)``  - a page shown in the window itself (the top frame): only ``https://web.whatsapp.com/...``
* ``check_frame(url)`` - a page inside WhatsApp's page (a frame): WhatsApp's own hosts (*.whatsapp.com,
                         *.whatsapp.net) over https, and the empty ``about:`` frames pages make
* ``check_window(url)``- another window WhatsApp opens (a call's own window): the same as a page; an empty one
                         (``about:blank``) may stay until it goes somewhere

Each returns None (fine) or a few words for the log (never the address's path or query). Checked by the service on
every page load Chromium asks for (DevTools ``Fetch.requestPaused`` for documents) and again on every page shown
(``Page.frameNavigated``). Pictures, scripts and WhatsApp's connections (its own hosts) are not looked at: only
where a page comes from. A link in a chat to anywhere else is refused.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

HOME = "https://web.whatsapp.com/"
ORIGIN = "https://web.whatsapp.com"  # the only origin given the camera and the microphone
HOST = "web.whatsapp.com"
OWN_SUFFIXES = (".whatsapp.com", ".whatsapp.net")
MAX_URL = 4096


def _parts(url: str) -> tuple[str, str] | None:
    url = str(url or "")
    if len(url) > MAX_URL:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.username or parts.password:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port not in (None, 443):
        return None
    return parts.scheme.lower(), (parts.hostname or "").lower().rstrip(".")


def host_of(url: str) -> str:
    """The host, for logs (never the path or the query)."""
    try:
        return (urlsplit(str(url or "")).hostname or "")[:80]
    except ValueError:
        return ""


def _test_page(url: str, insecure_hosts: Iterable[str]) -> bool:
    """A test's own local page (``insecure_hosts``: hosts allowed over plain http; never set on the robot)."""
    hosts = set(insecure_hosts)
    if not hosts:
        return False
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in hosts


def check_page(url: str, *, insecure_hosts: Iterable[str] = ()) -> str | None:
    """May the window itself show ``url``? None: yes."""
    if _test_page(url, insecure_hosts):
        return None
    got = _parts(url)
    if got is None:
        return "not an address"
    scheme, host = got
    if scheme != "https":
        return f"not https ({scheme or 'no scheme'})"
    if host != HOST:
        return "not WhatsApp Web"
    return None


def check_frame(url: str, *, insecure_hosts: Iterable[str] = ()) -> str | None:
    """May a frame inside WhatsApp's page show ``url``? None: yes."""
    if str(url or "").startswith("about:") or _test_page(url, insecure_hosts):
        return None
    got = _parts(url)
    if got is None:
        return "not an address"
    scheme, host = got
    if scheme != "https":
        return f"not https ({scheme or 'no scheme'})"
    if host == HOST or host == "whatsapp.com" or host.endswith(OWN_SUFFIXES):
        return None
    return "not one of WhatsApp's own hosts"


def check_window(url: str, *, insecure_hosts: Iterable[str] = ()) -> str | None:
    """May another window WhatsApp opened show ``url``? None: yes (an empty one, or WhatsApp Web)."""
    if str(url or "") in ("", "about:blank"):
        return None
    return check_page(url, insecure_hosts=insecure_hosts)


__all__ = ["HOME", "HOST", "ORIGIN", "check_frame", "check_page", "check_window", "host_of"]
