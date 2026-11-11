# Workstream B: Artlist search and download (Claude computer use)

Fills `stock_video` and `music` shots in `shot_list.json` by letting Claude drive a
logged-in Chromium tab on artlist.io: search, review results, download the best
clip, rename it to the shot ID, and record the pick for licensing.

## One-time setup

```bash
pip install -r requirements.txt          # anthropic, playwright, jsonschema, pytest
export ANTHROPIC_API_KEY=...             # or `ant auth login`
```

Chromium: Playwright normally uses its own browser build (`playwright install chromium`).
If that build is missing, the backend falls back to `/opt/pw-browsers/chromium*`, or set
`BROLL_CHROMIUM_PATH=/path/to/chrome`.

### Log in once (Jonathan)

```bash
python -m pipeline.artlist_download login --profile ~/.broll/artlist-profile
```

This opens a headed browser that uses a **persistent profile directory**. Log in to Artlist
there (do the captcha / 2FA yourself), then press Enter in the terminal. Every later `run`
with the same `--profile` is already logged in. Treat the profile dir like a password: it
holds the session cookies. Close any other Chromium that uses that profile first.

## Run

```bash
python -m pipeline.artlist_download run --project-root ~/episodes/ep12 \
    --profile ~/.broll/artlist-profile [--only S001,S002] [--max-shots N] \
    [--non-interactive] [--headless] [--model claude-opus-5-5] [--dry-run]
```

Other flags: `--effort low|medium|high|xhigh|max` (default `medium`), `--retry-failed`
(also retry shots B marked `failed`, e.g. after a `needs_human`), `--allow-domain HOST`
(repeatable), `--start-url`, `--max-steps-per-shot`, `--max-total-steps`,
`--max-total-cost-usd`, `--shot-timeout-s`, `--no-fallbacks`, `--config settings.json`
(any field of `Settings` in `config.py`).

Start with `--dry-run`, then `--max-shots 1` headed and watch it.

What happens per shot (`status: pending`, type `stock_video` or `music`):

1. The tab opens `start_url` (or a search URL template if configured). If a login page or
   captcha is detected, the run pauses (see below).
2. Claude gets the shot: reason, queries, required minimum length
   (`duration_s + 2 s` handles), >= 1080p MP4/MOV for video, WAV/MP3 for music. It runs
   query 1, reviews results, falls back to queries 2 and 3.
3. It clicks Download, then calls `report_pick(title, url, resolution, duration_s,
   confidence, query)`. The harness **checks in code**: length >= required, video
   resolution >= 1080p, Artlist URL, a download was actually captured, file extension
   allowed. Otherwise the pick is rejected and Claude keeps looking.
4. The Playwright download is saved to `assets/<shot_id><ext>` (real extension kept,
   lower-cased: `.mp4`/`.mov`, `.wav`/`.mp3`), a row is appended to `logs/picks.csv`
   (`timestamp,shot_id,type,query,title,url,resolution,duration_s,confidence,file`), and
   the shot is updated via `pipeline.common.shotlist.update_shot`: `status: downloaded`,
   `file`, `confidence`, `source_title`, `source_url`, `updated_by: "B"`.
5. If nothing fits, Claude calls `report_no_match` (accepted only once every query was
   tried) -> `status: needs_ai`, reason in `b_no_match_reason`. Errors -> `status: failed`
   with `error`.

Per-shot records also go to `logs/artlist_download_runs.jsonl` (steps, seconds, running cost).

### Re-runs / resume

Only `pending` shots are touched (plus `failed` with `--retry-failed`). If
`assets/<id>.<ext>` already exists for a pending shot (e.g. the run was interrupted after
the download), it is marked `downloaded` without re-downloading, restoring title/url/
confidence from its `picks.csv` row when present (the CSV row is written before the shot
list, for exactly this case).

## Safety guardrails (enforced in code)

- **Domain allowlist** (`allowed_domains`, default `["artlist.io"]`, suffix match so
  `www.`/CDN/auth subdomains are covered). A Playwright route on the whole context aborts
  every navigation or download to any other host; off-site sub-resources (fonts,
  analytics) are allowed so the site renders, or blocked too with
  `"strict_subresources": true`. Popups are closed. The `navigate` tool refuses
  disallowed URLs, and after every batch of actions the current URL is re-checked; if it
  is not allowed, the harness goes back. More than `max_guardrail_violations` (3) in one
  shot fails the shot.
  If Artlist serves downloads from a non-artlist.io CDN, the log shows
  `[guardrail] blocked navigation: <url>`; add that host with `--allow-domain` after
  checking it.
- **Account/billing never**: paths matching `/account`, `/billing`, `/settings`,
  `/subscription`, `/checkout`, `/payment`, `/pricing`, `/plans`, `/upgrade`, `/invoice`,
  `/cart`, `/logout`, ... are blocked (`blocked_path_patterns`).
- **Login / captcha pause**: URL patterns (`/login`, `/sign-in`, `/auth`, `captcha`,
  `/challenge`, ...), a visible password field, captcha iframes (reCAPTCHA, hCaptcha,
  Turnstile, Arkose), "Just a moment..." titles, and the `request_human` tool all trigger
  a pause. Interactive: the terminal prints the reason and waits for Enter while you fix it
  in the browser window. `--non-interactive`: the shot is marked `failed` with
  `error: "needs_human"` and the run stops (remaining shots stay `pending`); log in again
  and re-run with `--retry-failed`.
- **Limits**: `max_steps_per_shot` (40 model calls), `shot_timeout_s` (420 s),
  `max_total_steps` (400) and `max_total_cost_usd` (15, estimated from token usage). When
  a run budget is hit, the current shot stays `pending` and the run stops.

## Model / API notes

- Default model `claude-opus-5-5` (`--model` to override). Computer use goes through the
  `computer_toolset_20260801` toolset (GA, no beta header; Opus 5.5 rejects the older
  `computer_20251124` tool on the Claude API). `zoom` is disabled; all other members are
  implemented against the Playwright page. Screenshots are the 1280x800 viewport.
- Thinking is adaptive (always on for Opus 5.5); depth via `output_config.effort`.
- The server-side refusal fallback (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`) is on by default; turn off with `--no-fallbacks`.
- Each shot is a fresh conversation; the system prompt and tools are identical across
  shots so they cache.

## Tests

```bash
python -m pytest tests/artlist_download -q
```

No network: a scripted fake LLM plus a fake browser cover the pick/CSV/status flow,
pick validation, `needs_ai` fallback, guardrails (off-site + billing navigation, URL
check after actions, captcha pause interactive/non-interactive), limits and resume. One
test runs the real Playwright backend headless against a local mock "stock site" with a
download link; one drives the real Anthropic SDK against a mock HTTP transport to check
the wire format.

## Open questions for Jonathan

- **Artlist plan download limits and licensing for automated downloads.** We have not
  checked whether Artlist's terms allow scripted/agent-driven downloading, or whether the
  plan has daily/monthly download caps. Please confirm before running this on a long
  episode; `--max-shots` keeps a run small meanwhile.
- Which Artlist hosts serve the actual files (for `allowed_domains`), and the real search
  URL format (optional `video_search_url` / `music_search_url` templates with `{query}`)
  - both need one supervised run to confirm; we built against local fakes only.
