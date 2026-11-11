"""Phase 2 (generate) with a pure-Python fake MCP backend."""
from __future__ import annotations

import asyncio
import csv
import json

import pytest

from aigen_helpers import load, set_budget
from pipeline.ai_gen import budget as B
from pipeline.ai_gen import cli, mcp_client
from pipeline.ai_gen.generate import PICKS_COLUMNS, generate
from pipeline.ai_gen.mcp_client import MCPCallError
from pipeline.ai_gen.plan import plan
from pipeline.common import shotlist


class FakeBackend:
    """Mimics ArtlistMCP: async context manager with call_tool()."""

    def __init__(self, out_dir, *, mode="ok", polls=1, never_finish=False):
        self.out_dir, self.mode, self.polls, self.never_finish = out_dir, mode, polls, never_finish
        self.calls: list[tuple[str, dict]] = []
        self.entered = 0
        self.jobs: dict[str, int] = {}

    def factory(self):
        return self

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        return None

    @property
    def submits(self):
        return [c for c in self.calls if c[0] == "generate_video"]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "generate_video":
            if self.mode == "submit_error":
                raise MCPCallError("tool 'generate_video' returned an error: insufficient credits")
            gid = f"job{len(self.jobs) + 1}"
            self.jobs[gid] = 0
            return {"generation_id": gid, "status": "queued"}
        if name == "get_generation_status":
            gid = arguments["generation_id"]
            self.jobs[gid] = self.jobs.get(gid, 0) + 1
            if self.never_finish or self.jobs[gid] <= self.polls:
                return {"status": "processing"}
            if self.mode == "fail_job":
                return {"status": "failed", "error": "content policy"}
            p = self.out_dir / f"{gid}.mp4"
            p.write_bytes(b"VIDEO")
            return {"status": "completed", "result": {"video_url": p.as_uri()}}
        raise AssertionError(f"unexpected tool {name}")


async def _nosleep(_):
    await asyncio.sleep(0)


@pytest.fixture
def fake(tmp_path):
    d = tmp_path / "remote"
    d.mkdir()
    return FakeBackend(d)


def run(project, cfg, fake, **kw):
    return generate(project, cfg, backend_factory=fake.factory, sleep=_nosleep, **kw)


def planned(project, cfg, approve_ids=(), max_clips=None):
    if max_clips is not None:
        set_budget(project, max_clips=max_clips)
    plan(project, cfg, use_llm=False)
    if approve_ids:
        B.approve(project, list(approve_ids))


def test_never_generates_without_approval(project, cfg, fake):
    planned(project, cfg)
    report = run(project, cfg, fake)
    assert fake.entered == 0 and fake.calls == []  # did not even connect
    assert report["results"] == {}
    assert load(project)["ai_budget"]["generated"] == []


def test_generates_only_approved(project, cfg, fake):
    planned(project, cfg, ["S004", "S007"])
    report = run(project, cfg, fake)
    assert report["results"] == {"S004": "downloaded", "S007": "downloaded"}
    assert [a["prompt"] for _, a in fake.submits] == [
        s["ai_prompt"] for s in load(project)["shots"] if s["id"] in ("S004", "S007")]
    data = load(project)
    shots = {s["id"]: s for s in data["shots"]}
    assert data["ai_budget"]["generated"] == ["S004", "S007"]
    for sid in ("S004", "S007"):
        s = shots[sid]
        assert s["status"] == "downloaded" and s["file"] == f"assets/{sid}.mp4"
        assert s["confidence"] == 0.7 and s["updated_by"] == "D"
        assert (project / s["file"]).read_bytes() == b"VIDEO"
    for sid in ("S003", "S005", "S008"):
        assert shots[sid]["status"] == "needs_ai" and shots[sid]["file"] is None
    # Submit args follow the config mapping.
    args = fake.submits[0][1]
    assert args["duration"] == 6.0 and args["model"] == "veo-3.1-fast" and args["aspect_ratio"] == "16:9"

    with open(project / "logs/picks.csv") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == PICKS_COLUMNS
    assert [r["shot_id"] for r in rows] == ["S004", "S007"]
    assert rows[0]["title"] == "AI generated: generate_video/veo-3.1-fast"
    assert "job1" in rows[0]["url"] and rows[0]["file"] == "assets/S004.mp4"


def test_cap_holds_across_repeated_runs_even_with_hand_edited_approvals(project, cfg, fake):
    planned(project, cfg, max_clips=2)
    # A human hand-edits approved beyond the budget (approve would refuse this).
    set_budget(project, approved=["S003", "S004", "S005", "S007", "S008"])
    r1 = run(project, cfg, fake)
    assert r1["results"] == {"S003": "downloaded", "S004": "downloaded", "S005": "cap_reached"}
    for _ in range(3):
        rn = run(project, cfg, fake)
        assert all(v == "cap_reached" for v in rn["results"].values())
    assert len(fake.submits) == 2
    data = load(project)
    assert data["ai_budget"]["generated"] == ["S003", "S004"]
    # Raising the cap is a human decision; then the next approved shots go.
    set_budget(project, max_clips=3)
    run(project, cfg, fake)
    assert len(fake.submits) == 3 and load(project)["ai_budget"]["generated"] == ["S003", "S004", "S005"]


def test_already_generated_count_against_cap(project, cfg, fake):
    planned(project, cfg, max_clips=3)
    set_budget(project, generated=["S010", "S011", "S012"], approved=["S004"])
    run(project, cfg, fake)
    assert fake.submits == []


def test_prompt_changed_after_approval_is_refused(project, cfg, fake):
    planned(project, cfg, ["S004", "S007"])
    shotlist.update_shot(shotlist.shot_list_path(project), "S007", ai_prompt="something else entirely")
    report = run(project, cfg, fake)
    assert report["results"] == {"S004": "downloaded"}
    assert any("changed since approval" in s for s in report["skipped"])
    assert "S007" not in load(project)["ai_budget"]["generated"]


def test_hand_approved_without_hash_allowed_unless_strict(project, cfg, fake):
    planned(project, cfg)
    set_budget(project, approved=["S004"])  # no ai_approval_hash
    cfg_strict = json.loads(json.dumps(cfg))
    cfg_strict["generate"]["require_approval_hash"] = True
    assert run(project, cfg_strict, fake)["results"] == {}
    assert run(project, cfg, fake)["results"] == {"S004": "downloaded"}


def test_ineligible_status_is_skipped_even_if_approved(project, cfg, fake):
    planned(project, cfg, ["S004"])
    shotlist.update_shot(shotlist.shot_list_path(project), "S004", status="downloaded", file="assets/S004.mp4")
    assert run(project, cfg, fake)["results"] == {} and fake.calls == []


def test_audio_disabled_is_not_sent_and_keeps_slot(project, cfg, fake):
    planned(project, cfg, ["S006"])
    report = run(project, cfg, fake)
    assert fake.calls == [] and any("disabled" in s for s in report["skipped"])
    assert load(project)["ai_budget"]["generated"] == []


def test_job_failure_marks_failed_and_keeps_slot(project, cfg, fake):
    fake.mode = "fail_job"
    planned(project, cfg, ["S004"])
    assert run(project, cfg, fake)["results"] == {"S004": "failed"}
    data = load(project)
    s = next(s for s in data["shots"] if s["id"] == "S004")
    assert s["status"] == "failed" and "content policy" in s["error"] and s["updated_by"] == "D"
    assert data["ai_budget"]["generated"] == ["S004"]  # credits may be spent: slot consumed
    run(project, cfg, fake)
    assert len(fake.submits) == 1  # never auto-retried


def test_submit_error_keeps_slot(project, cfg, fake):
    fake.mode = "submit_error"
    planned(project, cfg, ["S004"])
    assert run(project, cfg, fake)["results"] == {"S004": "failed"}
    data = load(project)
    assert data["ai_budget"]["generated"] == ["S004"]
    assert "insufficient credits" in next(s for s in data["shots"] if s["id"] == "S004")["error"]


def test_config_error_releases_reservation(project, cfg, fake):
    planned(project, cfg, ["S004"])
    bad = json.loads(json.dumps(cfg))
    bad["tools"]["video"]["submit_args"]["oops"] = "{not_a_var}"
    report = run(project, bad, fake)
    assert report["results"]["S004"].startswith("config_error")
    assert fake.calls == [] and load(project)["ai_budget"]["generated"] == []
    ledger = (project / "logs/ai_generation_ledger.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in ledger] == ["reserved", "released"]


def test_poll_timeout_keeps_slot_and_resumes_without_resubmit(project, cfg, fake):
    planned(project, cfg, ["S004"])
    fake.never_finish = True
    c = json.loads(json.dumps(cfg))
    c["poll"]["timeout_s"] = 0.05
    assert run(project, c, fake)["results"] == {"S004": "pending"}
    s = next(s for s in load(project)["shots"] if s["id"] == "S004")
    assert s["ai_job_id"] == "job1" and s["status"] == "pending"
    fake.never_finish = False
    report = run(project, cfg, fake)
    assert report["resumed"] == ["S004"] and report["results"] == {"S004": "downloaded"}
    assert len(fake.submits) == 1


def test_dry_run_does_not_connect(project, cfg, fake, monkeypatch, capsys):
    planned(project, cfg, ["S004", "S007"], max_clips=8)
    set_budget(project, max_clips=1)
    report = run(project, cfg, fake, dry_run=True)
    assert report["would_generate"] == ["S004"] and report["over_cap"] == ["S007"]
    assert fake.entered == 0

    def boom(*a, **k):
        raise AssertionError("dry run must not connect")
    monkeypatch.setattr(mcp_client.ArtlistMCP, "__aenter__", boom)
    assert cli.main(["generate", "--project-root", str(project), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["would_generate"] == ["S004"]
    assert load(project)["ai_budget"]["generated"] == []


def test_limit(project, cfg, fake):
    planned(project, cfg, ["S003", "S004", "S005"])
    run(project, cfg, fake, limit=1)
    assert load(project)["ai_budget"]["generated"] == ["S003"]
