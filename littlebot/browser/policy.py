"""Where LittleBot's browser view may go: https only, SafeSearch forced on, nothing local or internal.

* ``google_url(query)``     - Google results with SafeSearch on (``safe=active``); no query: Google's start page
* ``check(url)``            - a ``Verdict``: "ok", "rewrite" (a search engine without its safe-search parameter,
                              ``url`` is the same address with it) or "block" (``why`` says why, for the log)
* ``host_of(url)``          - the host, for logs and the dashboard (never the path or the query)
* ``query_blocked(query)``  - the safety rules (littlebot/safety/content.py ``find_blocked``) a query breaks

Checked by the browser service on every page load the browser asks for (DevTools ``Fetch.requestPaused``) and
again on every page it shows (``Page.frameNavigated``), so a page can't drop SafeSearch or leave https.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

GOOGLE = "https://www.google.com"
BING = "https://www.bing.com"  # when Google asks for a captcha ("unusual traffic"): its results, SafeSearch strict
MAX_QUERY = 200  # characters sent to Google
MAX_URL = 4096

# Search engines a result page can lead to, and how each is told to filter its results.
# Google: safe=active ("SafeSearch: filter"); Bing: adlt=strict; DuckDuckGo: kp=1 (strict).
_GOOGLE_HOST = re.compile(r"^(?:www\.|images\.|news\.)?google\.(?:com|[a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})$")
_SAFE_PARAMS: tuple[tuple[re.Pattern[str], tuple[str, ...], str, str], ...] = (
    (_GOOGLE_HOST, ("/search", "/webhp", "/images", "/imghp", "/"), "safe", "active"),
    (re.compile(r"^(?:www\.)?bing\.com$"), ("/search", "/images/search", "/videos/search"), "adlt", "strict"),
    (re.compile(r"^(?:html\.|lite\.)?duckduckgo\.com$"), ("/", "/html", "/html/", "/lite", "/lite/"), "kp", "1"),
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Verdict:
    action: str  # ok | rewrite | block
    url: str = ""  # rewrite: the address to load instead
    why: str = ""  # block: for the log and the page that says so


OK = Verdict("ok")


def clean_query(query: Any) -> str:
    """The query as it is sent: one line, single spaces, at most MAX_QUERY characters."""
    q = " ".join(_CONTROL.sub(" ", str(query or "")).split())
    return q[:MAX_QUERY].strip()


def google_url(query: str | None = None) -> str:
    """Google with SafeSearch on: the results for ``query``, or the start page."""
    q = clean_query(query)
    if not q:
        return f"{GOOGLE}/?safe=active"
    return f"{GOOGLE}/search?q={quote_plus(q)}&safe=active"


def bing_url(query: str | None = None) -> str:
    """Bing with SafeSearch strict: the results for ``query``, or the start page."""
    q = clean_query(query)
    if not q:
        return f"{BING}/?adlt=strict"
    return f"{BING}/search?q={quote_plus(q)}&adlt=strict"


def is_google_captcha(url: str) -> bool:
    """``url`` is Google's "unusual traffic" page (a captcha: www.google.com/sorry/...)."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return False
    return bool(_GOOGLE_HOST.match((parts.hostname or "").lower())) and parts.path.startswith("/sorry")


def host_of(url: str) -> str:
    try:
        return (urlsplit(str(url)).hostname or "")[:120]
    except ValueError:
        return ""


def check(url: str, *, insecure_hosts: Iterable[str] = ()) -> Verdict:
    """May the browser load ``url``? ``insecure_hosts``: hosts allowed over plain http (tests' local pages)."""
    url = str(url or "")
    if len(url) > MAX_URL:
        return Verdict("block", why="address too long")
    try:
        parts = urlsplit(url)
    except ValueError:
        return Verdict("block", why="not an address")
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme == "http" and host and host in set(insecure_hosts):
        return OK
    if scheme != "https":
        return Verdict("block", why=f"not https ({scheme or 'no scheme'})")
    if not host or parts.username or parts.password:
        return Verdict("block", why="no host" if not host else "an address with a password")
    if host in ("localhost",) or host.endswith(".local") or _private_ip(host):
        return Verdict("block", why="a local address")
    safe = _with_safe_search(parts, host)
    if safe is not None:
        return Verdict("rewrite", url=safe)
    return OK


def _private_ip(host: str) -> bool:
    import ipaddress  # noqa: PLC0415

    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
        or ip.is_unspecified


def _with_safe_search(parts: Any, host: str) -> str | None:
    """The same address with the search engine's safe-search parameter, when it is missing or wrong; else None."""
    for pattern, paths, key, value in _SAFE_PARAMS:
        if not pattern.match(host):
            continue
        path = parts.path or "/"
        if path not in paths:
            return None
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if [v for k, v in pairs if k == key] == [value]:
            return None
        pairs = [(k, v) for k, v in pairs if k != key] + [(key, value)]
        return urlunsplit((parts.scheme, parts.netloc, path, urlencode(pairs), parts.fragment))
    return None


def is_safe_search(url: str) -> bool:
    """``url`` is a search engine page with its safe-search parameter on (or not a search engine page at all)."""
    verdict = check(url)
    return verdict.action == "ok"


def query_blocked(query: str, relaxed: Iterable[str] = ()) -> list[str]:
    """The safety rules ``query`` breaks ([] when it is fine), as for anything LittleBot says."""
    from littlebot.safety.content import find_blocked  # noqa: PLC0415

    allow = set(relaxed or ())
    return [c for c in find_blocked(query) if c not in allow]


__all__ = ["BING", "GOOGLE", "OK", "Verdict", "bing_url", "check", "clean_query", "google_url", "host_of",
           "is_google_captcha", "is_safe_search", "query_blocked"]
