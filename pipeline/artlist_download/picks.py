"""logs/picks.csv (licensing record). Column order is fixed by docs/CONTRACT.md."""
from __future__ import annotations

import csv
import fcntl
import io
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common.shotlist import PICKS_CSV

COLUMNS = ["timestamp", "shot_id", "type", "query", "title", "url", "resolution", "duration_s",
           "confidence", "file"]


def picks_path(project_root: str | Path) -> Path:
    return Path(project_root) / PICKS_CSV


def append_pick(project_root: str | Path, row: dict) -> None:
    path = picks_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(row)
    row.setdefault("timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    with open(path, "a+", newline="", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0, io.SEEK_END)
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            if f.tell() == 0:
                w.writeheader()
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in COLUMNS})
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def last_pick_for(project_root: str | Path, shot_id: str) -> dict | None:
    path = picks_path(project_root)
    if not path.exists():
        return None
    last = None
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("shot_id") == shot_id:
                last = r
    return last
