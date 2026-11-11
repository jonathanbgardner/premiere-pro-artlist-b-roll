// Entry point so `node --test tests/premiere/` works on Node 22: when given a
// directory, Node loads it as a module (this file), which imports every
// *.test.mjs here. `node --test tests/premiere/*.test.mjs` works as well.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

for (const f of fs.readdirSync(__dirname).sort()) {
  if (f.endsWith(".test.mjs")) import(pathToFileURL(path.join(__dirname, f)).href);
}
