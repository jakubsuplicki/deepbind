# ADR 022 — Splash-driven async boot; window paints in ~200 ms

**Status:** Accepted
**Date:** 2026-05-08
**Related:** [ADR 003](003-desktop-distribution-tauri-and-sidecars.md), [ADR 015](015-single-target-local-only-stack.md), [ADR 016](016-chat-send-via-tauri-ipc.md), [ADR 019](019-licensing-operational-model.md), [ADR 021](021-sidecar-ml-warmup-at-boot.md)

## Context

Until this ADR, the Tauri shell built its main window only **after** every step of the boot pipeline completed synchronously inside `setup`:

1. Spawn bundled Ollama, wait for `:11435` to bind (~1–2 s).
2. Spawn the PyInstaller-frozen Python sidecar, wait for `JARVIS_BACKEND_READY` on stdout (8–25 s on a cold install — the bulk is the 1.8 GB `--onefile` unpack to `/var/folders/.../tmp.…`).
3. Probe `/api/license/state` synchronously so the entitlement wall could paint from the first frame (ADR 019 contract).
4. Build the window with `__JARVIS_CONFIG__` + `__JARVIS_LICENSE_STATE__` baked in via `initialization_script`.

That sequence shipped a working app, but produced a real UX regression on cold launches: the user clicked the icon, the dock bounced, and **no window appeared for 10–30 s**. Multiple cycles of "the app crashed" reports confirmed the perception was indistinguishable from a hard failure. The 1.8 GB sidecar binary unpacking to a fresh tempdir is the dominant cost — every fresh install (including notarized DMG installs at the customer site) hits it.

This is structurally separate from the chat-perf class of issues addressed by [ADR 021](021-sidecar-ml-warmup-at-boot.md): warmup runs in a daemon thread *after* the sidecar is up, so it can't add launch wall-clock. The blocker is upstream of warmup, in the shell's window-build gate.

## Decision

The Tauri shell builds the main window **immediately**, and runs the boot pipeline in an async task that emits `boot:stage` events as it progresses. The window's first paint is a `<SplashScreen>` component that subscribes to those events and crossfades to the real layout once the pipeline reports `Phase::Ready`.

Concretely:

- **Tauri shell** ([`desktop/src-tauri/src/lib.rs`](../../../desktop/src-tauri/src/lib.rs))
  - `setup` now does only three things: install the dev logger plugin, seed a `BootStateHandle` with the initial `Phase::OllamaStarting` snapshot, and `WebviewWindowBuilder.build()`. No `initialization_script` for config / license — those values aren't known yet.
  - `run_boot_sequence(app_handle)` is an async fn spawned from `setup` via `async_runtime::spawn`. It walks the same Ollama-spawn → sidecar-spawn → license-probe stages as before, but calls `emit_boot(...)` at each transition. Errors land as `Phase::Error` events instead of bubbling up to `setup`'s return — that lets the splash surface a tangible boot-failure state instead of vanishing the window.
  - New Tauri command `get_boot_state` returns the most-recent stage snapshot for late-mounting splash instances (Vue hydration vs. Rust task is a race; the snapshot prevents the splash from sitting on the default-pending state if it mounted after an early stage already fired).
- **Frontend** ([`useBoot.ts`](../../../frontend/app/composables/useBoot.ts), [`SplashScreen.vue`](../../../frontend/app/components/SplashScreen.vue))
  - `useBoot` is a module-level singleton that subscribes once to `boot:stage`. On `Phase::Ready` it writes `__JARVIS_CONFIG__` and `__JARVIS_LICENSE_STATE__` onto `window` *before* flipping `boot.ready` true, so the real layout's first read sees populated globals. ADR 019's first-paint contract holds: the splash itself is non-content (brand surface only), and the real layout doesn't mount until license state is known.
  - `SplashScreen.vue` renders a calibration-boot surface — all-monospace, brand-cyan + phosphor-amber accent, hairline borders, scanlines + grain — driven by the real stage events. Long stages (sidecar spawn) cycle through honest subtitle variants every 4 s so the eye sees motion during PyInstaller unpack instead of staring at a static label.
  - In browser-dev mode (`__TAURI_INTERNALS__` absent), `useBoot` short-circuits to `ready=true` immediately. The splash never paints; the dev Nuxt server is already talking to a hand-launched backend.
  - `default.vue` gates the real layout on `boot.ready` and unmounts the splash 460 ms after ready flips (longer than the splash's own 420 ms opacity transition, so the visual fade always finishes before the DOM node disappears). Other startup side-effects (`checkHealth`, `startWarmupPolling`, license file-opened listener) are deferred until boot completes — they need the backend to be up anyway, and firing them earlier just produced spurious "Offline" pills.

## Trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| Build window immediately, run boot async | Window paints in ~200 ms vs. 10-30 s blank dock-bounce. App no longer reads as crashed. | Two transport paths for "where do my config + license globals come from": init_script (gone) → event payload (new). All callers go through the new path; no compatibility shims needed. |
| Boot errors render as `Phase::Error` instead of aborting `setup` | User sees a tangible failure state with the actual error text, not a silently vanished window. | The shell stays alive on a partial boot; `OllamaHandle` / `SidecarHandle` may hold half-spawned children that the quit handler must still reap. The `app.manage(...)` calls were reordered to run as soon as each child spawns so the existing quit handler finds them. |
| Splash gates the real layout entirely (option A) instead of overlaying it (option B) | ADR 019's first-paint contract is preserved without any race window between layout-mount and license-state arrival. Eliminates a class of "wall flickered briefly" bugs. | Other layout-level side effects (health polling, file-opened listener) start later than they used to. Acceptable: they all depend on backend up anyway. |
| Subtitle variants on long stages | The 10-25 s sidecar unpack feels like progress instead of a hang. Variants are *true at the moment they show* — no faked progress. | One extra timer + a small string table per stage. The cost is borne by the eye, not the CPU. |
| Splash unmount delayed 460 ms after ready | The 420 ms opacity transition completes before the DOM node disappears — no jarring visual cutoff. | A small magic number coupled to the CSS transition duration. Tested on multiple animation timings; documented in both files. |

## Alternatives Considered

- **Switch PyInstaller `--onefile` → `--onedir`.** Would eliminate the unpack stall entirely (the dominant cost), but rewrites the bundling pipeline: tauri.conf.json's `externalBin` is documented as a single file, the build script would need to ship a directory tree, and recursive codesign + notarization would need a complete revalidation. Bigger scope; higher regression risk on signing. Splash + async boot is a smaller chunk that captures most of the perceived UX win without touching the bundle pipeline. `--onedir` remains a candidate for a later ADR if 1–3 s of warm-cache splash still feels long.
- **Show a native `NSWindow` splash before Tauri's main window.** Would give the absolute fastest possible paint (~50 ms), but requires platform-specific window code that defeats Tauri's portability story and triples the surface area for code-signing. The Tauri-built window painting in 200 ms is fast enough that a native pre-splash isn't worth the complexity.
- **Inline `__JARVIS_CONFIG__` / `__JARVIS_LICENSE_STATE__` via `initialization_script` *after* boot completes.** Tauri's init_script is a one-shot at window build; you can't inject more after the fact. The window globals need to flow through events.
- **Render the real layout behind the splash, with the splash overlaying.** Faster perceived transition but the real layout's setup() runs against unknown license state, opening a race window where the entitlement wall could paint a frame late. ADR 019 forbids that. Gating the real layout on `boot.ready` is the only option that holds the contract.
- **Build the window with `visible: false`, show it after boot completes.** Same UX as the prior shape — user clicks, dock bounces, nothing happens for 10-30 s. The whole point is to get *something* on screen fast.

## Migration Path

Lands as one self-contained chunk:

1. `desktop/src-tauri/src/lib.rs` — extract `run_boot_sequence`, add `BootStage` / `BootStateHandle` / `get_boot_state`, refactor `setup`.
2. `desktop/src-tauri/src/license.rs` — drop `boot_state_blocking` (unused), replace with async `boot_state` for the new path.
3. `frontend/app/composables/useBoot.ts` (new) — singleton subscription + `__JARVIS_CONFIG__` / `__JARVIS_LICENSE_STATE__` injection.
4. `frontend/app/components/SplashScreen.vue` (new) — the design surface.
5. `frontend/app/layouts/default.vue` — gate real layout on `boot.ready`, defer side-effects until ready.

No prior contracts break:

- `__JARVIS_CONFIG__` / `__JARVIS_LICENSE_STATE__` consumers see them populated when the layout mounts, same as before — only the *injection point* moves from `initialization_script` to the splash component.
- `useLicenseState` still seeds from the boot global on its first read.
- Quit handler still finds and reaps `OllamaHandle` / `SidecarHandle`; managed-state insertion now happens inside the async task, but it runs before the children can be killed (the kill order in the run handler is unchanged).

## Verification

- **`cargo check`** clean (one upstream `tauri-plugin-shell::open` deprecation warning, unrelated, pre-existing).
- **`nuxi build`** clean (vue-tsc + nitro prerender both pass).
- **Live verification** on M5 24 GB pending. Expected behaviour:
  - Window paints within ~200–400 ms of launch.
  - Splash shows `INITIALIZING` → `LOADING ENGINE` (the long stage during PyInstaller unpack) → `VERIFYING` → `READY`.
  - Subtitle variants cycle every 4 s during `LOADING ENGINE`.
  - `boot:complete` fires; splash fades out over ~420 ms; real layout takes over with `__JARVIS_CONFIG__` + `__JARVIS_LICENSE_STATE__` populated.
