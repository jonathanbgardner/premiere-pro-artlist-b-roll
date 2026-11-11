import random

import pytest

from pipeline.common import shotlist as sl
from pipeline.shotlist_gen.beats import (music_sections, speaker_change_times, split_beats,
                                         talking_head_stretches)
from pipeline.shotlist_gen.candidates import Candidate, MusicSpec
from pipeline.shotlist_gen.check import violations
from pipeline.shotlist_gen.heuristics import default_music, make_filler
from pipeline.shotlist_gen.rules import (RuleConfig, Timeline, build_music, enforce_video_rules,
                                         to_shots, uncovered)
from pipeline.shotlist_gen.transcript import Segment

CFG = RuleConfig(fps=29.97)


def segs_one_speaker(T=100.0, speaker="A"):
    return [Segment(float(t), float(min(t + 5, T)), speaker, f"line {t}") for t in range(0, int(T), 5)]


def segs_two_speakers():
    # A: 0-40, B: 40-70, A: 70-120
    out = []
    for t in range(0, 120, 5):
        spk = "A" if t < 40 or t >= 70 else "B"
        out.append(Segment(float(t), float(t + 5), spk, f"line {t}"))
    return out


def C(start, dur=5.0, **kw):
    kw.setdefault("queries", ["cattle feeding sunrise", "farm morning herd"])
    kw.setdefault("reason", f"clip at {start}")
    return Candidate(start, dur, **kw)


def run(cands, segs, cfg=CFG, filler=None):
    return enforce_video_rules(cands, segs, cfg, filler=filler)


def shots_data(video, segs, cfg=CFG, music=None):
    data = sl.new_shot_list("t", cfg.fps, max_ai_clips=cfg.max_ai_clips)
    data["shots"] = to_shots(music or [], video, cfg)
    sl.validate(data)
    return data


# ------------------------------------------------------------- structure

def test_beats_and_turns():
    segs = segs_two_speakers()
    beats = split_beats(segs, max_beat_s=30)
    assert all(len({segs[i].speaker for i in b.segment_indices}) == 1 for b in beats)
    assert beats[0].start_s == 0 and beats[-1].end_s == 120
    assert all(b.duration_s <= 45 for b in beats)
    assert speaker_change_times(segs) == [0.0, 40.0, 70.0]
    assert speaker_change_times(segs, include_first=False) == [40.0, 70.0]
    st = talking_head_stretches(segs, 20)
    assert [(t.start_s, t.end_s) for t in st] == [(0, 40), (40, 70), (70, 120)]


def test_beat_breaks_on_pause():
    segs = [Segment(0, 3, "A", "one."), Segment(3.2, 6, "A", "two."), Segment(9, 12, "A", "three.")]
    assert [b.segment_indices for b in split_beats(segs)] == [[0, 1], [2]]


# ------------------------------------------------------------- duration

@pytest.mark.parametrize("dur,expected", [(1.0, 3.0), (2.99, 3.0), (5.5, 5.5), (8.0, 8.0), (20.0, 8.0)])
def test_duration_clamped_3_to_8(dur, expected):
    out = run([C(30, dur)], segs_one_speaker())
    assert len(out) == 1 and out[0].duration_s == pytest.approx(expected, abs=1e-3)


# ------------------------------------------------------------- speaker change

def test_first_10s_of_first_speaker_protected():
    out = run([C(2, 5)], segs_one_speaker())
    assert len(out) == 1 and out[0].start_s >= 10.0 - 1e-6


def test_clip_shifted_past_new_speaker_window():
    out = run([C(41, 5)], segs_two_speakers())      # B starts at 40 -> protected until 50
    assert len(out) == 1 and out[0].start_s == pytest.approx(50.0, abs=0.05)


def test_clip_running_into_new_speaker_is_shrunk():
    out = run([C(34, 8)], segs_two_speakers())      # would cover 40-42
    assert len(out) == 1
    assert out[0].start_s == pytest.approx(34, abs=0.05) and out[0].end_s <= 40 + 1e-6
    assert out[0].duration_s >= 3


def test_clip_dropped_when_shift_too_far():
    # Speaker changes every 8 s: no legal 3 s slot anywhere
    segs = [Segment(float(t), float(t + 8), "AB"[(t // 8) % 2], "x") for t in range(0, 80, 8)]
    assert run([C(20, 4)], segs) == []


def test_speaker_window_can_be_turned_off_for_first_speaker():
    cfg = RuleConfig(fps=29.97, protect_first_speaker=False)
    out = run([C(2, 5)], segs_one_speaker(), cfg)
    assert out[0].start_s == pytest.approx(2, abs=0.05)


# ------------------------------------------------------------- back to back

def test_back_to_back_later_clip_shifted_to_gap():
    out = run([C(20, 5), C(25.5, 5)], segs_one_speaker())
    assert len(out) == 2
    assert out[1].start_s - out[0].end_s >= 2 - 1e-6


def test_back_to_back_dropped_when_no_room():
    # second clip cannot move >12 s and the window after it is the next speaker
    segs = [Segment(0, 30, "A", "x"), Segment(30, 60, "B", "y"), Segment(60, 100, "A", "z")]
    out = run([C(21, 8), C(23, 4)], segs)
    assert len(out) == 1 and out[0].start_s == pytest.approx(21, abs=0.05)


def test_back_to_back_higher_priority_wins():
    segs = [Segment(0, 30, "A", "x"), Segment(30, 60, "B", "y")]
    out = run([C(20, 8, priority=3, reason="weak"), C(22, 6, priority=1, reason="strong")], segs)
    assert [c.reason for c in out] == ["strong"]


def test_sequence_flag_allows_back_to_back_but_not_overlap():
    out = run([C(20, 4), C(24, 4, sequence=True), C(27, 4, sequence=True)], segs_one_speaker())
    assert len(out) == 3
    assert out[1].start_s == pytest.approx(out[0].end_s, abs=0.05)
    assert out[2].start_s >= out[1].end_s - 1e-6


def test_overlapping_clips_resolved():
    out = run([C(30, 8), C(31, 8), C(32, 8)], segs_one_speaker())
    for a, b in zip(out, out[1:]):
        assert b.start_s - a.end_s >= 2 - 1e-6


# ------------------------------------------------------------- transcript end

def test_clip_does_not_run_past_end():
    out = run([C(97, 8)], segs_one_speaker(100))
    assert len(out) == 1 and out[0].end_s <= 100 + 1e-6 and out[0].duration_s == pytest.approx(8, abs=1e-3)


def test_clip_starting_after_end_dropped():
    assert run([C(150, 5)], segs_one_speaker(100)) == []


def test_short_transcript_never_overflows():
    segs = [Segment(0, 14, "A", "short")]
    out = run([C(11, 8)], segs)
    assert all(c.end_s <= 14 + 1e-6 and c.duration_s >= 3 for c in out)


# ------------------------------------------------------------- ai cap, sanitize

def test_ai_cap_converts_extras_to_stock():
    cfg = RuleConfig(fps=29.97, max_ai_clips=2)
    cands = [C(t, 4, type="ai_video", ai_prompt=f"p{t}", priority=1 if t == 60 else 2) for t in (12, 24, 36, 48, 60)]
    out = run(cands, segs_one_speaker(), cfg)
    ai = [c for c in out if c.type == "ai_video"]
    assert len(out) == 5 and len(ai) == 2
    assert 60 in [round(c.start_s) for c in ai]          # priority 1 kept
    assert all(c.ai_prompt for c in out)                  # prompt kept for D fallback


def test_sanitize_queries_and_types():
    out = run([C(20, 5, queries=["  a  b ", "A B", "c", "d", "e"], type="weird"),
               C(40, 5, queries=[]),
               C(60, 5, type="ai_video", ai_prompt="", reason="glowing soil")], segs_one_speaker())
    assert [c.queries for c in out[:1]] == [["a b", "c", "d"]]
    assert out[0].type == "stock_video"
    assert len(out) == 2 and out[1].ai_prompt == "glowing soil"


def test_music_and_ai_audio_candidates_ignored_by_video_rules():
    out = run([C(20, 60, type="music"), C(40, 5, type="ai_audio")], segs_one_speaker())
    assert out == []


# ------------------------------------------------------------- talking heads

def test_talking_head_stretch_gets_a_cutaway():
    segs = segs_one_speaker(100)
    out = run([], segs, filler=make_filler(segs))
    assert out, "long monologue must get at least one candidate"
    tl = Timeline.from_segments(segs, CFG)
    assert all(b - a <= 20 + 1e-6 or b <= 10 + 1e-6 for a, b in uncovered(tl, out, 10, 100))
    assert all(c.trigger == "talking_head" for c in out)


def test_talking_head_filler_respects_rules():
    segs = segs_two_speakers()
    out = run([C(15, 6)], segs, filler=make_filler(segs))
    data = shots_data(out, segs)
    assert violations(data, segs) == []
    for t in talking_head_stretches(segs, 20):
        assert any(c.start_s < t.end_s and c.end_s > t.start_s for c in out), t


def test_no_filler_without_filler_fn():
    assert run([], segs_one_speaker(100)) == []


# ------------------------------------------------------------- music

def test_music_sections_span_and_clamp():
    segs = segs_one_speaker(240)
    secs = music_sections(segs)
    assert [s[0] for s in secs] == ["intro", "main", "outro"]
    assert secs[0][1] == 0 and secs[-1][2] == 240
    assert all(a[2] == b[1] for a, b in zip(secs, secs[1:]))
    beds = build_music({"main": MusicSpec("main", "calm", "slow", ["calm piano"])}, segs, CFG, default_music(segs))
    assert [b.section for b in beds] == ["intro", "main", "outro"]
    assert beds[1].queries == ["calm piano"] and beds[1].mood == "calm"
    assert beds[0].queries  # default spec fills missing sections
    assert beds[-1].end_s <= 240 + 1e-6
    assert sum(b.duration_s for b in beds) == pytest.approx(240, abs=0.2)
    assert any(b.duration_s > 8 for b in beds)          # exempt from 3-8 s


def test_short_video_single_main_bed():
    segs = segs_one_speaker(45)
    assert [s[0] for s in music_sections(segs)] == ["main"]


# ------------------------------------------------------------- ids / output fields

def test_ids_sequential_in_time_order_and_initial_fields():
    segs = segs_one_speaker(240)
    video = run([C(100, 5), C(20, 5), C(60, 5)], segs)
    music = build_music({}, segs, CFG, default_music(segs))
    data = shots_data(video, segs, music=music)
    ids = [s["id"] for s in data["shots"]]
    assert ids == [f"S{i:03d}" for i in range(1, len(ids) + 1)]
    starts = [sl.tc_to_seconds(s["start_tc"], 29.97) for s in data["shots"]]
    assert starts == sorted(starts)
    for s in data["shots"]:
        assert (s["status"], s["file"], s["confidence"], s["updated_by"]) == ("pending", None, 0.0, "A")
    assert violations(data, segs) == []


def test_start_tc_round_trips_frames_drop_frame():
    cfg = RuleConfig(fps=29.97, drop_frame=True)
    segs = segs_one_speaker(900)
    out = run([C(661.3, 5)], segs, cfg)
    data = sl.new_shot_list("t", 29.97, drop_frame=True)
    data["shots"] = to_shots([], out, cfg)
    sl.validate(data)
    tc = data["shots"][0]["start_tc"]
    assert ";" in tc
    assert sl.tc_to_seconds(tc, 29.97) == pytest.approx(out[0].start_s, abs=1e-6)


# ------------------------------------------------------------- fuzz: rules always hold

@pytest.mark.parametrize("seed", range(25))
def test_random_candidates_always_satisfy_rules(seed):
    rnd = random.Random(seed)
    segs, t, spk = [], 0.0, "A"
    while t < 300:
        d = rnd.uniform(1.5, 9)
        if rnd.random() < 0.15:
            spk = "B" if spk == "A" else "A"
        segs.append(Segment(round(t, 2), round(t + d, 2), spk, "the cattle in the field"))
        t += d + rnd.choice([0, 0, 0.3, 2.0])
    cands = [C(rnd.uniform(-5, 320), rnd.uniform(0.5, 15),
               type=rnd.choice(["stock_video", "ai_video", "stock_video"]),
               ai_prompt="p", sequence=rnd.random() < 0.2, priority=rnd.randint(1, 3))
             for _ in range(rnd.randint(0, 60))]
    cfg = RuleConfig(fps=rnd.choice([23.976, 25, 29.97, 59.94]), max_ai_clips=3)
    out = run(cands, segs, cfg, filler=make_filler(segs))
    data = sl.new_shot_list("t", cfg.fps, max_ai_clips=3)
    data["shots"] = to_shots(build_music({}, segs, cfg, default_music(segs)), out, cfg)
    sl.validate(data)
    assert violations(data, segs, cfg) == []


def test_displaced_clip_yields_to_clip_already_in_place():
    # first clip is pushed out of B's protected window (40-50) onto the second
    out = run([C(41, 5, reason="pushed"), C(50.5, 5, reason="in place")], segs_two_speakers())
    assert [c.reason for c in out] == ["in place"]
    assert out[0].start_s == pytest.approx(50.5, abs=0.05)


def test_losing_clip_moves_after_winner_when_it_can():
    out = run([C(20, 5, priority=3, reason="weak"), C(21, 4, priority=1, reason="strong")], segs_one_speaker())
    assert [c.reason for c in out] == ["strong", "weak"]
    assert out[1].start_s - out[0].end_s >= 2 - 1e-6
