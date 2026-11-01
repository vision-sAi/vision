// Dictation: hold the mic button (or Ctrl+Shift+Space, ⌘⇧Space on a Mac, unless Settings › Shortcuts
// changed it) and talk; let go and the words land in the composer to read over before sending. A click
// starts it and the next click stops it.
//
// The page records the microphone itself (getUserMedia; lib.rs grants the web view's request) and makes a
// 16 kHz mono WAV, what the phone sends and what the server's Whisper reads; the computer's Whisper does
// the words (POST /transcribe, through mic.rs). Recording natively would need ALSA headers to build on
// Linux and the same macOS permission prompt anyway.

import { invoke } from "@tauri-apps/api/core";
import { Mic, createElement } from "lucide";
import * as shortcuts from "./settings/shortcuts";

type State = "idle" | "starting" | "recording" | "transcribing";

const RATE = 16000;            // what Whisper wants: the server resamples anything else, but this is smaller
const HOLD_MS = 350;           // pressed longer than this, letting go stops; shorter is a click that toggles
const MIN_SECONDS = 0.3;       // anything shorter is a slip, not words
const MAX_SECONDS = 300;

let host: { toast: (text: string) => void } = { toast: () => {} };
let state: State = "idle";
let heard = false;                    // a hello has come in
let installed: boolean | undefined;   // hello.transcribe (undefined: a Vision from before it said)
let voiceOn = false;
let pressedAt = 0;
let holding = false;                  // the button or the shortcut is down
let stopWanted = false;               // let go while the microphone was still opening
let cancelled = false;
let startedAt = 0;
let level = 0;
let ticker = 0;

let stream: MediaStream | null = null;
let ctx: AudioContext | null = null;
let source: MediaStreamAudioSourceNode | null = null;
let tap: ScriptProcessorNode | null = null;
let chunks: Float32Array[] = [];

const button = () => document.getElementById("mic") as HTMLButtonElement;
const isMac = () => document.documentElement.dataset.os === "macos" || /Mac/.test(navigator.platform);
const keys = () => shortcuts.label(shortcuts.dictationKeys());  // Settings › Shortcuts can change them

/** What the server says it can do: hello's `transcribe` and `voice_on`, then `voice_power` frames. */
export function server(on: boolean, canTranscribe?: boolean | null) {
  voiceOn = on;
  if (canTranscribe !== undefined) { heard = true; installed = canTranscribe ?? undefined; }
  if (!usable()) stop(true);
  draw();
}

/** Why the button can't be used right now, or "" when it can. */
function unusable(): string {
  if (!navigator.mediaDevices?.getUserMedia) return "This window can't reach a microphone.";
  if (!voiceOn) return "Dictation needs the voice on: turn it on in Settings.";
  return "";
}

const usable = () => installed !== false && !unusable();

function draw() {
  const b = button();
  if (!b) return;
  b.hidden = !heard || installed === false; // no Whisper on that computer: nothing to offer
  const why = unusable();
  // Greyed rather than disabled: a disabled button shows no tooltip in every web view, and a press says why.
  b.classList.toggle("off", !!why && state === "idle");
  b.setAttribute("aria-disabled", String(!!why && state === "idle"));
  b.classList.toggle("recording", state === "recording" || state === "starting");
  b.classList.toggle("transcribing", state === "transcribing");
  b.setAttribute("aria-pressed", String(state === "recording"));
  b.title = why || (state === "recording" ? "Let go to stop (Esc throws it away)"
    : state === "transcribing" ? "Transcribing…" : `Hold to dictate, or click to start and stop (${keys()})`);
  const label = b.querySelector(".mic-label")!;
  label.textContent = state === "recording" ? clock((Date.now() - startedAt) / 1000) : state === "transcribing" ? "Transcribing…" : "";
  (b.querySelector(".mic-level") as HTMLElement).style.setProperty("--level", level.toFixed(2));
}

const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

async function start() {
  state = "starting";
  stopWanted = cancelled = false;
  chunks = [];
  level = 0;
  draw();
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    ctx = new AudioContext();
    await ctx.resume().catch(() => {});
    source = ctx.createMediaStreamSource(stream);
    // ScriptProcessor rather than an AudioWorklet: deprecated but in every WebKit, and needs no module file.
    tap = ctx.createScriptProcessor(4096, 1, 1);
    tap.onaudioprocess = (e) => {
      const data = e.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(data));
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const db = 20 * Math.log10(Math.sqrt(sum / data.length) || 1e-8);
      level = level * 0.6 + Math.min(1, Math.max(0, (db + 55) / 45)) * 0.4;
    };
    source.connect(tap);
    tap.connect(ctx.destination); // WebKit runs a processor only while it is connected; its output stays silent
  } catch (e) {
    release();
    state = "idle";
    draw();
    const name = (e as DOMException)?.name;
    host.toast(name === "NotAllowedError" ? "Vision isn't allowed to use the microphone."
      + (isMac() ? " Allow it in System Settings › Privacy & Security › Microphone." : "")
      : name === "NotFoundError" ? "No microphone found." : `The microphone didn't open: ${e}`);
    return;
  }
  state = "recording";
  startedAt = Date.now();
  ticker = window.setInterval(() => {
    if ((Date.now() - startedAt) / 1000 >= MAX_SECONDS) stop();
    else draw();
  }, 100);
  draw();
  if (stopWanted) stop(cancelled);
}

function release() {
  clearInterval(ticker);
  tap?.disconnect();
  source?.disconnect();
  stream?.getTracks().forEach((t) => t.stop());
  ctx?.close().catch(() => {});
  tap = source = stream = ctx = null;
}

/** Stop recording; unless `discard`, send what was said and put the words in the composer. */
async function stop(discard = false) {
  if (state === "starting") { stopWanted = true; cancelled = discard; return; }
  if (state !== "recording") return;
  const rate = ctx?.sampleRate ?? 48000;
  release();
  const samples = join(chunks);
  chunks = [];
  level = 0;
  if (discard || samples.length < rate * MIN_SECONDS) {
    state = "idle";
    draw();
    if (!discard) host.toast("Hold the mic button while you talk.");
    return;
  }
  state = "transcribing";
  draw();
  try {
    const text = await invoke<string>("transcribe", wav(downsample(samples, rate), Math.min(rate, RATE)));
    if (text) insert(text); else host.toast("No words in that recording.");
  } catch (e) {
    host.toast(`Couldn't transcribe: ${e}`);
  } finally {
    state = "idle";
    draw();
  }
}

function join(parts: Float32Array[]): Float32Array {
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

/** To 16 kHz by averaging each output sample's span of input: a box filter, plenty for speech. */
function downsample(input: Float32Array, rate: number): Float32Array {
  if (rate <= RATE) return input;
  const ratio = rate / RATE;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const from = Math.floor(i * ratio), to = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = from; j < to; j++) sum += input[j];
    out[i] = sum / Math.max(1, to - from);
  }
  return out;
}

/** 16-bit PCM WAV, mono. */
function wav(samples: Float32Array, rate: number): Uint8Array {
  const buf = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (at: number, s: string) => { for (let i = 0; i < s.length; i++) buf.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF"); buf.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
  text(12, "fmt "); buf.setUint32(16, 16, true); buf.setUint16(20, 1, true); buf.setUint16(22, 1, true);
  buf.setUint32(24, rate, true); buf.setUint32(28, rate * 2, true); buf.setUint16(32, 2, true); buf.setUint16(34, 16, true);
  text(36, "data"); buf.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) buf.setInt16(44 + i * 2, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
  return new Uint8Array(buf.buffer);
}

/** The words at the caret, a space apart from what's around them, undoable like typing. */
function insert(text: string) {
  const input = document.getElementById("input") as HTMLTextAreaElement;
  input.focus();
  const before = input.value.slice(0, input.selectionStart);
  const piece = (before && !/\s$/.test(before) ? " " : "") + text;
  if (!document.execCommand("insertText", false, piece)) {
    input.setRangeText(piece, input.selectionStart, input.selectionEnd, "end");
  }
  input.dispatchEvent(new Event("input", { bubbles: true })); // the box grows, `!` turns it into a command line
}

/** Pressed: start, or (recording after a click) stop. */
function press() {
  if (state === "transcribing") return;
  if (state === "recording" || state === "starting") { holding = false; stop(); return; }
  if (!usable()) { const why = unusable(); if (why) host.toast(why); return; }
  holding = true;
  pressedAt = Date.now();
  start();
}

/** Let go: after a hold that's the end of it; after a click it records on until the next. */
function letGo() {
  if (!holding) return;
  holding = false;
  if (Date.now() - pressedAt >= HOLD_MS) stop();
}

export function setup(h: typeof host) {
  host = h;
  const b = button();
  if (!b) return;
  const svg = createElement(Mic) as unknown as SVGElement;
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.classList.add("icon");
  b.querySelector(".mic-icon")!.append(svg);
  b.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); // the caret stays in the composer
    try { b.setPointerCapture(e.pointerId); } catch { /* a synthetic press */ }
    press();
  });
  b.addEventListener("pointerup", letGo);
  b.addEventListener("pointercancel", letGo);
  // Return or Space on the focused button: no hold to go by, so it toggles.
  b.addEventListener("click", (e) => { if (e.detail === 0) { press(); holding = false; } });

  window.addEventListener("keydown", (e) => {
    if (shortcuts.capturing()) return; // Settings is taking these keys down as a new shortcut
    if (e.key === "Escape" && (state === "recording" || state === "starting")) {
      e.preventDefault(); // main.ts's Escape (stop the reply) leaves a handled one alone
      holding = false;
      stop(true);
      return;
    }
    if (shortcuts.comboOf(e) !== shortcuts.dictationKeys()) return;
    if (b.hidden || b.offsetParent === null) return; // no composer on screen
    e.preventDefault();
    if (!e.repeat) press();
  }, true);
  window.addEventListener("keyup", (e) => {
    const key = shortcuts.dictationKeys().split("+").pop();
    if (holding && (e.code === key || e.key === "Shift" || e.key === "Control" || e.key === "Alt" || e.key === "Meta")) letGo();
  }, true);
  window.addEventListener("dictation-keys", draw);  // the tooltip names the keys
  window.addEventListener("blur", () => { if (holding) letGo(); });
  draw();
}
