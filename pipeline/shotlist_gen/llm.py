"""Creative pass with Claude: B-roll candidates, Artlist queries, music beds.

One structured-output request per transcript. The model's output is only a
proposal: rules.py re-checks every timing rule afterwards.
"""
from __future__ import annotations

import json
import os
from typing import Any

from .beats import Beat, Turn, music_sections
from .candidates import Candidate, MusicSpec
from .transcript import Segment

DEFAULT_MODEL = "claude-opus-5-5"
MODEL_ENV = "BROLL_SHOTLIST_MODEL"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


def resolve_model(cli_model: str | None = None) -> str:
    return cli_model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL


SYSTEM_PROMPT = """\
You are a documentary B-roll editor planning cutaways for a long-form video edited in \
Adobe Premiere Pro. Stock footage and music come from Artlist (artlist.io); a few shots \
can be AI-generated when stock cannot cover them.

You receive the transcript as numbered segments with start/end times in seconds and \
speaker labels, grouped into beats. Propose B-roll candidates:

- Flag moments that benefit from a cutaway: concrete nouns (objects, animals, tools), \
places, physical actions, and stats/numbers. Also give every listed talking-head stretch \
at least one cutaway.
- start_s is the time the cutaway should begin, normally the start of the segment where \
the thing is said (or slightly after). duration_s is 3 to 8 seconds.
- queries: 2 or 3 Artlist search queries, most specific first, broadest last. Write what \
the footage shows, in 2-5 words, as a stock-footage searcher would type it \
(e.g. "cattle feeding sunrise", "farm morning herd", "cows field"). No speaker names, no \
quotes, no abstract words that are not visible on screen.
- type: "stock_video" by default. Use "ai_video" only for abstract or very specific shots \
stock footage cannot cover (microscopic, conceptual, historical reconstructions, a very \
specific product or place). For ai_video write ai_prompt: one cinematic sentence \
describing the shot, camera move and lighting, no on-screen text. For stock_video set \
ai_prompt to "".
- At most {max_ai} ai_video candidates in total.
- sequence: true only when a clip is deliberately part of a back-to-back montage with the \
clip before it (e.g. a list of items said in quick succession). Otherwise false.
- priority: 1 = strongly needed, 2 = good, 3 = optional.
- trigger: what made you flag it.
- reason: one short sentence an editor can read on a timeline marker, e.g. \
"Speaker mentions feeding cattle at dawn".

Do not worry about exact timing rules (speaker introductions, spacing between clips, \
transcript end): a deterministic pass enforces them afterwards. Prefer fewer, well-chosen \
cutaways to many weak ones; roughly one every 10-20 seconds of talk is typical.

Also describe one music bed per listed section (intro, main, outro): mood (a few \
adjectives), tempo (e.g. "slow, 70-80 bpm"), and 2-3 Artlist music search queries from \
specific to broad that match the content and tone of the video.
"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_s": {"type": "number"},
                    "duration_s": {"type": "number"},
                    "type": {"type": "string", "enum": ["stock_video", "ai_video"]},
                    "trigger": {"type": "string", "enum": ["concrete_noun", "place", "action", "stat",
                                                            "abstract", "talking_head"]},
                    "reason": {"type": "string"},
                    "queries": {"type": "array", "items": {"type": "string"}},
                    "ai_prompt": {"type": "string"},
                    "sequence": {"type": "boolean"},
                    "priority": {"type": "integer", "enum": [1, 2, 3]},
                },
                "required": ["start_s", "duration_s", "type", "trigger", "reason", "queries",
                             "ai_prompt", "sequence", "priority"],
                "additionalProperties": False,
            },
        },
        "music": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["intro", "main", "outro"]},
                    "mood": {"type": "string"},
                    "tempo": {"type": "string"},
                    "queries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["section", "mood", "tempo", "queries"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates", "music"],
    "additionalProperties": False,
}


def build_user_message(segments: list[Segment], beats: list[Beat], stretches: list[Turn],
                       project: str) -> str:
    beat_payload = []
    for b in beats:
        beat_payload.append({
            "beat": b.id,
            "speaker": b.speaker,
            "segments": [{"i": i, "start_s": round(segments[i].start_s, 2), "end_s": round(segments[i].end_s, 2),
                          "text": segments[i].text} for i in b.segment_indices],
        })
    payload = {
        "project": project,
        "duration_s": round(max(s.end_s for s in segments), 2),
        "speakers": sorted({s.speaker for s in segments if s.speaker}),
        "music_sections": [{"section": n, "start_s": round(a, 2), "end_s": round(e, 2)}
                           for n, a, e in music_sections(segments)],
        "talking_head_stretches": [{"start_s": round(t.start_s, 2), "end_s": round(t.end_s, 2),
                                    "speaker": t.speaker} for t in stretches],
        "beats": beat_payload,
    }
    return ("Plan B-roll and music for this transcript. Respond with the JSON object only.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1))


def _response_text(message: Any) -> str:
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        raise LLMError(f"model refused the request ({getattr(details, 'category', None)}); "
                       "re-run with --offline or another --model")
    if stop == "max_tokens":
        raise LLMError("model output hit max_tokens; transcript may be too long for one request")
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    raise LLMError(f"no text block in model response (stop_reason={stop})")


def parse_response(text: str) -> tuple[list[Candidate], dict[str, MusicSpec]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"model returned invalid JSON: {e}") from e
    cands: list[Candidate] = []
    for item in data.get("candidates") or []:
        try:
            cands.append(Candidate(
                start_s=float(item["start_s"]),
                duration_s=float(item.get("duration_s", 5.0)),
                type=item.get("type", "stock_video"),
                reason=str(item.get("reason", "")),
                queries=[str(q) for q in item.get("queries") or []],
                ai_prompt=(item.get("ai_prompt") or None),
                sequence=bool(item.get("sequence", False)),
                priority=int(item.get("priority", 2)),
                trigger=str(item.get("trigger", "")),
            ))
        except (KeyError, TypeError, ValueError):
            continue  # malformed item: skip, rules + fillers cover the gap
    music: dict[str, MusicSpec] = {}
    for m in data.get("music") or []:
        sec = m.get("section")
        if sec in ("intro", "main", "outro") and sec not in music:
            music[sec] = MusicSpec(sec, str(m.get("mood", "")).strip() or "neutral",
                                   str(m.get("tempo", "")).strip() or "medium",
                                   [str(q) for q in m.get("queries") or []])
    return cands, music


def make_client():  # pragma: no cover - needs the SDK + credentials
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("the 'anthropic' package is not installed (pip install anthropic) "
                       "or use --offline") from e
    return anthropic.Anthropic()


def generate(segments: list[Segment], beats: list[Beat], stretches: list[Turn], *, project: str,
             max_ai_clips: int, model: str, client: Any = None, effort: str = "high",
             use_fallbacks: bool = True) -> tuple[list[Candidate], dict[str, MusicSpec]]:
    client = client or make_client()
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=64000,
        system=SYSTEM_PROMPT.format(max_ai=max_ai_clips),
        messages=[{"role": "user", "content": build_user_message(segments, beats, stretches, project)}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    if use_fallbacks:
        # Server-side refusal fallback: the API re-runs a declined request on a
        # fallback model inside the same call.
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    try:
        with client.beta.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
    except LLMError:
        raise
    except Exception as e:  # SDK errors: auth, rate limit, connection...
        raise LLMError(f"Claude request failed: {type(e).__name__}: {e}") from e
    return parse_response(_response_text(message))
