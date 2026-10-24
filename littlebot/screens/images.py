"""Pictures for info cards, fetched from the web when a card asks and never kept.

``fetch(ctx, query="rainbow")`` returns a :class:`WebPicture` at once (``ready`` False); a daemon
thread looks for a picture and fills in ``surface`` (a pygame.Surface of at most :data:`MAX_SIZE`) and
``credit`` ("Wikipedia"), or leaves ``surface`` None when there is none. The display never waits.

Where a picture of ``query`` comes from, in order (the first that gives a usable image wins):

    1. Wikipedia   GET https://en.wikipedia.org/api/rest_v1/page/summary/<query> -> ``thumbnail``; when
                   there is no such article (or it is a disambiguation page) the title comes from
                   GET https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=<query>
    2. Commons     GET https://commons.wikimedia.org/w/api.php?action=query&generator=search
                   &gsrsearch=<query> filetype:bitmap&gsrnamespace=6&prop=imageinfo&iiurlwidth=400
    3. Openverse   GET https://api.openverse.org/v1/images/?q=<query>&mature=false (no key) -> ``thumbnail``

``fetch(ctx, url="https://...")`` loads that one image instead (credit: its site's name).

Rules: nothing is fetched in offline mode, while cloud communication is off in Privacy, or with the
parent's ``screens.web_pictures`` setting off (the picture is ready at once, without a surface). The
query must pass the content rules (littlebot/safety/content.py ``find_blocked``) and an explicit-term
guard, and so must the title of what was found. Only https, at most :data:`MAX_BYTES` per image,
only JPEG, PNG or WebP (WebP when this pygame can decode it), short timeouts and a deadline for the
whole search. Decoded pictures are kept in a small in-memory LRU (:data:`CACHE_ITEMS`) so a card shown
again doesn't fetch again; nothing is ever written to disk, and the picture is never logged (one
INFO line per fetch: the source, how long it took, its size).
"""

from __future__ import annotations

import io
import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("littlebot.screens.images")

WIKI_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
COMMONS_URL = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_URL = "https://api.openverse.org/v1/images/"

MAX_BYTES = 2_000_000  # per image download
MAX_SIZE = (360, 300)  # decoded pictures are scaled down to fit this (cards scale them into their box)
CACHE_ITEMS = 16  # decoded pictures kept in memory (at most ~430 KB each)
MAX_IN_FLIGHT = 3  # fetches at once; more are refused (the card shows no picture)
TIMEOUT = httpx.Timeout(4.0, connect=2.5)
SLOW_TIMEOUT = httpx.Timeout(7.0, connect=2.5)  # Openverse's search often takes 3-6 s
DEADLINE_S = 12.0  # the whole search for one picture (the voice talks for longer than that)
MAX_QUERY = 100
CANDIDATES_PER_SOURCE = 3  # image URLs tried from Commons / Openverse before moving on

# Content types accepted, and what each must start with (a mislabelled file is skipped).
TYPES: dict[str, tuple[str, tuple[bytes, ...]]] = {
    "image/jpeg": ("x.jpg", (b"\xff\xd8\xff",)),
    "image/png": ("x.png", (b"\x89PNG\r\n\x1a\n",)),
    "image/webp": ("x.webp", (b"RIFF",)),
}

# Never fetch a picture for these (on top of the conversation rules): sexual content, nudity, gore.
_EXPLICIT = re.compile(
    r"\b(?:porn\w*|p0rn|xxx|nsfw|nude[sd]?|nudity|nudist\w*|naked|sex|sexy|sexual\w*|erotic\w*|hentai|fetish\w*"
    r"|lingerie|topless|bottomless|boobs?|titties|penis\w*|vagina\w*|genital\w*|orgasm\w*|strip(?:per|tease)\w*"
    r"|onlyfans|playboy|camgirl\w*|escort\w*|gore|gory|gruesome|corpses?|cadavers?|dead bod(?:y|ies)"
    r"|decapitat\w*|behead\w*|mutilat\w*|dismember\w*|autopsy|suicide\w*|self[- ]?harm\w*|bloody|bloodied"
    r"|massacre\w*|lynch\w*|executions?|torture\w*|swastika\w*|nazi\w*)\b")
_SQUASHED = ("porn", "hentai", "nsfw", "xxx", "onlyfans")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9@$ -]+", " ", t)


# Names of ordinary things that contain a guarded word (an animal, not nudity); taken out before both checks.
_HARMLESS = re.compile(r"\bnaked mole[- ]?rats?\b", re.IGNORECASE)


def explicit(text: str) -> bool:
    """True when ``text`` names something a picture must never show (the explicit-term guard)."""
    folded = _HARMLESS.sub(" ", _fold(text))
    for variant in {folded, folded.translate(_LEET)}:
        if _EXPLICIT.search(variant):
            return True
        squashed = re.sub(r"[^a-z]", "", variant)
        if any(term in squashed for term in _SQUASHED):
            return True
    return False


def unsafe(text: str) -> bool:
    """The safety screen for a picture query or a found picture's title."""
    if not text:
        return False
    if explicit(text):
        return True
    from littlebot.safety.content import find_blocked

    rest = _HARMLESS.sub(" ", text)
    return bool(rest.strip()) and bool(find_blocked(rest))


# ---------------------------------------------------------------- the picture
class WebPicture:
    """A picture being fetched (the ``base.Picture`` protocol). ``reason`` says why there is none."""

    def __init__(self, *, surface: Any = None, credit: str = "", source: str = "", reason: str = "",
                 ready: bool = False) -> None:
        self._event = threading.Event()
        self._surface = surface
        self._credit = credit
        self.source = source  # "wikipedia" | "commons" | "openverse" | "url" | "cache" | ""
        self.reason = reason  # "" | "offline" | "cloud_off" | "setting_off" | "unsafe" | "busy" | "none" | ...
        if ready:
            self._event.set()

    @property
    def ready(self) -> bool:
        return self._event.is_set()

    @property
    def surface(self) -> Any:
        return self._surface if self._event.is_set() else None

    @property
    def credit(self) -> str:
        return self._credit if self._event.is_set() else ""

    def wait(self, timeout: float | None = None) -> bool:
        """Block until ready (tests and scripts only; cards poll ``ready``)."""
        return self._event.wait(timeout)

    def _finish(self, surface: Any = None, credit: str = "", source: str = "", reason: str = "") -> None:
        self._surface, self._credit, self.source, self.reason = surface, credit, source, reason
        self._event.set()

    def __repr__(self) -> str:
        state = "loading" if not self.ready else ("ok" if self._surface is not None else self.reason or "none")
        return f"<WebPicture {self.source or '-'} {state}>"


def _none(reason: str) -> WebPicture:
    return WebPicture(reason=reason, ready=True)


# ---------------------------------------------------------------- cache and in-flight
_LOCK = threading.Lock()
_CACHE: OrderedDict[str, tuple[Any, str, str]] = OrderedDict()  # key -> (surface, credit, source)
_IN_FLIGHT: dict[str, WebPicture] = {}


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _cache_get(key: str) -> tuple[Any, str, str] | None:
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
        return hit


def _cache_put(key: str, value: tuple[Any, str, str]) -> None:
    with _LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_ITEMS:
            _CACHE.popitem(last=False)


# ---------------------------------------------------------------- gates
def blocked_reason(ctx: Any) -> str | None:
    """Why no picture may be fetched now ("offline", "cloud_off", "setting_off"), or None."""
    cfg = getattr(ctx, "config", None)
    if cfg is not None and getattr(cfg, "is_offline", False):
        return "offline"
    privacy = getattr(ctx, "privacy", None)
    if privacy is None or not privacy.cloud_enabled:
        return "cloud_off"
    settings = getattr(ctx, "settings", None)
    if settings is not None and not settings.get("screens.web_pictures", True):
        return "setting_off"
    return None


# Wikimedia's robot policy (https://w.wiki/4wJS) wants a way to reach whoever runs a client: without one
# (and naming httpx) the robot's requests got HTTP 403 "Please respect our robot policy".
CONTACT_URL = "https://github.com/ali-sial-786/littlebot"


def _user_agent() -> str:
    from littlebot import __version__

    return f"LittleBot/{__version__} (home companion robot; {CONTACT_URL})"


def fetch(ctx: Any, *, query: str = "", url: str = "",
          transport: httpx.BaseTransport | None = None) -> WebPicture:
    """A picture of ``query`` (or the image at ``url``), fetched in the background. Never blocks."""
    query = " ".join(str(query or "").split())[:MAX_QUERY]
    url = str(url or "").strip()[:500]
    if not query and not url:
        return _none("empty")
    reason = blocked_reason(ctx)
    if reason:
        log.debug("images: not fetched (%s)", reason)
        return _none(reason)
    if url and not url.lower().startswith("https://"):
        log.info("images: not fetched (not https)")
        return _none("not_https")
    if unsafe(query) or (url and explicit(url)):
        log.info("images: not fetched (the query didn't pass the safety screen)")
        return _none("unsafe")
    key = f"url:{url}" if url else f"q:{query.casefold()}"
    hit = _cache_get(key)
    if hit is not None:
        log.debug("images: %s picture from memory", hit[2])
        return WebPicture(surface=hit[0], credit=hit[1], source="cache", ready=True)
    with _LOCK:
        pending = _IN_FLIGHT.get(key)
        if pending is not None:
            return pending
        if len(_IN_FLIGHT) >= MAX_IN_FLIGHT:
            log.info("images: not fetched (%d already loading)", len(_IN_FLIGHT))
            return _none("busy")
        pic = _IN_FLIGHT[key] = WebPicture()
    threading.Thread(target=_work, args=(pic, key, query, url, transport), name="littlebot-images",
                     daemon=True).start()
    return pic


# ---------------------------------------------------------------- the work (a daemon thread)
class _Skip(Exception):
    """This candidate (or source) gave nothing usable: try the next."""


def _https_only(request: httpx.Request) -> None:
    if request.url.scheme != "https":
        raise _Skip("not https")


def _work(pic: WebPicture, key: str, query: str, url: str, transport: httpx.BaseTransport | None) -> None:
    t0 = time.monotonic()
    result: tuple[Any, str, str, int] | None = None
    reason = "none"
    try:
        with httpx.Client(transport=transport, follow_redirects=True, max_redirects=3, timeout=TIMEOUT,
                          headers={"User-Agent": _user_agent(), "Accept": "application/json"},
                          event_hooks={"request": [_https_only]}) as client:
            deadline = t0 + DEADLINE_S
            result = _search(client, query, url, deadline)
            if result is None and time.monotonic() > deadline:
                reason = "timeout"
    except Exception:  # a picture that can't be fetched is simply not shown
        log.debug("images: fetch failed", exc_info=True)
        reason = "failed"
    ms = (time.monotonic() - t0) * 1000
    if result is not None:
        surface, credit, source, nbytes = result
        _cache_put(key, (surface, credit, source))
        w, h = surface.get_size()
        log.info("images: %s picture in %.0f ms, %dx%d, %d KB", source, ms, w, h, round(nbytes / 1024))
        pic._finish(surface, credit, source)
    else:
        log.info("images: no picture (%s) in %.0f ms", reason, ms)
        pic._finish(reason=reason)
    with _LOCK:
        _IN_FLIGHT.pop(key, None)


def _search(client: httpx.Client, query: str, url: str,
            deadline: float) -> tuple[Any, str, str, int] | None:
    """(surface, credit, source, bytes downloaded) of the first usable picture, or None."""
    if url:
        host = httpx.URL(url).host.removeprefix("www.")
        got = _try_image(client, url, deadline)
        return (got[0], host[:40], "url", got[1]) if got is not None else None
    sources: tuple[tuple[str, Callable[[httpx.Client, str], Iterator[tuple[str, str]]]], ...] = (
        ("wikipedia", _wikipedia), ("commons", _commons), ("openverse", _openverse))
    for source, candidates in sources:
        try:
            for image_url, credit in candidates(client, query):
                if time.monotonic() > deadline:
                    return None
                got = _try_image(client, image_url, deadline)
                if got is not None:
                    return got[0], credit, source, got[1]
        except (_Skip, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.debug("images: %s gave nothing (%s)", source, e if isinstance(e, _Skip) else type(e).__name__)
        if time.monotonic() > deadline:
            return None
    return None


def _get_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None, *,
              timeout: httpx.Timeout | None = None, missing_ok: bool = False) -> Any:
    resp = client.get(url, params=params, timeout=timeout or TIMEOUT)
    if missing_ok and resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise _Skip(f"HTTP {resp.status_code}")
    return resp.json()


def _wiki_thumb(client: httpx.Client, title: str) -> str | None:
    """The thumbnail of the Wikipedia article ``title`` (redirects followed), or None."""
    if not title or unsafe(title):
        return None
    summary = _get_json(client, WIKI_SUMMARY_URL + quote(title.replace(" ", "_"), safe=""), missing_ok=True)
    if not isinstance(summary, dict) or summary.get("type") == "disambiguation":
        return None
    if unsafe(str(summary.get("title") or "")) or unsafe(str(summary.get("description") or "")):
        return None
    thumb = (summary.get("thumbnail") or {}).get("source")
    return str(thumb) if thumb else None


def _wikipedia(client: httpx.Client, query: str) -> Iterator[tuple[str, str]]:
    """The article called ``query`` itself first (one request), else the best search hits."""
    thumb = _wiki_thumb(client, query[:1].upper() + query[1:])
    if thumb:
        yield thumb, "Wikipedia"
        return
    found = _get_json(client, WIKI_SEARCH_URL, {
        "action": "query", "list": "search", "srsearch": query, "srlimit": 2, "srnamespace": 0, "srprop": "",
        "format": "json", "formatversion": 2})
    for hit in (found.get("query") or {}).get("search") or []:
        thumb = _wiki_thumb(client, str(hit.get("title") or ""))
        if thumb:
            yield thumb, "Wikipedia"
            return


def _commons(client: httpx.Client, query: str) -> Iterator[tuple[str, str]]:
    found = _get_json(client, COMMONS_URL, {
        "action": "query", "generator": "search", "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
        "gsrlimit": 6, "prop": "imageinfo", "iiprop": "url|mime|size", "iiurlwidth": 400, "format": "json",
        "formatversion": 2})
    pages = sorted((found.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 0))
    n = 0
    for page in pages:
        title = str(page.get("title") or "")
        info = (page.get("imageinfo") or [{}])[0]
        if unsafe(title) or info.get("mime") not in TYPES:
            continue
        thumb = info.get("thumburl") or info.get("url")
        if thumb:
            yield str(thumb), "Wikimedia Commons"
            n += 1
            if n >= CANDIDATES_PER_SOURCE:
                return


def _openverse(client: httpx.Client, query: str) -> Iterator[tuple[str, str]]:
    found = _get_json(client, OPENVERSE_URL, {"q": query, "mature": "false", "page_size": 6},
                      timeout=SLOW_TIMEOUT)
    n = 0
    for item in found.get("results") or []:
        if item.get("mature"):
            continue
        tags = " ".join(str(t.get("name") or "") for t in (item.get("tags") or [])[:12] if isinstance(t, dict))
        if unsafe(str(item.get("title") or "")) or explicit(tags):
            continue
        image_url = item.get("thumbnail") or item.get("url")
        if not image_url:
            continue
        creator = " ".join(str(item.get("creator") or "").split())[:24]
        lic = str(item.get("license") or "").upper()[:8]
        bits = [b for b in (creator, f"CC {lic}" if lic and lic not in ("PDM", "CC0") else lic) if b]
        yield str(image_url), ("Openverse: " + ", ".join(bits)) if bits else "Openverse"
        n += 1
        if n >= CANDIDATES_PER_SOURCE:
            return


def _try_image(client: httpx.Client, url: str, deadline: float) -> tuple[Any, int] | None:
    """The decoded, scaled-down image at ``url`` and its download size, or None (wrong type, too big,
    can't decode)."""
    try:
        data, ctype = _download(client, url, deadline)
        return _decode(data, ctype), len(data)
    except (_Skip, httpx.HTTPError) as e:
        log.debug("images: skipped a candidate (%s)", e if isinstance(e, _Skip) else type(e).__name__)
        return None


def _download(client: httpx.Client, url: str, deadline: float) -> tuple[bytes, str]:
    if not url.lower().startswith("https://"):
        raise _Skip("not https")
    with client.stream("GET", url, headers={"Accept": "*/*"}) as resp:  # (checked below)
        if resp.status_code != 200:
            raise _Skip(f"HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in TYPES:
            raise _Skip(f"type {ctype[:30]}")
        try:
            if int(resp.headers.get("content-length") or 0) > MAX_BYTES:
                raise _Skip("too big")
        except ValueError:
            pass
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > MAX_BYTES:
                raise _Skip("too big")
            if time.monotonic() > deadline:
                raise _Skip("deadline")
    return bytes(buf), ctype


def _decode(data: bytes, ctype: str) -> Any:
    name, magic = TYPES[ctype]
    if not any(data.startswith(m) for m in magic) or (ctype == "image/webp" and data[8:12] != b"WEBP"):
        raise _Skip("content doesn't match its type")
    import pygame

    try:
        img = pygame.image.load(io.BytesIO(data), name)
    except (pygame.error, ValueError) as e:  # e.g. WebP on a pygame built without it
        raise _Skip(f"can't decode {ctype}") from e
    w, h = img.get_size()
    if w < 16 or h < 16:
        raise _Skip("too small")
    if img.get_bitsize() not in (24, 32):  # palette PNGs: smoothscale needs 24/32 bits (no display needed)
        full = pygame.Surface((w, h), pygame.SRCALPHA, 32)
        full.blit(img, (0, 0))
        img = full
    k = min(1.0, MAX_SIZE[0] / w, MAX_SIZE[1] / h)
    if k < 1.0:
        img = pygame.transform.smoothscale(img, (max(1, round(w * k)), max(1, round(h * k))))
    return img


__all__ = ["CACHE_ITEMS", "MAX_BYTES", "MAX_SIZE", "WebPicture", "blocked_reason", "clear_cache", "explicit",
           "fetch", "unsafe"]
