---
title: Desktop Shell Spike (ADR 003 §K)
type: spike
status: spike
last_updated: 2026-04-29
related_adrs:
  - 003-desktop-distribution-tauri-and-sidecars
---

## Purpose

Validates the Tauri 2 + PyInstaller-sidecar architecture from [ADR 003](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) end-to-end on macOS arm64 — *before* any real backend work depends on the bundle existing. Per ADR 003 §"v1 schedule risk" and §K (amendment 2026-04-29), the first notarized build is a v1 prerequisite, not a v1 task.

This is **not** the real backend or the real frontend. It exists to prove the architecture compiles, packages, signs, launches, and notarizes.

## What's wired

```
┌─────────────────────────────────────────────────────┐
│ DeepBind.app  (Tauri shell, Rust)                │
│   ├─ MacOS/deepbind-desktop  ← shell binary      │
│   └─ MacOS/jarvis-sidecar       ← PyInstaller       │
│        (FastAPI + uvicorn, single-file binary)      │
└─────────────────────────────────────────────────────┘
```

1. Shell launches `jarvis-sidecar` via `tauri-plugin-shell::sidecar` and passes `JARVIS_SHELL_PID=<pid>` in the spawn env.
2. Sidecar binds an OS-assigned ephemeral port, writes one machine-readable line to stdout:
   ```
   JARVIS_BACKEND_READY host=127.0.0.1 port=<n>
   ```
3. Shell parses that line (regex match on the prefix), constructs `BackendConfig { backend_url, ws_url }`.
4. Shell creates the webview window with an `initialization_script` that injects `window.__JARVIS_CONFIG__ = { backendUrl, wsUrl }` *before* page load — so the static `index.html` finds it on first script execution.
5. Static page calls `fetch(cfg.backendUrl + '/api/health')` and renders the JSON.
6. On shutdown — clean (Cmd+Q / AppleScript quit) **or** force-kill (SIGKILL on the shell) — the sidecar terminates within ~1s via the shell-PID watchdog.

## Source-of-truth files

| File | Role |
|---|---|
| [desktop/sidecar/hello.py](../../desktop/sidecar/hello.py) | Minimal FastAPI app + READY-line emit + shell-PID watchdog. |
| [desktop/sidecar/hello.spec](../../desktop/sidecar/hello.spec) | PyInstaller spec — onefile, hidden imports, no UPX. |
| [desktop/scripts/build-sidecar.sh](../../desktop/scripts/build-sidecar.sh) | Builds sidecar binary into `src-tauri/binaries/jarvis-sidecar-<triple>`. |
| [desktop/src-tauri/Cargo.toml](../../desktop/src-tauri/Cargo.toml) | Rust deps: `tauri`, `tauri-plugin-shell`, `tokio`, log/serde. |
| [desktop/src-tauri/tauri.conf.json](../../desktop/src-tauri/tauri.conf.json) | Bundle config — identifier, externalBin, hardenedRuntime, entitlements path, CSP. |
| [desktop/src-tauri/src/lib.rs](../../desktop/src-tauri/src/lib.rs) | Spawn, READY-line parser, init_script injection, shutdown handler. |
| [desktop/src-tauri/capabilities/default.json](../../desktop/src-tauri/capabilities/default.json) | Capability allowlist — `shell:allow-execute` for `jarvis-sidecar` only. |
| [desktop/src-tauri/macos/Entitlements.plist](../../desktop/src-tauri/macos/Entitlements.plist) | Hardened-runtime entitlements (jit, dyld-env, library-validation off). |
| [desktop/frontend-spike/index.html](../../desktop/frontend-spike/index.html) | Static HTML probe page — reads `window.__JARVIS_CONFIG__`, hits `/api/health`. |

## How to build (macOS arm64)

```bash
cd desktop
npm install                       # installs @tauri-apps/cli
bash scripts/build-sidecar.sh     # PyInstaller → src-tauri/binaries/jarvis-sidecar-aarch64-apple-darwin
APPLE_SIGNING_IDENTITY="-" \
  npx tauri build --target aarch64-apple-darwin
```

Outputs:
- `src-tauri/target/aarch64-apple-darwin/release/bundle/macos/DeepBind.app`
- `src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/DeepBind_0.1.0_aarch64.dmg`

Ad-hoc-signed (`APPLE_SIGNING_IDENTITY="-"`) — local launch + Gatekeeper-override only. **Not notarized.**

To launch and observe:
```bash
open src-tauri/target/aarch64-apple-darwin/release/bundle/macos/DeepBind.app
```

The window shows a probe card: backend URL (ephemeral port), `/api/health` status, sidecar version, raw JSON.

## Validated empirically (2026-04-29)

| Check | Result |
|---|---|
| Rust shell compiles | ✅ |
| PyInstaller produces 20 MB onefile binary | ✅ |
| Tauri bundles `.app` + `.dmg` (31 MB / 24 MB) | ✅ |
| `codesign --verify --deep --strict` passes | ✅ |
| Shell parses READY line, builds config | ✅ |
| `window.__JARVIS_CONFIG__` injected before page load | ✅ |
| `/api/health` reachable from inside the bundled webview | ✅ |
| Clean Cmd+Q quit terminates shell + sidecar | ✅ |
| SIGKILL of shell terminates sidecar within ~1s | ✅ (shell-PID watchdog) |
| **Notarized `.app` accepted by Gatekeeper** | ✅ `source=Notarized Developer ID` |
| **Notarized `.dmg` accepted + stapled** | ✅ offline-verifiable |
| **Hardened-runtime entitlements applied** (jit, unsigned-exec-mem, dyld-env, library-validation off) | ✅ |
| **Cert chain: Developer ID Application → Developer ID CA → Apple Root CA** | ✅ |
| **Dev-mode sidecar bypass via `JARVIS_DEV_BACKEND_URL`** | ✅ (Python iteration without PyInstaller rebuild) |

The notarization gate (ADR 003 §K) **passed** on 2026-04-29 against signing identity `Developer ID Application: EXAMPLE (TEAMID)`.

## Dev mode

For iteration without rebuilding the PyInstaller binary on every Python edit:

```bash
bash desktop/scripts/dev.sh
```

What it does:
1. Starts the sidecar from the backend venv directly (`backend/.venv/bin/python desktop/sidecar/hello.py`) on port 8765.
2. Polls `/api/health` until ready.
3. Runs `npx tauri dev` with `JARVIS_DEV_BACKEND_URL=http://127.0.0.1:8765` set in the env.

The Rust shell sees the env var and *skips* the bundled-sidecar spawn path entirely — it just builds `BackendConfig { backend_url, ws_url }` directly from the URL, then injects it via `initialization_script` exactly like the production path. The webview can't tell the difference.

Iteration loop:
| Change | Reload step |
|---|---|
| `frontend-spike/index.html` | Close + reopen the window. |
| `desktop/src-tauri/src/lib.rs` | Save → `tauri dev` recompiles + restarts (~10s). |
| `desktop/sidecar/hello.py` (or, after graduation, `backend/main.py`) | Ctrl+C, re-run `dev.sh`. (uvicorn `--reload` integration comes during graduation.) |

`scripts/dev.sh` never signs, never notarizes, never bundles. For a real distributable, run `scripts/build-notarized.sh`.

## Architectural finding

PyInstaller's onefile bootloader creates a parent/child topology (bootloader → Python uvicorn) that defeats Tauri's default cleanup. Tauri's `child.kill()` sends SIGKILL to the immediate child (the bootloader); the bootloader has no chance to forward the signal to its own Python child, which then orphans to launchd.

**Mitigation in the spike** — pass `JARVIS_SHELL_PID` to the sidecar via env at spawn time; the Python process polls `os.kill(shell_pid, 0)` once per second and self-terminates when the shell dies. Robust across Cmd+Q, force-quit, debugger detach, IDE stop. This will graduate into the real backend's startup.

This is the failure mode ADR 003 §"Negative" §"Child-process zombies on macOS force-quit" predicted; the spike confirmed it empirically and validated the watchdog mitigation.

## What this does NOT cover

- Notarization (waiting on Developer ID Application cert generation).
- The real Nuxt frontend (uses static `frontend-spike/index.html`).
- The real FastAPI backend (uses minimal `hello.py`, not `backend/main.py`).
- Bundled Ollama sidecar (separate spike chunk).
- Windows x86_64 (deferred per [ADR 003 amendment §C](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md#amendment-2026-04-29-implementation-decisions)).
- Auto-updater (`tauri-plugin-updater`) — wired after notarization gate passes.
- Resource paths for vault, app-data, models.
- Background reindex.

These all land *after* the notarization gate. This spike's only job is to prove the path exists.

## Graduation plan

When the spike notarizes successfully on macOS arm64:

1. Replace `frontend-spike/` reference in `tauri.conf.json` with `../../frontend/.output/public` (real Nuxt static build).
2. Replace `hello.py` with `backend/scripts/run_frozen.py` (entrypoint to the real `backend/main.py`).
3. Extend the PyInstaller spec with the real backend's hidden imports + bundled fastembed/spaCy weights (per [ADR 003 §A](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md#a-plumbing-model-weights-ship-inside-the-installer)).
4. Add the bundled Ollama sidecar (`Ollama.app/Contents/Resources/ollama` extracted, re-signed) per [ADR 003 §B](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md#b-ollama-bundling-shape-per-os).
5. Wire `tauri-plugin-updater` against the signed manifest infrastructure.
6. Mirror the entire shape for Windows x86_64 with EV signing.

The spike code in `desktop/sidecar/hello.py` and `desktop/frontend-spike/` can be deleted at that point; the supervision logic in `desktop/src-tauri/src/lib.rs` graduates.
