// The pieces every Settings page is made of: a titled section, a row (label, hint, control), a segmented
// picker and a switch.

import { el } from "../render";

export function section(title: string, ...rows: HTMLElement[]): HTMLElement {
  const box = el("section", "set-section");
  box.append(el("h3", "", title), ...rows);
  return box;
}

export function setRow(label: string, hint: string, control: HTMLElement): HTMLElement {
  const row = el("div", "set-row");
  const text = el("div", "set-text");
  text.append(el("div", "", label));
  if (hint) text.append(el("div", "dim small", hint));
  row.append(text, control);
  return row;
}

export function segmented(options: [string, string][], current: string, pick: (v: string) => void): HTMLElement {
  const box = el("div", "segmented");
  for (const [value, label] of options) {
    const b = el("button", value === current ? "on" : "", label);
    b.onclick = () => { box.querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); pick(value); };
    box.append(b);
  }
  return box;
}

export function toggle(on: boolean, flip: (on: boolean) => void): HTMLElement {
  const b = el("button", "toggle" + (on ? " on" : ""));
  b.setAttribute("role", "switch");
  b.onclick = () => { b.classList.toggle("on"); flip(b.classList.contains("on")); };
  return b;
}
