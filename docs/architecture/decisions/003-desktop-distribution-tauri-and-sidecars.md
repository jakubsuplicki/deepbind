# ADR 003 — Desktop distribution: Tauri shell + PyInstaller backend + Ollama sidecar

**Status:** Accepted
**Date:** 2026-04-27
**Revised:** 2026-04-28 — tightened spec (model-storage location, installer-size ceiling, signing infra, shell-outbound exception, key custody, process supervision, attribution).
**Related:** [ADR 002](002-pure-local-product-shape.md) · [`docs/SELF-CONTAINED-APP-REVIEW.md`](../../SELF-CONTAINED-APP-REVIEW.md) §4–§5

## Context

V1 ships as a self-installing Mac and Windows desktop app. The buyer must not have to touch a terminal, install a system service, accept admin elevation prompts, or wait for separate components to be downloaded one by one. The current codebase is a Nuxt 3 SPA + FastAPI backend, with Ollama as the local-LLM runtime. Lifting that into a packaged desktop app forces three distribution decisions that interact:

1. **Desktop shell** — what wraps the existing Nuxt UI?
2. **Backend packaging** — how does the FastAPI + Python stack ship inside the app?
3. **Ollama distribution** — auto-installed system service, bundled sidecar, or skipped?

Each of these has alternatives and trade-offs. They have to be answered together because the wrong combination produces an app that either bloats past defensibility, leaks system-state on uninstall, or fails the "no admin elevation, no terminal" UX promise.

## Decision drivers

1. **No admin elevation, no system services, no terminal.** A first-launch flow that succeeds end-to-end on a fresh laptop with no developer tools installed.
2. **No system pollution on uninstall.** Removing the app removes everything except the user's own canonical Markdown vault and the user's own license file. This is what an IT-review questionnaire actually asks.
3. **Compliance posture must be *structurally* assertable, not just behaviourally observable.** A capability-gated runtime is auditable; a "we promise we don't make outbound calls" runtime is not.
4. **Bundle size must remain defensible.** Models will pull on first launch (5–80 GB depending on profile + tier), so the *installer* itself must stay small enough to not telegraph the model footprint at install time. **Hard ceiling: 600 MB compressed installer per platform**, of which the realistic split is ~150 MB Ollama binary + ~400 MB PyInstaller bundle (FastAPI + sentence-transformers + NLP libs + embedding model deps) + ~30 MB Tauri shell + headroom. Anything that would push past 600 MB requires an ADR amendment, not a quiet bundle bump.
5. **Code signing + notarization is architecture, not procurement.** Apple Developer ID is a $99/yr account; Authenticode EV (required for SmartScreen reputation on a fresh installer) ships on a hardware token (YubiKey FIPS or HSM) and **cannot be stored as a CI secret**. This forces either a self-hosted signing runner with the token attached or a managed signing service (DigiCert KeyLocker, SSL.com eSigner). Decision: **managed signing service** for Windows EV, self-hosted Apple notarization on the macOS CI builder. SmartScreen reputation matters.
6. **The FastAPI backend is load-bearing.** ~15 services, aiosqlite, embedding model, NLP libraries, FTS5 helpers. Rewriting in Rust or Node would discard a working backend for no compliance-relevant gain.

## Decision

### Desktop shell — **Tauri 2**

Tauri 2 wraps the existing Nuxt 3 build, runs the backend as a managed sidecar, and signs/notarizes as a single bundle. Capability allowlist is configured to permit only:
- Local HTTP to the backend sidecar (loopback only).
- Local filesystem within the user's vault path and app-data path.
- OS notification + clipboard surfaces required by the existing UI.
- **Outbound HTTP from the shell is restricted to a single pinned, signed update-feed URL** (the auto-updater, see follow-up #2). Every other outbound call is the backend sidecar's surface, gated by `services/privacy.py`. The shell-level capability config encodes this exception by URL pattern, not by a blanket "http enabled" flag, so the no-outbound posture remains structurally assertable.

### Backend packaging — **PyInstaller sidecar**

The FastAPI + Python stack packages with PyInstaller into a single platform binary per OS (`darwin-arm64`, `darwin-x86_64`, `win-x86_64`). Per-platform CI builders. The Tauri shell spawns this binary as a managed child process at launch and shuts it down on quit. Process supervision is the shell's job, not the user's.

**v1 schedule risk (named explicitly, not buried).** PyInstaller + macOS hardened runtime is the single most likely cause of v1 slip. Embedded Python's `dlopen` requires `disable-library-validation`; PyTorch / Apple Silicon BLAS often need `allow-jit`; the wrong entitlement combination silently fails Apple notarization with cryptic errors. This is multi-day-debugging-per-platform territory. **Mitigation: stand up notarized macOS arm64 + win-x86_64 builds against a thin "hello-world FastAPI" sidecar before any v1 backend work depends on the bundle existing.** Treat the first successful notarization as a v1 prerequisite, not a v1 task.

### Ollama distribution — **Bundled sidecar binary**

The `ollama` binary ships inside the app bundle (Mac `.app/Contents/Resources/`, Windows install dir under `bin/`). The Tauri shell spawns it as a child process on a non-default loopback port at app launch and terminates it on quit. **The system never sees a LaunchDaemon, a Windows service, or a system-installed `/usr/local/bin/ollama`.** Uninstall removes everything except the user's vault and license file.

**Model storage is overridden, not defaulted.** Ollama's default `~/.ollama/models` would (a) co-mingle blobs with any system-installed Ollama on a developer's machine, and (b) survive uninstall — both violate driver #2. The shell launches the bundled binary with `OLLAMA_MODELS` set to an app-scoped path:
- macOS: `~/Library/Application Support/DeepFilesAI/ollama-models/`
- Windows: `%LOCALAPPDATA%\DeepFilesAI\ollama-models\`

Uninstall removes that directory.

**Coexistence with a system-installed Ollama.** Developer laptops often already run Ollama on `:11434`. The bundled sidecar binds a non-default loopback port (e.g. `:11435`) so port collision is impossible. We do not attempt to reuse the system instance — version drift, model-storage co-mingling, and lifecycle ownership all argue against it. The runtime UI surfaces a one-line note when a system Ollama is detected on `:11434`, so the user understands why GPU/VRAM may show contention.

**Attribution.** Ollama is MIT-licensed; redistribution inside a commercial product is explicitly permitted, but MIT requires attribution in distributed binary form. The Ollama LICENSE plus all transitive third-party notices ship in `Resources/LICENSES/` (macOS) / `licenses\` next to the install root (Windows), and are linked from a Help → Open-Source Notices menu item.

### First-launch model fetch (the one outbound call we accept at install time)

Hardware tier + onboarding selections determine which model blobs to fetch. The install manifest specifies blobs by **pinned URL with SHA-256 verification**, signed by us (Ed25519 over the manifest contents). On first launch the app:

1. Verifies the manifest signature locally.
2. Pulls each blob from the pinned URL.
3. Verifies SHA-256 of each blob before activating it.
4. Surfaces the fetch as an explicit "downloading models for [tier]" UX step the user authorizes once.

Where Ollama provides the pull mechanism (`/api/pull`), we use it but verify the result against our manifest's SHA, not Ollama's. Ollama tags drift; SHAs do not.

**Manifest signing key custody.** The Ed25519 manifest-signing key lives in a cloud KMS (AWS KMS or GCP Cloud KMS — chosen at infra setup), never on a developer laptop and never in CI plaintext. Manifest signing runs through a CI step that calls `Sign` on the KMS-held key; the public verification key is compiled into the shell at build time. Rotation cadence: annual, plus immediate on any suspected exposure. Revocation is a manifest-version bump that ships in the next signed update — superseding (not in-band CRL) since the app already trusts a fixed pubkey at build time. A future ADR will cover dual-signature / threshold custody if a customer requires it.

### Optional pre-bundled USB / DVD SKU for air-gap buys

For true air-gap customers (ITAR seats, classified programs), an offline installer SKU bundles the installer + the pre-pulled model blobs for one profile + tier. Distribution is out-of-band (controlled media). Not the v1 default; tractable when a customer demands it.

## Alternatives considered

### Desktop shell

- **Electron.** Familiar, larger ecosystem, more battle-tested signing flows. Bundle adds ~150 MB to the installer and historically has broader CVE exposure. Tauri's smaller surface and capability-allowlist are a better fit for a compliance product. **Rejected.**
- **Native (Swift + WinUI).** Smallest, best UX. Doubles the platform-code volume. Not viable without a platform team. **Rejected for v1.**
- **Wails (Go + WebView).** Comparable shape to Tauri but smaller ecosystem and fewer first-class sidecar examples. **Rejected.**

### Backend packaging

- **Port FastAPI services to Node/Bun (Nuxt Nitro).** Eliminates the second-process. Discards a working backend for no compliance gain. **Rejected.**
- **Port to Rust (Tauri-native).** Smallest possible bundle. Throws away every Python-ecosystem dependency we rely on (NLP, embedding, classification, FTS5 tooling). **Rejected.**
- **Run the backend as a separate user-installed service.** Reintroduces the "system service" UX failure mode this ADR is trying to avoid. **Rejected.**

### Ollama distribution

- **Auto-install Ollama as a system service via the upstream installer.** What the user originally meant by "Ollama should just install." Requires admin elevation; leaves a system service surviving uninstall; we don't control Ollama's update cadence; first IT review fails. **Rejected.**
- **Skip Ollama, link `llama.cpp` directly.** Smaller bundle, more control, no second process. Real engineering depth; required eventually for first-class MLX on Apple Silicon. Out of scope for v1. **Deferred** — recorded in [`SELF-CONTAINED-APP-REVIEW.md`](../../SELF-CONTAINED-APP-REVIEW.md) §4 as a future-quality milestone.
- **Ship without Ollama; require user installs it themselves.** What the current codebase assumes. Fails the "no terminal" promise. **Rejected.**

## Consequences

### Positive
- Single bundle, single code-signing flow, single uninstall surface.
- "What system services does the installer create?" — none. Audit answer.
- Capability-gated shell makes the no-outbound posture structurally assertable.
- Version-pinned Ollama means we test against a specific build and own the upgrade story.
- The `ollama` binary is small (~50 MB); the *models* are the size, and they're not in the installer.

### Negative
- Two-process app at runtime (shell + backend sidecar; technically three including Ollama). Process supervision becomes the shell's responsibility, with these named failure modes:
  - **Backend crash mid-session** — risk of in-flight chat / write loss. Mitigation: every user-affecting write commits to Markdown-on-disk before the API returns (per source-of-truth doctrine), so a backend crash loses at most the in-flight turn, not committed state. Shell auto-restarts the sidecar with exponential backoff; UI surfaces a one-line "backend restarted" toast.
  - **Child-process zombies on macOS force-quit** — Tauri's sidecar API binds child lifetime to shell lifetime, but a force-quit (`kill -9` of the shell) can orphan children. Mitigation: backend + Ollama sidecars register a `prctl`/`setpgid`-style group on launch and the shell installs a launch-time sweep that kills any prior orphans matching our app's marker before spawning new ones.
  - **Port-binding races on rapid restart** — TIME_WAIT can block re-bind for ~60s. Mitigation: bind with `SO_REUSEADDR` (and on Windows accept the corresponding flag); shell reads the actually-bound port back from the child rather than assuming.
- PyInstaller signing on macOS — see "v1 schedule risk" callout under the backend-packaging decision. Not deferrable.
- Bundle does not include models — first-launch UX has a download step that takes meaningful time on slow connections. Mitigated by the profile-aware fetch (only the chosen profile's stack is pulled).
- Updating the bundled Ollama version is our responsibility; an upstream regression becomes our outage. Acceptable trade for the compliance posture; mitigated by version pinning + staged rollout via Sparkle (Mac) / Squirrel or MSIX (Windows).

### What this changes about existing code
- New top-level Tauri configuration + `tauri.conf.json`.
- New PyInstaller spec + per-OS CI builders.
- [`backend/services/ollama_service.py`](../../../backend/services/ollama_service.py) `_normalize_and_validate_ollama_base_url()` already restricts to loopback — compatible with the bundled-sidecar port; no change needed there.
- The frontend's existing `fetchRuntime()` probe in [`useLocalModels.ts`](../../../frontend/app/composables/useLocalModels.ts) (which calls `/api/local/runtime` and reads the `installed` / `running` flags) becomes "is our bundled Ollama child process running?" — same shape, different lifetime.
- [`OnboardingLocalFlow.vue`](../../../frontend/app/components/OnboardingLocalFlow.vue) needs the "install Ollama yourself" step removed — Ollama is always present in the shipped app.

## Open follow-ups (non-blocking)

1. **Signing infrastructure stand-up.** The cert decisions are made (driver #5); what's open is the operational stand-up: managed-signing-service vendor selection (DigiCert KeyLocker vs SSL.com eSigner) for Windows EV, and self-hosted macOS notarization runner with stored Apple Developer ID credentials. Must complete before the first notarized build target lands.
2. **Auto-update mechanism choice.** Sparkle (Mac) is the default; Squirrel vs MSIX-via-winget for Windows is a judgment call once we have the first Windows build. Signed delta updates with rollback are required (a bad update on a compliance workstation is an outage). Updating the bundled Ollama binary travels with the rest of the bundle.
3. **GPU/Metal/CUDA detection surface.** Ollama handles this internally; the runtime UI should surface accelerator presence so the user understands their hardware floor, and the system-Ollama coexistence note (see Ollama distribution decision) lives here too.
4. **First-launch model-fetch resumability.** Half-pulled blobs on a flaky connection should resume rather than restart. Tracked as a UX requirement on the model-fetch surface.
5. **Manifest-signing key custody hardening.** Initial cadence + KMS choice are recorded in the decision body; a future ADR covers dual-signature / threshold custody if a regulated customer requires it.
6. **Future: `llama.cpp` direct integration** to remove the second sidecar and unlock first-class MLX. Out of scope for v1; revisit when v2 platform work is scheduled.
