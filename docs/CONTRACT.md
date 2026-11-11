# Day 0 contract (locked)

Change this only by agreement across all workstreams.

## Files

- Schema: `schema/shot_list.schema.json` (JSON Schema 2020-12, `schema_version: 1`).
- Python I/O + timecode helpers: `pipeline/common/shotlist.py`. All Python code reads and writes the shot list through it (`load`, `save`, `locked`, `update_shot`).
- Example: `tests/fixtures/shot_list_5.json` (the hand-made 5-shot list).

## Project folder layout

```
project_root/
  shot_list.json
  transcript/   SRT or timestamped text exported from Premiere
  assets/       one file per shot ID: S001.mp4, S002.wav, ...
  scripts/      generated per-project scripts (e.g. Premiere run config)
  logs/         picks.csv (licensing), run logs, ai_generation_list.json
```

## Repo layout (one directory per workstream, so they build in parallel)

| Path | Owner | Language |
|---|---|---|
| `pipeline/common/` | Day 0 (shared) | Python |
| `pipeline/shotlist_gen/` | A: shot list generator | Python |
| `pipeline/artlist_download/` | B: search and download (computer use) | Python |
| `premiere/broll-builder/` | C: Premiere Pro 2026 UXP plugin | JavaScript |
| `pipeline/ai_gen/` | D: AI generation (rationed) | Python |
| `pipeline/orchestrator.py` | Integration | Python |
| `tests/<workstream>/` | each owner | pytest / node:test |

## Semantics

- `start_tc` is sequence time, `HH:MM:SS:FF` at `fps`. `;` before frames means drop-frame. NDF at 29.97 means frames are counted at 30 nominal, real time = frames / (30000/1001).
- `file` is relative to project_root, `null` until something is on disk.
- Status flow: `pending` -> (B) `downloaded` | `needs_ai` | `failed`; `needs_ai` / `ai_*` -> (D, only if in `ai_budget.approved`) `downloaded`; `downloaded` -> (C) `placed`.
- `ai_budget.approved` is written by a human only. D appends to `ai_budget.generated` and never lets `len(generated)` exceed `max_clips`.
- Every writer sets `updated_by` on shots it touches.
- `logs/picks.csv` columns: `timestamp,shot_id,type,query,title,url,resolution,duration_s,confidence,file`.

## Integration addenda (after the four workstreams reported)

Additive only; `schema_version` stays 1 and every existing list stays valid.

- **Timecode origin.** `start_tc` counts from the source sequence's first frame (`00:00:00:00`), which is what A writes. C's job option `timecode_origin: "auto"` also accepts display timecode when the sequence starts at e.g. `01:00:00:00` (it subtracts the start only if every TC is past it). `duration_s` rounds to the nearest whole frame on the timeline. If `drop_frame` and the `;` separator disagree, the separator wins.
- **File extensions.** `shot.file` is authoritative. B keeps the real extension (`.mp4`/`.mov`, `.wav`/`.mp3`), and D may return `.mp3`. `ASSET_EXT` is only the default for new names.
- **`ai_budget.generated`** means "slots used", including failed attempts, because Artlist charges at request time. Only a human removes an ID to allow a retry.
- **Extra shot fields** (now in the schema): A writes `trigger` and `sequence`; B writes `b_no_match_reason`; D writes `ai_approval_hash`, `ai_job_id`, `ai_asset_url`, `ai_tool`, `ai_reserved_at`.
- **`ai_audio`** is placed on A4 with fades and no duck. It is currently disabled in D's config, because the Artlist MCP doesn't offer music generation.
- **Premiere hand-off files.** The orchestrator writes `scripts/broll_job.json` with `write_shot_list: false`. The panel writes `logs/premiere_result.json`, and the orchestrator then sets `placed` under the Python file lock, because UXP can't take `fcntl` locks. The formats are in `premiere/broll-builder/README.md`.
- **Orchestrator logs.** `logs/orchestrator.log` and `logs/orchestrator_last_run.json`.
