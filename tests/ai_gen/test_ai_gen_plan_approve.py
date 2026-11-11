"""Phase 1 (plan) and the human approve command."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aigen_helpers import load, set_budget
from pipeline.ai_gen import budget as B
from pipeline.ai_gen import cli, mcp_client
from pipeline.ai_gen.plan import plan
from pipeline.ai_gen.prompts import template_prompt

ELIGIBLE = ["S003", "S004", "S005", "S006", "S007", "S008"]


@pytest.fixture
def no_mcp(monkeypatch):
    """Any attempt to touch MCP (connect or call) fails the test."""
    def boom(*a, **k):
        raise AssertionError("MCP must not be used in this phase")
    monkeypatch.setattr(mcp_client.ArtlistMCP, "__aenter__", boom)
    monkeypatch.setattr(mcp_client.ArtlistMCP, "call_tool", boom)
    monkeypatch.setattr(mcp_client, "Client", boom)


def test_plan_lists_eligible_and_never_generates(project, cfg, no_mcp, capsys):
    rc = cli.main(["plan", "--project-root", str(project), "--no-llm"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "STOPPED: waiting for approval" in out and "NOTHING has been generated" in out

    gl = json.loads((project / "logs/ai_generation_list.json").read_text())
    assert [i["id"] for i in gl["items"]] == ELIGIBLE
    for it in gl["items"]:
        assert set(["id", "type", "prompt", "duration_s", "est_credits", "reason"]) <= set(it)
    assert gl["budget"]["remaining"] == 8
    assert gl["totals"]["count"] == 6
    assert gl["totals"]["est_credits"] == sum(i["est_credits"] for i in gl["items"])

    data = load(project)
    assert data["ai_budget"]["approved"] == [] and data["ai_budget"]["generated"] == []
    shots = {s["id"]: s for s in data["shots"]}
    # A's prompt is kept verbatim; others get a template prompt; credits written; updated_by D.
    assert shots["S004"]["ai_prompt"].startswith("Macro cinematic shot of soil bacteria")
    for sid in ELIGIBLE:
        assert shots[sid]["ai_prompt"] and shots[sid]["est_credits"] >= 0 and shots[sid]["updated_by"] == "D"
        assert shots[sid]["status"] in ("needs_ai", "pending") and shots[sid]["file"] is None
    # Non-eligible shots are untouched.
    assert "ai_prompt" not in shots["S001"] and "updated_by" not in shots["S002"]
    assert not any((project / "assets").iterdir())


def test_plan_credit_estimate_uses_config_table(project, cfg):
    res = plan(project, cfg, use_llm=False)
    s004 = next(i for i in res["items"] if i["id"] == "S004")  # 6 s, veo-3.1-fast 720p = 175/s
    assert s004["gen_duration_s"] == 6 and s004["est_credits"] == 6 * 175
    s007 = next(i for i in res["items"] if i["id"] == "S007")  # 3.5 s rounds up to 4
    assert s007["gen_duration_s"] == 4 and s007["est_credits"] == 4 * 175
    s006 = next(i for i in res["items"] if i["id"] == "S006")
    assert any("disabled" in n for n in s006["notes"])


def test_plan_marks_items_beyond_remaining_budget(project, cfg):
    set_budget(project, max_clips=2)
    res = plan(project, cfg, use_llm=False)
    assert [i["within_budget"] for i in res["items"]] == [True, True, False, False, False, False]


class FakeAnthropic:
    def __init__(self, text="A drone glides low over green crop rows at golden hour.", fail=False):
        self.calls = []
        self.text, self.fail = text, fail
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("no credentials")
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=self.text)])


def test_plan_llm_prompts_and_fallback(project, cfg):
    fake = FakeAnthropic()
    res = plan(project, cfg, use_llm=True, llm_client=fake)
    src = {i["id"]: i["prompt_source"] for i in res["items"]}
    assert src["S004"] == "shot"  # existing ai_prompt is not sent to Claude
    assert src["S007"] == "llm"
    assert all(c["model"] == "claude-opus-5-5" for c in fake.calls)
    assert len(fake.calls) == 5

    root2 = project.parent / "proj2"
    from aigen_helpers import write_shot_list
    write_shot_list(root2)
    res2 = plan(root2, cfg, use_llm=True, llm_client=FakeAnthropic(fail=True))
    s7 = next(i for i in res2["items"] if i["id"] == "S007")
    assert s7["prompt_source"] == "template" and s7["prompt"] == template_prompt(
        next(s for s in load(root2)["shots"] if s["id"] == "S007") | {"ai_prompt": ""})


# ---------------------------------------------------------------- approve (human command)

def test_approve_appends_and_hashes(project, cfg, no_mcp):
    plan(project, cfg, use_llm=False)
    assert cli.main(["approve", "--project-root", str(project), "S004", "S007"]) == 0
    data = load(project)
    assert data["ai_budget"]["approved"] == ["S004", "S007"]
    shots = {s["id"]: s for s in data["shots"]}
    assert shots["S004"]["ai_approval_hash"] == B.approval_hash(shots["S004"])
    assert shots["S004"]["updated_by"] == "human"
    assert data["ai_budget"]["generated"] == []  # approving never generates


def test_approve_refuses_over_budget_atomically(project, cfg, capsys):
    set_budget(project, max_clips=3, generated=["S002"])  # 2 slots left
    plan(project, cfg, use_llm=False)
    rc = cli.main(["approve", "--project-root", str(project), "S003", "S004", "S005"])
    assert rc == 2
    assert "remaining budget 2" in capsys.readouterr().err
    assert load(project)["ai_budget"]["approved"] == []  # nothing partially approved

    assert cli.main(["approve", "--project-root", str(project), "S003", "S004"]) == 0
    # Already-approved-but-not-generated count against the remaining budget too.
    assert cli.main(["approve", "--project-root", str(project), "S005"]) == 2
    assert load(project)["ai_budget"]["approved"] == ["S003", "S004"]


def test_approve_all_listed_respects_budget(project, cfg):
    plan(project, cfg, use_llm=False)
    with pytest.raises(B.BudgetError):
        set_budget(project, max_clips=5)
        B.approve(project, all_listed=True)  # 6 listed > 5
    set_budget(project, max_clips=8)
    assert B.approve(project, all_listed=True) == ELIGIBLE


def test_approve_refuses_ineligible_or_unplanned(project, cfg):
    with pytest.raises(B.BudgetError, match="no ai_prompt"):
        B.approve(project, ["S007"])  # not planned yet: no concrete prompt to approve
    plan(project, cfg, use_llm=False)
    with pytest.raises(B.BudgetError, match="not eligible"):
        B.approve(project, ["S002"])  # stock_video pending
    with pytest.raises(B.BudgetError, match="no such shot"):
        B.approve(project, ["S999"])
    with pytest.raises(B.BudgetError, match="all-listed"):
        B.approve(project, [])
    assert load(project)["ai_budget"]["approved"] == []


def test_approve_all_listed_needs_plan(project):
    with pytest.raises(B.BudgetError, match="run `plan` first"):
        B.approve(project, all_listed=True)
