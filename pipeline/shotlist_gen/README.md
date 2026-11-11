# Workstream A: shot list generator

Turns a Premiere Pro 2026 transcript export into `project_root/shot_list.json`
(contract v1, written through `pipeline.common.shotlist`).

```
python -m pipeline.shotlist_gen episode.srt --project-root ./ep12 --fps 29.97 \
    [--project NAME] [--source-sequence NAME] [--max-ai-clips 8] \
    [--model claude-opus-5-5] [--offline] [--force] [--drop-frame]
```

- Input: SRT/VTT caption export, or the Text panel's "Export to text" file
  (`00:01:12:10 - 00:01:15:02` / `Speaker 1` / text). Also accepted: the speaker
  on the time line, `[00:00:01:12] Name: text`, start-only times, `-->`/`-`/en dash
  separators, `;` drop-frame and `HH:MM:SS.mmm` times. Speaker labels in SRT can
  be `Name: text`, `[Name] text`, `<v Name>` or `>>` change markers.
- Model: `--model`, else `$BROLL_SHOTLIST_MODEL`, else `claude-opus-5-5`.
  It needs `pip install anthropic` and an `ANTHROPIC_API_KEY` (or an `ant auth login` profile).
  One structured-output request per transcript, with the server-side refusal fallback turned on.
- `--offline`: keyword heuristics, no API call. It is deterministic, so CI and tests use it.
- Re-runs: it refuses (exit 3) to overwrite a shot list that has any non-pending
  shot or anything in `ai_budget.approved`/`generated`, unless you pass `--force`.
- Side effects: it creates the project folders, copies the transcript into
  `transcript/`, and writes the rule decisions (drops, shifts, fillers) to
  `logs/shotlist_gen.log`.

Exit codes: 0 ok, 2 transcript error, 3 refused overwrite, 4 model error.

## Pipeline

1. `transcript.py`: parse the transcript into segments `{start_s, end_s, speaker, text}` in real seconds.
2. `beats.py`: split into beats (on speaker change, a pause over 1.5 s or about 30 s of length), find
   speaker turns, talking-head stretches (one speaker for more than 20 s) and the intro/main/outro sections.
3. Candidates: `llm.py` (Claude) or `heuristics.py` (offline). They cover concrete
   nouns, places, actions, stats and abstract ideas, with 2-3 queries from specific to broad.
   The type is `stock_video` by default; `ai_video` (with an `ai_prompt`) is only for
   abstract shots.
4. `rules.py` enforces every timing rule deterministically after step 3:
   - clips are clamped to 3-8 s;
   - nothing in the first 10 s after any speaker starts (the first speaker included): the clip
     is shrunk or shifted (at most 12 s from where it was asked for), or dropped;
   - each clip needs a gap of 2 s or more to the previous clip unless it has `"sequence": true`;
   - nothing runs past the transcript end;
   - `ai_video` is capped at `--max-ai-clips`, and extras become `stock_video` but keep their `ai_prompt`;
   - every uncovered stretch over 20 s gets a heuristic cutaway if a legal slot exists.

   Music beds span their section and are exempt from the 3-8 s, speaker and gap rules.
5. `to_shots`: IDs `S001..` in time order, `status: pending`, `file: null`,
   `confidence: 0.0`, `updated_by: "A"`.

Extra per-shot fields (allowed by the schema): `trigger` (why it was flagged) and
`sequence: true` (only on montage clips). Extra top-level fields: `generator`
(mode/model) and `transcript` (path relative to project_root).

`check.violations(data, segments)` re-checks a finished list from its `start_tc` values.
The tests use it, and the orchestrator can use it too.

## Tests

```
python -m pytest tests/shotlist_gen -q
UPDATE_EXPECTED=1 python -m pytest tests/shotlist_gen -q -k matches_fixture   # refresh expected_offline.json
```

Fixtures: `tests/fixtures/shotlist_gen/interview_4min.srt` (about 4 min, 2 speakers) and its
offline output `expected_offline.json`.
