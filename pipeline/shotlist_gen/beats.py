"""Deterministic structure: beats, speaker turns, talking-head stretches, sections."""
from __future__ import annotations

from dataclasses import dataclass, field

from .transcript import Segment


@dataclass
class Beat:
    id: int
    start_s: float
    end_s: float
    speaker: str | None
    segment_indices: list[int] = field(default_factory=list)
    text: str = ""

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class Turn:
    start_s: float
    end_s: float
    speaker: str | None

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def transcript_end(segments: list[Segment]) -> float:
    return max(s.end_s for s in segments)


def split_beats(segments: list[Segment], max_beat_s: float = 30.0, pause_s: float = 1.5) -> list[Beat]:
    """Group segments into beats: break on speaker change, long pause, or length."""
    beats: list[Beat] = []
    cur: Beat | None = None
    for i, seg in enumerate(segments):
        new = cur is None
        if cur is not None:
            prev = segments[cur.segment_indices[-1]]
            sentence_end = prev.text.rstrip().endswith((".", "!", "?", '"', "…"))
            if seg.speaker != cur.speaker:
                new = True
            elif seg.start_s - prev.end_s > pause_s:
                new = True
            elif cur.duration_s >= max_beat_s and sentence_end:
                new = True
            elif cur.duration_s >= max_beat_s * 1.5:
                new = True
        if new:
            cur = Beat(len(beats) + 1, seg.start_s, seg.end_s, seg.speaker, [i], seg.text)
            beats.append(cur)
        else:
            cur.end_s = max(cur.end_s, seg.end_s)
            cur.segment_indices.append(i)
            cur.text = f"{cur.text} {seg.text}"
    return beats


def speaker_turns(segments: list[Segment]) -> list[Turn]:
    turns: list[Turn] = []
    for seg in segments:
        if turns and seg.speaker == turns[-1].speaker:
            turns[-1].end_s = max(turns[-1].end_s, seg.end_s)
        else:
            turns.append(Turn(seg.start_s, seg.end_s, seg.speaker))
    return turns


def speaker_change_times(segments: list[Segment], include_first: bool = True) -> list[float]:
    """Times where a (new) speaker starts talking. The first speaker counts too
    by default: the audience has to see who is talking at the top as well."""
    turns = speaker_turns(segments)
    times = [t.start_s for t in turns]
    return times if include_first else times[1:]


def talking_head_stretches(segments: list[Segment], min_s: float = 20.0) -> list[Turn]:
    """Single-speaker turns longer than ``min_s`` (candidates for cutaways)."""
    return [t for t in speaker_turns(segments) if t.duration_s > min_s]


def music_sections(segments: list[Segment]) -> list[tuple[str, float, float]]:
    """(section, start_s, end_s) for intro/main/outro music beds.

    Intro: from 0 to the first segment boundary after min(30 s, 15% of runtime).
    Outro: from the last segment boundary before T - min(30 s, 15%) to T.
    Videos under 60 s get a single main bed.
    """
    T = transcript_end(segments)
    if T < 60:
        return [("main", 0.0, T)]
    span = min(30.0, 0.15 * T)
    intro_end = next((s.end_s for s in segments if s.end_s >= span), span)
    outro_start = next((s.start_s for s in reversed(segments) if s.start_s <= T - span), T - span)
    if outro_start - intro_end < 20:
        return [("main", 0.0, T)]
    return [("intro", 0.0, intro_end), ("main", intro_end, outro_start), ("outro", outro_start, T)]
