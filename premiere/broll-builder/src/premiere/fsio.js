"use strict";
/**
 * File helpers over a Node-style promise fs. In UXP pass require("fs")
 * (paths without a scheme are treated as file:, needs localFileSystem
 * "fullAccess"); in tests pass node:fs/promises.
 */

const shotlistIo = require("../core/shotlist_io.js");

async function exists(fs, p) {
  try {
    await fs.lstat(p);
    return true;
  } catch (_) {
    return false;
  }
}

async function readText(fs, p) {
  return String(await fs.readFile(p, { encoding: "utf-8" }));
}

async function readJson(fs, p) {
  return JSON.parse(await readText(fs, p));
}

async function listDir(fs, p) {
  try {
    const entries = await fs.readdir(p);
    return entries.map((e) => (typeof e === "string" ? e : e.name));
  } catch (_) {
    return [];
  }
}

async function ensureDir(fs, p) {
  if (await exists(fs, p)) return;
  try {
    await fs.mkdir(p, { recursive: true });
  } catch (e) {
    if (!(await exists(fs, p))) throw e;
  }
}

/** Write to <path>.c-<stamp>.tmp in the same folder, then rename over the target. */
async function writeTextAtomic(fs, p, text) {
  const tmp = `${p}.c-${Date.now()}-${Math.floor(Math.random() * 1e6)}.tmp`;
  await fs.writeFile(tmp, text, { encoding: "utf-8" });
  try {
    await fs.rename(tmp, p);
  } catch (e) {
    // Rename-over-existing should work (libuv replaces), but be defensive.
    try {
      if (await exists(fs, p)) await fs.unlink(p);
      await fs.rename(tmp, p);
    } catch (e2) {
      try {
        await fs.unlink(tmp);
      } catch (_) {
        /* ignore */
      }
      throw e2;
    }
  }
}

async function writeJsonAtomic(fs, p, obj) {
  await writeTextAtomic(fs, p, shotlistIo.serialize(obj));
}

module.exports = { exists, readText, readJson, listDir, ensureDir, writeTextAtomic, writeJsonAtomic };
