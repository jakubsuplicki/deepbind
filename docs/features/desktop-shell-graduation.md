---
title: Desktop Shell Graduation (ADR 003 spike → v1)
type: tracker
status: in-progress
last_updated: 2026-05-01
related_adrs:
  - 003-desktop-distribution-tauri-and-sidecars
  - 005-hardware-tiered-model-stack-and-first-run-policy
  - 015-single-target-local-only-stack
related_features:
  - desktop-shell-spike
---

> **2026-05-01 status pulse:** ADR 015 landed — the build is now a single local-only target with no `JARVIS_DESKTOP_BUNDLE` flag, no cloud-provider SDKs anywhere in the repo, no LiteLLM, and no duel feature. ADR 014 (the previous dual-target shape) is superseded; its Info.plist `JarvisBundleCapabilities` audit signal is preserved as a static array. G4b first-run pull UX is functionally complete in code (orchestrator + wizard + ladder + memory-pressure auto-downgrade); G4b6 (cold-launch verification on a notarized bundle) is the remaining dev-loop step.

## Purpose

Tracks the work to replace the [desktop-shell-spike](desktop-shell-spike.md) toy code (`hello.py`, `frontend-spike/index.html`) with the real product surface — real backend, real Nuxt frontend, bundled Ollama, background-safe cold start — without re-opening any of the architecture choices already locked in [ADR 003](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md) and its 2026-04-29 amendment.

The spike proved the architecture compiles, signs, notarizes, and supervises children correctly on macOS arm64. **Graduation = swap the contents inside that proven shell.**

## Status

| Chunk | What lands | State | Last touched |
|---|---|---|---|
| **G1** | Env-driven backend config + frozen entrypoint | ✅ done | 2026-04-29 |
| **G3** | Real Nuxt frontend reading `window.__JARVIS_CONFIG__` | ✅ done | 2026-04-29 |
| **G5** | Background reindex + `/api/memory/reindex/status` + cold-start pill | ✅ done | 2026-04-29 |
| **G2a** | PyInstaller bundle compiles + boots; READY-line + 5 endpoints validated | ✅ done | 2026-04-29 |
| **G2b** | Bundle fastembed ONNX + spaCy weights inside installer (ADR 003 §A) | ✅ done | 2026-04-29 |
| **G2c** | Notarize the new ~352 MB bundle (re-run build-notarized.sh) | ✅ done | 2026-04-30 |
| **G4a** | Bundled Ollama process plumbing — extract + re-sign + spawn + supervise | ✅ done (local verify) | 2026-04-30 |
| **G4b1–b5** | First-run orchestrator + wizard + ladder + memory-pressure swap + lightweight mode | ✅ done | 2026-04-30 |
| **G4b6** | Cold-launch verification on the notarized bundle (fresh `<app_data>`) | ☐ pending (gated on chunk 7 of ADR 015) | — |

Sequencing rationale (rank by dependencies + ADR completeness, not user perception):
1. **G3 first** — pure frontend, validated through dev mode against the live `backend/main.py` without any PyInstaller rebuild. Closes the "frontend half" of ADR 003 §D's runtime-config injection contract.
2. **G5 next** — backend feature, validated via tests + dev mode. Must land *before* G2 packages anything, otherwise G2 ships a backend whose first launch blocks the UI for ~minutes on a cold vault.
3. **G2 then** — packages the real backend. Highest schedule risk per ADR 003 §"v1 schedule risk" because of fastembed ONNX + spaCy weights and the hidden-import surface.
4. **G4 last** — adds the Ollama process. Independent of G2's bundle internals; depends on the spawn/supervision plumbing G2 will already have wired through `lib.rs`.

---

## G1 — Env-driven backend config + frozen entrypoint ✅

Landed 2026-04-29. Captured here so the dependency story is complete; full diff is in git.

| Concern | Resolution |
|---|---|
| Backend reads its host/port/CORS/Ollama URL from process env | [`backend/config.py`](../../backend/config.py) — `app_data_path` field added; `cors_origins: Annotated[list[str], NoDecode]` accepts comma-separated values via `_split_cors_csv` before-validator. |
| Ollama base URL no longer hardcoded | [`backend/services/ollama_service.py`](../../backend/services/ollama_service.py) — `DEFAULT_OLLAMA_BASE_URL` now reads `JARVIS_OLLAMA_BASE_URL` at module load. |
| Frozen entrypoint per ADR 003 §F | [`backend/scripts/run_frozen.py`](../../backend/scripts/run_frozen.py) — pre-binds socket with SO_REUSEADDR, prints exactly `JARVIS_BACKEND_READY host=… port=…\n`, installs shell-PID watchdog when `JARVIS_SHELL_PID` is set. |

Validated by booting `JARVIS_API_PORT=8766 backend/.venv/bin/python backend/scripts/run_frozen.py` — `/api/health` responds with version 0.15.0; full pytest suite (109 tests) passes.

---

## G3 — Frontend graduation ✅

Landed 2026-04-29.

**Goal:** the Tauri shell loads the real Nuxt UI from `frontend/.output/public/`, and the frontend reads its backend URL from `window.__JARVIS_CONFIG__` injected by `WebviewWindowBuilder::initialization_script` (ADR 003 §D).

### Concrete moves

1. **Frontend — read `window.__JARVIS_CONFIG__` at runtime.**
   - [`frontend/app/composables/useWebSocket.ts:15-30`](../../frontend/app/composables/useWebSocket.ts#L15-L30) currently falls back to `useRuntimeConfig().public.backendWsUrl`. Add a higher-priority branch that reads `window.__JARVIS_CONFIG__.wsUrl` when present (Tauri shell injects it; browser dev mode does not).
   - Same treatment for HTTP base URL — introduce a tiny composable `useBackendBase()` (returns `window.__JARVIS_CONFIG__.backendUrl ?? ''` so existing relative `/api/...` calls keep working in browser dev where Nitro proxies them). All current `$fetch('/api/...')` callsites become `$fetch(`${useBackendBase()}/api/...`)`.
   - Add a TS declaration so `window.__JARVIS_CONFIG__` typechecks.

2. **Nuxt config — produce a static SPA bundle.**
   - [`frontend/nuxt.config.ts`](../../frontend/nuxt.config.ts) already has `ssr: false`. Add `nitro.preset: 'static'` so `nuxt generate` emits a self-contained `.output/public/` (no Node server required).
   - The `nitro.devProxy` and `routeRules` proxying to `127.0.0.1:8000` are dev-only; they don't affect the static bundle but should be guarded by `process.env.NODE_ENV !== 'production'` for clarity.

3. **Tauri config — point the bundle at the real frontend.**
   - [`desktop/src-tauri/tauri.conf.json`](../../desktop/src-tauri/tauri.conf.json) `frontendDist` currently references `../frontend-spike`. Change to `../../frontend/.output/public` (path relative to `src-tauri/`).

4. **Build orchestration — generate frontend before bundling.**
   - [`desktop/scripts/build-notarized.sh`](../../desktop/scripts/build-notarized.sh) gains a step before sidecar build: `cd frontend && npm install --no-audit --no-fund && npm run generate`.
   - Add `npm run generate` script to [`frontend/package.json`](../../frontend/package.json) wired to `nuxt generate` (verify it isn't already there).

5. **Dev mode unchanged.**
   - `bash desktop/scripts/dev.sh` still works: it runs `npx tauri dev`, Tauri boots Nuxt's own dev server (`npm run dev` on `:3000`), the shell injects `window.__JARVIS_CONFIG__` against the dev sidecar on `:8765`. Verify the Vite HMR survives the injection script.

### Done when
- [x] `useWebSocket.ts` reads `window.__JARVIS_CONFIG__.wsUrl` first ([useWebSocket.ts:15-31](../../frontend/app/composables/useWebSocket.ts#L15-L31))
- [x] All `/api/...` HTTP calls flow through `apiUrl()` ([app/utils/apiUrl.ts](../../frontend/app/utils/apiUrl.ts) + 11 callsites patched)
- [x] `frontend/nuxt.config.ts` produces a working `.output/public/` via `npm run generate` (`nitro.preset = 'static'`)
- [x] `desktop/src-tauri/tauri.conf.json` `frontendDist` points at `../../frontend/.output/public`; `devUrl` points at Nuxt dev server
- [x] `desktop/scripts/build-notarized.sh` runs `nuxt generate` before `tauri build` (step 1/4)
- [x] `desktop/scripts/dev.sh` boots backend (`run_frozen.py`) + Nuxt dev (`:3000`) + `tauri dev` against `JARVIS_DEV_BACKEND_URL`
- [ ] **Verified empirically**: production-built bundle launches the real UI against the bundled backend (blocked on G2 — no real-backend bundle yet)
- [ ] `frontend-spike/` removed from repo (deferred to end of graduation, after G2-G5 all green)

### Risks
- **Vite/Nuxt dev HMR + `initialization_script` collision** — the shell injects a script tag before page load; Vite's HMR client also injects. If they fight, dev mode breaks but production is unaffected. Mitigation: keep injection minimal (just one global) and verify HMR works on first dev boot.
- **Static bundle assumes no SSR features** — already enforced by `ssr: false`. Audit for any code that touches `useNuxtApp().ssrContext` or server-only nitro routes; none expected.

---

## G5 — Background reindex + cold-start UX ✅

Landed 2026-04-29.

**Goal:** First launch of the bundled app does not block the UI on a multi-minute fastembed reindex. Per [ADR 003 §I](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md#i-cold-start-ux-on-large-vaults).

### Concrete moves

1. **Backend — make reindex a background task with progress.**
   - [`backend/services/memory_service.py:324`](../../backend/services/memory_service.py#L324) `reindex_all` and [`backend/services/embedding_service.py:184`](../../backend/services/embedding_service.py#L184) `reindex_all` keep their current sync-coroutine signatures (used by tests).
   - New module `backend/services/reindex_supervisor.py`: holds an in-process state machine `ReindexJob { state: 'idle'|'running'|'failed', started_at, scanned, total, last_error, progress_pct }`. Exposes `start_async()` / `current_status()` / `is_running()`. State lives in module globals (single-process, single-user — fine for desktop).
   - On FastAPI startup ([`backend/main.py`](../../backend/main.py)), check vault state via a cheap heuristic: SQLite `notes` table count vs. Markdown count in vault. If the gap is non-trivial *or* this is the first launch (no `app_data_path/.first_run_complete` marker), kick off `reindex_supervisor.start_async()` in a background task.

2. **Backend — status endpoint.**
   - [`backend/routers/memory.py:80`](../../backend/routers/memory.py#L80) `POST /api/memory/reindex` becomes non-blocking: it calls `reindex_supervisor.start_async()` and returns `{ "status": "started", "job_id": ... }` immediately (preserves the 200 contract for existing callers — they just get a different body shape; document the break).
   - New `GET /api/memory/reindex/status` → `{ state, progress_pct, scanned, total, started_at, last_error }`.
   - Tests: cover the supervisor's idempotent `start_async()` (second call while running is a no-op), the status endpoint's shape, and the startup auto-kick path.

3. **Frontend — cold-start toast.**
   - New composable `useReindexStatus()` polls `/api/memory/reindex/status` every 2s while state is `running`, stops on `idle`/`failed`.
   - Plug into the chat layout: a non-blocking toast/banner "Indexing your vault — N of M notes…" with a percent. Dismiss when state goes `idle`. On `failed`, show the error and a "Retry" button that POSTs `/api/memory/reindex`.
   - Match the chat UI's existing toast/banner pattern (do not introduce a new component library).

### Done when
- [x] `backend/services/reindex_supervisor.py` exists with state machine ([reindex_supervisor.py](../../backend/services/reindex_supervisor.py))
- [x] FastAPI startup auto-kicks reindex via `start_async()` ([main.py:54-55](../../backend/main.py#L54-L55))
- [x] Markdown→SQLite reindex stays sync; embedding pass moves to background
- [x] `GET /api/memory/reindex/status` returns the shape declared in `ReindexStatusResponse`
- [x] `POST /api/memory/reindex-embeddings` returns `{state: "started"|"already_running"}`
- [x] Frontend pill ([StatusBar.vue](../../frontend/app/components/StatusBar.vue)) shows during indexing with live N/M, dismisses on idle, retries on failure
- [x] Tests: 7 supervisor unit tests + 4 router-level HTTP tests ([test_reindex_supervisor.py](../../backend/tests/test_reindex_supervisor.py), [test_memory_reindex_status.py](../../backend/tests/test_memory_reindex_status.py))
- [x] Existing 1245-test suite still green
- [ ] Concept doc `docs/concepts/index-rebuild.md` describing the lifecycle (deferred — small, write when adjacent feature lands)

### Risks
- **First-run heuristic false-negatives** — if the SQLite index is up-to-date but stale embeddings exist, we won't kick reindex. Acceptable for desktop v1; semantic-search degrades gracefully when fastembed-derived rows are missing. Don't try to detect embedding staleness — that's a real bug magnet.
- **Concurrent reindex requests** — supervisor must guarantee single-flight. The state machine handles this; just make sure `start_async()` is the only entry point.

---

## G2 — Real-backend PyInstaller bundle (split into G2a / G2b / G2c)

### G2a ✅ — Bundle compiles, boots, serves real endpoints

Landed 2026-04-29. The PyInstaller spec at [`desktop/sidecar/jarvis-sidecar.spec`](../../desktop/sidecar/jarvis-sidecar.spec) targets `backend/scripts/run_frozen.py`, walks the whole `backend/` package via `collect_submodules`, and adds explicit hidden imports for fastembed/onnxruntime/spaCy/keyring per ADR 003 §A + §J.

[`desktop/scripts/build-sidecar.sh`](../../desktop/scripts/build-sidecar.sh) was rewired to use the new spec and now does a real smoke test: boots the binary on an ephemeral port, parses the READY line, hits `/api/health`, and fails the build if either step doesn't pass within 60 s. Output binary: ~109 MB, `desktop/src-tauri/binaries/jarvis-sidecar-aarch64-apple-darwin`.

**Verified empirically against the bundled binary** (curl from outside the bundle):
- `GET /api/health` → `{"status":"ok","version":"0.15.0"}`
- `GET /api/memory/reindex/status` → idle state from G5's supervisor
- `GET /api/local/hardware` → real arm64/24GB system probe
- `GET /api/local/runtime` → real Ollama detection (existing host-installed Ollama, not yet bundled — that's G4)
- `GET /api/settings` (no error)

**Findings during G2a build:**

| Finding | Resolution |
|---|---|
| Production code (`services/chat_model_probe.py:52`) imports from `tests.eval.latency`. | Added `tests.eval.latency` to `collect_submodules`; can't add `tests` to `excludes`. The underlying tangle (production-imports-tests) is a separate cleanup tracked in the spec comments. |
| First sanity check killed the bundle in 0.6s — way too short for the real backend's startup. | Rewrote the smoke test in `build-sidecar.sh` with a 60 s deadline and a `curl /api/health` confirmation. |

### G2b ✅ — Bundle fastembed + spaCy weights inside the installer

Landed 2026-04-29. ADR 003 §A's "self-contained, offline-capable from minute zero" property is restored: bundled binary boots without HuggingFace network access and serves real embeddings from `_MEIPASS/_bundled_models/fastembed`.

**What landed:**
1. **fastembed cache fetched at build time.** [`desktop/scripts/fetch-bundled-models.sh`](../../desktop/scripts/fetch-bundled-models.sh) populates `backend/_bundled_models/fastembed/` (240 MB), dereferences HuggingFace's symlink layout (snapshots → blobs) and drops `blobs/` + `.locks/` so PyInstaller bundles 240 MB once instead of ~470 MB doubled. Idempotent. Wired into [`desktop/scripts/build-sidecar.sh`](../../desktop/scripts/build-sidecar.sh) as the first step; the spec aborts if the cache is missing.
2. **spaCy NER model packages** (`pl_core_news_sm` and `en_core_web_sm`, both 3.8.0) added to [`backend/requirements.txt`](../../backend/requirements.txt) as direct GitHub wheel URLs — `npm run install:backend` now installs them automatically. PyInstaller picks them up via `collect_submodules` + `collect_data_files` like any other Python package; `spacy.load("pl_core_news_sm")` resolves through `sys.path` inside the bundle without any path gymnastics.
3. **PyInstaller spec extended** ([`desktop/sidecar/jarvis-sidecar.spec`](../../desktop/sidecar/jarvis-sidecar.spec)) — adds `(_bundled_models/fastembed, _bundled_models/fastembed)` to `datas`; aborts the build if the cache directory is missing so we never quietly ship a non-self-contained bundle.
4. **`_MEIPASS` resolution** in [`backend/services/embedding_service.py`](../../backend/services/embedding_service.py) — `_bundled_cache_dir()` returns `<bundle>/_bundled_models/fastembed` when `sys._MEIPASS` is set, else `None` (dev path uses fastembed's default `~/.cache`).
5. **Local sign with Developer ID.** Same identity used by `build-notarized.sh`. Without it, ad-hoc-signed bundles >300 MB hang in `_dyld_start` indefinitely on macOS Tahoe (see Findings below). Wired into [`build-sidecar.sh`](../../desktop/scripts/build-sidecar.sh) so the smoke test boots; falls back to ad-hoc if the cert isn't on the box (CI), with a warning.

**Verified empirically** (2026-04-29) — bundled binary booted with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1`:
- READY in ~12 s, no HuggingFace download attempts in stderr.
- `POST /api/memory/reindex-embeddings` → `{"status":"started"}` (proves `is_available()` returned True → `import fastembed` succeeded inside the bundle).
- `GET /api/memory/semantic-search?q=Warsaw` → `mode:"semantic"` (not `"unavailable"` — the embedding model loaded from `_MEIPASS`).
- Bundle size: 352 MB (was 109 MB pre-G2b; +243 MB matches the bundled cache + spaCy package data).

**Findings during G2b build:**

| Finding | Resolution |
|---|---|
| spaCy NER models were never installed in the host venv — `entity_extraction.py` had been silently falling back to regex since the project began. | Added the wheels to [`requirements.txt`](../../backend/requirements.txt); installing them surfaced *both* `persName/placeName/date` (Polish) and `PERSON/GPE/DATE` (English) entity tagging for the first time. |
| HuggingFace cache uses symlinks `snapshots/<hash>/* → blobs/<sha>` for dedup; PyInstaller dereferences them, doubling bundle weight. | `fetch-bundled-models.sh` walks `snapshots/`, copies-through symlinks, then deletes `blobs/` + `.locks/`. Keeps `refs/main` (40 bytes) — without it HF can't resolve the snapshot hash. |
| `fastembed.common.types` does `from PIL import Image` at module top, even on the text-only path; including real Pillow drags ~30 MB of native libs (libjpeg/libtiff/libwebp/...) into the bundle. | [`backend/utils/pil_stub.py`](../../backend/utils/pil_stub.py) registers a 5-line PIL.Image stub in `sys.modules` from [`run_frozen.py`](../../backend/scripts/run_frozen.py) before any fastembed import. The stub satisfies `Image.Image` (type alias) and `Image.Resampling` (enum used as default kwarg in `fastembed.image.transform.functional`); we never call image embedding code. PIL stays excluded. |
| **352 MB ad-hoc-signed bundle hangs in `_dyld_start` for 24+ minutes on macOS Tahoe.** Confirmed via `sample` — process stuck at 112 KB resident, `Physical footprint` constant. amfid (Apple Mobile File Integrity Daemon) is pathologically slow at verifying large novel ad-hoc-signed binaries. | [`build-sidecar.sh`](../../desktop/scripts/build-sidecar.sh) now signs the local bundle with our Developer ID (same cert `build-notarized.sh` uses). Apple-trusted identities skip the heavy verification; the same binary then boots in 12 s. Falls back to ad-hoc with a warning if the cert is unavailable (CI). |

### G2c ✅ — Notarize the new bundle

Landed 2026-04-30. Both Apple notary submissions returned `status: Accepted`; both stapled; `spctl --assess` reports `accepted, source=Notarized Developer ID` for the .app and the .dmg.

| Artifact | Notarytool ID | Status | Path |
|---|---|---|---|
| .app | `932b532f-4f24-4d81-9bbc-2a5f47afec42` | Accepted, stapled | `desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/DeepFilesAI.app` |
| .dmg | `f1a46024-57dc-47b2-85b3-de8f14496fcd` | Accepted, stapled | `desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/DeepFilesAI_0.1.0_aarch64.dmg` (355 MB) |

**Credential flow:** [`build-notarized.sh`](../../desktop/scripts/build-notarized.sh) was rewired to prefer a stored notarytool keychain profile (`notarytool-profile`, set up via `xcrun notarytool store-credentials notarytool-profile --apple-id you@example.com --team-id TEAMID` once per box) and falls back to `APPLE_ID` / `APPLE_PASSWORD` env vars on CI. The profile name is overridable via `NOTARY_PROFILE=<name>` env var. When the keychain profile is in use, env vars are *unset* before invoking `tauri build` so Tauri's bundler skips its built-in auto-notarize step (which only speaks env-var auth and would otherwise prompt). The script then explicitly notarizes both .app (zipped via `ditto`) and .dmg using the keychain profile. One-time setup, no per-shell exports thereafter.

**Pipeline split (2026-05-06).** The .dmg notarize+staple+verify phase is delegated to a separate [`build-dmg.sh`](../../desktop/scripts/build-dmg.sh) so a transient hdiutil/notarytool/stapler flake on the .dmg doesn't force a full ~25-min .app rebuild. `build-notarized.sh` finishes its .app verification first, prints the .app path, then calls `build-dmg.sh` with a friendly retry hint on failure. If the .dmg phase fails, the .app is fully signed and stapled — re-run `bash desktop/scripts/build-dmg.sh` alone to retry. Prerequisite for G4b6 cold-launch verification, where the test loop is expected to surface real bugs that need multiple build iterations.

**Verified empirically:**
- `codesign -dvv` on the embedded sidecar shows `flags=0x10000(runtime)` (hardened runtime), full Apple authority chain (Developer ID Application → Developer ID CA → Apple Root CA), `TeamIdentifier=TEAMID`.
- `xcrun stapler validate` passes on both artifacts.
- `spctl --assess` returns `accepted, source=Notarized Developer ID` for both.

### Original "Concrete moves" (kept for reference; mostly satisfied by G2a)

### Concrete moves

1. **New PyInstaller spec — `desktop/sidecar/jarvis-sidecar.spec`.**
   - Entry: `backend/scripts/run_frozen.py`.
   - `pathex=['backend']` so `from main import app` resolves.
   - Hidden imports (per [ADR 003 §A + §J](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md#amendment-2026-04-29-implementation-decisions)):
     - All FastAPI/uvicorn submodules (already worked out in `hello.spec`).
     - `fastembed`, `onnxruntime`, `tokenizers`.
     - `spacy`, `spacy.lang.en` (and any other model lang packs we use).
     - `keyring`, `keyring.backends.macOS` (mac), `keyring.backends.Windows` (win), `keyrings.alt.file` (encrypted-file fallback per the project's secret-storage doctrine).
     - `pydantic_settings`, `httpx`, project routers/services/models packages (PyInstaller usually misses dynamic imports inside FastAPI routers).
   - Datas: bundled fastembed ONNX weights + spaCy model directory (under `backend/_bundled_models/` — checked into git LFS or fetched at build time; see §A of ADR amendment).
   - `upx=False` (confuses macOS hardened-runtime — verified during spike).
   - Excludes: `tkinter`, `matplotlib`, `PIL`, `numpy.tests`, `pandas`, `IPython`, anything pulled by transitive deps that we don't ship.

2. **Bundled model assets — `backend/_bundled_models/`.**
   - Per ADR 003 §A: ship weights inside the installer. Add a one-off fetch script `desktop/scripts/fetch-bundled-models.sh` that downloads the fastembed ONNX file + spaCy model into `backend/_bundled_models/` if missing. Hooked into `build-notarized.sh` before `pyinstaller` runs.
   - At runtime, `embedding_service.py` resolves the model path via `PyInstaller._MEIPASS` when frozen, falls back to the fastembed default cache when not. Already compatible with how fastembed loads from a local path; just need a small `_resolve_model_dir()` helper.

3. **Build orchestration.**
   - Replace `desktop/sidecar/hello.spec` references in `build-sidecar.sh` with the new spec, or create a parallel `build-jarvis-sidecar.sh` and update `build-notarized.sh` to call it.
   - Bundle smoke test: after PyInstaller produces the binary, run `dist/jarvis-sidecar` directly with `JARVIS_API_PORT=0` and `curl` `/api/health` before signing.

4. **Notarize.**
   - `build-notarized.sh` already does the codesign + notarytool dance for both `.app` and `.dmg`. Re-run; the larger binary (likely 200-400 MB with weights) will take longer to upload but the flow is unchanged.
   - Verify `spctl --assess` passes for the new binary.

### Done when
- [x] `desktop/sidecar/jarvis-sidecar.spec` produces a working onefile binary (G2a)
- [x] Binary boots the real FastAPI app, READY-line emits, `/api/health` responds with version 0.15.0 (G2a)
- [x] Embeddings work end-to-end inside the bundled binary against a sample vault, with HuggingFace network access blocked (G2b — verified 2026-04-29)
- [x] spaCy NER models bundled (`pl_core_news_sm`, `en_core_web_sm`) and importable inside the bundle (G2b)
- [ ] Keyring round-trip works inside the bundle (write + read a test key) — to validate during G2c notarized build, since keyring's macOS backend talks to the system keychain via the OS keychainservices framework, which may behave differently under hardened runtime + notarization
- [ ] `build-notarized.sh` produces a notarized + stapled `.dmg` (G2c)
- [ ] `desktop-shell-spike` doc updated to point at the new spec (deferred to end of graduation, after G4)
- [ ] `hello.py` and `hello.spec` deleted, registry entry trimmed (deferred to end of graduation)

### Risks
- **Hidden-import surface is large** — FastAPI's lazy imports + project's late imports (e.g. `from services.embedding_service import reindex_all` inside `routers/memory.py:90`) will likely surface PyInstaller misses on first run. Mitigation: smoke-test thoroughly *before* notarizing (notarization is the slow part).
- **Self-contained inference path** — the bundle must boot offline-capable on first launch (per ADR 003 §A amendment). fastembed ONNX, spaCy NER models, and the Python runtime all land inside the PyInstaller archive; resolved via `sys._MEIPASS` at runtime.
- **Code-signing nested binaries** — fastembed's ONNX runtime ships its own dylibs; PyInstaller extracts them at runtime. Hardened runtime + library-validation-disabled entitlement (already on per spike) handles this. If it doesn't, the fallback is a per-dylib `codesign` pass.

---

## G4 — Bundled Ollama sidecar (split into G4a / G4b)

### G4a ✅ — Process plumbing: extract, re-sign, spawn, supervise

Landed 2026-04-30 (local verification — re-notarization tracked separately). Bundled `ollama serve` 0.22.0 spawns from the Tauri shell on `127.0.0.1:11435`, the Python sidecar receives `JARVIS_OLLAMA_BASE_URL=http://127.0.0.1:11435` at spawn time, both children are killed on canonical Cmd+Q.

**What landed:**
1. **[`desktop/scripts/fetch-ollama.sh`](../../desktop/scripts/fetch-ollama.sh)** — pinned `OLLAMA_VERSION=0.22.0` + SHA-256, downloads upstream `Ollama-darwin.zip` (~164 MB) to a per-repo cache, extracts the runtime payload (binary + libggml + mlx_metal_v3 + mlx_metal_v4) to `desktop/src-tauri/binaries/ollama-runtime/`, strips quarantine xattrs, re-signs every Mach-O (1 binary + 14 dylibs/sos) under our Developer ID with hardened runtime + the existing Entitlements.plist, smoke-tests `ollama --version`. Idempotent: skips on re-run when the version marker matches.
2. **[`desktop/src-tauri/tauri.conf.json`](../../desktop/src-tauri/tauri.conf.json)** — adds `bundle.resources: { "binaries/ollama-runtime": "ollama-runtime" }`. Uses the resources primitive (not `externalBin`) because the Ollama runtime is a directory of binary + dylibs + nested runner dirs, not a single executable. `externalBin` is retained for the PyInstaller sidecar.
3. **[`desktop/src-tauri/src/lib.rs`](../../desktop/src-tauri/src/lib.rs)** — refactored to a two-child supervisor:
   - `spawn_ollama()` resolves `<resource_dir>/ollama-runtime/ollama` via `app.path().resource_dir()`, sets `OLLAMA_HOST=127.0.0.1:11435`, `OLLAMA_MODELS=<app_data>/ollama-models/`, `OLLAMA_KEEP_ALIVE=5m`, and spawns via `std::process::Command` with `current_dir` pointed at the runtime dir so `@loader_path` rpaths resolve sibling dylibs.
   - `await_ollama_ready()` blocks on a TCP connect-probe to `:11435` (50 ms backoff, 10 s deadline) — std-only, no HTTP client dep.
   - The PyInstaller sidecar is spawned *after* ollama is ready, with `JARVIS_OLLAMA_BASE_URL` injected so [`backend/services/ollama_service.py`](../../backend/services/ollama_service.py) talks to our bundled instance.
   - `RunEvent::ExitRequested` / `Exit` kills both children and reaps ollama with `wait()`.
4. **[`desktop/scripts/build-notarized.sh`](../../desktop/scripts/build-notarized.sh)** — wires `fetch-ollama.sh` as step 3/5 (after sidecar build, before tauri build). Renumbered notarize steps to 5a/5b.

**Verified empirically (2026-04-30, non-notarized local build):**
- All 16 Mach-O files in `Resources/ollama-runtime/` signed under `Developer ID Application: EXAMPLE (TEAMID)`, hardened-runtime flag set; `codesign --verify --deep --strict` passes on the assembled `.app`.
- Launch → bundled ollama binds `127.0.0.1:11435`, `curl http://127.0.0.1:11435/` returns `Ollama is running`.
- Sidecar `/api/health` → `{"status":"ok","version":"0.15.0"}`.
- Sidecar `/api/local/runtime` reports `base_url: http://127.0.0.1:11435`, `version: 0.22.0`, `reachable: true` — confirming the env-var handshake routes the backend at our bundled instance, not the host's separately-installed Ollama (which was simultaneously alive on `:11434`, version 0.18.0 — coexistence verified per ADR 003 §"Coexistence").
- Cmd+Q (`osascript -e 'tell application "DeepFilesAI" to quit'`) kills both children cleanly; no orphans.

**Findings during G4a build:**

| Finding | Resolution |
|---|---|
| Ollama 0.22.0 on macOS is no longer a self-contained CLI — the binary at `Resources/ollama` dlopens libggml + mlx_metal runner dylibs at startup. The original ADR 003 §B assumption (Metal kernels statically linked into the binary) is stale. | Bundle the entire runtime payload as a directory; switch from Tauri's `externalBin` (single-binary) to `bundle.resources` (directory). [ADR 003 Amendment 2026-04-30 §L+§M](../architecture/decisions/003-desktop-distribution-tauri-and-sidecars.md#amendment-2026-04-30--ollama-0220-macos-payload--bundling-primitive-g4a) records the shape change. |
| Apple notary requires every Mach-O inside a hardened-runtime app to be signed by a single Developer ID — a single ad-hoc dylib in `Resources/ollama-runtime/` would block the verdict. | `fetch-ollama.sh` walks every `*.dylib` / `*.so` and codesigns it with `--options runtime` before the main binary is signed; entitlements applied only to the main binary (dylibs inherit). |
| `pkill -TERM` against the Tauri shell leaves ollama orphaned — RunEvent::Exit doesn't fire under signal-driven termination. | Documented as a known force-quit limitation (matches ADR 003 §"Negative" §zombies for jarvis-sidecar). Canonical Cmd+Q path is clean. Hardening SIGTERM via a signal handler + process-group propagation is filed as a follow-up below. |

**Follow-ups (not blocking G4a):**
- **SIGTERM-graceful teardown** for force-quit scenarios. Wire a signal handler in the Rust shell that synthesizes `app.exit()` so the existing cleanup runs. Matches the watchdog discipline already in place for jarvis-sidecar (`JARVIS_SHELL_PID`).
- **Re-notarize the new ~745 MB bundle** as G2c was done — separate user-authorized step.
- **Surface system-Ollama coexistence note** in the runtime UI (ADR 003 follow-up #3) — when a user has Ollama on `:11434`, mention it next to GPU/VRAM stats.

### G4b1–b5 ✅ — First-run orchestrator + ladder + lightweight mode

Backend orchestrator and frontend wizard landed per ADR 005 §B and §C. Implementation lives in [`backend/services/first_run_orchestrator.py`](../../backend/services/first_run_orchestrator.py), [`backend/services/memory_pressure_monitor.py`](../../backend/services/memory_pressure_monitor.py), [`frontend/app/composables/useFirstRun.ts`](../../frontend/app/composables/useFirstRun.ts), and [`frontend/app/components/OnboardingLocalFlow.vue`](../../frontend/app/components/OnboardingLocalFlow.vue). See [docs/features/local-models.md](local-models.md) for the full pipeline description (state machine, marker semantics, skip path, downgrade ladder, OOM-retry, lightweight mode).

### G4b6 ☐ — Cold-launch verification on the notarized bundle

**Goal:** prove the §B pipeline drives the wizard correctly on a clean install — fresh `<app_data>`, fresh keychain, no prior marker — when the binary is the notarized DMG (not a dev-mode launch).

**Runbook:** [`docs/runbooks/g4b6-cold-launch-verification.md`](../runbooks/g4b6-cold-launch-verification.md) — step-by-step procedure with pass/fail observations, diagnostic commands, and the cold-launch-state-reset recipe (Option A: separate Mac, Option B: simulate clean on this Mac by clearing Tauri app-data, workspace dir, logs, and keychain entries).

**Done when**
- [ ] Notarized DMG installs cleanly on a stock macOS arm64 box.
- [ ] First launch shows the OnboardingLocalFlow wizard (no marker present) and auto-kicks the first-run orchestrator.
- [ ] Primary pull lands; chat is reachable via the `chatReady` release.
- [ ] Background fallback pull and chat-model probe complete without blocking the chat UI.
- [ ] Marker file `<app_data>/.first_run_complete` is written.
- [ ] Second launch skips the wizard entirely (marker-present early return).
- [ ] `find DeepFilesAI.app -name "*anthropic*" -o -name "*openai*" -o -name "*litellm*"` → empty (ADR 015 §F audit signal 2).
- [ ] `defaults read DeepFilesAI.app/Contents/Info.plist JarvisBundleCapabilities` → `["local-llm", "vault-markdown", "knowledge-graph", "semantic-search"]`.

Gated on the notarized build artifact from ADR 015 chunk 7.

### Original "Concrete moves" (kept for reference; G4a satisfies the process-plumbing half)

### Concrete moves

1. **Extract + re-sign Ollama.**
   - One-off (or build-time) script `desktop/scripts/fetch-ollama.sh`: downloads upstream `Ollama-darwin.zip`, extracts `Ollama.app/Contents/Resources/ollama` (the CLI binary, not the menu-bar app), strips quarantine xattrs, re-signs with our Developer ID Application identity + hardened runtime + the same entitlements file.
   - Place at `desktop/src-tauri/binaries/ollama-<triple>` (matching Tauri's externalBin naming convention so `tauri-plugin-shell::sidecar("ollama")` resolves).

2. **Tauri config — second externalBin entry.**
   - `tauri.conf.json` `bundle.externalBin: ["binaries/jarvis-sidecar", "binaries/ollama"]`.
   - `capabilities/default.json` extends `shell:allow-execute` to cover `ollama` too.

3. **Spawn from Rust.**
   - In `lib.rs`, after parsing the jarvis-sidecar's READY line, spawn `ollama` with:
     - `OLLAMA_HOST=127.0.0.1:11435` (private port — avoids clashing with a separately-installed Ollama on `:11434`)
     - `OLLAMA_MODELS=<app_data_dir>/ollama-models/`
     - `OLLAMA_KEEP_ALIVE=5m` (or whatever ADR 003 §B specifies)
   - Wait for `ollama serve` to be reachable on `:11435` (HTTP `GET /` returns 200 `Ollama is running` — poll with 50ms backoff, 10s deadline).
   - Pass `JARVIS_OLLAMA_BASE_URL=http://127.0.0.1:11435` into the jarvis-sidecar's spawn env (so the already-built `ollama_service.py` reads it).
   - Track both child processes; on shutdown, kill both. Watchdog story is already covered for jarvis-sidecar; Ollama upstream handles its own shutdown cleanly when sent SIGTERM.

4. **First-run model pull.**
   - On first launch (no marker file, or `ollama list` returns empty), trigger a `POST /api/pull { model: "<default>" }` against the bundled Ollama. Surface progress via the same toast pattern used in G5.
   - Default model is a real ADR 005 decision (hardware-tiered first-run policy + downgrade ladder). Probe machine hardware → recommend the top fitting Tier A/B/C model → pull recommended-top + at least one smaller fallback so memory-pressure downgrade actually has somewhere to go. Land ADR 005 before G4b.

### Done when
- [ ] `fetch-ollama.sh` produces a re-signed `ollama` binary that passes `codesign --verify`
- [ ] `tauri.conf.json` ships both sidecars
- [ ] Rust shell spawns Ollama, waits for ready, passes its URL to jarvis-sidecar
- [ ] Real backend's `ollama_service.py` talks to the bundled Ollama (verified by chat round-trip in the bundled app)
- [ ] First-run model pull works with progress UI
- [ ] Both children die on shell quit + force-kill
- [ ] Notarized; `spctl --assess` passes for the new bundle (which now has two embedded externalBins)

### Risks
- **Re-signing Ollama** — Ollama's binary is already Developer ID signed by their team. We're stripping that and re-signing with ours. License check: confirm Ollama's MIT/Apache permits redistribution; pin the version we extract from. Treat the upstream version as a dependency.
- **Two-process supervision** — if Ollama dies mid-session, chat hangs. Add a healthcheck (the same `:11435` ping) on a 30s interval; surface "Ollama crashed, restarting…" toast and respawn.
- **Bundle size doubles** — Ollama binary is ~70 MB, model file 1-3 GB depending on choice. Per ADR 003 §B trade-offs, this is the explicit cost of "no separate install."
- **Port clash on user's machine** — `:11435` is non-default on purpose; collision unlikely but possible. Fall back to `:0` (OS-assigned) if bind fails, just like jarvis-sidecar.

---

## Definition of done (whole graduation)

- All four chunks ✅ in the table above.
- Notarized DMG launches the real product end-to-end on a stock macOS arm64 box: chat works, vault editing works, semantic search works, knowledge graph renders.
- `desktop-shell-spike.md` retitled / archived; `frontend-spike/` and `hello.{py,spec}` removed from git.
- ADR 003's "v1 schedule risk" callout in §"Negative" updated to ✅ resolved.
- Windows graduation tracked in a follow-up doc — out of scope here.

## Update protocol

When a chunk lands or a sub-task progresses:
1. Flip its row state in the status table at the top.
2. Tick the relevant `[ ]` checkboxes in its "Done when" section.
3. Bump `last_updated` in the frontmatter.
4. If the work surfaced an unforeseen issue, append a "Findings" subsection to that chunk and link the resolving commit/PR.
