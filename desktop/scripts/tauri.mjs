// Run the Tauri CLI with cargo on PATH: `npm run tauri dev`. Editors, GUI apps and launchd start
// without the shell profile that rustup edits, so look in rustup's own folder before giving up.
// `dev` runs as its own app (tauri.dev.conf.json), so it opens beside an installed Vision instead of
// focusing it, and keeps its own pairing and settings.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, join } from "node:path";

const exe = process.platform === "win32" ? "cargo.exe" : "cargo";
const onPath = (process.env.PATH ?? "").split(delimiter).some((dir) => dir && existsSync(join(dir, exe)));

if (!onPath) {
  const bin = join(process.env.CARGO_HOME ?? join(homedir(), ".cargo"), "bin");
  if (!existsSync(join(bin, exe))) {
    console.error("cargo not found. Install Rust from https://rustup.rs, then open a new terminal.");
    process.exit(1);
  }
  process.env.PATH = `${bin}${delimiter}${process.env.PATH ?? ""}`;
}

const cli = process.platform === "win32" ? "tauri.cmd" : "tauri";
const args = process.argv.slice(2);
if (args[0] === "dev") args.splice(1, 0, "--config", "src-tauri/tauri.dev.conf.json");
const run = spawnSync(cli, args, { stdio: "inherit", shell: process.platform === "win32" });
process.exit(run.status ?? 1);
