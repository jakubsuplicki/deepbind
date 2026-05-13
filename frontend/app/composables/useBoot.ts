/**
 * useBoot — splash-screen boot orchestration.
 *
 * The Tauri shell builds the window immediately, then runs Ollama spawn →
 * sidecar spawn → READY handshake → license probe in an async task and emits
 * `boot:stage` events as it progresses. This composable subscribes once,
 * mirrors stage state into reactive refs, and on `Phase::Ready` writes the
 * resolved config + license-state onto `window.__JARVIS_CONFIG__` and
 * `window.__JARVIS_LICENSE_STATE__` *before* the splash transitions out.
 *
 * That ordering preserves ADR 019's first-paint contract: the splash itself
 * is non-content (brand surface only), and by the time the real layout
 * mounts, both globals are populated, so `useLicenseState` reads a real
 * state on its first call instead of "unknown".
 *
 * In browser-dev mode (`__TAURI_INTERNALS__` absent) this composable
 * short-circuits to `ready=true` immediately. The splash never paints; the
 * dev Nuxt server is talking to a hand-launched backend that's already up.
 *
 * Module-level singleton: only one event subscription across the app.
 */
import { ref, readonly } from 'vue'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
export type BootPhase =
  | 'ollama_starting'
  | 'ollama_ready'
  | 'sidecar_starting'
  | 'sidecar_ready'
  | 'license_probing'
  | 'ready'
  | 'error'

export interface BootStage {
  phase: BootPhase
  detail: string
  progress: number
  config?: { backend_url: string; ws_url: string }
  license?: unknown
  error?: string
}

const _phase = ref<BootPhase>('ollama_starting')
const _detail = ref<string>('initializing')
const _progress = ref<number>(0)
const _error = ref<string | null>(null)
const _ready = ref<boolean>(false)
let _initialized = false

function _isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

function _applyStage(stage: BootStage): void {
  _phase.value = stage.phase
  _detail.value = stage.detail
  // Progress is monotone-ish from the shell side; clamp defensively.
  _progress.value = Math.max(0, Math.min(1, stage.progress))
  if (stage.phase === 'error') {
    _error.value = stage.error ?? stage.detail
    return
  }
  if (stage.phase === 'ready') {
    // Inject the resolved globals onto window BEFORE flipping the ready
    // flag, so consumers reading these in their setup() (useLicenseState,
    // useApi) see populated values on the very first read.
    if (typeof window !== 'undefined') {
      const w = window as unknown as Record<string, unknown>
      if (stage.config) {
        w.__JARVIS_CONFIG__ = {
          backendUrl: stage.config.backend_url,
          wsUrl: stage.config.ws_url,
        }
      }
      if (stage.license !== undefined) {
        w.__JARVIS_LICENSE_STATE__ = stage.license
      }
    }
    _ready.value = true
  }
}

/**
 * Initialize the boot subscription. Idempotent — subsequent calls are no-ops.
 * Called once from `default.vue::onMounted`.
 */
export async function initBoot(): Promise<void> {
  if (_initialized) return
  _initialized = true

  if (!_isTauri()) {
    // Browser-dev: there's no Tauri shell driving the boot. Trust the dev
    // backend is already up and skip the splash entirely.
    _ready.value = true
    _phase.value = 'ready'
    _progress.value = 1
    _detail.value = 'dev'
    return
  }

  try {
    const [{ listen }, { invoke }] = await Promise.all([
      import('@tauri-apps/api/event'),
      import('@tauri-apps/api/core'),
    ])

    // Late-mount catch-up: if the boot task already fired stages before the
    // splash mounted, `get_boot_state` returns the last snapshot so we can
    // resume from the right point instead of sitting on the default-pending.
    try {
      const snapshot = await invoke<BootStage>('get_boot_state')
      if (snapshot?.phase) {
        _applyStage(snapshot)
      }
    } catch (e) {
      // Non-fatal — the live event stream below will catch us up.
      console.warn('[useBoot] get_boot_state snapshot failed:', e)
    }

    await listen<BootStage>('boot:stage', (evt) => {
      _applyStage(evt.payload)
    })
  } catch (e) {
    console.error('[useBoot] failed to subscribe to boot:stage:', e)
    // Hard failure subscribing — fall through to the no-splash path so the
    // app doesn't sit on an empty splash forever.
    _error.value = String(e)
    _ready.value = true
  }
}

export function useBoot() {
  return {
    phase: readonly(_phase),
    detail: readonly(_detail),
    progress: readonly(_progress),
    error: readonly(_error),
    /** True once boot completes (or in browser-dev mode). */
    ready: readonly(_ready),
    initBoot,
  }
}
