# Workstream D: rationed AI generation (Artlist MCP)

Takes shots that stock can't cover and generates them through the Artlist MCP server,
**only after Jonathan approves them**, and **never more than `ai_budget.max_clips`**.

```
plan  ->  (human) approve  ->  generate  ->  C places assets/<id>.mp4
```

## Commands

```bash
# Phase 1: build logs/ai_generation_list.json, print an approval table, STOP.
python -m pipeline.ai_gen plan --project-root DIR [--no-llm] [--model claude-opus-5-5] [--json]

# Human only: approve. The orchestrator must NEVER call this.
python -m pipeline.ai_gen approve --project-root DIR S004 S007
python -m pipeline.ai_gen approve --project-root DIR --all-listed

# Phase 2: generate approved shots (hard-capped).
python -m pipeline.ai_gen generate --project-root DIR [--dry-run] [--limit N]

# Connect to the Artlist MCP and list its real tools + argument schemas.
ARTLIST_MCP_TOKEN=... python -m pipeline.ai_gen tools [--json]
```

All commands take `--config FILE` (or `$ARTLIST_MCP_CONFIG`): a TOML merged over
`pipeline/ai_gen/artlist_mcp.toml`, so an override only lists the keys it changes.

Exit codes: 0 ok; 1 `generate` had a failed shot; 2 refused or bad input (e.g. approve over budget).

### plan

Selects shots with `status == needs_ai` (any type), or `type in (ai_video, ai_audio)` with
`status == pending`, excluding IDs already in `ai_budget.generated`.

- Prompt: `shot.ai_prompt` if A (or a human) already wrote one; otherwise Claude
  (`claude-opus-5-5`, low effort, server-side refusal fallback on) writes one; any Claude
  error falls back to a deterministic template. `--no-llm` uses only the template.
- `est_credits` = credits/second for the configured model and resolution × the generation
  duration (shot duration rounded **up** to an allowed model length).
- Writes `ai_prompt` and `est_credits` back onto each shot via `update_shot` (`updated_by: "D"`).
- Writes `logs/ai_generation_list.json`: `items: [{id, type, prompt, duration_s, est_credits, reason, ...}]`,
  `totals`, and `budget.remaining = max_clips - len(generated)`. Items past the remaining
  budget are flagged `within_budget: false` (`OVER` in the table).
- It never connects to the MCP server. Tests check this.

### approve (human command)

Appends IDs to `ai_budget.approved` and stamps each shot with `ai_approval_hash`
(a hash of type + prompt + duration, `updated_by: "human"`). It is all or nothing, and it refuses:
- if `(approved but not yet generated) + new > max_clips - len(generated)`;
- shots that are not eligible, unknown, already generated, or have no `ai_prompt` (run `plan` first, so you approve a real prompt).

Editing `ai_budget.approved` by hand is still allowed (CONTRACT: a human writes it). `generate`
accepts hand-approved shots that have no hash, unless `[generate] require_approval_hash = true`.

### generate

Generates only shots that are (a) in `approved`, (b) not in `generated`, (c) still eligible,
(d) not changed since approval (if the hash is present, it must match the current prompt,
duration and type; otherwise the shot is skipped with "re-approve"), and (e) of a tool family enabled in config.
For each one: reserve a slot, call the submit tool, poll the status tool, download the result to
`assets/<id>.mp4` (or `.wav`/`.mp3` from the asset URL), and set `status: downloaded`, `file`,
`confidence` (0.7 by default), `ai_job_id`, `ai_asset_url`, `ai_tool`, `updated_by: "D"`. It appends a
`logs/picks.csv` row with the CONTRACT columns: `title = "AI generated: <tool>/<model>"`,
`url = <asset url> (job <id>)`, `query = prompt`. Every step is logged to `logs/ai_generation_ledger.jsonl`.

## Budget safety (how the cap is enforced)

- `len(ai_budget.generated)` is the single counter, and it never exceeds `max_clips`.
- **Reserve before calling.** Under the `shot_list.json` lock (`pipeline.common.shotlist.locked`),
  `generate` reloads the file, re-checks approval, eligibility, the hash and `len(generated) < max_clips`,
  and appends the ID to `generated`. Only then does it send the MCP request. Two `generate` processes
  (or the orchestrator plus a human) can't overshoot or generate the same shot twice. This is tested
  with 2 and 3 concurrent processes against a real stdio MCP server.
- **When the reservation is released and when it is kept.** Artlist deducts credits when the
  request is made, and a generation can't be cancelled. So:
  - The reservation is *released* only when nothing was sent: an argument-template or config error before the submit call.
  - The reservation is *kept* when the submit call errored, the job failed, or the download failed.
    The shot gets `status: failed` and an `error`, and it is never retried automatically. To retry, a human removes the ID from `ai_budget.generated`
    (and resets `status`). This is deliberate: it is better to under-use the budget than to pay twice.
  - If the job was submitted but is still running (poll timeout or crash), the slot is kept along with `ai_job_id`.
    The next `generate` run resumes polling that job and does not submit it again.
  - If the process crashes between the reservation and the submit, the slot is held with no job ID. A human frees it as above.
- `--dry-run` doesn't connect to the server and doesn't reserve anything.
- Jobs run sequentially within one process, which keeps spending predictable and stays far below Artlist's 120 requests/min limit.

## Artlist MCP: what's known vs. what to verify

What's public (Artlist help center, blog and artlist.io/mcp; read 2026-11):

| Item | Status |
|---|---|
| Endpoint `https://mcp.artlist.io/mcp`, remote MCP (streamable HTTP) | documented |
| Auth: sign in with your Artlist account (OAuth via Claude/ChatGPT/VS Code connectors) | documented; **whether a static bearer token works from a script is not documented** |
| Supports text-to-image and text-to-video (voiceover mentioned in one article) | documented (beta) |
| Music generation, catalog search and video-to-video are NOT available via MCP | documented |
| Credit-based only; Unlimited plans don't apply; same pool as the web AI Toolkit | documented |
| Credits deducted at request time; can't cancel a started generation | documented |
| 120 requests/min per account | documented |
| Async: a generation ID is returned, then progress is checked separately | documented (loosely) |
| Tool names, argument names, response fields | **unknown: VERIFY with `tools`** |
| Credits per model and second (Veo 3.1 300/500, Veo 3.1 Fast 175/280, Kling 2.1 Master 300, Seedance 1.5 Pro 40-200) | from the web AI Toolkit pricing article, **not confirmed for MCP** |

All guesses are in `artlist_mcp.toml` and marked `VERIFY`. To fill them in:

1. Get an access token for `mcp.artlist.io` and export it as `ARTLIST_MCP_TOKEN`. It is only placed in the
   HTTP header. It is never printed, and it is scrubbed from any error text. If Artlist only supports interactive OAuth,
   the `mcp` SDK has an OAuth client provider (`mcp.client.auth`) that can be wired into `mcp_client.ArtlistMCP`.
   Alternatively, run a local bridge and use `transport = "stdio"`.
2. Run `python -m pipeline.ai_gen tools` and copy the real names into `[tools.video]`: `submit_tool`, `status_tool`,
   `submit_args` and `status_args` (templates: `"{prompt}"`, `"{duration_s}"`, `"{model}"`, `"{resolution}"`,
   `"{job_id}"`, `"{shot_id}"`), plus `model`, `resolution` and `duration_choices`.
3. Check the `[response]` key lists against one real result. Results are parsed from structured content
   or from JSON in text content, and keys are searched recursively.
4. Set `[credits]` from your plan, then run `generate --dry-run`, and approve one cheap shot first.

`ai_audio` is planned and shown to Jonathan, but `[tools.audio] enabled = false`, so it is never sent.
Artlist says music generation isn't available over MCP. Approved audio shots are skipped without using a slot.

## Files

| File | Purpose |
|---|---|
| `cli.py`, `__main__.py` | argparse CLI |
| `plan.py` | phase 1 + approval table |
| `budget.py` | eligibility, approval hash, `approve` |
| `generate.py` | phase 2: reserve, submit, poll, download, record |
| `mcp_client.py` | `mcp.Client` wrapper (streamable HTTP / stdio / in-process), token handling |
| `prompts.py` | Claude prompt writer + template fallback |
| `config.py`, `artlist_mcp.toml` | mapping, credits, polling config |

Tests: `python -m pytest tests/ai_gen -q`. They use a pure-Python fake backend and a real MCP server built with
the `mcp` SDK (`tests/ai_gen/fake_artlist_server.py`), run in-process and over stdio.
