// Memory: what Vision keeps between conversations (memory.py on the computer), like the phone's Memory
// screen. The profile (MEMORY.md) is part of every conversation; each area is a topic file read when a
// conversation touches it. Every fact can be reworded in place or forgotten, and the search box looks
// through all of them at once.

import { ago } from "../agents";
import { icon } from "../icons";
import { Json, el } from "../render";
import type { Page, SettingsHost } from "./index";

let host: SettingsHost;
let content: HTMLElement;
let topics: Json[] = [];         // the area files, from the last full listing
let opened: string | null = null; // the area being read, null for the overview
let query = "";
let drawn = 0;                    // the newest draw; an older one that answers late is dropped

export const page: Page = {
  id: "memory",
  label: "Memory",
  icon: "Brain",
  build(body, h) {
    host = h;
    const search = el("label", "search mem-search");
    const input = el("input") as HTMLInputElement;
    input.placeholder = "Search memory";
    input.value = query;
    search.append(icon("Search", 14), input);
    let wait = 0;
    input.oninput = () => {
      clearTimeout(wait);
      wait = window.setTimeout(() => { query = input.value.trim(); draw(); }, 200);
    };
    input.onkeydown = (e) => { if (e.key === "Escape" && input.value) { e.stopPropagation(); input.value = ""; query = ""; draw(); } };
    content = el("div", "mem");
    body.append(search, content);
    return draw();
  },
};

/** Draw what is asked for now: search hits, one area, or the overview. */
async function draw() {
  const n = ++drawn;
  try {
    if (query) {
      const r = await host.http("GET", "memory", { q: query });
      if (n === drawn) drawHits(r.facts ?? []);
    } else if (opened !== null) {
      const r = await host.http("GET", "memory", { topic: opened });
      if (n === drawn) drawTopic(opened, r.facts ?? []);
    } else {
      const r = await host.http("GET", "memory");
      topics = r.topics ?? [];
      if (n === drawn) drawOverview(r);
    }
  } catch (err) {
    if (n !== drawn) return;
    const again = el("button", "chip-btn bordered", "Try again");
    again.onclick = () => draw();
    const box = el("div", "mem-empty");
    box.append(el("b", "", "Couldn't load memory"), el("span", "", String(err)), again);
    content.replaceChildren(box);
  }
}

function drawOverview(r: Json) {
  const you = section("You", "Part of every conversation.");
  you.append(factList(r.facts ?? [], "", "Nothing here yet. Vision adds what it learns about you, or add a fact yourself."), adder(""));
  const parts: HTMLElement[] = [you];
  if (topics.length) {
    const areas = section("Areas", "Read when a conversation touches them.");
    for (const t of topics) {
      const row = el("button", "mem-topic");
      row.dataset.topic = t.name;
      const text = el("div", "mem-topic-text");
      text.append(el("div", "mem-topic-title", title(t.name, t.about)));
      if (t.about) text.append(el("div", "dim small", blurb(t.name, t.about)));
      const meta = el("span", "dim small", `${t.count} ${t.count === 1 ? "fact" : "facts"}`);
      if (t.updated) meta.title = `Updated ${ago(t.updated)}`;
      row.append(text, meta, icon("ChevronRight", 14));
      row.onclick = () => { opened = t.name; draw(); };
      areas.append(row);
    }
    parts.push(areas);
  }
  content.replaceChildren(...parts);
}

function drawTopic(name: string, facts: Json[]) {
  const about = topics.find((t) => t.name === name)?.about ?? "";
  const back = el("button", "chip-btn mem-back");
  back.append(icon("ChevronRight", 14), el("span", "", "Memory"));
  back.onclick = () => { opened = null; draw(); };
  const head = el("div", "mem-head");
  head.append(el("h3", "", title(name, about)));
  if (about) head.append(el("div", "dim small", blurb(name, about)));
  const box = el("section", "mem-section");
  box.append(factList(facts, name, "Nothing here yet."), adder(name));
  content.replaceChildren(back, head, box);
}

function drawHits(hits: Json[]) {
  if (!hits.length) {
    const box = el("div", "mem-empty");
    box.append(el("span", "", `Nothing in memory mentions “${query}”.`));
    content.replaceChildren(box);
    return;
  }
  const box = section(`${hits.length} ${hits.length === 1 ? "fact" : "facts"} found`, "");
  box.append(factList(hits, null, ""));
  content.replaceChildren(box);
}

function section(heading: string, hint: string): HTMLElement {
  const box = el("section", "mem-section");
  box.append(el("h3", "", heading));
  if (hint) box.append(el("div", "dim small mem-hint", hint));
  return box;
}

/** Facts as bullets: click one to reword it, the bin forgets it. `topic` null: search hits, each saying
 *  which file it is in. */
function factList(facts: Json[], topic: string | null, empty: string): HTMLElement {
  const list = el("div", "mem-facts");
  for (const f of facts) list.append(factRow(f, topic ?? f.topic ?? "", topic === null));
  if (!facts.length && empty) list.append(el("p", "dim small mem-none", empty));
  return list;
}

function factRow(f: Json, topic: string, where: boolean): HTMLElement {
  const row = el("div", "mem-fact");
  row.dataset.line = f.line;
  const text = el("div", "mem-text", f.text);
  text.title = "Click to edit";
  const side = el("div", "mem-side");
  if (where) {
    const file = el("button", "mem-file", topic ? title(topic, topics.find((t) => t.name === topic)?.about ?? "") : "Profile");
    file.title = "Open this file";
    file.onclick = () => { opened = topic || null; clearSearch(); draw(); };
    side.append(file);
  }
  if (f.date) side.append(el("span", "dim small mem-date", day(f.date)));
  const edit = el("button", "icon-btn mem-act");
  edit.title = "Edit";
  edit.append(icon("Pencil", 13));
  const forget = el("button", "icon-btn mem-act mem-forget") as HTMLButtonElement;
  forget.title = "Forget";
  forget.append(icon("Trash2", 13));
  side.append(edit, forget);
  row.append(el("span", "mem-dot", "•"), text, side);

  const start = () => editor(text, f.text, async (words) => {
    if (words === f.text) return true;
    return ok(host.http("POST", "memory/edit", undefined, { line: f.line, text: words, topic }));
  });
  text.onclick = start;
  edit.onclick = start;
  forget.onclick = async () => {
    forget.disabled = true;
    if (await ok(host.http("POST", "memory/forget", undefined, { line: f.line, topic }))) draw();
    else forget.disabled = false;
  };
  return row;
}

/** "Add a fact", which opens a box at the foot of the list; Return saves and leaves it open for the next. */
function adder(topic: string): HTMLElement {
  const b = el("button", "chip-btn mem-add");
  b.append(icon("Plus", 14), el("span", "", "Add a fact"));
  b.onclick = () => {
    const slot = el("div", "mem-text");
    const row = el("div", "mem-fact adding");
    row.append(el("span", "mem-dot", "•"), slot);
    b.replaceWith(row);
    editor(slot, "", async (words, more) => {
      if (!(await ok(host.http("POST", "memory", undefined, { fact: words, topic })))) return false;
      await draw();
      if (more) (content.querySelector(".mem-add") as HTMLElement | null)?.click();
      return true;
    }, () => draw());
  };
  return b;
}

/** Turn `slot` into a text box holding `start`. Return or clicking away saves (`save` says whether it took;
 *  `more` is true after Return), Escape or an empty box puts things back. */
function editor(slot: HTMLElement, start: string, save: (words: string, more: boolean) => Promise<boolean>, cancel?: () => void) {
  if (slot.querySelector("textarea")) return;
  const box = el("textarea", "mem-edit") as HTMLTextAreaElement;
  box.rows = 1;
  box.value = start;
  box.placeholder = "Something Vision should remember";
  const fit = () => { box.style.height = "auto"; box.style.height = `${box.scrollHeight}px`; };
  const old = slot.textContent ?? "";
  slot.replaceChildren(box);
  slot.classList.add("editing");
  let busy = false;
  const putBack = () => { if (cancel) cancel(); else { slot.classList.remove("editing"); slot.textContent = old; } };
  const commit = async (more: boolean) => {
    const words = box.value.replace(/\s+/g, " ").trim();
    if (busy) return;
    if (!words) return putBack();
    busy = true;
    if (await save(words, more)) { if (words === start) putBack(); else if (!cancel) draw(); }
    else { busy = false; box.focus(); }
  };
  box.oninput = fit;
  box.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); commit(true); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); busy = true; putBack(); }
  };
  box.onblur = () => { if (!busy) { if (cancel && !box.value.trim()) putBack(); else commit(false); } };
  requestAnimationFrame(() => { fit(); box.focus(); box.setSelectionRange(box.value.length, box.value.length); });
}

async function ok(call: Promise<unknown>): Promise<boolean> {
  try {
    await call;
    return true;
  } catch (err) {
    host.toast(String(err));
    return false;
  }
}

function clearSearch() {
  query = "";
  const input = document.querySelector(".mem-search input") as HTMLInputElement | null;
  if (input) input.value = "";
}

/** A file's name as the phone shows it: its heading's name when it has one ("Acme: the user's…" → "Acme"),
 *  else the file name ("iphone-app" → "iPhone app"). */
function title(name: string, about: string): string {
  if (!name) return "Profile";
  const head = about.includes(":") ? about.split(":")[0].trim() : "";
  if (head && head.length <= 30) return head;
  const words = name.replace(/-/g, " ");
  return (words[0].toUpperCase() + words.slice(1)).replace(/iphone/i, "iPhone").replace(/^ios\b/i, "iOS");
}

/** What a file covers, without the name its title already shows ("Acme: the user's…" → "The user's…"). */
function blurb(name: string, about: string): string {
  const head = title(name, about);
  if (!about.startsWith(head + ":")) return about;
  const rest = about.slice(head.length + 1).trim();
  return rest ? rest[0].toUpperCase() + rest.slice(1) : "";
}

/** "27 Sep", with the year once it is not this one. */
function day(date: string): string {
  const d = new Date(`${date}T12:00:00`);
  if (isNaN(d.getTime())) return date;
  const year = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, year ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" });
}
