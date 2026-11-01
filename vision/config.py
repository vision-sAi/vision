"""Configuration for Vision.

Config lives in ~/.config/vision/config.toml (created with defaults on first run).
Speech models and Vision's working directory live under ~/.local/share/vision/.
"""
from __future__ import annotations

import contextlib
import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from vision.models import THINKING_OFF, provider_for
from vision.providers import PROVIDER_NAMES, REGISTRY, load_plugins, register_endpoints

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vision"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "vision"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "vision"
CONFIG_PATH = CONFIG_DIR / "config.toml"
# Vision's voice and example exchanges; replaces the built-in personality when it has any text.
PERSONALITY_PATH = CONFIG_DIR / "personality.md"
MODELS_DIR = DATA_DIR / "models"
WORKSPACE_DIR = DATA_DIR / "workspace"

LLAMA_DIR = DATA_DIR / "llama"
# Cloned / designed voices for the Qwen3-TTS engine: one folder per voice holding ref.wav (the clip the
# model imitates), ref.txt (its transcript) and, for designed voices, design.txt (the description).
VOICES_DIR = DATA_DIR / "voices"

# Qwen3-TTS 1.7B (Alibaba, Apache 2.0): Base clones any voice from a few seconds of audio; VoiceDesign
# invents a voice from a text description. Both come from the Hugging Face hub into its cache.
QWEN_TTS_BASE = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
QWEN_TTS_DESIGN = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
# The same models converted for MLX, used on Apple Silicon (the voice-mac build). Best first: `vision setup`
# times each on the Mac and keeps the first that runs comfortably faster than real time.
MLX_TTS_TIERS = (
    "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
    "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-4bit",  # ~0.45
    "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",  # ~0.45, lighter and quicker to start
)
MLX_TTS_DESIGN = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit"
# Whisper through MLX on Apple Silicon, by [listen] whisper_model. Large-v3-turbo 8-bit makes ~30 %
# fewer mistakes than small.en on the CPU (LibriSpeech: 3.9 % vs 5.7 % WER; 18 % vs 25 % with babble) and is
# a little faster; 8-bit matches fp16's accuracy in half the memory.
MLX_WHISPER = {"large-v3-turbo": "mlx-community/whisper-large-v3-turbo-asr-8bit"}
# Built-in voice designs: `vision voice design <name>` uses these when no description is given.
_DESIGN_LINE = (
    "Good afternoon. All systems are online and running within normal parameters. "
    "I've taken the liberty of reviewing today's schedule; nothing that can't be handled with a little finesse."
)
VOICE_DESIGNS = {
    "jarvis": (
        "A refined adult male voice with a distinctly British Received Pronunciation accent: crisp, clipped "
        "English consonants and rounded vowels, like a well-spoken Londoner. Low-to-mid pitch, warm and smooth "
        "timbre, precise articulation at a brisk, efficient conversational pace. Composed and quietly confident, "
        "with the faintest hint of dry wit: a discreet, highly capable assistant who is never flustered.",
        _DESIGN_LINE,
    ),
    "narrator": (
        "A mature British male narrator with a mellifluous, theatrical delivery in Received Pronunciation. "
        "Rich mid-to-low pitch, velvety timbre, immaculate diction. A fast talker: rapid, energetic, fluent "
        "delivery at a quick conversational clip, words tumbling out briskly with no dramatic pauses and no "
        "drawn-out vowels. Warm, wry and faintly amused, with a gently condescending gravitas, as if narrating "
        "a story whose every outcome he already knows.",
        "Stanley walked through the red door, exactly as he had been told. Correct choice, naturally. The narrator "
        "noted, with quiet satisfaction, that for once somebody was actually listening.",
    ),
    "friday": (
        "A calm, clear adult female voice with a light Irish accent. Mid pitch, bright and friendly timbre, "
        "crisp articulation at a relaxed conversational pace. Warm, quick-witted and self-assured, "
        "like a capable assistant who enjoys the work.",
        _DESIGN_LINE,
    ),
}

# Orpheus 3B (Canopy Labs' Llama-based TTS) as a Q4_K_M GGUF, run by llama.cpp's server, plus the SNAC
# codec decoder (ONNX) that turns its audio tokens into 24 kHz sound.
ORPHEUS_MODEL_FILE = "orpheus-3b-0.1-ft-q4_k_m.gguf"
ORPHEUS_MODEL_URL = "https://huggingface.co/isaiahbjork/orpheus-3b-0.1-ft-Q4_K_M-GGUF/resolve/main/orpheus-3b-0.1-ft-q4_k_m.gguf"
SNAC_MODEL_FILE = "snac_24khz_decoder.onnx"
SNAC_MODEL_URL = "https://huggingface.co/onnx-community/snac_24khz-ONNX/resolve/main/onnx/decoder_model.onnx"
# Prebuilt llama.cpp (CUDA 13 build for Linux x64; the cudart bundle carries the CUDA runtime it needs).
LLAMA_BUILD = "b11007"
LLAMA_URLS = [
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/llama-{LLAMA_BUILD}-bin-ubuntu-cuda-13.3-x64.tar.gz",
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/cudart-llama-{LLAMA_BUILD}-bin-ubuntu-cuda-13.3-x64.tar.gz",
]

DEFAULT_CONFIG = '''# Vision configuration. Edit freely; run `vision config` to see the resolved values.

[brain]
# Default model (Claude alias, Codex slug, or Grok id) and effort level.
# Claude's top tier is "ultracode"; Codex's is "ultra"; Grok uses its advertised effort list.
# Change them with `vision default` or /default inside a chat; /model and /effort change only the current session.
model = "opus"
effort = "medium"
# The tools Vision describes to the model. Claude's auto mode has every tool regardless (only
# denied_tools and plan mode restrict it); Codex maps this list to the sandbox below.
# Bash = shell commands, Read/Glob/Grep = read files, Write/Edit = create and change files.
allowed_tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
# Claude command patterns Vision may never run, even with Bash allowed. Syntax: Bash(prefix:*)
# Codex receives these as prompt guidance; choose its read-only sandbox for mechanical enforcement.
denied_tools = ["Bash(sudo:*)", "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)", "Bash(mkfs:*)", "Bash(dd:*)", "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(claude:*)", "Bash(codex:*)", "Bash(grok:*)"]
# Patterns that pause for your say-so in auto mode: a matching call shows a Yes / No selector (here and
# on the phone) before it runs, and everything else runs as usual. Same syntax as denied_tools, plus a
# bare tool name or a path glob: ask_tools = ["Bash(git push:*)", "Bash(rm -rf:*)", "Edit(.env)", "Write"].
# Enforced for Claude (its own ask rules) and the servers Vision runs tools for ([local] and
# [providers.*]); Codex, Grok and ACP agents are asked to check with you first but nothing stops them.
ask_tools = []
# Exceptions to ask_tools: a call one of these covers runs without asking even though an ask rule caught it
# (denied_tools still wins). Same syntax, enforced where ask_tools is, in chats that aren't a scout's.
allow_tools = []
# What scouts may do on their own when a scout hasn't set its own rule, per action (land, publish, send,
# destructive): "does", "asks" or "never", e.g. { publish = "asks" }. Left out, it follows the scout's leash.
scout_rules = {}
# Directory Vision works in. Empty = the directory you launch `vision` from.
workdir = ""
# A file of instructions for the folder Vision works in: this name is looked for in the working directory
# and the folders above it up to the repository root or your home folder, and every brain reads what it
# finds (a repo-wide one and a subfolder's both apply, the nearest last). /instructions shows them. "" = off.
folder_instructions = ".vision.md"
# How you'd like Vision to address you now and then (e.g. your first name). Empty = nothing special.
# Its personality lives in ~/.config/vision/personality.md (see the README); delete that file for the default.
address_user_as = ""
# Starting mode. "auto": every tool runs without approval (denied_tools and ask_tools still apply; Codex gets
# danger-full-access). "plan": read-only — Vision investigates and proposes a plan, and carries it out
# once you approve it (Claude) or switch to auto. Shift-Tab or /mode switches during a chat.
mode = "auto"
# Approving a plan: "session" switches to auto for the rest of the session; "turn" runs only the
# approved plan and goes back to plan mode for your next message.
plan_approval = "session"
# Banked limit resets (/usage) and Grok's allowance come from undocumented endpoints that Vision calls
# with the logins Claude Code (~/.claude/.credentials.json) and Grok (~/.grok/auth.json) saved.
# false = Vision never reads those files; /usage then shows only what the CLIs report themselves.
read_cli_logins = true
# Stall watchdog for Claude turns: seconds without any output from the brain before the turn is
# killed and reported as stalled. Long tool calls still stream events; only true silence counts.
# 0 disables it.
stall_s = 900
# How long the Claude Code process (and an ACP agent's) is kept between typed turns, so the next
# reply skips its start-up (a second or two). It restarts by itself when the model, effort, mode,
# directory or conversation changes. 0 = a fresh process every turn, as before.
keep_alive_s = 600
# Before each reply in a git repository, Vision records the working tree (tracked and untracked
# files; nothing is committed and the index is left alone), so /diff shows what the model changed
# and /rewind puts it back. false = no checkpoints.
checkpoints = true
# When a turn fails because the provider's usage limit is hit: "ask" offers the other providers that
# are set up (the transcript so far carries over and the message is sent again), "off" leaves it to
# you, or a list such as ["codex", "grok"] switches to the first of those that is ready without asking.
failover = "ask"
# The input box's corner shows how much of the provider's tightest window is left once a turn has
# reported it; at or under this percentage it turns yellow and a note says when the window resets.
quota_warn_pct = 15

[memory]
# Leaving a chat (/quit, Ctrl-D) after enough turns reviews it: one tool-free call lists the durable
# facts it left (about you, your machines, accounts, projects; decisions; corrections) and you tick
# the ones to keep. "ask" = that; "off" = never. /review and `vision memory review` run it any time.
review = "ask"
# Chats shorter than this many replies leave without a review.
review_after_turns = 4
# How long the review call may take before the chat just leaves.
review_timeout_s = 45

[providers]
# Which providers /model offers, in this order: "claude" (Claude Code), "codex" (Codex CLI), "grok"
# (Grok CLI), "local" (the llama-server in [local]). /providers picks them. One that is listed but not
# set up yet (CLI not installed, server not answering) shows in /model with how to set it up. Leave the
# line out to offer every provider, including the ones added below.
enabled = ["claude", "codex", "grok", "local"]

# More servers: any OpenAI-compatible chat endpoint (Ollama, LM Studio, vLLM, llama-server, OpenRouter,
# any hosted API) as its own provider, run the way [local] is: Vision runs its tools itself, nothing goes
# through a CLI. Its models show in /model as <name>/<model> and come from the server's /models unless
# `models` lists them. The API key is read from the environment variable in api_key_env (or api_key).
#
# [providers.ollama]
# base_url = "http://localhost:11434/v1"
# label = "Ollama"
# context = 32768
#
# [providers.openrouter]
# base_url = "https://openrouter.ai/api/v1"
# api_key_env = "OPENROUTER_API_KEY"
# models = ["anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"]
#
# An agent that speaks ACP (Gemini CLI and others) is `type = "acp"` with its command; it brings its
# own tools and Vision answers its permission prompts (see docs/guide.md, "Choosing providers").
#
# [providers.gemini]
# type = "acp"
# command = ["gemini", "--experimental-acp"]

[codex]
# Sandbox used for GPT/Codex models in auto mode: "auto" (= danger-full-access, matching Claude's auto
# mode), "workspace-write" (shell and file changes only inside the working directory) or "read-only".
# Plan mode is always read-only.
sandbox = "auto"
# Optional raw `codex exec -c key=value` entries. Most users should leave this empty.
extra_config = []
# How turns run: "app-server" (messages can go into a running reply, text streams in) or "exec"
# (`codex exec --json`, one message per turn, replies arrive whole).
transport = "app-server"

[grok]
# Sandbox used for Grok models in auto mode: "auto" (= off, unrestricted, matching Claude's auto mode),
# "workspace" (write only inside the working directory), "read-only" or "strict".
# Plan mode is always read-only. Grok enforces this with its kernel sandbox.
sandbox = "auto"
# Optional extra `grok` CLI flags for each headless turn. Most users should leave this empty.
extra_args = []
# How turns run: "acp" (`grok agent stdio`: messages can go into a running reply, live tool rows) or
# "headless" (`grok -p`, one message per turn). Worker turns always run headless.
transport = "acp"

[local]
# An OpenAI-compatible server hosting the Local models: llama.cpp's llama-server (port 8080), Ollama
# (http://localhost:11434/v1), LM Studio (http://localhost:1234/v1), vLLM. Spoken to directly, no CLI, nothing
# leaves your network; its models show in /model under the names the server reports. Vision runs their tool
# calls itself (Bash, Read, Write, Edit, WebSearch, WebFetch, as listed in [brain].allowed_tools). Point it at
# the machine running the server; a Tailscale name works away from home.
base_url = "http://localhost:8080/v1"
timeout_s = 180
# The model's context size on the server; the conversation is trimmed to stay inside it.
context = 131072
# WebSearch asks DuckDuckGo (no account) unless a Brave Search API key is set here (free tier at
# brave.com/search/api), which is steadier and rate-limited per key rather than per IP.
brave_api_key = ""

[conversation]
# Voice input goes to a conversation model with tools, hooks, skills and MCP disabled. It decides
# when to delegate and speaks the worker's results. It is the chat's own /model whenever that is a
# Claude, Codex or Local model, so one model answers the whole chat; the model below talks only for
# chats on Grok, which cannot hold a voice conversation. A Claude model uses your Claude subscription,
# a Codex model your ChatGPT one; a Local model runs on the server in [local] for free.
model = "sonnet"
# A Local conversation model never thinks (its replies are grammar-constrained), so its effort is always off.
effort = "low"
# Let the voice model search and read the web itself (read-only WebSearch/WebFetch) instead of
# delegating live facts such as the weather to the CLI worker. Everything else stays off.
web = true
# Maximum delegated tasks per spoken turn; the final response must just speak.
max_delegations = 3
# Timeout for each conversational response (the CLI worker keeps its normal lifetime).
timeout_s = 120
# Record stage durations only (no speech or tool content) in ~/.local/state/vision/voice-timing.jsonl.
timing = false

[router]
# The front end: the conversation model (a local model, or a Claude) takes every request, answers the
# basic ones itself, fetches the weather from WeatherKit, runs a search-only web lookup for current
# facts, and hands everything substantive to an agent launched by Vision's supervisor (vision/supervisor.py).
#   "off"    the conversation model decides delegation on its own, as before, on the session's brain
#   "audit"  routes are worked out and logged (~/.local/state/vision/routing.jsonl) but nothing
#            changes: review them, then switch on
#   "on"     routes are enforced; substantive work goes to default_agent at default_effort
# /router off|audit|on switches for the session.
mode = "off"
# With mode = "on", typed messages go through the front end too (false: only spoken ones; typed input
# keeps talking straight to the /model brain).
typed = true
# Where substantive work goes unless the user says otherwise ("use Codex high for this",
# /agent codex --effort high). Architecture, hard debugging, repo-wide changes, security-sensitive work,
# multi-step research and tasks that already failed at the default effort get high_effort instead.
default_agent = "opus"
default_effort = "medium"
high_effort = "high"
# The agents the supervisor may launch: name = catalogue model (see /model). "" for codex means the
# first Codex model in the catalogue. Anything else the user or the model names is refused, never
# swapped for something else.
[router.agents]
opus = "opus"
codex = ""
[router.limits]
# A run is stopped and reported as timed out after this many seconds.
timeout_s = 1800
# How many times one run may come back to you with a question before it is stopped.
max_rounds = 4
# A run that has written more than this many tokens is not resumed after a question.
max_output_tokens = 200000
[router.permissions]
# Commands the launched agent may not run on its own: it comes back with a question, you answer yes,
# and the run resumes with that command allowed. Syntax as denied_tools. The rest of [brain].denied_tools
# stays forbidden outright.
approval = ["Bash(git push:*)", "Bash(pip install:*)", "Bash(uv pip install:*)", "Bash(npm install -g:*)", "Bash(apt:*)", "Bash(apt-get:*)", "Bash(dnf:*)", "Bash(brew:*)", "Bash(rm -rf:*)", "Bash(gh pr create:*)", "Bash(gh release:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)", "Bash(docker push:*)", "Bash(kubectl apply:*)", "Bash(terraform apply:*)", "Bash(mail:*)", "Bash(sendmail:*)", "Bash(curl -X POST:*)", "Bash(curl -d:*)"]

[voice]
# false: Vision loads no voice and no speech recognition, so the GPU stays free; spoken replies, calls,
# dictation and the wake word wait until it is back on. `/voice off|on` in the terminal or the switch in the
# phone's Settings change it, for the background service and every terminal at once.
enabled = true
# Speech engine: "qwen3" (Qwen3-TTS 1.7B: cloned or designed voices, the most natural) or
# "orpheus" (Orpheus 3B via llama.cpp, eight built-in voices).
engine = "qwen3"
# qwen3: the name of a voice under ~/.local/share/vision/voices (make one with `vision voice design jarvis`
# or `vision voice add <name> --from clip.wav`), or a path to a WAV clip to imitate.
# orpheus: a preset ("friday") or one of tara, leah, jess, leo, dan, mia, zac, zoe.
# Run `vision voices` to list what is available, `--preview` to hear them.
voice = "jarvis"
# Language the qwen3 engine speaks ("Auto" detects; English, Chinese, German, French, Spanish, ...).
language = "English"
# How qwen3 imitates the reference clip: "embedding" keeps its timbre and lets the model shape the delivery
# itself (cleanest, the default); "context" continues the clip's own audio, closer to its exact pacing
# and tone but it also inherits any roughness in the clip (needs ref.txt, the transcript).
clone_mode = "embedding"
# qwen3: each sentence continues from what the voice just said (its own audio is the context), so a reply
# flows as one performance instead of a string of separately read sentences. false = every sentence cold.
continuity = true
# Speaking rate applied to the audio after synthesis (pitch unchanged): 1.0 = as spoken, 1.15 = brisk,
# 1.3 = hurried. Fine-tune pace without changing the voice.
rate = 1.0
# Where the voice model runs: "auto" tries the GPU (CUDA) then falls back to CPU (and says why, e.g. another
# Vision still holding the GPU). "cuda" never falls back: the voice refuses to load and names what is in the way.
# On the CPU these models are several times slower than real time; the GPU is the intended home.
# On Apple Silicon with the voice-mac build, "auto" (or "mlx") runs qwen3 on the Mac's GPU through MLX.
device = "auto"
# qwen3 on the GPU: "none" keeps the voice model's weights in bf16 (the default). "int8" stores the
# transformer weights as 8-bit integers: about 1.4 GB less VRAM and inaudible, but the lighter load can let
# the driver drop the GPU clocks, which can make it slower; see vision/int8.py.
quant = "none"
# qwen3 through MLX (Apple Silicon): the model to use. Empty = the one `vision setup` picked by timing this Mac
# (see `vision doctor`); or name one of mlx-community's Qwen3-TTS-12Hz Base conversions.
mlx_model = ""
# orpheus only: local port for the llama.cpp server that hosts the voice model (127.0.0.1 only).
port = 8766
# Output device index or name substring (a PipeWire sink name such as "Snowball" works too). Empty = system default.
output_device = ""
# A spoken conversation: when the reply's first words have not started filler_after_ms after you stop
# talking (transcription and the model's own thinking time), the voice says a short "hmm" instead of
# leaving dead air, then the reply follows. The phrases are made in the current voice at warm-up and kept
# under ~/.local/state/vision/fillers, so they cost nothing on the turn. With several listed, one is picked
# at random, never the same one twice running. false turns the filler off.
filler = true
filler_after_ms = 700
filler_phrases = ["Hmm."]

[listen]
# Input device index or name substring (a PipeWire source name such as "Snowball" works too).
# Empty = system default (follows your PipeWire default source).
input_device = ""
# Whisper model: "large-v3-turbo" (GPU, best), "distil-large-v3", "small.en", "base.en" ...
whisper_model = "large-v3-turbo"
# "auto" tries CUDA then falls back to CPU. Or force "cuda" / "cpu". On Apple Silicon with the voice-mac
# build, "auto" (or "mlx") runs large-v3-turbo on the Mac's GPU through MLX; without it, "mlx" falls back like "auto".
device = "auto"
# Voice activity detection aggressiveness 0-3 (3 = strictest, best in noisy rooms).
vad_aggressiveness = 2
# Milliseconds of silence that ends an utterance.
end_silence_ms = 900
# Give up waiting for speech after this many seconds (0 = wait forever).
start_timeout_s = 0
# Maximum utterance length in seconds.
max_utterance_s = 45
# Play a short chime when Vision starts listening.
chime = true
# Show the words forming on screen while you talk when Whisper is on the GPU (re-transcribes the
# utterance so far every live_ms; 0 turns it off). CPU preview is skipped so it cannot stall /talk.
live_ms = 600
# Cut a reply short by voice while Vision is speaking (Esc / Ctrl-C always work):
#   "wake"   say Vision's name ("Vision, stop" / "Vision, what about…" runs straight away). Works on
#            speakers: a tiny Whisper on the CPU listens for the name over Vision's own voice.
#   "speech" just start talking (~a quarter second of voice). Needs headphones or echo cancellation
#            (`pactl load-module module-echo-cancel`, then input_device = "echo-cancel"), or Vision
#            hears itself through the speakers and cuts itself off.
#   "off"    keyboard only.
barge_in = "wake"
# Only act on your voice. `vision voice enrol` records a few phrases; with only_me = true every utterance
# the mic hears (/talk, the wake word, cutting in, /listen) is compared with them by a small speaker
# model on the CPU (WeSpeaker, about 30 ms) and anyone else's is dropped with a note. The score is a
# cosine similarity: your own voice scores about 0.6-0.8, other people 0.0-0.3. Raise the threshold if
# someone else gets through, lower it if you are turned away (`vision voice enrol --check` shows yours).
only_me = false
only_me_threshold = 0.45

[wake]
# Say Vision's name to start talking without touching the keyboard (also /wake inside a chat).
# While the chat is idle a tiny Whisper on the CPU listens for the name; when it hears it the full ears
# and voice warm up and Vision listens ("Vision, what's the weather?" runs straight away).
enabled = false
# Spellings that count as the name (matched loosely: "Vision", "vision's", "envision" all wake).
names = ["vision"]
# The small Whisper that spots the name. It runs on the CPU, so keep it tiny.
model = "tiny.en"
# After a reply, keep listening this many seconds for a follow-up before going back to sleep.
follow_up_s = 8

[buddy]
# Vision's buddy, the little creature with a sprout in the bottom-left input gutter (the apps draw the
# same one). Set enabled = false to hide it.
name = "Vision"
enabled = true
slide_in = true         # it slides in from the left when a chat opens; false = it is just there

[chat]
# A desktop notification (notify-send on Linux, Notification Center on a Mac) when a reply that took
# longer than this many seconds finishes while the terminal is not the focused window, so you can
# leave a long job and come back. 0 = never. Terminals that don't report focus (most do) notify
# past the threshold regardless.
notify_after_s = 30

[remote]
# `vision serve`: the HTTP + WebSocket server the Vision Remote iOS app talks to.
# 0.0.0.0 = reachable from the phone on the same Wi-Fi (the QR carries the machine's LAN address);
# 127.0.0.1 = machine only, for use behind Tailscale Serve (`tailscale serve --bg 8765`: your own devices
# only, and Vision puts its https address in the QR) or a public tunnel (Tailscale Funnel, Cloudflare Tunnel).
# The pairing token lives in remote_token next to this file (`vision serve --new-token` rotates it).
host = "0.0.0.0"
port = 8765
# The https:// address the phone should use away from home (your tunnel hostname).
# Empty = detect Tailscale, else the LAN address.
public_url = ""
# After a reply, guess your next message (one small Haiku call) and offer it in the phone's empty composer.
suggestions = true
# The folder a chat started from the phone or the desktop app begins in (their Settings > New chats sets it).
# Empty = [brain] workdir, else the folder `vision serve` was started in. The terminal keeps to [brain].
new_chat_folder = ""
# The mode such a chat starts in: "auto" or "plan". Empty = [brain] mode.
new_chat_mode = ""

[weather]
# Live weather. When a spoken request is about the weather, Vision fetches the report while the voice
# model starts thinking and hands it over as data, so nothing is web-searched. `vision weather [place]`
# tests it. It works with no account: the US National Weather Service answers for US places and
# Open-Meteo for the rest. Only `location` below needs filling in, for "the weather" with no place named.
# Optional: Apple WeatherKit instead (included with an Apple Developer membership: 500,000 calls a
# month), for measured current conditions and alerts in more countries. It takes over once the three
# ids are filled in. Setup at developer.apple.com: Certificates, Identifiers & Profiles → Keys → new key with WeatherKit
# enabled (download the .p8 once; it lives next to this file as weatherkit.p8), and Identifiers → Services
# IDs → a new identifier with WeatherKit enabled. Then fill in the three ids below.
enabled = true
team_id = ""       # 10-character Team ID (top right of the developer account page)
service_id = ""    # the Services ID identifier, e.g. com.example.vision.weatherkit
key_id = ""        # the Key ID shown on the WeatherKit key
key_file = "weatherkit.p8"   # path to the .p8; relative paths are under ~/.config/vision
# Where "the weather" means when no place is named. A place name is geocoded once (Open-Meteo, no key)
# and cached; or give latitude/longitude (and timezone) directly to skip geocoding.
location = ""
latitude = 0.0
longitude = 0.0
timezone = ""      # e.g. Europe/London; only used with latitude/longitude
country_code = ""  # e.g. GB; enables official weather alerts when using latitude/longitude
units = "metric"   # "metric" (°C, km/h) or "imperial" (°F, mph)
language = "en"

[push]
# Notifications on the iPhone app while it is shut: reminders (`vision remind`) and the replies of
# scheduled tasks. Apple only accepts pushes signed with the key of the account the app is built under,
# so either set `relay` to a push relay that holds that key, or, under your own Apple Developer account,
# make a key there:
# Certificates, Identifiers & Profiles → Keys → new key with Apple Push Notifications service (APNs)
# enabled. Download the .p8 once, keep it next to this file as apns.p8 and fill in the rest below; Vision
# then pushes straight to Apple. The app registers itself with `vision serve` when it connects. Each
# notification is encrypted here with a key only this machine and the phone have, so the relay and Apple
# carry text they cannot read.
enabled = true
relay = ""         # the address of a push relay, used when no key of your own is set below
team_id = ""       # 10-character Team ID; empty = the one under [weather]
key_id = ""        # the Key ID shown on the APNs key
key_file = "apns.p8"   # path to the .p8; relative paths are under ~/.config/vision
topic = ""         # the app's bundle id, e.g. com.example.VisionRemote
'''

VOICE_ENGINES = ("qwen3", "orpheus")
# The voices baked into the Orpheus fine-tune, roughly in order of how polished they are.
VOICES = ("tara", "leah", "jess", "leo", "dan", "mia", "zac", "zoe")
# Named Orpheus presets. "friday" is the calm, clear female voice closest to the FRIDAY character.
VOICE_PRESETS = {"friday": "tara"}


# What the voice says while a spoken reply is still coming ([voice] filler_phrases).
FILLER_PHRASES = ("Hmm.",)
# Filler lists earlier releases wrote into config.toml as the default. A config still holding one of these
# unchanged gets today's default instead; a list the user edited is left alone.
OLD_FILLER_DEFAULTS = (
    ("One sec.", "Let me see.", "Hmm, let me think.", "Right, let me have a look.", "Hang on a moment.", "Let me check."),
)


def voice_dir(name: str) -> Path:
    return VOICES_DIR / name


def saved_voices() -> list[str]:
    """Names of the cloned / designed voices on disk (qwen3 engine)."""
    if not VOICES_DIR.is_dir():
        return []
    return sorted(d.name for d in VOICES_DIR.iterdir() if (d / "ref.wav").is_file())


def voice_choices(vc: "VoiceConfig") -> list[tuple[str, str]]:
    """(name, note) pairs for the /voice picker and `vision voices`, for the configured engine."""
    if vc.engine == "orpheus":
        return [(n, f"preset → {v}") for n, v in VOICE_PRESETS.items()] + [(v, "") for v in VOICES]
    have = saved_voices()
    out = [(name, "designed" if (voice_dir(name) / "design.txt").is_file() else "cloned") for name in have]
    out += [(name, f"built-in design, not made yet → vision voice design {name}") for name in VOICE_DESIGNS if name not in have]
    return out


@dataclass
class CodexConfig:
    sandbox: str = "auto"
    extra_config: list[str] = field(default_factory=list)
    transport: str = "app-server"  # or "exec" (see vision/codex_app.py)


@dataclass
class GrokConfig:
    sandbox: str = "auto"
    extra_args: list[str] = field(default_factory=list)
    transport: str = "acp"  # or "headless" (see vision/grok_acp.py)


@dataclass
class LocalConfig:
    """The OpenAI-compatible server that hosts the Local models."""

    base_url: str = "http://localhost:8080/v1"
    timeout_s: float = 180
    context: int = 131072  # the model's context size; history is trimmed to stay under it
    brave_api_key: str = ""  # WebSearch uses Brave's API with a key, DuckDuckGo's HTML results without




@dataclass
class ProvidersConfig:
    enabled: list[str] = field(default_factory=lambda: list(PROVIDER_NAMES))
    notes: list[str] = field(default_factory=list)  # what was wrong with a [providers.<name>] table, if anything


@dataclass
class BrainConfig:
    model: str = "opus"
    effort: str = "medium"
    # Session-only provider speed tier. /fast toggles it; it is deliberately not saved with defaults.
    fast: bool = False
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
    )
    denied_tools: list[str] = field(
        default_factory=lambda: ["Bash(sudo:*)", "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)", "Bash(mkfs:*)", "Bash(dd:*)", "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(claude:*)", "Bash(codex:*)", "Bash(grok:*)"]
    )
    # Patterns (denied_tools syntax, or a bare tool / path glob) that ask before running in auto mode;
    # see ask_tools in DEFAULT_CONFIG. Empty = nothing asks.
    ask_tools: list[str] = field(default_factory=list)
    # Exceptions to ask_tools that run without asking (denied_tools still wins); not in a scout's chats.
    allow_tools: list[str] = field(default_factory=list)
    # Scouts' rule per action (projects.ACTIONS: does | asks | never) over the leash's default; a scout's own wins.
    scout_rules: dict = field(default_factory=dict)
    workdir: str = ""
    # The per-folder instructions file looked for from the working directory upwards; "" = none.
    folder_instructions: str = ".vision.md"
    address_user_as: str = ""
    # "auto" (every tool pre-approved, Codex on danger-full-access; denied_tools still applies) or
    # "plan" (read-only until the plan is approved). Shift-Tab or /mode switches for the session.
    mode: str = "auto"
    # What approving a plan unlocks: "session" (Vision switches to auto for the rest of the session)
    # or "turn" (only the approved plan runs; the next message starts in plan mode again).
    plan_approval: str = "session"
    # Read Claude Code's and Grok's saved logins to query their usage endpoints directly (see
    # read_cli_logins in DEFAULT_CONFIG); False = Vision never opens those credential files.
    read_cli_logins: bool = True
    # Seconds of silence from the Claude process before the turn is killed as stalled; 0 = never.
    stall_s: float = 900
    # Seconds the Claude Code (or ACP agent) process is kept between turns; 0 = a new one per turn.
    keep_alive_s: float = 600
    # Record the working tree before each reply in a git repository (/diff, /rewind; vision/checkpoints.py).
    checkpoints: bool = True
    # A turn failing on the provider's usage limit: "ask" (offer the other providers), "off", or a
    # provider order like ["codex", "grok"] to switch to the first ready one unasked (failover.py).
    failover: object = "ask"
    # Percent of the tightest window left at which the corner's quota figure turns yellow and a note warns.
    quota_warn_pct: float = 15
    codex: CodexConfig = field(default_factory=CodexConfig, repr=False)
    grok: GrokConfig = field(default_factory=GrokConfig, repr=False)
    local: LocalConfig = field(default_factory=LocalConfig, repr=False)
    weather: "WeatherConfig | None" = None


MODES = ("auto", "plan")

_read_cli_logins = True  # [brain].read_cli_logins from the last load_config()


def cli_logins_allowed() -> bool:
    """Whether Vision may read the Claude Code and Grok login files (usage.py, grok.py)."""
    return _read_cli_logins


@dataclass
class MemoryConfig:
    review: str = "ask"
    review_after_turns: int = 4
    review_timeout_s: float = 45


@dataclass
class ConversationConfig:
    model: str = "sonnet"
    effort: str = "low"
    web: bool = True
    max_delegations: int = 3
    timeout_s: float = 120
    timing: bool = False


@dataclass
class RouterConfig:
    """The front end and its supervisor (vision/routing.py, vision/supervisor.py)."""

    mode: str = "off"  # "off" | "audit" | "on"
    typed: bool = True  # in "on" mode typed input goes through the front end too
    default_agent: str = "opus"
    default_effort: str = "medium"
    high_effort: str = "high"
    agents: dict = field(default_factory=lambda: {"opus": "opus", "codex": ""})  # name → catalogue model ("" = provider default)
    timeout_s: float = 1800
    max_rounds: int = 4
    max_output_tokens: int = 200_000
    approval: list[str] = field(default_factory=lambda: [
        "Bash(git push:*)", "Bash(pip install:*)", "Bash(uv pip install:*)", "Bash(npm install -g:*)", "Bash(apt:*)",
        "Bash(apt-get:*)", "Bash(dnf:*)", "Bash(brew:*)", "Bash(rm -rf:*)", "Bash(gh pr create:*)", "Bash(gh release:*)",
        "Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)", "Bash(docker push:*)", "Bash(kubectl apply:*)", "Bash(terraform apply:*)",
        "Bash(mail:*)", "Bash(sendmail:*)", "Bash(curl -X POST:*)", "Bash(curl -d:*)",
    ])


ROUTER_MODES = ("off", "audit", "on")


@dataclass
class VoiceConfig:
    enabled: bool = True  # off: no voice or speech recognition loaded anywhere (/voice off, the phone's Settings)
    engine: str = "qwen3"
    voice: str = "jarvis"
    language: str = "English"
    clone_mode: str = "embedding"
    continuity: bool = True
    rate: float = 1.0
    device: str = "auto"
    quant: str = "none"
    mlx_model: str = ""
    output_device: str = ""
    port: int = 8766
    # Something to say while the reply is still coming: see [voice] filler in DEFAULT_CONFIG.
    filler: bool = True
    filler_after_ms: int = 700
    filler_phrases: list[str] = field(default_factory=lambda: list(FILLER_PHRASES))


@dataclass
class ListenConfig:
    input_device: str = ""
    whisper_model: str = "large-v3-turbo"
    device: str = "auto"
    vad_aggressiveness: int = 2
    end_silence_ms: int = 900
    start_timeout_s: float = 0
    max_utterance_s: float = 45
    chime: bool = True
    live_ms: int = 600  # live transcription preview interval; 0 = off
    barge_in: str = "wake"  # "wake" | "speech" | "off"
    only_me: bool = False  # act only on the enrolled voice (`vision voice enrol`)
    only_me_threshold: float = 0.45  # cosine similarity to the enrolment an utterance needs


@dataclass
class WakeConfig:
    enabled: bool = False
    names: list[str] = field(default_factory=lambda: ["vision"])
    model: str = "tiny.en"
    follow_up_s: float = 8


@dataclass
class BuddyConfig:
    name: str = "Vision"
    enabled: bool = True
    slide_in: bool = True


@dataclass
class ChatConfig:
    """The terminal chat itself (the voice, the buddy and the phone have sections of their own)."""

    notify_after_s: float = 30  # a desktop notification when a reply this long finishes unwatched; 0 = never


@dataclass
class RemoteConfig:
    host: str = "0.0.0.0"
    port: int = 8765
    public_url: str = ""
    suggestions: bool = True
    # Where a chat opened from the phone or desktop starts, and in which mode; "" = [brain] workdir / mode.
    new_chat_folder: str = ""
    new_chat_mode: str = ""


@dataclass
class PushConfig:
    enabled: bool = True
    relay: str = ""
    team_id: str = ""
    key_id: str = ""
    key_file: str = "apns.p8"
    topic: str = ""


@dataclass
class WeatherConfig:
    enabled: bool = True
    team_id: str = ""
    service_id: str = ""
    key_id: str = ""
    key_file: str = "weatherkit.p8"
    location: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    timezone: str = ""
    country_code: str = ""
    units: str = "metric"
    language: str = "en"

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.team_id.strip() and self.service_id.strip() and self.key_id.strip())


@dataclass
class Config:
    brain: BrainConfig = field(default_factory=BrainConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    grok: GrokConfig = field(default_factory=GrokConfig)
    local: LocalConfig = field(default_factory=LocalConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    buddy: BuddyConfig = field(default_factory=BuddyConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    conversation: ConversationConfig = field(default_factory=ConversationConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    push: PushConfig = field(default_factory=PushConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    def __post_init__(self) -> None:
        self.brain.codex = self.codex
        self.brain.grok = self.grok
        self.brain.local = self.local
        self.brain.weather = self.weather


def weather_ready(brain: BrainConfig) -> bool:
    """True when this brain's config has the weather on. No key is needed: without a WeatherKit one
    the Weather Service and Open-Meteo answer (vision/weather.py)."""
    w = getattr(brain, "weather", None)
    return bool(w is not None and w.enabled)


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, STATE_DIR, MODELS_DIR, WORKSPACE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
    try:
        raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"Config error in {CONFIG_PATH}: {e}")
    cfg = Config()
    providers_raw = raw.get("providers", {}) if isinstance(raw.get("providers"), dict) else {}
    cfg.providers.notes = load_plugins() + register_endpoints({k: v for k, v in providers_raw.items() if isinstance(v, dict)})
    for section, target in (("brain", cfg.brain), ("providers", cfg.providers), ("codex", cfg.codex), ("grok", cfg.grok), ("local", cfg.local), ("conversation", cfg.conversation), ("voice", cfg.voice), ("listen", cfg.listen), ("wake", cfg.wake), ("buddy", cfg.buddy), ("chat", cfg.chat), ("remote", cfg.remote), ("weather", cfg.weather), ("push", cfg.push), ("memory", cfg.memory)):
        for k, v in raw.get(section, {}).items():
            if hasattr(target, k):
                if section == "brain" and k == "model" and not v:
                    continue  # migrate the old "Claude default" setting to Vision's Opus default
                if section == "brain" and k == "effort" and not v and not raw.get("brain", {}).get("model"):
                    continue  # same migration; an explicit model + empty effort is meaningful (e.g. Haiku)
                setattr(target, k, v)
    if not str(cfg.push.team_id).strip():
        cfg.push.team_id = cfg.weather.team_id  # one developer account signs both
    cfg.brain.mode = str(cfg.brain.mode).strip().lower()
    if cfg.brain.mode not in MODES:
        cfg.brain.mode = "auto"
    cfg.remote.new_chat_mode = str(cfg.remote.new_chat_mode or "").strip().lower()
    if cfg.remote.new_chat_mode not in MODES:
        cfg.remote.new_chat_mode = ""
    if cfg.brain.plan_approval not in ("session", "turn"):
        cfg.brain.plan_approval = "session"
    from vision.failover import order as _failover_order

    cfg.brain.failover = _failover_order(cfg.brain.failover)
    try:
        cfg.brain.quota_warn_pct = max(0.0, min(100.0, float(cfg.brain.quota_warn_pct)))
    except (TypeError, ValueError):
        cfg.brain.quota_warn_pct = 15
    global _read_cli_logins
    _read_cli_logins = cfg.brain.read_cli_logins is not False
    if "enabled" in providers_raw and isinstance(cfg.providers.enabled, list):
        enabled = [str(x).strip().lower() for x in cfg.providers.enabled]
    else:
        enabled = list(REGISTRY)  # not chosen: every provider, the config-defined ones included
    cfg.providers.enabled = list(dict.fromkeys(x for x in enabled if x in REGISTRY))
    vc = cfg.voice
    vc.filler_after_ms = max(0, int(vc.filler_after_ms or 0))
    vc.filler_phrases = [str(x).strip() for x in (vc.filler_phrases if isinstance(vc.filler_phrases, list) else []) if str(x).strip()]
    if tuple(vc.filler_phrases) in OLD_FILLER_DEFAULTS:
        vc.filler_phrases = list(FILLER_PHRASES)  # an untouched old default: move it to the "Hmm." filler
    router = raw.get("router", {})
    for k, v in router.items():
        if k in ("agents", "limits", "permissions"):
            continue
        if hasattr(cfg.router, k):
            setattr(cfg.router, k, v)
    if isinstance(router.get("agents"), dict):
        cfg.router.agents = {str(k).strip().lower(): str(v or "").strip() for k, v in router["agents"].items() if str(k).strip()}
    for k, v in (router.get("limits") or {}).items():
        if k in ("timeout_s", "max_rounds", "max_output_tokens"):
            setattr(cfg.router, k, v)
    if isinstance((router.get("permissions") or {}).get("approval"), list):
        cfg.router.approval = [str(p) for p in router["permissions"]["approval"]]
    cfg.router.mode = str(cfg.router.mode or "off").strip().lower()
    if cfg.router.mode in ("false", "no", "0"):
        cfg.router.mode = "off"
    if cfg.router.mode not in ROUTER_MODES:
        cfg.router.mode = "off"
    cfg.router.default_agent = str(cfg.router.default_agent or "opus").strip().lower()
    cfg.router.default_effort = str(cfg.router.default_effort or "medium").strip().lower()
    cfg.router.high_effort = str(cfg.router.high_effort or "high").strip().lower()
    cfg.router.timeout_s = max(30.0, float(cfg.router.timeout_s or 1800))
    cfg.router.max_rounds = max(1, int(cfg.router.max_rounds or 4))
    cfg.router.max_output_tokens = max(1000, int(cfg.router.max_output_tokens or 200_000))
    cfg.conversation.max_delegations = min(8, max(1, int(cfg.conversation.max_delegations)))
    from vision.providers import cap

    if cap(provider_for(cfg.conversation.model), "conversation_thinks", True) is False:
        cfg.conversation.effort = THINKING_OFF  # e.g. LocalConversation always sends enable_thinking=False (grammar mode)
    cfg.conversation.timeout_s = max(10, float(cfg.conversation.timeout_s))
    cfg.brain.stall_s = max(0.0, float(cfg.brain.stall_s or 0))
    cfg.chat.notify_after_s = max(0.0, float(cfg.chat.notify_after_s or 0))
    cfg.brain.keep_alive_s = max(0.0, float(cfg.brain.keep_alive_s or 0))
    if cfg.weather.units not in ("metric", "imperial"):
        cfg.weather.units = "metric"
    if cfg.voice.engine not in VOICE_ENGINES:
        cfg.voice.engine = "qwen3"
    cfg.voice.rate = min(2.0, max(0.5, float(cfg.voice.rate or 1.0)))
    if cfg.voice.clone_mode not in ("embedding", "context"):
        cfg.voice.clone_mode = "embedding"
    if isinstance(cfg.wake.names, str):
        cfg.wake.names = [cfg.wake.names]
    cfg.wake.names = [n.strip().lower() for n in cfg.wake.names if n.strip()] or ["vision"]
    cfg.listen.barge_in = str(cfg.listen.barge_in or "off").strip().lower()
    if cfg.listen.barge_in in ("false", "no", "none", "0"):
        cfg.listen.barge_in = "off"
    if cfg.listen.barge_in not in ("wake", "speech", "off"):
        cfg.listen.barge_in = "wake"
    if re.fullmatch(r"[a-z]{2}_\w+(:[\d.]+)?(,.*)?", cfg.voice.voice):
        cfg.voice.voice = "friday"  # a Kokoro-era voice or blend
    if cfg.voice.engine == "qwen3" and cfg.voice.voice in VOICE_PRESETS and cfg.voice.voice not in saved_voices():
        cfg.voice.voice = "jarvis"  # an Orpheus-era preset left in the config; jarvis is the qwen3 default
    # Existing configs that still deny only claude/codex pick up the grok shim-matching rule.
    cfg.brain.ask_tools = [str(r).strip() for r in (cfg.brain.ask_tools or []) if str(r).strip()]
    cfg.brain.allow_tools = [str(r).strip() for r in (cfg.brain.allow_tools or []) if str(r).strip()]
    if not isinstance(cfg.brain.scout_rules, dict):
        cfg.brain.scout_rules = {}
    if "Bash(grok:*)" not in cfg.brain.denied_tools and "Bash(codex:*)" in cfg.brain.denied_tools:
        cfg.brain.denied_tools = list(cfg.brain.denied_tools) + ["Bash(grok:*)"]
    # The drivers receive BrainConfig for historical compatibility; attach the provider-specific
    # settings without changing every public constructor.
    cfg.brain.codex = cfg.codex
    cfg.brain.grok = cfg.grok
    cfg.brain.local = cfg.local
    cfg.brain.weather = cfg.weather
    return cfg


def saved_brain_defaults() -> tuple[str, str] | None:
    """The default model and effort as config.toml has them now, or None when the file cannot be read.

    For long-running processes (`vision serve`) that must pick up /default without a restart and must
    not die on a half-edited config: load_config exits on a TOML error, so that is caught here too."""
    try:
        brain = load_config().brain
    except (Exception, SystemExit):
        return None
    return brain.model, brain.effort


def save_brain_defaults(model: str, effort: str) -> None:
    """Persist the default model and effort in config.toml, editing the lines in place so comments survive."""
    ensure_dirs()
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else DEFAULT_CONFIG
    lines = text.splitlines()
    wanted = {"model": model, "effort": effort}
    section, done = None, set()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        if section == "brain":
            m = re.match(r"^\s*(model|effort)\s*=", line)
            if m and m.group(1) not in done:
                lines[i] = f'{m.group(1)} = "{wanted[m.group(1)]}"'
                done.add(m.group(1))
    missing = [k for k in ("model", "effort") if k not in done]
    if missing:
        try:
            at = next(i for i, l in enumerate(lines) if l.strip() == "[brain]") + 1
        except StopIteration:
            lines += ["", "[brain]"]
            at = len(lines)
        for k in reversed(missing):
            lines.insert(at, f'{k} = "{wanted[k]}"')
    out = "\n".join(lines) + "\n"
    tomllib.loads(out)  # refuse to write a broken config
    CONFIG_PATH.write_text(out, encoding="utf-8")


def save_voice_default(name: str) -> None:
    """Persist `[voice] voice = name` in config.toml, editing the line in place so comments survive."""
    ensure_dirs()
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else DEFAULT_CONFIG
    lines = text.splitlines()
    section, done = None, False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        if section == "voice" and not done and re.match(r"^\s*voice\s*=", line):
            lines[i] = f'voice = "{name}"'
            done = True
    if not done:
        try:
            at = next(i for i, l in enumerate(lines) if l.strip() == "[voice]") + 1
        except StopIteration:
            lines += ["", "[voice]"]
            at = len(lines)
        lines.insert(at, f'voice = "{name}"')
    out = "\n".join(lines) + "\n"
    tomllib.loads(out)  # refuse to write a broken config
    CONFIG_PATH.write_text(out, encoding="utf-8")


def save_config_value(section: str, key: str, literal: str) -> None:
    """Persist one `[section] key = literal` in config.toml, editing the line in place so comments survive."""
    ensure_dirs()
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else DEFAULT_CONFIG
    lines = text.splitlines()
    current, done = None, False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1].strip()
            continue
        if current == section and not done and re.match(rf"^\s*{re.escape(key)}\s*=", line):
            lines[i] = f"{key} = {literal}"
            done = True
    if not done:
        try:
            at = next(i for i, l in enumerate(lines) if l.strip() == f"[{section}]") + 1
        except StopIteration:
            lines += ["", f"[{section}]"]
            at = len(lines)
        lines.insert(at, f"{key} = {literal}")
    out = "\n".join(lines) + "\n"
    tomllib.loads(out)  # refuse to write a broken config
    CONFIG_PATH.write_text(out, encoding="utf-8")


_ROUTER_LIMITS = ("timeout_s", "max_rounds", "max_output_tokens")  # [router.limits], see load_config


def config_keys() -> dict[str, object]:
    """Every `section.key` that `vision config set` and `get` know, with its default (which says the
    type a value must have). `[providers.<name>]` tables and `[router.agents]` take any key and are
    not listed."""
    import dataclasses

    out: dict[str, object] = {}
    for f in dataclasses.fields(Config):
        inst = f.default_factory()  # type: ignore[misc]
        for g in dataclasses.fields(type(inst)):
            value = getattr(inst, g.name)
            if g.name == "notes" or dataclasses.is_dataclass(value) or value is None:
                continue  # bookkeeping, or a sub-config attached in __post_init__
            out[f"{f.name}.{g.name}"] = value
    for k in _ROUTER_LIMITS:  # the router's sub-tables, as load_config reads them
        out[f"router.limits.{k}"] = out.pop(f"router.{k}")
    out["router.permissions.approval"] = out.pop("router.approval")
    out.pop("router.agents")
    return out


def _free_table(section: str) -> bool:
    return section == "router.agents" or (section.startswith("providers.") and section.count(".") == 1)


def parse_config_literal(value: str) -> tuple[str, object]:
    """What a value typed on the command line means: a TOML literal (`true`, `12`, `"x"`, `["a", "b"]`)
    is taken as written; anything else is a string. Returns (the literal to write, its value)."""
    try:
        parsed = tomllib.loads(f"v = {value}")["v"]
        if not isinstance(parsed, dict):  # an inline table is not one value
            return value.strip(), parsed
    except tomllib.TOMLDecodeError:
        pass
    return _toml_str(value), value


def set_config_value(path: str, value: str) -> str:
    """`vision config set section.key value`: checks the key is one Vision reads and the value has
    its type, writes it into config.toml in place (comments and order kept), and returns the literal
    written. ValueError says what was wrong."""
    import difflib

    section, _, key = path.strip().rpartition(".")
    known = config_keys()
    if not section or not key:
        raise ValueError("give the key as section.key, e.g. brain.mode")
    if not _free_table(section) and path not in known:
        near = difflib.get_close_matches(path, known, n=3, cutoff=0.5)
        raise ValueError(f"no setting called {path}" + (f"; did you mean {', '.join(near)}?" if near else "")
                         + " (`vision config` lists them all)")
    literal, parsed = parse_config_literal(value)
    if not _free_table(section):
        default = known[path]
        if isinstance(default, str) and not isinstance(parsed, str):
            literal, parsed = _toml_str(value), value  # `brain.model 5` means the string "5"
        elif isinstance(default, bool) and not isinstance(parsed, bool):
            raise ValueError(f"{path} is true or false")
        elif isinstance(default, int) and not isinstance(default, bool) and (isinstance(parsed, bool) or not isinstance(parsed, int)):
            raise ValueError(f"{path} takes a whole number")
        elif isinstance(default, float) and (isinstance(parsed, bool) or not isinstance(parsed, (int, float))):
            raise ValueError(f"{path} takes a number")
        elif isinstance(default, list) and not isinstance(parsed, list):
            raise ValueError(f'{path} takes a list, e.g. ["a", "b"]')
    save_config_value(section, key, literal)
    return literal


def get_config_value(path: str) -> tuple[object, bool]:
    """The value of `section.key` as config.toml has it, and whether it is set there (False: the
    default is what you get). KeyError for a key Vision does not read."""
    section, _, key = path.strip().rpartition(".")
    known = config_keys()
    if not _free_table(section) and path not in known:
        raise KeyError(path)
    raw: object = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    for part in (*section.split("."), key):
        if not isinstance(raw, dict) or part not in raw:
            if path in known:
                return known[path], False
            raise KeyError(path)
        raw = raw[part]
    return raw, True


_voice_seen: tuple[float, bool] = (-1.0, True)


def voice_enabled() -> bool:
    """[voice] enabled as config.toml says it now: the phone or another terminal may have switched it since
    this process read its config. Read again only when the file changed."""
    global _voice_seen
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return True
    if mtime != _voice_seen[0]:
        try:
            on = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("voice", {}).get("enabled", True) is not False
        except (OSError, tomllib.TOMLDecodeError):
            on = _voice_seen[1]
        _voice_seen = (mtime, on)
    return _voice_seen[1]


def save_voice_enabled(enabled: bool) -> None:
    """Persist `[voice] enabled` in config.toml."""
    save_config_value("voice", "enabled", "true" if enabled else "false")


def save_wake_enabled(enabled: bool) -> None:
    """Persist `[wake] enabled` in config.toml."""
    save_config_value("wake", "enabled", "true" if enabled else "false")


def save_enabled_providers(names: list[str]) -> None:
    """Persist `[providers] enabled` in config.toml."""
    save_config_value("providers", "enabled", "[" + ", ".join(f'"{n}"' for n in names) + "]")


def _toml_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_provider_table(name: str, values: dict) -> None:
    """Write (or rewrite) a `[providers.<name>]` table in config.toml, comments elsewhere untouched."""
    ensure_dirs()
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else DEFAULT_CONFIG
    lines = remove_provider_table(name, text).rstrip("\n").splitlines()
    block = [f"[providers.{name}]"]
    for k, v in values.items():
        if v in ("", None, []):
            continue
        lit = _toml_str(v) if isinstance(v, str) else "[" + ", ".join(_toml_str(x) for x in v) + "]" if isinstance(v, list) else str(v)
        block.append(f"{k} = {lit}")
    out = "\n".join(lines + ["", *block]) + "\n"
    tomllib.loads(out)
    CONFIG_PATH.write_text(out, encoding="utf-8")
    _enable_provider(name, True)


def _enable_provider(name: str, on: bool) -> None:
    """Keep an explicit `[providers] enabled` list in step with a table added or removed (one that is
    left out already offers everything)."""
    try:
        raw = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return
    listed = (raw.get("providers") or {}).get("enabled")
    if not isinstance(listed, list):
        return
    names = [str(x) for x in listed]
    if on and name not in names:
        names.append(name)
    elif not on and name in names:
        names.remove(name)
    else:
        return
    save_enabled_providers(names)


def remove_provider_table(name: str, text: str | None = None) -> str:
    """config.toml without the `[providers.<name>]` table (returned; written back when `text` is None)."""
    write = text is None
    text = CONFIG_PATH.read_text(encoding="utf-8") if text is None and CONFIG_PATH.exists() else (text or "")
    out, skipping = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            skipping = s[1:-1].strip() == f"providers.{name}"
        if not skipping:
            out.append(line)
    result = "\n".join(out).rstrip("\n") + "\n"
    if write:
        tomllib.loads(result)
        CONFIG_PATH.write_text(result, encoding="utf-8")
        _enable_provider(name, False)
    return result


def save_input_device(spec: str) -> None:
    """Persist `[listen] input_device` in config.toml ("" = the system default)."""
    save_config_value("listen", "input_device", '"' + spec.replace('\\', '\\\\').replace('"', '\\"') + '"')


def save_output_device(spec: str) -> None:
    """Persist `[voice] output_device` in config.toml ("" = the system default)."""
    save_config_value("voice", "output_device", '"' + spec.replace('\\', '\\\\').replace('"', '\\"') + '"')


_OPEN_LOCK = threading.Lock()


@dataclass(frozen=True)
class AudioDevice:
    """A sounddevice index (None = system default) plus, for a name that only matched a PipeWire
    node, the node to route to.

    PortAudio lists every PipeWire node under its JACK host API, but those entries hang on open
    and only run at the graph rate, so a node is reached through the ALSA `pipewire` PCM with
    PIPEWIRE_NODE set while the stream is constructed: pipewire-alsa reads it at snd_pcm_open.
    """

    index: int | None = None
    node: str | None = None

    @contextlib.contextmanager
    def opening(self):
        """Wrap the sd.InputStream/OutputStream/play call that opens this device."""
        if self.node is None:
            yield
            return
        with _OPEN_LOCK:  # the variable is process-wide: one open at a time
            saved = os.environ.get("PIPEWIRE_NODE")
            os.environ["PIPEWIRE_NODE"] = self.node
            try:
                yield
            finally:
                if saved is None:
                    os.environ.pop("PIPEWIRE_NODE", None)
                else:
                    os.environ["PIPEWIRE_NODE"] = saved


def pipewire_nodes(kind: str) -> list[tuple[str, str]]:
    """(node.name, description) of each PipeWire source (input) or sink (output); [] without PipeWire."""
    import json
    import subprocess

    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True, encoding="utf-8").stdout
        objects = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    want = "Audio/Source" if kind == "input" else "Audio/Sink"
    nodes = []
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == want and props.get("node.name"):
            nodes.append((props["node.name"], props.get("node.description") or props.get("node.nick") or props["node.name"]))
    return nodes


_DEVICE_TTL_S = 60.0  # how long a device name's lookup stands: it runs pw-dump, the whole PipeWire graph
_devices: dict[tuple[str, str], tuple[float, AudioDevice]] = {}
_devices_lock = threading.Lock()


def resolve_device(spec: str | int | None, kind: str, *, fresh: bool = False) -> AudioDevice:
    """Turn a device index or name fragment into an AudioDevice (the system default when empty).
    A name's lookup is kept for _DEVICE_TTL_S: finding it dumps the whole PipeWire graph, and
    `vision talk` asked for the speakers before every turn. `fresh` looks again now (a replug, a
    stalled mic, a /mic change)."""
    if spec is None or spec == "":
        return AudioDevice()
    import sounddevice as sd
    import time

    if isinstance(spec, int) or str(spec).isdigit():
        return AudioDevice(int(spec))
    cache_key = (str(spec), kind)
    if not fresh:
        with _devices_lock:
            hit = _devices.get(cache_key)
        if hit is not None and time.monotonic() - hit[0] < _DEVICE_TTL_S:
            return hit[1]
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    needle = str(spec).lower()
    devices = sd.query_devices()
    found = None
    # Through PipeWire first: its `pipewire` PCM shares the device, whereas PortAudio's raw `hw:` entry
    # takes it exclusively, so PipeWire itself can no longer open it and everything else (the desktop,
    # a second Vision going through PipeWire) waits for audio that never comes.
    for name, description in pipewire_nodes(kind):
        if needle in description.lower() or needle in name.lower():
            alsa = next((i for i, d in enumerate(devices) if d["name"] == "pipewire" and d[key] > 0), None)
            found = AudioDevice(alsa, name)
            break
    if found is None:
        for i, d in enumerate(devices):
            if d[key] > 0 and needle in d["name"].lower() and "JACK" not in sd.query_hostapis(d["hostapi"])["name"]:
                found = AudioDevice(i)
                break
    if found is None:
        raise SystemExit(f"No {kind} device matching {spec!r}. Run `vision doctor` to list devices.")
    with _devices_lock:
        _devices[cache_key] = (time.monotonic(), found)
    return found


def input_device_choices() -> list[tuple[str, str, str]]:
    """(value, label, desc) rows for a microphone picker: the system default first, then every PipeWire
    source by node name (what resolve_device matches exactly), or the plain PortAudio inputs without PipeWire."""
    return _device_choices("input")


def output_device_choices() -> list[tuple[str, str, str]]:
    """The same rows for the speakers: the system default, then every PipeWire sink or PortAudio output."""
    return _device_choices("output")


def _device_choices(kind: str) -> list[tuple[str, str, str]]:
    rows = [("", "system default", f"follows your PipeWire default {'source' if kind == 'input' else 'sink'}")]
    nodes = pipewire_nodes(kind)
    if nodes:
        return rows + [(name, desc, name) for name, desc in nodes]
    import sounddevice as sd

    key, word = ("max_input_channels", "in") if kind == "input" else ("max_output_channels", "out")
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and "JACK" not in sd.query_hostapis(d["hostapi"])["name"]:
            rows.append((str(i), d["name"], f"#{i} · {d[key]} {word}"))
    return rows
