"use strict";
/**
 * End-to-end run: read shot_list.json -> gather Premiere state -> buildPlan ->
 * execute -> (after a successful save) mark shots placed -> write result file.
 * Used by both the panel buttons and the job-file hand-off.
 */

const paths = require("../core/paths.js");
const { buildPlan, stripBrollSuffix } = require("../core/plan.js");
const shotlistIo = require("../core/shotlist_io.js");
const job = require("../core/job.js");
const fsio = require("./fsio.js");
const { Executor } = require("./executor.js");

function iso(d) {
  return (d || new Date()).toISOString();
}

/** Collect what buildPlan needs from Premiere + disk. */
async function gatherEnv({ ppro, fs }, project, shotList, req) {
  const projectPath = project.path;
  const sequences = await project.getSequences();
  const names = sequences.map((s) => s.name);
  let activeName;
  try {
    const active = await project.getActiveSequence();
    activeName = active ? active.name : undefined;
  } catch (_) {
    activeName = undefined;
  }
  const sourceName = req.sourceSequence || shotList.source_sequence || activeName;
  let zeroPointTicks = "0";
  if (sourceName) {
    const src = sequences.find((s) => s.name === stripBrollSuffix(sourceName));
    if (src) {
      try {
        const zp = await src.getZeroPoint();
        if (zp && zp.ticks !== undefined) zeroPointTicks = String(zp.ticks);
      } catch (_) {
        /* keep 0 */
      }
    }
  }
  const existingProjectFiles = projectPath ? await fsio.listDir(fs, paths.dirname(projectPath)) : [];
  const existing = new Set();
  for (const s of shotList.shots) {
    if (!s.file) continue;
    const abs = paths.isAbsolute(s.file) ? s.file : paths.join(req.projectRoot, s.file);
    if (await fsio.exists(fs, abs)) existing.add(abs);
  }
  return {
    projectPath,
    existingSequenceNames: names,
    activeSequenceName: activeName,
    sourceSequence: req.sourceSequence,
    zeroPointTicks,
    existingProjectFiles,
    fileExists: (p) => existing.has(p),
  };
}

async function getProject(ppro, prproj, log) {
  let project = await ppro.Project.getActiveProject();
  if (prproj && (!project || !paths.samePath(project.path, prproj))) {
    log(`Opening ${prproj}`);
    project = await ppro.Project.open(prproj);
  }
  if (!project) throw new Error("no active Premiere project");
  return project;
}

/**
 * @param deps { ppro, fs, log, sleep, pollTries, pollMs }
 * @param req  { projectRoot, shotListRel, prproj, sourceSequence, planOptions, writeShotList, jobId, writeResult, dryRun }
 */
async function runBuild(deps, req) {
  const log = deps.log || (() => {});
  const startedAt = iso();
  const base = { jobId: req.jobId, startedAt };
  const shotListPath = paths.join(req.projectRoot, req.shotListRel || shotlistIo.SHOT_LIST_NAME);
  let result;
  let plan = null;
  try {
    const shotList = await fsio.readJson(deps.fs, shotListPath);
    const project = await getProject(deps.ppro, req.prproj, log);
    const env = await gatherEnv(deps, project, shotList, req);
    plan = buildPlan(shotList, { ...env, ...(req.planOptions || {}), projectRoot: req.projectRoot });
    for (const w of plan.warnings) log(w, "warn");
    if (req.dryRun) {
      return { plan, result: null };
    }
    if (plan.noop) {
      log("Nothing to place (all shots placed, missing files, or no files yet).");
      result = job.makeResult({ ...base, status: "noop", finishedAt: iso(), projectPathIn: plan.projectPathIn, skipped: plan.skipped, warnings: plan.warnings });
    } else {
      log(`Plan: ${plan.markPlaced.length} clips -> "${plan.targetSequence}" in ${plan.projectPathOut}`);
      const ex = new Executor({ ...deps, project, log });
      let execRes;
      let execErr = null;
      try {
        execRes = await ex.run(plan);
      } catch (e) {
        execErr = e;
        execRes = ex.result;
      }
      let shotListUpdated = false;
      if (!execErr && execRes.saved && execRes.placed.length && req.writeShotList !== false) {
        // Re-read right before writing so we only touch status/updated_by of our shots.
        const fresh = await fsio.readJson(deps.fs, shotListPath);
        const { data, changed } = shotlistIo.applyPlaced(fresh, execRes.placed, "C");
        if (changed.length) await fsio.writeJsonAtomic(deps.fs, shotListPath, data);
        shotListUpdated = true;
        log(`shot_list.json: marked placed ${changed.join(", ") || "(none changed)"}`);
      }
      const status = execErr ? "error" : execRes.failed.length ? "partial" : "ok";
      result = job.makeResult({
        ...base,
        status,
        finishedAt: iso(),
        projectPathIn: plan.projectPathIn,
        projectPathOut: execRes.projectPath || plan.projectPathOut,
        sequence: execRes.sequence,
        clonedFrom: plan.cloneFrom,
        placed: execErr ? [] : execRes.placed,
        failed: execRes.failed,
        skipped: plan.skipped,
        fallbacks: execRes.fallbacks,
        markersAdded: execRes.markersAdded,
        shotListUpdated,
        warnings: plan.warnings,
        error: execErr ? execErr.message : null,
      });
      if (execErr) log(`Stopped: ${execErr.message}`, "error");
    }
  } catch (e) {
    log(`Error: ${e.message}`, "error");
    result = job.makeResult({ ...base, status: "error", finishedAt: iso(), error: e.message, projectPathIn: plan && plan.projectPathIn });
  }
  if (req.writeResult !== false) {
    const logsDir = paths.join(req.projectRoot, "logs");
    await fsio.ensureDir(deps.fs, logsDir);
    await fsio.writeJsonAtomic(deps.fs, paths.join(req.projectRoot, job.RESULT_REL), result);
  }
  return { plan, result };
}

/** Process <projectRoot>/scripts/broll_job.json if present. Returns the result or null. */
async function processPendingJob(deps, projectRoot) {
  const log = deps.log || (() => {});
  const jobPath = paths.join(projectRoot, job.JOB_REL);
  if (!(await fsio.exists(deps.fs, jobPath))) return null;
  let parsed;
  let raw = null;
  try {
    raw = await fsio.readJson(deps.fs, jobPath);
    parsed = job.parseJob(raw, projectRoot);
  } catch (e) {
    log(`Bad job file: ${e.message}`, "error");
    const result = job.makeResult({ jobId: raw && raw.job_id, status: "error", startedAt: iso(), finishedAt: iso(), error: `bad job file: ${e.message}` });
    await fsio.ensureDir(deps.fs, paths.join(projectRoot, "logs"));
    await fsio.writeJsonAtomic(deps.fs, paths.join(projectRoot, job.RESULT_REL), result);
    await archiveJob(deps.fs, jobPath, (raw && raw.job_id) || "invalid", "failed");
    return result;
  }
  log(`Running job ${parsed.jobId}`);
  const { result } = await runBuild(deps, {
    jobId: parsed.jobId,
    projectRoot: parsed.projectRoot,
    shotListRel: parsed.shotListRel,
    prproj: parsed.prproj,
    sourceSequence: parsed.sourceSequence,
    planOptions: parsed.planOptions,
    writeShotList: parsed.writeShotList,
  });
  await archiveJob(deps.fs, jobPath, parsed.jobId, result.status === "error" ? "failed" : "done");
  return result;
}

async function archiveJob(fs, jobPath, jobId, suffix) {
  const safe = String(jobId).replace(/[^A-Za-z0-9._-]/g, "_");
  const dest = paths.join(paths.dirname(jobPath), `broll_job.${safe}.${suffix}.json`);
  try {
    await fs.rename(jobPath, dest);
  } catch (_) {
    try {
      await fs.unlink(jobPath);
    } catch (_e) {
      /* ignore */
    }
  }
}

module.exports = { runBuild, processPendingJob, gatherEnv };
