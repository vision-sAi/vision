// New chats: what a chat started from this window begins with. The computer keeps these (GET/POST
// /defaults), so the phone's new chats start the same way.

import { icon } from "../icons";
import { Json, el } from "../render";
import type { Page } from "./index";
import { section, segmented, setRow } from "./ui";

export const page: Page = {
  id: "chats",
  label: "New chats",
  icon: "MessageSquare",
  build(body, host) {
    const defaultsBtn = el("button", "chip-btn bordered", "…");
    const box = section("New chats", setRow("Model", "What a new chat starts on. Open chats keep their own.", defaultsBtn));
    body.append(box);
    const save = async (change: Json) => {
      try {
        const data = await host.http("POST", "defaults", undefined, change);
        host.defaultsChanged();
        return data;
      } catch (err) { host.toast(String(err)); return null; }
    };
    host.http("GET", "defaults").then((data: Json) => {
      defaultsBtn.textContent = host.modelLabel(data, data.current, data.effort);
      defaultsBtn.append(icon("ChevronDown", 14));
      defaultsBtn.onclick = (e) => {
        e.stopPropagation();
        host.modelMenu(defaultsBtn, data, (model) => host.http("GET", "models", { model }), async (model, effort, label) => {
          if (await save({ model, effort })) defaultsBtn.replaceChildren(document.createTextNode(label), icon("ChevronDown", 14));
        });
      };
      if (data.mode === undefined) return;  // an older Vision: only the model is kept

      let workdir: string = data.workdir ?? "";
      const folderBtn = el("button", "chip-btn bordered set-folder");
      const drawFolder = (label: string) => folderBtn.replaceChildren(icon("Folder", 14), el("span", "", label), icon("ChevronDown", 14));
      drawFolder(data.folder || workdir || "Folder");
      folderBtn.title = workdir;
      folderBtn.onclick = (e) => {
        e.stopPropagation();
        host.folderMenu(folderBtn, workdir, async (path, label) => {
          const saved = await save({ workdir: path });
          if (!saved) return;
          workdir = saved.workdir ?? path;
          folderBtn.title = workdir;
          drawFolder(saved.folder || label);
        });
      };
      box.append(
        setRow("Folder", "Where a new chat works. The folder button by the message box picks another for one chat.", folderBtn),
        setRow("Mode", "Auto runs tools as it goes. Plan looks around first and asks before changing anything.",
          segmented([["auto", "Auto"], ["plan", "Plan"]], data.mode, (mode) => { save({ mode }); })),
      );
    }).catch(() => box.remove());
  },
};
