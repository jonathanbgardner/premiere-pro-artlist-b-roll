"""Browser abstraction the agent drives.

`Computer` is what the agent loop needs; `PlaywrightComputer`
(playwright_backend.py) is the real one and tests use a fake. Coordinates are
in screenshot pixel space (= the viewport, no scaling).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol


class ComputerError(Exception):
    pass


class DownloadLike(Protocol):
    url: str
    suggested_filename: str

    def save_as(self, path: str) -> None: ...


class Computer(ABC):
    width: int
    height: int

    # --- computer-use actions
    @abstractmethod
    def screenshot(self) -> bytes:
        """PNG bytes of the visible viewport."""

    @abstractmethod
    def click(self, x: int | None, y: int | None, button: str = "left", count: int = 1,
              modifiers: list[str] | None = None) -> None: ...

    @abstractmethod
    def mouse_move(self, x: int, y: int) -> None: ...

    @abstractmethod
    def mouse_down(self) -> None: ...

    @abstractmethod
    def mouse_up(self) -> None: ...

    @abstractmethod
    def drag(self, x0: int, y0: int, x1: int, y1: int, modifiers: list[str] | None = None) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def key(self, combo: str, repeat: int = 1) -> None: ...

    @abstractmethod
    def hold_key(self, combo: str, duration_s: float) -> None: ...

    @abstractmethod
    def scroll(self, direction: str, amount: float, x: int | None = None, y: int | None = None,
               modifiers: list[str] | None = None) -> None: ...

    @abstractmethod
    def wait(self, seconds: float) -> None: ...

    @abstractmethod
    def cursor_position(self) -> tuple[int, int]: ...

    # --- browser helpers used by guardrails / custom tools
    @abstractmethod
    def goto(self, url: str) -> None: ...

    @abstractmethod
    def current_url(self) -> str: ...

    @abstractmethod
    def go_back_or(self, fallback_url: str) -> None:
        """Leave a disallowed page: history back, else goto fallback_url."""

    @abstractmethod
    def page_signals(self) -> dict:
        """{url, title, has_password_input, captcha_frames, text_snippet}"""

    @abstractmethod
    def take_downloads(self, wait_s: float = 0.0) -> list[DownloadLike]:
        """Return (and forget) downloads started since the last call; wait up to wait_s for one."""

    def blocked_requests(self) -> list[str]:
        """Return (and forget) URLs the route interceptor aborted."""
        return []

    def close(self) -> None:
        pass


# xdotool-style key names (what Claude tends to send) -> Playwright names.
_KEY_MAP = {
    "return": "Enter", "enter": "Enter", "kp_enter": "Enter",
    "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": " ", "backspace": "Backspace", "delete": "Delete",
    "page_down": "PageDown", "pagedown": "PageDown", "next": "PageDown",
    "page_up": "PageUp", "pageup": "PageUp", "prior": "PageUp",
    "home": "Home", "end": "End", "insert": "Insert",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "arrowup": "ArrowUp", "arrowdown": "ArrowDown", "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
    "ctrl": "Control", "control": "Control", "control_l": "Control", "control_r": "Control",
    "shift": "Shift", "shift_l": "Shift", "shift_r": "Shift",
    "alt": "Alt", "alt_l": "Alt", "alt_r": "Alt", "option": "Alt",
    "super": "Meta", "super_l": "Meta", "meta": "Meta", "cmd": "Meta", "command": "Meta", "win": "Meta",
}


def to_playwright_key(combo: str) -> str:
    """'ctrl+shift+Return' -> 'Control+Shift+Enter'; 'F5' -> 'F5'; 'a' -> 'a'."""
    out = []
    for part in combo.split("+"):
        p = part.strip()
        if not p:
            continue
        low = p.lower()
        if low in _KEY_MAP:
            out.append(_KEY_MAP[low])
        elif len(low) >= 2 and low[0] == "f" and low[1:].isdigit():
            out.append(p.upper())
        else:
            out.append(p)
    return "+".join(out) if out else combo


def parse_modifiers(text: str | None) -> list[str]:
    if not text:
        return []
    return [to_playwright_key(p) for p in text.split("+") if p.strip()]
