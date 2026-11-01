// Notifications: which kinds the desktop shows, a sound, quiet hours, and the system's permission. main.ts
// asks `allowed(kind)` before each one; the sidebar badge and the tray keep counting whatever is muted.

import { isPermissionGranted, requestPermission, sendNotification } from "@tauri-apps/plugin-notification";
import { el } from "../render";
import type { Page, SettingsHost } from "./index";
import { refresh } from "./index";
import { section, setRow, toggle } from "./ui";

/** What a notification is about: each has its switch. */
export type Kind = "replies" | "asks" | "approvals" | "review" | "reports";

const KINDS: [Kind, string, string][] = [
  ["replies", "Replies", "A reply finishes, a scheduled prompt's too, while you're in another window."],
  ["asks", "Questions", "A chat asks you something, or an agent's thread needs you."],
  ["approvals", "Approvals", "A scout asks before running a command."],
  ["review", "Finished work", "An agent's thread is ready for you to review."],
  ["reports", "Scout reports", "A scout sends its status report."],
];

interface Prefs { off: Kind[]; sound: boolean; quiet: { on: boolean; from: string; to: string } }

function prefs(): Prefs {
  let saved: Partial<Prefs> = {};
  try { saved = JSON.parse(localStorage.getItem("notifications") ?? "{}"); } catch { /* start over */ }
  return { off: saved.off ?? [], sound: saved.sound ?? true, quiet: { on: false, from: "22:00", to: "08:00", ...saved.quiet } };
}

function save(change: (p: Prefs) => void) {
  const p = prefs();
  change(p);
  localStorage.setItem("notifications", JSON.stringify(p));
}

const isMac = () => document.documentElement.dataset.os === "macos" || /Mac/.test(navigator.platform);
const minutes = (hhmm: string) => { const [h, m] = hhmm.split(":").map(Number); return (h || 0) * 60 + (m || 0); };

/** Inside quiet hours at `now`. The window may run past midnight (22:00 to 08:00). */
export function quiet(now = new Date()): boolean {
  const q = prefs().quiet;
  if (!q.on) return false;
  const t = now.getHours() * 60 + now.getMinutes(), from = minutes(q.from), to = minutes(q.to);
  return from <= to ? t >= from && t < to : t >= from || t < to;
}

/** Whether a notification of this kind may be shown now. */
export function allowed(kind: Kind, now = new Date()): boolean {
  return !prefs().off.includes(kind) && !quiet(now);
}

/** What to add to a notification for its sound: a system sound by name, or nothing when it's off. */
export function sound(): { sound?: string } {
  if (!prefs().sound) return {};
  return { sound: isMac() ? "Glass" : "message-new-instant" };
}

export const page: Page = {
  id: "notifications",
  label: "Notifications",
  icon: "Bell",
  async build(body, host) {
    const p = prefs();
    body.append(section("Notify me about", ...KINDS.map(([kind, label, hint]) => {
      const row = setRow(label, hint, toggle(!p.off.includes(kind), (on) =>
        save((q) => { q.off = q.off.filter((k) => k !== kind); if (!on) q.off.push(kind); })));
      row.dataset.kind = kind;
      return row;
    })));
    // A reminder is put on the screen by the computer Vision runs on (server.py's notify-send), not here.
    body.append(el("p", "dim small", "Reminders show on the computer Vision runs on, and on a paired phone."));

    body.append(section("Sound", setRow("Play a sound", "The system's message sound, with each notification.",
      toggle(p.sound, (on) => save((q) => { q.sound = on; })))));

    const time = (value: string, set: (q: Prefs, v: string) => void) => {
      const input = el("input", "select") as HTMLInputElement;
      input.type = "time";
      input.value = value;
      input.disabled = !p.quiet.on;
      input.onchange = () => { if (input.value) save((q) => set(q, input.value)); };
      return input;
    };
    const from = time(p.quiet.from, (q, v) => { q.quiet.from = v; });
    const to = time(p.quiet.to, (q, v) => { q.quiet.to = v; });
    from.classList.add("quiet-from");
    to.classList.add("quiet-to");
    const span = el("div", "set-buttons quiet-span");
    span.append(from, el("span", "dim small", "to"), to);
    const pause = toggle(p.quiet.on, (on) => { save((q) => { q.quiet.on = on; }); from.disabled = to.disabled = !on; });
    pause.classList.add("quiet-on");
    body.append(section("Quiet hours",
      setRow("Pause notifications", "Nothing notifies in these hours. The sidebar and the tray still keep count.", pause),
      setRow("Every day from", "By this computer's clock.", span)));

    const system = section("System");
    body.append(system);
    const granted = await isPermissionGranted().catch(() => false);
    let control: HTMLElement;
    if (granted) control = el("span", "dim small set-permission", "Allowed");
    else {
      control = el("button", "chip-btn bordered set-permission", "Allow…");
      control.onclick = async () => {
        const answer = await requestPermission().catch(() => "denied");
        if (answer !== "granted") blocked(host);
        refresh();
      };
    }
    system.append(setRow("Permission", granted ? "The system lets Vision show notifications."
      : "The system isn't letting Vision show notifications yet.", control));

    const test = el("button", "chip-btn bordered set-test", "Send");
    test.onclick = async () => {
      if (!(await isPermissionGranted().catch(() => false))) { blocked(host); return; }
      sendNotification({ title: "Vision", body: "Notifications are working.", ...sound() });
    };
    system.append(setRow("Send a test notification", "Shows one now, whatever the switches above say.", test));
  },
};

function blocked(host: SettingsHost) {
  host.toast(isMac() ? "Notifications are off for Vision: turn them on in System Settings › Notifications."
    : "Notifications are off for Vision in the system's settings.");
}
