"""Real `Computer`: Playwright Chromium with a persistent user-data dir.

The persistent profile keeps the Artlist login between runs, so Jonathan logs
in once with `python -m pipeline.artlist_download login --profile DIR`.

Guardrails wired in here:
- `context.route("**/*")` aborts every request the UrlPolicy rejects
  (navigations to non-allowlisted hosts or blocked account/billing paths;
  optionally all off-site sub-resources).
- popups are closed; if their URL is allowed the main page opens it instead.
"""
from __future__ import annotations

import glob
import os
import time
from pathlib import Path

from .computer import Computer, ComputerError, DownloadLike, parse_modifiers, to_playwright_key
from .guardrails import UrlPolicy

_SIGNALS_JS = r"""
() => {
  const vis = (el) => { const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const pw = Array.from(document.querySelectorAll('input[type=password]')).some(vis);
  const frames = Array.from(document.querySelectorAll('iframe'))
      .map(f => f.src || '').filter(s => /captcha|turnstile|challenges\.cloudflare|arkose/i.test(s));
  const captchaDivs = document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, #challenge-form') ? ['captcha-element'] : [];
  return { title: document.title || '', has_password_input: pw,
           captcha_frames: frames.concat(captchaDivs),
           text_snippet: (document.body ? document.body.innerText : '').slice(0, 2000) };
}
"""


def find_chromium_executable() -> str | None:
    """Explicit path for environments where pip-playwright's browser build is missing.

    Order: $BROLL_CHROMIUM_PATH, then nothing (let Playwright pick its own),
    with /opt/pw-browsers/... tried only as a fallback after a launch failure.
    """
    return os.environ.get("BROLL_CHROMIUM_PATH") or None


def _fallback_executables() -> list[str]:
    cands = []
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    cands += [os.path.join(base, "chromium")]
    cands += sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux*", "chrome")), reverse=True)
    return [c for c in cands if os.path.exists(c)]


def launch_persistent(playwright, profile_dir: str | Path, *, headless: bool, viewport: tuple[int, int],
                      downloads_dir: str | Path | None = None):
    profile_dir = str(Path(profile_dir).expanduser())
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    kwargs = dict(headless=headless, accept_downloads=True,
                  viewport={"width": viewport[0], "height": viewport[1]})
    if downloads_dir:
        kwargs["downloads_path"] = str(downloads_dir)
    exe = find_chromium_executable()
    if exe:
        return playwright.chromium.launch_persistent_context(profile_dir, executable_path=exe, **kwargs)
    try:
        return playwright.chromium.launch_persistent_context(profile_dir, **kwargs)
    except Exception as first:  # browser build missing for this playwright version
        if "Executable doesn't exist" not in str(first):
            raise
        for cand in _fallback_executables():
            try:
                return playwright.chromium.launch_persistent_context(profile_dir, executable_path=cand, **kwargs)
            except Exception:
                continue
        raise


class PlaywrightComputer(Computer):
    def __init__(self, context, policy: UrlPolicy, *, width: int = 1280, height: int = 800, log=print):
        self.context = context
        self.policy = policy
        self.width, self.height = width, height
        self._log = log
        self._downloads: list = []
        self._blocked: list[str] = []
        self._popups: list = []
        self._cursor = (width // 2, height // 2)
        context.route("**/*", self._route)
        context.on("page", self._on_page)
        self.page = context.pages[0] if context.pages else context.new_page()
        self._attach(self.page)
        self.page.set_viewport_size({"width": width, "height": height})

    # ------------------------------------------------------------ wiring
    def _attach(self, page) -> None:
        page.on("download", self._on_download)

    def _on_download(self, download) -> None:
        self._downloads.append(download)

    def _route(self, route, request) -> None:
        try:
            is_nav = request.is_navigation_request()
        except Exception:
            is_nav = request.resource_type == "document"
        reason = self.policy.check_request(request.url, is_nav)
        if reason:
            if is_nav:  # sub-resource blocks (strict mode) are not the agent's doing
                self._blocked.append(f"{request.url} ({reason})")
                self._log(f"[guardrail] blocked navigation: {request.url}: {reason}")
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def _on_page(self, page) -> None:
        # Keep the handler tiny (sync-API event callback); real handling in _process_popups.
        if page is self.page:
            return
        self._attach(page)  # popup-triggered downloads still count
        self._popups.append(page)

    def _process_popups(self) -> None:
        while self._popups:
            page = self._popups.pop(0)
            try:
                page.wait_for_load_state("commit", timeout=3000)
            except Exception:
                pass
            url = page.url
            ok = bool(url) and url != "about:blank" and self.policy.check_navigation(url) is None
            if not ok:
                # Give a download-only popup a moment before closing it.
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
            try:
                page.close()
            except Exception:
                pass
            if ok:
                self._log(f"[guardrail] popup {url} reopened in main tab")
                self.goto(url)
            else:
                self._log(f"[guardrail] closed popup {url!r}")

    # ------------------------------------------------------------ actions
    def screenshot(self) -> bytes:
        return self.page.screenshot(type="png", full_page=False)

    def _with_mods(self, modifiers, fn):
        mods = modifiers or []
        for m in mods:
            self.page.keyboard.down(m)
        try:
            fn()
        finally:
            for m in reversed(mods):
                self.page.keyboard.up(m)

    def click(self, x, y, button="left", count=1, modifiers=None):
        if x is None or y is None:
            x, y = self._cursor
        self._check_xy(x, y)
        self._cursor = (x, y)
        self._with_mods(modifiers, lambda: self.page.mouse.click(x, y, button=button, click_count=count))
        self._settle()

    def mouse_move(self, x, y):
        self._check_xy(x, y)
        self._cursor = (x, y)
        self.page.mouse.move(x, y)

    def mouse_down(self):
        self.page.mouse.down()

    def mouse_up(self):
        self.page.mouse.up()
        self._settle()

    def drag(self, x0, y0, x1, y1, modifiers=None):
        self._check_xy(x0, y0)
        self._check_xy(x1, y1)

        def do():
            self.page.mouse.move(x0, y0)
            self.page.mouse.down()
            self.page.mouse.move(x1, y1, steps=10)
            self.page.mouse.up()
        self._with_mods(modifiers, do)
        self._cursor = (x1, y1)

    def type_text(self, text):
        self.page.keyboard.type(text, delay=15)

    def key(self, combo, repeat=1):
        k = to_playwright_key(combo)
        for _ in range(max(1, min(int(repeat), 100))):
            self.page.keyboard.press(k)
        self._settle()

    def hold_key(self, combo, duration_s):
        keys = parse_modifiers(combo)
        for k in keys:
            self.page.keyboard.down(k)
        self.page.wait_for_timeout(int(min(duration_s, 30) * 1000))
        for k in reversed(keys):
            self.page.keyboard.up(k)

    def scroll(self, direction, amount, x=None, y=None, modifiers=None):
        if x is not None and y is not None:
            self.mouse_move(x, y)
        px = float(amount) * 100
        dx, dy = {"up": (0, -px), "down": (0, px), "left": (-px, 0), "right": (px, 0)}[direction]
        self._with_mods(modifiers, lambda: self.page.mouse.wheel(dx, dy))
        self.page.wait_for_timeout(300)

    def wait(self, seconds):
        self.page.wait_for_timeout(int(max(0.0, min(float(seconds), 30.0)) * 1000))

    def cursor_position(self):
        return self._cursor

    # ------------------------------------------------------------ helpers
    def _check_xy(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ComputerError(f"coordinate ({x}, {y}) outside the {self.width}x{self.height} screen")

    def _settle(self):
        if self._popups:
            self._process_popups()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        self.page.wait_for_timeout(400)

    def goto(self, url):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            # Download-only URLs raise "Download is starting"; that's fine.
            if "Download is starting" not in str(e):
                raise ComputerError(f"navigation to {url} failed: {e}") from e
        self._settle()

    def current_url(self):
        if self._popups:
            self._process_popups()
        return self.page.url

    def go_back_or(self, fallback_url):
        try:
            self.page.go_back(timeout=10000)
        except Exception:
            pass
        if self.policy.check_navigation(self.page.url) is not None:
            try:
                self.page.goto(fallback_url, timeout=30000)
            except Exception:
                self.page.goto("about:blank")

    def page_signals(self):
        try:
            sig = self.page.evaluate(_SIGNALS_JS)
        except Exception:
            sig = {"title": "", "has_password_input": False, "captcha_frames": [], "text_snippet": ""}
        sig["url"] = self.page.url
        return sig

    def take_downloads(self, wait_s=0.0) -> list[DownloadLike]:
        deadline = time.monotonic() + wait_s
        while not self._downloads and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
        out, self._downloads = self._downloads, []
        return out

    def blocked_requests(self):
        out, self._blocked = self._blocked, []
        return out

    def close(self):
        try:
            self.context.close()
        except Exception:
            pass
