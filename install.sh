#!/bin/sh
# Install Vision, or update an install made with this script:
#
#   curl -fsSL https://raw.githubusercontent.com/vision-sAi/vision/master/install.sh | sh
#
# It fetches Vision into ~/.local/share/vision/app, makes its own Python environment there (with uv, which
# it installs first if you don't have it), and links the `vision` command into ~/.local/bin. Nothing is
# installed system-wide and nothing needs sudo. This is the text chat, a few MB; `vision` asks about your
# providers and the voice the first time it runs.
#
#   VISION_HOME     where to put it (default ~/.local/share/vision/app)
#   VISION_BRANCH   the branch to install (default master)
#   VISION_TARBALL  a .tar.gz of the source to install instead of fetching from GitHub
set -eu

repo="${VISION_REPO:-https://github.com/vision-sAi/vision}"
branch="${VISION_BRANCH:-master}"
dest="${VISION_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/vision/app}"
bin="$HOME/.local/bin"

say() { printf '%s\n' "$*"; }
step() { printf '\033[1;36m◆\033[0m %s\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

case "$(uname -s)" in
    Linux | Darwin) ;;
    *) die "Vision runs on Linux and macOS. On Windows, install it inside WSL." ;;
esac
have curl || die "curl is needed to download Vision."
have tar || die "tar is needed to unpack Vision."
have bash || die "bash is needed to run Vision's setup."

step "Installing Vision"

# uv: Vision's Python and packages come through it, so no system Python is needed
orig_path="$PATH"
PATH="$bin:$HOME/.cargo/bin:$PATH"
if ! have uv; then
    say "  Getting uv (the Python package manager Vision uses)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "Couldn't install uv. See https://docs.astral.sh/uv/ and run this again."
    have uv || die "uv installed, but it isn't in $bin. Open a new terminal and run this again."
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# the code: a git checkout when git is here (so an update is a pull), a tarball otherwise
mkdir -p "$dest"
if [ -z "${VISION_TARBALL:-}" ] && have git && [ -d "$dest/.git" ]; then
    git -C "$dest" pull --quiet --ff-only || die "Couldn't update $dest (local changes?). Fix it there, or remove it and run this again."
elif [ -z "${VISION_TARBALL:-}" ] && have git && [ -z "$(ls -A "$dest")" ]; then
    git clone --quiet --depth 1 --branch "$branch" "$repo" "$dest" || die "Couldn't download Vision from $repo."
else
    tmp="$(mktemp)"
    curl -fsSL "${VISION_TARBALL:-$repo/archive/refs/heads/$branch.tar.gz}" -o "$tmp" || { rm -f "$tmp"; die "Couldn't download Vision from $repo."; }
    tar -xzf "$tmp" -C "$dest" --strip-components=1 || { rm -f "$tmp"; die "Couldn't unpack Vision into $dest."; }
    rm -f "$tmp"
fi
ok "Vision is in $dest"

# its environment and the `vision` command
say "  Setting up its Python environment…"
log="$(mktemp)"
if ! bash "$dest/scripts/setup.sh" --link --yes --no-doctor >"$log" 2>&1; then
    cat "$log" >&2
    rm -f "$log"
    die "Setup failed. The lines above say why; run this again once it's fixed."
fi
rm -f "$log"
ok "vision command in $bin"

say ""
case ":$orig_path:" in *":$bin:"*) on_path=1 ;; *) on_path=0 ;; esac
if [ "$on_path" = 1 ]; then
    printf '\033[1mDone.\033[0m Start it with:  \033[1;36mvision\033[0m\n'
else
    case "${SHELL:-}" in
        */zsh) rc="~/.zshrc" ;;
        */fish) rc="" ;;
        *) rc="~/.bashrc" ;;
    esac
    printf '\033[1mDone.\033[0m One more thing: %s is not on your PATH yet. Run this, then open a new terminal:\n\n' "$bin"
    if [ -n "$rc" ]; then
        say "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> $rc"
    else
        say "  fish_add_path ~/.local/bin"
    fi
    printf '\nThen start it with:  \033[1;36mvision\033[0m\n'
fi
