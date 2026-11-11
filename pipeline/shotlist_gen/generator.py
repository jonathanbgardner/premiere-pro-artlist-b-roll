"""Workstream A entry point: transcript -> shot_list.json."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.common import shotlist as sl

from . import heuristics, llm
from .beats import split_beats, talking_head_stretches
from .rules import RuleConfig, Timeline, build_music, enforce_video_rules, to_shots
from .transcript import Segment, parse_transcript


class RefuseOverwrite(RuntimeError):
    pass


@dataclass
class Result:
    data: dict
    log: list[str] = field(default_factory=list)
    path: Path | None = None


def build_shot_list(segments: list[Segment], *, project: str, fps: float, max_ai_clips: int = 8,
                    source_sequence: str | None = None, drop_frame: bool = False, offline: bool = False,
                    model: str | None = None, client: Any = None, transcript_rel: str | None = None,
                    cfg: RuleConfig | None = None) -> Result:
    cfg = cfg or RuleConfig(fps=fps, drop_frame=drop_frame, max_ai_clips=max_ai_clips)
    beats = split_beats(segments)
    stretches = talking_head_stretches(segments, cfg.talking_head_s)
    default_music = heuristics.default_music(segments)
    if offline:
        cands = heuristics.offline_candidates(segments, beats)
        music_specs: dict = {}
        gen_meta = {"workstream": "A", "mode": "offline"}
    else:
        model = llm.resolve_model(model)
        cands, music_specs = llm.generate(segments, beats, stretches, project=project,
                                          max_ai_clips=max_ai_clips, model=model, client=client)
        gen_meta = {"workstream": "A", "mode": "llm", "model": model}

    tl = Timeline.from_segments(segments, cfg)
    video = enforce_video_rules(cands, segments, cfg, filler=heuristics.make_filler(segments), tl=tl)
    music = build_music(music_specs, segments, cfg, default_music)

    data = sl.new_shot_list(project, fps, max_ai_clips=max_ai_clips, source_sequence=source_sequence,
                            drop_frame=drop_frame)
    data["shots"] = to_shots(music, video, cfg)
    data["generator"] = gen_meta
    if transcript_rel:
        data["transcript"] = transcript_rel
    sl.validate(data)
    return Result(data, tl.log)


def check_overwrite(path: Path, force: bool) -> None:
    if not path.exists() or force:
        return
    try:
        existing = sl.load(path)
    except Exception as e:  # unreadable/invalid: still don't clobber silently
        raise RefuseOverwrite(f"{path} exists and could not be read ({e}); use --force to overwrite") from e
    busy = [s["id"] for s in existing["shots"] if s.get("status") != "pending"]
    if busy:
        raise RefuseOverwrite(f"{path} has {len(busy)} non-pending shot(s) ({', '.join(busy[:5])}"
                              f"{'...' if len(busy) > 5 else ''}); downstream work would be lost. "
                              "Use --force to overwrite.")
    budget = existing.get("ai_budget", {})
    if budget.get("approved") or budget.get("generated"):
        raise RefuseOverwrite(f"{path} has AI approvals/generations recorded in ai_budget; "
                              "use --force to overwrite.")


def run(transcript: str | Path, project_root: str | Path, *, fps: float, project: str | None = None,
        source_sequence: str | None = None, max_ai_clips: int = 8, model: str | None = None,
        offline: bool = False, force: bool = False, drop_frame: bool = False, client: Any = None) -> Result:
    transcript = Path(transcript)
    root = sl.init_project(project_root)
    out = sl.shot_list_path(root)
    check_overwrite(out, force)

    # keep a copy of the transcript in the project layout
    tdir = root / sl.LAYOUT["transcript"]
    dest = tdir / transcript.name
    if transcript.resolve() != dest.resolve():
        shutil.copyfile(transcript, dest)
    segments = parse_transcript(dest, fps)

    res = build_shot_list(segments, project=project or transcript.stem, fps=fps, max_ai_clips=max_ai_clips,
                          source_sequence=source_sequence, drop_frame=drop_frame, offline=offline,
                          model=model, client=client,
                          transcript_rel=f"{sl.LAYOUT['transcript']}/{transcript.name}")
    sl.save(out, res.data)
    logs = root / sl.LAYOUT["logs"]
    (logs / "shotlist_gen.log").write_text("\n".join(res.log) + ("\n" if res.log else ""), encoding="utf-8")
    res.path = out
    return res
