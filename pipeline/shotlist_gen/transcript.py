"""Transcript parsers for Premiere Pro exports.

Supported inputs:
- SRT (and WebVTT) caption exports: ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` cues.
- Premiere's Text panel "Export to text" (.txt), e.g.::

      00:01:12:10 - 00:01:15:02
      Speaker 1
      We feed the cattle at dawn.

  plus a few variants: speaker on the time line (``Speaker 1  00:00:00:00 - ...``
  or ``00:00:00:00 - 00:00:05:00 Speaker 1``), ``[00:00:01:12] Name: text``,
  start-only timecodes, ``-->``/``-``/en dash separators, drop-frame ``;``
  timecodes and ``HH:MM:SS.mmm`` / ``MM:SS`` times.

Everything is normalized to ``Segment(start_s, end_s, speaker, text)`` in real
seconds (frame timecodes are converted with ``pipeline.common.shotlist``).

Speaker labels are accepted only when they look like a label *and* either match
``Speaker N`` or occur at least twice in the file, so a line like ``Note: ...``
is not mistaken for a speaker.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline.common.shotlist import tc_to_seconds


@dataclass
class Segment:
    start_s: float
    end_s: float
    speaker: str | None
    text: str

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def to_dict(self) -> dict:
        return asdict(self)


class TranscriptError(ValueError):
    pass


# ------------------------------------------------------------------ speakers

_NAME_RE = re.compile(r"^[A-Z0-9][\w.'\-]*(?: [A-Z0-9#][\w.'\-]*){0,3}$")
_SPEAKER_N_RE = re.compile(r"^(?:speaker|spk|s)\s*#?\s*\d+$", re.I)
_NON_SPEECH = {
    "music", "laughter", "laughs", "laughing", "applause", "inaudible", "crosstalk",
    "silence", "noise", "background noise", "pause", "sighs", "coughs", "unintelligible",
    "music playing", "upbeat music", "indistinct",
}
_ROLES = {"host", "guest", "interviewer", "interviewee", "narrator", "presenter", "moderator",
          "reporter", "anchor", "voiceover", "vo", "announcer"}
_NOT_NAMES = {"note", "step", "warning", "rule", "tip", "question", "answer", "update", "edit",
              "ps", "fyi", "chapter", "part", "q", "a", "re"}


def _looks_like_name(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 32 or s.lower() in _NOT_NAMES:
        return False
    if _SPEAKER_N_RE.match(s):
        return True
    return bool(_NAME_RE.match(s)) and not s[-1] in ".!?,"


@dataclass
class _Raw:
    start_s: float
    end_s: float | None
    label: str | None          # tentative speaker label
    label_form: str            # "colon" | "line" | "bracket" | "voice" | ""
    change_marker: bool        # ">>" style speaker change without a name
    text: str


_TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _split_label(line: str) -> tuple[str | None, str, str, bool]:
    """Return (label, rest, form, change_marker) for the first text line of a cue."""
    s = line.strip()
    change = False
    m = re.match(r"^<v(?:\.[\w.]+)?\s+([^>]+)>(.*)$", s)  # WebVTT voice tag
    if m:
        return m.group(1).strip(), _TAG_RE.sub("", m.group(2)).strip(), "voice", False
    s = _TAG_RE.sub("", s).strip()
    if s.startswith(">>"):
        change = True
        s = s[2:].strip()
    elif s.startswith("- ") or s.startswith("– "):
        change = True
        s = s[2:].strip()
    m = re.match(r"^\[([^\]]{1,40})\]\s*:?\s*(.*)$", s)
    if m:
        inner = m.group(1).strip()
        if inner.lower() in _NON_SPEECH:
            return None, m.group(2).strip(), "", change
        if _looks_like_name(inner):
            return inner, m.group(2).strip(), "bracket", change
    m = re.match(r"^([^:]{1,32}):\s+(.+)$", s)
    if m and _looks_like_name(m.group(1)):
        return m.group(1).strip(), m.group(2).strip(), "colon", change
    return None, s, "", change


def _finalize(raws: list[_Raw]) -> list[Segment]:
    counts = Counter(r.label for r in raws if r.label)
    voice = {r.label for r in raws if r.label and r.label_form == "voice"}
    valid = {lab for lab, n in counts.items()
             if n >= 2 or _SPEAKER_N_RE.match(lab) or lab in voice or lab.lower() in _ROLES}
    segs: list[Segment] = []
    speaker: str | None = None
    unknown = 0
    for i, r in enumerate(raws):
        text = r.text
        if r.label and r.label in valid:
            speaker = r.label
        else:
            if r.label:  # rejected label: put it back into the text
                text = f"{r.label}: {text}" if r.label_form == "colon" else f"{r.label} {text}"
            if r.change_marker:
                unknown += 1
                speaker = f"Speaker ?{unknown}"
        text = re.sub(r"\s+", " ", text).strip()
        # drop pure non-speech cues like "[Music]"
        if not text or re.fullmatch(r"[\[\(♪][^\]\)]*[\]\)♪]?", text):
            continue
        end = r.end_s
        est = r.start_s + max(1.0, len(text.split()) / 2.5)
        if end is None:  # start-only timecodes: run to the next cue
            nxt = next((x.start_s for x in raws[i + 1:] if x.start_s > r.start_s), None)
            end = nxt if nxt is not None else est
        elif end <= r.start_s:
            end = est
        segs.append(Segment(round(r.start_s, 3), round(end, 3), speaker, text))
    segs.sort(key=lambda s: (s.start_s, s.end_s))
    # remove overlaps from sloppy exports: end never runs into the next start
    for a, b in zip(segs, segs[1:]):
        if a.end_s > b.start_s and b.start_s > a.start_s:
            a.end_s = b.start_s
    return segs


# ---------------------------------------------------------------------- SRT

_SRT_TIME = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_SRT_LINE = re.compile(rf"^\s*{_SRT_TIME}\s*-->\s*{_SRT_TIME}")


def _srt_secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Segment]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    raws: list[_Raw] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if _SRT_LINE.match(ln)), None)
        if idx is None:
            continue
        m = _SRT_LINE.match(lines[idx])
        g = m.groups()
        start, end = _srt_secs(*g[:4]), _srt_secs(*g[4:])
        body = lines[idx + 1:]
        if not body:
            continue
        label, first, form, change = _split_label(body[0])
        rest = " ".join(_TAG_RE.sub("", ln).strip() for ln in body[1:])
        raws.append(_Raw(start, end, label, form, change, f"{first} {rest}".strip()))
    if not raws:
        raise TranscriptError("no SRT cues found")
    return _finalize(raws)


# ------------------------------------------------------- Premiere text export

_TC = (r"\d{1,2}:\d{2}:\d{2}[:;]\d{2}"      # HH:MM:SS:FF / HH:MM:SS;FF
       r"|\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?"  # HH:MM:SS(.mmm)
       r"|\d{1,2}:\d{2}(?:[.,]\d{1,3})?")      # MM:SS(.mmm)
_TIME_LINE = re.compile(
    rf"^\s*(?:(?P<sp>[^\d\[\s][^\[]{{0,31}}?)\s+)?\[?(?P<t1>{_TC})\]?"
    rf"(?:\s*(?:-->|-|–|—|to)\s*\[?(?P<t2>{_TC})\]?)?"
    rf"(?:\s*[:|]?\s+(?P<rest>.*))?\s*$"
)


def _tc_secs(tc: str, fps: float) -> float:
    parts = re.split(r"[:;]", tc.replace(",", "."))
    if len(parts) == 4:
        return tc_to_seconds(tc if len(parts[0]) == 2 else "0" + tc, fps)
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def parse_premiere_text(text: str, fps: float) -> list[Segment]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    entries: list[dict] = []
    for line in text.split("\n"):
        m = _TIME_LINE.match(line)
        if m and (m.group("sp") is None or _looks_like_name(m.group("sp"))):
            e = {"t1": m.group("t1"), "t2": m.group("t2"), "label": None, "form": "",
                 "change": False, "lines": []}
            if m.group("sp"):
                e["label"], e["form"] = m.group("sp").strip(), "line"
            rest = (m.group("rest") or "").strip()
            if rest:
                if e["label"] is None and _looks_like_name(rest):
                    e["label"], e["form"] = rest, "line"
                else:
                    lab, body, form, change = _split_label(rest)
                    if lab and e["label"] is None:
                        e["label"], e["form"] = lab, form
                    e["change"] = change
                    if body:
                        e["lines"].append(body)
            entries.append(e)
        elif entries and line.strip():
            e = entries[-1]
            s = line.strip()
            if not e["lines"] and e["label"] is None:
                if _looks_like_name(s):
                    e["label"], e["form"] = s, "line"
                    continue
                lab, body, form, change = _split_label(s)
                e["label"], e["form"], e["change"] = lab, form, change
                s = body
            if s:
                e["lines"].append(s)
    if not entries:
        raise TranscriptError("no timestamped lines found")
    raws: list[_Raw] = []
    for e in entries:
        start = _tc_secs(e["t1"], fps)
        end = _tc_secs(e["t2"], fps) if e["t2"] else None
        body = " ".join(e["lines"])
        if not body and e["label"] and e["form"] == "line" and not _SPEAKER_N_RE.match(e["label"]):
            body, e["label"] = e["label"], None  # a short text line, not a speaker
        raws.append(_Raw(start, end, e["label"], e["form"], e["change"], body))
    return _finalize(raws)


# ----------------------------------------------------------------- dispatch

def parse_transcript_text(text: str, fps: float, fmt: str | None = None) -> list[Segment]:
    if fmt is None:
        fmt = "srt" if "-->" in text else "text"
    segs = parse_srt(text) if fmt in ("srt", "vtt") else parse_premiere_text(text, fps)
    if not segs:
        raise TranscriptError("transcript has no spoken segments")
    return segs


def parse_transcript(path: str | Path, fps: float) -> list[Segment]:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    ext = path.suffix.lower()
    fmt = "srt" if ext in (".srt", ".vtt") else None
    return parse_transcript_text(text, fps, fmt)
