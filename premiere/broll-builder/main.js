"use strict";
/* Panel glue. All logic lives in src/; this file only wires buttons to it. */

const ppro = require("premierepro");
const uxp = require("uxp");
const fs = require("fs");

const { summarizeShotList } = require("./src/core/plan.js");
const paths = require("./src/core/paths.js");
const fsio = require("./src/premiere/fsio.js");
const { runBuild, processPendingJob } = require("./src/premiere/runner.js");

const WATCH_MS = 5000;
const LS_ROOT = "broll.projectRoot";

let projectRoot = null;
let busy = false;
let watchTimer = null;

const $ = (id) => document.getElementById(id);

function log(msg, level) {
  const line = document.createElement("div");
  if (level) line.className = level;
  line.textContent = `${new Date().toLocaleTimeString()}  ${msg}`;
  $("log").appendChild(line);
  $("log").scrollTop = $("log").scrollHeight;
  (level === "error" ? console.error : console.log)(msg);
}

function setRoot(p) {
  projectRoot = p;
  $("root").textContent = p || "(no folder)";
  try {
    if (p) localStorage.setItem(LS_ROOT, p);
  } catch (_) {
    /* storage unavailable */
  }
}

async function pickRoot() {
  const folder = await uxp.storage.localFileSystem.getFolder();
  if (!folder) return;
  setRoot(folder.nativePath);
  await reload();
}

async function reload() {
  if (!projectRoot) return;
  try {
    const data = await fsio.readJson(fs, paths.join(projectRoot, "shot_list.json"));
    const s = summarizeShotList(data);
    const counts = Object.entries(s.counts).map(([k, v]) => `${k}: ${v}`).join(", ");
    $("summary").textContent = `${s.project} @ ${s.fps} fps, ${s.total} shots (${counts})`;
    const rows = s.rows
      .map((r) => {
        const cls = [r.status === "placed" ? "placed" : "", r.confidence < 0.6 || r.status === "failed" ? "low" : ""].join(" ");
        return `<tr class="${cls}"><td>${r.id}</td><td>${r.start_tc}</td><td>${r.duration_s}s</td><td>${r.type}</td><td>${r.status}</td><td>${Number(r.confidence).toFixed(2)}</td></tr>`;
      })
      .join("");
    $("shots").innerHTML = `<tr><th>ID</th><th>TC</th><th>Dur</th><th>Type</th><th>Status</th><th>Conf</th></tr>${rows}`;
  } catch (e) {
    $("summary").textContent = `Could not read shot_list.json: ${e.message}`;
    $("shots").innerHTML = "";
  }
}

async function guarded(fn) {
  if (busy) {
    log("Busy, try again in a moment.", "warn");
    return;
  }
  busy = true;
  try {
    await fn();
  } catch (e) {
    log(`Error: ${e.message}`, "error");
  } finally {
    busy = false;
  }
}

function deps() {
  return { ppro, fs, log };
}

async function build(dryRun) {
  if (!projectRoot) return log("Pick project_root first.", "warn");
  const { plan, result } = await runBuild(deps(), { projectRoot, dryRun, jobId: dryRun ? null : `panel-${Date.now()}` });
  if (plan) {
    for (const op of plan.operations) {
      if (dryRun) log(`${op.op} ${op.shotId || op.name || op.path || op.to || ""} ${op.startTc ? "@ " + op.startTc + " " + (op.trackLabel || "") : ""}`);
    }
    for (const s of plan.skipped) log(`skip ${s.shot_id}: ${s.reason}`);
  }
  if (result) log(`Result: ${result.status}; placed ${result.placed.join(", ") || "none"}${result.error ? "; " + result.error : ""}`, result.status === "error" ? "error" : null);
  await reload();
}

async function runJob() {
  if (!projectRoot) return log("Pick project_root first.", "warn");
  const result = await processPendingJob(deps(), projectRoot);
  if (!result) log("No pending job (scripts/broll_job.json).");
  else log(`Job ${result.job_id}: ${result.status}`, result.status === "error" ? "error" : null);
  await reload();
}

function toggleWatch(on) {
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = null;
  if (on) {
    watchTimer = setInterval(async () => {
      if (busy || !projectRoot) return;
      if (!(await fsio.exists(fs, paths.join(projectRoot, "scripts", "broll_job.json")))) return;
      await guarded(runJob);
    }, WATCH_MS);
    log(`Watching ${projectRoot}/scripts/broll_job.json every ${WATCH_MS / 1000}s`);
  }
}

function init() {
  $("pick").addEventListener("click", () => guarded(pickRoot));
  $("reload").addEventListener("click", () => guarded(reload));
  $("dryrun").addEventListener("click", () => guarded(() => build(true)));
  $("build").addEventListener("click", () => guarded(() => build(false)));
  $("runjob").addEventListener("click", () => guarded(runJob));
  $("watch").addEventListener("change", (e) => toggleWatch(!!e.target.checked));
  try {
    const saved = localStorage.getItem(LS_ROOT);
    if (saved) {
      setRoot(saved);
      reload();
    }
  } catch (_) {
    /* storage unavailable */
  }
}

let initialized = false;
function initOnce() {
  if (initialized) return;
  initialized = true;
  init();
}

uxp.entrypoints.setup({
  panels: {
    brollPanel: {
      show() {
        initOnce();
      },
    },
  },
});
initOnce(); // the DOM is already parsed: main.js is loaded at the end of <body>
