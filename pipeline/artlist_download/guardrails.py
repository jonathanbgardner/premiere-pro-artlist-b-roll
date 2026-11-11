"""Safety checks enforced in code (not just in the prompt).

- `UrlPolicy`: domain allowlist + blocked account/billing paths. Used by the
  Playwright route interceptor (every request), by the `navigate` tool, and by
  the URL check after every action.
- `detect_blocker`: login page / captcha heuristics from page signals.
- `HumanGate`: pause for a person (interactive) or give up (non-interactive).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from .config import Settings


class NeedsHuman(Exception):
    """Raised when a person must act (login, captcha) and we cannot wait."""


class GuardrailViolation(Exception):
    pass


def host_allowed(host: str, allowed_domains: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    for d in allowed_domains:
        d = d.lower().lstrip(".").rstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


@dataclass
class UrlPolicy:
    allowed_domains: list[str]
    blocked_path_patterns: list[str] = field(default_factory=list)
    login_url_patterns: list[str] = field(default_factory=list)
    strict_subresources: bool = False

    def __post_init__(self) -> None:
        self._blocked = [re.compile(p, re.I) for p in self.blocked_path_patterns]
        self._login = [re.compile(p, re.I) for p in self.login_url_patterns]

    @classmethod
    def from_settings(cls, s: Settings) -> "UrlPolicy":
        return cls(s.allowed_domains, s.blocked_path_patterns, s.login_url_patterns, s.strict_subresources)

    def check_navigation(self, url: str) -> str | None:
        """Return a reason string if the main page may NOT go to `url`."""
        if url in ("about:blank", "") or url.startswith(("data:", "blob:")):
            return None
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return f"scheme {parts.scheme!r} not allowed"
        if not host_allowed(parts.hostname or "", self.allowed_domains):
            return f"host {parts.hostname!r} is not on the allowlist {self.allowed_domains}"
        path = parts.path or "/"
        for rx in self._blocked:
            if rx.search(path):
                return f"path {path!r} is blocked (account/billing/settings area)"
        return None

    def check_request(self, url: str, is_navigation: bool) -> str | None:
        """Policy for the route interceptor."""
        if is_navigation:
            return self.check_navigation(url)
        if url.startswith(("data:", "blob:")):
            return None
        if self.strict_subresources:
            host = urlsplit(url).hostname or ""
            if not host_allowed(host, self.allowed_domains):
                return f"sub-resource host {host!r} not allowed (strict mode)"
        return None

    def looks_like_login_url(self, url: str) -> bool:
        parts = urlsplit(url)
        target = (parts.path or "") + ("?" + parts.query if parts.query else "")
        return any(rx.search(target) for rx in self._login) or "captcha" in (parts.hostname or "")


# Page-content heuristics. `signals` is what Computer.page_signals() returns:
#   url, title, has_password_input (visible), captcha_frames (list of iframe
#   srcs that look like captcha providers), text_snippet (first ~2k chars).
CAPTCHA_TITLE_HINTS = ("just a moment", "attention required", "are you a robot", "verify you are human",
                       "security check")
CAPTCHA_FRAME_HINTS = ("recaptcha", "hcaptcha", "turnstile", "challenges.cloudflare.com", "arkoselabs",
                       "funcaptcha", "captcha")
LOGIN_TEXT_HINTS = ("log in to artlist", "sign in to artlist", "log in to continue", "sign in to continue",
                    "welcome back", "forgot password")


def detect_blocker(signals: dict, policy: UrlPolicy) -> str | None:
    """Return a human-readable reason if the page needs a person, else None."""
    url = signals.get("url", "") or ""
    title = (signals.get("title") or "").lower()
    text = (signals.get("text_snippet") or "").lower()
    frames = [f.lower() for f in signals.get("captcha_frames") or []]
    if any(h in f for f in frames for h in CAPTCHA_FRAME_HINTS):
        return "captcha detected on page"
    if any(h in title for h in CAPTCHA_TITLE_HINTS):
        return f"human verification page ({signals.get('title')!r})"
    if "captcha" in url.lower():
        return "captcha URL"
    if policy.looks_like_login_url(url):
        return f"login page ({url})"
    if signals.get("has_password_input"):
        return "password field visible: login required"
    if any(h in text for h in LOGIN_TEXT_HINTS):
        return "login prompt text on page"
    return None


class HumanGate:
    """Pause for a person. Interactive: print and wait for Enter. Otherwise raise NeedsHuman."""

    def __init__(self, interactive: bool, input_fn: Callable[[str], str] = input,
                 print_fn: Callable[[str], None] = print):
        self.interactive = interactive
        self._input = input_fn
        self._print = print_fn
        self.pauses = 0

    def pause(self, shot_id: str, reason: str) -> None:
        if not self.interactive:
            raise NeedsHuman(reason)
        self.pauses += 1
        self._print(f"\n[artlist_download] Shot {shot_id}: human needed: {reason}\n"
                    "  Fix it in the browser window (log in / solve the captcha), "
                    "but do not change account settings.")
        self._input("  Press Enter to continue... ")
