"""Where the WhatsApp window may go (littlebot/calls/policy.py): web.whatsapp.com, and nowhere else."""

from __future__ import annotations

import pytest

from littlebot.calls import policy


@pytest.mark.parametrize("url", ["https://web.whatsapp.com/", "https://web.whatsapp.com/some/path?x=1",
                                 "https://WEB.WhatsApp.com/", "https://web.whatsapp.com:443/"])
def test_whatsapp_web_pages_load(url: str) -> None:
    assert policy.check_page(url) is None


@pytest.mark.parametrize("url", [
    "http://web.whatsapp.com/", "https://www.whatsapp.com/", "https://whatsapp.com/", "https://example.com/",
    "https://web.whatsapp.com.evil.example/", "https://evil.example/web.whatsapp.com", "https://user@web.whatsapp.com/",
    "https://web.whatsapp.com:8443/", "file:///etc/passwd", "chrome://settings", "javascript:alert(1)", "",
    "data:text/html,hi", "https://static.whatsapp.net/x.html", "https://" + "a" * 5000 + ".com/",
])
def test_nothing_else_loads_in_the_window(url: str) -> None:
    assert policy.check_page(url) is not None


@pytest.mark.parametrize("url,ok", [
    ("https://static.whatsapp.net/frame.html", True), ("https://web.whatsapp.com/x", True),
    ("https://www.whatsapp.com/legal", True), ("https://mmg.whatsapp.net/v", True), ("about:blank", True),
    ("about:srcdoc", True), ("https://whatsapp.net.evil.example/", False), ("https://evilwhatsapp.net/", False),
    ("http://static.whatsapp.net/x", False), ("https://www.youtube.com/embed/x", False),
])
def test_frames_only_from_whatsapps_own_hosts(url: str, ok: bool) -> None:
    assert (policy.check_frame(url) is None) is ok


def test_another_window() -> None:
    assert policy.check_window("") is None and policy.check_window("about:blank") is None
    assert policy.check_window("https://web.whatsapp.com/call") is None
    assert policy.check_window("https://example.com/") is not None
    assert policy.check_window("https://www.whatsapp.com/") is not None


def test_a_tests_local_page_only_when_asked_for() -> None:
    url = "http://127.0.0.1:8123/index.html"
    assert policy.check_page(url) is not None
    assert policy.check_page(url, insecure_hosts=("127.0.0.1",)) is None
    assert policy.check_page("http://example.com/", insecure_hosts=("127.0.0.1",)) is not None


def test_reasons_and_hosts_never_hold_the_path() -> None:
    url = "https://example.com/private/path?token=abc"
    assert "private" not in str(policy.check_page(url)) and policy.host_of(url) == "example.com"
