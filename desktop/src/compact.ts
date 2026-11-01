// Compacting a chat from the composer's context ring: a popover with how full the context is and a Compact
// button. The server has the brain summarise the conversation and carries on from the summary in a fresh
// session of the same chat (vision/server.py Chat.compact); it shows that under way like a turn (a `start`
// with a note, its status, then `done`), so the transcript draws it as it does any reply.

import { el, Json } from "./render";
import * as usage from "./usage";

export interface CompactHost {
  chat(): Json | undefined;    // the chat on screen, as its latest `chat` frame has it (none on a new chat's page)
  send(frame: Json): void;
  popover(anchor: HTMLElement, content: HTMLElement, width: number): void;
  closePopover(): void;
}

let host: CompactHost;
let available = false;  // hello.compact: the server takes `compact` frames (an older one calls them unknown)

export function setCompactAvailable(on: boolean) {
  available = on;
}

export function initCompact(h: CompactHost) {
  host = h;
  const ring = document.getElementById("context")!;
  ring.setAttribute("role", "button");
  ring.tabIndex = 0;
  ring.addEventListener("click", (e) => { e.stopPropagation(); toggle(ring); });
  ring.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); toggle(ring); }
  });
}

function toggle(ring: HTMLElement) {
  const pop = document.getElementById("popover")!;
  if (!pop.hidden && pop.querySelector(".context-pop")) host.closePopover();
  else open(ring);
}

function open(ring: HTMLElement) {
  const c = host.chat();
  const ctx = c?.context;
  if (!c || !ctx?.window) return;
  const fraction = Math.min(1, ctx.tokens / ctx.window);
  const level = fraction >= 0.9 ? "full" : fraction >= 0.7 ? "warn" : "";
  const box = el("div", "context-pop" + (level ? " " + level : ""));
  const head = el("div", "context-pop-head");
  head.append(el("span", "", "Context"), el("span", "context-pop-percent", `${Math.round(fraction * 100)}%`));
  const meter = el("div", "context-meter");
  const fill = el("div", "context-meter-fill");
  fill.style.width = `${fraction * 100}%`;
  meter.append(fill);
  const n = (x: number) => Math.round(x).toLocaleString("en-US");
  box.append(head, meter, el("div", "context-pop-tokens", `${n(ctx.tokens)} of ${n(ctx.window)} tokens`));
  box.append(el("p", "context-pop-text", level === "full" ? "The chat is nearly out of room. Compacting summarises it and carries on from the summary in a fresh session."
    : level === "warn" ? "The chat is filling up. Compacting summarises it and carries on from the summary in a fresh session, so there is room again."
    : "Compacting summarises the conversation and carries on from the summary in a fresh session, so the context empties."));
  if (!available) {
    box.append(el("p", "context-pop-text dim", "Compacting needs a newer Vision on that computer."));
  } else {
    const b = el("button", (level ? "primary" : "chip-btn bordered") + " context-compact", "Compact") as HTMLButtonElement;
    b.disabled = !!c.busy;
    b.title = c.busy ? "Compact once this reply has finished" : "Summarise the conversation and carry on in a fresh session";
    b.onclick = () => { host.send({ type: "compact", chat: c.chat }); host.closePopover(); };
    box.append(b);
  }
  const limits = el("button", "context-usage", "Subscription usage…");
  limits.onclick = () => { host.closePopover(); usage.open(); };  // the plan's own limits, a click from the chat's
  box.append(limits);
  host.popover(ring, box, 290);
}
