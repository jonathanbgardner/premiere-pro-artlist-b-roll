// Executor against the mock premierepro module: call sequence and resulting timeline.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { load, FIXTURE_5 } from "./helpers/load.mjs";
import { createMockPpro } from "./helpers/mock_premierepro.mjs";

const { buildPlan } = load("src/core/plan.js");
const { Executor } = load("src/premiere/executor.js");
const audio = load("src/core/audio.js");

const ROOT = "/p";
const PRPROJ = "/p/ep.prproj";
const TPF = 8475667200n;

function shotList() {
  const data = JSON.parse(fs.readFileSync(FIXTURE_5, "utf-8"));
  data.source_sequence = "ep_main";
  for (const s of data.shots) {
    s.status = "downloaded";
    s.file = s.type === "music" ? `assets/${s.id}.wav` : `assets/${s.id}.mp4`;
    s.confidence = s.id === "S003" ? 0.4 : 0.9;
  }
  return data;
}

function media() {
  // stock clips have audio too (worst case); music is longer than its bed
  return {
    "/p/assets/S001.wav": { durationS: 180, video: false, audio: true },
    "/p/assets/S002.mp4": { durationS: 20, video: true, audio: true },
    "/p/assets/S003.mp4": { durationS: 12, video: true, audio: true },
    "/p/assets/S004.mp4": { durationS: 8, video: true, audio: false },
    "/p/assets/S005.mp4": { durationS: 30, video: true, audio: true },
  };
}

function setup(config = {}) {
  const ppro = createMockPpro({
    projectPath: PRPROJ,
    media: media(),
    // the original has dialogue on V1/A1 that must survive untouched
    sequences: [{ name: "ep_main", items: [["video", 0, 0, 300n * TPF], ["audio", 0, 0, 300n * TPF]] }],
    ...config,
  });
  const plan = buildPlan(shotList(), {
    projectRoot: ROOT,
    projectPath: PRPROJ,
    existingProjectFiles: ["ep.prproj"],
    existingSequenceNames: ppro.project.sequences.map((s) => s.name),
    ...(config.planOptions || {}),
  });
  const logs = [];
  const ex = new Executor({ ppro, project: ppro.project, log: (m, l) => logs.push(`${l || "info"}: ${m}`), sleep: async () => {}, pollTries: 3 });
  return { ppro, plan, ex, logs };
}

const seqByName = (ppro, n) => ppro.project.sequences.find((s) => s.name === n);

test("happy path: duplicate, bins, import, place on V2/A3, markers, save copy", async () => {
  const { ppro, plan, ex } = setup();
  const res = await ex.run(plan);
  assert.deepEqual(res.failed, []);
  assert.deepEqual(res.placed, ["S001", "S002", "S003", "S004", "S005"]);
  assert.equal(res.saved, true);
  assert.equal(res.sequence, "ep_main_BROLL_v1");

  // the original .prproj is never written; first write is the copy
  assert.deepEqual(ppro.state.writes, ["/p/ep_BROLL_v1.prproj", "/p/ep_BROLL_v1.prproj"]);
  const firstSave = ppro.calls.findIndex((c) => c.startsWith("Project.saveAs"));
  const firstAction = ppro.calls.findIndex((c) => /\.create\w+Action$/.test(c));
  assert.ok(firstSave >= 0 && firstSave < firstAction, "saveAs happens before any edit");
  assert.equal(ppro.calls.filter((c) => c === "Project.save /p/ep_BROLL_v1.prproj").length, 1);
  assert.ok(ppro.calls.at(-1).startsWith("Project.save "), "save is the last call");

  // original sequence untouched
  const orig = seqByName(ppro, "ep_main");
  assert.equal(orig.video[1].items.length, 0);
  assert.equal(orig.audio[2].items.length, 0);
  assert.equal(orig.markers.length, 0);
  assert.equal(orig.video[0].items.length, 1);

  const seq = seqByName(ppro, "ep_main_BROLL_v1");
  assert.ok(seq, "duplicate renamed");
  // dialogue survived in the copy
  assert.equal(seq.video[0].items.length, 1);
  assert.equal(seq.audio[0].items[0].end, 300n * TPF);

  // V2 clips at exact start ticks, trimmed to duration_s, no audio taken
  const v2 = seq.video[1].items.map((i) => [i.projectItem.name, i.start / TPF, (i.end - i.start) / TPF]);
  assert.deepEqual(v2, [
    ["S002_120f", 450n, 120n],
    ["S003_165f", 945n, 165n],
    ["S004_180f", 1440n, 180n],
    ["S005_90f", 1970n, 90n],
  ]);
  // S004 media is only 8 s, subclip still exactly 6 s (180f)
  for (const t of seq.audio.slice(1)) {
    for (const it of t.items) assert.ok(!/^S00[2-5]/.test(it.projectItem.name), "no linked audio from video B-roll");
  }
  // music on A3, 1798 frames, with duck/fade keyframes
  const a3 = seq.audio[2].items;
  assert.equal(a3.length, 1);
  assert.equal(a3[0].projectItem.name, "S001_1798f");
  assert.equal(a3[0].start, 0n);
  assert.equal(a3[0].end, 1798n * TPF);
  const level = a3[0].chain.components[0].params[1];
  assert.equal(level.timeVarying, true);
  const duck = Math.pow(10, (-12 - 15) / 20);
  assert.deepEqual(level.keyframes.map((k) => [BigInt(k.ticks) / TPF, k.value]), [
    [0n, 0], [30n, duck], [1737n, duck], [1797n, 0],
  ]);

  // bins and their contents
  const bins = Object.fromEntries(ppro.project.root.items.filter((i) => i.isFolder).map((b) => [b.name, b.items.map((i) => i.name).sort()]));
  assert.deepEqual(bins["Stock Video"], ["S002.mp4", "S002_120f", "S003.mp4", "S003_165f", "S005.mp4", "S005_90f"]);
  assert.deepEqual(bins["Music"], ["S001.wav", "S001_1798f"]);
  assert.deepEqual(bins["AI Generated"], ["S004.mp4", "S004_180f"]);

  // markers: one per clip, red for low confidence, comments carry reason + confidence
  const ms = seq.markers.map((m) => [m.name, m.start / TPF, m.colorIndex]);
  assert.deepEqual(ms, [
    ["S001 music", 0n, 0],
    ["S002 stock_video", 450n, 0],
    ["S003 stock_video", 945n, 1],
    ["S004 ai_video", 1440n, 0],
    ["S005 stock_video", 1970n, 0],
  ]);
  assert.ok(seq.markers[2].comments.includes("Mentions grain prices rising 30%"));
  assert.ok(seq.markers[2].comments.includes("confidence: 0.40"));
  assert.equal(seq.markers[0].type, "Comment");
  assert.equal(seq.markers[1].duration, 120n * TPF);
});

test("call sequence for one stock shot follows the documented pattern", async () => {
  const { ppro, plan, ex } = setup();
  await ex.run(plan);
  const seq = ppro.calls;
  const i = seq.indexOf("ClipProjectItem.createSubClipAction", seq.findIndex((c) => c.startsWith("Project.executeTransaction B-roll: subclip S002")) - 2);
  const window = seq.slice(i - 1, i + 12).filter((c) => !/getItems|getSequences|getTrackItems/.test(c));
  assert.deepEqual(window.slice(0, 3), ["Project.lockedAccess", "ClipProjectItem.createSubClipAction", "Project.executeTransaction B-roll: subclip S002_120f"]);
  const ow = seq.indexOf("SequenceEditor.createOverwriteItemAction");
  assert.equal(seq[ow - 1], "Project.lockedAccess");
  assert.equal(seq[ow + 1], "Project.executeTransaction B-roll: place S001 on A3");
  assert.ok(seq.includes("Sequence.createCloneAction"));
  assert.ok(seq.indexOf("Sequence.createCloneAction") < seq.indexOf("FolderItem.createBinAction"));
  assert.equal(seq.filter((c) => c === "FolderItem.createBinAction").length, 3);
  assert.equal(seq.filter((c) => c === "Markers.createAddMarkerAction").length, 5);
  assert.equal(seq.filter((c) => c === "Marker.createSetColorByIndexAction").length, 5);
});

test("pre-26.3 fallback: no subclips -> clip in/out, linked audio disabled on a scratch track", async () => {
  const { ppro, plan, ex } = setup({ features: { subclip: false } });
  const res = await ex.run(plan);
  assert.deepEqual(res.failed, []);
  const seq = seqByName(ppro, "ep_main_BROLL_v1");
  const v2 = seq.video[1].items.map((i) => [i.start / TPF, (i.end - i.start) / TPF]);
  assert.deepEqual(v2, [[450n, 120n], [945n, 165n], [1440n, 180n], [1970n, 90n]]);
  // linked audio landed on A5 (index 4), never on A1-A4, and is disabled + trimmed
  for (const idx of [0, 1, 2, 3]) {
    for (const it of seq.audio[idx].items) assert.ok(!it.projectItem || !/S00[2-5]/.test(it.projectItem.name));
  }
  const scratch = seq.audio[4].items;
  assert.ok(scratch.length >= 3);
  for (const it of scratch) {
    assert.equal(it.disabled, true);
  }
  assert.ok(res.fallbacks.some((f) => f.what === "mute"));
  assert.ok(ppro.calls.includes("ClipProjectItem.createSetInOutPointsAction"));
  assert.ok(ppro.calls.includes("AudioClipTrackItem.createSetDisabledAction"));
});

test("unknown Level units -> no keyframes, yellow DUCK reminder marker", async () => {
  const { ppro, plan, ex } = setup({ levelDefault: 0.5 });
  const res = await ex.run(plan);
  const seq = seqByName(ppro, "ep_main_BROLL_v1");
  const level = seq.audio[2].items[0].chain.components[0].params[1];
  assert.equal(level.keyframes.length, 0);
  const duck = seq.markers.find((m) => m.name === "DUCK S001");
  assert.ok(duck);
  assert.equal(duck.colorIndex, 4);
  assert.ok(/duck about 12 dB/.test(duck.comments));
  assert.ok(res.fallbacks.some((f) => f.shot_id === "S001" && f.what === "duck"));
  assert.ok(res.placed.includes("S001"));
});

test("dB-unit Level writes -12 directly", async () => {
  const { ppro, plan, ex } = setup({ levelDefault: 0 });
  await ex.run(plan);
  const level = seqByName(ppro, "ep_main_BROLL_v1").audio[2].items[0].chain.components[0].params[1];
  assert.deepEqual(level.keyframes.map((k) => k.value), [audio.SILENCE_DB, -12, -12, audio.SILENCE_DB]);
});

test("import that misses the target bin is moved; failed import does not stop the run", async () => {
  const m = media();
  delete m["/p/assets/S005.mp4"]; // importFiles returns false for this one
  const { ppro, plan, ex } = setup({ media: m, features: { importToRoot: true } });
  const res = await ex.run(plan);
  assert.deepEqual(res.placed, ["S001", "S002", "S003", "S004"]);
  assert.equal(res.failed.length, 1);
  assert.equal(res.failed[0].shot_id, "S005");
  assert.ok(ppro.calls.includes("FolderItem.createMoveItemAction"));
  const seq = seqByName(ppro, "ep_main_BROLL_v1");
  assert.ok(seq.markers.some((m) => m.name === "S005 PLACE FAILED" && m.colorIndex === 1));
  assert.equal(res.saved, true);
});

test("re-run from a B-roll project clones the latest version and leaves it alone", async () => {
  const { ppro, plan, ex } = setup();
  await ex.run(plan);
  const v1 = seqByName(ppro, "ep_main_BROLL_v1");
  const v1Items = v1.video[1].items.length;
  const data = shotList();
  for (const s of data.shots) if (s.id !== "S005") s.status = "placed";
  data.shots.find((s) => s.id === "S005").start_tc = "00:01:10:00";
  const plan2 = buildPlan(data, {
    projectRoot: ROOT,
    projectPath: ppro.project.path,
    existingProjectFiles: ["ep.prproj", "ep_BROLL_v1.prproj"],
    existingSequenceNames: ppro.project.sequences.map((s) => s.name),
  });
  assert.equal(plan2.cloneFrom, "ep_main_BROLL_v1");
  const ex2 = new Executor({ ppro, project: ppro.project, sleep: async () => {}, pollTries: 3 });
  const res2 = await ex2.run(plan2);
  assert.deepEqual(res2.placed, ["S005"]);
  assert.equal(v1.video[1].items.length, v1Items, "v1 untouched");
  const v2 = seqByName(ppro, "ep_main_BROLL_v2");
  assert.equal(v2.video[1].items.length, v1Items + 1);
  assert.deepEqual(ppro.state.writes.slice(2), ["/p/ep_BROLL_v2.prproj", "/p/ep_BROLL_v2.prproj"]);
});

test("guards: saveAs onto the original is refused", async () => {
  const { ppro, ex } = setup();
  await assert.rejects(ex.run({ operations: [{ op: "saveProjectAs", path: PRPROJ, originalPath: PRPROJ }] }), /original/);
  assert.deepEqual(ppro.state.writes, []);
});
