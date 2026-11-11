"""CLI: python -m pipeline.shotlist_gen <transcript> --project-root DIR --fps 29.97 ..."""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from . import llm
from .generator import RefuseOverwrite, run
from .transcript import TranscriptError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pipeline.shotlist_gen",
                                description="Generate shot_list.json from a Premiere transcript (SRT or timestamped text).")
    p.add_argument("transcript", help="SRT / VTT / Premiere text export")
    p.add_argument("--project-root", required=True, help="project folder (created if missing)")
    p.add_argument("--fps", type=float, required=True, help="sequence frame rate, e.g. 29.97")
    p.add_argument("--drop-frame", action="store_true", help="write drop-frame timecodes (29.97/59.94 only)")
    p.add_argument("--project", help="project name (default: transcript file name)")
    p.add_argument("--source-sequence", help="name of the original Premiere sequence")
    p.add_argument("--max-ai-clips", type=int, default=8, help="ai_budget.max_clips (default 8)")
    p.add_argument("--model", help=f"Claude model (default ${llm.MODEL_ENV} or {llm.DEFAULT_MODEL})")
    p.add_argument("--offline", action="store_true", help="heuristic candidates, no API call")
    p.add_argument("--force", action="store_true", help="overwrite a shot list that already has progress")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        res = run(args.transcript, args.project_root, fps=args.fps, project=args.project,
                  source_sequence=args.source_sequence, max_ai_clips=args.max_ai_clips, model=args.model,
                  offline=args.offline, force=args.force, drop_frame=args.drop_frame)
    except RefuseOverwrite as e:
        print(f"refusing to overwrite: {e}", file=sys.stderr)
        return 3
    except (TranscriptError, FileNotFoundError) as e:
        print(f"transcript error: {e}", file=sys.stderr)
        return 2
    except llm.LLMError as e:
        print(f"model error: {e}", file=sys.stderr)
        return 4
    if not args.quiet:
        counts = Counter(s["type"] for s in res.data["shots"])
        print(f"wrote {res.path}: {len(res.data['shots'])} shots "
              + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
