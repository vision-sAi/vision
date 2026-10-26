// The All activity board: every project's threads (vision/runs.py) by what they need, the scheduled tasks, and
// when this computer slept; a click opens a thread beside the board. main.ts owns the connection and
// hands frames in (onThread, onScheduled, onSlept); this module keeps what it is told and draws it.

import { icon } from "./icons";
import { Json, el, markdown } from "./render";

export interface AgentsHost {
  http: (method: string, path: string, query?: Json, body?: Json) => Promise<any>;
  toast: (text: string) => void;
  projectName: (p: Json) => string;
  projects: () => Json[];
  openChat: (chat: string) => void;
  /** The board's counts changed (the sidebar badge). */
  changed: () => void;
  /** A desktop notification. */
  notify: (title: string, body: string) => void;
}

let host: AgentsHost;
const threads = new Map<string, Json>(); // "<project>/<thread>" → the thread as the server last sent it
let scheduled: Json[] = [];
let asleep: { from: number; to: number }[] = [];
let selected: string | null = null; // the thread open beside the board
let tab: "conversation" | "changes" = "conversation";
let root: HTMLElement | null = null; // where the board is drawn while it is on screen

export function setHost(h: AgentsHost) {
  host = h;
}

const key = (pid: string, tid: string) => `${pid}/${tid}`;

/** Load every project's threads and the scheduled tasks (on connecting, and when the board opens). */
export async function loadAll() {
  const lists = await Promise.all(host.projects().filter((p) => p.status !== "archived").map(async (p) => {
    try { return { pid: p.id, list: await host.http("GET", `projects/${p.id}/threads`) as Json[] }; } catch { return { pid: p.id, list: [] }; }
  }));
  threads.clear();
  for (const { pid, list } of lists) for (const t of list) threads.set(key(pid, t.id), { ...t, project: pid });
  await loadScheduled();
  host.changed();
  redraw();
}

async function loadScheduled() {
  try { scheduled = await host.http("GET", "scheduled"); } catch { scheduled = []; }
}

export function onThread(pid: string, thread: Json) {
  const k = key(pid, thread.id);
  const before = threads.get(k);
  const now: Json = { ...before, ...thread, project: pid };
  threads.set(k, now);
  // Turning to need the user, or finishing with something to look at, is worth a notification.
  if (before && before.state !== now.state && (NEEDS.includes(now.state) || REVIEW.includes(now.state))) {
    const project = host.projects().find((p) => p.id === pid);
    const where = project ? host.projectName(project) : "Agents";
    host.notify(now.state === "waiting_on_you" ? `${where}: needs you` : `${where}: ready for review`,
      `${now.title || "A thread"}${lineOf(now) ? ` · ${lineOf(now)}` : ""}`);
  }
  host.changed();
  redraw();
}

export async function onScheduled() {
  await loadScheduled();
  redraw();
}

export function setAsleep(list: { from: number; to: number }[]) {
  asleep = list ?? [];
  redraw();
}

export function onSlept(gap: { from: number; to: number }) {
  asleep = [...asleep, gap];
  redraw();
}

const NEEDS = ["waiting_on_you"];
const REVIEW = ["ready_for_review", "landing"];
const WORKING = ["working"];

export function counts(): { needs: number; working: number; review: number } {
  let needs = 0, working = 0, review = 0;
  for (const t of threads.values()) {
    if (NEEDS.includes(t.state)) needs++;
    else if (REVIEW.includes(t.state)) review++;
    else if (WORKING.includes(t.state) || t.queued) working++;
  }
  return { needs, working, review };
}

export function summary(): string {
  const c = counts();
  return [c.needs && `${c.needs} need${c.needs === 1 ? "s" : ""} you`, c.review && `${c.review} ready`, c.working && `${c.working} working`]
    .filter(Boolean).join(" · ");
}

// ---------------------------------------------------------------- drawing

export function ago(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function span(seconds: number): string {
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60 ? `${m % 60} min` : ""}`.trim();
}

const STATE: Record<string, [string, string]> = {
  waiting_on_you: ["Needs you", "ask"], ready_for_review: ["Ready for review", "ready"], landing: ["Landing", "ready"],
  working: ["Working", "busy"], idle: ["Idle", "idle"], resolved: ["Done", "idle"],
};

function stateOf(t: Json): [string, string] {
  if (t.queued && t.state !== "working") return ["Queued", "busy"];
  return STATE[t.state] ?? [t.state, "idle"];
}

/** What a thread's card says under its title: its question, its result, or what it is doing. */
function lineOf(t: Json): string {
  if (t.state === "waiting_on_you") return t.question || t.last_result?.question || "Waiting for you";
  if (REVIEW.includes(t.state)) return t.last_result?.summary || "Finished; ready to look at";
  if (t.error) return t.error;
  return t.activity || t.last_result?.summary || t.objective || "";
}

let redrawn: (() => void) | null = null; // the scout page, which shows threads and this panel too

/** Something else that draws threads: told whenever the board would redraw. */
export function onRedraw(fn: () => void) {
  redrawn = fn;
}

export function redraw() {
  if (root?.isConnected) draw(root);
  redrawn?.();
}

/** A project's threads as the server last sent them, newest change first. */
export function threadsOf(pid: string): Json[] {
  return [...threads.values()].filter((t) => t.project === pid).sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
}

/** Open a thread's panel (or close it: tid null), wherever it is shown. */
export function selectThread(pid: string, tid: string | null) {
  selected = tid ? key(pid, tid) : null;
  tab = "conversation";
  redraw();
}

/** The thread open in the panel, if it belongs to this project. */
export function selectedIn(pid: string): Json | null {
  const t = selected ? threads.get(selected) : undefined;
  return t && t.project === pid ? t : null;
}

/** The panel for the open thread, to show beside something other than the board. */
export function threadPanel(t: Json): HTMLElement {
  return panel(t);
}

/** Whether the board is what's on screen (the scout page shows the panel otherwise). */
export function boardShown(): boolean {
  return !!root?.isConnected && !!root.offsetParent;
}

export function draw(box: HTMLElement) {
  root = box;
  box.replaceChildren();
  const board = el("div", "board" + (selected ? " with-panel" : ""));
  const lanes = el("div", "lanes");
  board.append(lanes);
  box.append(board);

  const banner = sleepBanner();
  if (banner) lanes.append(banner);

  const all = [...threads.values()].filter((t) => t.state !== "resolved").sort((a, b) => (b.updated ?? 0) - (a.updated ?? 0));
  const lane = (title: string, list: Json[], hint = "") => {
    if (!list.length) return;
    const section = el("section", "lane");
    const head = el("div", "lane-head");
    head.append(el("h3", "", title), el("span", "lane-count", String(list.length)));
    if (hint) head.append(el("span", "dim small", hint));
    section.append(head);
    const grid = el("div", "thread-grid");
    for (const t of list) grid.append(threadCard(t));
    section.append(grid);
    lanes.append(section);
  };
  lane("Needs you", all.filter((t) => NEEDS.includes(t.state)));
  lane("Ready for review", all.filter((t) => REVIEW.includes(t.state)));
  lane("Working", all.filter((t) => WORKING.includes(t.state) || (t.queued && !NEEDS.includes(t.state) && !REVIEW.includes(t.state))));
  lane("Idle", all.filter((t) => t.state === "idle" && !t.queued), "finished, nothing to land");

  if (!all.length) {
    const empty = el("div", "board-empty");
    empty.append(icon("Bot", 28), el("h3", "", "No agents running"),
      el("p", "dim", "This is what your scouts' threads are doing: work that runs in the background, on this computer, while you do something else. Send a scout from the sidebar to get one going."));
    lanes.append(empty);
  }

  lanes.append(scheduledSection());
  if (selected && threads.has(selected)) board.append(panel(threads.get(selected)!));
  else selected = null;
}

/** A sleep that ended in the last twelve hours: say that work paused, so a stalled thread doesn't look stuck. */
function sleepBanner(): HTMLElement | null {
  const now = Date.now() / 1000;
  const last = [...asleep].reverse().find((g) => g.to > now - 12 * 3600 && g.to - g.from > 300);
  if (!last || localStorage.getItem("sleep-seen") === String(last.to)) return null;
  const b = el("div", "sleep-banner");
  const text = el("div", "sleep-text");
  text.append(el("b", "", `This computer was asleep from ${clock(last.from)} to ${clock(last.to)} (${span(last.to - last.from)}).`),
    el("span", "", " Agents and scheduled tasks paused then and carried on when it woke. To keep them going around the clock, run Vision on a computer that stays on."));
  const x = el("button", "icon-btn");
  x.append(icon("X", 14));
  x.title = "Dismiss";
  x.onclick = () => { localStorage.setItem("sleep-seen", String(last.to)); redraw(); };
  b.append(icon("Moon", 16), text, x);
  return b;
}

function threadCard(t: Json): HTMLElement {
  const k = key(t.project, t.id);
  const [label, tone] = stateOf(t);
  const card = el("button", `thread-card tone-${tone}` + (selected === k ? " selected" : ""));
  const top = el("div", "thread-top");
  top.append(el("span", `pill ${tone}`, label), el("span", "thread-when dim small", ago(t.updated ?? 0)));
  const project = host.projects().find((p) => p.id === t.project);
  const where = el("div", "thread-project dim small");
  where.append(icon("Layers", 12), document.createTextNode(project ? host.projectName(project) : t.project));
  card.append(top, el("div", "thread-title", t.title || t.objective || "Thread"), el("div", "thread-line", lineOf(t)), where);
  card.onclick = () => { selected = selected === k ? null : k; tab = "conversation"; redraw(); };
  return card;
}

function scheduledSection(): HTMLElement {
  const section = el("section", "lane");
  const head = el("div", "lane-head");
  head.append(el("h3", "", "Scheduled"), el("span", "lane-count", String(scheduled.length)));
  section.append(head);
  if (!scheduled.length) {
    section.append(el("p", "dim small lane-empty", "Nothing scheduled. Ask Vision to remind you of something, or to check on something every morning."));
    return section;
  }
  const list = el("div", "sched-list");
  for (const task of [...scheduled].sort((a, b) => (a.next_run ?? Infinity) - (b.next_run ?? Infinity))) {
    const row = el("div", "sched-row" + (task.enabled ? "" : " off"));
    const text = el("div", "sched-text");
    const title = el("div", "sched-title");
    title.append(el("span", "", task.title || task.prompt));
    if (task.kind === "reminder") title.append(el("span", "badge", "reminder"));
    text.append(title, el("div", "dim small", whenOf(task)));
    const run = el("button", "chip-btn bordered", "Run now");
    run.onclick = async () => {
      try {
        const r = await host.http("POST", `scheduled/${task.id}/run`);
        if (r.chat) host.openChat(r.chat);
      } catch (e) { host.toast(String(e)); }
    };
    const onoff = el("button", "toggle" + (task.enabled ? " on" : ""));
    onoff.title = task.enabled ? "On" : "Off";
    onoff.onclick = async () => {
      try { await host.http("POST", "scheduled", undefined, { ...task, enabled: !task.enabled }); await onScheduled(); } catch (e) { host.toast(String(e)); }
    };
    const del = el("button", "icon-btn");
    del.append(icon("Trash2", 14));
    del.title = "Delete";
    del.onclick = async () => {
      try { await host.http("DELETE", `scheduled/${task.id}`); await onScheduled(); } catch (e) { host.toast(String(e)); }
    };
    row.append(icon(task.kind === "reminder" ? "Bell" : "Clock", 15), text, ...(task.kind === "reminder" ? [] : [run]), onoff, del);
    list.append(row);
  }
  section.append(list);
  return section;
}

function whenOf(task: Json): string {
  const repeat = { once: "", daily: "every day", weekdays: "weekdays", weekly: "every week" }[task.repeat as string] ?? task.repeat ?? "";
  if (!task.enabled) return ["Off", repeat].filter(Boolean).join(" · ");
  if (!task.next_run) return repeat || "Done";
  const at = new Date(task.next_run * 1000);
  const today = new Date();
  const tomorrow = new Date(Date.now() + 86400000);
  const day = at.toDateString() === today.toDateString() ? "Today" : at.toDateString() === tomorrow.toDateString() ? "Tomorrow"
    : at.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  return [`${day} ${at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`, repeat].filter(Boolean).join(" · ");
}

// ---------------------------------------------------------------- one thread, beside the board

const conversations = new Map<string, Json[]>(); // thread → its history, once fetched
const diffs = new Map<string, Json>();           // thread → what landing it would bring in

function panel(t: Json): HTMLElement {
  const k = key(t.project, t.id);
  const base = `projects/${t.project}/threads/${t.id}`;
  const box = el("aside", "thread-panel");
  const head = el("div", "panel-head");
  const close = el("button", "icon-btn");
  close.append(icon("X", 16));
  close.onclick = () => { selected = null; redraw(); };
  const [label, tone] = stateOf(t);
  const titles = el("div", "panel-titles");
  titles.append(el("span", `pill ${tone}`, label), el("h2", "", t.title || "Thread"));
  head.append(titles, close);
  box.append(head);

  const meta = el("div", "panel-meta dim small");
  meta.append(...[t.folder, t.branch && `branch ${t.branch}`, t.model && [t.model, t.effort].filter(Boolean).join(" · "), t.tools ? `${t.tools} tools` : ""]
    .filter(Boolean).map((x) => el("span", "", x)));
  box.append(meta);

  const body = el("div", "panel-body");
  if (t.state === "waiting_on_you") {
    const q = el("div", "panel-question");
    q.append(el("div", "q-header", "It asks"), markdown(t.question || t.last_result?.question || "It's waiting for you."));
    body.append(q);
  }
  if (t.error) { const e = el("div", "reply-error"); e.append(icon("CircleAlert", 15), el("span", "", t.error)); body.append(e); }
  if (t.activity && t.state === "working") {
    const a = el("div", "panel-activity");
    a.append(el("span", "state running"), el("span", "", t.activity));
    body.append(a);
  }
  const r = t.last_result;
  if (r?.summary) {
    const res = el("div", "panel-section");
    res.append(el("h4", "", "Result"), markdown([r.summary, ...(["changes", "findings", "decisions"] as const)
      .filter((f) => r[f]?.length).map((f) => `**${f[0].toUpperCase() + f.slice(1)}**\n${r[f].map((x: string) => `- ${x}`).join("\n")}`)].join("\n\n")));
    body.append(res);
  }
  const goal = el("details", "panel-section") as HTMLDetailsElement;
  goal.append(el("summary", "", "Objective"), markdown(t.objective || ""));
  if (!r?.summary) goal.open = true;
  body.append(goal);

  const tabs = el("div", "segmented panel-tabs");
  for (const [id, name] of [["conversation", "Conversation"], ["changes", "Changes"]] as const) {
    const b = el("button", tab === id ? "on" : "", name);
    b.onclick = () => { tab = id; redraw(); };
    tabs.append(b);
  }
  body.append(tabs);
  const pane = el("div", "panel-pane");
  body.append(pane);
  if (tab === "conversation") drawConversation(pane, k, base);
  else drawChanges(pane, k, base);
  box.append(body);

  // Talk to it, and the buttons for what can be done with it now.
  const foot = el("div", "panel-foot");
  const msg = el("textarea", "plan-changes") as HTMLTextAreaElement;
  msg.rows = 1;
  msg.placeholder = t.state === "waiting_on_you" ? "Answer it…" : t.state === "working" ? "Steer it…" : "Tell it what to do next…";
  const sendIt = async () => {
    const text = msg.value.trim();
    if (!text) return;
    msg.value = "";
    try { onThread(t.project, await host.http("POST", `${base}/message`, undefined, { text })); conversations.delete(k); } catch (e) { host.toast(String(e)); }
  };
  msg.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendIt(); } });
  const buttons = el("div", "panel-buttons");
  const action = (label: string, path: string, cls = "chip-btn bordered") => {
    const b = el("button", cls, label);
    b.onclick = async () => {
      try { onThread(t.project, await host.http("POST", `${base}/${path}`)); } catch (e) { host.toast(String(e)); }
    };
    buttons.append(b);
  };
  if (t.state === "working") action("Stop", "stop");
  if (REVIEW.includes(t.state) && t.branch) action("Land", "land", "primary land");
  if (t.state !== "resolved") action("Close", "resolve");
  const send = el("button", "primary", "Send");
  send.onclick = sendIt;
  buttons.append(send);
  foot.append(msg, buttons);
  box.append(foot);
  return box;
}

function drawConversation(pane: HTMLElement, k: string, base: string) {
  const list = conversations.get(k);
  if (!list) {
    pane.append(el("div", "dim small pad", "Loading…"));
    host.http("GET", `${base}/history`).then((r) => { conversations.set(k, r.messages ?? []); redraw(); })
      .catch(() => { conversations.set(k, []); redraw(); });
    return;
  }
  if (!list.length) { pane.append(el("div", "dim small pad", "Its conversation can't be read; the result above says what it did.")); return; }
  for (const m of list) {
    const row = el("div", `convo ${m.role === "user" ? "from-vision" : "from-thread"}`);
    row.append(el("div", "convo-who", m.role === "user" ? "Vision asked" : "It answered"), markdown(m.text ?? ""));
    pane.append(row);
  }
}

function drawChanges(pane: HTMLElement, k: string, base: string) {
  const d = diffs.get(k);
  if (!d) {
    pane.append(el("div", "dim small pad", "Loading…"));
    host.http("GET", `${base}/diff`).then((r) => { diffs.set(k, r); redraw(); })
      .catch((e) => { diffs.set(k, { error: String(e) }); redraw(); });
    return;
  }
  if (d.error) { pane.append(el("div", "dim small pad", /409|branch/.test(d.error) ? "It works in the project's folder, so there's no branch to compare." : d.error)); return; }
  pane.append(el("div", "dim small pad", `${d.commits} commit${d.commits === 1 ? "" : "s"} on top of ${d.base || "the project's branch"}`));
  for (const f of d.files ?? []) {
    const file = el("details", "change-file") as HTMLDetailsElement;
    const sum = el("summary", "");
    const counts = el("span", "count");
    if (f.added > 0) counts.append(el("span", "add", `+${f.added}`));
    if (f.removed > 0) counts.append(el("span", "del", `−${f.removed}`));
    sum.append(icon("FileText", 13), el("span", "change-path", f.path), counts);
    file.append(sum);
    file.addEventListener("toggle", async () => {
      if (!file.open || file.querySelector(".patch")) return;
      try {
        const p = await host.http("GET", `${base}/diff`, { path: f.path });
        const pre = el("div", "patch diff");
        for (const line of String(p.patch ?? "").split("\n")) {
          if (/^(diff --git|index |--- |\+\+\+ )/.test(line)) continue;
          const sign = line[0];
          const row = el("div", "diff-row" + (sign === "+" ? " add" : sign === "-" ? " del" : line.startsWith("@@") ? " hunk" : ""));
          row.append(el("span", "diff-num", ""), el("span", "diff-sign", sign === "+" || sign === "-" ? sign : ""), el("span", "diff-text", line.startsWith("@@") ? line : line.slice(1)));
          pre.append(row);
        }
        file.append(pre);
      } catch (e) { host.toast(String(e)); }
    });
    pane.append(file);
  }
  if (d.more) pane.append(el("div", "dim small pad", `and ${d.more} more file${d.more === 1 ? "" : "s"}`));
}
