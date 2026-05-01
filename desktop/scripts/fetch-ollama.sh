#!/usr/bin/env bash
#
# Fetch + extract + re-sign the bundled Ollama runtime payload for the
# desktop app.
#
# Per ADR 003 §B + §"Amendment 2026-04-30" (G4a finding):
#   Ollama 0.22.0 on macOS is no longer a self-contained CLI. The CLI
#   binary at Ollama.app/Contents/Resources/ollama dlopens a runtime
#   payload that lives next to it:
#     - libggml-base.0.0.0.dylib (+ symlinks)
#     - libggml-cpu-*.so          (x86_64 CPU-feature kernels)
#     - mlx_metal_v3/             (libmlx + libmlxc + mlx.metallib)
#     - mlx_metal_v4/             (libmlx + libmlxc + mlx.metallib)
#   We bundle the entire runtime as a directory under
#   desktop/src-tauri/binaries/ollama-runtime/ and reference it from
#   Tauri's bundle.resources (not externalBin, which is single-binary
#   only). At launch the Rust shell spawns
#     <resource_dir>/ollama-runtime/ollama
#   directly via Command::new — relative dlopen of sibling dylibs works
#   because the binary uses @loader_path / @executable_path rpaths.
#
# Pin policy: per docs/research/deep-dive-deployment-architectures.md,
# Ollama is treated as a dependency with a version + SHA-256 pin. Bumps
# require a deliberate update to OLLAMA_VERSION + OLLAMA_DARWIN_ZIP_SHA256
# in this script, and re-running build-notarized.sh end-to-end.
#
# Apple notarization is satisfied by re-signing every Mach-O in the
# payload (binary + dylibs + .so files) under our Developer ID with
# hardened runtime; entitlements live on the main binary only (dylibs
# inherit at load time). The four entitlements in
# desktop/src-tauri/macos/Entitlements.plist were already chosen for
# this exact case (allow-jit + allow-unsigned-executable-memory +
# allow-dyld-environment-variables + disable-library-validation; see
# ADR 003 §B + the .plist comment block).
#
# Every codesign call below uses `--timestamp` to embed a secure
# timestamp from Apple's RFC-3161 TSA. Without it Apple's notary
# rejects with "The signature does not include a secure timestamp"
# on every nested .so / .dylib (verified 2026-04-30).

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DESKTOP_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"
REPO_ROOT="$( cd -- "$DESKTOP_DIR/.." &> /dev/null && pwd )"

# --- pinned upstream Ollama -------------------------------------------------
OLLAMA_VERSION="0.22.0"
# SHA-256 of upstream Ollama-darwin.zip @ v0.22.0. To bump:
#   1. update OLLAMA_VERSION above
#   2. delete this value, run the script, copy the printed SHA back here
#   3. commit the OLLAMA_VERSION + SHA together in one diff
OLLAMA_DARWIN_ZIP_SHA256="a410e2f722fb25d6f87ad2ac23a9d44e330b078762e19bfe5a3b0162d236b278"
OLLAMA_DARWIN_ZIP_URL="https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/Ollama-darwin.zip"

# --- output -----------------------------------------------------------------
RUNTIME_DIR="$DESKTOP_DIR/src-tauri/binaries/ollama-runtime"
VERSION_MARKER="$RUNTIME_DIR/.ollama-version"

# Idempotency: skip the whole flow if already present at the pinned version.
# The marker also drives bundle-cache invalidation when OLLAMA_VERSION moves.
if [[ -x "$RUNTIME_DIR/ollama" && -f "$VERSION_MARKER" ]]; then
    if [[ "$(cat "$VERSION_MARKER")" == "$OLLAMA_VERSION" ]]; then
        echo "==> ollama runtime $OLLAMA_VERSION already extracted at $RUNTIME_DIR"
        exit 0
    fi
fi

# --- download (cached) ------------------------------------------------------
CACHE_DIR="$REPO_ROOT/desktop/.ollama-cache"
mkdir -p "$CACHE_DIR"
ZIP_PATH="$CACHE_DIR/Ollama-darwin-${OLLAMA_VERSION}.zip"

if [[ ! -f "$ZIP_PATH" ]]; then
    echo "==> downloading $OLLAMA_DARWIN_ZIP_URL"
    curl -fL --progress-bar -o "$ZIP_PATH.tmp" "$OLLAMA_DARWIN_ZIP_URL"
    mv "$ZIP_PATH.tmp" "$ZIP_PATH"
else
    echo "==> using cached $ZIP_PATH"
fi

# --- verify SHA-256 ---------------------------------------------------------
ACTUAL_SHA="$(shasum -a 256 "$ZIP_PATH" | awk '{print $1}')"
if [[ -z "$OLLAMA_DARWIN_ZIP_SHA256" ]]; then
    echo "error: OLLAMA_DARWIN_ZIP_SHA256 is empty in fetch-ollama.sh." >&2
    echo "       Pin this value: $ACTUAL_SHA" >&2
    exit 1
elif [[ "$ACTUAL_SHA" != "$OLLAMA_DARWIN_ZIP_SHA256" ]]; then
    echo "error: SHA-256 mismatch on $ZIP_PATH" >&2
    echo "  expected: $OLLAMA_DARWIN_ZIP_SHA256" >&2
    echo "  actual:   $ACTUAL_SHA" >&2
    echo "  if upstream re-cut the release, audit the new artifact and update the pin." >&2
    exit 1
fi
echo "==> SHA-256 verified: $ACTUAL_SHA"

# --- extract ----------------------------------------------------------------
EXTRACT_DIR="$(mktemp -d)"
trap 'rm -rf "$EXTRACT_DIR"' EXIT
unzip -q "$ZIP_PATH" -d "$EXTRACT_DIR"

SRC_RES="$EXTRACT_DIR/Ollama.app/Contents/Resources"
if [[ ! -f "$SRC_RES/ollama" ]]; then
    echo "error: extracted layout doesn't match expected Ollama.app/Contents/Resources/ollama" >&2
    echo "       upstream zip layout may have changed; inspect $EXTRACT_DIR" >&2
    find "$EXTRACT_DIR" -maxdepth 5 -name "ollama" -type f >&2
    exit 1
fi

# Wipe any prior runtime dir so a stale dylib from a previous version can't
# survive a bump (ad-hoc-signed remnants would fail re-notarization).
rm -rf "$RUNTIME_DIR"
mkdir -p "$RUNTIME_DIR"

# Copy only the runtime payload — we leave behind:
#   - icon.icns / *.png         (menu-bar app artifacts; unused)
#   - Contents/MacOS/Ollama      (Swift menu-bar wrapper; ADR 003 §B says no)
#   - Contents/Library/LaunchAgents (com.ollama.ollama.plist — that's the
#     LaunchAgent we explicitly do NOT want — driver #2)
#   - Contents/Frameworks/Squirrel.framework (upstream auto-updater; ADR 003
#     uses tauri-plugin-updater for our own bundle, so Ollama's updater
#     would be both redundant and a second outbound surface)
echo "==> staging runtime payload from Ollama.app/Contents/Resources/"
cp "$SRC_RES/ollama" "$RUNTIME_DIR/"
cp -P "$SRC_RES/"libggml-base.*.dylib "$RUNTIME_DIR/" 2>/dev/null || true
cp -P "$SRC_RES/"libggml-base.dylib "$RUNTIME_DIR/" 2>/dev/null || true
# libggml-cpu-*.so are x86_64 CPU-feature variants. We're currently only
# building aarch64-apple-darwin; on Apple Silicon they're never dlopened.
# Bundling them anyway keeps the payload structurally identical to upstream
# (matters if we ever lipo a fat ollama binary or add an x86_64 build).
cp -P "$SRC_RES/"libggml-cpu*.so "$RUNTIME_DIR/" 2>/dev/null || true
# MLX Metal runners — both v3 (older macOS) and v4 (current) so we run on
# any macOS 12+ machine.
[[ -d "$SRC_RES/mlx_metal_v3" ]] && cp -R "$SRC_RES/mlx_metal_v3" "$RUNTIME_DIR/"
[[ -d "$SRC_RES/mlx_metal_v4" ]] && cp -R "$SRC_RES/mlx_metal_v4" "$RUNTIME_DIR/"

chmod +x "$RUNTIME_DIR/ollama"

# Strip Apple's quarantine bit on every staged file. Without this, Gatekeeper
# would refuse to load the dylibs at runtime even after we re-sign them
# (quarantine is sticky and survives codesign).
xattr -cr "$RUNTIME_DIR" 2>/dev/null || true

# --- re-sign every Mach-O ---------------------------------------------------
SIGNING_IDENTITY="${APPLE_SIGNING_IDENTITY:-Developer ID Application: EXAMPLE (TEAMID)}"
ENTITLEMENTS="$DESKTOP_DIR/src-tauri/macos/Entitlements.plist"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "==> not on macOS — leaving payload unsigned (build target is macOS-only for v1)"
    echo -n "$OLLAMA_VERSION" > "$VERSION_MARKER"
    echo "==> ollama runtime staged: $RUNTIME_DIR"
    exit 0
fi

if ! security find-identity -p codesigning 2>&1 | grep -qF "$SIGNING_IDENTITY"; then
    echo "==> WARNING: '$SIGNING_IDENTITY' not in keychain. Leaving ad-hoc signatures." >&2
    echo "    The bundle will not notarize without re-signing on the build box." >&2
    echo -n "$OLLAMA_VERSION" > "$VERSION_MARKER"
    exit 0
fi

# Find every Mach-O in the staged tree. We sign dylibs first, binary last,
# so the binary's signature can re-validate after dependents are hashed.
echo "==> signing payload with: $SIGNING_IDENTITY"

# 1) dylibs and .so files (no entitlements — those only apply to executables)
while IFS= read -r f; do
    # Skip symlinks (dylib short-names); codesign acts on the target.
    [[ -L "$f" ]] && continue
    codesign \
        --force \
        --options runtime \
        --sign "$SIGNING_IDENTITY" \
        --timestamp \
        "$f"
done < <(find "$RUNTIME_DIR" \( -name "*.dylib" -o -name "*.so" \) -type f)

# 2) the .metallib files — these are signable too, and Apple's notary
# rejects an unsigned .metallib inside a signed .app on hardened runtime.
while IFS= read -r f; do
    [[ -L "$f" ]] && continue
    codesign \
        --force \
        --sign "$SIGNING_IDENTITY" \
        --timestamp \
        "$f" 2>/dev/null || true   # .metallib codesign is best-effort; some
                                   # versions of notarytool tolerate them
                                   # unsigned. Don't fail the build.
done < <(find "$RUNTIME_DIR" -name "*.metallib" -type f)

# 3) the main binary — entitlements applied here. This is the entrypoint
# the OS evaluates when checking hardened-runtime constraints; sibling
# dylibs inherit those constraints at dlopen time.
codesign \
    --force \
    --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$SIGNING_IDENTITY" \
    --timestamp \
    "$RUNTIME_DIR/ollama"

# Verify the binary's signature is well-formed before declaring success.
codesign --verify --verbose=2 "$RUNTIME_DIR/ollama"

# --- smoke test -------------------------------------------------------------
echo "==> smoke test: ollama --version"
"$RUNTIME_DIR/ollama" --version

echo -n "$OLLAMA_VERSION" > "$VERSION_MARKER"

PAYLOAD_SIZE="$(du -sh "$RUNTIME_DIR" | awk '{print $1}')"
echo "==> ollama runtime ready: $RUNTIME_DIR (${PAYLOAD_SIZE})"
