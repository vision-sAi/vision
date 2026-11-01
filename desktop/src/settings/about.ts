// About: which versions are running.

import { getVersion } from "@tauri-apps/api/app";
import { el } from "../render";
import type { Page } from "./index";

export const page: Page = {
  id: "about",
  label: "About",
  icon: "Info",
  async build(body, host) {
    const version = await getVersion().catch(() => "");
    const server = host.serverVersion();
    body.append(el("p", "set-about dim small", `Vision desktop ${version}${server ? ` · Vision ${server} on ${host.machine() || "the computer"}` : ""}`));
  },
};
