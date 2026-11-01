// New chats: what a chat started from this window begins with.

import { icon } from "../icons";
import { Json, el } from "../render";
import type { Page } from "./index";
import { section, setRow } from "./ui";

export const page: Page = {
  id: "chats",
  label: "New chats",
  icon: "MessageSquare",
  build(body, host) {
    const defaultsBtn = el("button", "chip-btn bordered", "…");
    body.append(section("New chats", setRow("Model", "What a new chat starts on. Open chats keep their own.", defaultsBtn)));
    host.http("GET", "defaults").then((data: Json) => {
      defaultsBtn.textContent = host.modelLabel(data, data.current, data.effort);
      defaultsBtn.append(icon("ChevronDown", 14));
      defaultsBtn.onclick = (e) => {
        e.stopPropagation();
        host.modelMenu(defaultsBtn, data, (model) => host.http("GET", "models", { model }), async (model, effort, label) => {
          try {
            data = await host.http("POST", "defaults", undefined, { model, effort });
            defaultsBtn.replaceChildren(document.createTextNode(label), icon("ChevronDown", 14));
            host.defaultsChanged();
          } catch (err) { host.toast(String(err)); }
        });
      };
    }).catch(() => defaultsBtn.closest(".set-section")?.remove());
  },
};
