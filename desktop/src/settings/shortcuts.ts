// Shortcuts: every key the window answers to, drawn as key caps for the computer it runs on, and the one
// for dictation, which can be changed (mic.ts reads it from here).
//
// A combo is written "Mod+Shift+KeyK": Mod is ⌘ on a Mac and Ctrl elsewhere, then Ctrl, Meta, Alt, Shift in
// that order, and last the key's KeyboardEvent.code. One order means two combos can be compared as strings.

import { el } from "../render";
import type { Page } from "./index";
import { section, setRow } from "./ui";

type Combo = string;

const DICTATION = "Mod+Shift+Space";
const MODIFIERS = ["Shift", "Control", "Alt", "Meta"];

// The menu's keys are set in lib.rs (`menu`) and can't be changed here: keep this table in step with it.
// The rest are keydown handlers: main.ts (composer, zoom, Escape), find.ts, scouts.ts.
const GROUPS: [string, [string, Combo[]][]][] = [
  ["Window", [
    ["New chat", ["Mod+KeyN"]],
    ["New scout", ["Mod+Shift+KeyN"]],
    ["Search chats", ["Mod+KeyK"]],
    ["All activity", ["Mod+Shift+KeyA"]],
    ["Coming up", ["Mod+Shift+KeyL"]],
    ["Usage", ["Mod+Shift+KeyU"]],
    ["Show or hide the sidebar", ["Mod+KeyB"]],
    ["Settings", ["Mod+Comma"]],
    ["Zoom in", ["Mod+Equal"]],
    ["Zoom out", ["Mod+Minus"]],
    ["Actual size", ["Mod+Digit0"]],
    ["Close a sheet, picture or menu", ["Escape"]],
  ]],
  ["Chat", [
    ["Send", ["Enter"]],
    ["New line", ["Shift+Enter"]],
    ["Switch between Auto and Plan", ["Shift+Tab"]],
    ["Take the suggested reply (empty box)", ["Tab"]],
    ["Change model", ["Mod+Shift+KeyM"]],
    ["Stop the reply", ["Mod+Period", "Escape"]],
    ["Open in Terminal", ["Mod+Shift+KeyT"]],
    ["Save a rename, or a message being edited", ["Enter"]],
  ]],
  ["Find in chat", [
    ["Find", ["Mod+KeyF"]],
    ["Next match", ["Enter", "Mod+KeyG"]],
    ["Previous match", ["Shift+Enter", "Mod+Shift+KeyG"]],
    ["Close", ["Escape"]],
  ]],
  ["Scouts", [
    ["Fill in the example (empty box)", ["Tab"]],
    ["Make the scout from your sentence", ["Mod+Enter"]],
  ]],
  ["Editing", [
    ["Undo", ["Mod+KeyZ"]],
    ["Redo", ["Mod+Shift+KeyZ"]],
    ["Cut, copy, paste", ["Mod+KeyX", "Mod+KeyC", "Mod+KeyV"]],
    ["Select all", ["Mod+KeyA"]],
  ]],
];
// Taken by the system's own menu items (lib.rs: quit, close, hide, minimize) though not listed above.
const RESERVED = ["Mod+KeyQ", "Mod+KeyW", "Mod+KeyH", "Mod+KeyM"];

const isMac = () => document.documentElement.dataset.os === "macos" || /Mac/.test(navigator.platform);

/** The combo a key press makes. */
export function comboOf(e: KeyboardEvent): Combo {
  const mac = isMac();
  const parts: string[] = [];
  if (mac ? e.metaKey : e.ctrlKey) parts.push("Mod");
  if (mac && e.ctrlKey) parts.push("Ctrl");
  if (!mac && e.metaKey) parts.push("Meta");
  if (e.altKey) parts.push("Alt");
  if (e.shiftKey) parts.push("Shift");
  parts.push(e.code);
  return parts.join("+");
}

const NAMES: Record<string, string> = {
  Comma: ",", Period: ".", Equal: "=", Minus: "-", Slash: "/", Backslash: "\\", Semicolon: ";", Quote: "'",
  BracketLeft: "[", BracketRight: "]", Backquote: "`", ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→",
  Escape: "Esc", Space: "Space", Tab: "Tab", Backspace: "Backspace", Delete: "Delete",
};

/** What each part of a combo is called on the caps of this computer's keyboard. */
function caps(combo: Combo): string[] {
  const mac = isMac();
  return combo.split("+").map((part) => {
    switch (part) {
      case "Mod": return mac ? "⌘" : "Ctrl";
      case "Ctrl": return mac ? "⌃" : "Ctrl";
      case "Meta": return mac ? "⌘" : "Super";
      case "Alt": return mac ? "⌥" : "Alt";
      case "Shift": return mac ? "⇧" : "Shift";
      case "Enter": return mac ? "Return" : "Enter";
    }
    return NAMES[part] ?? part.replace(/^(Key|Digit|Numpad)/, "");
  });
}

/** A combo as text, for a tooltip: ⌘⇧Space on a Mac, Ctrl+Shift+Space elsewhere. */
export function label(combo: Combo): string {
  return caps(combo).join(isMac() ? "" : "+");
}

function keycaps(combos: Combo[]): HTMLElement {
  const box = el("span", "keys");
  combos.forEach((combo, i) => {
    if (i) box.append(el("span", "dim small", "or"));
    for (const cap of caps(combo)) box.append(el("kbd", "keycap", cap));
  });
  return box;
}

/** The keys that dictate (mic.ts): hold to talk. */
export function dictationKeys(): Combo {
  return localStorage.getItem("dictation-keys") || DICTATION;
}

let listening: ((e: KeyboardEvent) => void) | null = null;
let listener: HTMLElement | null = null;  // the button waiting for the keys

/** Settings is waiting for a new combo: no other shortcut should act on the keys meanwhile. Settings
 *  closed (or on another page) is waiting no longer. */
export function capturing(): boolean {
  if (listening && !listener?.isConnected) stopListening();
  return listening !== null;
}

function stopListening() {
  if (listening) window.removeEventListener("keydown", listening, true);
  listening = null;
  listener = null;
}

/** What a combo already does, if anything. */
function taken(combo: Combo): string {
  for (const [, rows] of GROUPS) for (const [what, combos] of rows) if (combos.includes(combo)) return what;
  return RESERVED.includes(combo) ? "a menu item" : "";
}

function dictationRow(): HTMLElement {
  const HINT = "Hold to talk, let go to stop. Click the keys to change them.";
  const keys = el("button", "keys-btn");
  keys.title = "Change the keys";
  const reset = el("button", "chip-btn bordered keys-reset", "Reset");
  const box = el("div", "set-buttons");
  box.append(keys, reset);
  const row = setRow("Dictate", HINT, box);
  row.classList.add("keys-dictation");
  const hint = row.querySelector(".set-text .dim")!;

  const draw = (note = HINT) => {
    keys.classList.toggle("listening", capturing());
    keys.replaceChildren(capturing() ? el("span", "small", "Press the new keys…") : keycaps([dictationKeys()]));
    reset.hidden = dictationKeys() === DICTATION;
    hint.textContent = note;
  };
  const changed = () => window.dispatchEvent(new Event("dictation-keys"));

  keys.onclick = () => {
    if (capturing()) { stopListening(); draw(); return; }
    listener = keys;
    listening = (e: KeyboardEvent) => {
      if (!capturing()) return;
      e.preventDefault();
      e.stopImmediatePropagation();  // nothing else acts on it: not the Escape that closes Settings
      if (MODIFIERS.includes(e.key)) return;  // still on the way to the key itself
      if (e.key === "Escape" && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) { stopListening(); draw(); return; }
      // Without a modifier, a plain key would dictate instead of typing (the F keys are free).
      const combo = comboOf(e);
      if (!/^(Mod|Ctrl|Meta|Alt)\+/.test(combo) && !/^(Shift\+)?F\d+$/.test(combo)) {
        draw(isMac() ? "Hold ⌘, ⌥ or ⌃ with it." : "Hold Ctrl, Alt or Super with it.");
        return;
      }
      const used = taken(combo);
      if (used) { draw(`${label(combo)} is already ${used === "a menu item" ? used : `“${used}”`}. Try another.`); return; }
      stopListening();
      localStorage.setItem("dictation-keys", combo);
      draw();
      changed();
    };
    window.addEventListener("keydown", listening, true);
    draw("Press the new keys, or Esc to keep these.");
  };
  reset.onclick = () => {
    stopListening();
    localStorage.removeItem("dictation-keys");
    draw();
    changed();
  };
  draw();
  return row;
}

export const page: Page = {
  id: "shortcuts",
  label: "Shortcuts",
  icon: "Keyboard",
  build(body) {
    stopListening();  // a page drawn again (or left) stops waiting for keys
    body.append(section("Dictation", dictationRow(), setRow("Throw the recording away", "", keycaps(["Escape"]))));
    for (const [title, rows] of GROUPS) {
      body.append(section(title, ...rows.map(([what, combos]) => setRow(what, "", keycaps(combos)))));
    }
  },
};
