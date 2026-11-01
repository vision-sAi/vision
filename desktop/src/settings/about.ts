// About: which versions are running, and updating both: this app (from GitHub's releases) and Vision on
// the computer (`vision upgrade`, run from here when that computer is this one).

import { getVersion } from "@tauri-apps/api/app";
import { invoke } from "@tauri-apps/api/core";
import { openUrl } from "@tauri-apps/plugin-opener";
import { icon } from "../icons";
import { el } from "../render";
import type { Page, SettingsHost } from "./index";
import { section, setRow } from "./ui";

const RELEASES = "https://github.com/vision-sAi/vision/releases";
const GUIDE = "https://github.com/vision-sAi/vision/blob/master/docs/guide.md";

export const page: Page = {
  id: "about",
  label: "About",
  icon: "Info",
  async build(body, host) {
    const [version, kind] = await Promise.all([
      getVersion().catch(() => ""),
      invoke<string>("install_kind").catch(() => "dev"),
    ]);
    const link = (label: string, hint: string, url: string) => {
      const button = el("button", "chip-btn bordered", "Open");
      button.onclick = () => openUrl(url).catch((err) => host.toast(String(err)));
      return setRow(label, hint, button);
    };
    body.append(appSection(version, kind, host), serverSection(host), section("Links",
      link("Release notes", "Every version of Vision and this app, and what changed.", RELEASES),
      link("Guide", "How Vision works: its settings, commands and the apps.", GUIDE)));
  },
};

/** A row whose hint changes as things happen (setRow's stays as it was given). */
function liveRow(label: string, hint: string, control: HTMLElement): [HTMLElement, HTMLElement] {
  const row = setRow(label, "", control);
  const text = el("div", "dim small", hint);
  row.querySelector(".set-text")!.append(text);
  return [row, text];
}

/** This app: its version, and a look for a newer one (installed in place where the app updates itself). */
function appSection(version: string, kind: string, host: SettingsHost): HTMLElement {
  const button = el("button", "chip-btn bordered", "Check for updates") as HTMLButtonElement;
  const [row, hint] = liveRow(`Vision desktop ${version}`, kind === "dev" ? "A development build. Updates come with releases." : "", button);
  if (kind === "dev") { button.remove(); return section("This app", row); }
  const selfUpdates = kind === "app" || kind === "appimage";
  button.onclick = async () => {
    button.disabled = true;
    button.textContent = "Checking…";
    hint.textContent = "";
    try {
      const found = await host.checkAppUpdate();
      button.disabled = false;
      if (!found) {
        hint.textContent = "This is the newest version.";
        button.textContent = "Check for updates";
        return;
      }
      hint.textContent = `Vision desktop ${found.version} is out.`;
      button.textContent = selfUpdates ? "Restart to update" : "Download";
      button.onclick = async () => {
        if (!selfUpdates) { host.installAppUpdate(); return; }  // the releases page: a package updates with the system
        button.disabled = true;
        button.textContent = "Downloading…";
        hint.textContent = `Getting Vision desktop ${found.version}. The app restarts when it's in.`;
        try { await host.installAppUpdate(); } catch (err) {
          hint.textContent = `The update didn't install: ${err}`;
          button.textContent = "Download";
          button.disabled = false;
          button.onclick = () => openUrl(RELEASES);
        }
      };
    } catch (err) {
      hint.textContent = `Couldn't look for updates: ${err}`;
      button.textContent = "Try again";
      button.disabled = false;
    }
  };
  return section("This app", row);
}

/** Vision on the computer this window talks to: its version, and how to update it when a newer one is out. */
function serverSection(host: SettingsHost): HTMLElement {
  const where = host.local() ? "this computer" : host.machine() || "the computer";
  const version = host.serverVersion();
  const how = host.serverUpdate();
  // Only the installed Vision updates itself; a working checkout gets its git command shown instead.
  const upgradable = !!how && host.local() && how === "vision upgrade";
  const button = el("button", "chip-btn bordered", "Upgrade Vision") as HTMLButtonElement;
  const [row, hint] = liveRow(version ? `Vision ${version}` : "Vision", how ? "A newer version is out." : "Up to date.", button);
  const box = section(`Vision on ${where}`, row);
  if (!upgradable) button.remove();
  if (!how) return box;
  if (upgradable) {
    button.onclick = async () => {
      button.disabled = true;
      button.textContent = "Upgrading…";
      const lines: string[] = [];
      const code = await host.upgradeServer((t) => { if (t.trim()) { lines.push(t); hint.textContent = t.slice(0, 90); } });
      hint.textContent = code === 0
        ? "Updated. Vision restarts once every reply has finished, and this window reconnects by itself."
        : `The update stopped: ${lines.slice(-1)[0] ?? "no output"}`;
      button.textContent = code === 0 ? "Upgraded" : "Try again";
      button.disabled = code === 0;
    };
    return box;
  }
  hint.textContent = `A newer version is out. To update, run this on ${where}:`;
  const command = el("div", "set-command");
  const copy = el("button", "icon-btn");
  copy.title = "Copy";
  copy.append(icon("Copy", 14));
  copy.onclick = () => { navigator.clipboard.writeText(how).then(() => host.toast("Copied."), () => {}); };
  command.append(el("code", "", how), copy);
  box.append(command);
  return box;
}
