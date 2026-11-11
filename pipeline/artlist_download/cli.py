"""CLI: `python -m pipeline.artlist_download {login,run} ...`"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_MODEL, Settings
from .guardrails import HumanGate, UrlPolicy


def _settings(args) -> Settings:
    overrides = {"model": args.model, "effort": args.effort}
    s = Settings.from_file(args.config, **overrides) if args.config else Settings(
        **{k: v for k, v in overrides.items() if v is not None})
    if args.allow_domain:
        s.allowed_domains = list(dict.fromkeys(s.allowed_domains + args.allow_domain))
    if args.start_url:
        s.start_url = args.start_url
    for name in ("max_steps_per_shot", "max_total_steps", "max_total_cost_usd", "shot_timeout_s"):
        v = getattr(args, name, None)
        if v is not None:
            setattr(s, name, v)
    if getattr(args, "no_fallbacks", False):
        s.use_fallbacks = False
    s.interactive = not getattr(args, "non_interactive", False)
    s.headless = bool(getattr(args, "headless", False))
    return s


def cmd_login(args) -> int:
    from playwright.sync_api import sync_playwright

    from .playwright_backend import launch_persistent

    url = args.url or "https://artlist.io/"
    with sync_playwright() as p:
        ctx = launch_persistent(p, args.profile, headless=False, viewport=(1280, 800))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        print(f"A browser window is open with profile {args.profile}.\n"
              "Log in to Artlist there (complete any captcha / 2FA), then come back here.")
        input("Press Enter when you are logged in to close the browser and save the session... ")
        ctx.close()
    print("Saved. Use the same --profile for `run`.")
    return 0


def cmd_run(args) -> int:
    from .runner import Runner

    s = _settings(args)
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    computer_factory = llm = None
    pw_holder: dict = {}
    if not args.dry_run:
        from .llm import AnthropicLLM

        llm = AnthropicLLM(s.model, use_fallbacks=s.use_fallbacks)

        def computer_factory():
            from playwright.sync_api import sync_playwright

            from .playwright_backend import PlaywrightComputer, launch_persistent

            pw = sync_playwright().start()
            pw_holder["pw"] = pw
            ctx = launch_persistent(pw, args.profile, headless=s.headless,
                                    viewport=(s.viewport_width, s.viewport_height))
            return PlaywrightComputer(ctx, UrlPolicy.from_settings(s), width=s.viewport_width,
                                      height=s.viewport_height)

    runner = Runner(args.project_root, s, computer_factory=computer_factory, llm=llm,
                    gate=HumanGate(s.interactive), only=only, max_shots=args.max_shots,
                    retry_failed=args.retry_failed, dry_run=args.dry_run)
    try:
        summary = runner.run()
    finally:
        if "pw" in pw_holder:
            pw_holder["pw"].stop()
    print(json.dumps(summary, indent=2))
    failed = [p for p in summary["processed"] if p.get("status") == "failed"]
    return 1 if failed or summary.get("stopped") else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m pipeline.artlist_download",
                                 description="Workstream B: Artlist search + download via Claude computer use")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="open a headed browser once to log in to Artlist")
    lg.add_argument("--profile", required=True, help="persistent browser profile dir")
    lg.add_argument("--url", help="page to open (default https://artlist.io/)")
    lg.set_defaults(func=cmd_login)

    r = sub.add_parser("run", help="fill pending stock_video / music shots")
    r.add_argument("--project-root", required=True, type=Path)
    r.add_argument("--profile", required=True, help="persistent browser profile dir used for `login`")
    r.add_argument("--only", help="comma-separated shot IDs, e.g. S001,S002")
    r.add_argument("--max-shots", type=int, help="process at most N shots with the agent")
    r.add_argument("--non-interactive", action="store_true",
                   help="never wait for a person: login/captcha -> shot failed with error needs_human, run stops")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--model", default=None, help=f"Claude model (default {DEFAULT_MODEL})")
    r.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--dry-run", action="store_true", help="list what would run; no browser, no API calls")
    r.add_argument("--retry-failed", action="store_true", help="also retry shots B marked failed")
    r.add_argument("--config", help="JSON file with Settings overrides (see config.py)")
    r.add_argument("--allow-domain", action="append", help="extra allowed host (suffix match); repeatable")
    r.add_argument("--start-url", help="page each shot starts on (default https://artlist.io/)")
    r.add_argument("--max-steps-per-shot", type=int)
    r.add_argument("--max-total-steps", type=int)
    r.add_argument("--max-total-cost-usd", type=float)
    r.add_argument("--shot-timeout-s", type=float)
    r.add_argument("--no-fallbacks", action="store_true", help="disable server-side refusal fallback")
    r.set_defaults(func=cmd_run)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
