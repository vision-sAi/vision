// Drawing a conversation: messages, markdown with highlighted code, and the tool and subagent cards that
// sit in a reply where they ran (each frame's `at` is how far into the reply's text it started).

import { marked } from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/common";
import { icon, IconName } from "./icons";

export type Json = Record<string, any>;

export interface Hunk { old: number; new: number; lines: string[] }
export interface Tool {
  id: string; name: string; detail: string; done: boolean; isError: boolean;
  output: string; input: [string, string][]; diff: Hunk[]; at?: number;
}
export interface Agent {
  id: string; kind: string; label: string; done: boolean; failed: boolean; tools: number; model: string;
  prompt: string; summary: string; steps: { tool: string; detail: string }[]; at?: number;
}
export interface Msg {
  role: "user" | "assistant" | "note" | "shell";
  text: string;
  streaming?: boolean;
  status?: string;
  error?: string;
  durationMs?: number;
  tools: Tool[];
  agents: Agent[];
  images?: string[];
  shellRunning?: boolean;   // a `!` command: still running,
  shellOutput?: string;     // what it printed (the end of it),
  shellCode?: number | null; // and how it exited (null: stopped)
  checkin?: Json;           // a scout's check-in, as GET /history has it ({label, started, ended, needs_you, text})
}

/** What a reply's buttons do (main.ts owns the chat they act on). */
export interface MsgActions {
  retry?: (m: Msg) => void;
  readAloud?: (m: Msg) => void;
  stopShell?: () => void;
}

export const el = (tag: string, cls = "", text = "") => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text) node.textContent = text;
  return node;
};

export function toolOf(t: Json): Tool {
  return {
    id: t.id ?? "", name: t.name ?? "", detail: t.detail ?? "", done: t.done ?? true, isError: !!t.is_error,
    output: t.output ?? "", input: Array.isArray(t.input) ? t.input.filter((p: unknown) => Array.isArray(p) && p.length === 2) : [],
    diff: Array.isArray(t.diff) ? t.diff : [], at: typeof t.at === "number" ? t.at : undefined,
  };
}

/** A tool frame folded into the row it updates: fields it leaves out keep their value. */
export function updateTool(t: Tool, ev: Json) {
  const n = toolOf(ev);
  t.name = n.name || t.name;
  t.detail = n.detail || t.detail;
  t.done = ev.done ?? t.done;
  t.isError = ev.is_error ?? t.isError;
  if (ev.output !== undefined) t.output = n.output;
  if (ev.input !== undefined) t.input = n.input;
  if (ev.diff !== undefined) t.diff = n.diff;
  if (t.at === undefined) t.at = n.at;
}

export function agentOf(a: Json): Agent {
  return {
    id: a.id ?? "", kind: a.kind ?? "agent", label: a.label ?? "", done: !!a.done, failed: !!a.failed,
    tools: a.tools ?? 0, model: a.model ?? "", prompt: a.prompt ?? "", summary: a.summary ?? "",
    steps: Array.isArray(a.steps) ? a.steps : [], at: typeof a.at === "number" ? a.at : undefined,
  };
}

export function updateAgent(a: Agent, ev: Json) {
  const n = agentOf(ev);
  Object.assign(a, { ...n, at: a.at ?? n.at, label: n.label || a.label, prompt: n.prompt || a.prompt });
}

// ---------------------------------------------------------------- pictures

/** Draws a picture that lives on the Vision computer: main.ts fetches it (GET /image) into a blob URL. */
let loadPicture: (path: string) => Promise<string> = async () => "";
export function setPictureLoader(load: (path: string) => Promise<string>) {
  loadPicture = load;
}
/** Opens a picture full size (main.ts's viewer). */
let showPicture: (src: string, caption: string) => void = () => {};
export function setPictureViewer(show: (src: string, caption: string) => void) {
  showPicture = show;
}

function picture(path: string, caption = "", cls = "picture"): HTMLElement {
  const img = el("img", cls) as HTMLImageElement;
  img.alt = caption;
  img.title = caption || (path.split("/").pop() ?? "");
  loadPicture(path).then((src) => { if (src) img.src = src; else img.classList.add("missing"); });
  img.onclick = () => img.src && showPicture(img.src, caption);
  return img;
}

/** A local path rather than a web address: a picture on the Vision computer. */
const isLocal = (src: string) => /^(\/|~\/)/.test(src);

// ---------------------------------------------------------------- markdown

export function markdown(text: string): HTMLElement {
  const body = el("div", "md");
  body.innerHTML = DOMPurify.sanitize(marked.parse(text, { async: false, gfm: true, breaks: false }) as string);
  for (const pre of body.querySelectorAll("pre")) decorateCode(pre);
  for (const img of body.querySelectorAll("img")) {
    const src = img.getAttribute("src") ?? "";
    if (isLocal(decodeURI(src))) img.replaceWith(picture(decodeURI(src), img.alt));
    else img.onclick = () => showPicture(img.src, img.alt);
  }
  return body;
}

/** What the server appends to a sent message for its attachments (vision/server.py with_attachments). */
const ATTACHED = /^\[Attached (image|file|video): (.+)\]$/;
const ATTACH_NOISE = /^(\[(Video length|Frame |Video audio)[^\]]*\]|(Look at|Watch|Read) the attached .+\.)$/;

/** A sent message's own words, and the files it carried. */
export function splitAttachments(text: string): { text: string; images: string[] } {
  const images: string[] = [];
  const kept = text.split("\n").filter((line) => {
    const m = line.trim().match(ATTACHED);
    if (m) { images.push(m[2]); return false; }
    return !ATTACH_NOISE.test(line.trim());
  });
  return { text: kept.join("\n").trim(), images };
}

const PICTURE_TYPES = /\.(png|jpe?g|gif|webp|heic|bmp)$/i;

/** A code block: its language and a copy button above it, and its syntax coloured. */
function decorateCode(pre: HTMLElement) {
  const code = pre.querySelector("code");
  const lang = [...(code?.classList ?? [])].find((c) => c.startsWith("language-"))?.slice(9) ?? "";
  if (code && lang && hljs.getLanguage(lang)) {
    try { code.innerHTML = hljs.highlight(code.textContent ?? "", { language: lang, ignoreIllegals: true }).value; } catch { /* leave it plain */ }
  }
  const bar = el("div", "code-bar");
  bar.append(el("span", "", lang));
  bar.append(copyButton(() => code?.textContent ?? "", "Copy"));
  pre.prepend(bar);
}

export function copyButton(text: () => string, label = ""): HTMLElement {
  const b = el("button", label ? "copy" : "act");
  b.title = "Copy";
  const show = (done: boolean) => {
    b.replaceChildren(icon(done ? "Check" : "Copy", 14));
    if (label) b.append(document.createTextNode(done ? "Copied" : label));
  };
  show(false);
  b.onclick = () => {
    navigator.clipboard.writeText(text());
    show(true);
    setTimeout(() => show(false), 1200);
  };
  return b;
}

// ---------------------------------------------------------------- tool and agent cards

const expanded = new Set<string>(); // card ids opened by a click; kept across redraws of a streaming reply

const TOOL_LOOK: Record<string, [IconName, string]> = {
  Read: ["FileText", "Read"], Edit: ["FilePen", "Edit"], MultiEdit: ["FilePen", "Edit"], Write: ["FilePen", "Write"],
  NotebookEdit: ["FilePen", "Edit notebook"], Bash: ["Terminal", "Run"], BashOutput: ["Terminal", "Output"],
  Grep: ["Search", "Search"], Glob: ["FolderSearch", "Find files"], LS: ["Folder", "List"],
  WebFetch: ["Globe", "Fetch"], WebSearch: ["Globe", "Web search"], TodoWrite: ["ListTodo", "Update tasks"],
  Task: ["Bot", "Agent"], Agent: ["Bot", "Agent"], ExitPlanMode: ["Lightbulb", "Plan"],
};

function lookOf(name: string): [IconName, string] {
  return TOOL_LOOK[name] ?? (/shell|exec|command/i.test(name) ? ["Terminal", name] : ["Wrench", name.replace(/^mcp__\w+?__/, "")]);
}

function stateMark(done: boolean, failed: boolean): HTMLElement {
  const s = el("span", "state" + (failed ? " failed" : done ? " ok" : " running"));
  if (failed) s.append(icon("CircleAlert", 14));
  else if (done) s.append(icon("Check", 14));
  return s;
}

/** `+12 −3` for an edit. */
function diffCount(diff: Hunk[]): HTMLElement | null {
  let add = 0, del = 0;
  for (const h of diff) for (const l of h.lines ?? []) { if (l.startsWith("+")) add++; else if (l.startsWith("-")) del++; }
  if (!add && !del) return null;
  const c = el("span", "count");
  if (add) c.append(el("span", "add", `+${add}`));
  if (del) c.append(el("span", "del", `−${del}`));
  return c;
}

function card(id: string, head: Element[], body: () => HTMLElement[], cls = ""): HTMLElement {
  const box = el("div", "card " + cls + (expanded.has(id) ? " open" : ""));
  const row = el("button", "card-head");
  row.append(...head, icon("ChevronRight", 14));
  const inner = el("div", "card-body");
  const fill = () => { if (!inner.childElementCount) inner.append(...body()); };
  if (expanded.has(id)) fill();
  row.onclick = () => {
    if (expanded.delete(id)) box.classList.remove("open");
    else { expanded.add(id); fill(); box.classList.add("open"); }
  };
  box.append(row, inner);
  return box;
}

/** The plan a plan-mode reply proposed (its ExitPlanMode call's `plan`), or "". */
export function planOf(t: Tool): string {
  return t.name === "ExitPlanMode" ? t.input.find(([k]) => k === "plan")?.[1] ?? "" : "";
}

function toolCard(t: Tool): HTMLElement {
  const [glyph, verb] = lookOf(t.name);
  const plan = planOf(t);
  if (plan) {
    return card(t.id, [icon("Lightbulb", 15), el("span", "card-verb", "Plan"), el("span", "card-detail", firstLine(plan)), stateMark(t.done, t.isError)],
      () => [markdown(plan)], "plan");
  }
  const head: Element[] = [icon(glyph, 15), el("span", "card-verb", verb), el("span", "card-detail", t.detail)];
  const count = diffCount(t.diff);
  if (count) head.push(count);
  head.push(stateMark(t.done, t.isError));
  return card(t.id, head, () => {
    const parts: HTMLElement[] = [];
    const inputs = t.input.filter(([k]) => k !== "description" || t.name !== "Bash");
    if (t.name === "Bash" && t.input.length) {
      const cmd = t.input.find(([k]) => k === "command")?.[1] ?? t.detail;
      parts.push(el("pre", "card-cmd", `$ ${cmd}`));
    } else if (inputs.length && !t.diff.length) {
      const dl = el("dl", "card-input");
      for (const [k, v] of inputs) dl.append(el("dt", "", k), el("dd", "", v));
      parts.push(dl);
    }
    if (t.diff.length) parts.push(diffView(t.diff));
    if (t.output && !(t.diff.length && !t.isError)) parts.push(el("pre", "card-out" + (t.isError ? " failed" : ""), t.output));
    if (!parts.length) parts.push(el("div", "card-empty", t.done ? "No output." : "Running…"));
    return parts;
  }, "tool" + (t.isError ? " failed" : ""));
}

/** A plan's first real line, for its card's heading. */
export function firstLine(text: string): string {
  const line = text.split("\n").map((l) => l.replace(/^[#>*\-\s\d.)]+/, "").replace(/[*`]/g, "").trim()).find(Boolean) ?? "";
  return line.length > 90 ? line.slice(0, 89) + "…" : line;
}

function diffView(diff: Hunk[]): HTMLElement {
  const table = el("div", "diff");
  diff.forEach((h, i) => {
    if (i) table.append(el("div", "diff-gap", "⋯"));
    let oldN = h.old || 0, newN = h.new || 0;
    for (const line of h.lines ?? []) {
      const sign = line[0] ?? " ";
      const row = el("div", "diff-row" + (sign === "+" ? " add" : sign === "-" ? " del" : ""));
      const num = sign === "+" ? newN++ : sign === "-" ? oldN++ : (oldN++, newN++);
      row.append(el("span", "diff-num", String(num)), el("span", "diff-sign", sign === " " ? "" : sign), el("span", "diff-text", line.slice(1)));
      table.append(row);
    }
  });
  return table;
}

function agentCard(a: Agent): HTMLElement {
  const head: Element[] = [icon("Bot", 15), el("span", "card-verb", a.label || "Agent"),
    el("span", "card-detail", [a.kind !== "agent" ? a.kind : "", a.model, a.tools ? `${a.tools} tool${a.tools === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ")),
    stateMark(a.done, a.failed)];
  return card(a.id, head, () => {
    const parts: HTMLElement[] = [];
    if (a.prompt) parts.push(el("div", "card-prompt", a.prompt));
    if (a.steps.length) {
      const list = el("div", "card-steps");
      for (const s of a.steps.slice(-30)) {
        const [glyph, verb] = lookOf(s.tool);
        const row = el("div", "step");
        row.append(icon(glyph, 13), el("span", "card-verb", verb), el("span", "card-detail", s.detail));
        list.append(row);
      }
      parts.push(list);
    }
    if (a.summary) parts.push(markdown(a.summary));
    if (!parts.length) parts.push(el("div", "card-empty", "Starting…"));
    return parts;
  }, "agent" + (a.failed ? " failed" : ""));
}

/** A `!` command run on the computer: what was typed, what it printed, how it ended. */
function shellCard(m: Msg, actions: MsgActions): HTMLElement {
  const card = el("div", "shell-card" + (m.shellRunning ? " running" : ""));
  const head = el("div", "shell-head");
  head.append(icon("Terminal", 14), el("span", "shell-cmd", m.text));
  if (m.shellRunning) {
    head.append(el("span", "state running"));
    if (actions.stopShell) {
      const stop = el("button", "shell-stop", "Stop");
      stop.onclick = actions.stopShell;
      head.append(stop);
    }
  } else if (m.shellCode === 0) head.append(stateMark(true, false));
  else head.append(el("span", "shell-code", m.shellCode == null ? "stopped" : `exit ${m.shellCode}`));
  card.append(head);
  if (m.shellOutput) card.append(el("pre", "shell-out", m.shellOutput));
  else if (!m.shellRunning) card.append(el("div", "shell-none", "No output."));
  return card;
}

// ---------------------------------------------------------------- messages

function seconds(ms?: number) {
  if (!ms) return "";
  return ms < 60000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

/** The reply's text cut at each card's `at`, with the cards between the pieces. */
function replyBody(m: Msg): HTMLElement[] {
  const chars = Array.from(m.text);
  const cards = [
    ...m.tools.map((t) => ({ at: t.at ?? chars.length, node: () => toolCard(t) })),
    ...m.agents.map((a) => ({ at: a.at ?? chars.length, node: () => agentCard(a) })),
  ].sort((x, y) => x.at - y.at);
  const out: HTMLElement[] = [];
  let from = 0;
  let group: HTMLElement | null = null;
  for (const c of cards) {
    const at = Math.min(Math.max(c.at, from), chars.length);
    const piece = chars.slice(from, at).join("");
    if (piece.trim()) { out.push(markdown(piece)); group = null; }
    from = at;
    if (!group) { group = el("div", "cards"); out.push(group); }
    group.append(c.node());
  }
  const rest = chars.slice(from).join("");
  if (rest.trim()) out.push(markdown(rest));
  return out;
}

export function drawMsg(m: Msg, actions: MsgActions = {}): HTMLElement {
  const box = el("div", `msg ${m.role}` + (m.streaming ? " streaming" : ""));
  if (m.role === "user") {
    if (m.images?.length) {
      const files = el("div", "sent-files");
      for (const p of m.images) {
        if (PICTURE_TYPES.test(p)) files.append(picture(p, "", "sent-picture"));
        else {
          const chip = el("span", "file-chip");
          chip.append(icon("Paperclip", 13), document.createTextNode(p.split("/").pop()?.replace(/^\d{8}-\d{6}-[0-9a-f]{6}-/, "") ?? p));
          files.append(chip);
        }
      }
      box.append(files);
    }
    if (m.text) box.append(el("div", "bubble", m.text));
    return box;
  }
  if (m.role === "shell") {
    box.append(shellCard(m, actions));
    return box;
  }
  if (m.role === "note") {
    box.append(el("div", "note-text", m.text));
    return box;
  }
  box.append(...replyBody(m));
  if (m.streaming) {
    const s = el("div", "status");
    s.append(el("span", "spark"), document.createTextNode(m.status || "Thinking…"));
    box.append(s);
  }
  if (m.error) {
    const e = el("div", "reply-error");
    e.append(icon("CircleAlert", 15), el("span", "", m.error));
    box.append(e);
  }
  if (!m.streaming && (m.text || m.error)) {
    const bar = el("div", "actions");
    bar.append(copyButton(() => m.text));
    if (actions.readAloud && m.text) {
      const b = el("button", "act");
      b.title = "Read aloud";
      b.append(icon("Volume2", 14));
      b.onclick = () => actions.readAloud!(m);
      bar.append(b);
    }
    if (actions.retry) {
      const b = el("button", "act");
      b.title = "Send the message again";
      b.append(icon("RotateCcw", 14));
      b.onclick = () => actions.retry!(m);
      bar.append(b);
    }
    if (m.durationMs) bar.append(el("span", "took", seconds(m.durationMs)));
    box.append(bar);
  }
  return box;
}
