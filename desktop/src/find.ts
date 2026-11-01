// Find in the open chat (Ctrl+F, ⌘F): a bar at the chat's top right that marks every match in the drawn
// messages, says which one is current, and steps through them. The transcript is redrawn on every streamed
// frame and again after each reply's history refetch, so the marks are laid again after every draw (refreshFind).

import { ChevronUp, createElement } from "lucide";
import { icon } from "./icons";
import { el } from "./render";

export interface FindHost {
  chat(): string | null;       // the chat on screen, so a switch starts again from the first match in view
  scrolled(): void;            // a match was brought into view: the transcript stops following the bottom
}

// Not what was said: buttons, timers, line numbers and the live status line.
const SKIP = ".actions, .status, button:not(.steps-head):not(.card-head), .code-bar, .diff-num, .diff-sign, .find-bar";

let host: FindHost;
let bar: HTMLElement;
let input: HTMLInputElement;
let count: HTMLElement;
let query = "";
let shownFor: string | null = null;  // the chat the marks were laid in
let marks: HTMLElement[] = [];        // the marks on screen, in reading order
let current = -1;
const laid = new Set<HTMLElement>();  // every mark made, on screen or in a drawn message kept for later
let searched = new WeakMap<Element, string>(); // a drawn message → the words its marks are for

const transcript = () => document.getElementById("transcript")!;

export function initFind(h: FindHost) {
  host = h;
  bar = el("div", "find-bar");
  bar.hidden = true;
  input = el("input", "find-input") as HTMLInputElement;
  input.placeholder = "Find in chat";
  input.spellcheck = false;
  input.autocomplete = "off";
  count = el("span", "find-count");
  const button = (glyph: SVGElement, title: string, act: () => void) => {
    const b = el("button", "icon-btn");
    b.title = title;
    b.append(glyph);
    b.onclick = act;
    return b;
  };
  const up = createElement(ChevronUp) as unknown as SVGElement;
  up.setAttribute("width", "15"); up.setAttribute("height", "15"); up.classList.add("icon");
  bar.append(icon("Search", 14), input, count,
    button(up, "Previous (Shift+Return)", () => go(-1)),
    button(icon("ChevronDown", 15), "Next (Return)", () => go(1)),
    button(icon("X", 15), "Close (Esc)", closeFind));
  document.getElementById("main")!.append(bar);

  input.addEventListener("input", () => search());
  bar.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeFind(); }
    else if (e.target === input && (e.key === "Enter" || e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      go(e.key === "ArrowUp" || (e.key === "Enter" && e.shiftKey) ? -1 : 1);
    }
  });
  // The menu's accelerator takes Ctrl+F first where there is a menu bar; this is for when it doesn't (and ⌘G steps on).
  window.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    const key = e.key.toLowerCase();
    if (key === "f" && !e.shiftKey) { e.preventDefault(); openFind(); }
    else if (key === "g" && !bar.hidden) { e.preventDefault(); go(e.shiftKey ? -1 : 1); }
  }, true);
  // A group of steps opened or closed: what shows in it changed, so that message is looked through again.
  transcript().addEventListener("click", (e) => {
    if (bar.hidden || !query) return;
    const top = [...transcript().children].find((c) => c.contains(e.target as Node));
    if (!top) return;
    setTimeout(() => { searched.delete(top); refreshFind(); }, 0);
  });
}

/** Open the bar (or bring it back to the words typed), on a chat's page only. */
export function openFind() {
  if (!visible(transcript())) return;
  bar.hidden = false;
  input.focus();
  input.select();
  if (input.value.trim() && input.value.trim() !== query) search();
}

/** Close the bar and take every mark away, from messages kept for later too. */
export function closeFind() {
  if (bar.hidden) return;
  bar.hidden = true;
  clearMarks();
  query = "";
  marks = [];
  current = -1;
  document.getElementById("input")?.focus();
}

/** After the transcript is drawn: the messages it holds that weren't looked through for these words are, and
 * the current match stays the current one where it is still there. Nothing scrolls. */
export function refreshFind() {
  if (!bar || bar.hidden) return;
  if (!visible(transcript())) { closeFind(); return; }
  if (!query) return;
  const chat = host.chat();
  const moved = chat !== shownFor;
  shownFor = chat;
  const was = marks[current];
  // A streaming reply is drawn afresh every frame: the copies it left behind are gone for good.
  for (const m of laid) if (!m.isConnected && m.closest(".streaming")) laid.delete(m);
  for (const node of transcript().children) {
    if (searched.get(node) === query && !node.classList.contains("streaming")) continue;
    for (const m of node.querySelectorAll<HTMLElement>("mark.find-hit")) unmark(m);
    markIn(node);
    searched.set(node, query);
  }
  marks = [...transcript().querySelectorAll<HTMLElement>("mark.find-hit")];
  const kept = was ? marks.indexOf(was) : -1;
  current = moved ? firstInView() : kept >= 0 ? kept : Math.min(current, marks.length - 1);
  drawCurrent();
}

/** The words changed: every message is looked through again, and the first match from where the chat is
 * scrolled to comes into view. */
function search() {
  clearMarks();
  query = input.value.trim();
  marks = [];
  current = -1;
  shownFor = host.chat();
  if (query) {
    for (const node of transcript().children) { markIn(node); searched.set(node, query); }
    marks = [...transcript().querySelectorAll<HTMLElement>("mark.find-hit")];
    current = firstInView();
  }
  drawCurrent();
  bringIntoView();
}

function go(step: number) {
  if (!marks.length) return;
  current = current < 0 ? (step > 0 ? 0 : marks.length - 1) : (current + step + marks.length) % marks.length;
  drawCurrent();
  bringIntoView();
}

function drawCurrent() {
  marks.forEach((m, i) => m.classList.toggle("current", i === current));
  count.textContent = !query ? "" : marks.length ? `${current + 1} of ${marks.length}` : "No matches";
  bar.classList.toggle("none", !!query && !marks.length);
}

function bringIntoView() {
  const m = marks[current];
  if (!m) return;
  m.scrollIntoView({ block: "center" });
  host.scrolled();
}

/** The first match at or below the top of what the transcript shows, else the last one above it. */
function firstInView(): number {
  if (!marks.length) return -1;
  const top = transcript().getBoundingClientRect().top;
  const i = marks.findIndex((m) => m.getBoundingClientRect().top >= top);
  return i >= 0 ? i : marks.length - 1;
}

/** Wrap each match in `root`'s text in a mark: only text that shows, and one text node at a time. */
function markIn(root: Element) {
  const pattern = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  const shows = new Map<Element, boolean>();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const parent = n.parentElement;
      if (!parent || !n.nodeValue?.trim() || parent.closest(SKIP)) return NodeFilter.FILTER_REJECT;
      if (!shows.has(parent)) shows.set(parent, parent.getClientRects().length > 0);
      return shows.get(parent) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const texts: Text[] = [];
  while (walker.nextNode()) texts.push(walker.currentNode as Text);
  for (const text of texts) {
    const found = [...text.nodeValue!.matchAll(pattern)];
    // From the end, so the places still to wrap stay where they were found.
    for (const hit of found.reverse()) {
      const part = text.splitText(hit.index!);
      part.splitText(hit[0].length);
      const mark = el("mark", "find-hit");
      part.replaceWith(mark);
      mark.append(part);
      laid.add(mark);
    }
  }
}

function unmark(m: HTMLElement) {
  laid.delete(m);
  const parent = m.parentNode;
  if (!parent) return;
  parent.replaceChild(document.createTextNode(m.textContent ?? ""), m);
  parent.normalize();
}

function clearMarks() {
  for (const m of [...laid]) unmark(m);
  searched = new WeakMap();
}

const visible = (node: HTMLElement) => !node.hidden && node.getClientRects().length > 0;
