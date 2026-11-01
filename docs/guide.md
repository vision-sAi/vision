# Vision guide

The full reference: every command, the voice pipeline, the providers and the optional pieces.
For installing, start with the [README](../README.md).

## Commands

```bash
vision                # text chat (Ctrl-D or /quit to leave)
vision --speak        # text chat, replies read aloud
vision talk           # the chat with /talk on: hands-free; say "goodbye" to exit
vision talk --ptt     # push-to-talk (Enter on an empty box to start/send) for noisy rooms
vision ask "what's the tallest building in Dublin?"
vision ask -m gpt-6-astra --effort ultra "review this repository"
vision say "Good morning."
vision say --out hello.wav "Saved to a file."
vision listen         # transcribe one utterance from the mic
vision listen -f clip.wav
vision voices --preview   # list and audition the voices
vision voice design jarvis   # invent a voice from a description (built-in: jarvis, narrator, friday)
vision usage          # subscription rate-limit windows used
vision --version      # Vision plus the Claude Code, Codex and Grok CLI versions
vision update         # update those CLIs with their own updaters (or: vision update grok)
vision doctor         # check CLI versions, logins, usage, the background service, models, GPU, audio devices
vision doctor --fix   # ...and mend what it can (install, update or log in to a CLI, fetch models, make the voice, restart the service), asking first (-y: don't ask)
vision voice check    # can you hear it, can it hear you, does its name wake it
vision onboard        # the first-run setup again: providers, your name, auto or plan mode, voice, phone
vision pair           # the pairing code for the Vision Remote app, again
vision service install  # keep `vision serve` running in the background (status, remove)
vision sessions       # earlier conversations (list, grep <words>, export <id>)
vision sessions grep tailscale funnel   # every conversation where both words were said, with the lines
vision sessions export 7eb17c64 -o ~/notes/   # one conversation as markdown
vision default        # pick and save the default model + effort
vision --install-completion   # Tab completion for the commands and options (bash, zsh, fish); --show-completion prints the script
vision config --edit  # edit ~/.config/vision/config.toml
vision config set brain.mode plan   # change one setting in place (comments kept); vision config get brain.mode reads it
```

`vision talk` is the ordinary chat opened with `/talk` on (`/talk ptt` for push-to-talk), so you can
also type a message and press Enter at any time instead of speaking.
While a voice conversation is on, typed input goes to the same conversation model as speech, so one
model answers the whole chat; enabling speech playback alone does not turn a typed message into a
voice conversation.
The chat is a full-screen view in the apps' look: the conversation's title and folder are pinned at the top
once it has a title, the conversation scrolls under them, and the composer, a rounded card edged in violet,
stays pinned to the bottom. Its bottom border carries the mode (`⏵⏵ auto` in amber, `⏸ plan` in green) and
the folder on the left and the model on the right; the status row under it leads with up to three of your
scouts, those waiting on you first (`● Atlas needs you`, `● Moss landed something`; asked of the local
`vision serve` every 20 seconds, nothing when none runs). The composer grows as your text wraps (Enter sends, Ctrl-J adds a line,
Ctrl-V pastes a picture from the clipboard as an attachment, or its text; it needs `wl-paste`, or `xclip` on X11, so on a Mac drag the picture in or paste its path instead;
a pasted or dragged-in picture path attaches that picture too, shown as `[Image #N]` and a clickable link once sent,
Up/Down recall your messages in this conversation, Esc cancels a reply in progress). PgUp/PgDn, Shift-↑/↓ or the mouse wheel scroll
the transcript, Alt-Home/Alt-End jump to the top/bottom, and a reply that is still streaming will not
pull you back down while you are reading; scroll to the bottom, click the "↓ Jump to bottom" pill, press Ctrl-End or send a message to follow again.
Mouse support is on, so hold Shift while dragging to select text in the terminal. Your messages sit behind a violet
bar down their left edge, in bold, and replies stream indented under them, with no mark. Fenced code sits on a darker fill between two hairlines, the top one
naming the language. A run of tool calls folds, once the reply moves on, into one quiet row saying what it
did, `● Read 2 files, searched once, edited a file +12 −3 ›`, with the lines the edits added and removed;
a click on the row, or Ctrl-O for the last reply's, unfolds it into a row per call (`✓ Bash git status`) and
each edit's diff. `/clear` wipes the screen. `/copy` puts the last reply's
last fenced code block on the clipboard (`/copy 2` the one before it, `/copy reply` the whole reply as
markdown) through `wl-copy`, `xclip` or `xsel`, or an OSC 52 escape where none of those is around (over SSH or on a Mac,
if the terminal allows it: iTerm2 with clipboard access allowed, Ghostty and kitty do, Terminal.app doesn't); it says how many lines went.
While a reply is being worked out a spinner line says what Vision is doing (`thinking…`, `using Bash…`).
When it delegates to subagents (Claude Code's Agent tool: Explore, Plan, general-purpose…) each one gets
its own block above the reply — `⠋ Explore what it was asked` with the tool calls it makes listed
underneath as they happen, ending in `✓ … · 3 tools · 4.2s` — so you can watch what they are up to
instead of just seeing `using Agent…`. The block stays in the transcript afterwards.
Vision runs on Opus 5 at medium effort out of the box; the input box's bottom-right corner shows the model
typed input actually goes to and its effort (e.g. `Opus 5 (high)`, where the Grok CLI puts its own),
and while `/talk` is on the status line under it names the voice model too. `/model` and `/effort` open arrow-key pickers for Claude,
Codex and Grok models and only show effort levels supported by the selected model. `/model sonnet`,
`/model gpt-6-astra`, `/model grok-4.6`, or `/effort low` set one directly, and
`/model default` puts the session back on the saved default model and effort.
Switching models on the same provider keeps the existing session and its full transcript, with no
summary-generation turn. Claude ↔ Codex ↔ Grok cannot share a session, so Vision reads all saved
user/assistant messages from the previous provider's session files and sends them with your next message
in a fresh session. Provider-specific tool traces are omitted from these cross-provider transfers.
Transfers retain context imported from earlier providers, so chatting between repeated switches keeps
the earlier conversation too.
When a reply fails because the provider's usage window is used up, Vision offers the other providers
that are set up here (`failover = "ask"` under `[brain]`, the default): pick one and the conversation
carries on there from the transcript so far, with your message sent again. `failover = ["codex", "grok"]`
switches to the first of those that is ready without asking (a note in the chat says so), and
`failover = "off"` leaves it to you. Once a turn has reported the provider's rate-limit windows, the
corner label also shows how much of the tightest one is left (`Opus 5 (high) · 38% left`); at or under
`quota_warn_pct` (15 by default) the figure turns amber and a note says when the window resets. The
figure comes from what turns and `/usage` already recorded, never from an extra call.
The legacy `full` suffix (`/model sonnet full`) still works but is no longer needed; old `handoff`
config settings are ignored. The incoming model's normal context limits still apply.
`/default` (or `vision default`, optionally with `--model opus --effort high`) walks through both pickers
and saves the choice to the config file; `/default reset` (or `vision default --reset`) puts it back to
Opus 5 · medium, the factory default: quick to start and enough for everyday work, with `/effort high`
one command away for the hard ones.
`/compare` puts one prompt to several exact brains at once and shows the answers side by side:
`/compare opus:high gpt-6-astra:medium grok-4.6 is this migration safe to run twice?` names each brain
as any model `/model` takes, with an optional `:effort` (left out, the session's effort is fitted to that
model), and the rest is the prompt. `/compare opus:high sonnet:low` without a prompt arms it for the next
message (the status row says `comparing next: …`; `/compare off` drops it), and a bare `/compare` ticks
the models in a form. Each brain is a fresh, read-only driver (plan-mode permissions, so two of them
never edit the same files at once) that reads the conversation so far the way a cross-provider switch
hands it over, and is thrown away afterwards: the chat's own brain is not asked, no session is resumed
or remembered, and the next message goes to the chat's brain as before. The answers sit in a column
each when the terminal is wide enough and stack otherwise, with each brain's time and output tokens
underneath; Esc cancels the lot, and a phone joined to the chat gets the answers as one note.
Vision has two modes, and the composer's bottom-left corner always shows which one is on:
`⏵⏵ auto` runs every tool without asking (the `denied_tools` patterns still apply); `⏸ plan` is read-only —
Vision reads, searches and runs read-only commands, then presents a plan and a `Carry out this plan?`
selector. Yes switches to auto mode and carries it out in the same turn; No (or typing what should change)
keeps it planning. Shift-Tab toggles the modes, `/mode plan`, `/mode auto` (or `/plan`, `/auto`) set one,
and `mode` under `[brain]` picks the one Vision starts in (auto by default). With
`plan_approval = "turn"` a Yes carries out only that plan and the next message starts in plan mode again.
Between the two sits `ask_tools` under `[brain]`: patterns in the `denied_tools` syntax (plus a bare tool
name or a path glob, e.g. `["Bash(git push:*)", "Bash(rm -rf:*)", "Edit(.env)", "Write"]`) that auto mode
pauses on. A matching call opens a `Run this?` selector showing the command or path (here, and on the phone
as any question): Yes runs it, `Yes, for the rest of this turn` stops that rule asking again until the reply
ends, No tells the model you declined and it carries on without it. Claude Code enforces the list as its own
`ask` permission rules (they prompt in every mode, deny rules first), and Vision enforces it itself for the
servers it runs tools for (`[local]` and `[providers.*]`); Codex, Grok and ACP agents are only asked in
their prompt to check with you first, so for them it is advisory. Nothing asks in plan mode (there is
nothing to write), and a worker thread that hits a rule comes back with a question instead.
`allow_tools` under `[brain]` lists exceptions: a call an ask rule caught that one of them also covers runs
without asking (`ask_tools = ["Bash(git push:*)"]` with `allow_tools = ["Bash(git push origin dev:*)"]`);
`denied_tools` still wins, and a scout's chat and threads ignore the exceptions. The desktop app's
**Settings › Permissions** shows the three lists as rows in words (asks first, always allowed, never
allowed), adds a rule (a plain command like `git push` becomes `Bash(git push:*)`), moves one between lists
and removes it, saving to config.toml (`GET/POST /permissions`); open chats take a change from their next
step. It also shows, read only, the rules in Claude Code's own `~/.claude/settings.json`, which Claude Code
applies to Claude chats itself.
Codex and Grok have no Vision-wired plan-approval
UI, so plan mode gives them a read-only sandbox and asks them to present the plan for you to approve by switching to auto.
The phone has the same switch: the mode icon in a chat's composer (a bolt for auto, a clipboard for plan) sends `mode {chat, mode}`, which
`vision serve` applies to that chat (or passes to the terminal the chat is open in), refuses while a reply is
running, and keeps in the chat's journal so a restart does not hand a plan chat back in auto.
Shell mode, as in Claude Code: `!` on an empty input box turns it into a shell prompt (a pink `!` and
border). Enter runs the line with your `$SHELL` in the chat's folder and shows what it printed (its last 200
lines, and the exit code when it failed); the box is a message box again after. Esc, or Backspace on an empty
box, leaves without running anything; Esc or Ctrl-C stops a command that is running, and one still going after
ten minutes is stopped. The commands and their output go to the brain ahead of your next message (`<bash-input>`
/ `<bash-stdout>`, as Claude Code passes them), in a chat joined to the phone too; history views show only
your message. They are not replayed when the chat is reopened.

The phone has it too, for chats running in `vision serve` (hello `shell: true`; an older Vision gets no `!`):
`!` typed into the empty composer turns its border and send button pink, and the line runs on the computer
in the chat's folder (`shell` frame; `vision/shellmode.py` runs it for both). It comes back as a `shell` row
(a frame when it starts, another with the last 200 lines of output and the exit code), kept in the chat's
transcript while the server is up; the pink stop button (`shell_stop`) ends it. Backspace on the empty
composer leaves shell mode. A chat open in a terminal says to use `!` there.
A chat started on the phone works in the directory `vision serve` runs in (or `[brain] workdir`), unless a
folder was picked for it: the drawer's folder button lists the recent directories and lets you walk the
machine's (`GET /folders?path=`), and the chat is opened with `new {workdir}`. Each chat's summary says
where it works.
A chat can be pinned to the top of the list: a long press and **Pin** on the phone, the pin button on a
row in the desktop app's sidebar (`pin {chat, pinned}`). The server keeps the pins in
`~/.local/state/vision/pinned.json` and says `pinned` in each chat's summary, so every app shows the
same; closing a chat drops its pin.
To rename a chat in the desktop app, double-click its row in the sidebar or its title above the chat; Return
keeps the name, Esc puts the old one back (`rename {chat, title}`, up to 120 characters). Every app shows the
new name, it is kept with the chat's journal over a restart, and a title worked out from the first message
never replaces it. A chat open in a terminal keeps the name until the terminal starts another conversation.
To edit a message you sent, hover it in the desktop app and click the pencil: the text opens in place, and
Return sends it as a **new chat** branched from that point, titled like the old one with "(edit)"; the chat it
came from stays as it was. The new chat shows the earlier turns, and its model starts a fresh session with them
handed over as text (as a switch to another provider's model does), so it works the same on every brain
(`branch {chat, at, was, text, images?}`: `at` is the message's row in `GET /history?limit=200`, `was` its
text, for when the app's copy of the history is behind).
The desktop app's search box looks inside the open chats too, not only their titles: under the titles
that match, **In messages** lists each chat where every word turned up in one message, with that line,
and a click opens the chat there (`GET /search?q=`; scout chats count).
Ctrl+F (⌘F; **Edit › Find in Chat**) searches the open chat instead: a bar at its top right marks every
match, says which one of how many is showing, and Return, Shift+Return or the arrows step through them;
Esc closes it. The marks follow a reply as it streams in.
The ring by the model in the desktop composer is how full the chat's context is (amber from 70%, red from
90%). Clicking it shows the token counts and **Compact**: the brain summarises the conversation, and the
chat carries on from that summary and its latest exchanges in a fresh session of the same model, so the
context empties (`compact {chat}`; hello `compact: true`). It works for every brain, runs like a reply
(Stop cancels it and the chat keeps its session), leaves a "Compacted the conversation" note in the chat,
and is refused while a reply is running. If the brain can't summarise, the latest exchanges alone carry on.
Banked limit resets and Grok's allowance on the usage page come from undocumented endpoints that Vision
calls with the logins Claude Code and Grok saved on disk; `read_cli_logins = false` under `[brain]` keeps
Vision out of those files.
Inside chat: `/speak` toggles voice, `/talk` starts a hands-free spoken conversation right there in the
chat screen (type to answer instead of speaking; Esc, `/talk` again or saying "goodbye" ends it; with the
background service running, which has the voice and the ears, the terminal borrows them: what your mic hears
goes to the service to be written down and each sentence of the reply comes back as audio to play here,
`vision/servicevoice.py`), `/wake`
turns the wake word on: whenever the chat is idle a tiny Whisper on the CPU listens for "Vision"; say it and
the real ears and voice warm up and it listens (say "Vision, what's the weather?" in one breath and that runs
straight away), then it dozes off again after a quiet follow-up window (`[wake]` in the config: `enabled`
to start that way, `names`, `follow_up_s`; `vision --wake` too), `/listen`
speaks one turn, `/voice narrator` changes the voice, `/mic` picks the microphone from a list (or `/mic snowball`,
`/mic default`; it is saved as `input_device` and a running `/talk` or `/wake` moves over to it), `/new` starts a fresh conversation, `/cd <dir>` changes the working directory for this session (`/cd ~`, `/cd ..`, `/cd -` for the previous one, `~` and relative paths as in a shell; every brain works there from the next reply on, the composer's corner shows it, bare `/cd` says where you are).
A Claude conversation stays with its directory (Claude Code keeps it under that directory and finds it only
from there), so `/cd` away from one starts a new conversation there that opens with the transcript so far,
and `/session` on a Claude conversation from another directory moves to that directory (`/cd -` comes back).
A folder can carry standing instructions: a `.vision.md` in the working directory, or in any folder above it
up to the repository root or your home folder, goes into every brain's prompt on each turn (a repo-wide one
and a subfolder's both apply, the nearest last; a worker started in that folder reads it too). It is read
fresh every turn, so an edit counts from the next reply, and `/cd` into a folder that has one says so.
`/instructions` shows the files in effect and their text; `[brain].folder_instructions` names the file
(`"AGENTS.md"` to share one with other tools, `""` to switch it off). A file longer than 8,000 characters
is cut there. The coding CLIs' own files follow the folder too, whichever brain is on: Claude Code reads a
`CLAUDE.md` and Codex an `AGENTS.md` by themselves, so Vision hands each brain the ones its CLI doesn't (a
Codex chat in a repo with a `CLAUDE.md` gets its rules, a Claude chat an `AGENTS.md`'s; Grok and the
local models get both, a file linked to the other counts once, and these are cut at 24,000 characters).
`/instructions` lists them as handed over.
In a git repository Vision takes a checkpoint of the working tree before every reply (tracked and untracked
files, ignored ones left out; nothing is committed and your index and HEAD stay where they are), so you can see
and undo what the model did: `/diff` shows what changed since the last reply started (`/diff 3` since the
third-last, `/diff list` the checkpoints with the message each was taken for), and `/rewind` puts the files
back as they were then after a `Yes` (`/rewind 3` likewise; a rewind is itself a checkpoint, so `/rewind`
again undoes it). The checkpoints are git trees kept under `refs/vision/checkpoints` (not in `git log`;
`git update-ref -d refs/vision/checkpoints` drops them), and which belong to which conversation is kept in
`~/.local/state/vision/checkpoints.json`, so a resumed conversation can still `/diff`. `checkpoints = false`
under `[brain]` switches them off.
You can cut a spoken reply short by voice (`barge_in` under `[listen]`, in `/talk`, `vision talk` and
with the wake word on): `"wake"` (the default) means say "Vision" — a tiny CPU Whisper keeps listening
over Vision's own voice, so it works on speakers, and "Vision, what about Mars?" runs straight away, while
"Vision" alone stops it and it listens; `"speech"` means just start talking (a quarter second of voice
cuts in, so it needs headphones or, on Linux, PipeWire echo cancellation: `pactl load-module module-echo-cancel`,
then `input_device = "echo-cancel"`); `"off"` leaves it to Esc / Ctrl-C.
Vision can listen to you alone: `vision voice enrol` records three short phrases (or `--from a.wav b.wav`
takes recordings) and keeps a voiceprint in `~/.local/share/vision/speakers`; with `only_me = true` under
`[listen]`, everything the mic hears in `/talk`, `vision talk`, `/listen`, the wake word and a cut-in is
scored against it by a small speaker model (WeSpeaker's ResNet34, 26 MB, fetched on first use or by
`vision setup`; about 30 ms on the CPU, nothing you can hear) before it is transcribed, and another voice is
dropped with `not your voice (0.12)` in the transcript. A stranger who says the name sends Vision back to
sleep. The score is a cosine similarity: your own voice lands around 0.6-0.8 and other people 0.0-0.3, so the
default `only_me_threshold = 0.45` sits between; `vision voice enrol --check` shows your score from one
phrase, `--forget` removes the enrolment, and `vision doctor` says whether one exists. With `only_me` on and
nothing enrolled, or the model not downloaded, every voice is heard and the chat says so once.
Typing `/` pops up the command menu above the input box and it narrows as you type (`/mo` → `/model`);
↑/↓ choose, Enter runs the highlighted command, Tab fills it in, Esc hides the menu. After a command
that takes an argument (`/model `, `/effort `, `/voice `, `/session `) the menu switches to its choices.
`/session` lists earlier Vision conversations in Claude, Codex and Grok tabs (←/→ switches provider)
and resumes the one you pick from the next message on. Same provider keeps that native thread;
another provider continues the transcript here on the current brain. `/session 7eb17c64` resumes by
id prefix (current provider first, then the others), `/session id` shows the current id.
Conversations are read from Claude Code's, Codex's and Grok's own session stores, so nothing extra
is written. `vision -c` / `vision talk -c` continues the last conversation.
`/export` saves the current conversation as markdown (a title line, then a `## You` / `## Vision`
section per turn, tool calls left out) to `~/Documents/vision/<date>-<title>.md`; `/export <file>` or
`/export <folder>` puts it elsewhere. From the shell, `vision sessions` lists every conversation,
`vision sessions grep <words>` finds the ones where all the words were said (case-insensitive, the
three best lines each, `--all-lines` for every one, `-p claude` for one provider), and
`vision sessions export <id> [-o path]` writes one out.
Vision's own memory is a folder of markdown files every brain reads (`~/.local/share/vision/memory`;
`/memory` shows it, `/remember <fact>` and `/forget <text>` edit it by hand, `/memory tidy` has the
brain clean it up). Leaving a chat with `/quit` or Ctrl-D after `[memory].review_after_turns` replies
(4) runs the **memory review**: one tool-free call on the chat's own provider (Sonnet on Claude, the
quick Codex or Grok model otherwise) reads the transcript and what memory already holds, and proposes
the durable facts it left, each with the file it belongs in. They come as a tick list, all ticked;
untick what should not be kept, Enter on Submit saves them, Esc skips the lot. Nothing is written
without a tick. `/review` runs it mid-chat and `vision memory review` over the last conversation
without opening one; `review = "off"` under `[memory]` stops the automatic one, and
`review_timeout_s` (45) caps how long the call may hold the exit. A chat mid-reply, on a voice
call, joined from the phone, or too short just leaves.
`vision memory import` brings in what another assistant already knows about you, so a switch of brain
doesn't start from nothing: it reads Claude Code's notes (`~/.claude/CLAUDE.md` and every project's
memory folder under `~/.claude/projects/`; the index files are skipped), has one call pick out the
durable facts Vision's memory doesn't hold yet, and offers them as the same tick list, a page of eight at
a time. `vision memory import FILE` (or `-` for stdin) reads anything else instead, such as a claude.ai
memory export or what ChatGPT says it knows about you.
The phone's Memory screen (Settings › Memory) and the desktop app's Settings › Memory show the same
files: the profile, then each area with what it covers. On the desktop every fact can be reworded in
place (click it; Return or clicking away saves, Escape puts it back, the date stays) or forgotten,
"Add a fact" adds one to the file open, and the search box looks through every file at once.
Changing between Claude, Codex and Grok with `/model` starts a fresh conversation because their native
thread stores are separate; changing models within one provider keeps that provider's conversation.
`--continue` resumes the most recent thread for whichever provider owns the selected model.
Vision's buddy sits in the bottom-left gutter beside the input box: the same violet creature with a
sprout that the phone and desktop apps draw, in half-block pixels. Its face follows the chat: curious while idle (with a blink every few seconds and the odd glance
aside), focused while thinking (dots count up by its head) and while a tool runs (a dot runs along its
mouth), proud while the voice speaks (its mouth moves with it), worried after an error until you type
again, and asleep, dimmed with a `z`, after ten idle minutes. While listening its mouth opens with the
actual microphone level and its sides breathe gently in the quiet. It slides in from the left when a
chat opens (`slide_in = false` under `[buddy]` turns that off); `enabled = false` there hides it. In a
terminal narrower than 72 columns it becomes a tiny face on the status row with a word on what it is doing
(`› Bash`, `listening`, `zz`).
A reply that took longer than `notify_after_s` under `[chat]` (30 s by default; 0 = never) and finishes
while the terminal is not the focused window puts a line on the desktop (`notify-send` on Linux,
Notification Center on a Mac): how long it took and its first words. Spoken replies and ones from the
phone don't; a terminal that doesn't report focus notifies past the threshold regardless.
During `/talk` and `/listen` warmup, the buddy's bottom row fills as speech recognition, voice, and the
optional interruption listener finish loading. The percentage counts ready components, not elapsed
time: it holds during a slow load, excludes failed loads, and resets for the next warmup. Narrow
terminals show the same percentage beside its inline face.

## The voice

**The voice switch.** `/voice off` in the terminal, or the Voice switch in the phone's Settings, unloads the voice
and speech recognition so the GPU is free, and keeps them unloaded: it is saved as `[voice] enabled = false`, the
background service and every terminal on the machine follow it, and a service started while it is off loads
neither. While it is off, spoken replies, `/say`, `/talk`, `/listen`, the wake word and the phone's calls wait
(the phone's call button is dimmed and says so). `/voice on`, or the switch, loads them again in the background.
The service keeps CUDA's own context, a few hundred MB, until it next restarts.

Spoken turns (`vision talk`, `/talk`, `/listen`, wake words, barge-in and the iPhone's live voice mode) use
a conversation model: the chat's own model when that is a Claude, Codex or Local one, and
`[conversation].model` (**Sonnet at low effort** by default) for a Grok chat. It handles ordinary
conversation itself. For work requiring files, commands or current facts, it returns a validated
task with an objective, context, constraints and success criteria. The selected CLI agent executes
that task in a separate worker session and returns status, findings, changes, checks and any question.
Only the conversation model's spoken response reaches TTS; worker prose, JSON and tool logs stay private.
Plan approvals still show the actual plan for review. Worker clarification questions return to the
conversation model so it can ask naturally.

The conversational process uses a replacement system prompt, read-only web tools (or `--tools ""` when disabled), an empty MCP configuration,
and safe mode to disable project instructions, hooks, plugins and skills while keeping subscription
authentication. It never resumes a coding session or gains tools when a task is delegated. These
options require a recent Claude CLI; see the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).
Voice therefore needs a Claude login even when `/model` selects Codex as the worker.

Optional settings in `~/.config/vision/config.toml` (existing configs receive these defaults):

```toml
[conversation]
model = "sonnet"
effort = "low"
web = true          # read-only WebSearch/WebFetch for the voice model; false = no tools at all
max_delegations = 3
timeout_s = 120
timing = false      # optional stage durations in ~/.local/state/vision/voice-timing.jsonl
```

`/model` and `/effort` control the typed agent and voice worker. Name a brain in the request and that
task's worker runs on it instead: "get Opus to fix the tests", "use Codex for this", "have Haiku do it at
low effort". The worker shows as a row above the reply, agent · model effort · description · time · tokens: a
live timer and output tokens so far while it runs, total time and tokens in and out once done. A model name
that is not in the catalogue fails the task rather than quietly running on the default. In talk mode the
chat's own model does the talking when it is a Claude, Codex or Local model; a Grok chat talks through
`[conversation].model` instead (set it in the config) and keeps its model as the worker. Its conversation history stays in memory for the current Vision process, with a bounded
recent context; `/new` or resuming a different session resets it. `--continue` continues the typed
agent's native history, which the voice model can read as context. Private worker sessions do not
replace that continue target or appear in `/session`. The remote server keeps a combined typed/voice
transcript for reconnecting phones until the server stops or the conversation is reset.

Desktop voice sessions reuse an isolated Claude process, sending context once and then only new
turns, worker results and changed context. Complete responses are validated before speech or task
dispatch, and each speech response is immediately flushed into the streaming speaker. The process
closes when desktop voice mode ends, on cancellation or failure, and on a new session. Configuration
changes restart it on the next request. Native context is recycled after twenty responses, when
sent context exceeds 64,000 characters, or when local history is pruned. A fresh connection receives
the bounded local history; failed or interrupted actions are never automatically retried.

To diagnose desktop response delays, set `[conversation].timing = true` and restart Vision.
Each spoken turn appends one JSON line containing only stage labels, relative milliseconds, and
whether the connection was cold. It includes endpoint detection, transcription, model requests,
web calls, delegated work, synthesis and estimated playback. Acknowledgement and answer readiness
are separate events. Playback time includes the output device's reported latency; it is an estimate,
not an acoustic measurement. Wake-word and push-to-talk input may lack a speech-end timestamp.
Paths follow `XDG_STATE_HOME` when set. Diagnostics are off by default and log no conversation content.

A repeatable transport benchmark is available from the repository:

```bash
.venv/bin/python scripts/bench_conversation.py --transport one-shot --count 10 --output /tmp/voice-before.json
.venv/bin/python scripts/bench_conversation.py --transport persistent --count 10 --output /tmp/voice-after.json
```

This uses your Claude subscription and performs fresh weather searches. It reports a cold request
and ten warm requests per category, with median and slowest answer times. It does not record the
microphone or play audio, and excludes transcription, synthesis and speaker latency.

The voice is [Qwen3-TTS 1.7B](https://github.com/QwenLM/Qwen3-TTS) (Apache 2.0): it imitates a short
reference clip, so Vision's voice is whatever you give it. Two ways to make one, both saved under
`~/.local/share/vision/voices/<name>/` (`ref.wav` + `ref.txt`, plus `design.txt` for designed voices):

```bash
vision voice design narrator                  # a built-in description; generates takes, you pick one
vision voice design butler "A dry, precise English butler..." --takes 3 --use
vision voice add mine --from me.wav           # clone a recording (transcribed with Whisper); only voices you may use
vision voices --preview                       # list them, hear them
vision say -v narrator "Testing."
```

`[voice] voice` in the config picks the default (or `/voice <name>` in a chat). `clone_mode` chooses
how the clip is imitated: `embedding` (default) keeps its timbre and lets the model shape the delivery,
`context` continues the clip's own audio for its exact pacing and tone (and any roughness in it).
`rate` stretches the pace after synthesis without changing the pitch (1.13 is brisk), `language` sets
what it speaks.

A **styled voice** is three clones of the same speaker named `<name>-conversational`,
`<name>-expressive` and `<name>-reassuring`. Set `voice = "<name>-conversational"` and every reply is
spoken in the style its text suits: reassuring for "don't worry" and apologies, expressive when it
exclaims, conversational otherwise. `~/.local/state/vision/voice.log` records which one fired and why.

In a spoken conversation the reply's first words can take a while (transcription, then the model's own
thinking: a second on a good turn, twenty on a hard one). Rather than dead air, the voice says a short
"Hmm." when nothing has started `filler_after_ms` after you stop talking, and the reply follows it. The
phrases (`filler_phrases` under `[voice]`, just `["Hmm."]` by default) are made in your voice at warm-up and
cached under `~/.local/state/vision/fillers`, so they cost nothing on the turn; `filler = false` turns it off.
A config.toml still holding an older release's default list is read as `["Hmm."]`; a list you edited is kept.

Vision streams the model rather than waiting for whole clips. `vision/talker.py` replaces the library's
`transformers.generate()` loop: the prompt is prefilled once, then every 80 ms frame is one CUDA-graph
replay of the 1.9B talker step and one of the whole 15-step residual-code sub-model (sampling included,
Gumbel-max so nothing syncs) on static KV caches. Each frame goes straight to the causal codec decoder,
which renders 4 frames for the first piece and 8 thereafter behind 72 frames of context. Performance depends
on the GPU and model settings; the voice setup checks whether the selected model can run on your hardware.

The previous engine, [Orpheus 3B](https://github.com/canopyai/Orpheus-TTS) under a private
`llama-server`, is still there behind `[voice] engine = "orpheus"` (eight built-in voices, `<chuckle>`-style
inline sounds; `vision setup --orpheus` fetches it).

## Weather (Apple WeatherKit)

Entering `/talk` starts the conversation process and prefetches configured home weather during
audio warm-up. Successful reports are cached in memory for two minutes, shared by startup and
follow-up questions. Different locations have separate cache entries; expired reports are not
served after a failed refresh. Say “refresh the weather” to bypass the cache. Simple current-weather
questions request a short answer rather than an unsolicited multi-day forecast.

"Vision, do I need an umbrella?" used to mean a web search inside the voice model, several seconds of
it. Now the answer comes straight from a weather service, with no account needed: the US National
Weather Service for US places, Open-Meteo for everywhere else. Set `location` under `[weather]` and it
works. With an Apple Developer membership WeatherKit's REST API (500,000 calls a month are included)
can answer instead, which adds measured current conditions and alerts in more countries; the setup is
below. Vision spots a weather question in what you said, fetches the
report while the rest of the turn is prepared and hands it to the voice
model as data, so the model never searches. The report only goes as far as the question: "what's it
like out?" gets now, the next twelve hours and today; "tomorrow" adds tomorrow; "the weekend", "this
week" or a day name gets the whole outlook. When the voice model needs a report it wasn't handed
(another place, another day) it asks WeatherKit itself through the `weather` field of its reply. Its
web tools never serve the weather: a Claude voice model's WebSearch or WebFetch call for the weather
(or to a weather site) is refused in code and answered with the WeatherKit report, and so is a
front-end search for it. The typed brain runs `vision weather [place]` for the
same report. Every report ends by naming its source ("Apple Weather", "US National Weather Service" or
"Open-Meteo").

WeatherKit setup (optional), once, at [developer.apple.com](https://developer.apple.com/account/resources):

1. **Keys → +**: name it, tick WeatherKit, download the `.p8` (only offered once) and save it as
   `~/.config/vision/weatherkit.p8`. Note its Key ID.
2. **Identifiers → + → Services IDs**: an identifier such as `com.yourname.vision.weatherkit`, with
   WeatherKit ticked under its capabilities.
3. In `~/.config/vision/config.toml` under `[weather]`, fill in `team_id` (top right of the account page),
   `service_id`, `key_id`, and where home is: a `location` name (geocoded once through Open-Meteo and
   cached in `~/.local/state/vision/weather-places.json`) or `latitude`/`longitude`/`timezone`.

```bash
vision weather                 # the home report
vision weather Lisbon          # anywhere; "in <Place>" in a spoken request does the same, and a place
                               # named in the last few turns carries over ("time in NYC?" … "and the weather?")
vision weather --week          # tomorrow and the days after too (--tomorrow for just tomorrow)
vision weather --raw | jq .    # the WeatherKit JSON
```

`units = "imperial"` switches to °F and mph; `enabled = false` turns the integration off and the voice
model goes back to searching. New keys can take a few minutes to start returning 200s.

## The front end (router + supervisor)

The conversation model (`[conversation]`: a Claude, or a Local model) can be Vision's front end:
it talks to you, answers genuinely basic questions itself, gets the weather from WeatherKit and
current facts from a search-only lookup, and hands everything substantive to an agent that Vision
launches and supervises. The model never runs anything: it has no shell, files or browser, and it
cannot grant a permission. `[router] mode` picks how far this goes:

- `off`: the voice model decides delegation on its own, on the `/model` brain, as it always did.
- `audit` (the default): every request is classified and the decision logged to
  `~/.local/state/vision/routing.jsonl` and shown as a dim `router (audit): …` line; nothing changes.
  Live with it for a while, read the log, then switch on.
- `on`: routes are enforced (`/router on`, `/router on save` to keep it). Typed messages go through the
  front end too unless `typed = false`.

Routing is code, not a prompt (`vision/routing.py`), in this order: your explicit choice; a weather
request; a request for current information (search only); a basic question (the front end answers);
everything else goes to `default_agent` at `default_effort` (Opus 5 · medium), or at `high_effort`
for architecture, hard debugging, repository-wide changes, security-sensitive work, multi-stage
research, work spanning several systems and a request that already failed at medium. Say it or type it:

```
use Opus high for this: refactor the parser     /agent opus --effort high refactor the parser
have Codex handle this                          /agent codex        (applies to the next message, or the last request)
answer this locally                             /local what is JSON
only search the web for this                    /search latest python release
                                                /weather Lisbon        /cancel        /router on
```

A choice that cannot be honoured is an error you see, never a quiet substitute: an agent not in
`[router.agents]`, an effort the model does not support, a CLI that is not installed. The supervisor
(`vision/supervisor.py`) launches the agent as a task-mode worker in its own session, shows
`Launching Opus 5 · medium effort` and then real states (`Opus 5 is running tests…`), enforces the
`[router.limits]` timeout, question rounds and token budget, refuses to launch the same request twice
while a run is alive, and reports only what the worker's structured result said: a run with no valid
`completed` result is failed, whatever it wrote in prose. When the agent stops to ask, its question is
shown word for word, the run waits, and your next plain message goes back into that same session (an
explicit command still wins). `[router.permissions] approval` lists commands the agent may not run on
its own (`git push`, package installs, `rm -rf`, `ssh`, deploys…): it comes back with a question, your
`yes` lifts that one rule for that run, and only your answer can. Search results reach the model as
titles, links and snippets marked untrusted; on a search route a task is refused outright, so nothing
in a snippet can start work.

## Model providers

Claude turns run through `claude -p --output-format stream-json --resume <session>`, passing Vision's
persona via `--append-system-prompt`. Claude conversation memory stays in Claude Code's session store.
The process is kept from one typed turn to the next (`keep_alive_s` under `[brain]`, ten minutes by
default), so a reply starts without the CLI's start-up and transcript load, a second or two each
time. The process is started as the chat opens (here, or on the phone), so the first reply skips the
start-up too, and when the model, effort, mode, fast mode, working directory, memory or conversation
changes (`/model`, `/effort`, `/mode`, `/fast`, `/cd`, `/remember`, `/new`, `/session`) its replacement
starts at once, while the next message is being typed; a change made during a reply takes effect
after it, and the replacement starts then. The first reply after an idle spell starts a fresh one.
`keep_alive_s = 0` starts one per turn, as before. Voice task workers always run one task per process.
Auto mode is `--dangerously-skip-permissions` with `denied_tools` enforced by Claude Code's deny rules;
plan mode is Claude Code's own `--permission-mode plan`, and the plan arrives over the control channel as
an `ExitPlanMode` permission request that Vision turns into the approval selector.

Claude can also ask *you* things: its `AskUserQuestion` tool (always on when any tool is allowed) opens a
real selector in the terminal, like Claude Code's own — single or multi-select, several questions at
once (←/→ or Tab between them), 1-9 to pick, Space to tick, Enter to confirm, or type your own answer in
the box. Esc dismisses it and Claude carries on without the answers. The answers travel over Claude
Code's stdio control channel (`--input-format stream-json --permission-prompt-tool stdio`); Codex and
Grok have no equivalent.

GPT turns run through `codex exec --json` and resume with Codex's native thread IDs. Codex does not have
Claude's per-tool allow/deny interface, so the modes map to its sandbox: plan mode is `read-only`, auto
mode is `danger-full-access` unless `[codex].sandbox` names `workspace-write` or `read-only`.
`denied_tools` is included as firm model guidance but cannot be mechanically enforced by Codex.

Codex and Grok both like to announce a tool before using it ("I'm checking the forecast."), which the
persona cannot reliably talk them out of. Vision holds a short one-sentence message back until the next
event and drops it when a tool call follows (`vision/reply.py`); the reply then starts with the answer.
Anything longer than a sentence, such as a warning before a destructive command, streams through as
written, and text that is followed by nothing is always shown.

Grok turns run through `grok --prompt-file --output-format streaming-json --resume <id>`, passing
Vision's persona via `--rules`. Auto mode is `--always-approve` with Grok's kernel sandbox (`off` unless
`[grok].sandbox` names `workspace`, `read-only` or `strict`); plan mode is the read-only sandbox.
`denied_tools` are passed as `--deny` rules, which Grok does enforce. Headless Grok cannot show
AskUserQuestion or ExitPlanMode, so those tools are removed for Vision turns.

Neither brain can spend another subscription behind your back. A turn's shell gets fake `claude`,
`codex` and `grok` commands first on its PATH (`~/.local/share/vision/shims/`, they print "blocked by Vision" and
exit 1), the other providers' CLIs are pointed at an empty config dir (`CODEX_HOME` / `CLAUDE_CONFIG_DIR` /
`GROK_HOME`) so they have no credentials even if the shim is bypassed, and the default `denied_tools` also lists
`Bash(claude:*)`, `Bash(codex:*)` and `Bash(grok:*)`. The persona tells the model to point you at `/model` instead of
trying. Cross-provider work is always an explicit `/model` switch.

### Choosing providers, adding your own

`[providers] enabled` in config.toml says which providers `/model` offers, in that order; `/providers`
picks them (a multi-select: Enter ticks, Enter on Submit saves), or name them outright
(`/providers claude local`). A provider that is on but not set up here (its CLI missing, its server not
answering) still shows in `/model`, as one row that says how to set it up, and a chat whose saved model
can't run here starts on the first provider that can and says so. Leave the `enabled` line out to offer
everything, including servers you add.

Any OpenAI-compatible chat server is a provider of its own: Ollama, LM Studio, vLLM, another
llama-server, OpenRouter, a hosted API. Add a table under `[providers]`:

```toml
[providers.ollama]
base_url = "http://localhost:11434/v1"
label = "Ollama"            # the /model tab (default: the name capitalised)
context = 32768             # tokens; the conversation is trimmed to fit

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"   # or api_key = "…"; sent as a bearer token
models = ["anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"]   # else the server's /models
```

It runs exactly as `[local]` does (`vision/local.py`): Vision talks to it directly and runs the model's
tool calls itself (Bash, Read, Write, Edit, WebSearch, WebFetch), nothing goes through a CLI, and it can
be the voice conversation model too. Its models show in `/model` as `<name>/<model>` (`ollama/qwen3`),
so two servers' names can't clash, and the model id after the slash is what the server is asked for.
Its conversations list under their own `/session` tab. A table Vision can't use (a built-in name, no
`base_url`, a `type` other than `"openai"`) is skipped with a note at start-up.

`[local]` is the same kind of provider, built in, with a shorter name: its models stay bare in `/model`,
under the names the server reports. Point `[local].base_url` at any OpenAI-compatible server on any OS:
llama.cpp (`llama-server -m model.gguf --port 8080`, then `http://localhost:8080/v1`), Ollama
(`http://localhost:11434/v1`) or LM Studio (`http://localhost:1234/v1`), and set `[local].context` to the
model's context size. Effort `high` turns thinking on for models whose chat template has the switch.

An agent that speaks ACP (the Agent Client Protocol, JSON-RPC over stdio: Gemini CLI with
`--experimental-acp`, and others adopting it) is a provider too, with `type = "acp"` and the command:

```toml
[providers.gemini]
type = "acp"
command = ["gemini", "--experimental-acp"]
models = ["default", "gemini-2.5-pro"]   # optional; "default" is the agent's own
model_flag = "--model"                    # how a named model is passed, if the agent takes one
[providers.gemini.env]                    # optional, set for its turns
GEMINI_SANDBOX = "false"
```

Vision starts the command, opens or resumes a session, and shows its text, tool rows and thinking
the way it shows Grok's agent mode (`vision/acp.py`, on the same transport). The process is kept
from one turn to the next (`keep_alive_s` under `[brain]`, as for Claude and Grok's agent mode),
rather than started again, with the whole conversation replayed, for every turn. The agent
brings its own tools; Vision answers its permission prompts, approving everything `denied_tools`
doesn't rule out and refusing writes in plan mode, and says what it refused when the agent then
ends the turn. Plain ACP has no rules channel, so the persona leads the first prompt of a fresh
session; no way into a running prompt either, so a message sent mid-reply queues for the next turn.
Vision keeps its own transcript of every agent session (in the local-sessions folder), so `/session`
lists them and a session the agent has lost carries its history into a fresh one. The command is
blocked inside every other provider's turns, like the built-in CLIs. `vision provider add gemini
--command "gemini --experimental-acp"` writes the table. Built against the protocol's standard slice
(tests/fake_acp_agent.py); not yet run against a real agent.

The first `vision` on a machine that has never held a chat goes through `vision onboard` by itself, a
chain of short steps (`vision/onboard.py`):

1. **Your providers.** The setup below. With a provider already working (Claude Code was signed in
   first) nothing is asked: it says which and moves on.
2. **You.** What to call you (`address_user_as`) and auto or plan mode (`mode`).
3. **Voice.** Offered where the machine can run it in real time (Apple Silicon, or an NVIDIA card),
   left out anywhere else; `vision voice setup` is the same offer later, and the chat's voice commands
   point at it until the voice is installed. Once it is installed, `vision voice check` runs: Vision
   says a line and asks whether you heard it, listens to one and shows what it made of it, then waits
   for its name. A check that fails offers the other speakers or microphones once and saves the pick
   (`[voice] output_device`, `[listen] input_device`); a missed wake word fails nothing.
4. **Your phone.** Optional: it installs the `serve` extra and the background service after a yes to
   each, shows the pairing code and waits for the app to connect.

Each finished step is recorded in `~/.local/state/vision/onboarding.json`, so leaving with Ctrl-C
resumes at the step you left. Without a provider that can answer, it stops at step 1 and no chat
opens. `vision onboard` runs all of it again with what is saved as the defaults, and
`VISION_NO_SETUP=1` (or no terminal) skips it.

`vision providers` (also `vision provider setup`) is the guided way in, and the way to change things
later: a tick list of every provider, ticked when it's on, plus the servers found running on their usual
local ports (Ollama 11434, LM Studio 1234, llama.cpp 8080, vLLM 8000) and rows to add a model server,
an API key or an ACP agent. Ticking one sets it up: a CLI is installed and signed in on request (each
CLI is asked whether it is signed in, so the sign-in is skipped when it is, checked again after it
has run, and offered once more when it did not take), a found server is added with no questions, an API
starts from a preset and asks only for the key (taken from its environment variable when that is set,
else pasted and kept in config.toml, which is then made readable only by you). Unticking switches one
off and keeps its settings. It ends by asking which model to start on, when something is new or the
old default went away. A provider that can't answer yet (a CLI that is signed out, a port that
answers but lists no models) is never the one it starts on. The same goes for `/model`: a server
counts as there when its `/models` gives a list or asks for a key, so some other program on
`localhost:8080` is not taken for the local model. `vision provider add` asks the same questions
for one server (name, URL, key variable), checks the server lists its models, and writes the table; `vision provider list` shows every provider with whether it is on and set
up here, `vision provider remove <name>` takes a table out. With nothing set up at all, a new chat says
so and points at the CLIs' installers and `vision provider add`.

A provider with its own protocol can come from a package: a `vision.providers` entry point that is a
`vision.providers.Provider` (or a callable returning one or a list), giving its brain class and the
hooks it supports; `pip install` it and it appears in `/model`, `/providers` and `vision provider list`
(`source = "plugin"`). The registry is documented at the top of `vision/providers.py`.

The phone draws every provider the machine's way (`GET /providers`: label, SF Symbol, tint, whether it is
on and ready), so a server added on the machine gets its own look on the phone without an app update.

All three CLI providers work in the directory where you launch Vision (or `[brain].workdir`). Install and log in
to the provider you want with `claude`, `codex login` or `grok login`; `vision doctor` checks all three
(version, login, and subscription usage; `--no-usage` skips the usage tables).

`vision --version` (or `vision version`, `/version` in chat) lists Vision's version next to the installed
Claude Code, Codex and Grok CLI versions, with an update column (`up to date` / `2.1.280 available`); name one
or more (`vision version codex`, `/version grok`) for a subset. The chat's bottom status bar shows the same for the
provider in use (`Claude Code up to date`, or `Codex 0.156.0 available · /update`); it is checked in the background
once every six hours, and again right after `/update`. Latest versions come from Claude Code's release feed, npm
for Codex, and `grok update --check` for Grok. `vision update` (or `/update`) runs each CLI's own updater — `claude update`,
`codex update`, `grok update` — and reports the before → after version; name one or more providers
(`vision update codex grok`, `/update claude`) to update only those.

`vision usage` (or `/usage` inside chat and talk) shows usage for the active model's provider; pass
`all` (`vision usage all` or `/usage all`) to show every provider together. Claude
uses Claude Code's own `/usage` report; Codex reads subscription windows from its local thread rollout;
Grok reads the grok.com weekly allowance (the shared SuperGrok pool). `--full` adds Claude's
contribution breakdown, or Grok's per-product split, when available.

## Remote (`vision serve`)

`vision serve` starts with no chat open. The phone can open a new chat with its first message or
the new-chat button. Use `--new` to open a fresh chat at startup, or `--continue` to reopen the
last conversation for the selected model's provider.

To pick up new code without cutting anyone off, restart it gracefully: `vision restart`, `r` in the
`vision serve` window, or `/restart` on the phone. It waits until every chat has finished its reply,
then relaunches itself in the same window on the same port; the phone reconnects on its own and chats
come back from their journals. A message that lands in the last instant runs in the new process. `c`
clears the window.

`vision serve` puts the brain, ears and voice behind an HTTP + WebSocket server on port 8765
for a remote client (the **Vision Remote** iPhone app in `ios/`): text chat with streamed replies, dictation
through the machine's Whisper, replies read aloud sentence by sentence through the voice engine, AskUserQuestion
forms, model switching and resuming conversations. On the same Wi-Fi the phone talks to the machine
directly (the QR carries the LAN address). Away from home, Tailscale Serve (`tailscale serve --bg 8765`)
carries it to your own tailnet devices only, and Vision puts its https address in the QR; with
`[remote] host = "127.0.0.1"` nothing listens on the LAN at all. Tailscale Funnel or a Cloudflare Tunnel
publish it to the whole internet instead, so prefer Serve. Every request needs the bearer token in `~/.config/vision/remote_token`. `vision serve` prints the address, the token and a QR code
the app scans to pair; the code also carries the computer's name, which is what the app lists the
connection under. `p` in the `vision serve` window shows the code again, and `vision pair` shows it
for a server with no window.

The speaker under a finished reply in the app reads that reply aloud; tap it again to stop. The server cuts
the reply into speakable pieces (`POST /speak/pieces`: markdown and links reduced to their words, each code
block said as "Code skipped") and the app asks `POST /speak` for each piece's audio while the one before plays.
With `[voice] enabled = false`, or if the computer's voice fails part way through, the phone reads the rest
in its own voice: the best one installed for its language (the enhanced and premium ones are downloaded in
Settings › Accessibility › Spoken Content).

The desktop app's mic button, by the paperclip, dictates through the same Whisper: hold it (or Ctrl+Shift+Space,
⌘⇧Space on a Mac, or the keys picked in Settings › Shortcuts) and talk, and when you let go the words land in the composer to read over before Return sends
them. A click starts it and the next click stops it; Esc throws the recording away. The app records the
microphone itself (macOS asks once) and sends a 16 kHz mono WAV to `POST /transcribe`. The button shows only
when the computer has a voice extra installed (hello `transcribe`) and is greyed while the voice is off, when
`/transcribe` answers 503.

The desktop app notifies when a reply finishes or a chat asks something while its window isn't in front, when
an agent's thread needs you or is ready for review, when a scout asks before running a command, and when a
scout sends its status report. Settings › Notifications has a switch for each, the system's message sound,
and quiet hours: every day between two times nothing notifies, while the sidebar and the tray still keep
count. It also says whether the system lets Vision notify (with a button to ask) and sends a test. Reminders
are notified by the computer Vision runs on (and a paired phone), not the app. Settings › Shortcuts lists
every key the app answers to; the menu's are fixed, the dictation keys can be changed there.

The app has Home Screen and Lock Screen widgets. **Projects** shows how many project threads are working, need
you or are ready, and (medium size) the first few by name, each a tap away from its project; on the Lock Screen
it is a strip or the line over the clock ("1 needs you · 2 working"). **Usage** shows each subscription's session
and week, as the Usage screen does, and on the Lock Screen a ring for the session. **Next Up** shows the next
reminder or scheduled task (the medium size, the next three; on the Lock Screen too). **Recent Chats** shows the
chats last spoken in with the opening of each latest reply, a dot on one still replying, each a tap away.
**Computer** shows whether Vision's computer can be reached, its CPU, memory and disk in use, how long it has been
up, and the voice switch or an update that is out. **Message Vision**, on the Lock Screen or in Control Center,
opens the app with the keyboard up. The
widgets ask the computer themselves (`GET /widgets`, one small answer, usage read again at most every five
minutes) about every quarter of an hour and whenever the app has been used, through the pairing the app shares
with them in its keychain group; when the computer can't be reached they keep the last answer, marked "as of".

The desktop app has the same two pages. **Usage** (View › Usage, Settings › Usage, or a click on the composer's
context ring) is a sheet with a bar per subscription window and when it resets beneath, amber from 75% and red
from 90%, plus any banked resets with a Use button that asks once more first; it reads `GET /usage` again each time
it opens. **Coming up** (in the sidebar, or View › Coming Up) lists the reminders and scheduled prompts set from
chats, each with how it repeats and when it next runs, a switch to pause it, Run now and Cancel, and follows
`scheduled` frames as they change. Its short form adds one: a reminder (the words come back as a notification) or
a prompt sent into a chat you pick, once on a day this week, every day, on weekdays or every week. A scout's
check-ins stay on the scout's page.

After a typed reply, as in Claude Code's terminal, `vision serve` guesses the message you would send next
(`vision/suggest.py`: one small call with the last few turns, whatever brain the chat is on: Claude's Haiku
when Claude Code is installed, else Codex's newest Luna model, else Grok; one whose call fails, signed out
say, is passed over for ten minutes and the next asked, and providers switched off in `[providers]` never are) and sends it
as a `suggestion` frame, also kept in the chat's snapshot until the next message. The app shows it as the
empty composer's grey text: the faded arrow sends it, and tapping the text puts it in the composer, selected,
to change or type straight over. The terminal does the same in its empty input box: Tab
puts the guess in the box, Enter sends it. A terminal's own chat makes its guess itself (and passes it to
the phone when the chat is listed there); one joined to a phone chat shows the server's. Calls get none, and
`[remote] suggestions = false` turns it off everywhere.

A chat started from the phone or the desktop app begins on the saved default model (`/default`), in
`[remote] new_chat_folder` (empty: `[brain] workdir`, else the folder `vision serve` started in) and in
`[remote] new_chat_mode` (empty: `[brain] mode`), so the terminal's own start is left alone. The desktop
app's Settings › New chats sets all three (`GET`/`POST /defaults`, `{model?, effort?, workdir?, mode?}`); the
folder button by the message box still picks another folder for one chat. Its Settings › About shows the
app's version and the computer's, looks for a newer app, and when the computer's Vision is out of date runs
`vision upgrade` from there (on this computer) or shows what to run (on another, or for a checkout).

`vision service install` keeps `vision serve` running in the background, now and at every login, so
the phone finds Vision whenever the computer is on: a systemd user unit on Linux
(`~/.config/systemd/user/vision.service`, logs in `journalctl --user -u vision`), a launchd agent on
macOS (`~/Library/LaunchAgents/com.vision.serve.plist`, logs in `~/.local/state/vision/serve.log`). No
root. It runs with the PATH of the shell that installed it, so it finds the same provider CLIs.
On a Mac, `vision serve` (in a terminal or as the service) also keeps the computer from sleeping while it
runs, through `caffeinate -s`, since a sleeping Mac is one the phone cannot reach: the display still
sleeps on its own schedule, and the hold applies only on AC power, so a Mac on battery sleeps as usual.
`vision service status` and `vision service remove` do what they say. A second `vision serve` on the
same port says the first is running instead of failing on the port. A service started while a
`vision serve` in a terminal holds the port waits and takes over when that one stops; `status` says
`waiting` until then. The API lives in `vision/server.py` if you want to write your own client.

Agents, scheduled tasks and replies run inside `vision serve`, so they stop while the computer sleeps. The
server notices a suspend when it wakes (the wall clock moved on and the monotonic clock didn't:
`vision/sleepwatch.py`) and tells the apps: a `slept` frame with when it went to sleep and woke, and the last
day's sleeps in every `hello` (`asleep`). The desktop app's All activity board says so, so a thread that stalled
overnight doesn't look stuck. For agents that keep going around the clock, run `vision serve` on a computer
that stays on and connect to it from the others.

Vision says when it is out of date. It runs from a git checkout (the installer makes one), and every
hour it asks the remote whether the branch checked out there has a commit this one does not carry
(`vision/selfupdate.py`; nothing is downloaded, and a remote that wants a password or can't be reached,
a detached HEAD or a working copy's branch the remote doesn't have means no answer). An installed Vision
whose branch is gone from the remote (merged and deleted) counts as out of date, and `vision upgrade`
moves it to the remote's main branch first. The terminal then shows one note a
session, `Vision update available`, with what to run: `vision upgrade` for an installed Vision (it runs
the installer, then restarts `vision serve` once every chat has finished replying; a Vision older than
this command is updated by running its installer again and `vision restart`), or
`git -C <folder> pull && vision restart` for a checkout. `vision serve` tells the phone (hello `update`,
then an `update` event as soon as the answer changes), which shows it in Settings and says so in a
toast over the chat, once a day while it waits. Separately, hello
carries `protocol`, one number for what the app and the server need from each other (`PROTOCOL` in
`vision/server.py`, `VisionClient.protocolVersion` in the app); a Vision that sends none is 1. An app
ahead of the server says the computer's Vision is out of date, in Settings and once in a toast; an app
behind says to update the app. The API lives in `vision/server.py` if you want to write your own client.

A `vision` chat open in a terminal on the same machine shows up in the app's chat list too (terminal
icon): the terminal announces itself on a Unix socket under `~/.local/state/vision/live/`, `vision serve`
picks it up, and what is typed on the phone runs in that terminal while its replies stream to both.
It leaves the list when the terminal quits, and closing it on the phone quits that terminal (`vision/link.py`).

The other way round, a chat started on the phone can be joined from a terminal: while `vision serve`
runs, `/session` in a `vision` chat gets a **Phone** tab listing its chats; pick one and the terminal
follows it (`vision/remote.py`). The server keeps running the conversation, what is typed in either
place shows in both, and the terminal's own chat drops out of the phone's list while it is joined.
`/model` and `/effort` switch the chat on the server; `/new` or resuming another session leaves it
(the phone chat carries on). Resuming a phone chat's session id the old way would give you a second
copy of the conversation; joining gives you the same one. The apps can do the joining for you: the
terminal button on a chat (the phone's, or the desktop app's title bar and **Chat › Open in Terminal**,
Ctrl+Shift+T) opens it in a terminal window on the computer with `vision --join` (`open_terminal`).

### Projects

A project is a goal, a folder and standing instructions around one chat of `vision serve`'s. The
project's chat works in that folder and is told the goal and instructions on every turn, whichever
provider runs it. (Instructions that belong to a folder rather than a piece of work go in a
`.vision.md` there instead, see `/instructions` above; a project chat reads both.) On the phone, projects sit above the chats in the drawer: **New project** starts
one, a tap opens its chat (on the machine too, if it isn't open), and a long press archives it. From
a terminal:

```bash
vision project new "fix the flaky server tests" --dir ~/code/vision --instructions "never push"
vision project list            # --all includes archived ones
vision project show <id>       # ids can be shortened to their first letters
vision project edit <id> --instructions "…"   # the chat takes it from its next turn
vision project open <id>       # joins the project's chat in this terminal
vision project archive <id>    # off the lists; its chat carries on as an ordinary one
```

The quicker way in is from the chat you're already in. `/project` in a `vision` chat makes that chat a
project's, as it is: the conversation carries on, in the same folder, and from its next turn it's the
project's coordinator (below). The goal is what you first asked for in the chat, or `/project <goal>`
names it; the title is the chat's. A chat of this terminal's own is opened in `vision serve` first and
joined, so its threads can report to it after the window is gone (`/new` leaves it, as with any joined
chat). In a fresh chat with nothing said, `/project` joins the project this folder already has, and
`vision` says at start when the folder has one.

Projects live in `~/.local/share/vision/projects/<id>/`. Archiving deletes nothing.

**Threads** are background workers in a project. Each one runs outside any chat turn and keeps going
while you're away. A thread that edits gets its own git worktree on its own branch (under
`~/.local/share/vision/worktrees/`), so threads don't trip over each other. It commits there and
never pushes. Up to `max_threads` (3) run at once and the rest queue.

The project's chat is the **coordinator**. Brief it the way you'd brief a lead: it does quick things
itself, hands longer or parallel work to threads, sends follow-ups to the thread that owns the work,
and reviews what comes back. Every turn it gets a hidden view of the threads as they are now and the
project memory. When threads stop, it gets a turn of its own with their results (the chat shows a
one-line note, not your words), and decides what's next: carry a thread on, start the next piece, or
tell you. How freely it starts threads is the project's `autonomy` (ask, balanced, free), and how
much it says after a report is `updates` (all, milestones, needs_you). After 10 reports in a row
without a word from you, it stops and asks. On a call, the voice model starts and messages threads by
name ("tell the tests thread to skip the flaky one"), answers "how's it going?" from the thread table,
and says each report in a line.

Threads add what they decide to the **project memory** (`memory.md`), which the coordinator and every
later thread see. A thread can build on earlier ones (`--after`). It gets their results, never their
transcripts. Files threads make for you, and what you send in the project's chat, go to the project's
**Library**. **Landing** merges a finished thread's branch into the project folder's current branch
(a merge commit) and closes the thread. It refuses when either side has uncommitted changes, and a
conflict is aborted and reported, so nothing is left half-merged. The coordinator lands only when you
say so, unless autonomy is `free` and the thread's checks passed.

Every project is also a **scout** in the apps: it gets a name (Atlas, Juniper, Wren…), a colour and a
trait for its avatar, a small creature whose mood shows what the project is doing: asleep, curious
(a thread is reading), focused (one is editing or running things), needs you, still waiting (for more
than three hours), proud (it landed something in the last hour) or worried (a thread failed). Its page
lists what its threads did today, from `activity.jsonl` in the project's folder (the last 200 events).
The desktop app shows scouts in its sidebar. A scout's **leash** says how far it goes on its own: On call
(it works only when you give it something), Check-ins (the default: it works alone on its schedule, and
anything with consequences asks first) or Autonomous (it takes those steps itself, within its rules).
`autonomy` is the leash's older name (ask, balanced, free) and still works.

**Rules** say, per action, whether the scout does it, asks first or never does: landing a branch into the
project folder, pushing or opening a PR, sending anything outside the computer (comments, mail,
messages), and deleting files outside its own folder or installing software. Each follows the leash
(only pushing differs: Autonomous does it, Check-ins asks) unless you set it yourself. They're enforced,
not just suggested, for Claude and local models: the coordinator and its threads get them as permission
rules, and a call that asks first becomes an **approval request** in the scout's Needs you, with the
command and why it asks. Approve, edit the command and approve it, or deny; the thread carries on with
other work meanwhile and is told to run it once you approve (that one call, once). Never is refused
outright. An `rm -r` inside the thread's own worktree doesn't ask. Codex, Grok and ACP agents get the
rules in their prompt only. Requests are kept in `approvals.json` beside the project. `scout_rules` under
`[brain]` (Settings › Permissions › Scouts) sets an action for every scout that hasn't set its own, in
place of the leash's default: `scout_rules = { publish = "asks" }`.

```bash
vision project set <id> leash=autonomous rule.publish=never rule.send=default
```

**New scout** in the apps asks one thing: what should it do? Say when in the same words ("keep the tests
green, check every morning", "every weekday at 7, give me three tech stories", "fix the flaky test when I ask")
and the schedule is read out of them, with what's left as the goal; a line beneath shows the name, the goal
and when it'll report before you send it (`POST /scouts/draft`, then `POST /scouts {text, workdir?}`). Nothing
said about when means every morning at 8. More options opens the full form with all of it filled in.

**Needs you** gathers what every scout is waiting on you for into one list, newest first (`GET /needs`): requests
to run something (Approve, or Deny; Edit the command first on the desktop, from a long press on the phone),
questions a thread asked (answer right there) and finished work (Apply lands its branch; on the phone, swipe a
row for Approve, Deny or Apply). A row opens its scout. On the desktop the Scouts heading's count opens the list;
on the phone, the "3 waiting on you" row atop the Scouts page.

On the phone, the drawer's **Scouts** page lists them by their buddies, with what needs you first. A
scout's chat shows its check-ins and thread reports as cards; the pill at its top opens its sheet: its
requests to approve (or edit, or deny), its threads, Library and memory, and Check in now. Its settings
hold its name, colour and look, and **Leash and check-ins** (the leash, each rule, the schedule). A
request's notification has Approve and Deny on it, and a finished thread's has Land; they work from the
Lock Screen without opening the app. The Scouts widget shows each scout's buddy and what needs you.

A scout **checks in** on a schedule you give it (every morning at 8, weekdays at 9, every two hours) or
when you press Check in now. A check-in is a turn of the project's chat on its own: it's told what
changed since the last one (what its threads did, new commits in the folder, how long the computer
slept), works on the goal as its autonomy allows, and ends with a short report, which the apps show as
a card. One push follows if something needs you (every time with `notify all`, never with `none`). A
check-in is skipped while the project is paused or on call (ask), stops after the project's `length`,
and one missed while the computer slept runs once when it wakes. Check-ins are kept in
`checkins.jsonl` beside the project; their schedules are tasks of kind `checkin` in the scheduled list.

Pushes follow `notify`: a thread asking always pushes, a failure pushes once (not on every retry), a
finished thread only with `notify all`. A tap opens that thread. **Pausing** a project stops its
threads where they are; resuming carries each on in its own session.

```bash
vision thread new "add the events endpoint"   # --read-only: no worktree · --after <id>,<id>: builds on them
vision thread list                            # --project <id> outside a project chat
vision thread show <id>                       # its result, branch and recent runs · --history: its conversation
vision thread tell <id> "use SQLite"          # answers it, steers it or gives it more to do (a closed one: a new thread after it)
vision thread land <id>                       # merge its branch into the project's folder and close it
vision thread open <id>                       # its own session in its provider's CLI (when it isn't working)
vision thread stop <id>
vision thread close <id>                      # its worktree goes unless it holds unmerged work
vision project threads <id>                   # the same as `vision thread list`
vision project remember "dates are UTC"       # into the project memory · `vision project memory [--forget N]`
vision project pause <id> / resume <id>
vision project set <id> autonomy=free notify=all max_threads=4 thread_model=sonnet
```

In a joined project chat, `/threads` lists them and `/tell <id> <text>` messages one. On the phone the
pill at the top of a project's chat opens the project: threads grouped by state, its Library and its
memory, with settings and Pause in the menu. A thread's page lands it, shows its conversation, and
its field answers it or steers it. Threads are kept short so they don't burn your plan: a follow-up to
one whose session has grown long (past 120k tokens of context or 6 goes) or sat 12 hours gets a fresh
session with a short handover, a closed thread isn't reopened (a message to it starts a new thread
after it), and threads that finished without changes are closed after a day idle. The project's chat
does the same for itself: past 100k tokens or 12 hours idle its next turn starts a fresh session with
a short recap (the chat on screen keeps everything), and threads that finish within a minute and a half
of each other report in one turn; one with a question or a failure reports at once. `vision restart` doesn't wait for threads: they pick
their sessions back up when Vision is back, and reports owed to the coordinator are made then.

## Layout

```
vision/brain.py    Claude Code headless driver (streaming, sessions)
vision/codex.py    OpenAI Codex headless driver (JSONL, threads, usage)
vision/grok.py     Grok CLI headless driver (streaming-json, sessions, usage)
vision/conversation.py  tool-free Claude voice conversation and delegation loop
vision/delegation.py    structured task/result contracts and the silent worker prompt
vision/weather.py  Apple WeatherKit client: signed tokens, geocoding, the spoken report the voice loop prefetches
vision/models.py   shared model catalogue, provider routing, effort capabilities
vision/link.py     terminal chats announce themselves so `vision serve` lists and drives them from the phone
vision/remote.py   a terminal joins a chat `vision serve` runs (the phone's): one conversation, read from both
vision/tts.py      Speaker: Qwen3-TTS engine (streamed frames, cloned/designed voices, WSOLA rate) and the
                   Orpheus engine (llama-server + SNAC); markdown→speech cleanup, streaming sentence player
vision/talker.py   CUDA-graph decode loop for the Qwen3-TTS talker (static caches, Gumbel-max sampling)
vision/stt.py      faster-whisper (CUDA with CPU fallback), hallucination filtering
vision/audio.py    mic capture with Silero/WebRTC VAD end-pointing / push-to-talk
vision/wake.py     wake word: a tiny CPU Whisper spots "Vision" at the start of an utterance, hands it to the voice loop
vision/cli.py      Typer CLI
vision/onboard.py  the first run: a chain of steps (providers, you, voice, phone), resumable, `vision onboard`
vision/welcome.py  the provider tick list and the voice offer (`vision providers`, `vision voice setup`)
vision/service.py  `vision serve` as a background service: systemd user unit or launchd agent
vision/voicecheck.py  `vision voice check`: the speakers, the microphone and the wake word, after an install
vision/ui.py       input box, model picker, message rendering (prompt_toolkit + rich)
vision/buddy.py    the buddy in half-block pixels: face()/mini() for any scout, Buddy for the chat's gutter
vision/persona.py  Vision's system prompt (text vs voice output rules)
vision/config.py   config + paths
vision/server.py   `vision serve`: HTTP + WebSocket API for the iOS app (token, pairing QR, history)
```

Data: models in `~/.local/share/vision/models`, Whisper weights in `~/.cache/huggingface`,
config in `~/.config/vision/config.toml`, chat history and last session in `~/.local/state/vision`.

## Notes

- `vision setup` fetches Qwen3-TTS Base (4.3 GB) into the Hugging Face cache and, if the configured
  voice is one of the built-in designs and does not exist yet, fetches VoiceDesign (4.3 GB) and makes it.
  With the `voice-nvidia` build, PyTorch brings its own CUDA 13 runtime (`nvidia-*-cu13`); Whisper
  (CTranslate2, int8_float16) and the Silero VAD stay on the pip `nvidia-*-cu12` libraries with
  `onnxruntime-gpu` 1.22, and the two coexist. The `voice-cpu` build has none of those: CPU torch from
  PyTorch's index, Whisper on int8 with `small.en` in place of the large models. `onnxruntime-gpu` only
  ships x86_64 Linux wheels, so on macOS and ARM Linux both builds take the plain `onnxruntime` instead.
  `vision setup --orpheus` adds the Orpheus GGUF (2.4 GB), the SNAC decoder and a prebuilt CUDA 13
  llama.cpp. CPU fallback is automatic but several times slower than real time, so only fit for `vision say`.
- Hands-free mode listens only after Vision has finished speaking, so machine speakers work, but a headset
  gives cleaner end-pointing.
