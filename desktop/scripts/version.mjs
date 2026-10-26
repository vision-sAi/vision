// Set the desktop app's version everywhere it is written: `node scripts/version.mjs 0.2.0`.
// The release workflow builds a `desktop-v<version>` tag only when these all say <version>; with no
// argument this prints the version and fails if they disagree.

import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const root = join(new URL(".", import.meta.url).pathname, "..");
const files = {
  tauri: join(root, "src-tauri/tauri.conf.json"),
  npm: join(root, "package.json"),
  cargo: join(root, "src-tauri/Cargo.toml"),
};
const read = () => ({
  tauri: JSON.parse(readFileSync(files.tauri, "utf8")).version,
  npm: JSON.parse(readFileSync(files.npm, "utf8")).version,
  cargo: readFileSync(files.cargo, "utf8").match(/^version = "([^"]+)"/m)?.[1],
});

const wanted = process.argv[2];
if (!wanted) {
  const v = read();
  console.log(v.tauri);
  if (v.tauri !== v.npm || v.tauri !== v.cargo) {
    console.error(`versions disagree: tauri.conf.json ${v.tauri}, package.json ${v.npm}, Cargo.toml ${v.cargo}`);
    process.exit(1);
  }
  process.exit(0);
}
if (!/^\d+\.\d+\.\d+$/.test(wanted)) throw new Error(`not a version: ${wanted}`);
for (const key of ["tauri", "npm"]) {
  const json = JSON.parse(readFileSync(files[key], "utf8"));
  json.version = wanted;
  writeFileSync(files[key], JSON.stringify(json, null, 2) + "\n");
}
writeFileSync(files.cargo, readFileSync(files.cargo, "utf8").replace(/^version = "[^"]+"/m, `version = "${wanted}"`));
console.log(`desktop app is now ${wanted}: commit, then tag desktop-v${wanted} and push the tag`);
