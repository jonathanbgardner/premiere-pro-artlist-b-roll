"""Check the real Anthropic SDK path end to end against a mock HTTP transport (no network):
request shape, beta header, toolset results, and that response blocks replay cleanly."""
from __future__ import annotations

import json

import pytest

from artlist_fakes import FakeComputer, FakeDownload, make_project
from pipeline.artlist_download.agent import RunBudget, ShotAgent
from pipeline.artlist_download.config import Settings
from pipeline.artlist_download.guardrails import HumanGate, UrlPolicy
from pipeline.artlist_download.llm import FALLBACK_BETA, AnthropicLLM
from pipeline.common import shotlist as sl

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")


def _msg(content, stop="tool_use"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}}


def test_sdk_wire_format(tmp_path):
    replies = [
        _msg([{"type": "thinking", "thinking": "", "signature": "sig"},
              {"type": "tool_use", "id": "toolu_a", "name": "screenshot", "input": {},
               "toolset_name": "computer", "caller": {"type": "direct"}},
              {"type": "tool_use", "id": "toolu_b", "name": "left_click", "input": {"coordinate": [10, 20]},
               "toolset_name": "computer", "caller": {"type": "direct"}}]),
        _msg([{"type": "tool_use", "id": "toolu_c", "name": "report_pick", "caller": {"type": "direct"},
               "input": {"title": "Cows", "url": "https://artlist.io/clip/1", "resolution": "1920x1080",
                         "duration_s": 9, "confidence": 0.8, "query": "cattle feeding sunrise"}}]),
    ]
    sent = []

    def handler(request):
        sent.append({"url": str(request.url), "headers": dict(request.headers),
                     "body": json.loads(request.content)})
        return httpx2.Response(200, json=replies[len(sent) - 1])

    client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    llm = AnthropicLLM("claude-opus-5-5", client=client)

    root = make_project(tmp_path)
    s = Settings(interactive=False)
    fake = FakeComputer(UrlPolicy.from_settings(s),
                        on_click=lambda x, y, f: f.downloads.append(FakeDownload("u", "cows.mp4")))
    agent = ShotAgent(computer=fake, llm=llm, settings=s, gate=HumanGate(False), budget=RunBudget(s),
                      project_root=root, log=lambda m: None)
    shot = sl.get_shot(sl.load(root / "shot_list.json"), "S002")
    result = agent.run_shot(shot)
    assert result.status == "downloaded" and result.file == "assets/S002.mp4"

    first, second = sent
    assert "/v1/messages" in first["url"]
    assert FALLBACK_BETA in first["headers"].get("anthropic-beta", "")
    b = first["body"]
    assert b["model"] == "claude-opus-5-5" and b["fallbacks"] == "default"
    assert b["tools"][0]["type"] == "computer_toolset_20260801" and "name" not in b["tools"][0]
    assert b["output_config"] == {"effort": "medium"} and "thinking" not in b
    msgs = second["body"]["messages"]
    assistant = msgs[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0]["type"] == "thinking" and assistant["content"][0]["signature"] == "sig"
    assert assistant["content"][1]["toolset_name"] == "computer"
    results = msgs[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b"]
    assert all(r["toolset_name"] == "computer" for r in results)
    assert results[0]["content"][0]["type"] == "image"


def test_no_fallbacks_uses_plain_endpoint(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json=_msg([{"type": "text", "text": "hi"}], stop="end_turn"))

    client = anthropic.Anthropic(api_key="k", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    llm = AnthropicLLM("claude-opus-5-5", use_fallbacks=False, client=client)
    llm.create(max_tokens=10, messages=[{"role": "user", "content": "x"}])
    assert "fallbacks" not in json.loads(sent[0].content)
    assert FALLBACK_BETA not in sent[0].headers.get("anthropic-beta", "")
