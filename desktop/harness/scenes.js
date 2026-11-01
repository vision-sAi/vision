// What the fake server holds for each screenshot (fake-tauri.js plays it). Each scene: chats, history,
// projects, http overrides, and steps run after connecting: {frame}, {click: selector}, {type: [sel, text]},
// {key: [sel, key]}, {run: fn}, {check: "what", fn: () => bool}; `wait` is the pause before a step (ms).

(() => {
  const now = Date.now() / 1000;
  const D = 86400;
  const chat = (id, title, ago, more = {}) => ({
    type: "chat", chat: id, source: "server", title, model: "Opus 5.5", model_id: "opus", effort: "high",
    folder: "~/code/vision", busy: false, waiting: false, questions: [], queued: [], updated: now - ago, mode: "auto", ...more,
  });
  const CHATS = [
    chat("c1", "Fix the flaky upload test", 600, { suggestion: "Run the whole suite to be sure", context: { tokens: 84_000, window: 200_000 } }),
    chat("c2", "Weekend plans in Austin", 3000, { busy: true, model: "Sonnet 5.5", folder: "~" }),
    chat("c3", "Frigate camera dropouts", D + 100, { source: "terminal", folder: "~" }),
    chat("c4", "Draft the TestFlight notes", 3 * D, { waiting: true }),
    chat("c5", "Compare NAS drives", 12 * D, { folder: "~", pinned: true }),
    chat("c6", "Homelab backup plan", 50 * D, { folder: "~" }),
    chat("sc1", "Vision desktop app", 120, { project: "p1" }),
  ];
  // Every project is a scout (vision/projects.py): a name, colour and trait, and the mood the server works out.
  const PROJECTS = [
    { id: "p1", title: "Vision desktop app", status: "active", folder: "~/code/vision", chat: "sc1", threads: { waiting_on_you: 1, ready_for_review: 1, working: 1 },
      goal: "Keep the Vision desktop app moving: triage new issues, fix what's small, and draft PRs for the rest.",
      scout: { name: "Atlas", hue: 205, trait: "antenna" }, mood: "needs_you", leash: "checkins", settings: { autonomy: "balanced", leash: "checkins" }, tokens_out: 48210,
      rules: [{ action: "land", label: "Land a branch into the project folder", rule: "asks", set: false },
        { action: "publish", label: "Push or open a pull request", rule: "asks", set: false },
        { action: "send", label: "Send anything outside the computer (email, messages, comments)", rule: "never", set: true },
        { action: "destructive", label: "Delete files outside its folder, or install software", rule: "asks", set: false }],
      approvals: [{ id: "ap1", target: "t-review", title: "Add notifications for finished replies", tool: "Bash", command: "git push -u origin vision/t-review",
        rule: "Bash(git push:*)", action: "publish", state: "pending", created: now - 900 }],
      checkin: { schedule: { id: "k1", repeat: "daily", time: "08:00", enabled: true, next_run: now + 13 * 3600 }, last: { started: now - 3 * 3600, ended: now - 3 * 3600 + 2340, needs_you: 2 }, running: null } },
    { id: "p2", title: "Homelab cleanup", status: "active", folder: "~", threads: { working: 1, idle: 1 },
      goal: "Keep the homelab tidy: old images, full disks, stale backups.",
      scout: { name: "Juniper", hue: 32, trait: "sprout" }, mood: "focused", leash: "on_call", settings: { autonomy: "ask" } },
    { id: "p3", title: "Inbox and calendar", status: "active", folder: "~", threads: {},
      goal: "Sort the inbox every morning and flag what needs an answer.",
      scout: { name: "Wren", hue: 268, trait: "ears" }, mood: "asleep", leash: "checkins", settings: { autonomy: "balanced" },
      checkin: { schedule: { id: "k3", repeat: "weekdays", time: "07:00", enabled: true, next_run: (() => { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(7, 0, 0, 0); return d.getTime() / 1000; })() }, last: null, running: null } },
    { id: "p4", title: "Frigate cameras", status: "active", folder: "~", threads: {},
      goal: "Keep every Frigate camera recording and say when one drops out.",
      scout: { name: "Moss", hue: 140, trait: "curl" }, mood: "proud", leash: "autonomous", settings: { autonomy: "free" } },
  ];

  const t1 = "I'll look at the test and the cleanup code first.";
  const t2 = "\n\nFound it. `drop_uploads` runs on a timer, and when the test is slow the file is gone before the assertion reads it. I made it skip anything younger than a minute:";
  const t3 = "\n\nThe test passed 50 runs in a row after that.\n\n```python\nfor path in UPLOAD_DIR.iterdir():\n    if time.time() - path.stat().st_mtime < 60:\n        continue  # still being read\n    path.unlink(missing_ok=True)\n```\n\nWant me to commit it?";
  const L1 = [...t1].length, L2 = [...(t1 + t2)].length;
  const TOOLS = [
    { id: "t1", name: "Read", detail: "tests/test_upload.py", done: true, at: L1, output: "import unittest\n…" },
    { id: "t2", name: "Grep", detail: "drop_uploads", done: true, at: L1, output: "vision/server.py:412: def drop_uploads(paths):" },
    { id: "t3", name: "Edit", detail: "vision/server.py", done: true, at: L2, diff: [{ old: 410, new: 410, lines: [
      " def drop_uploads(paths):", "     for path in paths:", "+        if time.time() - path.stat().st_mtime < 60:",
      "+            continue  # still being read", "-        # remove it", "         path.unlink(missing_ok=True)"] }] },
    { id: "t4", name: "Bash", detail: "for i in $(seq 50); do pytest -q tests/test_upload.py; done", done: true, at: L2,
      input: [["description", "Run the test 50 times"], ["command", "for i in $(seq 50); do pytest -q tests/test_upload.py; done"]],
      output: "..........\n50 passed in 41.2s" },
  ];
  const AGENTS = [{ id: "a1", kind: "Explore", label: "Find every caller of drop_uploads", done: true, tools: 6, model: "Haiku 5.5", at: L1,
    prompt: "Find every caller of drop_uploads and say when each runs.",
    steps: [{ tool: "Grep", detail: "drop_uploads" }, { tool: "Read", detail: "vision/server.py" }],
    summary: "Two callers: the `unqueue` handler and the hourly sweep." }];
  const HISTORY = { c1: [
    { role: "user", text: "why does test_upload fail about one run in five?" },
    { role: "assistant", text: t1 + t2 + t3, duration_ms: 48400, tools: TOOLS, agents: AGENTS },
  ], sc1: [
    { role: "checkin", label: "Morning check-in", started: now - 3 * 3600, ended: now - 3 * 3600 + 2340, needs_you: 2, duration_ms: 2340000,
      text: "Three new requests came in. One small bug is fixed; two proposals are ready for review:\n\n- The toolbar icon now follows the system theme\n- Image attachments keep their original filenames\n- A request for another platform needs a decision" },
    { role: "note", text: "3 threads stopped: “Choose a tray icon style” needs you: Monochrome tray icon that follows the panel's colours, or the full-colour app icon? · “Add notifications for finished replies” is ready for review: Notifications fire when a reply finishes or Vision asks while the window isn't focused. · “Read the new requests” finished: a small bug is fixed, two proposals are ready for review, and a platform request needs a decision." },
    { role: "assistant", text: "Two need you on the right: the icon choice and the update workflow are ready for review. The release draft still needs a decision.", duration_ms: 9100 },
    { role: "user", text: "nice. leave the release draft for me to review" },
    { role: "assistant", text: "Got it, I’ll leave the release draft alone. The icon choice and update workflow are still waiting for you on the right.", duration_ms: 4200 },
  ] };
  const base = { chats: CHATS, projects: PROJECTS, history: HISTORY };

  const thread = (id, title, state, ago, more = {}) => ({
    id, title, state, queued: false, objective: `Objective for ${title.toLowerCase()}.`, model: "Opus 5.5", effort: "high",
    folder: "~/code/vision", branch: `vision/${id}`, updated: now - ago, activity: "", tools: 0, last_result: null, question: null, error: null, ...more,
  });
  const THREADS = {
    p1: [
      thread("t-ask", "Choose a tray icon style", "waiting_on_you", 400, { question: "Monochrome tray icon that follows the panel's colours, or the full-colour app icon?" }),
      thread("t-review", "Add notifications for finished replies", "ready_for_review", 1800, { tools: 23, last_result: { status: "completed",
        summary: "Notifications now fire when a reply finishes or Vision asks a question while the window isn't focused.",
        changes: ["desktop/src/main.ts: notify on done and question", "desktop/src-tauri/Cargo.toml: notification plugin"], findings: [], decisions: ["Only notify when the window isn't focused"] } }),
      thread("t-work", "Write the Agents board tests", "working", 60, { tools: 9, activity: "Running npm run shots…" }),
    ],
    p2: [
      thread("t-work2", "Clean old Docker images on the NAS", "working", 200, { folder: "~", branch: null, tools: 4, activity: "Listing images older than 90 days" }),
      thread("t-idle", "Check the backup schedule", "idle", 7200, { folder: "~", branch: null, last_result: { status: "completed", summary: "Backups run nightly at 03:00; the last one finished in 41 min." } }),
    ],
  };
  const ACTIVITY = [
    { at: now - 400, kind: "needs_you", text: "“Choose a tray icon style” needs you: Monochrome or full colour?", thread: "t-ask" },
    { at: now - 1800, kind: "ready", text: "“Add notifications for finished replies” is ready for review: Notifications now fire…", thread: "t-review" },
    { at: now - 2400, kind: "started", text: "Started “Write the Agents board tests”", thread: "t-work" },
    { at: now - 3000, kind: "finished", text: "“Review new requests” finished: a small bug is fixed and two proposals are ready for review." },
    { at: now - 3600, kind: "started", text: "Started “Review new requests”" },
  ];
  const FACTS = ["- 2026-10-22: The user prefers to review changes before they land (from you)",
    "- 2026-10-23: The user prefers concise commit subjects (from you)"];
  const AGENT_HTTP = {
    "GET projects/p1/threads": THREADS.p1,
    "GET projects/p2/threads": THREADS.p2,
    "GET scheduled": [
      { id: "s1", title: "Morning brief", prompt: "Summarise my calendar and the weather", kind: "task", repeat: "weekdays", time: "08:30", enabled: true, next_run: now + 13 * 3600 },
      { id: "s2", title: "Call the dentist", prompt: "Call the dentist", kind: "reminder", repeat: "once", time: "18:00", enabled: true, next_run: now + 3600 },
      { id: "s3", title: "Check the build", prompt: "Check the nightly build", kind: "task", repeat: "daily", time: "07:00", enabled: false, next_run: null },
    ],
    "GET projects/p1/threads/t-review/history": { messages: [
      { role: "user", text: "Add desktop notifications: when a reply finishes or Vision asks a question and the window isn't focused." },
      { role: "assistant", text: "Done. I used the Tauri notification plugin and only fire when the window is unfocused." },
    ] },
    "GET projects/p1/threads/t-review/diff": (q) => q?.path ? { patch: "@@ -10,3 +10,6 @@\n import { listen }\n+import { sendNotification } from \"@tauri-apps/plugin-notification\";\n+\n-// old\n const x = 1;" }
      : { base: "master", commits: 2, files: [{ path: "desktop/src/main.ts", added: 48, removed: 3 }, { path: "desktop/src-tauri/Cargo.toml", added: 1, removed: 0 }], more: 0 },
  };
  // Settings › Permissions: the computer's rule lists, changed as the server would (a rule in two lists is refused).
  const PERMISSIONS_HTTP = () => {
    const ACTS = [["land", "Land a branch into the project folder", "asks", "asks"], ["publish", "Push or open a pull request", "asks", "does"],
      ["send", "Send anything outside the computer (email, messages, comments)", "asks", "asks"],
      ["destructive", "Delete files outside its folder, or install software", "asks", "asks"]];
    const state = {
      allow: ["Write(docs/**)"], ask: ["Bash(git push:*)", "Edit(.env)"], deny: ["Bash(sudo:*)", "Bash(rm -rf ~:*)", "Bash(claude:*)", "WebFetch(domain:example.com)"],
      builtin: ["Bash(sudo:*)", "Bash(rm -rf ~:*)", "Bash(claude:*)"], scoutRules: {},
      claude: { path: "~/.claude/settings.json", allow: ["Bash(npm test:*)"], ask: [], deny: ["Read(.env.production)"] },
    };
    const view = () => ({ allow: state.allow, ask: state.ask, deny: state.deny, builtin: state.builtin, claude: state.claude,
      scouts: ACTS.map(([action, label, checkins, autonomous]) => ({ action, label, rule: state.scoutRules[action] ?? "", leash: { checkins, autonomous } })) });
    return {
      "GET permissions": view,
      "POST permissions": (_q, body) => {
        const next = { allow: body.allow ?? state.allow, ask: body.ask ?? state.ask, deny: body.deny ?? state.deny };
        const all = [...next.allow, ...next.ask, ...next.deny];
        const bad = all.find((r) => !/^[A-Za-z0-9_*?]+(\(.*\))?$/.test(r));
        if (bad) throw `\`${bad}\` isn't a rule: write a tool, or a tool and what it's used on, e.g. Bash(git push:*)`;
        Object.assign(state, next);
        for (const [a, v] of Object.entries(body.scouts ?? {})) { if (v === "default") delete state.scoutRules[a]; else state.scoutRules[a] = v; }
        return view();
      },
    };
  };
  let facts = [...FACTS];
  const SCOUT_HTTP = {
    ...AGENT_HTTP,
    "GET projects/p3/threads": [], "GET projects/p4/threads": [],
    "GET projects/p1/activity": { events: ACTIVITY },
    "GET projects/p1/memory": () => ({ facts }),
    "POST projects/p1/memory": (q, b) => { facts = [...facts, `- 2026-10-24: ${b.fact} (from you)`]; return { added: [], facts }; },
    "DELETE projects/p1/memory/1": () => { facts = facts.slice(1); return { forgot: "", facts }; },
    "POST projects/p1/threads/t-review/land": { ...THREADS.p1[1], state: "resolved", landed: now },
    "POST projects/p1/checkin": { chat: "sc1" },
    "POST projects/p1/approvals/ap1": (q, b) => ({ ...PROJECTS[0].approvals[0], state: b.decision === "approve" ? "approved" : "denied", ...(b.command ? { edited: b.command } : {}) }),
    "POST projects/p1/checkins": (q, b) => ({ ...PROJECTS[0], checkin: { ...PROJECTS[0].checkin, schedule: { id: "k1", enabled: true, next_run: now + 7200, ...b } } }),
    "POST projects/p9/checkins": (q, b) => ({ checkin: { schedule: { id: "k9", enabled: true, ...b }, last: null, running: null } }),
    "POST projects": (q, b) => ({ id: "p9", title: b.goal, goal: b.goal, status: "active", folder: "~/code/vision", threads: {}, chat: "",
      scout: { name: b.scout?.name ?? "Pip", hue: b.scout?.hue ?? 205, trait: b.scout?.trait ?? "antenna" }, mood: "asleep", leash: "checkins" }),
  };
  SCOUT_HTTP["POST scouts/draft"] = (q, b) => /^Every weekday at 7, /.test(b.text)
    ? { goal: "Give me the three tech stories worth reading", reports: { repeat: "weekdays", time: "07:00" }, words: "Reports weekdays at 7:00", scout: { name: "Fern", hue: 175, trait: "curl" } }
    : /every morning/.test(b.text)
    ? { goal: b.text.replace(/,? check every morning/, ""), reports: { repeat: "daily", time: "08:00" }, words: "Reports every morning at 8:00", scout: { name: "Pip", hue: 340, trait: "ears" } }
    : { goal: b.text, reports: { repeat: "daily", time: "08:00" }, words: "Reports every morning at 8:00", scout: { name: "Pip", hue: 340, trait: "ears" } };
  SCOUT_HTTP["POST scouts"] = (q, b) => ({ ...SCOUT_HTTP["POST projects"](q, { goal: SCOUT_HTTP["POST scouts/draft"](q, b).goal, scout: { name: "Pip", hue: 340, trait: "ears" } }),
    checkin: { schedule: { id: "k9", repeat: "daily", time: "08:00", enabled: true, next_run: now + 3600 }, last: null, running: null } });
  let needsDone = new Set();
  const NEEDS = [
    { project: "p1", name: "Atlas", hue: 205, trait: "antenna", kind: "approval", id: "ap1", title: "Add notifications for finished replies asks to run", text: "git push -u origin vision/t-review", tool: "Bash", at: now - 900 },
    { project: "p2", name: "Juniper", hue: 32, trait: "sprout", kind: "question", id: "t-disk", title: "Clear old Docker images", text: "There are 41 GB of images older than 90 days. Remove all of them, or keep the ones tagged latest?", at: now - 1500 },
    { project: "p1", name: "Atlas", hue: 205, trait: "antenna", kind: "review", id: "t-review", title: "Add notifications for finished replies", text: "Notifications fire for finished replies and questions when the window isn't focused. Tests pass.", can_apply: true, at: now - 2400 },
  ];
  SCOUT_HTTP["GET needs"] = () => NEEDS.filter((n) => !needsDone.has(n.id));
  SCOUT_HTTP["POST projects/p2/threads/t-disk/message"] = (q, b) => { needsDone.add("t-disk"); return { id: "t-disk", project: "p2", title: "Clear old Docker images", state: "working", told: b.text }; };
  for (const pid of ["p2", "p3", "p4"]) { SCOUT_HTTP[`GET projects/${pid}/activity`] = { events: [] }; SCOUT_HTTP[`GET projects/${pid}/memory`] = { facts: [] }; }
  const openAtlas = { wait: 300, run: () => [...document.querySelectorAll(".chat-item.scout")].find((x) => x.textContent.includes("Atlas")).click() };
  const sentOf = (type) => window.__sent.filter((f) => f.type === type);
  const PROVIDERS = () => [
    { name: "claude", label: "Claude", product: "Claude Code", note: "via Claude Code · Claude Subscription", enabled: true, installed: true, signed_in: true, detail: "logged in", install_cmd: "curl …", install_url: "", login_cmd: ["claude", "auth", "login"] },
    { name: "codex", label: "Codex", product: "Codex", note: "via Codex CLI · ChatGPT Subscription", enabled: true, installed: true, signed_in: false, detail: "", install_cmd: "npm i -g @openai/codex", install_url: "", login_cmd: ["codex", "login"] },
    { name: "grok", label: "Grok", product: "Grok", note: "via Grok CLI · Grok Subscription", enabled: true, installed: false, signed_in: false, detail: "", install_cmd: "", install_url: "https://x.ai/cli", login_cmd: ["grok", "login"] },
  ];

  // GET /usage as vision/usage.py's usage_data shapes each provider.
  const win = (label, pct, inSecs) => ({ label, used_percent: pct, resets_at: now + inSecs, resets: null });
  const CLAUDE_USAGE = { provider: "claude", label: "Claude", plan: "Max", source: "live", at: null, error: null, notes: [],
    windows: [win("Current session", 34, 2.5 * 3600 + 30), win("Current week", 78, 3 * D + 4 * 3600), win("Fable", 93, 3 * D + 4 * 3600)],
      banked: { count: 2, expires_at: now + 20 * D, label: null, items: [{ id: "g1", kind: "Full Reset", expires_at: now + 2 * D }, { id: "g2", kind: "Session Reset", expires_at: now + 20 * D }] } };
  const USAGE_HTTP = {
    "GET providers": { providers: [{ name: "claude", tint: "#d97857" }, { name: "codex", tint: "#0f997a" }, { name: "grok", tint: "#5c6680" }] },
    "GET usage": () => ({ at: now, providers: [
      { provider: "grok", label: "Grok", plan: null, windows: [], banked: null, notes: [], source: null, at: null,
        error: "Grok usage unavailable: needs a live grok.com login (run `grok login`)." },
      CLAUDE_USAGE,
      { provider: "codex", label: "Codex", plan: "Pro", source: "snapshot", at: now - 1500, error: null, banked: null,
        windows: [win("Current session", 8, 4 * 3600), win("Current week", 41, 5 * D)], notes: ["from the last reply's rate-limit snapshot"] },
    ] }),
    "POST usage/reset": (q, b) => ({ ok: true, outcome: "reset", message: "Session cleared: your Claude session and weekly limits are back to zero.",
      provider: { ...CLAUDE_USAGE, windows: [win("Current session", 0, 5 * 3600), win("Current week", 0, 7 * D), win("Fable", 93, 3 * D)],
        banked: { count: 1, expires_at: now + 20 * D, items: [{ id: "g2", kind: "Session Reset", expires_at: now + 20 * D }] } } }),
  };
  // GET /scheduled as vision/scheduled.py keeps tasks; the scene's steps change it and send a `scheduled` frame.
  const SCHED_HTTP = () => {
    const list = window.__sched = [
      { id: "s1", title: "Morning brief", prompt: "Summarise my calendar and the weather", kind: "task", chat: "c1", repeat: "weekdays", time: "08:30", enabled: true, next_run: now + 13 * 3600 },
      { id: "s2", title: "Call the dentist", prompt: "Call the dentist", kind: "reminder", chat: "", repeat: "once", date: "2026-11-02", time: "18:00", enabled: true, next_run: new Date(2026, 10, 2, 18).getTime() / 1000 },
      { id: "s3", title: "Check the build", prompt: "Check the nightly build", kind: "task", chat: "", repeat: "daily", time: "07:00", enabled: false, next_run: null },
      { id: "s4", title: "Stretch", prompt: "Stretch", kind: "reminder", chat: "", repeat: "every", every_minutes: 120, time: "", enabled: true, next_run: now + 1800 },
      { id: "k1", title: "Atlas's status report", prompt: "", kind: "checkin", project: "p1", repeat: "daily", time: "08:00", enabled: true, next_run: now + 13 * 3600 },
    ];
    return {
      "GET scheduled": () => list.map((t) => ({ ...t })),
      "GET projects/p1/threads": [], "GET projects/p2/threads": [], "GET projects/p3/threads": [], "GET projects/p4/threads": [],
      "POST scheduled": (q, b) => {
        const old = list.find((t) => t.id === b.id);
        const task = { title: b.prompt, chat: "", ...old, ...b, id: b.id ?? `n${list.length}`, next_run: b.enabled === false ? null : now + 2 * D };
        if (old) list[list.indexOf(old)] = task; else list.push(task);
        return task;
      },
      "DELETE scheduled/s9": () => { list.splice(list.findIndex((t) => t.id === "s9"), 1); return { ok: true }; },
      "POST scheduled/s2/run": {},
    };
  };
  // Vision's own memory (vision/memory.py) behind GET/POST /memory…: the core under "", topics by name.
  // One copy for the memory scenes: only one scene runs per page. (Pairs, not keys: shots.mjs takes a
  // four-space `name: {` line for a scene.)
  const MEMORY = Object.fromEntries([
    ["", { about: "", facts: ["- 2026-03-14: The user's name is Ishaan; they live in Austin",
      "- 2026-05-02: Prefers short replies and plain sentences, no exclamation marks",
      "- 2026-09-30: Runs Fedora on spike, a desktop with an RTX 3070", "- 2025-11-08: Doesn't drink coffee after 2 pm"] }],
    ["homelab", { about: "Homelab: the servers at home and what runs where", facts: ["- 2026-06-01: tank runs Ollama and Frigate",
      "- 2026-07-12: Backups go to the NAS nightly at 03:00", "- 2026-08-20: laputa is the Proxmox host"] }],
    ["iphone-app", { about: "The iPhone app, Vision Remote", facts: ["- 2026-10-01: Videos in chats never autoplay"] }],
    ["travel", { about: "Travel: trips taken and planned", facts: [] }],
  ]);
  const memEntry = (line, topic) => {
    const m = line.match(/^- (\d{4}-\d{2}-\d{2}): (.*)$/);
    return { line, date: m?.[1] ?? "", text: m?.[2] ?? line.slice(2), ...(topic === undefined ? {} : { topic }) };
  };
  const memFile = (b) => {
    const f = MEMORY[b?.topic ?? ""];
    if (!f || !f.facts.includes(b.line)) throw new Error("no such fact");
    return f;
  };
  const MEMORY_HTTP = {
    "GET memory": (q) => {
      if (q?.q) {
        const words = q.q.toLowerCase().split(/\s+/);
        return { topics: [], facts: Object.entries(MEMORY).flatMap(([name, f]) => f.facts.filter((ln) => words.every((w) => ln.toLowerCase().includes(w))).map((ln) => memEntry(ln, name))) };
      }
      const topic = q?.topic ?? "";
      return { facts: MEMORY[topic].facts.map((ln) => memEntry(ln)), updated: now - 3600, topics: topic ? [] : Object.entries(MEMORY).filter(([n]) => n)
        .map(([name, f]) => ({ name, about: f.about, count: f.facts.length, updated: now - 2 * D })) };
    },
    "POST memory": (q, b) => { const line = `- 2026-11-01: ${b.fact}`; MEMORY[b.topic ?? ""].facts.push(line); return memEntry(line); },
    "POST memory/edit": (q, b) => { const f = memFile(b); const i = f.facts.indexOf(b.line); f.facts[i] = `- ${memEntry(b.line).date}: ${b.text}`; return memEntry(f.facts[i]); },
    "POST memory/forget": (q, b) => { const f = memFile(b); f.facts.splice(f.facts.indexOf(b.line), 1); return { ok: true }; },
  };
  const memoryPage = [
    { wait: 300, click: "[data-action=settings]" },
    { wait: 100, click: ".set-nav-item[data-page=memory]" },
  ];
  const memText = () => [...document.querySelectorAll(".mem-fact .mem-text")].map((t) => t.textContent);
  const memEditor = () => document.querySelector(".mem-edit");

  window.SCENES = {
    chat: { ...base, steps: [
      { wait: 300, check: "the composer shows how full the chat's context is", fn: () => !document.querySelector("#context").hidden && document.querySelector("#context-text").textContent === "42%" },
      { run: () => {
        const i = document.querySelector("#input");
        i.focus(); i.value = "first line"; i.setSelectionRange(10, 10);
        i.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true }));
        i.setRangeText("second line", i.selectionStart, i.selectionEnd, "end");
      } },
      { check: "Shift+Return breaks the line instead of sending", fn: () => document.querySelector("#input").value === "first line\nsecond line" && !sentOf("message").length },
      { run: () => { const i = document.querySelector("#input"); i.value = ""; i.dispatchEvent(new Event("input")); } },
      { check: "a pinned chat heads the list", fn: () => document.querySelector("#chats .group:not(.scouts-head)")?.textContent === "Pinned" && document.querySelector("#chats .group:not(.scouts-head) + .chat-item .chat-name")?.textContent === "Compare NAS drives" },
      { check: "a chat can be opened in a terminal from its title bar", fn: () => !document.querySelector("#open-terminal").hidden },
      { run: () => document.querySelector("#open-terminal").click() },
      { wait: 50, check: "which asks its computer to open it", fn: () => sentOf("open_terminal")[0]?.chat === "c1" },
      { check: "a chat's row says the folder it works in", fn: () => [...document.querySelectorAll("#chats .chat-item")].find((i) => i.textContent.includes("Fix the flaky"))?.querySelector(".chat-folder")?.textContent === "~/code/vision" },
      { run: () => [...document.querySelectorAll("#chats .chat-item")].find((i) => i.textContent.includes("Weekend plans"))?.querySelector(".pin").click() },
      { wait: 50, check: "pinning another tells the server", fn: () => sentOf("pin").some((f) => f.chat === "c2" && f.pinned === true) },
      { check: "and it joins the pinned group", fn: () => { const names = []; let n = [...document.querySelectorAll("#chats .group")].find((g) => g.textContent === "Pinned")?.nextElementSibling; while (n && n.classList.contains("chat-item")) { names.push(n.querySelector(".chat-name").textContent); n = n.nextElementSibling; } return names.sort().join() === "Compare NAS drives,Weekend plans in Austin"; } },
    ] },

    cards: { ...base, steps: [
      { wait: 300, check: "a run of steps reads as one line", fn: () => [...document.querySelectorAll(".steps-text")].map((e) => e.textContent).join(" | ") === "Read a file, searched once, sent an agent | Edited a file, ran a command" },
      { run: () => document.querySelectorAll(".steps-head").forEach((h) => h.click()) },
      { wait: 100, run: () => document.querySelectorAll(".card-head").forEach((h, i) => i > 1 && h.click()) },
      { check: "an edit opens as a diff", fn: () => !!document.querySelector(".card.open .diff-row.add") },
      { check: "a command shows its output", fn: () => document.querySelector(".card.open .card-out")?.textContent.includes("50 passed") },
      { run: () => document.querySelector("#transcript").scrollTop = 120 },
    ] },

    welcome: { ...base, steps: [
      { wait: 300, click: ".side-row[data-action=new_chat]" },
      { wait: 200, check: "a new chat shows the welcome page", fn: () => !document.querySelector("#welcome").hidden },
      { check: "the composer sits in the middle", fn: () => document.querySelector("#welcome-slot #composer") !== null },
      { check: "home shows the scouts, the one waiting on you first", fn: () => document.querySelectorAll(".home-scout").length === 4 && document.querySelector(".home-scout").classList.contains("needs") },
      { check: "and the last few chats", fn: () => document.querySelectorAll(".home-chat").length === 3 },
      { type: ["#input", "Tidy up the README"] },
      { key: ["#input", "Enter"] },
      { wait: 50, check: "the first send opens a chat in the picked folder", fn: () => sentOf("new")[0]?.workdir === "/home/me" },
      { run: () => document.querySelector(".side-row[data-action=new_chat]").click() },
      { wait: 100, run: () => document.querySelector("#input").value = "" },
    ], onSend: (f, emit) => { if (f.type === "new") { /* stay on the welcome page for the shot */ } } },

    folders: { ...base, steps: [
      { wait: 300, click: ".side-row[data-action=new_chat]" },
      { wait: 200, click: "#folder" },
      { wait: 200, check: "the folder menu lists recent folders", fn: () => document.querySelector("#popover .menu-item") !== null },
    ] },

    newchats: { ...base, steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=chats]" },
      { wait: 150, check: "New chats sets the model, folder and mode", fn: () => document.querySelectorAll(".set-body .set-row").length === 3
        && document.querySelector(".set-folder")?.textContent === "~" && document.querySelector("#settings .segmented button.on")?.textContent === "Auto" },
      { run: () => [...document.querySelectorAll("#settings .segmented button")].find((b) => b.textContent === "Plan").click() },
      { wait: 80, check: "picking Plan saves it on the computer", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "defaults" && r.body?.mode === "plan") },
      { click: ".set-folder" },
      { wait: 200, check: "the folder button opens the composer's folder menu", fn: () => document.querySelector("#popover .menu-item") !== null },
      { run: () => [...document.querySelectorAll("#popover .menu-item")].find((b) => b.textContent === "~/code/vision").click() },
      { wait: 100, check: "picking a folder saves it and shows it", fn: () => window.__http.some((r) => r.method === "POST" && r.body?.workdir === "/home/me/code/vision")
        && document.querySelector(".set-folder").textContent === "~/code/vision" },
      { run: () => document.querySelector(".set-close").click() },
      { wait: 50, click: ".side-row[data-action=new_chat]" },
      { wait: 200, check: "a new chat then starts in that folder, in Plan", fn: () => document.querySelector("#folder-text").textContent === "~/code/vision"
        && document.querySelector("#mode-text").textContent === "Plan" },
      { type: ["#input", "Look over the docs"] },
      { key: ["#input", "Enter"] },
      { wait: 100, check: "the first message opens it there", fn: () => sentOf("new")[0]?.workdir === "/home/me/code/vision" },
      { check: "and a Vision that opened it in Auto is switched to Plan", fn: () => sentOf("mode")[0]?.mode === "plan" && sentOf("mode")[0]?.chat === "n1" },
      { wait: 100, click: "[data-action=settings]" },
    ], onSend: (f, emit) => { if (f.type === "new") emit({ type: "chat", ...CHATS[0], chat: "n1", title: "", mode: "auto", opened: true }); } },

    newchatsold: { ...base, http: { "GET defaults": () => ({ tabs: [{ tab: "Claude", models: [{ id: "opus", label: "Opus 5.5" }] }], efforts: [], current: "opus", effort: "" }) }, steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=chats]" },
      { wait: 150, check: "an older Vision keeps only the model, so only the model shows", fn: () => document.querySelectorAll(".set-body .set-row").length === 1
        && document.querySelector(".set-body .chip-btn").textContent.includes("Opus 5.5") },
    ] },

    models: { ...base, steps: [
      { wait: 300, click: "#model" },
      { wait: 200, check: "the model menu opens", fn: () => !document.querySelector("#popover").hidden },
      { check: "the effort slider shows without scrolling the models", fn: () => {
        const pop = document.querySelector("#popover").getBoundingClientRect(), slider = document.querySelector("#popover .effort-slider")?.getBoundingClientRect();
        return document.querySelector("#popover").scrollTop === 0 && slider && slider.bottom <= pop.bottom && slider.top >= pop.top;
      } },
      { run: () => document.querySelectorAll("#popover .menu-item")[1].click() },
      { wait: 100, check: "picking a model switches the chat", fn: () => sentOf("model")[0]?.model === "sonnet" },
    ] },

    // The model menu left open: a long list scrolls, the effort slider stays at its foot.
    modelmenu: { ...base, steps: [
      { wait: 300, click: "#model" },
      { wait: 300, check: "the effort slider is in view with the list scrolled to its end too", fn: () => {
        const pop = document.querySelector("#popover");
        pop.scrollTop = pop.scrollHeight;
        const p = pop.getBoundingClientRect(), r = pop.querySelector(".effort-slider").getBoundingClientRect();
        const ok = r.bottom <= p.bottom && r.top >= p.top;
        pop.scrollTop = 0;
        return ok;
      } },
      { wait: 3000, check: "the menu stays open by itself", fn: () => !document.querySelector("#popover").hidden },
    ] },

    streaming: { ...base, steps: [
      { wait: 200, frame: { type: "chat", ...CHATS[0], busy: true, queued: ["also check the thumbnail cache"] } },
      { frame: { type: "start", chat: "c1", text: "ok, run the full suite and commit" } },
      { frame: { type: "tool", chat: "c1", id: "s1", name: "Bash", detail: "pytest -q", done: false, at: 0 } },
      { frame: { type: "delta", chat: "c1", text: "Running the whole suite now; " } },
      { frame: { type: "delta", chat: "c1", text: "it takes about a minute." } },
      { frame: { type: "status", chat: "c1", tool: "Bash", label: "Running pytest…" } },
      { check: "the running tool spins", fn: () => !!document.querySelector(".state.running") },
      { check: "the queued message waits at the bottom", fn: () => !!document.querySelector(".queued-wrap .bubble") },
    ] },

    // The chat summary held here is from the turn's start (no text, no steps): a reload of the history
    // mid-reply (clicking the chat again) must keep what streamed since, as the phone does.
    "streaming-reload": { ...base, steps: (() => {
      const p1 = "To keep the shrink with no jump, the card needs its own layer.";
      const p2 = "\n\nThe border needs to sit on top of the chat.";
      return [
      { wait: 200, frame: { type: "chat", ...CHATS[0], busy: true, partial: "", tools: [] } },
      { frame: { type: "start", chat: "c1", text: "I want no edge jump" } },
      { frame: { type: "delta", chat: "c1", text: p1 } },
      { frame: { type: "tool", chat: "c1", id: "r1", name: "Bash", detail: "sed -n 108,175p SideDrawer.swift", done: true, at: [...p1].length, output: "…" } },
      { frame: { type: "delta", chat: "c1", text: p2 } },
      { frame: { type: "tool", chat: "c1", id: "r2", name: "Bash", detail: "git checkout -- SideDrawer.swift", done: false, at: [...(p1 + p2)].length } },
      { wait: 100, run: () => [...document.querySelectorAll("#chats .chat-item")].find((i) => i.textContent.includes("Fix the flaky")).click() },
      { wait: 300, check: "the reply keeps the text streamed before the reload", fn: () => document.querySelector(".msg.streaming")?.textContent.includes("The border needs to sit") },
      { check: "and the steps around it", fn: () => document.querySelectorAll(".msg.streaming .card").length === 2 },
      { frame: { type: "delta", chat: "c1", text: "\n\nStill clipped." } },
      { check: "and goes on from there", fn: () => document.querySelector(".msg.streaming")?.textContent.includes("Still clipped") },
    ]; })() },

    question: { ...base, steps: [
      { wait: 200, frame: { type: "chat", ...CHATS[0], busy: true, waiting: true } },
      { frame: { type: "start", chat: "c1", text: "commit it" } },
      { frame: { type: "question", chat: "c1", questions: [{ question: "Commit this to master or a branch?", header: "Commit", multiSelect: false,
        options: [{ label: "New branch", description: "fix-upload-race, then a PR" }, { label: "Straight to master" }] }] } },
      { wait: 100, run: () => document.querySelector(".q-option").click() },
      { check: "a picked option is marked", fn: () => !!document.querySelector(".q-option.on") },
      { run: () => document.querySelector(".q-actions .primary").click() },
      { check: "Answer sends the picked label", fn: () => sentOf("answer")[0]?.answers?.["Commit this to master or a branch?"] === "New branch" },
    ] },

    plan: { ...base, steps: (() => {
      const plan = "## Fix the upload race\n\n1. Make `drop_uploads` skip files younger than a minute\n2. Add a test that uploads, waits on a slow assertion and checks the file is still there\n3. Run the suite 50 times to be sure it's gone\n\nNo other files change.";
      const q = { question: "Carry out this plan?\n\n" + plan, header: "Plan", multiSelect: false,
        options: [{ label: "Yes", description: "switch to auto mode and start" }, { label: "No", description: "stay in plan mode" }] };
      return [
        { wait: 200, frame: { type: "chat", ...CHATS[0], busy: true, waiting: true, mode: "plan" } },
        { frame: { type: "start", chat: "c1", text: "plan the fix first" } },
        { frame: { type: "delta", chat: "c1", text: "Here's what I'd do." } },
        { frame: { type: "tool", chat: "c1", id: "p1", name: "ExitPlanMode", detail: "", done: false, at: 19, input: [["plan", plan]] } },
        { frame: { type: "question", chat: "c1", questions: [q] } },
        { wait: 120, check: "a plan question shows as a plan card", fn: () => !!document.querySelector(".plan-review .plan-body h2") },
        { type: [".plan-changes", "Also log what it skips"] },
        { run: () => document.querySelector(".plan-review .primary").click() },
        { check: "typed changes go back as the answer", fn: () => sentOf("answer")[0]?.answers?.[q.question] === "Also log what it skips" },
        { run: () => { document.querySelector(".plan-changes").value = ""; document.querySelector(".plan-changes").dispatchEvent(new Event("input")); } },
      ];
    })() },

    pictures: { ...base, history: { ...HISTORY, c1: [
      { role: "user", text: "what's wrong with this chart?\n\n[Attached image: /home/me/.local/share/vision/uploads/20261024-101500-a1b2c3.png]\n[Attached file: /home/me/.local/share/vision/uploads/20261024-101500-d4e5f6-sales.csv]" },
      { role: "assistant", text: "The y-axis starts at 40, so the dip looks far bigger than it is. Here it is redrawn from zero:\n\n![Sales from zero](/home/me/chart.png)\n\nThe drop is about 6%, not the cliff the original suggests.", duration_ms: 12100 },
    ] }, steps: [
      { wait: 400, check: "a sent picture shows as a thumbnail", fn: () => document.querySelector(".sent-picture")?.naturalWidth > 0 },
      { check: "a sent file shows as a chip without its stamp", fn: () => document.querySelector(".file-chip")?.textContent === "sales.csv" },
      { check: "the attachment lines leave the bubble", fn: () => document.querySelector(".msg.user .bubble")?.textContent === "what's wrong with this chart?" },
      { check: "a reply's local picture loads and draws", fn: () => { const img = document.querySelector(".md .picture"); return img?.src.startsWith("blob:") && img.complete && img.naturalWidth > 0; } },
    ] },

    shell: { ...base, history: { ...HISTORY, c1: [...HISTORY.c1,
      { role: "shell", text: "git status --short", output: " M vision/server.py\n?? tests/test_upload_race.py", code: 0 },
      { role: "shell", text: "make lint", output: "vision/server.py:418: E501 line too long (131 > 120)", code: 2 },
    ] }, steps: [
      { wait: 300, type: ["#input", "!pytest -q tests"] },
      { check: "a leading ! turns the composer into a command line", fn: () => document.querySelector("#composer").classList.contains("shell-mode") },
      { key: ["#input", "Enter"] },
      { check: "Return runs it as a shell command", fn: () => sentOf("shell")[0]?.text === "pytest -q tests" },
      { frame: { type: "shell", chat: "c1", text: "pytest -q tests", running: true } },
      { frame: { type: "shell_output", chat: "c1", text: "pytest -q tests", output: "........................................ [ 41%]\n..............." } },
      { wait: 150, check: "a running command shows Stop", fn: () => !!document.querySelector(".shell-card.running .shell-stop") },
    ] },

    settings: { ...base, voiceOn: true, steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 200, check: "Settings opens on General, its pages down the left", fn: () => !document.querySelector("#settings").hidden
        && document.querySelector(".set-nav-item.on")?.dataset.page === "general" && document.querySelectorAll(".set-nav-item").length >= 8
        && document.querySelector("#settings-card .set-head h2").textContent === "General" },
      { click: ".set-nav-item[data-page=chats]" },
      { wait: 150, check: "the default model shows", fn: () => document.querySelector(".set-section .chip-btn")?.textContent.includes("Opus 5.5") },
      { click: ".set-nav-item[data-page=voice]" },
      { wait: 150, check: "the voice picker lists voices", fn: () => document.querySelectorAll("#settings select option").length === 2
        && document.querySelector(".set-nav-item.on")?.dataset.page === "voice" },
      { run: () => document.querySelector(".set-close").click() },
      { wait: 50, click: "[data-action=settings]" },
      { wait: 150, check: "it opens again where it was left", fn: () => document.querySelector(".set-nav-item.on")?.dataset.page === "voice" },
    ] },

    // Settings › Notifications: a muted kind and quiet hours hold notifications back; the board still moves.
    // Thread frames notify whether or not the window is focused, so they stand in for every kind.
    notifications: { ...base, http: AGENT_HTTP, steps: [
      { wait: 300, click: ".side-row[data-action=agents]" },
      { wait: 200, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=notifications]" },
      { wait: 200, check: "a switch for each kind, all on", fn: () => [...document.querySelectorAll(".set-row[data-kind]")].map((r) => r.dataset.kind).join() === "replies,asks,approvals,review,reports"
        && [...document.querySelectorAll(".set-row[data-kind] .toggle")].every((t) => t.classList.contains("on")) },
      { check: "the system's permission shows as allowed", fn: () => document.querySelector(".set-permission")?.textContent === "Allowed" },
      { check: "quiet hours start off, their times greyed", fn: () => document.querySelector(".quiet-from").disabled && document.querySelector(".quiet-to").value === "08:00" },
      { run: () => document.querySelector(".set-row[data-kind=review] .toggle").click() },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[2], state: "ready_for_review" } } },
      { wait: 300, check: "a muted kind doesn't notify", fn: () => !window.__notified.length },
      { check: "but the board still moves the card", fn: () => document.querySelectorAll(".lane")[1].querySelectorAll(".thread-card").length === 2 },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[0], state: "working" } } },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[0], state: "waiting_on_you" } } },
      { wait: 100, check: "a kind left on notifies, with the system's sound", fn: () => window.__notified.length === 1
        && window.__notified[0].title === "Vision desktop app: needs you" && window.__notified[0].sound === "message-new-instant" },
      { run: () => {
        const hhmm = (d) => `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
        const set = (sel, d) => { const i = document.querySelector(sel); i.value = hhmm(d); i.dispatchEvent(new Event("change")); };
        document.querySelector(".quiet-on").click();
        set(".quiet-from", new Date(Date.now() - 3600e3));
        set(".quiet-to", new Date(Date.now() + 3600e3));
      } },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[0], state: "working" } } },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[0], state: "waiting_on_you" } } },
      { wait: 300, check: "quiet hours hold one back", fn: () => window.__notified.length === 1 && !document.querySelector(".quiet-from").disabled },
      { check: "while the badge still counts it", fn: () => document.querySelector("#agents-badge").textContent !== "" && !document.querySelector("#agents-badge").hidden },
      { run: () => { document.querySelector(".quiet-on").click(); [...document.querySelectorAll(".set-row")].find((r) => r.textContent.includes("Play a sound")).querySelector(".toggle").click(); } },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[0], state: "working" } } },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[0], state: "waiting_on_you" } } },
      { wait: 100, check: "out of quiet hours it notifies again, silently with the sound off", fn: () => window.__notified.length === 2 && !window.__notified[1].sound },
      { click: ".set-test" },
      { wait: 100, check: "Send shows a test notification", fn: () => window.__notified.at(-1)?.body === "Notifications are working." },
      { check: "the choices are kept", fn: () => { const p = JSON.parse(localStorage.getItem("notifications")); return p.off.join() === "review" && p.sound === false && p.quiet.on === false; } },
      { run: () => { document.querySelector(".quiet-on").click(); document.querySelector(".set-body").scrollTop = 1e4; } },  // for the picture: quiet hours on, the bottom of the page
    ] },
    notifypermission: { ...base, notifyPermission: "default", steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=notifications]" },
      { wait: 200, check: "without the system's permission there's a button to ask", fn: () => document.querySelector(".set-permission")?.tagName === "BUTTON" },
      { click: ".set-permission" },
      { wait: 200, check: "which asks, and then it shows as allowed", fn: () => document.querySelector(".set-permission")?.textContent === "Allowed" },
    ] },

    // Settings › Shortcuts: every key as caps; the dictation keys take a new combination and mic.ts follows.
    shortcuts: { ...base, voiceOn: true, transcribe: true, mic: {}, heard: "hello", steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=shortcuts]" },
      { wait: 150, check: "shortcuts are grouped", fn: () => [...document.querySelectorAll(".set-section h3")].map((h) => h.textContent).join() === "Dictation,Window,Chat,Find in chat,Scouts,Editing" },
      { check: "drawn as Linux key caps", fn: () => capsOf("New chat") === "Ctrl N" && capsOf("Send") === "Enter" && capsOf("Stop the reply") === "Ctrl . Esc"
        && [...document.querySelectorAll(".keys-btn .keycap")].map((k) => k.textContent).join(" ") === "Ctrl Shift Space" },
      { check: "the dictation keys aren't changed yet", fn: () => document.querySelector(".keys-reset").hidden },
      { click: ".keys-btn" },
      { wait: 50, check: "a click waits for the new keys", fn: () => document.querySelector(".keys-btn").classList.contains("listening") },
      { run: () => document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true })) },
      { wait: 50, check: "Esc keeps the old ones, and leaves Settings open", fn: () => !document.querySelector(".keys-btn").classList.contains("listening") && !document.querySelector("#settings").hidden },
      { click: ".keys-btn" },
      { run: () => press({ key: "k", code: "KeyK", ctrlKey: true }) },
      { wait: 50, check: "keys already taken are refused", fn: () => document.querySelector(".keys-dictation .set-text .dim").textContent.includes("“Search chats”")
        && document.querySelector(".keys-btn").classList.contains("listening") },
      { run: () => press({ key: "d", code: "KeyD" }) },
      { wait: 50, check: "so is a key without a modifier", fn: () => document.querySelector(".keys-dictation .set-text .dim").textContent.startsWith("Hold Ctrl") },
      { run: () => { press({ key: "Control", code: "ControlLeft", ctrlKey: true }); press({ key: "Alt", code: "AltLeft", ctrlKey: true, altKey: true }); press({ key: "d", code: "KeyD", ctrlKey: true, altKey: true }); } },
      { wait: 50, check: "a free combination is taken and kept", fn: () => localStorage.getItem("dictation-keys") === "Mod+Alt+KeyD"
        && [...document.querySelectorAll(".keys-btn .keycap")].map((k) => k.textContent).join(" ") === "Ctrl Alt D" && !document.querySelector(".keys-reset").hidden },
      { run: () => document.querySelector(".set-close").click() },
      { wait: 50, check: "the mic's tooltip names the new keys", fn: () => mic().title.includes("(Ctrl+Alt+D)") },
      { run: () => press({ key: " ", code: "Space", ctrlKey: true, shiftKey: true }) },
      { wait: 300, check: "the old keys no longer dictate", fn: () => !mic().classList.contains("recording") },
      { run: () => press({ key: "d", code: "KeyD", ctrlKey: true, altKey: true }) },
      { wait: 600, check: "the new ones do", fn: () => mic().classList.contains("recording") },
      { run: () => window.dispatchEvent(new KeyboardEvent("keyup", { key: "d", code: "KeyD", ctrlKey: true, altKey: true, bubbles: true })) },
      { wait: 500, check: "and letting go of them transcribes", fn: () => window.__transcribed.length === 1 && !mic().classList.contains("recording") },
      { click: "[data-action=settings]" },
      { wait: 150, click: ".keys-reset" },
      { wait: 50, check: "Reset puts the keys back", fn: () => localStorage.getItem("dictation-keys") === null && document.querySelector(".keys-reset").hidden
        && mic().title.includes("(Ctrl+Shift+Space)") },
    ] },
    shortcutsmac: { ...base, os: "macos", steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=shortcuts]" },
      { wait: 150, check: "on a Mac the caps say ⌘ and ⇧, and Return", fn: () => capsOf("New scout") === "⌘ ⇧ N" && capsOf("Send") === "Return"
        && [...document.querySelectorAll(".keys-btn .keycap")].map((k) => k.textContent).join(" ") === "⌘ ⇧ Space" },
    ] },

    permissions: { ...base, http: PERMISSIONS_HTTP(), steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=permissions]" },
      { wait: 200, check: "the rules show in words over the rule itself, in their lists", fn: () => {
        const row = document.querySelector("[data-kind=ask] .perm-row[data-rule='Bash(git push:*)']");
        return row?.querySelector(".perm-label").textContent === "Run commands starting with git push"
          && row.querySelector(".segmented .on")?.textContent === "Ask"
          && document.querySelectorAll("[data-kind=deny] .perm-row").length === 4;
      } },
      { check: "Vision's own defaults are marked", fn: () => !!document.querySelector("[data-kind=deny] .perm-row[data-rule='Bash(sudo:*)'] .perm-tag") },
      { check: "a file rule reads as one", fn: () => document.querySelector(".perm-row[data-rule='Write(docs/**)'] .perm-label")?.textContent === "Create or overwrite files matching docs/**" },
      { check: "Claude Code's own rules are shown read only, with where they live", fn: () => document.querySelectorAll(".perm-claude .perm-row.readonly").length === 2
        && !document.querySelector(".perm-claude .segmented") && document.querySelector(".perm-claude .perm-hint code")?.textContent === "~/.claude/settings.json" },
      { check: "it says which brains only get them as instructions", fn: () => [...document.querySelectorAll(".perm-who")].some((r) => r.textContent.startsWith("Codex")) },
      { check: "scouts' defaults follow the leash until set, and say what that means", fn: () => document.querySelector(".perm-scouts [data-action=publish] .segmented .on")?.textContent === "Leash"
        && document.querySelector(".perm-scouts [data-action=publish] .dim")?.textContent === "Leash: asks on Check-ins, does it on Autonomous." },
    ] },

    permissionsedit: { ...base, http: PERMISSIONS_HTTP(), steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=permissions]" },
      { wait: 200, type: [".perm-input", "npm publish"] },
      { wait: 50, check: "a typed command becomes a rule, previewed", fn: () => document.querySelector(".perm-preview code")?.textContent === "Bash(npm publish:*)" },
      { run: () => document.querySelector(".perm-add .segmented button:nth-child(3)").click() },
      { run: () => document.querySelector(".perm-add-btn").click() },
      { wait: 150, check: "Add sends the whole list with it", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "permissions"
        && r.body?.deny?.at(-1) === "Bash(npm publish:*)") && !!document.querySelector("[data-kind=deny] .perm-row[data-rule='Bash(npm publish:*)']") },
      { run: () => [...document.querySelector(".perm-row[data-rule='Edit(.env)'] .segmented").children].find((b) => b.textContent === "Never").click() },
      { wait: 150, check: "the picker moves a rule to another list", fn: () => !!document.querySelector("[data-kind=deny] .perm-row[data-rule='Edit(.env)']")
        && !document.querySelector("[data-kind=ask] .perm-row[data-rule='Edit(.env)']") },
      { run: () => document.querySelector(".perm-row[data-rule='Write(docs/**)'] .perm-remove").click() },
      { wait: 150, check: "and the cross takes one away", fn: () => !document.querySelector(".perm-row[data-rule='Write(docs/**)']")
        && document.querySelector("[data-kind=allow] .perm-empty")?.textContent === "No exceptions." },
      { run: () => [...document.querySelector(".perm-scouts [data-action=publish] .segmented").children].find((b) => b.textContent === "Asks").click() },
      { wait: 150, check: "a scouts' default can be set for every scout", fn: () => window.__http.some((r) => r.body?.scouts?.publish === "asks")
        && document.querySelector(".perm-scouts [data-action=publish] .segmented .on")?.textContent === "Asks" },
      { type: [".perm-input", "Bash(git push:*)"] },
      { run: () => document.querySelector(".perm-add .segmented button:nth-child(1)").click() },
      { run: () => document.querySelector(".perm-add-btn").click() },
      { wait: 150, check: "adding a rule that's in another list moves it", fn: () => !!document.querySelector("[data-kind=allow] .perm-row[data-rule='Bash(git push:*)']")
        && !document.querySelector("[data-kind=ask] .perm-row[data-rule='Bash(git push:*)']") },
      { type: [".perm-input", "Edit(.env"] },
      { run: () => document.querySelector(".perm-add-btn").click() },
      { wait: 150, check: "a rule the computer refuses says why and nothing changes", fn: () => document.querySelector("#toast").textContent.includes("isn't a rule")
        && !document.querySelector(".perm-row[data-rule='Edit(.env']") },
      { run: () => { document.querySelector("#toast").hidden = true; document.querySelector(".perm-add").scrollIntoView({ block: "start" }); } },
    ] },

    memory: { ...base, http: MEMORY_HTTP, steps: [
      ...memoryPage,
      { wait: 200, check: "Memory lists the profile's facts with their dates", fn: () => memText().length === 4 && memText()[0].startsWith("The user's name is Ishaan")
        && document.querySelector(".mem-fact .mem-date")?.textContent.length > 0 },
      { check: "and the areas, each with what it covers and how many facts", fn: () => [...document.querySelectorAll(".mem-topic")].map((r) => r.querySelector(".mem-topic-title").textContent + "/" + r.querySelector(":scope > .dim").textContent).join()
        === "Homelab/3 facts,iPhone app/1 fact,Travel/0 facts" && document.querySelector(".mem-topic .dim")?.textContent === "The servers at home and what runs where" },
      { click: ".mem-fact:nth-child(2) .mem-text" },
      { wait: 80, check: "clicking a fact opens it for editing, holding its words", fn: () => memEditor()?.value === "Prefers short replies and plain sentences, no exclamation marks" },
      { run: () => { memEditor().value = "Prefers short replies"; } },
      { key: [".mem-edit", "Enter"] },
      { wait: 150, check: "Return rewords it in place", fn: () => window.__http.some((r) => r.path === "memory/edit" && r.body.line === "- 2026-05-02: Prefers short replies and plain sentences, no exclamation marks"
        && r.body.text === "Prefers short replies" && r.body.topic === "") && memText()[1] === "Prefers short replies" && !memEditor() },
      { click: ".mem-fact:nth-child(3) .mem-act[title=Edit]" },
      { wait: 80, run: () => { memEditor().value = "something else"; } },
      { key: [".mem-edit", "Escape"] },
      { wait: 80, check: "Escape puts it back without saving, and leaves Settings open", fn: () => !memEditor() && memText()[2] === "Runs Fedora on spike, a desktop with an RTX 3070"
        && window.__http.filter((r) => r.path === "memory/edit").length === 1 && !document.querySelector("#settings").hidden },
      { click: ".mem-fact:nth-child(4) .mem-forget" },
      { wait: 150, check: "the bin forgets a fact", fn: () => window.__http.some((r) => r.path === "memory/forget" && r.body.line === "- 2025-11-08: Doesn't drink coffee after 2 pm") && memText().length === 3 },
      { click: ".mem-add" },
      { wait: 80, run: () => { memEditor().value = "Has a cat called Miso"; } },
      { key: [".mem-edit", "Enter"] },
      { wait: 200, check: "Add a fact saves to the profile and opens a box for the next", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "memory" && r.body.fact === "Has a cat called Miso" && r.body.topic === "")
        && memText().includes("Has a cat called Miso") && memEditor()?.value === "" },
      { run: () => memEditor().blur() },
      { wait: 100, check: "an empty box closes when left", fn: () => !memEditor() && !!document.querySelector(".mem-add") },
      { click: ".mem-fact:nth-child(1) .mem-text" },
    ] },
    memorytopic: { ...base, http: MEMORY_HTTP, steps: [
      ...memoryPage,
      { wait: 200, click: ".mem-topic[data-topic=homelab]" },
      { wait: 150, check: "an area opens on its own facts, under its name and what it covers", fn: () => document.querySelector(".mem-head h3")?.textContent === "Homelab"
        && memText().join() === "tank runs Ollama and Frigate,Backups go to the NAS nightly at 03:00,laputa is the Proxmox host" },
      { click: ".mem-fact:nth-child(1) .mem-text" },
      { wait: 80, run: () => { memEditor().value = "tank runs Ollama, Frigate and Home Assistant"; memEditor().blur(); } },
      { wait: 150, check: "clicking away saves an edit, in that area's file", fn: () => window.__http.some((r) => r.path === "memory/edit" && r.body.topic === "homelab") && memText()[0] === "tank runs Ollama, Frigate and Home Assistant" },
      { click: ".mem-add" },
      { wait: 80, run: () => { memEditor().value = "kanade is a Raspberry Pi"; } },
      { key: [".mem-edit", "Enter"] },
      { wait: 200, check: "a new fact goes into the open area", fn: () => window.__http.some((r) => r.path === "memory" && r.body?.topic === "homelab") && MEMORY.homelab.facts.length === 4 },
      { key: [".mem-edit", "Escape"] },
      { wait: 100, click: ".mem-back" },
      { wait: 150, check: "Back returns to the list, its count up to date", fn: () => document.querySelector(".mem-topic[data-topic=homelab] > .dim")?.textContent === "4 facts" },
      { click: ".mem-topic[data-topic=travel]" },
      { wait: 150, check: "an empty area says so", fn: () => document.querySelector(".mem-none")?.textContent === "Nothing here yet." },
      { click: ".mem-back" },
      { wait: 150, click: ".mem-topic[data-topic=homelab]" },
    ] },
    memorysearch: { ...base, http: MEMORY_HTTP, steps: [
      ...memoryPage,
      { wait: 200, type: [".mem-search input", "the"] },
      { wait: 350, check: "the search looks through every file, saying where each fact is", fn: () => [...document.querySelectorAll(".mem-fact .mem-file")].map((b) => b.textContent).join() === "Profile,Homelab,Homelab"
        && window.__http.some((r) => r.path === "memory" && r.query?.q === "the") },
      { type: [".mem-search input", "zebra"] },
      { wait: 350, check: "and says when nothing matches", fn: () => document.querySelector(".mem-empty")?.textContent === "Nothing in memory mentions “zebra”." },
      { type: [".mem-search input", "nas"] },
      { wait: 350, click: ".mem-fact .mem-text" },
      { wait: 80, run: () => { memEditor().value = "Backups go to the NAS nightly at 02:00"; } },
      { key: [".mem-edit", "Enter"] },
      { wait: 200, check: "a hit can be edited where it is found", fn: () => MEMORY.homelab.facts[1] === "- 2026-07-12: Backups go to the NAS nightly at 02:00" && memText()[0] === "Backups go to the NAS nightly at 02:00" },
      { click: ".mem-file" },
      { wait: 200, check: "and its file opened, which clears the search", fn: () => document.querySelector(".mem-head h3")?.textContent === "Homelab" && document.querySelector(".mem-search input").value === "" },
      { type: [".mem-search input", "o"] },
    ] },

    agents: { ...base, http: AGENT_HTTP, asleep: [{ from: now - 9 * 3600, to: now - 2 * 3600 }], steps: [
      { wait: 300, click: ".side-row[data-action=agents]" },
      { wait: 200, check: "the board's columns each carry their colour", fn: () => [...document.querySelectorAll(".lane")].map((l) => [...l.classList].find((c) => c.startsWith("tone-")) ?? "wide").join() === "tone-ask,tone-ready,tone-busy,tone-idle,wide" },
      { wait: 200, check: "the board groups threads by what they need", fn: () => [...document.querySelectorAll(".lane-head h3")].map((h) => h.textContent).join(",") === "Needs you,Ready for review,Working,Idle,Coming up" },
      { check: "the sidebar badge counts what needs you: questions, work to review and requests", fn: () => document.querySelector("#agents-badge").textContent === "3" },
      { check: "a recent sleep is explained", fn: () => !!document.querySelector(".sleep-banner") },
      { frame: { type: "thread", project: "p1", thread: { ...THREADS.p1[2], state: "ready_for_review", last_result: { status: "completed", summary: "Tests added." } } } },
      { wait: 100, check: "a thread frame moves its card", fn: () => document.querySelectorAll(".lane")[1].querySelectorAll(".thread-card").length === 2 },
      { check: "finishing notifies", fn: () => window.__notified.some((n) => n.title === "Vision desktop app: ready for review") },
      { check: "the tray says what the agents are up to, with the same count", fn: () => window.__tray === "4 need you · 1 working" },
    ] },

    thread: { ...base, http: AGENT_HTTP, steps: [
      { wait: 300, click: ".side-row[data-action=agents]" },
      { wait: 200, run: () => document.querySelectorAll(".thread-card")[1].click() },
      { wait: 200, check: "a card opens the thread beside the board", fn: () => !!document.querySelector(".thread-panel .panel-titles h2") },
      { check: "its conversation loads", fn: () => document.querySelectorAll(".convo").length === 2 },
      { check: "a finished thread offers Land", fn: () => [...document.querySelectorAll(".panel-buttons button")].some((b) => b.textContent === "Land") },
      { run: () => [...document.querySelectorAll(".panel-tabs button")][1].click() },
      { wait: 150, run: () => { const f = document.querySelector(".change-file"); f.open = true; f.dispatchEvent(new Event("toggle")); } },
      { wait: 150, check: "a changed file opens as a patch", fn: () => !!document.querySelector(".patch .diff-row.add") },
    ] },

    firstrun: { ...base, unpaired: true, steps: [
      { wait: 300, check: "a computer without Vision gets the welcome page", fn: () => document.querySelector("#setup h1")?.textContent === "Welcome to Vision" },
    ] },

    install: { ...base, unpaired: true, providers: PROVIDERS(), steps: [
      { wait: 300, click: "#setup .primary.big" },
      { wait: 80, check: "installing shows its steps", fn: () => document.querySelectorAll(".setup-steps li").length === 3 },
      { run: () => { document.querySelector(".setup-log").open = true; } },
    ] },

    installfail: { ...base, unpaired: true, failAt: "install", steps: [
      { wait: 300, click: "#setup .primary.big" },
      { wait: 800, check: "a failed install says so and offers to try again", fn: () => !!document.querySelector(".setup-card .reply-error:not([hidden])") && [...document.querySelectorAll(".setup-card .primary")].some((b) => b.textContent === "Try again") },
    ] },

    subscriptions: { ...base, unpaired: true, providers: PROVIDERS(), steps: [
      { wait: 300, click: "#setup .primary.big" },
      { wait: 1500, check: "once running, it asks for a subscription", fn: () => document.querySelectorAll(".sub").length === 3 },
      { check: "and it's connected underneath", fn: () => !document.querySelector("#app").hidden },
      { run: () => [...document.querySelectorAll(".sub .chip-btn")].find((b) => b.textContent === "Sign in").click() },
      { wait: 400, check: "signing in turns the row ready", fn: () => document.querySelectorAll(".sub.ready").length === 2 },
    ] },

    update: { ...base, update: "vision upgrade", steps: [
      { wait: 300, check: "a newer Vision gets a notice with Update", fn: () => document.querySelector("#notice .chip-btn")?.textContent === "Update" },
    ] },

    appupdate: { ...base, appUpdate: "0.2.0", steps: [
      { wait: 400, check: "a newer app shows in the sidebar", fn: () => document.querySelector("#app-update:not([hidden]) .app-update-act")?.textContent === "Restart to update" },
    ] },

    appupdatepkg: { ...base, appUpdate: "0.2.0", installKind: "package", steps: [
      { wait: 400, check: "an rpm or deb install is sent to the download", fn: () => document.querySelector("#app-update .app-update-act")?.textContent === "Download" },
    ] },

    oldserver: { ...base, protocol: 0, steps: [
      { wait: 300, check: "an older Vision is called out", fn: () => document.querySelector("#notice.urgent") !== null },
    ] },

    about: { ...base, update: "vision upgrade", appUpdate: "0.2.0", steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=about]" },
      { wait: 150, check: "About shows both versions and the computer", fn: () => document.querySelector(".set-body").textContent.includes("Vision desktop 0.1.0")
        && document.querySelector(".set-body").textContent.includes("Vision 0.9.3")
        && [...document.querySelectorAll(".set-section h3")].map((h) => h.textContent).join("|") === "This app|Vision on this computer|Links" },
      { run: () => buttonIn("Vision desktop", "Check for updates").click() },
      { wait: 150, check: "Check for updates finds the new app", fn: () => rowOf("Vision desktop").textContent.includes("Vision desktop 0.2.0 is out.") && buttonIn("Vision desktop", "Restart to update") },
      { check: "a newer Vision here can be upgraded from the app", fn: () => buttonIn("Vision 0.9.3", "Upgrade Vision") !== undefined },
      { run: () => buttonIn("Vision 0.9.3", "Upgrade Vision").click() },
      { wait: 400, check: "which runs vision upgrade and says what happens next", fn: () => rowOf("Vision 0.9.3").textContent.includes("Vision restarts once every reply has finished")
        && document.querySelector("#notice").hidden },
      { check: "and the links open GitHub", fn: () => rowOf("Release notes").querySelector("button")?.textContent === "Open" && rowOf("Guide") !== undefined },
    ] },

    aboutcheckout: { ...base, update: "cd ~/code/vision && git pull", installKind: "dev", steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=about]" },
      { wait: 150, check: "a development build says updates come with releases", fn: () => rowOf("Vision desktop").textContent.includes("Updates come with releases")
        && !rowOf("Vision desktop").querySelector("button") },
      { check: "a Vision run from a checkout shows the command instead of a button", fn: () => document.querySelector(".set-command code")?.textContent === "cd ~/code/vision && git pull"
        && !buttonIn("Vision 0.9.3", "Upgrade Vision") },
    ] },

    aboutcurrent: { ...base, steps: [
      { wait: 300, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=about]" },
      { wait: 150, run: () => buttonIn("Vision desktop", "Check for updates").click() },
      { wait: 150, check: "with nothing newer it says so", fn: () => rowOf("Vision desktop").textContent.includes("This is the newest version.")
        && rowOf("Vision 0.9.3").textContent.includes("Up to date.") && !document.querySelector(".set-command") },
    ] },

    connect: { ...base, steps: [{ wait: 300, click: "#computer" }] },

    // Scouts (scouts.ts, buddy.ts)
    buddies: { ...base, steps: [
      { wait: 300, run: () => {
        const moods = ["asleep", "waking", "curious", "focused", "found_it", "needs_you", "still_waiting", "proud", "worried", "out_cold"];
        const grid = document.createElement("div");
        grid.id = "buddy-grid";
        grid.style.cssText = "position:fixed;inset:0;z-index:50;background:var(--bg);display:grid;grid-template-columns:repeat(10,1fr);gap:18px 6px;padding:40px 30px;align-content:start;color:var(--text);font-size:11px;text-align:center";
        for (const [trait, hue] of [["antenna", 205], ["sprout", 32], ["ears", 268], ["curl", 140]]) for (const mood of moods) {
          const cell = document.createElement("div");
          cell.append(window.__buddy({ hue, trait }, mood, 84, { still: true }), Object.assign(document.createElement("div"), { textContent: mood.replace("_", " ") }));
          grid.append(cell);
        }
        for (const mood of moods) grid.append(window.__buddy({ hue: 340, trait: "antenna" }, mood, 26, { still: true }));
        document.body.append(grid);
      } },
      { wait: 200, check: "every mood draws for every trait", fn: () => document.querySelectorAll("#buddy-grid .buddy svg").length === 50 },
      { check: "each has a body and a face", fn: () => [...document.querySelectorAll("#buddy-grid .buddy svg")].every((s) => s.querySelector("path[fill^='url(']") && s.querySelector(".gaze")) },
      { check: "small ones leave the props off", fn: () => [...document.querySelectorAll("#buddy-grid > .buddy")].every((b) => !b.innerHTML.includes("#ffd76a") && !b.innerHTML.includes("<text")) },
    ] },

    scouts: { ...base, http: SCOUT_HTTP, steps: [
      { wait: 300, check: "a scout waiting on you says so in amber", fn: () => document.querySelector(".chat-item.scout .scout-line")?.classList.contains("tone-ask") },
      { wait: 300, check: "the sidebar lists scouts with what they're doing", fn: () => document.querySelector(".chat-item.scout .scout-line")?.textContent.endsWith("3 need you") },
      { check: "a scout's own chat isn't listed with the chats", fn: () => ![...document.querySelectorAll(".chat-item:not(.scout) .chat-name")].some((n) => n.textContent === "Vision desktop app") },
      { check: "the Scouts heading adds up what each scout's row says", fn: () => document.querySelector(".scouts-head .needs-link")?.textContent === "3 need you" && document.querySelector(".chat-item.scout .scout-line")?.textContent === "3 need you" },
      openAtlas,
      { wait: 300, check: "a scout's page shows its name and goal", fn: () => document.querySelector(".scout-titles .scout-name")?.textContent === "Atlas" && document.querySelector(".scout-leash")?.hidden && !!document.querySelector(".scout-goal")?.textContent },
      { check: "the rail has its four sections", fn: () => [...document.querySelectorAll(".rail-head h3")].map((h) => h.textContent).join(",") === "Needs you,Activity,Schedule,Remembers" },
      { check: "a thread report shows as a card", fn: () => document.querySelectorAll(".report-card .report-list li").length === 3 },
      { check: "what needs you is listed", fn: () => document.querySelectorAll(".need-card:not(.approval-card)").length === 2 },
      { check: "today's activity is listed", fn: () => document.querySelectorAll(".timeline .ev").length === 5 },
      { check: "it asks before big changes", fn: () => document.querySelector(".ask-row .toggle")?.classList.contains("on") },
      { check: "the composer speaks to the scout", fn: () => document.querySelector("#input").placeholder === "Tell Atlas what to do next…" },
      { frame: { type: "activity", project: "p1", event: { at: now, kind: "finished", text: "“Write the Agents board tests” finished: 12 scenes pass" } } },
      { wait: 100, check: "a finished thread makes the buddy cheer", fn: () => document.querySelector(".scout-head .buddy")?.dataset.mood === "found_it" },
      { check: "and lands in the activity", fn: () => document.querySelector(".timeline .ev .ev-text")?.textContent.includes("12 scenes pass") },
    ] },

    checkin: { ...base, http: SCOUT_HTTP, steps: [
      openAtlas,
      { wait: 300, check: "a check-in shows as a card with its time and what needs you", fn: () => {
        const c = document.querySelector(".checkin-card");
        return c?.querySelector("b")?.textContent === "Morning check-in" && c.querySelector(".pill")?.textContent === "2 need you" && c.querySelector(".report-when")?.textContent.includes("39 min");
      } },
      { check: "the rail says when it checks in", fn: () => document.querySelector(".rail-row .link-btn")?.textContent === "every morning at 8:00" },
      { check: "and what its threads have used", fn: () => [...document.querySelectorAll(".rail-row")].some((r) => r.textContent.includes("48k tokens")) },
      { check: "a resting scout's line says when it's next", fn: () => [...document.querySelectorAll(".chat-item.scout")].find((x) => x.textContent.includes("Wren")).querySelector(".scout-line").textContent.includes(":00") },
      { run: () => document.querySelector(".rail-row .link-btn").click() },
      { wait: 80, run: () => { const k = document.querySelector(".sched-kind"); k.value = "every"; k.dispatchEvent(new Event("change")); document.querySelector(".sched-hours").value = "120"; } },
      { run: () => document.querySelector(".sched-editor .chip-btn").click() },
      { wait: 150, check: "the schedule can be changed from the rail", fn: () => document.querySelector(".rail-row .link-btn")?.textContent === "every 2 hours" },
      { run: () => document.querySelector(".checkin-now").click() },
      { wait: 80, check: "Check in now asks the server", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "projects/p1/checkin") },
      { frame: { type: "checkin", project: "p1", chat: "sc1", state: "started", label: "Afternoon check-in", started: now } },
      { wait: 80, check: "the buddy wakes up for it", fn: () => document.querySelector(".scout-head .buddy")?.dataset.mood === "waking" },
      { check: "and the button says it's under way", fn: () => document.querySelector(".checkin-now").textContent === "Reporting…" },
    ] },

    approvals: { ...base, http: SCOUT_HTTP, steps: [
      openAtlas,
      { wait: 300, check: "a request to run something waits in Needs you", fn: () => document.querySelector(".approval-card .approval-command")?.textContent === "git push -u origin vision/t-review" },
      { check: "and counts towards what needs you", fn: () => document.querySelector(".rail-section .rail-count")?.textContent === "3" },
      { check: "the rules show what it may do alone", fn: () => document.querySelectorAll(".rule-row:not(.fixed)").length === 4 && document.querySelector(".rule-row.set .rule-seg .on")?.textContent === "Never" },
      { run: () => [...document.querySelectorAll(".rule-row")][1].querySelectorAll(".rule-seg button")[0].click() },
      { wait: 80, check: "a rule can be changed", fn: () => window.__http.some((r) => r.path === "projects" && r.body?.settings?.rules?.publish === "does") },
      { run: () => [...document.querySelectorAll(".rule-row")][2].querySelector(".link-btn").click() },
      { wait: 80, check: "and put back to the leash's default", fn: () => window.__http.some((r) => r.body?.settings?.rules?.send === "default") },
      { run: () => [...document.querySelectorAll(".approval-card button")].find((b) => b.textContent === "Edit").click() },
      { wait: 80, check: "Edit turns the command into a field", fn: () => document.querySelector(".approval-edit")?.value === "git push -u origin vision/t-review" },
      { run: () => { document.querySelector(".approval-edit").value = "git push origin vision/t-review"; } },
      { run: () => [...document.querySelectorAll(".approval-card button")].find((b) => b.textContent === "Approve edited").click() },
      { wait: 150, check: "approving the edit sends it and the card goes", fn: () => window.__http.some((r) => r.path === "projects/p1/approvals/ap1" && r.body?.command === "git push origin vision/t-review") && !document.querySelector(".approval-card") },
      { frame: { type: "approval", project: "p1", approval: { id: "ap2", target: "coordinator", title: "Atlas", tool: "Bash", command: "sudo dnf install ripgrep", rule: "Bash(sudo:*)", action: "destructive", state: "pending", created: now } } },
      { wait: 100, check: "a new request arrives as a card", fn: () => document.querySelector(".approval-card .need-title")?.textContent === "Atlas asks to run" },
    ] },

    leash: { ...base, http: SCOUT_HTTP, steps: [
      openAtlas,
      // The leash is one switch, "asks first": Atlas asks first, so turning it off lets it go ahead on its own.
      { wait: 300, check: "the leash switch shows it asks first", fn: () => document.querySelector("#scout-rail .ask-row .toggle")?.getAttribute("aria-checked") === "true" },
      { run: () => document.querySelector("#scout-rail .ask-row .toggle").click() },
      { wait: 80, check: "the leash switch sends the leash", fn: () => window.__http.some((r) => r.body?.settings?.leash === "autonomous") },
      { run: () => { const r = document.querySelector("#scout-rail"); r.scrollTop = r.scrollHeight; } },
    ] },

    scoutneeds: { ...base, http: SCOUT_HTTP, steps: [
      openAtlas,
      { wait: 300, run: () => [...document.querySelectorAll(".need-card:not(.approval-card) button")].find((b) => b.textContent === "Land").click() },
      { wait: 150, check: "Land lands the thread and its card goes", fn: () => document.querySelectorAll(".need-card:not(.approval-card)").length === 1 },
      { run: () => [...document.querySelectorAll(".need-card:not(.approval-card) button")].find((b) => b.textContent === "Not now").click() },
      { wait: 100, check: "Not now puts a card away", fn: () => document.querySelectorAll(".need-card:not(.approval-card)").length === 0 },
      { run: () => document.querySelector(".timeline .ev").click() },
      { wait: 200, check: "an activity line opens its thread in the rail", fn: () => document.querySelector(".scout-rail .thread-panel h2")?.textContent === "Choose a tray icon style" },
    ] },

    scoutmemory: { ...base, http: SCOUT_HTTP, steps: [
      openAtlas,
      { wait: 300, check: "the memory shows without dates", fn: () => document.querySelector(".fact-text")?.textContent === "The user prefers to review changes before they land" },
      { type: [".fact-add", "Releases go out on Fridays"] },
      { key: [".fact-add", "Enter"] },
      { wait: 150, check: "a fact can be added", fn: () => [...document.querySelectorAll(".fact-text")].some((f) => f.textContent === "Releases go out on Fridays") },
      { run: () => document.querySelector(".fact .icon-btn").click() },
      { wait: 150, check: "and forgotten", fn: () => document.querySelectorAll(".fact").length === 2 && !document.querySelector(".fact-text").textContent.startsWith("The user prefers to review") },
    ] },

    newscout: { ...base, http: SCOUT_HTTP, onSend: (f, emit) => {
      if (f.type === "new" && f.project === "p9") emit({ type: "chat", ...CHATS[0], chat: "sc9", title: "Watch the homelab", project: "p9", opened: true });
    }, steps: [
      { wait: 300, run: () => document.querySelector(".chat-item.new-scout").click() },
      { wait: 100, run: () => [...document.querySelectorAll(".scout-foot .link")].find((b) => b.textContent === "More options").click() },
      { wait: 200, check: "New scout opens a sheet with a live buddy", fn: () => !!document.querySelector(".scout-sheet .scout-preview .buddy svg") },
      { check: "it suggests a name nobody has", fn: () => document.querySelector(".scout-name-input").value === "Pip" },
      { run: () => document.querySelector(".trait[data-trait=ears]").click() },
      { run: () => document.querySelectorAll(".hue")[4].click() },
      { type: [".scout-goal-input", "Watch my homelab and tell me when something looks off"] },
      { wait: 100, check: "the preview follows the picks", fn: () => document.querySelector(".scout-preview .buddy svg").innerHTML.includes("hsla(340,") },
      { run: () => document.querySelector(".scout-foot .primary").click() },
      { wait: 200, check: "Send scout opens its page", fn: () => document.querySelector(".scout-titles .scout-name")?.textContent === "Pip" },
      { check: "with its check-ins set", fn: () => window.__http.some((r) => r.path === "projects/p9/checkins" && r.body?.repeat === "daily" && r.body?.time === "08:00") },
      { run: () => document.querySelector(".chat-item.new-scout").click() },
      { wait: 100, run: () => [...document.querySelectorAll(".scout-foot .link")].find((b) => b.textContent === "More options").click() },
      { wait: 150, run: () => { document.querySelector(".scout-goal-input").value = "Keep the dependencies up to date, one tidy PR at a time"; document.querySelector(".trait[data-trait=sprout]").click(); } },
    ] },

    quickscout: { ...base, http: SCOUT_HTTP, onSend: (f, emit) => {
      if (f.type === "new" && f.project === "p9") emit({ type: "chat", ...CHATS[0], chat: "sc9", title: "Keep the server tests green", project: "p9", opened: true });
    }, steps: [
      { wait: 300, run: () => document.querySelector(".chat-item.new-scout").click() },
      { wait: 150, check: "New scout asks one thing", fn: () => document.querySelector(".quick-input")?.placeholder.startsWith("What should it do?") && !document.querySelector(".scout-name-input") },
      { type: [".quick-input", "Keep the server tests green, check every morning"] },
      { wait: 450, check: "one line says who it'll be and when it reports, from the words", fn: () => document.querySelector(".quick-line")?.textContent === "Pip · reports every morning at 8:00" },
      { run: () => document.querySelector(".quick-sheet .scout-foot .primary").click() },
      { wait: 250, check: "Send scout makes it in one call and opens its page", fn: () => window.__http.some((r) => r.path === "scouts" && r.body?.text?.startsWith("Keep the server"))
        && document.querySelector(".scout-titles .scout-name")?.textContent === "Pip" },
      { run: () => document.querySelector(".chat-item.new-scout").click() },
      { wait: 150, type: [".quick-input", "Every weekday at 7, give me the three tech stories worth reading"] },
      { wait: 450, run: () => {} },
    ] },

    needsinbox: { ...base, http: SCOUT_HTTP, steps: [
      { wait: 300, run: () => document.querySelector(".scouts-head .needs-link").click() },
      { wait: 200, check: "Needs you gathers every scout's asks, newest first", fn: () => [...document.querySelectorAll(".inbox-row")].map((c) => c.classList[1]).join() === "kind-approval,kind-question,kind-review" },
      { check: "each row says who wants what", fn: () => [...document.querySelectorAll(".inbox-lead b")].map((b) => b.textContent).join("|") === "Atlas wants to run|Juniper asked|Atlas finished “Add notifications for finished replies”" },
      { check: "finished work offers Apply, not Land", fn: () => document.querySelector(".kind-review .link-btn")?.textContent === "Apply" },
      { type: [".inbox-answer", "Keep the ones tagged latest"] },
      { run: () => [...document.querySelectorAll(".kind-question .link-btn")].find((b) => b.textContent === "Send").click() },
      { wait: 500, check: "an answer goes to its thread and the row leaves", fn: () => window.__http.some((r) => r.path === "projects/p2/threads/t-disk/message" && r.body?.text === "Keep the ones tagged latest")
        && document.querySelectorAll(".inbox-row").length === 2 },
    ] },

    noscouts: { ...base, projects: [], steps: [
      { wait: 300, check: "with no scouts the sidebar offers the first", fn: () => document.querySelector(".first-scout b")?.textContent === "Send your first scout" },
    ] },

    // Search finds what was said, not only titles: a hit opens its chat at the message.
    search: { ...base,
      history: { ...HISTORY, c6: [
        { role: "user", text: "what should the backup plan be for the NAS?" },
        { role: "assistant", text: "Snapshots every night on the NAS itself, and a weekly copy of the important shares to the offsite box over Tailscale with restic." },
        { role: "user", text: "ok, and how long do we keep them" },
        { role: "assistant", text: "Thirty nightly snapshots and twelve weekly copies is plenty for a homelab." },
      ] },
      http: { "GET search": (q) => q.q === "restic" ? [
        { chat: "c6", title: "Homelab backup plan", project: "", role: "assistant",
          snippet: "…and a weekly copy of the important shares to the offsite box over Tailscale with restic." },
      ] : [] },
      steps: [
        { wait: 300, type: ["#search", "restic"] },
        { wait: 500, check: "search finds what was said in a chat, not just its title", fn: () => document.querySelector(".chat-item.hit .chat-name")?.textContent === "Homelab backup plan" },
        { check: "the matching words are picked out", fn: () => document.querySelector(".hit-line mark")?.textContent === "restic" },
        { check: "the server was asked", fn: () => window.__http.some((h) => h.path === "search" && h.query?.q === "restic") },
        { click: ".chat-item.hit" },
        { wait: 400, check: "a hit opens its chat at that message", fn: () => document.querySelector("#chat-title").textContent === "Homelab backup plan" && document.querySelector(".msg.found")?.textContent.includes("restic") },
        { run: () => { const s = document.querySelector("#search"); s.value = "restic"; s.dispatchEvent(new Event("input", { bubbles: true })); } },
      ] },

    // Find in the open chat: every match marked, stepped through, and marked again when the reply redraws.
    find: { ...base, http: { "GET history": (q) => q.chat === "c1" && window.__replied ? [...HISTORY.c1,
      { role: "user", text: "run the test again" }, { role: "assistant", text: "Testing it now: the test passed.", duration_ms: 3100 }] : HISTORY[q.chat] ?? [] },
      steps: (() => {
        const bar = () => document.querySelector(".find-bar");
        const hits = () => [...document.querySelectorAll("#transcript mark.find-hit")];
        const ctrlF = () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "f", ctrlKey: true, bubbles: true, cancelable: true }));
        const press = (key, shiftKey = false) => document.querySelector(".find-input").dispatchEvent(new KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true }));
        let first;
        return [
          { wait: 300, run: ctrlF },
          { check: "Ctrl+F opens the find bar, ready to type", fn: () => !bar().hidden && document.activeElement === document.querySelector(".find-input") },
          { type: [".find-input", "test"] },
          { check: "every match in the chat is marked and counted", fn: () => hits().length === 4 && document.querySelector(".find-count").textContent === "1 of 4" },
          { check: "the tool rows and buttons are left alone", fn: () => !document.querySelector(".actions mark, .steps mark, button mark") },
          { run: () => { first = document.querySelector("mark.find-hit.current"); press("Enter"); } },
          { check: "Return steps to the next match", fn: () => document.querySelector(".find-count").textContent === "2 of 4" && hits()[1].classList.contains("current") },
          { run: () => press("Enter", true) },
          { check: "Shift+Return steps back", fn: () => document.querySelector(".find-count").textContent === "1 of 4" && document.querySelector("mark.find-hit.current") === first },
          { run: () => press("ArrowUp") },
          { check: "and past the first goes round to the last", fn: () => document.querySelector(".find-count").textContent === "4 of 4" },
          { frame: { type: "chat", ...CHATS[0], busy: true } },
          { frame: { type: "start", chat: "c1", text: "run the test again" } },
          { frame: { type: "delta", chat: "c1", text: "Testing it now: the test passed." } },
          { wait: 300, check: "a streaming reply is marked as it comes in", fn: () => hits().length === 7 && document.querySelector(".msg.streaming mark.find-hit") !== null },
          { check: "and the current match stays put", fn: () => document.querySelector(".find-count").textContent === "4 of 7" },
          { run: () => { window.__replied = true; } },
          { frame: { type: "done", chat: "c1", text: "Testing it now: the test passed.", error: "", busy: false, duration_ms: 3100 } },
          { wait: 400, check: "the marks come back after the history is fetched again", fn: () => !document.querySelector(".msg.streaming") && hits().length === 7 && /^\d of 7$/.test(document.querySelector(".find-count").textContent) },
          { type: [".find-input", "nothing like this"] },
          { check: "no match says so", fn: () => document.querySelector(".find-count").textContent === "No matches" && hits().length === 0 },
          { run: () => press("Escape") },
          { check: "Escape closes the bar and takes the marks away", fn: () => bar().hidden && !document.querySelector("mark.find-hit") },
          { check: "without stopping anything", fn: () => !sentOf("cancel").length },
          { run: ctrlF },
          { type: [".find-input", "drop"] },
          { check: "a search again finds the word inside code too", fn: () => hits().length >= 1 && document.querySelector(".md code mark.find-hit") !== null },
          { type: [".find-input", "test"] },
          { run: () => press("Enter") },
        ];
      })() },

    // The context ring opens a popover with the token counts and Compact; the server shows it under way like a turn.
    compact: { ...base, chats: [{ ...CHATS[0], context: { tokens: 152_000, window: 200_000 } }, ...CHATS.slice(1)],
      http: { "GET history": (q) => q.chat === "c1" && window.__compacted ? [...HISTORY.c1,
        { role: "note", text: "Compacted the conversation (152k tokens) into a summary; Vision carries on from it in a fresh session.", compacted: true, duration_ms: 8200 }]
        : HISTORY[q.chat] ?? [] },
      onSend: (f, emit) => {
        if (f.type !== "compact") return;
        emit({ ...CHATS[0], busy: true, context: { tokens: 152_000, window: 200_000 } });
        emit({ type: "start", chat: "c1", text: "", note: "Compacting the conversation" });
        emit({ type: "status", chat: "c1", tool: "", label: "Summarising the conversation…" });
        setTimeout(() => {
          window.__compacted = true;
          emit({ type: "done", chat: "c1", text: "", error: "", busy: false, compacted: true, duration_ms: 8200 });
          emit({ ...CHATS[0], busy: false, context: { tokens: 3_900, window: 200_000 } });
        }, 1500);
      },
      steps: [
        { wait: 300, check: "a full context shows amber", fn: () => document.querySelector("#context").classList.contains("warn") && document.querySelector("#context-text").textContent === "76%" },
        { click: "#context" },
        { wait: 150, check: "the ring opens a popover with the token counts", fn: () => !document.querySelector("#popover").hidden && document.querySelector(".context-pop-tokens")?.textContent === "152,000 of 200,000 tokens" },
        { check: "and Compact stands out from 70%", fn: () => document.querySelector(".context-compact")?.classList.contains("primary") && !document.querySelector(".context-compact").disabled },
        { click: ".context-compact" },
        { wait: 100, check: "Compact asks the server to compact this chat", fn: () => sentOf("compact").length === 1 && sentOf("compact")[0].chat === "c1" && document.querySelector("#popover").hidden },
        { check: "and it shows under way", fn: () => [...document.querySelectorAll(".note-text")].some((n) => n.textContent === "Compacting the conversation") && document.querySelector(".msg.streaming .status")?.textContent.includes("Summarising") },
        { click: "#context" },
        { wait: 150, check: "while it runs Compact waits", fn: () => document.querySelector(".context-compact")?.disabled === true },
        { click: "#context" },
        { wait: 1600, check: "afterwards a note marks the place, after the history refetch", fn: () => !document.querySelector(".msg.streaming") && [...document.querySelectorAll(".note-text")].some((n) => n.textContent.startsWith("Compacted the conversation")) },
        { check: "and the ring shows the context emptied", fn: () => document.querySelector("#context-text").textContent === "2%" && !document.querySelector("#context").classList.contains("warn") },
        { check: "nothing pinged as a reply", fn: () => !window.__notified.length },
        { click: "#context" },
        { wait: 150, check: "with room to spare Compact is a plain button", fn: () => document.querySelector(".context-compact")?.classList.contains("bordered") && !document.querySelector(".context-compact").classList.contains("primary") },
      ] },

    // An older Vision has no compact: the popover says so instead of offering it.
    compactold: { ...base, compact: false, steps: [
      { wait: 300, click: "#context" },
      { wait: 150, check: "an older Vision gets no Compact button", fn: () => !document.querySelector(".context-compact") && document.querySelector(".context-pop")?.textContent.includes("needs a newer Vision") },
    ] },

    // Files from the file manager come as a native drop (paths); a screenshot pasted comes from the native clipboard.
    drop: { ...base, clipboard: [{ name: "pasted image.png", path: "/home/me/.local/share/vision/uploads/x-pasted-image.png" }], steps: [
      { wait: 300, run: () => window.__emit("tauri://drag-enter", { paths: ["/home/me/Pictures/chart.png"], position: { x: 600, y: 400 } }) },
      { wait: 50, check: "dragging files over the window says where they'll go", fn: () => !document.querySelector("#drop").hidden },
      { run: () => window.__emit("tauri://drag-drop", { paths: ["/home/me/Pictures/chart.png", "/home/me/notes.txt"], position: { x: 600, y: 400 } }) },
      { wait: 300, check: "files dropped from the file manager are attached", fn: () => [...document.querySelectorAll(".attachment span")].map((s) => s.textContent).join() === "chart.png,notes.txt" && document.querySelector("#drop").hidden },
      { check: "a dropped picture shows a preview", fn: () => document.querySelector(".attachment img")?.naturalWidth > 0 },
      { run: () => {
        const dt = new DataTransfer();
        dt.setData("image/png", "x");  // a screenshot: a picture on the clipboard, no file the page can read
        document.querySelector("#input").dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
      } },
      { wait: 300, check: "a pasted screenshot is attached", fn: () => [...document.querySelectorAll(".attachment span")].some((s) => s.textContent === "pasted image.png") },
      { run: () => {
        const dt = new DataTransfer();
        dt.setData("text/plain", "just some words");
        const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
        document.querySelector("#input").dispatchEvent(ev);
        window.__textPasteKept = !ev.defaultPrevented;
      } },
      { wait: 50, check: "plain text still pastes as text", fn: () => window.__textPasteKept === true && document.querySelectorAll(".attachment").length === 3 },
    ] },

    // Double-click a chat's name (in the sidebar or above the chat) to rename it; the server tells every app.
    rename: { ...base, onSend: (f, emit) => {
      const c = CHATS.find((x) => x.chat === f.chat);
      if (f.type === "rename" && c) setTimeout(() => emit({ ...c, title: f.title }), 20);
    }, steps: (() => {
      const row = (name) => [...document.querySelectorAll("#chats .chat-item")].find((i) => i.textContent.includes(name));
      const dbl = (node) => node.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
      return [
        { wait: 300, run: () => dbl(row("Fix the flaky")) },
        { wait: 100, check: "double-clicking a chat in the sidebar makes its name a field", fn: () => document.activeElement?.classList.contains("rename-input")
          && document.querySelector("#chats .chat-item.renaming .rename-input")?.value === "Fix the flaky upload test" },
        { type: [".rename-input", "Upload race"] },
        { key: [".rename-input", "Enter"] },
        { wait: 100, check: "Return renames it on the server", fn: () => sentOf("rename")[0]?.chat === "c1" && sentOf("rename")[0]?.title === "Upload race" },
        { check: "and the row and title show the new name", fn: () => !document.querySelector(".rename-input") && !!row("Upload race") && document.querySelector("#chat-title").textContent === "Upload race" },
        { run: () => dbl(document.querySelector("#chat-title")) },
        { wait: 100, check: "the title above the chat renames it too", fn: () => document.querySelector("#chat-title .rename-input")?.value === "Upload race" },
        { type: ["#chat-title .rename-input", "Something else"] },
        { key: ["#chat-title .rename-input", "Escape"] },
        { wait: 100, check: "Escape puts the old name back and sends nothing", fn: () => sentOf("rename").length === 1 && document.querySelector("#chat-title").textContent === "Upload race" },
        { check: "nor stops anything", fn: () => !sentOf("cancel").length },
        { run: () => dbl(row("Homelab backup")) },
        { wait: 100, type: [".rename-input", "Backups: NAS and offsite"] },
        { frame: { type: "chat", ...CHATS[1], busy: false } },  // another chat changing redraws the list
        { wait: 100, check: "a field being typed in survives the list redrawing", fn: () => document.activeElement?.classList.contains("rename-input") && document.activeElement.value === "Backups: NAS and offsite" },
      ];
    })() },

    // Hover a sent message, Edit: the edited message goes to a new chat branched from that point.
    edit: { ...base,
      history: { ...HISTORY, "c1-edit": [
        { role: "user", text: "why does test_upload fail about one run in three on CI only?" },
        { role: "assistant", text: "On CI the runner is slower, so the hourly sweep catches the upload more often. The same fix applies: skip files younger than a minute.", duration_ms: 21300 },
      ] },
      onSend: (f, emit) => {
        if (f.type !== "branch") return;
        const reply = "On CI the runner is slower, so the hourly sweep catches the upload more often. The same fix applies: skip files younger than a minute.";
        const opened = { ...CHATS[0], chat: "c1-edit", title: "Fix the flaky upload test (edit)", updated: now, suggestion: "", context: null };
        emit({ ...opened, opened: true });
        emit(opened);
        setTimeout(() => emit({ type: "start", chat: "c1-edit", text: f.text }), 30);
        setTimeout(() => emit({ type: "delta", chat: "c1-edit", text: reply }), 80);
        setTimeout(() => emit({ type: "done", chat: "c1-edit", text: reply, error: "", busy: false, duration_ms: 21300 }), 140);
      },
      steps: [
        { wait: 400, check: "a sent message offers Edit", fn: () => !!document.querySelector(".msg.user .act.edit") },
        { check: "a reply doesn't", fn: () => !document.querySelector(".msg.assistant .act.edit") },
        { run: () => document.querySelector(".msg.user .act.edit").click() },
        { wait: 100, check: "Edit opens the message in place", fn: () => document.querySelector(".msg.user .edit-input")?.value === "why does test_upload fail about one run in five?"
          && document.querySelector(".msg.user .bubble").hidden && document.activeElement?.classList.contains("edit-input") },
        { key: [".edit-input", "Escape"] },
        { wait: 50, check: "Escape puts it back as it was", fn: () => !document.querySelector(".edit-box") && !document.querySelector(".msg.user .bubble").hidden && !sentOf("cancel").length },
        { run: () => document.querySelector(".msg.user .act.edit").click() },
        { wait: 100, type: [".edit-input", "why does test_upload fail about one run in three on CI only?"] },
        { key: [".edit-input", "Enter"] },
        { wait: 50, check: "Return sends it as a branch of this chat from that message", fn: () => {
          const b = sentOf("branch")[0];
          return b?.chat === "c1" && b.at === 0 && b.was === "why does test_upload fail about one run in five?" && b.text === "why does test_upload fail about one run in three on CI only?";
        } },
        { check: "nothing goes to the chat itself", fn: () => !sentOf("message").length },
        { wait: 300, check: "the new chat opens with the edited message and its reply", fn: () => document.querySelector("#chat-title").textContent === "Fix the flaky upload test (edit)"
          && document.querySelector(".msg.user .bubble")?.textContent === "why does test_upload fail about one run in three on CI only?"
          && document.querySelector(".msg.assistant")?.textContent.includes("the runner is slower") },
        { check: "its reply keeps its time after the history is fetched again", fn: () => document.querySelector(".msg.assistant .took")?.textContent === "21.3s"
          && window.__http.some((h) => h.path === "history" && h.query?.chat === "c1-edit") },
        { check: "and the sidebar lists it beside the chat it came from", fn: () => [...document.querySelectorAll("#chats .chat-name")].map((n) => n.textContent).filter((t) => t.startsWith("Fix the flaky")).length === 2 },
        { run: () => [...document.querySelectorAll("#chats .chat-item")].find((i) => i.textContent.includes("upload test") && !i.textContent.includes("(edit)")).click() },
        { wait: 300, check: "the chat it came from is as it was", fn: () => document.querySelector(".msg.user .bubble")?.textContent === "why does test_upload fail about one run in five?" && document.querySelectorAll(".msg").length === 2 },
        { run: () => document.querySelector(".msg.user .act.edit").click() },
        { wait: 100, type: [".edit-input", "why does test_upload fail about one run in five, and only since Tuesday?"] },
      ] },

    collapsed: { ...base, steps: [{ wait: 300, click: ".side-top [data-action=toggle_sidebar]" }, { wait: 400, check: "the sidebar folds away", fn: () => document.querySelector(".main").getBoundingClientRect().left < 20 }] },

    usage: { ...base, http: USAGE_HTTP, steps: [
      { wait: 300, click: "#context" },
      { wait: 100, click: ".context-usage" },
      { wait: 200, check: "the context ring's popover opens Usage", fn: () => !document.querySelector("#settings").hidden && document.querySelector("#settings-card h2")?.textContent === "Usage" },
      { check: "it asks for every provider, as the chat on screen", fn: () => window.__http.some((r) => r.path === "usage" && r.query?.provider === "all" && r.query?.chat === "c1") },
      { check: "a card per provider, in the house order", fn: () => [...document.querySelectorAll(".usage-head b")].map((b) => b.textContent).join() === "Claude,Codex,Grok" },
      { check: "a window's name is short, with its percent", fn: () => { const b = document.querySelector(".usage-bar"); return b.querySelector(".usage-bar-title").textContent === "Session" && b.querySelector(".usage-pct").textContent === "34%"; } },
      { check: "when it resets sits beneath the bar", fn: () => document.querySelector(".usage-bar .usage-resets").textContent.startsWith("Resets in 2h") },
      { check: "amber from 75%, red from 90%", fn: () => { const bars = [...document.querySelectorAll(".usage-bar")]; return bars[1].classList.contains("warn") && bars[2].classList.contains("full") && !bars[0].matches(".warn,.full"); } },
      { check: "the plan and a snapshot's time show", fn: () => document.querySelector(".usage-plan")?.textContent === "Max" && document.querySelectorAll(".usage-asof").length === 1 },
      { check: "a provider that can't say says why", fn: () => document.querySelector(".usage-error")?.textContent.includes("grok login") },
      { check: "banked resets get a row each", fn: () => document.querySelectorAll(".usage-reset").length === 2 },
      { run: () => document.querySelector("#settings-card .set-head .icon-btn:last-child").click() },
      { wait: 80, click: "[data-action=settings]" },
      { wait: 100, click: ".set-nav-item[data-page=accounts]" },
      { wait: 150, check: "Settings has a way into Usage", fn: () => [...document.querySelectorAll(".set-section h3")].some((h) => h.textContent === "Usage") },
      { run: () => [...document.querySelectorAll(".set-row")].find((r) => r.textContent.includes("Subscription usage")).querySelector("button").click() },
      { wait: 150, check: "which opens it again, refreshed", fn: () => document.querySelector("#settings-card h2")?.textContent === "Usage" && window.__http.filter((r) => r.path === "usage").length === 2 },
      { run: () => document.querySelector(".usage-use").click() },
      { wait: 80, check: "Use asks once more first", fn: () => document.querySelector(".usage-use").textContent === "Use now" && !!document.querySelector(".usage-sure") && !window.__http.some((r) => r.path === "usage/reset") },
      { run: () => document.querySelector(".usage-use").click() },
      { wait: 150, check: "then spends exactly that reset", fn: () => window.__http.some((r) => r.path === "usage/reset" && r.body?.provider === "claude" && r.body?.id === "g1") },
      { check: "and says how it went, with the fresh numbers", fn: () => document.querySelector(".usage-outcome.ok")?.textContent.includes("Session cleared") && document.querySelector(".usage-pct").textContent === "0%" },
    ] },

    scheduled: { ...base, http: SCHED_HTTP(), steps: [
      { wait: 300, click: ".side-row[data-action=scheduled]" },
      { wait: 200, check: "the sidebar opens Coming up", fn: () => !document.querySelector("#scheduled").hidden && document.querySelector("#chat-title").textContent === "Coming up" && document.querySelector(".side-row[data-action=scheduled]").classList.contains("active") },
      { check: "the header counts them", fn: () => document.querySelector("#chat-folder").textContent === "2 reminders · 2 scheduled prompts" },
      { check: "reminders and prompts are listed apart, scout check-ins left to their scouts", fn: () => [...document.querySelectorAll(".sched-group h3")].map((h) => h.textContent).join() === "Reminders,Scheduled prompts" && !document.body.textContent.includes("Atlas's status report") },
      { check: "each says how it repeats and when it's next", fn: () => { const r = document.querySelector(".sched-item[data-id=s2]"); return r.textContent.includes("Reminder · Once, Nov 2 at 6:00 PM") && r.querySelector(".sched-next").textContent.startsWith("Next ")
        && document.querySelector(".sched-item[data-id=s4]").textContent.includes("Every 2 hours"); } },
      { check: "a prompt says which chat it goes to", fn: () => [...document.querySelectorAll(".sched-item")].some((r) => r.textContent.includes("Weekdays at") && r.textContent.includes("in “Fix the flaky upload test”")) },
      { check: "a paused one sits last, marked paused", fn: () => { const rows = [...document.querySelectorAll(".sched-group")[1].querySelectorAll(".sched-item")]; return rows.at(-1).classList.contains("off") && rows.at(-1).textContent.includes("Paused"); } },
      { run: () => { window.__sched.push({ id: "s9", title: "Water the plants", prompt: "Water the plants", kind: "reminder", repeat: "daily", time: "19:00", enabled: true, next_run: now + 5 * 3600 }); } },
      { frame: { type: "scheduled" } },
      { wait: 150, check: "a scheduled frame brings in a new one", fn: () => document.body.textContent.includes("Water the plants") && document.querySelector("#chat-folder").textContent.startsWith("3 reminders") },
      { run: () => document.querySelector(".sched-item[data-id=s2] .sched-run").click() },
      { wait: 100, check: "Run now runs it", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "scheduled/s2/run") },
      { run: () => document.querySelector(".sched-item[data-id=s9] .sched-cancel").click() },
      { wait: 150, check: "Cancel takes it off the computer and the list", fn: () => window.__http.some((r) => r.method === "DELETE" && r.path === "scheduled/s9") && !document.querySelector(".sched-item[data-id=s9]") },
      { run: () => document.querySelector(".sched-item[data-id=s3] .toggle").click() },
      { wait: 100, check: "the switch resumes a paused one", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "scheduled" && r.body?.id === "s3" && r.body?.enabled === true) },
      { check: "a reminder can't be added with no words", fn: () => document.querySelector(".sched-add").disabled },
      { type: [".sched-words", "Take the bins out"] },
      { run: () => { const r = document.querySelector(".sched-repeat"); r.value = "weekly"; r.dispatchEvent(new Event("change")); const d = document.querySelector(".sched-day"); d.value = "2"; document.querySelector(".sched-time").value = "20:30"; } },
      { run: () => document.querySelector(".sched-add").click() },
      { wait: 150, check: "the form adds a weekly reminder", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "scheduled" && r.body?.kind === "reminder" && r.body?.repeat === "weekly" && r.body?.weekday === 2 && r.body?.time === "20:30" && r.body?.prompt === "Take the bins out") },
      { check: "and it shows, with the box cleared", fn: () => document.body.textContent.includes("Wednesdays at") && document.querySelector(".sched-words").value === "" },
      { run: () => document.querySelectorAll(".sched-kinds button")[1].click() },
      { wait: 50, check: "a prompt picks its chat, the last one open first", fn: () => !document.querySelector(".sched-chat").hidden && document.querySelector(".sched-chat").value === "c1" && document.querySelector(".sched-words").placeholder === "What should Vision do?" },
      { run: () => { const r = document.querySelector(".sched-repeat"); r.value = "daily"; r.dispatchEvent(new Event("change")); } },
      { type: [".sched-words", "Check the nightly build and say if it failed"] },
      { run: () => document.querySelector(".sched-add").click() },
      { wait: 150, check: "the form sends a prompt into that chat", fn: () => window.__http.some((r) => r.method === "POST" && r.path === "scheduled" && r.body?.kind === "task" && r.body?.chat === "c1" && r.body?.repeat === "daily" && !("date" in r.body)) },
      { run: () => document.querySelectorAll(".sched-kinds button")[0].click() },
    ] },

    noscheduled: { ...base, http: { "GET scheduled": [] }, steps: [
      { wait: 300, click: ".side-row[data-action=scheduled]" },
      { wait: 200, check: "with nothing set, it says how to set something", fn: () => document.querySelector("#scheduled .board-empty h3")?.textContent === "Nothing coming up" && document.querySelector("#chat-folder").textContent === "Nothing coming up" },
    ] },
    // Dictation (mic.ts): hold, let go, the words land in the composer unsent. The scene ends idle with them there.
    mic: { ...base, voiceOn: true, transcribe: true, mic: {}, heard: "remind me to water the plants at six", steps: [
      { wait: 300, check: "the mic button sits by the paperclip, ready", fn: () => { const b = mic(); return !b.hidden && !b.classList.contains("off") && b.title.includes("Hold to dictate") && b.previousElementSibling?.id === "attach"; } },
      { type: ["#input", "Note:"] },
      { run: () => micPress() },
      { wait: 900, check: "holding records: a pulsing dot, the level and the time", fn: () => mic().classList.contains("recording") && /^0:0\d$/.test(mic().textContent.trim())
        && Number(getComputedStyle(document.querySelector(".mic-level")).getPropertyValue("--level")) > 0 },
      { run: () => micRelease() },
      { wait: 30, check: "letting go shows Transcribing…", fn: () => mic().classList.contains("transcribing") && mic().textContent.includes("Transcribing…") },
      { wait: 500, check: "the words land after what was typed, a space apart", fn: () => document.querySelector("#input").value === "Note: remind me to water the plants at six" },
      { check: "nothing was sent, and Send is ready for it", fn: () => !window.__sent.some((f) => f.type === "message") && !document.querySelector("#composer").classList.contains("blank") },
      { check: "the recording is a 16 kHz mono 16-bit WAV", fn: () => { const w = window.__transcribed[0]; return w.riff === "RIFF" && w.rate === 16000 && w.channels === 1 && w.bits === 16 && w.bytes > 16000; } },
      { check: "back to the plain button", fn: () => !mic().classList.contains("recording") && !mic().classList.contains("transcribing") && !mic().textContent.trim() },
      { run: () => window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", ctrlKey: true, shiftKey: true, bubbles: true })) },
      { wait: 600, check: "holding Ctrl+Shift+Space records too", fn: () => mic().classList.contains("recording") },
      { run: () => window.dispatchEvent(new KeyboardEvent("keyup", { key: " ", code: "Space", ctrlKey: true, shiftKey: true, bubbles: true })) },
      { wait: 500, check: "and letting go of it transcribes", fn: () => window.__transcribed.length === 2 && document.querySelector("#input").value.endsWith("six remind me to water the plants at six") },
      { run: () => { micPress(); micRelease(); } },
      { wait: 600, check: "a click keeps recording until the next", fn: () => mic().classList.contains("recording") },
      { run: () => document.querySelector("#input").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })) },
      { wait: 100, check: "Esc throws the recording away", fn: () => !mic().classList.contains("recording") && window.__transcribed.length === 2 },
      { check: "and doesn't stop a reply", fn: () => !window.__sent.some((f) => f.type === "cancel") },
    ] },
    micrecording: { ...base, voiceOn: true, transcribe: true, mic: {}, steps: [
      { wait: 300, type: ["#input", "Draft: "] },
      { run: () => micPress() },
      { wait: 1600, check: "recording shows the dot, the level and the time", fn: () => mic().classList.contains("recording") && mic().textContent.trim() === "0:01" },
    ] },
    mictranscribing: { ...base, voiceOn: true, transcribe: true, mic: {}, heard: null, steps: [
      { wait: 300, run: () => micPress() },
      { wait: 800, run: () => micRelease() },
      { wait: 100, check: "while the computer works it says Transcribing…", fn: () => mic().classList.contains("transcribing") && mic().title === "Transcribing…" },
      { run: () => micPress() },
      { wait: 100, check: "and a press then does nothing", fn: () => mic().classList.contains("transcribing") && window.__transcribed.length === 1 },
    ] },
    micoff: { ...base, voiceOn: false, transcribe: true, mic: {}, steps: [
      { wait: 300, check: "with the voice off the mic is greyed, and says why", fn: () => !mic().hidden && mic().classList.contains("off") && mic().title.includes("turn it on in Settings") },
      { run: () => micPress() },
      { wait: 100, check: "a press says why instead of recording", fn: () => !mic().classList.contains("recording") && document.querySelector("#toast").textContent.includes("voice on") },
      { run: () => micRelease() },
      { frame: { type: "voice_power", on: true } },
      { wait: 100, check: "the voice coming on makes it ready", fn: () => !mic().classList.contains("off") },
      { frame: { type: "voice_power", on: false } },
      { wait: 100, check: "and going off greys it again", fn: () => mic().classList.contains("off") },
    ] },
    micmissing: { ...base, voiceOn: true, transcribe: false, mic: {}, steps: [
      { wait: 300, check: "no Whisper on the computer: no mic button", fn: () => mic().hidden && mic().getBoundingClientRect().width === 0 },
      { run: () => window.dispatchEvent(new KeyboardEvent("keydown", { key: " ", code: "Space", ctrlKey: true, shiftKey: true, bubbles: true })) },
      { wait: 300, check: "and the shortcut does nothing", fn: () => !mic().classList.contains("recording") && !window.__transcribed.length },
    ] },
    micrefused: { ...base, voiceOn: true, transcribe: true, mic: {}, transcribeError: "The voice is off on the computer.", steps: [
      { wait: 300, run: () => micPress() },
      { wait: 800, run: () => micRelease() },
      { wait: 200, check: "a refusal says why and the button is ready again", fn: () => document.querySelector("#toast").textContent === "Couldn't transcribe: The voice is off on the computer."
        && !mic().classList.contains("transcribing") && document.querySelector("#input").value === "" },
    ] },
    micdenied: { ...base, voiceOn: true, transcribe: true, mic: { denied: true }, steps: [
      { wait: 300, run: () => micPress() },
      { wait: 200, check: "no microphone permission says so", fn: () => document.querySelector("#toast").textContent.startsWith("Vision isn't allowed to use the microphone.") && !mic().classList.contains("recording") },
    ] },
  };

  function rowOf(label) { return [...document.querySelectorAll(".set-row")].find((r) => r.querySelector(".set-text > div")?.textContent.startsWith(label)); }
  function buttonIn(label, text) { return [...rowOf(label)?.querySelectorAll("button") ?? []].find((b) => b.textContent === text); }
  function mic() { return document.querySelector("#mic"); }
  function press(init) { window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init })); }
  /** The key caps on the Shortcuts row for `what`, space-separated (an "or" between two combos shows too). */
  function capsOf(what) {
    const row = [...document.querySelectorAll(".set-row")].find((r) => r.querySelector(".set-text div")?.textContent === what);
    return row ? [...row.querySelectorAll(".keycap")].map((k) => k.textContent).join(" ") : "";
  }
  function micPress() { mic().dispatchEvent(new PointerEvent("pointerdown", { button: 0, pointerId: 1, bubbles: true, cancelable: true })); }
  function micRelease() { mic().dispatchEvent(new PointerEvent("pointerup", { button: 0, pointerId: 1, bubbles: true })); }
})();
