declare global {
  interface Window {
    __JARVIS_CONFIG__?: {
      backendUrl: string
      wsUrl: string
    }
    /**
     * Initial license/entitlement state, baked in by the Tauri shell at
     * boot via `lib.rs::boot_state_blocking`. Lets the first paint
     * decide between trial-banner / wall / settings without a network
     * round-trip. May be `null` if the boot probe failed (rare — the
     * shell logs and falls back; the frontend treats null as "state
     * unknown" and shows a small loading hint).
     *
     * Shape mirrors `services.entitlements.EntitlementState`. See
     * `composables/useLicenseState.ts`.
     */
    __JARVIS_LICENSE_STATE__?: LicenseStateSnapshot | null
  }
}

export type LicenseStateName =
  | 'unlicensed_trial_active'
  | 'unlicensed_trial_expiring'
  | 'unlicensed_trial_expired'
  | 'licensed_active'
  | 'licensed_in_grace'
  | 'licensed_past_grace'
  | 'licensed_invalid'
  | 'clock_invalid'

export interface LicenseStateSnapshot {
  state: LicenseStateName
  is_functional: boolean
  is_read_only: boolean
  claims: Record<string, unknown> | null
  days_remaining: number
  trial_started_at: string | null
  expires_at: string | null
  customer: string | null
  reason: string | null
  license_id: string | null
}

export function apiUrl(path: string): string {
  if (typeof window !== 'undefined' && window.__JARVIS_CONFIG__?.backendUrl) {
    return window.__JARVIS_CONFIG__.backendUrl + path
  }
  return path
}

export {}
