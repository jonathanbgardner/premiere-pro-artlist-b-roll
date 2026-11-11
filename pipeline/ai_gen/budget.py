"""Eligibility, approval hashes and the human-only `approve` command."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pipeline.common import shotlist

GEN_LIST = "logs/ai_generation_list.json"
LEDGER = "logs/ai_generation_ledger.jsonl"
AI_TYPES = ("ai_video", "ai_audio")


class BudgetError(RuntimeError):
    pass


def is_eligible(shot: dict) -> bool:
    """needs_ai (any type), or ai_video/ai_audio still pending."""
    return shot["status"] == "needs_ai" or (shot["type"] in AI_TYPES and shot["status"] == "pending")


def budget(data: dict) -> dict:
    b = data["ai_budget"]
    b.setdefault("approved", [])
    b.setdefault("generated", [])
    return b


def remaining(data: dict) -> int:
    b = budget(data)
    return max(0, int(b["max_clips"]) - len(b["generated"]))


def approval_hash(shot: dict) -> str:
    """What the human approved: type, prompt, duration. Changing any of them voids approval."""
    payload = json.dumps({"type": shot["type"], "prompt": (shot.get("ai_prompt") or "").strip(),
                          "duration_s": float(shot["duration_s"])}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def approve(project_root: str | os.PathLike, ids: list[str] | None = None, *,
            all_listed: bool = False) -> list[str]:
    """HUMAN COMMAND. Append shot IDs to ai_budget.approved (atomic: all or nothing).

    Refuses if the approved-but-not-generated count would exceed
    max_clips - len(generated). The orchestrator must never call this.
    Returns the IDs newly approved.
    """
    root = Path(project_root)
    path = shotlist.shot_list_path(root)
    if all_listed:
        gen_list = root / GEN_LIST
        if not gen_list.is_file():
            raise BudgetError(f"{GEN_LIST} not found: run `plan` first")
        listed = [it["id"] for it in json.loads(gen_list.read_text())["items"]]
        ids = list(dict.fromkeys((ids or []) + listed))
    if not ids:
        raise BudgetError("nothing to approve: give shot IDs or --all-listed")
    ids = list(dict.fromkeys(ids))

    with shotlist.locked(path) as data:
        b = budget(data)
        problems = []
        new: list[str] = []
        for sid in ids:
            try:
                shot = shotlist.get_shot(data, sid)
            except KeyError:
                problems.append(f"{sid}: no such shot")
                continue
            if sid in b["generated"]:
                problems.append(f"{sid}: already generated (or attempted)")
                continue
            if not is_eligible(shot):
                problems.append(f"{sid}: status {shot['status']!r}/type {shot['type']!r} is not eligible for AI")
                continue
            if not (shot.get("ai_prompt") or "").strip():
                problems.append(f"{sid}: no ai_prompt; run `plan` first so you approve a concrete prompt")
                continue
            if sid not in b["approved"]:
                new.append(sid)
        if problems:
            raise BudgetError("refused, nothing approved:\n  " + "\n  ".join(problems))
        pending = [s for s in b["approved"] if s not in b["generated"]]
        room = int(b["max_clips"]) - len(b["generated"])
        if len(pending) + len(new) > room:
            raise BudgetError(
                f"refused, nothing approved: {len(pending)} already approved + {len(new)} new = "
                f"{len(pending) + len(new)} > remaining budget {room} "
                f"(max_clips {b['max_clips']} - generated {len(b['generated'])})")
        for sid in ids:
            shot = shotlist.get_shot(data, sid)
            shot["ai_approval_hash"] = approval_hash(shot)
            shot["updated_by"] = "human"
        b["approved"].extend(new)
    return new
