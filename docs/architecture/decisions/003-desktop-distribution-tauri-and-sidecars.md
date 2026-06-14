# ADR 003 — Desktop distribution: Tauri shell + PyInstaller backend + Ollama sidecar

**Status:** Accepted
**Date:** 2026-04-27
**Revised:** 2026-04-28 — tightened spec (model-storage location, signing infra, shell-outbound exception, key custody, process supervision, attribution).
**Revised:** 2026-04-29 — implementation amendment (see §"Amendment 2026-04-29"): plumbing-model bundling, Ollama bundle shape per OS, Linux deferred to v1.1, env-driven config + port handshake, Tauri-native updater, first-run vault picker, PyInstaller hidden-imports list, background reindex.
**Related:** [ADR 002](002-pure-local-product-shape.md)

## Context

V1 ships as a self-installing Mac and Windows desktop app. The operator must not have to touch a terminal, install a system service, accept admin elevation prompts, or wait for separate components to be downloaded one by one. The current codebase is a Nuxt 3 SPA + FastAPI backend, with Ollama as the local-LLM runtime. Lifting that into a packaged desktop app forces three distribution decisions that interact:

1. **Desktop shell** — what wraps the existing Nuxt UI?
2. **Backend packaging** — how does the FastAPI + Python stack ship inside the app?
3. **Ollama distribution** — auto-installed system service, bundled sidecar, or skipped?

Each of these has alternatives and trade-offs. They have to be answered together because the wrong combination produces an app that either bloats past defensibility, leaks system-state on uninstall, or fails the "no admin elevation, no terminal" UX promise.

## Decision drivers

1. **No admin elevation, no system services, no terminal.** A first-launch flow that succeeds end-to-end on a fresh laptop with no developer tools installed.
2. **No system pollution on uninstall.** Removing the app removes everything except the user's own canonical Markdown vault and the user's own license file. This is what an IT-review questionnaire actually asks.
3. **Compliance posture must be *structurally* assertable, not just behaviourally observable.** A capability-gated runtime is auditable; a "we promise we don't make outbound calls" runtime is not.
4. **Bundle is self-contained from minute zero.** The installer ships everything structurally required to boot offline — Python runtime, FastAPI app, embedding model weights, NLP language packs, the Ollama inference runtime + its runners. Nothing deferred to a post-install fetch except the LLM model blobs (5–80 GB depending on profile + tier), which pull on first launch via signed-manifest + SHA-256 verification. This is what makes the "no outbound except pinned manifest" posture in driver #3 structurally assertable rather than behaviourally observable.
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
- macOS: `~/Library/Application Support/DeepBind/ollama-models/`
- Windows: `%LOCALAPPDATA%\DeepBind\ollama-models\`

Uninstall removes that directory.

**Coexistence with a system-installed Ollama.** Developer laptops often already run Ollama on `:11434`. The bundled sidecar binds a non-default loopback port (e.g. `:11435`) so port collision is impossible. We do not attempt to reuse the system instance — version drift, model-storage co-mingling, and lifecycle ownership all argue against it. The runtime UI surfaces a one-line note when a system Ollama is detected on `:11434`, so the user understands why GPU/VRAM may show contention.

**Attribution.** Ollama is MIT-licensed; redistribution inside a bundled application is explicitly permitted, but MIT requires attribution in distributed binary form. The Ollama LICENSE plus all transitive third-party notices ship in `Resources/LICENSES/` (macOS) / `licenses\` next to the install root (Windows), and are linked from a Help → Open-Source Notices menu item.

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
- **Skip Ollama, link `llama.cpp` directly.** Smaller bundle, more control, no second process. Real engineering depth; required eventually for first-class MLX on Apple Silicon. Out of scope for v1. **Deferred** as a future-quality milestone.
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

## Amendment 2026-04-29 — implementation decisions

This section locks the implementation-level decisions surfaced when validating ADR 003 against the actual codebase. Each item resolves an assumption in the original decision body that was either underspecified or contradicted by current code.

### A. Plumbing-model weights ship inside the installer
`fastembed` (embedding model + reranker, ONNX) and spaCy (NER) both default to first-use download from upstream caches — a structural contradiction with the "no outbound except pinned manifest URLs" driver. **Decision:** these weights are bundled inside the PyInstaller archive as data files. The signed manifest covers chat-model blobs only; plumbing-model weights are part of the installer surface and audited at signing time.

The PyInstaller spec sets `FASTEMBED_CACHE_PATH` and the spaCy data path to the bundled-resource location at runtime via `sys._MEIPASS` resolution, so the installed app never reads from `~/.cache/fastembed` or the venv's `site-packages`. Cross-machine portability is preserved.

#### Bundled ML weights — current set (per ADR 018, v1 English-only)

Updated 2026-05-05 as part of the commercial-licensing audit and ADR 018 English-only scope decision:

| Slot | Model | License | Size | How bundled |
|---|---|---|---|---|
| Embedding (`embedding_service.py`) | `snowflake/snowflake-arctic-embed-l` | Apache-2.0 | ~1 GB ONNX | fastembed registry, fetched into `backend/_bundled_models/fastembed/` by `desktop/scripts/fetch-bundled-models.sh` at build time |
| NER (`entity_extraction.py`) | `xx_ent_wiki_sm` | MIT (model) + CC-BY 3.0 (WikiNER training data) | 11 MB | spaCy wheel pinned in `backend/requirements.txt`, picked up by PyInstaller via `collect_data_files` |
| Reranker (`reranker_service.py`) | `BAAI/bge-reranker-v2-m3` via `onnx-community/bge-reranker-v2-m3-ONNX` (INT8) | Apache-2.0 | ~570 MB INT8 ONNX | Registered with fastembed at runtime via `TextCrossEncoder.add_custom_model(...)` (not in built-in registry — qdrant/fastembed#494). Bundled by `desktop/scripts/fetch-bundled-models.sh` alongside the embedder. |

**Retired in this audit:** `paraphrase-multilingual-MiniLM-L12-v2` (multilingual embedding, replaced by Arctic-Embed-L for the English-quality lift), `pl_core_news_sm` (GPL-3.0 — closed audit finding #1), `en_core_web_sm` (OntoNotes commercial-corpus lineage — closed audit finding #3), `jinaai/jina-reranker-v2-base-multilingual` (CC-BY-NC-4.0 — replaced by `BAAI/bge-reranker-v2-m3`).

### B. Ollama bundling shape per OS
- **macOS (arm64, x86_64):** Extract the `ollama` CLI binary from upstream `Ollama-darwin.zip` (`Ollama.app/Contents/Resources/ollama`) **plus its sibling runtime payload** — the binary dlopens `libggml-base*`, `libggml-cpu-*.so`, and the `mlx_metal_v3/` + `mlx_metal_v4/` runner directories at startup. ~~The CLI is self-contained — Metal kernels are linked into the binary~~ — that was true at older Ollama versions but **stopped being true by 0.22.0** (see [Amendment 2026-04-30 §L](#l-macos-ollama-bundling--full-runtime-payload-not-just-the-cli)). The Swift menu-bar wrapper (`Contents/MacOS/Ollama`) and the `Contents/Library/LaunchAgents/com.ollama.ollama.plist` LaunchAgent are *not* shipped; we own process lifecycle from the Tauri shell. The bundled binary + every dylib in the runtime payload is re-signed under our Developer ID with hardened runtime + the entitlements at [`desktop/src-tauri/macos/Entitlements.plist`](../../../desktop/src-tauri/macos/Entitlements.plist) before notarization.
- **Windows (x86_64):** Use the standalone `ollama-windows-amd64.zip` distribution, *not* `OllamaSetup.exe` (which registers a Windows service we explicitly don't want — driver #2). Ship `ollama.exe` plus the `lib/ollama/runners/` directory containing CUDA-compiled inference runners. ROCm runners are *not* bundled in v1 — AMD-GPU support on Windows is a v1.1 scope question alongside the Linux platform decision (§C). The CPU runner is bundled.
- **Linux:** deferred to v1.1 (see §C).

This is the only correct interpretation of the original ADR 003 phrase "the `ollama` binary ships inside the app bundle" once the upstream distribution shapes are accounted for.

### C. v1 platform matrix: macOS + Windows. Linux deferred to v1.1
ADR 003's signing infra and CI matrix were written for two platforms. Adding Linux is a separate design problem: distribution shape diverges (AppImage vs. flatpak vs. deb/rpm), there is no notarization analogue to anchor "structurally trusted by the OS," and the GPU-runtime matrix forks again (CPU + CUDA + ROCm + ARM-Mali). Linux v1.1 will get its own ADR amendment covering distribution shape, GPU-runtime split, and trust-anchor posture (probably "publish a signed `.deb` + `.rpm` repo, drop the AppImage").

### D. Env-driven config + stdout port handshake
The original ADR mentions "shell reads the actually-bound port back from the child" but does not pick a mechanism. **Decision:**
- Backend accepts `JARVIS_API_PORT`, `JARVIS_API_HOST`, `JARVIS_OLLAMA_BASE_URL`, `JARVIS_WORKSPACE_PATH`, `JARVIS_APP_DATA_PATH`, `JARVIS_CORS_ORIGINS` from env. The Tauri shell sets these at child-spawn time.
- Backend launches with `JARVIS_API_PORT=0` (OS-assigned ephemeral) and prints exactly one machine-readable line on stdout once the FastAPI app is ready: `JARVIS_BACKEND_READY host=127.0.0.1 port=<n>`. The Tauri shell awaits that line, then injects `window.__JARVIS_CONFIG__ = { backendUrl, wsUrl }` into the webview before serving the SPA.
- Frontend reads `window.__JARVIS_CONFIG__` first, falls back to `runtimeConfig.public.backendWsUrl` (dev-server mode). The dev proxy in `nuxt.config.ts` is unchanged for `nuxt dev`.

This removes every hardcoded port assumption ([config.py:10](../../../backend/config.py#L10), [ollama_service.py:88](../../../backend/services/ollama_service.py#L88), [nuxt.config.ts:21-23](../../../frontend/nuxt.config.ts#L21-L23)). `DEFAULT_OLLAMA_BASE_URL` becomes `os.environ.get("JARVIS_OLLAMA_BASE_URL", "http://127.0.0.1:11434")`.

### E. CORS origin list extended for the Tauri webview
[config.py:11](../../../backend/config.py#L11) currently lists only `http://localhost:3000`. The Tauri webview origin is `tauri://localhost` on macOS and `https://tauri.localhost` on Windows. The `cors_origins` setting reads from `JARVIS_CORS_ORIGINS` (comma-separated) and the shell sets the appropriate origin per OS at launch.

### F. Frozen entrypoint separate from `main.py`
[main.py:105-109](../../../backend/main.py#L105-L109) runs uvicorn with `reload=True`, which is dev-only and incompatible with PyInstaller. **Decision:** add `backend/scripts/run_frozen.py` as the PyInstaller entry script. It calls `uvicorn.run(app, host=..., port=..., reload=False)` after binding `port=0` and printing the ready line. `main.py`'s `__main__` block remains for `python main.py` dev workflows.

### G. First-run vault location: native folder picker with default
Tauri's `dialog.open` plugin shows a native folder picker on first launch, defaulting to `~/Jarvis` (mac) / `%USERPROFILE%\Jarvis` (win). The chosen path is written to app-data config and passed to the backend via `JARVIS_WORKSPACE_PATH` on every subsequent launch. Compliance-focused operators explicitly ask whether the vault path is user-controlled; this is the right shape.

### H. Auto-updater: Tauri-native (`tauri-plugin-updater`)
ADR 003 listed Sparkle (mac) + Squirrel/MSIX (win) as candidates. The Tauri-native updater plugin uses a single signed JSON feed across all platforms, signs against a build-time-compiled public key (compatible with our manifest-key-custody model from the original ADR), and supports delta updates with rollback. **Decision:** use `tauri-plugin-updater` for v1; revisit only if a deployment-specific requirement forces a platform-native channel.

### I. Background reindex on startup
[main.py:43-46](../../../backend/main.py#L43-L46) currently runs `reindex_all()` synchronously inside the FastAPI lifespan, blocking the `/api/health` response. On a 50k-note vault that is many seconds. The Tauri shell waits on `/api/health` to flip the splash screen, so this would manifest as a "slow first launch" bug.

**Decision:** reindex moves to a background `asyncio.create_task(...)` after the lifespan yields, and progress is exposed via `GET /api/memory/reindex/status` so the UI can show a one-line "indexing your vault…" toast that auto-dismisses on completion. `/api/health` returns immediately.

### J. PyInstaller hidden imports
`keyring` resolves backends at import time via entry points, which PyInstaller does not detect. The PyInstaller spec must include:
```
hiddenimports = [
    "keyring.backends.macOS",     # macOS Keychain
    "keyring.backends.Windows",   # Windows Credential Manager
    "keyring.backends.SecretService",  # Linux (for Linux v1.1)
    "keyring.backends.fail",      # safety fallback
]
```
Plus the standard FastAPI/Pydantic/uvicorn/anyio set. A first run of `pyinstaller --collect-all keyring` on each platform validates completeness before spec freeze.

### K. v1 hello-world notarization spike (sequencing reaffirmation)
Per the original ADR's "v1 schedule risk" callout: the first notarized macOS arm64 + Windows x64 build targets a *minimal* FastAPI sidecar inside a Tauri shell — not the real backend. This is the gate. Real backend work (paths, config, reindex, ollama lifecycle) lands only after the spike notarizes successfully on both platforms.

**Spike status (2026-04-29) — PASSED.** macOS arm64 notarization gate cleared. Both `.app` and `.dmg` accepted by Gatekeeper as `source=Notarized Developer ID`, signed under `Developer ID Application: EXAMPLE (TEAMID)` with full chain to Apple Root CA, all four hardened-runtime entitlements applied (allow-jit, allow-unsigned-executable-memory, allow-dyld-environment-variables, disable-library-validation), and notarization tickets stapled to both the `.app` and `.dmg` (offline-verifiable). The spike's architectural mechanics — Tauri shell ↔ PyInstaller sidecar spawn, stdout READY-line port handshake, `window.__JARVIS_CONFIG__` injection via `initialization_script`, clean-quit + SIGKILL cleanup via shell-PID watchdog, dev-mode bypass via `JARVIS_DEV_BACKEND_URL` — all validated end-to-end. The spike empirically confirmed the §"Negative" §zombies failure mode (PyInstaller onefile bootloader-fork + Tauri SIGKILL leaves Python child orphaned) and validated the watchdog mitigation. See [docs/features/desktop-shell-spike.md](../../features/desktop-shell-spike.md) for the full validation matrix and graduation plan. Per [§C](#c-v1-platform-matrix-macos--windows-linux-deferred-to-v11), Windows x86_64 spike now unblocked but deferred until the macOS graduation chunks land (real backend, real frontend, bundled Ollama).

## Amendment 2026-05-01 — Bundled Ollama pinned at 0.18.0 (Apple M5 Metal regression)

`OLLAMA_VERSION` in `fetch-ollama.sh` was downgraded from 0.22.0 to **0.18.0** after the cold-launch smoke on Apple M5 hit a hard runner crash. The bundled GGML metal-kernel library in 0.21.2+ instantiates `MPPTensorOpsMatMul2dImpl` template specializations that fail Apple's Metal 4 framework strict bfloat/half cooperative-tensor type-matching at MTLLibrary compile time; the runner subprocess `SIGABRT`s in `ggml_metal_library_init`, Ollama returns `500 model failed to load`. Reproduced 2026-05-01 with both 0.21.2 (homebrew daemon) and 0.22.0 (the previously bundled version) against `qwen3:8b` on this M5 box. Ollama 0.18.0 hits the same Metal source-compile error but **falls back to an embedded prebuilt metal library** (`using embedded metal library`, `loaded in 0.005 sec`); that fallback path was removed in later versions.

The structural findings of the 2026-04-30 amendment below (runtime payload = `ollama` binary + libggml + mlx_metal_v3/v4 dylibs, bundled via `bundle.resources` rather than `externalBin`) still hold for 0.18.0 — the upstream zip layout is unchanged. Only the version + SHA-256 pin and the M5-specific reasoning have moved.

Re-evaluate the pin when upstream Ollama bumps GGML to a release whose llama.cpp metal kernels match Apple Metal 4's strict typing. Smoke test: extract the new zip, run `OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS=<dir> ./ollama serve`, then `curl -X POST /api/generate` against `qwen3:8b`. If `500 model failed to load` returns, the new version still has the bug — don't bump.

---

## Amendment 2026-04-30 — Ollama 0.22.0 macOS payload + bundling primitive (G4a)

This amendment captures two implementation findings surfaced during the [G4a graduation chunk](../../features/desktop-shell-graduation.md#g4--bundled-ollama-sidecar-) (extract + re-sign + spawn the bundled Ollama sidecar). Both override assumptions in the original §B.

### L. macOS Ollama bundling — full runtime payload, not just the CLI
At Ollama 0.22.0 the macOS distribution is no longer a self-contained CLI binary. Inspecting `Ollama-darwin.zip` (164 MB, SHA-256 `a410e2f7…b278`):

| Path inside `Ollama.app/Contents/Resources/` | Purpose | Required at runtime |
|---|---|---|
| `ollama` (75 MB, universal) | CLI binary | ✅ |
| `libggml-base.0.0.0.dylib` (+ `libggml-base.0.dylib`, `libggml-base.dylib` symlinks) | shared base for ggml backends | ✅ (loaded by libggml-cpu) |
| `libggml-cpu-{alderlake,haswell,icelake,sandybridge,skylakex,sse42,x64}.so`, `libggml-cpu.so` | x86_64 CPU-feature kernels | ⚠️ unused on arm64 but kept for layout fidelity |
| `mlx_metal_v3/{libmlx,libmlxc}.dylib + mlx.metallib` (~152 MB) | MLX Metal runner — older macOS | ✅ |
| `mlx_metal_v4/{libmlx,libmlxc}.dylib + mlx.metallib` (~152 MB) | MLX Metal runner — current macOS | ✅ |
| `Contents/MacOS/Ollama` (49 MB Swift) | menu-bar wrapper | ❌ skipped per §B |
| `Contents/Library/LaunchAgents/com.ollama.ollama.plist` | LaunchAgent | ❌ skipped per driver #2 |
| `Contents/Frameworks/Squirrel.framework` | upstream auto-updater | ❌ skipped — we use tauri-plugin-updater for our own bundle |
| `Resources/icon.icns`, `*.png` | menu-bar app artifacts | ❌ unused |

`otool -L ollama` shows only system framework dependencies (Metal, Foundation, Accelerate, libSystem); the libggml/libmlx payload is dlopened at runtime based on detected hardware + macOS version. **All Mach-O files in the payload must be re-signed** under our Developer ID with hardened runtime to satisfy notarization — a single unsigned dylib inside the bundled `.app` blocks Apple's notary verdict.

The pin lives in [`desktop/scripts/fetch-ollama.sh`](../../../desktop/scripts/fetch-ollama.sh) as `OLLAMA_VERSION` + `OLLAMA_DARWIN_ZIP_SHA256`; bumps require a deliberate update to both — the mitigation for CUDA/Metal backend divergence is pinning Ollama versions per-OS in the Jarvis installer rather than tracking upstream.

### M. Bundling primitive: `bundle.resources`, not `externalBin`
Tauri's `externalBin` config is single-binary-per-target: it accepts one path per platform triple, applies a triple suffix, and invokes via `app.shell().sidecar(name)`. It cannot ship a binary + dylibs + nested directories.

**Decision:** for the Ollama runtime payload, use Tauri's [`bundle.resources`](https://v2.tauri.app/reference/config/#bundleconfig) primitive. The runtime directory at `desktop/src-tauri/binaries/ollama-runtime/` is shipped intact into `<bundle>/Contents/Resources/ollama-runtime/` via the `{ "binaries/ollama-runtime": "ollama-runtime" }` mapping in [`tauri.conf.json`](../../../desktop/src-tauri/tauri.conf.json). At runtime the Rust shell resolves the path via `app.path().resource_dir()?.join("ollama-runtime/ollama")` and spawns the binary with `std::process::Command` — `current_dir` set to the runtime dir so the binary's `@loader_path` rpath finds sibling dylibs without `DYLD_LIBRARY_PATH` gymnastics. See [`desktop/src-tauri/src/lib.rs`](../../../desktop/src-tauri/src/lib.rs) `spawn_ollama()` for the exact wiring.

`externalBin` is retained for the PyInstaller sidecar, which **is** a single binary and benefits from Tauri's `tauri-plugin-shell` lifecycle handle (`CommandChild`). Two-process supervision in `lib.rs` then juggles two children: a `CommandChild` (jarvis-sidecar) and a `std::process::Child` (ollama). Both are killed on `RunEvent::ExitRequested` / `RunEvent::Exit`; ollama is also `wait()`-reaped to avoid zombies.

---



1. **Signing infrastructure stand-up.** The cert decisions are made (driver #5); what's open is the operational stand-up: managed-signing-service vendor selection (DigiCert KeyLocker vs SSL.com eSigner) for Windows EV, and self-hosted macOS notarization runner with stored Apple Developer ID credentials. Must complete before the first notarized build target lands.
2. **Auto-update mechanism choice.** Sparkle (Mac) is the default; Squirrel vs MSIX-via-winget for Windows is a judgment call once we have the first Windows build. Signed delta updates with rollback are required (a bad update on a compliance workstation is an outage). Updating the bundled Ollama binary travels with the rest of the bundle.
3. **GPU/Metal/CUDA detection surface.** Ollama handles this internally; the runtime UI should surface accelerator presence so the user understands their hardware floor, and the system-Ollama coexistence note (see Ollama distribution decision) lives here too.
4. **First-launch model-fetch resumability.** Half-pulled blobs on a flaky connection should resume rather than restart. Tracked as a UX requirement on the model-fetch surface.
5. **Manifest-signing key custody hardening.** Initial cadence + KMS choice are recorded in the decision body; a future ADR covers dual-signature / threshold custody if a regulated customer requires it.
6. **Future: `llama.cpp` direct integration** to remove the second sidecar and unlock first-class MLX. Out of scope for v1; revisit when v2 platform work is scheduled.
