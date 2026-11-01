// Computer: which Vision this window talks to.

import { el } from "../render";
import type { Page } from "./index";
import { closeSettings } from "./index";
import { section, setRow } from "./ui";

export const page: Page = {
  id: "computer",
  label: "Computer",
  icon: "Monitor",
  build(body, host) {
    const paired = host.paired();
    const where = host.local() ? "This computer" : paired?.url ?? "";
    const change = el("button", "chip-btn bordered", "Change…");
    change.onclick = () => { closeSettings(); host.showPairing(); };
    const disconnect = el("button", "chip-btn bordered danger", "Disconnect");
    disconnect.onclick = async () => { closeSettings(); await host.disconnect(); };
    const buttons = el("div", "set-buttons");
    buttons.append(change, disconnect);
    let name = host.machine();
    try { name ||= paired?.url ? new URL(paired.url).hostname : ""; } catch { /* not a URL */ }
    body.append(section("Computer", setRow(name || "Vision", where, buttons)));
  },
};
