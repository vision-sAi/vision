// Voice: Vision speaking its replies, which voice, how fast.

import { Json, el } from "../render";
import type { Page } from "./index";
import { refresh } from "./index";
import { section, setRow, toggle } from "./ui";

export const page: Page = {
  id: "voice",
  label: "Voice",
  icon: "Volume2",
  build(body, host) {
    const voice = section("Voice", setRow("Vision's voice", "Speaks replies and reads them aloud. It runs on the computer's GPU.",
      toggle(host.voiceOn(), async (on) => {
        try { host.setVoiceOn((await host.http("POST", "voice/power", undefined, { on })).on); } catch (err) { host.toast(String(err)); }
        refresh();
      })));
    body.append(voice);
    if (!host.voiceOn()) return;
    host.http("GET", "voice").then((data: Json) => {
      const select = el("select", "select") as HTMLSelectElement;
      for (const v of data.voices ?? []) {
        const o = el("option", "", v.note ? `${v.id} · ${v.note}` : v.id) as HTMLOptionElement;
        o.value = v.id;
        o.selected = v.id === data.voice;
        select.append(o);
      }
      select.onchange = () => host.http("POST", "voice", undefined, { voice: select.value }).catch((err) => host.toast(String(err)));
      voice.append(setRow("Voice", "", select));
    }).catch(() => {});
    host.http("GET", "voice/rate").then((data: Json) => {
      const wrap = el("div", "rate");
      const range = el("input") as HTMLInputElement;
      Object.assign(range, { type: "range", min: "0.5", max: "2", step: "0.05", value: String(data.rate ?? 1) });
      const shown = el("span", "dim small", `${Number(range.value).toFixed(2)}×`);
      range.oninput = () => (shown.textContent = `${Number(range.value).toFixed(2)}×`);
      range.onchange = () => host.http("POST", "voice/rate", undefined, { rate: Number(range.value) }).catch((err) => host.toast(String(err)));
      wrap.append(range, shown);
      voice.append(setRow("Speed", "", wrap));
    }).catch(() => {});
  },
};
