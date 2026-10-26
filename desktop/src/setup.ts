// The first run on a computer without Vision: install it, keep it running, connect, and sign in to a
// subscription (Claude, ChatGPT through Codex, Grok). The steps run in the Rust side (lib.rs `setup_run`)
// and stream what they print back as `setup` events. The subscriptions page is also Settings' way in later.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { openUrl } from "@tauri-apps/plugin-opener";
import { icon } from "./icons";
import { Json, el } from "./render";

export interface SetupHost {
  /** Connect to the Vision now running here; false when it can't be found yet. */
  connectLocal: () => Promise<boolean>;
  /** Pair with another computer instead. */
  pairElsewhere: () => void;
  toast: (text: string) => void;
}

let host: SetupHost;
const runs = new Map<number, { line: (text: string) => void; done: (code: number) => void }>();
const early = new Map<number, Json[]>(); // events for a run that arrived before its id came back

export async function setupInit(h: SetupHost) {
  host = h;
  await listen<Json>("setup", (e) => {
    const ev = e.payload;
    const handler = runs.get(ev.run);
    if (!handler) { early.set(ev.run, [...(early.get(ev.run) ?? []), ev]); return; }
    deliver(handler, ev);
  });
}

function deliver(handler: { line: (t: string) => void; done: (c: number) => void }, ev: Json) {
  if (ev.done) { runs.delete(ev.run); handler.done(ev.code); }
  else handler.line(String(ev.line ?? "").replace(/\x1b\[[0-9;]*[A-Za-z]/g, ""));
}

/** Run a setup step to the end; each line it prints goes to `line`. Resolves with its exit code. */
export async function step(action: string, line: (text: string) => void, name?: string): Promise<number> {
  const run = await invoke<number>("setup_run", { action, name: name ?? null });
  return new Promise((resolve) => {
    const handler = { line, done: resolve };
    runs.set(run, handler);
    for (const ev of early.get(run) ?? []) deliver(handler, ev);
    early.delete(run);
  });
}

const page = () => document.getElementById("setup")!;

function show(card: HTMLElement) {
  const box = page();
  box.replaceChildren(card);
  box.hidden = false;
}

export function closeSetup() {
  page().hidden = true;
}

function brand(title: string, text: string): HTMLElement[] {
  const img = el("img", "setup-mark") as HTMLImageElement;
  img.src = "/vision.png";
  return [img, el("h1", "", title), el("p", "dim", text)];
}

/** What to show when there's no Vision to connect to: install it here, or connect to one elsewhere. */
export async function firstRun() {
  const here = await invoke<Json>("vision_here");
  if (here.installed) { install(true); return; }
  const card = el("div", "setup-card");
  card.append(...brand("Welcome to Vision",
    "An assistant that works on your computer: it chats, writes and runs code, and keeps agents going in the background. It runs here, on this computer, using an AI subscription you already have."));
  const go = el("button", "primary big", "Set up Vision on this computer");
  go.onclick = () => install(false);
  const other = el("button", "link", "Vision already runs on another computer? Connect to it");
  other.onclick = () => host.pairElsewhere();
  const fine = el("p", "dim small", here.os === "macos" ? "Needs about 200 MB. Nothing is installed outside your home folder." : "Needs about 200 MB and no admin password: it all goes in your home folder.");
  card.append(go, fine, other);
  show(card);
}

interface Step { label: string; state: "todo" | "run" | "ok" | "fail"; node?: HTMLElement }

/** Install Vision (unless `installed`), start it in the background, connect, then the subscriptions. */
async function install(installed: boolean) {
  const card = el("div", "setup-card");
  card.append(...brand(installed ? "Starting Vision" : "Setting up Vision", installed
    ? "Vision is installed here but isn't running. Starting it, and keeping it running in the background from now on."
    : "This takes a minute or two."));
  const steps: Step[] = [
    ...(installed ? [] : [{ label: "Download and install Vision", state: "todo" } as Step]),
    { label: "Keep it running in the background", state: "todo" },
    { label: "Connect", state: "todo" },
  ];
  const list = el("ol", "setup-steps");
  for (const s of steps) { s.node = el("li", ""); list.append(s.node); }
  const log = el("details", "setup-log") as HTMLDetailsElement;
  const out = el("pre", "");
  log.append(el("summary", "", "Show what it's doing"), out);
  const error = el("div", "reply-error");
  error.hidden = true;
  card.append(list, error, log);
  show(card);

  const paint = () => {
    for (const s of steps) {
      s.node!.className = `step-${s.state}`;
      s.node!.replaceChildren(s.state === "ok" ? icon("Check", 15) : s.state === "fail" ? icon("CircleAlert", 15) : el("span", s.state === "run" ? "state running" : "step-dot"),
        el("span", "", s.label));
    }
  };
  const line = (text: string) => {
    out.textContent += text + "\n";
    out.scrollTop = out.scrollHeight;
  };
  const fail = (s: Step, why: string) => {
    s.state = "fail";
    paint();
    error.hidden = false;
    error.replaceChildren(icon("CircleAlert", 15), el("span", "", why));
    log.open = true;
    const again = el("button", "primary", "Try again");
    again.onclick = () => install(installed);
    card.append(again);
  };
  paint();

  let i = 0;
  if (!installed) {
    const s = steps[i++];
    s.state = "run"; paint();
    if (await step("install", line).catch((e) => { line(String(e)); return -1; }) !== 0) {
      fail(s, "The installer stopped. What it said is below; fix that and try again.");
      return;
    }
    s.state = "ok"; paint();
  }
  const svc = steps[i++];
  svc.state = "run"; paint();
  if (await step("service", line).catch((e) => { line(String(e)); return -1; }) !== 0) {
    fail(svc, "Vision is installed but couldn't be set to run in the background. What it said is below.");
    return;
  }
  svc.state = "ok"; paint();
  const conn = steps[i];
  conn.state = "run"; paint();
  for (let tries = 0; tries < 60; tries++) {
    if ((await invoke<Json>("vision_here")).serving && (await host.connectLocal())) {
      conn.state = "ok"; paint();
      await subscriptions(true);
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  fail(conn, "Vision started but isn't answering yet. Give it a moment and try again.");
}

/**
 * The subscriptions Vision can use, each installed and signed in from here. `first`: part of the first run
 * (Continue goes on into the app); otherwise opened from Settings (Done closes it).
 */
export async function subscriptions(first = false) {
  const card = el("div", "setup-card wide");
  card.append(...brand(first ? "Vision is running" : "Subscriptions",
    "Vision works through an AI subscription you already pay for. Sign in to at least one; you can add others later in Settings."));
  const list = el("div", "subs");
  card.append(list);
  const foot = el("div", "setup-foot");
  const skip = el("button", "link", first ? "Skip for now" : "");
  const next = el("button", "primary", first ? "Continue" : "Done");
  skip.onclick = next.onclick = closeSetup;
  if (first) foot.append(skip);
  foot.append(next);
  card.append(foot);
  show(card);

  const busy = new Set<string>();
  const draw = async () => {
    let rows: Json[];
    try { rows = await invoke<Json[]>("provider_status"); } catch (e) {
      list.replaceChildren(el("div", "dim small", String(e)));
      return;
    }
    list.replaceChildren();
    for (const r of rows) {
      const row = el("div", "sub" + (r.signed_in ? " ready" : ""));
      const text = el("div", "sub-text");
      text.append(el("div", "sub-name", r.product), el("div", "dim small", r.note.replace(/^via /, "")));
      const status = el("div", "sub-status");
      if (busy.has(r.name)) {
        status.append(el("span", "state running"), el("span", "dim small", r.installed ? "Finish signing in in your browser…" : "Installing…"));
      } else if (r.signed_in) {
        status.append(el("span", "pill ready", "Ready"));
      } else {
        const act = async (action: string) => {
          busy.add(r.name); draw();
          const lines: string[] = [];
          const code = await step(action, (t) => lines.push(t), r.name).catch((e) => { lines.push(String(e)); return -1; });
          busy.delete(r.name);
          if (code !== 0) host.toast(`${r.product}: ${lines.filter(Boolean).slice(-1)[0] ?? "it didn't finish"}`);
          draw();
        };
        if (r.installed) {
          const b = el("button", "chip-btn bordered", "Sign in");
          b.onclick = () => act("provider_login");
          status.append(b);
        } else if (r.install_cmd) {
          const b = el("button", "chip-btn bordered", "Install");
          b.onclick = () => act("provider_install");
          status.append(b);
        } else if (r.install_url) {
          const b = el("button", "chip-btn bordered", "Get it");
          b.onclick = () => { openUrl(r.install_url); host.toast(`Install ${r.product}, then come back and sign in.`); };
          status.append(b);
        }
      }
      row.append(text, status);
      list.append(row);
    }
    next.classList.toggle("muted", first && !rows.some((r) => r.signed_in));
  };
  await draw();
}
