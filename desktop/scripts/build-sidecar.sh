#!/usr/bin/env bash
#
# Build the spike sidecar binary into desktop/src-tauri/binaries/, named
# per Tauri's externalBin naming convention: <name>-<target-triple>.
#
# Tauri 2 expects sidecar binaries to be named like:
#     jarvis-sidecar-aarch64-apple-darwin
#     jarvis-sidecar-x86_64-apple-darwin
#     jarvis-sidecar-x86_64-pc-windows-msvc.exe
#
# so it can pick the right binary at bundle time per target.

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DESKTOP_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "$DESKTOP_DIR/.." &> /dev/null && pwd )"

VENV_PY="$REPO_ROOT/backend/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    echo "error: backend venv not found at $VENV_PY" >&2
    echo "run 'npm run wake-up-jarvis' from the repo root first." >&2
    exit 1
fi

# Resolve target triple (used by Tauri's sidecar lookup).
TRIPLE="$( "$VENV_PY" -c 'import platform, sys; m = platform.machine(); s = sys.platform; \
print(("aarch64" if m == "arm64" else m) + "-apple-darwin" if s == "darwin" else \
      ("x86_64" if m in ("AMD64","x86_64") else m) + "-pc-windows-msvc" if s == "win32" else \
      m + "-unknown-linux-gnu")' )"

OUT_DIR="$DESKTOP_DIR/src-tauri/binaries"
mkdir -p "$OUT_DIR"

echo "==> building sidecar for triple: $TRIPLE"

# G2b: ensure fastembed weights are cached locally before PyInstaller runs,
# otherwise the spec aborts (ADR 003 §A — the bundle must be offline-capable
# from minute zero). The fetch script is idempotent.
bash "$SCRIPT_DIR/fetch-bundled-models.sh"

cd "$DESKTOP_DIR/sidecar"
# G2 graduation: bundle the real backend (run_frozen.py) instead of the
# spike's hello.py. The new spec collects all backend submodules + the
# hidden imports for fastembed/onnxruntime/spacy/keyring per ADR 003 §A+§J,
# and (G2b) ships the fastembed cache + spaCy NER models inside the bundle.
"$VENV_PY" -m PyInstaller --noconfirm --clean jarvis-sidecar.spec

SRC="$DESKTOP_DIR/sidecar/dist/jarvis-sidecar"
if [[ ! -f "$SRC" ]]; then
    echo "error: PyInstaller did not produce $SRC" >&2
    exit 1
fi

DEST="$OUT_DIR/jarvis-sidecar-$TRIPLE"
cp "$SRC" "$DEST"
chmod +x "$DEST"

# Sign with Developer ID locally so amfid trusts the bundle on launch.
# Without this, ad-hoc-signed bundles >300 MB hang in `_dyld_start` for many
# minutes on macOS Tahoe (verified 2026-04-29 during G2b — the embedded
# fastembed weights pushed the bundle past the threshold). Notarized builds
# (build-notarized.sh) re-sign with the same identity, so this matches prod.
# When the cert is missing (CI box), we leave the bundle ad-hoc and the smoke
# test below will surface the dyld hang as a 60 s READY-line timeout.
if [[ "$(uname -s)" == "Darwin" ]]; then
    SIGNING_IDENTITY="${APPLE_SIGNING_IDENTITY:-Developer ID Application: EXAMPLE (TEAMID)}"
    ENTITLEMENTS="$DESKTOP_DIR/src-tauri/macos/Entitlements.plist"
    if security find-identity -p codesigning 2>&1 | grep -qF "$SIGNING_IDENTITY"; then
        echo "==> signing sidecar with: $SIGNING_IDENTITY"
        codesign \
            --force \
            --options runtime \
            --entitlements "$ENTITLEMENTS" \
            --sign "$SIGNING_IDENTITY" \
            --timestamp=none \
            "$DEST"
    else
        echo "    (Developer ID cert not on this box — leaving ad-hoc; smoke test may stall on macOS Tahoe)"
    fi
fi

# Smoke test — boot the real backend on an ephemeral port, wait for the
# READY line, hit /api/health, then kill. The real backend takes longer to
# spin up than the spike (lazy-imports during the first request, fastembed
# loads ~240 MB ONNX from the bundled cache), so the deadline is 60s.
echo "==> smoke test: booting $DEST"
LOG=$(mktemp)
JARVIS_API_PORT=0 "$DEST" > "$LOG" 2>&1 &
PID=$!
PORT=""
deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
    if grep -q "JARVIS_BACKEND_READY" "$LOG" 2>/dev/null; then
        PORT=$(grep -m1 "JARVIS_BACKEND_READY" "$LOG" | sed -n 's/.*port=\([0-9]*\).*/\1/p')
        break
    fi
    sleep 0.5
done

if [[ -z "$PORT" ]]; then
    echo "error: bundle did not emit READY line within 60s. Log tail:" >&2
    tail -20 "$LOG" >&2
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$LOG"
    exit 1
fi

echo "    READY: port=$PORT"
if curl -sSf "http://127.0.0.1:$PORT/api/health" > /dev/null; then
    echo "    /api/health responded ✓"
else
    echo "    /api/health failed ✗"
    tail -20 "$LOG" >&2
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$LOG"
    exit 1
fi

kill -TERM "$PID" 2>/dev/null || true
sleep 0.3
kill -9 "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
rm -f "$LOG"

echo "==> sidecar built: $DEST"
