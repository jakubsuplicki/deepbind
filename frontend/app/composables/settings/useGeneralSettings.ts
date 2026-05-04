import { ref } from 'vue'

export type GeneralSettings = {
  workspace_path: string
  voice: { auto_speak: string | boolean; tts_voice: string }
}

export function useGeneralSettings() {
  const loaded = ref(false)
  const workspacePath = ref('')
  const autoSpeak = ref(false)
  const error = ref('')

  async function load() {
    try {
      const resp = await $fetch<GeneralSettings>(apiUrl('/api/settings'))
      workspacePath.value = resp.workspace_path
      autoSpeak.value = resp.voice.auto_speak === 'true' || resp.voice.auto_speak === true
      loaded.value = true
    } catch {
      loaded.value = true
      error.value = 'Failed to load settings'
    }
  }

  async function updateVoice(): Promise<void> {
    try {
      await $fetch(apiUrl('/api/settings/voice'), {
        method: 'PATCH',
        body: { auto_speak: String(autoSpeak.value) },
      })
    } catch { /* ignore */ }
  }

  async function reindexMemory(): Promise<number | null> {
    try {
      const resp = await $fetch<{ indexed: number }>(apiUrl('/api/memory/reindex'), { method: 'POST' })
      return resp.indexed
    } catch { return null }
  }

  async function rebuildGraph(): Promise<boolean> {
    try {
      await $fetch(apiUrl('/api/graph/rebuild'), { method: 'POST' })
      return true
    } catch { return false }
  }

  return {
    loaded, workspacePath, autoSpeak, error,
    load, updateVoice, reindexMemory, rebuildGraph,
  }
}
