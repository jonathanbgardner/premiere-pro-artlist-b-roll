"""Deterministic rule enforcement. Runs AFTER candidate generation (LLM or
heuristic); nothing here trusts the source to have followed the rules.

Video rules (stock_video / ai_video):
1. duration clamped to [min_clip_s, max_clip_s] (3-8 s);
2. never inside the first ``speaker_protect_s`` (10 s) after a speaker starts
   talking: shrink to end before the window, or shift past it (bounded by
   ``max_shift_s`` from where the source wanted it), else drop;
3. never past the end of the transcript;
4. no two clips back to back: the gap to the previous clip must be at least
   ``min_gap_s`` (2 s) unless the later clip carries ``sequence: True``
   (then it may butt up against it, but never overlap). Conflicts are resolved
   by keeping the better clip (higher priority, then less displaced) in place
   and moving the other after it, else dropping it;
5. at most ``max_ai_clips`` ai_video shots; extras become stock_video
   (their ai_prompt is kept so D can still use it if stock fails);
6. every uncovered talking-head stretch longer than ``talking_head_s`` gets a
   filler cutaway if one fits anywhere inside it.

Music beds are exempt from 1/2/4, span their section, and are clamped to the
transcript end.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable

from pipeline.common.shotlist import seconds_to_tc

from .beats import music_sections, speaker_change_times, transcript_end
from .candidates import Candidate, MusicSpec, clean_queries
from .transcript import Segment

EPS = 1e-6


@dataclass
class RuleConfig:
    fps: float = 29.97
    drop_frame: bool = False
    min_clip_s: float = 3.0
    max_clip_s: float = 8.0
    speaker_protect_s: float = 10.0
    protect_first_speaker: bool = True
    min_gap_s: float = 2.0
    max_shift_s: float = 12.0
    talking_head_s: float = 20.0
    filler_durations: tuple[float, ...] = (5.0, 4.0, 3.0)
    max_ai_clips: int = 8


def true_rate(fps: float) -> float:
    nom = round(fps)
    return nom * 1000 / 1001 if abs(fps - nom) > 0.001 else float(nom)


@dataclass
class Timeline:
    end_s: float
    windows: list[tuple[float, float]]
    cfg: RuleConfig
    log: list[str] = field(default_factory=list)

    @classmethod
    def from_segments(cls, segments: list[Segment], cfg: RuleConfig) -> "Timeline":
        T = transcript_end(segments)
        wins = [(t, min(T, t + cfg.speaker_protect_s))
                for t in speaker_change_times(segments, cfg.protect_first_speaker)]
        return cls(T, wins, cfg)

    # frame helpers: starts are snapped to whole frames so start_tc round-trips exactly
    def snap_up(self, t: float) -> float:
        r = true_rate(self.cfg.fps)
        return math.ceil(t * r - 1e-4) / r

    def snap_down(self, t: float) -> float:
        r = true_rate(self.cfg.fps)
        return math.floor(t * r + 1e-4) / r

    def window_hit(self, start: float, end: float) -> tuple[float, float] | None:
        for w0, w1 in self.windows:
            if start < w1 - EPS and end > w0 + EPS:
                return (w0, w1)
        return None

    def fit(self, start: float, dur: float, origin: float | None = None) -> tuple[float, float] | None:
        """Adjust (start, dur) to satisfy duration, transcript-end and speaker-window
        rules. Returns None if impossible within ``max_shift_s`` of ``origin``."""
        c = self.cfg
        origin = start if origin is None else origin
        dur = min(max(dur, c.min_clip_s), c.max_clip_s)
        start = max(0.0, start)
        for _ in range(8):
            start = self.snap_up(start)
            if start + dur > self.end_s + EPS:
                start = self.snap_down(self.end_s - dur)
                if start < 0:
                    start, dur = 0.0, self.end_s
            hit = self.window_hit(start, start + dur)
            if hit is None:
                break
            w0, w1 = hit
            if start < w0 and w0 - start >= c.min_clip_s - EPS:
                dur = w0 - start          # shrink so it ends where the speaker starts
            else:
                start = w1                # move past the protected window
        else:
            return None
        dur = math.floor(dur * 1000 + 1e-6) / 1000
        if start + dur > self.end_s + EPS:
            dur = math.floor((self.end_s - start) * 1000 + 1e-6) / 1000
        if dur < c.min_clip_s - EPS or dur > c.max_clip_s + EPS:
            return None
        if self.window_hit(start, start + dur) or start < 0:
            return None
        if abs(start - origin) > c.max_shift_s + EPS:
            return None
        return start, dur

    def gap_ok(self, start: float, end: float, shots: list[Candidate], sequence: bool = False) -> bool:
        for s in shots:
            if not s.is_video:
                continue
            if s.end_s <= start + EPS:          # s before
                need = 0.0 if sequence else self.cfg.min_gap_s
                if start - s.end_s < need - EPS:
                    return False
            elif s.start_s >= end - EPS:        # s after
                need = 0.0 if s.sequence else self.cfg.min_gap_s
                if s.start_s - end < need - EPS:
                    return False
            else:
                return False                    # overlap
        return True


# ------------------------------------------------------------------ passes

def _sanitize(c: Candidate, tl: Timeline) -> Candidate | None:
    c = replace(c, queries=clean_queries(c.queries))
    if not c.queries:
        tl.log.append(f"drop @{c.start_s:.2f}: no queries")
        return None
    if c.type not in ("stock_video", "ai_video"):
        c.type = "stock_video"
    if c.type == "ai_video" and not (c.ai_prompt or "").strip():
        c.ai_prompt = c.reason or c.queries[0]
    if c.type == "stock_video" and c.ai_prompt is not None and not c.ai_prompt.strip():
        c.ai_prompt = None
    c.priority = min(3, max(1, int(c.priority or 2)))
    c.reason = (c.reason or c.queries[0]).strip()
    try:
        c.start_s, c.duration_s = float(c.start_s), float(c.duration_s)
    except (TypeError, ValueError):
        return None
    if math.isnan(c.start_s) or math.isnan(c.duration_s) or c.start_s >= tl.end_s:
        tl.log.append(f"drop @{c.start_s}: outside transcript")
        return None
    c.origin_s = c.start_s if c.origin_s is None else c.origin_s
    return c


def _place(c: Candidate, tl: Timeline) -> Candidate | None:
    r = tl.fit(c.start_s, c.duration_s, c.origin_s)
    if r is None:
        tl.log.append(f"drop @{c.start_s:.2f} '{c.reason[:40]}': no legal slot near it")
        return None
    c.start_s, c.duration_s = r
    return c


def _shift(c: Candidate) -> float:
    return round(abs(c.start_s - (c.origin_s if c.origin_s is not None else c.start_s)), 3)


def _resolve_back_to_back(cands: list[Candidate], tl: Timeline) -> list[Candidate]:
    gap = tl.cfg.min_gap_s
    kept: list[Candidate] = []
    for c in sorted(cands, key=lambda x: (x.start_s, x.priority)):
        if not kept:
            kept.append(c)
            continue
        prev = kept[-1]
        need = 0.0 if c.sequence else gap
        if c.start_s >= prev.end_s + need - EPS:
            kept.append(c)
            continue
        # Conflict. The better clip (higher priority, then less displaced from where
        # it was asked for) keeps its slot; the other one tries to move after it.
        before = kept[-2] if len(kept) > 1 else None
        c_better = (c.priority, _shift(c)) < (prev.priority, _shift(prev))
        if c_better and (before is None or c.start_s >= before.end_s + need - EPS):
            kept[-1] = c
            p_need = 0.0 if prev.sequence else gap
            r = tl.fit(c.end_s + p_need, prev.duration_s, prev.origin_s)
            if r and r[0] >= c.end_s + p_need - EPS:
                prev.start_s, prev.duration_s = r
                kept.append(prev)
            else:
                tl.log.append(f"drop @{prev.start_s:.2f} '{prev.reason[:40]}': back to back, lost to "
                              f"'{c.reason[:30]}'")
            continue
        r = tl.fit(prev.end_s + need, c.duration_s, c.origin_s)
        if r and r[0] >= prev.end_s + need - EPS:
            c.start_s, c.duration_s = r
            kept.append(c)
            continue
        tl.log.append(f"drop @{c.start_s:.2f} '{c.reason[:40]}': back to back with previous clip")
    return kept


def _cap_ai(cands: list[Candidate], tl: Timeline) -> None:
    ai = sorted((c for c in cands if c.type == "ai_video"), key=lambda x: (x.priority, x.start_s))
    for c in ai[tl.cfg.max_ai_clips:]:
        c.type = "stock_video"
        tl.log.append(f"ai cap: @{c.start_s:.2f} '{c.reason[:40]}' -> stock_video")


def uncovered(tl: Timeline, shots: list[Candidate], start: float = 0.0, end: float | None = None
              ) -> list[tuple[float, float]]:
    end = tl.end_s if end is None else end
    out, cur = [], start
    for s in sorted((s for s in shots if s.is_video), key=lambda x: x.start_s):
        if s.start_s > cur:
            out.append((cur, min(s.start_s, end)))
        cur = max(cur, s.end_s)
        if cur >= end:
            break
    if cur < end:
        out.append((cur, end))
    return [(a, b) for a, b in out if b > a]


FillerFn = Callable[[float, float, frozenset], Candidate | None]


def _fill_talking_heads(cands: list[Candidate], tl: Timeline, filler: FillerFn | None) -> list[Candidate]:
    if filler is None:
        return cands
    c = tl.cfg
    shots = list(cands)
    tried: set[tuple[int, int]] = set()
    for _ in range(500):
        stretch = next(((a, b) for a, b in uncovered(tl, shots)
                        if b - a > c.talking_head_s and (round(a * 10), round(b * 10)) not in tried), None)
        if stretch is None:
            break
        a, b = stretch
        tried.add((round(a * 10), round(b * 10)))
        mid = (a + b) / 2
        placed = None
        for dur in c.filler_durations:
            starts = []
            t = a
            while t + dur <= b + EPS:
                starts.append(t)
                t += 0.5
            starts.sort(key=lambda s: (abs(s + dur / 2 - mid), s))
            for s in starts:
                s = tl.snap_up(s)
                if s + dur > min(b, tl.end_s) + EPS or tl.window_hit(s, s + dur):
                    continue
                if not tl.gap_ok(s, s + dur, shots):
                    continue
                placed = (s, dur)
                break
            if placed:
                break
        if not placed:
            tl.log.append(f"talking head {a:.1f}-{b:.1f}s: no legal slot for a cutaway")
            continue
        avoid = frozenset(q.lower() for s in shots if s.is_video for q in s.queries[:1])
        cand = filler(placed[0], placed[1], avoid)
        if cand is None:
            continue
        cand.start_s, cand.duration_s, cand.origin_s = placed[0], placed[1], placed[0]
        cand.trigger = cand.trigger or "talking_head"
        cand = _sanitize(cand, tl)
        if cand is None:
            continue
        shots.append(cand)
        tl.log.append(f"talking head {a:.1f}-{b:.1f}s: added cutaway at {placed[0]:.2f}s")
    return sorted(shots, key=lambda x: x.start_s)


def _final_check(cands: list[Candidate], tl: Timeline) -> list[Candidate]:
    out: list[Candidate] = []
    c = tl.cfg
    for s in sorted(cands, key=lambda x: x.start_s):
        ok = (c.min_clip_s - EPS <= s.duration_s <= c.max_clip_s + EPS
              and s.start_s >= 0 and s.end_s <= tl.end_s + EPS
              and not tl.window_hit(s.start_s, s.end_s)
              and tl.gap_ok(s.start_s, s.end_s, out, s.sequence))
        if ok:
            out.append(s)
        else:
            tl.log.append(f"final check dropped @{s.start_s:.2f} '{s.reason[:40]}'")
    return out


def enforce_video_rules(cands: list[Candidate], segments: list[Segment], cfg: RuleConfig,
                        filler: FillerFn | None = None, tl: Timeline | None = None) -> list[Candidate]:
    tl = tl or Timeline.from_segments(segments, cfg)
    work = [x for x in (_sanitize(replace(c), tl) for c in cands if c.type != "music" and c.type != "ai_audio") if x]
    work = [x for x in (_place(c, tl) for c in work) if x]
    work = _resolve_back_to_back(work, tl)
    work = _fill_talking_heads(work, tl, filler)
    work = _final_check(work, tl)
    _cap_ai(work, tl)
    return work


def build_music(specs: dict[str, MusicSpec], segments: list[Segment], cfg: RuleConfig,
                default: Callable[[str], MusicSpec]) -> list[Candidate]:
    tl = Timeline.from_segments(segments, cfg)
    beds = []
    for section, a, b in music_sections(segments):
        spec = specs.get(section) or default(section)
        queries = clean_queries(spec.queries) or default(section).queries
        start = 0.0 if a <= 0 else tl.snap_up(a)
        dur = math.floor((min(b, tl.end_s) - start) * 1000 + 1e-6) / 1000
        if dur <= 0:
            continue
        beds.append(Candidate(start, dur, "music",
                              spec.reason or f"{section.capitalize()} music bed: {spec.mood}, {spec.tempo}",
                              queries, section=section, mood=spec.mood, tempo=spec.tempo))
    return beds


def to_shots(music: list[Candidate], video: list[Candidate], cfg: RuleConfig) -> list[dict]:
    allc = sorted(music + video, key=lambda c: (c.start_s, 0 if c.type == "music" else 1))
    shots = []
    for i, c in enumerate(allc, 1):
        shot = {
            "id": f"S{i:03d}",
            "start_tc": seconds_to_tc(c.start_s, cfg.fps, cfg.drop_frame),
            "duration_s": round(c.duration_s, 3),
            "type": c.type,
            "reason": c.reason,
            "queries": list(c.queries),
            "status": "pending",
            "file": None,
            "confidence": 0.0,
            "updated_by": "A",
        }
        if c.type == "music":
            shot.update(section=c.section, mood=c.mood, tempo=c.tempo)
        if c.ai_prompt:
            shot["ai_prompt"] = c.ai_prompt
        if c.sequence:
            shot["sequence"] = True
        if c.trigger:
            shot["trigger"] = c.trigger
        shots.append(shot)
    return shots
