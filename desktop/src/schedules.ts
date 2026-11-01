// The Coming up page: reminders and scheduled prompts set from chats (GET /scheduled), each with when it
// next runs and how it repeats, Run now, a switch to pause it and Cancel; and a short form for a new one.
// A scout's check-ins are on the scout's page, so they're left out here. main.ts hands in `scheduled` frames.

import { icon } from "./icons";
import { Json, el } from "./render";

export interface SchedulesHost {
  http: (method: string, path: string, query?: Json, body?: Json) => Promise<any>;
  toast: (text: string) => void;
  /** Chats a prompt can be sent into, newest first. */
  chats: () => Json[];
  /** The chat that was on screen last, the form's first pick. */
  active: () => string | null;
  openChat: (chat: string) => void;
  /** The list changed (the header's count). */
  changed: () => void;
}

let host: SchedulesHost;
let tasks: Json[] | null = null;  // null: not loaded yet
let error = "";
let root: HTMLElement | null = null;
let refreshForm = () => {};  // the form's chat list again, as the chats stand now

export function setHost(h: SchedulesHost) {
  host = h;
}

const shown = (): Json[] => (tasks ?? []).filter((t) => t.kind !== "checkin");

/** "2 reminders · 1 scheduled prompt", for the page's header. */
export function summary(): string {
  const list = shown();
  if (!tasks) return "";
  const r = list.filter((t) => t.kind === "reminder").length, p = list.length - r;
  if (!list.length) return "Nothing coming up";
  return [r && `${r} reminder${r === 1 ? "" : "s"}`, p && `${p} scheduled prompt${p === 1 ? "" : "s"}`].filter(Boolean).join(" · ");
}

export async function load() {
  try {
    tasks = await host.http("GET", "scheduled");
    error = "";
  } catch (e) {
    error = String(e);
  }
  host.changed();
  redraw();
}

/** A `scheduled` frame: something was added, changed, ran or went. */
export function onScheduled() {
  if (root?.isConnected && !root.hidden) void load();
  else tasks = null;  // read again when the page next opens
}

function redraw() {
  if (root?.isConnected && !root.hidden) draw(root);
}

export function draw(box: HTMLElement) {
  root = box;
  let page = box.querySelector<HTMLElement>(".sched-page");
  if (!page) {
    // The form is made once and kept, so a frame arriving doesn't clear what's being typed; the lists redraw.
    page = el("div", "sched-page");
    page.append(newForm(), el("div", "sched-groups"));
    box.replaceChildren(page);
  }
  refreshForm();
  const groups = page.querySelector<HTMLElement>(".sched-groups")!;
  groups.replaceChildren();
  if (!tasks) {
    groups.append(el("p", "dim small sched-loading", error ? `Couldn't load what's coming up: ${error}` : "Loading…"));
    return;
  }
  const list = shown();
  if (!list.length) {
    const empty = el("div", "board-empty");
    empty.append(icon("Clock", 28), el("h3", "", "Nothing coming up"),
      el("p", "dim", "Ask in a chat (“remind me at six”, “in an hour, check the deploy”) or add one above, and it shows here. For a job that runs every day or week, send a scout."));
    groups.append(empty);
    return;
  }
  const order = (a: Json, b: Json) => Number(!a.enabled) - Number(!b.enabled) || (a.next_run ?? Infinity) - (b.next_run ?? Infinity);
  const group = (title: string, items: Json[]) => {
    if (!items.length) return;
    const section = el("section", "sched-group");
    const head = el("div", "lane-head");
    head.append(el("h3", "", title), el("span", "lane-count", String(items.length)));
    const rows = el("div", "sched-list");
    for (const t of [...items].sort(order)) rows.append(row(t));
    section.append(head, rows);
    groups.append(section);
  };
  group("Reminders", list.filter((t) => t.kind === "reminder"));
  group("Scheduled prompts", list.filter((t) => t.kind !== "reminder"));
  groups.append(el("p", "dim small sched-foot", "These run while Vision is on, by the clock of the computer it runs on. A scout's check-ins are on its own page."));
}

function row(t: Json): HTMLElement {
  const reminder = t.kind === "reminder";
  const r = el("div", "sched-row sched-item" + (t.enabled ? "" : " off"));
  r.dataset.id = t.id;
  const mark = el("span", "sched-mark");
  mark.append(icon(reminder ? "Bell" : "Clock", 15));
  const text = el("div", "sched-text");
  const chat = t.chat ? host.chats().find((c) => c.chat === t.chat) : undefined;
  const how = [reminder ? "Reminder" : "", repeatText(t), !reminder && chat ? `in “${chat.title || "a chat"}”` : ""].filter(Boolean).join(" · ");
  text.append(el("div", "sched-title", t.title || t.prompt), el("div", "dim small", how));
  if (t.enabled && t.next_run) text.append(el("div", "small sched-next", `Next ${nextText(t.next_run)}`));
  else text.append(el("div", "small sched-next dim", t.enabled || (t.repeat === "once" && t.last_run) ? "Done" : "Paused"));
  const run = el("button", "chip-btn bordered sched-run", "Run now");
  run.title = reminder ? "Send the reminder now" : "Run it now, whatever its schedule";
  run.onclick = async () => {
    try {
      const res = await host.http("POST", `scheduled/${t.id}/run`);
      if (res?.chat && !reminder) host.openChat(res.chat);
      await load();
    } catch (e) { host.toast(String(e)); }
  };
  const onoff = el("button", "toggle" + (t.enabled ? " on" : ""));
  onoff.setAttribute("role", "switch");
  onoff.title = t.enabled ? "On: switch off to pause it" : "Paused: switch on to resume it";
  onoff.onclick = async () => {
    try { await host.http("POST", "scheduled", undefined, { ...t, enabled: !t.enabled }); await load(); } catch (e) { host.toast(String(e)); }
  };
  const cancel = el("button", "chip-btn bordered danger sched-cancel", "Cancel");
  cancel.title = t.repeat === "once" ? "Cancel it" : "Cancel it, and every time after";
  cancel.onclick = async () => {
    try { await host.http("DELETE", `scheduled/${t.id}`); tasks = (tasks ?? []).filter((x) => x.id !== t.id); redraw(); host.changed(); await load(); }
    catch (e) { host.toast(String(e)); }
  };
  r.append(mark, text, run, onoff, cancel);
  return r;
}

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]; // the server's weekday: Monday = 0

/** "Every day at 8:30 AM", "Weekdays at 7:00 AM", "Sundays at 6:00 PM", "Once, Oct 26 at 9:00 AM", "Every 2 hours". */
function repeatText(t: Json): string {
  const at = clockText(t.time ?? "");
  switch (t.repeat) {
    case "daily": return `Every day at ${at}`;
    case "weekdays": return `Weekdays at ${at}`;
    case "weekly": return `${DAYS[(t.weekday ?? 0) % 7]}s at ${at}`;
    case "every": {
      const m = t.every_minutes ?? 60;
      return m % 60 ? `Every ${m} minutes` : m === 60 ? "Every hour" : `Every ${m / 60} hours`;
    }
    default: {
      const d = t.date ? new Date(`${t.date}T00:00`) : null;
      return d && !isNaN(+d) ? `Once, ${d.toLocaleDateString([], { month: "short", day: "numeric" })} at ${at}` : `Once at ${at}`;
    }
  }
}

/** "today at 6:00 PM, in 2 hours", "tomorrow at 7:00 AM", "Mon, Nov 3 at 8:30 AM". */
function nextText(ts: number): string {
  const at = new Date(ts * 1000);
  const now = new Date();
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  const clock = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const mins = Math.round((ts - now.getTime() / 1000) / 60), hours = Math.round(mins / 60);
  const soon = mins < 1 ? "now" : mins < 60 ? `in ${mins} min` : mins < 6 * 60 ? `in ${hours} hour${hours === 1 ? "" : "s"}` : "";
  if (at.toDateString() === now.toDateString()) return `today at ${clock}${soon ? `, ${soon}` : ""}`;
  if (at.toDateString() === tomorrow.toDateString()) return `tomorrow at ${clock}${soon ? `, ${soon}` : ""}`;
  return `${at.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} at ${clock}`;
}

function clockText(hhmm: string): string {
  const [h, m] = hhmm.split(":").map(Number);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return hhmm;
  return new Date(2000, 0, 1, h, m).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/** The short form at the top: the words, a reminder or a prompt for a chat, and when. */
function newForm(): HTMLElement {
  const box = el("form", "sched-new");
  let kind: "reminder" | "task" = "reminder";
  const words = el("input", "sched-words") as HTMLInputElement;
  words.autocomplete = "off";
  const kinds = el("div", "segmented sched-kinds");
  const reminderBtn = el("button", "on", "Reminder") as HTMLButtonElement;
  const promptBtn = el("button", "", "Prompt") as HTMLButtonElement;
  reminderBtn.type = promptBtn.type = "button";
  reminderBtn.title = "The words come back to you as they are, as a notification";
  promptBtn.title = "Vision gets the words as a message in a chat, and answers";
  kinds.append(reminderBtn, promptBtn);

  const repeat = el("select", "sched-repeat") as HTMLSelectElement;
  for (const [v, label] of [["once", "Once"], ["daily", "Every day"], ["weekdays", "Weekdays"], ["weekly", "Every week"]]) {
    const o = el("option", "", label) as HTMLOptionElement;
    o.value = v;
    repeat.append(o);
  }
  // Today, tomorrow and the five days after for a one-off; the day of the week for a weekly one.
  const day = el("select", "sched-day") as HTMLSelectElement;
  const time = el("input", "sched-time") as HTMLInputElement;
  time.type = "time";
  const soon = new Date(Date.now() + 3600 * 1000);
  time.value = `${String(soon.getHours()).padStart(2, "0")}:00`;
  const chat = el("select", "sched-chat") as HTMLSelectElement;
  const add = el("button", "primary sched-add", "Add") as HTMLButtonElement;
  add.type = "submit";
  const hint = el("p", "dim small sched-hint");

  const fillDays = () => {
    const was = day.value;
    day.replaceChildren();
    if (repeat.value === "weekly") {
      DAYS.forEach((d, i) => { const o = el("option", "", d) as HTMLOptionElement; o.value = String(i); day.append(o); });
      day.value = was && !was.includes("-") ? was : String((new Date().getDay() + 6) % 7);
    } else {
      for (let i = 0; i < 7; i++) {
        const d = new Date();
        d.setDate(d.getDate() + i);
        const o = el("option", "", i === 0 ? "Today" : i === 1 ? "Tomorrow" : d.toLocaleDateString([], { weekday: "long" })) as HTMLOptionElement;
        o.value = iso(d);
        day.append(o);
      }
      if (was.includes("-") && [...day.options].some((o) => o.value === was)) day.value = was;
    }
  };
  const fillChats = () => {
    const was = chat.value || host.active() || "";
    chat.replaceChildren();
    for (const c of host.chats()) {
      const o = el("option", "", `in ${c.title || "New chat"}`) as HTMLOptionElement;
      o.value = c.chat;
      chat.append(o);
    }
    if ([...chat.options].some((o) => o.value === was)) chat.value = was;
  };
  const sync = () => {
    reminderBtn.classList.toggle("on", kind === "reminder");
    promptBtn.classList.toggle("on", kind === "task");
    words.placeholder = kind === "reminder" ? "Remind me to…" : "What should Vision do?";
    day.hidden = !["once", "weekly"].includes(repeat.value);
    if (kind === "task") fillChats();
    chat.hidden = kind !== "task";
    hint.textContent = kind === "reminder"
      ? "Comes back to you as it is: a notification here and on your phone."
      : chat.options.length ? "Sent to Vision as a message in that chat, which answers it." : "Open a chat first: a prompt is sent into one. For a job of its own, send a scout.";
    add.disabled = !words.value.trim() || (kind === "task" && !chat.options.length);
  };
  reminderBtn.onclick = () => { kind = "reminder"; sync(); };
  promptBtn.onclick = () => { kind = "task"; sync(); };
  repeat.onchange = () => { fillDays(); sync(); };
  words.addEventListener("input", sync);
  fillDays();
  day.value = iso(soon);  // tomorrow, when the next hour is past midnight
  sync();
  refreshForm = sync;

  box.onsubmit = async (e) => {
    e.preventDefault();
    const text = words.value.trim();
    if (!text || (kind === "task" && !chat.value)) return;
    if (repeat.value === "once" && day.value === iso(new Date()) && time.value && time.value <= new Date().toTimeString().slice(0, 5)) {
      host.toast("That time has already gone today.");
      return;
    }
    const body: Json = { prompt: text, kind, repeat: repeat.value, time: time.value || "09:00", enabled: true };
    if (repeat.value === "once") body.date = day.value;
    if (repeat.value === "weekly") body.weekday = Number(day.value);
    if (kind === "task") body.chat = chat.value;
    add.disabled = true;
    try {
      await host.http("POST", "scheduled", undefined, body);
      words.value = "";
      await load();
    } catch (err) {
      host.toast(String(err));
    }
    sync();
  };

  const line = el("div", "sched-when");
  line.append(kinds, repeat, day, el("span", "dim small", "at"), time, chat, add);
  box.append(words, line, hint);
  return box;
}
