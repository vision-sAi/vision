#!/usr/bin/env bash
# Dev stack beside the installed Vision: a serve from this checkout on its own port and data folder,
# plus the desktop app in dev mode. Ctrl-C stops both. Pair the window with the printed address and key.
set -euo pipefail
cd "$(dirname "$0")"
port="${PORT:-8766}"
export XDG_DATA_HOME="$HOME/.vision-dev"
export XDG_CONFIG_HOME="$HOME/.vision-dev/config"
export XDG_STATE_HOME="$HOME/.vision-dev/state"

# The exported XDG folders reach the app too, so it finds this serve by itself; the key is for pairing by hand.
[ -x .venv/bin/python ] || { echo "dev.sh: no .venv here: run \`uv sync --extra serve\` first" >&2; exit 1; }
if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "dev.sh: port $port is busy: stop what holds it, or PORT=8767 ./dev.sh" >&2; exit 1
fi

.venv/bin/python -m vision serve --port "$port" --no-qr &
serve=$!
trap 'kill $serve 2>/dev/null; wait $serve 2>/dev/null' EXIT INT TERM

for _ in $(seq 50); do [ -s "$XDG_CONFIG_HOME/vision/remote_token" ] && break; kill -0 "$serve" 2>/dev/null || break; sleep 0.2; done
kill -0 "$serve" 2>/dev/null || { echo "dev.sh: vision serve stopped (its output is above)" >&2; exit 1; }
echo "Pair with http://127.0.0.1:$port  key: $(cat "$XDG_CONFIG_HOME/vision/remote_token" 2>/dev/null)"
cd desktop
[ -d node_modules ] || npm install
npm run tauri dev
