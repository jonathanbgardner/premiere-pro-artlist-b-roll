"use strict";
/**
 * Pure helpers for writing statuses back to shot_list.json.
 * The UXP side does the actual temp-write + rename (src/premiere/fsio.js).
 */

const SHOT_LIST_NAME = "shot_list.json";

/**
 * Mark the given shot IDs as placed (updated_by "C"). Every other field and
 * every other shot is preserved as-is. Returns { data, changed: [ids] }.
 * Input is not mutated.
 */
function applyPlaced(shotList, ids, updatedBy = "C") {
  const want = new Set(ids);
  const data = JSON.parse(JSON.stringify(shotList));
  const changed = [];
  for (const shot of data.shots || []) {
    if (want.has(shot.id) && shot.status !== "placed") {
      shot.status = "placed";
      shot.updated_by = updatedBy;
      changed.push(shot.id);
    }
  }
  return { data, changed };
}

/** Same layout as pipeline/common/shotlist.save(): indent=2, trailing newline, no ASCII escaping. */
function serialize(data) {
  return JSON.stringify(data, null, 2) + "\n";
}

module.exports = { SHOT_LIST_NAME, applyPlaced, serialize };
