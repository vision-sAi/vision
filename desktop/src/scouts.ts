// Scouts: every project is a scout, someone with a name, a buddy avatar (buddy.ts) and a
// leash, that works on its goal in the background. This module draws the sidebar's Scouts section, a scout's
// page around its project chat (the header, report cards, and a rail with what needs you, today's activity,
// the leash and what it remembers) and the New scout sheet. main.ts owns the connection and the chat itself.

import * as agents from "./agents";
import { Mood, Scout, TRAITS, buddy } from "./buddy";
import { icon } from "./icons";
import { Json, el, markdown, placeFixed } from "./render";

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
/** Retired scouts: off the home page, dimmed at the foot of the sidebar, with a Bring back. */
export const retired = (): Json[] => host.projects().filter((p) => p.status === "archived");
export const isRetired = (p: Json) => p.status === "archived";
export const project = (pid: string | null) => (pid ? host.projects().find((p) => p.id === pid) : undefined);
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

// The one choice of how far a scout goes alone (its leash underneath): ask before big changes, or don't.
const ASK_LABEL = "Ask me before big changes";
const ASK_NOTE = "Off, it pushes, deletes and merges without asking.";
const asksFirst = (leash: string) => leash !== "autonomous";
const leashOf = (asking: boolean) => (asking ? "checkins" : "autonomous");
/** A switch, `on` to start with; `change` gets each flip. */
function askSwitch(on: boolean, change: (on: boolean) => void): HTMLElement {
  const row = el("label", "ask-row");
  const t = el("button", "toggle" + (on ? " on" : "")) as HTMLButtonElement;
  t.type = "button";
  t.setAttribute("role", "switch");
  t.setAttribute("aria-checked", String(on));
  t.onclick = () => { on = !on; t.classList.toggle("on", on); t.setAttribute("aria-checked", String(on)); change(on); };
  row.append(el("span", "", ASK_LABEL), t);
  return row;
}

/** How many things wait on the user: its threads' questions and work to look at, and its approval requests. */
function needCount(p: Json): number {
  const t = p.threads ?? {};
  return (t.waiting_on_you ?? 0) + (t.ready_for_review ?? 0) + (p.approvals?.length ?? 0);
}

/** What a scout is up to, in a word or two, and its colour: `ask` (amber) when something waits on you,
 * `busy` (violet) while it works, `done` (green) after it found or landed something, `failed`, or `quiet`. */
export function stateOf(p: Json): [string, string] {
  if (isRetired(p)) return ["retired", "quiet"];
  const n = needCount(p);
  if (n) return [`${n} need${n === 1 ? "s" : ""} you`, "ask"];
  const mood = moodOf(p);
  const word = ({ worried: "something failed", focused: "working", curious: "reading up", proud: "landed something", found_it: "found it",
    waking: "waking up", out_cold: "paused while asleep" } as Record<string, string>)[mood];
  if (word) return [word, mood === "worried" ? "failed" : ["proud", "found_it"].includes(mood) ? "done" : mood === "out_cold" ? "quiet" : "busy"];
  return [p.status === "paused" ? "paused" : p.leash === "on_call" ? "resting"
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
  const matches = (p: Json) => !query || `${nameOf(p)} ${p.title ?? ""} ${p.goal ?? ""}`.toLowerCase().includes(query);
  const byName = (a: Json, b: Json) => nameOf(a).localeCompare(nameOf(b));
  const all = [...live().filter(matches).sort(byName), ...retired().filter(matches).sort(byName)];
  if (query && !all.length) return;  // searching, and no scout matches: no heading over nothing
  const head = el("div", "group scouts-head");
  head.append(el("span", "", "Scouts"));
  const needs = all.reduce((n, p) => n + needCount(p), 0);  // things, not scouts: the same count as each row
  if (needs) {
    const b = el("button", "needs-link", `${needs} need${needs === 1 ? "s" : ""} you`);
    b.title = "Everything your scouts are waiting on you for";
    b.onclick = () => openInbox();
    head.append(b);
  }
  nav.append(head);
  inboxChanged();
  if (!all.length && !query) {
    const first = el("button", "first-scout");
    first.append(buddy({ hue: 205, trait: "sprout" }, "asleep", 34, { still: true }));
    const text = el("span", "first-text");
    text.append(el("b", "", "Send your first scout"), el("span", "dim small", "Give it a goal; it works on it in the background"));
    first.append(text);
    first.onclick = () => openNew();
    nav.append(first);
    return;
  }
  for (const p of all) {
    const on = onPage && p.id === current;
    const mood = isRetired(p) ? "asleep" : moodOf(p);
    const quiet = !on && !["needs_you", "still_waiting", "found_it", "worried"].includes(mood);
    const item = el("div", "chat-item scout" + (on ? " active" : "") + (isRetired(p) ? " retired" : ""));
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
  const waiting = all.reduce((n, p) => n + needCount(p), 0);
  head.append(el("span", "", "Your scouts"));
  if (waiting) {
    const b = el("button", "home-add needs", `${waiting} need${waiting === 1 ? "s" : ""} you`);
    b.onclick = () => openInbox();
    head.append(b);
  }
  head.append(add);
  box.append(head, grid);
}

// ---------------------------------------------------------------- the scout's page

/** The page's header: the buddy, its name and goal, and the chips for its leash, folder and model. */
export function drawHead(box: HTMLElement, p: Json) {
  box.replaceChildren();
  box.append(keptBuddy("head", p, moodOf(p), 40, { pokeable: true }));
  const text = el("div", "scout-titles");
  const name = el("h1");
  const gone = isRetired(p);
  const leash = el("span", "scout-leash" + (p.status === "paused" || gone ? " paused" : ""),
    gone ? "Retired" : p.status === "paused" ? "Paused" : "");
  leash.hidden = !gone && p.status !== "paused";
  leash.title = gone ? "Retired: it doesn't work or send status reports until it's brought back" : "";
  name.append(el("span", "scout-name", nameOf(p)), leash);
  const goal = el("p", "scout-goal", p.goal ?? "");
  const chat = host.chatOf(p);
  goal.title = [p.goal, p.folder, chat?.model && [chat.model, chat.effort].filter(Boolean).join(" · ")].filter(Boolean).join("\n");
  text.append(name, goal);
  const more = el("button", "icon-btn bordered");
  more.append(icon("Ellipsis", 16));
  more.title = "More";
  more.onclick = (e) => { e.stopPropagation(); menu(more, p); };
  if (gone) {
    const back = el("button", "chip-btn bordered checkin-now", "Bring back") as HTMLButtonElement;
    back.title = "Make it active again, with its chat, memory and status reports as they were";
    back.onclick = async () => {
      back.disabled = true;
      if (await post(`projects/${p.id}/resume`)) host.toast(`${nameOf(p)} is back`);
      else back.disabled = false;
    };
    const del = el("button", "chip-btn bordered danger", "Delete") as HTMLButtonElement;
    del.title = "Delete it for good";
    del.onclick = (e) => { e.stopPropagation(); menu(more, p, true); };
    box.append(text, back, del, more);
    return;
  }
  const running = !!p.checkin?.running;
  const now = el("button", "chip-btn bordered checkin-now", running ? "Reporting…" : "Status report now") as HTMLButtonElement;
  now.disabled = running || p.status === "paused";
  now.title = p.status === "paused" ? "Resume it to get a status report" : "Have it look at everything now and report back";
  now.onclick = async () => {
    now.disabled = true;
    now.textContent = "Reporting…";
    if (!(await post(`projects/${p.id}/checkin`))) { now.disabled = false; now.textContent = "Status report now"; }
  };
  box.append(text, now, more);
}

/** The ⋯ menu; `deleting` opens it straight at the Delete question. */
function menu(anchor: HTMLElement, p: Json, deleting = false) {
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
  if (isRetired(p)) item("Play", "Bring back", () => post(`projects/${p.id}/resume`));
  else if (p.status === "paused") item("Play", "Resume", () => post(`projects/${p.id}/resume`));
  else item("Pause", "Pause", () => post(`projects/${p.id}/pause`));
  // Tauri's webview has no window.confirm (it returns false unseen), so these ask in the menu itself.
  const ask = (glyph: "Archive" | "Trash2", label: string, question: string, verb: string, doing: string, run: () => Promise<unknown>) => {
    const b = el("button", "menu-item" + (glyph === "Trash2" ? " danger" : ""));
    b.append(icon(glyph, 15), el("span", "menu-text", label));
    b.onclick = (e) => {
      e.stopPropagation();
      const box2 = el("div", "menu-confirm");
      const row = el("div", "menu-confirm-row");
      const cancel = el("button", "chip-btn bordered", "Cancel");
      cancel.onclick = (ev) => { ev.stopPropagation(); pop.hidden = true; };
      const yes = el("button", "chip-btn bordered danger", verb) as HTMLButtonElement;
      yes.onclick = async (ev) => {
        ev.stopPropagation();
        yes.disabled = true;
        yes.textContent = doing;
        await run();
        pop.hidden = true;
      };
      row.append(cancel, yes);
      box2.append(el("p", "menu-confirm-text", question), row);
      box.replaceChildren(box2);
    };
    box.append(b);
    return b;
  };
  const last = isRetired(p) ? ask("Trash2", "Delete for good", deleteQuestion(p), "Delete", "Deleting…", () => remove(p))
    : ask("Archive", "Retire scout", `Retire ${nameOf(p)}? It stops working and sending status reports until you bring it back. Nothing is deleted.`,
    "Retire", "Retiring…", async () => { if (await post(`projects/${p.id}`, "DELETE")) host.toast(`${nameOf(p)} retired`); });
  pop.replaceChildren(box);
  placeFixed(pop, anchor, { width: 220, align: "right", prefer: "below", gap: 6 });
  if (deleting) last.click();
}

const deleteQuestion = (p: Json) =>
  `Delete ${nameOf(p)} for good? Its chat, threads, memory and status reports go. Files in ${p.folder || "its folder"} stay.`;

/** Delete a retired scout for good, saying so (and which worktrees were kept because they hold work). */
async function remove(p: Json) {
  const r = await post(`projects/${p.id}/delete`);
  if (!r) return;
  host.toast(r.kept?.length ? `${nameOf(p)} deleted. ${r.kept.join("; ")}` : `${nameOf(p)} deleted`);
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

  const s3 = railSection("Schedule", "");
  s3.append(askSwitch(asksFirst(p.leash), (on) => { void post("projects", "POST", { id: p.id, settings: { leash: leashOf(on) } }); }),
    el("p", "rail-note", ASK_NOTE));
  s3.append(rulesBox(p));
  const sched = p.checkin?.schedule;
  const row = (label: string, value: HTMLElement | string) => {
    const r = el("div", "rail-row");
    r.append(el("span", "dim", label), typeof value === "string" ? el("span", "", value) : value);
    return r;
  };
  const change = el("button", "link-btn", sched ? scheduleWords(sched) : "Off");
  change.title = "Change when it sends a status report";
  change.onclick = () => {
    const editor = scheduleEditor(sched, async (body) => {
      const r = await post(`projects/${p.id}/checkins`, "POST", body);
      if (r) { Object.assign(p, r); host.changed(); }
    });
    change.replaceWith(editor);
  };
  s3.append(row("Status reports", change));
  if (sched?.next_run && p.leash !== "on_call" && p.status !== "paused") s3.append(row("Next", nextWords(sched.next_run)));
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
      reset.title = "Back to what its setting says";
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

/** Off, every morning at a time, weekdays, one day a week, or every few hours; `save` gets what POST …/checkins takes. */
function scheduleEditor(current: Json | null | undefined, save: (body: Json) => void, live = false): HTMLElement {
  const box = el("div", "sched-editor");
  const kind = el("select", "sched-kind") as HTMLSelectElement;
  for (const [v, label] of [["off", "Off"], ["daily", "Every day at"], ["weekdays", "Weekdays at"], ["weekly", "Every week on"], ["every", "Every"]]) {
    const o = el("option", "", label) as HTMLOptionElement;
    o.value = v;
    kind.append(o);
  }
  kind.value = current && current.enabled !== false ? (current.repeat === "once" ? "daily" : current.repeat) : "off";
  const day = el("select", "sched-day") as HTMLSelectElement;
  DAY.forEach((d, i) => {
    const o = el("option", "", d) as HTMLOptionElement;
    o.value = String(i);
    day.append(o);
  });
  day.value = String(current?.weekday ?? 0);
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
    : kind.value === "every" ? { repeat: "every", every_minutes: Number(hours.value) }
    : { repeat: kind.value, time: time.value || "08:00", ...(kind.value === "weekly" ? { weekday: Number(day.value) } : {}) };
  const shown = () => {
    time.hidden = !["daily", "weekdays", "weekly"].includes(kind.value);
    day.hidden = kind.value !== "weekly";
    hours.hidden = kind.value !== "every";
  };
  shown();
  box.append(kind, day, time, hours);
  if (live) {
    for (const c of [kind, day, time, hours]) c.addEventListener("change", () => { shown(); save(body()); });
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
  head.append(buddy(scoutOf(p), c.text ? (needs ? "needs_you" : "proud") : "waking", 22, { still: true }), el("b", "", c.label || "Status report"));
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
    going.append(el("span", "state running"), el("span", "", "Reporting…"));
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

/** A new scout (the one-sentence sheet), or the full form for editing `edit`. */
export function openNew(edit?: Json) {
  if (edit) openForm(edit);
  else openQuick();
}

/** The full form: a new scout's every choice (`prefill` carries over what the quick sheet had), or editing
 * `edit`'s name, colour, trait, goal and leash. */
function openForm(edit?: Json, prefill?: { goal?: string; reports?: Json; workdir?: string; folder?: string }) {
  const others = live().filter((p) => p.id !== edit?.id).map((p) => p.scout ?? {});
  const takenNames = new Set(others.map((s) => String(s.name ?? "").toLowerCase()));
  const usedHues = others.map((s) => s.hue).filter((h) => Number.isFinite(h));
  const gap = (h: number) => Math.min(360, ...usedHues.map((u) => Math.min(Math.abs(h - u), 360 - Math.abs(h - u))));
  const fewest = TRAITS.map((t) => [t, others.filter((s) => s.trait === t).length] as const).sort((a, b) => a[1] - b[1])[0][0];
  const state = {
    goal: edit?.goal ?? prefill?.goal ?? "",
    name: edit?.scout?.name ?? NAMES.find((n) => !takenNames.has(n.toLowerCase())) ?? "Scout",
    hue: edit?.scout?.hue ?? [...HUES].sort((a, b) => gap(b) - gap(a))[0],
    trait: edit?.scout?.trait ?? fewest,
    leash: edit?.leash ?? "checkins",
    workdir: prefill?.workdir ?? "", folder: edit?.folder ?? prefill?.folder ?? "",
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
  // One example from the pool sits in the empty box; Tab takes it.
  const example = EXAMPLES[Math.floor(Math.random() * EXAMPLES.length)];
  goal.placeholder = edit ? "What should it work on?" : example;
  goal.value = state.goal;
  goal.oninput = () => (state.goal = goal.value);
  goal.onkeydown = (e) => {
    if (e.key === "Tab" && !e.shiftKey && !edit && !goal.value) { e.preventDefault(); goal.value = state.goal = example; }
  };

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
  // Optional: with none it works in the default folder (a news brief needs none). How much it does alone only
  // matters for a scout working on files, so that choice shows once a folder is picked (or when editing).
  let leashField: HTMLElement | null = null;
  const drawFolder = () => {
    folder.replaceChildren(icon("Folder", 14), el("span", "", state.folder || "None: for code or files, pick one"));
    if (leashField && !edit) leashField.hidden = !state.workdir;
  };
  folder.disabled = !!edit;
  folder.title = edit ? "A scout stays in the folder it started in" : "Where it works (optional)";
  folder.onclick = (e) => { e.stopPropagation(); host.pickFolder(folder, (path, label) => { state.workdir = path; state.folder = label; drawFolder(); }); };

  const leash = askSwitch(asksFirst(state.leash), (on) => (state.leash = leashOf(on)));

  const start: Json = prefill?.reports ?? { repeat: "daily", time: "08:00" };
  let schedule: Json = edit ? { repeat: "keep" } : start;
  const scheduleBox = scheduleEditor(edit ? edit.checkin?.schedule : start.repeat === "off" ? null : { ...start, enabled: true }, (b) => (schedule = b), true);

  const field = (label: string, ...controls: HTMLElement[]) => {
    const f = el("div", "scout-field");
    f.append(el("label", "", label), ...controls);
    return f;
  };
  const who = el("div", "scout-who");
  const whoFields = el("div", "scout-who-fields");
  whoFields.append(field("Name", nameInput), field("Colour", hueRow), field("Look", traitRow));
  who.append(preview, whoFields);
  leashField = field("", leash);
  drawFolder();
  body.append(field("Goal", goal), field("When it runs", scheduleBox), field(edit ? "Folder" : "Folder (optional)", folder), who, leashField);

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

// ---------------------------------------------------------------- New scout in one sentence

const QUICK_EXAMPLES = [
  "Keep the server tests green, check every morning",
  "Every weekday at 7, give me the three tech stories worth reading",
  "Watch my homelab and tell me when something looks off, every 2 hours",
  "Keep the dependencies up to date, one tidy PR at a time, weekly",
];

/** "Pip · reports every morning at 8:00": the draft's name and its words, as the one line under the box. */
const draftLine = (d: Json) => `${d.scout?.name || "Scout"} · ${String(d.words || "").replace(/^\w/, (c) => c.toLowerCase())}`;

/** New scout: say what it should do (and when, in the same words); the server reads out the schedule and
 * picks it a name, shown in one line beneath as you type. More options opens the full form with it filled in. */
function openQuick() {
  const sheet = document.getElementById("settings")!;
  const card = document.getElementById("settings-card")!;
  card.classList.add("scout-sheet", "quick-sheet");
  const close = () => { sheet.hidden = true; card.classList.remove("scout-sheet", "quick-sheet"); };
  let draft: Json | null = null;
  let workdir = "", folderLabel = "";

  const head = el("div", "set-head");
  head.append(el("h2", "", "New scout"));
  const x = el("button", "icon-btn");
  x.append(icon("X", 16));
  x.onclick = close;
  head.append(x);

  const body = el("div", "set-body scout-form");
  const text = el("textarea", "scout-goal-input quick-input") as HTMLTextAreaElement;
  text.rows = 3;
  const example = QUICK_EXAMPLES[Math.floor(Math.random() * QUICK_EXAMPLES.length)];
  text.placeholder = `What should it do? e.g. ${example}`;
  const line = el("p", "quick-line");
  const drawLine = () => { line.textContent = draft && draft.goal ? draftLine(draft) : "Say when in the same words: every morning, weekdays at 9, every 2 hours, only when I ask."; };
  let timer = 0, asked = 0;
  const refresh = () => {
    clearTimeout(timer);
    const value = text.value.trim();
    if (!value) { draft = null; drawLine(); return; }
    timer = window.setTimeout(async () => {
      const n = ++asked;
      try {
        const d = await host.http("POST", "scouts/draft", undefined, { text: value });
        if (n === asked) { draft = d; drawLine(); }
      } catch { /* an older server: the button still sends it */ }
    }, 220);
  };
  text.oninput = refresh;
  text.onkeydown = (e) => {
    if (e.key === "Tab" && !e.shiftKey && !text.value) { e.preventDefault(); text.value = example; refresh(); }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go.click(); }
  };

  const folder = el("button", "chip-btn bordered folder-pick") as HTMLButtonElement;
  const drawFolder = () => folder.replaceChildren(icon("Folder", 14), el("span", "", folderLabel || "No folder: pick one for code or files"));
  folder.title = "Where it works (optional)";
  folder.onclick = (e) => { e.stopPropagation(); host.pickFolder(folder, (path, label) => { workdir = path; folderLabel = label; drawFolder(); }); };
  drawFolder();
  drawLine();
  body.append(text, line, folder);

  const foot = el("div", "scout-foot");
  const error = el("p", "error small");
  error.hidden = true;
  const more = el("button", "link", "More options");
  more.onclick = () => {
    close();
    openForm(undefined, { goal: draft?.goal || text.value.trim(), reports: draft?.reports, workdir, folder: folderLabel });
  };
  const go = el("button", "primary", "Send scout") as HTMLButtonElement;
  go.onclick = async () => {
    const value = text.value.trim();
    if (!value) { error.hidden = false; error.textContent = "Say what it should do first."; text.focus(); return; }
    go.disabled = true;
    try {
      const p = await host.http("POST", "scouts", undefined, { text: value, workdir: workdir || undefined });
      close();
      host.open(p.id, p);
    } catch (e) {
      error.hidden = false;
      error.textContent = String(e);
      go.disabled = false;
    }
  };
  foot.append(error, more, go);
  card.replaceChildren(head, body, foot);
  sheet.hidden = false;
  text.focus();
}

// ---------------------------------------------------------------- Needs you: every scout's asks in one place

let inbox: HTMLElement | null = null; // the sheet's list while it's open
let inboxTimer = 0;

/** Something changed that might add to or clear the inbox: fetch it again, once things settle. */
export function inboxChanged() {
  if (!inbox) return;
  clearTimeout(inboxTimer);
  inboxTimer = window.setTimeout(() => void fillInbox(), 300);
}

/** The Needs you sheet: what every scout is waiting on you for (GET /needs), newest first, a row each with
 * its one next step: Approve a request, answer a question, Apply finished work. A row opens its scout. */
export function openInbox() {
  const sheet = document.getElementById("settings")!;
  const card = document.getElementById("settings-card")!;
  card.classList.add("scout-sheet", "inbox-sheet");
  const head = el("div", "set-head");
  head.append(el("h2", "", "Needs you"));
  const x = el("button", "icon-btn");
  x.append(icon("X", 16));
  const close = () => { sheet.hidden = true; inbox = null; card.classList.remove("scout-sheet", "inbox-sheet"); };
  x.onclick = close;
  head.append(x);
  inbox = el("div", "set-body inbox-list");
  inbox.append(el("p", "rail-empty", "Loading…"));
  card.replaceChildren(head, inbox);
  sheet.hidden = false;
  void fillInbox();
}

async function fillInbox() {
  const box = inbox;
  if (!box) return;
  let items: Json[];
  try { items = await host.http("GET", "needs"); } catch (e) { box.replaceChildren(el("p", "error small", String(e))); return; }
  if (box !== inbox) return;
  box.replaceChildren();
  if (!items.length) {
    const empty = el("div", "inbox-empty");
    empty.append(buddy({ hue: 140, trait: "sprout" }, "proud", 64, { still: true }), el("b", "", "Nothing needs you"),
      el("span", "dim small", "Your scouts are getting on with it. Anything that needs a yes lands here."));
    box.append(empty);
    return;
  }
  for (const n of items) box.append(inboxRow(n));
}

/** "15m", "2h", "3d". */
function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 3600 ? `${Math.max(1, Math.round(s / 60))}m` : s < 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
}

/** The two lines a need reads as: who wants what, and the thing itself. */
function needWords(n: Json): [string, string] {
  if (n.kind === "approval") return [`${n.name} wants to run`, n.text];
  if (n.kind === "question") return [`${n.name} asked`, n.text];
  return [`${n.name} finished “${n.title}”`, n.text];
}

function inboxRow(n: Json): HTMLElement {
  const row = el("div", `inbox-row kind-${n.kind}`);
  const open = () => {
    document.getElementById("settings")!.hidden = true;
    inbox = null;
    host.open(n.project);
    if (n.kind !== "approval") setTimeout(() => agents.selectThread(n.project, n.id), 200);
  };
  row.onclick = open;
  row.append(buddy({ hue: n.hue, trait: n.trait }, n.kind === "review" ? "proud" : "needs_you", 24, { still: true }));
  const text = el("div", "inbox-text");
  const [lead, detail] = needWords(n);
  const top = el("div", "inbox-lead");
  top.append(el("b", "", lead), el("span", "inbox-when", ago(n.at)));
  const detailEl = n.kind === "approval" ? el("code", "inbox-command", detail) : el("div", "inbox-detail", detail);
  text.append(top, detailEl);
  const actions = el("div", "inbox-actions");
  const error = el("p", "error small");
  error.hidden = true;
  const stop = (e: Event) => e.stopPropagation();
  const link = (label: string, run: () => Promise<unknown>) => {
    const b = el("button", "link-btn", label) as HTMLButtonElement;
    b.onclick = async (e) => {
      stop(e);
      actions.querySelectorAll("button").forEach((x) => ((x as HTMLButtonElement).disabled = true));
      error.hidden = true;
      try {
        await run();
        row.classList.add("done");
        inboxChanged();
      } catch (err) {
        error.hidden = false;
        error.textContent = String(err);
        actions.querySelectorAll("button").forEach((x) => ((x as HTMLButtonElement).disabled = false));
      }
    };
    actions.append(b);
    return b;
  };
  const decide = (decision: string, command?: string) => async () => {
    const r = await host.http("POST", `projects/${n.project}/approvals/${n.id}`, undefined, { decision, ...(command ? { command } : {}) });
    onApproval(n.project, r);
  };
  if (n.kind === "approval") {
    link("Approve", decide("approve"));
    if (n.tool === "Bash") {
      const edit = el("button", "link-btn dim", "Edit");
      edit.onclick = (e) => {
        stop(e);
        const input = el("textarea", "approval-edit") as HTMLTextAreaElement;
        input.value = n.text;
        input.rows = 2;
        input.onclick = stop;
        detailEl.replaceWith(input);
        input.focus();
        actions.replaceChildren();
        link("Approve edited", async () => decide("approve", input.value.trim())());
      };
      actions.append(edit);
    }
    link("Deny", decide("deny")).classList.add("dim");
  } else if (n.kind === "question") {
    const answer = el("input", "inbox-answer") as HTMLInputElement;
    answer.placeholder = "Answer…";
    answer.onclick = stop;
    const send = link("Send", async () => {
      if (!answer.value.trim()) { answer.focus(); throw new Error("Type an answer first."); }
      const r = await host.http("POST", `projects/${n.project}/threads/${n.id}/message`, undefined, { text: answer.value.trim() });
      agents.onThread(n.project, r);
    });
    answer.addEventListener("keydown", (e) => { if (e.key === "Enter") send.click(); });
    actions.prepend(answer);
  } else if (n.can_apply) {
    link("Apply", async () => {
      const r = await host.http("POST", `projects/${n.project}/threads/${n.id}/land`, undefined, {});
      agents.onThread(n.project, r);
    });
  }
  text.append(actions, error);
  row.append(text);
  return row;
}
