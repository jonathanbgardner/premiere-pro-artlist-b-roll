"""Settings for workstream B (Artlist search + download via computer use).

Everything safety-relevant (domain allowlist, blocked paths, limits) lives here
so it can be changed from a JSON file (`--config`) or CLI flags without code
edits. Defaults are conservative.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "claude-opus-5-5"

# Hosts the agent may navigate the main page to, and that downloads may come
# from. Suffix match: "artlist.io" also allows "www.artlist.io",
# "cms-artifacts.artlist.io", "auth.artlist.io", ...
# Artlist may serve downloads from a third-party CDN / signed-URL host; if a
# download is blocked, the run log prints the host -> add it here (or with
# --allow-domain) after checking it really belongs to Artlist.
DEFAULT_ALLOWED_DOMAINS = ["artlist.io"]

# Path fragments the agent must never open (account, billing, plan changes).
# Matched as regex against the URL path (case-insensitive).
DEFAULT_BLOCKED_PATH_PATTERNS = [
    r"/account",
    r"/billing",
    r"/settings",
    r"/subscription",
    r"/subscriptions",
    r"/checkout",
    r"/payment",
    r"/pricing",
    r"/plans?(/|$)",
    r"/upgrade",
    r"/invoice",
    r"/cart",
    r"/logout",
    r"/sign-?out",
    r"/delete",
]

# URL hints that we landed on a login / human-verification page.
DEFAULT_LOGIN_URL_PATTERNS = [
    r"/login",
    r"/log-in",
    r"/sign-?in",
    r"/signup",
    r"/sign-up",
    r"/auth(/|$|\?)",
    r"/oauth",
    r"captcha",
    r"/challenge",
    r"cdn-cgi/challenge",
]

VIDEO_EXTS = (".mp4", ".mov")
MUSIC_EXTS = (".wav", ".mp3")

# USD per million tokens (input, output, cache write, cache read). Unknown
# models fall back to the default model's prices.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-fable-5-2": (8.0, 40.0, 10.0, 0.20),
}

# Models for which we enable the server-side refusal fallback by default.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5"}


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    max_tokens: int = 16000
    use_fallbacks: bool = True

    start_url: str = "https://artlist.io/"
    # Optional search URL templates with "{query}" (URL-encoded) placeholder.
    # Left empty by default: the agent uses the site's own search box.
    video_search_url: str = ""
    music_search_url: str = ""

    allowed_domains: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_DOMAINS))
    blocked_path_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_PATH_PATTERNS))
    login_url_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_LOGIN_URL_PATTERNS))
    # If False (default), sub-resources (scripts, images, XHR) from other hosts
    # are allowed so the site renders; navigations and downloads are always
    # restricted to allowed_domains. Set True to also block off-site
    # sub-resources (may break the site).
    strict_subresources: bool = False

    viewport_width: int = 1280
    viewport_height: int = 800

    max_steps_per_shot: int = 40          # model calls per shot
    max_total_steps: int = 400            # model calls per run
    max_total_cost_usd: float = 15.0      # estimated, from token usage
    shot_timeout_s: float = 420.0         # wall clock per shot
    download_timeout_s: float = 120.0     # wait for a download to finish
    max_guardrail_violations: int = 3     # per shot, then fail the shot
    handle_s: float = 2.0                 # extra seconds required beyond duration_s
    min_video_height: int = 1080

    interactive: bool = True
    headless: bool = False

    @classmethod
    def from_file(cls, path: str | Path, **overrides: Any) -> "Settings":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown settings in {path}: {sorted(unknown)}")
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)

    def prices(self) -> tuple[float, float, float, float]:
        return PRICES.get(self.model, PRICES[DEFAULT_MODEL])
