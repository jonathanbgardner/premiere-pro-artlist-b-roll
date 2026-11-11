"use strict";
/**
 * Applies a plan from src/core/plan.js through the Premiere Pro UXP API.
 * Thin on purpose: all decisions live in the pure plan; this file only maps
 * operations onto `require("premierepro")` calls.
 *
 * Rules followed (Premiere UXP 26.3+):
 *   - every create*Action() happens inside project.lockedAccess()
 *   - addAction() happens inside project.executeTransaction()
 *   - no await inside either callback; Actions never escape the lock
 *
 * The premierepro module is injected (`ppro`) so tests can pass a mock.
 */

const paths = require("../core/paths.js");
const audio = require("../core/audio.js");

const FALLBACK_MARKER_COLORS = { GREEN: 0, RED: 1, MAGENTA: 2, ORANGE: 3, YELLOW: 4, WHITE: 5, BLUE: 6, CYAN: 7 };

class ExecError extends Error {}

const defaultSleep = (ms) => new Promise((r) => setTimeout(r, ms));

function guidStr(obj) {
  try {
    return obj && obj.guid ? String(obj.guid.toString()) : null;
  } catch (_) {
    return null;
  }
}

function ticksOf(t) {
  if (t === null || t === undefined) return null;
  if (typeof t === "string" || typeof t === "number" || typeof t === "bigint") return BigInt(t);
  if (t.ticks !== undefined) return BigInt(t.ticks);
  return null;
}

class Executor {
  /**
   * @param {object} deps
   *   ppro     the premierepro module
   *   project  active Project
   *   log      (msg, level) => void
   *   sleep    ms => Promise (injectable for tests)
   *   pollTries / pollMs  polling for async-applied operations (import, subclip)
   */
  constructor({ ppro, project, log, sleep, pollTries, pollMs }) {
    this.ppro = ppro;
    this.project = project;
    this.log = log || (() => {});
    this.sleep = sleep || defaultSleep;
    this.pollTries = pollTries ?? 20;
    this.pollMs = pollMs ?? 150;
    this.sequence = null;
    this.originalGuid = null;
    this.bins = {};
    this.clips = {};
    this.result = { placed: [], failed: [], fallbacks: [], markersAdded: 0, saved: false, sequence: null, projectPath: null };
    this.failedShots = new Set();
  }

  // ---------------------------------------------------------------- helpers

  TT(ticks) {
    return this.ppro.TickTime.createWithTicks(String(ticks));
  }

  /** Create actions inside the lock, add them inside one undoable transaction. */
  tx(undo, buildActions) {
    let ok;
    let err = null;
    this.project.lockedAccess(() => {
      try {
        const actions = buildActions().filter(Boolean);
        ok = this.project.executeTransaction((compound) => {
          for (const a of actions) compound.addAction(a);
        }, undo);
      } catch (e) {
        err = e;
      }
    });
    if (err) throw err;
    if (ok === false) throw new ExecError(`transaction failed: ${undo}`);
    return true;
  }

  /** Read-only work that needs the lock (sync getters such as getComponentAtIndex). */
  locked(fn) {
    let out;
    let err = null;
    this.project.lockedAccess(() => {
      try {
        out = fn();
      } catch (e) {
        err = e;
      }
    });
    if (err) throw err;
    return out;
  }

  async poll(fn) {
    for (let i = 0; i < this.pollTries; i++) {
      const v = await fn();
      if (v) return v;
      await this.sleep(this.pollMs);
    }
    return null;
  }

  asFolder(item) {
    try {
      const f = this.ppro.FolderItem.cast(item);
      return f && typeof f.getItems === "function" ? f : null;
    } catch (_) {
      return null;
    }
  }

  asClip(item) {
    try {
      const c = this.ppro.ClipProjectItem.cast(item);
      return c && typeof c.getMediaFilePath === "function" ? c : null;
    } catch (_) {
      return null;
    }
  }

  async findSequence(name) {
    const seqs = await this.project.getSequences();
    return seqs.find((s) => s.name === name) || null;
  }

  async findClipByPath(folder, filePath, excludeName) {
    const items = await folder.getItems();
    for (const item of items) {
      if (excludeName && excludeName.test(item.name)) continue;
      const clip = this.asClip(item);
      if (!clip) continue;
      let mp = null;
      try {
        mp = await clip.getMediaFilePath();
      } catch (_) {
        continue;
      }
      if (paths.samePath(mp, filePath)) return { item, clip };
    }
    return null;
  }

  async findItemByName(folder, name) {
    const items = await folder.getItems();
    return items.find((i) => i.name === name) || null;
  }

  colorIndex(name) {
    const C = this.ppro.Constants && this.ppro.Constants.MarkerColor;
    if (C && typeof C[name] === "number") return C[name];
    return FALLBACK_MARKER_COLORS[name] ?? 0;
  }

  async findTrackItemAt(track, startTicks, halfFrame) {
    if (!track) return null;
    const items = await track.getTrackItems(this.ppro.Constants.TrackItemType.CLIP, false);
    const want = BigInt(startTicks);
    for (const it of items || []) {
      const st = ticksOf(await it.getStartTime());
      if (st !== null && (st > want ? st - want : want - st) <= halfFrame) return it;
    }
    return null;
  }

  // ---------------------------------------------------------------- ops

  async saveProjectAs(op) {
    if (paths.samePath(op.path, op.originalPath)) throw new ExecError("refusing to save over the original project");
    this.log(`Save As ${op.path} (original stays untouched)`);
    const ok = await this.project.saveAs(op.path);
    if (ok === false) throw new ExecError(`saveAs failed: ${op.path}`);
    // Per docs the handle now represents the copy; re-fetch to be safe.
    const active = await this.ppro.Project.getActiveProject();
    if (active) this.project = active;
    if (this.project.path && !paths.samePath(this.project.path, op.path)) {
      throw new ExecError(`active project is ${this.project.path}, expected ${op.path}; stopping so the original is not edited`);
    }
    this.result.projectPath = op.path;
  }

  async duplicateSequence(op) {
    const src = await this.findSequence(op.from);
    if (!src) throw new ExecError(`sequence "${op.from}" not found`);
    if (await this.findSequence(op.to)) throw new ExecError(`sequence "${op.to}" already exists`);
    const original = await this.findSequence(op.original);
    this.originalGuid = guidStr(original);
    const before = new Set((await this.project.getSequences()).map(guidStr));
    this.tx(`B-roll: duplicate ${op.from}`, () => [src.createCloneAction()]);
    const created = await this.poll(async () => {
      const now = await this.project.getSequences();
      return now.find((s) => !before.has(guidStr(s))) || null;
    });
    if (!created) throw new ExecError("sequence clone did not appear");
    const pi = await created.getProjectItem();
    this.tx(`B-roll: rename to ${op.to}`, () => [pi.createSetNameAction(op.to)]);
    this.sequence = created;
    if (this.originalGuid && guidStr(created) === this.originalGuid) throw new ExecError("clone resolved to the original sequence");
    try {
      await this.project.openSequence(created);
      await this.project.setActiveSequence(created);
    } catch (e) {
      this.log(`could not activate ${op.to}: ${e.message}`, "warn");
    }
    this.result.sequence = op.to;
    this.log(`Duplicated "${op.from}" -> "${op.to}"`);
  }

  async createBin(op) {
    const root = await this.project.getRootItem();
    const findBin = async () => {
      const items = await root.getItems();
      const item = items.find((i) => i.name === op.name && this.asFolder(i));
      return item ? { item, folder: this.asFolder(item) } : null;
    };
    let bin = await findBin();
    if (!bin) {
      this.tx(`B-roll: create bin ${op.name}`, () => [root.createBinAction(op.name, false)]);
      bin = await this.poll(findBin);
    }
    if (!bin) throw new ExecError(`could not create bin ${op.name}`);
    this.bins[op.name] = bin;
  }

  async importFile(op) {
    const bin = this.bins[op.bin];
    if (!bin) throw new ExecError(`bin ${op.bin} missing`);
    const sub = /_\d+f$/; // our subclips share the media path; skip them
    let found = await this.findClipByPath(bin.folder, op.path, sub);
    if (!found) {
      const ok = await this.project.importFiles([op.path], true, bin.item, false);
      if (ok === false) throw new ExecError(`import failed: ${op.path}`);
      found = await this.poll(() => this.findClipByPath(bin.folder, op.path, sub));
      if (!found) {
        // Some builds import into the root regardless of targetBin: move it.
        const root = await this.project.getRootItem();
        const inRoot = await this.findClipByPath(root, op.path, sub);
        if (inRoot) {
          this.tx(`B-roll: move ${op.shotId} to ${op.bin}`, () => [root.createMoveItemAction(inRoot.item, bin.folder)]);
          found = (await this.poll(() => this.findClipByPath(bin.folder, op.path, sub))) || inRoot;
        }
      }
    }
    if (!found) throw new ExecError(`imported item not found for ${op.path}`);
    this.clips[op.shotId] = found;
  }

  async makeSource(op, found, bin) {
    const { clip, item } = found;
    if (typeof clip.createSubClipAction === "function") {
      try {
        let sub = await this.findItemByName(bin.folder, op.subclipName);
        if (!sub) {
          this.tx(`B-roll: subclip ${op.subclipName}`, () => [
            clip.createSubClipAction(op.subclipName, this.TT("0"), this.TT(op.durationTicks), true, {
              takeVideo: op.takeVideo,
              takeAudio: op.takeAudio,
            }),
          ]);
          sub = await this.poll(async () => (await this.findItemByName(bin.folder, op.subclipName)) || (await this.findItemByName(await this.project.getRootItem(), op.subclipName)));
        }
        if (sub) return { source: sub, viaSubclip: true };
        throw new ExecError("subclip did not appear");
      } catch (e) {
        this.result.fallbacks.push({ shot_id: op.shotId, what: "subclip", detail: `subclip unavailable (${e.message}); used clip in/out instead` });
      }
    }
    this.tx(`B-roll: in/out ${op.shotId}`, () => [clip.createSetInOutPointsAction(this.TT("0"), this.TT(op.durationTicks))]);
    return { source: item, viaSubclip: false };
  }

  async placeClip(op, plan) {
    const seq = this.sequence;
    if (!seq) throw new ExecError("no target sequence");
    if (this.originalGuid && guidStr(seq) === this.originalGuid) throw new ExecError("refusing to edit the original sequence");
    const found = this.clips[op.shotId];
    if (!found) throw new ExecError(`${op.shotId} was not imported`);
    const bin = this.bins[op.bin];
    const halfFrame = BigInt(plan.ticksPerFrame) / 2n;
    const editor = this.ppro.SequenceEditor.getEditor(seq);

    const { source, viaSubclip } = await this.makeSource(op, found, bin);

    const vCount = await seq.getVideoTrackCount();
    const aCount = await seq.getAudioTrackCount();
    // The "other" media goes to a fresh track past the last one so it can never
    // overwrite dialogue (A1/A2) or V1. With subclips there is no other media.
    // Scratch indices are fixed once per run (first empty track past V3 / A4).
    if (this.scratchV === undefined) this.scratchV = Math.max(vCount, 3);
    if (this.scratchA === undefined) this.scratchA = Math.max(aCount, 4);
    const vIdx = op.mediaType === "video" ? op.trackIndex : this.scratchV;
    const aIdx = op.mediaType === "audio" ? op.trackIndex : this.scratchA;

    this.tx(`B-roll: place ${op.shotId} on ${op.trackLabel}`, () => [editor.createOverwriteItemAction(source, this.TT(op.startTicks), vIdx, aIdx)]);

    const getTrack = (kind, idx) => (kind === "video" ? seq.getVideoTrack(idx) : seq.getAudioTrack(idx));
    const ti = await this.poll(async () => this.findTrackItemAt(await getTrack(op.mediaType, op.trackIndex), op.startTicks, halfFrame));
    if (!ti) throw new ExecError(`${op.shotId}: placed item not found on ${op.trackLabel} at ${op.startTc}`);

    // Enforce the exact out point (duration_s) even if the source was longer.
    const end = ticksOf(await ti.getEndTime());
    if (end === null || end !== BigInt(op.endTicks)) {
      this.tx(`B-roll: trim ${op.shotId}`, () => [ti.createSetEndAction(this.TT(op.endTicks))]);
    }

    // Video B-roll is muted: disable any linked audio that came along.
    if (op.muteAudio) {
      const nowA = await seq.getAudioTrackCount();
      if (aIdx < nowA) {
        const at = await seq.getAudioTrack(aIdx);
        const linked = await this.findTrackItemAt(at, op.startTicks, halfFrame);
        if (linked) {
          this.tx(`B-roll: mute ${op.shotId} audio`, () => [linked.createSetDisabledAction(true), linked.createSetEndAction(this.TT(op.endTicks))]);
          this.result.fallbacks.push({ shot_id: op.shotId, what: "mute", detail: `linked audio disabled on A${aIdx + 1}` });
        }
      }
      if (!viaSubclip) this.log(`${op.shotId}: placed full clip with in/out; linked audio handled on A${aIdx + 1}`, "warn");
    }

    if (op.mediaType === "audio") {
      if (op.audio) {
        try {
          await this.applyEnvelope(ti, op.audio);
          this.log(`${op.shotId}: volume ${op.audio.gainDb} dB with ${op.audio.fadeInFrames}f/${op.audio.fadeOutFrames}f fades`);
        } catch (e) {
          this.result.fallbacks.push({ shot_id: op.shotId, what: "duck", detail: `volume keyframes failed (${e.message}); reminder marker added` });
          this.log(`${op.shotId}: volume keyframes failed (${e.message}); adding reminder marker`, "warn");
          if (op.fallbackMarker) await this.addMarker(op.fallbackMarker);
        }
      }
    }
    this.result.placed.push(op.shotId);
    this.log(`Placed ${op.shotId} on ${op.trackLabel} at ${op.startTc} (${op.durationFrames} frames)`);
  }

  async applyEnvelope(trackItem, env) {
    const chain = await trackItem.getComponentChain();
    if (!chain) throw new ExecError("no audio component chain");
    const comps = this.locked(() => {
      const n = chain.getComponentCount();
      const out = [];
      for (let i = 0; i < n; i++) out.push(chain.getComponentAtIndex(i));
      return out;
    });
    let volume = null;
    for (const c of comps) {
      const dn = String((await c.getDisplayName()) || "");
      const mn = String((await c.getMatchName()) || "");
      if ((/volume/i.test(dn) || /volume/i.test(mn)) && !/channel/i.test(dn + mn)) {
        volume = c;
        break;
      }
    }
    if (!volume) throw new ExecError("Volume component not found");
    const params = this.locked(() => {
      const n = volume.getParamCount();
      const out = [];
      for (let i = 0; i < n; i++) out.push(volume.getParam(i));
      return out;
    });
    const level = params.find((p) => /^level$/i.test(p.displayName)) || params.find((p) => /level/i.test(p.displayName));
    if (!level) throw new ExecError("Volume Level param not found");
    if (typeof level.areKeyframesSupported === "function" && !(await level.areKeyframesSupported())) {
      throw new ExecError("Level does not support keyframes");
    }
    const inPoint = await trackItem.getInPoint();
    const cur = await level.getValueAtTime(inPoint);
    const curVal = cur && typeof cur === "object" && "value" in cur ? cur.value : cur;
    const unit = audio.detectLevelUnit(curVal);
    if (!unit) throw new ExecError(`cannot tell Level units (default value ${curVal})`);
    this.tx("B-roll: volume keyframes on", () => [level.createSetTimeVaryingAction(true)]);
    this.tx("B-roll: duck + fades", () =>
      env.keyframes.map((k) => {
        const kf = level.createKeyframe(audio.levelValue(k.db, unit));
        kf.position = inPoint.add(this.TT(k.offsetTicks));
        return level.createAddKeyframeAction(kf);
      })
    );
  }

  async addMarker(op) {
    const seq = this.sequence;
    if (!seq) throw new ExecError("no target sequence");
    let name = op.name;
    let color = op.color;
    if (op.kind === "shot" && this.failedShots.has(op.shotId)) {
      name = `${op.shotId} PLACE FAILED`;
      color = "RED";
    }
    const want = BigInt(op.startTicks);
    const findIt = (markers) =>
      (markers.getMarkers() || []).find((m) => m.getName() === name && ticksOf(m.getStart()) === want) || null;
    const markers = await this.ppro.Markers.getMarkers(seq);
    if (findIt(markers)) {
      this.log(`marker "${name}" already there`);
      return;
    }
    const type = (this.ppro.Marker && this.ppro.Marker.MARKER_TYPE_COMMENT) || "Comment";
    this.tx(`B-roll: marker ${name}`, () => [markers.createAddMarkerAction(name, type, this.TT(op.startTicks), this.TT(op.durationTicks), op.comments)]);
    const added = await this.poll(async () => findIt(await this.ppro.Markers.getMarkers(seq)));
    if (added) {
      const idx = this.colorIndex(color);
      this.tx(`B-roll: marker color ${name}`, () => [added.createSetColorByIndexAction(idx)]);
    } else {
      this.log(`marker "${name}" added but not found to color it`, "warn");
    }
    this.result.markersAdded += 1;
  }

  async saveProject(op) {
    if (!this.result.projectPath || (this.project.path && !paths.samePath(this.project.path, op.path))) {
      throw new ExecError("refusing to save: active project is not the B-roll copy");
    }
    const ok = await this.project.save();
    if (ok === false) throw new ExecError("project save failed");
    this.result.saved = true;
    this.log(`Saved ${op.path}`);
  }

  // ---------------------------------------------------------------- run

  async run(plan) {
    for (const op of plan.operations) {
      switch (op.op) {
        case "saveProjectAs":
          await this.saveProjectAs(op);
          break;
        case "duplicateSequence":
          await this.duplicateSequence(op);
          break;
        case "createBin":
          await this.createBin(op);
          break;
        case "importFile":
          try {
            await this.importFile(op);
          } catch (e) {
            this.failedShots.add(op.shotId);
            this.result.failed.push({ shot_id: op.shotId, error: e.message });
            this.log(`${op.shotId}: ${e.message}`, "error");
          }
          break;
        case "placeClip":
          if (this.failedShots.has(op.shotId)) break;
          try {
            await this.placeClip(op, plan);
          } catch (e) {
            this.failedShots.add(op.shotId);
            this.result.failed.push({ shot_id: op.shotId, error: e.message });
            this.log(`${op.shotId}: ${e.message}`, "error");
          }
          break;
        case "addMarker":
          try {
            await this.addMarker(op);
          } catch (e) {
            this.log(`marker ${op.name}: ${e.message}`, "error");
            this.result.fallbacks.push({ shot_id: op.shotId, what: "marker", detail: e.message });
          }
          break;
        case "saveProject":
          await this.saveProject(op);
          break;
        default:
          throw new ExecError(`unknown op ${op.op}`);
      }
    }
    return this.result;
  }
}

async function executePlan(plan, deps) {
  const ex = new Executor(deps);
  return ex.run(plan);
}

module.exports = { Executor, executePlan, ExecError, FALLBACK_MARKER_COLORS };
