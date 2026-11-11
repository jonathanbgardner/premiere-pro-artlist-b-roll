from pathlib import Path

import pytest

from pipeline.common.shotlist import tc_to_seconds
from pipeline.shotlist_gen.transcript import (TranscriptError, parse_premiere_text, parse_srt,
                                              parse_transcript, parse_transcript_text)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "shotlist_gen"


def test_srt_basic_and_speaker_inheritance():
    srt = """﻿1
00:00:01,000 --> 00:00:04,500
Host: Welcome to the show.

2
00:00:04,600 --> 00:00:07,000
Today we talk farming.

3
00:00:07,200 --> 00:00:10,000
Dana: Thanks for having me.
Glad to be here.

4
00:00:10,100 --> 00:00:12,000
Host: Let's start.

5
00:00:12,100 --> 00:00:14,000
Dana: Sure.
"""
    segs = parse_srt(srt)
    assert [s.speaker for s in segs] == ["Host", "Host", "Dana", "Host", "Dana"]
    assert segs[0].start_s == 1.0 and segs[0].end_s == 4.5
    assert segs[2].text == "Thanks for having me. Glad to be here."
    assert segs[1].text == "Today we talk farming."


def test_srt_crlf_tags_nonspeech_and_rejected_label():
    srt = ("1\r\n00:00:00,000 --> 00:00:02,000\r\n[Music]\r\n\r\n"
           "2\r\n00:00:02,000 --> 00:00:05,000\r\n<i>Note: this is not a speaker</i>\r\n\r\n"
           "3\r\n00:00:05,000 --> 00:00:08,000\r\n{\\an8}Plain caption text\r\n")
    segs = parse_srt(srt)
    assert len(segs) == 2                       # [Music] dropped
    assert segs[0].speaker is None
    assert segs[0].text == "Note: this is not a speaker"
    assert segs[1].text == "Plain caption text"


def test_srt_label_must_recur_unless_speaker_n():
    srt = """1
00:00:00,000 --> 00:00:02,000
Yesterday: we went out.

2
00:00:02,000 --> 00:00:04,000
Speaker 2: Hi.
"""
    segs = parse_srt(srt)
    assert segs[0].speaker is None and segs[0].text.startswith("Yesterday: ")
    assert segs[1].speaker == "Speaker 2"


def test_srt_chevron_speaker_changes_and_bracket_labels():
    srt = """1
00:00:00,000 --> 00:00:03,000
>> So how did it start?

2
00:00:03,000 --> 00:00:06,000
>> It started with one cow.

3
00:00:06,000 --> 00:00:09,000
[Ann] And then two.

4
00:00:09,000 --> 00:00:12,000
[Ann] Then forty.
"""
    segs = parse_srt(srt)
    assert segs[0].speaker != segs[1].speaker
    assert segs[2].speaker == segs[3].speaker == "Ann"


def test_vtt_voice_tags():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
<v Roger>Hello there.

00:00:03.500 --> 00:00:05.000
<v Ann>Hi Roger.
"""
    segs = parse_transcript_text(vtt, 25)
    assert [(s.speaker, s.text) for s in segs] == [("Roger", "Hello there."), ("Ann", "Hi Roger.")]
    assert segs[0].start_s == 1.0


def test_srt_without_hours_and_dot_ms():
    segs = parse_srt("1\n01:02.5 --> 01:04.25\nhi\n")
    assert segs[0].start_s == 62.5 and segs[0].end_s == 64.25


def test_premiere_text_standard_block_format():
    txt = """00:00:00:00 - 00:00:05:12
Speaker 1
We feed the cattle at dawn.

00:00:05:12 - 00:00:09:00
Speaker 2
Every day?

00:01:12:10 - 00:01:15:02
Speaker 1
Every single day.
More than once.
"""
    segs = parse_premiere_text(txt, 29.97)
    assert [s.speaker for s in segs] == ["Speaker 1", "Speaker 2", "Speaker 1"]
    assert segs[2].start_s == pytest.approx(tc_to_seconds("00:01:12:10", 29.97), abs=1e-3)
    assert segs[2].end_s == pytest.approx(tc_to_seconds("00:01:15:02", 29.97), abs=1e-3)
    assert segs[2].text == "Every single day. More than once."
    assert segs[0].end_s == pytest.approx(tc_to_seconds("00:00:05:12", 29.97), abs=1e-3)


def test_premiere_text_speaker_on_time_line_and_dashes():
    txt = """Speaker 1  00:00:00:00 – 00:00:04:00
First line.

00:00:04:00 --> 00:00:08:00 Dana Whitfield
Second line.

Dana Whitfield 00:00:08:00 - 00:00:10:00
Third line.
"""
    segs = parse_premiere_text(txt, 25)
    assert [s.speaker for s in segs] == ["Speaker 1", "Dana Whitfield", "Dana Whitfield"]
    assert segs[1].start_s == 4.0 and segs[2].end_s == 10.0


def test_premiere_text_inline_bracket_and_start_only():
    txt = """[00:00:01:00] Host: Welcome back.
[00:00:04:00] Guest: Thanks.
[00:00:06:00] Host: Let's begin with the farm.
[00:00:09:15] Guest: Sure, it started in 1911.
"""
    segs = parse_premiere_text(txt, 30)
    assert [s.speaker for s in segs] == ["Host", "Guest", "Host", "Guest"]
    assert segs[0].end_s == 4.0                    # runs to next start
    assert segs[3].start_s == pytest.approx(9.5)
    assert segs[3].end_s > segs[3].start_s         # estimated from word count
    assert segs[3].text == "Sure, it started in 1911."


def test_premiere_text_drop_frame_and_ms_times():
    df = parse_premiere_text("00:10:00;00 - 00:10:02;00\nSpeaker 1\nHi.\n", 29.97)
    assert df[0].start_s == pytest.approx(600.0, abs=0.01)
    ms = parse_premiere_text("00:00:01.500 - 00:00:03.000\nSpeaker 1\nHi.\n", 29.97)
    assert ms[0].start_s == 1.5 and ms[0].end_s == 3.0


def test_premiere_text_short_text_line_not_taken_as_speaker():
    txt = "00:00:00:00 - 00:00:02:00\nOkay\n\n00:00:02:00 - 00:00:04:00\nSpeaker 1\nGo on.\n"
    segs = parse_premiere_text(txt, 25)
    assert segs[0].text == "Okay" and segs[0].speaker is None
    assert segs[1].speaker == "Speaker 1"


def test_header_lines_ignored_and_empty_raises():
    segs = parse_premiere_text("Transcript of Episode 4\n\n00:00:00:00 - 00:00:02:00\nSpeaker 1\nHi.\n", 25)
    assert len(segs) == 1
    with pytest.raises(TranscriptError):
        parse_premiere_text("no times here", 25)
    with pytest.raises(TranscriptError):
        parse_srt("garbage")


def test_fixture_transcript_parses():
    segs = parse_transcript(FIX / "interview_4min.srt", 29.97)
    assert len(segs) == 42
    assert {s.speaker for s in segs} == {"Host", "Dana"}
    assert 180 < segs[-1].end_s < 300
    assert all(s.end_s > s.start_s for s in segs)
    assert all(a.end_s <= b.start_s for a, b in zip(segs, segs[1:]))
