"use strict";
/**
 * buildPlan(shotList, options) -> declarative plan. PURE: no Premiere/UXP dependency.
 *
 * The executor (src/premiere/executor.js) walks `plan.operations` in order:
 *   saveProjectAs -> duplicateSequence -> createBin x3 -> importFile* ->
 *   (placeClip, addMarker)* / addMarker* -> saveProject
 *
 * saveProjectAs comes FIRST on purpose: Project.saveAs() makes the copy the
 * active project, so every later edit lands in <project>_BROLL_vN.prproj and
 * the original .prproj on disk is never written.
 */

const tc = require("./timecode.js");
const paths = require("./paths.js");

const PLAN_VERSION = 1;

const BINS = Object.freeze({ stock: "Stock Video", music: "Music", ai: "AI Generated" });

const DEFAULTS = Object.freeze({
  cloneFrom: "latest", // "latest": clone the newest <src>_BROLL_vN if any (keeps earlier placements); "original": clone <src>
  timecodeOrigin: "auto", // "auto" | "sequence_start" | "zero_point"
  zeroPointTicks: "0",
  lowConfidence: 0.6,
  musicGainDb: -12,
  aiAudioGainDb: 0,
  fadeInS: 1.0,
  fadeOutS: 2.0,
  duckMode: "auto", // "auto": try volume keyframes, fall back to a reminder marker; "marker": reminder marker only
  markMissingFiles: true,
  // 0-based track indices (Premiere UXP indices): V2 = 1, A3 = 2, A4 = 3
  videoTrackIndex: 1,
  musicTrackIndex: 2,
  aiAudioTrackIndex: 3,
});

const MARKER_COLORS = Object.freeze({ ok: "GREEN", low: "RED", reminder: "YELLOW" });

class PlanError extends Error {}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const BROLL_SUFFIX_RE = /_BROLL_v(\d+)$/;

function stripBrollSuffix(name) {
  return name.replace(BROLL_SUFFIX_RE, "");
}

/** Versions N already used for `${base}_BROLL_vN` among names (exact match, case-sensitive). */
function brollVersions(base, names, ext = "") {
  const re = new RegExp("^" + escapeRe(base) + "_BROLL_v(\\d+)" + escapeRe(ext) + "$", ext ? "i" : "");
  const out = [];
  for (const n of names || []) {
    const m = re.exec(n);
    if (m) out.push(parseInt(m[1], 10));
  }
  return out.sort((a, b) => a - b);
}

/**
 * Sequence naming. Returns { base, target, cloneFrom, version }.
 * Never returns a target equal to an existing name.
 */
function planSequenceNames(sourceName, existingNames, cloneFrom = "latest") {
  const base = stripBrollSuffix(sourceName);
  const versions = brollVersions(base, existingNames);
  const latest = versions.length ? versions[versions.length - 1] : 0;
  let version = latest + 1;
  const taken = new Set(existingNames || []);
  while (taken.has(`${base}_BROLL_v${version}`)) version += 1;
  const from = cloneFrom === "latest" && latest > 0 ? `${base}_BROLL_v${latest}` : base;
  return { base, target: `${base}_BROLL_v${version}`, cloneFrom: from, version };
}

/** Project file naming: <stem>_BROLL_vN.prproj next to the current project. */
function planProjectPath(projectPath, existingFiles) {
  const dir = paths.dirname(projectPath);
  const stem = stripBrollSuffix(paths.stripExt(paths.basename(projectPath)));
  const versions = brollVersions(stem, existingFiles, ".prproj");
  let version = (versions.length ? versions[versions.length - 1] : 0) + 1;
  const lower = new Set((existingFiles || []).map((f) => f.toLowerCase()));
  while (lower.has(`${stem}_BROLL_v${version}.prproj`.toLowerCase())) version += 1;
  const out = paths.join(dir, `${stem}_BROLL_v${version}.prproj`);
  if (paths.samePath(out, projectPath)) throw new PlanError("refusing to overwrite the original project");
  return { path: out, version };
}

function binFor(shot, generated) {
  if (shot.type === "ai_video" || shot.type === "ai_audio" || generated.has(shot.id)) return BINS.ai;
  if (shot.type === "music") return BINS.music;
  return BINS.stock;
}

function fmtConf(c) {
  return typeof c === "number" ? c.toFixed(2) : "n/a";
}

function shotComments(shot, extra) {
  const lines = [
    shot.reason || "",
    `shot: ${shot.id} (${shot.type})`,
    `confidence: ${fmtConf(shot.confidence)}`,
    `status: ${shot.status}`,
  ];
  if (shot.file) lines.push(`file: ${shot.file}`);
  if (shot.source_title) lines.push(`source: ${shot.source_title}`);
  if (shot.source_url) lines.push(`url: ${shot.source_url}`);
  if (shot.error) lines.push(`error: ${shot.error}`);
  if (extra) lines.push(extra);
  return lines.filter(Boolean).join("\n");
}

function validateShotList(shotList) {
  if (!shotList || typeof shotList !== "object") throw new PlanError("shot list must be an object");
  if (shotList.schema_version !== 1) throw new PlanError(`unsupported schema_version ${shotList.schema_version}`);
  if (typeof shotList.fps !== "number" || !(shotList.fps > 0)) throw new PlanError("fps must be a positive number");
  if (!Array.isArray(shotList.shots)) throw new PlanError("shots must be an array");
  const seen = new Set();
  for (const s of shotList.shots) {
    if (!s || typeof s.id !== "string") throw new PlanError("every shot needs an id");
    if (seen.has(s.id)) throw new PlanError(`duplicate shot id ${s.id}`);
    seen.add(s.id);
  }
}

/** Volume keyframes for an audio bed: silence -> gain -> gain -> silence. db=null means silence. */
function audioEnvelope(durationFrames, fps, gainDb, fadeInS, fadeOutS) {
  let fi = fadeInS > 0 ? Math.max(1, Math.round(fadeInS * tc.trueRate(fps))) : 0;
  let fo = fadeOutS > 0 ? Math.max(1, Math.round(fadeOutS * tc.trueRate(fps))) : 0;
  const room = durationFrames - 1; // last keyframe sits on the last frame
  if (fi + fo > room && fi + fo > 0) {
    const scale = Math.max(0, room) / (fi + fo);
    fi = Math.floor(fi * scale);
    fo = Math.floor(fo * scale);
  }
  const kf = [];
  const push = (frame, db) => kf.push({ offsetFrames: frame, offsetTicks: tc.framesToTicks(frame, fps), db });
  if (fi > 0) push(0, null);
  push(fi, gainDb);
  if (fo > 0) {
    if (room - fo > fi) push(room - fo, gainDb);
    push(room, null);
  }
  return {
    gainDb,
    fadeInFrames: fi,
    fadeOutFrames: fo,
    fadeInTicks: tc.framesToTicks(fi, fps),
    fadeOutTicks: tc.framesToTicks(fo, fps),
    keyframes: kf,
  };
}

/**
 * @param {object} shotList  parsed shot_list.json (contract v1)
 * @param {object} options
 *   projectRoot (required)       native absolute path of project_root
 *   projectPath (required)       native path of the active .prproj
 *   existingProjectFiles         basenames in the .prproj's folder (for _BROLL_vN bumping)
 *   existingSequenceNames        names of all sequences in the project
 *   sourceSequence               override for shotList.source_sequence
 *   activeSequenceName           fallback when neither is given
 *   fileExists(absPath) -> bool  defaults to () => true
 *   ...DEFAULTS (see above)
 */
function buildPlan(shotList, options = {}) {
  validateShotList(shotList);
  const o = { ...DEFAULTS, ...stripUndefined(options) };
  if (!o.projectRoot) throw new PlanError("options.projectRoot is required");
  if (!o.projectPath) throw new PlanError("options.projectPath is required (save the project once first)");
  const fileExists = typeof o.fileExists === "function" ? o.fileExists : () => true;

  const fps = shotList.fps;
  const dropFrame = !!shotList.drop_frame;
  const generated = new Set((shotList.ai_budget && shotList.ai_budget.generated) || []);

  const sourceName = o.sourceSequence || shotList.source_sequence || o.activeSequenceName;
  if (!sourceName) throw new PlanError("no source sequence: set source_sequence in shot_list.json or open the sequence");
  const existingSeq = o.existingSequenceNames || null;
  const seqNames = planSequenceNames(sourceName, existingSeq || [], o.cloneFrom);
  if (existingSeq && !existingSeq.includes(seqNames.cloneFrom)) {
    throw new PlanError(`source sequence "${seqNames.cloneFrom}" not found in project`);
  }
  const proj = planProjectPath(o.projectPath, o.existingProjectFiles || []);

  const tpf = tc.ticksPerFrame(fps);
  const warnings = [];
  const skipped = [];

  // Parse every TC once, decide on zero-point handling.
  const parsed = [];
  for (const shot of shotList.shots) {
    try {
      // Contract: ';' before frames means drop-frame (same as shotlist.tc_to_frames(tc, fps)).
      parsed.push({ shot, frames: tc.tcToFrames(shot.start_tc, fps) });
      if (dropFrame !== shot.start_tc.includes(";")) {
        warnings.push(`${shot.id}: start_tc ${shot.start_tc} separator disagrees with drop_frame=${dropFrame}; using the separator`);
      }
    } catch (e) {
      parsed.push({ shot, frames: null });
      warnings.push(`${shot.id}: ${e.message}`);
    }
  }
  const zpFrames = tc.ticksToFrames(o.zeroPointTicks || "0", fps);
  let originFrames = 0;
  if (o.timecodeOrigin === "zero_point") originFrames = zpFrames;
  else if (o.timecodeOrigin === "auto" && zpFrames > 0) {
    const valid = parsed.filter((p) => p.frames !== null);
    if (valid.length && valid.every((p) => p.frames >= zpFrames)) originFrames = zpFrames;
  }
  if (originFrames) warnings.push(`start_tc values treated as display TC: subtracting sequence zero point ${tc.framesToTc(originFrames, fps, dropFrame)}`);

  const ordered = parsed
    .map((p, i) => ({ ...p, i }))
    .sort((a, b) => (a.frames ?? Infinity) - (b.frames ?? Infinity) || a.i - b.i);

  const imports = [];
  const shotOps = [];
  const markPlaced = [];

  for (const { shot, frames } of ordered) {
    if (frames === null) {
      skipped.push({ shot_id: shot.id, reason: "bad start_tc" });
      continue;
    }
    const startFrames = frames - originFrames;
    if (startFrames < 0) {
      skipped.push({ shot_id: shot.id, reason: "start_tc before sequence start" });
      continue;
    }
    if (shot.status === "placed") {
      skipped.push({ shot_id: shot.id, reason: "already placed" });
      continue;
    }
    const durationFrames = tc.secondsToFrames(Number(shot.duration_s) || 0, fps);
    const startTicks = tc.framesToTicks(startFrames, fps);
    const durationTicks = tc.framesToTicks(durationFrames, fps);
    const timing = {
      startFrames,
      startTicks,
      startTc: tc.framesToTc(startFrames, fps, dropFrame),
      durationFrames,
      durationTicks,
      endTicks: tc.addTicks(startTicks, durationTicks),
    };
    const lowConf = shot.status === "failed" || !(Number(shot.confidence) >= o.lowConfidence);

    if (!shot.file) {
      if (shot.status === "failed") {
        shotOps.push({
          op: "addMarker",
          shotId: shot.id,
          kind: "failed",
          name: `${shot.id} FAILED`,
          comments: shotComments(shot, "No clip placed: shot failed."),
          color: MARKER_COLORS.low,
          ...timing,
        });
        skipped.push({ shot_id: shot.id, reason: "failed, no file (red marker only)" });
      } else {
        skipped.push({ shot_id: shot.id, reason: `no file (status ${shot.status})` });
      }
      continue;
    }

    const absPath = paths.isAbsolute(shot.file) ? shot.file : paths.join(o.projectRoot, shot.file);
    if (!fileExists(absPath)) {
      if (o.markMissingFiles) {
        shotOps.push({
          op: "addMarker",
          shotId: shot.id,
          kind: "missing",
          name: `${shot.id} MISSING FILE`,
          comments: shotComments(shot, `File not found: ${absPath}`),
          color: MARKER_COLORS.low,
          ...timing,
        });
      }
      skipped.push({ shot_id: shot.id, reason: `file not found: ${shot.file}` });
      warnings.push(`${shot.id}: file not found ${absPath}`);
      continue;
    }

    const bin = binFor(shot, generated);
    imports.push({ op: "importFile", shotId: shot.id, path: absPath, relPath: shot.file, bin });

    const isVideo = shot.type === "stock_video" || shot.type === "ai_video";
    const place = {
      op: "placeClip",
      shotId: shot.id,
      type: shot.type,
      path: absPath,
      bin,
      subclipName: `${shot.id}_${durationFrames}f`,
      ...timing,
    };
    if (isVideo) {
      Object.assign(place, {
        mediaType: "video",
        trackIndex: o.videoTrackIndex,
        trackLabel: `V${o.videoTrackIndex + 1}`,
        takeVideo: true,
        takeAudio: false,
        muteAudio: true,
        audio: null,
        fallbackMarker: null,
      });
    } else {
      const isMusic = shot.type === "music";
      const trackIndex = isMusic ? o.musicTrackIndex : o.aiAudioTrackIndex;
      const gainDb = isMusic ? o.musicGainDb : o.aiAudioGainDb;
      const env = audioEnvelope(durationFrames, fps, gainDb, o.fadeInS, o.fadeOutS);
      const reminder = {
        op: "addMarker",
        shotId: shot.id,
        kind: "duck_reminder",
        name: `${isMusic ? "DUCK" : "FADE"} ${shot.id}`,
        comments:
          `${shot.id} on A${trackIndex + 1}: ` +
          (gainDb ? `duck about ${Math.abs(gainDb)} dB under dialogue and ` : "") +
          `add fade in (${o.fadeInS}s) / fade out (${o.fadeOutS}s). Automatic volume keyframes were not applied.`,
        color: MARKER_COLORS.reminder,
        ...timing,
      };
      Object.assign(place, {
        mediaType: "audio",
        trackIndex,
        trackLabel: `A${trackIndex + 1}`,
        takeVideo: false,
        takeAudio: true,
        muteAudio: false,
        audio: o.duckMode === "marker" ? null : env,
        fallbackMarker: reminder,
      });
    }
    shotOps.push(place);
    if (!isVideo && o.duckMode === "marker") shotOps.push(place.fallbackMarker);
    shotOps.push({
      op: "addMarker",
      shotId: shot.id,
      kind: "shot",
      name: `${shot.id} ${shot.type}`,
      comments: shotComments(shot),
      color: lowConf ? MARKER_COLORS.low : MARKER_COLORS.ok,
      ...timing,
    });
    markPlaced.push(shot.id);
  }

  const noop = markPlaced.length === 0;
  const operations = noop
    ? []
    : [
        { op: "saveProjectAs", path: proj.path, originalPath: o.projectPath },
        { op: "duplicateSequence", from: seqNames.cloneFrom, to: seqNames.target, original: seqNames.base },
        ...Object.values(BINS).map((name) => ({ op: "createBin", name })),
        ...imports,
        ...shotOps,
        { op: "saveProject", path: proj.path },
      ];

  return {
    planVersion: PLAN_VERSION,
    noop,
    project: shotList.project,
    fps,
    dropFrame,
    ticksPerFrame: tpf,
    originFrames,
    sourceSequence: seqNames.base,
    cloneFrom: seqNames.cloneFrom,
    targetSequence: seqNames.target,
    projectPathIn: o.projectPath,
    projectPathOut: proj.path,
    operations,
    markPlaced,
    skipped,
    warnings,
  };
}

function stripUndefined(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) if (v !== undefined) out[k] = v;
  return out;
}

/** One-line-per-shot summary for the panel UI. */
function summarizeShotList(shotList) {
  const counts = {};
  for (const s of shotList.shots || []) counts[s.status] = (counts[s.status] || 0) + 1;
  return {
    project: shotList.project,
    fps: shotList.fps,
    total: (shotList.shots || []).length,
    counts,
    rows: (shotList.shots || []).map((s) => ({
      id: s.id,
      start_tc: s.start_tc,
      duration_s: s.duration_s,
      type: s.type,
      status: s.status,
      confidence: s.confidence,
      file: s.file,
    })),
  };
}

module.exports = {
  PLAN_VERSION,
  BINS,
  DEFAULTS,
  MARKER_COLORS,
  PlanError,
  buildPlan,
  planSequenceNames,
  planProjectPath,
  audioEnvelope,
  summarizeShotList,
  stripBrollSuffix,
};
