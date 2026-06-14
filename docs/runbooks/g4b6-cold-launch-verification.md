---
title: G4b6 — Cold-launch Verification on the Notarized Bundle
type: runbook
status: active
last_updated: 2026-06-14
related_adrs:
  - 003-desktop-distribution-tauri-and-sidecars
  - 005-profile-driven-model-stacks
  - 015-single-target-local-only-stack
  - 019-licensing-operational-model
related_features:
  - desktop-shell-graduation
---

# G4b6 — Cold-launch verification on the notarized bundle

End-to-end verification that the notarized DeepFilesAI bundle behaves correctly for a first-time user install — fresh `<app_data>`, fresh keychain, no prior marker, no dev artifacts. This is the empirical gate at the bottom of [`docs/features/desktop-shell-graduation.md`](../features/desktop-shell-graduation.md#g4b6----cold-launch-verification-on-the-notarized-bundle); pass means desktop shell graduation closes, fail means each finding becomes a follow-up chunk before re-attempt.

The test loop is expected to surface real bugs (sidecar path resolution under Gatekeeper, `.deepfileslic` file-association flow, first-run wizard timing under bundle-mode, missing bundled assets). The build pipeline was split 2026-05-06 ([`build-dmg.sh`](../../desktop/scripts/build-dmg.sh)) so a transient `.dmg`-phase flake during retries doesn't cost the full 25-min .app rebuild.

---

## Prerequisites

- [`docs/runbooks/release-build-macos.md`](release-build-macos.md) — signing identity, notarytool keychain profile, and the build pipeline itself. **Run that runbook first** to produce the `.app` + `.dmg`. The verification below assumes both exist.
- Apple Silicon Mac (target architecture is `aarch64-apple-darwin`).

## Two ways to simulate a cold launch

### Option A — Different Mac (most realistic)
Move the `.dmg` to a stock macOS arm64 box that has never seen DeepFilesAI. This is the canonical test — it catches issues a dev box masks (cached signing tickets, lingering keychain entries, residual `~/Jarvis/`).

### Option B — Reset state on this Mac
Faster but less thorough. Removes the four persistence locations the app touches:

```sh
# 1. Tauri shell app-data dir (ollama-models cache lives here)
rm -rf "$HOME/Library/Application Support/app.deepfilesai.desktop"

# 2. Workspace dir (notes, vault, jarvis.db, .first_run_complete marker)
#    DEFAULT path; if you've changed Settings → Workspace, adjust.
rm -rf "$HOME/Jarvis"

# 3. Logs dir (so this run's diagnostics are uncontaminated)
rm -rf "$HOME/Library/Logs/DeepFilesAI"

# 4. Keychain entries (trial_started_at + monotonic_floor — service
#    'com.deepfilesai.desktop'). Idempotent — silently no-ops if absent.
security delete-generic-password -s "com.deepfilesai.desktop" -a "trial_started_at" 2>/dev/null || true
security delete-generic-password -s "com.deepfilesai.desktop" -a "monotonic_floor"   2>/dev/null || true
```

Then drag the `.app` from the mounted `.dmg` to `/Applications/` exactly like a user would. **Do not run from `desktop/src-tauri/target/...` — that's the dev path; run from `/Applications/DeepFilesAI.app` to hit the same Gatekeeper code path a user hits.**

---

## The 8 done-when checks

Run them in order. Items 7 and 8 are static (inspect the `.app` without launching) and can be done before or after launch — they're cheapest, do them first to catch obvious build issues.

### ✅ Check 7 — No cloud-SDK leakage in the bundle

> **Pass:** `find` output is empty.

```sh
find /Applications/DeepFilesAI.app \
    -name "*anthropic*" -o -name "*openai*" -o -name "*litellm*" \
    -o -name "*tiktoken*" -o -name "*google.generativeai*"
```

Per [ADR 015 §F audit signal 2](../architecture/decisions/015-single-target-local-only-stack.md). The build-time CI assertion in [`build-sidecar.sh`](../../desktop/scripts/build-sidecar.sh) already guards this for the sidecar binary's archive TOC, but `find` covers the full bundle (frontend + Rust + sidecar + Ollama + bundled weights).

> **Fail:** even one match means the dev venv had a cloud SDK installed and PyInstaller's `excludes` didn't catch it. Check [`desktop/sidecar/jarvis-sidecar.spec`](../../desktop/sidecar/jarvis-sidecar.spec) `excludes=` block — `litellm`, `anthropic`, `openai`, `tiktoken`, `tiktoken_ext`, `google.generativeai`, `google.generativelanguage` should all be present. If they are, the leak is from the Rust shell or the frontend bundle — `grep -r anthropic frontend/.output/public/` to localize.

### ✅ Check 8 — Info.plist capabilities match ADR 015

> **Pass:** array contains exactly the four expected capabilities, no more, no less.

```sh
defaults read /Applications/DeepFilesAI.app/Contents/Info.plist JarvisBundleCapabilities
```

Expected output:
```
(
    "local-llm",
    "vault-markdown",
    "knowledge-graph",
    "semantic-search"
)
```

> **Fail:** the `[4b/5]` step in [`build-notarized.sh`](../../desktop/scripts/build-notarized.sh) didn't run, or didn't re-sign after the plist edit (which would invalidate the notarization). If the key is missing entirely, PlistBuddy failed silently — re-run the build. If the key exists but with wrong values, someone added/removed a capability without updating ADR 015 §F.

### ✅ Check 1 — DMG installs cleanly

> **Pass:** double-click `.dmg` → drag-to-/Applications view → drag → no Gatekeeper warning, no quarantine prompt, app appears in `/Applications/`. `spctl --assess` returns `accepted, source=Notarized Developer ID`.

```sh
spctl --assess -vvv --type install /Volumes/DeepFilesAI/DeepFilesAI.app  # while .dmg is mounted
spctl --assess -vvv --type exec    /Applications/DeepFilesAI.app           # after copy
xcrun stapler validate /Applications/DeepFilesAI.app                       # ticket present
```

> **Fail modes:**
> - `"is damaged and can't be opened"` → quarantine xattr without a valid ticket. Run `xcrun stapler validate` on both the `.app` *inside* the `.dmg` and the post-copy version. If the dmg-internal one is fine but the copy isn't, Gatekeeper's policy DB is stale: `sudo spctl --master-disable; sudo spctl --master-enable` cycles it.
> - `"unidentified developer"` → notarization failed silently or the ticket wasn't stapled. `xcrun stapler validate` will say so. Re-run `bash desktop/scripts/build-dmg.sh` to retry the .dmg phase without rebuilding the .app.
> - DMG won't mount → hdiutil flake. Re-run `bash desktop/scripts/build-dmg.sh` (the new split). If it persists, try `tauri build --bundles dmg` separately to isolate from notarytool.

### ✅ Check 2 — First launch shows OnboardingLocalFlow + auto-kicks orchestrator

> **Pass:** double-click `/Applications/DeepFilesAI.app`. Within ~5-10 seconds the window opens directly into the OnboardingLocalFlow modal (not the chat UI). The orchestrator starts pulling the recommended model — progress visible in the wizard.

What's happening behind the scenes:
1. Tauri shell spawns the bundled Ollama (`OLLAMA_HOST=127.0.0.1:11435`).
2. Tauri shell spawns the sidecar with `JARVIS_OLLAMA_BASE_URL` pointing at it.
3. Frontend boots, reads license state (trial-active, since the keychain trial_started_at gets written on first run), then checks `is_first_run_complete()` → marker absent → renders `<OnboardingLocalFlow>`.
4. The orchestrator is auto-kicked via `POST /api/local/first-run/start` — see [`backend/services/first_run_orchestrator.py`](../../backend/services/first_run_orchestrator.py).

> **Fail modes:**
> - **Window opens but blank / spinner forever.** Sidecar didn't start. Check `~/Library/Logs/DeepFilesAI/*.log` for Python tracebacks. Most likely cause: bundled tokenizer cache (`_bundled_tokenizers/`) or fastembed weights missing — the sidecar spec aborts the *build* if these are missing, but if it built and these are at the wrong runtime path, look at `_MEIPASS` resolution in [`services/token_counting.py:_bundled_tokenizers_root`](../../backend/services/token_counting.py).
> - **Window opens to chat UI, not wizard.** Marker file from a prior run survived. Re-run the cold-launch reset (Option B above), or check `ls ~/Jarvis/app/.first_run_complete`.
> - **OnboardingLocalFlow shows but progress bar stuck at 0%.** Ollama isn't reachable. From a terminal: `curl http://127.0.0.1:11435/` should return `Ollama is running`. If it doesn't, the sidecar's bundled Ollama didn't spawn — `ps aux | grep ollama` and check the Tauri shell's log output for spawn errors. Most common cause: the `binaries/ollama-runtime/ollama` binary isn't quarantine-stripped or isn't signed → won't run under Gatekeeper-enforced hardened runtime.

### ✅ Check 3 — Primary pull lands; chat reachable via `chatReady`

> **Pass:** OnboardingLocalFlow's primary-pull progress completes → wizard transitions to "Ready, opening chat…" → chat UI loads → first message round-trip works.

The default model per [ADR 005](../architecture/decisions/005-profile-driven-model-stacks.md) is hardware-tiered. On M5 Pro 24 GB you should get qwen3-8b or qwen3-14b. The pull is ~5-9 GB depending on tier, so this step takes 5-15 min on a typical home network.

> **Fail modes:**
> - **Pull starts then errors.** Open `~/Library/Logs/DeepFilesAI/*.log` and grep for `pull failed`. Usually a network issue — Ollama's pull endpoint hits `registry.ollama.ai` over HTTPS. Local firewalls may block this.
> - **Pull completes but chat doesn't open.** `chatReady` event didn't fire. Check the Tauri shell log for the WebSocket emit; check the frontend devtools console (Cmd+Opt+I — only works if the build was made with devtools enabled; production builds disable this).
> - **Chat opens but first message hangs.** Ollama generate failed. From a terminal: `curl -X POST http://127.0.0.1:11435/api/generate -d '{"model":"qwen3:8b","prompt":"hi","stream":false}'`. SIGABRT in the runner = the M5/Metal 4 issue (should be impossible since we pin Ollama 0.18.0; if it happens anyway, the bundled binary isn't actually 0.18.0 — `/Applications/DeepFilesAI.app/Contents/Resources/ollama-runtime/ollama --version` will say).

### ✅ Check 4 — Background fallback pull + chat-model probe complete without blocking

> **Pass:** chat is responsive while a second model pull (the fallback per [ADR 005](../architecture/decisions/005-profile-driven-model-stacks.md) downgrade ladder) finishes in the background. The chat-model probe ([`backend/services/chat_model_probe.py`](../../backend/services/chat_model_probe.py)) runs on the primary, then on the fallback, and surfaces verdicts in Settings → Local Models.

> **Fail modes:**
> - **Chat freezes during background pull.** Async wiring broken — should be checked by [`backend/tests/test_g4b_cold_launch.py`](../../backend/tests/test_g4b_cold_launch.py), but a real-world failure indicates the test stub didn't catch it. File a follow-up; not a release blocker if primary pull works.
> - **Chat-model probe verdict missing.** Settings → Local Models shows the model card but no probe verdict pill. Probe failed or didn't run. Check sidecar log for `chat_model_probe`.

### ✅ Check 5 — Marker file written

> **Pass:** after the wizard completes (chat is reachable), the marker file exists at the workspace path:

```sh
ls -la ~/Jarvis/app/.first_run_complete  # default workspace path
cat ~/Jarvis/app/.first_run_complete     # JSON with timestamp + completed-step list
```

> **Fail:** wizard claims success but no marker. The orchestrator hit the `chatReady` event but [`first_run_orchestrator.write_marker`](../../backend/services/first_run_orchestrator.py) didn't run. Sidecar log will have a Python traceback. Most likely cause: the workspace path doesn't exist or isn't writable — but the orchestrator should create it if missing.

### ✅ Check 6 — Second launch skips the wizard

> **Pass:** quit the app (Cmd+Q). Re-launch (`open /Applications/DeepFilesAI.app`). Window opens directly into the chat UI. No wizard, no orchestrator kick, no model re-pull.

The early-return path is `is_first_run_complete()` → True → `<OnboardingLocalFlow>` doesn't render → frontend goes straight to the chat layout.

> **Fail:** wizard re-appears. The marker check is reading the wrong path — usually a workspace-path mismatch between writes and reads. `is_first_run_complete()` resolves via `get_settings().workspace_path`; if that diverged between runs (e.g. user changed Settings → Workspace mid-session), the marker is in the old location. Reset state via Option B and try again.

---

## If all 8 pass

G4b6 is closed. Update [`docs/features/desktop-shell-graduation.md`](../features/desktop-shell-graduation.md) — flip the G4b6 row to ✅ done with the verification date, and remove the "(gated on chunk 7 of ADR 015)" qualifier. Desktop shell graduation hits its definition of done.

## If something fails

Each failure is its own follow-up chunk. The pattern: file an issue or note, link to the failing check in this runbook, capture the diagnostic output, fix the root cause, re-run the failing check (and any downstream check that depends on it). Don't bundle multiple unrelated fixes into one chunk — the verification is meant to flush bugs out individually so each fix is reviewable.

Common fix → re-test costs (using the 2026-05-06 split build pipeline):
- Pure Rust shell fix: `cargo build --release` + `bash desktop/scripts/build-notarized.sh` end-to-end (~25 min)
- Sidecar Python fix: `bash desktop/scripts/build-sidecar.sh` then resume `build-notarized.sh` from step 4
- Frontend fix: `cd frontend && npm run generate` then resume from step 4
- `.dmg`-only failure: `bash desktop/scripts/build-dmg.sh` (~2 min, no rebuild)

## Diagnostics quick reference

| Symptom | Command |
|---|---|
| App won't launch / Gatekeeper rejects | `spctl --assess -vvv --type exec /Applications/DeepFilesAI.app` |
| Verify notarization stapled | `xcrun stapler validate /Applications/DeepFilesAI.app` |
| Inspect signature + entitlements | `codesign -dvvv --entitlements - /Applications/DeepFilesAI.app` |
| Check Info.plist values | `defaults read /Applications/DeepFilesAI.app/Contents/Info.plist <key>` |
| Sidecar / orchestrator logs | `tail -f ~/Library/Logs/DeepFilesAI/*.log` |
| Bundled Ollama version | `/Applications/DeepFilesAI.app/Contents/Resources/ollama-runtime/ollama --version` |
| Test sidecar HTTP directly | `curl http://127.0.0.1:<sidecar-port>/api/health` (port logged on startup) |
| Test bundled Ollama directly | `curl http://127.0.0.1:11435/` |
| List keychain entries we own | `security dump-keychain \| grep com.deepfilesai.desktop` |
| Watch process tree | `ps -ef \| grep -E 'DeepFilesAI\|ollama\|jarvis-sidecar'` |
| Force-clear all state | see Option B above |
