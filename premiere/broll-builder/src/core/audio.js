"use strict";
/**
 * Volume "Level" parameter value mapping. PURE.
 *
 * ASSUMPTION (not documented by Adobe): the clip Volume > Level parameter is
 * stored either
 *   - "linear": Premiere's internal amplitude where value = 10^((dB - 15) / 20)
 *     (0 dB == 0.17783, the long-standing ExtendScript convention), or
 *   - "db": the dB number itself.
 * The executor reads the current (default 0 dB) value first and calls
 * detectLevelUnit(); if it cannot tell, it does NOT write keyframes and drops
 * a reminder marker instead.
 */

const LINEAR_0DB = Math.pow(10, -15 / 20); // 0.177827941...
const SILENCE_DB = -96;

function detectLevelUnit(currentValue) {
  const v = Number(currentValue);
  if (!Number.isFinite(v)) return null;
  if (Math.abs(v) < 1e-6) return "db";
  if (Math.abs(v - LINEAR_0DB) < 0.005) return "linear";
  return null;
}

/** db === null means silence (fade end points). */
function levelValue(db, unit) {
  if (unit === "linear") return db === null ? 0 : Math.pow(10, (db - 15) / 20);
  if (unit === "db") return db === null ? SILENCE_DB : db;
  throw new Error(`unknown level unit ${unit}`);
}

module.exports = { LINEAR_0DB, SILENCE_DB, detectLevelUnit, levelValue };
