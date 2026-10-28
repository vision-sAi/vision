// Scouts: every project is a scout, someone with a name, a buddy avatar (buddy.ts) and a
// leash, that works on its goal in the background. This module draws the sidebar's Scouts section, a scout's
// page around its project chat (the header, report cards, and a rail with what needs you, today's activity,
// the leash and what it remembers) and the New scout sheet. main.ts owns the connection and the chat itself.

import * as agents from "./agents";
import { Mood, Scout, TRAITS, buddy } from "./buddy";
import { icon } from "./icons";
import { Json, el, markdown } from "./render";

export interface ScoutsHost {
  http: (method: string, path: string, query?: Json, body?: Json) => Promise<any>;
  toast: (text: string) => void;
  projects: () => Json[];
  /** The project chat's summary, when it's open. */
  chatOf: (p: Json) => Json | undefined;
  /** Show a scout's page (its chat, opened on the server if need be). */
  open: (pid: string, p?: Json) => void;
  /** The folder menu by `anchor`, starting from `path`; `pick` gets the folder chosen. */
  pickFolder: (anchor: HTMLElement, pick: (path: string, label: string) => void) => void;
  /** The All activity board. */
  showBoard: () => void;
  /** Something the page shows changed (draw the header, the rail and the sidebar again). */
  changed: () => void;
}

let host: ScoutsHost;
export let current: string | null = null; // the scout whose page is on screen
const activity = new Map<string, Json[]>(); // project → today's events, newest first
const memory = new Map<string, string[]>(); // project → its memory's lines, oldest first
const passing = new Map<string, { mood: Mood; until: number }>(); // the short moods the app plays
let notNow = new Set<string>(JSON.parse(localStorage.getItem("scouts-not-now") ?? "[]"));
const kept = new Map<string, HTMLElement>(); // where/project → its buddy, reused while it looks the same (redrawing restarts it)

/** A scout's buddy for one place on the page, the same element as last time unless its look changed. */
function keptBuddy(where: string, p: Json, mood: Mood, size: number, opts: { still?: boolean; pokeable?: boolean } = {}): HTMLElement {
  const s = scoutOf(p);
  const look = `${s.hue}/${s.trait}/${mood}/${size}/${!!opts.still}`;
  const k = `${where}/${p.id}`;
  const old = kept.get(k);
  if (old && old.dataset.look === look) return old;
  const b = buddy(s, mood, size, opts);
  b.dataset.look = look;
  kept.set(k, b);
  return b;
}

export function setHost(h: ScoutsHost) {
  host = h;
}

export function setCurrent(pid: string | null) {
  current = pid;
  if (pid) load(pid);
}

export const live = (): Json[] => host.projects().filter((p) => p.status !== "archived");
export const project = (pid: string | null) => (pid ? live().find((p) => p.id === pid) : undefined);
export const scoutOf = (p: Json): Scout => p.scout ?? {};
export const nameOf = (p: Json): string => p.scout?.name || p.title || "Scout";

/** The mood the buddy shows: a short one the app is playing, else the server's. */
export function moodOf(p: Json): Mood {
  const t = passing.get(p.id);
  if (t && t.until > Date.now()) return t.mood;
  return (p.mood as Mood) || "asleep";
}

function play(pid: string, mood: Mood, seconds: number) {
  passing.set(pid, { mood, until: Date.now() + seconds * 1000 });
  setTimeout(() => host.changed(), seconds * 1000 + 50);
  host.changed();
}

const LEASH: [string, string, string][] = [["on_call", "ask", "On call"], ["checkins", "balanced", "Check-ins"], ["autonomous", "free", "Autonomous"]];
const leashWords = (p: Json) => LEASH.find((l) => l[0] === p.leash)?.[2] ?? "Check-ins";

/** How many things wait on the user: its threads' questions and work to look at, and its approval requests. */
function needCount(p: Json): number {
  const t = p.threads ?? {};
  return (t.waiting_on_you ?? 0) + (t.ready_for_review ?? 0) + (p.approvals?.length ?? 0);
}

/** What a scout is up to, in a word or two, and its colour: `ask` (amber) when something waits on you,
 * `busy` (violet) while it works, `done` (green) after it found or landed something, `failed`, or `quiet`. */
export function stateOf(p: Json): [string, string] {
  const n = needCount(p);
  if (n) return [`${n} need${n === 1 ? "s" : ""} you`, "ask"];
  const mood = moodOf(p);
  const word = ({ worried: "something failed", focused: "working", curious: "reading up", proud: "landed something", found_it: "found it",
    waking: "waking up", out_cold: "paused while asleep" } as Record<string, string>)[mood];
  if (word) return [word, mood === "worried" ? "failed" : ["proud", "found_it"].includes(mood) ? "done" : mood === "out_cold" ? "quiet" : "busy"];
  return [p.status === "paused" ? "paused" : p.leash === "on_call" ? "on call"
    : p.checkin?.schedule?.next_run ? nextWords(p.checkin.schedule.next_run) : "resting", "quiet"];
}

// ---------------------------------------------------------------- events from main.ts

/** A line went into a project's activity log: the rail shows it, and a finish makes the buddy cheer. */
export function onActivity(pid: string, event: Json) {
  const list = activity.get(pid);
  if (list) activity.set(pid, [event, ...list.filter((e) => !(e.at === event.at && e.text === event.text))]);
  if (["ready", "finished", "landed"].includes(event.kind)) play(pid, "found_it", 4);
  else host.changed();
}

/** An approval request came in, or was decided (here or elsewhere). */
export function onApproval(pid: string, approval: Json) {
  const p = project(pid);
  if (!p) return;
  const rest = (p.approvals ?? []).filter((a: Json) => a.id !== approval.id);
  p.approvals = approval.state === "pending" ? [...rest, approval] : rest;
  host.changed();
}

/** A check-in began (the buddy wakes up) or ended (the card comes with the chat's next history). */
export function onCheckin(ev: Json) {
  const p = project(ev.project);
  if (!p) return;
  p.checkin = { ...(p.checkin ?? {}), running: ev.state === "started" ? ev.started : null };
  if (ev.state === "started") play(p.id, "waking", 3.2);
  else host.changed();
}

/** The computer slept: scouts that were at work then are shown out cold for a moment as it wakes. */
export function onSlept() {
  for (const p of live()) if (["focused", "curious"].includes(p.mood)) play(p.id, "out_cold", 90);
}

async function load(pid: string) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  try {
    const r = await host.http("GET", `projects/${pid}/activity`, { since: String(today.getTime() / 1000), limit: "60" });
    activity.set(pid, r.events ?? []);
  } catch { activity.set(pid, []); }
  try { memory.set(pid, (await host.http("GET", `projects/${pid}/memory`)).facts ?? []); } catch { memory.set(pid, []); }
  host.changed();
}

// ---------------------------------------------------------------- the sidebar

export function drawSidebar(nav: HTMLElement, query: string, onPage: boolean) {
  const all = live().filter((p) => !query || `${nameOf(p)} ${p.title ?? ""} ${p.goal ?? ""}`.toLowerCase().includes(query))
    .sort((a, b) => nameOf(a).localeCompare(nameOf(b)));
  if (query && !all.length) return;  // searching, and no scout matches: no heading over nothing
  const head = el("div", "group scouts-head");
  head.append(el("span", "", "Scouts"));
  const needs = all.filter((p) => needCount(p)).length;
  if (needs) head.append(el("b", "", `${needs} need${needs === 1 ? "s" : ""} you`));
  nav.append(head);
  if (!live().length && !query) {
    const first = el("button", "first-scout");
    first.append(buddy({ hue: 205, trait: "antenna" }, "asleep", 34, { still: true }));
    const text = el("span", "first-text");
    text.append(el("b", "", "Send your first scout"), el("span", "dim small", "Give it a goal; it works on it in the background"));
    first.append(text);
    first.onclick = () => openNew();
    nav.append(first);
    return;
  }
  for (const p of all) {
    const on = onPage && p.id === current;
    const mood = moodOf(p);
    const quiet = !on && !["needs_you", "still_waiting", "found_it", "worried"].includes(mood);
    const item = el("div", "chat-item scout" + (on ? " active" : ""));
    const text = el("span", "scout-text");
    const [state, tone] = stateOf(p);
    text.append(el("span", "chat-name", nameOf(p)), el("span", `scout-line tone-${tone}`, state));
    item.append(keptBuddy("side", p, mood, 26, { still: quiet }), text);
    item.title = [nameOf(p), p.goal, p.folder].filter(Boolean).join(" · ");
    item.onclick = () => host.open(p.id);
    nav.append(item);
  }
  if (!query) {
    const add = el("button", "chat-item new-scout");
    add.append(icon("Plus", 15), el("span", "chat-name", "New scout"));
    add.onclick = () => openNew();
    nav.append(add);
  }
}

/** The home page's row of scouts: a card each, the ones waiting on you first, and one to send another. */
export function drawHome(box: HTMLElement) {
  box.replaceChildren();
  const all = live().sort((a, b) => (needCount(b) ? 1 : 0) - (needCount(a) ? 1 : 0) || nameOf(a).localeCompare(nameOf(b)));
  if (!all.length) return;
  const grid = el("div", "home-scouts");
  for (const p of all) {
    const mood = moodOf(p);
    const n = needCount(p);
    const card = el("button", "home-scout" + (n ? " needs" : ""));
    const text = el("span", "home-scout-text");
    text.append(el("b", "", nameOf(p)), el("span", "home-scout-line", stateOf(p)[0]));
    card.append(keptBuddy("home", p, mood, 34, { still: !["needs_you", "still_waiting", "found_it", "worried", "focused"].includes(mood) }), text);
    if (n) card.append(el("span", "pill ask", String(n)));
    card.title = [nameOf(p), p.goal].filter(Boolean).join(" · ");
    card.onclick = () => host.open(p.id);
    grid.append(card);
  }
  const head = el("div", "home-label");
  const add = el("button", "home-add");
  add.append(icon("Plus", 13), el("span", "", "New scout"));
  add.onclick = () => openNew();
  head.append(el("span", "", "Your scouts"), add);
  box.append(head, grid);
}

// ---------------------------------------------------------------- the scout's page

/** The page's header: the buddy, its name and goal, and the chips for its leash, folder and model. */
export function drawHead(box: HTMLElement, p: Json) {
  box.replaceChildren();
  box.append(keptBuddy("head", p, moodOf(p), 40, { pokeable: true }));
  const text = el("div", "scout-titles");
  const name = el("h1");
  const leash = el("span", "scout-leash" + (p.status === "paused" ? " paused" : ""), p.status === "paused" ? "Paused" : leashWords(p));
  leash.title = "Its leash: change it on the right";
  name.append(el("span", "scout-name", nameOf(p)), leash);
  const goal = el("p", "scout-goal", p.goal ?? "");
  const chat = host.chatOf(p);
  goal.title = [p.goal, p.folder, chat?.model && [chat.model, chat.effort].filter(Boolean).join(" · ")].filter(Boolean).join("\n");
  text.append(name, goal);
  const more = el("button", "icon-btn bordered");
  more.append(icon("Ellipsis", 16));
  more.title = "More";
  more.onclick = (e) => { e.stopPropagation(); menu(more, p); };
  const running = !!p.checkin?.running;
  const now = el("button", "chip-btn bordered checkin-now", running ? "Checking in…" : "Check in now") as HTMLButtonElement;
  now.disabled = running || p.status === "paused";
  now.title = p.status === "paused" ? "Resume it to check in" : "Have it look at everything now and report back";
  now.onclick = async () => {
    now.disabled = true;
    now.textContent = "Checking in…";
    if (!(await post(`projects/${p.id}/checkin`))) { now.disabled = false; now.textContent = "Check in now"; }
  };
  box.append(text, now, more);
}

function menu(anchor: HTMLElement, p: Json) {
  const pop = document.getElementById("popover")!;
  const box = el("div", "menu");
  const item = (glyph: "Pencil" | "Pause" | "Play" | "Archive" | "Activity", label: string, run: () => void) => {
    const b = el("button", "menu-item");
    b.append(icon(glyph, 15), el("span", "menu-text", label));
    b.onclick = (e) => { e.stopPropagation(); pop.hidden = true; run(); };
    box.append(b);
  };
  item("Pencil", "Edit scout", () => openNew(p));
  item("Activity", "All activity", () => host.showBoard());
  if (p.status === "paused") item("Play", "Resume", () => post(`projects/${p.id}/resume`));
  else item("Pause", "Pause", () => post(`projects/${p.id}/pause`));
  item("Archive", "Retire scout", () => {
    if (confirm(`Retire ${nameOf(p)}? Its chat stays as an ordinary one and nothing on disk is deleted.`)) post(`projects/${p.id}`, "DELETE");
  });
  pop.replaceChildren(box);
  pop.style.width = "220px";
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  pop.style.left = `${Math.min(r.right - 220, window.innerWidth - 228)}px`;
  pop.style.top = `${r.bottom + 6}px`;
}

async function post(path: string, method = "POST", body?: Json) {
  try { return await host.http(method, path, undefined, body); } catch (e) { host.toast(String(e)); return null; }
}

/** The rail: an open thread's panel, else what needs you, today's activity, the leash and the memory. */
export function drawRail(box: HTMLElement, p: Json) {
  box.replaceChildren();
  const open = agents.selectedIn(p.id);
  if (open) { box.classList.add("with-thread"); box.append(agents.threadPanel(open)); return; }
  box.classList.remove("with-thread");

  const needs = agents.threadsOf(p.id).filter((t) => ["waiting_on_you", "ready_for_review"].includes(t.state) && !notNow.has(notKey(t)));
  const asks: Json[] = p.approvals ?? [];
  const s1 = railSection("Needs you", needs.length + asks.length ? String(needs.length + asks.length) : "");
  if (!needs.length && !asks.length) s1.append(el("p", "rail-empty", "Nothing. It'll ask here when it needs a yes or has work to look at."));
  for (const a of asks) s1.append(approvalCard(p, a));
  for (const t of needs) s1.append(needCard(p, t));
  box.append(s1);

  const events = activity.get(p.id);
  const s2 = railSection("Activity", "");
  if (!events) s2.append(el("p", "rail-empty", "Loading…"));
  else if (!events.length) s2.append(el("p", "rail-empty", "Nothing yet today."));
  else {
    const list = el("ol", "timeline");
    for (const e of events.slice(0, 12)) {
      const li = el("li", `ev ev-${e.kind}`);
      li.append(el("span", "ev-dot"), el("span", "ev-text", e.text), el("span", "ev-when", clock(e.at)));
      if (e.thread) li.onclick = () => agents.selectThread(p.id, e.thread);
      list.append(li);
    }
    s2.append(list);
  }
  box.append(s2);

  const s3 = railSection("Leash", "");
  const seg = el("div", "segmented leash-seg");
  for (const [leash, , label] of LEASH) {
    const b = el("button", p.leash === leash ? "on" : "", label);
    b.onclick = async () => {
      seg.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      await post("projects", "POST", { id: p.id, settings: { leash } });
    };
    seg.append(b);
  }
  s3.append(seg, el("p", "rail-note", {
    on_call: "Works only when you give it something.",
    checkins: "Starts small, clear work by itself; asks before anything big.",
    autonomous: "Starts, continues and splits work as it sees fit, within its rules.",
  }[p.leash as string] ?? ""));
  s3.append(rulesBox(p));
  const sched = p.checkin?.schedule;
  const row = (label: string, value: HTMLElement | string) => {
    const r = el("div", "rail-row");
    r.append(el("span", "dim", label), typeof value === "string" ? el("span", "", value) : value);
    return r;
  };
  const change = el("button", "link-btn", sched ? scheduleWords(sched) : "Off");
  change.title = "Change when it checks in";
  change.onclick = () => {
    const editor = scheduleEditor(sched, async (body) => {
      const r = await post(`projects/${p.id}/checkins`, "POST", body);
      if (r) { Object.assign(p, r); host.changed(); }
    });
    change.replaceWith(editor);
  };
  s3.append(row("Check-ins", change));
  if (sched?.next_run && p.leash !== "on_call" && p.status !== "paused") s3.append(row("Next", nextWords(sched.next_run)));
  else if (sched && p.leash === "on_call") s3.append(el("p", "rail-note", "On call: it skips its check-ins until the leash is longer."));
  if (p.checkin?.last) s3.append(row("Last", `${clock(p.checkin.last.started)} · ${p.checkin.last.needs_you ? `${p.checkin.last.needs_you} needed you` : "nothing needed you"}`));
  if (p.tokens_out) s3.append(row("Usage", `${tokens(p.tokens_out)} written by its threads`));
  box.append(s3);

  const facts = memory.get(p.id) ?? [];
  const s4 = railSection("Remembers", facts.length ? String(facts.length) : "");
  const list = el("div", "facts");
  facts.forEach((line, i) => {
    const row = el("div", "fact");
    row.append(el("span", "fact-text", factText(line)));
    row.title = line.replace(/^- /, "");
    const x = el("button", "icon-btn");
    x.append(icon("X", 13));
    x.title = "Forget this";
    x.onclick = async () => {
      const r = await post(`projects/${p.id}/memory/${i + 1}`, "DELETE");
      if (r) { memory.set(p.id, r.facts ?? []); host.changed(); }
    };
    row.append(x);
    list.append(row);
  });
  if (!facts.length) list.append(el("p", "rail-empty", "Nothing yet. Decisions its threads make land here."));
  const add = el("input", "fact-add") as HTMLInputElement;
  add.placeholder = "Tell it something to remember…";
  add.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter" || !add.value.trim()) return;
    const r = await post(`projects/${p.id}/memory`, "POST", { fact: add.value.trim(), source: "from you" });
    if (r) { memory.set(p.id, r.facts ?? []); add.value = ""; host.changed(); }
  });
  s4.append(list, add);
  box.append(s4);
}

function railSection(title: string, count: string): HTMLElement {
  const s = el("section", "rail-section");
  const h = el("div", "rail-head");
  h.append(el("h3", "", title));
  if (count) h.append(el("span", "rail-count", count));
  s.append(h);
  return s;
}

const notKey = (t: Json) => `${t.project}/${t.id}/${t.updated ?? 0}`;

function needCard(p: Json, t: Json): HTMLElement {
  const ready = t.state === "ready_for_review";
  const card = el("div", "need-card" + (ready ? " ready" : ""));
  card.append(el("div", "need-title", t.title || "A thread"));
  const line = ready ? t.last_result?.summary : t.question || t.last_result?.question;
  if (line) card.append(el("div", "need-line", line));
  const meta = [t.branch && `branch ${t.branch}`, t.tools ? `${t.tools} tools` : ""].filter(Boolean).join(" · ");
  if (meta) card.append(el("div", "need-meta", meta));
  const buttons = el("div", "need-buttons");
  const button = (label: string, cls: string, run: () => void) => {
    const b = el("button", cls, label);
    b.onclick = (e) => { e.stopPropagation(); run(); };
    buttons.append(b);
  };
  if (ready && t.branch) button("Land", "primary land", async () => {
    const r = await post(`projects/${p.id}/threads/${t.id}/land`);
    if (r) agents.onThread(p.id, r);
  });
  button(ready ? "Review" : "Answer", ready && t.branch ? "chip-btn bordered" : "primary", () => agents.selectThread(p.id, t.id));
  button("Not now", "chip-btn bordered", () => {
    notNow.add(notKey(t));
    notNow = new Set([...notNow].slice(-200));
    localStorage.setItem("scouts-not-now", JSON.stringify([...notNow]));
    host.changed();
  });
  card.append(buttons);
  card.onclick = () => agents.selectThread(p.id, t.id);
  return card;
}

/** A memory line as the rail shows it: no leading date, no "(thread …)" source. */
function factText(line: string): string {
  return line.replace(/^- (\d{4}-\d{2}-\d{2}: )?/, "").replace(/ \((?:thread|from) [^)]*\)$/, "");
}

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

// ---------------------------------------------------------------- the leash's rules and approvals

const RULE_WORDS: [string, string][] = [["does", "Does it"], ["asks", "Asks"], ["never", "Never"]];

/** Each action's rule (Does it / Asks / Never), from the project's `rules`; a set one can go back to the default. */
function rulesBox(p: Json): HTMLElement {
  const box = el("div", "rules");
  box.append(el("div", "rules-head", "What it may do alone"));
  for (const r of p.rules ?? []) {
    const row = el("div", "rule-row" + (r.set ? " set" : ""));
    const top = el("div", "rule-top");
    top.append(el("span", "rule-label", r.label));
    if (r.set) {
      const reset = el("button", "link-btn small", "Default");
      reset.title = "Back to what the leash says";
      reset.onclick = () => post("projects", "POST", { id: p.id, settings: { rules: { [r.action]: "default" } } });
      top.append(reset);
    }
    const seg = el("div", "segmented rule-seg");
    for (const [value, label] of RULE_WORDS) {
      const b = el("button", r.rule === value ? "on" : "", label);
      b.onclick = async () => {
        if (r.rule === value) return;
        seg.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
        b.classList.add("on");
        await post("projects", "POST", { id: p.id, settings: { rules: { [r.action]: value } } });
      };
      seg.append(b);
    }
    row.append(top, seg);
    box.append(row);
  }
  const fixed = el("div", "rule-row fixed");
  fixed.append(el("span", "rule-label", "Go past its usage budget"), el("span", "dim small", "Never"));
  box.append(fixed);
  return box;
}

/** A request to run something that asks first: what, who asks and why, with Approve / Edit / Deny. */
function approvalCard(p: Json, a: Json): HTMLElement {
  const card = el("div", "need-card approval-card");
  const rule = (p.rules ?? []).find((r: Json) => r.action === a.action);
  card.append(el("div", "need-title", `${a.title || nameOf(p)} asks to run`));
  const code = el("code", "approval-command", a.command);
  card.append(code);
  card.append(el("div", "need-meta", rule ? `${rule.label}: asks first` : a.rule ? `${a.rule} asks first` : "needs your say-so"));
  const buttons = el("div", "need-buttons");
  const decide = async (decision: string, command?: string) => {
    (document.activeElement as HTMLElement | null)?.blur(); // the rail holds still while something in it is typed into
    buttons.querySelectorAll("button").forEach((b) => ((b as HTMLButtonElement).disabled = true));
    const r = await post(`projects/${p.id}/approvals/${a.id}`, "POST", { decision, ...(command ? { command } : {}) });
    if (r) onApproval(p.id, r);
    else buttons.querySelectorAll("button").forEach((b) => ((b as HTMLButtonElement).disabled = false));
  };
  const button = (label: string, cls: string, run: () => void) => {
    const b = el("button", cls, label);
    b.onclick = (e) => { e.stopPropagation(); run(); };
    buttons.append(b);
  };
  button("Approve", "primary", () => decide("approve"));
  if (a.tool === "Bash") button("Edit", "chip-btn bordered", () => {
    const input = el("textarea", "approval-edit") as HTMLTextAreaElement;
    input.value = a.command;
    input.rows = 2;
    code.replaceWith(input);
    input.focus();
    buttons.replaceChildren();
    button("Approve edited", "primary", () => decide("approve", input.value.trim()));
    button("Cancel", "chip-btn bordered", () => { input.blur(); host.changed(); });
  });
  button("Deny", "chip-btn bordered", () => decide("deny"));
  card.append(buttons);
  card.onclick = null;
  return card;
}

// ---------------------------------------------------------------- check-ins

const DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "every morning at 8:00", "weekdays at 9:00", "every 2 hours". */
export function scheduleWords(s: Json): string {
  if (!s) return "Off";
  if (s.enabled === false) return "Off";
  const at = s.time ? s.time.replace(/^0/, "") : "";
  if (s.repeat === "every") {
    const m = Number(s.every_minutes) || 60;
    return m % 60 ? `every ${m} minutes` : m === 60 ? "every hour" : `every ${m / 60} hours`;
  }
  if (s.repeat === "weekdays") return `weekdays at ${at}`;
  if (s.repeat === "weekly") return `${DAY[s.weekday ?? 0]}s at ${at}`;
  if (s.repeat === "daily") {
    const h = Number((s.time ?? "0").split(":")[0]);
    return `every ${h < 12 ? "morning" : h < 17 ? "afternoon" : "evening"} at ${at}`;
  }
  return "once";
}

/** When the next check-in is: "in 40 min", "14:00", "8:00 tomorrow", "Mon 9:00". */
function nextWords(ts: number): string {
  const at = new Date(ts * 1000), now = new Date();
  const mins = Math.round((ts * 1000 - now.getTime()) / 60000);
  if (mins < 60) return `in ${Math.max(1, mins)} min`;
  const time = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (at.toDateString() === now.toDateString()) return time;
  if (at.toDateString() === new Date(now.getTime() + 86400000).toDateString()) return `${time} tomorrow`;
  return `${at.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

function tokens(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M tokens` : n >= 1000 ? `${Math.round(n / 1000)}k tokens` : `${n} tokens`;
}

/** Off, every morning at a time, weekdays at a time, or every few hours; `save` gets what POST …/checkins takes. */
function scheduleEditor(current: Json | null | undefined, save: (body: Json) => void, live = false): HTMLElement {
  const box = el("div", "sched-editor");
  const kind = el("select", "sched-kind") as HTMLSelectElement;
  for (const [v, label] of [["off", "Off"], ["daily", "Every day at"], ["weekdays", "Weekdays at"], ["every", "Every"]]) {
    const o = el("option", "", label) as HTMLOptionElement;
    o.value = v;
    kind.append(o);
  }
  kind.value = current && current.enabled !== false ? (current.repeat === "weekly" || current.repeat === "once" ? "daily" : current.repeat) : "off";
  const time = el("input", "sched-time") as HTMLInputElement;
  time.type = "time";
  time.value = current?.time ?? "08:00";
  const hours = el("select", "sched-hours") as HTMLSelectElement;
  for (const h of [1, 2, 3, 4, 6, 8, 12]) {
    const o = el("option", "", h === 1 ? "hour" : `${h} hours`) as HTMLOptionElement;
    o.value = String(h * 60);
    hours.append(o);
  }
  hours.value = String(current?.every_minutes && [60, 120, 180, 240, 360, 480, 720].includes(current.every_minutes) ? current.every_minutes : 120);
  const body = (): Json => kind.value === "off" ? { repeat: "off" }
    : kind.value === "every" ? { repeat: "every", every_minutes: Number(hours.value) } : { repeat: kind.value, time: time.value || "08:00" };
  const shown = () => { time.hidden = !["daily", "weekdays"].includes(kind.value); hours.hidden = kind.value !== "every"; };
  shown();
  box.append(kind, time, hours);
  if (live) {
    for (const c of [kind, time, hours]) c.addEventListener("change", () => { shown(); save(body()); });
    save(body());
  } else {
    kind.addEventListener("change", shown);
    const ok = el("button", "chip-btn bordered", "Save");
    ok.onclick = () => save(body());
    box.append(ok);
  }
  return box;
}

/** A check-in as a card: when it ran, what it says, how many things need the user. */
export function checkinCard(c: Json, p: Json): HTMLElement {
  const card = el("div", "report-card checkin-card");
  const head = el("div", "report-head");
  const needs = Number(c.needs_you) || 0;
  head.append(buddy(scoutOf(p), c.text ? (needs ? "needs_you" : "proud") : "waking", 22, { still: true }), el("b", "", c.label || "Check-in"));
  if (needs) head.append(el("span", "pill ask", `${needs} need${needs === 1 ? "s" : ""} you`));
  if (c.started) {
    const mins = c.ended ? Math.max(1, Math.round((c.ended - c.started) / 60)) : 0;
    head.append(el("span", "report-when", c.ended ? `${clock(c.started)} – ${clock(c.ended)} · ${mins} min` : clock(c.started)));
  }
  card.append(head);
  if (c.text) {
    const body = markdown(c.text);
    body.classList.add("checkin-body");
    card.append(body);
    const show = el("button", "chip-btn bordered", "Show what I did");
    show.onclick = () => host.showBoard();
    card.append(show);
  } else {
    const going = el("div", "checkin-going dim");
    going.append(el("span", "state running"), el("span", "", "Checking in…"));
    card.append(going);
  }
  return card;
}

// ---------------------------------------------------------------- report cards

const REPORT = /^(?:Thread (“.+)|(\d+) threads stopped: (.+))$/s;

/** A thread report (the note a report turn shows in the chat) drawn as a card, or null for any other note. */
export function reportCard(text: string, p: Json): HTMLElement | null {
  const m = REPORT.exec(text.trim());
  if (!m) return null;
  const lines = m[1] ? [m[1]] : m[3].split(" · ");
  const needs = lines.filter((l) => / needs you: /.test(l)).length;
  const failed = lines.some((l) => / stopped: /.test(l));
  const card = el("div", "report-card");
  const head = el("div", "report-head");
  head.append(buddy(scoutOf(p), needs ? "needs_you" : failed ? "worried" : "proud", 22, { still: true }),
    el("b", "", lines.length === 1 ? "Report" : `${lines.length} threads reported`));
  if (needs) head.append(el("span", "pill ask", `${needs} need${needs === 1 ? "s" : ""} you`));
  card.append(head);
  const list = el("ul", "report-list");
  for (const line of lines) {
    const q = /^“(.+?)” (.*)$/s.exec(line);
    const li = el("li", "");
    if (q) {
      li.append(el("b", "", q[1]), document.createTextNode(" " + q[2]));
      const t = agents.threadsOf(p.id).find((x) => x.title === q[1]);
      if (t) { li.classList.add("linked"); li.onclick = () => agents.selectThread(p.id, t.id); }
    } else li.textContent = line;
    list.append(li);
  }
  card.append(list);
  const show = el("button", "chip-btn bordered", "Show what I did");
  show.onclick = () => host.showBoard();
  card.append(show);
  return card;
}

// ---------------------------------------------------------------- New scout (and Edit scout)

const HUES = [205, 32, 268, 140, 340, 175, 52, 300];
const NAMES = ["Atlas", "Juniper", "Wren", "Moss", "Pip", "Fern", "Orion", "Clover", "Basil", "Rowan", "Indigo", "Maple", "Sable", "Tansy", "Echo", "Nova"];
const EXAMPLES = [
  "Keep this repo's issues triaged: fix the small ones, draft PRs for the rest",
  "Watch my homelab and tell me when something looks off",
  "Keep the dependencies up to date, one tidy PR at a time",
];

/** The sheet for a new scout, or for editing `edit`'s name, colour, trait, goal and leash. */
export function openNew(edit?: Json) {
  const others = live().filter((p) => p.id !== edit?.id).map((p) => p.scout ?? {});
  const takenNames = new Set(others.map((s) => String(s.name ?? "").toLowerCase()));
  const usedHues = others.map((s) => s.hue).filter((h) => Number.isFinite(h));
  const gap = (h: number) => Math.min(360, ...usedHues.map((u) => Math.min(Math.abs(h - u), 360 - Math.abs(h - u))));
  const fewest = TRAITS.map((t) => [t, others.filter((s) => s.trait === t).length] as const).sort((a, b) => a[1] - b[1])[0][0];
  const state = {
    goal: edit?.goal ?? "",
    name: edit?.scout?.name ?? NAMES.find((n) => !takenNames.has(n.toLowerCase())) ?? "Scout",
    hue: edit?.scout?.hue ?? [...HUES].sort((a, b) => gap(b) - gap(a))[0],
    trait: edit?.scout?.trait ?? fewest,
    leash: edit?.leash ?? "checkins",
    workdir: "", folder: edit?.folder ?? "",
  };
  const sheet = document.getElementById("settings")!;
  const card = document.getElementById("settings-card")!;
  card.classList.add("scout-sheet");
  const close = () => { sheet.hidden = true; card.classList.remove("scout-sheet"); };

  const head = el("div", "set-head");
  head.append(el("h2", "", edit ? `Edit ${nameOf(edit)}` : "New scout"));
  const x = el("button", "icon-btn");
  x.append(icon("X", 16));
  x.onclick = close;
  head.append(x);

  const body = el("div", "set-body scout-form");
  const preview = el("div", "scout-preview");
  const drawPreview = () => {
    preview.replaceChildren(buddy({ hue: state.hue, trait: state.trait }, edit ? "proud" : "curious", 96, { pokeable: true }),
      el("div", "preview-name", state.name || "Scout"));
    for (const b of traitRow.querySelectorAll<HTMLElement>("button")) {
      b.classList.toggle("on", b.dataset.trait === state.trait);
      b.replaceChildren(buddy({ hue: state.hue, trait: b.dataset.trait }, "asleep", 34, { still: true }), el("span", "", b.dataset.trait!));
    }
    for (const b of hueRow.querySelectorAll<HTMLElement>("button")) b.classList.toggle("on", Number(b.dataset.hue) === state.hue);
  };

  const goal = el("textarea", "scout-goal-input") as HTMLTextAreaElement;
  goal.rows = 3;
  goal.placeholder = "What should it work on? A goal it can keep chipping away at.";
  goal.value = state.goal;
  goal.oninput = () => (state.goal = goal.value);
  const examples = el("div", "chips small-chips");
  if (!edit) for (const ex of EXAMPLES) {
    const b = el("button", "chip", ex);
    b.onclick = () => { goal.value = state.goal = ex; goal.focus(); };
    examples.append(b);
  }

  const nameInput = el("input", "scout-name-input") as HTMLInputElement;
  nameInput.value = state.name;
  nameInput.maxLength = 24;
  nameInput.oninput = () => { state.name = nameInput.value; preview.querySelector(".preview-name")!.textContent = state.name || "Scout"; };

  const hueRow = el("div", "hue-row");
  for (const h of HUES) {
    const b = el("button", "hue");
    b.dataset.hue = String(h);
    b.style.background = `hsl(${h} 85% 60%)`;
    b.title = "Colour";
    b.onclick = () => { state.hue = h; drawPreview(); };
    hueRow.append(b);
  }
  const traitRow = el("div", "trait-row");
  for (const t of TRAITS) {
    const b = el("button", "trait");
    b.dataset.trait = t;
    b.onclick = () => { state.trait = t; drawPreview(); };
    traitRow.append(b);
  }

  const folder = el("button", "chip-btn bordered folder-pick") as HTMLButtonElement;
  const drawFolder = () => folder.replaceChildren(icon("Folder", 14), el("span", "", state.folder || "Choose a folder"));
  drawFolder();
  folder.disabled = !!edit;
  folder.title = edit ? "A scout stays in the folder it started in" : "Where it works";
  folder.onclick = (e) => { e.stopPropagation(); host.pickFolder(folder, (path, label) => { state.workdir = path; state.folder = label; drawFolder(); }); };
  if (!edit) host.http("GET", "folders").then((f) => { if (!state.workdir) { state.workdir = f.path; state.folder = f.label; drawFolder(); } }).catch(() => {});

  const leash = el("div", "segmented");
  for (const [value, , label] of LEASH) {
    const b = el("button", value === state.leash ? "on" : "", label);
    b.onclick = () => { leash.querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); state.leash = value; };
    leash.append(b);
  }

  let schedule: Json = edit ? { repeat: "keep" } : { repeat: "daily", time: "08:00" };
  const scheduleBox = scheduleEditor(edit ? edit.checkin?.schedule : { repeat: "daily", time: "08:00", enabled: true }, (b) => (schedule = b), true);

  const field = (label: string, ...controls: HTMLElement[]) => {
    const f = el("div", "scout-field");
    f.append(el("label", "", label), ...controls);
    return f;
  };
  const who = el("div", "scout-who");
  const whoFields = el("div", "scout-who-fields");
  whoFields.append(field("Name", nameInput), field("Colour", hueRow), field("Look", traitRow));
  who.append(preview, whoFields);
  body.append(field("Goal", goal, examples), who, field("Folder", folder), field("Leash", leash), field("Check-ins", scheduleBox));

  const foot = el("div", "scout-foot");
  const error = el("p", "error small");
  error.hidden = true;
  const go = el("button", "primary", edit ? "Save" : "Send scout") as HTMLButtonElement;
  go.onclick = async () => {
    if (!state.goal.trim()) { error.hidden = false; error.textContent = "Give it a goal first."; goal.focus(); return; }
    go.disabled = true;
    const scout = { name: state.name.trim() || undefined, hue: state.hue, trait: state.trait };
    const bodyOut: Json = edit
      ? { id: edit.id, goal: state.goal.trim(), scout, settings: { leash: state.leash } }
      : { goal: state.goal.trim(), workdir: state.workdir || undefined, scout, settings: { leash: state.leash } };
    try {
      const p = await host.http("POST", "projects", undefined, bodyOut);
      if (schedule.repeat !== "keep" && (!edit || JSON.stringify(schedule) !== JSON.stringify(edit.checkin?.schedule))) {
        Object.assign(p, await host.http("POST", `projects/${p.id}/checkins`, undefined, schedule).catch(() => ({})));
      }
      close();
      if (!edit) host.open(p.id, p);
    } catch (e) {
      error.hidden = false;
      error.textContent = String(e);
      go.disabled = false;
    }
  };
  const cancel = el("button", "link", "Cancel");
  cancel.onclick = close;
  foot.append(error, cancel, go);

  card.replaceChildren(head, body, foot);
  drawPreview();
  sheet.hidden = false;
  if (!edit) goal.focus();
}
