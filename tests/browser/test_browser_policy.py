"""Where the browser view may go (littlebot/browser/policy.py): https only, SafeSearch on, nothing local."""

from __future__ import annotations

import pytest

from littlebot.browser.policy import (
    MAX_QUERY,
    check,
    clean_query,
    google_url,
    host_of,
    is_safe_search,
    query_blocked,
)


def test_google_url_always_asks_for_safe_search() -> None:
    assert google_url("cats & dogs?") == "https://www.google.com/search?q=cats+%26+dogs%3F&safe=active"
    assert google_url("  ") == google_url(None) == "https://www.google.com/?safe=active"
    assert google_url("x" * 500).count("x") == MAX_QUERY


def test_clean_query() -> None:
    assert clean_query(" why\n is\tthe\x00sky  blue ") == "why is the sky blue"
    assert clean_query(None) == ""


@pytest.mark.parametrize("url", [
    "https://en.wikipedia.org/wiki/Giraffe",
    "https://www.google.com/search?q=x&safe=active",
    "https://www.bing.com/search?q=x&adlt=strict",
    "https://duckduckgo.com/?q=x&kp=1",
    "https://www.google.com/maps/place/x",  # not a search page
])
def test_ok(url: str) -> None:
    assert check(url).action == "ok"
    assert is_safe_search(url)


@pytest.mark.parametrize(("url", "fixed"), [
    ("https://www.google.com/search?q=x", "https://www.google.com/search?q=x&safe=active"),
    ("https://www.google.com/search?q=x&safe=off", "https://www.google.com/search?q=x&safe=active"),
    ("https://www.google.co.uk/search?safe=images&q=x", "https://www.google.co.uk/search?q=x&safe=active"),
    ("https://www.google.com/", "https://www.google.com/?safe=active"),
    ("https://www.bing.com/search?q=x", "https://www.bing.com/search?q=x&adlt=strict"),
    ("https://duckduckgo.com/?q=x&kp=-2", "https://duckduckgo.com/?q=x&kp=1"),
])
def test_search_pages_get_safe_search_back(url: str, fixed: str) -> None:
    verdict = check(url)
    assert (verdict.action, verdict.url) == ("rewrite", fixed)
    assert not is_safe_search(url)
    assert check(verdict.url).action == "ok"


@pytest.mark.parametrize("url", [
    "http://example.com/", "file:///etc/passwd", "chrome://settings", "data:text/html,<b>hi</b>",
    "javascript:alert(1)", "about:blank", "ftp://x.example/", "https:///nohost",
    "https://user:pw@example.com/", "https://localhost:8080/", "https://127.0.0.1/", "https://10.0.0.5/",
    "https://192.168.1.20/admin", "https://[::1]/", "https://169.254.1.1/", "https://littlebot.local/",
    "https://example.com/" + "x" * 5000,
])
def test_blocked(url: str) -> None:
    verdict = check(url)
    assert verdict.action == "block" and verdict.why


def test_plain_http_only_for_named_test_hosts() -> None:
    assert check("http://127.0.0.1:8000/", insecure_hosts=("127.0.0.1",)).action == "ok"
    assert check("http://example.com/", insecure_hosts=("127.0.0.1",)).action == "block"


def test_host_of_never_gives_the_path() -> None:
    assert host_of("https://www.google.com/search?q=secret") == "www.google.com"
    assert host_of("not a url") == ""


def test_query_blocked_uses_the_safety_rules() -> None:
    assert query_blocked("how tall are giraffes") == []
    assert query_blocked("naked women") == ["inappropriate"]
    assert query_blocked("naked women", relaxed=("inappropriate",)) == []
