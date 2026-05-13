---
title: App Shell & Navigation
status: active
type: feature
sources:
  - frontend/app/app.vue
  - frontend/app/layouts/default.vue
  - frontend/app/pages/index.vue
  - frontend/app/pages/main.vue
  - frontend/app/composables/useAppState.ts
  - frontend/app/composables/useKeyboard.ts
  - frontend/app/composables/useApi.ts
  - frontend/app/components/Orb.vue
  - frontend/app/components/StatusBar.vue
  - frontend/app/components/ConfirmDialog.vue
  - frontend/app/components/ObsidianHelper.vue
  - frontend/app/types/index.ts
depends_on: []
last_reviewed: 2026-04-14
---

## Summary

The app shell is the structural frame of the Jarvis frontend: it handles initial routing based on workspace state, renders the persistent navigation bar, and provides the shared state and HTTP client that every other feature builds on. It also contains the animated Orb — the primary visual indicator of what the system is currently doing.

## How It Works

### Boot and route guard

The root page (`pages/index.vue`) is the only entry point for the application. On load it calls `checkWorkspaceStatus()` synchronously (using `await` in `<script setup>`), then immediately redirects to either `/main` or `/onboarding` with `replace: true` so the index route is never part of the browser history. The backend decides whether setup is complete; the frontend just trusts the `initialized` boolean from `/api/workspace/status`.

This means there is no client-side route guard middleware — the redirect logic lives entirely in `index.vue` and runs once per page load.

### Splash-driven async boot ([ADR 022](../architecture/decisions/022-splash-boot-async.md))

The Tauri shell builds the main window **immediately** on launch — before Ollama spawns, before the Python sidecar unpacks, before the license probe. That window's first paint is [`SplashScreen.vue`](../../frontend/app/components/SplashScreen.vue), a calibration-boot surface that mirrors the real boot stages emitted by the shell as `boot:stage` events.

The shell's `setup` hook is now ~25 lines: install the dev logger, seed a `BootStateHandle` with the initial pending stage, build the window, and `async_runtime::spawn` `run_boot_sequence`. That async task walks Ollama-spawn → sidecar-spawn → `JARVIS_BACKEND_READY` handshake → license probe, calling `emit_boot(...)` at each transition. Errors land as `Phase::Error` events instead of bubbling up — that lets the splash surface a tangible failure state rather than vanishing the window.

Frontend pieces:

- [`useBoot.ts`](../../frontend/app/composables/useBoot.ts) is a module-level singleton that subscribes once to `boot:stage`. On `Phase::Ready` it writes `__JARVIS_CONFIG__` and `__JARVIS_LICENSE_STATE__` onto `window` *before* flipping `boot.ready` true, so the real layout's first read sees populated globals. ADR 019's first-paint contract holds: the splash itself is non-content (brand surface only), and the real layout doesn't mount until license state is known.
- A late-mount catch-up via the `get_boot_state` Tauri command handles the Vue-hydration vs. Rust-task race — if the splash mounts after the first event already fired, the snapshot resumes the readout from the right point.
- `SplashScreen.vue` renders an instrument-panel surface (all-monospace, brand-cyan + phosphor-amber accent, hairline borders, scanlines + grain, corner ticks) driven by the real stage events. Long stages (sidecar spawn — 10–25 s on a cold install while the 1.8 GB PyInstaller bundle unpacks to `/var/folders/.../tmp.…`) cycle through honest subtitle variants every 4 s so the eye sees motion instead of staring at a static label.
- In browser-dev mode (`__TAURI_INTERNALS__` absent), `useBoot` short-circuits to `ready=true` immediately. The splash never paints; the dev Nuxt server is already talking to a hand-launched backend.
- `default.vue` gates the real layout on `boot.ready` and unmounts the splash 460 ms after ready flips (longer than the splash's own 420 ms opacity transition, so the visual fade always finishes before the DOM node disappears).
- Other startup side-effects (`checkHealth`, `startWarmupPolling`, the `license:file_opened` listener) are deferred until boot completes — they all depend on backend up anyway, and firing them earlier produced spurious "Offline" pills.

Net result: the cold-launch perceived wait drops from a 10–30 s blank dock-bounce (which read as "the app crashed") to a ~200 ms paint-to-splash plus a calibrated readout of what's actually happening.

### Shared state via `useAppState`

Three pieces of state are owned at the application level and shared across all pages using Nuxt's `useState()` (keyed strings, so calls from different components return the same reactive ref):

- `isInitialized` — set during the boot check; used only by `index.vue`
- `backendStatus` — polled by `main.vue` on mount via `checkHealth()`, displayed as the "Alive / Offline / Checking..." pill in the status bar
- `chatActive` — a boolean that `main.vue` sets whenever `messages` is non-empty; `StatusBar` reads it to fade out the "JARVIS" wordmark so the orb can animate into its position

### Layout

`app.vue` wraps everything in `<NuxtLayout><NuxtPage />`, and `layouts/default.vue` composes `<StatusBar />` above a `<slot />`. Every page therefore receives the navigation bar automatically. The layout is `height: 100vh; overflow: hidden` — scroll management is pushed down to individual page components.

### Status bar and navigation

`StatusBar.vue` provides five navigation links (Chat, Memory, Graph, Specialists, Settings) and the backend status indicator. The "JARVIS" wordmark on the left fades to `opacity: 0` when `chatActive` is true — this is intentional to clear visual space for the mini orb that animates into the same top-left region.

On viewports narrower than 640px the navigation links collapse behind a hamburger button. The hamburger icon animates into an X when the menu is open. The dropdown appears below the status bar with a slide-down animation and a semi-transparent backdrop that closes the menu on tap. Active links use a left-border accent instead of the desktop pill style. Route changes also auto-close the menu.

### The Orb and its position animation

`Orb.vue` is a fully SVG-based animated component. It accepts a single `state` prop (`'idle' | 'listening' | 'thinking' | 'speaking'`) and uses that to drive CSS class–based changes across its layers: arc rotation speed, glow pulse rate, core gradient colors, and drop-shadow intensity.

The orb's position is managed in `main.vue` via a CSS class toggle on a `position: fixed` wrapper, not by the Orb component itself. In hero mode (no chat messages), the wrapper is centered in the content area. Once chat starts (`chatActive` becomes true), the wrapper transitions to `top: 20px; left: 48px; transform: scale(0.13)` — overlaying the faded wordmark. The transition uses an 0.85s cubic-bezier curve so the shrink-and-fly motion is smooth.

`main.vue` also keeps the session sidebar in sync with the backend in real time. Two watchers call `sessionsState.loadSessions()`: one when `chat.sessionId` changes (new session created via WebSocket `session_start`), and another when a chat response completes (`isLoading` transitions from true to false). This ensures new sessions and updated titles appear immediately without a page refresh.

`main.vue` derives the orb state from voice and loading state with this priority:

1. If voice is active (listening / speaking), use the voice state
2. Else if a chat response is in-flight, use `'thinking'`
3. Otherwise `'idle'`

### HTTP client (`useApi`)

All HTTP calls in the app go through `useApi`, which wraps Nuxt's `$fetch`. Every method follows the same pattern: call `$fetch`, catch errors, normalize them into an `ApiError` instance with a numeric `status` code, and rethrow. Network failures (no `status` property on the error) are surfaced as `ApiError(0, 'Network error')`.

`useApi` is not a singleton — it is instantiated per call site, which is fine because it holds no state. All returned functions are thin wrappers around `$fetch` with typed response shapes from `~/types`.

### Keyboard shortcuts (`useKeyboard`)

`useKeyboard` registers a global `keydown` listener for the lifetime of the component that calls it. Two bindings are supported:

- **Space** — toggles voice (only fires when the focused element is not an input, textarea, or contenteditable element)
- **Escape** — triggers the cancel callback unconditionally

An `enabled` guard function can be passed to disable all shortcuts conditionally.

### Confirm dialog

`ConfirmDialog.vue` is a fully generic destructive-action modal. It uses `<Teleport to="body">` so it always renders above other stacking contexts, and `<Transition>` for a scale+fade entrance. Clicking the overlay backdrop fires `cancel`, matching the expected UX for dismissable modals. The component is stateless — visibility and callbacks are entirely prop/emit driven, so the parent owns the open/close lifecycle.

### Obsidian integration helper

`ObsidianHelper.vue` (used on the Settings page) generates an `obsidian://open?vault=Jarvis` deep link. It opens with `window.open(..., '_blank')` to hand off to the Obsidian desktop application without navigating the current tab. The vault name is hardcoded as `Jarvis` — this works if the user accepted the default workspace name during onboarding, but will silently fail to open the right vault if they renamed it.

## Key Files

| File | Role |
|---|---|
| `frontend/app/app.vue` | Root component; mounts layout and page router outlet |
| `frontend/app/layouts/default.vue` | Persistent shell: StatusBar above page slot, full-viewport height |
| `frontend/app/pages/index.vue` | Boot route; checks workspace status and redirects to `/main` or `/onboarding` |
| `frontend/app/pages/main.vue` | Primary chat view; wires voice, chat, sessions, and orb state together |
| `frontend/app/composables/useAppState.ts` | Shared reactive state for backend status, workspace init flag, and chat-active flag |
| `frontend/app/composables/useApi.ts` | Typed HTTP client for all backend endpoints; normalizes errors into `ApiError` |
| `frontend/app/composables/useKeyboard.ts` | Global keyboard shortcut handler (Space for voice, Escape for cancel) |
| `frontend/app/components/Orb.vue` | SVG animated state indicator; driven entirely by a single `OrbState` prop |
| `frontend/app/components/StatusBar.vue` | Navigation bar with five section links and live backend status pill |
| `frontend/app/components/ConfirmDialog.vue` | Generic teleported modal for destructive confirmations |
| `frontend/app/components/ObsidianHelper.vue` | Settings widget that deep-links into the Obsidian desktop app |
| `frontend/app/types/index.ts` | Canonical TypeScript type definitions for all API shapes and shared enums |

## Gotchas

**Orb position is hardcoded to a pixel offset.** The mini-orb target position (`top: 20px; left: 48px`) is calculated to overlap the "JARVIS" label in the status bar. If the status bar padding or label width changes, this offset will need a matching update in `main.vue`'s CSS — the two locations are not linked programmatically.

**Space bar shortcut fires globally.** `useKeyboard` only suppresses Space when the event target is a focusable input element. Any custom component that handles text input without using a native `<input>` or `<textarea>` (e.g., a contenteditable `div` without `isContentEditable`) will not be caught by the guard, and voice will toggle unexpectedly.

**`ObsidianHelper` hardcodes the vault name.** The `obsidian://` deep link always uses `vault=Jarvis`. If the user chose a different workspace name during onboarding, the link opens the wrong vault or prompts Obsidian to create a new one.

**`checkWorkspaceStatus` swallows all errors as `initialized: false`.** If the backend is down when `index.vue` loads, the user is sent to onboarding rather than seeing an error. This is intentional for the MVP but means a temporary backend restart could put a user through onboarding again.

**`useApi` has no request deduplication or caching.** Every call creates a fresh `$fetch` request. Pages that call the same endpoint on mount (e.g., fetching sessions) will fire redundant requests if rendered multiple times. This is acceptable at current scale but worth noting for high-frequency endpoints.
