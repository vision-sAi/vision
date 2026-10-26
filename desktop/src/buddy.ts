// A scout's avatar: a small squishy creature in the scout's colour, with a trait of its own and a mood
// that says what its project is doing. Drawn as SVG with SMIL animation, no library. The server works out the mood
// (vision/projects.py `mood`); the short ones (waking, found_it, out_cold) the app plays on events.

export type Mood =
  | "asleep" | "waking" | "curious" | "focused" | "found_it" | "needs_you" | "still_waiting" | "proud" | "worried" | "out_cold";
export type Trait = "antenna" | "sprout" | "ears" | "curl";
export interface Scout { name?: string; hue?: number; trait?: string }

const AMBER = "#f5a524", GREEN = "#3fcf6e", INK = "#16161d", PINK = "#ff7a9a";
const H = (h: number, s: number, l: number, a = 1) => `hsla(${h},${s}%,${l}%,${a})`;
const f = (n: number) => (+n).toFixed(2);
let uid = 0;

/** A keyframe: [time 0–1, scale x, scale y, lift, gaze x, gaze y, tilt°]. */
type Key = [number, number, number, number, number, number, number];
interface MoodDef {
  label: string; dur: number; still: number; k: Key[];
  eyes: string; mouth: string; brows?: [[number, number], [number, number]];
  arms: [number, number] | { l: number[]; r: number[] }; wave?: boolean; blush?: boolean; dim?: boolean; droop?: boolean;
  extras?: string[];
}

export const MOODS: Record<Mood, MoodDef> = {
  asleep: { label: "Asleep", dur: 5, still: 2.6, k: [[0, 1, 1, 0, 0, 0, 0], [.5, 1.05, .94, 0, 0, 0, 0], [1, 1, 1, 0, 0, 0, 0]],
    eyes: "closed", mouth: "tiny", arms: [25, -25], extras: ["zz", "bubble"], dim: true },
  waking: { label: "Waking up", dur: 3.2, still: 1.3,
    k: [[0, 1, 1, 0, 0, 0, 0], [.3, .9, 1.12, 1, 0, -1, 0], [.6, .9, 1.12, 1, 0, -1, 0], [.78, 1.07, .93, 0, 0, 0, 0], [1, 1, 1, 0, 0, 0, 0]],
    eyes: "squeeze", mouth: "yawn", arms: { l: [25, 150, 150, 25, 25], r: [-25, -150, -150, -25, -25] } },
  curious: { label: "Curious", dur: 3.6, still: .9,
    k: [[0, 1, 1, 0, -4, -1, -7], [.42, 1, 1, 0, -4, -1, -7], [.5, 1, 1, 0, 4, -1, 7], [.92, 1, 1, 0, 4, -1, 7], [1, 1, 1, 0, -4, -1, -7]],
    eyes: "open", brows: [[0, 0], [-5, -3]], mouth: "o", arms: [25, -25], extras: ["question"] },
  focused: { label: "Focused", dur: .8, still: .2, k: [[0, 1, 1, 0, 0, 3, 0], [.5, 1.03, .97, 0, 0, 3, 0], [1, 1, 1, 0, 0, 3, 0]],
    eyes: "narrow", brows: [[-1.5, 2.5], [2.5, -1.5]], mouth: "tongue", arms: { l: [40, 60, 40], r: [-60, -40, -60] }, extras: ["sparks"] },
  found_it: { label: "Found it", dur: 1.5, still: .5,
    k: [[0, 1, 1, 0, 0, 0, 0], [.15, 1.12, .85, 0, 0, 1, 0], [.35, .9, 1.12, 14, 0, -2, 0], [.55, 1, 1, 6, 0, 0, 0], [.7, 1.1, .88, 0, 0, 1, 0], [.82, 1, 1, 0, 0, 0, 0], [1, 1, 1, 0, 0, 0, 0]],
    eyes: "star", mouth: "open", blush: true, arms: { l: [25, 30, 150, 140, 40, 25, 25], r: [-25, -30, -150, -140, -40, -25, -25] }, extras: ["stars"] },
  needs_you: { label: "Needs you", dur: 1.8, still: .6,
    k: [[0, 1, 1, 0, 0, 0, 0], [.14, 1.12, .86, 0, 0, 1, 0], [.32, .92, 1.1, 10, 0, -1, 0], [.48, .96, 1.05, 3, 0, 0, 0], [.58, 1.1, .9, 0, 0, 1, 0], [.72, 1, 1, 0, 0, 0, 0], [1, 1, 1, 0, 0, 0, 0]],
    eyes: "big", brows: [[-3.5, -2.5], [-2.5, -3.5]], mouth: "smile", arms: [25, -25], wave: true, extras: ["bang", "ring"] },
  still_waiting: { label: "Still waiting", dur: 4.6, still: 2.3, k: [[0, 1, 1, 0, 0, 3, 0], [.5, 1.07, .91, 0, 0, 4, -3], [1, 1, 1, 0, 0, 3, 0]],
    eyes: "glossy", brows: [[1, -3.5], [-3.5, 1]], mouth: "frown", arms: [12, -12], extras: ["dots"], droop: true },
  proud: { label: "Proud", dur: 1.4, still: .35,
    k: [[0, 1, 1, 0, 0, 0, -6], [.25, 1.06, .94, 0, 0, 0, 0], [.5, 1, 1, 3, 0, 0, 6], [.75, 1.06, .94, 0, 0, 0, 0], [1, 1, 1, 0, 0, 0, -6]],
    eyes: "happy", mouth: "open", blush: true, arms: { l: [140, 165, 140, 165, 140], r: [-165, -140, -165, -140, -165] }, extras: ["confetti"] },
  worried: { label: "Worried", dur: .35, still: .1, k: [[0, 1, 1, 0, -1, 0, -2], [.5, 1, 1, 0, 1, 0, 2], [1, 1, 1, 0, -1, 0, -2]],
    eyes: "small", brows: [[1.5, -3], [-3, 1.5]], mouth: "wobbly", arms: [55, -55], extras: ["sweat"], droop: true },
  out_cold: { label: "Out cold", dur: 5, still: 2.6, k: [[0, 1, 1, 0, 0, 0, 0], [.5, 1.05, .94, 0, 0, 0, 0], [1, 1, 1, 0, 0, 0, 0]],
    eyes: "closed", mouth: "o-small", arms: [25, -25], extras: ["zz", "nightcap", "moon"], dim: true },
};

export const TRAITS: Trait[] = ["antenna", "sprout", "ears", "curl"];
export const MOOD_WORDS: Record<Mood, string> = Object.fromEntries(Object.entries(MOODS).map(([k, m]) => [k, m.label])) as Record<Mood, string>;
const PROPS = new Set(["zz", "bubble", "question", "sparks", "stars", "bang", "ring", "dots", "confetti", "moon"]); // dropped when small

// ---------------------------------------------------------------- shape

function smooth(pts: number[][]): string {
  const n = pts.length;
  let d = `M${f(pts[0][0])} ${f(pts[0][1])}`;
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n], p1 = pts[i], p2 = pts[(i + 1) % n], p3 = pts[(i + 2) % n];
    d += `C${f(p1[0] + (p2[0] - p0[0]) / 6)} ${f(p1[1] + (p2[1] - p0[1]) / 6)} ${f(p2[0] - (p3[0] - p1[0]) / 6)} ${f(p2[1] - (p3[1] - p1[1]) / 6)} ${f(p2[0])} ${f(p2[1])}`;
  }
  return d + "Z";
}

const R = 31, BOT = 82;
const cyOf = (sy: number, lift: number) => BOT - lift - .85 * R * sy;
const C0 = cyOf(1, 0), TOP0 = C0 - R, EY = C0 - 3, MY = EY + 10.5, BY = EY - 10.5;
const EX = [39, 61];

function bodyPath([, sx, sy, lift]: Key): string {
  const c = cyOf(sy, lift), pts: number[][] = [];
  for (let i = 0; i < 12; i++) {
    const a = i / 12 * 2 * Math.PI - Math.PI / 2, s = Math.sin(a);
    pts.push([50 + R * sx * Math.cos(a), c + R * sy * s * (s > 0 ? .85 : 1)]);
  }
  return smooth(pts);
}

// ---------------------------------------------------------------- face

function eyes(type: string): string {
  const blink = (ry: number) => `<animate attributeName="ry" values="${ry};${ry};.6;${ry}" keyTimes="0;.93;.965;1" dur="3.7s" repeatCount="indefinite"/>`;
  const shine = (x: number, y: number, r: number) => `<circle cx="${x}" cy="${y}" r="${r}" fill="#fff"><animate attributeName="opacity" values="1;1;0;1" keyTimes="0;.93;.965;1" dur="3.7s" repeatCount="indefinite"/></circle>`;
  const stroke = `fill="none" stroke="${INK}" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"`;
  return EX.map((x, i) => {
    switch (type) {
      case "closed": return `<path d="M${x - 5} ${EY} q5 4.5 10 0" ${stroke}/>`;
      case "happy": return `<path d="M${x - 5} ${EY + 2} l5 -5.5 l5 5.5" ${stroke}/>`;
      case "squeeze": return i ? `<path d="M${x + 4} ${EY - 4} l-7 4 l7 4" ${stroke}/>` : `<path d="M${x - 4} ${EY - 4} l7 4 l-7 4" ${stroke}/>`;
      case "star": return `<g transform="translate(${x} ${EY})"><path d="M0 -8Q0 0 7 0Q0 0 0 8Q0 0 -7 0Q0 0 0 -8Z" fill="${INK}" stroke="${INK}" stroke-width="2" stroke-linejoin="round">
        <animateTransform attributeName="transform" type="scale" values="1;1.3;1" dur=".6s" repeatCount="indefinite"/></path><circle r="1.6" fill="#fff"/></g>`;
      case "narrow": return `<ellipse cx="${x}" cy="${EY + 1}" rx="5" ry="4.6" fill="${INK}">${blink(4.6)}</ellipse>${shine(x + 1.6, EY - .6, 1.3)}`;
      case "small": return `<ellipse cx="${x}" cy="${EY}" rx="3.8" ry="5.6" fill="${INK}"/><circle cx="${x + 1.2}" cy="${EY - 2}" r="1.2" fill="#fff"/>`;
      case "big": return `<ellipse cx="${x}" cy="${EY}" rx="6.5" ry="9" fill="${INK}">${blink(9)}</ellipse>${shine(x + 2, EY - 3.2, 2.4)}${shine(x - 2.2, EY + 3.4, 1.1)}`;
      case "glossy": return `<ellipse cx="${x}" cy="${EY + 1}" rx="6.8" ry="8.6" fill="${INK}"/><circle cx="${x + 2}" cy="${EY - 2.4}" r="2.8" fill="#fff"/>
        <circle cx="${x - 2.4}" cy="${EY + 3}" r="1.3" fill="#fff"/><path d="M${x - 5} ${EY + 6} q5 3.5 10 0" fill="none" stroke="#9fd8ff" stroke-width="1.6" stroke-linecap="round">
        <animate attributeName="opacity" values=".4;1;.4" dur="1.8s" repeatCount="indefinite"/></path>`;
      default: return `<ellipse cx="${x}" cy="${EY}" rx="5" ry="7.5" fill="${INK}">${blink(7.5)}</ellipse>${shine(x + 1.8, EY - 2.8, 1.7)}`;
    }
  }).join("");
}

function mouth(type: string, m: MoodDef, anim: (attr: string, vals: string) => string): string {
  const stroke = `fill="none" stroke="${INK}" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"`;
  switch (type) {
    case "tiny": return `<path d="M48 ${MY} q2 1.8 4 0" ${stroke}/>`;
    case "smile": return `<path d="M45.5 ${MY} q4.5 4 9 0" ${stroke}/>`;
    case "o": return `<ellipse cx="50" cy="${MY + 1}" rx="2.4" ry="3" fill="${INK}"/>`;
    case "o-small": return `<ellipse cx="50" cy="${MY + 1}" rx="1.6" ry="1.9" fill="${INK}"/>`;
    case "frown": return `<path d="M46.5 ${MY + 2.5} q3.5 -3.2 7 0" ${stroke}/>`;
    case "wobbly": return `<path d="M44 ${MY + 1} q1.5 -2 3 0 q1.5 2 3 0 q1.5 -2 3 0 q1.5 2 3 0" ${stroke} stroke-width="2"/>`;
    case "tongue": return `<ellipse cx="54" cy="${MY + 2.6}" rx="2.2" ry="3" fill="${PINK}"/><path d="M45 ${MY} q5 3 9.5 -.5" ${stroke}/>`;
    case "open": return `<path d="M44 ${MY - 1} q6 9 12 0Z" fill="${INK}" stroke="${INK}" stroke-width="1.5" stroke-linejoin="round"/><ellipse cx="50" cy="${MY + 3.4}" rx="3" ry="1.6" fill="${PINK}"/>`;
    case "yawn": {
      const wide = (k: Key) => k[0] === .3 || k[0] === .6;
      return `<ellipse cx="50" cy="${MY + 2}" rx="2.2" ry="1.6" fill="${INK}">${anim("ry", m.k.map((k) => (wide(k) ? 6.5 : 1.6)).join(";"))}${anim("rx", m.k.map((k) => (wide(k) ? 4.2 : 2.2)).join(";"))}</ellipse>`;
    }
  }
  return "";
}

function brows(b?: [[number, number], [number, number]]): string {
  if (!b) return "";
  return EX.map((x, i) => `<path d="M${x - 5} ${BY + b[i][0]} L${x + 5} ${BY + b[i][1]}" fill="none" stroke="${INK}" stroke-width="2.4" stroke-linecap="round"/>`).join("");
}

function trait(type: string, h: number, mood: Mood, id: string): { front: string; back: string } {
  const m = MOODS[mood], T = TOP0;
  const sad = !!m.droop, sleepy = mood === "asleep" || mood === "out_cold";
  const swing = (from: number, to: number, dur: number) =>
    `<animateTransform attributeName="transform" type="rotate" values="${from} 50 ${T + 1};${to} 50 ${T + 1};${from} 50 ${T + 1}" dur="${dur}s" repeatCount="indefinite" calcMode="spline" keySplines=".4 0 .6 1;.4 0 .6 1"/>`;
  switch (type) {
    case "sprout": {
      const leaf = sleepy ? "#4a9a5a" : "#5fd27a";
      return { back: "", front: `<g>${sad ? swing(38, 42, 3) : swing(-7, 7, 2.2)}<path d="M50 ${T + 2} Q48 ${T - 4} 51 ${T - 9}" fill="none" stroke="#3f9a52" stroke-width="2.2" stroke-linecap="round"/>
        <ellipse cx="45" cy="${T - 10}" rx="6" ry="3.2" fill="${leaf}" transform="rotate(-28 45 ${T - 10})"/>
        <ellipse cx="56.5" cy="${T - 12}" rx="6.5" ry="3.4" fill="${leaf}" transform="rotate(32 56.5 ${T - 12})"/></g>` };
    }
    case "ears": {
      const ear = (side: number) => {
        const x = 50 + side * 17, flat = sad || sleepy ? side * 38 : 0;
        const mirror = side < 0 ? "" : `scale(-1 1) translate(${-2 * x} 0)`;
        const twitch = side > 0 && !sad && !sleepy
          ? `<animateTransform attributeName="transform" type="rotate" values="0 ${x} ${T + 8};0 ${x} ${T + 8};16 ${x} ${T + 8};0 ${x} ${T + 8}" keyTimes="0;.82;.88;1" dur="3s" repeatCount="indefinite"/>` : "";
        return `<g transform="rotate(${flat} ${x} ${T + 8})"><g>${twitch}<path d="M${x - 9} ${T + 10} Q${x - 4} ${T - 14} ${x + 9} ${T + 6} Z" fill="url(#${id}f)" transform="${mirror}"/>
          <path d="M${x - 5} ${T + 7} Q${x - 2} ${T - 7} ${x + 5} ${T + 4} Z" fill="${PINK}" opacity=".55" transform="${mirror}"/></g></g>`;
      };
      return { back: ear(-1) + ear(1), front: "" };
    }
    case "curl":
      return { back: "", front: `<path d="M48 ${T + 2} C44 ${T - 7} 57 ${T - 11} 57 ${T - 4} C57 ${T} 51 ${T} 52 ${T - 4}" fill="none" stroke="${H(h, 70, sad ? 34 : 38)}" stroke-width="2.6" stroke-linecap="round"/>` };
    default: { // antenna
      const bulb = mood === "needs_you" ? AMBER : mood === "proud" || mood === "found_it" ? GREEN : sad || sleepy ? H(h, 20, 58) : H(h + 30, 95, 78);
      return { back: "", front: `<g>${sad ? swing(24, 28, 3) : swing(-9, 9, mood === "focused" ? .5 : 1.4)}<path d="M50 ${T + 2} Q49 ${T - 5} 50 ${T - 10}" fill="none" stroke="${H(h, 50, 35)}" stroke-width="2.2" stroke-linecap="round"/>
        <circle cx="50" cy="${T - 13}" r="7" fill="${bulb}" opacity="${sleepy || sad ? 0 : .3}"/><circle cx="50" cy="${T - 13}" r="3.6" fill="${bulb}"/></g>` };
    }
  }
}

function extras(list: string[], h: number, id: string): string {
  let s = "";
  for (const e of list) {
    if (e === "zz") s += [0, 1.6].map((d) => `<text x="76" y="24" font-size="13" font-weight="700" font-family="system-ui,sans-serif" fill="${H(h, 60, 82)}" opacity="0">z
        <animate attributeName="opacity" values="0;.9;0" dur="3.2s" begin="${d}s" repeatCount="indefinite"/>
        <animateTransform attributeName="transform" type="translate" values="0 0;8 -13" dur="3.2s" begin="${d}s" repeatCount="indefinite"/></text>`).join("");
    if (e === "question") s += `<text x="77" y="25" font-size="17" font-weight="800" font-family="system-ui,sans-serif" fill="${H(h, 70, 82)}">?<animateTransform attributeName="transform" type="translate" values="0 0;0 -3;0 0" dur="1.8s" repeatCount="indefinite"/></text>`;
    if (e === "sparks") s += ([[80, 26, 0], [86, 40, .25], [72, 16, .5]] as const).map(([x, y, d]) => `<path d="M${x} ${y - 4}Q${x} ${y} ${x + 4} ${y}Q${x} ${y} ${x} ${y + 4}Q${x} ${y} ${x - 4} ${y}Q${x} ${y} ${x} ${y - 4}Z" fill="${H(h, 90, 82)}" opacity="0">
        <animate attributeName="opacity" values="0;1;0" dur=".75s" begin="${d}s" repeatCount="indefinite"/></path>`).join("");
    if (e === "stars") s += ([[16, 30, 0], [84, 24, .2], [12, 60, .45], [88, 58, .65], [50, 4, .3]] as const).map(([x, y, d]) => `<g transform="translate(${x} ${y})"><path d="M0 -6Q0 0 6 0Q0 0 0 6Q0 0 -6 0Q0 0 0 -6Z" fill="#ffd76a">
        <animateTransform attributeName="transform" type="scale" values="0;1.2;0" dur=".9s" begin="${d}s" repeatCount="indefinite"/></path></g>`).join("");
    if (e === "bang") s += `<g transform="translate(81 21)"><g><animateTransform attributeName="transform" type="scale" values="1;1.15;1" dur=".9s" repeatCount="indefinite"/>
        <circle r="8.5" fill="${AMBER}"/><path d="M0 -4.5V1" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/><circle cy="4.3" r="1.5" fill="#fff"/></g></g>`;
    if (e === "ring") s = `<circle cx="50" cy="${C0}" r="33" fill="none" stroke="${AMBER}" stroke-width="2.5">
        <animate attributeName="r" values="33;48" dur="1.8s" repeatCount="indefinite"/><animate attributeName="opacity" values=".8;0" dur="1.8s" repeatCount="indefinite"/></circle>` + s;
    if (e === "dots") s += `<g transform="translate(79 22)"><rect x="-11" y="-6.5" width="22" height="13" rx="6.5" fill="${H(h, 15, 60, .25)}"/>
        ${[-5, 0, 5].map((x, i) => `<circle cx="${x}" r="1.7" fill="${AMBER}" opacity=".3"><animate attributeName="opacity" values=".3;1;.3;.3" keyTimes="0;.2;.4;1" dur="2.4s" begin="${i * .4}s" repeatCount="indefinite"/></circle>`).join("")}</g>`;
    if (e === "confetti") {
      const cols = [AMBER, GREEN, PINK, "#3daee9", "#fff", "#b28dff"];
      for (let i = 0; i < 9; i++) {
        const x = 8 + i * 10.5, d = (i * .37) % 1.6;
        s += `<g transform="translate(${x} 0)"><rect x="-1.6" y="-2.6" width="3.2" height="5.2" rx=".8" fill="${cols[i % cols.length]}" opacity="0">
          <animateTransform attributeName="transform" type="translate" values="0 -2;${i % 2 ? 4 : -4} 92" dur="1.6s" begin="${d}s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="1;1;0" keyTimes="0;.7;1" dur="1.6s" begin="${d}s" repeatCount="indefinite"/></rect></g>`;
      }
    }
    if (e === "nightcap") s += `<path d="M27 ${TOP0 + 13} Q30 ${TOP0 - 10} 58 ${TOP0 - 9} Q78 ${TOP0 - 8} 86 ${TOP0 + 17} Q75 ${TOP0 + 3} 72 ${TOP0 + 9} Z" fill="${H(h + 200, 45, 42)}"/>
        <path d="M26 ${TOP0 + 14} Q50 ${TOP0 - 1} 74 ${TOP0 + 10}" fill="none" stroke="#f1eee6" stroke-width="5" stroke-linecap="round"/>
        <circle cx="86.5" cy="${TOP0 + 19}" r="4.2" fill="#f1eee6"/>`;
    if (e === "moon") s += `<defs><mask id="${id}m"><rect x="0" y="0" width="100" height="100" fill="#fff"/><circle cx="19" cy="15" r="6" fill="#000"/></mask></defs>
        <circle cx="15" cy="18" r="7" fill="#ffe08a" mask="url(#${id}m)"><animate attributeName="opacity" values=".6;1;.6" dur="4s" repeatCount="indefinite"/></circle>`;
  }
  return s;
}

/** The buddy as SVG markup. `props`: the bubbles, badges and confetti around it (left out when small). */
export function buddySvg(hue: number, traitType: string, mood: Mood, props = true): string {
  const m = MOODS[mood] ?? MOODS.asleep, id = `bd${uid++}`, h = hue;
  const times = m.k.map((k) => k[0]).join(";"), splines = Array(m.k.length - 1).fill(".42 0 .58 1").join(";");
  const anim = (attr: string, vals: string, type = "") =>
    `<${type ? "animateTransform" : "animate"} attributeName="${attr}" ${type ? `type="${type}"` : ""} values="${vals}" keyTimes="${times}" dur="${m.dur}s" repeatCount="indefinite" calcMode="spline" keySplines="${splines}"/>`;
  const center = m.k.map(([, , sy, lift]) => `0 ${f(cyOf(sy, lift) - C0)}`).join(";");
  const face = m.k.map(([, , sy, lift, gx, gy]) => `${gx} ${f(cyOf(sy, lift) - C0 + gy)}`).join(";");
  const top = m.k.map(([, , sy, lift]) => `0 ${f(cyOf(sy, lift) - R * sy - TOP0)}`).join(";");
  const tilt = m.k.map((k) => `${k[6]} 50 ${BOT}`).join(";");
  const shadow = m.k.map(([, sx, , lift]) => f(Math.max(10, 23 * sx - lift * 1.1))).join(";");
  const arm = (side: number, vals: number | number[], wave = false) => {
    const rot = wave ? `<animateTransform attributeName="transform" type="rotate" values="-150;-105;-150" dur=".55s" repeatCount="indefinite" calcMode="spline" keySplines=".4 0 .6 1;.4 0 .6 1"/>`
      : Array.isArray(vals) ? anim("transform", vals.join(";"), "rotate") : "";
    const fixed = typeof vals === "number" ? ` transform="rotate(${vals})"` : "";
    return `<g transform="translate(${50 + side * 25} ${C0 + 5})"><g${fixed}>${rot}<ellipse cx="0" cy="10.5" rx="5" ry="9" fill="${H(h, 75, 50)}"/></g></g>`;
  };
  const arms = Array.isArray(m.arms) ? m.arms : [m.arms.l, m.arms.r] as const;
  const t = trait(traitType, h, mood, id);
  const list = (m.extras ?? []).filter((e) => props || !PROPS.has(e));
  const cap = list.includes("nightcap") ? extras(["nightcap"], h, id) : "";
  const ex = extras(list.filter((e) => e !== "nightcap" && e !== "bubble" && e !== "sweat"), h, id);
  return `<svg viewBox="0 0 100 100" aria-hidden="true"><defs>
    <radialGradient id="${id}f" cx="38%" cy="30%" r="80%"><stop offset="0" stop-color="${H(h, 95, 80)}"/><stop offset=".45" stop-color="${H(h, 85, 61)}"/><stop offset="1" stop-color="${H(h + 8, 70, 42)}"/></radialGradient>
  </defs>
  <ellipse cx="50" cy="88" rx="23" ry="3.2" fill="#000" opacity=".3">${anim("rx", shadow)}</ellipse>
  <g${m.dim ? ' opacity=".88"' : ""}>${anim("transform", tilt, "rotate")}
    <g>${anim("transform", center, "translate")}${arm(-1, arms[0])}${arm(1, arms[1], !!m.wave)}</g>
    <g>${anim("transform", top, "translate")}${t.back}</g>
    <path d="${bodyPath(m.k[0])}" fill="url(#${id}f)">${anim("d", m.k.map(bodyPath).join(";"))}</path>
    <g>${anim("transform", top, "translate")}${t.front}${cap}</g>
    <g>${anim("transform", face, "translate")}
      <ellipse cx="34" cy="${C0 - 17}" rx="7" ry="4.2" fill="#fff" opacity=".42" transform="rotate(-25 34 ${C0 - 17})"/>
      ${m.blush ? EX.map((x) => `<ellipse cx="${x + (x < 50 ? -9 : 9)}" cy="${EY + 8}" rx="5" ry="3" fill="${PINK}" opacity=".5"/>`).join("") : ""}
      ${brows(m.brows)}
      <g class="gaze">${eyes(m.eyes)}</g>
      ${mouth(m.mouth, m, anim)}
      ${list.includes("bubble") ? `<circle cx="57" cy="${MY + 1}" r="0" fill="#fff" fill-opacity=".25" stroke="#fff" stroke-opacity=".7" stroke-width=".8">
        <animate attributeName="r" values="0;5.5;6;0" keyTimes="0;.75;.8;.81" dur="5s" repeatCount="indefinite"/></circle>` : ""}
      ${list.includes("sweat") ? `<path d="M74 ${EY - 10}c3 4 3 7 0 7s-3 -3 0 -7z" fill="#9fd8ff"><animateTransform attributeName="transform" type="translate" values="0 0;1 9" dur="1.4s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="1;1;0" keyTimes="0;.6;1" dur="1.4s" repeatCount="indefinite"/></path>` : ""}
    </g>
  </g>${ex}</svg>`;
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
      const k = len > 360 ? 0 : Math.min(1, len / 120) * 3.2;
      for (const g of b.querySelectorAll(".gaze")) g.setAttribute("transform", `translate(${f(dx / len * k)} ${f(dy / len * k)})`);
    }
  }, 50);
});
document.addEventListener("visibilitychange", () => {
  for (const svg of document.querySelectorAll<SVGSVGElement>(".buddy:not([data-still]) > svg")) {
    if (document.hidden || reduced.matches) svg.pauseAnimations(); else svg.unpauseAnimations();
  }
});
