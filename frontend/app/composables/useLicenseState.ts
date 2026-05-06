/**
 * useLicenseState — single source of truth for the license/entitlement
 * state in the frontend (ADR 019, chunk 4).
 *
 * The Tauri shell pushes the initial state into `__JARVIS_LICENSE_STATE__`
 * at boot via `lib.rs::boot_state_blocking`, so the first paint can
 * decide between trial-banner / wall / settings without a network
 * round-trip. Subsequent state changes are surfaced by:
 *
 *   - `refresh()` — re-reads from disk + keychain via the
 *     `license_get_state` Tauri command, used after any installer/clear
 *     action and on focus-back.
 *   - `installFromText(text)` — paste-a-key flow. Validates with the
 *     sidecar; if accepted, atomically writes to disk and returns the
 *     new state.
 *   - `clearLicense()` — Settings → "Reset license" affordance.
 *   - `openDataFolder(path)` — past-grace "Open in Finder/Explorer".
 *
 * Singleton via `useState` so every consumer reads/writes the same
 * reactive ref. The composable mirrors the shape used by other
 * licence-aware bits of the app (`useChat`, `useSpecialists`).
 *
 * In dev / web mode (no Tauri shell), the commands return mock data
 * that the dev workflow can use to render the UI without the shell.
 */

import { computed, type ComputedRef } from 'vue'
import type { LicenseStateName, LicenseStateSnapshot } from '~/utils/apiUrl'

const MOCK_DEV_STATE: LicenseStateSnapshot = {
  state: 'unlicensed_trial_active',
  is_functional: true,
  is_read_only: false,
  claims: null,
  days_remaining: 30,
  trial_started_at: new Date().toISOString(),
  expires_at: null,
  customer: null,
  reason: null,
  license_id: null,
}

function readBootState(): LicenseStateSnapshot | null {
  if (typeof window === 'undefined') return null
  const baked = window.__JARVIS_LICENSE_STATE__
  return baked ?? null
}

async function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  // Dev / browser mode has no Tauri runtime — return null so callers
  // can fall back to mock state.
  if (typeof window === 'undefined') return null
  if (!('__TAURI_INTERNALS__' in window)) return null
  try {
    const { invoke } = await import('@tauri-apps/api/core')
    return (await invoke(cmd, args)) as T
  } catch (e) {
    console.error(`[license] tauri invoke '${cmd}' failed:`, e)
    return null
  }
}

export function useLicenseState() {
  // useState<T> persists across navigation per Nuxt convention.
  const state = useState<LicenseStateSnapshot | null>('license-state', () => {
    return readBootState() ?? MOCK_DEV_STATE
  })

  /** True for unlicensed_trial_expired / licensed_invalid (full wall). */
  const showActivationWall: ComputedRef<boolean> = computed(() => {
    const s = state.value?.state
    return s === 'unlicensed_trial_expired' || s === 'licensed_invalid'
  })

  /** True for licensed_past_grace (read-only wall with Open in Finder). */
  const showPastGraceWall: ComputedRef<boolean> = computed(() => {
    return state.value?.state === 'licensed_past_grace'
  })

  /** True for clock_invalid (system clock behind the floor — diagnostic UI). */
  const showClockInvalidWall: ComputedRef<boolean> = computed(() => {
    return state.value?.state === 'clock_invalid'
  })

  /** True when the wall (any kind) should occlude the app. */
  const isWalled: ComputedRef<boolean> = computed(() => {
    return (
      showActivationWall.value ||
      showPastGraceWall.value ||
      showClockInvalidWall.value
    )
  })

  /** True when the trial-countdown banner should render. */
  const showTrialBanner: ComputedRef<boolean> = computed(() => {
    const s = state.value?.state
    return s === 'unlicensed_trial_active' || s === 'unlicensed_trial_expiring'
  })

  /** True when the in-grace renewal banner should render. */
  const showGraceBanner: ComputedRef<boolean> = computed(() => {
    return state.value?.state === 'licensed_in_grace'
  })

  /**
   * Re-read state from disk + keychain via the Tauri shell. Use after
   * any install/clear action and on window-focus events.
   */
  async function refresh(): Promise<LicenseStateSnapshot | null> {
    const fresh = await tauriInvoke<LicenseStateSnapshot>('license_get_state')
    if (fresh) state.value = fresh
    return fresh
  }

  /**
   * Validate the pasted license text via the sidecar; on success the
   * shell atomically writes it to disk and returns the new state. On
   * failure the state machine returns `licensed_invalid` (the UI shows
   * the diagnostic in `state.reason`) and the file on disk is unchanged.
   *
   * Throws on transport-level errors only; license-validation failures
   * surface as a returned `licensed_invalid` state, not as an exception.
   */
  async function installFromText(text: string): Promise<LicenseStateSnapshot | null> {
    const next = await tauriInvoke<LicenseStateSnapshot>('license_install_text', { text })
    if (next) state.value = next
    return next
  }

  /** Settings → "Reset license". Drops the file; recomputes state. */
  async function clearLicense(): Promise<LicenseStateSnapshot | null> {
    const next = await tauriInvoke<LicenseStateSnapshot>('license_clear')
    if (next) state.value = next
    return next
  }

  /** Past-grace "Open in Finder/Explorer". `path` is the workspace root. */
  async function openDataFolder(path: string): Promise<void> {
    await tauriInvoke<void>('license_open_data_folder', { path })
  }

  return {
    state,
    showActivationWall,
    showPastGraceWall,
    showClockInvalidWall,
    isWalled,
    showTrialBanner,
    showGraceBanner,
    refresh,
    installFromText,
    clearLicense,
    openDataFolder,
  }
}
