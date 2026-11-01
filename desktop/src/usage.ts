// The Usage sheet: each subscription's windows (session, week, a model's own week) as a bar with when it
// resets beneath, and the resets it has banked, as the phone's Usage screen has them (GET /usage). It reads
// usage again every time it opens; the last report stays on screen meanwhile. main.ts opens it.

import { IconName, icon } from "./icons";
import { Json, el } from "./render";

export interface UsageHost {
  http: (method: string, path: string, query?: Json, body?: Json) => Promise<any>;
  /** The chat on screen: its brain answers for its own provider. */
  chat: () => string | null;
}

let host: UsageHost;
let report: Json | null = null;    // the last GET /usage
let fetchedAt = 0;                 // when it came, ms
let error = "";                    // why the last read failed
let refreshing = false;
let tints: Record<string, string> | null = null; // GET /providers: how the computer draws each provider
let body: HTMLElement | null = null;             // the sheet's body while it's open
const outcomes = new Map<string, { ok: boolean; message: string }>(); // provider → what a Use tap said, until it closes
let confirming = "";               // "<provider>/<index>": the banked reset whose Use is asking to be sure
let spending = "";
let ticker = 0;

export function setHost(h: UsageHost) {
  host = h;
}

// The phone's fallbacks (ProviderStyle) for before /providers answers, or a computer that doesn't say.
const TINT: Record<string, string> = { claude: "#d97857", codex: "#0f997a", grok: "#5c6680", local: "#6161ed" };
const ICON: Record<string, IconName> = { claude: "Sparkles", codex: "Terminal", grok: "Zap", local: "Monitor" };
const ORDER = ["claude", "codex", "grok", "local"];
const TITLE: Record<string, string> = { claude: "Claude", codex: "Codex", grok: "Grok", local: "Local" };

const tintOf = (p: string) => tints?.[p] || TINT[p] || "#8a8799";

/** Open the sheet and read usage again. */
export function open() {
  const sheet = document.getElementById("settings")!;
  const card = document.getElementById("settings-card")!;
  card.className = "settings-card";
  outcomes.clear();
  confirming = "";
  const head = el("div", "set-head");
  const actions = el("div", "set-buttons");
  const refresh = el("button", "icon-btn usage-refresh");
  refresh.append(icon("RotateCcw", 15));
  refresh.title = "Refresh";
  refresh.onclick = () => void load();
  const close = el("button", "icon-btn");
  close.append(icon("X", 16));
  close.title = "Close";
  close.onclick = () => { sheet.hidden = true; };
  actions.append(refresh, close);
  head.append(el("h2", "", "Usage"), actions);
  body = el("div", "set-body usage-body");
  card.replaceChildren(head, body);
  sheet.hidden = false;
  window.clearInterval(ticker);
  // "Updated 3 min ago" ticks over while the sheet is open; it stops once something else takes the sheet.
  ticker = window.setInterval(() => {
    if (!body?.isConnected || sheet.hidden) { window.clearInterval(ticker); body = null; return; }
    drawUpdated();
  }, 15000);
  draw();
  void load();
  if (!tints) {
    host.http("GET", "providers").then((r: Json) => {
      tints = Object.fromEntries((r.providers ?? []).filter((p: Json) => p.tint).map((p: Json) => [p.name, p.tint]));
      draw();
    }).catch(() => { tints = {}; });
  }
}

async function load() {
  if (refreshing) return;
  refreshing = true;
  draw();
  try {
    const chat = host.chat();
    report = await host.http("GET", "usage", { provider: "all", ...(chat ? { chat } : {}) });
    fetchedAt = Date.now();
    error = "";
  } catch (e) {
    error = String(e);  // the last report, if any, stays on screen
  }
  refreshing = false;
  draw();
}

function draw() {
  if (!body?.isConnected) return;
  const box = body;
  box.parentElement?.querySelector(".usage-refresh")?.classList.toggle("spinning", refreshing);
  if (!report && !refreshing && error) {
    const empty = el("div", "usage-empty");
    const again = el("button", "chip-btn bordered", "Try again");
    again.onclick = () => void load();
    empty.append(icon("CircleAlert", 26), el("b", "", "Couldn't read usage"), el("span", "dim small", error), again);
    box.replaceChildren(empty);
    return;
  }
  box.replaceChildren(el("p", "usage-updated dim small"));
  drawUpdated();
  if (!report) {
    for (const p of ["claude", "codex", "grok"]) box.append(card({ provider: p, label: TITLE[p], windows: [
      { label: "Current session" }, { label: "Current week" }] }, true));
    return;
  }
  const rank = (p: string) => (ORDER.includes(p) ? ORDER.indexOf(p) : 99);
  const rows: Json[] = [...(report.providers ?? [])].sort((a, b) => rank(a.provider) - rank(b.provider));
  for (const p of rows) box.append(card(p, false));
  if (!rows.length) box.append(el("p", "dim small", "No subscriptions to show."));
}

/** "Updated just now" / "Updated 3 min ago" / "Updated 2:41 PM"; "Refreshing…" while it reads. */
function drawUpdated() {
  const line = body?.querySelector(".usage-updated");
  if (!line) return;
  const secs = (Date.now() - fetchedAt) / 1000;
  line.textContent = refreshing ? "Refreshing…" : !fetchedAt ? "" : secs < 60 ? "Updated just now"
    : secs < 3600 ? `Updated ${Math.floor(secs / 60)} min ago` : `Updated ${clock(fetchedAt / 1000)}`;
}

/** One provider: its name and plan, a bar per window, its banked resets and any notes. */
function card(p: Json, loading: boolean): HTMLElement {
  const box = el("section", "usage-card" + (loading ? " loading" : ""));
  box.style.setProperty("--tint", tintOf(p.provider));
  const head = el("div", "usage-head");
  const badge = el("span", "usage-badge");
  badge.append(icon(ICON[p.provider] ?? "Sparkles", 15));
  head.append(badge, el("b", "", p.label || TITLE[p.provider] || p.provider));
  if (p.plan) head.append(el("span", "usage-plan", p.plan));
  if (p.source === "snapshot" || p.source === "rollout") {
    const asOf = el("span", "usage-asof dim small");
    asOf.append(icon("Clock", 12), document.createTextNode(p.at ? `as of ${clock(p.at)}` : "snapshot"));
    head.append(asOf);
  }
  box.append(head);
  const windows: Json[] = p.windows ?? [];
  if (p.error) {
    const err = el("div", "usage-error");
    err.append(icon("CircleAlert", 14), el("span", "", p.error));
    box.append(err);
  } else if (!windows.length) {
    box.append(el("p", "dim small usage-none", "Nothing to show yet."));
  } else {
    const bars = el("div", "usage-bars");
    for (const w of windows) bars.append(bar(w, loading));
    box.append(bars);
  }
  const banked = bankedItems(p.banked);
  if (banked.length && !loading) box.append(bankedRows(p, banked));
  const said = outcomes.get(p.provider);  // outside the banked rows: spending the last reset takes them away
  if (said) {
    const line = el("div", "usage-outcome" + (said.ok ? " ok" : ""));
    line.append(icon(said.ok ? "Check" : "CircleAlert", 13), el("span", "", said.message));
    box.append(line);
  }
  if (p.notes?.length) box.append(el("p", "usage-notes", p.notes.join(" · ")));
  return box;
}

/** "Current session" reads as "Session", and so on; anything else keeps its own name. */
function title(label: string): string {
  return ({ "Current session": "Session", "Current week": "Week", "Current window": "Window" } as Record<string, string>)[label] ?? label;
}

/** A window as one line of words over a bar, then when it resets. Status colours only when a limit is close:
 * amber from 75%, red from 90%; below that the bar wears its provider's colour. */
function bar(w: Json, loading: boolean): HTMLElement {
  const pct = typeof w.used_percent === "number" ? Math.max(0, Math.min(100, w.used_percent)) : null;
  const level = pct === null ? "" : pct >= 90 ? " full" : pct >= 75 ? " warn" : "";
  const box = el("div", "usage-bar" + level);
  const top = el("div", "usage-bar-top");
  top.append(el("span", "usage-bar-title", title(w.label ?? "")), el("span", "usage-pct" + (pct === null ? " dim" : ""), pct === null ? "—" : `${Math.round(pct)}%`));
  const track = el("div", "usage-track");
  if (pct !== null || loading) {
    const fill = el("div", "usage-fill");
    // A sliver stays visible at 0% so the bar still reads as live.
    fill.style.width = loading ? "35%" : pct! > 0 ? `max(8px, ${pct}%)` : "0";
    track.append(fill);
  }
  box.append(top, track, el("div", "usage-resets dim small", loading ? "Resets soon" : resets(w, pct)));
  box.title = pct === null ? "Not reported" : `${Math.round(pct)}% used`;
  return box;
}

/** "Resets in 4h 12m" inside a day, "Resets Thu 10:59 PM" beyond; the computer's own wording otherwise. */
function resets(w: Json, pct: number | null): string {
  if (pct === null && !w.resets_at) return "Not reported";
  if (!w.resets_at) return w.resets ? `Resets ${w.resets}` : "";
  const secs = w.resets_at - Date.now() / 1000;
  if (secs < -60) return "Already reset";  // a snapshot can outlive its window; its percent is from before the reset
  if (secs <= 60) return "Resets now";
  if (secs < 3600) return `Resets in ${Math.floor(secs / 60)}m`;
  if (secs < 86400) {
    const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
    return m ? `Resets in ${h}h ${m}m` : `Resets in ${h}h`;
  }
  const at = new Date(w.resets_at * 1000);
  return `Resets ${at.toLocaleDateString([], { weekday: "short" })} ${clock(w.resets_at)}`;
}

/** One row per saved reset: the computer's list, or its groups spelt out one by one. */
function bankedItems(banked: Json | null | undefined): Json[] {
  if (!banked?.count) return [];
  if (banked.items?.length) return banked.items;
  const groups: Json[] = banked.resets?.length ? banked.resets : [{ count: banked.count, expires_at: banked.expires_at }];
  return groups.flatMap((g) => Array.from({ length: g.count ?? 0 }, () => ({ kind: g.kind, expires_at: g.expires_at })));
}

/** Every saved reset on its own row: what it clears, when it lapses, and Use, which asks once more first. */
function bankedRows(p: Json, items: Json[]): HTMLElement {
  const box = el("div", "usage-banked");
  box.append(el("div", "usage-banked-head", "Banked resets"));
  items.forEach((item, i) => {
    const k = `${p.provider}/${i}`;
    const row = el("div", "usage-reset");
    const mark = el("span", "usage-reset-mark");
    mark.append(icon("RotateCcw", 14));
    const text = el("div", "usage-reset-text");
    text.append(el("div", "usage-reset-kind", item.kind || "Usage reset"), expiry(item.expires_at));
    const use = el("button", "chip-btn usage-use" + (confirming === k ? " confirm" : ""), spending === k ? "Using…" : confirming === k ? "Use now" : "Use") as HTMLButtonElement;
    use.disabled = !!spending;
    use.title = `Use ${item.kind || "a usage reset"}`;
    use.onclick = () => {
      if (confirming !== k) { confirming = k; draw(); return; }
      void spend(p.provider, item, k);
    };
    row.append(mark, text, use);
    box.append(row);
    if (confirming === k && spending !== k) {
      const sure = el("div", "usage-sure dim small", `This clears ${clears(item.kind, p.label || p.provider)} straight away. It can't be undone.`);
      const no = el("button", "link-btn", "Cancel");
      no.onclick = () => { confirming = ""; draw(); };
      sure.append(" ", no);
      box.append(sure);
    }
  });
  return box;
}

/** "Expires Oct 22 · 28 days left", amber in its last three days; "No expiry date" when unknown. */
function expiry(ts: number | null | undefined): HTMLElement {
  if (!ts) return el("div", "dim small", "No expiry date");
  const days = Math.max(0, Math.ceil((ts - Date.now() / 1000) / 86400));
  const left = days <= 1 ? "last day" : `${days} days left`;
  const day = new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  return el("div", "small " + (days <= 3 ? "usage-soon" : "dim"), `Expires ${day} · ${left}`);
}

function clears(kind: string | undefined, label: string): string {
  if (kind === "Full Reset") return `your ${label} session and weekly limits`;
  if (kind === "Session Reset") return `your ${label} session limit`;
  if (kind === "Weekly Reset") return `your ${label} weekly limit`;
  return `your ${label} usage limits`;
}

async function spend(provider: string, item: Json, k: string) {
  spending = k;
  draw();
  try {
    const chat = host.chat();
    const r = await host.http("POST", "usage/reset", undefined, { provider, ...(item.id ? { id: item.id } : {}), ...(chat ? { chat } : {}) });
    outcomes.set(provider, { ok: !!r.ok, message: r.message ?? (r.ok ? "Done." : "That didn't work.") });
    if (r.provider && report) report.providers = (report.providers ?? []).map((p: Json) => (p.provider === provider ? r.provider : p));
  } catch (e) {
    outcomes.set(provider, { ok: false, message: String(e) });
  }
  spending = "";
  confirming = "";
  draw();
}

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
