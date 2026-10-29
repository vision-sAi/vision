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
    chat("c1", "Fix the flaky upload test", 600, { suggestion: "Run the whole suite to be sure" }),
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
  for (const pid of ["p2", "p3", "p4"]) { SCOUT_HTTP[`GET projects/${pid}/activity`] = { events: [] }; SCOUT_HTTP[`GET projects/${pid}/memory`] = { facts: [] }; }
  const openAtlas = { wait: 300, run: () => [...document.querySelectorAll(".chat-item.scout")].find((x) => x.textContent.includes("Atlas")).click() };
  const sentOf = (type) => window.__sent.filter((f) => f.type === type);
  const PROVIDERS = () => [
    { name: "claude", label: "Claude", product: "Claude Code", note: "via Claude Code · Claude Subscription", enabled: true, installed: true, signed_in: true, detail: "logged in", install_cmd: "curl …", install_url: "", login_cmd: ["claude", "auth", "login"] },
    { name: "codex", label: "Codex", product: "Codex", note: "via Codex CLI · ChatGPT Subscription", enabled: true, installed: true, signed_in: false, detail: "", install_cmd: "npm i -g @openai/codex", install_url: "", login_cmd: ["codex", "login"] },
    { name: "grok", label: "Grok", product: "Grok", note: "via Grok CLI · Grok Subscription", enabled: true, installed: false, signed_in: false, detail: "", install_cmd: "", install_url: "https://x.ai/cli", login_cmd: ["grok", "login"] },
  ];

  window.SCENES = {
    chat: { ...base, steps: [
      { wait: 300, check: "a pinned chat heads the list", fn: () => document.querySelector("#chats .group:not(.scouts-head)")?.textContent === "Pinned" && document.querySelector("#chats .group:not(.scouts-head) + .chat-item .chat-name")?.textContent === "Compare NAS drives" },
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

    models: { ...base, steps: [
      { wait: 300, click: "#model" },
      { wait: 200, check: "the model menu opens", fn: () => !document.querySelector("#popover").hidden },
      { run: () => document.querySelectorAll("#popover .menu-item")[1].click() },
      { wait: 100, check: "picking a model switches the chat", fn: () => sentOf("model")[0]?.model === "sonnet" },
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
      { wait: 200, check: "Settings opens", fn: () => !document.querySelector("#settings").hidden },
      { check: "the default model shows", fn: () => document.querySelector(".set-section .chip-btn")?.textContent.includes("Opus 5.5") },
      { check: "the voice picker lists voices", fn: () => document.querySelectorAll("#settings select option").length === 2 },
    ] },

    agents: { ...base, http: AGENT_HTTP, asleep: [{ from: now - 9 * 3600, to: now - 2 * 3600 }], steps: [
      { wait: 300, click: ".side-row[data-action=agents]" },
      { wait: 200, check: "the board's columns each carry their colour", fn: () => [...document.querySelectorAll(".lane")].map((l) => [...l.classList].find((c) => c.startsWith("tone-")) ?? "wide").join() === "tone-ask,tone-ready,tone-busy,tone-idle,wide" },
      { wait: 200, check: "the board groups threads by what they need", fn: () => [...document.querySelectorAll(".lane-head h3")].map((h) => h.textContent).join(",") === "Needs you,Ready for review,Working,Idle,Scheduled" },
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
      { check: "the Scouts heading adds up what each scout's row says", fn: () => document.querySelector(".scouts-head b")?.textContent === "3 need you" && document.querySelector(".chat-item.scout .scout-line")?.textContent === "3 need you" },
      openAtlas,
      { wait: 300, check: "a scout's page shows its name and goal", fn: () => document.querySelector(".scout-titles .scout-name")?.textContent === "Atlas" && document.querySelector(".scout-leash")?.textContent === "Check-ins" && !!document.querySelector(".scout-goal")?.textContent },
      { check: "the rail has its four sections", fn: () => [...document.querySelectorAll(".rail-head h3")].map((h) => h.textContent).join(",") === "Needs you,Activity,Leash,Remembers" },
      { check: "a thread report shows as a card", fn: () => document.querySelectorAll(".report-card .report-list li").length === 3 },
      { check: "what needs you is listed", fn: () => document.querySelectorAll(".need-card:not(.approval-card)").length === 2 },
      { check: "today's activity is listed", fn: () => document.querySelectorAll(".timeline .ev").length === 5 },
      { check: "the leash shows its setting", fn: () => document.querySelector(".leash-seg .on")?.textContent === "Check-ins" },
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
      { check: "and the button says it's under way", fn: () => document.querySelector(".checkin-now").textContent === "Checking in…" },
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
      { wait: 300, run: () => document.querySelectorAll(".leash-seg button")[2].click() },
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
      { wait: 150, run: () => { document.querySelector(".scout-goal-input").value = "Keep the dependencies up to date, one tidy PR at a time"; document.querySelector(".trait[data-trait=sprout]").click(); } },
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

    collapsed: { ...base, steps: [{ wait: 300, click: ".side-top [data-action=toggle_sidebar]" }, { wait: 400, check: "the sidebar folds away", fn: () => document.querySelector(".main").getBoundingClientRect().left < 20 }] },
  };
})();
