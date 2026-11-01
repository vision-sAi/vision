// Settings: a sheet with its pages listed down the left, like the system's own settings. Each page is a
// file of its own in this folder: it builds its body from the pieces in ui.ts and reaches the rest of the
// window through `host`.

import { IconName, icon } from "../icons";
import { Json, el } from "../render";
import * as general from "./general";
import * as chats from "./chats";
import * as voice from "./voice";
import * as notifications from "./notifications";
import * as shortcuts from "./shortcuts";
import * as memory from "./memory";
import * as permissions from "./permissions";
import * as accounts from "./accounts";
import * as computer from "./computer";
import * as about from "./about";

/** What the pages need from the window (main.ts). */
export interface SettingsHost {
  http(method: string, path: string, query?: Json, body?: Json): Promise<any>;
  toast(text: string): void;
  /** The computer this window talks to, and what it said in its hello. */
  paired(): { url: string; name: string } | null;
  local(): boolean;
  machine(): string;
  serverVersion(): string;
  voiceOn(): boolean;
  setVoiceOn(on: boolean): void;
  hasTray(): boolean;
  applyAppearance(): void;
  /** The model picker the composer uses, anchored to `anchor`. */
  modelMenu(anchor: HTMLElement, data: Json, levels: (model: string) => Promise<Json>, pick: (model: string, effort: string, label: string) => void): void;
  modelLabel(data: Json, model: string, effort: string): string;
  /** The default model or folder changed: an empty draft picks it up. */
  defaultsChanged(): void;
  showPairing(): void;
  disconnect(): Promise<void>;
  /** The desktop app's own update: look for one, and install the one found (then relaunch). */
  checkAppUpdate(): Promise<{ version: string; notes: string } | null>;
  installAppUpdate(): Promise<void>;
}

export interface Page {
  id: string;
  label: string;
  icon: IconName;
  /** Fill `body`. Rows that need the server may arrive after it returns. */
  build(body: HTMLElement, host: SettingsHost): void | Promise<void>;
  /** Left out of the list when false (e.g. subscriptions on another computer). */
  shown?(host: SettingsHost): boolean;
}

const PAGES: Page[] = [
  general.page, chats.page, voice.page, notifications.page, shortcuts.page,
  memory.page, permissions.page, accounts.page, computer.page, about.page,
];

let host: SettingsHost;
let current = "general";

export function setHost(h: SettingsHost) {
  host = h;
}

/** Open Settings, on `page` if given, else where it was left. */
export function open(id?: string) {
  if (id) current = id;
  const sheet = document.getElementById("settings")!;
  const card = document.getElementById("settings-card")!;
  card.classList.remove("scout-sheet");
  card.classList.add("set-paged");
  const pages = PAGES.filter((p) => p.shown?.(host) ?? true);
  if (!pages.some((p) => p.id === current)) current = pages[0].id;

  const nav = el("nav", "set-nav");
  nav.append(el("div", "set-nav-title", "Settings"));
  for (const p of pages) {
    const b = el("button", "set-nav-item" + (p.id === current ? " on" : ""));
    b.dataset.page = p.id;
    b.append(icon(p.icon, 15), el("span", "", p.label));
    b.onclick = () => open(p.id);
    nav.append(b);
  }

  const page = pages.find((p) => p.id === current)!;
  const head = el("div", "set-head");
  const close = el("button", "icon-btn set-close");
  close.title = "Close";
  close.append(icon("X", 16));
  close.onclick = closeSettings;
  head.append(el("h2", "", page.label), close);
  const body = el("div", "set-body");
  body.dataset.page = page.id;
  const main = el("div", "set-main");
  main.append(head, body);

  card.replaceChildren(nav, main);
  sheet.hidden = false;
  Promise.resolve(page.build(body, host)).catch((err) => host.toast(String(err)));
}

/** Draw the open page again (something it shows changed, e.g. the voice was switched on elsewhere). */
export function refresh() {
  if (isOpen()) open();
}

export function isOpen(): boolean {
  const card = document.getElementById("settings-card")!;
  return !document.getElementById("settings")!.hidden && card.classList.contains("set-paged");
}

export function closeSettings() {
  document.getElementById("settings")!.hidden = true;
  document.getElementById("settings-card")!.classList.remove("set-paged");
}
