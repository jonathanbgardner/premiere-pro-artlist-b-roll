# B-roll Builder: Premiere Pro 2026 UXP panel (workstream C)

This panel reads `project_root/shot_list.json` and `project_root/assets/`. It saves a new copy of the open project, duplicates the source sequence, and places the B-roll and music on that duplicate. Then it writes `placed` statuses back to the shot list. It never edits the original `.prproj` or the original sequence.

What one build does:

1. **Save As** `<project>_BROLL_vN.prproj` next to the open project. N is the next unused number. Premiere's `saveAs` makes the copy the active project, so every later edit lands in the copy. The original file on disk is never written.
2. **Duplicate** the source sequence to `<source>_BROLL_vN`. The source sequence comes from `source_sequence` in the shot list, or else the active sequence.
   - If a `<source>_BROLL_vK` already exists, the newest one is cloned by default (`clone_from: "latest"`), so earlier placements carry over.
   - Neither the original sequence nor the older version is edited.
3. **Bins**: creates `Stock Video`, `Music` and `AI Generated`, or reuses them if they exist. Each asset is imported into its bin:
   - `ai_video`, `ai_audio`, and any shot listed in `ai_budget.generated` go to **AI Generated**.
   - `music` goes to **Music**.
   - `stock_video` goes to **Stock Video**.
4. **Video** (`stock_video`, `ai_video`):
   - Placed on **V2** at `start_tc` and trimmed to `duration_s`, rounded to whole frames.
   - **Muted.** A video-only subclip `<ID>_<frames>f` is used (`takeAudio: false`), so no audio is placed at all.
   - Fallback before 26.3: the clip's in/out points are set, its linked audio goes to a scratch track past A4, and that audio is **disabled**.
5. **Music** (`music`):
   - Placed on **A3** with fades: 1 s in and 2 s out by default.
   - **Ducked to -12 dB** with Volume > Level keyframes.
   - If the keyframes can't be written safely, a yellow `DUCK <ID>` marker reminds Jonathan to duck and fade it by hand.
6. **`ai_audio`**: placed on **A4** with fades and no duck. This choice is mine, because the spec doesn't say; see the suggestions below.
7. **Markers**: every placed clip gets a sequence marker spanning the clip, named `<ID> <type>`. Its comments hold the reason, confidence, status, file and Artlist title/URL.
   - Green by default.
   - **Red** if `confidence < 0.6` or `status == "failed"`.
   - A `failed` shot with no file gets a red `<ID> FAILED` marker only.
   - A missing file on disk gets a red `<ID> MISSING FILE` marker.
   - A clip that failed to place gets a red `<ID> PLACE FAILED` marker.
8. **Save** the copy.
9. **Only after a successful save**, the panel updates `shot_list.json`:
   - It re-reads the file and sets `status: "placed"` and `updated_by: "C"` on the shots that were actually placed.
   - It writes `shot_list.json.c-<stamp>.tmp`, then renames it over the original. All other fields are kept.

**What is skipped:**
- `status == "placed"` (so a re-run is safe).
- Shots with `file: null`.
- Files not on disk.
- A bad `start_tc`.

**When nothing is left to place**, the run is a no-op: no Save As and no new version.

## Layout

```
premiere/broll-builder/
  manifest.json        UXP manifest v5, host premierepro >= 26.0, localFileSystem fullAccess
  index.html, main.js  panel UI (pick project_root, summary, Dry run, Build, Run pending job, Watch)
  src/core/            PURE JS, no Premiere dependency (tested in Node 22)
    timecode.js        tc <-> frames <-> seconds <-> ticks (mirrors pipeline/common/shotlist.py exactly)
    plan.js            buildPlan(shotList, options) -> declarative operations
    audio.js           Volume Level value mapping (dB -> param value)
    job.js             job / result file shapes
    shotlist_io.js     status write-back helpers
    paths.js           POSIX + Windows path helpers
  src/premiere/        the thin layer that talks to require("premierepro") and require("fs")
    executor.js        applies plan.operations
    runner.js          read -> plan -> execute -> write statuses -> write result; job processing
    fsio.js            exists / readJson / atomic write
tests/premiere/        node:test suites + mock premierepro module
```

## Install in Premiere Pro 2026 (v26.x)

1. Install **UXP Developer Tools (UDT) 2.2 or later** from the Creative Cloud app (Apps > All apps > UXP Developer Tools). Enable Developer Mode when it asks, which needs admin rights once.
2. Start Premiere Pro 2026 and open the episode project. Save it at least once, because the plugin needs a project path.
3. In UDT, click **Add Plugin**. Select `premiere/broll-builder/manifest.json`, then click **Load** in the plugin's row (⋯ > Load). Use **Watch** to reload on edits and **Debug** to see console output.
4. In Premiere: **Window > UXP Plugins > B-roll Builder** (the menu location may vary by build; it is under the plugins/extensions menu).

To distribute without UDT, click **Package** in UDT to build a `.ccx` and double-click it to install.

The manifest asks for `localFileSystem: "fullAccess"`. The panel reads and writes plain native paths (`project_root/...`) with `require("fs")`, which treats paths without a scheme as `file:`.

## Use

1. Click **Pick project_root** and choose the folder that holds `shot_list.json`. The panel remembers it.
2. Check the summary table. Rows shown in red have low confidence or failed.
3. **Dry run** logs the full plan (each operation, timecode and track) without touching Premiere.
4. **Build B-roll sequence** runs the build. The log lists each placement, fallback and marker, and the result is also written to `logs/premiere_result.json`.

## Orchestrator hand-off (job file)

Premiere can't be driven reliably from a command line, so the orchestrator talks to the panel through files under `project_root`:

1. The orchestrator deletes any old `logs/premiere_result.json`.
2. It writes `scripts/broll_job.json`, writing a temp file first and then renaming it, so the panel never reads a half-written job.
3. Someone clicks **Run pending job**, or leaves **Watch for jobs** ticked: the panel checks for `scripts/broll_job.json` every 5 s while it is open.
4. The panel runs the build and writes `logs/premiere_result.json` (temp file + rename). It then renames the job to `scripts/broll_job.<job_id>.done.json`, or `.failed.json` on error, so the job never runs twice.
5. The orchestrator polls until `logs/premiere_result.json` exists with a matching `job_id`.
   - `status` `ok`, `partial` or `noop` means continue.
   - `error` means stop and show `error`.

By default the panel updates `shot_list.json` itself. The orchestrator can instead pass `"write_shot_list": false` and apply `placed` itself with `pipeline.common.shotlist.update_shot(..., status="placed", updated_by="C")` under its file lock. UXP can't take the Python `fcntl` lock.

### `scripts/broll_job.json`

```json
{
  "job_version": 1,
  "job_id": "2026-11-11T14-03-22Z-ep12",
  "action": "build_broll",
  "project_root": "/Volumes/Edit/ep12",
  "shot_list": "shot_list.json",
  "prproj": "/Volumes/Edit/ep12/ep12.prproj",
  "source_sequence": "ep12_main",
  "write_shot_list": true,
  "options": {
    "clone_from": "latest",
    "timecode_origin": "auto",
    "low_confidence": 0.6,
    "music_gain_db": -12,
    "ai_audio_gain_db": 0,
    "fade_in_s": 1.0,
    "fade_out_s": 2.0,
    "duck_mode": "auto",
    "mark_missing_files": true,
    "video_track_index": 1,
    "music_track_index": 2,
    "ai_audio_track_index": 3
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `job_version` | yes | Must be `1`. |
| `job_id` | yes | Echoed in the result. Use something unique per run. |
| `action` | no | Only `build_broll` is supported (the default). |
| `project_root` | no | Defaults to the folder the panel is watching. |
| `shot_list` | no | Path relative to `project_root`. Default `shot_list.json`. |
| `prproj` | no | If set and it isn't the active project, the panel opens it with `Project.open`. If omitted, the active project is used. |
| `source_sequence` | no | Overrides `source_sequence` in the shot list. Falls back to the active sequence. |
| `write_shot_list` | no | Default `true`. |
| `options.clone_from` | no | `latest` (default) clones the newest `<src>_BROLL_vK`. `original` always clones `<src>`. |
| `options.timecode_origin` | no | `auto` (default): `start_tc` counts from the sequence start, unless every TC is at or past a non-zero sequence start time (e.g. `01:00:00:00`); then the TCs are treated as display timecode and that start time is subtracted. `sequence_start` never subtracts. `zero_point` always subtracts. |
| `options.duck_mode` | no | `auto` (default) tries volume keyframes and adds a marker if that fails. `marker` adds the reminder marker only. |
| `options.*_track_index` | no | 0-based Premiere indices: V2 = 1, A3 = 2, A4 = 3. |

Unknown option keys are ignored.

### `logs/premiere_result.json`

```json
{
  "result_version": 1,
  "job_id": "2026-11-11T14-03-22Z-ep12",
  "status": "ok",
  "started_at": "2026-11-11T14:03:25.120Z",
  "finished_at": "2026-11-11T14:03:41.877Z",
  "project_path_in": "/Volumes/Edit/ep12/ep12.prproj",
  "project_path_out": "/Volumes/Edit/ep12/ep12_BROLL_v1.prproj",
  "sequence": "ep12_main_BROLL_v1",
  "cloned_from": "ep12_main",
  "placed": ["S001", "S002", "S003", "S005"],
  "failed": [{ "shot_id": "S007", "error": "import failed: /Volumes/Edit/ep12/assets/S007.mp4" }],
  "skipped": [{ "shot_id": "S004", "reason": "no file (status pending)" }],
  "fallbacks": [{ "shot_id": "S001", "what": "duck", "detail": "volume keyframes failed (...); reminder marker added" }],
  "markers_added": 6,
  "shot_list_updated": true,
  "warnings": [],
  "error": null,
  "plugin_version": "1.0.0"
}
```

`status` values:
- `ok`: everything planned was placed and saved.
- `partial`: saved, but some shots are in `failed`.
- `noop`: nothing to place.
- `error`: the run stopped. Nothing was marked placed, and `error` says why. If the stop came after Save As, a partial `_BROLL_vN.prproj` may exist; the original is still untouched.

`placed` lists only shots that are really on the timeline in the saved copy.

## Timecode rules (identical to `pipeline/common/shotlist.py`)

- `start_tc` is `HH:MM:SS:FF` at the shot list's `fps`. A `;` before the frames means drop-frame (29.97/59.94: drop 2 or 4 frames per minute, except every tenth minute). NDF at 29.97 counts frames at 30 nominal, and real time is frames / (30000/1001).
- The frame number is computed exactly as Python does, including Python's banker's rounding of `fps` to the nominal rate. Ticks are `frames × 254016000000 × den / num`, using exact BigInt arithmetic. That gives 8475667200 ticks per frame at 29.97, 10594584000 at 23.976, 4237833600 at 59.94 and 10160640000 at 25.
- `duration_s` is rounded to the nearest whole frame (4.0 s at 29.97 = 120 frames).
- `tests/premiere/timecode.test.mjs` runs about 5,800 cases through python3 (`tc_to_frames`, `tc_to_seconds`, `frames_to_tc`, `seconds_to_tc` at 23.976/24/25/29.97/30/50/59.94/60, NDF and DF, plus invalid TCs). It asserts exact equality, including bit-identical seconds.

## Premiere UXP API: verified vs assumed

I checked against Adobe's docs repo `AdobeDocs/uxp-premiere-pro` (as of 2026-11-04) and the typings in `@adobe/premierepro` 26.5.1 and 27.0.0. I also read the sample panel `AdobeDocs/uxp-premiere-pro-samples/sample-panels/premiere-api` (`sequenceEditor.ts`, `markers.ts`, `projectPanel.ts`, `keyframe.ts`).

**Verified** means the class, method and argument order are in the reference or typings, and where noted also used the same way in Adobe's samples. **Assumed** means the signature exists but the runtime behaviour isn't documented.

| Call | Status | Notes |
|---|---|---|
| `require("premierepro")` module with `Project`, `SequenceEditor`, `Markers`, `Marker`, `TickTime`, `FolderItem`, `ClipProjectItem`, `Constants` | verified | typings `premierepro` type, samples |
| `project.lockedAccess(cb)`, `project.executeTransaction(cb, undo) -> boolean`, `compoundAction.addAction(a)` | verified | 26.3: create*Action must be inside lockedAccess; no async in callbacks (changelog, eslint plugin) |
| `Project.getActiveProject()`, `Project.open(path)`, `project.path` | verified | |
| `project.saveAs(path)`: the copy becomes active and the handle points to it | verified | docs + 26.5 changelog; the executor re-reads the active project and refuses to continue if its path isn't the copy |
| `project.save()`, `getSequences()`, `getActiveSequence()`, `openSequence()`, `setActiveSequence()`, `getRootItem()` | verified | |
| `project.importFiles(paths, suppressUI, targetBin, asNumberedStills)` | verified signature | **assumed**: the item shows up in `targetBin` right away. A forum report says importFiles resolves before it's applied, so the executor polls. If the item lands in the root bin it moves it with `FolderItem.createMoveItemAction`. |
| `sequence.createCloneAction()` | verified signature | **assumed**: the clone shows up in `getSequences()` under an unknown name (likely "… Copy"). The executor finds it by GUID diff and renames it with `(await clone.getProjectItem()).createSetNameAction(name)`. |
| `sequence.getZeroPoint()`, `getVideoTrack(i)`, `getAudioTrack(i)`, `get*TrackCount()` | verified | |
| `FolderItem.cast`, `createBinAction(name, makeUnique)`, `getItems()`, `createMoveItemAction` | verified | samples use `createBinAction` and find the bin by name afterwards |
| `ClipProjectItem.cast`, `getMediaFilePath()`, `createSetInOutPointsAction(in, out)` | verified | in/out setters fixed in 26.5 |
| `ClipProjectItem.createSubClipAction(name, start, end, hardBoundaries, {takeVideo, takeAudio})` | verified (26.3+) | sample finds the subclip by name in the parent bin; the executor does the same |
| `SequenceEditor.getEditor(seq).createOverwriteItemAction(item, time, vIdx, aIdx)` | verified | sample uses (item, TickTime, 1, 1) for V2/A2. **Assumed**: an index past the last track creates a track; that is documented for insert only. |
| `TickTime.createWithTicks(string)`, `.ticks`, `.add()` | verified | |
| `track.getTrackItems(Constants.TrackItemType.CLIP, false)`, `trackItem.getStartTime/getEndTime/getInPoint` | verified | |
| `trackItem.createSetEndAction(t)` (trim to `duration_s`) | verified signature | used only if the out point isn't already right |
| `AudioClipTrackItem.createSetDisabledAction(true)` (mute linked audio) | verified signature | only on the fallback path |
| `Markers.getMarkers(seq)`, `markers.createAddMarkerAction(name, type, start, duration, comments)`, `markers.getMarkers()`, `Marker.MARKER_TYPE_COMMENT` | verified | sample |
| `marker.createSetColorByIndexAction(Constants.MarkerColor.RED)` | verified | sample uses `MarkerColor.BLUE`. If the constant is missing the executor falls back to indices 0 green, 1 red, 4 yellow (ExtendScript convention, **assumed**). |
| `trackItem.getComponentChain()`, `chain.getComponentCount/getComponentAtIndex`, `component.getDisplayName/getMatchName/getParamCount/getParam`, `param.displayName` | verified | |
| `param.createSetTimeVaryingAction(true)`, `param.createKeyframe(v)`, `kf.position = TickTime`, `param.createAddKeyframeAction(kf)`, `param.getValueAtTime(t)` | verified | keyframe sample |
| Volume component named "Volume" with a param named "Level" | **assumed** | matched by display/match name containing "volume" (not "channel") and param "Level" |
| Level units | **assumed** | not documented. The executor reads the default (0 dB) value: `0.1778` means linear (`10^((dB-15)/20)`), `0` means dB. Anything else means no keyframes and a reminder marker. |
| Keyframe times relative to the track item's in point | **assumed** | ExtendScript convention. With subclips the in point is the subclip start. |
| Audio fades as cross-fade transitions | not available | UXP only has `TransitionFactory.createVideoTransition` and no audio transitions, so fades are Level keyframes. |
| "Ducking" in the Essential Sound sense | not available | no UXP API. It is a static -12 dB bed plus fades; if keyframes fail, the `DUCK` marker is the fallback. |

The mock in `tests/premiere/helpers/mock_premierepro.mjs` follows the 26.5.1 typings. Like Premiere 26.3+, it throws if an Action is created outside `lockedAccess` or added outside `executeTransaction`.

## Tests

```
node --test tests/premiere/              # from the repo root (Node 22; python3 + jsonschema for the parity checks)
node --test tests/premiere/*.test.mjs    # same thing
```

- `timecode.test.mjs`: Python parity table; ticks and duration snapping.
- `plan.test.mjs`: the "Done when" check. The 5-shot fixture with fake files lands at the right TC, frames, ticks, track, bin and length, and each start matches Python's `tc_to_seconds` to within 1 tick. Also covers re-run, version bumping, failed/missing shots, DF/23.976/25, zero point and the duck options.
- `executor.test.mjs`: the executor against the mock module. Checks the call sequence (saveAs before any edit, save last, original sequence untouched), the resulting timeline, the subclip vs pre-26.3 fallback, the Level-unit fallback marker and import fallbacks.
- `runner.test.mjs`: the full job-file round trip on a temp `project_root`, with the real file system. The original `.prproj` stays untouched, and the updated `shot_list.json` still passes `pipeline.common.shotlist.load()` (schema).
- `core_misc.test.mjs`: paths, job parsing, write-back helpers and manifest sanity.

## Known limits

- The spec doesn't say where `ai_audio` goes, so it uses A4 with fades and no duck.
- The panel can't take the Python file lock. The orchestrator runs C last, but if B or D could still be writing, use `write_shot_list: false` (see above).
- JSON written by the panel prints `60.0` as `60`. The values are the same, and the schema and the Python loader accept it.
- Premiere must be open with the panel visible for job watching. UXP panels can't run headless.
