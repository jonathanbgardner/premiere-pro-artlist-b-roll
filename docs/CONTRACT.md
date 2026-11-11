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
