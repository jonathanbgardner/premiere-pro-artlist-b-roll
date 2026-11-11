"""Phase 2: generate approved shots through the Artlist MCP, under a hard cap.

Cap and reservation rules (see README "Budget safety"):
- A slot is reserved by appending the shot ID to ai_budget.generated *under the
  shot_list lock*, after re-checking approval, eligibility, prompt hash and
  len(generated) < max_clips on freshly loaded data. Only then is the MCP called.
  Two parallel `generate` runs therefore can't overshoot or double-generate.
- If anything fails BEFORE the submit tool call is sent (bad arg mapping, tool disabled),
  the reservation is released.
- Once the submit call has been sent, the slot stays consumed even on failure:
  Artlist deducts credits at request time and a generation can't be cancelled, so the
  conservative assumption is that credits were spent. The shot is marked failed with
  the error. A human can free the slot by removing the ID from ai_budget.generated.
- A job that was submitted but not finished (poll timeout, crash) keeps its slot and
  its ai_job_id; the next `generate` run resumes polling it without a new submit.
"""
from __future__ import annotations

import asyncio
import csv
import fcntl
import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline.common import shotlist

from . import budget as B
from .config import ConfigError, gen_duration, kind_for, render_args, tool_cfg
from .mcp_client import ArtlistMCP, MCPCallError, ToolBackend

PICKS_COLUMNS = ["timestamp", "shot_id", "type", "query", "title", "url", "resolution",
                 "duration_s", "confidence", "file"]
MEDIA_EXT = {".mp4", ".mov", ".webm", ".wav", ".mp3", ".m4a", ".aac", ".flac"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"[ai_gen] {msg}", file=sys.stderr, flush=True)


@contextmanager
def _flocked_append(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            f.flush()
            fcntl.flock(f, fcntl.LOCK_UN)


def ledger(root: Path, event: str, shot_id: str, **extra: Any) -> None:
    with _flocked_append(root / B.LEDGER) as f:
        f.write(json.dumps({"at": _now(), "event": event, "shot_id": shot_id, "pid": os.getpid(), **extra}) + "\n")


def append_pick(root: Path, row: dict) -> None:
    path = root / shotlist.PICKS_CSV
    with _flocked_append(path) as f:
        w = csv.DictWriter(f, fieldnames=PICKS_COLUMNS)
        if f.tell() == 0:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in PICKS_COLUMNS})


# ---------------------------------------------------------------- response parsing

def find_key(obj: Any, keys: list[str]) -> Any:
    """Breadth-first search for the first present key (in `keys` priority order)."""
    queue = [obj]
    while queue:
        level, queue = queue, []
        for o in level:
            if isinstance(o, dict):
                for k in keys:
                    if k in o and o[k] not in (None, "") and not isinstance(o[k], (dict, list)):
                        return o[k]
                queue.extend(v for v in o.values() if isinstance(v, (dict, list)))
            elif isinstance(o, list):
                queue.extend(v for v in o if isinstance(v, (dict, list)))
    return None


def classify(cfg: dict, data: dict) -> tuple[str, str | None, str | None]:
    """-> (state, url, error) with state in {'done', 'failed', 'running'}."""
    r = cfg["response"]
    status = find_key(data, r["status_keys"])
    url = find_key(data, r["url_keys"])
    status_l = str(status).lower() if status is not None else ""
    if status_l in r["failed_values"]:
        return "failed", url, str(find_key(data, r["error_keys"]) or f"status {status}")
    if status_l in r["done_values"] or (status is None and url):
        return ("done", url, None) if url else ("failed", None, f"status {status} but no asset url")
    return "running", url, None


# ---------------------------------------------------------------- selection / reservation

def candidates(data: dict, cfg: dict) -> tuple[list[dict], list[dict], list[str]]:
    """-> (new shots to generate, in-flight shots to resume, skip messages)."""
    b = B.budget(data)
    new, resume, skips = [], [], []
    for sid in b["approved"]:
        try:
            shot = shotlist.get_shot(data, sid)
        except KeyError:
            skips.append(f"{sid}: approved but not in shot list")
            continue
        if sid in b["generated"]:
            if shot.get("ai_job_id") not in (None, "", "(none)") and B.is_eligible(shot):
                resume.append(shot)
            continue
        if not B.is_eligible(shot):
            skips.append(f"{sid}: status {shot['status']!r} not eligible")
            continue
        reason = _approval_problem(shot, cfg)
        if reason:
            skips.append(f"{sid}: {reason}")
            continue
        if not tool_cfg(cfg, kind_for(shot)).get("enabled", True):
            skips.append(f"{sid}: {kind_for(shot)} generation disabled in config")
            continue
        new.append(shot)
    return new, resume, skips


def _approval_problem(shot: dict, cfg: dict) -> str | None:
    if not (shot.get("ai_prompt") or "").strip():
        return "no ai_prompt (run plan)"
    h = shot.get("ai_approval_hash")
    if h is None:
        if cfg.get("generate", {}).get("require_approval_hash"):
            return "approved without `approve` (no approval hash); re-approve"
        return None
    if h != B.approval_hash(shot):
        return "prompt/duration/type changed since approval; re-approve"
    return None


class CapReached(Exception):
    pass


def reserve(path: Path, cfg: dict, shot_id: str) -> dict | None:
    """Atomically claim a budget slot for shot_id. Returns the fresh shot, or None if
    it is no longer eligible. Raises CapReached when len(generated) == max_clips."""
    with shotlist.locked(path) as data:
        b = B.budget(data)
        shot = shotlist.get_shot(data, shot_id)
        if (shot_id not in b["approved"] or shot_id in b["generated"] or not B.is_eligible(shot)
                or _approval_problem(shot, cfg)):
            return None
        if len(b["generated"]) >= int(b["max_clips"]):
            raise CapReached(f"cap reached: {len(b['generated'])}/{b['max_clips']} generated")
        b["generated"].append(shot_id)
        shot["ai_reserved_at"] = _now()
        shot["updated_by"] = "D"
        return dict(shot)


def release(path: Path, shot_id: str) -> None:
    with shotlist.locked(path) as data:
        gen = B.budget(data)["generated"]
        if shot_id in gen:
            gen.remove(shot_id)
        shot = shotlist.get_shot(data, shot_id)
        shot.pop("ai_reserved_at", None)
        shot["updated_by"] = "D"


def set_fields(path: Path, shot_id: str, drop: tuple[str, ...] = (), **fields: Any) -> None:
    with shotlist.locked(path) as data:
        shot = shotlist.get_shot(data, shot_id)
        for k in drop:
            shot.pop(k, None)
        shot.update(fields)
        shot["updated_by"] = "D"


# ---------------------------------------------------------------- download

def download(url: str, dest_no_ext: Path, default_ext: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    ext = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
    if ext not in MEDIA_EXT:
        ext = default_ext
    dest = dest_no_ext.with_suffix(ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(ext + ".part")
    if parsed.scheme == "file":
        shutil.copyfile(urllib.request.url2pathname(parsed.path), tmp)
    elif parsed.scheme in ("http", "https"):
        import httpx2  # the MCP token is deliberately NOT sent to asset hosts
        with httpx2.stream("GET", url, follow_redirects=True, timeout=httpx2.Timeout(30.0, read=300.0)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    else:
        raise ValueError(f"unsupported asset url scheme {parsed.scheme!r}")
    if tmp.stat().st_size == 0:
        tmp.unlink()
        raise ValueError("downloaded file is empty")
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- main loop

async def _poll(backend: ToolBackend, cfg: dict, t: dict, job_id: str,
                sleep: Callable[[float], Any]) -> tuple[str, str | None, str | None]:
    poll = cfg["poll"]
    deadline = time.monotonic() + float(poll["timeout_s"])
    errors = 0
    while True:
        try:
            data = await backend.call_tool(t["status_tool"], render_args(t.get("status_args", {}), {"job_id": job_id}))
            errors = 0
        except MCPCallError as e:
            errors += 1
            if errors >= int(poll.get("max_status_errors", 5)):
                return "running", None, f"status polling failed repeatedly: {e}"
            data = {}
        state, url, err = classify(cfg, data) if data else ("running", None, None)
        if state != "running":
            return state, url, err
        if time.monotonic() >= deadline:
            return "running", None, f"poll timeout after {poll['timeout_s']}s"
        await sleep(float(poll["interval_s"]))


async def _finish(root: Path, path: Path, cfg: dict, backend: ToolBackend, shot: dict, job_id: str,
                  first: dict | None, sleep) -> str:
    """Poll (unless the submit result is already final), download, record. Returns outcome."""
    kind = kind_for(shot)
    t = tool_cfg(cfg, kind)
    sid = shot["id"]
    state, url, err = classify(cfg, first) if first is not None else ("running", None, None)
    if state == "running":
        state, url, err = await _poll(backend, cfg, t, job_id, sleep)
    if state == "running":
        _log(f"{sid}: still running ({err}); slot kept, re-run generate to resume job {job_id}")
        ledger(root, "pending", sid, job_id=job_id, note=err)
        return "pending"
    if state == "failed":
        set_fields(path, sid, status="failed", error=f"AI generation failed: {err}")
        ledger(root, "failed", sid, job_id=job_id, error=err)
        _log(f"{sid}: FAILED: {err} (slot stays consumed: credits may have been spent)")
        return "failed"
    try:
        dest = download(url, root / shotlist.LAYOUT["assets"] / sid, shotlist.ASSET_EXT[shot["type"]])
    except Exception as e:  # noqa: BLE001
        msg = f"download failed: {e}; asset url kept in ai_asset_url"
        set_fields(path, sid, status="failed", error=msg, ai_asset_url=url)
        ledger(root, "failed", sid, job_id=job_id, error=msg)
        _log(f"{sid}: FAILED: {msg}")
        return "failed"
    rel = dest.relative_to(root).as_posix()
    confidence = float(cfg.get("generate", {}).get("confidence", 0.7))
    set_fields(path, sid, drop=("error",), status="downloaded", file=rel, confidence=confidence,
               ai_asset_url=url, ai_tool=f"{t['submit_tool']}/{t.get('model') or 'default'}")
    append_pick(root, {
        "timestamp": _now(), "shot_id": sid, "type": shot["type"], "query": shot.get("ai_prompt", ""),
        "title": f"AI generated: {t['submit_tool']}/{t.get('model') or 'default'}",
        "url": f"{url} (job {job_id})", "resolution": t.get("resolution", ""),
        "duration_s": gen_duration(cfg, kind, float(shot["duration_s"])), "confidence": confidence, "file": rel,
    })
    ledger(root, "downloaded", sid, job_id=job_id, file=rel)
    _log(f"{sid}: downloaded -> {rel}")
    return "downloaded"


async def generate_async(project_root: str | os.PathLike, cfg: dict, *, dry_run: bool = False,
                         backend_factory: Callable[[], Any] | None = None,
                         sleep: Callable[[float], Any] = asyncio.sleep, limit: int | None = None) -> dict:
    root = Path(project_root)
    path = shotlist.shot_list_path(root)
    data = shotlist.load(path)
    new, resume, skips = candidates(data, cfg)
    rem = B.remaining(data)
    report: dict[str, Any] = {"dry_run": dry_run, "skipped": skips, "remaining_before": rem,
                              "would_generate": [], "resumed": [], "results": {}}
    for s in skips:
        _log(f"skip {s}")
    if limit is not None:
        new = new[:max(0, limit)]
    if dry_run:
        report["would_generate"] = [s["id"] for s in new[:rem]]
        report["over_cap"] = [s["id"] for s in new[rem:]]
        report["would_resume"] = [s["id"] for s in resume]
        return report
    if not new and not resume:
        return report

    factory = backend_factory or (lambda: ArtlistMCP(cfg))
    async with factory() as backend:
        for shot in resume:
            report["resumed"].append(shot["id"])
            report["results"][shot["id"]] = await _finish(root, path, cfg, backend, shot, shot["ai_job_id"], None, sleep)
        for cand in new:
            sid = cand["id"]
            try:
                shot = reserve(path, cfg, sid)
            except CapReached as e:
                _log(f"STOP: {e}")
                report["results"][sid] = "cap_reached"
                report["cap_reached"] = True
                break
            if shot is None:
                report["results"][sid] = "not_eligible_anymore"
                continue
            ledger(root, "reserved", sid)
            kind = kind_for(shot)
            t = tool_cfg(cfg, kind)
            try:
                args = render_args(t.get("submit_args", {}), {
                    "prompt": shot["ai_prompt"], "duration_s": gen_duration(cfg, kind, float(shot["duration_s"])),
                    "model": t.get("model", ""), "resolution": t.get("resolution", ""),
                    "shot_id": sid, "aspect_ratio": "16:9",
                })
            except ConfigError as e:
                release(path, sid)  # nothing was sent: give the slot back
                ledger(root, "released", sid, error=str(e))
                report["results"][sid] = f"config_error: {e}"
                _log(f"{sid}: config error, reservation released: {e}")
                continue
            try:
                first = await backend.call_tool(t["submit_tool"], args)
            except MCPCallError as e:
                # The request may have reached Artlist: keep the slot, record the failure.
                set_fields(path, sid, status="failed", error=f"AI submit failed: {e}")
                ledger(root, "failed", sid, error=str(e), stage="submit")
                report["results"][sid] = "failed"
                _log(f"{sid}: submit FAILED: {e} (slot stays consumed: credits may have been spent)")
                continue
            job_id = find_key(first, cfg["response"]["job_id_keys"])
            job_id = str(job_id) if job_id is not None else ""
            set_fields(path, sid, ai_job_id=job_id or "(none)")
            ledger(root, "submitted", sid, job_id=job_id)
            if not job_id and classify(cfg, first)[0] == "running":
                set_fields(path, sid, status="failed", error="submit returned no job id and no asset")
                report["results"][sid] = "failed"
                continue
            report["results"][sid] = await _finish(root, path, cfg, backend, shot, job_id, first, sleep)
    return report


def generate(project_root: str | os.PathLike, cfg: dict, **kw: Any) -> dict:
    return asyncio.run(generate_async(project_root, cfg, **kw))
