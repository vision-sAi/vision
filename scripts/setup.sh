#!/usr/bin/env bash
# Set up Vision on Linux or macOS: a Python 3.12 virtualenv in the checkout, the dependencies and, with
# --voice, the speech models. Needs uv; installs nothing system-wide. Safe to re-run: an existing .venv is
# reused and nothing already installed is removed (except the CUDA libraries when you switch to --cpu, and
# torch when you switch to --mac).
#
#   scripts/setup.sh [--voice] [--serve] [--all] [--nvidia | --mac | --cpu] [--link] [--yes] [--no-doctor]
#
#   --voice    speech in and out, then `vision setup` for the speech models (about 10 GB, into ~/.cache/huggingface)
#   --nvidia   the voice build for an NVIDIA GPU: CUDA torch and the NVIDIA runtime libraries (about 6 GB; Linux only)
#   --mac      the voice build for Apple Silicon: the voice runs on the Mac's GPU through MLX (about 1 GB; real time)
#   --cpu      the voice build for everything else: CPU-only torch (about 2 GB), several times slower than real time
#              Without one, the script looks for an NVIDIA GPU (nvidia-smi) and asks on Linux; Apple Silicon
#              Macs get --mac and Intel Macs --cpu.
#   --serve    `vision serve`, for the Vision Remote iPhone app
#   --all      voice, serve and weather
#   --link     symlink bin/vision into ~/.local/bin, and put that on your shell's PATH if it isn't
#              (one line in ~/.zshrc, ~/.bashrc, ~/.bash_profile on macOS, or fish's conf.d;
#              VISION_NO_MODIFY_PATH=1 leaves your profile alone)
#   --yes      don't ask: take the detected voice build and go ahead with the downloads
#   --no-doctor  skip the `vision doctor` check at the end (the installer and `vision voice setup` pass it)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
voice=0 serve=0 all=0 link=0 yes=0 doctor=1 backend=""

say() { echo "vision setup: $*"; }
usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Put ~/.local/bin on the PATH of the user's login shell, once, in the profile that shell reads: zsh reads
# ~/.zshrc everywhere, but macOS Terminal starts bash as a login shell, which skips ~/.bashrc. The line is
# guarded, so a profile that also sets it (Ubuntu's ~/.profile) doesn't put the folder on PATH twice.
# VISION_USER_PATH is the PATH from before install.sh put ~/.local/bin on its own.
add_to_path() {
    local bin="$HOME/.local/bin" sh="${SHELL##*/}" rc="" shown
    case ":${VISION_USER_PATH:-$PATH}:" in *":$bin:"*) return 0 ;; esac
    if [ -z "${VISION_NO_MODIFY_PATH:-}" ]; then
        case "$sh" in
            zsh) rc="${ZDOTDIR:-$HOME}/.zshrc" ;;
            bash) if [ "$(uname -s)" = Darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
            fish) rc="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/vision.fish" ;;
        esac
    fi
    if [ -z "$rc" ]; then
        say "~/.local/bin is not on your PATH; add it in your shell profile"
        return 0
    fi
    case "$rc" in "$HOME"/*) shown="~${rc#"$HOME"}" ;; *) shown="$rc" ;; esac
    if [ -f "$rc" ] && grep -q '\.local/bin' "$rc"; then
        say "~/.local/bin is on your PATH in $shown; open a new terminal to use vision"
        return 0
    fi
    mkdir -p "$(dirname "$rc")"
    {
        printf '\n# Added by Vision: its command lives in ~/.local/bin\n'
        if [ "$sh" = fish ]; then
            printf 'fish_add_path -g ~/.local/bin\n'
        else
            printf '%s\n' 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'
        fi
    } >>"$rc"
    say "added ~/.local/bin to your PATH in $shown; open a new terminal (or run: source $shown) to use vision"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --voice) voice=1 ;;
        --serve) serve=1 ;;
        --all) all=1 ;;
        --nvidia) backend=nvidia ;;
        --cpu) backend=cpu ;;
        --mac) backend=mac ;;
        --link) link=1 ;;
        --yes | -y) yes=1 ;;
        --no-doctor) doctor=0 ;;
        -h | --help) usage; exit 0 ;;
        *) say "unknown option $1"; usage; exit 2 ;;
    esac
    shift
done
[ "$all" = 1 ] && voice=1 serve=1
[ -n "$backend" ] && voice=1

confirm() {  # confirm "question" -> 0 for yes
    [ "$yes" = 1 ] && return 0
    local answer=""
    read -r -p "$1 [y/N] " answer || true  # no terminal: no
    [[ "$answer" =~ ^([yY]|[yY][eE][sS])$ ]]
}

uv="$(command -v uv || true)"
if [ -z "$uv" ]; then
    say "uv (the Python package manager Vision uses) is required: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi
[ "$doctor" = 1 ] && { command -v claude >/dev/null || say "Claude Code is not installed (https://code.claude.com); Vision can also use Codex, Grok or a local model."; }

extras=()
if [ "$voice" = 1 ]; then
    apple_silicon=0
    [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] && apple_silicon=1
    if [ "$backend" = mac ] && [ "$apple_silicon" = 0 ]; then
        say "--mac needs an Apple Silicon Mac (MLX runs on its GPU); use --cpu"; exit 2
    fi
    if [ "$(uname -s)" != Linux ]; then
        [ "$backend" = nvidia ] && { say "--nvidia is Linux only (there are no CUDA wheels for $(uname -s)); use --mac or --cpu"; exit 2; }
        # Apple Silicon: MLX, in real time; PyTorch there is 1.5-3x slower than real time and breaks up.
        [ -z "$backend" ] && { [ "$apple_silicon" = 1 ] && backend=mac || backend=cpu; }
    fi
    gpu=""
    command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 && gpu="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
    if [ -z "$backend" ]; then
        detected=cpu && [ -n "$gpu" ] && detected=nvidia
        if [ -n "$gpu" ]; then say "found an NVIDIA GPU: $gpu"; else say "no NVIDIA GPU found (nvidia-smi)"; fi
        backend="$detected"
        if [ "$yes" = 0 ]; then
            answer=""
            read -r -p "Voice build: [n]vidia (CUDA, ~6 GB) or [c]pu (~2 GB)? [${detected:0:1}] " answer || true  # no terminal: detected
            case "$answer" in
                [nN]*) backend=nvidia ;;
                [cC]*) backend=cpu ;;
            esac
        fi
    elif [ "$backend" = nvidia ] && [ -z "$gpu" ]; then
        say "--nvidia, but no NVIDIA GPU was found: the voice will fall back to the CPU and the CUDA libraries go unused."
    fi
    case "$backend" in nvidia) size="about 6 GB" ;; mac) size="about 1 GB" ;; *) size="about 2 GB" ;; esac
    if confirm "Voice ($backend) downloads $size of packages now and about 10 GB of speech models after. Continue?"; then
        extras+=("voice-$backend")
    else
        say "skipping voice; run this again with --voice when you want it."
        voice=0
    fi
fi
[ "$serve" = 1 ] && extras+=(serve)
[ "$all" = 1 ] && extras+=(weather)

cd "$root"
if [ ! -x .venv/bin/python ]; then
    say "creating .venv (Python 3.12; uv downloads it if needed)"
    "$uv" venv --python 3.12 .venv
fi
sync=(sync --frozen --inexact)  # --inexact: a re-run never removes what an earlier one installed
for extra in ${extras[@]+"${extras[@]}"}; do sync+=(--extra "$extra"); done  # bash 3.2 (macOS): empty array + set -u
say "installing: text chat${extras[*]+ + ${extras[*]}}"
"$uv" "${sync[@]}"

# Switching an NVIDIA build to --cpu: --inexact left the CUDA libraries behind, and nothing uses them now.
if [ "$voice" = 1 ] && [ "$backend" = cpu ]; then
    stale=$("$uv" pip list --python .venv/bin/python --format freeze 2>/dev/null |
        awk -F'==' '$1 ~ /^(nvidia-|cuda-)/ || $1 == "triton" { print $1 }')
    if [ -n "$stale" ]; then
        say "removing the CUDA libraries the NVIDIA build left behind"
        # shellcheck disable=SC2086
        "$uv" pip uninstall --python .venv/bin/python $stale
    fi
fi

# Switching to --mac: the torch voice stack stays behind (--inexact), and it no longer imports next to
# mlx-audio's transformers 5, so take it out.
if [ "$voice" = 1 ] && [ "$backend" = mac ]; then
    stale=$("$uv" pip list --python .venv/bin/python --format freeze 2>/dev/null |
        awk -F'==' '$1 ~ /^(torch|torchaudio|qwen-tts|accelerate|triton)$/ || $1 ~ /^(nvidia-|cuda-)/ { print $1 }')
    if [ -n "$stale" ]; then
        say "removing the torch voice build (--mac runs the voice through MLX)"
        # shellcheck disable=SC2086
        "$uv" pip uninstall --python .venv/bin/python $stale
    fi
fi

if [ "$voice" = 1 ]; then
    say "fetching the speech models (vision setup)"
    .venv/bin/python -m vision setup
fi

if [ "$link" = 1 ]; then
    mkdir -p "$HOME/.local/bin"
    ln -sf "$root/bin/vision" "$HOME/.local/bin/vision"
    say "linked $HOME/.local/bin/vision"
    add_to_path
fi

[ "$doctor" = 0 ] && exit 0
say "checking the install (vision doctor$(command -v claude >/dev/null && echo ': one small Claude request'))"
.venv/bin/python -m vision doctor --no-usage || true

start=$([ "$link" = 1 ] && echo vision || echo "$root/bin/vision")
say "done. Start a chat with:  $start"
[ "$link" = 0 ] && ! command -v vision >/dev/null && say "run this again with --link to start it as just: vision"
exit 0
