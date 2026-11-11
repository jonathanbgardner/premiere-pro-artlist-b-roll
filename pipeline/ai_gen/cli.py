"""python -m pipeline.ai_gen {plan,approve,generate,tools} --project-root DIR [...]"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from pipeline.common.shotlist import ShotListError

from .budget import BudgetError, approve
from .config import ConfigError, load_config
from .mcp_client import ArtlistMCP, MCPCallError
from .prompts import DEFAULT_MODEL


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pipeline.ai_gen",
                                description="Workstream D: rationed AI B-roll generation via the Artlist MCP.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, root=True):
        if root:
            sp.add_argument("--project-root", required=True, help="project folder containing shot_list.json")
        sp.add_argument("--config", help="override TOML merged over pipeline/ai_gen/artlist_mcp.toml "
                                         "(or set ARTLIST_MCP_CONFIG)")

    sp = sub.add_parser("plan", help="phase 1: build logs/ai_generation_list.json and STOP (never generates)")
    common(sp)
    sp.add_argument("--no-llm", action="store_true", help="deterministic template prompts, no Claude call")
    sp.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model for prompt writing (default {DEFAULT_MODEL})")
    sp.add_argument("--json", action="store_true", help="print the list as JSON instead of a table")

    sp = sub.add_parser("approve", help="HUMAN ONLY: approve shot IDs for generation (orchestrator must never call)")
    sp.add_argument("--project-root", required=True)
    sp.add_argument("ids", nargs="*", metavar="SHOT_ID")
    sp.add_argument("--all-listed", action="store_true", help="approve every item in logs/ai_generation_list.json")

    sp = sub.add_parser("generate", help="phase 2: generate approved shots (hard-capped at ai_budget.max_clips)")
    common(sp)
    sp.add_argument("--dry-run", action="store_true", help="show what would be generated; no MCP connection")
    sp.add_argument("--limit", type=int, help="generate at most N new shots this run")

    sp = sub.add_parser("tools", help="connect to the Artlist MCP and list its real tools")
    common(sp, root=False)
    sp.add_argument("--json", action="store_true")
    return p


async def _list_tools(cfg: dict) -> list[dict]:
    async with ArtlistMCP(cfg) as mcp:
        return await mcp.list_tools()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "approve":
            new = approve(args.project_root, args.ids, all_listed=args.all_listed)
            print(f"approved: {', '.join(new) if new else '(nothing new)'}")
            return 0

        cfg = load_config(args.config)

        if args.cmd == "plan":
            from .plan import format_table, plan
            result = plan(args.project_root, cfg, use_llm=not args.no_llm, model=args.model)
            print(json.dumps(result, indent=2) if args.json else format_table(result))
            return 0

        if args.cmd == "generate":
            from .generate import generate
            report = generate(args.project_root, cfg, dry_run=args.dry_run, limit=args.limit)
            print(json.dumps(report, indent=2))
            failed = [k for k, v in report["results"].items() if v == "failed"]
            return 1 if failed else 0

        if args.cmd == "tools":
            mcp_desc = ArtlistMCP(cfg).describe()
            tools = asyncio.run(_list_tools(cfg))
            if args.json:
                print(json.dumps(tools, indent=2, default=str))
            else:
                print(f"{len(tools)} tools on {mcp_desc}\n")
                for t in tools:
                    print(f"- {t['name']}: {t['description'].strip().splitlines()[0] if t['description'] else ''}")
                    schema = t.get("input_schema") or {}
                    req = set(schema.get("required", []))
                    for name, prop in (schema.get("properties") or {}).items():
                        typ = prop.get("type", "") if isinstance(prop, dict) else ""
                        enum = f" one of {prop['enum']}" if isinstance(prop, dict) and prop.get("enum") else ""
                        print(f"    {name}{'*' if name in req else ''}: {typ}{enum}")
                print("\nFill [tools.video]/[tools.audio] in artlist_mcp.toml from this list (* = required).")
            return 0
    except (BudgetError, ConfigError, ShotListError, MCPCallError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 2
