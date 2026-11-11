// "Done when": buildPlan on the hand-made 5-shot list puts everything at the right timecodes/ticks/tracks.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { spawnSync } from "node:child_process";
import { load, REPO, FIXTURE_5 } from "./helpers/load.mjs";

const { buildPlan, planSequenceNames, planProjectPath, PlanError, BINS } = load("src/core/plan.js");
const tc = load("src/core/timecode.js");

const ROOT = "/projects/test_episode";
const PRPROJ = "/projects/test_episode/test_episode.prproj";

/** Fixture with fake downloaded files. */
function fixtureWithFiles() {
  const data = JSON.parse(fs.readFileSync(FIXTURE_5, "utf-8"));
  const conf = { S001: 0.9, S002: 0.85, S003: 0.5, S004: 0.7, S005: 0.95 };
  for (const s of data.shots) {
    s.status = "downloaded";
    s.file = s.type === "music" ? `assets/${s.id}.wav` : `assets/${s.id}.mp4`;
    s.confidence = conf[s.id];
  }
  return data;
}

function baseOpts(extra = {}) {
  return {
    projectRoot: ROOT,
    projectPath: PRPROJ,
    existingProjectFiles: ["test_episode.prproj"],
    existingSequenceNames: ["test_episode_main"],
    ...extra,
  };
}

const places = (plan) => plan.operations.filter((o) => o.op === "placeClip");
const markers = (plan) => plan.operations.filter((o) => o.op === "addMarker");

function pythonSeconds(shots, fps) {
  const py = spawnSync(
    "python3",
    ["-c", "import json,sys; sys.path.insert(0, sys.argv[1]); from pipeline.common.shotlist import tc_to_seconds; d=json.load(sys.stdin); print(json.dumps([tc_to_seconds(t, d['fps']) for t in d['tcs']]))", REPO],
    { input: JSON.stringify({ fps, tcs: shots.map((s) => s.start_tc) }), encoding: "utf-8" }
  );
  assert.equal(py.status, 0, py.stderr);
  return JSON.parse(py.stdout);
}

test("5-shot fixture: every clip at the right timecode, ticks, track and length", () => {
  const data = fixtureWithFiles();
  const plan = buildPlan(data, baseOpts());
  assert.equal(plan.noop, false);
  assert.equal(plan.targetSequence, "test_episode_main_BROLL_v1");
  assert.equal(plan.cloneFrom, "test_episode_main");
  assert.equal(plan.projectPathOut, "/projects/test_episode/test_episode_BROLL_v1.prproj");

  const expected = {
    S001: { startTc: "00:00:00:00", startFrames: 0, durationFrames: 1798, track: "A3", trackIndex: 2, media: "audio", bin: BINS.music },
    S002: { startTc: "00:00:15:00", startFrames: 450, durationFrames: 120, track: "V2", trackIndex: 1, media: "video", bin: BINS.stock },
    S003: { startTc: "00:00:31:15", startFrames: 945, durationFrames: 165, track: "V2", trackIndex: 1, media: "video", bin: BINS.stock },
    S004: { startTc: "00:00:48:00", startFrames: 1440, durationFrames: 180, track: "V2", trackIndex: 1, media: "video", bin: BINS.ai },
    S005: { startTc: "00:01:05:20", startFrames: 1970, durationFrames: 90, track: "V2", trackIndex: 1, media: "video", bin: BINS.stock },
  };
  const pySec = pythonSeconds(data.shots, data.fps);
  const tpf = 8475667200n; // 29.97: 254016000000 * 1001 / 30000

  const ps = places(plan);
  assert.deepEqual(ps.map((p) => p.shotId), ["S001", "S002", "S003", "S004", "S005"]);
  for (const p of ps) {
    const e = expected[p.shotId];
    assert.equal(p.startTc, e.startTc, p.shotId);
    assert.equal(p.startFrames, e.startFrames, p.shotId);
    assert.equal(p.startTicks, String(BigInt(e.startFrames) * tpf), p.shotId);
    assert.equal(p.durationFrames, e.durationFrames, p.shotId);
    assert.equal(p.durationTicks, String(BigInt(e.durationFrames) * tpf), p.shotId);
    assert.equal(p.endTicks, String(BigInt(e.startFrames + e.durationFrames) * tpf), p.shotId);
    assert.equal(p.trackLabel, e.track, p.shotId);
    assert.equal(p.trackIndex, e.trackIndex, p.shotId);
    assert.equal(p.mediaType, e.media, p.shotId);
    assert.equal(p.bin, e.bin, p.shotId);
    assert.equal(p.path, `${ROOT}/${data.shots.find((s) => s.id === p.shotId).file}`);
    // start in real seconds agrees with Python's tc_to_seconds to within one tick
    const i = data.shots.findIndex((s) => s.id === p.shotId);
    assert.ok(Math.abs(tc.ticksToSeconds(p.startTicks) - pySec[i]) * tc.TICKS_PER_SECOND <= 1, `${p.shotId} seconds`);
    // duration within half a frame of duration_s
    const dur = data.shots[i].duration_s;
    assert.ok(Math.abs(tc.ticksToSeconds(p.durationTicks) - dur) <= 0.5 / 29.97, `${p.shotId} duration`);
    if (e.media === "video") {
      assert.equal(p.takeAudio, false, "video B-roll is placed without audio");
      assert.equal(p.muteAudio, true);
      assert.equal(p.audio, null);
    } else {
      assert.equal(p.takeVideo, false);
      assert.equal(p.audio.gainDb, -12);
      assert.ok(p.audio.fadeInFrames > 0 && p.audio.fadeOutFrames > 0);
      assert.equal(p.fallbackMarker.kind, "duck_reminder");
    }
  }

  // markers: one per placed clip with ID, reason, confidence; red when confidence < 0.6
  const ms = markers(plan).filter((m) => m.kind === "shot");
  assert.equal(ms.length, 5);
  for (const m of ms) {
    const shot = data.shots.find((s) => s.id === m.shotId);
    assert.ok(m.name.startsWith(shot.id));
    assert.ok(m.comments.includes(shot.reason));
    assert.ok(m.comments.includes(`confidence: ${shot.confidence.toFixed(2)}`));
    assert.equal(m.startTicks, places(plan).find((p) => p.shotId === m.shotId).startTicks);
    assert.equal(m.color, shot.confidence < 0.6 ? "RED" : "GREEN", m.shotId);
  }
  assert.deepEqual(plan.markPlaced, ["S001", "S002", "S003", "S004", "S005"]);
});

test("operation order: saveAs copy first, duplicate before edits, save last; original never a target", () => {
  const plan = buildPlan(fixtureWithFiles(), baseOpts());
  const ops = plan.operations.map((o) => o.op);
  assert.equal(ops[0], "saveProjectAs");
  assert.equal(ops[1], "duplicateSequence");
  assert.deepEqual(ops.slice(2, 5), ["createBin", "createBin", "createBin"]);
  assert.deepEqual(plan.operations.slice(2, 5).map((o) => o.name), ["Stock Video", "Music", "AI Generated"]);
  assert.equal(ops.at(-1), "saveProject");
  const firstEdit = ops.findIndex((o) => o === "placeClip" || o === "addMarker");
  assert.ok(ops.lastIndexOf("importFile") < firstEdit);
  assert.notEqual(plan.operations[0].path, PRPROJ);
  assert.notEqual(plan.operations[1].to, "test_episode_main");
  for (const o of plan.operations) assert.notEqual(o.path === PRPROJ && o.op.startsWith("save"), true);
});

test("version bumping: sequence and project file names never collide", () => {
  assert.deepEqual(planSequenceNames("ep", ["ep"]), { base: "ep", target: "ep_BROLL_v1", cloneFrom: "ep", version: 1 });
  assert.deepEqual(planSequenceNames("ep", ["ep", "ep_BROLL_v1", "ep_BROLL_v2"]), { base: "ep", target: "ep_BROLL_v3", cloneFrom: "ep_BROLL_v2", version: 3 });
  assert.equal(planSequenceNames("ep", ["ep", "ep_BROLL_v1"], "original").cloneFrom, "ep");
  assert.equal(planSequenceNames("ep_BROLL_v4", ["ep", "ep_BROLL_v4"]).target, "ep_BROLL_v5");
  assert.equal(planProjectPath("/a/ep.prproj", ["ep.prproj"]).path, "/a/ep_BROLL_v1.prproj");
  assert.equal(planProjectPath("/a/ep.prproj", ["ep.prproj", "ep_BROLL_v1.prproj", "EP_BROLL_V2.PRPROJ"]).path, "/a/ep_BROLL_v3.prproj");
  assert.equal(planProjectPath("/a/ep_BROLL_v1.prproj", ["ep.prproj", "ep_BROLL_v1.prproj"]).path, "/a/ep_BROLL_v2.prproj");
  assert.equal(planProjectPath("C:\\Edit\\ep.prproj", []).path, "C:\\Edit\\ep_BROLL_v1.prproj");
});

test("re-run: skips placed shots, clones latest B-roll sequence, noop when nothing new", () => {
  const data = fixtureWithFiles();
  for (const s of data.shots) if (s.id !== "S005") s.status = "placed";
  const plan = buildPlan(data, baseOpts({ existingSequenceNames: ["test_episode_main", "test_episode_main_BROLL_v1"], existingProjectFiles: ["test_episode.prproj", "test_episode_BROLL_v1.prproj"] }));
  assert.deepEqual(plan.markPlaced, ["S005"]);
  assert.equal(plan.cloneFrom, "test_episode_main_BROLL_v1");
  assert.equal(plan.targetSequence, "test_episode_main_BROLL_v2");
  assert.equal(plan.projectPathOut, "/projects/test_episode/test_episode_BROLL_v2.prproj");
  assert.equal(plan.skipped.filter((s) => s.reason === "already placed").length, 4);

  data.shots.find((s) => s.id === "S005").status = "placed";
  const again = buildPlan(data, baseOpts());
  assert.equal(again.noop, true);
  assert.deepEqual(again.operations, []);
});

test("failed / missing / no-file shots", () => {
  const data = fixtureWithFiles();
  const s2 = data.shots.find((s) => s.id === "S002");
  s2.status = "failed";
  s2.file = null;
  s2.error = "nothing fit";
  const s3 = data.shots.find((s) => s.id === "S003");
  s3.status = "failed"; // failed but with a file: placed, red
  s3.confidence = 0.9;
  const s4 = data.shots.find((s) => s.id === "S004");
  s4.status = "needs_ai";
  s4.file = null;
  const plan = buildPlan(data, baseOpts({ fileExists: (p) => !p.endsWith("S005.mp4") }));
  const ps = places(plan).map((p) => p.shotId);
  assert.deepEqual(ps, ["S001", "S003"]);
  const ms = markers(plan);
  const m2 = ms.filter((m) => m.shotId === "S002");
  assert.equal(m2.length, 1);
  assert.equal(m2[0].kind, "failed");
  assert.equal(m2[0].color, "RED");
  assert.ok(m2[0].comments.includes("nothing fit"));
  assert.equal(ms.find((m) => m.shotId === "S003" && m.kind === "shot").color, "RED");
  assert.equal(ms.find((m) => m.shotId === "S005").kind, "missing");
  assert.ok(!ms.some((m) => m.shotId === "S004"));
  assert.deepEqual(plan.markPlaced, ["S001", "S003"]);
  assert.ok(plan.skipped.some((s) => s.shot_id === "S004" && /no file/.test(s.reason)));
});

test("AI-generated stock shot goes to the AI Generated bin; ai_audio on A4", () => {
  const data = fixtureWithFiles();
  data.ai_budget.generated = ["S002"];
  data.shots.push({ id: "S006", start_tc: "00:01:20:00", duration_s: 3, type: "ai_audio", reason: "whoosh", queries: [], status: "downloaded", file: "assets/S006.wav", confidence: 0.8 });
  const plan = buildPlan(data, baseOpts());
  assert.equal(places(plan).find((p) => p.shotId === "S002").bin, BINS.ai);
  const s6 = places(plan).find((p) => p.shotId === "S006");
  assert.equal(s6.trackLabel, "A4");
  assert.equal(s6.bin, BINS.ai);
  assert.equal(s6.audio.gainDb, 0);
});

test("drop-frame 29.97 and 59.94, 23.976, 25 fps start ticks", () => {
  const mk = (fps, df, start_tc) => ({
    schema_version: 1, project: "p", fps, drop_frame: df, source_sequence: "seq",
    shots: [{ id: "S001", start_tc, duration_s: 4, type: "stock_video", reason: "r", queries: [], status: "downloaded", file: "assets/S001.mp4", confidence: 1 }],
    ai_budget: { max_clips: 0, approved: [] },
  });
  const cases = [
    [29.97, true, "00:10:00;00", 17982, "8475667200"],
    [29.97, true, "00:01:00;02", 1800, "8475667200"],
    [59.94, true, "00:01:00;04", 3600, "4237833600"],
    [23.976, false, "00:01:00:00", 1440, "10594584000"],
    [25, false, "00:00:10:12", 262, "10160640000"],
  ];
  for (const [fps, df, t, frames, tpf] of cases) {
    const p = places(buildPlan(mk(fps, df, t), baseOpts({ existingSequenceNames: ["seq"] })))[0];
    assert.equal(p.startFrames, frames, `${t} @ ${fps}`);
    assert.equal(p.startTicks, String(BigInt(frames) * BigInt(tpf)), `${t} @ ${fps}`);
    assert.equal(p.startTc, t);
  }
});

test("sequence zero point: auto-subtract only when every TC is past it", () => {
  const data = fixtureWithFiles();
  for (const s of data.shots) s.start_tc = "01" + s.start_tc.slice(2);
  const zp = tc.framesToTicks(tc.tcToFrames("01:00:00:00", 29.97), 29.97);
  const plan = buildPlan(data, baseOpts({ zeroPointTicks: zp }));
  assert.equal(places(plan).find((p) => p.shotId === "S002").startTc, "00:00:15:00");
  const fixed = buildPlan(fixtureWithFiles(), baseOpts({ zeroPointTicks: zp }));
  assert.equal(places(fixed).find((p) => p.shotId === "S002").startTc, "00:00:15:00");
  assert.equal(fixed.originFrames, 0);
});

test("duck_mode marker emits the reminder marker up front", () => {
  const plan = buildPlan(fixtureWithFiles(), baseOpts({ duckMode: "marker" }));
  const s1 = places(plan).find((p) => p.shotId === "S001");
  assert.equal(s1.audio, null);
  assert.ok(markers(plan).some((m) => m.shotId === "S001" && m.kind === "duck_reminder"));
});

test("music envelope: silence -> -12 dB -> -12 dB -> silence", () => {
  const p = places(buildPlan(fixtureWithFiles(), baseOpts()))[0];
  const kf = p.audio.keyframes;
  assert.deepEqual(kf.map((k) => k.db), [null, -12, -12, null]);
  assert.deepEqual(kf.map((k) => k.offsetFrames), [0, 30, 1797 - 60, 1797]);
});

test("errors: missing source sequence, unsaved project, bad schema", () => {
  assert.throws(() => buildPlan(fixtureWithFiles(), baseOpts({ existingSequenceNames: ["other"] })), PlanError);
  assert.throws(() => buildPlan(fixtureWithFiles(), baseOpts({ projectPath: "" })), PlanError);
  assert.throws(() => buildPlan({ ...fixtureWithFiles(), schema_version: 2 }, baseOpts()), PlanError);
});
