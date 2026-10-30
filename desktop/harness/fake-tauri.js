// Stands in for Tauri's native side so the built page runs in an ordinary browser (shots.mjs drives it).
// The scene (scenes.js, picked by ?scene=) says what the "server" holds and what happens after connecting:
// frames arriving, clicks, typing, and checks whose results go into #harness-results.

(() => {
  const params = new URLSearchParams(location.search);
  const scene = window.SCENES[params.get("scene") || "chat"];
  const theme = params.get("theme") || "dark";
  const os = params.get("os") || "linux";
  // Headless Chrome under a virtual time budget can hold animation frames back for good; the page draws on them.
  window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 16);
  const callbacks = new Map();
  const listeners = new Map(); // event name → [callback id]
  let nextId = 1;
  let serving = !scene.unpaired;
  let paired = false; // a first-run scene's Vision starts once its `service` step has run
  let installed = !!scene.installed;
  let runId = 1;
  window.__sent = [];
  window.__notified = [];
  window.Notification = function (title, options) { window.__notified.push({ title, body: options?.body }); };
  window.Notification.permission = "granted";

  function emit(event, payload) {
    for (const id of listeners.get(event) ?? []) callbacks.get(id)?.({ event, id, payload });
  }
  window.__emit = emit;  // a scene's steps can play native events (a drop from the file manager)

  window.__http = [];
  function http({ method, path, query, body }) {
    window.__http.push({ method, path, query, body });
    const routes = scene.http ?? {};
    const key = `${method} ${path}`;
    if (key in routes) return typeof routes[key] === "function" ? routes[key](query, body) : routes[key];
    if (path === "history") return (scene.history ?? {})[query?.chat] ?? [];
    if (path === "models") return { ...MODELS, current: query?.model || "opus", effort: query?.effort || "high" };
    if (path === "folders") return FOLDERS;
    if (path === "speak/pieces") return { pieces: [] };
    if (path === "defaults") return { ...MODELS, current: body?.model ?? "opus", effort: body?.effort ?? "high" };
    if (path === "voice") return { voice: "vision", on: true, voices: [{ id: "vision", note: "the default" }, { id: "calm", note: "designed" }] };
    if (path === "voice/rate") return { rate: 1.1 };
    if (path === "voice/power") return { on: !!body?.on };
    throw new Error(`fake server has no ${key}`);
  }

  async function invoke(cmd, args = {}) {
    switch (cmd) {
      case "plugin:event|listen":
        if (!listeners.has(args.event)) listeners.set(args.event, []);
        listeners.get(args.event).push(args.handler);
        return args.handler;
      case "plugin:event|unlisten": return null;
      case "appearance": return { os, scheme: theme, accent: scene.accent ?? "#3daee9" };
      case "saved": return scene.unpaired && !paired ? null : { url: "http://127.0.0.1:8765", name: "" };
      case "resume":
        if (scene.unpaired) return false;
        setTimeout(() => { connectNow(); runSteps(); }, 30);
        return true;
      case "local_vision": return serving ? { url: "http://127.0.0.1:8765", token: "t" } : null;
      case "vision_here": return { installed, serving, os };
      case "provider_status": return scene.providers ?? [];
      case "setup_run": {
        const run = runId++;
        const lines = { install: ["◆ Installing Vision", "  ✓ uv 0.12.23", "  ✓ Vision is in ~/.local/share/vision/app", "  Setting up its Python environment…", "  ✓ vision command in ~/.local/bin"],
          service: ["Installed vision.service (systemd user unit)", "Started."], upgrade: ["◆ Installing Vision", "restarting once 1 chat finishes replying"] }[args.action] ?? ["Opening your browser to sign in…"];
        lines.forEach((line, i) => setTimeout(() => emit("setup", { run, line }), 40 * (i + 1)));
        setTimeout(() => {
          if (args.action === "install") installed = true;
          if (args.action === "service") serving = true;
          if (args.action === "provider_login") scene.providers.find((p) => p.name === args.name).signed_in = true;
          emit("setup", { run, done: true, code: scene.failAt === args.action ? 1 : 0 });
        }, 40 * (lines.length + 2));
        return run;
      }
      case "pair": paired = true; setTimeout(connectNow, 30); return null;
      case "forget": return null;
      case "send": window.__sent.push(args.frame); scene.onSend?.(args.frame, emitFrame); return null;
      case "http": return http(args);
      case "upload": return "/home/me/.local/share/vision/uploads/20261024-120000-abc123-file.png";
      case "fetch_bytes": return PNG;
      case "upload_path": return { name: args.path.split("/").pop(), path: `/home/me/.local/share/vision/uploads/x-${args.path.split("/").pop()}` };
      case "paste_files": return scene.clipboard ?? [];
      case "speak": return new ArrayBuffer(0);
      case "plugin:opener|open_url": return null;
      case "plugin:app|version": return "0.1.0";
      case "install_kind": return scene.installKind ?? "app";
      case "plugin:updater|check": return scene.appUpdate ? { rid: 1, currentVersion: "0.1.0", version: scene.appUpdate, date: "", body: "", rawJson: {} } : null;
      case "set_tray_status": window.__tray = args.text; return null;
      case "set_keep_in_tray": return true;
      case "plugin:notification|is_permission_granted": return true;
      default: throw new Error(`fake tauri has no command ${cmd}`);
    }
  }

  const emitFrame = (frame) => emit("frame", frame);

  /** The socket comes up and the server says hello. */
  function connectNow() {
    emit("connection", { state: "online", detail: "" });
    emitFrame({ type: "hello", name: "studio", read_aloud: true, shell: true, voice_on: !!scene.voiceOn, version: "0.9.3", asleep: scene.asleep ?? [], update: scene.update ?? null, protocol: scene.protocol ?? 1, chats: scene.chats ?? [], projects: scene.projects ?? [] });
  }

  /** Run the scene's steps in order (once: after connecting, or from the start on a first run). */
  let started = false;
  async function runSteps() {
    if (started) return;
    started = true;
    const results = document.createElement("pre");
    results.id = "harness-results";
    results.hidden = true;
    document.body.append(results);
    for (const step of scene.steps ?? []) {
      await new Promise((r) => setTimeout(r, step.wait ?? 60));
      try {
        if (step.frame) emitFrame(step.frame);
        if (step.click) {
          const target = document.querySelector(step.click);
          if (target) target.click(); else results.append(`FAIL no ${step.click} to click\n`);
        }
        if (step.type) {
          const input = document.querySelector(step.type[0]);
          input.value = step.type[1];
          input.dispatchEvent(new Event("input", { bubbles: true }));
        }
        if (step.key) document.querySelector(step.key[0]).dispatchEvent(new KeyboardEvent("keydown", { key: step.key[1], bubbles: true }));
        if (step.run) step.run();
        if (step.check) {
          // Drawing waits for an animation frame, which headless Chrome can hold back: give it a moment.
          let ok = step.fn();
          for (let i = 0; !ok && i < 20; i++) { await new Promise((r) => setTimeout(r, 100)); ok = step.fn(); }
          results.append(`${ok ? "PASS" : "FAIL"} ${step.check}\n`);
        }
      } catch (e) {
        results.append(`FAIL ${step.check ?? JSON.stringify(step).slice(0, 80)}: ${e}\n`);
      }
    }
    results.append("DONE\n");
  }

  window.__TAURI_INTERNALS__ = {
    invoke,
    transformCallback(cb, once) {
      const id = nextId++;
      callbacks.set(id, (v) => { if (once) callbacks.delete(id); cb(v); });
      return id;
    },
    unregisterCallback(id) { callbacks.delete(id); },
    convertFileSrc: (p) => p,
  };

  if (scene.unpaired) addEventListener("DOMContentLoaded", () => setTimeout(runSteps, 200));

  // macOS previews: the window buttons the real title bar would draw over the sidebar.
  if (os === "macos") {
    addEventListener("DOMContentLoaded", () => {
      const lights = document.createElement("div");
      lights.style.cssText = "position:fixed;top:18px;left:18px;display:flex;gap:8px;z-index:99";
      for (const c of ["#ff5f57", "#febc2e", "#28c840"]) {
        const d = document.createElement("span");
        d.style.cssText = `width:12px;height:12px;border-radius:50%;background:${c}`;
        lights.append(d);
      }
      document.body.append(lights);
    });
  }

  const MODELS = {
    tabs: [{ tab: "Claude", models: [{ id: "opus", label: "Opus 5.5", description: "Most capable" }, { id: "sonnet", label: "Sonnet 5.5", description: "Fast and capable" }] },
           { tab: "Codex", models: [{ id: "gpt-5.5-codex", label: "GPT-5.5 Codex", description: "OpenAI's coding model" },
             { id: "gpt-5.5", label: "GPT-5.5", description: "" }, { id: "gpt-5.5-mini", label: "GPT-5.5 mini", description: "" }] },
           { tab: "Grok", models: [{ id: "grok-5", label: "Grok 5", description: "" }, { id: "grok-5-fast", label: "Grok 5 Fast", description: "" }] },
           { tab: "Local", models: [{ id: "local/qwen3", label: "Qwen3 32B", description: "" }, { id: "local/llama", label: "Llama 4 Scout", description: "" },
             { id: "local/gemma", label: "Gemma 4 27B", description: "" }] }],
    efforts: ["low", "medium", "high", "max"].map((e) => ({ id: e, label: e[0].toUpperCase() + e.slice(1), description: "" })),
  };
  const FOLDERS = {
    path: "/home/me", label: "~", parent: "/home", home: "/home/me",
    folders: [{ name: "Documents", path: "/home/me/Documents" }, { name: "Downloads", path: "/home/me/Downloads" }],
    recent: [{ path: "/home/me/code/vision", label: "~/code/vision" }, { path: "/home/me", label: "~" }],
  };
  // A small picture for any image a reply or message shows.
  const PNG = Uint8Array.from(atob(
    "iVBORw0KGgoAAAANSUhEUgAAAUAAAAC0EAYAAAC1dClSAAAAIGNIUk0AAHomAACAhAAA+gAAAIDoAAB1MAAA6mAAADqYAAAXcJy6UTwAAAAGYktHRP///////wlY99wAAAAHdElNRQfqChgXBBDEpJUAAAAAJXRFWHRkYXRlOmNyZWF0ZQAyMDI2LTEwLTI0VDIzOjA0OjE2KzAwOjAw4TLmyQAAACV0RVh0ZGF0ZTptb2RpZnkAMjAyNi0xMC0yNFQyMzowNDoxNiswMDowMJBvXnUAAAAodEVYdGRhdGU6dGltZXN0YW1wADIwMjYtMTAtMjRUMjM6MDQ6MTYrMDA6MDDHen+qAAAjDElEQVR42u3dZ2AUVfv38e8km95DKiQU6UVABAG5AWmKIMWGDRG4RREUAbFSLEhWxYagKNgrioCogEoVEFF6BwFpAum9Z5N5XuxiFkFI8qh4/+f3eZOdw8yZ2dndmcM11znH6Nhx/vykJNNERERERCzBhp0EllzowxARERGRf4rNsJtqAIqIiIhYiNFp+7wrkoL0CFhERETEKmyG3UwwFQEUERERsQzlAIqIiIhYjHIARURERCzGuCJ97s0nE5UDKCIiImIVigCKiIiIWIxyAEVEREQsRhFAEREREYuxGXY0DIyIiIiIhdiwmwksvtCHISIiIiL/FJuRwBQ9AhYRERGxDqP723OM4xoGRkRERMQy1AlERERExGKcw8AoB1BERETEMhQBFBEREbEYGxoGRkRERMRSFAEUERERsRhNBSciIiJiMTbDbiaY6gQiIiIiYhmKAIqIiIhYjHIARURERCzGuPq3DzoeC9RMICIiIiJWoRxAEREREYtRDqCIiIiIxSgHUERERMRijGt478YjJ5UDKCIiImIVNhQBFBEREbEUm2EnAXUCEREREbEMRQBFRERELMZm2Ekw1QAUERERsQxFAEVEREQsRjmAIiIiIhZjw27qEbCIiIiIhTgjgGoAioiIiFiGpoITERERsRhNBSciIiJiMTYSmGKqE4iIiIiIZSgCKCIiImIxygEUERERsRiboWFgRERERCzFGQFUDqCIiIiIZSgHUERERMRijBvy37j8oL9pXugDEREREZF/hnIARURERCzGORWccgBFRERELMOGcgBFRERELMVm2NEjYBERERELUQRQRERExGKcOYBqAIqIiIhY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"), (c) => c.charCodeAt(0)).buffer;
})();
