# AI B-roll pipeline for Premiere Pro 2026 + Artlist

Turns a long-form edit into a `<sequence>_BROLL_v1` sequence in a new `.prproj`. It places Artlist stock video and music on the timeline, plus a small, human-approved number of AI-generated clips. Spec: `docs/BUILD_SPEC.md`. Shared contract: `docs/CONTRACT.md` and `schema/shot_list.schema.json`.

| Stage | Code | Docs |
|---|---|---|
| A: transcript → `shot_list.json` | `pipeline/shotlist_gen/` | its README |
| B: Artlist search and download (Claude computer use + Playwright) | `pipeline/artlist_download/` | its README |
| C: Premiere timeline builder (UXP plugin) | `premiere/broll-builder/` | its README |
| D: AI generation via the Artlist MCP (rationed, approval-gated) | `pipeline/ai_gen/` | its README |
| Orchestrator | `pipeline/orchestrator.py` | below |

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # A, B (and optional D prompt writing)
export ARTLIST_MCP_TOKEN=...        # D
python -m pipeline.artlist_download login --profile ~/.broll/artlist-profile   # log in once, by hand
```

In Premiere Pro 2026, load the plugin with UXP Developer Tools: Add Plugin → `premiere/broll-builder/manifest.json` → Load. Then open Window > UXP Plugins > B-roll Builder and tick **Watch for jobs**.

## Run

Export the transcript from Premiere's Text panel (SRT), then:

```bash
python -m pipeline.orchestrator --project-root ~/Edit/ep12 \
  --transcript ep12.srt --fps 29.97 --source-sequence ep12_main \
  --profile ~/.broll/artlist-profile --prproj ~/Edit/ep12/ep12.prproj
```

The run goes A → (B ∥ D plan) → D plan again (to pick up B's misses) → D generate → C. It prints a summary and lists any AI shots waiting for approval. To approve some:

```bash
python -m pipeline.ai_gen approve --project-root ~/Edit/ep12 S004 S011   # human only
python -m pipeline.orchestrator --project-root ~/Edit/ep12 --profile ~/.broll/artlist-profile
```

Re-running is safe: shots that are already downloaded, generated or placed are skipped. A second Premiere build produces `_BROLL_v2`, cloned from v1, so nothing is ever edited in place.

Useful flags:
- `--offline`: no Claude call in A, and template prompts in D.
- `--interactive`: B waits for you on a login page or captcha instead of stopping.
- `--stop-for-approval`: don't build the timeline until the AI shots are approved.
- `--skip-premiere`: just write the job file.
- `--b-arg=--max-shots --b-arg=1`: pass options through to B; this one is for the first real B run.

## Tests

```bash
python -m pytest tests -q        # A, B, D, orchestrator
node --test tests/premiere/      # C
```

## Before the first real run

1. Do a short (5-minute) video first, as the spec says.
2. **B:** one shot, headed (`--b-arg=--max-shots --b-arg=1`, no `--headless`). Check where Artlist downloads come from; add the host with `--b-arg=--allow-domain --b-arg=HOST` if it's blocked.
3. **D:** run `python -m pipeline.ai_gen tools` and fix the tool and argument mapping in `pipeline/ai_gen/artlist_mcp.toml` (marked VERIFY). Then run `generate --dry-run` and approve one cheap shot.
4. **C:** check the APIs listed as "assumed" in `premiere/broll-builder/README.md`. The ones to watch are music volume and keyframe units, the name the duplicated sequence gets, and import timing.
5. **Open questions:** whether your Artlist plan's download limits and licence allow automated downloads, and your AI credit budget per video.
