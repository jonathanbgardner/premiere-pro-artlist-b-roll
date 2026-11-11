// paths, job parsing, shot list write-back helpers, panel/manifest sanity.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { load, PLUGIN } from "./helpers/load.mjs";

const paths = load("src/core/paths.js");
const job = load("src/core/job.js");
const io = load("src/core/shotlist_io.js");

test("paths: POSIX and Windows joins", () => {
  assert.equal(paths.join("/p/root", "assets/S001.mp4"), "/p/root/assets/S001.mp4");
  assert.equal(paths.join("/p/root/", "./assets//S001.mp4"), "/p/root/assets/S001.mp4");
  assert.equal(paths.join("C:\\Edit\\ep", "assets/S001.mp4"), "C:\\Edit\\ep\\assets\\S001.mp4");
  assert.equal(paths.dirname("C:\\Edit\\ep.prproj"), "C:\\Edit");
  assert.equal(paths.dirname("C:\\ep.prproj"), "C:\\");
  assert.equal(paths.basename("/a/b/ep.prproj"), "ep.prproj");
  assert.ok(paths.samePath("C:\\Edit\\A.mp4", "c:/edit/a.mp4"));
  assert.ok(!paths.samePath("/a/A.mp4", "/a/a.mp4"));
});

test("job parsing and option mapping", () => {
  const j = job.parseJob({ job_version: 1, job_id: "x", options: { clone_from: "original", fade_in_s: 0.5, unknown: 1 } }, "/root");
  assert.equal(j.projectRoot, "/root");
  assert.equal(j.shotListRel, "shot_list.json");
  assert.equal(j.writeShotList, true);
  assert.deepEqual(j.planOptions, { cloneFrom: "original", fadeInS: 0.5 });
  assert.throws(() => job.parseJob({ job_version: 1, job_id: "x", options: { duck_mode: "loud" } }), /duck_mode/);
  assert.throws(() => job.parseJob({ job_version: 1 }), /job_id/);
  assert.throws(() => job.parseJob({ job_version: 1, job_id: "x", action: "render" }), /action/);
});

test("applyPlaced only touches status/updated_by of listed, not-yet-placed shots", () => {
  const src = { schema_version: 1, extra: { keep: true }, shots: [{ id: "S001", status: "downloaded", x: 1 }, { id: "S002", status: "placed", updated_by: "C" }, { id: "S003", status: "pending" }] };
  const { data, changed } = io.applyPlaced(src, ["S001", "S002"]);
  assert.deepEqual(changed, ["S001"]);
  assert.deepEqual(data.shots[0], { id: "S001", status: "placed", x: 1, updated_by: "C" });
  assert.deepEqual(data.shots[2], { id: "S003", status: "pending" });
  assert.equal(src.shots[0].status, "downloaded", "input not mutated");
  assert.ok(io.serialize(data).endsWith("}\n"));
});

test("manifest is UXP v5 for premierepro with fullAccess fs and the panel entrypoint", () => {
  const m = JSON.parse(fs.readFileSync(path.join(PLUGIN, "manifest.json"), "utf-8"));
  assert.equal(m.manifestVersion, 5);
  assert.equal(m.host.app, "premierepro");
  assert.equal(m.requiredPermissions.localFileSystem, "fullAccess");
  assert.equal(m.entrypoints[0].type, "panel");
  assert.equal(m.entrypoints[0].id, "brollPanel");
  const main = fs.readFileSync(path.join(PLUGIN, "main.js"), "utf-8");
  assert.ok(main.includes("brollPanel"));
  for (const f of ["main.js", "index.html", ...m.icons.map((i) => i.path)]) assert.ok(fs.existsSync(path.join(PLUGIN, f)), f);
  const chk = spawnSync(process.execPath, ["--check", path.join(PLUGIN, "main.js")], { encoding: "utf-8" });
  assert.equal(chk.status, 0, chk.stderr);
});
