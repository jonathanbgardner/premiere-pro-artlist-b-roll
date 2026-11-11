"""Candidate shots (pre-rules) and conversion to contract shots."""
from __future__ import annotations

from dataclasses import dataclass, field

VIDEO_TYPES = ("stock_video", "ai_video")


@dataclass
class Candidate:
    start_s: float
    duration_s: float
    type: str = "stock_video"
    reason: str = ""
    queries: list[str] = field(default_factory=list)
    ai_prompt: str | None = None
    sequence: bool = False        # explicit reason flag: may sit back to back with the previous clip
    priority: int = 2             # 1 = must-have, 3 = nice-to-have
    trigger: str = ""             # concrete_noun | place | action | stat | abstract | talking_head
    # music beds only
    section: str | None = None
    mood: str | None = None
    tempo: str | None = None
    origin_s: float | None = None  # where the source asked for it (set by rules, limits shifting)

    @property
    def end_s(self) -> float:
        return self.start_s + self.duration_s

    @property
    def is_video(self) -> bool:
        return self.type in VIDEO_TYPES


@dataclass
class MusicSpec:
    section: str
    mood: str
    tempo: str
    queries: list[str]
    reason: str = ""


def clean_queries(queries: list[str], limit: int = 3) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for q in queries or []:
        q = " ".join(str(q).split()).strip(" .,;\"'")
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:limit]
