"""Fakes for workstream B tests: scripted LLM + fake browser. No network."""
from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from pipeline.artlist_download.computer import Computer, ComputerError
from pipeline.artlist_download.guardrails import UrlPolicy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "shot_list_5.json"
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")

_ids = itertools.count(1)


def tu(name: str, **inp) -> SimpleNamespace:
    """A computer-toolset tool_use block."""
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids):04d}", name=name, input=inp,
                           toolset_name="computer")


def ct(name: str, **inp) -> SimpleNamespace:
    """A custom-tool tool_use block."""
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids):04d}", name=name, input=inp,
                           toolset_name=None)


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


class ScriptedLLM:
    """Returns one scripted turn per call. A turn is a list of blocks, or a
    callable(request) -> list of blocks. Records every request."""

    def __init__(self, turns: list, usage: dict | None = None):
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 100}

    def create(self, **request: Any):
        request = dict(request, messages=list(request["messages"]))  # snapshot: the agent keeps appending
        self.requests.append(request)
        if not self.turns:
            raise AssertionError("ScriptedLLM ran out of turns")
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(request)
        has_tool = any(getattr(b, "type", None) == "tool_use" for b in turn)
        stop = "tool_use" if has_tool else "end_turn"
        if turn and getattr(turn[0], "type", None) == "refusal":
            stop, turn = "refusal", []
        return SimpleNamespace(content=turn, stop_reason=stop, usage=SimpleNamespace(**self.usage))

    def last_user_content(self, i: int = -1) -> list:
        return self.requests[i]["messages"][-1]["content"]


class FakeDownload:
    def __init__(self, url: str, suggested_filename: str, data: bytes = b"\x00fakevideo" * 10):
        self.url = url
        self.suggested_filename = suggested_filename
        self.data = data

    def save_as(self, path: str) -> None:
        Path(path).write_bytes(self.data)


class FakeComputer(Computer):
    """In-memory browser. `on_click(x, y, fake)` lets a test script what a click does."""

    def __init__(self, policy: UrlPolicy, *, on_click: Callable | None = None, width=1280, height=800):
        self.policy = policy
        self.width, self.height = width, height
        self.url = "about:blank"
        self.history: list[str] = []
        self.on_click = on_click
        self.signals_override: dict = {}
        self.downloads: list = []
        self.blocked: list[str] = []
        self.actions: list[tuple] = []
        self.closed = False
        self.cursor = (0, 0)

    # simulate the route interceptor
    def _nav(self, url: str, through_route: bool = True) -> None:
        if through_route:
            reason = self.policy.check_navigation(url)
            if reason:
                self.blocked.append(f"{url} ({reason})")
                return
        self.history.append(self.url)
        self.url = url

    def screenshot(self):
        self.actions.append(("screenshot",))
        return PNG_1x1

    def click(self, x, y, button="left", count=1, modifiers=None):
        if x is not None and not (0 <= x < self.width and 0 <= y < self.height):
            raise ComputerError("outside screen")
        self.actions.append(("click", x, y, button, count, tuple(modifiers or ())))
        if self.on_click:
            self.on_click(x, y, self)

    def mouse_move(self, x, y):
        self.actions.append(("move", x, y))
        self.cursor = (x, y)

    def mouse_down(self):
        self.actions.append(("down",))

    def mouse_up(self):
        self.actions.append(("up",))

    def drag(self, x0, y0, x1, y1, modifiers=None):
        self.actions.append(("drag", x0, y0, x1, y1))

    def type_text(self, text):
        self.actions.append(("type", text))

    def key(self, combo, repeat=1):
        self.actions.append(("key", combo, repeat))

    def hold_key(self, combo, duration_s):
        self.actions.append(("hold_key", combo, duration_s))

    def scroll(self, direction, amount, x=None, y=None, modifiers=None):
        self.actions.append(("scroll", direction, amount, x, y))

    def wait(self, seconds):
        self.actions.append(("wait", seconds))

    def cursor_position(self):
        return self.cursor

    def goto(self, url):
        self.actions.append(("goto", url))
        self._nav(url)

    def current_url(self):
        return self.url

    def go_back_or(self, fallback_url):
        self.actions.append(("back",))
        prev = self.history.pop() if self.history else fallback_url
        if self.policy.check_navigation(prev) is not None:
            prev = fallback_url
        self.url = prev

    def page_signals(self):
        sig = {"url": self.url, "title": "Artlist", "has_password_input": False, "captcha_frames": [],
               "text_snippet": "Stock footage results"}
        sig.update(self.signals_override)
        return sig

    def take_downloads(self, wait_s=0.0):
        out, self.downloads = self.downloads, []
        return out

    def blocked_requests(self):
        out, self.blocked = self.blocked, []
        return out

    def close(self):
        self.closed = True


def make_project(tmp_path: Path, mutate: Callable[[dict], None] | None = None) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    data = json.loads(FIXTURE.read_text())
    if mutate:
        mutate(data)
    (root / "shot_list.json").write_text(json.dumps(data, indent=2))
    for d in ("assets", "logs", "transcript", "scripts"):
        (root / d).mkdir(exist_ok=True)
    return root


def copy_fixture(dst: Path) -> None:
    shutil.copy(FIXTURE, dst)
