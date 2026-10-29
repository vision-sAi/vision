// Screenshots of the window without a window: the built page in headless Chromium, with fake-tauri.js in
// place of the native side. `npm run shots` (or `node harness/shots.mjs chat:dark plan:light …`) writes
// harness/out/<scene>-<theme>[-<os>].png and prints each scene's checks.
//
// Needs a Chromium: $CHROME, else helium, chromium or google-chrome on the PATH. It runs in a scratch
// profile, so the browser you use day to day is never touched.

import { execFileSync, spawn, spawnSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { extname, join } from "node:path";

const here = new URL(".", import.meta.url).pathname;
const root = join(here, "..");
const out = join(here, "out");
const browser = process.env.CHROME || ["helium", "chromium", "chromium-browser", "google-chrome"].find((b) =>
  spawnSync("sh", ["-c", `command -v ${b}`]).status === 0);
if (!browser) throw new Error("no Chromium found: set CHROME");

execFileSync("npx", ["vite", "build", "--logLevel", "error"], { cwd: root, stdio: "inherit" });
const site = mkdtempSync(join(tmpdir(), "vision-shots-"));
cpSync(join(root, "dist"), site, { recursive: true });
cpSync(join(here, "fake-tauri.js"), join(site, "fake-tauri.js"));
cpSync(join(here, "scenes.js"), join(site, "scenes.js"));
// The window's own content security policy, as the app applies it: a picture or a script it would block
// is blocked here too (blob: pictures once showed only their captions in the app while every shot passed).
const csp = JSON.parse(readFileSync(join(root, "src-tauri/tauri.conf.json"), "utf8")).app?.security?.csp ?? "";
const page = readFileSync(join(site, "index.html"), "utf8")
  .replace("<head>", '<head>\n<script src="/scenes.js"></script>\n<script src="/fake-tauri.js"></script>'
    + (csp ? `\n<meta http-equiv="Content-Security-Policy" content="${csp.replace(/"/g, "&quot;")}">` : ""));
writeFileSync(join(site, "index.html"), page);

const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml" };
const server = createServer((req, res) => {
  const path = join(site, decodeURIComponent(new URL(req.url, "http://x").pathname));
  const file = existsSync(path) && !path.endsWith("/") ? path : join(site, "index.html");
  res.writeHead(200, { "content-type": types[extname(file)] ?? "application/octet-stream" });
  res.end(readFileSync(file));
}).listen(0, "127.0.0.1");
await new Promise((r) => server.on("listening", r));
const port = server.address().port;

const scenesSrc = readFileSync(join(here, "scenes.js"), "utf8");
const all = [...scenesSrc.matchAll(/^    "?([\w-]+)"?: \{/gm)].map((m) => m[1]);
const wanted = process.argv.slice(2).length ? process.argv.slice(2) : all.flatMap((s) => [`${s}:dark`, `${s}:light`]);

/** The browser, run without blocking: the page it loads comes from this process's own server. */
function run(args) {
  return new Promise((resolve) => {
    const child = spawn(browser, args, { stdio: ["ignore", "pipe", "ignore"] });
    let stdout = "";
    child.stdout.on("data", (d) => (stdout += d));
    const timer = setTimeout(() => child.kill(), 60000);
    child.on("close", () => { clearTimeout(timer); resolve(stdout); });
  });
}

mkdirSync(out, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), "vision-shots-profile-"));
let failed = 0;
for (const spec of wanted) {
  const [scene, theme = "dark", os = "linux"] = spec.split(":");
  const url = `http://127.0.0.1:${port}/?scene=${scene}&theme=${theme}&os=${os}`;
  const name = `${scene}-${theme}${os === "linux" ? "" : "-" + os}`;
  const common = [`--headless=new`, `--user-data-dir=${profile}`, "--disable-gpu", "--hide-scrollbars", "--no-first-run",
    "--window-size=1280,860", "--force-device-scale-factor=1", "--virtual-time-budget=6000"];
  await run([...common, `--screenshot=${join(out, name + ".png")}`, url]);
  const dom = await run([...common, "--dump-dom", url]);
  const results = (dom.match(/<pre id="harness-results"[^>]*>([\s\S]*?)<\/pre>/)?.[1] ?? "").trim();
  const lines = results.split("\n").filter((l) => l && l !== "DONE");
  failed += lines.filter((l) => l.startsWith("FAIL")).length;
  console.log(`${name}: ${results.includes("DONE") ? "ran" : "DID NOT FINISH"}${lines.length ? "\n  " + lines.join("\n  ") : ""}`);
}
server.close();
rmSync(profile, { recursive: true, force: true });
rmSync(site, { recursive: true, force: true });
console.log(`screenshots in ${out}`);
process.exit(failed ? 1 : 0);
