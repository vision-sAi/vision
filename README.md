# Vision

A voice and text AI assistant for the terminal, driven by the **Claude Code, Codex or Grok subscription
you already pay for**. No API keys, no scraped tokens: Vision runs the official CLIs headless with your
normal login and puts a proper chat, a voice and a pair of ears around them.

- **Chat** in a full-screen terminal UI with streaming replies, live subagent rows, a plan mode and
  real selectors when the model needs you to choose.
- **Switch brains** mid-conversation with `/model`: Claude, Codex, Grok, or a model on your own
  server (llama.cpp, Ollama, LM Studio). The conversation comes with you.
- **Talk to it** hands-free (`vision talk`, or `/talk` in a chat) with a wake word and barge-in. Speech
  recognition (faster-whisper) and the voice (Qwen3-TTS) run locally on your GPU. Enrol your voice
  (`vision voice enrol`) and it listens to you alone.
- **Give it any voice**: design one from a text description, or clone a few seconds of audio you have
  the right to use.
- **Keep it on a leash**: `denied_tools` patterns, sandboxes for Codex and Grok, and a read-only plan
  mode. Vision also stops one provider from quietly spending another's subscription.

> [!WARNING]
> Vision starts in **auto mode**: the model runs shell commands and edits files without asking, apart
> from the `denied_tools` patterns. Press Shift-Tab (or set `mode = "plan"` under `[brain]`) if you
> would rather approve a plan first. Only run it where you would run the underlying CLI unattended.

## Requirements

| For | You need |
| --- | --- |
| Text chat | Python 3.11+ on Linux or macOS, and at least one brain: [Claude Code](https://code.claude.com), the [Codex CLI](https://github.com/openai/codex), the Grok CLI, any OpenAI-compatible server (Ollama, LM Studio, `llama-server`, a hosted API), or an agent that speaks ACP |
| Voice | A microphone (and PipeWire on Linux). Runs in real time on an NVIDIA GPU with 8 GB+ of VRAM or an Apple Silicon Mac (through MLX); anywhere else it runs on the CPU, several times slower than real time. Up to about 10 GB of downloads for the speech models |
| The voice conversation model | A Claude Code or Codex login, or a local model (see [the guide](docs/guide.md#the-voice)) |
| Weather | Optional: an Apple Developer account for WeatherKit. Without it Vision searches the web |

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/vision-sAi/vision/master/install.sh | sh
vision
```

That is the whole install: it puts Vision in `~/.local/share/vision/app` with its own Python (through
[uv](https://docs.astral.sh/uv/), fetched if you don't have it), links `vision` into `~/.local/bin`, and
needs no sudo. Run it again to update. The first `vision` is a short guided setup. It asks which
providers you use and sets each one up (nothing is asked when one is already signed in and working),
then what to call you and whether to start in auto or plan mode. It offers the voice if the machine
can run it: an Apple Silicon Mac or an NVIDIA graphics card (8 GB+); anywhere else Vision is text
only. Last, it offers to pair the iPhone app (see [Apps](#apps)): it keeps `vision serve` running in the background and
shows the code to scan. Leave halfway and the next `vision` resumes at that step; `vision onboard`
goes through it again. `vision voice setup` adds the voice later (and checks you can hear it and it
can hear you; `vision voice check` does that again), and `vision providers` changes your providers.

### From a clone

```bash
git clone https://github.com/vision-sAi/vision.git && cd vision
scripts/setup.sh --all --link     # or --voice, --serve; nothing for text chat only
```

For voice, the script picks a build for the machine: `--nvidia` (CUDA, about 6 GB, Linux only; on Linux it
looks for an NVIDIA GPU and asks), `--mac` (MLX on the Mac's GPU, about 1 GB; Apple Silicon gets this one)
or `--cpu` (about 2 GB; everything else, Intel Macs included). Pass one to skip the question. It then
fetches the speech models (`vision setup`) and runs `vision doctor`, which checks the CLIs, logins, GPU
and audio devices. On a Mac, `vision setup` also times the voice models and keeps the best one that
runs comfortably faster than real time on that chip (`vision setup --retime` to time them again).

The same by hand:

```bash
uv venv --python 3.12 .venv
uv sync --frozen --inexact --extra voice-nvidia --extra serve --extra weather   # or voice-mac, voice-cpu
ln -s "$PWD/bin/vision" ~/.local/bin/vision               # any directory on your PATH
vision setup      # fetches the speech models (skip for text only)
vision doctor
```

The extras are `voice-nvidia`, `voice-mac` or `voice-cpu` (speech in and out; pick one), `serve` (the remote API),
`weather` (Apple WeatherKit), and `all` / `all-cpu` for everything. `voice` is the old name for
`voice-nvidia`. Install the voice with uv, not plain pip: only uv takes torch from PyTorch's CPU or
CUDA index. If a command needs an extra you don't have, Vision tells you which to install.

Log in to whichever providers you want with their own tools first: `claude`, `codex login`, `grok login`.
Or skip that: the first time `vision` runs with nothing ready it asks what you use. Tick your
subscriptions (each CLI is installed and signed in for you, and asked whether the sign-in took), a model server it finds running on this
machine (Ollama, LM Studio, llama.cpp, vLLM), an API key (OpenRouter, OpenAI, Groq or any
OpenAI-compatible API) or an ACP agent, and it sets each one up in turn. `vision providers` brings the
same list back any time, with what you have ticked: tick to add, untick to switch one off.

## Use

```bash
vision                  # chat (Ctrl-D or /quit to leave)
vision talk             # hands-free voice conversation; say "goodbye" to end it
vision ask "what's the tallest building in Dublin?"
vision say "Good morning."
vision listen           # transcribe one utterance
vision default          # pick and save the default model and effort
vision --install-completion   # Tab completes the commands and options in bash, zsh or fish (new shell to take effect)
```

Inside a chat, type `/` for the command menu. The ones you'll use most: `/model`, `/effort`, `/talk`,
`/speak`, `/wake`, `/session`, `/new`, `/cd`, and Shift-Tab to flip between auto and plan mode.

Everything else, including the voice pipeline, the router, provider details and the remote API, is in
**[docs/guide.md](docs/guide.md)**.

## Apps

Both are clients of `vision serve` on your machine; the conversations run there.

- **iPhone.** Vision Remote, in `ios/`: chat, dictation, live voice calls and notifications. It isn't on the
  App Store; build it onto your phone under your own Apple team, as [ios/README.md](ios/README.md) explains.
- **Desktop.** For macOS and Linux, from the [Releases](https://github.com/vision-sAi/vision/releases) page
  (or build it from `desktop/`, see [desktop/README.md](desktop/README.md)). The Mac app isn't signed with
  an Apple Developer ID, so allow it once in System Settings › Privacy & Security; it updates itself after that.

## Make it yours

Config lives in `~/.config/vision/config.toml`, written with commented defaults on first run
(`vision config --edit` opens it; `vision config set brain.mode plan` changes one setting in place).

- **Personality.** Vision ships with a direct, lightly witty voice. To change it, write your own in
  `~/.config/vision/personality.md`: a paragraph on how it should talk plus a few `User:` / `Vision:`
  example exchanges. It replaces the built-in one outright; delete the file to go back.
- **What it calls you.** `address_user_as` under `[brain]`, e.g. your first name. Empty by default.
- **Its voice.** `vision voice design <name> "<description>"` invents one; `vision voice add <name>
  --from clip.wav` clones a recording; `/voice <name>` switches. Name three clones
  `<name>-conversational`, `<name>-expressive` and `<name>-reassuring`, set `voice =
  "<name>-conversational"`, and each reply is spoken in whichever style suits it.
- **Instructions for a folder.** Put a `.vision.md` in a project's folder (or one above it) and every
  brain reads it on each turn there, like a `CLAUDE.md` that works for Codex and Grok too.
  `/instructions` shows what is in effect; `folder_instructions` under `[brain]` renames the file.
- **Memory.** Tell it to remember something and it appends a line to
  `~/.local/share/vision/memory/MEMORY.md`, which every brain reads. Leaving a chat after a few
  replies reviews it: Vision lists the durable facts it left (about you, your machines, projects,
  decisions) and you tick the ones to keep. `/review` runs that any time; `review = "off"` under
  `[memory]` turns it off.
- **A model at home.** Any OpenAI-compatible server (llama.cpp, Ollama, LM Studio, vLLM) on any machine is
  a free, private brain: point `[local].base_url` at it. More servers (OpenRouter, a hosted API) are a
  `[providers.<name>]` table away, and `/providers` picks which providers `/model` offers.

## Privacy

The model's thinking happens wherever your chosen brain runs (Anthropic, OpenAI, xAI, or your own
model server). Speech recognition and the voice run on your machine, and nothing else leaves it
unless you turn on the weather, web search, or `vision serve`. `vision serve` needs a bearer token for
every request; put it behind Tailscale or a similar private network rather than the open internet.

## Contributing

Vision isn't taking outside contributions for now, so pull requests are limited to the maintainers.
You're welcome to fork it under the GPL-3.0. How the code is worked on is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[GPL-3.0-or-later](LICENSE). Qwen3-TTS, faster-whisper and the other models Vision downloads keep their
own licences.
