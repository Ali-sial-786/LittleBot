"""Headlines for the briefing skill: the RSS / Atom feeds in ``skills.news_feeds`` (free, no key).

Titles only, with their source ("CBC", "BBC"): never the articles, never anything about the family sent anywhere
(a plain GET of each feed). Feeds are fetched together, each kept for ``CACHE_S`` (15 minutes); one answer waits
at most ``TIMEOUT_S`` for all of them, and a feed still loading carries on in the background so the next question
finds it cached. Nothing is fetched in offline mode or while cloud communication is off in Privacy.

Parsing is ``xml.etree`` on at most ``MAX_BYTES`` of a feed; a feed that declares entities is refused (no entity
expansion tricks). RSS 2.0, RSS 1.0 (RDF) and Atom all work.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Callable

import httpx

log = logging.getLogger("littlebot.skills.news")

CACHE_S = 15 * 60.0
TIMEOUT_S = 4.0  # one answer waits at most this long for the feeds
RETRY_S = 120.0  # a feed that failed is asked again after this long (answers stay fast meanwhile)
MAX_BYTES = 2_000_000
MAX_ITEMS = 25  # per feed
MAX_TITLE = 160
ERRORS = {
    "offline": "LittleBot is in offline mode, so it can't get the news",
    "cloud_off": "cloud communication is off in Privacy, so LittleBot can't get the news",
    "no_feeds": "no news feeds are set up (skills.news_feeds)",
    "unavailable": "the news feeds couldn't be reached right now",
}
# Feed hosts -> a short source name for the voice ("from the CBC" rather than "CBC | Top Stories News").
SOURCES = {
    "cbc.ca": "CBC", "bbc.co.uk": "BBC", "bbci.co.uk": "BBC", "bbc.com": "BBC", "theguardian.com": "The Guardian",
    "reuters.com": "Reuters", "npr.org": "NPR", "nytimes.com": "The New York Times", "cnn.com": "CNN",
    "globalnews.ca": "Global News", "ctvnews.ca": "CTV News", "thestar.com": "The Toronto Star",
    "thespec.com": "The Hamilton Spectator", "apnews.com": "AP", "aljazeera.com": "Al Jazeera",
    "theglobeandmail.com": "The Globe and Mail", "washingtonpost.com": "The Washington Post",
    "nasa.gov": "NASA", "techcrunch.com": "TechCrunch", "theverge.com": "The Verge", "arstechnica.com": "Ars Technica",
}
_STOP = frozenset("the a an and or of in on at to for about with news latest today what whats any some".split())


class NewsError(Exception):
    """Why there are no headlines: ``kind`` is a key of ERRORS; str() is a plain sentence."""

    def __init__(self, kind: str) -> None:
        self.kind = kind if kind in ERRORS else "unavailable"
        super().__init__(ERRORS[self.kind])


@dataclasses.dataclass(frozen=True)
class Headline:
    title: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "source": self.source}


# ---------------------------------------------------------------- parsing
def _local(tag: Any) -> str:
    """ "{http://www.w3.org/2005/Atom}entry" -> "entry" """
    return str(tag).rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


_TAGS = re.compile(r"<[^>]+>")


def clean_title(text: Any) -> str:
    """One tidy line: entities decoded, markup and newlines gone, at most MAX_TITLE characters."""
    s = html.unescape(_TAGS.sub(" ", html.unescape(str(text or ""))))
    s = " ".join(s.split())
    if len(s) > MAX_TITLE:
        s = s[:MAX_TITLE].rsplit(" ", 1)[0].rstrip(",;:-") + "..."
    return s


_SEP = re.compile(r"\s+[|\-–—:]\s+")
_PLAIN = frozenset("news top stories story latest headlines rss feed the home".split())


def source_name(url: str, channel_title: str = "") -> str:
    """A short name for the feed: its host's ("cbc.ca" -> "CBC"; "CBC | Hamilton News" -> "CBC Hamilton"), else
    its own title's first part, else the host."""
    try:
        host = (httpx.URL(url).host or "").lower()
    except (httpx.InvalidURL, TypeError, ValueError):
        host = ""
    parts = [p.strip() for p in _SEP.split(clean_title(channel_title)) if p.strip()]
    for known, name in SOURCES.items():
        if host == known or host.endswith("." + known):
            for part in parts[1:]:  # the feed's section, when it says more than "Top Stories News"
                extra = [w for w in part.split() if w.lower() not in _PLAIN]
                if extra and len(extra) <= 2:
                    word = " ".join(extra)[:20]
                    return name if word.lower() in name.lower() else f"{name} {word}"
            return name
    if parts:
        return parts[0][:40]
    return host.removeprefix("www.")[:40] or "the news"


def parse_feed(data: bytes, url: str = "") -> list[Headline]:
    """The headlines of an RSS 2.0 / RSS 1.0 / Atom feed, in its own order; [] for anything else."""
    if not data or len(data) > MAX_BYTES or b"<!ENTITY" in data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    channel_title = ""
    for el in root.iter():
        name = _local(el.tag)
        if name in ("channel", "feed"):
            t = next((c for c in el if _local(c.tag) == "title"), None)
            channel_title = "".join(t.itertext()) if t is not None else ""
            break
    if _local(root.tag) == "feed":  # Atom: the feed's own <title>
        t = next((c for c in root if _local(c.tag) == "title"), None)
        channel_title = "".join(t.itertext()) if t is not None else channel_title
    source = source_name(url, channel_title)
    out: list[Headline] = []
    seen: set[str] = set()
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        t = next((c for c in el if _local(c.tag) == "title"), None)
        title = clean_title("".join(t.itertext())) if t is not None else ""
        key = _key(title)
        if title and key not in seen:
            seen.add(key)
            out.append(Headline(title, source))
        if len(out) >= MAX_ITEMS:
            break
    return out


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def mix(feeds: list[list[Headline]], count: int) -> list[Headline]:
    """The feeds' top stories taken in turns (the first feed's first, the second's first, ...), no repeats."""
    out: list[Headline] = []
    seen: set[str] = set()
    depth = max((len(f) for f in feeds), default=0)
    for i in range(depth):
        for f in feeds:
            if i < len(f):
                key = _key(f[i].title)
                if key and key not in seen:
                    seen.add(key)
                    out.append(f[i])
                    if len(out) >= count:
                        return out
    return out


def topic_words(topic: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", str(topic or "").lower())
    return [w for w in words if w not in _STOP and (len(w) >= 3 or w.isdigit())][:6]


def about(headline: Headline, words: list[str]) -> bool:
    """The headline mentions one of the topic's words (a word start: "election" finds "elections")."""
    text = headline.title.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w), text) for w in words)


def valid_feed_url(url: Any) -> str | None:
    s = str(url or "").strip()
    try:
        u = httpx.URL(s)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    return s if u.scheme in ("http", "https") and u.host else None


# ---------------------------------------------------------------- the service
class NewsFeeds:
    """``headlines(count, topic)``: the configured feeds, fetched together and cached."""

    def __init__(self, ctx: Any, *, transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], float] = time.monotonic, timeout: float = TIMEOUT_S) -> None:
        self.ctx = ctx
        self._transport = transport
        self._now = now
        self.timeout = float(timeout)
        self._cache: dict[str, tuple[float, list[Headline]]] = {}
        self._failed: dict[str, float] = {}
        self._pending: dict[str, asyncio.Task[list[Headline]]] = {}
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def feeds(self) -> list[str]:
        raw = getattr(self.ctx.config.skills, "news_feeds", None) or []
        return [u for u in (valid_feed_url(x) for x in raw) if u][:12]

    def blocked_reason(self) -> str | None:
        if self.ctx.config.is_offline:
            return "offline"
        if not self.ctx.privacy.cloud_enabled:
            return "cloud_off"
        if not self.feeds():
            return "no_feeds"
        return None

    def cached(self, count: int) -> list[Headline]:
        """What is already known (fresh or not), without fetching."""
        return mix([self._cache[u][1] for u in self.feeds() if u in self._cache], count)

    async def headlines(self, count: int = 5, topic: str | None = None) -> list[Headline]:
        """Up to ``count`` headlines (about ``topic``: its words in the title), mixed across the feeds. Raises
        NewsError when nothing could be had at all; never takes longer than ``timeout``."""
        reason = self.blocked_reason()
        if reason:
            raise NewsError(reason)
        urls = self.feeds()
        loop = asyncio.get_running_loop()
        tasks = [self._get(u, loop) for u in urls]
        waiting = [t for t in tasks if isinstance(t, asyncio.Future)]
        if waiting:  # (asyncio.wait never cancels: a feed still loading carries on for next time)
            await asyncio.wait(waiting, timeout=self.timeout)
        feeds: list[list[Headline]] = []
        for u, t in zip(urls, tasks):
            if isinstance(t, list):
                feeds.append(t)
            elif t.done() and not t.cancelled() and t.exception() is None:
                feeds.append(t.result())
            elif u in self._cache:  # still loading or failed: the last copy, however old
                feeds.append(self._cache[u][1])
        if not any(feeds):
            raise NewsError("unavailable")
        n = max(1, min(50, int(count)))
        words = topic_words(topic or "")
        if words:
            return mix([[h for h in f if about(h, words)] for f in feeds], n)
        return mix(feeds, n)

    def _get(self, url: str, loop: asyncio.AbstractEventLoop) -> list[Headline] | asyncio.Task[list[Headline]]:
        hit = self._cache.get(url)
        if hit is not None and self._now() - hit[0] < CACHE_S:
            return hit[1]
        failed = self._failed.get(url)
        if failed is not None and self._now() - failed < RETRY_S:
            return hit[1] if hit is not None else []
        task = self._pending.get(url)
        if task is None or task.done() or task.get_loop() is not loop:
            task = self._pending[url] = loop.create_task(self._fetch(url))
            task.add_done_callback(functools.partial(self._settle, url))
        return task

    def _settle(self, url: str, task: asyncio.Task[list[Headline]]) -> None:
        if self._pending.get(url) is task:
            del self._pending[url]
        if task.cancelled():
            return
        if task.exception() is not None:  # (retrieves it: no warning)
            self._failed[url] = self._now()
            return
        self._failed.pop(url, None)
        self._cache[url] = (self._now(), task.result())

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            from littlebot import __version__  # noqa: PLC0415

            self._client = httpx.AsyncClient(
                transport=self._transport, follow_redirects=True, max_redirects=3,
                headers={"User-Agent": f"LittleBot/{__version__} (news headlines)",
                         "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"},
                timeout=httpx.Timeout(max(self.timeout, 8.0), connect=min(3.0, self.timeout)))
            self._client_loop = loop
        return self._client

    async def _fetch(self, url: str) -> list[Headline]:
        t0 = self._now()
        host = httpx.URL(url).host
        try:
            async with self._http().stream("GET", url) as resp:
                if resp.status_code >= 400:
                    log.info("news: %s answered HTTP %d", host, resp.status_code)
                    raise NewsError("unavailable")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        log.info("news: %s sent too much; skipped", host)
                        raise NewsError("unavailable")
        except httpx.HTTPError as e:
            log.info("news: %s couldn't be fetched (%s)", host, type(e).__name__)
            raise NewsError("unavailable") from None
        items = await asyncio.to_thread(parse_feed, bytes(body), url)
        log.info("news: %s -> %d headlines in %d ms", host, len(items), int((self._now() - t0) * 1000))
        if not items:
            raise NewsError("unavailable")
        return items

    async def aclose(self) -> None:
        for t in list(self._pending.values()):
            t.cancel()
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


__all__ = ["ERRORS", "Headline", "NewsError", "NewsFeeds", "about", "clean_title", "mix", "parse_feed",
           "source_name", "topic_words"]
