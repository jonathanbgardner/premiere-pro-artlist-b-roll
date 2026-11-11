// Timecode parity: src/core/timecode.js must agree exactly with pipeline/common/shotlist.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { load, REPO } from "./helpers/load.mjs";

const tc = load("src/core/timecode.js");

const RATES = [23.976, 24, 25, 29.97, 30, 50, 59.94, 60];

function pad(n) {
  return String(n).padStart(2, "0");
}

// Deterministic PRNG so the table is stable.
function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}

function buildCases() {
  const rand = rng(12345);
  const tcCases = [];
  const frameCases = [];
  const secCases = [];
  for (const fps of RATES) {
    const nom = Math.round(fps);
    const dfOk = fps === 29.97 || fps === 59.94;
    const fixed = [
      "00:00:00:00", "00:00:00:01", "00:00:59:" + pad(nom - 1), "00:01:00:00", "00:01:00:02", "00:01:00:04",
      "00:09:59:" + pad(nom - 1), "00:10:00:00", "00:10:00:02", "00:59:59:" + pad(nom - 1), "01:00:00:00",
      "00:00:15:00", "00:00:31:15", "00:00:48:00", "00:01:05:20", "10:23:45:" + pad(Math.min(nom - 1, 12)), "23:59:59:" + pad(nom - 1),
    ];
    for (const t of fixed) {
      tcCases.push({ tc: t, fps, df: null });
      if (dfOk) tcCases.push({ tc: t.slice(0, 8) + ";" + t.slice(9), fps, df: null });
    }
    for (let i = 0; i < 150; i++) {
      const t = `${pad(Math.floor(rand() * 24))}:${pad(Math.floor(rand() * 60))}:${pad(Math.floor(rand() * 60))}`;
      const ff = pad(Math.floor(rand() * nom));
      tcCases.push({ tc: `${t}:${ff}`, fps, df: null });
      tcCases.push({ tc: `${t}:${ff}`, fps, df: false });
      if (dfOk) {
        tcCases.push({ tc: `${t};${ff}`, fps, df: null });
        tcCases.push({ tc: `${t}:${ff}`, fps, df: true }); // explicit flag overrides separator
      }
    }
    // invalid ones must fail on both sides
    for (const bad of [`00:00:00:${pad(nom)}`, "00:60:00:00", "00:00:60:00", "00:00:00", "aa:00:00:00", "00:00:00:00:00"]) {
      tcCases.push({ tc: bad, fps, df: null });
    }
    const maxF = nom * 3600 * 24;
    const frames = [0, 1, 2, 3, 4, 5, nom - 1, nom, nom * 60 - 1, nom * 60, nom * 60 + 1, nom * 60 + 2, nom * 60 + 4, nom * 600, nom * 600 - 18, 17982, 17983, 35964, 107892];
    for (let i = 0; i < 150; i++) frames.push(Math.floor(rand() * maxF));
    for (const f of frames) {
      frameCases.push({ frames: f, fps, df: false });
      if (dfOk) frameCases.push({ frames: f, fps, df: true });
    }
    const secs = [0, 0.5, 1, 4.0, 15, 31.5, 59.999, 60, 600, 3599.9, 3600];
    for (let i = 0; i < 80; i++) secs.push(Math.round(rand() * 86400 * 1000) / 1000);
    for (const s of secs) {
      secCases.push({ s, fps, df: false });
      if (dfOk) secCases.push({ s, fps, df: true });
    }
  }
  return { tcCases, frameCases, secCases };
}

const PY = String.raw`
import json, sys
sys.path.insert(0, sys.argv[1])
from pipeline.common.shotlist import tc_to_frames, frames_to_tc, tc_to_seconds, seconds_to_tc
cases = json.load(sys.stdin)
out = {"tc": [], "frames": [], "secs": []}
for c in cases["tcCases"]:
    try:
        out["tc"].append({"frames": tc_to_frames(c["tc"], c["fps"], c["df"]), "seconds": tc_to_seconds(c["tc"], c["fps"], c["df"])})
    except Exception as e:
        out["tc"].append({"error": type(e).__name__})
for c in cases["frameCases"]:
    out["frames"].append(frames_to_tc(c["frames"], c["fps"], c["df"]))
for c in cases["secCases"]:
    out["secs"].append(seconds_to_tc(c["s"], c["fps"], c["df"]))
json.dump(out, sys.stdout)
`;

test("timecode parity with pipeline/common/shotlist.py", () => {
  const cases = buildCases();
  const py = spawnSync("python3", ["-c", PY, REPO], { input: JSON.stringify(cases), encoding: "utf-8", maxBuffer: 64 * 1024 * 1024 });
  assert.equal(py.status, 0, py.stderr);
  const want = JSON.parse(py.stdout);

  let n = 0;
  cases.tcCases.forEach((c, i) => {
    const w = want.tc[i];
    if (w.error) {
      assert.throws(() => tc.tcToFrames(c.tc, c.fps, c.df), `expected error for ${c.tc} @ ${c.fps}`);
    } else {
      assert.equal(tc.tcToFrames(c.tc, c.fps, c.df), w.frames, `tcToFrames ${c.tc} @ ${c.fps} df=${c.df}`);
      // bit-identical float math
      assert.equal(tc.tcToSeconds(c.tc, c.fps, c.df), w.seconds, `tcToSeconds ${c.tc} @ ${c.fps} df=${c.df}`);
    }
    n++;
  });
  cases.frameCases.forEach((c, i) => {
    assert.equal(tc.framesToTc(c.frames, c.fps, c.df), want.frames[i], `framesToTc ${c.frames} @ ${c.fps} df=${c.df}`);
    n++;
  });
  cases.secCases.forEach((c, i) => {
    assert.equal(tc.secondsToTc(c.s, c.fps, c.df), want.secs[i], `secondsToTc ${c.s} @ ${c.fps} df=${c.df}`);
    n++;
  });
  assert.ok(n > 3000, `parity table too small (${n})`);
  console.log(`parity cases: ${n}`);
});

test("nominal() uses Python banker's rounding", () => {
  assert.equal(tc.nominal(23.976), 24);
  assert.equal(tc.nominal(29.97), 30);
  assert.equal(tc.nominal(59.94), 60);
  assert.equal(tc.nominal(24.5), 24);
  assert.equal(tc.nominal(25.5), 26);
});

test("ticks: 254016000000 per second, exact per-frame values", () => {
  assert.equal(tc.TICKS_PER_SECOND, 254016000000);
  assert.equal(tc.ticksPerFrame(29.97), "8475667200");
  assert.equal(tc.ticksPerFrame(23.976), "10594584000");
  assert.equal(tc.ticksPerFrame(59.94), "4237833600");
  assert.equal(tc.ticksPerFrame(25), "10160640000");
  assert.equal(tc.ticksPerFrame(30), "8467200000");
  // one hour of 29.97 NDF frames = 108000 frames = 3603.6 s
  assert.equal(tc.framesToTicks(108000, 29.97), String(108000n * 8475667200n));
  assert.equal(tc.ticksToSeconds(tc.framesToTicks(108000, 29.97)), 3603.6);
  // DF one hour is real-time one hour (within a frame)
  const dfHour = tc.tcToFrames("01:00:00;00", 29.97);
  assert.equal(dfHour, 107892);
  assert.ok(Math.abs(tc.ticksToSeconds(tc.framesToTicks(dfHour, 29.97)) - 3600) < 1 / 29.97);
  assert.equal(tc.ticksToFrames(tc.framesToTicks(12345, 23.976), 23.976), 12345);
  assert.equal(tc.secondsToTicks(1), "254016000000");
  // 10 h at 59.94 stays exact (beyond 2^53 ticks would lose precision as Number)
  const big = tc.framesToTicks(60 * 3600 * 10, 59.94);
  assert.equal(big, String(BigInt(60 * 3600 * 10) * 4237833600n));
});

test("durations snap to whole frames", () => {
  assert.equal(tc.secondsToFrames(4.0, 29.97), 120);
  assert.equal(tc.secondsToFrames(5.5, 29.97), 165);
  assert.equal(tc.secondsToFrames(3.0, 25), 75);
  assert.equal(tc.secondsToFrames(60, 23.976), 1439);
});
