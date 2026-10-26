// Vision's desktop window: a client for `vision serve`, like the phone app. The socket and HTTP calls
// live in the Rust side (src-tauri/src/lib.rs); this page keeps the chats and draws them (render.ts).

import { invoke } from "@tauri-apps/api/core";
import { getVersion } from "@tauri-apps/api/app";
import { listen } from "@tauri-apps/api/event";
import { isPermissionGranted, requestPermission, sendNotification } from "@tauri-apps/plugin-notification";
import { openUrl } from "@tauri-apps/plugin-opener";
import { relaunch } from "@tauri-apps/plugin-process";
import { Update, check } from "@tauri-apps/plugin-updater";
import * as agents from "./agents";
import * as scouts from "./scouts";
import { buddy } from "./buddy";
import { IconName, icon, placeIcons } from "./icons";
import { closeSetup, firstRun, setupInit, step, subscriptions } from "./setup";
import {
  Agent, Json, Msg, agentOf, drawMsg, el, markdown, planOf, setPictureLoader, setPictureViewer, splitAttachments, toolOf,
  updateAgent, updateTool,
} from "./render";

interface Question { question: string; header?: string; multiSelect?: boolean; options: { label: string; description?: string }[] }
/** A new chat not yet on the server: it is opened there when its first message goes. */
interface Draft { workdir: string; folder: string; model?: string; effort?: string; modelLabel: string; mode: string }
interface Attachment { id: number; name: string; path?: string; error?: string; preview?: string }

const chats = new Map<string, Json>();          // chat id → its latest summary (`chat` frame)
const transcripts = new Map<string, Msg[]>();   // chat id → what the window shows
const questions = new Map<string, Question[]>(); // chat id → the form waiting for an answer
let projects: Json[] = [];
let view: "chat" | "agents" | "scout" = "chat"; // "scout": scouts.current's page, around its project chat
let active: string | null = null;
let draft: Draft | null = null;
let machine = "";
let readAloudOn = false;                        // hello.read_aloud: POST /speak/pieces is there
let shellOn = false;                            // hello.shell: the computer runs `!` commands
let voiceOn = false;                            // hello.voice_on, then `voice_power` frames
let serverVersion = "";
let updateHow: string | null = null;            // hello.update: what to run, when a newer Vision is out
let serverProtocol: number | null = null;
const PROTOCOL = 1;                             // vision/server.py PROTOCOL this app speaks
let system: Json = {};                          // the system's appearance (lib.rs `appearance`)
let wantNew = false;                            // a `new` went out: switch to the chat it opens
let pendingSend: { text: string; images: string[]; mode: string } | null = null; // the draft's first message
let paired: { url: string; name: string } | null = null; // the computer this window talks to
let repairedLocal = false;                      // the local key was refreshed once after a refusal
let attachments: Attachment[] = [];
let nextAttachment = 1;

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

// ---------------------------------------------------------------- talking to Vision

const send = (frame: Json) => invoke("send", { frame }).catch((e) => toast(String(e)));
const http = (method: string, path: string, query?: Json, body?: Json) =>
  invoke<any>("http", { method, path, query: query ?? null, body: body ?? null });

function row(r: Json): Msg {
  if (r.role === "shell") {
    return { ...blank("shell", r.text ?? ""), shellRunning: !!r.running, shellOutput: r.output ?? "", shellCode: r.running ? undefined : r.code ?? null };
  }
  if (r.role === "checkin") return { ...blank("note", r.text ?? ""), checkin: r, durationMs: r.duration_ms };
  const role = r.role === "user" || r.role === "assistant" ? r.role : "note";
  const sent = role === "user" ? splitAttachments(r.text ?? "") : { text: r.text ?? "", images: [] };
  return {
    role, text: sent.text, images: sent.images, durationMs: r.duration_ms, error: r.error || undefined,
    tools: Array.isArray(r.tools) ? r.tools.map(toolOf) : [],
    agents: Array.isArray(r.agents) ? r.agents.map(agentOf) : [],
  };
}

async function loadHistory(chat: string) {
  try {
    const rows: Json[] = await http("GET", "history", { chat, limit: "200" });
    const list = rows.map(row);
    const c = chats.get(chat);
    if (c?.busy) list.push(...liveFrom(c));
    transcripts.set(chat, list);
    if (chat === active) drawTranscript(true);
  } catch (e) {
    toast(`Couldn't load that chat: ${e}`);
  }
}

/** A turn already running when the window joined: its question and the reply so far. */
function liveFrom(c: Json): Msg[] {
  const out: Msg[] = [];
  if (c.user_text) out.push({ role: "user", text: c.user_text, tools: [], agents: [] });
  out.push({
    role: "assistant", text: c.partial ?? "", streaming: true, status: "Working…",
    tools: (c.tools ?? []).map(toolOf), agents: (c.agents ?? []).map(agentOf),
  });
  return out;
}

function streamingMsg(chat: string): Msg | undefined {
  const list = transcripts.get(chat);
  if (!list) return undefined;
  for (let i = list.length - 1; i >= 0; i--) if (list[i].streaming) return list[i];
  return undefined;
}

const blank = (role: Msg["role"], text = ""): Msg => ({ role, text, tools: [], agents: [] });

function handle(ev: Json) {
  const kind = ev.type as string;
  const chat = ev.chat as string | undefined;
  switch (kind) {
    case "hello": {
      machine = ev.name ?? "";
      readAloudOn = !!ev.read_aloud;
      shellOn = !!ev.shell;
      voiceOn = !!ev.voice_on;
      serverVersion = ev.version ?? "";
      updateHow = ev.update ?? null;
      serverProtocol = typeof ev.protocol === "number" ? ev.protocol : null;
      drawNotice();
      projects = ev.projects ?? [];
      const seen = new Set<string>();
      for (const c of ev.chats ?? []) { chats.set(c.chat, c); seen.add(c.chat); }
      for (const id of [...chats.keys()]) if (!seen.has(id)) { chats.delete(id); transcripts.delete(id); }
      for (const c of ev.chats ?? []) {
        if (c.waiting && c.questions?.length) questions.set(c.chat, c.questions); else questions.delete(c.chat);
      }
      agents.setAsleep(ev.asleep ?? []);
      agents.loadAll();
      if (view === "scout" && !scouts.project(scouts.current)) view = "chat";
      if (active && !chats.has(active)) active = null;
      if (view === "scout" && scouts.current && !active) { openScout(scouts.current); return; }
      if (!active && !draft) { active = newestChat(); if (!active) startDraft(); }
      if (active) loadHistory(active);
      drawAll();
      return;
    }
    case "chat": {
      chats.set(ev.chat, ev);
      if (ev.waiting && ev.questions?.length) questions.set(ev.chat, ev.questions);
      if (ev.opened && wantNew) { wantNew = false; opened(ev.chat); return; }
      drawSidebar();
      if (ev.chat === active) { drawHeader(); drawQueued(); drawQuestion(); drawDock(); }
      return;
    }
    case "chat_closed":
      chats.delete(ev.chat); transcripts.delete(ev.chat); questions.delete(ev.chat);
      if (active === ev.chat) { active = newestChat(); if (active) loadHistory(active); else startDraft(); }
      drawAll();
      return;
    case "project": {
      const p = ev.project;
      projects = [...projects.filter((x) => x.id !== p.id), p];
      drawSidebar();
      if (view === "scout" && p.id === scouts.current) drawScout();
      return;
    }
    case "activity":
      scouts.onActivity(ev.project, ev.event ?? {});
      return;
    case "approval":
      scouts.onApproval(ev.project, ev.approval ?? {});
      if (ev.approval?.state === "pending" && !document.hasFocus()) {
        const p = scouts.project(ev.project);
        if (p) notify(`${scouts.nameOf(p)} asks to run`, String(ev.approval.command ?? "").slice(0, 160));
      }
      return;
    case "checkin":
      scouts.onCheckin(ev);
      if (ev.state === "done" && !document.hasFocus()) {
        const p = scouts.project(ev.project);
        if (p) notify(`${scouts.nameOf(p)} checked in`, ev.needs_you ? `${ev.needs_you} need${ev.needs_you === 1 ? "s" : ""} you` : String(ev.summary ?? "").split("\n")[0].slice(0, 160));
      }
      return;
    case "thread":
      agents.onThread(ev.project, ev.thread);
      return;
    case "scheduled":
      agents.onScheduled();
      return;
    case "slept":
      agents.onSlept({ from: ev.from, to: ev.to });
      scouts.onSlept();
      return;
    case "project_removed":
      projects = projects.filter((x) => x.id !== ev.project);
      if (view === "scout" && scouts.current === ev.project) { view = "chat"; scouts.setCurrent(null); drawAll(); }
      else drawSidebar();
      return;
    case "update":
      updateHow = ev.update ?? null;
      drawNotice();
      return;
    case "voice_power":
      voiceOn = !!ev.on;
      if (!$("settings").hidden) openSettings();
      return;
    case "error":
    case "toast":
      toast(ev.text ?? "");
      return;
  }
  if (!chat) return;
  const list = transcripts.get(chat);
  switch (kind) {
    case "start": {
      const c = chats.get(chat);
      if (c) { c.busy = true; c.suggestion = ""; }
      if (!list) break;
      if (ev.note) list.push(blank("note", ev.note));
      else {
        const last = [...list].reverse().find((m) => m.role !== "note");
        const sent = splitAttachments(ev.text ?? "");
        if (!(last?.role === "user" && sameTurn(last.text, sent.text))) list.push({ ...blank("user", sent.text), images: sent.images });
      }
      list.push({ ...blank("assistant"), streaming: true, status: "Thinking…" });
      break;
    }
    case "delta": { const m = streamingMsg(chat); if (m) { m.text += ev.text ?? ""; m.status = "Writing…"; } break; }
    case "steered": {
      const m = streamingMsg(chat);
      if (!list || !m) break;
      m.streaming = false; m.status = "";
      list.push(blank("user", ev.text ?? ""));
      list.push({ ...blank("assistant"), streaming: true, status: "Working…" });
      break;
    }
    case "status": { const m = streamingMsg(chat); if (m) m.status = ev.label || (ev.tool ? `Using ${ev.tool}…` : "Working…"); break; }
    case "tool": {
      const m = streamingMsg(chat);
      if (!m) break;
      const t = m.tools.find((x) => x.id === ev.id);
      if (t) updateTool(t, ev); else m.tools.push(toolOf(ev));
      break;
    }
    case "agent": {
      const m = streamingMsg(chat);
      if (!m) break;
      const a: Agent | undefined = m.agents.find((x) => x.id === ev.id);
      if (a) updateAgent(a, ev); else m.agents.push(agentOf(ev));
      break;
    }
    case "question":
      questions.set(chat, ev.questions ?? []);
      if (!document.hasFocus()) notify(chats.get(chat)?.title || "Vision", `Asks: ${(ev.questions?.[0]?.question ?? "").split("\n")[0]}`);
      if (chat === active) drawQuestion();
      return;
    case "answered":
      questions.delete(chat);
      { const m = streamingMsg(chat); if (m) m.status = "Working…"; }
      if (chat === active) drawQuestion();
      break;
    case "done": {
      const m = streamingMsg(chat);
      if (m) {
        m.streaming = false; m.status = "";
        if (!m.text && ev.text) m.text = ev.text;
        m.error = ev.error || undefined;
        m.durationMs = ev.duration_ms;
      }
      const c = chats.get(chat);
      if (c) c.busy = !!ev.busy;
      questions.delete(chat);
      if (!document.hasFocus() && !ev.busy) {
        const said = String(ev.text ?? "").replace(/[#*`>_\[\]]/g, "").split("\n").find((l) => l.trim()) ?? "";
        notify(c?.title || "Vision", ev.error ? `Stopped: ${ev.error}` : said.slice(0, 160) || "Replied");
      }
      if (!ev.busy) loadHistory(chat);  // the server's transcript is the one to keep (it times each part)
      drawSidebar();
      if (chat === active) { drawQuestion(); drawDock(); }
      break;
    }
    case "note":
      list?.push(blank("note", ev.text ?? ""));
      break;
    case "shell": {
      // A `!` command: a frame when it starts, then one with what it printed when it ends.
      if (!list) break;
      const running = [...list].reverse().find((m) => m.role === "shell" && m.shellRunning && m.text === ev.text);
      if (ev.running) list.push({ ...blank("shell", ev.text ?? ""), shellRunning: true });
      else if (running) Object.assign(running, { shellRunning: false, shellOutput: ev.output ?? "", shellCode: ev.code ?? null });
      else list.push({ ...blank("shell", ev.text ?? ""), shellOutput: ev.output ?? "", shellCode: ev.code ?? null });
      break;
    }
    case "shell_output": {
      const running = [...(list ?? [])].reverse().find((m) => m.role === "shell" && m.shellRunning && m.text === ev.text);
      if (running) running.shellOutput = ev.output ?? "";
      break;
    }
    case "suggestion":
      { const c = chats.get(chat); if (c) c.suggestion = ev.text ?? ""; }
      if (chat === active) drawDock();
      return;
    default:
      return;
  }
  if (chat === active) scheduleDraw();
}

/** The server adds attachment paths to the text it echoes back: the same turn if the typed part matches. */
function sameTurn(sent: string, echoed: string | undefined) {
  return !!echoed && (sent === echoed || echoed.startsWith(sent));
}

function newestChat(): string | null {
  const scoutChats = new Set(scouts.live().map((p) => p.chat));
  const all = [...chats.values()].filter((c) => !scoutChats.has(c.chat)).sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
  return all[0]?.chat ?? null;
}

function select(chat: string) {
  active = chat;
  draft = null;
  view = "chat";
  scouts.setCurrent(null);
  drawAll();
  loadHistory(chat);
  $("input").focus();
}

// ---------------------------------------------------------------- new chats

/** A new chat, kept here until its first message: the folder and model are picked first, as in the phone app. */
async function startDraft() {
  view = "chat";
  const keepFolder = draft?.workdir ? draft : null;
  draft = { workdir: keepFolder?.workdir ?? "", folder: keepFolder?.folder ?? "", modelLabel: "", mode: "auto" };
  active = null;
  drawAll();
  $("input").focus();
  try {
    const folders = await http("GET", "folders");
    if (draft && !draft.workdir) { draft.workdir = folders.path; draft.folder = folders.label; }
    const defaults = await http("GET", "models");
    if (draft) { draft.model = defaults.current; draft.effort = defaults.effort; draft.modelLabel = modelLabel(defaults, defaults.current, defaults.effort); }
    drawDock();
    drawWelcome();
  } catch { /* the chips stay blank until the server answers */ }
}

function modelLabel(data: Json, model: string, effort: string): string {
  for (const tab of data.tabs ?? []) for (const m of tab.models ?? []) if (m.id === model) {
    const e = (data.efforts ?? []).find((x: Json) => x.id === effort);
    return [m.label, e?.label].filter(Boolean).join(" · ");
  }
  return [model, effort].filter(Boolean).join(" · ");
}

/** A scout's page: its project's chat (opened on the server first if it isn't), with its header and rail. */
function openScout(pid: string) {
  const p = scouts.project(pid);
  if (!p) return;
  view = "scout";
  draft = null;
  scouts.setCurrent(pid);
  const chat = p.chat && chats.has(p.chat) ? p.chat : null;
  active = chat;
  if (chat) loadHistory(chat);
  else { wantNew = true; send({ type: "new", project: pid }); }
  drawAll();
}

/** The server opened the draft's chat (or a scout's): switch to it and send what was typed. */
function opened(chat: string) {
  active = chat;
  draft = null;
  if (view === "scout") { drawAll(); loadHistory(chat); return; }
  transcripts.set(chat, []);
  const first = pendingSend;
  pendingSend = null;
  if (first) {
    if (first.mode !== "auto") send({ type: "mode", chat, mode: first.mode });
    transcripts.get(chat)!.push({ ...blank("user", first.text), images: first.images });
    send({ type: "message", chat, text: first.text, images: first.images.length ? first.images : undefined });
  }
  drawAll();
}

// ---------------------------------------------------------------- drawing

let drawPending = false;
function scheduleDraw() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(() => { drawPending = false; drawTranscript(false); });
}

let shownKey = ""; // what the main pane shows, so a change of chat or page can fade in

/** Fade in whichever page just came up: a chat, the home page or the board. */
function fadeIfNew() {
  const key = `${view}/${active ?? ""}/${draft ? "draft" : ""}`;
  if (key === shownKey) return;
  shownKey = key;
  for (const id of ["transcript", "welcome", "agents"]) {
    const box = $(id);
    box.classList.remove("opening");
    void box.offsetWidth; // so the animation starts again
    box.classList.add("opening");
  }
}

function drawAll() {
  fadeIfNew();
  drawSidebar();
  drawHeader();
  drawView();
  drawScout();
  drawTranscript(true);
  drawWelcome();
  drawQuestion();
  drawDock();
}

/** Which date heading a chat goes under, by when it last changed. */
function groupOf(updated: number, now = new Date()): string {
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day(now) - day(new Date(updated * 1000))) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}

function drawSidebar() {
  $("machine").textContent = machine || hostOf(paired?.url) || "Vision";
  const nav = $("chats");
  nav.replaceChildren();
  const query = $<HTMLInputElement>("search").value.trim().toLowerCase();

  scouts.drawSidebar(nav, query, view === "scout");
  const live = scouts.live();
  const scoutChats = new Set(live.map((p) => p.chat).filter(Boolean));

  const all = [...chats.values()]
    .filter((c) => !scoutChats.has(c.chat) && !(c.project && live.some((p) => p.id === c.project)))
    .filter((c) => !query || `${c.title ?? ""} ${c.folder ?? ""}`.toLowerCase().includes(query))
    .sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || (b.updated ?? 0) - (a.updated ?? 0));
  let heading = "";
  for (const c of all) {
    const group = c.pinned ? "Pinned" : groupOf(c.updated ?? 0);
    if (group !== heading) { nav.append(el("div", "group", group)); heading = group; }
    const item = el("div", "chat-item" + (c.chat === active && view === "chat" ? " active" : ""));
    item.title = [c.title, c.model, c.folder].filter(Boolean).join(" · ");
    const text = el("span", "chat-text");
    text.append(el("span", "chat-name", c.title || "New chat"));
    if (c.folder) text.append(el("span", "chat-folder", c.folder));
    item.append(text);
    if (c.waiting) item.append(el("span", "dot ask"));
    else if (c.busy) item.append(el("span", "dot busy"));
    else if (c.source === "terminal") item.append(el("span", "badge", "terminal"));
    const pin = el("button", "icon-btn pin" + (c.pinned ? " on" : ""));
    pin.append(icon(c.pinned ? "PinOff" : "Pin", 13));
    pin.title = c.pinned ? "Unpin" : "Pin to the top";
    pin.onclick = (e) => { e.stopPropagation(); setPinned(c.chat, !c.pinned); };
    item.append(pin);
    const close = el("button", "icon-btn close");
    close.append(icon("X", 14));
    close.title = c.source === "terminal" ? "Quit this chat in its terminal" : "Close this chat";
    close.onclick = (e) => { e.stopPropagation(); send({ type: "close", chat: c.chat }); };
    item.append(close);
    item.onclick = () => select(c.chat);
    nav.append(item);
  }
  if (!all.length) nav.append(el("div", "empty-list", query ? "Nothing matches." : "No chats yet."));
  for (const b of document.querySelectorAll<HTMLElement>(".side-row[data-action=new_chat]")) b.classList.toggle("active", !!draft && view === "chat");
  for (const b of document.querySelectorAll<HTMLElement>(".side-row[data-action=agents]")) b.classList.toggle("active", view === "agents");
}

/** Pin a chat to the top of the list, or unpin it: the server keeps it, so the phone shows the same. */
function setPinned(chat: string, on: boolean) {
  const c = chats.get(chat);
  if (c) { c.pinned = on; drawSidebar(); }
  send({ type: "pin", chat, pinned: on });
}

function projectName(p: Json): string {
  return p.title || p.name || p.goal || "Project";
}

function hostOf(url?: string): string {
  try { return url ? new URL(url).hostname : ""; } catch { return ""; }
}

function isLocal(url?: string): boolean {
  return ["127.0.0.1", "localhost", "::1", "[::1]"].includes(hostOf(url));
}

/** A line over the chat when Vision and this app don't match, or a newer Vision is out. */
function drawNotice() {
  const box = $("notice");
  const local = isLocal(paired?.url);
  let text = "";
  let urgent = false;
  if (serverProtocol === null || serverProtocol < PROTOCOL) { text = `Vision on ${machine || "that computer"} is older than this app, so some things won't work.`; urgent = true; }
  else if (serverProtocol > PROTOCOL) { text = "This app is older than Vision: get the newest app so everything works."; urgent = true; }
  else if (updateHow) text = "A new version of Vision is out.";
  box.hidden = !text || localStorage.getItem("notice-hidden") === text + updateHow;
  box.replaceChildren();
  if (box.hidden) return;
  box.classList.toggle("urgent", urgent);
  box.append(icon(urgent ? "CircleAlert" : "Sparkles", 15), el("span", "notice-text", text));
  if (serverProtocol !== null && serverProtocol > PROTOCOL) { /* the app itself: nothing to run here yet */ }
  else if (local) {
    const b = el("button", "chip-btn bordered", "Update");
    b.onclick = () => upgrade();
    box.append(b);
  } else {
    const how = updateHow || "vision upgrade";
    box.append(el("code", "", how), el("span", "dim small", `on ${machine || "that computer"}`));
  }
  const x = el("button", "icon-btn");
  x.append(icon("X", 14));
  x.onclick = () => { localStorage.setItem("notice-hidden", text + updateHow); drawNotice(); };
  box.append(x);
}

/** `vision upgrade` here, its output in the notice; the server restarts once replies finish and the app reconnects. */
async function upgrade() {
  const box = $("notice");
  box.replaceChildren(el("span", "state running"), el("span", "notice-text", "Updating Vision…"));
  const lines: string[] = [];
  const code = await step("upgrade", (t) => { lines.push(t); box.querySelector(".notice-text")!.textContent = `Updating Vision… ${t.slice(0, 80)}`; })
    .catch((e) => { lines.push(String(e)); return -1; });
  box.replaceChildren(icon(code === 0 ? "Check" : "CircleAlert", 15), el("span", "notice-text", code === 0
    ? "Updated. Vision restarts once every reply has finished, and this window reconnects by itself."
    : `The update stopped: ${lines.filter(Boolean).slice(-1)[0] ?? "no output"}`));
  if (code === 0) updateHow = null;
}

// ---------------------------------------------------------------- updating this app

const RELEASES = "https://github.com/vision-sAi/vision/releases/latest";
let appUpdate: Update | null = null;
let installKind = "dev";

/** Ask GitHub for a newer release of this app (on launch and every six hours). */
async function checkAppUpdate() {
  if (installKind === "dev") return;
  try { appUpdate = await check(); } catch { appUpdate = null; }
  drawAppUpdate();
}

/** A row over the computer at the bottom of the sidebar: restart into the new version, or download it. */
function drawAppUpdate() {
  const row = $("app-update");
  row.hidden = !appUpdate;
  if (!appUpdate) return;
  const selfUpdates = installKind === "app" || installKind === "appimage";
  row.replaceChildren(icon("Sparkles", 15), el("span", "app-update-text", `Vision ${appUpdate.version} is out`),
    el("span", "app-update-act", selfUpdates ? "Restart to update" : "Download"));
  row.onclick = async () => {
    if (!selfUpdates) { openUrl(RELEASES); return; }
    row.replaceChildren(el("span", "state running"), el("span", "app-update-text", "Downloading…"));
    try {
      await appUpdate!.downloadAndInstall();
      await relaunch();
    } catch (e) {
      toast(`The update didn't install: ${e}`);
      openUrl(RELEASES);
      drawAppUpdate();
    }
  };
}

/** The board or a chat: the main pane shows one at a time. */
function drawView() {
  const board = view === "agents";
  $("agents").hidden = !board;
  $("main").classList.toggle("is-agents", board);
  $("main").classList.toggle("is-scout", view === "scout");
  if (board) agents.draw($("agents"));
}

/** A scout's header and rail, when its page is on screen. */
function drawScout() {
  const p = view === "scout" ? scouts.project(scouts.current) : undefined;
  $("scout-head").hidden = !p;
  $("scout-rail").hidden = !p;
  document.querySelector(".title-wrap")!.classList.toggle("hidden-for-scout", !!p);
  $("app").classList.toggle("with-rail", !!p);
  if (!p) return;
  scouts.drawHead($("scout-head"), p);
  // Redrawing the rail would throw away what's being typed into it.
  const rail = $("scout-rail");
  const typing = rail.contains(document.activeElement) && /^(INPUT|TEXTAREA)$/.test(document.activeElement!.tagName);
  if (!typing) scouts.drawRail(rail, p);
}

let notifying: boolean | null = null; // allowed to notify (asked once)

async function notify(title: string, body: string) {
  if (notifying === null) {
    try { notifying = (await isPermissionGranted()) || (await requestPermission()) === "granted"; } catch { notifying = false; }
  }
  if (notifying) sendNotification({ title, body });
}

function drawBadge() {
  invoke("set_tray_status", { text: agents.summary() }).catch(() => {});
  const c = agents.counts();
  const badge = $("agents-badge");
  const n = c.needs || c.review || c.working;
  badge.hidden = !n;
  badge.textContent = String(n);
  badge.className = "pill " + (c.needs ? "ask" : c.review ? "ready" : "busy");
  if (view === "agents") drawHeader();
}

function drawHeader() {
  if (view === "scout") {
    const p = scouts.project(scouts.current);
    $("chat-title").textContent = p ? scouts.nameOf(p) : "";
    $("chat-folder").textContent = "";
    return;
  }
  if (view === "agents") {
    $("chat-title").textContent = "All activity";
    $("chat-folder").textContent = agents.summary() || "Nothing running";
    return;
  }
  const c = active ? chats.get(active) : undefined;
  $("chat-title").textContent = c ? c.title || "New chat" : "";
  $("chat-folder").textContent = c?.folder ?? "";
}

function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? "Up late" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

const STARTERS: [IconName, string, string][] = [
  ["FileText", "Review today's changes", "Review what changed in this folder today"],
  ["Lightbulb", "Plan something", "Help me plan something"],
  ["Search", "Find a bug", "Find and fix a bug"],
];

/** The new-chat page: a greeting, the composer in the middle, and a few ways to start. */
function drawWelcome() {
  const on = !!draft && view === "chat";
  $("welcome").hidden = !on;
  $("transcript").hidden = on;
  $("main").classList.toggle("is-welcome", on);
  const composer = $("composer");
  if (on && composer.parentElement !== $("welcome-slot")) $("welcome-slot").append(composer);
  if (!on && composer.parentElement !== $("dock")) $("dock").append(composer);
  if (!on) return;
  $("greeting").textContent = greeting();
  const chips = $("chips");
  chips.replaceChildren();
  for (const [glyph, label, text] of STARTERS) {
    const b = el("button", "chip");
    b.append(icon(glyph, 14), el("span", "", label));
    b.onclick = () => { const i = $<HTMLTextAreaElement>("input"); i.value = text; grow(i); i.focus(); };
    chips.append(b);
  }
  scouts.drawHome($("home-scouts"));
  drawRecent();
}

/** The home page's last few chats, to pick one back up. */
function drawRecent() {
  const box = $("home-recent");
  box.replaceChildren();
  const live = scouts.live();
  const recent = [...chats.values()]
    .filter((c) => c.title && !(c.project && live.some((p) => p.id === c.project)))
    .sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0)).slice(0, 3);
  if (!recent.length) return;
  const list = el("div", "home-recent");
  for (const c of recent) {
    const row = el("button", "home-chat");
    row.append(icon("MessageSquare", 15), el("span", "home-chat-title", c.title), el("span", "dim small", agents.ago(c.updated ?? 0)));
    row.onclick = () => select(c.chat);
    list.append(row);
  }
  box.append(el("div", "home-label", "Pick up where you left off"), list);
}

const rendered = new WeakMap<Msg, HTMLElement>(); // finished messages drawn once, reused while a reply streams
const drawnCount = new Map<string, number>(); // chat → how many messages it showed last time; any past that ease in

function drawTranscript(jump: boolean) {
  const box = $("transcript");
  const pinned = jump || box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.replaceChildren();
  const list = active ? transcripts.get(active) ?? [] : [];
  const scout = view === "scout" ? scouts.project(scouts.current) : undefined;
  if (scout && active && transcripts.has(active) && !list.length) {
    const empty = el("div", "scout-empty");
    empty.append(el("h3", "", `${scouts.nameOf(scout)} is ready`),
      el("p", "dim", "Tell it what to do first, or just say hello. It keeps working on its goal in the background and reports back here."));
    box.append(empty);
  }
  const actions = {
    retry: retryFrom, readAloud: readAloudOn ? readAloud : undefined,
    stopShell: () => active && send({ type: "shell_stop", chat: active }),
  };
  const before = active ? drawnCount.get(active) : undefined;
  if (active) drawnCount.set(active, list.length);
  list.forEach((m, i) => {
    const live = m.streaming || m.shellRunning;
    let node = live ? undefined : rendered.get(m);
    const owner = scout ?? (active ? scouts.live().find((p) => p.chat === active) : undefined);
    if (!node && owner && m.checkin) node = scouts.checkinCard(m.checkin, owner);
    if (!node && scout && m.role === "note") node = scouts.reportCard(m.text, scout) ?? undefined;
    if (!node) {
      node = drawMsg(m, actions);
      if (!live) rendered.set(m, node);
    }
    if (before !== undefined && i >= before) {
      node.classList.add("enter");
      node.addEventListener("animationend", () => node!.classList.remove("enter"), { once: true });
    }
    box.append(node);
  });
  drawQueued();
  if (pinned) box.scrollTop = box.scrollHeight;
}

/** Messages waiting for the running reply to finish, as the server lists them. */
function drawQueued() {
  const box = $("transcript");
  box.querySelector(".queued-wrap")?.remove();
  const queued: string[] = (active && chats.get(active)?.queued) || [];
  if (!queued.length) return;
  const wrap = el("div", "queued-wrap");
  for (const text of queued) {
    const m = el("div", "msg user queued");
    m.append(el("div", "bubble", text));
    const undo = el("button", "link small", "Remove from queue");
    undo.onclick = () => send({ type: "unqueue", chat: active, text });
    m.append(undo);
    wrap.append(m);
  }
  box.append(wrap);
}

function drawQuestion() {
  const box = $("question");
  const qs = active ? questions.get(active) : undefined;
  // The form takes room from the transcript: one that was following the reply keeps following it.
  const t = $("transcript");
  const pinned = t.scrollHeight - t.scrollTop - t.clientHeight < 80;
  box.hidden = !qs?.length;
  box.replaceChildren();
  const plan = qs?.length === 1 && qs[0].header === "Plan" ? qs[0] : null;
  box.classList.toggle("plan-review", !!plan);
  if (plan) fillPlan(box, plan);
  else if (qs?.length) fillQuestion(box, qs);
  if (pinned) t.scrollTop = t.scrollHeight;
}

/** Plan mode's approval: the plan itself, then go ahead, keep planning, or say what to change. */
function fillPlan(box: HTMLElement, q: Question) {
  // The plan rides in the question (after a blank line) or in the reply's ExitPlanMode call.
  let plan = q.question.split("\n\n").slice(1).join("\n\n");
  if (!plan && active) {
    const m = streamingMsg(active);
    plan = [...(m?.tools ?? [])].reverse().map(planOf).find(Boolean) ?? "";
  }
  const head = el("div", "plan-head");
  head.append(icon("Lightbulb", 16), el("span", "", "Vision has a plan"), el("span", "dim small", "Nothing runs until you approve it"));
  box.append(head);
  if (plan) { const body = markdown(plan); body.classList.add("plan-body"); box.append(body); }
  const changes = el("textarea", "plan-changes") as HTMLTextAreaElement;
  changes.rows = 1;
  changes.placeholder = "Ask for changes…";
  const actions = el("div", "q-actions");
  const keep = el("button", "link", "Keep planning");
  keep.onclick = () => send({ type: "answer", chat: active, answers: { [q.question]: "No" } });
  const go = el("button", "primary", "Approve and start");
  const answer = () => {
    const typed = changes.value.trim();
    send({ type: "answer", chat: active, answers: { [q.question]: typed || "Yes" } });
  };
  go.onclick = answer;
  changes.addEventListener("input", () => { go.textContent = changes.value.trim() ? "Send changes" : "Approve and start"; grow(changes); });
  changes.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); answer(); } });
  actions.append(keep, go);
  box.append(changes, actions);
}

function fillQuestion(box: HTMLElement, qs: Question[]) {
  const picks = new Map<string, Set<string>>();
  const other = new Map<string, HTMLInputElement>();
  for (const q of qs) {
    const part = el("div", "q");
    if (q.header) part.append(el("div", "q-header", q.header));
    part.append(el("div", "q-text", q.question));
    const chosen = new Set<string>();
    picks.set(q.question, chosen);
    const opts = el("div", "q-options");
    for (const o of q.options ?? []) {
      const b = el("button", "q-option");
      b.append(el("span", "q-label", o.label));
      if (o.description) b.append(el("span", "q-desc", o.description));
      b.onclick = () => {
        if (!q.multiSelect) { chosen.clear(); opts.querySelectorAll(".q-option").forEach((x) => x.classList.remove("on")); }
        if (chosen.has(o.label)) { chosen.delete(o.label); b.classList.remove("on"); }
        else { chosen.add(o.label); b.classList.add("on"); }
      };
      opts.append(b);
    }
    part.append(opts);
    const free = el("input", "q-other") as HTMLInputElement;
    free.placeholder = "Something else…";
    other.set(q.question, free);
    part.append(free);
    box.append(part);
  }
  const actions = el("div", "q-actions");
  const skip = el("button", "link", "Skip");
  skip.onclick = () => send({ type: "answer", chat: active, answers: null });
  const submit = el("button", "primary", "Answer");
  submit.onclick = () => {
    const answers: Json = {};
    for (const q of qs) {
      // The machine's option order, then free text last, as the phone and terminal send it.
      const parts = (q.options ?? []).map((o) => o.label).filter((l) => picks.get(q.question)!.has(l));
      const typed = other.get(q.question)!.value.trim();
      if (typed) parts.push(typed);
      if (parts.length) answers[q.question] = parts.join(", ");
    }
    send({ type: "answer", chat: active, answers: Object.keys(answers).length ? answers : null });
  };
  actions.append(skip, submit);
  box.append(actions);
}

/** The composer's buttons, as the chat (or draft) stands. */
function drawDock() {
  const c = active ? chats.get(active) : undefined;
  const busy = !!c?.busy;
  $("stop").hidden = !busy;
  drawSendReady();
  const scout = view === "scout" ? scouts.project(scouts.current) : undefined;
  $<HTMLTextAreaElement>("input").placeholder = draft ? "What should Vision work on?" : busy ? "Queue a message"
    : scout ? `Tell ${scouts.nameOf(scout)} what to do next…` : "Message Vision";
  $("folder-text").textContent = draft ? draft.folder || "Folder" : c?.folder || "";
  $("folder").hidden = !draft && !c?.folder;
  $("folder").classList.toggle("fixed", !draft);
  $("folder").title = draft ? "Choose the folder this chat works in" : "A chat stays in the folder it started in";
  $("model-text").textContent = draft ? draft.modelLabel || "Model" : [c?.model, c?.effort].filter(Boolean).join(" · ") || "Model";
  $("model").hidden = !draft && !c;
  const mode = draft ? draft.mode : c?.mode ?? "auto";
  for (const b of $("mode").querySelectorAll<HTMLElement>("button")) b.classList.toggle("on", b.dataset.mode === mode);
  $("mode").hidden = !draft && !c;
  const suggestion = !draft && !busy ? c?.suggestion ?? "" : "";
  const s = $("suggestion");
  s.hidden = !suggestion;
  s.replaceChildren(icon("Sparkles", 14), el("span", "", suggestion));
  s.onclick = () => { const i = $<HTMLTextAreaElement>("input"); i.value = suggestion; grow(i); i.focus(); };
  drawAttachments();
}

function drawAttachments() {
  const box = $("attachments");
  box.hidden = !attachments.length;
  box.replaceChildren();
  for (const a of attachments) {
    const chip = el("div", "attachment" + (a.error ? " failed" : a.path ? "" : " uploading"));
    if (a.preview) { const img = el("img") as HTMLImageElement; img.src = a.preview; chip.append(img); }
    else chip.append(icon("Paperclip", 14));
    chip.append(el("span", "", a.name));
    chip.title = a.error ?? (a.path ? a.path : "Uploading…");
    const x = el("button", "x");
    x.append(icon("X", 12));
    x.onclick = () => { attachments = attachments.filter((b) => b.id !== a.id); drawAttachments(); };
    chip.append(x);
    box.append(chip);
  }
  drawSendReady();
}

let toastTimer = 0;
function toast(text: string) {
  if (!text) return;
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (t.hidden = true), 4000);
}

// ---------------------------------------------------------------- popovers: model, folder

function closePopover() {
  $("popover").hidden = true;
}

/** A menu above (or below) the button that opened it. */
function popover(anchor: HTMLElement, content: HTMLElement, width = 320) {
  const pop = $("popover");
  pop.replaceChildren(content);
  pop.style.width = `${width}px`;
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  const h = pop.offsetHeight;
  const left = Math.min(Math.max(8, r.left), window.innerWidth - width - 8);
  const top = r.top - h - 8 > 8 ? r.top - h - 8 : r.bottom + 8;
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

/**
 * A model and effort menu by `anchor`: `data` is what /models or /defaults answers, `levels` the efforts a
 * model offers, and `pick` gets each choice (with its label) as it is made.
 */
function modelMenu(anchor: HTMLElement, data: Json, levels: (model: string) => Promise<Json>, pick: (model: string, effort: string, label: string) => void) {
  let model: string = data.current;
  let effort: string = data.effort;
  let lastEfforts: Json[] = data.efforts ?? [];
  const box = el("div", "menu");
  const efforts = el("div", "efforts");
  const apply = () => pick(model, effort, modelLabel({ ...data, efforts: lastEfforts }, model, effort));
  const drawEfforts = (list: Json[]) => {
    efforts.replaceChildren();
    for (const e of list) {
      const b = el("button", "effort" + (e.id === effort ? " on" : ""), e.label);
      b.title = e.description ?? "";
      b.onclick = (ev) => { ev.stopPropagation(); effort = e.id; drawEfforts(list); apply(); };
      efforts.append(b);
    }
  };
  for (const tab of data.tabs ?? []) {
    box.append(el("div", "menu-label", tab.tab));
    for (const m of tab.models ?? []) {
      const b = el("button", "menu-item" + (m.id === model ? " on" : ""));
      const text = el("span", "menu-text");
      text.append(el("span", "", m.label), el("span", "dim small", m.description ?? ""));
      b.append(text, icon("Check", 14));
      b.onclick = async (ev) => {
        ev.stopPropagation();
        model = m.id;
        box.querySelectorAll(".menu-item").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        try {
          const next = await levels(model);
          lastEfforts = next.efforts ?? [];
          if (!lastEfforts.some((e) => e.id === effort)) effort = next.effort ?? lastEfforts[0]?.id ?? effort;
          drawEfforts(lastEfforts);
        } catch { /* keep the old levels */ }
        apply();
      };
      box.append(b);
    }
  }
  box.append(el("div", "menu-label", "Effort"), efforts);
  drawEfforts(lastEfforts);
  popover(anchor, box, 340);
}

/** The composer's model button: the open chat's model, or the draft's. */
async function openModels() {
  const forDraft = !!draft;
  if (!forDraft && !active) return;
  if (!forDraft && chats.get(active!)?.busy) { toast("Switch models once this reply has finished."); return; }
  let data: Json;
  try {
    data = forDraft ? await http("GET", "models", { model: draft!.model ?? "", effort: draft!.effort ?? "" })
      : await http("GET", "models", { chat: active! });
  } catch (e) { toast(String(e)); return; }
  const chat = active;
  modelMenu($("model"), data, (model) => http("GET", "models", forDraft ? { model } : { chat: chat!, model }), (model, effort, label) => {
    if (forDraft) {
      if (!draft) return;
      draft.model = model; draft.effort = effort; draft.modelLabel = label;
      drawDock();
    } else if (model !== data.current || effort !== data.effort) {
      send({ type: "model", chat, model, effort });
      data.current = model; data.effort = effort;
    }
  });
}

async function openFolders(path = "") {
  if (!draft) return;
  folderMenu($("folder"), draft.workdir, (p, label) => {
    if (draft) { draft.workdir = p; draft.folder = label; }
    drawDock();
    drawWelcome();
  }, path);
}

/** A folder menu by `anchor`: recent folders, then this one's subfolders; `pick` gets the one chosen. */
async function folderMenu(anchor: HTMLElement, current: string, pick: (path: string, label: string) => void, path = "") {
  let data: Json;
  try { data = await http("GET", "folders", path ? { path } : undefined); } catch (e) { toast(String(e)); return; }
  const box = el("div", "menu");
  const use = (p: string, label: string) => {
    closePopover();
    pick(p, label);
  };
  const item = (glyph: "Folder" | "Check", label: string, onclick: () => void, extra = "", trailing?: "ChevronRight" | "Check") => {
    const b = el("button", "menu-item" + extra);
    b.append(icon(glyph, 15), el("span", "menu-text", label));
    if (trailing) b.append(icon(trailing, 14));
    b.onclick = (ev) => { ev.stopPropagation(); onclick(); };
    box.append(b);
  };
  if (!path && data.recent?.length) {
    box.append(el("div", "menu-label", "Recent"));
    for (const r of data.recent.slice(0, 6)) item("Folder", r.label, () => use(r.path, r.label), r.path === current ? " on" : "", "Check");
  }
  box.append(el("div", "menu-label", data.label || data.path));
  item("Check", "Use this folder", () => use(data.path, data.label || data.path), " strong");
  if (data.parent) item("Folder", "..", () => folderMenu(anchor, current, pick, data.parent));
  for (const f of (data.folders ?? []).slice(0, 200)) item("Folder", f.name, () => folderMenu(anchor, current, pick, f.path), "", "ChevronRight");
  popover(anchor, box, 320);
}

// ---------------------------------------------------------------- replies: retry, read aloud

/** Send again the message a reply answered. */
function retryFrom(m: Msg) {
  const list = active ? transcripts.get(active) ?? [] : [];
  const i = list.indexOf(m);
  const asked = list.slice(0, i < 0 ? list.length : i).reverse().find((x) => x.role === "user");
  if (asked) sendText(asked.text, []);
}

let speaking: { stop: () => void } | null = null;

async function readAloud(m: Msg) {
  if (speaking) { speaking.stop(); speaking = null; return; }
  let stopped = false;
  let audio: HTMLAudioElement | null = null;
  speaking = { stop: () => { stopped = true; audio?.pause(); } };
  try {
    const { pieces } = await http("POST", "speak/pieces", undefined, { text: m.text });
    for (const piece of pieces as string[]) {
      if (stopped) break;
      const wav = await invoke<ArrayBuffer>("speak", { text: piece });
      if (stopped) break;
      audio = new Audio(URL.createObjectURL(new Blob([wav], { type: "audio/wav" })));
      await new Promise<void>((done) => { audio!.onended = () => done(); audio!.onerror = () => done(); audio!.play().catch(() => done()); });
    }
  } catch (e) {
    toast(String(e));
  }
  speaking = null;
}

// ---------------------------------------------------------------- pictures

const pictures = new Map<string, Promise<string>>(); // path on the Vision computer → blob URL

setPictureLoader((path) => {
  if (!pictures.has(path)) {
    pictures.set(path, invoke<ArrayBuffer>("fetch_bytes", { path: "image", query: { path } })
      .then((bytes) => URL.createObjectURL(new Blob([bytes])))
      .catch(() => { pictures.delete(path); return ""; }));
  }
  return pictures.get(path)!;
});

setPictureViewer((src, caption) => {
  const view = $("viewer");
  view.replaceChildren();
  const img = el("img") as HTMLImageElement;
  img.src = src;
  view.append(img);
  if (caption) view.append(el("div", "viewer-caption", caption));
  view.hidden = false;
});

// ---------------------------------------------------------------- attachments

async function attach(files: FileList | File[]) {
  for (const file of Array.from(files)) {
    const a: Attachment = { id: nextAttachment++, name: file.name || "pasted image.png" };
    if (file.type.startsWith("image/")) a.preview = URL.createObjectURL(file);
    attachments.push(a);
    drawAttachments();
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      a.path = await invoke<string>("upload", bytes, { headers: { "x-name": encodeURIComponent(a.name) } });
    } catch (e) {
      a.error = String(e);
      toast(`${a.name}: ${e}`);
    }
    drawAttachments();
  }
}

// ---------------------------------------------------------------- connecting

/** The connect form: the whole window when there is nothing to show yet, else a sheet over the app. */
function showPairing(error = "") {
  const sheet = paired !== null && !$("app").hidden;
  $("pair").classList.toggle("sheet", sheet);
  $("pair-close").hidden = !sheet && !!paired;
  $("pair").hidden = false;
  $("pair-error").hidden = !error;
  $("pair-error").textContent = error;
  invoke<Json | null>("local_vision").then((local) => {
    const b = $("pair-local");
    b.hidden = !local;
    $("pair-local-url").textContent = local ? local.url : "";
    b.onclick = () => local && connect(local.url, local.token, "");
  });
}

function hidePairing() {
  $("pair").hidden = true;
  if (!paired) firstRun(); // the first run's "connect elsewhere", backed out of
}

async function connect(url: string, token: string, name: string) {
  const pasted = url.trim().startsWith("{") ? safeJson(url) : null;
  if (pasted) { url = pasted.url ?? ""; token = pasted.token ?? ""; name = pasted.name ?? ""; }
  try {
    await invoke("pair", { url, token, name });
    chats.clear(); transcripts.clear(); questions.clear(); projects = []; active = null; draft = null; machine = "";
    paired = await invoke<{ url: string; name: string } | null>("saved");
    $("pair").hidden = true;
    showApp();
  } catch (e) {
    showPairing(String(e));
  }
}

/** On launch: the computer this window was last connected to, else a Vision running right here. */
async function start() {
  paired = await invoke<{ url: string; name: string } | null>("saved");
  if (paired && (await invoke<boolean>("resume"))) { showApp(); return; }
  const local = await invoke<Json | null>("local_vision");
  if (local) { await connect(local.url, local.token, ""); return; }
  firstRun();
}

/** The key was refused. A Vision on this computer may have a new one: take it once, quietly. */
async function refused(detail: string) {
  const local = await invoke<Json | null>("local_vision");
  if (local && isLocal(paired?.url) && !repairedLocal) {
    repairedLocal = true;
    await connect(local.url, local.token, "");
    return;
  }
  showPairing(detail);
}

function safeJson(text: string): Json | null {
  try { return JSON.parse(text); } catch { return null; }
}

function showApp() {
  $("app").hidden = false;
  drawAll();
}

const CONN_TEXT: Record<string, string> = {
  online: "Connected", connecting: "Connecting…", offline: "Can't reach it", refused: "Key refused", error: "Bad address",
};

function setConnection(state: string, detail: string) {
  $("conn").className = `conn ${state}`;
  const where = isLocal(paired?.url) ? "This computer" : hostOf(paired?.url);
  $("conn-text").textContent = state === "online" ? where : CONN_TEXT[state] ?? state;
  $("computer").title = detail || `${where} · change computer`;
  if (state === "online") repairedLocal = false;
}

// ---------------------------------------------------------------- appearance and commands

/** Follow the system's light/dark choice and accent colour (see `appearance` in lib.rs), unless Settings
 *  pins light or dark. */
function applyAppearance(a: Json = system) {
  system = a;
  const root = document.documentElement;
  root.dataset.os = a.os ?? "";
  const pinned = localStorage.getItem("appearance");
  const scheme = pinned === "light" || pinned === "dark" ? pinned : a.scheme;
  if (scheme) root.dataset.theme = scheme; else delete root.dataset.theme;
  const macAccent = a.os === "macos" && CSS.supports("color", "-apple-system-control-accent");
  const accent = a.accent || (macAccent ? "-apple-system-control-accent" : "");
  if (accent) root.style.setProperty("--accent", accent); else root.style.removeProperty("--accent");
}

function toggleSidebar() {
  const collapsed = document.documentElement.classList.toggle("collapsed");
  localStorage.setItem("sidebar", collapsed ? "hidden" : "shown");
}

/** What a menu item or toolbar button does, by its id. */
function act(action: string) {
  switch (action) {
    case "new_chat": startDraft(); break;
    case "connect": showPairing(); break;
    case "search":
      if (document.documentElement.classList.contains("collapsed")) toggleSidebar();
      $("search").focus();
      break;
    case "toggle_sidebar": toggleSidebar(); break;
    case "model": openModels(); break;
    case "stop": if (active && chats.get(active)?.busy) send({ type: "cancel", chat: active }); break;
    case "settings": openSettings(); break;
    case "agents": view = "agents"; scouts.setCurrent(null); drawAll(); agents.loadAll(); break;
    case "new_scout": scouts.openNew(); break;
  }
}

// ---------------------------------------------------------------- settings

function section(title: string, ...rows: HTMLElement[]): HTMLElement {
  const box = el("section", "set-section");
  box.append(el("h3", "", title), ...rows);
  return box;
}

function setRow(label: string, hint: string, control: HTMLElement): HTMLElement {
  const row = el("div", "set-row");
  const text = el("div", "set-text");
  text.append(el("div", "", label));
  if (hint) text.append(el("div", "dim small", hint));
  row.append(text, control);
  return row;
}

function segmented(options: [string, string][], current: string, pick: (v: string) => void): HTMLElement {
  const box = el("div", "segmented");
  for (const [value, label] of options) {
    const b = el("button", value === current ? "on" : "", label);
    b.onclick = () => { box.querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); pick(value); };
    box.append(b);
  }
  return box;
}

function toggle(on: boolean, flip: (on: boolean) => void): HTMLElement {
  const b = el("button", "toggle" + (on ? " on" : ""));
  b.setAttribute("role", "switch");
  b.onclick = () => { b.classList.toggle("on"); flip(b.classList.contains("on")); };
  return b;
}

/** Settings: how the window looks, what a new chat starts on, the voice, and which computer this is. */
async function openSettings() {
  const card = $("settings-card");
  card.classList.remove("scout-sheet");
  const head = el("div", "set-head");
  const close = el("button", "icon-btn");
  close.append(icon("X", 16));
  close.onclick = () => ($("settings").hidden = true);
  head.append(el("h2", "", "Settings"), close);
  const body = el("div", "set-body");
  card.replaceChildren(head, body);
  $("settings").hidden = false;

  body.append(section("Appearance", setRow("Theme", "System follows your desktop's light or dark setting.",
    segmented([["system", "System"], ["light", "Light"], ["dark", "Dark"]], localStorage.getItem("appearance") ?? "system", (v) => {
      if (v === "system") localStorage.removeItem("appearance"); else localStorage.setItem("appearance", v);
      applyAppearance();
    }))));

  const defaultsBtn = el("button", "chip-btn bordered", "…");
  body.append(section("New chats", setRow("Model", "What a new chat starts on. Open chats keep their own.", defaultsBtn)));
  http("GET", "defaults").then((data: Json) => {
    defaultsBtn.textContent = modelLabel(data, data.current, data.effort);
    defaultsBtn.append(icon("ChevronDown", 14));
    defaultsBtn.onclick = (e) => {
      e.stopPropagation();
      modelMenu(defaultsBtn, data, (model) => http("GET", "models", { model }), async (model, effort, label) => {
        try {
          data = await http("POST", "defaults", undefined, { model, effort });
          defaultsBtn.replaceChildren(document.createTextNode(label), icon("ChevronDown", 14));
          if (draft && !draft.model) startDraft();
        } catch (err) { toast(String(err)); }
      });
    };
  }).catch(() => defaultsBtn.closest(".set-section")?.remove());

  const voice = section("Voice", setRow("Vision's voice", "Speaks replies and reads them aloud. It runs on the computer's GPU.",
    toggle(voiceOn, async (on) => {
      try { voiceOn = (await http("POST", "voice/power", undefined, { on })).on; } catch (err) { toast(String(err)); }
      openSettings();
    })));
  body.append(voice);
  if (voiceOn) {
    http("GET", "voice").then((data: Json) => {
      const select = el("select", "select") as HTMLSelectElement;
      for (const v of data.voices ?? []) {
        const o = el("option", "", v.note ? `${v.id} · ${v.note}` : v.id) as HTMLOptionElement;
        o.value = v.id;
        o.selected = v.id === data.voice;
        select.append(o);
      }
      select.onchange = () => http("POST", "voice", undefined, { voice: select.value }).catch((err) => toast(String(err)));
      voice.append(setRow("Voice", "", select));
    }).catch(() => {});
    http("GET", "voice/rate").then((data: Json) => {
      const wrap = el("div", "rate");
      const range = el("input") as HTMLInputElement;
      Object.assign(range, { type: "range", min: "0.5", max: "2", step: "0.05", value: String(data.rate ?? 1) });
      const shown = el("span", "dim small", `${Number(range.value).toFixed(2)}×`);
      range.oninput = () => (shown.textContent = `${Number(range.value).toFixed(2)}×`);
      range.onchange = () => http("POST", "voice/rate", undefined, { rate: Number(range.value) }).catch((err) => toast(String(err)));
      wrap.append(range, shown);
      voice.append(setRow("Speed", "", wrap));
    }).catch(() => {});
  }

  if (isLocal(paired?.url)) {
    const manage = el("button", "chip-btn bordered", "Manage…");
    manage.onclick = () => { $("settings").hidden = true; subscriptions(false); };
    body.append(section("Subscriptions", setRow("Claude, ChatGPT, Grok", "Install and sign in to the subscriptions Vision works through.", manage)));
  }

  if (hasTray) {
    body.append(section("Window", setRow("Keep running in the tray", "Closing the window leaves Vision running, so replies and agents can still notify you.",
      toggle(localStorage.getItem("keep-in-tray") !== "off", (on) => {
        localStorage.setItem("keep-in-tray", on ? "on" : "off");
        invoke("set_keep_in_tray", { on });
      }))));
  }

  const where = isLocal(paired?.url) ? "This computer" : paired?.url ?? "";
  const change = el("button", "chip-btn bordered", "Change…");
  change.onclick = () => { $("settings").hidden = true; showPairing(); };
  const disconnect = el("button", "chip-btn bordered danger", "Disconnect");
  disconnect.onclick = async () => {
    await invoke("forget");
    chats.clear(); transcripts.clear(); questions.clear(); projects = []; active = null; draft = null; paired = null;
    $("settings").hidden = true;
    $("app").hidden = true;
    showPairing();
  };
  const buttons = el("div", "set-buttons");
  buttons.append(change, disconnect);
  body.append(section("Computer", setRow(machine || hostOf(paired?.url) || "Vision", where, buttons)));

  const version = await getVersion().catch(() => "");
  body.append(el("p", "set-about dim small", `Vision desktop ${version}${serverVersion ? ` · Vision ${serverVersion} on ${machine || "the computer"}` : ""}`));
}

function setMode(mode: string) {
  if (draft) { draft.mode = mode; drawDock(); return; }
  const c = active ? chats.get(active) : undefined;
  if (!c || c.mode === mode) return;
  if (c.busy) { toast("Switch modes once this reply has finished."); return; }
  send({ type: "mode", chat: active, mode });
}

/** Send a message: into the open chat, or open the draft's chat with it first. */
function sendText(text: string, images: string[]) {
  if (draft) {
    pendingSend = { text, images, mode: draft.mode };
    wantNew = true;
    send({ type: "new", workdir: draft.workdir || undefined, model: draft.model, effort: draft.effort });
    return;
  }
  if (!active) return;
  const c = chats.get(active);
  if (!c?.busy) {
    transcripts.get(active)?.push({ ...blank("user", text), images });
    drawTranscript(true);
  }
  send({ type: "message", chat: active, text, images: images.length ? images : undefined });
}

/** `!command` in an open chat: run on the computer, in the chat's folder (the terminal's shell mode). */
function shellCommand(text: string): string | null {
  const c = active ? chats.get(active) : undefined;
  return text.startsWith("!") && shellOn && c && c.source !== "terminal" ? text.slice(1).trim() : null;
}

function submit() {
  const input = $<HTMLTextAreaElement>("input");
  const text = input.value.trim();
  const command = shellCommand(text);
  if (command !== null) {
    if (command) send({ type: "shell", chat: active, text: command });
    input.value = "";
    grow(input);
    drawShellMode();
    return;
  }
  if (attachments.some((a) => !a.path && !a.error)) { toast("Still uploading…"); return; }
  const images = attachments.filter((a) => a.path).map((a) => a.path!);
  if (!text && !images.length) return;
  sendText(text, images);
  input.value = "";
  attachments = [];
  grow(input);
  drawAttachments();
}

/** The composer turns into a command line while what's typed starts with `!`. */
function drawShellMode() {
  const on = shellCommand($<HTMLTextAreaElement>("input").value) !== null;
  $("composer").classList.toggle("shell-mode", on);
  $("send").title = on ? "Run on the computer (Return)" : "Send (Return)";
}

/** The send button greys out while there's nothing to send. */
function drawSendReady() {
  const empty = !$<HTMLTextAreaElement>("input").value.trim() && !attachments.length;
  $("composer").classList.toggle("empty", empty);
}

function grow(t: HTMLTextAreaElement) {
  drawSendReady();
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 240) + "px";
}

scouts.setHost({
  http, toast, projects: () => projects,
  chatOf: (p) => (p.chat ? chats.get(p.chat) : undefined),
  open: (pid, p) => {
    if (p && !projects.some((x) => x.id === p.id)) projects = [...projects, p]; // before its `project` frame
    openScout(pid);
  },
  pickFolder: (anchor, pick) => folderMenu(anchor, "", pick),
  showBoard: () => act("agents"),
  changed: () => { drawSidebar(); drawScout(); },
});
agents.onRedraw(() => { if (view === "scout") drawScout(); });

agents.setHost({
  http, toast, projectName, projects: () => projects,
  openChat: (chat) => { if (chats.has(chat)) select(chat); else { active = chat; view = "chat"; } },
  changed: drawBadge,
  notify,
});

let hasTray = false;
if ((window as any).SCENES) (window as any).__buddy = buddy; // the screenshot harness draws every mood with it

window.addEventListener("DOMContentLoaded", async () => {
  placeIcons();
  applyAppearance(await invoke<Json>("appearance"));
  hasTray = await invoke<boolean>("set_keep_in_tray", { on: localStorage.getItem("keep-in-tray") !== "off" }).catch(() => false);
  await setupInit({
    connectLocal: async () => {
      const local = await invoke<Json | null>("local_vision");
      if (!local) return false;
      await connect(local.url, local.token, "");
      return !!paired;
    },
    pairElsewhere: () => { closeSetup(); showPairing(); },
    toast,
  });
  if (localStorage.getItem("sidebar") === "hidden") document.documentElement.classList.add("collapsed");
  await listen<Json>("appearance", (e) => applyAppearance(e.payload));
  await listen<string>("menu", (e) => act(e.payload));
  await listen<Json>("frame", (e) => handle(e.payload));
  await listen<Json>("connection", (e) => {
    const { state, detail } = e.payload;
    setConnection(state, detail);
    if (state === "refused") refused(detail);
  });

  for (const b of document.querySelectorAll<HTMLElement>("[data-action]")) b.onclick = () => act(b.dataset.action!);
  $("computer").onclick = () => showPairing();
  $("viewer").onclick = () => ($("viewer").hidden = true);
  $("settings").onclick = (e) => { if (e.target === $("settings")) $("settings").hidden = true; };
  $("pair-close").onclick = hidePairing;
  $("pair-form").onsubmit = (e) => {
    e.preventDefault();
    connect($<HTMLInputElement>("pair-url").value, $<HTMLInputElement>("pair-token").value, "");
  };
  $("search").addEventListener("input", drawSidebar);
  $("search").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); ($("search") as HTMLInputElement).value = ""; drawSidebar(); $("input").focus(); }
    if (e.key === "Enter") $("chats").querySelector<HTMLElement>(".chat-item")?.click();
  });

  $("composer").onsubmit = (e) => { e.preventDefault(); submit(); };
  const input = $<HTMLTextAreaElement>("input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  input.addEventListener("input", () => { grow(input); drawShellMode(); });
  input.addEventListener("paste", (e) => {
    const files = e.clipboardData?.files;
    if (files?.length) { e.preventDefault(); attach(files); }
  });
  $("stop").onclick = () => act("stop");
  $("model").onclick = (e) => { e.stopPropagation(); openModels(); };
  $("folder").onclick = (e) => { e.stopPropagation(); if (draft) openFolders(); };
  $("attach").onclick = () => $("file").click();
  $("file").addEventListener("change", () => {
    const f = $<HTMLInputElement>("file");
    if (f.files?.length) attach(f.files);
    f.value = "";
  });
  for (const b of $("mode").querySelectorAll<HTMLElement>("button")) b.onclick = () => setMode(b.dataset.mode!);

  // Files dropped anywhere on the chat are attached (Tauri's own drop handling is off: dragDropEnabled).
  let dragDepth = 0;
  const main = $("main");
  main.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; $("drop").hidden = false; } });
  main.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("drop").hidden = true; } });
  main.addEventListener("dragover", (e) => e.preventDefault());
  main.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("drop").hidden = true;
    if (e.dataTransfer?.files.length) attach(e.dataTransfer.files);
  });

  // A link in a reply opens in the browser: followed here, it would replace the whole window.
  $("transcript").addEventListener("click", (e) => {
    const a = (e.target as HTMLElement).closest("a[href]") as HTMLAnchorElement | null;
    if (!a) return;
    e.preventDefault();
    if (/^(https?|mailto):/i.test(a.href)) openUrl(a.href).catch((err) => toast(String(err)));
  });
  document.addEventListener("click", (e) => {
    if (!$("popover").hidden && !$("popover").contains(e.target as Node)) closePopover();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || e.defaultPrevented) return;
    if (!$("viewer").hidden) $("viewer").hidden = true;
    else if (!$("settings").hidden && $("popover").hidden) $("settings").hidden = true;
    else if (!$("popover").hidden) closePopover();
    else if (!$("pair").hidden && paired) hidePairing();
    else act("stop");
  });

  await start();
  installKind = await invoke<string>("install_kind").catch(() => "dev");
  checkAppUpdate();
  setInterval(checkAppUpdate, 6 * 3600 * 1000);
});
