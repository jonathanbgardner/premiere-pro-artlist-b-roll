// Job-file hand-off end to end on a temp project_root: job -> build -> shot_list statuses -> result file.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { load, REPO, FIXTURE_5 } from "./helpers/load.mjs";
import { createMockPpro } from "./helpers/mock_premierepro.mjs";

const { runBuild, processPendingJob } = load("src/premiere/runner.js");
const job = load("src/core/job.js");

function makeProject() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "broll-c-"));
  for (const d of ["assets", "scripts", "logs", "transcript"]) fs.mkdirSync(path.join(root, d));
  const data = JSON.parse(fs.readFileSync(FIXTURE_5, "utf-8"));
  const media = {};
  for (const s of data.shots) {
    if (s.id === "S004") continue; // AI shot not generated yet: stays pending, no file
    s.status = "downloaded";
    s.file = s.type === "music" ? `assets/${s.id}.wav` : `assets/${s.id}.mp4`;
    s.confidence = 0.8;
    s.updated_by = "B";
    s.source_title = `Artlist ${s.id}`;
    fs.writeFileSync(path.join(root, s.file), "fake");
    media[path.join(root, s.file)] = { durationS: 90, video: s.type !== "music", audio: true };
  }
  fs.writeFileSync(path.join(root, "shot_list.json"), JSON.stringify(data, null, 2) + "\n");
  const prproj = path.join(root, "test_episode.prproj");
  fs.writeFileSync(prproj, "ORIGINAL");
  const ppro = createMockPpro({
    projectPath: prproj,
    media,
    sequences: [{ name: "test_episode_main" }],
    onWrite: async (p) => fsp.writeFile(p, "BROLL COPY"),
  });
  return { root, prproj, ppro, data };
}

function pyValidate(file) {
  return spawnSync(
    "python3",
    ["-c", "import sys; sys.path.insert(0, sys.argv[1]); from pipeline.common.shotlist import load; d = load(sys.argv[2]); print(len(d['shots']))", REPO, file],
    { encoding: "utf-8" }
  );
}

test("orchestrator job: builds, marks placed, writes result, archives job", async () => {
  const { root, prproj, ppro, data } = makeProject();
  const jobBody = { job_version: 1, job_id: "run-001", action: "build_broll", project_root: root, options: { music_gain_db: -12, duck_mode: "auto" } };
  fs.writeFileSync(path.join(root, job.JOB_REL), JSON.stringify(jobBody));
  const logs = [];
  const result = await processPendingJob({ ppro, fs: fsp, log: (m) => logs.push(m), sleep: async () => {}, pollTries: 3 }, root);

  assert.equal(result.status, "ok", JSON.stringify(result));
  assert.equal(result.job_id, "run-001");
  assert.deepEqual(result.placed, ["S001", "S002", "S003", "S005"]);
  assert.equal(result.sequence, "test_episode_main_BROLL_v1");
  assert.equal(result.project_path_out, path.join(root, "test_episode_BROLL_v1.prproj"));
  assert.ok(result.skipped.some((s) => s.shot_id === "S004"));
  assert.equal(result.shot_list_updated, true);

  // original .prproj untouched, copy written
  assert.equal(fs.readFileSync(prproj, "utf-8"), "ORIGINAL");
  assert.equal(fs.readFileSync(path.join(root, "test_episode_BROLL_v1.prproj"), "utf-8"), "BROLL COPY");

  // result file on disk == returned result
  const onDisk = JSON.parse(fs.readFileSync(path.join(root, job.RESULT_REL), "utf-8"));
  assert.deepEqual(onDisk, result);

  // job archived, not re-run
  assert.ok(!fs.existsSync(path.join(root, job.JOB_REL)));
  assert.ok(fs.existsSync(path.join(root, "scripts", "broll_job.run-001.done.json")));
  assert.equal(await processPendingJob({ ppro, fs: fsp }, root), null);

  // shot_list: only status/updated_by changed on placed shots; everything else preserved; still schema-valid
  const after = JSON.parse(fs.readFileSync(path.join(root, "shot_list.json"), "utf-8"));
  for (const s of after.shots) {
    const before = data.shots.find((x) => x.id === s.id);
    if (["S001", "S002", "S003", "S005"].includes(s.id)) {
      assert.equal(s.status, "placed");
      assert.equal(s.updated_by, "C");
      assert.deepEqual({ ...s, status: before.status, updated_by: before.updated_by }, before);
    } else {
      assert.deepEqual(s, before);
    }
  }
  assert.deepEqual({ ...after, shots: null }, { ...data, shots: null });
  assert.deepEqual(fs.readdirSync(root).filter((f) => f.endsWith(".tmp")), []);
  const py = pyValidate(path.join(root, "shot_list.json"));
  assert.equal(py.status, 0, py.stderr);
});

test("second run is a noop and changes nothing", async () => {
  const { root, ppro } = makeProject();
  const deps = { ppro, fs: fsp, sleep: async () => {}, pollTries: 3 };
  await runBuild(deps, { projectRoot: root });
  const listBefore = fs.readFileSync(path.join(root, "shot_list.json"), "utf-8");
  const writes = ppro.state.writes.length;
  const { result } = await runBuild(deps, { projectRoot: root, jobId: "again" });
  assert.equal(result.status, "noop");
  assert.equal(ppro.state.writes.length, writes);
  assert.equal(fs.readFileSync(path.join(root, "shot_list.json"), "utf-8"), listBefore);
});

test("bad job file -> error result, job archived as failed", async () => {
  const { root, ppro } = makeProject();
  fs.writeFileSync(path.join(root, job.JOB_REL), JSON.stringify({ job_version: 99, job_id: "x" }));
  const result = await processPendingJob({ ppro, fs: fsp }, root);
  assert.equal(result.status, "error");
  assert.match(result.error, /job_version/);
  assert.ok(fs.existsSync(path.join(root, "scripts", "broll_job.x.failed.json")));
  assert.deepEqual(ppro.state.writes, []);
});

test("write_shot_list false leaves shot_list.json alone (orchestrator applies statuses itself)", async () => {
  const { root, ppro } = makeProject();
  const before = fs.readFileSync(path.join(root, "shot_list.json"), "utf-8");
  fs.writeFileSync(path.join(root, job.JOB_REL), JSON.stringify({ job_version: 1, job_id: "j2", write_shot_list: false }));
  const result = await processPendingJob({ ppro, fs: fsp, sleep: async () => {}, pollTries: 3 }, root);
  assert.equal(result.status, "ok");
  assert.equal(result.shot_list_updated, false);
  assert.equal(result.placed.length, 4);
  assert.equal(fs.readFileSync(path.join(root, "shot_list.json"), "utf-8"), before);
});

test("dry run returns the plan without touching Premiere", async () => {
  const { root, ppro } = makeProject();
  const { plan, result } = await runBuild({ ppro, fs: fsp }, { projectRoot: root, dryRun: true });
  assert.equal(result, null);
  assert.equal(plan.markPlaced.length, 4);
  assert.deepEqual(ppro.state.writes, []);
  assert.ok(!ppro.calls.some((c) => /Action$/.test(c)));
});
