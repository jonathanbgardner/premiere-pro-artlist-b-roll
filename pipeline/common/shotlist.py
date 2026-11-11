"""Shared helpers for shot_list.json (contract v1).

Every Python workstream (A, B, D, orchestrator) reads and writes the shot list
through this module only. Workstream C (UXP JavaScript) mirrors the timecode
rules in its own code and must stay consistent with `tc_to_seconds` here.

Rules:
- Always write via `save()` (validates, then atomic replace).
- Mutate shots with `update_shot()` while holding `locked()` when several
  workers may run at once (B and D run in parallel under the orchestrator).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "shot_list.schema.json"

SHOT_TYPES = ("stock_video", "music", "ai_video", "ai_audio")
STATUSES = ("pending", "downloaded", "placed", "needs_ai", "failed")

# Project folder layout (relative to project_root).
LAYOUT = {
    "transcript": "transcript",
    "assets": "assets",
    "scripts": "scripts",
    "logs": "logs",
}
SHOT_LIST_NAME = "shot_list.json"
PICKS_CSV = "logs/picks.csv"

# Extension used for files in assets/ by shot type.
ASSET_EXT = {"stock_video": ".mp4", "ai_video": ".mp4", "music": ".wav", "ai_audio": ".wav"}


class ShotListError(ValueError):
    pass


def _schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def validate(data: dict) -> None:
    try:
        jsonschema.validate(data, _schema())
    except jsonschema.ValidationError as e:
        raise ShotListError(f"shot_list invalid at {list(e.absolute_path)}: {e.message}") from e
    ids = [s["id"] for s in data["shots"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ShotListError(f"duplicate shot ids: {sorted(dupes)}")
    if data.get("drop_frame") and round(data["fps"], 2) not in (29.97, 59.94):
        raise ShotListError("drop_frame is only valid for 29.97 or 59.94 fps")


def init_project(project_root: str | os.PathLike) -> Path:
    root = Path(project_root)
    for d in LAYOUT.values():
        (root / d).mkdir(parents=True, exist_ok=True)
    return root


def new_shot_list(project: str, fps: float, *, max_ai_clips: int = 8,
                  source_sequence: str | None = None, drop_frame: bool = False) -> dict:
    data: dict[str, Any] = {
        "schema_version": 1,
        "project": project,
        "fps": fps,
        "drop_frame": drop_frame,
        "shots": [],
        "ai_budget": {"max_clips": max_ai_clips, "approved": [], "generated": []},
    }
    if source_sequence:
        data["source_sequence"] = source_sequence
    return data


def shot_list_path(project_root: str | os.PathLike) -> Path:
    return Path(project_root) / SHOT_LIST_NAME


def load(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    validate(data)
    return data


def save(path: str | os.PathLike, data: dict) -> None:
    validate(data)
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".shot_list.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(path: str | os.PathLike) -> Iterator[dict]:
    """Exclusive read-modify-write. Yields the loaded data; saves on clean exit."""
    path = Path(path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = load(path)
            yield data
            save(path, data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def get_shot(data: dict, shot_id: str) -> dict:
    for s in data["shots"]:
        if s["id"] == shot_id:
            return s
    raise KeyError(shot_id)


def update_shot(path: str | os.PathLike, shot_id: str, **fields: Any) -> dict:
    """Locked update of one shot's fields. Returns the updated shot."""
    with locked(path) as data:
        shot = get_shot(data, shot_id)
        shot.update(fields)
        return dict(shot)


def asset_filename(shot: dict, ext: str | None = None) -> str:
    return f"{LAYOUT['assets']}/{shot['id']}{ext or ASSET_EXT[shot['type']]}"


# ---------------------------------------------------------------- timecode

def _nominal(fps: float) -> int:
    return int(round(fps))


def tc_to_frames(tc: str, fps: float, drop_frame: bool | None = None) -> int:
    """SMPTE -> frame count. ';' before frames implies drop-frame."""
    if drop_frame is None:
        drop_frame = ";" in tc
    hh, mm, ss, ff = (int(x) for x in tc.replace(";", ":").split(":"))
    nom = _nominal(fps)
    if ff >= nom or ss >= 60 or mm >= 60:
        raise ShotListError(f"bad timecode {tc!r} for {fps} fps")
    frames = ((hh * 60 + mm) * 60 + ss) * nom + ff
    if drop_frame:
        drop = 2 if nom == 30 else 4
        total_min = hh * 60 + mm
        frames -= drop * (total_min - total_min // 10)
    return frames


def frames_to_tc(frames: int, fps: float, drop_frame: bool = False) -> str:
    nom = _nominal(fps)
    if drop_frame:
        drop = 2 if nom == 30 else 4
        per_10 = nom * 600 - drop * 9
        per_min = nom * 60 - drop
        d, m = divmod(frames, per_10)
        if m > drop:
            frames += drop * 9 * d + drop * ((m - drop) // per_min)
        else:
            frames += drop * 9 * d
    ff = frames % nom
    ss = (frames // nom) % 60
    mm = (frames // (nom * 60)) % 60
    hh = frames // (nom * 3600)
    sep = ";" if drop_frame else ":"
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


def tc_to_seconds(tc: str, fps: float, drop_frame: bool | None = None) -> float:
    """Real (wall-clock) seconds. NDF at 29.97 counts frames at 30000/1001."""
    return tc_to_frames(tc, fps, drop_frame) / _true_rate(fps)


def seconds_to_tc(seconds: float, fps: float, drop_frame: bool = False) -> str:
    return frames_to_tc(int(math.floor(seconds * _true_rate(fps) + 1e-6)), fps, drop_frame)


def _true_rate(fps: float) -> float:
    nom = _nominal(fps)
    if abs(fps - nom) > 0.001:  # 23.976, 29.97, 59.94
        return nom * 1000 / 1001
    return float(nom)
