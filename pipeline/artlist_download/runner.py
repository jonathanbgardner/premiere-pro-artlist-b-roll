"""Run workstream B over a project's shot list (resumable)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pipeline.common import shotlist as sl

from .agent import RunBudget, ShotAgent, ShotResult, shot_prompt
from .computer import Computer
from .config import MUSIC_EXTS, VIDEO_EXTS, Settings
from .guardrails import HumanGate
from .llm import LLMClient
from .picks import append_pick, last_pick_for

B_TYPES = ("stock_video", "music")
RUN_LOG = "logs/artlist_download_runs.jsonl"


def existing_asset(project_root: Path, shot: dict) -> Path | None:
    exts = VIDEO_EXTS if shot["type"] == "stock_video" else MUSIC_EXTS
    for p in sorted((project_root / "assets").glob(f"{shot['id']}.*")):
        if p.suffix.lower() in exts and p.is_file() and p.stat().st_size > 0:
            return p
    return None


def select_shots(data: dict, only: list[str] | None = None, retry_failed: bool = False) -> list[dict]:
    want = {"pending", "failed"} if retry_failed else {"pending"}
    out = []
    for s in data["shots"]:
        if s["type"] not in B_TYPES or s["status"] not in want:
            continue
        if only and s["id"] not in only:
            continue
        out.append(s)
    return out


def _clear_error(path: Path, shot_id: str) -> None:
    with sl.locked(path) as data:
        sl.get_shot(data, shot_id).pop("error", None)


class Runner:
    def __init__(self, project_root: str | Path, settings: Settings, *,
                 computer_factory: Callable[[], Computer] | None = None,
                 llm: LLMClient | None = None, gate: HumanGate | None = None,
                 only: list[str] | None = None, max_shots: int | None = None,
                 retry_failed: bool = False, dry_run: bool = False,
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic):
        self.root = Path(project_root)
        self.path = sl.shot_list_path(self.root)
        self.s = settings
        self.computer_factory = computer_factory
        self.llm = llm
        self.gate = gate or HumanGate(settings.interactive)
        self.only = only
        self.max_shots = max_shots
        self.retry_failed = retry_failed
        self.dry_run = dry_run
        self.log = log
        self.clock = clock
        self.budget = RunBudget(settings)
        self._computer: Computer | None = None

    def run(self) -> dict:
        sl.init_project(self.root)
        data = sl.load(self.path)
        todo = select_shots(data, self.only, self.retry_failed)
        summary: dict = {"processed": [], "recovered": [], "skipped_budget": [], "stopped": None}
        if self.dry_run:
            return self._dry_run(todo, summary)
        n_agent = 0
        try:
            for shot0 in todo:
                shot = sl.get_shot(sl.load(self.path), shot0["id"])  # fresh: D/orchestrator may write too
                if shot["status"] not in ({"pending", "failed"} if self.retry_failed else {"pending"}):
                    continue
                asset = existing_asset(self.root, shot)
                if asset is not None:
                    self._recover(shot, asset)
                    summary["recovered"].append(shot["id"])
                    continue
                if self.max_shots is not None and n_agent >= self.max_shots:
                    break
                if (why := self.budget.exhausted()):
                    summary["stopped"] = why
                    break
                n_agent += 1
                self.log(f"[B] {shot['id']} ({shot['type']}): {shot['reason']}")
                t0 = self.clock()
                result = self._agent().run_shot(shot)
                self._apply(shot, result)
                self._run_log(shot, result, self.clock() - t0)
                summary["processed"].append({"id": shot["id"], "status": result.status,
                                             "error": result.error or None, "file": result.file})
                if result.stop_run:
                    summary["stopped"] = result.error or "stopped"
                    if result.status == "pending":
                        summary["skipped_budget"].append(shot["id"])
                    break
        finally:
            if self._computer is not None:
                self._computer.close()
        summary["cost_usd"] = round(self.budget.cost_usd, 4)
        summary["model_steps"] = self.budget.steps
        return summary

    # ------------------------------------------------------------------ helpers
    def _agent(self) -> ShotAgent:
        if self._computer is None:
            if self.computer_factory is None or self.llm is None:
                raise RuntimeError("Runner needs computer_factory and llm unless dry_run=True")
            self._computer = self.computer_factory()
        return ShotAgent(computer=self._computer, llm=self.llm, settings=self.s, gate=self.gate,
                         budget=self.budget, project_root=self.root, log=self.log, clock=self.clock)

    def _recover(self, shot: dict, asset: Path) -> None:
        rel = f"assets/{asset.name}"
        fields = {"status": "downloaded", "file": rel, "updated_by": "B"}
        pick = last_pick_for(self.root, shot["id"])
        if pick and pick.get("file") == rel:
            try:
                fields["confidence"] = max(0.0, min(1.0, float(pick.get("confidence") or 0)))
            except ValueError:
                pass
            if pick.get("title"):
                fields["source_title"] = pick["title"]
            if pick.get("url"):
                fields["source_url"] = pick["url"]
        self.log(f"[B] {shot['id']}: {rel} already on disk; marking downloaded without re-downloading")
        sl.update_shot(self.path, shot["id"], **fields)
        if "error" in shot:
            _clear_error(self.path, shot["id"])

    def _apply(self, shot: dict, r: ShotResult) -> None:
        sid = shot["id"]
        if r.status == "downloaded":
            # CSV first: if we crash before the shot-list write, resume recovers from it.
            append_pick(self.root, {"shot_id": sid, "type": shot["type"], "query": r.query, "title": r.title,
                                    "url": r.url, "resolution": r.resolution, "duration_s": r.duration_s,
                                    "confidence": r.confidence, "file": r.file})
            sl.update_shot(self.path, sid, status="downloaded", file=r.file, confidence=r.confidence,
                           source_title=r.title, source_url=r.url, updated_by="B")
            if "error" in shot:
                _clear_error(self.path, sid)
            self.log(f"[B] {sid}: downloaded {r.file} ({r.title!r}, confidence {r.confidence})")
        elif r.status == "needs_ai":
            sl.update_shot(self.path, sid, status="needs_ai", file=None, confidence=0.0, updated_by="B",
                           b_no_match_reason=r.reason)
            if "error" in shot:
                _clear_error(self.path, sid)
            self.log(f"[B] {sid}: no stock match -> needs_ai ({r.reason})")
        elif r.status == "failed":
            sl.update_shot(self.path, sid, status="failed", error=r.error, updated_by="B")
            self.log(f"[B] {sid}: FAILED: {r.error}" + (f" ({r.reason})" if r.reason else ""))
        else:  # budget exhausted mid-shot: leave the shot pending
            self.log(f"[B] {sid}: left pending: {r.error}")

    def _run_log(self, shot: dict, r: ShotResult, secs: float) -> None:
        p = self.root / RUN_LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "shot_id": shot["id"],
               "status": r.status, "error": r.error or None, "reason": r.reason or None, "steps": r.steps,
               "seconds": round(secs, 1), "run_cost_usd": round(self.budget.cost_usd, 4),
               "model": self.s.model}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def _dry_run(self, todo: list[dict], summary: dict) -> dict:
        self.log(f"[B] dry run: {len(todo)} shot(s) eligible; model {self.s.model}; "
                 f"allowed domains {self.s.allowed_domains}")
        n = 0
        for shot in todo:
            asset = existing_asset(self.root, shot)
            if asset is not None:
                self.log(f"  {shot['id']}: would mark downloaded from existing {asset.name}")
                summary["recovered"].append(shot["id"])
                continue
            if self.max_shots is not None and n >= self.max_shots:
                break
            n += 1
            self.log(f"  {shot['id']}: would search Artlist\n    " + shot_prompt(shot, self.s).replace("\n", "\n    "))
            summary["processed"].append({"id": shot["id"], "status": "would_run"})
        summary["cost_usd"] = 0.0
        summary["model_steps"] = 0
        return summary
