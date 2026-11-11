import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.common import shotlist as sl
from pipeline.shotlist_gen import cli, llm
from pipeline.shotlist_gen.check import violations
from pipeline.shotlist_gen.generator import RefuseOverwrite, run
from pipeline.shotlist_gen.transcript import parse_transcript

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "shotlist_gen"
SRT = FIX / "interview_4min.srt"
EXPECTED = FIX / "expected_offline.json"
FPS = 29.97


def _run_offline(root, **kw):
    return run(SRT, root, fps=FPS, project="field_notes_ep12", source_sequence="field_notes_ep12_main",
               offline=True, **kw)


# ----------------------------------------------------------------- offline

def test_offline_end_to_end_valid_and_rule_clean(tmp_path):
    res = _run_offline(tmp_path)
    data = sl.load(tmp_path / "shot_list.json")       # schema-validates
    segs = parse_transcript(SRT, FPS)
    assert violations(data, segs) == []
    types = [s["type"] for s in data["shots"]]
    assert types.count("music") == 3
    assert {s["section"] for s in data["shots"] if s["type"] == "music"} == {"intro", "main", "outro"}
    assert types.count("stock_video") >= 8
    assert all(2 <= len(s["queries"]) <= 3 for s in data["shots"])
    assert data["source_sequence"] == "field_notes_ep12_main"
    assert data["ai_budget"] == {"max_clips": 8, "approved": [], "generated": []}
    # project layout + transcript copy + log
    for d in ("transcript", "assets", "scripts", "logs"):
        assert (tmp_path / d).is_dir()
    assert (tmp_path / "transcript" / SRT.name).read_text() == SRT.read_text()
    assert (tmp_path / "logs" / "shotlist_gen.log").exists()
    assert res.path == tmp_path / "shot_list.json"


def test_offline_output_is_deterministic_and_matches_fixture(tmp_path):
    _run_offline(tmp_path / "a")
    _run_offline(tmp_path / "b")
    a = json.loads((tmp_path / "a" / "shot_list.json").read_text())
    b = json.loads((tmp_path / "b" / "shot_list.json").read_text())
    assert a == b
    if os.environ.get("UPDATE_EXPECTED"):
        shutil.copyfile(tmp_path / "a" / "shot_list.json", EXPECTED)
    assert a == json.loads(EXPECTED.read_text()), "offline output changed; rerun with UPDATE_EXPECTED=1 if intended"


# ----------------------------------------------------------------- re-run safety

def test_rerun_overwrites_all_pending(tmp_path):
    _run_offline(tmp_path)
    _run_offline(tmp_path)  # fine: nothing downstream yet


def test_refuses_to_overwrite_progress_unless_force(tmp_path):
    _run_offline(tmp_path)
    path = tmp_path / "shot_list.json"
    sl.update_shot(path, "S002", status="downloaded", file="assets/S002.mp4", updated_by="B")
    with pytest.raises(RefuseOverwrite):
        _run_offline(tmp_path)
    assert sl.get_shot(sl.load(path), "S002")["status"] == "downloaded"
    _run_offline(tmp_path, force=True)
    assert sl.get_shot(sl.load(path), "S002")["status"] == "pending"


def test_refuses_to_overwrite_human_ai_approvals(tmp_path):
    _run_offline(tmp_path)
    path = tmp_path / "shot_list.json"
    with sl.locked(path) as data:
        data["ai_budget"]["approved"] = ["S003"]
    with pytest.raises(RefuseOverwrite):
        _run_offline(tmp_path)


def test_cli_offline_and_refusal_exit_codes(tmp_path, capsys):
    args = [str(SRT), "--project-root", str(tmp_path), "--fps", "29.97", "--offline",
            "--project", "ep", "--max-ai-clips", "2"]
    assert cli.main(args) == 0
    data = sl.load(tmp_path / "shot_list.json")
    assert data["project"] == "ep" and data["ai_budget"]["max_clips"] == 2
    sl.update_shot(tmp_path / "shot_list.json", "S001", status="placed", updated_by="C")
    assert cli.main(args) == 3
    assert "refusing" in capsys.readouterr().err
    assert cli.main(args + ["--force"]) == 0


def test_cli_missing_transcript(tmp_path):
    assert cli.main([str(tmp_path / "nope.srt"), "--project-root", str(tmp_path), "--fps", "25",
                     "--offline"]) == 2


def test_premiere_text_transcript_end_to_end(tmp_path):
    lines = []
    for i in range(30):
        t = i * 6
        spk = "Speaker 1" if i < 12 or i >= 20 else "Speaker 2"
        lines.append(f"00:{t // 60:02d}:{t % 60:02d}:00 - 00:{(t + 6) // 60:02d}:{(t + 6) % 60:02d}:00\n{spk}\n"
                     f"We drove the tractor past the barn and fed the cattle, line {i}.\n")
    p = tmp_path / "ep.txt"
    p.write_text("\n".join(lines))
    run(p, tmp_path / "proj", fps=25, offline=True)
    data = sl.load(tmp_path / "proj" / "shot_list.json")
    assert data["fps"] == 25 and data["project"] == "ep"
    assert violations(data, parse_transcript(p, 25)) == []


# ----------------------------------------------------------------- mocked LLM

class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, payload=None, stop_reason="end_turn", text=None):
        self.calls = []
        body = text if text is not None else json.dumps(payload)
        msg = SimpleNamespace(stop_reason=stop_reason, stop_details=None,
                              content=[SimpleNamespace(type="thinking", thinking=""),
                                       SimpleNamespace(type="text", text=body)])

        def stream(**kwargs):
            self.calls.append(kwargs)
            return FakeStream(msg)

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))


def _cand(start, dur, reason, queries, type="stock_video", ai_prompt="", sequence=False, priority=2,
          trigger="concrete_noun"):
    return dict(start_s=start, duration_s=dur, type=type, trigger=trigger, reason=reason, queries=queries,
                ai_prompt=ai_prompt, sequence=sequence, priority=priority)


MODEL_PAYLOAD = {
    "candidates": [
        _cand(2.0, 5, "Show title card over the farm", ["ranch gate sign", "montana ranch aerial"]),   # in first-speaker window
        _cand(18.0, 5, "Green grass", ["green pasture grass", "spring grass field"]),                 # in Dana's window
        _cand(27.0, 12, "Feeding cattle before dawn", ["cattle feeding dawn", "rancher feeding cows", "cattle herd"]),
        _cand(30.0, 5, "Cold morning", ["frosty morning ranch", "cold morning farm"]),                # back to back
        _cand(47.0, 4, "Red barn from 1911", ["old red barn", "barn exterior", "farm building"], priority=1),
        _cand(51.5, 4, "Son on the tractor", ["young farmer tractor", "tractor field"], sequence=True),
        _cand(112.0, 6, "Soil microbiome", ["soil microbes"], type="ai_video",
              ai_prompt="Macro shot of glowing soil microbes and fungal threads", trigger="abstract", priority=1),
        _cand(125.0, 6, "Rotational grazing, herd moving", ["cattle herd moving pasture", "cows walking field"],
              type="ai_video", ai_prompt="Herd moving across pasture, drone shot"),
        _cand(160.0, 5, "Drone over pasture", ["drone over pasture"], type="ai_video", ai_prompt="FPV drone over ranch"),
        _cand(252.0, 8, "Goodbye wave", ["rancher waving", "farm sunset"]),                            # past end
        _cand("bad", 5, "malformed", ["x"]),
    ],
    "music": [
        {"section": "intro", "mood": "warm, pastoral", "tempo": "slow, 70-80 bpm",
         "queries": ["warm acoustic guitar ranch", "folk americana intro"]},
        {"section": "main", "mood": "gentle, reflective", "tempo": "steady, 85-95 bpm",
         "queries": ["reflective acoustic underscore", "soft folk background"]},
        # outro omitted: generator must fall back to the default bed
    ],
}


def test_mocked_llm_end_to_end(tmp_path):
    client = FakeClient(MODEL_PAYLOAD)
    res = run(SRT, tmp_path, fps=FPS, project="ep12", max_ai_clips=2, client=client, model="claude-test-model")
    data = sl.load(tmp_path / "shot_list.json")
    segs = parse_transcript(SRT, FPS)
    assert violations(data, segs) == []
    assert data["generator"] == {"workstream": "A", "mode": "llm", "model": "claude-test-model"}

    # request shape: structured output, fallbacks, model
    (call,) = client.calls
    assert call["model"] == "claude-test-model"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"] == llm.OUTPUT_SCHEMA
    assert call["fallbacks"] == "default" and llm.FALLBACK_BETA in call["betas"]
    assert "max_tokens" in call and "temperature" not in call
    user = call["messages"][0]["content"]
    assert "talking_head_stretches" in user and "Feed costs went up" in user

    shots = data["shots"]
    video = [s for s in shots if s["type"] != "music"]
    reasons = {s["reason"] for s in video}
    assert "Feeding cattle before dawn" in reasons
    assert "Goodbye wave" not in reasons or all(
        sl.tc_to_seconds(s["start_tc"], FPS) + s["duration_s"] <= segs[-1].end_s + 1e-3 for s in video)
    feeding = next(s for s in video if s["reason"] == "Feeding cattle before dawn")
    assert feeding["duration_s"] == 8.0                                   # clamped from 12
    assert sl.tc_to_seconds(feeding["start_tc"], FPS) == pytest.approx(27.0, abs=0.05)  # kept its slot
    assert sum(s["type"] == "ai_video" for s in shots) <= 2               # capped
    soil = next(s for s in video if s["reason"] == "Soil microbiome")
    assert soil["type"] == "ai_video" and soil["ai_prompt"].startswith("Macro")
    music = {s["section"]: s for s in shots if s["type"] == "music"}
    assert music["intro"]["mood"] == "warm, pastoral" and music["intro"]["tempo"] == "slow, 70-80 bpm"
    assert music["outro"]["queries"]                                      # default bed filled in
    assert any(s.get("sequence") for s in video) or "Son on the tractor" not in reasons
    assert res.log  # something was dropped/shifted and logged


def test_model_resolution(monkeypatch):
    monkeypatch.delenv(llm.MODEL_ENV, raising=False)
    assert llm.resolve_model() == "claude-opus-5-5"
    monkeypatch.setenv(llm.MODEL_ENV, "claude-sonnet-5-5")
    assert llm.resolve_model() == "claude-sonnet-5-5"
    assert llm.resolve_model("claude-haiku-5-5") == "claude-haiku-5-5"


def test_model_env_used_by_generator(tmp_path, monkeypatch):
    monkeypatch.setenv(llm.MODEL_ENV, "claude-env-model")
    client = FakeClient({"candidates": [], "music": []})
    run(SRT, tmp_path, fps=FPS, client=client)
    assert client.calls[0]["model"] == "claude-env-model"
    data = sl.load(tmp_path / "shot_list.json")
    # empty model output: talking-head fillers + default music still produce a usable list
    assert sum(s["type"] == "music" for s in data["shots"]) == 3
    assert sum(s["type"] == "stock_video" for s in data["shots"]) >= 3
    assert violations(data, parse_transcript(SRT, FPS)) == []


@pytest.mark.parametrize("kw,match", [
    ({"stop_reason": "refusal", "payload": {}}, "refused"),
    ({"stop_reason": "max_tokens", "payload": {}}, "max_tokens"),
    ({"text": "not json"}, "invalid JSON"),
])
def test_llm_errors_raise_and_do_not_write(tmp_path, kw, match):
    with pytest.raises(llm.LLMError, match=match):
        run(SRT, tmp_path, fps=FPS, client=FakeClient(**kw))
    assert not (tmp_path / "shot_list.json").exists()


def test_llm_sdk_exception_wrapped(tmp_path):
    def boom(**kwargs):
        raise ConnectionError("network down")
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=boom)))
    with pytest.raises(llm.LLMError, match="network down"):
        run(SRT, tmp_path, fps=FPS, client=client)
