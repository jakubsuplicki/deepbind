#!/usr/bin/env bash
#
# Dev-mode loop for the desktop graduation: real backend + real Nuxt UI inside
# the Tauri shell, with no PyInstaller bundle in the path. Three processes,
# one terminal:
#
#   1. Backend  — backend/scripts/run_frozen.py on $JARVIS_DEV_PORT (default
#                 8765). Same READY-line + watchdog code that ships in the
#                 PyInstaller bundle, just running from the venv.
#   2. Frontend — Nuxt dev server on :3000 (Vite HMR). The Tauri webview
#                 loads this URL via tauri.conf.json `build.devUrl`.
#   3. Shell    — `npx tauri dev`. Reads JARVIS_DEV_BACKEND_URL from env and
#                 skips its own sidecar-spawn path entirely; just injects
#                 `window.__JARVIS_CONFIG__` against the URL we pass.
#
# Iteration shape:
#   - Edit backend code        → Ctrl+C, re-run dev.sh.
#   - Edit frontend Vue/TS     → save; HMR pushes through Vite to the webview.
#   - Edit lib.rs (Rust shell) → save; tauri dev recompiles + restarts.
#
# Override the backend port via JARVIS_DEV_PORT if 8765 is taken on your box.
#
# Dev-only — never signs, never notarizes, never bundles. For a real
# distributable, run scripts/build-notarized.sh.

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DESKTOP_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "$DESKTOP_DIR/.." &> /dev/null && pwd )"

VENV_PY="$REPO_ROOT/backend/.venv/bin/python"
BACKEND_PORT="${JARVIS_DEV_PORT:-8765}"
FRONTEND_PORT=3000

if [[ ! -x "$VENV_PY" ]]; then
    echo "error: backend venv not found at $VENV_PY" >&2
    echo "run 'npm run wake-up-jarvis' from the repo root first." >&2
    exit 1
fi

if [[ -f "$HOME/.cargo/env" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
fi

free_port() {
    local port="$1" name="$2"
    local existing
    existing=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
    if [[ -n "$existing" ]]; then
        echo "==> $name port $port busy (pid=$existing), killing"
        kill -9 "$existing" 2>/dev/null || true
        sleep 0.3
    fi
}
free_port "$BACKEND_PORT" backend
free_port "$FRONTEND_PORT" frontend

# 1. Start the real backend via the frozen entrypoint. Same code path the
#    PyInstaller bundle uses, so dev mode and production behave identically.
echo "==> [1/3] starting backend (run_frozen.py) on port $BACKEND_PORT"
JARVIS_API_PORT="$BACKEND_PORT" \
    "$VENV_PY" "$REPO_ROOT/backend/scripts/run_frozen.py" &
BACKEND_PID=$!

# 2. Start the Nuxt dev server. Frontend reads window.__JARVIS_CONFIG__ at
#    runtime via apiUrl()/useWebSocket; the Vite proxy in nuxt.config.ts is
#    the fallback for plain-browser dev (without Tauri injecting the global).
echo "==> [2/3] starting Nuxt dev server on port $FRONTEND_PORT"
(
    cd "$REPO_ROOT/frontend"
    if [[ ! -d node_modules ]]; then
        npm install --no-audit --no-fund --prefer-offline
    fi
    npm run dev
) &
FRONTEND_PID=$!

cleanup() {
    echo
    echo "==> stopping backend (pid=$BACKEND_PID) + frontend (pid=$FRONTEND_PID)"
    kill -TERM "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
    sleep 0.3
    kill -9 "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for both servers to come up before launching the Tauri shell.
echo "==> waiting for backend /api/health"
deadline=$((SECONDS + 20))
until curl -sSf "http://127.0.0.1:$BACKEND_PORT/api/health" > /dev/null 2>&1; do
    if (( SECONDS > deadline )); then
        echo "error: backend did not become ready within 20s" >&2
        exit 1
    fi
    sleep 0.3
done
echo "    backend ready"

echo "==> waiting for Nuxt dev server"
deadline=$((SECONDS + 60))
until curl -sSf "http://127.0.0.1:$FRONTEND_PORT/" > /dev/null 2>&1; do
    if (( SECONDS > deadline )); then
        echo "error: Nuxt dev server did not become ready within 60s" >&2
        exit 1
    fi
    sleep 0.5
done
echo "    Nuxt ready"

# 3. Launch the Tauri shell. JARVIS_DEV_BACKEND_URL makes lib.rs skip its
#    bundled-sidecar spawn path; tauri.conf.json `build.devUrl` makes it
#    load the Nuxt dev server (HMR).
cd "$DESKTOP_DIR"
echo "==> [3/3] launching tauri dev (Rust hot-recompile + Vite HMR)"
JARVIS_DEV_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" \
    npx tauri dev
