"""Orchestrator integration tests.

A (offline) and D (plan, --no-llm) run for real as subprocesses. B and the
Premiere panel are simulated: B by a fake runner that downloads dummy files
through the shared helpers, the panel by a fake `sleep` that answers the job.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from pipeline import orchestrator as orch
from pipeline.common import shotlist as sl

REPO = Path(__file__).resolve().parents[2]
SRT = REPO / "tests/fixtures/shotlist_gen/interview_4min.srt"
FIVE = REPO / "tests/fixtures/shot_list_5.json"


class Harness:
    """Runner: real subprocess for A and D, scripted fake for B."""

    def __init__(self, root: Path, b_no_match: tuple[str, ...] = ()):
        self.root = root
        self.calls: list[list[str]] = []
        self.b_no_match = set(b_no_match)
        self.lock = threading.Lock()
        self.premiere_jobs = 0

    def __call__(self, argv):
        argv = list(argv)
        with self.lock:
            self.calls.append(argv)
        mod = argv[1]
        if mod == "pipeline.artlist_download":
            return self.fake_b()
        if mod == "pipeline.ai_gen" and argv[2] == "generate":
            return self.fake_d_generate()
        return orch.subprocess_runner(argv)

    def fake_b(self) -> int:
        p = sl.shot_list_path(self.root)
        for s in sl.load(p)["shots"]:
            if s["type"] not in ("stock_video", "music") or s["status"] != "pending":
                continue
            if s["id"] in self.b_no_match:
                sl.update_shot(p, s["id"], status="needs_ai", updated_by="B")
                continue
            ext = ".mp4" if s["type"] == "stock_video" else ".mp3"
            rel = f"assets/{s['id']}{ext}"
            (self.root / rel).write_bytes(b"x")
            sl.update_shot(p, s["id"], status="downloaded", file=rel, confidence=0.8,
                           source_title="t", source_url="https://artlist.io/x", updated_by="B")
        return 0

    def fake_d_generate(self) -> int:
        p = sl.shot_list_path(self.root)
        with sl.locked(p) as data:
            b = data["ai_budget"]
            for sid in b["approved"]:
                if sid in b["generated"] or len(b["generated"]) >= b["max_clips"]:
                    continue
                b["generated"].append(sid)
                (self.root / f"assets/{sid}.mp4").write_bytes(b"x")
                sl.get_shot(data, sid).update(status="downloaded", file=f"assets/{sid}.mp4",
                                              confidence=0.7, updated_by="D")
        return 0

    def mods(self):
        return [" ".join(c[1:3]) for c in self.calls]


def fake_premiere(root: Path, placed_hook=None):
    """A sleep() that plays the UXP panel: consumes the job, writes a result."""
    def sleep(_s):
        job_p = root / orch.JOB_PATH
        if not job_p.exists():
            return
        job = json.loads(job_p.read_text())
        assert job["write_shot_list"] is False
        data = sl.load(root / "shot_list.json")
        placed = [s["id"] for s in data["shots"]
                  if s["status"] in ("downloaded", "failed") and s.get("file")]
        if placed_hook:
            placed_hook(placed)
        (root / orch.RESULT_PATH).write_text(json.dumps({
            "result_version": 1, "job_id": job["job_id"], "status": "ok", "placed": placed,
            "failed": [], "skipped": [], "project_path_out": "ep_BROLL_v1.prproj",
            "sequence": "ep_main_BROLL_v1"}))
        job_p.rename(root / f"scripts/broll_job.{job['job_id']}.done.json")
    return sleep


def opts(root, **kw):
    base = dict(project_root=root, transcript=SRT, fps=29.97, offline=True,
                profile=root / "profile", premiere_timeout_s=60, poll_s=1)
    base.update(kw)
    return orch.Options(**base)


def test_full_run_then_rerun_is_noop(tmp_path):
    root = tmp_path / "ep"
    h = Harness(root)
    placed_batches = []
    o = orch.Orchestrator(opts(root), runner=h, sleep=fake_premiere(root, placed_batches.append))
    assert o.execute() == 0
    mods = h.mods()
    assert mods[0].startswith("pipeline.shotlist_gen")
    assert "pipeline.artlist_download run" in mods and "pipeline.ai_gen plan" in mods
    assert "pipeline.ai_gen generate" not in mods  # nothing approved
    data = sl.load(root / "shot_list.json")
    video_music = [s for s in data["shots"] if s["type"] in ("stock_video", "music")]
    assert video_music and all(s["status"] == "placed" for s in video_music)
    assert data["ai_budget"]["approved"] == []  # orchestrator never approves
    assert len(placed_batches) == 1

    # Re-run: A skipped, B skipped (nothing pending), no Premiere job sent.
    h2 = Harness(root)
    o2 = orch.Orchestrator(opts(root), runner=h2, sleep=fake_premiere(root, placed_batches.append))
    o2.execute()
    assert not any(m.startswith("pipeline.shotlist_gen") for m in h2.mods())
    assert not any(m.startswith("pipeline.artlist_download") for m in h2.mods())
    assert len(placed_batches) == 1
    assert {r.name: r.skipped for r in o2.results}["C Premiere"] is True


def _project_from_fixture(tmp_path) -> Path:
    root = tmp_path / "ep5"
    sl.init_project(root)
    shutil.copy(FIVE, root / "shot_list.json")
    return root


def test_needs_ai_flows_into_approval_then_generation(tmp_path):
    root = _project_from_fixture(tmp_path)
    h = Harness(root, b_no_match=("S005",))
    o = orch.Orchestrator(opts(root, transcript=None, fps=None), runner=h, sleep=fake_premiere(root))
    o.execute()
    # D plan ran again after B, so S005 (B's miss) is in the approval list with a prompt.
    assert h.mods().count("pipeline.ai_gen plan") == 2
    gen_list = json.loads((root / "logs/ai_generation_list.json").read_text())
    listed = {i["id"] for i in gen_list["items"]}
    assert {"S004", "S005"} <= listed
    assert set(o.awaiting_approval()) == {"S004", "S005"}
    data = sl.load(root / "shot_list.json")
    assert sl.get_shot(data, "S004")["status"] == "pending"
    assert sl.get_shot(data, "S002")["status"] == "placed"

    # Jonathan approves S004 only, re-runs: D generates S004, C places it.
    rc = orch.subprocess_runner(["-m", "pipeline.ai_gen", "approve", "--project-root", str(root), "S004"])
    assert rc == 0
    h2 = Harness(root)
    o2 = orch.Orchestrator(opts(root, transcript=None, fps=None), runner=h2, sleep=fake_premiere(root))
    o2.execute()
    assert "pipeline.ai_gen generate" in h2.mods()
    data = sl.load(root / "shot_list.json")
    assert sl.get_shot(data, "S004")["status"] == "placed"
    assert sl.get_shot(data, "S005")["status"] == "needs_ai"
    assert o2.awaiting_approval() == ["S005"]


def test_stop_for_approval_skips_premiere(tmp_path):
    root = _project_from_fixture(tmp_path)
    h = Harness(root)
    o = orch.Orchestrator(opts(root, transcript=None, fps=None, stop_for_approval=True),
                          runner=h, sleep=fake_premiere(root))
    o.execute()
    assert not (root / orch.JOB_PATH).exists()
    assert sl.get_shot(sl.load(root / "shot_list.json"), "S002")["status"] == "downloaded"


def test_premiere_timeout_leaves_job_and_statuses(tmp_path):
    root = _project_from_fixture(tmp_path)
    t = [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s

    o = orch.Orchestrator(opts(root, transcript=None, fps=None, premiere_timeout_s=10),
                          runner=Harness(root), sleep=sleep, clock=clock)
    assert o.execute() == 1
    assert (root / orch.JOB_PATH).exists()
    assert sl.get_shot(sl.load(root / "shot_list.json"), "S002")["status"] == "downloaded"


def test_skip_premiere_writes_job(tmp_path):
    root = _project_from_fixture(tmp_path)
    o = orch.Orchestrator(opts(root, transcript=None, fps=None, skip_premiere=True,
                               prproj="/x/ep.prproj"), runner=Harness(root))
    o.execute()
    job = json.loads((root / orch.JOB_PATH).read_text())
    assert job["job_version"] == 1 and job["prproj"] == "/x/ep.prproj"
    assert job["source_sequence"] == "test_episode_main"


def test_panel_error_marks_nothing(tmp_path):
    root = _project_from_fixture(tmp_path)

    def sleep(_):
        job = json.loads((root / orch.JOB_PATH).read_text())
        (root / orch.RESULT_PATH).write_text(json.dumps(
            {"job_id": job["job_id"], "status": "error", "error": "no active project", "placed": []}))

    o = orch.Orchestrator(opts(root, transcript=None, fps=None), runner=Harness(root), sleep=sleep)
    assert o.execute() == 1
    assert not any(s["status"] == "placed" for s in sl.load(root / "shot_list.json")["shots"])


def test_missing_shot_list_requires_transcript(tmp_path):
    with pytest.raises(SystemExit):
        orch.Orchestrator(opts(tmp_path / "x", transcript=None), runner=lambda a: 0).execute()
