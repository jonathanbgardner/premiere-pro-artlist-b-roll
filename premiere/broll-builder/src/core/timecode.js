"use strict";
/**
 * Timecode + tick helpers. PURE: no Premiere or UXP dependency.
 *
 * Mirrors pipeline/common/shotlist.py exactly (tc_to_frames, frames_to_tc,
 * tc_to_seconds, seconds_to_tc, _nominal, _true_rate). Parity is enforced by
 * tests/premiere/timecode.test.mjs, which compares against python3.
 *
 * Premiere time unit: 254016000000 ticks per second.
 */

const TICKS_PER_SECOND = 254016000000;
const TICKS_PER_SECOND_BIG = 254016000000n;

class TimecodeError extends Error {}

/** Python's round() for floats -> int (round half to even). */
function pyRound(x) {
  const f = Math.floor(x);
  const diff = x - f; // exact for |x| < 2^52
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

/** Python's floor division / modulo for integers. */
function floorDiv(a, b) {
  return Math.floor(a / b);
}
function pyMod(a, b) {
  return ((a % b) + b) % b;
}

/** shotlist._nominal */
function nominal(fps) {
  return pyRound(fps);
}

/** True if fps is an NTSC-style fractional rate (23.976, 29.97, 59.94...). */
function isFractional(fps) {
  return Math.abs(fps - nominal(fps)) > 0.001;
}

/** shotlist._true_rate: real frames per second. */
function trueRate(fps) {
  const nom = nominal(fps);
  if (isFractional(fps)) return (nom * 1000) / 1001;
  return nom;
}

/** Exact rational frame rate {num, den} matching trueRate(). */
function rationalRate(fps) {
  const nom = nominal(fps);
  return isFractional(fps) ? { num: nom * 1000, den: 1001 } : { num: nom, den: 1 };
}

const INT_RE = /^\d+$/;

/** shotlist.tc_to_frames. ';' before frames implies drop-frame unless dropFrame is given. */
function tcToFrames(tc, fps, dropFrame = null) {
  if (typeof tc !== "string") throw new TimecodeError(`bad timecode ${String(tc)}`);
  if (dropFrame === null || dropFrame === undefined) dropFrame = tc.includes(";");
  const parts = tc.replaceAll(";", ":").split(":");
  if (parts.length !== 4 || !parts.every((p) => INT_RE.test(p))) {
    throw new TimecodeError(`bad timecode '${tc}' for ${fps} fps`);
  }
  const [hh, mm, ss, ff] = parts.map((p) => parseInt(p, 10));
  const nom = nominal(fps);
  if (ff >= nom || ss >= 60 || mm >= 60) {
    throw new TimecodeError(`bad timecode '${tc}' for ${fps} fps`);
  }
  let frames = ((hh * 60 + mm) * 60 + ss) * nom + ff;
  if (dropFrame) {
    const drop = nom === 30 ? 2 : 4;
    const totalMin = hh * 60 + mm;
    frames -= drop * (totalMin - floorDiv(totalMin, 10));
  }
  return frames;
}

function pad2(n) {
  const s = String(n);
  return s.length < 2 ? "0" + s : s;
}

/** shotlist.frames_to_tc */
function framesToTc(frames, fps, dropFrame = false) {
  const nom = nominal(fps);
  if (dropFrame) {
    const drop = nom === 30 ? 2 : 4;
    const per10 = nom * 600 - drop * 9;
    const perMin = nom * 60 - drop;
    const d = floorDiv(frames, per10);
    const m = pyMod(frames, per10);
    if (m > drop) {
      frames += drop * 9 * d + drop * floorDiv(m - drop, perMin);
    } else {
      frames += drop * 9 * d;
    }
  }
  const ff = pyMod(frames, nom);
  const ss = pyMod(floorDiv(frames, nom), 60);
  const mm = pyMod(floorDiv(frames, nom * 60), 60);
  const hh = floorDiv(frames, nom * 3600);
  const sep = dropFrame ? ";" : ":";
  return `${pad2(hh)}:${pad2(mm)}:${pad2(ss)}${sep}${pad2(ff)}`;
}

/** shotlist.tc_to_seconds: real (wall-clock) seconds. */
function tcToSeconds(tc, fps, dropFrame = null) {
  return tcToFrames(tc, fps, dropFrame) / trueRate(fps);
}

/** shotlist.seconds_to_tc */
function secondsToTc(seconds, fps, dropFrame = false) {
  return framesToTc(Math.floor(seconds * trueRate(fps) + 1e-6), fps, dropFrame);
}

/** Duration in seconds -> whole frames (nearest, at least 1). */
function secondsToFrames(seconds, fps) {
  return Math.max(1, Math.round(seconds * trueRate(fps)));
}

/** Ticks for one frame, as BigInt (exact for 23.976/24/25/29.97/30/50/59.94/60...). */
function ticksPerFrameBig(fps) {
  const { num, den } = rationalRate(fps);
  return divRound(TICKS_PER_SECOND_BIG * BigInt(den), BigInt(num));
}

function divRound(a, b) {
  // a, b BigInt, b > 0, a >= 0
  const q = a / b;
  const r = a - q * b;
  return r * 2n >= b ? q + 1n : q;
}

/** Frame count -> Premiere ticks as a decimal string (exact, BigInt math). */
function framesToTicks(frames, fps) {
  if (!Number.isInteger(frames)) throw new TimecodeError(`frames must be an integer: ${frames}`);
  const { num, den } = rationalRate(fps);
  const neg = frames < 0;
  const t = divRound(BigInt(Math.abs(frames)) * TICKS_PER_SECOND_BIG * BigInt(den), BigInt(num));
  return (neg ? -t : t).toString();
}

/** Ticks (string|number|BigInt) -> nearest whole frame. */
function ticksToFrames(ticks, fps) {
  const { num, den } = rationalRate(fps);
  const t = BigInt(typeof ticks === "number" ? Math.round(ticks) : ticks);
  const neg = t < 0n;
  const f = divRound((neg ? -t : t) * BigInt(num), TICKS_PER_SECOND_BIG * BigInt(den));
  return Number(neg ? -f : f);
}

function secondsToTicks(seconds) {
  return String(Math.round(seconds * TICKS_PER_SECOND));
}

function ticksToSeconds(ticks) {
  return Number(BigInt(ticks)) / TICKS_PER_SECOND;
}

function addTicks(a, b) {
  return (BigInt(a) + BigInt(b)).toString();
}

function subTicks(a, b) {
  return (BigInt(a) - BigInt(b)).toString();
}

module.exports = {
  TICKS_PER_SECOND,
  TimecodeError,
  pyRound,
  nominal,
  isFractional,
  trueRate,
  rationalRate,
  tcToFrames,
  framesToTc,
  tcToSeconds,
  secondsToTc,
  secondsToFrames,
  ticksPerFrame: (fps) => ticksPerFrameBig(fps).toString(),
  framesToTicks,
  ticksToFrames,
  secondsToTicks,
  ticksToSeconds,
  addTicks,
  subTicks,
};
