# AI B-Roll Pipeline: Build Spec

Goal: take an existing long-form video in Premiere Pro and get back a finished project file with Artlist stock video and music placed on the timeline, plus a small number of AI-generated clips where stock doesn't fit. Jonathan reviews; the agents do the work.

## Shared contract: shot\_list.json

Every workstream reads or writes this file. Lock it first so the workstreams can be built in parallel.

```json
{
  "project": "episode_name",
  "fps": 29.97,
  "shots": [
    {
      "id": "S001",
      "start_tc": "00:01:12:10",
      "duration_s": 4.0,
      "type": "stock_video | music | ai_video | ai_audio",
      "reason": "Speaker mentions feeding cattle at dawn",
      "queries": ["cattle feeding sunrise", "farm morning herd"],
      "status": "pending | downloaded | placed | needs_ai | failed",
      "file": "assets/S001.mp4",
      "confidence": 0.0
    }
  ],
  "ai_budget": { "max_clips": 8, "approved": [] }
}
```

Folder layout: `project_root/transcript/`, `project_root/assets/` (one file per shot ID), `project_root/scripts/`, `project_root/logs/`.

## Workstream A: Shot list generator

Input: transcript exported from Premiere (SRT or text with timestamps).

- Split the transcript into beats. Flag moments that need B-roll: concrete nouns, places, actions, stats, long talking-head stretches over about 20 seconds.
- For each, write 2 or 3 Artlist search queries, from specific to broad.
- Mark music beds per section (intro, main body, outro) with mood and tempo queries.
- Default type is stock\_video. Use ai\_video only for abstract or very specific shots stock can't cover.
- Rules: minimum 3 s, maximum 8 s per clip; don't cover the first 10 s of a new speaker; no two B-roll clips back to back without a reason.
- Output: shot\_list.json.

Done when: a test transcript produces a valid shot list you agree with on a quick read.

## Workstream B: Search and download agent (computer use)

Input: shot\_list.json. Uses Claude computer use driving a logged-in browser on artlist.io (more reliable than the Premiere plugin panel).

- For each pending stock\_video or music shot: run query 1, review the top results, and pick the best match by relevance, resolution (1080p or higher), and minimum length (duration\_s plus 2 s of handles).
- If nothing fits, try queries 2 and 3. Still nothing: set status to needs\_ai.
- Download, rename the file to the shot ID, and move it to assets/. Update status and file, and give a confidence score from 0 to 1.
- Log every pick with the clip's Artlist title or URL to logs/picks.csv for licensing records.
- Safety: stay on artlist.io, never change account or billing settings, pause on any login or captcha and ask.

Done when: it fills 10 test shots on its own with no more than 2 bad picks.

## Workstream C: Premiere timeline builder (scripting)

Input: shot\_list.json plus assets/. Built as a UXP plugin (newer Premiere) or an ExtendScript .jsx (wider support); pick based on your Premiere version.

- Duplicate the original sequence to `[name]_BROLL_v1`. Never touch the original.
- Import assets/ into bins: Stock Video, Music, AI Generated.
- Place stock\_video and ai\_video on V2 at start\_tc, trimmed to duration\_s and muted.
- Place music on A3 with fades in and out, ducked about 12 dB under dialogue if possible; otherwise add a marker reminding you to duck it.
- Add a marker on every placed clip with its shot ID, reason and confidence. Red marker if confidence is under 0.6 or status is failed.
- Save as a new .prproj.

Done when: running it on a hand-made 5-shot list puts everything at the right timecodes.

## Workstream D: AI generation (rationed)

Input: shots with status needs\_ai or type ai\_video or ai\_audio.

- Build a generation list with prompt, duration and estimated credits, then stop and wait for Jonathan's approval.
- Generate only the approved items, through the Artlist MCP, and never more than ai\_budget.max\_clips.
- Save to assets/ as the shot ID and set status to downloaded.

Done when: it respects the cap and never generates without approval.

## Orchestrator

One command runs A, then B and the D approval request in parallel, then D generation, then C. It can be re-run safely: it skips anything already placed.

## Build order for parallel work

1. Day 0: lock the shot\_list.json schema and the folder layout.
2. In parallel: A with real transcripts; B with a hand-written shot list; C with dummy clips; D with fake needs\_ai shots.
3. Integrate with the orchestrator, then do one full run on a short 5-minute video before trying a long one.

## Open questions

- Premiere version, to choose between UXP and ExtendScript.
- Artlist plan download limits and licensing terms for automated downloads.
- AI credit budget per video.
