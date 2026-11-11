"use strict";
/**
 * Job file / result file shapes for the orchestrator hand-off. PURE.
 *
 *   orchestrator writes  <project_root>/scripts/broll_job.json
 *   panel writes         <project_root>/logs/premiere_result.json
 *   panel renames job to <project_root>/scripts/broll_job.<job_id>.done.json (or .failed.json)
 *
 * See premiere/broll-builder/README.md for the documented shapes.
 */

const JOB_VERSION = 1;
const RESULT_VERSION = 1;
const JOB_REL = "scripts/broll_job.json";
const RESULT_REL = "logs/premiere_result.json";
const PLUGIN_VERSION = "1.0.0";

class JobError extends Error {}

// snake_case job option -> camelCase plan option
const OPTION_MAP = {
  clone_from: "cloneFrom",
  timecode_origin: "timecodeOrigin",
  low_confidence: "lowConfidence",
  music_gain_db: "musicGainDb",
  ai_audio_gain_db: "aiAudioGainDb",
  fade_in_s: "fadeInS",
  fade_out_s: "fadeOutS",
  duck_mode: "duckMode",
  mark_missing_files: "markMissingFiles",
  video_track_index: "videoTrackIndex",
  music_track_index: "musicTrackIndex",
  ai_audio_track_index: "aiAudioTrackIndex",
};

const ENUMS = {
  cloneFrom: ["latest", "original"],
  timecodeOrigin: ["auto", "sequence_start", "zero_point"],
  duckMode: ["auto", "marker"],
};

function optionsFromJob(opts) {
  const out = {};
  for (const [k, v] of Object.entries(opts || {})) {
    const key = OPTION_MAP[k];
    if (!key) continue; // unknown options are ignored (forward compatible)
    if (ENUMS[key] && !ENUMS[key].includes(v)) throw new JobError(`options.${k} must be one of ${ENUMS[key].join(", ")}`);
    out[key] = v;
  }
  return out;
}

/**
 * Normalize a parsed broll_job.json.
 * @param {object} raw parsed JSON
 * @param {string} projectRootFallback the folder the job file was found under
 */
function parseJob(raw, projectRootFallback) {
  if (!raw || typeof raw !== "object") throw new JobError("job must be a JSON object");
  if (raw.job_version !== JOB_VERSION) throw new JobError(`unsupported job_version ${raw.job_version}`);
  if (typeof raw.job_id !== "string" || !raw.job_id) throw new JobError("job_id is required");
  const action = raw.action || "build_broll";
  if (action !== "build_broll") throw new JobError(`unsupported action ${action}`);
  return {
    jobId: raw.job_id,
    action,
    projectRoot: raw.project_root || projectRootFallback,
    shotListRel: raw.shot_list || "shot_list.json",
    prproj: raw.prproj || null,
    sourceSequence: raw.source_sequence || undefined,
    writeShotList: raw.write_shot_list !== false,
    planOptions: optionsFromJob(raw.options),
  };
}

function makeResult(fields) {
  return {
    result_version: RESULT_VERSION,
    job_id: fields.jobId || null,
    status: fields.status, // "ok" | "partial" | "noop" | "error"
    started_at: fields.startedAt,
    finished_at: fields.finishedAt,
    project_path_in: fields.projectPathIn || null,
    project_path_out: fields.projectPathOut || null,
    sequence: fields.sequence || null,
    cloned_from: fields.clonedFrom || null,
    placed: fields.placed || [],
    failed: fields.failed || [],
    skipped: fields.skipped || [],
    fallbacks: fields.fallbacks || [],
    markers_added: fields.markersAdded || 0,
    shot_list_updated: !!fields.shotListUpdated,
    warnings: fields.warnings || [],
    error: fields.error || null,
    plugin_version: PLUGIN_VERSION,
  };
}

module.exports = { JOB_VERSION, RESULT_VERSION, JOB_REL, RESULT_REL, PLUGIN_VERSION, JobError, parseJob, optionsFromJob, makeResult };
