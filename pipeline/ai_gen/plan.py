"""Phase 1: build the generation list for approval. Never generates anything."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import shotlist

from . import budget as B
from .config import estimate_credits, gen_duration, kind_for, tool_cfg
from .prompts import DEFAULT_MODEL, prompt_for


def select(data: dict) -> list[dict]:
    gen = set(B.budget(data)["generated"])
    return [s for s in data["shots"] if B.is_eligible(s) and s["id"] not in gen]


def plan(project_root: str | os.PathLike, cfg: dict, *, use_llm: bool = True,
         model: str = DEFAULT_MODEL, llm_client=None) -> dict:
    root = Path(project_root)
    path = shotlist.shot_list_path(root)
    data = shotlist.load(path)
    shots = select(data)

    # Prompts are written outside the lock (Claude calls can be slow).
    items = []
    for shot in shots:
        prompt, source = prompt_for(shot, use_llm=use_llm, model=model, client=llm_client)
        kind = kind_for(shot)
        t = tool_cfg(cfg, kind)
        draft = dict(shot, ai_prompt=prompt)
        est = estimate_credits(cfg, draft)
        why = ("stock search found nothing (needs_ai)" if shot["status"] == "needs_ai"
               else f"planned as {shot['type']}")
        notes = []
        if not t.get("enabled", True):
            notes.append(f"{kind} generation is disabled in config (Artlist MCP may not support it yet)")
        items.append({
            "id": shot["id"], "type": shot["type"], "prompt": prompt, "prompt_source": source,
            "duration_s": float(shot["duration_s"]), "gen_duration_s": gen_duration(cfg, kind, float(shot["duration_s"])),
            "est_credits": est, "reason": f"{shot.get('reason', '')} [{why}]",
            "tool": t.get("submit_tool"), "model": t.get("model"),
            "approved": shot["id"] in B.budget(data)["approved"], "notes": notes,
        })

    for it in items:
        # Re-read inside update_shot's lock: never clobber another writer's fields.
        shotlist.update_shot(path, it["id"], ai_prompt=it["prompt"], est_credits=it["est_credits"], updated_by="D")

    data = shotlist.load(path)
    b = B.budget(data)
    rem = B.remaining(data)
    for n, it in enumerate(items):
        it["within_budget"] = n < rem
    result = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": data["project"],
        "items": items,
        "totals": {"count": len(items), "est_credits": sum(i["est_credits"] for i in items)},
        "budget": {"max_clips": b["max_clips"], "generated": list(b["generated"]),
                   "approved": list(b["approved"]), "remaining": rem},
        "next_step": "Human: `python -m pipeline.ai_gen approve --project-root DIR S00X ...` "
                     "(or --all-listed), then `python -m pipeline.ai_gen generate --project-root DIR`.",
    }
    out = root / B.GEN_LIST
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n")
    os.replace(tmp, out)
    return result


def format_table(result: dict) -> str:
    b = result["budget"]
    lines = [
        f"AI generation list for {result['project']!r}  (NOTHING has been generated)",
        f"Budget: max_clips {b['max_clips']}, generated {len(b['generated'])}, remaining {b['remaining']}",
        "",
        f"{'ID':<6} {'type':<11} {'dur':>5} {'gen':>5} {'credits':>8} {'ok?':<5} prompt",
        "-" * 100,
    ]
    for it in result["items"]:
        flag = "APPR" if it["approved"] else ("" if it["within_budget"] else "OVER")
        prompt = it["prompt"] if len(it["prompt"]) <= 60 else it["prompt"][:57] + "..."
        lines.append(f"{it['id']:<6} {it['type']:<11} {it['duration_s']:>5.1f} {it['gen_duration_s']:>5.1f} "
                     f"{it['est_credits']:>8.0f} {flag:<5} {prompt}")
        for n in it["notes"]:
            lines.append(f"{'':<6} note: {n}")
    lines += [
        "-" * 100,
        f"Total: {result['totals']['count']} shots, ~{result['totals']['est_credits']:.0f} credits "
        "(estimates from artlist_mcp.toml: VERIFY against your Artlist plan)",
        "",
        "STOPPED: waiting for approval. To approve (human only):",
        "  python -m pipeline.ai_gen approve --project-root DIR S004 S007",
        "  python -m pipeline.ai_gen approve --project-root DIR --all-listed",
    ]
    return "\n".join(lines)
