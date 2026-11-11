"""Real `mcp` SDK client path against a fake Artlist server (in-process and stdio)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from aigen_helpers import REPO, load, set_budget, write_shot_list, write_test_config
from pipeline.ai_gen import budget as B
from pipeline.ai_gen.config import load_config
from pipeline.ai_gen.generate import generate
from pipeline.ai_gen.mcp_client import ArtlistMCP, MCPCallError
from pipeline.ai_gen.plan import plan

sys.path.insert(0, os.path.dirname(__file__))
from fake_artlist_server import make_server  # noqa: E402


def submits(log_path):
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if json.loads(line)["tool"].startswith("generate_")]


def test_in_process_server_end_to_end(project, cfg, tmp_path):
    log = tmp_path / "calls.jsonl"
    server = make_server(log, tmp_path / "remote", polls=2)
    plan(project, cfg, use_llm=False)
    B.approve(project, ["S004", "S008"])
    report = generate(project, cfg, backend_factory=lambda: ArtlistMCP(cfg, target=server))
    assert report["results"] == {"S004": "downloaded", "S008": "downloaded"}
    shots = {s["id"]: s for s in load(project)["shots"]}
    assert (project / shots["S004"]["file"]).read_bytes().startswith(b"FAKEMEDIA")
    calls = submits(log)
    assert [c["args"]["duration"] for c in calls] == [6.0, 6.0]  # S008 5 s rounds up to 6
    assert calls[0]["args"]["prompt"] == shots["S004"]["ai_prompt"]


def test_in_process_server_tool_error_is_mcp_error(project, cfg, tmp_path):
    server = make_server(tmp_path / "calls.jsonl", tmp_path / "remote", mode="submit_error")
    plan(project, cfg, use_llm=False)
    B.approve(project, ["S004"])
    report = generate(project, cfg, backend_factory=lambda: ArtlistMCP(cfg, target=server))
    assert report["results"] == {"S004": "failed"}
    s = next(s for s in load(project)["shots"] if s["id"] == "S004")
    assert s["status"] == "failed" and "insufficient credits" in s["error"]


def test_list_tools_in_process(cfg, tmp_path):
    server = make_server(tmp_path / "c.jsonl", tmp_path / "r")

    async def go():
        async with ArtlistMCP(cfg, target=server) as m:
            return await m.list_tools()
    names = {t["name"] for t in asyncio.run(go())}
    assert {"generate_video", "get_generation_status", "generate_voiceover"} <= names


# ---------------------------------------------------------------- stdio via the real CLI

def _env(tmp_path, **extra):
    env = dict(os.environ)
    env.update({"FAKE_ARTLIST_LOG": str(tmp_path / "calls.jsonl"), "FAKE_ARTLIST_OUT": str(tmp_path / "remote"),
                "PYTHONPATH": str(REPO)})
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _cli(args, env, **kw):
    return subprocess.run([sys.executable, "-m", "pipeline.ai_gen", *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120, **kw)


def test_stdio_tools_subcommand(tmp_path):
    conf = write_test_config(tmp_path / "stdio.toml", stdio=True)
    r = _cli(["tools", "--config", str(conf)], _env(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "generate_video" in r.stdout and "prompt*" in r.stdout


def test_stdio_generate_cli(tmp_path):
    root = tmp_path / "proj"
    write_shot_list(root)
    conf = write_test_config(tmp_path / "stdio.toml", stdio=True)
    env = _env(tmp_path)
    assert _cli(["plan", "--project-root", str(root), "--no-llm", "--config", str(conf)], env).returncode == 0
    assert submits(tmp_path / "calls.jsonl") == []  # plan never generates
    assert _cli(["approve", "--project-root", str(root), "S007"], env).returncode == 0
    r = _cli(["generate", "--project-root", str(root), "--config", str(conf)], env)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["results"] == {"S007": "downloaded"}
    assert (root / "assets/S007.mp4").exists()
    assert len(submits(tmp_path / "calls.jsonl")) == 1


@pytest.mark.parametrize("procs", [2, 3])
def test_concurrent_generate_processes_never_overshoot(tmp_path, procs):
    root = tmp_path / "proj"
    write_shot_list(root, max_clips=3)
    conf = write_test_config(tmp_path / "stdio.toml", stdio=True)
    cfg = load_config(conf)
    plan(root, cfg, use_llm=False)
    # Hand-edited (over-budget) approvals: only the generate-side cap protects us here.
    set_budget(root, approved=["S003", "S004", "S005", "S007", "S008"])
    env = _env(tmp_path, FAKE_ARTLIST_SUBMIT_DELAY=0.3)
    cmd = [sys.executable, "-m", "pipeline.ai_gen", "generate", "--project-root", str(root), "--config", str(conf)]
    ps = [subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
          for _ in range(procs)]
    outs = [p.communicate(timeout=120) for p in ps]
    for p, (out, err) in zip(ps, outs):
        assert p.returncode == 0, err
    calls = submits(tmp_path / "calls.jsonl")
    assert len(calls) == 3, calls
    assert len({c["args"]["prompt"] for c in calls}) == 3  # no shot generated twice
    data = load(root)
    assert len(data["ai_budget"]["generated"]) == 3 == len(set(data["ai_budget"]["generated"]))
    downloaded = [s["id"] for s in data["shots"] if s["status"] == "downloaded"]
    assert sorted(downloaded) == sorted(data["ai_budget"]["generated"])
    # Work was actually shared or raced, and at least one process hit the cap.
    assert any('"cap_reached"' in out for out, _ in outs)
    # Re-running afterwards still spends nothing.
    r = _cli(["generate", "--project-root", str(root), "--config", str(conf)], env)
    assert r.returncode == 0 and len(submits(tmp_path / "calls.jsonl")) == 3


def test_token_is_never_in_errors(tmp_path, monkeypatch):
    secret = "sk-artlist-SECRET-123456"
    monkeypatch.setenv("ARTLIST_MCP_TOKEN", secret)
    conf = tmp_path / "bad.toml"
    conf.write_text('[server]\nurl = "http://127.0.0.1:9/mcp"\ntimeout_s = 5\n')
    cfg = load_config(conf)
    m = ArtlistMCP(cfg)
    assert secret not in m.describe() and "token: set" in m.describe()

    async def go():
        async with ArtlistMCP(cfg):
            pass
    t0 = time.monotonic()
    with pytest.raises(MCPCallError) as ei:
        asyncio.run(go())
    assert secret not in str(ei.value) and ei.value.__cause__ is None
    assert time.monotonic() - t0 < 60

    r = _cli(["tools", "--config", str(conf)], dict(os.environ, ARTLIST_MCP_TOKEN=secret, PYTHONPATH=str(REPO)))
    assert r.returncode == 2 and secret not in r.stdout + r.stderr
