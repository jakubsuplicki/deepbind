#!/usr/bin/env bash
#
# Build the desktop spike with full Developer ID signing + Apple notarization.
#
# Public values (signing identity, team ID) are baked into this script —
# they appear in any signed binary anyway. Apple notary credentials come from
# one of two places, in this order of preference:
#
#   1. Keychain profile `notarytool-profile` (recommended — set up once via
#      `xcrun notarytool store-credentials notarytool-profile`). Persists
#      The profile name is overridable via `NOTARY_PROFILE=<name>` env var.
#      across shells and requires no environment juggling at build time.
#
#   2. Falls back to APPLE_ID + APPLE_PASSWORD env vars if the keychain
#      profile is missing (CI, fresh box, or first-time-not-yet-stored):
#          export APPLE_ID="you@example.com"
#          export APPLE_PASSWORD="xxxx-xxxx-xxxx-xxxx"
#          bash desktop/scripts/build-notarized.sh
#
# What this script does:
#   1. Builds the Nuxt frontend.
#   2. Builds the PyInstaller sidecar (with weights, signed locally).
#   3. Invokes `tauri build`:
#        a. Compiles the Rust shell.
#        b. Bundles the .app and .dmg.
#        c. Signs everything under the Developer ID Application identity
#           with hardened-runtime + the entitlements at
#           src-tauri/macos/Entitlements.plist.
#        Tauri's auto-notarize step is intentionally skipped (we don't pass
#        APPLE_ID / APPLE_PASSWORD when using the keychain-profile path)
#        because Tauri's bundler doesn't speak `--keychain-profile`.
#   4. Manually notarizes BOTH .app and .dmg via `xcrun notarytool` using
#      our keychain profile (or env-var fallback), then staples both.
#
# Apple's verdict is one of:
#   - Accepted  → ticket stapled, build OK.
#   - Invalid   → run
#         xcrun notarytool log <id> --keychain-profile notarytool-profile
#       to see exact entitlement / signature errors per file.
#
# After a successful run, verify with:
#   spctl --assess -vvv \
#     desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/DeepFilesAI.app
#   xcrun stapler validate \
#     desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/DeepFilesAI.app

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DESKTOP_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"

# --- public, safe to commit -------------------------------------------------
export APPLE_SIGNING_IDENTITY="Developer ID Application: EXAMPLE (TEAMID)"
export APPLE_TEAM_ID="TEAMID"

# --- credential source: keychain profile preferred, env fallback ------------
# notarytool supports a `--keychain-profile <name>` form that reads creds
# Apple has stored in the login keychain (see `xcrun notarytool store-creds`).
# We use that path. Tauri's bundler doesn't speak --keychain-profile; instead
# we let Tauri sign-only (no APPLE_ID/PASSWORD in env → Tauri skips notarize)
# and notarize both .app and .dmg ourselves below using NOTARY_AUTH.
NOTARY_PROFILE="${NOTARY_PROFILE:-notarytool-profile}"

if xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    echo "==> using keychain profile '$NOTARY_PROFILE' for notarization"
    NOTARY_AUTH=(--keychain-profile "$NOTARY_PROFILE")
    # Force-unset env vars so Tauri doesn't try to auto-notarize in the
    # bundler step (which would re-prompt for the same creds via env).
    unset APPLE_ID APPLE_PASSWORD
elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_PASSWORD:-}" ]]; then
    echo "==> using APPLE_ID/APPLE_PASSWORD env vars for notarization"
    NOTARY_AUTH=(
        --apple-id "$APPLE_ID"
        --password "$APPLE_PASSWORD"
        --team-id "$APPLE_TEAM_ID"
    )
else
    cat >&2 <<EOF
error: no notarization credentials found.
Pick one:
  (recommended) Save them once into your login keychain:
      xcrun notarytool store-credentials $NOTARY_PROFILE \\
          --apple-id <your apple-id email> --team-id $APPLE_TEAM_ID
  (one-shot) Export for this shell:
      export APPLE_ID=<your apple id email>
      export APPLE_PASSWORD=<app-specific password>
EOF
    exit 1
fi

# --- ensure cargo is on PATH ------------------------------------------------
if [[ -f "$HOME/.cargo/env" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
fi

cd "$DESKTOP_DIR"

# G3 graduation: bundle the real Nuxt frontend, not the static spike page.
# `tauri build` reads frontendDist from tauri.conf.json (now ../../frontend/.output/public),
# so we must `nuxt generate` first or the bundle will be missing the SPA.
echo "==> [1/5] generating Nuxt frontend (.output/public)"
(
    cd "$DESKTOP_DIR/../frontend"
    if [[ ! -d node_modules ]]; then
        npm install --no-audit --no-fund --prefer-offline
    fi
    npm run generate
)

echo
echo "==> [2/5] building sidecar binary"
bash scripts/build-sidecar.sh

echo
echo "==> [3/5] staging bundled Ollama runtime (G4a)"
# Idempotent: skips re-download + re-extract + re-sign when the runtime
# directory already matches the pinned OLLAMA_VERSION in fetch-ollama.sh.
# The first run downloads ~164 MB; subsequent builds are fast.
bash scripts/fetch-ollama.sh

echo
echo "==> [4/5] tauri build (sign .app + .dmg with Developer ID)"
echo "    identity:  $APPLE_SIGNING_IDENTITY"
echo "    team id:   $APPLE_TEAM_ID"
echo
npx tauri build --target aarch64-apple-darwin

APP="$DESKTOP_DIR/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/DeepFilesAI.app"
DMG="$DESKTOP_DIR/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/DeepFilesAI_0.1.0_aarch64.dmg"

if [[ ! -d "$APP" ]]; then
    echo "error: tauri build did not produce $APP" >&2
    exit 1
fi
if [[ ! -f "$DMG" ]]; then
    echo "error: tauri build did not produce $DMG" >&2
    exit 1
fi

echo
echo "==> [4b/5] writing JarvisBundleCapabilities to Info.plist (ADR 015)"
# Audit verification path 3 — buyer reads the capability array from the
# .app's Info.plist without unpacking the binary. ADR 015 collapsed the
# build to a single target (local-only), so the array is now static.
# `Delete :Add` cycle keeps re-runs idempotent.
INFO_PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Delete :JarvisBundleCapabilities" "$INFO_PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :JarvisBundleCapabilities array" "$INFO_PLIST"
for cap in local-llm vault-markdown knowledge-graph semantic-search; do
    /usr/libexec/PlistBuddy -c "Add :JarvisBundleCapabilities: string $cap" "$INFO_PLIST"
done
# Re-sign after touching Info.plist — modifying the plist invalidates the
# existing bundle signature; without re-sign, notarization rejects.
codesign --force --sign "$APPLE_SIGNING_IDENTITY" --options runtime --timestamp \
    --entitlements "$DESKTOP_DIR/src-tauri/macos/Entitlements.plist" "$APP"

echo
echo "==> [5a/5] notarize + staple the .app"
# Notary takes a zip of the .app, not the .app itself. Build the zip in a
# temp file so we don't pollute the bundle output.
APP_ZIP="$(mktemp -u).zip"
ditto -c -k --keepParent "$APP" "$APP_ZIP"
xcrun notarytool submit "$APP_ZIP" "${NOTARY_AUTH[@]}" --wait
rm -f "$APP_ZIP"
xcrun stapler staple "$APP"

echo
echo "==> verification (.app)"
# At this point the .app is fully built, signed, and stapled — its work is
# preserved across any later .dmg-phase failure.
spctl --assess -vvv --type exec "$APP" || true
xcrun stapler validate "$APP" || true
echo
echo "    .app: $APP"

echo
echo "==> [5b/5] notarize + staple the .dmg (delegated to build-dmg.sh)"
# Split out so a transient hdiutil/notarytool/stapler flake on the .dmg
# doesn't force a 30-min .app rebuild. If this step fails, retry it alone:
#     bash desktop/scripts/build-dmg.sh
if ! bash "$SCRIPT_DIR/build-dmg.sh" "$DMG"; then
    cat >&2 <<EOF

  ✓ .app is fully built, signed, and stapled at:
      $APP
  ✗ .dmg phase failed (see error above).
  → retry without rebuilding the .app:
      bash desktop/scripts/build-dmg.sh
EOF
    exit 1
fi
