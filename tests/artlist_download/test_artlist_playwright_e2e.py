"""Real Playwright backend against a tiny local mock "stock site" (no internet).

The scripted fake LLM clicks a billing link and an off-site link (both must be
blocked by the route interceptor), then the Download button, then reports the
pick. Skipped if Playwright / Chromium are not usable here.
"""
from __future__ import annotations

import csv
import functools
import http.server
import threading

import pytest

from artlist_fakes import ScriptedLLM, ct, make_project, tu
from pipeline.artlist_download.config import Settings
from pipeline.artlist_download.guardrails import HumanGate, UrlPolicy
from pipeline.artlist_download.runner import Runner
from pipeline.common import shotlist as sl

sync_api = pytest.importorskip("playwright.sync_api")

INDEX = """<!doctype html><html><head><title>Mock Artlist - cattle</title>
<style>body{margin:0;font:16px sans-serif} a{position:absolute;display:block;width:200px;height:60px;
background:#eee;text-align:center;line-height:60px}</style></head><body>
<a id="dl" href="/files/cows_4k.mp4" download style="left:100px;top:100px">Download 4K</a>
<a id="acct" href="/account/billing" style="left:100px;top:300px">Billing</a>
<a id="off" href="http://offsite.invalid/" style="left:100px;top:400px">Elsewhere</a>
<p style="position:absolute;top:500px;left:100px">Cows at sunrise - 0:12 - 3840x2160</p>
</body></html>"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def mock_site(tmp_path):
    site = tmp_path / "site"
    (site / "files").mkdir(parents=True)
    (site / "index.html").write_text(INDEX)
    (site / "files" / "cows_4k.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    handler = functools.partial(_QuietHandler, directory=str(site))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_playwright_backend_end_to_end(tmp_path, mock_site):
    from pipeline.artlist_download.playwright_backend import PlaywrightComputer, launch_persistent

    root = make_project(tmp_path)
    s = Settings(interactive=False, headless=True, allowed_domains=["127.0.0.1"],
                 start_url=f"{mock_site}/index.html", download_timeout_s=10)
    pick = dict(title="Cows at sunrise", url=f"{mock_site}/clip/cows", resolution="3840x2160",
                duration_s=12.0, confidence=0.9, query="cattle feeding sunrise")
    llm = ScriptedLLM([
        [tu("screenshot")],
        [tu("left_click", coordinate=[200, 330])],            # billing link -> blocked
        [tu("left_click", coordinate=[200, 430])],            # off-site link -> blocked
        [ct("navigate", url="https://example.com/")],         # refused by the tool
        [tu("left_click", coordinate=[200, 130]), tu("wait", duration=1)],  # Download
        [ct("report_pick", **pick)],
    ])

    pw = sync_api.sync_playwright().start()
    try:
        try:
            ctx = launch_persistent(pw, tmp_path / "profile", headless=True, viewport=(1280, 800))
        except Exception as e:  # pragma: no cover - environment without a browser
            pytest.skip(f"chromium not launchable: {e}")
        holder = {}

        def factory():
            holder["c"] = PlaywrightComputer(ctx, UrlPolicy.from_settings(s), log=lambda m: None)
            return holder["c"]

        runner = Runner(root, s, computer_factory=factory, llm=llm, gate=HumanGate(False),
                        only=["S002"], log=lambda m: None)
        summary = runner.run()
    finally:
        pw.stop()

    assert summary["processed"][0]["status"] == "downloaded", summary
    # screenshot is a real PNG
    img = llm.requests[1]["messages"][-1]["content"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
    # billing + off-site clicks were blocked by the route interceptor and reported
    for i in (2, 3):
        notes = [b["text"] for b in llm.requests[i]["messages"][-1]["content"] if b.get("type") == "text"]
        assert notes and "GUARDRAIL" in notes[0], notes
    assert "/account/billing" in " ".join(
        b["text"] for b in llm.requests[2]["messages"][-1]["content"] if b.get("type") == "text")
    nav = llm.requests[4]["messages"][-1]["content"][0]
    assert nav["is_error"] and "Refused" in nav["content"][0]["text"]

    shot = sl.get_shot(sl.load(root / "shot_list.json"), "S002")
    assert shot["status"] == "downloaded" and shot["file"] == "assets/S002.mp4"
    data = (root / "assets" / "S002.mp4").read_bytes()
    assert data.startswith(b"\x00\x00\x00\x18ftypmp42")
    rows = list(csv.DictReader(open(root / "logs" / "picks.csv")))
    assert rows[0]["shot_id"] == "S002" and rows[0]["file"] == "assets/S002.mp4"
