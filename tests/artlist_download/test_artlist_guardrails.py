"""Unit tests for workstream B guardrails and helpers."""
from __future__ import annotations

import pytest

from pipeline.artlist_download.agent import parse_resolution_height
from pipeline.artlist_download.computer import parse_modifiers, to_playwright_key
from pipeline.artlist_download.config import Settings
from pipeline.artlist_download.guardrails import HumanGate, NeedsHuman, UrlPolicy, detect_blocker, host_allowed

POLICY = UrlPolicy.from_settings(Settings())


@pytest.mark.parametrize("host,ok", [
    ("artlist.io", True), ("www.artlist.io", True), ("cms-public-artifacts.artlist.io", True),
    ("ARTLIST.IO", True), ("evilartlist.io", False), ("artlist.io.evil.com", False), ("", False),
    ("google.com", False),
])
def test_host_allowed(host, ok):
    assert host_allowed(host, ["artlist.io"]) is ok


@pytest.mark.parametrize("url,blocked", [
    ("https://artlist.io/stock-footage/search?terms=cows", False),
    ("https://artlist.io/royalty-free-music/song/x/1", False),
    ("https://artlist.io/", False),
    ("about:blank", False),
    ("https://artlist.io/account", True),
    ("https://artlist.io/account/billing", True),
    ("https://artlist.io/Settings/profile", True),
    ("https://artlist.io/subscription/cancel", True),
    ("https://artlist.io/checkout?plan=max", True),
    ("https://artlist.io/pricing", True),
    ("https://artlist.io/plans", True),
    ("https://artlist.io/stock-footage/clip/plans-for-the-farm/1", False),
    ("https://www.youtube.com/", True),
    ("http://artlist.io.evil.com/", True),
    ("file:///etc/passwd", True),
    ("javascript:alert(1)", True),
])
def test_check_navigation(url, blocked):
    assert (POLICY.check_navigation(url) is not None) is blocked


def test_subresources_allowed_unless_strict():
    assert POLICY.check_request("https://fonts.gstatic.com/x.woff", is_navigation=False) is None
    strict = UrlPolicy(["artlist.io"], strict_subresources=True)
    assert strict.check_request("https://fonts.gstatic.com/x.woff", is_navigation=False)
    assert strict.check_request("https://cdn.artlist.io/x.js", is_navigation=False) is None
    assert POLICY.check_request("https://evil.com/", is_navigation=True)


@pytest.mark.parametrize("signals,hit", [
    ({"url": "https://artlist.io/stock-footage"}, None),
    ({"url": "https://artlist.io/login?next=/"}, "login"),
    ({"url": "https://artlist.io/sign-in"}, "login"),
    ({"url": "https://artlist.io/x", "has_password_input": True}, "password"),
    ({"url": "https://artlist.io/x", "captcha_frames": ["https://challenges.cloudflare.com/turnstile"]},
     "captcha"),
    ({"url": "https://artlist.io/x", "title": "Just a moment..."}, "verification"),
    ({"url": "https://artlist.io/x", "text_snippet": "Log in to continue downloading"}, "login prompt"),
])
def test_detect_blocker(signals, hit):
    r = detect_blocker(signals, POLICY)
    assert (r is None) if hit is None else (hit in r)


def test_human_gate():
    with pytest.raises(NeedsHuman):
        HumanGate(False).pause("S001", "captcha")
    seen = []
    g = HumanGate(True, input_fn=lambda m: seen.append(m) or "", print_fn=seen.append)
    g.pause("S001", "captcha")
    assert g.pauses == 1 and any("Enter" in s for s in seen)


@pytest.mark.parametrize("res,h", [
    ("1920x1080", 1080), ("3840 x 2160", 2160), ("1080x1920", 1080), ("1080p", 1080), ("720p", 720),
    ("4K", 2160), ("Full HD", 1080), ("HD", 720), ("audio", None), ("", None),
])
def test_parse_resolution(res, h):
    assert parse_resolution_height(res) == h


@pytest.mark.parametrize("combo,pw", [
    ("Return", "Enter"), ("ctrl+a", "Control+a"), ("ctrl+shift+Tab", "Control+Shift+Tab"),
    ("Page_Down", "PageDown"), ("f5", "F5"), ("super+l", "Meta+l"), ("Escape", "Escape"), ("a", "a"),
])
def test_key_mapping(combo, pw):
    assert to_playwright_key(combo) == pw


def test_parse_modifiers():
    assert parse_modifiers("ctrl+shift") == ["Control", "Shift"]
    assert parse_modifiers(None) == []


def test_settings_from_file(tmp_path):
    p = tmp_path / "b.json"
    p.write_text('{"allowed_domains": ["artlist.io", "artlist-cdn.example"], "max_total_steps": 5}')
    s = Settings.from_file(p, model="claude-sonnet-5-5")
    assert s.allowed_domains == ["artlist.io", "artlist-cdn.example"] and s.max_total_steps == 5
    assert s.model == "claude-sonnet-5-5"
    p.write_text('{"nope": 1}')
    with pytest.raises(ValueError):
        Settings.from_file(p)
