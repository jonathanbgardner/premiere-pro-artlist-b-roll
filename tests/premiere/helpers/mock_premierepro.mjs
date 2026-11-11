// In-memory mock of the `premierepro` UXP module, shaped after the 26.x API
// (classes/method names from @adobe/premierepro 26.5.1 typings). It enforces
// the 26.3+ rules: create*Action only inside project.lockedAccess(), and
// addAction only inside project.executeTransaction(). Every API call is logged
// in `ppro.calls` so tests can assert the call sequence.

const TPS = 254016000000n;

let guidCounter = 0;
const newGuid = () => ({ id: `guid-${++guidCounter}`, toString() { return this.id; } });

export function createMockPpro(config = {}) {
  const calls = [];
  const state = { lockDepth: 0, txDepth: 0, writes: [], project: null };
  const media = config.media || {}; // path -> { durationS, video, audio }
  const levelDefault = config.levelDefault ?? 0.17782794100389226; // 0 dB in "linear" units
  const features = { subclip: true, importToRoot: false, ...(config.features || {}) };

  const log = (name, detail) => calls.push(detail === undefined ? name : `${name} ${detail}`);
  const requireLock = (name) => {
    if (state.lockDepth === 0) throw new Error(`${name}: actions must be created inside lockedAccess`);
  };
  const action = (name, apply) => {
    requireLock(name);
    log(name);
    return { __action: name, apply };
  };

  // ------------------------------------------------------------ TickTime
  class TickTime {
    constructor(ticks) {
      this._t = BigInt(ticks);
    }
    get ticks() { return this._t.toString(); }
    get ticksNumber() { return Number(this._t); }
    get seconds() { return Number(this._t) / Number(TPS); }
    add(o) { return new TickTime(this._t + o._t); }
    subtract(o) { return new TickTime(this._t - o._t); }
    equals(o) { return this._t === o._t; }
    static createWithTicks(t) { if (typeof t !== "string") throw new Error("createWithTicks expects a string"); return new TickTime(t); }
    static createWithSeconds(s) { return new TickTime(Math.round(s * Number(TPS))); }
  }
  TickTime.TIME_ZERO = new TickTime(0);
  const T = (t) => (t instanceof TickTime ? t._t : BigInt(t));

  // ------------------------------------------------------------ project items
  class FolderItem {
    constructor(name) { this.name = name; this.items = []; this.isFolder = true; this.guid = newGuid(); }
    async getItems() { log("FolderItem.getItems", this.name); return [...this.items]; }
    createBinAction(name, makeUnique) {
      return action("FolderItem.createBinAction", () => {
        let n = name;
        if (makeUnique) { let i = 1; while (this.items.some((x) => x.name === n)) n = `${name} ${++i}`; }
        const f = new FolderItem(n); f.parent = this; this.items.push(f);
      });
    }
    createMoveItemAction(item, newParent) {
      return action("FolderItem.createMoveItemAction", () => {
        const from = item.parent || this;
        from.items = from.items.filter((x) => x !== item);
        newParent.items.push(item); item.parent = newParent;
      });
    }
  }

  class ClipItem {
    constructor(name, mediaPath, info, parent) {
      Object.assign(this, { name, mediaPath, info, parent, isClip: true, guid: newGuid() });
      this.inTicks = 0n;
      this.outTicks = BigInt(Math.round(info.durationS * Number(TPS)));
      this.takeVideo = !!info.video;
      this.takeAudio = !!info.audio;
    }
    async getMediaFilePath() { return this.mediaPath; }
    createSetInOutPointsAction(i, o) {
      return action("ClipProjectItem.createSetInOutPointsAction", () => { this.inTicks = T(i); this.outTicks = T(o); });
    }
    createSetNameAction(n) { return action("ProjectItem.createSetNameAction", () => { this.name = n; }); }
  }
  if (features.subclip) {
    ClipItem.prototype.createSubClipAction = function (name, start, end, hard, opts = {}) {
      return action("ClipProjectItem.createSubClipAction", () => {
        const sub = new ClipItem(name, this.mediaPath, this.info, this.parent);
        sub.inTicks = T(start); sub.outTicks = T(end); sub.isSubclip = true;
        sub.takeVideo = this.takeVideo && opts.takeVideo !== false;
        sub.takeAudio = this.takeAudio && opts.takeAudio !== false;
        this.parent.items.push(sub);
      });
    };
  }

  // ------------------------------------------------------------ track items / effects
  class Param {
    constructor(displayName, value) { this.displayName = displayName; this.value = value; this.timeVarying = false; this.keyframes = []; }
    async areKeyframesSupported() { return true; }
    async getValueAtTime() { return { value: this.value }; }
    createKeyframe(v) { return { value: { value: v }, position: TickTime.TIME_ZERO }; }
    createSetTimeVaryingAction(b) { return action("ComponentParam.createSetTimeVaryingAction", () => { this.timeVarying = b; }); }
    createAddKeyframeAction(kf) {
      return action("ComponentParam.createAddKeyframeAction", () => { this.keyframes.push({ ticks: kf.position.ticks, value: kf.value.value }); });
    }
  }
  class Component {
    constructor(name, match, params) { this.name = name; this.match = match; this.params = params; }
    async getDisplayName() { return this.name; }
    async getMatchName() { return this.match; }
    getParamCount() { requireLock("Component.getParamCount"); return this.params.length; }
    getParam(i) { return this.params[i]; }
  }
  class Chain {
    constructor(components) { this.components = components; }
    getComponentCount() { requireLock("ComponentChain.getComponentCount"); return this.components.length; }
    getComponentAtIndex(i) { return this.components[i]; }
  }

  class TrackItem {
    constructor(kind, projectItem, start, end, inPoint) {
      Object.assign(this, { kind, projectItem, start, end, inPoint, disabled: false });
      this.chain = kind === "audio"
        ? new Chain([
            new Component("Volume", "AudioVolume", [new Param("Bypass", false), new Param("Level", levelDefault)]),
            new Component("Channel Volume", "AudioChannelVolume", [new Param("Left", levelDefault)]),
            new Component("Panner", "AudioPanner", [new Param("Balance", 0)]),
          ])
        : new Chain([]);
    }
    async getStartTime() { return new TickTime(this.start); }
    async getEndTime() { return new TickTime(this.end); }
    async getInPoint() { return new TickTime(this.inPoint); }
    async getComponentChain() { return this.chain; }
    async getProjectItem() { return this.projectItem; }
    createSetEndAction(t) { return action(`${this.kind === "video" ? "Video" : "Audio"}ClipTrackItem.createSetEndAction`, () => { this.end = T(t); }); }
    createSetDisabledAction(b) { return action(`${this.kind === "video" ? "Video" : "Audio"}ClipTrackItem.createSetDisabledAction`, () => { this.disabled = b; }); }
  }

  class Track {
    constructor(kind) { this.kind = kind; this.items = []; }
    getTrackItems(type, includeEmpty) { log(`${this.kind === "video" ? "Video" : "Audio"}Track.getTrackItems`); return [...this.items].sort((a, b) => (a.start < b.start ? -1 : 1)); }
    overwrite(item) {
      // Overwrite edit: cut anything under [start, end) on this track.
      const out = [];
      for (const it of this.items) {
        if (it.end <= item.start || it.start >= item.end) out.push(it);
        else if (it.start < item.start) { it.end = item.start; out.push(it); }
        else if (it.end > item.end) { it.inPoint += item.end - it.start; it.start = item.end; out.push(it); }
      }
      out.push(item);
      this.items = out;
    }
  }

  // ------------------------------------------------------------ markers
  class Marker {
    constructor(name, type, start, duration, comments) { Object.assign(this, { name, type, start, duration, comments, colorIndex: 0, guid: newGuid() }); }
    getName() { return this.name; }
    getStart() { return new TickTime(this.start); }
    getDuration() { return new TickTime(this.duration); }
    getComments() { return this.comments; }
    getType() { return this.type; }
    getColorIndex() { return this.colorIndex; }
    createSetColorByIndexAction(i) { return action("Marker.createSetColorByIndexAction", () => { this.colorIndex = i; }); }
  }
  class MarkersObj {
    constructor(seq) { this.seq = seq; }
    getMarkers() { return [...this.seq.markers]; }
    createAddMarkerAction(name, type, start, duration, comments) {
      return action("Markers.createAddMarkerAction", () => { this.seq.markers.push(new Marker(name, type, T(start), T(duration), comments)); });
    }
  }

  // ------------------------------------------------------------ sequence
  class Sequence {
    constructor(name, opts = {}) {
      this.guid = newGuid();
      this.projectItem = { name, isSequenceItem: true, createSetNameAction: (n) => action("ProjectItem.createSetNameAction", () => { this.projectItem.name = n; }) };
      this.video = Array.from({ length: opts.videoTracks ?? 3 }, () => new Track("video"));
      this.audio = Array.from({ length: opts.audioTracks ?? 3 }, () => new Track("audio"));
      this.markers = [];
      this.zeroPoint = BigInt(opts.zeroPoint ?? 0);
    }
    get name() { return this.projectItem.name; }
    async getProjectItem() { return this.projectItem; }
    async getVideoTrackCount() { return this.video.length; }
    async getAudioTrackCount() { return this.audio.length; }
    async getVideoTrack(i) { return this.video[i] || null; }
    async getAudioTrack(i) { return this.audio[i] || null; }
    async getZeroPoint() { return new TickTime(this.zeroPoint); }
    createCloneAction() {
      return action("Sequence.createCloneAction", () => {
        const c = new Sequence(`${this.name} Copy`, { videoTracks: 0, audioTracks: 0, zeroPoint: this.zeroPoint });
        const copyTrack = (t) => { const n = new Track(t.kind); n.items = t.items.map((it) => Object.assign(Object.create(TrackItem.prototype), it)); return n; };
        c.video = this.video.map(copyTrack);
        c.audio = this.audio.map(copyTrack);
        c.markers = this.markers.map((m) => Object.assign(Object.create(Marker.prototype), m));
        state.project.sequences.push(c);
        state.project.root.items.push(c.projectItem);
      });
    }
  }

  // ------------------------------------------------------------ project
  class Project {
    constructor(path) {
      this.path = path;
      this.name = path.split(/[\\/]/).pop();
      this.root = new FolderItem("root");
      this.sequences = [];
      this.active = null;
      this.guid = newGuid();
    }
    async getSequences() { log("Project.getSequences"); return [...this.sequences]; }
    async getActiveSequence() { return this.active; }
    async setActiveSequence(s) { log("Project.setActiveSequence", s.name); this.active = s; return true; }
    async openSequence(s) { log("Project.openSequence", s.name); return true; }
    async getRootItem() { return this.root; }
    async importFiles(paths, suppressUI, targetBin, stills) {
      log("Project.importFiles", paths.join(","));
      for (const p of paths) {
        const info = media[p];
        if (!info) return false;
        const bin = features.importToRoot || !targetBin ? this.root : targetBin;
        bin.items.push(new ClipItem(p.split(/[\\/]/).pop(), p, info, bin));
      }
      return true;
    }
    async saveAs(p) { log("Project.saveAs", p); state.writes.push(p); this.path = p; this.name = p.split(/[\\/]/).pop(); if (config.onWrite) await config.onWrite(p); return true; }
    async save() { log("Project.save", this.path); state.writes.push(this.path); if (config.onWrite) await config.onWrite(this.path); return true; }
    lockedAccess(cb) {
      log("Project.lockedAccess");
      state.lockDepth++;
      try { const r = cb(); if (r && typeof r.then === "function") throw new Error("lockedAccess callback must not be async"); }
      finally { state.lockDepth--; }
    }
    executeTransaction(cb, undo) {
      log("Project.executeTransaction", undo);
      const list = [];
      state.txDepth++;
      try {
        const r = cb({ addAction: (a) => { if (state.txDepth === 0) throw new Error("addAction outside transaction"); list.push(a); return true; }, get empty() { return list.length === 0; } });
        if (r && typeof r.then === "function") throw new Error("executeTransaction callback must not be async");
      } finally { state.txDepth--; }
      for (const a of list) a.apply();
      return true;
    }
  }

  const project = new Project(config.projectPath || "/p/ep.prproj");
  state.project = project;
  for (const s of config.sequences || [{ name: "ep_main" }]) {
    const seq = new Sequence(s.name, s);
    if (s.items) for (const [kind, idx, start, end] of s.items) (kind === "video" ? seq.video : seq.audio)[idx].items.push(new TrackItem(kind, null, BigInt(start), BigInt(end), 0n));
    project.sequences.push(seq);
    project.root.items.push(seq.projectItem);
  }
  project.active = project.sequences[0];

  const SequenceEditor = {
    getEditor(seq) {
      log("SequenceEditor.getEditor", seq.name);
      return {
        createOverwriteItemAction(item, time, vIdx, aIdx) {
          return action("SequenceEditor.createOverwriteItemAction", () => {
            const start = T(time);
            const len = item.outTicks - item.inTicks;
            const place = (kind, tracks, idx) => {
              while (tracks.length <= idx) tracks.push(new Track(kind));
              tracks[idx].overwrite(new TrackItem(kind, item, start, start + len, item.inTicks));
            };
            if (item.takeVideo) place("video", seq.video, vIdx);
            if (item.takeAudio) place("audio", seq.audio, aIdx);
          });
        },
      };
    },
  };

  const ppro = {
    calls,
    state,
    project,
    TickTime,
    Project: { getActiveProject: async () => { log("Project.getActiveProject"); return project; }, open: async (p) => { log("Project.open", p); project.path = p; return project; } },
    SequenceEditor,
    Markers: { getMarkers: async (seq) => { log("Markers.getMarkers", seq.name); return new MarkersObj(seq); } },
    Marker: { MARKER_TYPE_COMMENT: "Comment" },
    FolderItem: { cast: (i) => (i && i.isFolder ? i : null) },
    ClipProjectItem: { cast: (i) => (i && i.isClip ? i : null) },
    ProjectItem: { cast: (i) => i },
    Constants: {
      TrackItemType: { EMPTY: 0, CLIP: 1, TRANSITION: 2, PREVIEW: 3, FEEDBACK: 4 },
      MarkerColor: { GREEN: 0, RED: 1, MAGNETA: 2, MAGENTA: 2, ORANGE: 3, YELLOW: 4, WHITE: 5, BLUE: 6, CYAN: 7 },
      MediaType: { ANY: "any", DATA: "data", VIDEO: "video", AUDIO: "audio" },
    },
  };
  return ppro;
}
