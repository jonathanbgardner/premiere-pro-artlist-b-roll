"""A fake Artlist MCP server built with the real `mcp` SDK server side.

Exposes the tool names our default config guesses (generate_video,
get_generation_status, generate_voiceover) with async-job semantics:
submit -> generation_id, status -> processing ... completed + file:// asset url.

Every submit is appended (flock'ed) to $FAKE_ARTLIST_LOG so tests can count
credit-spending calls across processes.

Env (stdio mode):
  FAKE_ARTLIST_LOG           JSONL call log (required)
  FAKE_ARTLIST_OUT           dir for fake assets (required)
  FAKE_ARTLIST_MODE          ok | fail_job | submit_error   (default ok)
  FAKE_ARTLIST_POLLS         'processing' answers before completed (default 1)
  FAKE_ARTLIST_SUBMIT_DELAY  seconds to sleep in submit (default 0)
"""
from __future__ import annotations

import fcntl
import itertools
import json
import os
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def make_server(log_path: str | os.PathLike, out_dir: str | os.PathLike, *, mode: str = "ok",
                polls: int = 1, submit_delay: float = 0.0) -> MCPServer:
    log_path, out_dir = Path(log_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs: dict[str, dict] = {}
    counter = itertools.count(1)
    server = MCPServer("fake-artlist")

    def record(tool: str, args: dict) -> None:
        with open(log_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps({"tool": tool, "args": args, "pid": os.getpid(), "t": time.time()}) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)

    def submit(tool: str, args: dict, ext: str) -> dict:
        record(tool, args)
        if submit_delay:
            time.sleep(submit_delay)
        if mode == "submit_error":
            raise ToolError("insufficient credits")
        gid = f"gen-{os.getpid()}-{next(counter)}"
        jobs[gid] = {"polls": 0, "ext": ext}
        return {"generation_id": gid, "status": "queued"}

    @server.tool()
    def generate_video(prompt: str, duration: float = 5.0, model: str = "", resolution: str = "",
                       aspect_ratio: str = "16:9") -> str:
        """Fake text-to-video. Costs credits (it doesn't, but pretend)."""
        return json.dumps(submit("generate_video", {"prompt": prompt, "duration": duration, "model": model,
                                                    "resolution": resolution, "aspect_ratio": aspect_ratio}, ".mp4"))

    @server.tool()
    def generate_voiceover(text: str) -> str:
        """Fake voiceover."""
        return json.dumps(submit("generate_voiceover", {"text": text}, ".mp3"))

    @server.tool()
    def get_generation_status(generation_id: str) -> str:
        """Fake job status."""
        job = jobs.get(generation_id)
        if job is None:
            return json.dumps({"generation_id": generation_id, "status": "failed", "error": "unknown id"})
        job["polls"] += 1
        if job["polls"] <= polls:
            return json.dumps({"generation_id": generation_id, "status": "processing", "progress": 50})
        if mode == "fail_job":
            return json.dumps({"generation_id": generation_id, "status": "failed", "error": "content policy"})
        asset = out_dir / f"{generation_id}{job['ext']}"
        asset.write_bytes(b"FAKEMEDIA" + generation_id.encode())
        return json.dumps({"generation_id": generation_id, "status": "completed",
                           "result": {"video_url": asset.resolve().as_uri()}})

    return server


if __name__ == "__main__":
    make_server(os.environ["FAKE_ARTLIST_LOG"], os.environ["FAKE_ARTLIST_OUT"],
                mode=os.environ.get("FAKE_ARTLIST_MODE", "ok"),
                polls=int(os.environ.get("FAKE_ARTLIST_POLLS", "1")),
                submit_delay=float(os.environ.get("FAKE_ARTLIST_SUBMIT_DELAY", "0"))).run("stdio")
