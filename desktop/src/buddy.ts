// A scout's avatar: a soft little capsule in the scout's colour (Pip's shape from the app icon), lit from above:
// a gentle gradient, a rim of light along its top, a shadow on the ground, and eyes with a catchlight. Enough to sit
// off the page, never glossy. A trait on top, and a mood that says what its project is doing. SVG with SMIL
// animation, no library. The server works out the mood (vision/projects.py `mood`); the short ones (waking,
// found_it, out_cold) the app plays on events. ios/VisionRemote/Shared/Buddy.swift draws the same.

export type Mood =
  | "asleep" | "waking" | "curious" | "focused" | "found_it" | "needs_you" | "still_waiting" | "proud" | "worried" | "out_cold";
export type Trait = "antenna" | "sprout" | "ears" | "curl";
export interface Scout { name?: string; hue?: number; trait?: string }

const AMBER = "#f5a524", GREEN = "#3fcf6e", SPARK = "#ffd76a";
const H = (h: number, s: number, l: number, a = 1) => `hsla(${h},${s}%,${l}%,${a})`;
const f = (n: number) => (+n).toFixed(2);
let uid = 0;

/** A step: [time 0–1, x, y] in steps of 4 units; the body eases from one to the next. */
type Step = [number, number, number];
interface MoodDef {
  label: string; dur: number; still: number; eyes: string;
  hop?: Step[]; look?: Step[]; blink?: boolean; dim?: boolean; sad?: boolean; extras?: string[];
}

export const MOODS: Record<Mood, MoodDef> = {
  asleep: { label: "Asleep", dur: 4, still: 1, eyes: "closed", extras: ["zz"], dim: true, hop: [[0, 0, 0], [.5, 0, .25]] },
  waking: { label: "Waking up", dur: 3, still: 1.2, eyes: "squeeze", hop: [[0, 0, 0], [.4, 0, -1], [.7, 0, 0]] },
  curious: { label: "Curious", dur: 3.2, still: .8, eyes: "open", blink: true, extras: ["question"] },
  focused: { label: "Focused", dur: 1.6, still: .2, eyes: "focused", blink: true, look: [[0, 0, .5]] },
  found_it: { label: "Found it", dur: 1.4, still: .3, eyes: "star", hop: [[0, 0, 0], [.2, 0, -2], [.45, 0, -1], [.6, 0, 0]], extras: ["sparks"] },
  needs_you: { label: "Needs you", dur: 1.6, still: .5, eyes: "big", blink: true,
    hop: [[0, 0, 0], [.15, 0, -1], [.3, 0, 0], [.45, 0, -1], [.6, 0, 0]], extras: ["bang"] },
  still_waiting: { label: "Still waiting", dur: 4, still: 2, eyes: "sad", blink: true, sad: true, extras: ["dots"] },
  proud: { label: "Proud", dur: 1, still: .3, eyes: "happy", hop: [[0, 0, 0], [.5, 0, -1]] },
  worried: { label: "Worried", dur: .5, still: .1, eyes: "small", sad: true, hop: [[0, -.4, 0], [.5, .4, 0]] },
  out_cold: { label: "Out cold", dur: 4, still: 1, eyes: "x", extras: ["zz"], dim: true },
};

export const TRAITS: Trait[] = ["antenna", "sprout", "ears", "curl"];
export const MOOD_WORDS: Record<Mood, string> = Object.fromEntries(Object.entries(MOODS).map(([k, m]) => [k, m.label])) as Record<Mood, string>;
const PROPS = new Set(["zz", "question", "sparks", "bang", "dots"]); // dropped when small

// ---------------------------------------------------------------- the shapes

// Paths keep to M L H V Q C Z, which Buddy.swift reads as they are. The body spans 15–85 across and 38–78 down (the phone sizes Vision's face by it); the eyes sit at 38 and 62.
const STEP = 4, EYE_Y = 57, EYE_X = 12.5, EYE_SCALE = 1.35;
export const BODY = "M30 38H70C78.28 38 85 44.72 85 53V63C85 71.28 78.28 78 70 78H30C21.72 78 15 71.28 15 63V53C15 44.72 21.72 38 30 38Z";
const RIM = "M21.5 46Q24.5 41 31 41H69Q75.5 41 78.5 46"; // the light along its top edge
const FEET = "M25 74h11v5q0 3.5-3.5 3.5h-4q-3.5 0-3.5-3.5ZM64 74h11v5q0 3.5-3.5 3.5h-4q-3.5 0-3.5-3.5Z";
const SPARKLE = (r: number) => `M0 ${-r}Q${f(r * .18)} ${f(-r * .18)} ${r} 0Q${f(r * .18)} ${f(r * .18)} 0 ${r}Q${f(-r * .18)} ${f(r * .18)} ${-r} 0Q${f(-r * .18)} ${f(-r * .18)} 0 ${-r}Z`;

/** One eye, drawn as the left one about its centre (+x is toward the nose); `fill` for solid, `line` for strokes. */
const EYES: Record<string, { d: string; line?: boolean; shine?: [number, number][] }> = {
  open: { d: "M-3.5 -1.5C-3.5 -6.17 3.5 -6.17 3.5 -1.5V1.5C3.5 6.17 -3.5 6.17 -3.5 1.5Z", shine: [[1.2, -2.2]] },
  big: { d: "M-4 -3C-4 -8.33 4 -8.33 4 -3V3C4 8.33 -4 8.33 -4 3Z", shine: [[1.4, -3.6], [-1.6, 2.6]] },
  closed: { d: "M-4 0.5Q0 3.5 4 0.5", line: true },
  focused: { d: "M-3.5 -0.5H3.5V1.5C3.5 6.17 -3.5 6.17 -3.5 1.5Z", shine: [[1.4, .6]] },
  sad: { d: "M-3.5 -1L3.5 -3.6V1C3.5 5.67 -3.5 5.67 -3.5 1Z", shine: [[1.2, -1.6]] },
  small: { d: "M-2 0C-2 -2.67 2 -2.67 2 0C2 2.67 -2 2.67 -2 0Z" },
  squeeze: { d: "M-3 -3.5L2.5 0L-3 3.5", line: true },
  happy: { d: "M-4 2Q0 -4 4 2", line: true },
  star: { d: SPARKLE(5) },
  x: { d: "M-3 -3L3 3M3 -3L-3 3", line: true },
};

function eyePair(name: string, ink: string): string {
  const e = EYES[name] ?? EYES.open;
  const one = e.line
    ? `<path d="${e.d}" fill="none" stroke="${ink}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`
    : `<path d="${e.d}" fill="${ink}"/>` + (e.shine ?? []).map(([x, y], i) =>
      `<circle cx="${x}" cy="${y}" r="${i ? .7 : 1.15}" fill="#fff" opacity="${i ? .45 : .85}"/>`).join("");
  return `<g transform="translate(${50 - EYE_X} ${EYE_Y}) scale(${EYE_SCALE})">${one}</g><g transform="translate(${50 + EYE_X} ${EYE_Y}) scale(${-EYE_SCALE} ${EYE_SCALE})">${one}</g>`;
}

function trait(type: string, mood: Mood, c: Colours, id: string): string {
  const m = MOODS[mood], sleepy = !!m.dim, droop = m.sad || sleepy;
  switch (type) {
    case "sprout": {
      const leaves = droop
        ? "M50 31Q42 27 37 33Q44 36 50 31ZM50 31Q58 27 63 33Q56 36 50 31Z"
        : "M50 31Q43 21 36 25Q41 32 50 31ZM50 30Q57 18 66 21Q61 30 50 30Z";
      return `<defs><linearGradient id="${id}l" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${sleepy ? "#4f9a62" : "#7ee394"}"/><stop offset="1" stop-color="${sleepy ? "#2f6e40" : "#3fae5a"}"/></linearGradient></defs>
        <path d="M50 39V30" stroke="#3a8d4e" stroke-width="2.2" stroke-linecap="round"/><path d="${leaves}" fill="url(#${id}l)"/>`;
    }
    case "ears": {
      const [l, r] = droop
        ? ["M19 44Q19 35 26 33Q31 37 33 40Z", "M81 44Q81 35 74 33Q69 37 67 40Z"]
        : ["M19 44Q17 30 22 26Q29 32 33 40Z", "M81 44Q83 30 78 26Q71 32 67 40Z"];
      const inner = droop ? "" : `<path d="M22 40Q21 33 23 30Q27 34 29 39Z" fill="${c.inner}"/><path d="M78 40Q79 33 77 30Q73 34 71 39Z" fill="${c.inner}"/>`;
      return `<path d="${l}${r}" fill="${c.top}"/>${inner}`;
    }
    case "curl":
      return `<path d="M49 39C45 31 52 25 57 28C61 31 57 36 53 33" fill="none" stroke="${c.top}" stroke-width="3.2" stroke-linecap="round"/>`;
    default: { // antenna
      const bulb = mood === "needs_you" ? AMBER : mood === "proud" || mood === "found_it" ? GREEN : droop ? c.low : c.glow;
      const flash = mood === "needs_you" ? `<animate attributeName="opacity" values=".35;1" keyTimes="0;.5" dur=".8s" calcMode="discrete" repeatCount="indefinite"/>` : "";
      return `<defs><radialGradient id="${id}b" cx=".35" cy=".3" r=".8"><stop offset="0" stop-color="#fff" stop-opacity=".9"/><stop offset=".35" stop-color="${bulb}"/><stop offset="1" stop-color="${bulb}"/></radialGradient></defs>
        <path d="M67.5 39V27" stroke="${c.low}" stroke-width="2.2" stroke-linecap="round"/><circle cx="67.5" cy="24.5" r="3.6" fill="url(#${id}b)">${flash}</circle>`;
    }
  }
}

function extras(list: string[], h: number): string {
  const steps = (attr: string, vals: string[], dur: number, begin = 0) =>
    `<animate attributeName="${attr}" values="${vals.join(";")}" keyTimes="${vals.map((_, i) => f(i / vals.length)).join(";")}" dur="${dur}s" begin="${begin}s" calcMode="discrete" repeatCount="indefinite"/>`;
  const line = (d: string, colour: string, w = 2) => `<path d="${d}" fill="none" stroke="${colour}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
  let s = "";
  for (const e of list) {
    if (e === "zz") s += [0, 1.5].map((d) => `<g opacity="0">
        <animate attributeName="opacity" values="0;1;1;0" keyTimes="0;.2;.7;1" dur="3s" begin="${d}s" repeatCount="indefinite"/>
        <animateTransform attributeName="transform" type="translate" values="0 0;9 -10" dur="3s" begin="${d}s" repeatCount="indefinite"/>
        ${line("M77 26h6l-6 7h6", H(h, 40, 78), 1.8)}</g>`).join("");
    if (e === "question") s += line("M82 19.5C82 15 89 15 89 19.5C89 21.5 87.5 22.3 86.5 22.8C85.5 23.3 85.5 24 85.5 25", H(h, 60, 85), 2.2) + `<circle cx="85.5" cy="30" r="1.3" fill="${H(h, 60, 85)}"/>`;
    if (e === "bang") s += `<g>${steps("opacity", ["1", "1", "1", ".35"], .8)}${line("M87 17v8", AMBER, 3)}<circle cx="87" cy="30.5" r="1.7" fill="${AMBER}"/></g>`;
    if (e === "dots") s += [0, 1, 2].map((i) => `<circle cx="${79 + i * 6}" cy="28" r="1.7" fill="${H(h, 20, 72)}" opacity=".3">${steps("opacity", [".3", ".3", ".3", ".3"].map((v, j) => (j === i ? "1" : v)), 2.4)}</circle>`).join("");
    if (e === "sparks") s += ([[84, 22, 0], [13, 30, .35], [89, 44, .7]] as const).map(([x, y, d]) =>
      `<g opacity="0">${steps("opacity", ["0", "1", "0"], 1.05, d)}<path transform="translate(${x} ${y})" d="${SPARKLE(4)}" fill="${SPARK}"/></g>`).join("");
  }
  return s;
}

interface Colours { top: string; bottom: string; low: string; glow: string; inner: string; ink: string }

/** Steps as an eased SMIL translation, back to the first at the end of the cycle. */
function move(steps: Step[] | undefined, dur: number): string {
  if (!steps) return "";
  const all = steps[steps.length - 1][0] < 1 ? [...steps, [1, steps[0][1], steps[0][2]] as Step] : steps;
  return `<animateTransform attributeName="transform" type="translate" values="${all.map(([, x, y]) => `${f(x * STEP)} ${f(y * STEP)}`).join(";")}" keyTimes="${all.map(([t]) => t).join(";")}" calcMode="spline" keySplines="${all.slice(1).map(() => ".45 0 .55 1").join(";")}" dur="${dur}s" repeatCount="indefinite"/>`;
}

/** The buddy as SVG markup. `props`: the badges and bubbles around it (left out when small). */
export function buddySvg(hue: number, traitType: string, mood: Mood, props = true): string {
  const m = MOODS[mood] ?? MOODS.asleep, h = hue, id = `bd${++uid}`;
  const c: Colours = m.dim
    ? { top: H(h, 24, 52), bottom: H(h, 26, 40), low: H(h, 24, 36), glow: H(h, 30, 70), inner: H(h, 30, 62), ink: H(h, 35, 12) }
    : { top: H(h, 58, 70), bottom: H(h, 54, 55), low: H(h, 48, 44), glow: H(h, 70, 88), inner: H(h, 70, 82), ink: H(h, 35, 12) };
  const blink = (on: boolean) => `<animate attributeName="opacity" values="${on ? "1;0;1" : "0;1;0"}" keyTimes="0;.94;.97" dur="3.7s" calcMode="discrete" repeatCount="indefinite"/>`;
  const eyes = m.blink
    ? `<g>${blink(true)}${eyePair(m.eyes, c.ink)}</g><g opacity="0">${blink(false)}${eyePair("closed", c.ink)}</g>`
    : eyePair(m.eyes, c.ink);
  const list = (m.extras ?? []).filter((e) => props || !PROPS.has(e));
  return `<svg viewBox="0 0 100 100" aria-hidden="true">
  <defs>
    <linearGradient id="${id}" x1="0" y1="38" x2="0" y2="78" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="${c.top}"/><stop offset="1" stop-color="${c.bottom}"/></linearGradient>
    <radialGradient id="${id}s"><stop offset="0" stop-color="#000" stop-opacity=".3"/><stop offset="1" stop-color="#000" stop-opacity="0"/></radialGradient>
  </defs>
  <ellipse cx="50" cy="83" rx="31" ry="4" fill="url(#${id}s)"/>
  <g>${move(m.hop, m.dur)}
    ${trait(traitType, mood, c, id)}
    <path d="${FEET}" fill="${c.low}"/>
    <path d="${BODY}" fill="url(#${id})"/>
    <path d="${RIM}" fill="none" stroke="#fff" stroke-opacity="${m.dim ? .14 : .32}" stroke-width="1.6" stroke-linecap="round"/>
    <path d="M24 74.5Q50 79 76 74.5" fill="none" stroke="${c.low}" stroke-opacity=".55" stroke-width="1.6" stroke-linecap="round"/>
    <g>${move(m.look, m.dur)}<g class="gaze">${eyes}</g></g>
  </g>${extras(list, h)}</svg>`;
}

// ---------------------------------------------------------------- on the page

const reduced = matchMedia("(prefers-reduced-motion: reduce)");
const SMALL = 48; // below this, no props

export interface BuddyOptions {
  /** Hold one pose instead of moving (the sidebar's quiet rows; always so under reduced motion). */
  still?: boolean;
  /** Clicking squishes it and it giggles. */
  pokeable?: boolean;
}

/** A scout's buddy as an element `size` pixels square. */
export function buddy(scout: Scout | undefined, mood: Mood | string, size: number, opts: BuddyOptions = {}): HTMLElement {
  const box = document.createElement("span");
  box.className = "buddy";
  box.style.width = box.style.height = `${size}px`;
  const m = (mood in MOODS ? mood : "asleep") as Mood;
  const hue = Number.isFinite(scout?.hue) ? scout!.hue! : 205;
  const tr = scout?.trait ?? "antenna";
  box.dataset.mood = m;
  const paint = (as: Mood) => {
    box.innerHTML = buddySvg(hue, tr, as, size >= SMALL);
    const svg = box.firstElementChild as SVGSVGElement;
    if (opts.still || reduced.matches || document.hidden) {
      hold(svg, MOODS[as].still);
      setTimeout(() => hold(svg, MOODS[as].still), 0); // an SVG's clock starts when it's on the page
    }
  };
  paint(m);
  if (opts.pokeable && !reduced.matches) {
    box.classList.add("pokeable");
    box.addEventListener("click", (e) => {
      e.stopPropagation();
      if (box.dataset.poked) return;
      box.dataset.poked = "1";
      paint("proud");
      box.classList.add("poke");
      setTimeout(() => { box.classList.remove("poke"); paint(m); delete box.dataset.poked; }, 1400);
    });
  }
  if (opts.still) box.dataset.still = "1";
  return box;
}

function hold(svg: SVGSVGElement, at: number) {
  try { svg.pauseAnimations(); svg.setCurrentTime(at); } catch { /* not attached yet: the next draw holds it */ }
}

// The eyes follow the pointer when it's near; animation stops while the window is hidden.
let gazeQueued = false;
let pointer = { x: -1e4, y: -1e4 };
document.addEventListener("mousemove", (e) => {
  pointer = { x: e.clientX, y: e.clientY };
  if (gazeQueued || reduced.matches) return;
  gazeQueued = true;
  setTimeout(() => {
    gazeQueued = false;
    for (const b of document.querySelectorAll<HTMLElement>(".buddy")) {
      const r = b.getBoundingClientRect();
      if (!r.width) continue;
      const dx = pointer.x - (r.left + r.width / 2), dy = pointer.y - (r.top + r.height / 2);
      const len = Math.hypot(dx, dy) || 1;
      const k = len > 360 ? 0 : len > 40 ? 2 : 0; // a glance toward the pointer
      for (const g of b.querySelectorAll(".gaze")) g.setAttribute("transform", `translate(${f(dx / len * k)} ${f(dy / len * k)})`);
    }
  }, 50);
});
document.addEventListener("visibilitychange", () => {
  for (const svg of document.querySelectorAll<SVGSVGElement>(".buddy:not([data-still]) > svg")) {
    if (document.hidden || reduced.matches) svg.pauseAnimations(); else svg.unpauseAnimations();
  }
});
