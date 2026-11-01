// General: how the window looks and whether closing it leaves Vision in the tray.

import { invoke } from "@tauri-apps/api/core";
import type { Page } from "./index";
import { section, segmented, setRow, toggle } from "./ui";

export const page: Page = {
  id: "general",
  label: "General",
  icon: "Palette",
  build(body, host) {
    body.append(section("Appearance", setRow("Theme", "System follows your desktop's light or dark setting.",
      segmented([["system", "System"], ["light", "Light"], ["dark", "Dark"]], localStorage.getItem("appearance") ?? "system", (v) => {
        if (v === "system") localStorage.removeItem("appearance"); else localStorage.setItem("appearance", v);
        host.applyAppearance();
      }))));

    if (host.hasTray()) {
      body.append(section("Window", setRow("Keep running in the tray", "Closing the window leaves Vision running, so replies and agents can still notify you.",
        toggle(localStorage.getItem("keep-in-tray") !== "off", (on) => {
          localStorage.setItem("keep-in-tray", on ? "on" : "off");
          invoke("set_keep_in_tray", { on });
        }))));
    }
  },
};
