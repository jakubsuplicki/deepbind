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

# ADR 019: inject the production license-verification public key at build
# time. The PyInstaller spec includes whatever ships at
# backend/services/_license_pubkey_baked.py via the existing
# collect_submodules("services") path; we write that file from the
# JARVIS_LICENSE_PUBKEY_HEX env var (64-char hex of 32 raw bytes), then
# remove it post-build so dev iteration doesn't accidentally bake a stale
# key. Without the env var, builds fall back to the dev key embedded in
# services/license_public_key.py — fine for local dev, refused for
# production builds (JARVIS_BUILD_PROFILE=production).
BAKED_FILE="$REPO_ROOT/backend/services/_license_pubkey_baked.py"
EPOCH_FILE="$REPO_ROOT/backend/services/_build_epoch_baked.py"
BUILD_PROFILE="${JARVIS_BUILD_PROFILE:-dev}"
if [[ -n "${JARVIS_LICENSE_PUBKEY_HEX:-}" ]]; then
    if [[ ${#JARVIS_LICENSE_PUBKEY_HEX} -ne 64 ]]; then
        echo "error: JARVIS_LICENSE_PUBKEY_HEX must be 64 hex chars (32 bytes); got ${#JARVIS_LICENSE_PUBKEY_HEX}" >&2
        exit 1
    fi
    if ! [[ "$JARVIS_LICENSE_PUBKEY_HEX" =~ ^[0-9a-fA-F]{64}$ ]]; then
        echo "error: JARVIS_LICENSE_PUBKEY_HEX is not valid hex" >&2
        exit 1
    fi
    echo "==> injecting production license public key (ADR 019)"
    cat > "$BAKED_FILE" <<EOF
# AUTO-GENERATED at build time by desktop/scripts/build-sidecar.sh.
# Do not hand-edit. Removed post-build. See ADR 019.
LICENSE_PUBLIC_KEY_HEX = "$JARVIS_LICENSE_PUBKEY_HEX"
EOF
elif [[ "$BUILD_PROFILE" == "production" ]]; then
    echo "error: production build requested (JARVIS_BUILD_PROFILE=production) but" >&2
    echo "       JARVIS_LICENSE_PUBKEY_HEX is not set. Refusing to build a" >&2
    echo "       production sidecar with the dev license fallback key." >&2
    exit 1
else
    echo "==> no JARVIS_LICENSE_PUBKEY_HEX set (dev build — using dev license key)"
fi

# ADR 019 chunk 6: bake the build epoch so the sidecar can refuse a
# system clock earlier than its build date (clock-rollback defense).
# Always bake this for any build that ships a license-aware sidecar —
# unlike the public key, an out-of-date dev epoch is harmless (it's a
# floor, not a fence). For dev builds we use the current time so the
# floor is always "now-ish" and trivially clears.
BUILD_EPOCH_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "==> baking build epoch (ADR 019 chunk 6): $BUILD_EPOCH_ISO"
cat > "$EPOCH_FILE" <<EOF
# AUTO-GENERATED at build time by desktop/scripts/build-sidecar.sh.
# Do not hand-edit. Removed post-build. See ADR 019 chunk 6.
BUILD_EPOCH_ISO = "$BUILD_EPOCH_ISO"
EOF

# Cleanup is unconditional — even if PyInstaller fails, we don't want
# either bake file lingering in the working tree (a stale public key
# would be especially bad on the next dev build).
trap 'rm -f "$BAKED_FILE" "$EPOCH_FILE"' EXIT

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

# ADR 015 audit gate: the bundle must contain no cloud-LLM SDKs. PyInstaller
# pulls whatever is installed in the build venv via the spec's hidden_imports
# + collect_data_files paths, so a dev venv with leftover `litellm` / `openai`
# / `anthropic` / `tiktoken` transitives would silently leak them into the
# `.app` even though they're absent from `requirements.txt`. The path
# fragments below appear in a PyInstaller one-file binary's archive TOC if
# (and only if) the corresponding package was bundled.
echo "==> verifying no cloud SDKs leaked into bundle (ADR 015)"
LEAKS=()
for mod in litellm anthropic openai tiktoken_ext; do
    if strings "$SRC" 2>/dev/null | LC_ALL=C grep -qF "${mod}/"; then
        LEAKS+=("$mod")
    fi
done
if (( ${#LEAKS[@]} > 0 )); then
    echo "error: cloud SDKs leaked into bundle: ${LEAKS[*]}" >&2
    echo "       Per ADR 015 the bundle must contain no cloud-LLM SDKs." >&2
    echo "       The dev venv likely has these installed as transitive leftovers." >&2
    echo "       Fix: \`uv pip sync backend/requirements.txt\` to clean the venv, then rebuild." >&2
    exit 1
fi
echo "    no cloud SDKs in bundle ✓"

# Audit findings #9 + #10: the bundle must not ship the developer-only
# samples/ directory (911Report.pdf is referenced by absolute path in test
# baselines) or the eval fixtures (reference_workspace/ is synthesized from
# OWASP CC-BY-SA-4.0 and Stanford HAI CC-BY-ND content — both license
# restrictions are problematic if redistributed). The PyInstaller spec
# already excludes both — only `tests.eval.latency` ships, not
# `tests.eval.fixtures` or `samples/`. This assertion is the regression
# guard so a future spec edit doesn't quietly re-include them.
echo "==> verifying no developer-only data leaked into bundle (audit #9, #10)"
DATA_LEAKS=()
for fragment in "samples/" "911Report.pdf" "tests/eval/fixtures/" "reference_workspace/" "reference_pdfs.json"; do
    if strings "$SRC" 2>/dev/null | LC_ALL=C grep -qF "$fragment"; then
        DATA_LEAKS+=("$fragment")
    fi
done
if (( ${#DATA_LEAKS[@]} > 0 )); then
    echo "error: developer-only data leaked into bundle: ${DATA_LEAKS[*]}" >&2
    echo "       Per commercial-licensing-audit.md findings #9 (samples/) and" >&2
    echo "       #10 (eval fixtures), the bundle must not redistribute these." >&2
    echo "       Likely cause: a recent edit to desktop/sidecar/jarvis-sidecar.spec" >&2
    echo "       added one of these paths to \`datas\` or extended a" >&2
    echo "       \`collect_submodules\` call past \`tests.eval.latency\`." >&2
    exit 1
fi
echo "    no developer-only data in bundle ✓"

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
