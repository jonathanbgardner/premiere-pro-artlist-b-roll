"""Generation prompts: shot.ai_prompt if present, else Claude (optional) or a template."""
from __future__ import annotations

import re
import sys

DEFAULT_MODEL = "claude-opus-5-5"

_LEADS = re.compile(
    r"^(abstract|speaker|host|guest|narrator)?\s*:?\s*(mentions|talks about|says|describes|discusses|refers to)?\s*",
    re.IGNORECASE,
)

VIDEO_STYLE = ("cinematic B-roll, realistic, natural light, smooth slow camera motion, "
               "shallow depth of field, 16:9, no text, no logos, no watermarks, no recognizable faces")


def _subject(shot: dict) -> str:
    reason = (shot.get("reason") or "").strip()
    subject = _LEADS.sub("", reason).strip(" .:") or reason
    return subject


def template_prompt(shot: dict) -> str:
    """Deterministic fallback prompt (used with --no-llm or when Claude is unavailable)."""
    subject = _subject(shot)
    queries = [q for q in shot.get("queries") or [] if q]
    detail = f" Visual idea: {queries[0]}." if queries else ""
    if shot["type"] in ("ai_audio", "music"):
        parts = [f"Instrumental music bed for a video {shot.get('section') or 'section'}"]
        if shot.get("mood"):
            parts.append(f"mood: {shot['mood']}")
        if shot.get("tempo"):
            parts.append(f"tempo: {shot['tempo']}")
        parts.append(f"about {round(float(shot['duration_s']))} seconds, no vocals, clean ending")
        if subject:
            parts.append(f"context: {subject}")
        return ", ".join(parts) + "."
    return (f"{subject[:1].upper() + subject[1:]}.{detail} "
            f"{round(float(shot['duration_s']))}-second {VIDEO_STYLE}.").strip()


SYSTEM = (
    "You write prompts for a text-to-video (or text-to-audio) generation model that makes short "
    "B-roll inserts for a long-form documentary-style video. Given one shot description, reply with "
    "ONLY the prompt text: one paragraph, under 60 words, concrete visual subject first, then camera "
    "move, lighting and mood. Realistic and cinematic unless the shot is abstract. No on-screen text, "
    "logos, brand names, real people or recognizable faces. Do not add quotes or a preamble."
)


def llm_prompt(shot: dict, *, model: str = DEFAULT_MODEL, client=None) -> str:
    """Ask Claude for a prompt. Raises on any failure; caller falls back to the template."""
    import anthropic  # imported lazily so --no-llm works without the SDK configured

    client = client or anthropic.Anthropic()
    desc = {
        "type": shot["type"], "duration_s": shot["duration_s"], "reason": shot.get("reason", ""),
        "stock_queries_tried": shot.get("queries", []),
    }
    for k in ("section", "mood", "tempo"):
        if shot.get(k):
            desc[k] = shot[k]
    response = client.beta.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": f"Shot: {desc}"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model refused")
    text = " ".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not text:
        raise RuntimeError(f"empty response (stop_reason={response.stop_reason})")
    return text.strip().strip('"').strip()


def prompt_for(shot: dict, *, use_llm: bool, model: str = DEFAULT_MODEL, client=None) -> tuple[str, str]:
    """Return (prompt, source) where source is 'shot', 'llm' or 'template'."""
    existing = (shot.get("ai_prompt") or "").strip()
    if existing:
        return existing, "shot"
    if use_llm:
        try:
            return llm_prompt(shot, model=model, client=client), "llm"
        except Exception as e:  # noqa: BLE001 - any failure -> deterministic fallback
            print(f"[ai_gen] {shot['id']}: Claude prompt failed ({type(e).__name__}: {e}); using template",
                  file=sys.stderr)
    return template_prompt(shot), "template"
