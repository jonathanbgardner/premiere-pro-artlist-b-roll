"""One command for the whole B-roll pipeline.

    python -m pipeline.orchestrator --project-root DIR --transcript FILE --fps 29.97 \
        --profile ~/.broll/artlist-profile --prproj /path/ep.prproj

Order (spec "Orchestrator"):
  1. A  shot list generator        (skipped if shot_list.json already exists)
  2. B  Artlist download  ||  D plan (approval request)    -- in parallel
     then D plan again, so shots B just marked needs_ai make the approval list
  3. D  generate                    (approved shots only; D enforces the cap)
  4. C  Premiere timeline builder   (job file -> panel -> result file)

Safe to re-run: every stage skips work that is already done (A keeps an
existing list, B and D only touch pending/approved shots, C skips placed
shots, and the orchestrator doesn't send a Premiere job when nothing new
is placeable). The orchestrator never approves AI generation.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from pipeline.common import shotlist as sl

log = logging.getLogger("orchestrator")

JOB_PATH = "scripts/broll_job.json"
RESULT_PATH = "logs/premiere_result.json"
PLACEABLE_STATUSES = ("downloaded", "failed")


@dataclass
class StepResult:
    name: str
    returncode: int
    skipped: bool = False
    note: str = ""


# A runner takes argv (without the python executable) and returns an exit code.
Runner = Callable[[Sequence[str]], int]


def subprocess_runner(argv: Sequence[str]) -> int:
    cmd = [sys.executable, *argv]
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1]).returncode


@dataclass
class Options:
    project_root: Path
    transcript: Path | None = None
    fps: float | None = None
    drop_frame: bool = False
    project: str | None = None
    source_sequence: str | None = None
    max_ai_clips: int | None = None
    offline: bool = False
    model: str | None = None
    profile: Path | None = None
    headless: bool = False
    interactive: bool = False
    skip_download: bool = False
    ai_llm_prompts: bool = False
    ai_config: str | None = None
    prproj: str | None = None
    skip_premiere: bool = False
    premiere_timeout_s: float = 1800.0
    poll_s: float = 5.0
    stop_for_approval: bool = False
    extra_b_args: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, opts: Options, runner: Runner = subprocess_runner,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.o = opts
        self.run = runner
        self.sleep = sleep
        self.clock = clock
        self.root = Path(opts.project_root)
        self.sl_path = sl.shot_list_path(self.root)
        self.results: list[StepResult] = []

    # ------------------------------------------------------------ stages
    def stage_a(self) -> StepResult:
        if self.sl_path.exists():
            return StepResult("A shot list", 0, skipped=True, note="shot_list.json exists")
        if not (self.o.transcript and self.o.fps):
            raise SystemExit("no shot_list.json yet: --transcript and --fps are required")
        argv = ["-m", "pipeline.shotlist_gen", str(self.o.transcript),
                "--project-root", str(self.root), "--fps", str(self.o.fps)]
        if self.o.drop_frame:
            argv.append("--drop-frame")
        for flag, val in (("--project", self.o.project), ("--source-sequence", self.o.source_sequence),
                          ("--max-ai-clips", self.o.max_ai_clips), ("--model", self.o.model)):
            if val is not None:
                argv += [flag, str(val)]
        if self.o.offline:
            argv.append("--offline")
        rc = self.run(argv)
        if rc != 0 or not self.sl_path.exists():
            raise SystemExit(f"A (shot list generator) failed with exit code {rc}")
        return StepResult("A shot list", rc)

    def _count(self, pred) -> int:
        return sum(1 for s in sl.load(self.sl_path)["shots"] if pred(s))

    def stage_b(self) -> StepResult:
        pending = self._count(lambda s: s["type"] in ("stock_video", "music") and s["status"] == "pending")
        if self.o.skip_download:
            return StepResult("B download", 0, skipped=True, note="--skip-download")
        if not pending:
            return StepResult("B download", 0, skipped=True, note="no pending stock/music shots")
        if not self.o.profile:
            return StepResult("B download", 2, note="--profile not given; run `python -m pipeline.artlist_download login` first")
        argv = ["-m", "pipeline.artlist_download", "run", "--project-root", str(self.root),
                "--profile", str(self.o.profile)]
        if not self.o.interactive:
            argv.append("--non-interactive")
        if self.o.headless:
            argv.append("--headless")
        if self.o.model:
            argv += ["--model", self.o.model]
        argv += self.o.extra_b_args
        return StepResult("B download", self.run(argv), note=f"{pending} pending")

    def _d_args(self, sub: str) -> list[str]:
        argv = ["-m", "pipeline.ai_gen", sub, "--project-root", str(self.root)]
        if self.o.ai_config:
            argv += ["--config", self.o.ai_config]
        return argv

    def stage_d_plan(self, label: str = "D plan") -> StepResult:
        argv = self._d_args("plan")
        if not self.o.ai_llm_prompts or self.o.offline:
            argv.append("--no-llm")
        return StepResult(label, self.run(argv))

    def stage_d_generate(self) -> StepResult:
        data = sl.load(self.sl_path)
        b = data["ai_budget"]
        todo = [i for i in b["approved"] if i not in b.get("generated", [])]
        if not todo:
            return StepResult("D generate", 0, skipped=True, note="nothing approved and ungenerated")
        return StepResult("D generate", self.run(self._d_args("generate")), note=f"{len(todo)} approved")

    def awaiting_approval(self) -> list[str]:
        data = sl.load(self.sl_path)
        b = data["ai_budget"]
        done = set(b["approved"]) | set(b.get("generated", []))
        return [s["id"] for s in data["shots"]
                if s["id"] not in done and (s["status"] == "needs_ai"
                                            or (s["type"] in ("ai_video", "ai_audio") and s["status"] == "pending"))]

    def placeable(self) -> list[str]:
        return [s["id"] for s in sl.load(self.sl_path)["shots"]
                if s["status"] in PLACEABLE_STATUSES and s.get("file")
                and (self.root / s["file"]).exists()]

    def stage_c(self) -> StepResult:
        todo = self.placeable()
        if not todo:
            return StepResult("C Premiere", 0, skipped=True, note="nothing new to place")
        job_id = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ") + f"-{os.getpid()}"
        data = sl.load(self.sl_path)
        job = {"job_version": 1, "job_id": job_id, "action": "build_broll",
               "project_root": str(self.root.resolve()), "shot_list": sl.SHOT_LIST_NAME,
               "write_shot_list": False, "options": {}}
        if self.o.prproj:
            job["prproj"] = self.o.prproj
        if self.o.source_sequence or data.get("source_sequence"):
            job["source_sequence"] = self.o.source_sequence or data["source_sequence"]
        result_file = self.root / RESULT_PATH
        result_file.unlink(missing_ok=True)
        _atomic_write_json(self.root / JOB_PATH, job)
        if self.o.skip_premiere:
            return StepResult("C Premiere", 0, skipped=True,
                              note=f"job written to {JOB_PATH}; run it from the B-roll Builder panel, then re-run")
        log.info("waiting for Premiere: open the B-roll Builder panel and click 'Run pending job' "
                 "(or tick 'Watch for jobs'); %d shots to place", len(todo))
        deadline = self.clock() + self.o.premiere_timeout_s
        while self.clock() < deadline:
            res = _read_json(result_file)
            if res and res.get("job_id") == job_id:
                return self._apply_result(res)
            self.sleep(self.o.poll_s)
        return StepResult("C Premiere", 3, note=f"timed out after {self.o.premiere_timeout_s:.0f}s; "
                                                 f"job left at {JOB_PATH}, re-run when Premiere is open")

    def _apply_result(self, res: dict) -> StepResult:
        status = res.get("status")
        if status == "error":
            return StepResult("C Premiere", 1, note=f"panel error: {res.get('error')}")
        with sl.locked(self.sl_path) as data:
            by_id = {s["id"]: s for s in data["shots"]}
            for sid in res.get("placed", []):
                if sid in by_id:
                    by_id[sid].update(status="placed", updated_by="C")
                    by_id[sid].pop("error", None)
            for f in res.get("failed", []):
                shot = by_id.get(f.get("shot_id"))
                if shot and shot["status"] != "placed":
                    shot.update(status="failed", error=f"premiere: {f.get('error')}", updated_by="C")
        note = (f"{status}: placed {len(res.get('placed', []))}, failed {len(res.get('failed', []))} "
                f"-> {res.get('project_path_out')} / {res.get('sequence')}")
        return StepResult("C Premiere", 0 if status in ("ok", "noop") else 1, note=note)

    # ------------------------------------------------------------ driver
    def execute(self) -> int:
        sl.init_project(self.root)
        self._record(self.stage_a())

        with cf.ThreadPoolExecutor(max_workers=2) as pool:
            fb = pool.submit(self.stage_b)
            fd = pool.submit(self.stage_d_plan)
            self._record(fb.result())
            self._record(fd.result())
        # Shots B gave up on are now needs_ai; refresh the approval request.
        if self._count(lambda s: s["status"] == "needs_ai"):
            self._record(self.stage_d_plan("D plan (after B)"))

        self._record(self.stage_d_generate())

        waiting = self.awaiting_approval()
        if waiting:
            log.warning("AI shots awaiting Jonathan's approval: %s  (see logs/ai_generation_list.json; "
                        "approve with `python -m pipeline.ai_gen approve --project-root %s <IDs>`, then re-run)",
                        ", ".join(waiting), self.root)
            if self.o.stop_for_approval:
                self._record(StepResult("C Premiere", 0, skipped=True, note="--stop-for-approval"))
                return self._summary(waiting)

        self._record(self.stage_c())
        return self._summary(waiting)

    def _record(self, r: StepResult) -> None:
        self.results.append(r)
        state = "skipped" if r.skipped else ("ok" if r.returncode == 0 else f"exit {r.returncode}")
        log.info("[%s] %s%s", r.name, state, f" ({r.note})" if r.note else "")

    def _summary(self, waiting: list[str]) -> int:
        counts: dict[str, int] = {}
        for s in sl.load(self.sl_path)["shots"]:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        summary = {"steps": [r.__dict__ for r in self.results], "shot_status": counts,
                   "awaiting_approval": waiting}
        _atomic_write_json(self.root / "logs" / "orchestrator_last_run.json", summary)
        print("\n== B-roll pipeline summary ==")
        for r in self.results:
            state = "skipped" if r.skipped else ("ok" if r.returncode == 0 else f"exit {r.returncode}")
            print(f"  {r.name:<18} {state:<8} {r.note}")
        print("  shots: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if waiting:
            print(f"  awaiting AI approval: {', '.join(waiting)}")
        return 0 if all(r.skipped or r.returncode == 0 for r in self.results) else 1


def _atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_args(argv: Sequence[str] | None = None) -> Options:
    p = argparse.ArgumentParser(prog="python -m pipeline.orchestrator", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-root", required=True, type=Path)
    g = p.add_argument_group("A: shot list (first run only)")
    g.add_argument("--transcript", type=Path)
    g.add_argument("--fps", type=float)
    g.add_argument("--drop-frame", action="store_true")
    g.add_argument("--project")
    g.add_argument("--source-sequence")
    g.add_argument("--max-ai-clips", type=int)
    g.add_argument("--offline", action="store_true", help="no Claude calls in A; template prompts in D")
    g.add_argument("--model", help="Claude model for A and B")
    g = p.add_argument_group("B: Artlist download")
    g.add_argument("--profile", type=Path, help="browser profile from `artlist_download login`")
    g.add_argument("--headless", action="store_true")
    g.add_argument("--interactive", action="store_true", help="wait for you on login/captcha instead of stopping")
    g.add_argument("--skip-download", action="store_true")
    g.add_argument("--b-arg", action="append", default=[], dest="extra_b_args",
                   help="extra argument passed to artlist_download run (repeatable)")
    g = p.add_argument_group("D: AI generation")
    g.add_argument("--ai-llm-prompts", action="store_true", help="let Claude write AI prompts in D plan")
    g.add_argument("--ai-config")
    g.add_argument("--stop-for-approval", action="store_true",
                   help="don't build the timeline while AI shots await approval")
    g = p.add_argument_group("C: Premiere")
    g.add_argument("--prproj")
    g.add_argument("--skip-premiere", action="store_true", help="write the job file and exit")
    g.add_argument("--premiere-timeout", type=float, default=1800.0, dest="premiere_timeout_s")
    a = p.parse_args(argv)
    return Options(**vars(a))


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    opts = parse_args(argv)
    root = Path(opts.project_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    fh = logging.FileHandler(root / "logs" / "orchestrator.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)
    return Orchestrator(opts).execute()


if __name__ == "__main__":
    sys.exit(main())
