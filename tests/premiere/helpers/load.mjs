// Shared loader: the plugin sources are CommonJS (UXP), tests are ESM.
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
export const REPO = path.resolve(here, "..", "..", "..");
export const PLUGIN = path.join(REPO, "premiere", "broll-builder");
export const FIXTURE_5 = path.join(REPO, "tests", "fixtures", "shot_list_5.json");

const require = createRequire(import.meta.url);
export const load = (rel) => require(path.join(PLUGIN, rel));
