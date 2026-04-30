/**
 * useLightweightMode — Settings toggle for ADR 005 §C trigger 3.
 *
 * When on, the chat router pre-flight pins the active local model to the
 * smallest installed entry on the user's tier ladder regardless of memory
 * pressure. Useful when the user is running other RAM-heavy apps and wants
 * chat to "just work" without negotiating with the OS for memory.
 *
 * Reads / writes `/api/settings/lightweight-mode` — same shape as the
 * existing privacy / voice / budget toggles (`{enabled: bool}` body).
 * State is persisted server-side in the workspace preferences.json so the
 * setting survives across launches without per-tab localStorage drift.
 */

import { ref } from 'vue'
import { apiUrl } from '~/utils/apiUrl'

export function useLightweightMode() {
  const enabled = ref<boolean>(false)
  const loaded = ref<boolean>(false)
  const saving = ref<boolean>(false)
  const error = ref<string>('')

  async function load() {
    try {
      const data = await $fetch<{ enabled: boolean }>(apiUrl('/api/settings/lightweight-mode'))
      enabled.value = !!data.enabled
      loaded.value = true
      error.value = ''
    } catch {
      error.value = 'Failed to load lightweight-mode setting'
    }
  }

  async function set(next: boolean) {
    saving.value = true
    error.value = ''
    try {
      const data = await $fetch<{ enabled: boolean }>(apiUrl('/api/settings/lightweight-mode'), {
        method: 'PATCH',
        body: { enabled: next },
      })
      enabled.value = !!data.enabled
    } catch (e: unknown) {
      const err = e as { data?: { detail?: string } }
      error.value = err?.data?.detail ?? 'Failed to update lightweight-mode setting'
      // Re-read so UI reflects what the server actually has.
      await load()
    } finally {
      saving.value = false
    }
  }

  return { enabled, loaded, saving, error, load, set }
}
