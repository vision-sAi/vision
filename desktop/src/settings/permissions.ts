// Permissions: what a brain may do without asking, on the computer this window talks to. Three lists of rules
// in Vision's config (`GET/POST /permissions`, vision/permissions.py): asks first, always allowed (exceptions
// to what asks) and never allowed; every scout's defaults per action; and, read only, Claude Code's own rules.

import { icon } from "../icons";
import { Json, el } from "../render";
import type { Page, SettingsHost } from "./index";
import { section, segmented, setRow } from "./ui";

type Kind = "allow" | "ask" | "deny";
const KINDS: [Kind, string][] = [["allow", "Allow"], ["ask", "Ask"], ["deny", "Never"]];
const SAID: Record<Kind, string> = { allow: "always allowed", ask: "asks first", deny: "never allowed" };

export const page: Page = {
  id: "permissions",
  label: "Permissions",
  icon: "ShieldCheck",
  async build(body, host) {
    let data: Json;
    try {
      data = await host.http("GET", "permissions");
    } catch {
      body.append(el("p", "dim small", "This computer's Vision is too old to change permissions from here. Update it to see them."));
      return;
    }
    draw(body, host, data);
  },
};

function draw(body: HTMLElement, host: SettingsHost, data: Json) {
  const save = async (change: Json) => {
    try {
      draw(body, host, await host.http("POST", "permissions", undefined, change));
    } catch (err) {
      host.toast(String(err));
      draw(body, host, data); // back to what the computer has
    }
  };
  // A rule moves to another list, or goes: every list it touches is sent whole.
  const put = (rule: string, to: Kind | null) => {
    const change: Json = {};
    for (const [k] of KINDS) {
      const had = (data[k] ?? []) as string[];
      const next = had.filter((r) => r !== rule);
      if (k === to) next.push(rule);
      if (next.length !== had.length || k === to) change[k] = next;
    }
    save(change);
  };

  const keep = body.scrollTop;
  body.replaceChildren();
  body.append(el("p", "dim small perm-intro",
    `Rules for what Vision's brains may do on ${host.machine() || "this computer"} without asking you. In Auto mode anything ` +
    "no rule names just runs; Plan mode never changes anything until you approve the plan."));

  const lists: [Kind, string, string, string][] = [
    ["ask", "Asks first", "Waits for your Yes, here or on your phone, before it runs.", "Nothing asks first."],
    ["allow", "Always allowed", "Runs without asking even when an ask-first rule covers it. Never allowed still wins.",
      "No exceptions."],
    ["deny", "Never allowed", "Refused every time, in every mode.", "Nothing is ruled out."],
  ];
  for (const [kind, title, hint, empty] of lists) {
    const box = section(title);
    box.dataset.kind = kind;
    box.append(el("div", "dim small perm-hint", hint));
    const rules = (data[kind] ?? []) as string[];
    for (const rule of rules) box.append(ruleRow(rule, kind, data.builtin?.includes(rule), put));
    if (!rules.length) box.append(el("div", "dim small perm-empty", empty));
    body.append(box);
  }
  body.append(addRow(data, save));
  body.append(el("p", "dim small perm-note",
    "Open chats take a change from their next step. A chat running in a terminal takes it when it starts again."));

  body.append(scoutsSection(data, save));
  body.append(whoSection());
  if (data.claude) body.append(claudeSection(data.claude));
  body.scrollTop = keep;
}

/** One rule: what it means in words, the rule itself, the Allow / Ask / Never picker and a remove button. */
function ruleRow(rule: string, kind: Kind, builtin: boolean, put: (rule: string, to: Kind | null) => void): HTMLElement {
  const row = el("div", "set-row perm-row");
  row.dataset.rule = rule;
  const text = el("div", "set-text");
  const label = el("div", "perm-label");
  label.append(...describe(rule));
  if (builtin) {
    const tag = el("span", "perm-tag", "Vision's default");
    tag.title = /^Bash\((claude|codex|grok):\*\)$/.test(rule)
      ? "Keeps one brain from running another's command line, so it can't spend that subscription"
      : "On from the start, to keep something hard to undo from happening by accident";
    label.append(tag);
  }
  text.append(label, el("code", "perm-rule", rule));
  const controls = el("div", "set-buttons");
  controls.append(segmented(KINDS, kind, (to) => { if (to !== kind) put(rule, to as Kind); }));
  const remove = el("button", "icon-btn perm-remove");
  remove.title = "Remove this rule";
  remove.append(icon("X", 14));
  remove.onclick = () => put(rule, null);
  controls.append(remove);
  row.append(text, controls);
  return row;
}

/** A new rule: a command (`git push`, made into `Bash(git push:*)`) or a rule as Claude Code writes them. */
function addRow(data: Json, save: (change: Json) => void): HTMLElement {
  const box = section("Add a rule");
  box.classList.add("perm-add");
  const line = el("div", "perm-add-line");
  const input = el("input", "fact-add perm-input") as HTMLInputElement;
  input.placeholder = "git push, or a rule like Edit(.env)";
  let kind: Kind = "ask";
  const picker = segmented(KINDS, kind, (v) => (kind = v as Kind));
  const add = el("button", "chip-btn bordered perm-add-btn", "Add");
  const preview = el("div", "dim small perm-preview");
  const show = () => {
    const rule = toRule(input.value);
    preview.replaceChildren(...(rule ? [document.createTextNode("Adds "), el("code", "", rule), document.createTextNode(": "), ...describe(rule)] : []));
    add.toggleAttribute("disabled", !rule);
  };
  const go = () => {
    const rule = toRule(input.value);
    if (!rule) return;
    const change: Json = { [kind]: [...((data[kind] ?? []) as string[]).filter((r) => r !== rule), rule] };
    for (const [k] of KINDS) if (k !== kind && (data[k] ?? []).includes(rule)) change[k] = data[k].filter((r: string) => r !== rule);
    save(change);
  };
  input.oninput = show;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  add.onclick = go;
  line.append(input, picker, add);
  box.append(line, preview);
  show();
  return box;
}

/** What was typed, as a rule: a rule (or a try at one, which the computer then explains) stays as it is;
 *  anything else is the start of a shell command. */
export function toRule(typed: string): string {
  const text = typed.trim();
  if (!text) return "";
  if (/^([A-Z*?][A-Za-z0-9_*?]*|mcp__[A-Za-z0-9_*?]+)(\(.*\))?$/.test(text) || /^[A-Z][A-Za-z0-9_]*\(/.test(text)) return text;
  const command = text.replace(/\s*(:\*|\*)$/, "").trim();
  return command ? `Bash(${command}:*)` : "";
}

/** A rule in words, with what it names set as code: `Bash(git push:*)` is "Run commands starting with git push". */
export function describe(rule: string): Node[] {
  const m = rule.match(/^([A-Za-z0-9_*?]+)(?:\((.*)\))?$/);
  if (!m) return [document.createTextNode(rule)];
  const tool = m[1];
  const spec = m[2] === undefined || m[2].trim() === "" || m[2].trim() === "*" ? "" : m[2];
  const words = (...parts: (string | [string])[]) => parts.map((p) => (typeof p === "string" ? document.createTextNode(p) : el("code", "", p[0])));
  if (tool === "Bash") {
    if (!spec) return words("Run any shell command");
    const prefix = spec.match(/^(.*?)(?::\*| \*)$/);
    return prefix ? words("Run commands starting with ", [prefix[1]]) : words("Run exactly ", [spec]);
  }
  const files: Record<string, [string, string]> = {
    Edit: ["Edit any file", "Edit files matching "], Write: ["Create or overwrite any file", "Create or overwrite files matching "],
    Read: ["Read any file", "Read files matching "], NotebookEdit: ["Edit any notebook", "Edit notebooks matching "],
    Glob: ["List files anywhere", "List files matching "], Grep: ["Search inside any file", "Search inside files matching "],
  };
  if (files[tool]) return spec ? words(files[tool][1], [spec]) : words(files[tool][0]);
  if (tool === "WebFetch") {
    if (!spec) return words("Fetch any web page");
    return spec.startsWith("domain:") ? words("Fetch pages from ", [spec.slice(7)]) : words("Fetch ", [spec]);
  }
  if (tool === "WebSearch") return words("Search the web");
  if (tool.startsWith("mcp__")) {
    const [, server = "", name = ""] = tool.split("__");
    if (!name || name === "*") return words("Use any tool from the ", [server], " connector");
    return words("Use ", [name.replace(/\*/g, "…")], " from ", [server === "*" ? "any connector" : server]);
  }
  return spec ? words("Use ", [tool], " on ", [spec]) : words("Use ", [tool]);
}

/** Every scout's default per action: follow its leash, or the same for all. A scout's own setting still wins. */
function scoutsSection(data: Json, save: (change: Json) => void): HTMLElement {
  const box = section("Scouts");
  box.classList.add("perm-scouts");
  box.append(el("div", "dim small perm-hint",
    "What every scout does on its own, unless you set it differently on the scout itself. Scouts keep to their own rules: " +
    "the exceptions above don't apply to them."));
  const word: Record<string, string> = { does: "does it", asks: "asks", never: "never" };
  for (const s of data.scouts ?? []) {
    const leash = `Leash: ${word[s.leash?.checkins] ?? "asks"} on Check-ins, ${word[s.leash?.autonomous] ?? "asks"} on Autonomous.`;
    const row = setRow(s.label, leash, segmented(
      [["default", "Leash"], ["does", "Does it"], ["asks", "Asks"], ["never", "Never"]], s.rule || "default",
      (v) => save({ scouts: { [s.action]: v } })));
    row.dataset.action = s.action;
    box.append(row);
  }
  return box;
}

/** Which brains hold to the rules and which are only told them. */
function whoSection(): HTMLElement {
  const box = section("Who follows these");
  const rows: [string, string][] = [
    ["Claude", "Follows every rule: Claude Code enforces them as its own."],
    ["Local models", "Follow every rule: Vision runs their tools and checks each call."],
    ["Grok and other agents", "Never allowed is enforced. Ask-first rules are only asked of them in their instructions."],
    ["Codex", "Gets the rules in its instructions only. For a hard limit, give it the read-only sandbox."],
  ];
  for (const [who, what] of rows) {
    const row = el("div", "set-row perm-who");
    const text = el("div", "set-text");
    text.append(el("div", "", who), el("div", "dim small", what));
    row.append(text);
    box.append(row);
  }
  return box;
}

/** Claude Code's own rules, from its settings.json: shown, never changed here. */
function claudeSection(claude: Json): HTMLElement {
  const box = section("Claude Code's own rules");
  box.classList.add("perm-claude");
  const hint = el("div", "dim small perm-hint");
  hint.append(document.createTextNode("Claude Code also applies these to Claude chats. They live in "), el("code", "", claude.path),
    document.createTextNode(", so change them there."));
  box.append(hint);
  for (const [kind] of KINDS) {
    for (const rule of claude[kind] ?? []) {
      const row = el("div", "set-row perm-row readonly");
      const text = el("div", "set-text");
      const label = el("div", "perm-label");
      label.append(...describe(rule));
      text.append(label, el("code", "perm-rule", rule));
      row.append(text, el("span", "dim small perm-kind", SAID[kind as Kind]));
      box.append(row);
    }
  }
  return box;
}
