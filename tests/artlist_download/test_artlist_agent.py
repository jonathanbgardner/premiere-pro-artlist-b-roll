"""Workstream B agent/runner tests with a scripted fake LLM and a fake browser (no network)."""
from __future__ import annotations

import csv
import json

import pytest

from artlist_fakes import FakeComputer, FakeDownload, ScriptedLLM, ct, make_project, text, tu
from pipeline.artlist_download.agent import COMPUTER_TOOLSET, CUSTOM_TOOLS
from pipeline.artlist_download.config import Settings
from pipeline.artlist_download.guardrails import HumanGate, UrlPolicy
from pipeline.artlist_download.picks import COLUMNS
from pipeline.artlist_download.runner import Runner
from pipeline.common import shotlist as sl

GOOD_VIDEO = dict(title="Cows at sunrise", url="https://artlist.io/stock-footage/clip/cows-sunrise/123",
                  resolution="3840x2160", duration_s=12.0, confidence=0.85, query="cattle feeding sunrise")
GOOD_MUSIC = dict(title="Morning Folk", url="https://artlist.io/royalty-free-music/song/morning-folk/9",
                  resolution="audio", duration_s=150.0, confidence=0.8, query="warm acoustic morning")


def settings(**kw) -> Settings:
    s = Settings(interactive=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def download_on_click(filename="artlist_cows_4k.mov", url="https://cdn.artlist.io/x/clip.mov"):
    def on_click(x, y, fake):
        fake.downloads.append(FakeDownload(url, filename))
    return on_click


def build(tmp_path, turns, *, s=None, on_click=None, gate=None, mutate=None, clock=None, **runner_kw):
    root = make_project(tmp_path, mutate)
    s = s or settings()
    fake = FakeComputer(UrlPolicy.from_settings(s), on_click=on_click)
    llm = ScriptedLLM(turns)
    logs: list[str] = []
    kw = dict(computer_factory=lambda: fake, llm=llm, gate=gate or HumanGate(s.interactive),
              log=logs.append, **runner_kw)
    if clock:
        kw["clock"] = clock
    runner = Runner(root, s, **kw)
    return root, runner, fake, llm, logs


def shot(root, sid):
    return sl.get_shot(sl.load(root / "shot_list.json"), sid)


def picks_rows(root):
    with open(root / "logs" / "picks.csv", newline="") as f:
        return list(csv.reader(f))


# ---------------------------------------------------------------- happy path

def test_pick_downloads_renames_and_updates_shot_and_csv(tmp_path):
    turns = [
        [tu("screenshot")],
        [tu("left_click", coordinate=[640, 60]), tu("type", text="cattle feeding sunrise"), tu("key", text="Return")],
        [tu("left_click", coordinate=[300, 400])],  # Download button
        [ct("report_pick", **GOOD_VIDEO)],
    ]
    root, runner, fake, llm, _ = build(tmp_path, turns, on_click=None, only=["S002"])
    clicks = {"n": 0}

    def on_click(x, y, f):
        clicks["n"] += 1
        if (x, y) == (300, 400):
            f.downloads.append(FakeDownload("https://cdn.artlist.io/x.mov", "Cows_Sunrise_4K.MOV"))
    fake.on_click = on_click

    summary = runner.run()
    assert summary["processed"] == [{"id": "S002", "status": "downloaded", "error": None,
                                     "file": "assets/S002.mov"}]
    s = shot(root, "S002")
    assert s["status"] == "downloaded"
    assert s["file"] == "assets/S002.mov"          # real extension kept, lower-cased
    assert s["confidence"] == 0.85
    assert s["source_title"] == "Cows at sunrise"
    assert s["source_url"] == GOOD_VIDEO["url"]
    assert s["updated_by"] == "B"
    assert (root / "assets" / "S002.mov").stat().st_size > 0
    assert not list((root / "assets").glob(".*"))  # no temp leftovers
    rows = picks_rows(root)
    assert rows[0] == COLUMNS == ["timestamp", "shot_id", "type", "query", "title", "url", "resolution",
                                  "duration_s", "confidence", "file"]
    assert rows[1][1:] == ["S002", "stock_video", "cattle feeding sunrise", "Cows at sunrise",
                           GOOD_VIDEO["url"], "3840x2160", "12.0", "0.85", "assets/S002.mov"]
    # other shots untouched
    assert shot(root, "S003")["status"] == "pending"
    assert fake.closed


def test_request_shape_and_toolset_results(tmp_path):
    turns = [[tu("screenshot"), tu("mouse_move", coordinate=[10, 10])],
             [tu("left_click", coordinate=[1, 1])],
             [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, fake, llm, _ = build(tmp_path, turns, on_click=download_on_click(), only=["S002"])
    runner.run()
    req = llm.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["tools"][0] == COMPUTER_TOOLSET and req["tools"][0]["type"] == "computer_toolset_20260801"
    assert {t["name"] for t in req["tools"][1:]} == {"navigate", "report_pick", "report_no_match",
                                                    "request_human"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in CUSTOM_TOOLS)
    assert "thinking" not in req and req["output_config"] == {"effort": "medium"}
    assert "S002" in req["messages"][0]["content"] and "6 s" in req["messages"][0]["content"]
    results = llm.requests[1]["messages"][-1]["content"]
    assert [r["toolset_name"] for r in results] == ["computer", "computer"]
    assert results[0]["content"][0]["type"] == "image"
    assert results[1]["content"][0]["text"] == "OK"
    # the assistant turn is replayed unchanged
    assert llm.requests[1]["messages"][1]["role"] == "assistant"


def test_music_pick(tmp_path):
    turns = [[tu("left_click", coordinate=[5, 5])], [ct("report_pick", **GOOD_MUSIC)]]
    root, runner, *_ = build(tmp_path, turns, on_click=download_on_click("Morning Folk.wav"), only=["S001"])
    runner.run()
    s = shot(root, "S001")
    assert s["status"] == "downloaded" and s["file"] == "assets/S001.wav"


# ---------------------------------------------------------------- acceptance rules

def test_pick_rejections_then_accept(tmp_path):
    turns = [
        [ct("report_pick", **{**GOOD_VIDEO, "duration_s": 5.0})],         # < 4 + 2
        [ct("report_pick", **{**GOOD_VIDEO, "resolution": "1280x720"})],   # < 1080p
        [ct("report_pick", **{**GOOD_VIDEO, "url": "https://pexels.com/x"})],
        [ct("report_pick", **GOOD_VIDEO)],                                 # no download yet
        [tu("left_click", coordinate=[2, 2])],
        [ct("report_pick", **GOOD_VIDEO)],
    ]
    s = settings(download_timeout_s=0.0)
    root, runner, fake, llm, _ = build(tmp_path, turns, s=s, on_click=download_on_click(), only=["S002"])
    runner.run()
    errs = [llm.requests[i]["messages"][-1]["content"][0] for i in range(1, 5)]
    assert all(e["is_error"] for e in errs)
    assert "shorter than the required 6" in errs[0]["content"][0]["text"]
    assert "below 1080p" in errs[1]["content"][0]["text"]
    assert "not an Artlist URL" in errs[2]["content"][0]["text"]
    assert "No download was captured" in errs[3]["content"][0]["text"]
    assert shot(root, "S002")["status"] == "downloaded"


def test_wrong_file_type_rejected(tmp_path):
    turns = [[tu("left_click", coordinate=[2, 2])], [ct("report_pick", **GOOD_VIDEO)],
             [ct("report_no_match", reason="only zip available",
                 queries_tried=["cattle feeding sunrise", "farm morning herd", "cows field"])]]
    root, runner, fake, llm, _ = build(tmp_path, turns, on_click=download_on_click("clip.zip"), only=["S002"])
    runner.run()
    assert "not one of" in llm.requests[2]["messages"][-1]["content"][0]["content"][0]["text"]
    assert not list((root / "assets").iterdir())
    assert shot(root, "S002")["status"] == "needs_ai"


def test_no_match_requires_all_queries_then_needs_ai(tmp_path):
    turns = [
        [ct("report_no_match", reason="nothing", queries_tried=["cattle feeding sunrise"])],
        [ct("report_no_match", reason="no dawn cattle clips >= 6 s",
            queries_tried=["Cattle feeding sunrise", "farm morning herd", "cows field"])],
    ]
    root, runner, fake, llm, _ = build(tmp_path, turns, only=["S002"])
    runner.run()
    rejected = llm.requests[1]["messages"][-1]["content"][0]
    assert rejected["is_error"] and "farm morning herd" in rejected["content"][0]["text"]
    s = shot(root, "S002")
    assert s["status"] == "needs_ai" and s["file"] is None and s["updated_by"] == "B"
    assert s["b_no_match_reason"] == "no dawn cattle clips >= 6 s"
    assert not (root / "logs" / "picks.csv").exists()


# ---------------------------------------------------------------- guardrails

def test_navigate_refuses_offsite_and_billing_and_fails_after_limit(tmp_path):
    turns = [
        [ct("navigate", url="https://www.google.com/search?q=cows")],
        [ct("navigate", url="https://artlist.io/account/billing")],
        [ct("navigate", url="https://artlist.io.evil.com/")],
        [ct("navigate", url="https://artlist.io/settings/subscription")],
    ]
    s = settings(max_guardrail_violations=3)
    root, runner, fake, llm, logs = build(tmp_path, turns, s=s, only=["S002"])
    runner.run()
    for i in (1, 2, 3):
        r = llm.requests[i]["messages"][-1]["content"][0]
        assert r["is_error"] and "Refused by harness" in r["content"][0]["text"]
    assert ("goto", "https://www.google.com/search?q=cows") not in fake.actions
    s2 = shot(root, "S002")
    assert s2["status"] == "failed" and s2["error"].startswith("guardrail")


def test_allowed_navigation_works(tmp_path):
    turns = [[ct("navigate", url="https://artlist.io/stock-footage/search?terms=cows")],
             [tu("left_click", coordinate=[1, 1])], [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, fake, llm, _ = build(tmp_path, turns, on_click=download_on_click(), only=["S002"])
    runner.run()
    assert ("goto", "https://artlist.io/stock-footage/search?terms=cows") in fake.actions
    assert shot(root, "S002")["status"] == "downloaded"


def test_url_check_after_action_goes_back(tmp_path):
    """A click that lands on a blocked page (e.g. via redirect) is undone after the action."""
    def on_click(x, y, f):
        if (x, y) == (50, 50):
            f._nav("https://artlist.io/account/subscription", through_route=False)
        else:
            f.downloads.append(FakeDownload("https://artlist.io/d", "a.mp4"))

    turns = [[tu("left_click", coordinate=[50, 50])], [tu("left_click", coordinate=[9, 9])],
             [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, fake, llm, logs = build(tmp_path, turns, on_click=on_click, only=["S002"])
    runner.run()
    assert ("back",) in fake.actions
    notes = [b for b in llm.requests[1]["messages"][-1]["content"] if b.get("type") == "text"]
    assert notes and "GUARDRAIL" in notes[0]["text"]
    assert fake.url == "https://artlist.io/"
    assert shot(root, "S002")["status"] == "downloaded"


def test_route_block_reported_to_model(tmp_path):
    def on_click(x, y, f):
        f._nav("https://artlist.io/checkout?plan=pro")  # interceptor aborts it

    turns = [[tu("left_click", coordinate=[50, 50])],
             [ct("report_no_match", reason="x", queries_tried=["cattle feeding sunrise", "farm morning herd",
                                                                "cows field"])]]
    root, runner, fake, llm, _ = build(tmp_path, turns, on_click=on_click, only=["S002"])
    runner.run()
    notes = [b for b in llm.requests[1]["messages"][-1]["content"] if b.get("type") == "text"]
    assert "blocked navigation" in notes[0]["text"] and "checkout" in notes[0]["text"]
    assert fake.url == "https://artlist.io/"


def test_captcha_pauses_in_interactive_mode(tmp_path):
    prompts = []

    def on_click(x, y, f):
        if (x, y) == (10, 10):
            f.signals_override = {"captcha_frames": ["https://www.google.com/recaptcha/api2/anchor"]}
        else:
            f.downloads.append(FakeDownload("u", "c.mp4"))

    s = settings(interactive=True)
    holder = {}

    def fake_input(msg):
        prompts.append(msg)
        holder["fake"].signals_override = {}  # the human solved it
        return ""

    gate = HumanGate(True, input_fn=fake_input, print_fn=lambda m: prompts.append(m))
    turns = [[tu("left_click", coordinate=[10, 10])], [tu("left_click", coordinate=[20, 20])],
             [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, fake, llm, logs = build(tmp_path, turns, s=s, on_click=on_click, gate=gate, only=["S002"])
    holder["fake"] = fake
    runner.run()
    assert gate.pauses == 1 and any("captcha" in p for p in prompts)
    assert shot(root, "S002")["status"] == "downloaded"


def test_captcha_non_interactive_fails_shot_and_stops_run(tmp_path):
    def on_click(x, y, f):
        f.signals_override = {"title": "Just a moment..."}

    turns = [[tu("left_click", coordinate=[10, 10])]]
    root, runner, fake, llm, _ = build(tmp_path, turns, on_click=on_click)
    summary = runner.run()
    first = shot(root, "S001")
    assert first["status"] == "failed" and first["error"] == "needs_human" and first["updated_by"] == "B"
    assert summary["stopped"] == "needs_human"
    assert [shot(root, i)["status"] for i in ("S002", "S003", "S005")] == ["pending"] * 3


def test_login_page_detected_at_start(tmp_path):
    s = settings(start_url="https://artlist.io/login")
    root, runner, fake, llm, _ = build(tmp_path, [], s=s, only=["S002"])
    runner.run()
    assert shot(root, "S002")["error"] == "needs_human"
    assert llm.requests == []


def test_request_human_tool_non_interactive(tmp_path):
    turns = [[ct("request_human", reason="Artlist asks to log in")]]
    root, runner, *_ = build(tmp_path, turns, only=["S002"])
    runner.run()
    assert shot(root, "S002")["error"] == "needs_human"


def test_request_human_tool_interactive_continues(tmp_path):
    seen = []
    gate = HumanGate(True, input_fn=lambda m: seen.append(m) or "", print_fn=lambda m: None)
    turns = [[ct("request_human", reason="2FA")], [tu("left_click", coordinate=[3, 3])],
             [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, fake, llm, _ = build(tmp_path, turns, s=settings(interactive=True), gate=gate,
                                       on_click=download_on_click(), only=["S002"])
    runner.run()
    assert seen and shot(root, "S002")["status"] == "downloaded"


# ---------------------------------------------------------------- limits

def test_max_steps_per_shot(tmp_path):
    turns = [[tu("screenshot")] for _ in range(5)]
    root, runner, *_ = build(tmp_path, turns, s=settings(max_steps_per_shot=3), only=["S002"])
    runner.run()
    assert "max steps per shot" in shot(root, "S002")["error"]


def test_total_budget_stops_run_and_leaves_pending(tmp_path):
    turns = [[tu("screenshot")] for _ in range(10)]
    root, runner, fake, llm, _ = build(tmp_path, turns, s=settings(max_total_steps=2))
    summary = runner.run()
    assert "step budget" in summary["stopped"]
    assert len(llm.requests) == 2
    assert all(shot(root, i)["status"] == "pending" for i in ("S001", "S002", "S003", "S005"))


def test_cost_budget(tmp_path):
    turns = [[tu("screenshot")] for _ in range(10)]
    # 1000 in + 100 out on opus 5.5 = $0.006 per call
    root, runner, fake, llm, _ = build(tmp_path, turns, s=settings(max_total_cost_usd=0.01))
    summary = runner.run()
    assert "cost budget" in summary["stopped"] and len(llm.requests) == 2


def test_shot_timeout(tmp_path):
    t = {"now": 0.0}

    def clock():
        t["now"] += 100.0
        return t["now"]

    turns = [[tu("screenshot")] for _ in range(10)]
    root, runner, *_ = build(tmp_path, turns, s=settings(shot_timeout_s=250), clock=clock, only=["S002"])
    runner.run()
    assert "timeout" in shot(root, "S002")["error"]


def test_model_stops_without_report(tmp_path):
    turns = [[text("I think this is fine.")], [text("done")], [text("done")]]
    root, runner, fake, llm, _ = build(tmp_path, turns, only=["S002"])
    runner.run()
    assert "without report_pick" in shot(root, "S002")["error"]
    assert "must finish" in llm.requests[1]["messages"][-1]["content"]


def test_refusal_fails_shot(tmp_path):
    from types import SimpleNamespace
    turns = [[SimpleNamespace(type="refusal")]]
    root, runner, *_ = build(tmp_path, turns, only=["S002"])
    runner.run()
    assert shot(root, "S002")["error"] == "model_refusal"


def test_batch_stops_after_failed_action(tmp_path):
    turns = [[tu("left_click", coordinate=[5000, 5]), tu("type", text="x")],
             [ct("report_no_match", reason="x", queries_tried=["cattle feeding sunrise", "farm morning herd",
                                                                "cows field"])]]
    root, runner, fake, llm, _ = build(tmp_path, turns, only=["S002"])
    runner.run()
    res = llm.requests[1]["messages"][-1]["content"]
    assert res[0]["is_error"] and res[1]["is_error"] and "Not executed" in res[1]["content"][0]["text"]
    assert ("type", "x") not in fake.actions


# ---------------------------------------------------------------- selection / resume

def test_only_and_max_shots_and_types(tmp_path):
    turns = [[ct("report_no_match", reason="x", queries_tried=q)] for q in (
        ["warm acoustic morning", "gentle folk intro"],
        ["cattle feeding sunrise", "farm morning herd", "cows field"])]
    root, runner, fake, llm, _ = build(tmp_path, turns, max_shots=2)
    runner.run()
    assert [shot(root, i)["status"] for i in ("S001", "S002", "S003", "S004", "S005")] == \
        ["needs_ai", "needs_ai", "pending", "pending", "pending"]  # S004 is ai_video: never B's


def test_resume_skips_non_pending_and_existing_assets(tmp_path):
    def mutate(d):
        d["shots"][0]["status"] = "downloaded"
        d["shots"][0]["file"] = "assets/S001.wav"
        d["shots"][2]["status"] = "needs_ai"
    turns = [[tu("left_click", coordinate=[1, 1])], [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, fake, llm, _ = build(tmp_path, turns, mutate=mutate, on_click=download_on_click())
    # S005 already on disk from an interrupted run, with its picks.csv row written
    (root / "assets" / "S005.mp4").write_bytes(b"data")
    from pipeline.artlist_download.picks import append_pick
    append_pick(root, {"shot_id": "S005", "type": "stock_video", "query": "small town main street",
                       "title": "Main St", "url": "https://artlist.io/c/5", "resolution": "1920x1080",
                       "duration_s": 10, "confidence": 0.7, "file": "assets/S005.mp4"})
    summary = runner.run()
    assert summary["recovered"] == ["S005"]
    assert [p["id"] for p in summary["processed"]] == ["S002"]
    s5 = shot(root, "S005")
    assert s5["status"] == "downloaded" and s5["file"] == "assets/S005.mp4"
    assert s5["confidence"] == 0.7 and s5["source_title"] == "Main St" and s5["updated_by"] == "B"
    assert shot(root, "S001")["file"] == "assets/S001.wav" and shot(root, "S003")["status"] == "needs_ai"
    # Second run: nothing left to do, no model calls, no extra csv rows
    n_req, n_rows = len(llm.requests), len(picks_rows(root))
    root2_runner = Runner(root, settings(), computer_factory=lambda: fake, llm=llm, log=lambda m: None)
    summary2 = root2_runner.run()
    assert summary2["processed"] == [] and summary2["recovered"] == []
    assert len(llm.requests) == n_req and len(picks_rows(root)) == n_rows


def test_retry_failed(tmp_path):
    def mutate(d):
        d["shots"][1].update(status="failed", error="needs_human")
    turns = [[tu("left_click", coordinate=[1, 1])], [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, *_ = build(tmp_path, turns, mutate=mutate, on_click=download_on_click(), only=["S002"],
                             retry_failed=True)
    runner.run()
    s = shot(root, "S002")
    assert s["status"] == "downloaded" and "error" not in s


def test_dry_run_makes_no_calls_or_changes(tmp_path):
    root, runner, fake, llm, logs = build(tmp_path, [], dry_run=True)
    before = (root / "shot_list.json").read_text()
    summary = runner.run()
    assert [p["id"] for p in summary["processed"]] == ["S001", "S002", "S003", "S005"]
    assert llm.requests == [] and fake.actions == []
    assert (root / "shot_list.json").read_text() == before


def test_shot_list_stays_schema_valid(tmp_path):
    turns = [[tu("left_click", coordinate=[1, 1])], [ct("report_pick", **GOOD_VIDEO)]]
    root, runner, *_ = build(tmp_path, turns, on_click=download_on_click(), only=["S002"])
    runner.run()
    sl.load(root / "shot_list.json")  # validates
    json.loads((root / "logs" / "artlist_download_runs.jsonl").read_text().splitlines()[0])
