"""Per-shot computer-use agent loop (manual loop over the Messages API).

One fresh conversation per shot. The model drives the browser with the
computer toolset and finishes by calling one of the custom tools:
report_pick / report_no_match (or request_human when blocked). Every
guardrail and acceptance rule here is enforced in code.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urljoin, urlsplit

from .computer import Computer, ComputerError, parse_modifiers
from .config import MUSIC_EXTS, VIDEO_EXTS, Settings
from .guardrails import HumanGate, NeedsHuman, UrlPolicy, detect_blocker, host_allowed
from .llm import LLMClient, block_get, block_input

COMPUTER_TOOLSET = {"type": "computer_toolset_20260801", "configs": {"zoom": {"enabled": False}}}
CUSTOM_TOOL_NAMES = {"navigate", "report_pick", "report_no_match", "request_human"}

CUSTOM_TOOLS = [
    {
        "name": "navigate",
        "description": "Open a URL in the browser tab (there is no address bar in the screenshots). "
                       "Only artlist.io pages are allowed; account, billing, settings and subscription "
                       "pages are blocked.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["url"],
                         "properties": {"url": {"type": "string", "description": "Absolute https URL."}}},
    },
    {
        "name": "report_pick",
        "description": "Call AFTER you clicked Download for the chosen clip and the download started. "
                       "Reports the chosen clip for this shot. The harness verifies the download and the "
                       "requirements; if it returns an error, fix the problem and call again.",
        "strict": True,
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "url", "resolution", "duration_s", "confidence", "query"],
            "properties": {
                "title": {"type": "string", "description": "Clip/track title exactly as shown on Artlist."},
                "url": {"type": "string", "description": "The clip/track page URL on artlist.io."},
                "resolution": {"type": "string",
                               "description": "Downloaded resolution, e.g. '1920x1080', '3840x2160', '4K'. "
                                              "For music use 'audio'."},
                "duration_s": {"type": "number", "description": "Clip/track length in seconds."},
                "confidence": {"type": "number",
                               "description": "0-1: how well the clip matches the shot's reason."},
                "query": {"type": "string", "description": "The search query that found it."},
            },
        },
    },
    {
        "name": "report_no_match",
        "description": "Call when none of the shot's queries produced a clip that meets the requirements "
                       "and fits the reason. The shot will be sent to AI generation instead.",
        "strict": True,
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["reason", "queries_tried"],
            "properties": {
                "reason": {"type": "string"},
                "queries_tried": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "request_human",
        "description": "Call when a login form, captcha, payment/plan prompt or anything else needs a person. "
                       "Never type credentials or solve captchas yourself.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                         "properties": {"reason": {"type": "string"}}},
    },
]

SYSTEM_PROMPT = """\
You find and download stock footage and music on artlist.io for a video editor's B-roll pipeline. \
You control a Chromium tab that is already logged in to Artlist, through the computer tools \
(screenshots, clicks, typing, scrolling) and a `navigate` tool for opening URLs.

How to work on a shot:
1. Search Artlist with query 1 (use the site's search box, or `navigate` to a search URL).
2. Review the top results (hover previews or open clip pages; check length and resolution).
3. Pick the best match by relevance to the shot's reason first, then quality. Hard requirements:
   - length >= the required minimum length given for the shot;
   - stock video: downloadable at 1080p (1920x1080) or higher; prefer an MP4 or MOV file;
   - music: WAV or MP3.
4. If nothing on query 1 fits, try query 2, then query 3.
5. When you found a clip: click Download (choose the >=1080p video option), wait a few seconds, then call \
`report_pick`. If nothing fits after all queries: call `report_no_match` with every query you tried.

Rules (the harness also enforces these and will block you):
- Stay on artlist.io. Do not open other websites.
- Never open or change account, profile, billing, plan, subscription or settings pages, and never \
buy, upgrade, or accept a paid offer.
- If you see a login form, a captcha or "verify you are human", call `request_human`. Never type \
passwords and never try to solve captchas.
- Do one download per shot. Be efficient: take a screenshot when you need to see the page.
- Confidence: 0.9+ means a near-perfect match for the reason; 0.6-0.8 good; below 0.6 weak.
"""


@dataclass
class ShotResult:
    status: str                       # downloaded | needs_ai | failed
    file: str | None = None           # relative to project_root
    title: str = ""
    url: str = ""
    resolution: str = ""
    duration_s: float | None = None
    confidence: float = 0.0
    query: str = ""
    error: str = ""
    reason: str = ""                  # no-match reason
    steps: int = 0
    stop_run: bool = False            # e.g. needs_human in non-interactive mode


class RunBudget:
    def __init__(self, settings: Settings):
        self.max_steps = settings.max_total_steps
        self.max_cost = settings.max_total_cost_usd
        self.prices = settings.prices()
        self.steps = 0
        self.cost_usd = 0.0
        self.tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    def add(self, usage: Any) -> None:
        self.steps += 1
        if usage is None:
            return
        g = lambda k: int(block_get(usage, k, 0) or 0)  # noqa: E731
        inp, out = g("input_tokens"), g("output_tokens")
        cw, cr = g("cache_creation_input_tokens"), g("cache_read_input_tokens")
        self.tokens["input"] += inp
        self.tokens["output"] += out
        self.tokens["cache_write"] += cw
        self.tokens["cache_read"] += cr
        p_in, p_out, p_cw, p_cr = self.prices
        self.cost_usd += (inp * p_in + out * p_out + cw * p_cw + cr * p_cr) / 1_000_000

    def exhausted(self) -> str | None:
        if self.steps >= self.max_steps:
            return f"run step budget exhausted ({self.steps}/{self.max_steps})"
        if self.cost_usd >= self.max_cost:
            return f"run cost budget exhausted (${self.cost_usd:.2f} >= ${self.max_cost:.2f})"
        return None


class BudgetExhausted(Exception):
    pass


def required_min_length(shot: dict, settings: Settings) -> float:
    return round(float(shot["duration_s"]) + settings.handle_s, 3)


_RES_WORDS = {"8k": 4320, "6k": 3160, "5k": 2880, "4k": 2160, "uhd": 2160, "2k": 1440, "qhd": 1440,
              "full hd": 1080, "fhd": 1080, "hd": 720}


def parse_resolution_height(res: str) -> int | None:
    """'1920x1080' -> 1080, '3840 x 2160' -> 2160, '1080p' -> 1080, '4K' -> 2160."""
    s = (res or "").lower().strip()
    m = re.search(r"(\d{3,5})\s*[x×*]\s*(\d{3,5})", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return min(w, h)  # vertical clips: the short side is the "p" number
    m = re.search(r"(\d{3,4})\s*p\b", s)
    if m:
        return int(m.group(1))
    for word, h in _RES_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", s):
            return h
    return None


def shot_prompt(shot: dict, settings: Settings) -> str:
    kind = "stock video clip" if shot["type"] == "stock_video" else "music track"
    req = [f"- minimum length: {required_min_length(shot, settings):g} s "
           f"(shot is {shot['duration_s']:g} s plus {settings.handle_s:g} s of handles)"]
    if shot["type"] == "stock_video":
        req.append(f"- resolution: {settings.min_video_height}p or higher; file type MP4 or MOV")
    else:
        req.append("- file type WAV or MP3")
        for k in ("section", "mood", "tempo"):
            if shot.get(k):
                req.append(f"- {k}: {shot[k]}")
    queries = "\n".join(f"  {i}. {q}" for i, q in enumerate(shot.get("queries") or [], 1))
    return (f"Shot {shot['id']}: find one Artlist {kind}.\n"
            f"Reason (what the editor needs to show/hear): {shot['reason']}\n"
            f"Search queries, most specific first:\n{queries}\n"
            f"Requirements:\n" + "\n".join(req) + "\n"
            "Start with query 1. Finish with report_pick or report_no_match.")


def _tool_result(tool_use_id: str, content: Any, *, is_error: bool = False, toolset: bool = False) -> dict:
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    r = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        r["is_error"] = True
    if toolset:
        r["toolset_name"] = "computer"
    return r


class ShotAgent:
    def __init__(self, *, computer: Computer, llm: LLMClient, settings: Settings, gate: HumanGate,
                 budget: RunBudget, project_root: str | Path, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic):
        self.c = computer
        self.llm = llm
        self.s = settings
        self.gate = gate
        self.budget = budget
        self.root = Path(project_root)
        self.policy = UrlPolicy.from_settings(settings)
        self.log = log
        self.clock = clock

    # ------------------------------------------------------------------ public
    def run_shot(self, shot: dict) -> ShotResult:
        self.shot = shot
        self.steps = 0
        self.violations = 0
        self.downloads: list = []
        self.start = self.clock()
        self.last_good_url = self.s.start_url
        try:
            return self._run()
        except NeedsHuman as e:
            return ShotResult("failed", error="needs_human", reason=str(e), steps=self.steps, stop_run=True)
        except BudgetExhausted as e:
            return ShotResult("pending", error=str(e), steps=self.steps, stop_run=True)

    # ------------------------------------------------------------------ loop
    def _start_url(self) -> str:
        tmpl = self.s.video_search_url if self.shot["type"] == "stock_video" else self.s.music_search_url
        queries = self.shot.get("queries") or []
        if tmpl and queries:
            return tmpl.replace("{query}", quote_plus(queries[0]))
        return self.s.start_url

    def _fail(self, error: str) -> ShotResult:
        return ShotResult("failed", error=error, steps=self.steps)

    def _run(self) -> ShotResult:
        start = self._start_url()
        if self.policy.check_navigation(start):
            return self._fail(f"guardrail: start URL not allowed: {self.policy.check_navigation(start)}")
        try:
            self.c.goto(start)
        except ComputerError as e:
            return self._fail(f"browser: {e}")
        self.c.take_downloads(0)  # discard anything stale from a previous shot
        self.c.blocked_requests()
        self._ensure_no_blocker()
        self.last_good_url = self.c.current_url() or start

        messages: list[dict] = [{"role": "user", "content": shot_prompt(self.shot, self.s)}]
        nudges = 0
        while True:
            if (why := self.budget.exhausted()):
                raise BudgetExhausted(why)
            if self.steps >= self.s.max_steps_per_shot:
                return self._fail(f"max steps per shot reached ({self.s.max_steps_per_shot})")
            if self.clock() - self.start > self.s.shot_timeout_s:
                return self._fail(f"shot timeout after {self.s.shot_timeout_s:g} s")

            resp = self.llm.create(
                model=self.s.model, max_tokens=self.s.max_tokens, system=SYSTEM_PROMPT,
                tools=[COMPUTER_TOOLSET, *CUSTOM_TOOLS], messages=messages,
                output_config={"effort": self.s.effort}, cache_control={"type": "ephemeral"},
            )
            self.steps += 1
            self.budget.add(block_get(resp, "usage"))
            content = list(block_get(resp, "content", []) or [])
            messages.append({"role": "assistant", "content": content})
            stop = block_get(resp, "stop_reason")
            if stop == "refusal":
                return self._fail("model_refusal")

            tool_uses = [b for b in content if block_get(b, "type") == "tool_use"]
            if not tool_uses:
                if nudges >= 2:
                    return self._fail("model stopped without report_pick/report_no_match")
                nudges += 1
                messages.append({"role": "user", "content": "Continue. You must finish this shot by calling "
                                 "report_pick (after downloading) or report_no_match."})
                continue

            results, done, notes = self._handle_turn(tool_uses)
            if done is not None:
                return done
            user_content: list[dict] = list(results)
            for n in notes:
                user_content.append({"type": "text", "text": n})
            messages.append({"role": "user", "content": user_content})

    # ------------------------------------------------------------------ one turn
    def _handle_turn(self, tool_uses: list) -> tuple[list[dict], ShotResult | None, list[str]]:
        results: list[dict] = []
        notes: list[str] = []
        computer_failed = False
        did_computer = False
        for b in tool_uses:
            tid, name = block_get(b, "id"), block_get(b, "name")
            inp = block_input(b)
            is_toolset = block_get(b, "toolset_name") == "computer" or name not in CUSTOM_TOOL_NAMES
            if is_toolset:
                if computer_failed:
                    results.append(_tool_result(tid, "Not executed: an earlier computer action in this turn "
                                                "failed.", is_error=True, toolset=True))
                    continue
                if self.clock() - self.start > self.s.shot_timeout_s:
                    results.append(_tool_result(tid, "Not executed: shot timeout.", is_error=True, toolset=True))
                    computer_failed = True
                    continue
                did_computer = True
                try:
                    results.append(_tool_result(tid, self._computer_action(name, inp), toolset=True))
                except (ComputerError, KeyError, ValueError, TypeError) as e:
                    computer_failed = True
                    results.append(_tool_result(tid, f"Error: {e}", is_error=True, toolset=True))
                self.downloads.extend(self.c.take_downloads(0))
                continue

            # custom tools: run post-action checks first so a pick is never
            # accepted from a page we should not be on
            if did_computer:
                notes += self._post_action_checks()
                did_computer = False
            if name == "navigate":
                results.append(self._navigate(tid, inp))
                notes += self._post_action_checks()
            elif name == "request_human":
                self.gate.pause(self.shot["id"], inp.get("reason", "agent requested help"))
                results.append(_tool_result(tid, "A person handled it. Take a new screenshot and continue."))
            elif name == "report_no_match":
                r = self._report_no_match(tid, inp)
                if isinstance(r, ShotResult):
                    return results, r, notes
                results.append(r)
            elif name == "report_pick":
                r = self._report_pick(tid, inp)
                if isinstance(r, ShotResult):
                    return results, r, notes
                results.append(r)
            else:  # pragma: no cover - guarded by CUSTOM_TOOL_NAMES
                results.append(_tool_result(tid, f"unknown tool {name}", is_error=True))
            if self.violations > self.s.max_guardrail_violations:
                return results, self._fail(f"guardrail: too many blocked navigations ({self.violations})"), notes
        if did_computer:
            notes += self._post_action_checks()
        if self.violations > self.s.max_guardrail_violations:
            return results, self._fail(f"guardrail: too many blocked navigations ({self.violations})"), notes
        return results, None, notes

    # ------------------------------------------------------------------ guardrails
    def _post_action_checks(self) -> list[str]:
        notes: list[str] = []
        blocked = self.c.blocked_requests()
        if blocked:
            self.violations += len(blocked)
            notes.append("GUARDRAIL: the harness blocked navigation to: " + "; ".join(blocked[:3]) +
                         ". Stay on artlist.io search and clip pages.")
        url = self.c.current_url()
        reason = self.policy.check_navigation(url)
        if reason:
            if not blocked:  # an aborted navigation leaves an error page; don't count it twice
                self.violations += 1
            self.log(f"[guardrail] {self.shot['id']}: left allowed area ({url}): {reason}; going back")
            self.c.go_back_or(self.last_good_url)
            notes.append(f"GUARDRAIL: you ended up on a disallowed page ({reason}). The harness navigated "
                         "back. Take a screenshot before continuing.")
        self._ensure_no_blocker()
        url = self.c.current_url()
        if self.policy.check_navigation(url) is None:
            self.last_good_url = url
        return notes

    def _ensure_no_blocker(self) -> None:
        for _ in range(3):
            why = detect_blocker(self.c.page_signals(), self.policy)
            if not why:
                return
            self.log(f"[guardrail] {self.shot['id']}: {why}")
            self.gate.pause(self.shot["id"], why)   # raises NeedsHuman when non-interactive
        why = detect_blocker(self.c.page_signals(), self.policy)
        if why:
            raise NeedsHuman(f"still blocked after 3 pauses: {why}")

    def _navigate(self, tid: str, inp: dict) -> dict:
        url = str(inp.get("url", "")).strip()
        reason = self.policy.check_navigation(url)
        if reason:
            self.violations += 1
            self.log(f"[guardrail] {self.shot['id']}: navigate refused: {url}: {reason}")
            return _tool_result(tid, f"Refused by harness: {reason}", is_error=True)
        try:
            self.c.goto(url)
        except ComputerError as e:
            return _tool_result(tid, f"Error: {e}", is_error=True)
        self.downloads.extend(self.c.take_downloads(0))
        return _tool_result(tid, f"Opened {self.c.current_url()}. Take a screenshot to see it.")

    # ------------------------------------------------------------------ computer
    def _computer_action(self, name: str, inp: dict) -> Any:
        c = self.c
        xy = inp.get("coordinate")
        x, y = (int(xy[0]), int(xy[1])) if xy else (None, None)
        mods = parse_modifiers(inp.get("text")) if name not in ("type", "key", "hold_key") else []
        if name == "screenshot":
            data = base64.standard_b64encode(c.screenshot()).decode("ascii")
            return [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]
        if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            button = {"right_click": "right", "middle_click": "middle"}.get(name, "left")
            count = {"double_click": 2, "triple_click": 3}.get(name, 1)
            c.click(x, y, button=button, count=count, modifiers=mods)
        elif name == "left_click_drag":
            sx, sy = inp["start_coordinate"]
            c.drag(int(sx), int(sy), int(x), int(y), modifiers=mods)
        elif name == "mouse_move":
            c.mouse_move(int(x), int(y))
        elif name == "left_mouse_down":
            c.mouse_down()
        elif name == "left_mouse_up":
            c.mouse_up()
        elif name == "cursor_position":
            cx, cy = c.cursor_position()
            return f"[{cx}, {cy}]"
        elif name == "scroll":
            c.scroll(inp["scroll_direction"], float(inp.get("scroll_amount", 3)), x, y, modifiers=mods)
        elif name == "type":
            c.type_text(str(inp["text"]))
        elif name == "key":
            c.key(str(inp["text"]), int(inp.get("repeat", 1)))
        elif name == "hold_key":
            c.hold_key(str(inp["text"]), float(inp.get("duration", 1)))
        elif name == "wait":
            c.wait(float(inp.get("duration", 1)))
        else:
            raise ValueError(f"computer tool {name!r} is not supported by this harness")
        return "OK"

    # ------------------------------------------------------------------ results
    def _report_no_match(self, tid: str, inp: dict) -> ShotResult | dict:
        tried = {q.strip().lower() for q in inp.get("queries_tried") or []}
        missing = [q for q in self.shot.get("queries") or [] if q.strip().lower() not in tried]
        if missing:
            return _tool_result(tid, "Not accepted: you must try every query before giving up. Not tried yet: "
                                + json.dumps(missing), is_error=True)
        return ShotResult("needs_ai", reason=str(inp.get("reason", "")), steps=self.steps)

    def _report_pick(self, tid: str, inp: dict) -> ShotResult | dict:
        shot = self.shot
        problems = []
        try:
            dur = float(inp.get("duration_s"))
        except (TypeError, ValueError):
            dur = -1.0
        need = required_min_length(shot, self.s)
        if dur < need:
            problems.append(f"clip length {dur:g} s is shorter than the required {need:g} s")
        try:
            conf = float(inp.get("confidence"))
        except (TypeError, ValueError):
            conf = -1.0
        if not 0.0 <= conf <= 1.0:
            problems.append("confidence must be between 0 and 1")
        res = str(inp.get("resolution", ""))
        if shot["type"] == "stock_video":
            h = parse_resolution_height(res)
            if h is None:
                problems.append(f"resolution {res!r} not understood; give WIDTHxHEIGHT like 1920x1080")
            elif h < self.s.min_video_height:
                problems.append(f"resolution {res!r} is below {self.s.min_video_height}p")
        url = str(inp.get("url", "")).strip()
        if url and not urlsplit(url).scheme:
            url = urljoin(self.c.current_url(), url)
        if not url:
            url = self.c.current_url()
        if not host_allowed(urlsplit(url).hostname or "", self.s.allowed_domains):
            problems.append(f"clip URL {url!r} is not an Artlist URL")
        if problems:
            self.downloads.clear()  # a rejected pick's download must not be reused
            return _tool_result(tid, "Pick rejected: " + "; ".join(problems) +
                                ". Choose a clip that meets the requirements (or try the next query).",
                                is_error=True)

        self.downloads.extend(self.c.take_downloads(0))
        if not self.downloads:
            self.downloads.extend(self.c.take_downloads(min(self.s.download_timeout_s, 15.0)))
        if not self.downloads:
            return _tool_result(tid, "No download was captured. Click the Download button for this clip "
                                "(highest quality >=1080p MP4/MOV for video, WAV/MP3 for music), wait, then "
                                "call report_pick again.", is_error=True)
        dl = self.downloads[-1]
        exts = VIDEO_EXTS if shot["type"] == "stock_video" else MUSIC_EXTS
        ext = os.path.splitext(dl.suggested_filename or "")[1].lower()
        if ext not in exts:
            self.downloads.clear()
            return _tool_result(tid, f"The downloaded file {dl.suggested_filename!r} is not one of {exts}. "
                                "Download the right format and call report_pick again.", is_error=True)
        try:
            rel = place_download(dl, self.root, shot["id"], ext, timeout_s=self.s.download_timeout_s)
        except Exception as e:  # noqa: BLE001 - surface any save failure as a shot failure
            return self._fail(f"download save failed: {e}")
        self.downloads.clear()
        return ShotResult("downloaded", file=rel, title=str(inp.get("title", "")), url=url,
                          resolution=res, duration_s=dur, confidence=round(conf, 3),
                          query=str(inp.get("query", "")), steps=self.steps)


def place_download(dl, project_root: Path, shot_id: str, ext: str, timeout_s: float = 120.0) -> str:
    """Save a Playwright-like download to assets/<shot_id><ext> atomically; return the relative path."""
    assets = Path(project_root) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    tmp = assets / f".{shot_id}.part{ext}"
    final = assets / f"{shot_id}{ext}"
    dl.save_as(str(tmp))  # Playwright blocks until the download completes
    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise ComputerError("downloaded file is empty")
    os.replace(tmp, final)
    return f"assets/{shot_id}{ext}"
