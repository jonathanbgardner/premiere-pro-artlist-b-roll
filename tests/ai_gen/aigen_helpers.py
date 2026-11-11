"""Shared helpers for Workstream D tests (unique module name to avoid clashes)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pipeline.common import shotlist

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "shot_list_5.json"
FAKE_SERVER = Path(__file__).with_name("fake_artlist_server.py")


def write_shot_list(root: Path, max_clips: int = 8) -> Path:
    """Fixture list + D's fake needs_ai shots. Eligible: S003, S004, S005, S006(ai_audio), S007, S008."""
    shotlist.init_project(root)
    data = json.loads(FIXTURE.read_text())
    data["ai_budget"]["max_clips"] = max_clips
    by_id = {s["id"]: s for s in data["shots"]}
    by_id["S003"]["status"] = "needs_ai"
    by_id["S005"]["status"] = "needs_ai"
    data["shots"] += [
        {"id": "S006", "start_tc": "00:01:20:00", "duration_s": 8.0, "type": "ai_audio",
         "reason": "Stinger under the stats segment", "queries": [], "status": "pending",
         "file": None, "confidence": 0.0, "mood": "tense", "tempo": "slow"},
        {"id": "S007", "start_tc": "00:01:40:00", "duration_s": 3.5, "type": "stock_video",
         "reason": "Mentions drone survey of fields", "queries": ["drone crop survey"],
         "status": "needs_ai", "file": None, "confidence": 0.0},
        {"id": "S008", "start_tc": "00:02:00:00", "duration_s": 5.0, "type": "stock_video",
         "reason": "Mentions a 1950s farm kitchen", "queries": ["vintage farm kitchen"],
         "status": "needs_ai", "file": None, "confidence": 0.0},
    ]
    path = shotlist.shot_list_path(root)
    shotlist.save(path, data)
    return path


def write_test_config(path: Path, *, stdio: bool = False, extra: str = "") -> Path:
    lines = ["[poll]", "interval_s = 0.01", "timeout_s = 10.0", ""]
    if stdio:
        lines += [
            "[server]",
            'transport = "stdio"',
            f"command = {json.dumps(sys.executable)}",
            f"args = [{json.dumps(str(FAKE_SERVER))}]",
            'env_passthrough = ["FAKE_ARTLIST_LOG", "FAKE_ARTLIST_OUT", "FAKE_ARTLIST_MODE", '
            '"FAKE_ARTLIST_POLLS", "FAKE_ARTLIST_SUBMIT_DELAY", "PYTHONPATH"]',
            "",
        ]
    path.write_text("\n".join(lines) + "\n" + extra)
    return path


def set_budget(root: Path, *, max_clips=None, approved=None, generated=None):
    """Simulate a human hand-editing ai_budget (bypassing `approve`'s checks)."""
    path = shotlist.shot_list_path(root)
    with shotlist.locked(path) as data:
        b = data["ai_budget"]
        if max_clips is not None:
            b["max_clips"] = max_clips
        if approved is not None:
            b["approved"] = approved
        if generated is not None:
            b["generated"] = generated


def load(root: Path) -> dict:
    return shotlist.load(shotlist.shot_list_path(root))
