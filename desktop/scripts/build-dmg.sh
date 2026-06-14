#!/usr/bin/env bash
#
# Notarize + staple the macOS .dmg.
#
# Split out from build-notarized.sh so a transient hdiutil / notarytool /
# stapler failure on the .dmg phase doesn't force a full rebuild — the .app
# is fully signed and stapled by the time build-notarized.sh delegates here,
# so the operator can re-run this script alone after a flake.
#
# Usage:
#   bash desktop/scripts/build-dmg.sh                  # default .dmg path
#   bash desktop/scripts/build-dmg.sh /path/to/X.dmg   # explicit path
#
# Credential resolution mirrors build-notarized.sh:
#   1. Keychain profile `notarytool-profile` (recommended; overridable via
#      NOTARY_PROFILE env var).
#   2. APPLE_ID + APPLE_PASSWORD env-var fallback.
#
# Pre-requisite: the .dmg must already exist on disk. `npx tauri build`
# (driven from build-notarized.sh) is what produces it. If the .dmg is
# missing, re-run build-notarized.sh from the start; this script does not
# rebuild artifacts.

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DESKTOP_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"

# --- signing: supply your own Apple Team ID via APPLE_TEAM_ID --------------
export APPLE_TEAM_ID="${APPLE_TEAM_ID:-TEAMID}"

# --- credential source: keychain profile preferred, env fallback ------------
NOTARY_PROFILE="${NOTARY_PROFILE:-notarytool-profile}"

if xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    echo "==> using keychain profile '$NOTARY_PROFILE' for notarization"
    NOTARY_AUTH=(--keychain-profile "$NOTARY_PROFILE")
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

# --- resolve .dmg path ------------------------------------------------------
DEFAULT_DMG="$DESKTOP_DIR/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/DeepBind_0.1.0_aarch64.dmg"
DMG="${1:-$DEFAULT_DMG}"

if [[ ! -f "$DMG" ]]; then
    cat >&2 <<EOF
error: .dmg not found at $DMG
       The tauri build step in build-notarized.sh produces the .dmg before
       this script runs. If it's missing, re-run build-notarized.sh from
       the start — this script only handles notarize + staple, not build.
EOF
    exit 1
fi

# --- notarize + staple ------------------------------------------------------
echo
echo "==> notarize + staple .dmg"
echo "    $DMG"
# .dmg gets its own notary submission so Gatekeeper can verify offline (no
# phone-home required on customer machines — relevant for our compliance
# audience).
xcrun notarytool submit "$DMG" "${NOTARY_AUTH[@]}" --wait
xcrun stapler staple "$DMG"

echo
echo "==> verification"
spctl --assess -vvv --type install "$DMG" || true
xcrun stapler validate "$DMG" || true

echo
echo "==> output"
echo "    .dmg: $DMG"
