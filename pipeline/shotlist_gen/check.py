"""Independent re-check of A's rules on a finished shot list (used by tests;
handy for the orchestrator too). Works from start_tc, not internal floats."""
from __future__ import annotations

from pipeline.common.shotlist import tc_to_seconds

from .beats import speaker_change_times, transcript_end
from .rules import RuleConfig
from .transcript import Segment

EPS = 1e-3


def violations(data: dict, segments: list[Segment], cfg: RuleConfig | None = None) -> list[str]:
    cfg = cfg or RuleConfig(fps=data["fps"], drop_frame=data.get("drop_frame", False),
                            max_ai_clips=data["ai_budget"]["max_clips"])
    T = transcript_end(segments)
    wins = [(t, t + cfg.speaker_protect_s) for t in speaker_change_times(segments, cfg.protect_first_speaker)]
    out: list[str] = []
    shots = data["shots"]
    starts = [tc_to_seconds(s["start_tc"], data["fps"]) for s in shots]
    for i, s in enumerate(shots, 1):
        if s["id"] != f"S{i:03d}":
            out.append(f"{s['id']}: expected id S{i:03d}")
        if s["status"] != "pending" or s["file"] is not None or s["confidence"] != 0.0 or s.get("updated_by") != "A":
            out.append(f"{s['id']}: initial status/file/confidence/updated_by wrong")
        if not 1 <= len(s["queries"]) <= 3:
            out.append(f"{s['id']}: {len(s['queries'])} queries")
    if starts != sorted(starts):
        out.append("shots not in time order")
    prev_end = None
    ai = 0
    for s, st in zip(shots, starts):
        end = st + s["duration_s"]
        if end > T + EPS:
            out.append(f"{s['id']}: runs past transcript end ({end:.2f} > {T:.2f})")
        if s["type"] == "music":
            if not all(s.get(k) for k in ("section", "mood", "tempo")):
                out.append(f"{s['id']}: music bed missing section/mood/tempo")
            continue
        if s["type"] == "ai_video":
            ai += 1
            if not s.get("ai_prompt"):
                out.append(f"{s['id']}: ai_video without ai_prompt")
        if not cfg.min_clip_s - EPS <= s["duration_s"] <= cfg.max_clip_s + EPS:
            out.append(f"{s['id']}: duration {s['duration_s']} outside {cfg.min_clip_s}-{cfg.max_clip_s}")
        for w0, w1 in wins:
            if st < w1 - EPS and end > w0 + EPS:
                out.append(f"{s['id']}: covers first {cfg.speaker_protect_s:g}s of speaker at {w0:.2f}")
        if prev_end is not None:
            need = 0.0 if s.get("sequence") else cfg.min_gap_s
            if st - prev_end < need - EPS:
                out.append(f"{s['id']}: back to back (gap {st - prev_end:.2f}s)")
        prev_end = end if prev_end is None else max(prev_end, end)
    if ai > data["ai_budget"]["max_clips"]:
        out.append(f"{ai} ai_video shots > max_clips {data['ai_budget']['max_clips']}")
    return out
