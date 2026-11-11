"use strict";
/**
 * Minimal path helpers that work for both POSIX and Windows native paths.
 * PURE: UXP has no node:path, so we do it by hand.
 */

function sepOf(p) {
  // Windows drive path or UNC path -> backslash; otherwise slash.
  return /^[A-Za-z]:[\\/]/.test(p) || p.startsWith("\\\\") || (p.includes("\\") && !p.includes("/")) ? "\\" : "/";
}

function trimTrailing(p) {
  if (p.length > 1 && (p.endsWith("/") || p.endsWith("\\")) && !/^[A-Za-z]:[\\/]$/.test(p)) {
    return p.slice(0, -1);
  }
  return p;
}

/** Join a native base dir with a relative path that uses '/' (as in shot_list.json). */
function join(base, ...rels) {
  const sep = sepOf(base);
  let out = trimTrailing(base);
  for (const rel of rels) {
    if (!rel) continue;
    const parts = String(rel).split(/[\\/]+/).filter((x) => x && x !== ".");
    for (const part of parts) {
      if (part === "..") {
        const i = Math.max(out.lastIndexOf("/"), out.lastIndexOf("\\"));
        if (i > 0) out = out.slice(0, i);
      } else {
        out = out.endsWith(sep) ? out + part : out + sep + part;
      }
    }
  }
  return out;
}

function dirname(p) {
  const t = trimTrailing(p);
  const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
  if (i < 0) return ".";
  if (i === 0) return t[0];
  if (/^[A-Za-z]:$/.test(t.slice(0, i))) return t.slice(0, i + 1);
  return t.slice(0, i);
}

function basename(p) {
  const t = trimTrailing(p);
  const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
  return i < 0 ? t : t.slice(i + 1);
}

function stripExt(name) {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(0, i) : name;
}

function isAbsolute(p) {
  return p.startsWith("/") || /^[A-Za-z]:[\\/]/.test(p) || p.startsWith("\\\\");
}

/** Compare two native paths loosely (case-insensitive on Windows-style paths, slash-agnostic). */
function samePath(a, b) {
  if (!a || !b) return false;
  const norm = (p) => {
    let s = trimTrailing(String(p)).replace(/\\/g, "/");
    if (/^[A-Za-z]:\//.test(s)) s = s.toLowerCase();
    return s;
  };
  return norm(a) === norm(b);
}

module.exports = { sepOf, join, dirname, basename, stripExt, isAbsolute, samePath };
